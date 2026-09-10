"""Observable warm-up estimates, without changing any index reads or admission."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import session


class WarmProgressTests(unittest.TestCase):
    def observe(self, value, now, completed, total=700_000_000_000):
        return value.observe({'read_bytes': completed, 'total_bytes': total}, now)

    def test_initial_residency_and_zero_byte_wait_never_enter_throughput(self):
        value = session.WarmProgress()
        self.assertEqual(self.observe(value, 0, 0)['eta']['state'], 'unknown')
        self.assertEqual(self.observe(value, 300, 0)['eta']['state'], 'unknown')
        self.assertEqual(self.observe(value, 3600, 1_000_000_000)['eta']['state'], 'unknown')
        self.assertEqual(self.observe(value, 3604, 9_000_000_000)['eta']['state'], 'unknown')
        result = self.observe(value, 3605, 11_000_000_000)
        self.assertEqual(result['eta']['state'], 'estimate')
        self.assertAlmostEqual(result['eta']['seconds'], 344.5)

    def test_nonincreasing_bytes_do_not_create_a_rate(self):
        value = session.WarmProgress()
        self.observe(value, 100, 1_000_000)
        self.assertEqual(self.observe(value, 200, 1_000_000)['eta']['state'], 'unknown')

    def test_actual_first_two_retained_samples_replace_thirteen_hour_artifact(self):
        # Exact first two Workbench warm samples from the 2026-09-10 cold proof;
        # old first ETA was 48,236.3 seconds after initial mincore inspection.
        value = session.WarmProgress()
        total = 699_905_921_024
        first = self.observe(value, 1789012650.0202863, 369_098_752, total)
        later = self.observe(value, 1789012660.067981, 26_658_996_224, total)
        self.assertEqual(first['eta']['state'], 'unknown')
        self.assertEqual(later['eta']['state'], 'estimate')
        self.assertGreater(later['eta']['seconds'], 250)
        self.assertLess(later['eta']['seconds'], 270)

    def test_full_read_switches_immediately_to_unknown_verification(self):
        value = session.WarmProgress()
        self.observe(value, 0, 1, 100)
        reading = self.observe(value, 5, 99, 100)
        self.assertEqual(reading['eta']['state'], 'estimate')
        # The semantic transition bypasses normal one-second throttling.
        verification = self.observe(value, 5.01, 100, 100)
        self.assertEqual(verification['eta']['state'], 'unknown')
        self.assertNotIn('seconds', verification['eta'])
        self.assertIn('verifying full page residency', verification['message'])
        self.assertEqual(self.observe(value, 36, 100, 100)['eta']['state'], 'unknown')

    def test_small_read_has_no_estimate_but_emits_verification(self):
        value = session.WarmProgress()
        result = self.observe(value, 1000, 100, 100)
        self.assertEqual(result['eta']['state'], 'unknown')
        self.assertEqual(result['completed'], result['total'])
        self.assertIn('verifying', result['message'])

    def test_verification_event_is_fresh_running_and_completion_remains_explicit(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()), \
                patch.dict(os.environ, {'BIO_WORKER_PROGRESS_LOG': ''}):
            output = Path(directory)
            fields = self.observe(session.WarmProgress(), 10, 100, 100)
            with patch.object(session.time, 'time_ns', return_value=123456789):
                session.startup_progress(output, 'a'*32, 'index_warm', 'running', **fields)
            event = session.load(output/'startup-progress.json')
            self.assertEqual(event['timestamp_ns'], 123456789)
            self.assertEqual(event['state'], 'running')
            self.assertEqual(event['eta']['state'], 'unknown')
            self.assertNotIn('seconds', event['eta'])
            session.startup_progress(output, 'a'*32, 'index_warm', 'complete',
                                     'Full private MSA index residency verified',
                                     completed=100, total=100, unit='bytes')
            self.assertEqual(session.load(output/'startup-progress.json')['state'], 'complete')


if __name__ == '__main__':
    unittest.main()
