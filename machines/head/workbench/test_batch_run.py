"""One-click intent/recovery tests using isolated files and fake owned units."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from workbench.api import API
from workbench.cli import rpc
from workbench.common import Error, canonical, now, uid, write_json
from workbench.runner import validation, verify_prepared
from workbench.service import Daemon
from workbench.store import Store
from workbench.test_scheduler import OwnedUnits


class BatchRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'state')
        self.api = API(self.store, 'harrison')
        self.config = {'tools_dir': str(Path(__file__).absolute().parents[1]),
                       'bio_submit': '/bin/false', 'library_root': str(self.root / 'library'),
                       'runtime_config': str(self.root / 'runtime.json'), 'max_jobs': 2,
                       'systemd_run': '/bin/false', 'systemctl': '/bin/false',
                       'inference_state': str(self.root / 'inference')}
        self.units = OwnedUnits()
        self.compiled = []

    def restart(self):
        return Daemon(Store(self.store.root), self.config,
                      system=self.units.state, launch=self.units.launch)

    def params(self, models=None):
        return {'request_key': uid(), 'name': 'one-click fixture',
                'models': models or ['protenix'],
                'inputs': [{'id': 'one', 'name': 'protein', 'molecule_type': 'protein',
                            'source': {'kind': 'text', 'format': 'sequence', 'text': 'ACDEFGHIK'}}]}

    def compiler(self, config, registry, reference, model, destination, backend, log):
        self.compiled.append((model, backend))
        if model == 'openfold3':
            raise ValueError('Fixture native chemistry rejects the declared bond')
        destination.mkdir()
        bundle = {'entrypoint': 'input.json', 'model': model, 'native_parser': True}
        write_json(destination / 'bundle.json', bundle)
        write_json(destination / 'input.json', registry.snapshot(reference))
        log.write_text('Fixture compiler only; no native executable, MSA or inference')
        return bundle

    def begin(self, params=None):
        batch = self.api.call('batch.run', params or self.params())
        self.restart().tick()
        return batch

    def receipt(self, batch, *, commit=True, state='complete'):
        ident = batch['batch_id']
        with self.store.connection() as db:
            row = dict(db.execute('SELECT * FROM operations WHERE object_id=?', (ident,)).fetchone())
        unit = self.units.states[row['unit']]
        binding = {'kind': 'validation', 'object_id': ident, 'unit': row['unit'],
                   'invocation_id': unit['InvocationID'], 'intent_sha256': row['intent_sha256'],
                   'started_at': now()}
        folder = self.store.directory('operations', ident)
        write_json(folder / 'started.json', binding)
        receipt = {**binding, 'state': state, 'finished_at': now()}
        write_json(folder / 'terminal.json', receipt)
        unit['SubState'] = 'exited'
        with self.store.transaction() as db:
            db.execute('UPDATE operations SET state=?,invocation_id=?,data=? WHERE object_id=?',
                       (state if commit else 'running', unit['InvocationID'], canonical(receipt).decode(), ident))
        return receipt

    def checked(self, batch, *, commit=True):
        with patch('workbench.inputs.source_pins', return_value={}):
            validation(self.store, batch['batch_id'], self.config, self.compiler)
        self.receipt(batch, commit=commit)
        return self.api.call('batch.get', {'batch_id': batch['batch_id']})

    def test_mixed_pairs_auto_queue_and_keep_rejections_in_final_partial_result(self):
        batch = self.begin(self.params(['protenix', 'openfold3', 'rfaa']))
        self.assertTrue(batch['auto_run'])
        self.assertEqual(batch['msa_backend'], 'private')
        self.assertEqual(batch['jobs'], [])
        self.checked(batch)
        self.assertEqual(self.restart().tick()['errors'], [])
        result = self.api.call('batch.get', {'batch_id': batch['batch_id']})
        self.assertEqual([p['state'] for p in result['pairs']], ['compatible', 'rejected', 'rejected'])
        self.assertIn('declared bond', result['pairs'][1]['reasons'][0])
        self.assertIn('not available', result['pairs'][2]['reasons'][0])
        self.assertEqual([job['model'] for job in result['jobs']], ['protenix'])
        self.assertEqual(result['counts']['rejected'], 2)
        self.assertEqual(self.compiled, [('protenix', 'private'), ('openfold3', 'private')])
        with self.store.transaction() as db:
            job = self.store.get(db, 'job', result['jobs'][0]['job_id'])
            job['state'] = 'complete'
            self.store.put(db, 'job', job)
        final = self.api.call('batch.get', {'batch_id': batch['batch_id']})
        self.assertEqual(final['state'], 'partial')
        self.assertEqual(final['counts']['complete'], 1)

    def test_all_rejected_finishes_without_jobs_and_replay_retains_reasons(self):
        params = self.params(['openfold3', 'rfaa'])
        batch = self.begin(params); self.checked(batch)
        self.assertEqual(self.restart().tick()['errors'], [])
        result = self.api.call('batch.run', params)
        self.assertEqual(result['state'], 'validation_failed')
        self.assertIn('No compatible', result['errors'][0])
        self.assertEqual(result['counts']['rejected'], 2)
        self.assertEqual(result['jobs'], [])
        self.assertEqual(len(self.units.launched), 1)  # Validation only.
        self.restart().tick()
        self.assertEqual(self.api.call('batch.run', params), result)

    def test_lost_initial_reply_replays_same_durable_run_after_client_restart(self):
        class LostReply:
            def write(self, _):
                raise BrokenPipeError('Fixture GUI disconnected before reply')
        params = self.params()
        wire = canonical({'id': 1, 'method': 'batch.run', 'params': params}) + b'\n'
        with self.assertRaises(BrokenPipeError):
            rpc(self.store, 'harrison', io.BytesIO(wire), LostReply())
        self.api = API(Store(self.store.root), 'harrison')
        batch = self.api.call('batch.run', params)
        self.assertEqual(len(self.store.listing('batch')), 1)
        self.restart().tick(); self.checked(batch); self.restart().tick()
        result = self.api.call('batch.run', params)
        self.assertEqual(result['batch_id'], batch['batch_id'])
        self.assertEqual(len(result['jobs']), 1)
        self.assertEqual(self.api.call('batch.run', params)['jobs'][0]['job_id'], result['jobs'][0]['job_id'])
        changed = {**params, 'name': 'Changed request'}
        with self.assertRaisesRegex(Error, 'different parameters'):
            self.api.call('batch.run', changed)

    def test_restart_recovers_terminal_receipt_then_enqueues_exactly_once(self):
        batch = self.begin(self.params(['protenix', 'boltz2']))
        self.checked(batch, commit=False)  # Runner died after terminal.json.
        self.units.states.clear()  # systemd forgot the completed validation unit.
        self.assertEqual(self.restart().tick()['errors'], [])
        first = self.api.call('batch.get', {'batch_id': batch['batch_id']})
        self.assertEqual(len(first['jobs']), 2)
        for _ in range(3):
            self.assertEqual(self.restart().tick()['errors'], [])
        second = self.api.call('batch.get', {'batch_id': batch['batch_id']})
        self.assertEqual([j['job_id'] for j in first['jobs']], [j['job_id'] for j in second['jobs']])
        self.assertEqual(len(self.units.launched), 3)  # One validation, two fake jobs.
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='run_queued'").fetchone()[0], 1)

    def test_running_validation_receipt_does_not_queue_early(self):
        batch = self.begin()
        with patch('workbench.inputs.source_pins', return_value={}):
            validation(self.store, batch['batch_id'], self.config, self.compiler)
        self.assertEqual(self.restart().tick()['errors'], [])
        self.assertEqual(self.store.listing('job'), [])
        self.receipt(batch)
        self.restart().tick()
        self.assertEqual(len(self.store.listing('job')), 1)

    def test_cancel_while_compiler_runs_stays_cancelled_and_never_queues(self):
        batch = self.begin()
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            self.assertTrue(release.wait(10))
            return self.compiler(*args)
        with patch('workbench.inputs.source_pins', return_value={}), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(validation, self.store, batch['batch_id'], self.config, blocked)
            try:
                self.assertTrue(entered.wait(10))
                cancelled = self.api.call('batch.cancel', {'batch_id': batch['batch_id']})
                self.assertEqual(cancelled['state'], 'cancelled')
            finally:
                release.set()
            future.result(timeout=10)
        self.receipt(batch)
        self.assertEqual(self.restart().tick()['errors'], [])
        self.assertEqual(self.api.call('batch.get', {'batch_id': batch['batch_id']})['state'], 'cancelled')
        self.assertEqual(self.store.listing('job'), [])
        self.assertEqual(len(self.units.launched), 1)

    def test_cancel_before_validation_and_after_check_before_enqueue(self):
        for phase in ('before', 'after'):
            with self.subTest(phase=phase):
                batch = self.api.call('batch.run', self.params())
                if phase == 'after':
                    self.restart().tick(); self.checked(batch)
                self.api.call('batch.cancel', {'batch_id': batch['batch_id']})
                self.restart().tick()
                self.assertEqual(self.api.call('batch.get', {'batch_id': batch['batch_id']})['state'], 'cancelled')
                self.assertEqual(self.store.listing('job'), [])

    def test_cancel_enqueue_race_has_no_uncancelled_jobs(self):
        batch = self.begin(); self.checked(batch)
        barrier = threading.Barrier(2)
        def enqueue():
            barrier.wait(); return API(Store(self.store.root), 'harrison')._automatic_run(batch['batch_id'])
        def cancel():
            barrier.wait(); return self.api.call('batch.cancel', {'batch_id': batch['batch_id']})
        with ThreadPoolExecutor(max_workers=2) as pool:
            one, two = pool.submit(enqueue), pool.submit(cancel)
            one.result(timeout=10); two.result(timeout=10)
        result = self.api.call('batch.get', {'batch_id': batch['batch_id']})
        self.assertEqual(result['state'], 'cancelled')
        self.assertTrue(all(job['state'] == 'cancelled' for job in self.store.listing('job')))
        self.assertLessEqual(len(self.store.listing('job')), 1)

    def test_queue_transaction_crash_rolls_back_all_pairs_and_retries_once(self):
        batch = self.begin(self.params(['protenix', 'boltz2'])); self.checked(batch)
        original = self.store.put
        count = 0
        def fail_second_job(db, kind, data, actor=None):
            nonlocal count
            if kind == 'job':
                count += 1
                if count == 2:
                    raise OSError('Fixture crash during queue transaction')
            return original(db, kind, data, actor)
        with patch.object(self.store, 'put', side_effect=fail_second_job):
            with self.assertRaisesRegex(OSError, 'Fixture crash'):
                self.api._automatic_run(batch['batch_id'])
        self.assertEqual(self.store.listing('job'), [])
        current = self.api.call('batch.get', {'batch_id': batch['batch_id']})
        self.assertEqual(current['state'], 'validated')
        self.assertTrue(all(pair['job_id'] is None for pair in current['pairs']))
        self.assertEqual(self.restart().tick()['errors'], [])
        self.assertEqual(len(self.store.listing('job')), 2)
        self.restart().tick()
        self.assertEqual(len(self.store.listing('job')), 2)

    def test_failed_or_missing_completion_evidence_never_queues(self):
        for evidence in ('failed', 'interrupted', 'missing'):
            with self.subTest(evidence=evidence):
                batch = self.begin(); self.checked(batch)
                with self.store.transaction() as db:
                    if evidence == 'missing':
                        db.execute('DELETE FROM operations WHERE object_id=?', (batch['batch_id'],))
                    else:
                        db.execute('UPDATE operations SET state=? WHERE object_id=?', (evidence, batch['batch_id']))
                self.restart().tick()
                result = self.api.call('batch.get', {'batch_id': batch['batch_id']})
                self.assertEqual(result['state'], 'validation_failed')
                self.assertIn('completion evidence', result['errors'][0])
                self.assertEqual(result['jobs'], [])

    def test_private_default_does_not_disable_workflows_without_msa(self):
        batch = self.begin(self.params(['esm', 'protenix'])); self.checked(batch)
        self.assertEqual(self.compiled, [('esm', 'public'), ('protenix', 'private')])
        self.restart().tick()
        result = self.api.call('batch.get', {'batch_id': batch['batch_id']})
        self.assertEqual(result['msa_backend'], 'private')
        self.assertEqual(len(result['jobs']), 2)
        jobs = {j['model']: self.store.read('job', j['job_id']) for j in result['jobs']}
        esm, folding = jobs['esm'], jobs['protenix']
        self.assertFalse(esm['provenance']['msa_applicable'])
        self.assertEqual(esm['provenance']['msa_backend'], 'not_applicable')
        self.assertEqual(esm['provenance']['msa_backend_requested'], 'private')
        for job, backend in ((esm, 'public'), (folding, 'private')):
            args = job['_prepared']['argv']
            self.assertEqual(args[0], '/bin/false')  # Never invoke real bio-submit.
            self.assertEqual(args[args.index('--msa-backend') + 1], backend)
            verify_prepared(job['_prepared'])
        self.assertFalse(esm['provenance']['automatic_retry'])

    def test_manual_preview_stays_manual_and_run_keys_are_actor_scoped(self):
        params = self.params()
        manual = self.api.call('batch.validate', params)
        automatic = self.api.call('batch.run', params)
        self.assertEqual(manual['msa_backend'], 'public')
        self.assertNotIn('auto_run', manual)
        self.assertNotEqual(manual['batch_id'], automatic['batch_id'])
        other = API(self.store, 'other')
        self.assertNotEqual(other.call('batch.run', params)['batch_id'], automatic['batch_id'])
        with self.assertRaisesRegex(Error, 'not found'):
            other.call('batch.get', {'batch_id': automatic['batch_id']})
        with patch('workbench.inputs.source_pins', return_value={}):
            validation(self.store, manual['batch_id'], self.config, self.compiler)
        self.restart().tick()
        self.assertEqual(self.api.call('batch.get', {'batch_id': manual['batch_id']})['state'], 'validated')
        self.assertEqual(self.store.listing('job'), [])
        committed = self.api.call('batch.create', {'batch_id': manual['batch_id'], 'request_key': uid(),
                                                  'pair_ids': [self.store.read('batch', manual['batch_id'])['pairs'][0]['pair_id']]})
        self.assertEqual(len(committed['jobs']), 1)
        with self.assertRaisesRegex(Error, 'automatically submits'):
            self.api.call('batch.create', {'batch_id': automatic['batch_id'], 'request_key': uid(),
                                          'pair_ids': [automatic['pairs'][0]['pair_id']]})

    def test_malformed_request_does_not_save_run_and_disabled_model_is_named_normally(self):
        for change in ({'models': ['unknown']}, {'settings': {'protenix': {'bogus': True}}}):
            with self.assertRaises(Error):
                self.api.call('batch.run', {**self.params(), **change})
        self.assertEqual(self.store.listing('batch'), [])
        from workbench.catalog import catalog
        rfaa = next(m for m in catalog()['models'] if m['id'] == 'rfaa')
        self.assertEqual(rfaa['name'], 'RoseTTAFold All-Atom')
        self.assertFalse(rfaa['enabled'])
        self.assertNotIn('parked', rfaa['disabled_reason'].lower())


if __name__ == '__main__':
    unittest.main()
