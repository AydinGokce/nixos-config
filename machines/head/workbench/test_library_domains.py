"""Current curated annotation coverage, never historical or guessed domains."""
from copy import deepcopy
import json
import random
import unittest
from unittest.mock import patch

from workbench import library_domains as domains
from workbench.common import Error, canonical
from workbench.library_api import opened, pin
from workbench import test_library_sequence as fixtures
from workbench.test_library_sequence import definition, digest, feature, reverse


class ProteinDomainsTests(unittest.TestCase):
    setUp = fixtures.LibrarySequenceTests.setUp
    attachments = fixtures.LibrarySequenceTests.attachments
    parent = fixtures.LibrarySequenceTests.parent
    product = fixtures.LibrarySequenceTests.product

    def annotated(self, sequence='ATGGCTGGGTAA', features=None, **kwargs):
        return self.parent(sequence, documents={'sequence.json': {'sequence_sha256': digest(sequence)},
            'annotations.json': {'sequence_features': features or []}}, **kwargs)

    def get(self, ref='product'):
        return self.api.call('library.protein_domains', {'ref': ref})

    def test_exact_current_source_cds_is_curated_and_endpoint_never_scans_or_writes(self):
        parent = self.annotated(features=[feature(1, [(3, 9)], label='TadA', color='#abCd12')])
        product = self.product(parent, definition([{'start': 0, 'end': 12}]))
        before = {str(p): p.read_bytes() for p in self.registry.root.rglob('*') if p.is_file()}
        with patch('workbench.library_sequence._orfs', side_effect=AssertionError('no scanning')):
            result = self.get()
        after = {str(p): p.read_bytes() for p in self.registry.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result['ref'], pin(product)); self.assertEqual(result['sha256'], product['sha256'])
        self.assertEqual(result['sequence'], 'MAG'); self.assertEqual(result['sequence_sha256'], digest('MAG'))
        self.assertEqual(result['source']['ref'], pin(parent))
        self.assertEqual(result['source']['sha256'], parent['sha256'])
        self.assertEqual(result['source']['status'], 'current')
        self.assertEqual(result['features'][0]['segments'], [{'start': 1, 'end': 3}])
        self.assertEqual(result['features'][0]['kind'], 'CDS')
        self.assertEqual(result['features'][0]['color'], '#ABCD12')
        self.assertEqual(result['features'][0]['status'], 'mapped')
        self.assertEqual(result['source']['annotations_receipt']['sha256'],
                         next(a['sha256'] for a in parent['attachments'] if a['path'].endswith('annotations.json')))
        self.assertEqual(self.api.call('batch.list', {})['batches'], [])

    def test_partial_codons_keep_only_complete_residues_and_report_clipping(self):
        parent = self.annotated(features=[feature(1, [(1, 8)]), feature(2, [(1, 2)]),
            feature(3, [(9, 12)]), feature(4, [(0, 12)])])
        self.product(parent, definition([{'start': 0, 'end': 12}]))
        result = self.get(); rows = result['features']
        self.assertEqual(rows[0]['segments'], [{'start': 1, 'end': 2}])
        self.assertEqual(rows[0]['status'], 'clipped')
        self.assertEqual(rows[0]['issues'][0]['code'], 'partial_codon')
        self.assertEqual(rows[1]['segments'], [{'start': 0, 'end': 3}])
        self.assertEqual(rows[1]['issues'][0]['code'], 'outside_product')
        self.assertEqual([r['reason'] for r in result['excluded']], ['partial_codon', 'outside_product'])

    def test_reverse_strand_and_joined_codon_coverage(self):
        # Biological sequence ATG / GCT / GGG / TAA across a split codon.
        source = reverse('ATGGC' + 'CCCC' + 'TGGGTAA')
        value = definition([{'start': 11, 'end': 16}, {'start': 0, 'end': 7}], strand=-1)
        parent = self.annotated(source, features=[feature(1, [(11, 13), (6, 7)], -1),
            feature(2, [(11, 13), (6, 7)], 1)])
        self.product(parent, value)
        result = self.get()
        self.assertEqual(result['sequence'], 'MAG')
        self.assertEqual(result['features'][0]['segments'], [{'start': 1, 'end': 2}])
        self.assertEqual(result['features'][0]['status'], 'mapped')
        self.assertEqual(result['excluded'][0]['reason'], 'opposite_strand')

    def test_circular_origin_split_codon_and_residue_crop(self):
        coding = 'ATGGCTGGGTAA'; source = coding[4:] + coding[:4]
        parent = self.annotated(source, circular=True, features=[feature(1, [(11, 12), (0, 5)])])
        self.product(parent, definition([{'start': 8, 'end': 12}, {'start': 0, 'end': 8}],
                                       residue_start=1, residue_end=3))
        result = self.get()
        self.assertEqual(result['sequence'], 'AG')
        self.assertEqual(result['features'][0]['segments'], [{'start': 0, 'end': 2}])
        self.assertEqual(result['features'][0]['status'], 'mapped')

    def test_schema2_saved_frame_first_stop_and_unused_bases(self):
        parent = self.annotated('ATGAATAAATAA', features=[feature(1, [(2, 5)]),
            feature(2, [(0, 3)]), feature(3, [(5, 12)])])
        self.product(parent, definition([{'start': 0, 'end': 12}], schema=2,
            codon_start=3, initiation='literal', stop_policy='first_stop'))
        result = self.get()
        self.assertEqual(result['sequence'], 'E')
        self.assertEqual(result['features'][0]['segments'], [{'start': 0, 'end': 1}])
        self.assertEqual([r['reason'] for r in result['excluded']], ['partial_codon', 'outside_product'])
        self.assertEqual(result['source']['translation']['codon_start'], 3)

    def test_disjoint_feature_segments_remain_disjoint_and_overlaps_count_once(self):
        parent = self.annotated(features=[feature(1, [(0, 3), (6, 9)]),
            feature(2, [(0, 3), (0, 2), (1, 3)])])
        self.product(parent, definition([{'start': 0, 'end': 12}]))
        rows = self.get()['features']
        self.assertEqual(rows[0]['segments'], [{'start': 0, 'end': 1}, {'start': 2, 'end': 3}])
        self.assertEqual(rows[1]['segments'], [{'start': 0, 'end': 1}])

    def test_parent_edit_does_not_substitute_latest_annotations_for_pinned_revision(self):
        parent = self.annotated(features=[feature(1, [(3, 6)])])
        product = self.product(parent, definition([{'start': 0, 'end': 12}]))
        self.registry.revise('parent', {'identity': {**parent['identity'], 'sequence': 'ATGGCCGGGTAA'}})
        old = self.get(pin(product)); current = self.get()
        self.assertEqual(old['source']['ref'], pin(parent)); self.assertEqual(len(old['features']), 1)
        self.assertEqual(current['source']['ref'], 'construct:parent@2')
        self.assertEqual(current['sequence'], old['sequence'])
        self.assertEqual(current['source']['status'], 'stale'); self.assertFalse(current['complete'])
        self.assertEqual(current['features'], []); self.assertEqual(current['excluded'][0]['reason'], 'annotations_stale')

    def test_historical_protein_evidence_is_not_read_and_labels_never_create_subdomains(self):
        parent = self.annotated(features=[feature(1, [(0, 12)], label='TwinStrep-TadA-Cas9')])
        self.product(parent, definition([{'start': 0, 'end': 12}]),
            documents={'derivation.json': {'features': [{'label': 'invented domain'}], 'evidence': 'bad old schema'}})
        result = self.get()
        self.assertEqual(len(result['features']), 1)
        self.assertEqual(result['features'][0]['label'], 'TwinStrep-TadA-Cas9')
        self.assertEqual(result['features'][0]['segments'], [{'start': 0, 'end': 3}])

    def test_automatic_provenance_excluded_but_curated_cds_and_labels_allowed(self):
        markers = [{'source': 'orf_scan'}, {'auto_detected': True}, {'provenance': {'method': 'computed'}},
                   {'provenance': 'autodetected'}, {'generated': 'true'}]
        features = [feature(i, [(0, 3)], **marker) for i, marker in enumerate(markers)]
        features.append(feature(99, [(0, 3)], label='Predicted activity (curated)', auto_detected=False))
        parent = self.annotated(features=features); self.product(parent, definition([{'start': 0, 'end': 12}]))
        result = self.get()
        self.assertEqual(len(result['features']), 1); self.assertEqual(result['features'][0]['source_feature_id'], 99)
        self.assertEqual([r['reason'] for r in result['excluded']], ['autodetected'] * len(markers))

    def test_unknown_remote_fuzzy_mixed_strand_duplicate_and_source_features_excluded(self):
        remote = feature(1, [(0, 3)]); parts = json.loads(remote['location_parts_json']); parts[0]['ref'] = 'other'
        remote['location_parts_json'] = json.dumps(parts)
        mixed = feature(2, [(0, 1), (1, 3)]); parts = json.loads(mixed['location_parts_json']); parts[1]['strand'] = -1
        mixed['location_parts_json'] = json.dumps(parts)
        features = [remote, mixed, feature(3, [(0, 3)], source_location='<1..3'),
                    feature(4, [(0, 3)], type='source'), feature(5, [(0, 3)]), feature(5, [(3, 6)]), None]
        parent = self.annotated(features=features); self.product(parent, definition([{'start': 0, 'end': 12}]))
        result = self.get()
        self.assertEqual(result['features'], [])
        self.assertEqual([r['reason'] for r in result['excluded']], ['unsupported_coordinates', 'unsupported_coordinates',
            'fuzzy_coordinates', 'sequence_record', 'ambiguous_source_id', 'ambiguous_source_id', 'unsupported_coordinates'])
        self.assertTrue(result['complete'])

    def test_missing_source_binding_cannot_be_treated_as_current(self):
        parent = self.parent(documents={'annotations.json': {'sequence_features': [feature(1, [(0, 3)])]}})
        self.product(parent)
        result = self.get()
        self.assertEqual(result['source']['status'], 'missing'); self.assertEqual(result['features'], [])
        self.assertEqual(result['excluded'][0]['reason'], 'annotations_missing'); self.assertFalse(result['complete'])

    def test_missing_annotations_standalone_and_unavailable_product_are_explicit(self):
        parent = self.parent(); self.product(parent)
        result = self.get(); self.assertEqual(result['source']['status'], 'missing')
        self.assertEqual(result['features'], []); self.assertTrue(result['available'])
        record = self.registry.import_record({'kind': 'construct', 'id': 'standalone',
            'identity': {'molecule_type': 'protein', 'sequence': 'MAG'}})
        standalone = self.get(pin(record)); self.assertIsNone(standalone['source'])
        self.assertEqual(standalone['issues'][0]['code'], 'not_coordinate_derived')
        self.product(parent, definition(residue_start=500), ident='bad')
        bad = self.get('bad'); self.assertFalse(bad['available']); self.assertIsNone(bad['sequence'])
        self.assertEqual(bad['features'], []); self.assertFalse(bad['complete'])

    def test_attachment_tamper_and_bad_annotation_list_fail_integrity(self):
        parent = self.annotated(features=[feature(1, [(0, 3)])]); self.product(parent, definition([{'start': 0, 'end': 12}]))
        path = self.registry._path(pin(parent)).parent / 'attachments/annotations.json'
        original = path.read_bytes(); path.write_bytes(original + b' ')
        with self.assertRaises(Error) as error:
            self.get()
        self.assertIn(error.exception.code, {'integrity', 'library'})
        path.write_bytes(original)
        parent = self.parent(ident='malformed', documents={'annotations.json': {'sequence_features': {}},
            'sequence.json': {'sequence_sha256': digest('ATGGCTTAA')}})
        self.product(parent, ident='malformed-product')
        with self.assertRaises(Error) as error:
            self.get('malformed-product')
        self.assertEqual(error.exception.code, 'integrity')

    def test_source_digest_mismatch_fails_closed(self):
        parent = self.annotated(features=[feature(1, [(0, 3)])]); product = self.product(parent, definition([{'start': 0, 'end': 12}]))
        with opened(self.api) as (module, registry, records):
            wrong = deepcopy(product); wrong['identity']['encoded_by']['sequence_sha256'] = '0' * 64
            result = domains.project(wrong, registry, records, module)
        self.assertFalse(result['available']); self.assertEqual(result['features'], [])
        self.assertIn('source_digest', [i['code'] for i in result['issues']])

    def test_colors_are_explicit_hex_only(self):
        self.assertIsNone(domains._color({'color': 'red; rm -rf'}))
        self.assertEqual(domains._color({'qualifiers_json': json.dumps({'ApEinfo_fwdcolor': ['#fedCab']})}), '#FEDCAB')
        self.assertIsNone(domains._color({'color': '#12345678', 'qualifiers_json': '[]'}))

    def test_bounded_metadata_and_feature_count_are_explicit_not_silent(self):
        parent = self.annotated(features=[feature(i, [(0, 3)]) for i in range(40)])
        self.product(parent, definition([{'start': 0, 'end': 12}]))
        with patch.object(domains, 'MAX_FEATURES', 12):
            limited = self.get()
        self.assertEqual(limited['counts'], {'total': 40, 'mapped': 12, 'excluded': 0, 'omitted': 28})
        with patch.object(domains, 'MAX_METADATA', 500):
            limited = self.get()
        self.assertFalse(limited['complete']); self.assertGreater(limited['counts']['omitted'], 0)
        self.assertLess(len(canonical(limited)), 3000)

    def test_projection_matches_brute_force_codon_membership_across_frames_and_crops(self):
        rng = random.Random(3614)
        for index in range(30):
            strand = rng.choice([-1, 1]); frame = rng.randrange(3)
            coding = 'C' * frame + 'ATG' + 'GCT' * 8 + 'TAA'
            source = coding if strand == 1 else reverse(coding)
            spans = sorted((rng.randrange(len(source)), rng.randrange(len(source))) for _ in range(3))
            spans = [(min(a, b), max(a, b) + 1) for a, b in spans]
            parent = self.annotated(source, ident=f'p{index}', features=[feature(1, spans, strand)])
            start, end = 1, 7
            product = self.product(parent, definition([{'start': 0, 'end': len(source)}],
                strand=strand, codon_start=frame + 1, residue_start=start, residue_end=end), ident=f'v{index}')
            result = self.get(pin(product))
            covered = {n for left, right in spans for n in range(left, right)}
            order = list(range(len(source))) if strand == 1 else list(reversed(range(len(source))))
            codons = [order[frame + 3 * n:frame + 3 * n + 3] for n in range(start, end)]
            expected = {n for n, codon in enumerate(codons) if set(codon) <= covered}
            actual = {n for row in result['features'] for span in row['segments'] for n in range(span['start'], span['end'])}
            self.assertEqual(actual, expected)

    def test_rpc_requires_protein_ref_and_rejects_extra_mutation_fields(self):
        self.parent()
        for params in ({'ref': 'parent'}, {'ref': 'missing'}, {'ref': 'parent', 'write': True}):
            with self.subTest(params=params), self.assertRaises(Error):
                self.api.call('library.protein_domains', params)


if __name__ == '__main__':
    unittest.main()
