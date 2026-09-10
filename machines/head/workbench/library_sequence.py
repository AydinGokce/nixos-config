"""Bounded sequence, original-annotation and coordinate-product views.

ORFs are suggestions over the exact displayed nucleotide revision. Only the
library's shared resolver translates a selected product; scans return metadata.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import itertools
import json

from .common import Error, WIRE, canonical, keys, number, require, string
from .library_api import opened, pin

MAX_FEATURES = 1000
MAX_ORFS = 250
MAX_SCAN_BASES = 1_000_000
MAX_SOURCE_BASES = 1_000_000
MAX_ALIGNED_RESIDUES = 16_384
MAX_ATTACHMENT = 16 * 1024 * 1024
MAX_METADATA = 256 * 1024
MAX_VIEW = WIRE - 64 * 1024
COMPLEMENT = str.maketrans('ACGTRYSWKMBDHVN', 'TGCAYRSWMKVHDBN')


def _issue(code, message):
    return {'code': code, 'message': message}


def _text(value, limit=240):
    return value[:limit] if isinstance(value, str) else ''


def _json(value, fallback):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _list(document, field):
    value = document.get(field, [])
    require(isinstance(value, list), 'Invalid source annotation list: ' + field, 'integrity')
    return value


def _attachment(registry, record, name, issues):
    receipt = next((a for a in record.get('attachments', []) if a['path'] == 'attachments/' + name), None)
    if receipt is None:
        return None
    if receipt['bytes'] > MAX_ATTACHMENT:
        issues.append(_issue('annotation_limit', name + ' exceeds the annotation display limit; export the original attachment.'))
        return None
    try:
        raw = (registry._path(pin(record)).parent / receipt['path']).read_bytes()
        require(len(raw) == receipt['bytes'] and hashlib.sha256(raw).hexdigest() == receipt['sha256'],
                'Annotation attachment integrity check failed', 'integrity')
        data = json.loads(raw)
        require(isinstance(data, dict), 'Annotation attachment must contain an object', 'integrity')
        return data
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Error('integrity', 'Annotation attachment is not valid UTF-8 JSON') from exc


def _parts(feature, length):
    original = _json(feature.get('location_parts_json'), [])
    segments, strands = [], set()
    partial = bool(set(_text(feature.get('source_location'), 4096)) & {'<', '>'})
    unsupported = not original or len(original) > 256
    for part in original[:256]:
        if not isinstance(part, dict):
            unsupported = True
            continue
        start, end, strand = part.get('start_0based'), part.get('end_exclusive'), part.get('strand')
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            unsupported = True
            continue
        segments.append({'start': start, 'end': end})
        valid_strand = type(strand) is int and strand in {-1, 1}
        unsupported |= end > length or bool(part.get('ref')) or not valid_strand
        strands.add(strand if valid_strand else 0)
        partial |= any(part.get(key) is not None and not str(part[key]).isdigit()
                       for key in ('start_text', 'end_text'))
    strand = next(iter(strands)) if len(strands) == 1 else 0
    unsupported |= strand not in {-1, 1}
    return segments, strand, partial, unsupported


def _nucleotide_features(record, registry, sequence, module, issues):
    document = _attachment(registry, record, 'annotations.json', issues)
    if document is None:
        return [], False
    source = _attachment(registry, record, 'sequence.json', issues)
    source_sha = source.get('sequence_sha256') if source else None
    actual_sha = hashlib.sha256(sequence.encode()).hexdigest()
    stale = source_sha != actual_sha
    if stale:
        issues.append(_issue('historical_annotations', 'Imported annotations describe a different or unverified source sequence; their old reference matches are historical.'))
    translations = {item['feature_id']: item for item in _list(document, 'protein_translations')
                    if isinstance(item, dict) and type(item.get('feature_id')) is int}
    original = _list(document, 'sequence_features')
    result = []
    for index, feature in enumerate(original[:MAX_FEATURES]):
        if not isinstance(feature, dict):
            continue
        segments, strand, partial, unsupported = _parts(feature, len(sequence))
        ident = feature.get('id')
        translated = translations.get(ident, {}) if type(ident) is int else {}
        partial |= bool(translated.get('incomplete_codon_bases'))
        flags = [_issue(_text(item.get('code'), 80), _text(item.get('detail'), 320))
                 for item in _json(translated.get('issues_json'), [])[:4] if isinstance(item, dict)]
        row = {'id': 'source:' + str(feature.get('id', index)),
               'label': _text(feature.get('label') or feature.get('type') or 'Feature'),
               'kind': _text(feature.get('type') or 'feature', 80), 'strand': strand,
               'segments': segments, 'source': 'imported', 'coordinate_space': 'nucleotide',
               'stale': stale, 'partial': partial, 'unsupported': bool(unsupported),
               'source_location': _text(feature.get('source_location'), 1024),
               'reference_match_status': 'historical' if stale else _text(translated.get('reference_match_status'), 80),
               'review_required': bool(translated.get('review_required')), 'issues': flags}
        if translated and not stale and not unsupported:
            definition = {'schema': 1, 'segments': segments, 'strand': strand,
                'genetic_code': translated.get('translation_table', 1),
                'codon_start': translated.get('codon_start', 1), 'initiation': 'literal',
                'residue_start': 0, 'residue_end': None}
            try:
                module.translation.validate_definition(definition)
                row['translation'] = definition
            except ValueError:
                row['unsupported'] = True
                row['issues'].append(_issue('annotation_translation', 'The imported feature uses an unsupported translation definition.'))
        result.append(row)
    return result, len(original) > MAX_FEATURES


def _protein_features(record, registry, records, resolved, module, issues):
    document = _attachment(registry, record, 'derivation.json', issues)
    if document is None:
        return [], False
    source = document.get('source_product', {})
    require(isinstance(source, dict), 'Invalid source product annotation', 'integrity')
    encoded = record['identity'].get('encoded_by', {})
    derived = module.translation.is_derived(record)
    start, end = 0, resolved.get('length') or 0
    if derived:
        definition = encoded['translation']
        start = definition['residue_start']
        end = start + (resolved.get('length') or 0)
        original = deepcopy(record)
        original['identity']['encoded_by']['translation'].update(residue_start=0, residue_end=None)
        whole = module.effective_sequence(original, records)
        exact = (whole['available'] and whole['sequence_sha256'] == source.get('protein_sha256')
                 and whole.get('coding_sequence_sha256') == source.get('coding_dna_sha256'))
    else:
        exact = resolved['available'] and resolved['sequence_sha256'] == source.get('protein_sha256')
    source_encoded = document.get('encoded_by', {})
    require(isinstance(source_encoded, dict), 'Invalid source product parent annotation', 'integrity')
    exact &= encoded.get('sequence_sha256') == source_encoded.get('sequence_sha256')
    stale = not exact
    if stale:
        issues.append(_issue('historical_annotations', 'Imported protein components describe a predecessor product; their old reference matches are historical.'))
    features = {f['id']: f for f in _list(document, 'features') if isinstance(f, dict) and type(f.get('id')) is int}
    evidence = _list(document, 'evidence')
    result = []
    for item in evidence[:MAX_FEATURES]:
        if not isinstance(item, dict):
            continue
        left, right = item.get('product_start_aa_1based'), item.get('product_end_aa_inclusive')
        if type(left) is not int or type(right) is not int or not 0 < left <= right:
            continue
        left -= 1
        # Crop only exact source mappings. Stale coordinates stay explicitly historical.
        if not stale and (right <= start or left >= end):
            continue
        partial = not stale and (left < start or right > end)
        segments = [{'start': max(left, start) - start, 'end': min(right, end) - start}] if not stale else [{'start': left, 'end': right}]
        ident = item.get('feature_id')
        feature = features.get(ident, {}) if type(ident) is int else {}
        result.append({'id': 'source:' + str(item.get('feature_id')), 'label': _text(feature.get('label') or feature.get('type') or 'Component'),
            'kind': _text(feature.get('type') or 'component', 80), 'strand': 1,
            'segments': segments, 'source': 'imported', 'coordinate_space': 'amino_acid',
            'stale': stale, 'partial': partial, 'unsupported': stale and right > (resolved.get('length') or 0),
            'relationship': _text(item.get('relationship'), 80),
            'reference_match_status': 'historical' if stale else 'exact_match'})
    return result, len(evidence) > MAX_FEATURES


def _position_triplets(definition, offset, count):
    """Map the resolver's coding offsets back to exact genomic coordinates."""
    ranges = (range(part['start'], part['end']) if definition['strand'] == 1
              else range(part['end'] - 1, part['start'] - 1, -1)
              for part in definition['segments'])
    selected = list(itertools.islice(itertools.chain.from_iterable(ranges), offset, offset + 3 * count))
    require(len(selected) == 3 * count, 'Resolved peptide exceeds its coding-coordinate map', 'integrity')
    return [selected[index:index + 3] for index in range(0, len(selected), 3)]


