"""Curation cannot overwrite history, discard chemistry, or expose half edits."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from workbench.api import API
from workbench.common import Error
from workbench.inputs import library
from workbench import library_edits as edits
from workbench.library_api import submission
from workbench.store import Store


class LibraryCurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.tools = Path(__file__).absolute().parents[1]
        self.module = library(self.tools)
        self.registry = self.module.Registry(self.base / 'library'); self.registry.init()
        self.config = {'tools_dir': str(self.tools), 'library_root': str(self.registry.root)}
        self.store = Store(self.base / 'workbench')
        self.api = API(self.store, 'alice', library_config=self.config)
        self.bob = API(self.store, 'bob', library_config=self.config)
        self.purpose = self.base / 'purpose.md'; self.purpose.write_bytes(b'# Intended purpose\r\n\r\nUnassessed.\r\n')
        self.original = self.base / 'original.gb'; self.original.write_bytes(b'ORIGINAL\x00SOURCE\r\n')
        self.counter = 0

    def protein(self, ident='editor', *, review=None, identity=None, provenance=None):
        values = {'molecule_type': 'protein', 'sequence': 'ACDEFGHIK', **(identity or {})}
        if review:
            values['product_review'] = {'status': review, 'source': 'original.sqlite',
                'source_product_id': ident, 'method_version': 'v1'}
        return self.registry.import_record({'kind': 'construct', 'id': ident,
            'name': 'pGC077 — Original verbose label (protein product)', 'status': 'defined',
            'identity': values, 'tags': [review] if review else [],
            'provenance': provenance or {'source_inventory_identifier': 'pGC077',
                'inventory': {'alt_orf_name': 'Editor', 'verbose_name': 'Original verbose label'}}},
            {'description.md': self.purpose, 'original.gb': self.original})

    def project(self, ident='study', refs=None):
        return self.registry.import_record(self.module.projects.project_document(
            ident, name='Research objective', members=[{'source_ref': ref, 'role': 'Target'}
                                                     for ref in (refs or ['editor'])]),
            {'project.md': self.purpose})

    def request(self, patch_value, ref='editor', api=None, request_key=None):
        record = self.registry.show(ref)
        self.counter += 1
        params = {'ref': self.module.reference(record), 'expected_sha256': record['sha256'],
                  'request_key': request_key or f'edit-{self.counter}', 'patch': patch_value}
        return edits.edit(api or self.api, params), params

    def reverse(self, action='undo', *, api=None, request_key=None):
        api = api or self.api
        status = edits.history(api, {})
        self.counter += 1
        params = {'operation_id': status[action]['operation_id'],
                  'request_key': request_key or f'{action}-{self.counter}'}
        return getattr(edits, action)(api, params), params

    def test_presentation_exact_source_values_empty_override_and_nondict_inventory(self):
        record = self.protein()
        self.assertEqual(edits.presentation(record), {'inventory_id': 'pGC077', 'alt_name': 'Editor',
            'verbose_name': 'Original verbose label', 'modality': 'protein', 'archived': False})
        result, _ = self.request({'alt_name': ''})
        current = self.registry.show(result['ref'])
        self.assertEqual(edits.presentation(current)['alt_name'], '')
        self.assertEqual(current['name'], record['name'])
        self.assertEqual(current['provenance']['inventory']['alt_orf_name'], 'Editor')
        self.assertEqual(edits.presentation(self.protein('odd', provenance={'inventory': 'legacy'}))['alt_name'], '')

    def test_archive_restore_revision_pins_historical_bytes_and_project_membership(self):
        original = self.protein(); project = self.project()
        original_bytes = self.registry._path('construct:editor@1').read_bytes()
        result, _ = self.request({'archived': True})
        self.assertEqual(result['changed_refs'], [
            {'before_ref': 'construct:editor@1', 'after_ref': 'construct:editor@2'},
            {'before_ref': 'project:study@1', 'after_ref': 'project:study@2'}])
        self.assertTrue(edits.presentation(self.registry.show('editor'))['archived'])
        self.assertEqual(self.registry.show('study')['identity']['members'][0]['source_ref'], 'construct:editor@2')
        self.assertEqual(self.registry.show('project:study@1'), project)
        self.assertEqual(self.registry._path('construct:editor@1').read_bytes(), original_bytes)
        self.assertEqual(self.registry.show('construct:editor@1'), original)
        self.request({'archived': False})
        self.assertFalse(edits.presentation(self.registry.show('editor'))['archived'])
        self.assertEqual(self.registry.attachment_path('editor', 'attachments/original.gb').read_bytes(), self.original.read_bytes())
        self.assertEqual(self.registry.verify()['records'], 6)

    def test_archive_project_keeps_members_and_can_undo_project_rename(self):
        self.protein(); self.project()
        self.request({'name': 'New research objective'}, 'study')
        self.request({'archived': True}, 'study')
        self.assertEqual(len(self.registry.show('study')['identity']['members']), 1)
        self.reverse(); self.reverse()
        self.assertFalse(edits.presentation(self.registry.show('study'))['archived'])
        self.assertEqual(self.registry.show('study')['name'], 'Research objective')
        self.reverse('redo'); self.reverse('redo')
        self.assertEqual(self.registry.show('study')['name'], 'New research objective')
        self.assertTrue(edits.presentation(self.registry.show('study'))['archived'])

    def test_request_replay_survives_new_api_and_conflicts_on_changed_payload(self):
        self.protein(); self.project()
        result, params = self.request({'alt_name': 'New'}, request_key='durable-key')
        api = API(Store(self.base / 'another-workbench'), 'alice', library_config=self.config)
        self.assertEqual(edits.edit(api, params), result)
        self.assertEqual(self.registry.verify()['records'], 4)
        with self.assertRaisesRegex(Error, 'different parameters') as exc:
            edits.edit(api, {**params, 'patch': {'alt_name': 'Other'}})
        self.assertEqual(exc.exception.code, 'conflict')
        with self.assertRaisesRegex(Error, 'changed'):
            edits.edit(api, {**params, 'request_key': 'new-key'})

    def test_undo_redo_stack_and_branch_survive_multiple_revisions(self):
        self.protein(); self.project()
        first, _ = self.request({'alt_name': 'A'})
        second, _ = self.request({'sequence': 'ACDEFGHIKL'})
        self.reverse(); undone, undo_params = self.reverse()
        self.assertEqual(edits.presentation(self.registry.show('editor'))['alt_name'], 'Editor')
        self.assertEqual(self.registry.show('editor')['identity']['sequence'], 'ACDEFGHIK')
        self.assertEqual(edits.undo(self.api, undo_params), undone)
        self.assertEqual(edits.history(self.api, {})['redo']['operation_id'], first['operation_id'])
        self.reverse('redo'); self.reverse('redo')
        self.assertEqual(edits.history(self.api, {})['undo']['operation_id'], second['operation_id'])
        self.reverse()
        self.request({'archived': True})
        self.assertIsNone(edits.history(self.api, {})['redo'])

    def test_conflicting_actor_cannot_undo_or_overwrite_other_edits(self):
        self.protein()
        alice, _ = self.request({'alt_name': 'Alice'})
        self.assertIsNone(edits.history(self.bob, {})['undo'])
        with self.assertRaisesRegex(Error, 'history changed'):
            edits.undo(self.bob, {'operation_id': alice['operation_id'], 'request_key': 'no-access'})
        self.request({'alt_name': 'Bob'}, api=self.bob)
        with self.assertRaisesRegex(Error, 'another edit'):
            self.reverse()
        self.assertEqual(edits.presentation(self.registry.show('editor'))['alt_name'], 'Bob')
        self.reverse(api=self.bob)
        self.reverse()
        self.assertEqual(edits.presentation(self.registry.show('editor'))['alt_name'], 'Editor')

    def test_other_construct_edits_do_not_block_project_propagated_undo(self):
        self.protein(); self.protein('other'); self.project(refs=['editor', 'other'])
        self.request({'alt_name': 'Alice'})
        self.request({'alt_name': 'Bob'}, 'other', api=self.bob)
        self.reverse()
        current = self.registry.show('study')['identity']['members']
        self.assertEqual([member['source_ref'] for member in current], ['construct:editor@3', 'construct:other@2'])

    def test_project_undo_preserves_current_pins_after_undoing_member_edit(self):
        self.protein(); self.project()
        self.request({'name': 'Renamed objective'}, 'study')
        self.request({'alt_name': 'Edited member'})
        self.reverse()
        self.reverse()
        project = self.registry.show('study')
        self.assertEqual(project['name'], 'Research objective')
        self.assertEqual(project['identity']['members'][0]['source_ref'], 'construct:editor@3')
        self.reverse('redo')
        self.assertEqual(self.registry.show('study')['name'], 'Renamed objective')
        self.assertEqual(self.registry.show('study')['identity']['members'][0]['source_ref'], 'construct:editor@3')
        self.reverse('redo')
        self.assertEqual(self.registry.show('study')['identity']['members'][0]['source_ref'], 'construct:editor@4')

    def test_project_undo_preserves_other_actor_member_edit_but_conflicts_on_same_field(self):
        self.protein(); self.project()
        self.request({'archived': True}, 'study')
        self.request({'alt_name': 'Bob'}, api=self.bob)
        self.reverse()
        self.assertFalse(edits.presentation(self.registry.show('study'))['archived'])
        self.assertEqual(self.registry.show('study')['identity']['members'][0]['source_ref'], 'construct:editor@2')
        self.request({'name': 'Alice'}, 'study')
        self.request({'name': 'Bob'}, 'study', api=self.bob)
        with self.assertRaisesRegex(Error, 'project field has another edit'):
            self.reverse()

    def test_sequence_change_preserves_exact_input_and_historical_evidence_without_false_matching(self):
        dna = self.registry.import_record({'kind': 'construct', 'id': 'plasmid',
            'identity': {'molecule_type': 'dna', 'sequence': 'ACGT', 'circular': True,
                         'strand_count': 2, 'molecular_form': 'plasmid'}})
        original = self.protein(review='reference_matched', identity={'encoded_by': {
            'construct_ref': self.module.reference(dna), 'sequence_sha256': hashlib.sha256(b'ACGT').hexdigest()}})
        self.project()
        result, _ = self.request({'sequence': 'ACDEFGHIKLM'})
        current = self.registry.show(result['ref'])
        self.assertEqual(current['identity'], {'molecule_type': 'protein', 'sequence': 'ACDEFGHIKLM'})
        self.assertNotIn('reference_matched', current['tags'])
        historical = current['provenance']['workbench']['sequence_edit']
        self.assertEqual(historical['historical_identity_claims']['product_review'], original['identity']['product_review'])
        self.assertEqual(historical['historical_identity_claims']['encoded_by'], original['identity']['encoded_by'])
        self.assertEqual(historical['source_ref'], 'construct:editor@1')
        self.assertEqual(current['attachments'], original['attachments'])
        self.assertEqual(self.registry.show('construct:editor@1'), original)
        self.assertTrue(submission(current, {})['allowed'])
        self.reverse()
        self.assertEqual(self.registry.show('editor')['identity'], original['identity'])

    def test_noop_sequence_cannot_clear_review_and_has_no_undo_step(self):
        original = self.protein(review='review_required')
        result, params = self.request({'sequence': 'ACDEFGHIK'})
        current = self.registry.show(result['ref'])
        self.assertFalse(result['changed'])
        self.assertEqual(current['identity'], original['identity'])
        self.assertEqual(current['tags'], ['review_required'])
        self.assertFalse(submission(current, {})['allowed'])
        self.assertIsNone(result['history']['undo'])
        self.assertEqual(edits.edit(self.api, params), result)

    def test_sequence_formatting_chemistry_and_caps_are_rejected_without_any_revision(self):
        self.protein(identity={'modifications': [{'position': 2, 'description': 'Synthetic residue'}]})
        for value in ('acd', 'AC DE', '>seq\nACDE', '', 'ACD*', 'A' * (edits.MAX_SEQUENCE + 1), 'ACDE'):
            with self.subTest(value=value[:20]), self.assertRaises(Error):
                self.request({'sequence': value})
        self.assertEqual(self.registry.verify()['records'], 1)
        self.assertEqual(self.registry.show('editor')['identity']['modifications'][0]['position'], 2)
        self.assertIsNone(edits.history(self.api, {})['undo'])

    def test_new_records_and_history_restore_without_workbench_database(self):
        self.protein(); self.project()
        result, params = self.request({'alt_name': 'Backed up'})
        archive = self.base / 'library.tar.gz'
        self.registry.export_snapshot(archive)
        with tarfile.open(archive) as contents:
            self.assertTrue(all(member.isfile() for member in contents.getmembers()))
        restored = self.base / 'restored'
        self.module.restore_backup(archive, restored)
        config = {**self.config, 'library_root': str(restored)}
        api = API(Store(self.base / 'empty-service'), 'alice', library_config=config)
        self.assertEqual(edits.edit(api, params), result)
        self.assertEqual(edits.history(api, {})['undo']['operation_id'], result['operation_id'])
        edits.undo(api, {'operation_id': result['operation_id'], 'request_key': 'restore-undo'})
        self.assertEqual(edits.presentation(self.module.Registry(restored).show('editor'))['alt_name'], 'Editor')

    def test_failed_preparation_never_publishes_first_valid_revision(self):
        protein = self.protein(); project = self.project()
        with self.assertRaises(self.module.Error):
            self.registry.revise_many([
                {'ref': 'construct:editor@1', 'expected_sha256': protein['sha256'], 'patch': {'name': 'Staged'}},
                {'ref': 'project:study@1', 'expected_sha256': project['sha256'],
                 'patch': {'identity': {'objectives_file': 'attachments/project.md',
                                       'members': [{'source_ref': 'construct:missing@1', 'role': ''}]}}},
            ])
        self.assertEqual(self.registry.verify()['records'], 2)
        self.assertEqual(self.registry.show('editor'), protein)
        self.assertEqual(list((self.registry.root / '.staging').iterdir()), [])

    def test_interrupted_publication_recovers_before_concurrent_reads_and_replay(self):
        self.protein(); self.project()
        original_install = self.module.Registry._install_publication
        installed = 0
        def interrupt(registry, temporary, ref):
            nonlocal installed
            installed += 1
            if installed == 2:
                raise OSError('simulated termination after target publication')
            return original_install(registry, temporary, ref)
        record = self.registry.show('editor')
        params = {'ref': 'construct:editor@1', 'expected_sha256': record['sha256'],
                  'request_key': 'interrupted', 'patch': {'alt_name': 'Recovered'}}
        with patch.object(self.module.Registry, '_install_publication', interrupt), self.assertRaises(Error):
            edits.edit(self.api, params)
        self.assertTrue(self.registry._pending_transactions())
        def observe(_):
            registry = self.module.Registry(self.registry.root)
            with registry._lock():
                records = registry._records_locked()
                self.assertIn('construct:editor@2', records)
                self.assertIn('project:study@2', records)
                return records['project:study@2']['identity']['members'][0]['source_ref']
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(observe, range(4))), ['construct:editor@2'] * 4)
        self.assertFalse(self.registry._pending_transactions())
        replay = edits.edit(self.api, params)
        self.assertEqual(replay['ref'], 'construct:editor@2')
        self.assertEqual(self.registry.verify()['records'], 4)

    def test_readers_wait_until_transaction_finishes(self):
        self.protein(); self.project()
        prepared = threading.Event(); release = threading.Event(); observed = threading.Event()
        original_install = self.module.Registry._install_publication
        def pause(registry, temporary, ref):
            result = original_install(registry, temporary, ref)
            if ref == 'construct:editor@2':
                prepared.set()
                self.assertTrue(release.wait(5))
            return result
        def read():
            result = self.registry.show('study')
            observed.set()
            return result
        with patch.object(self.module.Registry, '_install_publication', pause), ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(self.request, {'alt_name': 'Atomic'})
            self.assertTrue(prepared.wait(5))
            reader = pool.submit(read)
            time.sleep(0.05)
            self.assertFalse(observed.is_set())
            release.set()
            writer.result(timeout=5)
            self.assertEqual(reader.result(timeout=5)['identity']['members'][0]['source_ref'], 'construct:editor@2')

    def test_post_journal_rename_fsync_failure_keeps_staged_recovery_bytes(self):
        self.protein(); self.project()
        original_fsync = self.module.fsync_directory
        def fail_after_journal_rename(path):
            if path.name == '.transactions' and list(path.glob('*.json')):
                raise OSError('simulated journal directory fsync failure')
            return original_fsync(path)
        with patch.object(self.module, 'fsync_directory', fail_after_journal_rename), self.assertRaises(Error):
            self.request({'alt_name': 'Survives fsync failure'})
        self.assertEqual(len(self.registry._pending_transactions()), 1)
        self.assertEqual(len(list((self.registry.root / '.staging').iterdir())), 2)
        self.assertEqual(edits.presentation(self.registry.show('editor'))['alt_name'], 'Survives fsync failure')
        self.assertEqual(self.registry.show('study')['identity']['members'][0]['source_ref'], 'construct:editor@2')
        self.assertFalse(self.registry._pending_transactions())
        self.assertEqual(self.registry.verify()['records'], 4)

    def test_inherited_attachments_share_immutable_bytes_but_external_imports_do_not(self):
        self.protein(); self.project()
        first = self.registry._path('construct:editor@1').parent / 'attachments/original.gb'
        self.assertNotEqual(first.stat().st_ino, self.original.stat().st_ino)
        self.request({'alt_name': 'Linked'})
        second = self.registry._path('construct:editor@2').parent / 'attachments/original.gb'
        self.assertEqual((first.stat().st_dev, first.stat().st_ino), (second.stat().st_dev, second.stat().st_ino))
        project_first = self.registry._path('project:study@1').parent / 'attachments/project.md'
        project_second = self.registry._path('project:study@2').parent / 'attachments/project.md'
        self.assertEqual(project_first.stat().st_ino, project_second.stat().st_ino)
        self.original.write_bytes(b'EXTERNAL EDIT')
        self.assertEqual(second.read_bytes(), b'ORIGINAL\x00SOURCE\r\n')
        self.registry.verify()
        second.write_bytes(b'TAMPERED\x00SOURCE\r\n')
        with self.assertRaisesRegex(self.module.Error, 'Attachment integrity failure'):
            self.registry.verify()

    def test_digest_reuse_detects_mutation_through_historical_path_even_with_restored_mtime(self):
        self.protein(); self.request({'alt_name': 'Linked'})
        first = self.registry._path('construct:editor@1').parent / 'attachments/original.gb'
        original_digest = self.module.file_digest
        changed = False
        def mutate_after_first_digest(path, cache=None):
            nonlocal changed
            result = original_digest(path, cache)
            if Path(path) == first and not changed:
                changed = True
                stat = first.stat()
                first.write_bytes(b'TAMPERED\x00SOURCE\r\n')
                os.utime(first, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            return result
        with patch.object(self.module, 'file_digest', mutate_after_first_digest), self.assertRaisesRegex(
                self.module.Error, 'Attachment integrity failure'):
            self.registry.verify()
        self.assertTrue(changed)


if __name__ == '__main__':
    unittest.main()
