import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from inference.common import atomic_json, configuration_id, digest, inventory, now, read
from inference.dispatcher import Dispatcher
from inference.job_queue import Queue
from inference.resources import effective_cpus
from inference.worker import ResidentWorker


class DummyAdapter:
    loads = 0
    def __init__(self, config):
        self.config = config

    def load(self):
        type(self).loads += 1
        return {'native': 'fixture'}

    def predict(self, job, output_dir):
        if job.get('fail'):
            raise RuntimeError('deliberate native failure')
        path = Path(output_dir) / 'model.cif'
        path.write_text(job['id'])
        return {'structures': [str(path)], 'settings': self.config}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.queue = Queue(self.root / 'head/jobs.sqlite')
        self.config = {'model': 'fixture', 'native_config': {}, 'work_dir': str(self.root / 'scratch')}
        self.config_id = configuration_id(self.config)
        self.session = {'worker_id': 'worker1', 'config_id': self.config_id, 'config': self.config,
                        'deadline_epoch': now() + 3600, 'idle_seconds': 900,
                        'spool_root': str(self.root / 'shared')}
        self.registry = self.root / 'head/workers'
        atomic_json(self.registry / 'worker1.json', self.session)
        DummyAdapter.loads = 0

    def job(self, identity='job1', **extra):
        return dict(id=identity, config_id=self.config_id, model='fixture', **extra)

    def worker(self):
        worker = ResidentWorker(self.session, adapter_factory=DummyAdapter)
        self.addCleanup(worker.lock.close)
        worker.load()
        return worker

    def dispatcher(self, **kwargs):
        dispatcher = Dispatcher(self.queue, self.registry, **kwargs)
        self.addCleanup(dispatcher.close)
        return dispatcher

    def request(self, worker, identity):
        row = self.queue.get(identity)
        return worker.root / 'requests' / (row['token'] + '.json')

    def test_one_loaded_model_serves_two_distinct_jobs(self):
        worker = self.worker()
        dispatcher = self.dispatcher()
        for identity in ('job1', 'job2'):
            self.queue.enqueue(self.job(identity))
            self.assertFalse(dispatcher.tick()['errors'])
            worker.execute(self.request(worker, identity))
            self.assertFalse(dispatcher.tick()['errors'])
            self.assertEqual(self.queue.get(identity)['state'], 'complete')
            self.assertEqual((Path(self.queue.get(identity)['result']['output_dir']) / 'model.cif').read_text(), identity)
        self.assertEqual(DummyAdapter.loads, 1)

    def test_concurrent_dispatchers_do_not_duplicate_claim(self):
        self.queue.enqueue(self.job())
        def claim(n):
            return self.queue.claim(self.config_id, 'worker' + str(n), 'generation' + str(n), deadline=now()+300)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(claim, range(4)))
        self.assertEqual(sum(row is not None for row in rows), 1)
        self.assertEqual(len(self.queue.get('job1')['attempts']), 1)

    def test_idempotent_request_and_different_payload_rejection(self):
        first = self.queue.enqueue(self.job(), request_key='request')
        second = self.queue.enqueue(self.job('job2'), request_key='request')
        self.assertEqual(first['id'], second['id'])
        with self.assertRaises(ValueError):
            self.queue.enqueue(self.job('job3', seeds=[43]), request_key='request')

    def test_head_restart_recovers_committed_unpublished_request(self):
        worker = self.worker()
        self.queue.enqueue(self.job())
        first = self.queue.claim(self.config_id, worker.worker_id, worker.generation, deadline=self.session['deadline_epoch'])
        restarted = Dispatcher(Queue(self.queue.path), self.registry)
        self.addCleanup(restarted.close)
        self.assertFalse(restarted.tick()['errors'])
        self.assertEqual(read(self.request(worker, 'job1'))['token'], first['token'])
        worker.execute(self.request(worker, 'job1'))
        restarted.tick()
        self.assertEqual(self.queue.get('job1')['state'], 'complete')
        self.assertEqual(len(self.queue.get('job1')['attempts']), 1)

    def test_expired_result_is_fenced_and_retry_is_explicit(self):
        self.queue.enqueue(self.job())
        row = self.queue.claim(self.config_id, 'worker1', 'generation1', deadline=now()+300)
        with patch('inference.job_queue.now', return_value=row['lease_until'] + 1):
            self.queue.expire()
        self.assertEqual(self.queue.get('job1')['state'], 'interrupted')
        self.assertIsNone(self.queue.claim(self.config_id, 'worker2', 'generation2', deadline=now()+300))
        self.queue.retry('job1')
        second = self.queue.claim(self.config_id, 'worker2', 'generation2', deadline=now()+300)
        with self.assertRaises(ValueError):
            self.queue.finish('job1', row['token'], row['generation'], {})
        self.queue.finish('job1', second['token'], second['generation'], {})
        self.assertEqual([r['state'] for r in self.queue.get('job1')['attempts']], ['interrupted', 'predicted'])

    def test_failed_job_does_not_cancel_other_jobs(self):
        worker = self.worker()
        dispatcher = self.dispatcher()
        self.queue.enqueue(self.job(fail=True))
        self.queue.enqueue(self.job('job2'))
        dispatcher.tick()
        receipt = worker.execute(self.request(worker, 'job1'))
        dispatcher.tick()
        self.assertTrue(receipt['error']['worker_restart_required'])
        self.assertTrue(worker.stop.is_set())
        self.assertEqual(self.queue.get('job1')['state'], 'failed')
        self.assertEqual(self.queue.get('job2')['state'], 'queued')

    def test_slow_cpu_scoring_does_not_hold_gpu_slot(self):
        release = threading.Event()
        entered = threading.Event()
        def postprocess(job, receipt):
            entered.set()
            release.wait(5)
            return receipt
        worker = self.worker()
        dispatcher = self.dispatcher(postprocessor=postprocess)
        self.addCleanup(release.set)
        self.queue.enqueue(self.job())
        self.queue.enqueue(self.job('job2'))
        dispatcher.tick()
        worker.execute(self.request(worker, 'job1'))
        dispatcher.tick()
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.queue.get('job1')['state'], 'predicted')
        self.assertEqual(self.queue.get('job2')['state'], 'running')
        release.set()

    def test_tampered_output_is_not_published(self):
        worker = self.worker()
        dispatcher = self.dispatcher()
        self.queue.enqueue(self.job())
        dispatcher.tick()
        receipt = worker.execute(self.request(worker, 'job1'))
        (Path(receipt['output_dir']) / 'model.cif').write_text('tampered')
        self.assertTrue(dispatcher.tick()['errors'])
        self.assertEqual(self.queue.get('job1')['state'], 'running')

    def test_unsealed_extra_output_is_not_published(self):
        worker = self.worker()
        dispatcher = self.dispatcher()
        self.queue.enqueue(self.job())
        dispatcher.tick()
        receipt = worker.execute(self.request(worker, 'job1'))
        (Path(receipt['output_dir']) / 'unsealed.cif').write_text('later output')
        self.assertTrue(dispatcher.tick()['errors'])
        self.assertEqual(self.queue.get('job1')['state'], 'running')

    def test_added_input_before_or_during_prediction_fails_attempt(self):
        for during in (False, True):
            with self.subTest(during=during):
                inputs = self.root / ('inputs-' + str(during))
                inputs.mkdir()
                (inputs / 'input.json').write_text('{}')
                files = inventory(inputs)
                worker = self.worker()
                dispatcher = self.dispatcher()
                identity = 'mutation-' + str(during)
                self.queue.enqueue(self.job(identity, input_files={'root': str(inputs), 'files': files}))
                dispatcher.tick()
                original = worker.adapter.predict
                def mutate(job, output):
                    (inputs / 'extra.json').write_text('{}')
                    return original(job, output)
                if not during:
                    (inputs / 'extra.json').write_text('{}')
                with patch.object(worker.adapter, 'predict', side_effect=mutate if during else original) as predict:
                    receipt = worker.execute(self.request(worker, identity))
                    self.assertEqual(predict.call_count, 1 if during else 0)
                self.assertIn('inventory', receipt['error']['message'])
                dispatcher.tick()
                self.assertEqual(self.queue.get(identity)['state'], 'failed')
                worker.lock.close()

    def test_cpu_quota_not_host_count_sets_budget(self):
        quota = self.root / 'cpu.max'
        quota.write_text('800000 100000')
        self.assertEqual(effective_cpus(range(176), quota), 8)
        quota.write_text('max 100000')
        self.assertEqual(effective_cpus(range(16), quota), 16)

    def test_worker_scratch_does_not_change_model_queue(self):
        other = dict(self.config, work_dir='/different/worker/scratch')
        self.assertEqual(configuration_id(other), self.config_id)
        self.assertNotEqual(digest(other), digest(self.config))
        other['native_config'] = {'recycles': 1}
        self.assertNotEqual(configuration_id(other), self.config_id)

    def test_cpu_outcomes_remain_in_attempt_history_after_retry(self):
        self.queue.enqueue(self.job())
        first = self.queue.claim(self.config_id, 'worker1', 'gen1', deadline=now()+300)
        self.queue.finish('job1', first['token'], 'gen1', {'raw': 1})
        self.queue.postprocessed('job1', {'raw': 1}, error={'chemistry': 'failed'}, token=first['token'])
        self.queue.retry('job1')
        second = self.queue.claim(self.config_id, 'worker2', 'gen2', deadline=now()+300)
        self.queue.finish('job1', second['token'], 'gen2', {'raw': 2})
        with self.assertRaisesRegex(ValueError, 'earlier attempt'):
            self.queue.postprocessed('job1', {'raw': 1}, token=first['token'])
        self.queue.postprocessed('job1', {'raw': 2, 'validated': True}, token=second['token'])
        attempts = self.queue.get('job1')['attempts']
        self.assertEqual([r['state'] for r in attempts], ['failed', 'complete'])
        self.assertEqual(attempts[0]['error'], {'chemistry': 'failed'})
        self.assertEqual(attempts[1]['result'], {'raw': 2, 'validated': True})

    def test_two_cpu_dispatchers_run_one_postprocessor(self):
        entered, release = threading.Event(), threading.Event()
        count = []
        def process(job, receipt):
            count.append(job['id']); entered.set(); release.wait(5)
            return receipt
        self.queue.enqueue(self.job())
        row = self.queue.claim(self.config_id, 'worker1', 'gen1', deadline=now()+300)
        self.queue.finish('job1', row['token'], 'gen1', {'result': {}})
        first, second = self.dispatcher(postprocessor=process), self.dispatcher(postprocessor=process)
        self.addCleanup(release.set)
        first.postprocess()
        self.assertTrue(entered.wait(2))
        second.postprocess()
        second.processing['job1'].result(timeout=2)
        self.assertEqual(count, ['job1'])
        release.set()
        first.processing['job1'].result(timeout=2)
        second.postprocess()
        self.assertEqual(self.queue.get('job1')['state'], 'complete')

    def test_runtime_image_and_adapter_generation_change_queue_identity(self):
        first = dict(self.config, runtime_generation={'runtime_image_sha256': 'a'*64, 'adapter_source_sha256': 'b'*64})
        second = dict(first, runtime_generation={'runtime_image_sha256': 'c'*64, 'adapter_source_sha256': 'b'*64})
        self.assertNotEqual(configuration_id(first), configuration_id(second))

    def test_benchmark_worker_binding_cannot_migrate_to_compatible_gpu(self):
        self.queue.enqueue(self.job(required_worker_id='paired-gpu'))
        self.queue.enqueue(self.job('ordinary'))
        row = self.queue.claim(self.config_id, 'other-gpu', 'gen1', deadline=now()+300)
        self.assertEqual(row['id'], 'ordinary')
        paired = self.queue.claim(self.config_id, 'paired-gpu', 'gen2', deadline=now()+300)
        self.assertEqual(paired['id'], 'job1')


if __name__ == '__main__':
    unittest.main()