def _alignment_issue(result, code, message, *, omit_source=False):
    issue = _issue(code, message)
    if not any(item['code'] == code for item in result['issues']):
        result['issues'].append(issue)
    result['source']['issues'].append(issue)
    result.update(codon_positions=[], codon_positions_complete=False, terminal_stop_positions=None)
    if omit_source:
        result['source'].update(sequence=None, complete=False)


def _source_alignment(record, records, resolved, result):
    encoded = record['identity']['encoded_by']
    parent = records.get(encoded['construct_ref'], {})
    identity = parent.get('identity', {})
    sequence = identity.get('sequence')
    nucleotide = (parent.get('kind') == 'construct' and identity.get('molecule_type') in {'dna', 'rna'}
                  and isinstance(sequence, str))
    sequence_sha = hashlib.sha256(sequence.encode()).hexdigest() if nucleotide else None
    result.update(source={'ref': encoded['construct_ref'], 'sha256': parent.get('sha256'),
        'sequence_sha256': sequence_sha, 'molecule_type': identity.get('molecule_type'),
        'length': len(sequence) if nucleotide else None, 'circular': identity.get('circular', False),
        'sequence': None, 'complete': False, 'issues': []}, codon_positions=[],
        codon_positions_complete=False, terminal_stop_positions=None)
    if not nucleotide:
        _alignment_issue(result, 'source_unavailable', 'The exact pinned nucleotide source is unavailable.')
        return
    if sequence_sha != encoded['sequence_sha256']:
        _alignment_issue(result, 'source_digest', 'The parent nucleotide sequence differs from its pinned SHA-256.')
        return
    if len(sequence) > MAX_SOURCE_BASES:
        _alignment_issue(result, 'alignment_source_limit',
                         'Source DNA/RNA exceeds the one-million-base alignment limit; export the pinned parent to inspect it.')
        return
    result['source'].update(sequence=sequence, complete=True)
    # A bad frame must still expose its valid source so it can be corrected.
    # Never invent partial peptide letters or an alignment for an unavailable product.
    if not resolved['available']:
        return
    if resolved['length'] > MAX_ALIGNED_RESIDUES:
        _alignment_issue(result, 'alignment_residue_limit',
                         'Codon alignment exceeds the 16384-residue display limit; the complete peptide and source remain available.')
        return
    definition = encoded['translation']
    skip = definition['codon_start'] - 1
    offset = skip + definition['residue_start'] * 3
    result['codon_positions'] = _position_triplets(definition, offset, resolved['length'])
    result['codon_positions_complete'] = True
    # Schema 2 reports the actual first-stop offset. Schema 1 strips only its
    # terminal stop, so its original result shape suffices without changing it.
    uncropped = resolved['uncropped_length']
    stop = resolved.get('terminal_stop_offset')
    if 'terminal_stop_offset' not in resolved and resolved['coding_length'] == 3 * (uncropped + 1):
        stop = 3 * uncropped
    if stop is not None and definition['residue_start'] + resolved['length'] == uncropped:
        result['terminal_stop_positions'] = _position_triplets(definition, skip + stop, 1)[0]


