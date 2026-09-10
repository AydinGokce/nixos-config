"""Exact-revision sequence displays and read-only coordinate product previews."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import tempfile
import unittest

from workbench.api import API
from workbench.common import Error, WIRE, canonical
from workbench.inputs import library
from workbench import library_sequence as sequence_api
from workbench.library_api import opened, pin
from workbench.store import Store


def digest(sequence):
    return hashlib.sha256(sequence.encode()).hexdigest()


def definition(segments=None, **updates):
    value = {'schema': 1, 'segments': [{'start': 0, 'end': 9}] if segments is None else segments,
             'strand': 1, 'genetic_code': 1, 'codon_start': 1, 'initiation': 'cds',
             'residue_start': 0, 'residue_end': None}
    return {**value, **updates}


def reverse(sequence):
    return sequence.translate(sequence_api.COMPLEMENT)[::-1]


def feature(ident, segments, strand=1, **extra):
    parts = [{'start_0based': left, 'end_exclusive': right, 'strand': strand,
              'start_text': str(left), 'end_text': str(right), 'ref': None} for left, right in segments]
    return {'id': ident, 'label': 'Component ' + str(ident), 'type': 'CDS',
            'source_location': 'join(...)', 'location_parts_json': json.dumps(parts), **extra}


class LibrarySequenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.tools = Path(__file__).absolute().parents[1]
        self.module = library(self.tools)
        self.registry = self.module.Registry(self.base / 'library'); self.registry.init()
        self.store = Store(self.base / 'workbench')
        self.api = API(self.store, 'alice', library_config={
            'tools_dir': str(self.tools), 'library_root': str(self.registry.root)})
        self.serial = 0

    def attachments(self, documents):
        paths = {}
        for name, value in documents.items():
            self.serial += 1
            path = self.base / ('attachment-' + str(self.serial))
            path.write_bytes(canonical(value)); paths[name] = path
        return paths

    def parent(self, sequence='ATGGCTTAA', ident='parent', circular=False, documents=None, **identity):
        return self.registry.import_record({'kind': 'construct', 'id': ident,
            'identity': {'molecule_type': 'dna', 'sequence': sequence, 'circular': circular, **identity}},
            self.attachments(documents or {}))

    def product(self, parent, value=None, ident='product', documents=None, **identity):
        return self.registry.import_record({'kind': 'construct', 'id': ident,
            'identity': {'molecule_type': 'protein', 'encoded_by': {
                'construct_ref': pin(parent), 'sequence_sha256': digest(parent['identity']['sequence']),
                'translation': definition() if value is None else value}, **identity}},
            self.attachments(documents or {}))

    def scan(self, sequence, circular=False, minimum=1, code=1):
        return sequence_api._orfs(sequence, circular, minimum, code, self.module)

    def resolved_orf(self, sequence, row, circular=False):
        parent = {'kind': 'construct', 'identity': {'molecule_type': 'dna', 'sequence': sequence, 'circular': circular}}
        product = {'kind': 'construct', 'identity': {'molecule_type': 'protein', 'encoded_by': {
            'construct_ref': 'construct:source@1', 'sequence_sha256': digest(sequence), 'translation': row['translation']}}}
        return self.module.effective_sequence(product, {'construct:source@1': parent})

    def test_explicit_view_has_one_sequence_and_embedded_view_omits_duplicate(self):
        parent = self.parent()
        with opened(self.api) as (_, registry, records):
            embedded = sequence_api.view(parent, registry, records, min_orf_aa=1, include_sequence=False)
        self.assertNotIn('sequence', embedded)
        full = sequence_api.get_view(self.api, {'ref': 'parent', 'min_orf_aa': 1})
        self.assertEqual(full['sequence'], 'ATGGCTTAA')
        self.assertEqual(full['sequence_sha256'], digest('ATGGCTTAA'))
        self.assertEqual(full['ref'], 'construct:parent@1')
        self.assertEqual(full['derivation_kind'], 'explicit')
        self.assertEqual(full['length'], 9)
        self.assertEqual(full['orf_count'], 1)
        self.assertNotIn('sequence', full['orfs'][0])
        self.assertEqual(self.api.call('batch.list', {})['batches'], [])

    def test_six_linear_frames_resolve_through_shared_engine(self):
        coding = 'ATGGCTGGGTAA'
        for frame in range(3):
            for strand in (1, -1):
                oriented = 'C' * frame + coding + 'CC'
                sequence = oriented if strand == 1 else reverse(oriented)
                with self.subTest(frame=frame, strand=strand):
                    rows, _ = self.scan(sequence)
                    row = next(r for r in rows if r['strand'] == strand and r['frame'] == strand * (frame + 1))
                    result = self.resolved_orf(sequence, row)
                    self.assertTrue(result['available'], result)
                    self.assertEqual(result['sequence'], 'MAG')
                    self.assertEqual(row['length_aa'], 3)
                    self.assertFalse(row['wraps_origin'])

    def test_circular_origin_in_each_phase_and_both_strands(self):
        coding = 'ATGGCTGGGTAA'
        for padding in range(3):
            for shift in range(1, len(coding)):
                oriented = coding[shift:] + 'C' * padding + coding[:shift]
                for strand in (1, -1):
                    sequence = oriented if strand == 1 else reverse(oriented)
                    with self.subTest(padding=padding, shift=shift, strand=strand):
                        rows, _ = self.scan(sequence, circular=True)
                        matches = [r for r in rows if r['strand'] == strand and r['length_aa'] == 3 and r['wraps_origin']]
                        self.assertEqual(len(matches), 1, rows)
                        row = matches[0]
                        result = self.resolved_orf(sequence, row, circular=True)
                        self.assertTrue(result['available'], result)
                        self.assertEqual(result['sequence'], 'MAG')
                        self.assertEqual(sum(s['end'] - s['start'] for s in row['segments']), len(coding))

    def test_first_stop_ambiguity_and_no_more_than_one_circuit(self):
        rows, _ = self.scan('ATGATGGCTTAAGGGTAATAG')
        forward = [r for r in rows if r['strand'] == 1 and r['frame'] == 1]
        self.assertEqual([r['length_aa'] for r in forward], [3, 2])
        self.assertTrue(all(r['segments'][-1]['end'] == 12 for r in forward))
        self.assertEqual(self.scan('ATGNNNTAA')[0], [])
        self.assertEqual(self.scan('ATGAAA', circular=True)[0], [])
        self.assertEqual(self.scan('ATGAA', circular=True)[0], [])
        for tiny in ('', 'A', 'AT'):
            self.assertEqual(self.scan(tiny, circular=True), ([], 0))

    def test_orf_metadata_is_bounded_and_atg_only_for_both_supported_codes(self):
        sequence = 'ATGGCTTAA' * 400
        rows, count = self.scan(sequence, minimum=2, code=11)
        self.assertGreater(count, sequence_api.MAX_ORFS)
        self.assertEqual(len(rows), sequence_api.MAX_ORFS)
        self.assertTrue(all(r['translation']['genetic_code'] == 11 for r in rows))
        self.assertTrue(all('sequence' not in r and 'protein' not in r for r in rows))
        self.assertEqual(self.scan('GTGGCTTAA', code=11)[0], [])
        parent = self.parent('GTGGCTTAA')
        preview = sequence_api.preview(self.api, {'parent_ref': pin(parent), 'translation': definition(genetic_code=11)})
        self.assertTrue(preview['available']); self.assertEqual(preview['sequence'], 'MA')
        limited = sequence_api.get_view(self.api, {'ref': 'parent', 'min_orf_aa': 30})
        self.assertEqual(limited['orfs'], [])

    def test_random_orf_candidates_are_complete_exact_coordinate_products(self):
        rng = random.Random(2342)
        for circular in (False, True):
            for _ in range(80):
                sequence = ''.join(rng.choices('ACGTN', weights=[4, 4, 4, 4, 1], k=rng.randrange(9, 150)))
                rows, _ = self.scan(sequence, circular=circular)
                expected = set()
                # Deliberately slower, start-by-start oracle also detects missed
                # candidates; it does not share the backward stop-index scan.
                for strand, oriented in ((1, sequence), (-1, reverse(sequence))):
                    scanned = oriented * 2 if circular else oriented
                    for start in range(len(sequence)):
                        if scanned[start:start + 3] != 'ATG':
                            continue
                        boundary = start + len(sequence) if circular else len(sequence)
                        for stop in range(start + 3, boundary - 2, 3):
                            codon = scanned[stop:stop + 3]
                            if set(codon) - set('ACGT'):
                                break
                            if codon in {'TAA', 'TAG', 'TGA'}:
                                expected.add((strand, start, stop + 3))
                                break
                actual = {tuple(int(part) for part in row['id'].split(':')[1:4]) for row in rows}
                self.assertEqual(actual, expected, (sequence, circular))
                for row in rows:
                    result = self.resolved_orf(sequence, row, circular)
                    self.assertTrue(result['available'], (sequence, row, result))
                    self.assertEqual(result['length'], row['length_aa'])
                    self.assertEqual(result['sequence'][0], 'M')
                    self.assertNotIn('*', result['sequence'])

    def test_annotations_keep_reverse_join_order_fuzzy_and_unsupported_flags(self):
        source = 'AGCCATGGGTTA'
        fuzzy = feature(1, [(0, 6), (9, 12)], -1, source_location='complement(join(<10..12,1..6))')
        remote = feature(2, [(0, 6)], 1)
        remote['location_parts_json'] = json.dumps([{'start_0based': 0, 'end_exclusive': 6, 'strand': 1, 'ref': 'remote'}])
        parent = self.parent(source, circular=True, documents={
            'sequence.json': {'sequence_sha256': digest(source)},
            'annotations.json': {'sequence_features': [fuzzy, remote, feature(3, [(9, 20)])],
                'protein_translations': [{'feature_id': 1, 'translation_table': 1, 'codon_start': 1,
                    'reference_match_status': 'exact_match', 'issues_json': '[]'}]}})
        result = sequence_api.get_view(self.api, {'ref': pin(parent)})
        first, second, third = result['features']
        self.assertEqual(first['segments'], [{'start': 0, 'end': 6}, {'start': 9, 'end': 12}])
        self.assertEqual(first['strand'], -1)
        self.assertTrue(first['partial']); self.assertFalse(first['stale']); self.assertFalse(first['unsupported'])
        self.assertEqual(first['reference_match_status'], 'exact_match')
        self.assertEqual(first['translation']['segments'], first['segments'])
        self.assertTrue(second['unsupported']); self.assertTrue(third['unsupported'])
        self.assertNotIn('translation', second)

    def test_retained_annotation_matches_become_historical_after_parent_edit(self):
        source = 'ATGGCTTAA'
        parent = self.parent(source, documents={'sequence.json': {'sequence_sha256': digest(source)},
            'annotations.json': {'sequence_features': [feature(1, [(0, 9)])],
                'protein_translations': [{'feature_id': 1, 'reference_match_status': 'exact_match'}]}})
        self.registry.revise('parent', {'identity': {**parent['identity'], 'sequence': 'ATGGGTTAA'}})
        old = sequence_api.get_view(self.api, {'ref': pin(parent)})
        latest = sequence_api.get_view(self.api, {'ref': 'parent'})
        self.assertFalse(old['features'][0]['stale'])
        self.assertEqual(old['features'][0]['reference_match_status'], 'exact_match')
        self.assertTrue(latest['features'][0]['stale'])
        self.assertEqual(latest['features'][0]['reference_match_status'], 'historical')
        self.assertNotIn('translation', latest['features'][0])
        self.assertIn('historical_annotations', [i['code'] for i in latest['issues']])

    def test_malformed_annotation_types_are_flagged_or_fail_as_integrity_errors(self):
        odd = feature(1, [(0, 9)])
        odd['location_parts_json'] = '{"wrong":"shape"}'
        self.parent(documents={'sequence.json': {'sequence_sha256': digest('ATGGCTTAA')},
            'annotations.json': {'sequence_features': [odd, feature(2, [(0, 9)])],
                'protein_translations': [{'feature_id': 2, 'translation_table': 999, 'issues_json': '{}'}]}})
        result = sequence_api.get_view(self.api, {'ref': 'parent'})
        self.assertTrue(all(r['unsupported'] for r in result['features']))
        self.assertTrue(all('translation' not in r for r in result['features']))
        self.parent(ident='bad', documents={'annotations.json': {'sequence_features': {}}})
        with self.assertRaises(Error) as raised:
            sequence_api.get_view(self.api, {'ref': 'bad'})
        self.assertEqual(raised.exception.code, 'integrity')

    def product_evidence(self, parent):
        return {'derivation.json': {
            'source_product': {'protein_sha256': digest('MAG'), 'coding_dna_sha256': digest('ATGGCTGGGTAA')},
            'encoded_by': {'construct_ref': pin(parent), 'sequence_sha256': digest(parent['identity']['sequence'])},
            'features': [feature(1, [(0, 12)]), feature(2, [(3, 12)])],
            'evidence': [{'feature_id': 1, 'product_start_aa_1based': 1, 'product_end_aa_inclusive': 3,
                'relationship': 'whole_reference'}, {'feature_id': 2, 'product_start_aa_1based': 2,
                'product_end_aa_inclusive': 3, 'relationship': 'component_reference'}]}}

    def test_derived_sequence_and_original_protein_components_follow_exact_crop(self):
        parent = self.parent('ATGGCTGGGTAA')
        product = self.product(parent, definition([{'start': 0, 'end': 12}], residue_start=1, residue_end=3),
            documents=self.product_evidence(parent))
        with opened(self.api) as (_, registry, records):
            result = sequence_api.view(product, registry, records, include_sequence=False)
        self.assertEqual(result['sequence'], 'AG')
        self.assertEqual(result['derivation_kind'], 'derived')
        self.assertEqual(result['parent_ref'], pin(parent))
        self.assertEqual(result['features'][0]['segments'], [{'start': 0, 'end': 2}])
        self.assertTrue(result['features'][0]['partial'])
        self.assertFalse(result['features'][1]['partial'])
        self.assertTrue(all(not row['stale'] for row in result['features']))
        self.assertNotIn('sequence', self.registry.show('product')['identity'])

    def test_unavailable_derived_product_never_displays_old_peptide_or_reference_matches(self):
        parent = self.parent('ATGGCTGGGTAA')
        product = self.product(parent, definition([{'start': 0, 'end': 12}]), documents=self.product_evidence(parent))
        self.registry.revise('parent', {'identity': {**parent['identity'], 'sequence': 'ATGNNNGGGTAA'}})
        result = sequence_api.get_view(self.api, {'ref': 'product'})
        self.assertFalse(result['available']); self.assertIsNone(result['sequence'])
        self.assertEqual(result['parent_ref'], 'construct:parent@2')
        self.assertTrue(all(row['stale'] for row in result['features']))
        self.assertTrue(all(row['reference_match_status'] == 'historical' for row in result['features']))
        old = sequence_api.get_view(self.api, {'ref': pin(product)})
        self.assertEqual(old['sequence'], 'MAG'); self.assertTrue(old['available'])
        self.assertTrue(all(not row['stale'] for row in old['features']))

    def test_synonymous_parent_change_makes_imported_reference_evidence_historical(self):
        parent = self.parent('ATGGCTGGGTAA')
        self.product(parent, definition([{'start': 0, 'end': 12}]), documents=self.product_evidence(parent))
        self.registry.revise('parent', {'identity': {**parent['identity'], 'sequence': 'ATGGCCGGGTAA'}})
        result = sequence_api.get_view(self.api, {'ref': 'product'})
        self.assertEqual(result['sequence'], 'MAG')
        self.assertTrue(all(row['stale'] for row in result['features']))

    def test_preview_distinguishes_malformed_schema_from_unavailable_biology_without_writes(self):
        parent = self.parent()
        before = {str(p.relative_to(self.registry.root)): p.read_bytes()
                  for p in self.registry.root.rglob('*') if p.is_file()}
        result = sequence_api.preview(self.api, {'parent_ref': 'parent', 'translation': definition()})
        self.assertEqual(result['sequence'], 'MA')
        self.assertEqual(result['parent_sha256'], parent['sha256'])
        self.assertEqual(result['source_sequence_sha256'], digest('ATGGCTTAA'))
        for value in (definition([]), definition(residue_start=50), definition([{'start': 0, 'end': 8}])):
            result = sequence_api.preview(self.api, {'parent_ref': 'parent', 'translation': value})
            self.assertFalse(result['available']); self.assertIsNone(result['sequence']); self.assertTrue(result['issues'])
        for value in ({}, definition(strand=True), definition(codon_start=4), definition(extra='unknown')):
            with self.subTest(value=value), self.assertRaises(Error) as raised:
                sequence_api.preview(self.api, {'parent_ref': 'parent', 'translation': value})
            self.assertEqual(raised.exception.code, 'invalid')
        after = {str(p.relative_to(self.registry.root)): p.read_bytes()
                 for p in self.registry.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.api.call('batch.list', {})['batches'], [])

    def test_read_options_are_strict_and_pinned(self):
        parent = self.parent()
        for extra in ({'min_orf_aa': True}, {'min_orf_aa': 0}, {'min_orf_aa': 1.0},
                      {'genetic_code': 2}, {'genetic_code': True}, {'extra': 1}):
            with self.subTest(extra=extra), self.assertRaises(Error):
                sequence_api.get_view(self.api, {'ref': pin(parent), **extra})
        self.registry.revise('parent', {'notes': 'A new metadata revision'})
        self.assertEqual(sequence_api.get_view(self.api, {'ref': pin(parent)})['ref'], pin(parent))
        self.assertEqual(sequence_api.get_view(self.api, {'ref': 'parent'})['ref'], 'construct:parent@2')

    def test_sequence_and_metadata_caps_fit_the_rpc_without_silent_sequence_truncation(self):
        source = 'ATGGCTTAA' * 400
        bulky = [feature(i, [(0, 9)], label='L' * 1000, source_location='X' * 3000) for i in range(1100)]
        self.parent(source, documents={'sequence.json': {'sequence_sha256': digest(source)},
            'annotations.json': {'sequence_features': bulky}})
        result = sequence_api.get_view(self.api, {'ref': 'parent', 'min_orf_aa': 2})
        self.assertEqual(result['sequence'], source)
        self.assertTrue(result['features_truncated']); self.assertTrue(result['orfs_truncated'])
        self.assertLessEqual(len(canonical(result)), WIRE)
        self.assertLessEqual(len(canonical({k: v for k, v in result.items() if k != 'sequence'})), sequence_api.MAX_METADATA)
        self.assertGreater(len(result['orfs']), 0)
        # Large imported records remain exportable but are not truncated to fit a viewer response.
        synthetic = {'kind': 'construct', 'id': 'large', 'revision': 1,
            'identity': {'molecule_type': 'dna', 'sequence': 'A' * WIRE}, 'attachments': []}
        with self.assertRaises(Error) as raised:
            sequence_api.view(synthetic, self.registry, {})
        self.assertEqual(raised.exception.code, 'limit')

    def test_retained_attachment_integrity_is_checked_before_rendering(self):
        parent = self.parent(documents={'annotations.json': {'sequence_features': []}})
        path = self.registry._path(pin(parent)).parent / 'attachments/annotations.json'
        path.write_text('{"changed":true}')
        with self.assertRaisesRegex(Error, 'integrity'):
            sequence_api.get_view(self.api, {'ref': pin(parent)})

    def test_alignment_uses_exact_source_triplets_for_reverse_join_and_origin_crossing(self):
        cases = [
            ('ATGGCTTAA', definition(), False, [[0, 1, 2], [3, 4, 5]], [6, 7, 8]),
            ('TTAAGCCAT', definition(strand=-1), False, [[8, 7, 6], [5, 4, 3]], [2, 1, 0]),
            ('ATCCGGCTTAA', definition([{'start': 0, 'end': 2}, {'start': 4, 'end': 11}]),
             False, [[0, 1, 4], [5, 6, 7]], [8, 9, 10]),
            ('GGCTTAAAT', definition([{'start': 7, 'end': 9}, {'start': 0, 'end': 7}]),
             True, [[7, 8, 0], [1, 2, 3]], [4, 5, 6]),
            (reverse('GGCTTAAAT'), definition([{'start': 0, 'end': 2}, {'start': 2, 'end': 9}], strand=-1),
             True, [[1, 0, 8], [7, 6, 5]], [4, 3, 2]),
            ('CATGGCTTAA', definition([{'start': 0, 'end': 10}], codon_start=2),
             False, [[1, 2, 3], [4, 5, 6]], [7, 8, 9]),
        ]
        for index, (sequence, value, circular, positions, stop) in enumerate(cases):
            with self.subTest(sequence=sequence, definition=value):
                parent = self.parent(sequence, ident='source-' + str(index), circular=circular)
                product = self.product(parent, value, ident='aligned-' + str(index))
                result = sequence_api.get_view(self.api, {'ref': pin(product)})
                self.assertEqual(result['sequence'], 'MA')
                self.assertEqual(result['source'], {'ref': pin(parent), 'sha256': parent['sha256'],
                    'sequence_sha256': digest(sequence), 'molecule_type': 'dna', 'length': len(sequence),
                    'circular': circular, 'sequence': sequence, 'complete': True, 'issues': []})
                self.assertEqual(result['codon_positions'], positions)
                self.assertTrue(result['codon_positions_complete'])
                self.assertEqual(result['terminal_stop_positions'], stop)
                reconstructed = [''.join(sequence[p] for p in triplet) for triplet in positions]
                if value['strand'] == -1:
                    reconstructed = [codon.translate(sequence_api.COMPLEMENT) for codon in reconstructed]
                self.assertEqual(reconstructed, ['ATG', 'GCT'])

    def test_alignment_crops_residues_and_only_shows_a_visible_terminal_stop(self):
        parent = self.parent('ATGGCTGGGTAA')
        for index, (end, positions, stop) in enumerate([
            (None, [[3, 4, 5], [6, 7, 8]], [9, 10, 11]),
            (2, [[3, 4, 5]], None),
        ]):
            product = self.product(parent, definition([{'start': 0, 'end': 12}], residue_start=1, residue_end=end),
                                   ident='crop-' + str(index))
            result = sequence_api.get_view(self.api, {'ref': pin(product)})
            self.assertEqual(result['codon_positions'], positions)
            self.assertEqual(len(result['codon_positions']), len(result['sequence']))
            self.assertEqual(result['terminal_stop_positions'], stop)

    def test_alignment_letters_preserve_cds_initiator_override_and_rna_source(self):
        parent = self.parent('GTGGCTTAA')
        product = self.product(parent, definition(genetic_code=11))
        result = sequence_api.get_view(self.api, {'ref': pin(product)})
        self.assertEqual(result['sequence'], 'MA')  # GTG is an initiator here, not literal V.
        self.assertEqual(result['codon_positions'][0], [0, 1, 2])
        self.assertEqual(result['translation']['genetic_code'], 11)
        rna = self.parent('AUGGCUUAA', ident='rna', molecule_type='rna')
        product = self.product(rna, ident='rna-product')
        result = sequence_api.get_view(self.api, {'ref': pin(product)})
        self.assertEqual(result['sequence'], 'MA')
        self.assertEqual(result['source']['sequence'], 'AUGGCUUAA')
        self.assertEqual(result['source']['molecule_type'], 'rna')

    def test_unavailable_product_retains_valid_source_but_never_a_partial_alignment(self):
        parent = self.parent('ATGGCTTAAC')
        product = self.product(parent, definition([{'start': 0, 'end': 10}]))
        result = sequence_api.get_view(self.api, {'ref': pin(product)})
        self.assertFalse(result['available']); self.assertIsNone(result['sequence'])
        self.assertEqual(result['source']['sequence'], 'ATGGCTTAAC')
        self.assertTrue(result['source']['complete'])
        self.assertEqual(result['codon_positions'], [])
        self.assertFalse(result['codon_positions_complete'])
        self.assertIsNone(result['terminal_stop_positions'])
        self.assertIn('incomplete_codon', [issue['code'] for issue in result['issues']])

    def test_historical_alignment_never_substitutes_the_current_parent_or_unbound_dna(self):
        parent = self.parent()
        product = self.product(parent)
        self.registry.revise('parent', {'identity': {**parent['identity'], 'sequence': 'ATGGGTTAA'}})
        old = sequence_api.get_view(self.api, {'ref': pin(product)})
        current = sequence_api.get_view(self.api, {'ref': 'product'})
        self.assertEqual(old['sequence'], 'MA')
        self.assertEqual(old['source']['sequence'], 'ATGGCTTAA')
        self.assertEqual(old['source']['sha256'], parent['sha256'])
        self.assertEqual(current['sequence'], 'MG')
        self.assertEqual(current['source']['ref'], 'construct:parent@2')
        self.assertEqual(current['source']['sequence'], 'ATGGGTTAA')
        with opened(self.api) as (_, registry, records):
            wrong = deepcopy(product)
            wrong['identity']['encoded_by']['sequence_sha256'] = '0' * 64
            result = sequence_api.view(wrong, registry, records)
            self.assertFalse(result['source']['complete'])
            self.assertIsNone(result['source']['sequence'])
            self.assertEqual(result['codon_positions'], [])
            self.assertIn('source_digest', [issue['code'] for issue in result['source']['issues']])

    def test_standalone_proteins_have_no_invented_nucleotide_source(self):
        record = self.registry.import_record({'kind': 'construct', 'id': 'standalone',
            'identity': {'molecule_type': 'protein', 'sequence': 'MA'}})
        result = sequence_api.get_view(self.api, {'ref': pin(record)})
        self.assertEqual(result['sequence'], 'MA')
        self.assertNotIn('source', result)
        self.assertNotIn('codon_positions', result)

    def test_alignment_bulk_caps_omit_whole_fields_without_losing_the_peptide(self):
        parent = self.parent('ATGGCTTAA' + 'A' * sequence_api.MAX_SOURCE_BASES)
        product = self.product(parent)
        result = sequence_api.get_view(self.api, {'ref': pin(product)})
        self.assertEqual(result['sequence'], 'MA')
        self.assertFalse(result['source']['complete'])
        self.assertIsNone(result['source']['sequence'])
        self.assertEqual(result['source']['length'], sequence_api.MAX_SOURCE_BASES + 9)
        self.assertFalse(result['codon_positions_complete'])
        self.assertEqual(result['codon_positions'], [])
        self.assertIn('alignment_source_limit', [issue['code'] for issue in result['issues']])
        long = 'ATG' + 'GCT' * sequence_api.MAX_ALIGNED_RESIDUES + 'TAA'
        parent = self.parent(long, ident='long-peptide-source')
        product = self.product(parent, definition([{'start': 0, 'end': len(long)}]), ident='long-peptide')
        result = sequence_api.get_view(self.api, {'ref': pin(product)})
        self.assertEqual(len(result['sequence']), sequence_api.MAX_ALIGNED_RESIDUES + 1)
        self.assertTrue(result['source']['complete'])
        self.assertFalse(result['codon_positions_complete'])
        self.assertEqual(result['codon_positions'], [])
        self.assertIn('alignment_residue_limit', [issue['code'] for issue in result['issues']])

    def test_alignment_uses_remaining_envelope_budget_and_can_be_fetched_separately(self):
        parent = self.parent('ATGGCTTAA' + 'C' * 20000)
        product = self.product(parent)
        with opened(self.api) as (_, registry, records):
            limited = sequence_api.view(product, registry, records, include_sequence=False, max_bytes=3000)
            self.assertLessEqual(len(canonical(limited)), 3000)
            self.assertEqual(limited['sequence'], 'MA')
            self.assertEqual(limited['source']['ref'], pin(parent))
            self.assertEqual(limited['source']['sha256'], parent['sha256'])
            self.assertFalse(limited['source']['complete'])
            self.assertIsNone(limited['source']['sequence'])
            self.assertFalse(limited['codon_positions_complete'])
            self.assertIsNone(limited['terminal_stop_positions'])
            self.assertIn('alignment_response_limit', [issue['code'] for issue in limited['issues']])
            for budget in (True, 0, -1, WIRE + 1):
                with self.subTest(budget=budget), self.assertRaises(Error):
                    sequence_api.view(product, registry, records, max_bytes=budget)
            with self.assertRaises(Error) as raised:
                sequence_api.view(product, registry, records, max_bytes=20)
            self.assertEqual(raised.exception.code, 'limit')
            self.assertLessEqual(len(canonical(sequence_api.view(product, registry, records, max_bytes=WIRE))), sequence_api.MAX_VIEW)
        full = sequence_api.get_view(self.api, {'ref': pin(product)})
        self.assertTrue(full['source']['complete']); self.assertTrue(full['codon_positions_complete'])
        self.assertEqual(full['source']['sequence'], parent['identity']['sequence'])

    def test_large_exact_codon_mapping_is_bulk_not_a_metadata_overflow(self):
        coding = 'ATG' + 'GCT' * (sequence_api.MAX_ALIGNED_RESIDUES - 1) + 'TAA'
        source = 'C' * 500000 + coding
        parent = self.parent(source)
        product = self.product(parent, definition([{'start': 500000, 'end': len(source)}]))
        result = sequence_api.get_view(self.api, {'ref': pin(product)})
        self.assertEqual(len(result['codon_positions']), sequence_api.MAX_ALIGNED_RESIDUES)
        self.assertTrue(result['codon_positions_complete']); self.assertTrue(result['source']['complete'])
        self.assertGreater(len(canonical(result['codon_positions'])), sequence_api.MAX_METADATA)
        self.assertLess(len(canonical(result)), WIRE)

    def test_schema2_first_stop_mapping_uses_actual_stop_after_frame_skip(self):
        parent = self.parent('ATGAATAAATAA')
        product = self.product(parent, definition([{'start': 0, 'end': 12}], schema=2,
            codon_start=3, initiation='literal', stop_policy='first_stop'))
        result = sequence_api.get_view(self.api, {'ref': pin(product)})
        self.assertEqual(result['sequence'], 'E')
        self.assertEqual(result['codon_positions'], [[2, 3, 4]])
        self.assertEqual(result['terminal_stop_positions'], [5, 6, 7])
        self.assertTrue(result['source']['complete'])
        self.assertTrue(result['codon_positions_complete'])

    def test_schema2_stop_and_crop_mapping_cover_reverse_circular_and_unused_ambiguity(self):
        coding = 'ATGAATAAATAA'
        cases = [
            (reverse(coding), definition([{'start': 0, 'end': 12}], schema=2, strand=-1,
                codon_start=3, initiation='literal', stop_policy='first_stop'),
             False, 'E', [[9, 8, 7]], [6, 5, 4]),
            (coding[4:] + coding[:4], definition([{'start': 8, 'end': 12}, {'start': 0, 'end': 8}],
                schema=2, codon_start=3, initiation='literal', stop_policy='first_stop'),
             True, 'E', [[10, 11, 0]], [1, 2, 3]),
            ('GCTGGGTAANN', definition([{'start': 0, 'end': 11}], schema=2,
                initiation='literal', stop_policy='first_stop', residue_start=1),
             False, 'G', [[3, 4, 5]], [6, 7, 8]),
        ]
        for index, (source, value, circular, peptide, positions, stop) in enumerate(cases):
            parent = self.parent(source, ident='source-' + str(index), circular=circular)
            product = self.product(parent, value, ident='product-' + str(index))
            result = sequence_api.get_view(self.api, {'ref': pin(product)})
            self.assertEqual(result['sequence'], peptide)
            self.assertEqual(result['codon_positions'], positions)
            self.assertEqual(result['terminal_stop_positions'], stop)
            self.assertTrue(result['codon_positions_complete'])
            self.assertEqual(result['source']['sequence'], source)

    def test_schema2_boundary_partial_and_empty_stop_retain_correct_source(self):
        for index, (source, peptide, stop) in enumerate([
            ('GCTGG', 'A', None),
            ('TAAGCT', None, None),
        ]):
            parent = self.parent(source, ident='source-' + str(index))
            product = self.product(parent, definition([{'start': 0, 'end': len(source)}], schema=2,
                initiation='literal', stop_policy='first_stop'), ident='product-' + str(index))
            result = sequence_api.get_view(self.api, {'ref': pin(product)})
            self.assertEqual(result['sequence'], peptide)
            self.assertEqual(result['terminal_stop_positions'], stop)
            self.assertTrue(result['source']['complete'])
            self.assertEqual(result['source']['sequence'], source)
            self.assertEqual(result['codon_positions_complete'], peptide is not None)
            self.assertEqual(result['codon_positions'], [[0, 1, 2]] if peptide else [])


if __name__ == '__main__':
    unittest.main()
