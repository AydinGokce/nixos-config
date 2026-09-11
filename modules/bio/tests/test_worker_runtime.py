"""Selected runtime copies and CPU preparation permits, with no cloud calls."""
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / "py"
spec = importlib.util.spec_from_file_location("worker_runtime", SOURCE / "worker_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.shared = self.root / "shared"
        (self.shared / "envs/boltz/bin").mkdir(parents=True)
        (self.shared / "envs/boltz/bin/predict").write_text(f"#!{self.shared}/envs/boltz/bin/python\n")
        (self.shared / "envs/boltz/bin/python").symlink_to(sys.executable)
        (self.shared / "cache/boltz").mkdir(parents=True)
        (self.shared / "cache/boltz/weights").write_bytes(b"scientific-weights\0" * 100)
        (self.shared / "cache/hf/unrelated-large-model").mkdir(parents=True)

    def test_two_copies_preserve_paths_and_bytes_then_mutate_independently(self):
        value = runtime.plan(self.shared, "boltz2")
        self.assertEqual(value["paths"], ["cache/boltz", "envs/boltz"])
        for name in ["one", "two"]:
            runtime.stage(self.shared, self.root / name, value)
            self.assertEqual((self.root / name / "envs/boltz/bin/predict").read_bytes(),
                             (self.shared / "envs/boltz/bin/predict").read_bytes())
            self.assertEqual(os.readlink(self.root / name / "envs/boltz/bin/python"), sys.executable)
            self.assertFalse((self.root / name / "cache/hf/unrelated-large-model").exists())
        original = (self.shared / "cache/boltz/weights").read_bytes()
        (self.root / "one/cache/boltz/weights").write_bytes(b"private download or update")
        self.assertEqual((self.root / "two/cache/boltz/weights").read_bytes(), original)
        self.assertEqual((self.shared / "cache/boltz/weights").read_bytes(), original)

    def test_embedding_selects_only_requested_cache_and_preserves_large_model_room(self):
        value = runtime.plan(self.shared, "esm", "esm2_t48_15B_UR50D")
        self.assertIn("cache/hf/hub/models--facebook--esm2_t48_15B_UR50D", value["paths"])
        self.assertGreaterEqual(value["os_size_gb"], 96)
        self.assertNotIn("cache/hf", value["paths"])

    def test_oversized_plan_and_insufficient_worker_disk_fail_before_copy(self):
        with patch.object(runtime, "source_size", return_value=(200 * runtime.GIB, 1)):
            with self.assertRaisesRegex(ValueError, "200 GB cap"):
                runtime.plan(self.shared, "boltz2")
        value = runtime.plan(self.shared, "boltz2")
        with patch.object(runtime.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1)):
            with self.assertRaisesRegex(ValueError, "lacks space"):
                runtime.stage(self.shared, self.root / "copy", value)
        self.assertFalse((self.root / "copy").exists())

    def test_mutated_plan_and_symlinked_source_root_fail_closed(self):
        value = runtime.plan(self.shared, "boltz2")
        value["paths"] += ["../../outside"]
        with self.assertRaisesRegex(ValueError, "invalid worker"):
            runtime.stage(self.shared, self.root / "copy", value)
        (self.shared / "envs/esm2").symlink_to(self.shared / "envs/boltz", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            runtime.plan(self.shared, "esm")

    def test_internal_symlink_cannot_escape_to_unisolated_shared_data(self):
        (self.shared / "envs/boltz/unsafe").symlink_to(self.shared / "runs/other-job")
        with self.assertRaisesRegex(ValueError, "escapes selected assets"):
            runtime.plan(self.shared, "boltz2")


class PreparationGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.processes = []
        self.addCleanup(self.stop)

    def start(self, timeout=10, capacity_wait=0):
        process = subprocess.Popen([sys.executable, str(SOURCE / "head_preparation_gate.py"),
            "--state", self.temporary.name, "--timeout", str(timeout),
            '--capacity-wait-seconds', str(capacity_wait)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        return process

    def stop(self):
        for process in self.processes:
            if not process.stdin.closed:
                process.stdin.close()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def ready(self, process):
        self.assertTrue(select.select([process.stdout], [], [], 5)[0])
        self.assertEqual(process.stdout.readline(), "ready\n")

    def test_third_preparation_waits_then_uses_released_permit(self):
        one, two = self.start(), self.start()
        self.ready(one)
        self.ready(two)
        third = self.start()
        self.assertFalse(select.select([third.stdout], [], [], 0.2)[0])
        one.stdin.write("release\n")
        one.stdin.flush()
        one.wait(timeout=5)
        self.ready(third)
        self.assertIsNone(two.poll())

    def test_wait_timeout_and_caller_eof_release_permits(self):
        one, two = self.start(), self.start()
        self.ready(one)
        self.ready(two)
        third = self.start(timeout=0.1)
        self.assertEqual(third.wait(timeout=5), 2)
        self.assertIn("remained occupied", third.stderr.read())
        one.stdin.close()
        self.assertEqual(one.wait(timeout=5), 0)
        replacement = self.start()
        self.ready(replacement)

    def test_capacity_allowance_keeps_third_private_request_waiting_without_extra_permits(self):
        one, two = self.start(), self.start()
        self.ready(one); self.ready(two)
        third = self.start(timeout=0.1, capacity_wait=1)
        self.assertFalse(select.select([third.stdout], [], [], 0.25)[0])
        self.assertIsNone(third.poll())
        one.stdin.close(); one.wait(timeout=5)
        self.ready(third)
        self.assertIsNone(two.poll())

    def test_largest_native_timeout_can_add_bounded_capacity_allowance(self):
        self.ready(self.start(timeout=85500, capacity_wait=7200))
        for timeout, allowance in ((85501, 0), (85500, 7201), (1, -1)):
            with self.subTest(timeout=timeout, allowance=allowance):
                process = self.start(timeout=timeout, capacity_wait=allowance)
                self.assertEqual(process.wait(timeout=5), 2)

    def test_bash_coprocess_permit_does_not_leak_to_a_surviving_native_child(self):
        first = self.start()
        self.ready(first)
        command = '''set -euo pipefail
coproc GATE { "$1" "$2" --state "$3" --timeout 5; }
IFS= read -r ready <&"${GATE[0]}"
[ "$ready" = ready ]
sleep 20 >/dev/null 2>&1 &
printf '%s\\n' "$!"
exit 0
'''
        child = int(subprocess.check_output(["bash", "-c", command, "gate-test", sys.executable,
            str(SOURCE / "head_preparation_gate.py"), self.temporary.name], text=True,
            stderr=subprocess.DEVNULL, timeout=5).strip())
        try:
            # The submitting shell is gone; its child deliberately remains.
            os.kill(child, 0)
            self.ready(self.start())
        finally:
            os.kill(child, signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