def _segments(start, end, length, strand):
    oriented = [(start, min(end, length))]
    if end > length:
        oriented.append((0, end - length))
    return [{'start': left, 'end': right} if strand == 1 else {'start': length - right, 'end': length - left}
            for left, right in oriented]


def _orfs(sequence, circular, minimum, genetic_code, module):
    sequence = sequence.replace('U', 'T')
    length = len(sequence)
    rows, count = [], 0
    for strand, oriented in ((1, sequence), (-1, sequence.translate(COMPLEMENT)[::-1])):
        scanned = oriented + oriented if circular else oriented
        for frame in range(3):
            stop = None
            last = len(scanned) - 3
            last -= (last - frame) % 3
            for position in range(last, frame - 1, -3):
                codon = scanned[position:position + 3]
                symbol = module.translation.CODONS.get(codon)
                if symbol is None:
                    stop = None
                elif symbol == '*':
                    stop = position
                elif codon == 'ATG' and position < length and stop is not None:
                    amino_acids = (stop - position) // 3
                    if amino_acids < minimum or stop + 3 - position > length:
                        continue
                    count += 1
                    if len(rows) >= MAX_ORFS:
                        continue
                    segments = _segments(position, stop + 3, length, strand)
                    definition = {'schema': 1, 'segments': segments, 'strand': strand,
                        'genetic_code': genetic_code, 'codon_start': 1, 'initiation': 'cds',
                        'residue_start': 0, 'residue_end': None}
                    module.translation.validate_definition(definition)
                    rows.append({'id': f'orf:{strand}:{position}:{stop + 3}:{genetic_code}',
                        'strand': strand, 'segments': segments, 'length_aa': amino_acids,
                        'frame': strand * (frame + 1), 'wraps_origin': len(segments) > 1,
                        'translation': definition})
    rows.sort(key=lambda row: (-row['strand'], row['segments'][0]['start'], row['length_aa']))
    for index, row in enumerate(rows, 1):
        row['label'] = 'ORF ' + str(index)
    return rows, count


