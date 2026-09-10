"""Coordinate identity, revision cascades and model projections without inference."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import adapters
import registry as r
import translation as t


def definition(segments=None, **updates):
    result = {'schema': 1, 'segments': segments if segments is not None else [{'start': 0, 'end': 9}], 'strand': 1,
              'genetic_code': 1, 'codon_start': 1, 'initiation': 'cds', 'residue_start': 0, 'residue_end': None}
    result.update(updates)
    return result


def resolve(sequence, value=None, circular=False):
    source = {'kind': 'construct', 'id': 'source', 'revision': 1,
              'identity': {'molecule_type': 'dna', 'sequence': sequence, 'circular': circular}}
    product = {'kind': 'construct', 'id': 'product', 'revision': 1,
               'identity': {'molecule_type': 'protein', 'encoded_by': {
                   'construct_ref': 'construct:source@1', 'sequence_sha256': t.digest(sequence),
                   'translation': value or definition()}}}
    return t.effective_sequence(product, {'construct:source@1': source})


class TranslationTests(unittest.TestCase):
    def test_schema1_projection_bytes_remain_identical_to_published_engine(self):
        goldens = {
            'ATGGCTTAA': '6fdce461f5de2f5bc181fea4fbfdf1fce58648cba0589e209f70c4f13316b12c',
            'ATGTAAGCTTAA': '7045ad9ad2d71ea104d00a3c17bade95eb44e69240ed5bc43128c385da1455a1',
            'ATGNNNTAA': '102e1c46fc32c74a2d6d6d4b099121e8287b66080aca72b96a2805d108d64fc9',
        }
        for sequence, expected in goldens.items():
            value = definition([{'start': 0, 'end': len(sequence)}])
            product = {'identity': {'molecule_type': 'protein', 'encoded_by': {
                'construct_ref': 'construct:source@1', 'sequence_sha256': t.digest(sequence), 'translation': value}}}
            source = {'kind': 'construct', 'identity': {'molecule_type': 'dna', 'sequence': sequence}}
            projected = t.projection(product, {'construct:source@1': source})
            self.assertEqual(t.digest(json.dumps(projected, sort_keys=True, separators=(',', ':'))), expected)
            self.assertEqual(projected['engine_version'], 'coordinate-translation-v1')
            self.assertNotIn('terminal_stop_offset', projected)
            self.assertNotIn('trailing_bases', projected)

    def test_frame_definition_preserves_same_phase_and_all_other_coordinate_fields(self):
        original = definition([{'start': 20, 'end': 32}, {'start': 0, 'end': 6}], strand=-1,
                              genetic_code=11, codon_start=2, residue_start=1, residue_end=4)
        same = t.frame_definition(original, 1)
        self.assertEqual(same, original)
        self.assertIsNot(same, original)
        self.assertIsNot(same['segments'], original['segments'])
        for offset in (0, 2):
            changed = t.frame_definition(original, offset)
            self.assertEqual(changed, {**original, 'schema': 2, 'codon_start': offset + 1,
                                       'initiation': 'literal', 'stop_policy': 'first_stop'})
            self.assertEqual(t.frame_definition(changed, offset), changed)
            t.validate_definition(changed)
        self.assertEqual(original['schema'], 1)
        self.assertEqual(original['initiation'], 'cds')
        for offset in (-1, 3, True, False, 1.0, '1', None):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                t.frame_definition(original, offset)

    def test_frame_changes_translate_complete_codons_in_both_strand_directions(self):
        for sequence, strand in [('ATGGCTTAA', 1), ('TTAAGCCAT', -1)]:
            original = definition(strand=strand)
            self.assertEqual(resolve(sequence, t.frame_definition(original, 0))['sequence'], 'MA')
            for offset, peptide, trailing in [(1, 'WL', 2), (2, 'GL', 1)]:
                result = resolve(sequence, t.frame_definition(original, offset))
                self.assertTrue(result['available'], result)
                self.assertEqual(result['sequence'], peptide)
                self.assertEqual(result['trailing_bases'], trailing)
                self.assertIsNone(result['terminal_stop_offset'])
                self.assertEqual(result['engine_version'], t.ENGINE_V2)

    def test_first_stop_halts_before_downstream_codons_and_ignores_only_boundary_partial(self):
        cases = [('ATGTAANNNC', 'M', 3, 1), ('ATGGCTA', 'MA', None, 1), ('ATGGCTNN', 'MA', None, 2)]
        for sequence, peptide, stop, trailing in cases:
            value = definition([{'start': 0, 'end': len(sequence)}], schema=2,
                               stop_policy='first_stop', initiation='literal')
            result = resolve(sequence, value)
            self.assertTrue(result['available'], result)
            self.assertEqual((result['sequence'], result['terminal_stop_offset'], result['trailing_bases']),
                             (peptide, stop, trailing))
        # The stop offset is relative to coding DNA after the declared frame skip.
        value = definition([{'start': 0, 'end': 10}], schema=2, stop_policy='first_stop',
                           codon_start=2, initiation='literal')
        self.assertEqual(resolve('NATGGCTTAA', value)['terminal_stop_offset'], 6)

    def test_first_stop_preserves_crops_and_reports_unavailable_products_without_partial_peptides(self):
        valid = definition([{'start': 0, 'end': 15}], schema=2, stop_policy='first_stop',
                           initiation='literal', residue_start=1, residue_end=3)
        result = resolve('ATGGCTGGTTAAATG', valid)
        self.assertEqual((result['sequence'], result['uncropped_length'], result['terminal_stop_offset']), ('AG', 3, 9))
        cases = [('ATGNNNTAA', {}, 'ambiguous_codon'), ('TAAGCTTAA', {}, 'empty_product'),
                 ('AT', {}, 'incomplete_codon'), ('ATGTAAGCT', {'residue_start': 1}, 'residue_range'),
                 ('ATGTAAGCT', {'residue_end': 2}, 'residue_range')]
        for sequence, updates, code in cases:
            value = definition([{'start': 0, 'end': len(sequence)}], schema=2,
                               stop_policy='first_stop', initiation='literal', **updates)
            result = resolve(sequence, value)
            self.assertFalse(result['available'], result)
            self.assertIsNone(result['sequence'])
            self.assertEqual(result['issues'][0]['code'], code)

    def test_first_stop_retains_explicit_cds_semantics_and_circular_segment_order(self):
        value = definition([{'start': 0, 'end': 12}], schema=2, stop_policy='first_stop', genetic_code=11)
        result = resolve('GTGGCTTAAGGG', value)
        self.assertEqual(result['sequence'], 'MA')
        self.assertEqual(result['terminal_stop_offset'], 6)
        self.assertEqual(resolve('GTGGCTTAAGGG', {**value, 'initiation': 'literal'})['sequence'], 'VA')
        self.assertEqual(resolve('GTGGCTTAAGGG', {**value, 'genetic_code': 1})['issues'][0]['code'], 'initiation')
        self.assertEqual(resolve('GTGGCTGGGGGG', value)['issues'][0]['code'], 'terminal_stop')
        for sequence, value in [
            ('TAACCCATGGCT', definition([{'start': 6, 'end': 12}, {'start': 0, 'end': 3}])),
            ('AGCCATGGGTTA', definition([{'start': 0, 'end': 6}, {'start': 9, 'end': 12}], strand=-1)),
        ]:
            original = resolve(sequence, {**value, 'schema': 2, 'stop_policy': 'first_stop'}, circular=True)
            self.assertEqual(original['sequence'], 'MA')
            self.assertEqual(original['terminal_stop_offset'], 6)
            result = resolve(sequence, t.frame_definition(value, 1), circular=True)
            self.assertEqual(result['sequence'], 'WL')
            self.assertEqual(result['trailing_bases'], 2)

    def test_schema2_strict_retains_strict_rules_and_schema_validation_is_versioned(self):
        for sequence in ['ATGGCTTAA', 'ATGTAAGCTTAA', 'ATGNNNTAA', 'ATGGCTA']:
            value = definition([{'start': 0, 'end': len(sequence)}])
            strict = resolve(sequence, {**value, 'schema': 2, 'stop_policy': 'strict'})
            expected = resolve(sequence, value)
            if 'engine_version' in expected:
                expected['engine_version'] = t.ENGINE_V2
            self.assertEqual(strict, expected)
        for value in [definition(stop_policy='strict'), definition(schema=2),
                      definition(schema=True), definition(schema=2, stop_policy=False),
                      definition(schema=2, stop_policy='unknown'), definition(schema=2, stop_policy={})]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                t.validate_definition(value)

    def test_forward_reverse_spliced_and_circular_strands(self):
        for sequence, value, circular in [
            ('ATGGCTTAA', definition(), False),
            ('TTAAGCCAT', definition(strand=-1), False),
            ('CCATGAAAGCTTAAGG', definition([{'start': 2, 'end': 5}, {'start': 8, 'end': 14}]), False),
            ('TAACCCATGGCT', definition([{'start': 6, 'end': 12}, {'start': 0, 'end': 3}]), True),
            ('AGCCATGGGTTA', definition([{'start': 0, 'end': 6}, {'start': 9, 'end': 12}], strand=-1), True),
        ]:
            with self.subTest(sequence=sequence, value=value):
                result = resolve(sequence, value, circular)
                self.assertTrue(result['available'], result)
                self.assertEqual(result['sequence'], 'MA')

    def test_codes_frame_initiation_and_residue_crop(self):
        self.assertFalse(resolve('GTGGCTTAA')['available'])
        self.assertEqual(resolve('GTGGCTTAA', definition(genetic_code=11))['sequence'], 'MA')
        self.assertEqual(resolve('GTGGCT', definition([{'start': 0, 'end': 6}], initiation='literal'))['sequence'], 'VA')
        self.assertEqual(resolve('CATGGCTTAA', definition([{'start': 0, 'end': 10}], codon_start=2))['sequence'], 'MA')
        self.assertEqual(resolve('ATGGCTGGGTAA', definition([{'start': 0, 'end': 12}], residue_start=1, residue_end=3))['sequence'], 'AG')

    def test_invalid_biological_definitions_have_no_previous_or_partial_sequence(self):
        cases = [
            ('ATGNNNTAA', definition(), 'ambiguous_codon'),
            ('ATGTAAGCTTAA', definition([{'start': 0, 'end': 12}]), 'internal_stop'),
            ('ATGGCTAAA', definition(), 'terminal_stop'),
            ('ATGGC', definition([{'start': 0, 'end': 5}]), 'incomplete_codon'),
            ('ATGGCTTAA', definition(segments=[]), 'coordinates'),
            ('ATGGCTTAA', definition([{'start': 0, 'end': 15}]), 'coordinates'),
            ('ATGGCTTAA', definition(residue_start=2), 'residue_range'),
            ('ATGGCTTAA', definition([{'start': 0, 'end': 6}, {'start': 3, 'end': 9}]), 'overlap'),
        ]
        # An empty list is a meaningful unavailable definition, not the helper default.
        cases[4][1]['segments'] = []
        for sequence, value, code in cases:
            with self.subTest(code=code):
                result = resolve(sequence, value)
                self.assertFalse(result['available'])
                self.assertIsNone(result['sequence'])
                self.assertEqual(result['issues'][0]['code'], code)

    def test_malformed_fields_raise_value_error_instead_of_type_error(self):
        for field, value in [('initiation', {}), ('genetic_code', []), ('strand', True),
                             ('codon_start', '1'), ('residue_end', False), ('segments', {})]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                t.validate_definition(definition(**{field: value}))

    def test_splice_boundaries_and_ambiguous_edits_never_guess_a_new_product(self):
        original = definition([{'start': 10, 'end': 19}])
        self.assertEqual(t.remap_translation(original, 5, 5, 3)['segments'], [{'start': 13, 'end': 22}])
        self.assertEqual(t.remap_translation(original, 10, 10, 3)['segments'], [{'start': 13, 'end': 22}])
        self.assertEqual(t.remap_translation(original, 19, 19, 3), original)
        self.assertEqual(t.remap_translation(original, 13, 13, 3)['segments'], [{'start': 10, 'end': 22}])
        self.assertEqual(t.remap_translation(original, 0, 30, 30), original)
        self.assertEqual(t.remap_translation(original, 0, 30, 31)['segments'], [])
        crop = definition([{'start': 10, 'end': 19}], residue_start=1)
        self.assertEqual(t.remap_translation(crop, 13, 13, 3)['segments'], [])
        self.assertEqual(t.remap_translation(crop, 5, 5, 3)['residue_start'], 1)
        self.assertEqual(original, definition([{'start': 10, 'end': 19}]))


class DerivedRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.registry = r.Registry(self.base / 'registry')
        self.registry.init()
        self.parent = self.registry.import_record({'kind': 'construct', 'id': 'parent', 'status': 'defined',
            'identity': {'molecule_type': 'dna', 'sequence': 'ATGGCTTAA', 'circular': True,
                         'strand_count': 2, 'molecular_form': 'plasmid'}})

    def tearDown(self):
        self.temp.cleanup()

    def document(self, ident='product', value=None, review=None):
        identity = {'molecule_type': 'protein', 'encoded_by': {'construct_ref': r.reference(self.parent),
            'sequence_sha256': t.digest(self.parent['identity']['sequence']), 'translation': value or definition()}}
        if review:
            identity['product_review'] = {'status': review, 'source': 'fixture', 'source_product_id': ident, 'method_version': 'fixture-v1'}
        return {'kind': 'construct', 'id': ident, 'status': 'defined', 'identity': identity}

    def records(self):
        with self.registry._lock():
            return self.registry._records_locked()

    def test_canonical_records_and_old_snapshots_remain_immutable_during_cascade(self):
        product = self.registry.import_record(self.document())
        old = self.registry.snapshot('product')
        self.assertNotIn('sequence', product['identity'])
        self.assertEqual(old['resolved_polymers'][r.reference(product)]['sequence'], 'MA')
        self.registry.revise('parent', {'identity': {**self.parent['identity'], 'sequence': 'ATGGGTTAA'}})
        latest = self.registry.show('product')
        self.assertEqual(latest['revision'], 2)
        self.assertEqual(latest['identity']['encoded_by']['construct_ref'], 'construct:parent@2')
        self.assertEqual(r.effective_sequence(latest, self.records())['sequence'], 'MG')
        self.assertEqual(self.registry.snapshot('construct:product@1'), old)
        self.assertEqual(self.registry.show('construct:product@1')['sha256'], product['sha256'])
        adapters.verify_snapshot(old)
        adapters.verify_snapshot(self.registry.snapshot('product'))

    def test_schema2_frame_revision_snapshots_and_model_identity_keep_old_schema1_exact(self):
        product = self.registry.import_record(self.document())
        old = self.registry.snapshot('product')
        before = json.dumps(old, sort_keys=True, separators=(',', ':'))
        identity = deepcopy(product['identity'])
        identity['encoded_by']['translation'] = t.frame_definition(identity['encoded_by']['translation'], 1)
        current = self.registry.revise('product', {'identity': identity})
        snapshot = self.registry.snapshot('product')
        projected = snapshot['resolved_polymers'][r.reference(current)]
        self.assertEqual(projected['engine_version'], t.ENGINE_V2)
        self.assertEqual(projected['sequence'], 'WL')
        self.assertEqual(projected['trailing_bases'], 2)
        self.assertEqual(t.materialized_identity(current, snapshot)['sequence'], 'WL')
        self.assertEqual(json.dumps(self.registry.snapshot(r.reference(product)), sort_keys=True, separators=(',', ':')), before)
        adapters.verify_snapshot(old)
        adapters.verify_snapshot(snapshot)
        altered = deepcopy(snapshot)
        altered['resolved_polymers'][r.reference(current)]['trailing_bases'] = 1
        altered['sha256'] = r.digest_json(altered)
        with self.assertRaises(ValueError):
            adapters.verify_snapshot(altered)

    def test_metadata_revisions_and_cropped_variants_follow_parent_without_resetting_review(self):
        self.registry.import_record(self.document(review='review_required'))
        self.registry.import_record(self.document('crop', definition(residue_start=1)))
        self.registry.revise('parent', {'notes': 'Parent metadata'})
        self.assertEqual(self.registry.show('product')['identity']['encoded_by']['construct_ref'], 'construct:parent@2')
        self.registry.revise('parent', {'identity': {**self.parent['identity'], 'sequence': 'ATGGGTGCTTAA'}})
        records = self.records()
        self.assertEqual(r.effective_sequence(self.registry.show('product'), records)['sequence'], 'MGA')
        crop = self.registry.show('crop')
        self.assertEqual(crop['status'], 'draft')
        self.assertFalse(r.effective_sequence(crop, records)['available'])
        self.assertEqual(self.registry.show('product')['identity']['product_review']['status'], 'review_required')

    def test_parent_metadata_and_explicit_restoration_preserve_curated_child_status(self):
        document = self.document()
        document.update(status='draft', notes='Keep this reviewed draft classification')
        original = self.registry.import_record(document)
        self.registry.revise_many([{'ref': r.reference(self.parent), 'expected_sha256': self.parent['sha256'],
                                   'patch': {'notes': 'Parent display metadata only'}}])
        metadata_parent = self.registry.show('parent')
        metadata_child = self.registry.show('product')
        self.assertEqual(metadata_child['status'], 'draft')
        self.assertEqual(metadata_child['notes'], original['notes'])
        self.assertEqual(metadata_child['identity']['encoded_by']['construct_ref'], r.reference(metadata_parent))
        self.registry.revise('parent', {'identity': {**metadata_parent['identity'], 'sequence': 'ATGGGTTAA'}})
        changed_parent, changed_child = self.registry.show('parent'), self.registry.show('product')
        self.assertEqual(changed_child['status'], 'defined')
        self.registry.revise_many([
            {'ref': r.reference(changed_parent), 'expected_sha256': changed_parent['sha256'],
             'patch': {'identity': metadata_parent['identity']}},
            {'ref': r.reference(changed_child), 'expected_sha256': changed_child['sha256'],
             'patch': {'identity': metadata_child['identity'], 'status': metadata_child['status']}},
        ])
        restored = self.registry.show('product')
        self.assertEqual(restored['status'], 'draft')
        self.assertEqual(restored['notes'], original['notes'])
        self.assertEqual(r.effective_sequence(restored, self.records())['sequence'], 'MA')

    def test_ambiguous_parent_edit_publishes_draft_child_without_a_stale_peptide(self):
        self.registry.import_record(self.document())
        self.registry.revise('parent', {'identity': {**self.parent['identity'], 'sequence': 'ATGNNNTAA'}})
        latest = self.registry.show('product')
        self.assertEqual(latest['status'], 'draft')
        self.assertIsNone(r.effective_sequence(latest, self.records())['sequence'])
        snapshot = self.registry.snapshot('product')
        adapters.verify_snapshot(snapshot)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            t.materialized_identity(latest, snapshot)

    def test_planner_is_idempotent_and_rejects_false_splice_receipts(self):
        self.registry.import_record(self.document())
        change = {'ref': r.reference(self.parent), 'expected_sha256': self.parent['sha256'],
                  'patch': {'identity': {**self.parent['identity'], 'sequence': 'ATGGGTGCTTAA'}}}
        records = self.records()
        with self.assertRaisesRegex(r.Error, 'splice does not match'):
            self.registry.plan_derivations([change], records, {r.reference(self.parent): {'start': 0, 'end': 0, 'replacement_length': 3}})
        planned = self.registry.plan_derivations([change], records)
        self.assertEqual(planned, self.registry.plan_derivations(planned, records))
        self.assertEqual(len(planned), 2)

    def test_explicit_undo_child_definition_is_rebound_without_a_second_remap(self):
        old_product = self.registry.import_record(self.document('crop', definition(residue_start=1)))
        old_parent = self.parent
        self.registry.revise('parent', {'identity': {**self.parent['identity'], 'sequence': 'ATGGGTGCTTAA'}})
        parent, product = self.registry.show('parent'), self.registry.show('crop')
        self.assertEqual(product['identity']['encoded_by']['translation']['segments'], [])
        changes = [
            {'ref': r.reference(parent), 'expected_sha256': parent['sha256'], 'patch': {'identity': old_parent['identity']}},
            {'ref': r.reference(product), 'expected_sha256': product['sha256'], 'patch': {'identity': old_product['identity']}},
        ]
        self.registry.revise_many(changes)
        restored = self.registry.show('crop')
        self.assertEqual(restored['identity']['encoded_by']['construct_ref'], 'construct:parent@3')
        self.assertEqual(restored['identity']['encoded_by']['translation'], old_product['identity']['encoded_by']['translation'])
        self.assertEqual(r.effective_sequence(restored, self.records())['sequence'], 'A')

    def test_durable_cascade_recovers_after_publication_was_interrupted(self):
        self.registry.import_record(self.document())
        self.registry.import_record(self.document('second'))
        old = self.registry.snapshot('product')
        # _lock also checks recovery; inject failure only inside the writer scope.
        with self.registry._lock(exclusive=True):
            records = self.registry._records_locked()
            with patch.object(self.registry, '_recover_transactions_locked', side_effect=OSError('publication interrupted')):
                with self.assertRaises(OSError):
                    self.registry._revise_many_locked([{'ref': r.reference(self.parent),
                        'expected_sha256': self.parent['sha256'], 'patch': {'identity': {**self.parent['identity'], 'sequence': 'ATGGGTTAA'}}}], records)
        self.assertTrue(list((self.registry.root / '.transactions').glob('*.json')))
        self.assertEqual(self.registry.show('parent')['revision'], 2)
        self.assertEqual(self.registry.show('product')['revision'], 2)
        self.assertEqual(self.registry.show('second')['revision'], 2)
        self.assertEqual(r.effective_sequence(self.registry.show('product'), self.records())['sequence'], 'MG')
        self.assertEqual(self.registry.snapshot('construct:product@1'), old)
        self.assertFalse(list((self.registry.root / '.transactions').glob('*.json')))

    def test_create_and_project_membership_publish_together_and_rollback_before_journal(self):
        brief = self.base / 'project.md'; brief.write_text('# Test project\n')
        project = self.registry.import_record({'kind': 'project', 'id': 'study',
            'identity': {'objectives_file': 'attachments/project.md', 'members': []}}, {'project.md': brief})
        changes = [{'create': self.document()}, {'ref': r.reference(project), 'expected_sha256': project['sha256'],
            'patch': {'identity': {**project['identity'], 'members': [{'source_ref': 'construct:product@1', 'role': 'Example'}]}}}]
        with patch.object(r, 'atomic_json', side_effect=OSError('before journal')):
            with self.assertRaises(OSError): self.registry.revise_many(changes)
        self.assertEqual(self.registry.verify()['records'], 2)
        self.assertEqual(self.registry.show('study')['identity']['members'], [])
        outputs = self.registry.revise_many(changes)
        self.assertEqual([r.reference(item) for item in outputs], ['construct:product@1', 'project:study@2'])
        self.assertEqual(self.registry.show('study')['identity']['members'][0]['source_ref'], 'construct:product@1')

    def test_source_projection_and_closure_tampering_are_rejected_even_if_resealed(self):
        self.registry.import_record(self.document())
        original = self.registry.snapshot('product')
        for field in ['sequence', 'derivation_sources', 'resolved_polymers']:
            snapshot = deepcopy(original)
            if field == 'sequence': snapshot['resolved_polymers']['construct:product@1']['sequence'] = 'MC'
            else: snapshot[field] = {}
            snapshot['sha256'] = r.digest_json(snapshot)
            with self.assertRaises(ValueError): adapters.verify_snapshot(snapshot)
        snapshot = deepcopy(original)
        source = snapshot['derivation_sources']['construct:parent@1']
        source['identity']['sequence'] = 'ATGGGTTAA'
        source['sha256'] = r.digest_json(source)
        snapshot['sha256'] = r.digest_json(snapshot)
        with self.assertRaises(ValueError): adapters.verify_snapshot(snapshot)

    def test_all_native_builders_use_the_same_resolved_sequence(self):
        self.parent = self.registry.revise('parent', {'identity': {**self.parent['identity'], 'sequence': 'ATGGCTACTGGTTAA'}})
        product = self.registry.import_record(self.document(value=definition([{'start': 0, 'end': 15}])))
        snapshot = self.registry.snapshot('product')
        original = deepcopy(snapshot)
        for module_name in ('rf3_adapter', 'boltz_adapter', 'openfold3_adapter', 'protenix_adapter', 'rfaa_adapter'):
            with self.subTest(model=module_name):
                destination = self.base / module_name; destination.mkdir()
                module = importlib.import_module(module_name)
                metadata = module.build(snapshot, destination, {}, {'msa_backend': 'public'})
                text = (destination / metadata['entrypoint']).read_text()
                if module_name == 'rfaa_adapter': text = (destination / 'sequences' / 'A.fasta').read_text()
                if module_name == 'rf3_adapter':
                    self.assertIn('(MET)', text)
                    self.assertIn('(ALA)', text)
                    self.assertIn('(THR)', text)
                    self.assertIn('(GLY)', text)
                else:
                    self.assertIn('MATG', text)
                self.assertEqual(snapshot, original)
                self.assertEqual(self.registry.show('product')['sha256'], product['sha256'])

    def test_legacy_literal_protein_and_backup_restore_remain_compatible(self):
        legacy = self.document('legacy')
        legacy['identity']['sequence'] = 'MA'
        legacy['identity']['encoded_by'].pop('translation')
        self.registry.import_record(legacy)
        old = self.registry.snapshot('legacy')
        self.assertNotIn('resolved_polymers', old)
        adapters.verify_snapshot(old)
        self.registry.import_record(self.document())
        backup = self.base / 'backup.tar.gz'
        self.registry.export_snapshot(backup)
        r.restore_backup(backup, self.base / 'restored')
        restored = r.Registry(self.base / 'restored')
        self.assertEqual(restored.snapshot('product'), self.registry.snapshot('product'))


if __name__ == '__main__':
    unittest.main()
