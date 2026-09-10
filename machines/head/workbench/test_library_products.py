"""Derived products remain reproducible across creation, edits, history and runs."""
from copy import deepcopy
import unittest

from workbench.common import Error
from workbench import library_edits as edits
from workbench import inputs
from workbench.api import API
from workbench.store import Store
from workbench import test_library_edits as curation_fixtures
from workbench.test_library_sequence import definition


class LibraryProductTests(unittest.TestCase):
    setUp = curation_fixtures.LibraryCurationTests.setUp
    project = curation_fixtures.LibraryCurationTests.project
    request = curation_fixtures.LibraryCurationTests.request
    reverse = curation_fixtures.LibraryCurationTests.reverse

    def seed(self, sequence='ATGGCTGGGTAA'):
        parent = self.registry.import_record({'kind': 'construct', 'id': 'plasmid',
            'name': 'Original plasmid', 'identity': {'molecule_type': 'dna',
                'sequence': sequence, 'circular': True, 'strand_count': 2, 'molecular_form': 'plasmid'}})
        self.project(refs=[self.module.reference(parent)])
        return parent

    def create(self, value=None, key=None, api=None):
        parent = self.registry.show('plasmid')
        self.counter += 1
        params = {'parent_ref': self.module.reference(parent), 'expected_sha256': parent['sha256'],
            'translation': value or definition([{'start': 0, 'end': 12}]),
            'alt_name': 'Product', 'request_key': key or f'create-{self.counter}'}
        return edits.create_product(api or self.api, params), params

    def peptide(self, ref):
        record = self.registry.show(ref)
        with self.registry._lock():
            records = self.registry._records_locked()
            return self.module.effective_sequence(record, records)

    def test_creation_canonical_coordinates_project_snapshot_preview_and_replay(self):
        parent = self.seed()
        value = definition([{'start': 0, 'end': 12}])
        preview = self.api.call('library.product_preview', {'parent_ref': 'plasmid', 'translation': value})
        self.assertEqual(preview['sequence'], 'MAG')
        self.assertEqual(preview['parent_sha256'], parent['sha256'])
        result, params = self.create(value)
        product = self.registry.show(result['ref'])
        self.assertNotIn('sequence', product['identity'])
        self.assertEqual(result['changed_refs'][0]['before_ref'], None)
        self.assertEqual(self.peptide(result['ref'])['sequence'], 'MAG')
        members = self.registry.show('study')['identity']['members']
        self.assertIn(result['ref'], [row['source_ref'] for row in members])
        detail = self.api.call('library.get', {'ref': result['ref']})
        self.assertEqual(detail['sequence_view']['sequence'], 'MAG')
        self.assertTrue(detail['submission']['allowed'])
        self.assertEqual(edits.create_product(self.api, params), result)
        with self.assertRaises(Error):
            edits.create_product(self.api, {**params, 'alt_name': 'Different'})
        self.registry.verify()

    def test_creation_undo_archives_redo_restores_and_survives_reopen(self):
        self.seed(); result, params = self.create()
        original = self.registry.show(result['ref'])
        archive = self.base / 'created-product.tar.gz'
        self.registry.export_snapshot(archive)
        restored = self.base / 'restored'
        self.module.restore_backup(archive, restored)
        self.registry = self.module.Registry(restored)
        self.api = API(Store(self.base / 'empty-service'), 'alice',
            library_config={**self.config, 'library_root': str(restored)})
        self.assertEqual(edits.create_product(self.api, params), result)
        self.reverse()
        self.assertTrue(edits.presentation(self.registry.show(original['id']))['archived'])
        self.assertEqual(self.registry.show(result['ref']), original)
        self.reverse('redo')
        self.assertFalse(edits.presentation(self.registry.show(original['id']))['archived'])
        self.assertEqual(self.peptide(original['id'])['sequence'], 'MAG')
        self.registry.verify()

    def test_run_input_copy_keeps_original_source_and_translates_exact_pin(self):
        self.seed(); result, _ = self.create()
        target = self.module.Registry(self.base / 'batch-library'); target.init()
        pinned = inputs.clone_record(self.registry, target, result['ref'], self.module)
        self.request({'sequence': 'ATGTGGGGGTAA'}, 'plasmid')
        snapshot = target.snapshot(pinned)
        import adapters
        adapters.verify_snapshot(snapshot)
        out = self.base / 'fasta'; out.mkdir()
        adapters.fasta(snapshot, out)
        self.assertEqual((out / 'input.fasta').read_text(), '>construct\nMAG\n')
        self.assertEqual(target.show(pinned), self.registry.show(result['ref']))
        self.assertNotIn('sequence', target.show(pinned)['identity'])

    def test_reference_match_moves_to_history_and_parent_undo_restores_it(self):
        self.seed(); result, _ = self.create(); ident = self.registry.show(result['ref'])['id']
        identity = deepcopy(self.registry.show(ident)['identity'])
        identity['product_review'] = {'status': 'reference_matched', 'source': 'reference.fasta',
            'source_product_id': ident, 'method_version': 'v1'}
        self.registry.revise(ident, {'identity': identity, 'tags': ['reference_matched']})
        self.request({'sequence': 'ATGTGGGGGTAA'}, 'plasmid')
        self.assertNotIn('product_review', self.registry.show(ident)['identity'])
        self.reverse()
        self.assertEqual(self.registry.show(ident)['identity']['product_review']['status'], 'reference_matched')
        self.assertEqual(self.peptide(ident)['sequence'], 'MAG')

    def test_standalone_same_type_direct_edit_and_undo(self):
        self.seed(); project = self.registry.show('study')
        result = self.api.call('library.create', {'project_ref': self.module.reference(project),
            'expected_sha256': project['sha256'], 'sequence': 'MAG', 'alt_name': 'Independent',
            'request_key': 'standalone'})
        protein = self.registry.show(result['ref'])
        self.assertEqual(protein['identity'], {'molecule_type': 'protein', 'sequence': 'MAG'})
        self.request({'sequence': 'MAW'}, protein['id'])
        self.assertEqual(self.peptide(protein['id'])['sequence'], 'MAW')
        self.reverse(); self.reverse()
        self.assertTrue(edits.presentation(self.registry.show(protein['id']))['archived'])
        self.reverse('redo')
        self.assertFalse(edits.presentation(self.registry.show(protein['id']))['archived'])

    def test_parent_edit_cascades_all_variants_and_one_project_revision(self):
        self.seed(); full, _ = self.create()
        cropped, _ = self.create(definition([{'start': 0, 'end': 12}], residue_start=1, residue_end=3))
        before_project = self.registry.show('study')['revision']
        response, _ = self.request({'sequence': 'ATGTGGGGGTAA'}, 'plasmid')
        full_id, cropped_id = (self.registry.show(result['ref'])['id'] for result in (full, cropped))
        self.assertEqual(self.peptide(full_id)['sequence'], 'MWG')
        self.assertEqual(self.peptide(cropped_id)['sequence'], 'WG')
        self.assertEqual(len(response['changed_refs']), 4)
        self.assertEqual(self.registry.show('study')['revision'], before_project + 1)
        self.assertEqual(self.peptide(full['ref'])['sequence'], 'MAG')
        self.reverse()
        self.assertEqual(self.peptide(full_id)['sequence'], 'MAG')
        self.assertEqual(self.peptide(cropped_id)['sequence'], 'AG')
        self.reverse('redo')
        self.assertEqual(self.peptide(full_id)['sequence'], 'MWG')
        self.registry.verify()

    def test_frameshift_unavailable_and_undo_recovers_coordinates(self):
        self.seed(); result, _ = self.create(); ident = self.registry.show(result['ref'])['id']
        self.request({'sequence_edit': {'start': 4, 'end': 4, 'replacement': 'A'}}, 'plasmid')
        view = self.peptide(ident)
        self.assertFalse(view['available']); self.assertIsNone(view['sequence'])
        detail = self.api.call('library.get', {'ref': ident})
        self.assertFalse(detail['submission']['allowed'])
        self.reverse()
        self.assertEqual(self.peptide(ident)['sequence'], 'MAG')
        self.reverse('redo')
        self.assertFalse(self.peptide(ident)['available'])

    def test_parent_then_child_edit_two_undos_two_redos(self):
        self.seed(); result, _ = self.create(); ident = self.registry.show(result['ref'])['id']
        self.request({'sequence': 'ATGTGGGGGTAA'}, 'plasmid')
        self.request({'translation': definition([{'start': 0, 'end': 12}], residue_start=1)}, ident)
        self.assertEqual(self.peptide(ident)['sequence'], 'WG')
        self.reverse(); self.reverse()
        self.assertEqual(self.peptide(ident)['sequence'], 'MAG')
        self.reverse('redo'); self.reverse('redo')
        self.assertEqual(self.peptide(ident)['sequence'], 'WG')

    def test_foreign_child_edit_blocks_parent_undo_without_partial_write(self):
        self.seed(); result, _ = self.create(); ident = self.registry.show(result['ref'])['id']
        self.request({'sequence': 'ATGTGGGGGTAA'}, 'plasmid')
        self.request({'alt_name': 'Bob changed this'}, ident, api=self.bob)
        old = self.registry.show('plasmid')
        with self.assertRaisesRegex(Error, 'derived protein has another edit'):
            self.reverse()
        self.assertEqual(self.registry.show('plasmid'), old)

    def test_new_product_blocks_parent_undo_and_direct_derived_edit_rejected(self):
        self.seed(); self.request({'sequence': 'ATGTGGGGGTAA'}, 'plasmid')
        result, _ = self.create(api=self.bob)
        with self.assertRaisesRegex(Error, 'new protein product'):
            self.reverse()
        with self.assertRaises(Error):
            self.request({'sequence': 'MAG'}, result['ref'], api=self.bob)
        self.assertEqual(self.peptide(result['ref'])['sequence'], 'MWG')

    def test_crop_and_metadata_parent_changes_keep_creation_undo_usable(self):
        self.seed(); result, _ = self.create(); ident = self.registry.show(result['ref'])['id']
        self.request({'alt_name': 'New plasmid label'}, 'plasmid', api=self.bob)
        self.assertEqual(self.peptide(ident)['sequence'], 'MAG')
        self.reverse(); self.reverse('redo')
        self.assertEqual(self.registry.show(ident)['identity']['encoded_by']['construct_ref'],
                         self.module.reference(self.registry.show('plasmid')))

    def test_invalid_definition_stale_parent_and_review_flags(self):
        parent = self.seed(); result, _ = self.create(); ident = self.registry.show(result['ref'])['id']
        with self.assertRaises(Error):
            self.create(definition([{'start': 0, 'end': 1000}]))
        self.request({'alt_name': 'New parent'}, 'plasmid')
        with self.assertRaises(Error):
            edits.create_product(self.api, {'parent_ref': self.module.reference(parent),
                'expected_sha256': parent['sha256'], 'translation': definition(), 'request_key': 'stale'})
        record = self.registry.show(ident)
        identity = deepcopy(record['identity'])
        identity['product_review'] = {'status': 'review_required', 'source': 'original.sqlite',
            'source_product_id': ident, 'method_version': 'v1'}
        self.registry.revise(ident, {'identity': identity, 'tags': ['review_required']})
        self.request({'sequence': 'ATGTGGGGGTAA'}, 'plasmid')
        self.assertEqual(self.registry.show(ident)['identity']['product_review']['status'], 'review_required')
        self.assertFalse(self.api.call('library.get', {'ref': ident})['submission']['allowed'])

    def test_reparent_undo_removes_only_its_added_membership_and_redo_restores(self):
        self.seed(); result, _ = self.create(); ident = self.registry.show(result['ref'])['id']
        other = self.registry.import_record({'kind': 'construct', 'id': 'other',
            'identity': {'molecule_type': 'dna', 'sequence': 'ATGGCTGGGTAA'}})
        self.request({'parent_ref': self.module.reference(other)}, ident)
        def member_ids():
            return {member['source_ref'].split('@')[0] for member in self.registry.show('study')['identity']['members']}
        self.assertIn('construct:other', member_ids())
        self.reverse()
        self.assertNotIn('construct:other', member_ids())
        self.assertEqual(self.registry.show(ident)['identity']['encoded_by']['construct_ref'], 'construct:plasmid@1')
        self.reverse('redo')
        self.assertIn('construct:other', member_ids())
        self.assertEqual(self.registry.show(ident)['identity']['encoded_by']['construct_ref'], 'construct:other@1')

    def test_reparent_undo_keeps_preexisting_parent_member(self):
        self.seed(); result, _ = self.create(); ident = self.registry.show(result['ref'])['id']
        other = self.registry.import_record({'kind': 'construct', 'id': 'other',
            'identity': {'molecule_type': 'dna', 'sequence': 'ATGGCTGGGTAA'}})
        project = self.registry.show('study'); identity = deepcopy(project['identity'])
        identity['members'].append({'source_ref': self.module.reference(other), 'role': 'Independent target'})
        self.registry.revise('study', {'identity': identity})
        self.request({'parent_ref': self.module.reference(other)}, ident)
        self.reverse()
        self.assertIn({'source_ref': self.module.reference(other), 'role': 'Independent target'},
                      self.registry.show('study')['identity']['members'])

    def test_reparent_undo_conflicts_when_added_parent_gets_another_product(self):
        self.seed(); result, _ = self.create(); ident = self.registry.show(result['ref'])['id']
        other = self.registry.import_record({'kind': 'construct', 'id': 'other',
            'identity': {'molecule_type': 'dna', 'sequence': 'ATGGCTGGGTAA'}})
        self.request({'parent_ref': self.module.reference(other)}, ident)
        edits.create_product(self.bob, {'parent_ref': self.module.reference(other), 'expected_sha256': other['sha256'],
            'translation': definition([{'start': 0, 'end': 12}]), 'request_key': 'bobs-product'})
        old = self.registry.show(ident)
        with self.assertRaisesRegex(Error, 'now used by another project member'):
            self.reverse()
        self.assertEqual(self.registry.show(ident), old)

    def test_complete_history_cycle_restores_new_archived_variant_coordinates(self):
        self.seed()
        self.request({'sequence': 'ATGTTTGCTGGGTAA'}, 'plasmid')
        result, _ = self.create(definition([{'start': 0, 'end': 15}], residue_start=1))
        ident = self.registry.show(result['ref'])['id']
        self.assertEqual(self.peptide(ident)['sequence'], 'FAG')
        for _ in range(2):
            self.reverse()  # Archive the creation.
            self.reverse()  # Restore the shorter parent and remap the archived product.
            self.assertFalse(self.peptide(ident)['available'])
            self.reverse('redo')
            self.reverse('redo')
            self.assertFalse(edits.presentation(self.registry.show(ident))['archived'])
            self.assertEqual(self.peptide(ident)['sequence'], 'FAG')

    def test_frame_edit_is_actual_sequence_revision_and_undo_preserves_old_snapshot(self):
        self.seed(); result, _ = self.create(); ident = self.registry.show(result['ref'])['id']
        initial = self.registry.show(ident)
        snapshot = self.registry.snapshot(result['ref'])
        changed, params = self.request({'frame_offset': 1}, ident)
        current = self.registry.show(ident)
        self.assertEqual(self.peptide(ident)['sequence'], 'WLG')
        self.assertEqual(current['identity']['encoded_by']['translation']['codon_start'], 2)
        self.assertEqual(current['identity']['encoded_by']['translation']['stop_policy'], 'first_stop')
        self.assertEqual(current['identity']['encoded_by']['translation']['schema'], 2)
        self.assertNotIn('sequence', current['identity'])
        self.assertEqual(self.registry.show(result['ref']), initial)
        self.assertEqual(self.registry.snapshot(result['ref']), snapshot)
        self.assertEqual(edits.edit(self.api, params), changed)
        self.reverse()
        self.assertEqual(self.peptide(ident)['sequence'], 'MAG')
        self.assertEqual(self.registry.show(ident)['identity']['encoded_by']['translation']['schema'], 1)
        self.reverse('redo')
        self.assertEqual(self.peptide(ident)['sequence'], 'WLG')
        self.assertEqual(self.api.call('library.get', {'ref': ident})['sequence_view']['sequence'], 'WLG')

    def test_frame_first_stop_and_invalid_frame_remain_repairable(self):
        self.seed('ATGAATAAATAA'); result, _ = self.create(); ident = self.registry.show(result['ref'])['id']
        self.assertEqual(self.peptide(ident)['sequence'], 'MNK')
        self.request({'frame_offset': 1}, ident)  # TGA is the first codon.
        self.assertFalse(self.peptide(ident)['available'])
        detail = self.api.call('library.get', {'ref': ident})
        self.assertFalse(detail['submission']['allowed'])
        self.assertEqual(detail['sequence_view']['source']['sequence'], 'ATGAATAAATAA')
        self.request({'frame_offset': 2}, ident)  # GAA, TAA => E.
        self.assertEqual(self.peptide(ident)['sequence'], 'E')
        self.assertEqual(self.api.call('library.get', {'ref': ident})['sequence_view']['codon_positions'], [[2, 3, 4]])
        self.request({'frame_offset': 0}, ident)
        self.assertEqual(self.peptide(ident)['sequence'], 'MNK')

    def test_unchanged_frame_keeps_existing_cds_initiation_and_malformed_writes_fail(self):
        self.seed(); result, _ = self.create(); ident = self.registry.show(result['ref'])['id']
        original = deepcopy(self.registry.show(ident)['identity'])
        reply, _ = self.request({'frame_offset': 0}, ident)
        self.assertFalse(reply['changed'])
        self.assertEqual(self.registry.show(ident)['identity'], original)
        for invalid in (-1, 3, True, 1.0, '1'):
            with self.subTest(invalid=invalid), self.assertRaises(Error):
                self.request({'frame_offset': invalid}, ident)
        for patch in ({'frame_offset': 1, 'sequence': 'AA'},
                      {'frame_offset': 1, 'translation': definition()},
                      {'frame_offset': 1, 'parent_ref': 'construct:plasmid@1'}):
            with self.assertRaises(Error):
                self.request(patch, ident)
        with self.assertRaises(Error):
            self.request({'frame_offset': 1}, 'plasmid')

    def test_frame_edit_survives_registry_backup_and_new_actor_api(self):
        self.seed(); result, _ = self.create(); ident = self.registry.show(result['ref'])['id']
        changed, _ = self.request({'frame_offset': 2}, ident)
        self.assertEqual(self.peptide(ident)['sequence'], 'GWV')
        archive = self.base / 'frame-edited.tar.gz'; self.registry.export_snapshot(archive)
        restored = self.base / 'frame-restored'; self.module.restore_backup(archive, restored)
        reopened = API(Store(self.base / 'fresh-service'), 'alice',
            library_config={**self.config, 'library_root': str(restored)})
        detail = reopened.call('library.get', {'ref': ident})
        self.assertEqual(detail['sequence_view']['translation']['codon_start'], 3)
        self.assertEqual(detail['sequence_view']['sequence'], 'GWV')
        reopened.call('library.undo', {'operation_id': changed['operation_id'], 'request_key': 'restore-frame'})
        self.assertEqual(reopened.call('library.get', {'ref': ident})['sequence_view']['sequence'], 'MAG')

    def test_rna_parent_creation_frame_and_undo_use_the_same_protein_type(self):
        parent = self.registry.import_record({'kind': 'construct', 'id': 'plasmid',
            'name': 'RNA coding template', 'identity': {'molecule_type': 'rna',
                'sequence': 'AUGGCUGGGUAA'}})
        self.project(refs=[self.module.reference(parent)])
        result, _ = self.create()
        ident = self.registry.show(result['ref'])['id']
        self.assertEqual(self.peptide(ident)['sequence'], 'MAG')
        self.request({'frame_offset': 1}, ident)
        detail = self.api.call('library.get', {'ref': ident})
        self.assertEqual(detail['record']['identity']['molecule_type'], 'protein')
        self.assertNotIn('sequence', detail['record']['identity'])
        self.assertEqual(detail['sequence_view']['source']['molecule_type'], 'rna')
        self.assertEqual(detail['sequence_view']['source']['sequence'], 'AUGGCUGGGUAA')
        self.assertEqual(detail['sequence_view']['sequence'], 'WLG')
        self.reverse()
        self.assertEqual(self.peptide(ident)['sequence'], 'MAG')
        self.reverse('redo')
        self.assertEqual(self.peptide(ident)['sequence'], 'WLG')


if __name__ == '__main__':
    unittest.main()