def _options(min_orf_aa, genetic_code):
    number(min_orf_aa, 'min_orf_aa', 1, 100_000)
    require(type(genetic_code) is int and genetic_code in {1, 11}, 'genetic_code must be 1 or 11')


def _bound(result, max_bytes=MAX_VIEW):
    def metadata():
        value = {key: value for key, value in result.items() if key not in {'sequence', 'codon_positions'}}
        if 'source' in value:
            value['source'] = {key: value for key, value in value['source'].items() if key != 'sequence'}
        return value

    def fits():
        return len(canonical(metadata())) <= MAX_METADATA and len(canonical(result)) <= max_bytes

    if len(canonical(result)) > max_bytes and result.get('source', {}).get('complete'):
        _alignment_issue(result, 'alignment_response_limit',
                         'Source alignment was omitted to fit this response; use a separate sequence read or export the pinned parent.',
                         omit_source=True)

    if fits():
        return result
    for field in ('features', 'orfs'):
        original = result[field]
        if not original:
            continue
        result[field] = []
        result[field + '_truncated'] = True
        if not fits():
            continue
        # Find a fitting prefix without repeatedly serializing thousands of
        # individual removals from an unusually annotation-dense import.
        low, high = 0, len(original)
        while low < high:
            middle = (low + high + 1) // 2
            result[field] = original[:middle]
            if fits():
                low = middle
            else:
                high = middle - 1
        result[field] = original[:low]
        return result
    raise Error('limit', 'Sequence exceeds the interactive view size limit; export the original record instead.')


