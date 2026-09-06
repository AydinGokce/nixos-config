from pathlib import Path
import tempfile
import unittest

from inference.common import now
from inference.job_queue import Queue


class CancelQueuedTests(unittest.TestCase):
    def test_cancelled_request_cannot_be_claimed(self):
        with tempfile.TemporaryDirectory() as root:
            queue = Queue(Path(root) / 'jobs.sqlite')
            queue.enqueue({'id': 'request', 'config_id': 'a' * 64, 'model': 'fixture'})
            self.assertEqual(queue.cancel_queued('request')['state'], 'cancelled')
            self.assertIsNone(queue.claim('a' * 64, 'worker', 'generation', deadline=now() + 300))
            self.assertEqual(queue.get('request')['attempts'], [])

    def test_active_execution_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            queue = Queue(Path(root) / 'jobs.sqlite')
            queue.enqueue({'id': 'request', 'config_id': 'a' * 64, 'model': 'fixture'})
            active = queue.claim('a' * 64, 'worker', 'generation', deadline=now() + 300)
            after = queue.cancel_queued('request')
            self.assertEqual(after['state'], 'running')
            self.assertEqual(after['token'], active['token'])
            self.assertEqual(after['attempt'], 1)
