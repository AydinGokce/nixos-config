"""Exercise the local orchestrator's completion and failure propagation offline.

Run: python3 -m unittest discover -s modules/bio/tests -p test_bio_fold.py -v
SSH, upload, download, and visualization are replaced with temporary commands.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bio-fold.sh"
STUB = r'''
import json, os
from pathlib import Path
import sys

command = Path(sys.argv[0]).name
with open(os.environ["BIO_TEST_CALLS"], "a") as stream:
    stream.write(json.dumps([command, sys.argv[1:]]) + "\n")
if command == "ssh":
    # Even a misleading success marker must not hide a nonzero SSH exit.
    print("bio-submit: DONE — results at /var/lib/bio-runs/mock-job (head-local)")
    sys.exit(int(os.environ.get("BIO_TEST_REMOTE_STATUS", "0")))
if command == "rsync":
    status = int(os.environ.get("BIO_TEST_FETCH_STATUS", "0"))
    if not status:
        out = Path(sys.argv[-1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "prediction.pdb").write_text("END\n")
    sys.exit(status)
if command == "bio-viz":
    status = int(os.environ.get("BIO_TEST_RENDER_STATUS", "0")) if "--render" in sys.argv else 0
    if not status and "-o" in sys.argv:
        Path(sys.argv[sys.argv.index("-o") + 1]).write_bytes(b"mock image")
    sys.exit(status)
'''


class BioFoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bio-fold-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("ssh", "scp", "rsync", "bio-viz"):
            path = self.bin / name
            path.write_text("#!" + sys.executable + "\n" + STUB)
            path.chmod(0o755)
        self.fasta = self.root / "input with spaces.fasta"
        self.fasta.write_text(">query\nMKTAYIAKQRQISFVKSHFSRQDILDLWIYHTQGYFP\n")
        self.config = self.root / "config.sh"
        self.config.write_text("")
        self.key = self.root / "test-key"
        self.key.write_text("mock key; no real connection is made\n")
        self.calls = self.root / "calls.jsonl"
        self.out = self.root / "results with spaces"
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        BIO_CONFIG_FILE=str(self.config), BIO_CLUSTER_HEAD="192.0.2.10",
                        BIO_CLUSTER_USER="root", BIO_CLUSTER_SSHKEY=str(self.key),
                        BIO_TEST_CALLS=str(self.calls), TMPDIR=str(self.root))

    def run_fold(self, *flags, **env):
        return subprocess.run(["bash", str(SCRIPT), "boltz2", "--fasta", str(self.fasta),
                               "--out", str(self.out), *flags], env=dict(self.env, **env),
                              text=True, capture_output=True, timeout=15)

    def commands(self, name):
        if not self.calls.exists():
            return []
        return [args for command, args in map(json.loads, self.calls.read_text().splitlines())
                if command == name]

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.out / "prediction.pdb").is_file())

    def test_success_without_view_or_render_returns_zero(self):
        result = self.run_fold()
        self.assert_success(result)
        self.assertEqual(self.commands("bio-viz"), [])
        self.assertEqual(len(self.commands("rsync")), 1)

    def test_successful_render_only_returns_zero(self):
        result = self.run_fold("--render")
        self.assert_success(result)
        self.assertTrue((self.out / "render.png").is_file())
        self.assertEqual(self.commands("bio-viz"), [
            ["--render", str(self.out / "prediction.pdb"), "-o", str(self.out / "render.png")]])

    def test_successful_view_only_returns_zero(self):
        result = self.run_fold("--view")
        self.assert_success(result)
        self.assertEqual(self.commands("bio-viz"), [[str(self.out / "prediction.pdb")]])

    def test_render_failure_cannot_be_hidden_by_later_view(self):
        result = self.run_fold("--render", "--view", BIO_TEST_RENDER_STATUS="17")
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertEqual(len(self.commands("bio-viz")), 1)
        self.assertIn("--render", self.commands("bio-viz")[0])

    def test_remote_failure_propagates_without_downloading_or_rendering(self):
        result = self.run_fold("--render", BIO_TEST_REMOTE_STATUS="37")
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.assertEqual(self.commands("rsync"), [])
        self.assertEqual(self.commands("bio-viz"), [])
        self.assertIn("remote job failed", result.stderr)

    def test_failed_download_reports_failure_without_rendering(self):
        result = self.run_fold("--render", BIO_TEST_FETCH_STATUS="38")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.commands("bio-viz"), [])
        self.assertIn("rsync fetch failed", result.stderr)


if __name__ == "__main__":
    unittest.main()