def view(record, registry, records, min_orf_aa=30, genetic_code=1, include_sequence=True, max_bytes=MAX_VIEW):
    _options(min_orf_aa, genetic_code)
    number(max_bytes, 'max_bytes', 1, WIRE)
    max_bytes = min(max_bytes, MAX_VIEW)
    module = importlib.import_module(type(registry).__module__)
    identity = record['identity']
    require(record['kind'] == 'construct', 'Sequence view requires a molecular construct')
    try:
        resolved = module.effective_sequence(record, records)
    except ValueError as exc:
        raise Error('library', str(exc)) from exc
    encoded = identity.get('encoded_by', {})
    derived = module.translation.is_derived(record)
    result = {'ref': pin(record), 'molecule_type': identity.get('molecule_type'),
        'length': resolved['length'], 'sequence_sha256': resolved['sequence_sha256'],
        'circular': identity.get('circular', False), 'available': resolved['available'],
        'issues': deepcopy(resolved['issues']), 'derivation_kind': resolved['kind'],
        'parent_ref': encoded.get('construct_ref'), 'translation': deepcopy(encoded.get('translation')),
        'features': [], 'orfs': [], 'min_orf_aa': min_orf_aa, 'genetic_code': genetic_code,
        'features_truncated': False, 'orfs_truncated': False, 'orf_count': 0}
    if include_sequence or derived:
        result['sequence'] = resolved['sequence']
    if resolved['available'] and identity.get('molecule_type') in {'dna', 'rna'}:
        sequence = resolved['sequence']
        result['features'], result['features_truncated'] = _nucleotide_features(record, registry, sequence, module, result['issues'])
        chemistry = any(identity.get(field) for field in ('modifications', 'residues', 'linkages', 'termini', 'bonds', 'crosslinks'))
        if chemistry:
            result['issues'].append(_issue('orf_chemistry', 'Explicit nucleotide chemistry requires interpretation before deriving ORFs.'))
        elif len(sequence) > MAX_SCAN_BASES:
            result['issues'].append(_issue('orf_scan_limit', 'ORF scanning is limited to one million nucleotide bases.'))
        else:
            result['orfs'], result['orf_count'] = _orfs(sequence, result['circular'], min_orf_aa, genetic_code, module)
            result['orfs_truncated'] = result['orf_count'] > MAX_ORFS
    elif identity.get('molecule_type') == 'protein':
        result['features'], result['features_truncated'] = _protein_features(record, registry, records, resolved, module, result['issues'])
    if derived:
        _source_alignment(record, records, resolved, result)
    return _bound(result, max_bytes)


def get_view(api, params):
    keys(params, ('ref',), ('min_orf_aa', 'genetic_code'))
    string(params['ref'], 'ref', 256)
    with opened(api) as (_, registry, records):
        reference = registry._resolve(params['ref'], records, 'construct')
        return view(records[reference], registry, records, params.get('min_orf_aa', 30), params.get('genetic_code', 1))


def preview(api, params):
    keys(params, ('parent_ref', 'translation'))
    string(params['parent_ref'], 'parent_ref', 256)
    with opened(api) as (module, registry, records):
        reference = registry._resolve(params['parent_ref'], records, 'construct')
        parent = records[reference]
        sequence = parent['identity'].get('sequence')
        require(parent['identity'].get('molecule_type') in {'dna', 'rna'} and isinstance(sequence, str),
                'A derived protein requires an explicit nucleotide parent')
        try:
            module.translation.validate_definition(params['translation'])
            source_sha = hashlib.sha256(sequence.encode()).hexdigest()
            candidate = {'kind': 'construct', 'identity': {'molecule_type': 'protein',
                'encoded_by': {'construct_ref': reference, 'sequence_sha256': source_sha,
                               'translation': deepcopy(params['translation'])}}}
            result = module.effective_sequence(candidate, records)
        except ValueError as exc:
            raise Error('invalid', str(exc)) from exc
        response = {'parent_ref': reference, 'parent_sha256': parent['sha256'],
            'source_sequence_sha256': source_sha, 'translation': deepcopy(params['translation']),
            **{key: result.get(key) for key in ('available', 'sequence', 'sequence_sha256', 'length', 'issues')}}
        require(len(canonical(response)) <= MAX_VIEW, 'Preview exceeds the interactive response size limit', 'limit')
        return response
