"""Versioned coordinate-derived proteins; canonical records never contain a second sequence.

Genetic-code data: https://www.ncbi.nlm.nih.gov/Taxonomy/Utils/wprintgc.cgi
Tables 1 and 11 share elongation codons; their initiator sets differ.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import itertools
import json

ENGINE = 'coordinate-translation-v1'
ENGINE_V2 = 'coordinate-translation-v2'
_AMINO = 'FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG'
CODONS = dict(zip((''.join(x) for x in itertools.product('TCAG', repeat=3)), _AMINO))
STARTS = {1: {'TTG', 'CTG', 'ATG'}, 11: {'TTG', 'CTG', 'ATT', 'ATC', 'ATA', 'ATG', 'GTG'}}
_COMPLEMENT = str.maketrans('ACGT', 'TGCA')
FIELDS = {'schema', 'segments', 'strand', 'genetic_code', 'codon_start', 'initiation',
          'residue_start', 'residue_end'}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def is_derived(record):
    return isinstance(record.get('identity', {}).get('encoded_by', {}).get('translation'), dict)


def validate_definition(value, *, require=_require):
    require(isinstance(value, dict),
            'translation requires exactly the versioned coordinate, code, frame and residue-range fields')
    schema = value.get('schema')
    fields = FIELDS | {'stop_policy'} if type(schema) is int and schema == 2 else FIELDS
    require(set(value) == fields,
            'translation requires exactly the versioned coordinate, code, frame and residue-range fields')
    require(type(schema) is int and schema in {1, 2}, 'Unsupported translation schema')
    if schema == 2:
        require(isinstance(value['stop_policy'], str) and value['stop_policy'] in {'strict', 'first_stop'},
                'stop_policy must be strict or first_stop')
    require(type(value['strand']) is int and value['strand'] in {-1, 1}, 'Translation strand must be +1 or -1')
    require(type(value['genetic_code']) is int and value['genetic_code'] in STARTS, 'Supported genetic codes are 1 and 11')
    require(type(value['codon_start']) is int and value['codon_start'] in {1, 2, 3}, 'codon_start must be 1, 2 or 3')
    require(isinstance(value['initiation'], str) and value['initiation'] in {'cds', 'literal'}, 'initiation must be cds or literal')
    require(type(value['residue_start']) is int and value['residue_start'] >= 0, 'residue_start must be nonnegative')
    require(value['residue_end'] is None or type(value['residue_end']) is int and value['residue_end'] >= 0,
            'residue_end must be null or a nonnegative exclusive endpoint')
    require(isinstance(value['segments'], list) and len(value['segments']) <= 256, 'translation segments must be a bounded list')
    for segment in value['segments']:
        require(isinstance(segment, dict) and set(segment) == {'start', 'end'} and
                all(type(segment[key]) is int and segment[key] >= 0 for key in segment),
                'Translation segments use nonnegative, zero-based start/end integers')


def frame_definition(definition, offset):
    """Change phase within the exact coding footprint without inventing initiation.

    A same-phase release preserves all versioned semantics, including CDS
    initiator overrides. Callers still receive an independent definition.
    """
    validate_definition(definition)
    _require(type(offset) is int and offset in {0, 1, 2}, 'frame offset must be 0, 1 or 2')
    result = deepcopy(definition)
    if offset != definition['codon_start'] - 1:
        result.update(schema=2, codon_start=offset + 1, initiation='literal', stop_policy='first_stop')
    return result


def effective_sequence(record, records):
    identity = record.get('identity', {})
    encoded = identity.get('encoded_by', {})
    derived = is_derived(record)
    result = {'kind': 'derived' if derived else 'explicit', 'available': False, 'sequence': None,
              'sequence_sha256': None, 'length': None, 'issues': [],
              'source_ref': encoded.get('construct_ref') if derived else None}

    def fail(code, message):
        result['issues'].append({'code': code, 'message': message})
        return result

    if not derived:
        sequence = identity.get('sequence')
        if isinstance(sequence, str) and sequence:
            result.update(available=True, sequence=sequence, sequence_sha256=digest(sequence), length=len(sequence))
        return result
    definition = encoded['translation']
    validate_definition(definition)
    first_stop = definition['schema'] == 2 and definition['stop_policy'] == 'first_stop'
    if identity.get('molecule_type') != 'protein' or 'sequence' in identity or 'residues' in identity:
        return fail('ambiguous_definition', 'A derived protein must have coordinates instead of a stored sequence or residues.')
    source = records.get(encoded.get('construct_ref'), {})
    source_identity = source.get('identity', {})
    sequence = source_identity.get('sequence')
    if source.get('kind') != 'construct' or source_identity.get('molecule_type') not in {'dna', 'rna'} or not isinstance(sequence, str):
        return fail('source_unavailable', 'The pinned source nucleotide sequence is unavailable.')
    if digest(sequence) != encoded.get('sequence_sha256'):
        return fail('source_digest', 'The source sequence differs from its pinned SHA-256.')
    if any(source_identity.get(field) for field in ('modifications', 'residues', 'linkages', 'termini', 'bonds', 'crosslinks')):
        return fail('source_chemistry', 'Explicit source chemistry needs an interpretation before translation.')
    segments = definition['segments']
    if not segments or any(not 0 <= segment['start'] < segment['end'] <= len(sequence) for segment in segments):
        return fail('coordinates', 'Coding coordinates are empty or outside the current source sequence.')
    ordered = sorted(segments, key=lambda segment: segment['start'])
    if any(left['end'] > right['start'] for left, right in zip(ordered, ordered[1:])):
        return fail('overlap', 'Coding segments overlap; a source base cannot be translated twice.')
    expected_order = ordered if definition['strand'] == 1 else list(reversed(ordered))
    if segments != expected_order and not source_identity.get('circular', False):
        return fail('segment_order', 'A linear source must list segments in biological strand order.')
    if segments != expected_order:
        # Circular origin crossing permits exactly a rotation of biological order.
        if not any(segments == expected_order[n:] + expected_order[:n] for n in range(len(segments))):
            return fail('segment_order', 'Circular segments must follow the strand with at most one origin crossing.')
    parts = [sequence[s['start']:s['end']].replace('U', 'T') for s in segments]
    if not first_stop and any(set(part) - set('ACGT') for part in parts):
        return fail('ambiguous_codon', 'The coding span contains ambiguous nucleotides; no residues were guessed.')
    if definition['strand'] == -1:
        parts = [part.translate(_COMPLEMENT)[::-1] for part in parts]
    coding = ''.join(parts)[definition['codon_start'] - 1:]
    result.update(coding_length=len(coding), coding_sequence_sha256=digest(coding),
                  engine_version=ENGINE if definition['schema'] == 1 else ENGINE_V2)
    if first_stop:
        result.update(terminal_stop_offset=None, trailing_bases=len(coding) % 3)
    if not coding or (not first_stop and len(coding) % 3) or (first_stop and len(coding) < 3):
        return fail('incomplete_codon', 'The selected coding span does not contain complete codons.')
    if first_stop:
        translated = []
        for index in range(0, len(coding) - len(coding) % 3, 3):
            amino = CODONS.get(coding[index:index + 3])
            if amino is None:
                return fail('ambiguous_codon', 'An encountered codon contains ambiguous nucleotides; no residues were guessed.')
            translated.append(amino)
            if amino == '*':
                result['terminal_stop_offset'] = index
                break
        protein = ''.join(translated)
    else:
        protein = ''.join(CODONS[coding[index:index + 3]] for index in range(0, len(coding), 3))
    if definition['initiation'] == 'cds':
        if coding[:3] not in STARTS[definition['genetic_code']]:
            return fail('initiation', 'The first codon is not an initiator under the selected genetic code.')
        if not protein.endswith('*'):
            return fail('terminal_stop', 'The declared CDS has no terminal stop codon.')
        protein = 'M' + protein[1:]
    if protein.endswith('*'):
        protein = protein[:-1]
    if '*' in protein:
        return fail('internal_stop', 'A stop codon occurs inside the declared coding span.')
    start, end = definition['residue_start'], definition['residue_end']
    end = len(protein) if end is None else end
    result['uncropped_length'] = len(protein)
    if first_stop and not protein:
        return fail('empty_product', 'The selected frame terminates before producing any amino acids.')
    if not 0 <= start < end <= len(protein):
        return fail('residue_range', 'The requested protein residue range is empty or outside the translated product.')
    protein = protein[start:end]
    result.update(available=True, sequence=protein, sequence_sha256=digest(protein), length=len(protein))
    return result


def remap_translation(definition, start, end, replacement_length):
    """Map an explicit source splice; insertion at a feature boundary stays outside.

    Interior insertions expand the feature; complete deletions collapse its span,
    which remains a visible unavailable definition rather than an old sequence.
    """
    validate_definition(definition)
    _require(all(type(x) is int and x >= 0 for x in (start, end, replacement_length)) and start <= end,
             'Invalid source splice')
    result = deepcopy(definition)
    shift = replacement_length - (end - start)
    if shift == 0:
        return result  # Substitutions never move coordinates, even a broad text replacement.
    intersects_coding = any((start == end and segment['start'] < start < segment['end']) or
                            (start < segment['end'] and end > segment['start']) for segment in result['segments'])
    ambiguous_boundary = any(start < segment[key] < end for segment in result['segments'] for key in ('start', 'end'))
    cropped = definition['residue_start'] != 0 or definition['residue_end'] is not None
    if ambiguous_boundary or (cropped and intersects_coding):
        # A broad replacement does not specify where interior landmarks moved.
        # A cropped variant additionally needs residue-boundary remapping. Retain
        # a visible unavailable definition instead of selecting different residues.
        result['segments'] = []
        return result
    for segment in result['segments']:
        left, right = segment['start'], segment['end']
        if start == end:
            segment['start'] = left + (replacement_length if left >= start else 0)
            segment['end'] = right + (replacement_length if right > start else 0)
        else:
            segment['start'] = left if left <= start else left + shift if left >= end else start
            segment['end'] = right if right <= start else right + shift if right >= end else start + replacement_length
        segment['end'] = max(segment['start'], segment['end'])
    return result


def infer_splice(before, after):
    """Exact contiguous replacement bounded by the unchanged prefix and suffix."""
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    suffix = 0
    while suffix < min(len(before), len(after)) - start and before[-suffix - 1] == after[-suffix - 1]:
        suffix += 1
    return {'start': start, 'end': len(before) - suffix, 'replacement_length': len(after) - start - suffix}


def projection(record, records):
    result = effective_sequence(record, records)
    encoded = record['identity']['encoded_by']
    return {**result, 'source_sequence_sha256': encoded['sequence_sha256'],
            'derivation_sha256': digest(json.dumps(encoded['translation'], sort_keys=True, separators=(',', ':'))),
            'engine_version': ENGINE if encoded['translation']['schema'] == 1 else ENGINE_V2}


def materialized_identity(record, snapshot):
    """Model-only projection, never substituted into the canonical hashed record."""
    identity = deepcopy(record['identity'])
    if is_derived(record):
        ref = f"{record['kind']}:{record['id']}@{record['revision']}"
        result = projection(record, snapshot.get('derivation_sources', {}))
        _require(snapshot.get('resolved_polymers', {}).get(ref) == result,
                 'Derived sequence projection differs from its pinned sources or translation engine')
        _require(result['available'], '; '.join(issue['message'] for issue in result['issues']))
        identity['sequence'] = result['sequence']
    _require(identity.get('product_review', {}).get('status') != 'review_required',
             'Protein product definition requires review before prediction')
    identity.pop('encoded_by', None)
    identity.pop('product_review', None)
    return identity
