"""Pinned research intent, immutable edits and complete backup round trips."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import registry as r


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.library = r.Registry(self.base/'library')
        self.library.init()
        self.brief = self.base/'brief.md'
        self.brief.write_bytes(b'# Objective\r\n\r\nTest substrate binding; no functional result is established.\r\n')

    def tearDown(self):
        self.temp.cleanup()

    def construct(self, ident='enzyme'):
        return self.library.import_record({'kind': 'construct', 'id': ident,
            'identity': {'molecule_type': 'protein', 'sequence': 'ACDE'}})

    def project(self, members=None, ident='study'):
        document = r.projects.project_document(ident, name='Binding study', members=members)
        return self.library.import_record(document, {'project.md': self.brief})

    def test_new_construct_description_is_explicitly_incomplete_and_inherited(self):
        record = self.construct()
        receipt = r.projects.description_receipt(record)
        self.assertTrue(receipt['present'])
        original = self.library.attachment_path(r.reference(record), receipt['path']).read_bytes()
        self.assertEqual(r.projects.markdown_state(original), 'incomplete_scaffold')
        self.assertIn(b'Intended function', original)
        self.assertIn(b'Testable success criteria', original)
        self.assertIn(b'does not establish', original)
        changed = self.library.revise('enzyme', {'name': 'Renamed enzyme'})
        self.assertEqual(self.library.attachment_path(r.reference(changed), receipt['path']).read_bytes(), original)
        self.assertEqual(record['identity'], changed['identity'])

    def test_describe_preserves_raw_markdown_and_chemistry_in_new_revision(self):
        old = self.construct()
        original_record = self.library.record_path(r.reference(old)).read_bytes()
        updated = self.library.describe('enzyme', self.brief)
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(updated['identity'], old['identity'])
        self.assertIn(r.reference(old), updated['parents'])
        self.assertEqual(self.library.attachment_path(r.reference(updated), 'attachments/description.md').read_bytes(), self.brief.read_bytes())
        self.assertEqual(self.library.record_path(r.reference(old)).read_bytes(), original_record)
        self.assertEqual(r.projects.markdown_state(self.brief.read_bytes()), 'unassessed')
        with self.assertRaisesRegex(r.Error, 'stale revision'):
            self.library.describe(r.reference(old), self.brief)

    def test_project_requires_real_utf8_brief_but_may_start_without_members(self):
        document = r.projects.project_document('study')
        with self.assertRaisesRegex(r.Error, 'project.md'):
            self.library.import_record(document)
        self.assertEqual(self.library.list(), [])
        for raw in (b'', b'  \n', b'\xff', b'ok\x00hidden', b'x'*(r.projects.MAX_MARKDOWN_BYTES+1)):
            with self.subTest(raw=raw[:10]):
                self.brief.write_bytes(raw)
                with self.assertRaises(r.Error):
                    self.library.import_record(document, {'project.md': self.brief})
                self.assertEqual(self.library.list(), [])
        self.brief.write_text('# New objectives\n')
        project = self.project()
        self.assertEqual(project['identity']['members'], [])
        self.assertEqual(self.library.attachment_path('study', 'attachments/project.md').read_bytes(), self.brief.read_bytes())

    def test_project_pins_members_and_their_purpose_revisions(self):
        self.construct()
        description = self.library.describe('enzyme', self.brief)
        self.library.import_record({'kind': 'assembly', 'id': 'complex', 'identity': {
            'components': [{'chain_id': 'A', 'construct_ref': 'enzyme'}], 'bonds': []}})
        members = ['enzyme', {'source_ref': 'complex', 'role': 'Evaluate the bound assembly hypothesis.'}]
        raw = copy.deepcopy(members)
        project = self.project(members)
        self.assertEqual(members, raw)
        self.assertEqual([m['source_ref'] for m in project['identity']['members']],
                         ['construct:enzyme@2', 'assembly:complex@1'])
        snapshot = self.library.project_snapshot('study')
        r.verify_document(snapshot)
        self.assertEqual(snapshot['members'][0]['snapshot']['components'][0]['record'], description)
        self.assertEqual(snapshot['members'][0]['description']['sha256'],
                         r.projects.description_receipt(description)['sha256'])
        self.library.revise('enzyme', {'identity': {'molecule_type': 'protein', 'sequence': 'ACDEF'}})
        self.library.describe('complex', self.brief)
        self.assertEqual(snapshot, self.library.project_snapshot('study'))
        with self.assertRaisesRegex(r.Error, 'construct or assembly'):
            self.library.snapshot('study')

    def test_project_membership_and_brief_change_only_in_explicit_project_revision(self):
        self.construct()
        first = self.project(['enzyme'])
        old_snapshot = self.library.project_snapshot('study')
        self.library.revise('enzyme', {'notes': 'new input context'})
        update = r.projects.project_document('study', members=['enzyme'])['identity']
        second = self.library.revise('study', {'identity': update})
        self.assertEqual(second['identity']['members'][0]['source_ref'], 'construct:enzyme@2')
        self.assertEqual(self.library.project_snapshot(r.reference(first)), old_snapshot)
        old_bytes = self.library.attachment_path(r.reference(second), 'attachments/project.md').read_bytes()
        self.brief.write_text('# Revised objective\nMeasure affinity under specified conditions.\n')
        third = self.library.describe('study', self.brief)
        self.assertEqual(third['identity'], second['identity'])
        self.assertEqual(self.library.attachment_path(r.reference(second), 'attachments/project.md').read_bytes(), old_bytes)
        self.assertNotEqual(third['sha256'], second['sha256'])

    def test_member_kinds_duplicates_unknown_refs_and_unsafe_brief_path_fail(self):
        self.construct()
        self.library.import_record({'kind': 'monomer', 'id': 'custom', 'identity': {'ccd': 'MSE'}})
        self.project(['enzyme'])
        for members in (['missing'], ['custom'], ['study'], ['enzyme', 'construct:enzyme@1']):
            with self.subTest(members=members), self.assertRaises(r.Error):
                self.project(members, 'invalid')
        document = r.projects.project_document('bad')
        document['identity']['objectives_file'] = '../escape.md'
        with self.assertRaises(r.Error):
            self.library.import_record(document, {'project.md': self.brief})
        link = self.base/'linked.md'; link.symlink_to(self.brief)
        with self.assertRaisesRegex(r.Error, 'Symlinks'):
            self.library.describe('enzyme', link)

    def test_legacy_library_and_missing_description_remain_readable(self):
        record = self.construct()
        path = self.library.record_path('enzyme')
        legacy = copy.deepcopy(record)
        legacy['attachments'] = []
        legacy['sha256'] = r.digest_json(legacy)
        path.write_text(json.dumps(legacy))
        (path.parent/'attachments/description.md').unlink()
        shutil.rmtree(self.library.root/'projects')
        self.assertEqual(self.library.show('enzyme'), legacy)
        self.assertFalse(r.projects.description_receipt(legacy)['present'])
        self.assertEqual(self.library.verify()['records'], 1)
        project = self.project(['enzyme'])
        self.assertFalse(self.library.project_snapshot(r.reference(project))['members'][0]['description']['present'])
        updated = self.library.revise('enzyme', {'notes': 'New revision adds explicit incomplete purpose scaffold'})
        self.assertTrue(r.projects.description_receipt(updated)['present'])
        self.assertEqual(self.library.show('construct:enzyme@1'), legacy)

    def test_project_backup_restore_preserves_every_pinned_doc_and_snapshot(self):
        self.construct()
        self.library.describe('enzyme', self.brief)
        self.project(['enzyme'])
        expected = self.library.project_snapshot('study')
        archive = self.base/'library.tar.gz'
        self.library.export_snapshot(archive)
        restored = self.base/'restored'
        r.restore_backup(archive, restored)
        copy_library = r.Registry(restored)
        self.assertEqual(copy_library.project_snapshot('study'), expected)
        self.assertEqual(copy_library.attachment_path('study', 'attachments/project.md').read_bytes(), self.brief.read_bytes())
        self.assertEqual(copy_library.attachment_path('enzyme', 'attachments/description.md').read_bytes(), self.brief.read_bytes())
        path = copy_library.attachment_path('study', 'attachments/project.md')
        path.write_text('tampered')
        with self.assertRaisesRegex(r.Error, 'integrity'):
            copy_library.project_snapshot('study')

    def test_dynamic_registry_import_uses_adjacent_projects_without_pythonpath(self):
        isolated = self.base/'isolated'; isolated.mkdir()
        source = Path(r.__file__).parent
        for filename in ('registry.py', 'projects.py'):
            shutil.copyfile(source/filename, isolated/filename)
        spec = importlib.util.spec_from_file_location('isolated_registry', isolated/'registry.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.assertEqual(module.projects.filename('project'), 'project.md')
        self.assertEqual(module.Registry(self.library.root).verify()['records'], 0)


if __name__ == '__main__':
    unittest.main()
