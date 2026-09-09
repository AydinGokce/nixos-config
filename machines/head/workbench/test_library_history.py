"""Library run associations retain revision identity and job ownership."""
from pathlib import Path
import tempfile
import unittest

from workbench.api import API
from workbench.common import Error, uid
from workbench.inputs import library
from workbench.library_history import list_runs
from workbench.store import Store


class LibraryRunHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        tools = Path(__file__).absolute().parents[1]
        self.module = library(tools)
        self.registry = self.module.Registry(base / 'library'); self.registry.init()
        self.registry.import_record({'kind': 'construct', 'id': 'editor', 'name': 'Same name',
            'identity': {'molecule_type': 'protein', 'sequence': 'ACDEFGHIK'}})
        self.registry.revise('editor', {'notes': 'Second immutable revision'})
        self.registry.import_record({'kind': 'construct', 'id': 'other', 'name': 'Same name',
            'identity': {'molecule_type': 'protein', 'sequence': 'ACDEFGHIK'}})
        self.store = Store(base / 'workbench')
        self.api = API(self.store, 'alice', library_config={
            'tools_dir': str(tools), 'library_root': str(self.registry.root)})

    def job(self, source='construct:editor@1', actor='alice', mode='batch', extra=None,
            input_id='selected', artifact=False, created=None):
        bid, jid = uid(), uid()
        declared = {'kind': 'library', 'ref': source} if isinstance(source, str) else source
        inputs = [{'id': 'selected', 'source': declared}]
        inputs.extend(extra or [])
        batch = {'batch_id': bid, 'name': 'Retained run', 'state': 'complete',
                 '_request': {'mode': mode, 'inputs': inputs}}
        job = {'job_id': jid, 'batch_id': bid, 'input_id': input_id, 'input_name': 'Same name',
               'model': 'rf3', 'state': 'complete', '_prepared': {'private': 'not returned'}}
        if created:
            job['created_at'] = created
        with self.store.transaction() as db:
            self.store.put(db, 'batch', batch, actor)
            self.store.put(db, 'job', job, actor)
            if artifact:
                self.store.put(db, 'artifact', {'artifact_id': uid(), 'job_id': jid,
                    'name': 'fold.cif', 'format': 'mmcif', '_path': 'private'}, actor)
                self.store.put(db, 'artifact', {'artifact_id': uid(), 'job_id': jid,
                    'name': 'scores.json', 'format': 'json'}, actor)
        return jid

    def runs(self, **kwargs):
        return list_runs(self.api, {'ref': 'construct:editor@2', **kwargs})

    def test_empty_and_no_accidental_name_sequence_or_floating_match(self):
        self.job('construct:other@1')
        self.job('editor')
        self.job({'kind': 'text', 'format': 'sequence', 'text': 'ACDEFGHIK'})
        self.assertEqual(self.runs()['records'], [])
        self.assertEqual(self.runs()['match'], 'explicit_library_reference')

    def test_exact_revisions_and_native_artifact_counts_without_internal_fields(self):
        old = self.job(artifact=True)
        new = self.job('construct:editor@2')
        result = self.runs()['records']
        self.assertEqual([r['job_id'] for r in result], [new, old])
        self.assertTrue(result[0]['selected_revision'])
        self.assertFalse(result[1]['selected_revision'])
        self.assertEqual(result[1]['source_refs'], ['construct:editor@1'])
        self.assertEqual((result[1]['artifact_count'], result[1]['structure_count']), (2, 1))
        self.assertNotIn('_prepared', result[1])
        self.assertEqual([r['job_id'] for r in self.runs(include_revisions=False)['records']], [new])

    def test_actor_boundary_applies_to_both_jobs_and_batches(self):
        self.job(actor='bob')
        mismatch = self.job()
        with self.store.transaction() as db:
            db.execute("UPDATE objects SET actor='bob' WHERE kind='batch' AND id="
                       "(SELECT json_extract(data,'$.batch_id') FROM objects WHERE id=?)", (mismatch,))
        self.assertEqual(self.runs()['records'], [])

    def test_independent_input_pairs_do_not_inherit_another_input(self):
        self.job(input_id='other', extra=[{'id': 'other', 'source': {
            'kind': 'library', 'ref': 'construct:other@1'}}])
        self.assertEqual(self.runs()['records'], [])
        assembly = self.job(mode='assembly', input_id='assembly')
        self.assertEqual([r['job_id'] for r in self.runs()['records']], [assembly])

    def test_explicit_assembly_pin_matches_its_actual_component_revisions(self):
        self.registry.import_record({'kind': 'assembly', 'id': 'complex', 'identity': {
            'components': [{'chain_id': 'A', 'construct_ref': 'construct:editor@1'},
                           {'chain_id': 'B', 'construct_ref': 'construct:other@1'}], 'bonds': []}})
        jid = self.job('assembly:complex@1')
        result = self.runs()['records']
        self.assertEqual(result[0]['job_id'], jid)
        self.assertEqual(result[0]['source_refs'], ['construct:editor@1'])
        self.assertFalse(result[0]['selected_revision'])
        self.assertEqual(self.runs(include_revisions=False)['records'], [])

    def test_stable_cursor_pagination_with_identical_timestamps(self):
        ids = [self.job(created='2026-09-09T00:00:00+00:00') for _ in range(5)]
        seen, params = [], {'limit': 2}
        while True:
            page = self.runs(**params)
            seen.extend(r['job_id'] for r in page['records'])
            if page['next_cursor'] is None:
                break
            params['cursor'] = page['next_cursor']
        self.assertEqual(seen, sorted(ids, reverse=True))

    def test_unrelated_or_foreign_cursor_cannot_probe_other_history(self):
        for jid in (self.job(actor='bob'), self.job('construct:other@1'), uid()):
            with self.subTest(jid=jid), self.assertRaisesRegex(Error, 'Unknown run history cursor'):
                self.runs(cursor=jid)

    def test_limits_and_reference_validation(self):
        for options in ({'limit': 0}, {'limit': 101}, {'limit': True}, {'cursor': '../bad'},
                        {'include_revisions': 1}, {'ref': 'construct:missing@1'}, {'extra': 1}):
            with self.subTest(options=options), self.assertRaises(Error):
                self.runs(**options)


if __name__ == '__main__':
    unittest.main()
