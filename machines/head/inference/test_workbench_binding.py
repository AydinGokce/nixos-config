from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from inference.common import atomic_json, read, now
from inference.frontend import workbench_binding, enqueue_bound
from inference.job_queue import Queue
from workbench.runner import cancel_resident, request_cancel


class WorkbenchBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'resident-binding.json'
        atomic_json(self.path, {'schema': 1, 'job_id': 'a' * 32, 'token': 'b' * 32})
        self.environment = patch.dict('os.environ', {'BIO_WORKBENCH_BINDING_FILE': str(self.path)})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.queue = Queue(self.root / 'jobs.sqlite')
        self.job = {'id': 'protenix-resident-20260906-220000-12345678', 'model': 'protenix', 'config_id': 'c' * 64, 'provenance': {'name': 'évidence'}}

    def test_intent_precedes_enqueue_and_reconciles_crash(self):
        receipt = workbench_binding(self.job)
        self.assertEqual(read(receipt)['state'], 'intent')
        self.assertIsNone(self.queue.get(self.job['id']))
        self.queue.enqueue(self.job)  # Crash before post-enqueue receipt write.
        config = {'tools_dir': str(Path(__file__).parents[1]), 'inference_state': str(self.root)}
        self.assertEqual(cancel_resident(config, read(receipt))['state'], 'cancelled')
        self.assertIsNone(self.queue.claim('c' * 64, 'worker', 'generation', deadline=now() + 300))

    def test_repeat_binding_is_idempotent_but_foreign_request_rejects(self):
        receipt = workbench_binding(self.job)
        self.assertEqual(workbench_binding(self.job), receipt)
        enqueue_bound(self.queue, self.job, receipt)
        self.assertEqual(read(receipt)['state'], 'enqueued')
        self.assertEqual(enqueue_bound(self.queue, self.job, receipt)['attempt'], 0)
        with self.assertRaisesRegex(ValueError, 'different resident request'):
            workbench_binding(dict(self.job, id='different'))

    def test_spoofed_receipt_cannot_cancel_another_request(self):
        receipt = workbench_binding(self.job); enqueue_bound(self.queue, self.job, receipt)
        forged = read(receipt); forged['workbench_owner'] = {'job_id': 'e' * 32, 'token': 'f' * 32}
        config = {'tools_dir': str(Path(__file__).parents[1]), 'inference_state': str(self.root)}
        with self.assertRaisesRegex(ValueError, 'not owned'):
            cancel_resident(config, forged)
        self.assertEqual(self.queue.get(self.job['id'])['state'], 'queued')

    def test_nonprivate_binding_rejects(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private owned'):
            workbench_binding(self.job)

    def test_cancellation_wins_before_enqueue_and_fences_later_call(self):
        config = {'tools_dir': str(Path(__file__).parents[1]), 'inference_state': str(self.root)}
        receipt, result = request_cancel(self.root, 'a' * 32, 'b' * 32, config)
        self.assertIsNone(receipt)
        binding = workbench_binding(self.job)
        with self.assertRaisesRegex(ValueError, 'cancelled before resident enqueue'):
            enqueue_bound(self.queue, self.job, binding)
        self.assertIsNone(self.queue.get(self.job['id']))
