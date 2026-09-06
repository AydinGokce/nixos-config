import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from workbench.api import API
from workbench.cli import rpc
from workbench.common import CHUNK, WIRE, Error, canonical, file_sha, parse, uid, write_json
from workbench.inputs import request, native_compile, prepare_pair, source_pins
from workbench.runner import validation, verify_prepared, seal_results, run_job
from workbench.service import Daemon, configuration, matches
from workbench.store import Store


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / 'state'); self.api = API(self.store, 'harrison')
        self.config = {'tools_dir': str(Path(__file__).absolute().parents[1]), 'bio_submit': '/bin/true',
                       'library_root': str(self.root / 'library'), 'runtime_config': str(self.root / 'runtime.json'),
                       'max_jobs': 1, 'systemd_run': '/bin/false', 'systemctl': '/bin/false',
                       'inference_state': str(self.root / 'inference')}

    def upload(self, raw=b'>protein\nACDEFGHIK\n'):
        value = hashlib.sha256(raw).hexdigest()
        result = self.api.call('upload.begin', {'name': '../../display-only.fasta', 'size': len(raw), 'sha256': value})
        ident = result['upload_id']
        self.api.call('upload.chunk', {'upload_id': ident, 'offset': 0, 'data_base64': base64.b64encode(raw).decode()})
        return self.api.call('upload.finish', {'upload_id': ident, 'sha256': value})

    def params(self, models=None):
        return {'request_key': uid(), 'name': 'fixture', 'models': models or ['protenix'],
                'inputs': [{'id': 'one', 'name': 'protein', 'molecule_type': 'protein',
                            'source': {'kind': 'text', 'format': 'sequence', 'text': 'ACDEFGHIK'}}]}

    def compiler(self, config, registry, ref, model, destination, backend, log):
        if model == 'openfold3':
            raise ValueError('Concrete native fixture rejection: requested bond not representable')
        destination.mkdir()
        data = {'entrypoint': 'input.json', 'model': model, 'native_parser': True}
        write_json(destination / 'bundle.json', data)
        write_json(destination / 'input.json', registry.snapshot(ref))
        log.write_text('native CPU fixture passed')
        return data

    def validated(self, models=None):
        preview = self.api.call('batch.validate', self.params(models))
        with patch('workbench.inputs.source_pins', return_value={}):
            validation(self.store, preview['batch_id'], self.config, self.compiler)
        return self.api.call('batch.get', {'batch_id': preview['batch_id']})

    def committed(self):
        preview = self.validated()
        return self.api.call('batch.create', {'batch_id': preview['batch_id'], 'request_key': uid(),
                                            'pair_ids': [preview['pairs'][0]['pair_id']]})

    def test_upload_repeated_chunk_and_sealed_integrity(self):
        raw = b'ACDEFGHIK'; item = self.upload(raw)
        same = self.api.call('upload.chunk', {'upload_id': item['upload_id'], 'offset': 0, 'data_base64': base64.b64encode(raw).decode()})
        self.assertEqual(same['offset'], len(raw))
        with self.assertRaisesRegex(Error, 'differs'):
            self.api.call('upload.chunk', {'upload_id': item['upload_id'], 'offset': 0, 'data_base64': base64.b64encode(b'XXXXXXXXX').decode()})
        self.assertFalse((self.root / 'display-only.fasta').exists())

    def test_upload_recovery_after_bytes_precede_database_offset(self):
        raw = b'ACDE'
        item = self.api.call('upload.begin', {'name': 'file', 'size': 4})
        (self.store.directory('uploads', item['upload_id']) / 'content.part').write_bytes(raw)
        self.api.call('upload.chunk', {'upload_id': item['upload_id'], 'offset': 0, 'data_base64': base64.b64encode(raw).decode()})
        result = self.api.call('upload.finish', {'upload_id': item['upload_id'], 'sha256': hashlib.sha256(raw).hexdigest()})
        self.assertEqual(result['state'], 'complete')

    def test_upload_actor_scope_and_bad_hash(self):
        item = self.upload()
        with self.assertRaisesRegex(Error, 'not found'):
            API(self.store, 'another').call('upload.get', {'upload_id': item['upload_id']})
        with self.assertRaisesRegex(Error, 'SHA-256'):
            self.api.call('upload.finish', {'upload_id': item['upload_id'], 'sha256': '0' * 64})

    def test_upload_gap_chunk_limit_and_path_rejection(self):
        item = self.api.call('upload.begin', {'name': 'file', 'size': 10})
        with self.assertRaisesRegex(Error, 'gap'):
            self.api.call('upload.chunk', {'upload_id': item['upload_id'], 'offset': 3, 'data_base64': 'YQ=='})
        with self.assertRaises(Error):
            self.api.call('upload.chunk', {'upload_id': item['upload_id'], 'offset': 0, 'data_base64': base64.b64encode(b'a' * (CHUNK + 1)).decode()})
        with self.assertRaises(Error):
            self.api.call('upload.get', {'upload_id': '../state.sqlite'})

    def test_preview_does_not_create_jobs_and_reports_all_rejections(self):
        result = self.validated(['protenix', 'openfold3', 'rfaa'])
        self.assertEqual(result['state'], 'validated')
        self.assertEqual([p['state'] for p in result['pairs']], ['compatible', 'rejected', 'rejected'])
        self.assertIn('bond not representable', result['pairs'][1]['reasons'][0])
        self.assertEqual(self.store.listing('job'), [])

    def test_idempotency_and_explicit_selection_all_or_nothing(self):
        params = self.params(['protenix', 'openfold3'])
        first = self.api.call('batch.validate', params)
        self.assertEqual(self.api.call('batch.validate', params)['batch_id'], first['batch_id'])
        changed = dict(params, name='different')
        with self.assertRaisesRegex(Error, 'different parameters'):
            self.api.call('batch.validate', changed)
        with patch('workbench.inputs.source_pins', return_value={}):
            validation(self.store, first['batch_id'], self.config, self.compiler)
        preview = self.api.call('batch.get', {'batch_id': first['batch_id']})
        with self.assertRaisesRegex(Error, 'Every selected'):
            self.api.call('batch.create', {'batch_id': preview['batch_id'], 'request_key': uid(), 'pair_ids': [p['pair_id'] for p in preview['pairs']]})
        self.assertEqual(self.store.listing('job'), [])
        create = {'batch_id': preview['batch_id'], 'request_key': uid(), 'pair_ids': [preview['pairs'][0]['pair_id']]}
        result = self.api.call('batch.create', create)
        self.assertEqual(self.api.call('batch.create', create)['jobs'][0]['job_id'], result['jobs'][0]['job_id'])
        self.assertEqual(len(self.store.listing('job')), 1)

    def test_cancel_create_race_never_leaves_queued_work(self):
        for _ in range(4):
            preview = self.validated()
            barrier = threading.Barrier(2)
            def create():
                barrier.wait()
                try:
                    return self.api.call('batch.create', {'batch_id': preview['batch_id'], 'request_key': uid(), 'pair_ids': [preview['pairs'][0]['pair_id']]})
                except Error:
                    return None
            def cancel():
                barrier.wait(); return self.api.call('batch.cancel', {'batch_id': preview['batch_id']})
            with ThreadPoolExecutor(2) as pool:
                pending = [pool.submit(create), pool.submit(cancel)]
                [x.result() for x in pending]
            actual = self.api.call('batch.get', {'batch_id': preview['batch_id']})
            self.assertTrue(all(job['state'] == 'cancelled' for job in actual['jobs']))

    def test_queued_cancel_never_starts_systemd(self):
        batch = self.committed(); job = batch['jobs'][0]
        self.api.call('job.cancel', {'job_id': job['job_id']})
        launched = []
        daemon = Daemon(self.store, self.config, system=lambda *_: {'LoadState': 'not-found'}, launch=lambda *args, **kw: launched.append(args))
        daemon.tick()
        self.assertEqual(launched, [])

    def test_fasta_expansion_keeps_evolvepro_variant_set(self):
        params = self.params(['protenix', 'evolvepro'])
        params['inputs'][0]['source'] = {'kind': 'text', 'format': 'fasta', 'text': '>a\nACDE\n>b\nACDF\n'}
        from workbench.inputs import pairs
        combinations = pairs(self.store, 'harrison', request(params))
        self.assertEqual(sum(p['model'] == 'protenix' for p in combinations), 2)
        self.assertEqual(sum(p['model'] == 'evolvepro' for p in combinations), 1)

    def test_crosslink_never_replaced_by_plain_fasta(self):
        params = self.params()
        params['inputs'][0]['source'] = {'kind': 'text', 'format': 'library-json', 'text': json.dumps({
            'kind': 'construct', 'id': 'bonded', 'identity': {'molecule_type': 'protein', 'sequence': 'ACDC',
              'crosslinks': [{'from': {'position': 2, 'atom': 'SG'}, 'to': {'position': 4, 'atom': 'SG'}, 'order': 1}]}})}
        result = self.api.call('batch.validate', params)
        with patch('workbench.inputs.source_pins', return_value={}):
            validation(self.store, result['batch_id'], self.config, self.compiler)
        actual = self.store.read('batch', result['batch_id'])
        self.assertEqual(actual['pairs'][0]['state'], 'compatible', actual['pairs'][0]['reasons'])
        argv = actual['pairs'][0]['_prepared']['argv']
        self.assertIn('--construct', argv); self.assertNotIn('--fasta', argv)

    def test_native_input_tampering_prevents_launch(self):
        batch = self.committed(); job = self.store.read('job', batch['jobs'][0]['job_id'])
        prepared = job['_prepared']; verify_prepared(prepared)
        name = next(name for name in prepared['input_files'] if name.endswith('.fasta'))
        (Path(prepared['input_root']) / name).write_text('>changed\nAAAA\n')
        with self.assertRaisesRegex(Error, 'input changed'):
            verify_prepared(prepared)

    def test_nix_trusted_symlinks_resolve_but_upload_links_reject(self):
        target = self.root / 'config-real.json'; target.write_text(json.dumps({'tools_dir': self.config['tools_dir']}))
        link = self.root / 'config-link'; link.symlink_to(target)
        self.assertEqual(configuration(link)['max_jobs'], 1)
        item = self.upload()
        path = self.store.directory('uploads', item['upload_id']) / 'content'
        path.unlink(); path.symlink_to(target)
        with self.assertRaisesRegex(Error, 'Symlink'):
            file_sha(path)

    def test_unknown_unit_identity_is_not_cancelled_or_retried(self):
        batch = self.committed(); job = batch['jobs'][0]
        launches = []
        def run(args, **kwargs):
            launches.append(args)
            return subprocess.CompletedProcess(args, 0, '', '')
        daemon = Daemon(self.store, self.config, system=lambda *_: {'LoadState': 'not-found'}, launch=run)
        daemon.tick(); daemon.tick(); daemon.tick()
        self.assertEqual(len(launches), 1)
        self.assertIn(str(Path(self.config['tools_dir']) / 'workbench/cli.py'), launches[0])
        self.assertEqual(self.store.read('job', job['job_id'])['state'], 'interrupted')

    def test_exact_systemd_binding(self):
        command = ['/python', '-B', '/tool', '--id', 'abc']
        state = {'LoadState': 'loaded', 'InvocationID': 'a' * 32,
                 'ExecStart': '{ path=/python ; argv[]=/python -B /tool --id abc ; ignore_errors=no ; }'}
        self.assertTrue(matches(state, command))
        self.assertFalse(matches(state, command, 'b' * 32))
        self.assertFalse(matches(state, command[:-1] + ['different']))

    def test_restart_after_preintent_files_never_retries_launch(self):
        batch = self.committed(); job = batch['jobs'][0]
        folder = self.store.directory('operations', job['job_id']); folder.mkdir(parents=True)
        write_json(folder / 'config.json', {'partial': True}, exclusive=True)
        launches = []
        daemon = Daemon(self.store, self.config, system=lambda *_: {'LoadState': 'not-found'},
                        launch=lambda *args, **kwargs: launches.append(args))
        daemon.tick(); daemon.tick()
        self.assertEqual(launches, [])
        self.assertEqual(self.store.read('job', job['job_id'])['state'], 'interrupted')

    def test_result_artifacts_and_annotation_revision(self):
        batch = self.committed(); job = self.store.read('job', batch['jobs'][0]['job_id'])
        out = self.root / 'native'; (out / 'predictions').mkdir(parents=True)
        (out / 'predictions' / 'sample.cif').write_text('data_native\n')
        seal_results(self.store, job, out)
        artifact = self.api.call('job.artifacts', {'job_id': job['job_id']})['artifacts'][0]
        value = self.api.call('artifact.read', {'artifact_id': artifact['artifact_id'], 'max_bytes': 5})
        self.assertEqual(base64.b64decode(value['data_base64']), b'data_')
        annotation = self.api.call('annotation.put', {'artifact_id': artifact['artifact_id'], 'text': 'Residue 12', 'selection': {'chain': 'A', 'residues': [12]}})
        updated = self.api.call('annotation.put', {'artifact_id': artifact['artifact_id'], 'annotation_id': annotation['annotation_id'], 'expected_revision': 1, 'text': 'Updated'})
        self.assertEqual(updated['revision'], 2)
        with self.assertRaisesRegex(Error, 'conflicts'):
            self.api.call('annotation.put', {'artifact_id': artifact['artifact_id'], 'annotation_id': annotation['annotation_id'], 'expected_revision': 1, 'text': 'stale'})
        with self.assertRaisesRegex(Error, 'not found'):
            API(self.store, 'other').call('annotation.list', {'artifact_id': artifact['artifact_id']})

    def test_rpc_envelope_and_unknown_fields(self):
        output = io.BytesIO()
        rpc(self.store, 'harrison', io.BytesIO(b'{"id":"x","method":"catalog","params":{}}\n'), output)
        self.assertIn('models', parse(output.getvalue())['result'])
        for raw in (b'{"id":"x","method":"catalog","params":{"actor":"other"}}\n',
                    b'{"id":"x","id":"y","method":"catalog","params":{}}\n'):
            output = io.BytesIO(); rpc(self.store, 'harrison', io.BytesIO(raw), output)
            self.assertEqual(parse(output.getvalue())['error']['code'], 'invalid')

    def test_malformed_types_are_actionable(self):
        for field, value in [('mode', []), ('models', [{}]), ('execution', {}), ('msa_backend', [])]:
            params = self.params(); params[field] = value
            with self.assertRaises(Error):
                request(params)

    def test_batch_list_is_lightweight(self):
        batch = self.committed()
        summary = self.api.call('batch.list', {})['batches'][0]
        self.assertNotIn('jobs', summary); self.assertNotIn('pairs', summary)
        self.assertEqual(summary['counts']['jobs'], 1)
        detail = self.api.call('batch.get', {'batch_id': batch['batch_id']})
        self.assertEqual(detail['jobs'][0]['artifacts'], [])


    def running_fixture(self, script):
        batch = self.committed(); ident = batch['jobs'][0]['job_id']
        path = self.root / 'fixture-submit.py'; path.write_text(script)
        with self.store.transaction() as db:
            job = self.store.get(db, 'job', ident)
            job['state'] = 'starting'
            job['_prepared']['argv'] = [sys.executable, '-B', str(path)]
            job['_prepared']['timeout'] = 60
            self.store.put(db, 'job', job)
        return ident

    def test_owned_runner_seals_success_and_survives_client_absence(self):
        ident = self.running_fixture('''import os,pathlib
out=pathlib.Path(os.environ['BIO_RESULTS_DIR'])/'predictions';out.mkdir()
(out/'sample.cif').write_text('data_retained\\n')
print('fixture native complete',flush=True)
''')
        run_job(self.store, ident, self.config)
        restarted = API(Store(self.store.root), 'harrison')
        result = restarted.call('job.get', {'job_id': ident})
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(len([a for a in result['artifacts'] if a['role'] == 'structure']), 1)
        self.assertEqual(result['exit_code'], 0)

    def test_owned_cancel_allows_cleanup_and_retains_failed_output(self):
        flag = self.root / 'child-ready'
        ident = self.running_fixture('''import os,pathlib,signal,time,sys
out=pathlib.Path(os.environ['BIO_RESULTS_DIR']);(out/'partial.json').write_text('{"partial":true}')
def cleanup(sig,frame):
 (out/'cleanup.json').write_text('{"exact_owned_cleanup":true}')
 sys.exit(143)
signal.signal(signal.SIGTERM,cleanup)
pathlib.Path(''' + repr(str(flag)) + ''').write_text('ready')
while True: time.sleep(.05)
''')
        def cancel():
            import time
            for _ in range(200):
                if flag.exists():
                    self.api.call('job.cancel', {'job_id': ident}); return
                time.sleep(.02)
            raise AssertionError('Fixture child did not start')
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(cancel)
            run_job(self.store, ident, self.config); pending.result()
        result = self.api.call('job.get', {'job_id': ident})
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(result['exit_code'], 143)
        self.assertIn('cleanup.json', [a['name'] for a in result['artifacts']])
        self.assertIn('partial.json', [a['name'] for a in result['artifacts']])

    def test_import_retained_results_is_idempotent_and_never_enqueues(self):
        from workbench.import_retained import import_retained
        out = self.root / 'retained'; (out / 'predictions').mkdir(parents=True)
        (out / 'predictions' / 'sample.cif').write_text('data_existing\\n')
        first = import_retained(self.store, out, 'protenix', 'harrison', 'Retained validation — no new prediction')
        second = import_retained(self.store, out, 'protenix', 'harrison', 'Retained validation — no new prediction')
        self.assertEqual(first['batch_id'], second['batch_id'])
        self.assertFalse(first['inference_performed'])
        self.assertEqual(self.store.listing('job', states=['queued']), [])
        self.assertEqual(len(self.store.listing('artifact')), 1)

    def test_resident_client_exit_stays_tracked_and_recovers_after_restart(self):
        from inference.job_queue import Queue
        from inference.common import digest as native_digest, inventory as native_inventory, now as native_now
        from workbench.runner import reconcile_resident
        batch = self.committed(); ident = batch['jobs'][0]['job_id']
        owner = {'job_id': ident, 'token': 'a' * 32}
        payload = {'id': 'protenix-resident-fixture', 'model': 'protenix', 'config_id': 'b' * 64, 'provenance': {'workbench_owner': owner}}
        queue = Queue(Path(self.config['inference_state']) / 'jobs.sqlite')
        queue.enqueue(payload)
        native = queue.claim('b' * 64, 'worker-fixture', 'generation-fixture', deadline=native_now() + 300)
        receipt = {'request_id': payload['id'], 'workbench_owner': owner, 'request_sha256': native_digest(payload), 'state': 'enqueued'}
        folder = self.store.directory('jobs', ident); (folder / 'results').mkdir(parents=True)
        with self.store.transaction() as db:
            job = self.store.get(db, 'job', ident); job['state'] = 'running'
            job['_resident_pending'] = {'receipt': receipt, 'config': self.config, 'client_exit_code': 1}
            self.store.put(db, 'job', job)
        first = Daemon(Store(self.store.root), self.config, system=lambda *_: {'LoadState': 'not-found'})
        self.assertEqual(first.tick()['errors'], [])
        self.assertEqual(self.store.read('job', ident)['state'], 'running')
        self.assertEqual(queue.get(payload['id'])['token'], native['token'])
        out = self.root / 'native-completed'; (out / 'predictions').mkdir(parents=True)
        (out / 'predictions' / 'sample.cif').write_text('data_native_completed\\n')
        result = {'output_dir': str(out), 'files': native_inventory(out)}
        queue.finish(payload['id'], native['token'], native['generation'], result)
        queue.postprocessed(payload['id'], result, token=native['token'])
        old_pending = self.store.read('job', ident)
        restarted = Daemon(Store(self.store.root), self.config, system=lambda *_: {'LoadState': 'not-found'})
        self.assertEqual(restarted.tick()['errors'], [])
        actual = self.api.call('job.get', {'job_id': ident})
        self.assertEqual(actual['state'], 'complete')
        self.assertTrue(actual['provenance']['resident_reconciled'])
        count = actual['artifact_count']
        reconcile_resident(self.store, old_pending)  # crash replay after copy/seal
        self.assertEqual(self.api.call('job.get', {'job_id': ident})['artifact_count'], count)
        self.assertEqual(len(queue.get(payload['id'])['attempts']), 1)

    def test_abandoned_unclaimed_resident_is_cancelled_without_inference(self):
        from inference.job_queue import Queue
        from inference.common import digest as native_digest, now as native_now
        from workbench.runner import reconcile_resident
        batch = self.committed(); ident = batch['jobs'][0]['job_id']
        owner = {'job_id': ident, 'token': 'a' * 32}
        payload = {'id': 'resident-unclaimed', 'model': 'protenix', 'config_id': 'b' * 64, 'provenance': {'workbench_owner': owner}}
        queue = Queue(Path(self.config['inference_state']) / 'jobs.sqlite'); queue.enqueue(payload)
        (self.store.directory('jobs', ident) / 'results').mkdir(parents=True)
        with self.store.transaction() as db:
            job = self.store.get(db, 'job', ident); job['state'] = 'running'
            job['_resident_pending'] = {'receipt': {'request_id': payload['id'], 'workbench_owner': owner, 'request_sha256': native_digest(payload), 'state': 'enqueued'}, 'config': self.config, 'client_exit_code': 1}
            self.store.put(db, 'job', job)
        reconcile_resident(self.store, self.store.read('job', ident))
        self.assertEqual(self.store.read('job', ident)['state'], 'interrupted')
        self.assertEqual(queue.get(payload['id'])['state'], 'cancelled')
        self.assertIsNone(queue.claim('b' * 64, 'worker', 'generation', deadline=native_now() + 300))


if __name__ == '__main__':
    unittest.main()
