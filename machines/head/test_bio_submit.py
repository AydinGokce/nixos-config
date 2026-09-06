"""Exercise orchestration failures with a fake cloud, never real credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("bio-submit.sh")


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        commands = self.root / "bin"
        commands.mkdir()
        (commands / "python3").symlink_to(sys.executable)
        stubs = {
            "dc": '''#!/usr/bin/env bash
set -eu
if [ "$1" = launch ]; then
  if [ "${DENY_BUDGET:-0}" = 1 ]; then echo 'BUDGET HALT'; exit 4; fi
  echo 'READY id=12345678-1234-1234-1234-123456789012 ip=127.0.0.1'
else
  echo "$*" >> "$AUDIT/removals"
fi
''',
            "ssh": '''#!/usr/bin/env bash
set -eu
if [ "${!#}" = true ]; then exit 0; fi
cat > "$AUDIT/transmitted.sh"
bash -n "$AUDIT/transmitted.sh"
exit "${MODEL_EXIT:-0}"
''',
            "rsync": '''#!/usr/bin/env bash
set -eu
case "$*" in
  *root@*)
    [ "${FETCH_FAIL:-0}" != 1 ] || exit 23
    echo 'ATOM validated-result' > "${!#}/result.pdb" ;;
esac
''',
        }
        for name, body in stubs.items():
            path = commands / name
            path.write_text(body)
            path.chmod(0o700)
        self.input = self.root / "query with 'quote.fasta"
        self.input.write_text(">query\nNLYIQWLKDGGPSSGRPPPS\n")
        self.env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                        AUDIT=str(self.root), BIO_SHARED_MNT=str(self.root / "shared"),
                        BIO_TOOLS_SRC=str(self.root / "tools"),
                        BIO_STATE_DIR=str(self.root / "state"),
                        BIO_RESULTS_DIR=str(self.root / "results"))
        (self.root / "tools").mkdir()

    def run_job(self, **settings):
        env = dict(self.env, **settings)
        return subprocess.run(["bash", str(SCRIPT), "boltz2", "--fasta", str(self.input),
                               "--", "--example", "a quoted value"], env=env,
                              text=True, capture_output=True, timeout=20)

    def test_success_fetches_before_reporting_completion(self):
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DONE", result.stdout)
        self.assertEqual(len(list((self.root / "results").glob("*/result.pdb"))), 1)
        self.assertEqual((self.root / "removals").read_text().count("rm "), 1)
        self.assertIn("a\\ quoted\\ value", (self.root / "transmitted.sh").read_text())

    def test_model_failure_is_preserved_and_worker_deleted(self):
        result = self.run_job(MODEL_EXIT="17")
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertNotIn("DONE", result.stdout)
        self.assertTrue((self.root / "removals").exists())
        metadata = next((self.root / "results").glob("*/job.json"))
        self.assertEqual(json.loads(metadata.read_text())["exit_status"], 17)

    def test_fetch_failure_does_not_claim_success(self):
        result = self.run_job(FETCH_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("DONE", result.stdout)
        self.assertTrue((self.root / "removals").exists())

    def test_budget_denial_does_not_retry_or_launch(self):
        result = self.run_job(DENY_BUDGET="1")
        self.assertEqual(result.returncode, 4)
        self.assertEqual(result.stdout.count("BUDGET HALT"), 1)
        self.assertFalse((self.root / "removals").exists())
        self.assertFalse((self.root / "transmitted.sh").exists())


if __name__ == "__main__":
    unittest.main()
