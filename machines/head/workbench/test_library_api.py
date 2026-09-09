"""Explorer reads preserve published identities and never submit work."""
import base64
import hashlib
from pathlib import Path
import tempfile
import unittest

from workbench.api import API
from workbench.common import Error
from workbench.inputs import library
from workbench.library_api import submission
from workbench.store import Store


class LibraryExplorerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.tools = Path(__file__).absolute().parents[1]
        self.module = library(self.tools)
        self.registry = self.module.Registry(self.base / 'library'); self.registry.init()
        self.store = Store(self.base / 'workbench')
        self.config = {'tools_dir': str(self.tools), 'library_root': str(self.registry.root)}
        self.api = API(self.store, 'alice', library_config=self.config)
        self.doc = self.base / 'purpose.md'
        self.doc.write_bytes(b'# Purpose\r\n\r\nAn untested binding hypothesis.\r\n')
        self.binary = self.base / 'original.bin'; self.binary.write_bytes(bytes(range(256)) * 10)

    def protein(self, ident='editor', **extra):
        return self.registry.import_record({'kind': 'construct', 'id': ident,
            'name': 'Editor ' + ident, 'aliases': ['alias-' + ident], 'tags': ['inhibition'],
            'identity': {'molecule_type': 'protein', 'sequence': 'ACDEFGHIK'},
            'provenance': {'_original_label': 'Keep this exact source field', 'inventory': 'Cas9 inhibitor'},
            **extra}, {'description.md': self.doc, 'original.bin': self.binary})

    def project(self):
        return self.registry.import_record(self.module.projects.project_document(
            'study', name='Shared objectives', members=[{'source_ref': 'editor', 'role': 'Binding control'}]),
            {'project.md': self.doc})

    def test_empty_shared_library_has_no_jobs_or_placeholder_records(self):
        result = self.api.call('library.list', {})
        self.assertEqual(result['records'], [])
        self.assertEqual(result['counts'], {'construct': 0, 'monomer': 0, 'assembly': 0, 'project': 0})
        self.assertEqual(self.api.call('batch.list', {})['batches'], [])

    def test_project_members_remain_pinned_when_latest_construct_changes(self):
        self.protein(); self.project()
        self.registry.revise('editor', {'name': 'New editor'})
        all_records = self.api.call('library.list', {})
        self.assertEqual(next(r for r in all_records['records'] if r['kind'] == 'construct')['revision'], 2)
        members = self.api.call('library.list', {'project_ref': 'study'})
        self.assertEqual(members['records'][0]['ref'], 'construct:editor@1')
        self.assertEqual(members['project_ref'], 'project:study@1')
        detail = self.api.call('library.get', {'ref': 'study'})
        self.assertEqual(detail['members'][0]['source_ref'], 'construct:editor@1')
        self.assertEqual(detail['members'][0]['role'], 'Binding control')
        self.assertFalse(detail['submission']['allowed'])

    def test_detail_preserves_full_record_hash_private_looking_metadata_and_markdown_bytes(self):
        record = self.protein()
        detail = self.api.call('library.get', {'ref': 'alias-editor'})
        self.assertEqual(detail['record'], record)
        self.module.verify_document(detail['record'])
        self.assertEqual(detail['description']['text'].encode(), self.doc.read_bytes())
        self.assertEqual(detail['description']['sha256'], hashlib.sha256(self.doc.read_bytes()).hexdigest())
        self.assertTrue(detail['submission']['allowed'])
        self.assertFalse(detail['description']['incomplete'])

    def test_history_and_project_links_follow_exact_revisions(self):
        self.protein(); self.project(); self.registry.revise('editor', {'notes': 'New annotation'})
        old = self.api.call('library.get', {'ref': 'construct:editor@1'})
        new = self.api.call('library.get', {'ref': 'editor'})
        self.assertEqual([r['revision'] for r in old['revisions']], [2, 1])
        self.assertEqual(old['projects'][0]['ref'], 'project:study@1')
        self.assertEqual(new['projects'], [])
        self.assertIn({'relation': 'parent revision', 'ref': 'construct:editor@1', 'label': 'Editor editor'}, new['relations'])
        self.registry.revise('study', {'identity': {'objectives_file': 'attachments/project.md',
                              'members': [{'source_ref': 'construct:editor@2', 'role': 'Updated member'}]}})
        old = self.api.call('library.get', {'ref': 'construct:editor@1'})
        new = self.api.call('library.get', {'ref': 'construct:editor@2'})
        self.assertEqual([r['ref'] for r in old['projects']], ['project:study@1'])
        self.assertEqual([r['ref'] for r in new['projects']], ['project:study@2'])

    def test_filters_and_pagination_do_not_mix_molecule_types(self):
        self.protein('alpha'); self.protein('beta')
        self.registry.import_record({'kind': 'construct', 'id': 'oligo',
            'identity': {'molecule_type': 'rna', 'sequence': 'ACGU'}})
        first = self.api.call('library.list', {'query': 'CAS9 inhibition', 'molecule_type': 'protein', 'limit': 1})
        self.assertEqual(first['filtered_count'], 2); self.assertTrue(first['truncated'])
        second = self.api.call('library.list', {'query': 'cas9 inhibition', 'molecule_type': 'protein', 'limit': 1, 'offset': first['next_offset']})
        self.assertNotEqual(first['records'][0]['ref'], second['records'][0]['ref'])
        self.assertIsNone(second['next_offset'])
        self.assertEqual(first['total_count'], 3)

    def test_attachment_chunks_reassemble_original_bytes_with_exact_receipt(self):
        self.protein()
        data = bytearray(); offset = 0
        while True:
            part = self.api.call('library.attachment', {'ref': 'construct:editor@1', 'name': 'original.bin', 'offset': offset, 'length': 511})
            self.assertEqual(part['offset'], offset)
            data.extend(base64.b64decode(part['data_b64']))
            offset = part['next_offset']
            if part['eof']:
                break
        self.assertEqual(bytes(data), self.binary.read_bytes())
        self.assertEqual(part['size'], len(data))
        self.assertEqual(part['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(part['ref'], 'construct:editor@1')

    def test_attachment_traversal_missing_names_and_invalid_ranges_reject(self):
        self.protein()
        for params in ({'name': '../record.json'}, {'name': '/etc/passwd'}, {'name': 'record.json'},
                       {'name': 'original.bin', 'offset': 99999}, {'name': 'original.bin', 'length': 999999},
                       {'name': 'original.bin', 'offset': True}):
            with self.subTest(params=params), self.assertRaises(Error):
                self.api.call('library.attachment', {'ref': 'editor', **params})

    def test_corruption_fails_closed_for_list_detail_and_attachment(self):
        self.protein()
        (self.registry.root / 'constructs/editor/1/attachments/original.bin').write_bytes(b'changed')
        for method, params in (('library.list', {}), ('library.get', {'ref': 'editor'}),
                               ('library.attachment', {'ref': 'editor', 'name': 'description.md'})):
            with self.subTest(method=method), self.assertRaisesRegex(Error, 'integrity'):
                self.api.call(method, params)

    def test_shared_library_does_not_make_job_history_shared(self):
        self.protein()
        other = API(self.store, 'bob', library_config=self.config)
        self.assertEqual(other.call('library.get', {'ref': 'editor'}), self.api.call('library.get', {'ref': 'editor'}))
        self.assertEqual(other.call('batch.list', {})['batches'], [])

    def test_inventory_presentation_keeps_alt_name_separate_from_source_label(self):
        self.protein(provenance={'source_inventory_identifier': 'pGC077',
            'inventory': {'identifier': 'pGC077', 'verbose_name': 'Long inventory label',
                          'alt_orf_name': 'Short alternative'}})
        detail = self.api.call('library.get', {'ref': 'editor'})
        self.assertEqual(detail['inventory_id'], 'pGC077')
        self.assertEqual(detail['alt_name'], 'Short alternative')
        self.assertEqual(detail['verbose_name'], 'Long inventory label')
        self.assertEqual(detail['modality'], 'protein')
        self.assertEqual(detail['sha256'], detail['record']['sha256'])
        self.assertTrue(detail['is_latest'])
        self.assertEqual(detail['latest_ref'], 'construct:editor@1')
        provenance = dict(detail['record']['provenance'])
        provenance['workbench'] = {'alt_name': ''}
        self.registry.revise('editor', {'provenance': provenance})
        self.assertEqual(self.api.call('library.get', {'ref': 'editor'})['alt_name'], '')
        old = self.api.call('library.get', {'ref': 'construct:editor@1'})
        self.assertFalse(old['is_latest'])
        self.assertEqual(old['alt_name'], 'Short alternative')

    def test_archive_filters_current_entity_without_deleting_pinned_history(self):
        original = self.protein(); self.project()
        self.registry.revise('editor', {'provenance': {'workbench': {'archived': True}}})
        active = self.api.call('library.list', {'project_ref': 'study'})
        self.assertEqual(active['records'], [])
        self.assertEqual(active['archived_count'], 1)
        archived = self.api.call('library.list', {'archived': True})
        self.assertEqual([r['ref'] for r in archived['records']], ['construct:editor@2'])
        self.assertEqual(self.api.call('library.get', {'ref': 'construct:editor@1'})['record'], original)
        self.registry.revise('editor', {'provenance': {'workbench': {'archived': False}}})
        restored = self.api.call('library.list', {'project_ref': 'study'})
        self.assertEqual([r['ref'] for r in restored['records']], ['construct:editor@1'])
        self.assertEqual(self.api.call('library.list', {'archived': True})['records'], [])
        for value in ('true', 1, None):
            with self.subTest(value=value), self.assertRaises(Error):
                self.api.call('library.list', {'archived': value})

    def test_archived_project_does_not_archive_its_molecular_members(self):
        self.protein(); self.project()
        self.registry.revise('study', {'provenance': {'workbench': {'archived': True}}})
        active = self.api.call('library.list', {})
        self.assertEqual(active['projects'], [])
        self.assertEqual([r['kind'] for r in active['records']], ['construct'])
        archived = self.api.call('library.list', {'archived': True})
        self.assertEqual([r['kind'] for r in archived['records']], ['project'])
        self.assertEqual(self.api.call('library.get', {'ref': 'study'})['members'][0]['ref'],
                         'construct:editor@1')

    def test_unresolved_products_and_whole_plasmids_cannot_be_added_to_runs(self):
        plasmid = {'kind': 'construct', 'name': 'Plasmid', 'identity': {'molecule_type': 'dna', 'strand_count': 2, 'molecular_form': 'plasmid'}}
        candidate = {'kind': 'construct', 'name': 'Candidate', 'identity': {'molecule_type': 'protein', 'product_review': {'status': 'review_required'}}}
        records = {'construct:plasmid@1': plasmid, 'construct:candidate@1': candidate}
        for ref, record in records.items():
            self.assertFalse(submission(record, records)['allowed'])
            assembly = {'kind': 'assembly', 'identity': {'components': [{'chain_id': 'A', 'construct_ref': ref}]}}
            self.assertFalse(submission(assembly, records)['allowed'])


if __name__ == '__main__':
    unittest.main()
