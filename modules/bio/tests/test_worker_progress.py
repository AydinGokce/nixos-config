"""Real progress transport, bounded estimates and subprocess outcomes."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / "py"
sys.path.insert(0, str(SOURCE))
import worker_progress as progress


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {
            "BIO_WORKER_PROGRESS_LOG": "", "BIO_WORKER_PROGRESS_JSON": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_private_log_and_atomic_snapshot_receive_identical_bounded_event(self):
        log, snapshot = self.root / "log", self.root / "snapshot"
        log.touch(mode=0o600)
        with patch.dict(os.environ, {"BIO_WORKER_PROGRESS_LOG": str(log),
                                    "BIO_WORKER_PROGRESS_JSON": str(snapshot)}), contextlib.redirect_stderr(io.StringIO()) as stderr:
            value = progress.emit("runtime_download", completed=20, total=100, unit="bytes")
        self.assertEqual(log.read_text(), stderr.getvalue())
        self.assertEqual(json.loads(snapshot.read_text()), value)
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        self.assertLessEqual(len(log.read_bytes()), progress.LIMIT)
        self.assertEqual(list(self.root.glob(".worker-progress-*")), [])

    def test_side_channel_refuses_symlinks_and_public_files(self):
        real, link = self.root / "real", self.root / "link"
        real.touch(mode=0o644)
        link.symlink_to(real)
        with contextlib.redirect_stderr(io.StringIO()):
            for path in (real, link):
                with patch.dict(os.environ, {"BIO_WORKER_PROGRESS_LOG": str(path)}):
                    with self.assertRaises((ValueError, OSError)):
                        progress.emit("ready")
        self.assertEqual(real.read_bytes(), b"")

    def test_idle_heartbeat_stays_current_and_stops_after_terminal_event(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with progress.Activity("base_setup", interval=0.02):
                time.sleep(0.08)
            terminal = stderr.getvalue()
            time.sleep(0.04)
            self.assertEqual(stderr.getvalue(), terminal)
        events = [json.loads(line.removeprefix(progress.PREFIX)) for line in terminal.splitlines()]
        self.assertGreaterEqual(len(events), 3)
        self.assertEqual(events[-1]["state"], "complete")
        self.assertEqual(len({v["stage_id"] for v in events}), 1)
        self.assertTrue(all(a["timestamp_ns"] < b["timestamp_ns"] for a, b in zip(events, events[1:])))

    def test_expired_estimate_becomes_unknown_instead_of_restarting_countdown(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with progress.Activity("allocating", interval=60,
                eta=dict(state="range", scope="stage", basis="Prior observations", lower_seconds=2, upper_seconds=5)) as activity:
                activity.eta_observed -= 8
                activity._emit()
        events = [json.loads(line.removeprefix(progress.PREFIX)) for line in stderr.getvalue().splitlines()]
        self.assertEqual(events[1]["eta"]["state"], "unknown")
        with self.assertRaises(ValueError):
            progress.emit("allocating", eta=dict(state="estimate", scope="stage", basis="bad", seconds=float("inf")))

    def test_capacity_wait_never_turns_retry_window_into_completion_estimate(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with progress.Activity("waiting_capacity", interval=60,
                                   message="Waiting for capacity; retry window remaining 30:00"):
                pass
            with progress.Activity("allocating", interval=60, eta=dict(state="range", scope="stage",
                                   basis="Prior startup observations", lower_seconds=60, upper_seconds=180)):
                pass
        events = [json.loads(line.removeprefix(progress.PREFIX)) for line in stderr.getvalue().splitlines()]
        self.assertEqual([(v['stage'], v['state']) for v in events], [
            ('waiting_capacity', 'running'), ('waiting_capacity', 'complete'),
            ('allocating', 'running'), ('allocating', 'complete')])
        self.assertNotEqual(events[0]['stage_id'], events[2]['stage_id'])
        self.assertTrue(all(v['eta']['state'] == 'unknown' for v in events[:2]))
        self.assertEqual(events[2]['eta']['lower_seconds'], 60)
        self.assertEqual(events[2]['eta']['upper_seconds'], 180)
        for fields in [dict(completed=0, total=1800, unit='steps'),
                       dict(eta=dict(state='estimate', scope='stage', basis='Retry deadline', seconds=1800))]:
            with self.assertRaises(ValueError):
                progress.emit('waiting_capacity', **fields)

    def test_child_output_and_failure_code_preserved_separately_from_progress(self):
        result = subprocess.run([sys.executable, str(SOURCE / "worker_progress.py"), "run",
            "--stage", "allocating", "--", sys.executable, "-c",
            "print('READY id=fixture'); raise SystemExit(4)"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 4)
        self.assertEqual(result.stdout, "READY id=fixture\n")
        events = [json.loads(line.removeprefix(progress.PREFIX)) for line in result.stderr.splitlines()]
        self.assertEqual([v["state"] for v in events], ["running", "failed"])

    def test_termination_stops_owned_child(self):
        pid_file = self.root / "child.pid"
        child_code = "import os,pathlib,time; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(60)" % str(pid_file)
        process = subprocess.Popen([sys.executable, str(SOURCE / "worker_progress.py"), "run",
            "--stage", "allocating", "--", sys.executable, "-c", child_code],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.exists())
            pid = int(pid_file.read_text())
            process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertIn('"state":"failed"', stderr)
        finally:
            if process.poll() is None:
                process.kill(); process.communicate()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
