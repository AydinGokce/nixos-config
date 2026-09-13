"""Project retained source annotations through exact coordinate-product codons.

This is coordinate coverage, not domain prediction or ORF discovery. Historical
protein derivation evidence is deliberately not used to infer current positions.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from copy import deepcopy
import re

from .common import Error, canonical, keys, require, string
from .library_api import opened, pin
from .library_sequence import (MAX_FEATURES, MAX_METADATA, MAX_VIEW, _attachment,
                               _issue, _json, _list, _parts, _source_alignment, _text)


def _receipt(record, name):
    receipt = next((a for a in record.get('attachments', [])
                    if a['path'] == 'attachments/' + name), None)
    return {key: receipt[key] for key in ('path', 'bytes', 'sha256')} if receipt else None


def _automatic(feature):
    """Exclude explicit generated suggestions, never infer curation from a label.

    Retained GenBank CDS features are allowed. The sequence viewer's computed
    ORFs live elsewhere and are never read by this endpoint.
    """
    provenance = feature.get('provenance')
    fields = [feature, provenance] if isinstance(provenance, dict) else [feature]
    markers = {'auto', 'automatic', 'autodetected', 'auto_detected', 'auto_detect',
               'detected', 'computed', 'generated', 'predicted', 'suggestion',
               'orf', 'orf_scan', 'orf_scanner', 'orf_detection', 'cds_detection'}
    for values in fields:
        for key in ('autodetected', 'auto_detected', 'generated', 'computed', 'suggestion'):
            value = values.get(key)
            if value is True or value == 1 or isinstance(value, str) and value.lower() in {'true', 'yes', '1'}:
                return True
        for key in ('source', 'origin', 'method', 'generator', 'status'):
            value = values.get(key)
            if isinstance(value, str) and value.lower().replace('-', '_').replace(' ', '_') in markers:
                return True
    return isinstance(provenance, str) and provenance.lower().replace('-', '_').replace(' ', '_') in markers


def _color(feature):
    qualifiers = _json(feature.get('qualifiers_json'), {})
    for values in (feature, qualifiers):
        for key in ('color', 'colour', 'ApEinfo_fwdcolor', 'ApEinfo_revcolor'):
            value = values.get(key)
            if isinstance(value, list) and len(value) == 1:
                value = value[0]
            if isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value):
                return value.upper()
    return None


def _merge(segments):
    result = []
    for span in sorted(segments, key=lambda s: (s['start'], s['end'])):
        if result and span['start'] <= result[-1]['end']:
            result[-1]['end'] = max(span['end'], result[-1]['end'])
        else:
            result.append(dict(span))
    return result


def _residue_segments(indices):
    result = []
    for index in sorted(indices):
        if result and index == result[-1]['end']:
            result[-1]['end'] += 1
        else:
            result.append({'start': index, 'end': index + 1})
    return result


def _base_row(feature, index, source):
    ident = feature.get('id')
    if type(ident) is not int and not isinstance(ident, str):
        ident = None
    if isinstance(ident, str):
        ident = ident[:128]
    return {'id': 'source:' + str(index) + ':' + str(ident),
            'label': _text(feature.get('label') or feature.get('type') or 'Feature'),
            'kind': _text(feature.get('type') or 'feature', 80),
            'source_feature_id': ident, 'source_feature_index': index,
            'source_ref': source['ref'], 'issues': []}


def project(record, registry, records, module):
    require(record['identity'].get('molecule_type') == 'protein', 'Select a protein construct')
    try:
        resolved = module.effective_sequence(record, records)
    except ValueError as exc:
        raise Error('library', str(exc)) from exc
    result = {'schema': 1, 'ref': pin(record), 'sha256': record['sha256'],
        **{key: resolved[key] for key in ('available', 'sequence', 'sequence_sha256', 'length')},
        'coordinate_system': 'protein_0based_half_open', 'source': None,
        'features': [], 'excluded': [], 'issues': deepcopy(resolved['issues']),
        'complete': True, 'counts': {'total': 0, 'mapped': 0, 'excluded': 0, 'omitted': 0}}
    if not module.translation.is_derived(record):
        result['issues'].append(_issue('not_coordinate_derived',
            'This protein has no coordinate-derived nucleotide parent; no source domains were inferred.'))
        return _bounded(result)
    alignment = {'issues': []}
    _source_alignment(record, records, resolved, alignment)
    parent = records.get(record['identity']['encoded_by']['construct_ref'], {})
    source = {key: value for key, value in alignment['source'].items()
              if key not in {'sequence', 'complete', 'issues'}}
    source.update(translation=deepcopy(record['identity']['encoded_by']['translation']),
                  annotations_receipt=_receipt(parent, 'annotations.json'),
                  sequence_receipt=_receipt(parent, 'sequence.json'), status='unsupported')
    result['source'] = source
    result['issues'].extend(alignment['issues'])
    if not alignment['codon_positions_complete']:
        result['complete'] = False
        if not alignment['issues']:
            result['issues'].append(_issue('protein_unavailable', 'No reliable codon map is available for this protein.'))
        return _bounded(result)
    annotations = _attachment(registry, parent, 'annotations.json', result['issues'])
    evidence = _attachment(registry, parent, 'sequence.json', result['issues'])
    if annotations is None:
        source['status'] = 'missing' if source['annotations_receipt'] is None else 'unsupported'
        result['complete'] = False
        result['issues'].append(_issue('annotations_missing', 'The pinned parent has no readable retained annotations.'))
        return _bounded(result)
    features = _list(annotations, 'sequence_features')
    result['counts']['total'] = len(features)
    source['status'] = ('missing' if evidence is None else
                        'current' if evidence.get('sequence_sha256') == source['sequence_sha256'] else 'stale')
    if source['status'] != 'current':
        result['complete'] = False
        result['issues'].append(_issue('annotations_' + source['status'],
            'Retained annotations are not bound to the pinned parent sequence; no historical coordinates were projected.'))
    # Reverse index only the displayed peptide's exact codons. It naturally
    # preserves reverse strands, joined segments, origin wraps, frame and crop.
    positions = sorted((position, residue) for residue, triplet in enumerate(alignment['codon_positions'])
                       for position in triplet)
    coordinates = [position for position, _ in positions]
    identifiers = Counter(str(f.get('id')) for f in features if isinstance(f, dict) and f.get('id') is not None)
    metadata_size = 0
    for index, feature in enumerate(features[:MAX_FEATURES]):
        feature = feature if isinstance(feature, dict) else {}
        row = _base_row(feature, index, source)
        segments, strand, fuzzy, unsupported = _parts(feature, source['length'])
        row.update(source_strand=strand, source_segments=segments)
        reason = None
        if source['status'] != 'current':
            reason = 'annotations_' + source['status']
        elif _automatic(feature):
            reason = 'autodetected'
        elif feature.get('id') is not None and identifiers[str(feature['id'])] > 1:
            reason = 'ambiguous_source_id'
        elif unsupported:
            reason = 'unsupported_coordinates'
        elif fuzzy:
            reason = 'fuzzy_coordinates'
        elif strand != source['translation']['strand']:
            reason = 'opposite_strand'
        elif row['kind'].lower() == 'source':
            reason = 'sequence_record'
        if reason is None:
            coverage = Counter()
            nucleotide_count = 0
            for span in _merge(segments):
                nucleotide_count += span['end'] - span['start']
                left, right = bisect_left(coordinates, span['start']), bisect_left(coordinates, span['end'])
                coverage.update(residue for _, residue in positions[left:right])
            complete = [residue for residue, count in coverage.items() if count == 3]
            partial = sum(count != 3 for count in coverage.values())
            outside = nucleotide_count - sum(coverage.values())
            if partial:
                row['issues'].append(_issue('partial_codon',
                    f'{partial} overlapping codon(s) are not fully covered and were excluded.'))
            if outside:
                row['issues'].append(_issue('outside_product',
                    f'{outside} source base(s) lie outside this displayed protein, including any stop codon or crop.'))
            if complete:
                row.update(segments=_residue_segments(complete), status='clipped' if partial or outside else 'mapped')
                color = _color(feature)
                if color:
                    row['color'] = color
            else:
                reason = 'partial_codon' if partial else 'outside_product'
        if reason:
            row['reason'] = reason
        size = len(canonical(row))
        if metadata_size + size > MAX_METADATA:
            break
        metadata_size += size
        result['excluded' if reason else 'features'].append(row)
    result['counts'].update(mapped=len(result['features']), excluded=len(result['excluded']))
    result['counts']['omitted'] = len(features) - result['counts']['mapped'] - result['counts']['excluded']
    if result['counts']['omitted']:
        result['complete'] = False
        result['issues'].append(_issue('annotation_limit',
            'Some source annotations exceed the bounded feature/metadata response; inspect the retained attachment.'))
    return _bounded(result)


def _bounded(result):
    # Never return a truncated peptide with its complete sequence digest.
    require(len(canonical(result)) <= MAX_VIEW, 'Protein domains exceed the interactive response size limit', 'limit')
    return result


def get_domains(api, params):
    keys(params, ('ref',))
    string(params['ref'], 'ref', 256)
    with opened(api) as (module, registry, records):
        reference = registry._resolve(params['ref'], records, 'construct')
        return project(records[reference], registry, records, module)
