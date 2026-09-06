"""Exercise orchestration failures with a fake cloud, never real credentials."""
import json
import hashlib
import os
from pathlib import Path
import shutil
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
            "bio-rfaa-storage": '''#!/usr/bin/env bash
set -eu
echo "$*" >> "$AUDIT/storage-checks"
case "$1" in
  check)
    count=$(grep -c '^check ' "$AUDIT/storage-checks")
    if [ "${EXPIRE_ON_CHECK:-0}" = "$count" ]; then
      echo 'RFAA storage expired or retiring' >&2; exit 2
    fi ;;
  track)
    if [[ "$*" = *--instance* ]] && [ "${EXPIRE_AFTER_LAUNCH:-0}" = 1 ]; then
      echo 'RFAA storage expired or retiring' >&2; exit 2
    fi ;;
esac
''',
            "dc": '''#!/usr/bin/env bash
set -eu
if [ "$1" = launch ]; then
  if [ "${DENY_BUDGET:-0}" = 1 ]; then echo 'BUDGET HALT'; exit 4; fi
  echo "$2" >> "$AUDIT/launches"
  if [ "${NO_GPU_CAPACITY:-0}" = 1 ]; then exit 1; fi
  if [ "${NO_LARGE_A100:-0}" = 1 ] && [ "$2" = 1A100.22V ]; then exit 1; fi
  echo 'READY id=12345678-1234-1234-1234-123456789012 ip=127.0.0.1'
else
  echo "$*" >> "$AUDIT/removals"
  if [ "${DELETE_FAIL:-0}" = 1 ]; then echo 'fake deletion failed' >&2; exit 1; fi
  echo "removed $2 (confirmed; managed OS permanently removed; shared volumes retained)"
fi
''',
            "ssh": '''#!/usr/bin/env bash
set -eu
if [ "${!#}" = true ]; then exit 0; fi
cat > "$AUDIT/transmitted.sh"
bash -n "$AUDIT/transmitted.sh"
if [ "${EXECUTE_BUNDLE:-0}" = 1 ]; then
  python3 - <<'PY'
import os
from pathlib import Path
root = Path(os.environ["AUDIT"])
prefix = (root / "transmitted.sh").read_text().split("# END VERIFIED TOOL BUNDLE")[0]
if os.environ.get("CORRUPT_BUNDLE") == "1":
    before, payload = prefix.split("<<'BIO_TOOLS_ARCHIVE'\\n", 1)
    payload = ("A" if payload[0] != "A" else "B") + payload[1:]
    prefix = before + "<<'BIO_TOOLS_ARCHIVE'\\n" + payload
(root / "bundle-check.sh").write_text(prefix + '\\ncat "$BIO_TOOLS_DIR/recipes/boltz2.sh" > "$AUDIT/actual-recipe"\\n')
PY
  bash "$AUDIT/bundle-check.sh"
fi
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
                        BIO_CLUSTER_CONFIG=str(self.root / "cluster.sh"),
                        BIO_STATE_DIR=str(self.root / "state"),
                        BIO_RESULTS_DIR=str(self.root / "results"))
        (self.root / "tools").mkdir()
        shutil.copytree(SCRIPT.parent / "rfaa", self.root / "tools" / "rfaa",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "tools" / "recipes").mkdir()
        (self.root / "tools" / "recipes" / "boltz2.sh").write_text("# authoritative deployed recipe\n")
        (self.root / "tools" / "py").mkdir()
        (self.root / "tools" / "requirements").mkdir()

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

    def run_cleanup_with_closed_log_pipe(self, exit_command, **settings):
        # Run the real cleanup body with the same broken pipe left by a dead
        # tee, without ever calling a real cloud command or signalling the test.
        source = SCRIPT.read_text()
        cleanup = source[source.index("cleanup() {"):source.index("\ntrap cleanup EXIT")]
        script = '''set -euo pipefail
id=12345678-1234-1234-1234-123456789012
LOCALOUT="$BIO_RESULTS_DIR/cleanup"
mkdir -p "$LOCALOUT"
''' + cleanup + "\ntrap cleanup EXIT\ntrap 'exit 143' TERM\n" + exit_command
        reader, writer = os.pipe()
        os.close(reader)
        try:
            result = subprocess.run(["bash", "-c", script], env=dict(self.env, **settings),
                                    stdout=writer, stderr=subprocess.STDOUT, timeout=5)
        finally:
            os.close(writer)
        return result, self.root / "results" / "cleanup" / "run.log"

    def test_cleanup_survives_dead_logger_and_retains_model_exit_status(self):
        result, log = self.run_cleanup_with_closed_log_pipe("exit 17\n")
        self.assertEqual(result.returncode, 17)
        self.assertEqual((self.root / "removals").read_text().splitlines(),
                         ["rm 12345678-1234-1234-1234-123456789012"])
        self.assertIn("managed OS permanently removed; shared volumes retained", log.read_text())

    def test_term_cleanup_survives_dead_logger_and_retains_signal_status(self):
        result, log = self.run_cleanup_with_closed_log_pipe('kill -TERM "$BASHPID"\n')
        self.assertEqual(result.returncode, 143)
        self.assertEqual((self.root / "removals").read_text().splitlines(),
                         ["rm 12345678-1234-1234-1234-123456789012"])
        self.assertIn("managed OS permanently removed; shared volumes retained", log.read_text())

    def test_cleanup_deletion_failure_is_logged_without_hiding_original_failure(self):
        result, log = self.run_cleanup_with_closed_log_pipe("exit 17\n", DELETE_FAIL="1")
        self.assertEqual(result.returncode, 17)
        self.assertIn("ERROR deleting 12345678-1234-1234-1234-123456789012", log.read_text())
        self.assertNotIn("permanently removed", log.read_text())

    def test_cleanup_deletion_failure_prevents_success(self):
        result, log = self.run_cleanup_with_closed_log_pipe("exit 0\n", DELETE_FAIL="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR deleting 12345678-1234-1234-1234-123456789012", log.read_text())
        self.assertNotIn("permanently removed", log.read_text())

    def test_worker_receives_authoritative_tools_despite_stale_shared_copy(self):
        stale = self.root / "shared" / "tools" / "recipes"
        stale.mkdir(parents=True)
        (stale / "boltz2.sh").write_text("# stale shared recipe\n")
        result = self.run_job(EXECUTE_BUNDLE="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "actual-recipe").read_text(), "# authoritative deployed recipe\n")
        metadata = next((self.root / "results").glob("*/job.json"))
        bundle = metadata.with_name("tools.tar.gz")
        self.assertEqual(json.loads(metadata.read_text())["tools_sha256"],
                         hashlib.sha256(bundle.read_bytes()).hexdigest())

    def test_corrupted_code_bundle_fails_before_model_and_cleans_worker(self):
        result = self.run_job(EXECUTE_BUNDLE="1", CORRUPT_BUNDLE="1")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / "actual-recipe").exists())
        self.assertNotIn("DONE", result.stdout)
        self.assertEqual((self.root / "removals").read_text().count("rm "), 1)

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

    def test_protenix_fallback_keeps_cuda128_and_supported_gpu_architectures(self):
        result = subprocess.run(["bash", str(SCRIPT), "protenix", "--fasta", str(self.input)],
                                env=dict(self.env, NO_GPU_CAPACITY="1"),
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertEqual((self.root / "launches").read_text().splitlines(),
                         ["1A100.22V", "1L40S.20V", "1H100.80S.32V"])

    def test_protenix_explicit_unsupported_gpu_is_rejected_before_launch(self):
        for gpu in ("1RTXPRO6000.30V", "1A6000.10V"):
            with self.subTest(gpu=gpu):
                result = subprocess.run(["bash", str(SCRIPT), "protenix", "--fasta", str(self.input), "--gpu", gpu],
                                        env=self.env, text=True, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("Protenix requires", result.stderr)
                self.assertFalse((self.root / "launches").exists())

    def test_protenix_msa_endpoint_is_explicit_and_forwarded_to_worker(self):
        for endpoint in ("", "https://msa.private.example/api"):
            with self.subTest(endpoint=endpoint):
                result = subprocess.run(["bash", str(SCRIPT), "protenix", "--fasta", str(self.input)],
                                        env=dict(self.env, MMSEQS_SERVICE_HOST_URL=endpoint),
                                        text=True, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                expected = endpoint or "https://api.colabfold.com"
                self.assertIn("export MMSEQS_SERVICE_HOST_URL=" + expected + "\n",
                              (self.root / "transmitted.sh").read_text())

    def test_full_rfaa_without_database_configuration_never_rents_a_gpu(self):
        result = subprocess.run(["bash", str(SCRIPT), "rfaa", "--fasta", str(self.input)],
                                env=dict(self.env, RFAA_DB_VOLUME="", RFAA_DB_NFS=""),
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("database volume configured", result.stderr)
        self.assertFalse((self.root / "launches").exists())

    def test_full_rfaa_fallback_keeps_sufficient_ram_and_mounts_database_read_only(self):
        result = subprocess.run(["bash", str(SCRIPT), "rfaa", "--fasta", str(self.input)],
                                env=dict(self.env, RFAA_DB_VOLUME="database-volume",
                                         RFAA_DB_NFS="database-server:/rfaa", NO_LARGE_A100="1"),
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "launches").read_text().splitlines(),
                         ["1A100.22V", "1A100.40S.22V"])
        transmitted = (self.root / "transmitted.sh").read_text()
        self.assertIn("RFAA_DB_NFS=database-server:/rfaa", transmitted)
        self.assertIn("nconnect=16,nolock,ro", transmitted)
        metadata = next((self.root / "results").glob("*/job.json"))
        self.assertEqual(json.loads(metadata.read_text())["database_volume"], "database-volume")
        self.assertEqual((self.root / "storage-checks").read_text().count("check --volume"), 2)

    def test_rfaa_storage_expired_before_or_while_queued_never_rents_a_gpu(self):
        for check in ("1", "2"):
            with self.subTest(check=check):
                (self.root / "storage-checks").unlink(missing_ok=True)
                result = subprocess.run(["bash", str(SCRIPT), "rfaa", "--fasta", str(self.input)],
                                        env=dict(self.env, RFAA_DB_VOLUME="database-volume",
                                                 RFAA_DB_NFS="database-server:/rfaa", EXPIRE_ON_CHECK=check),
                                        text=True, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse((self.root / "launches").exists())
                self.assertFalse((self.root / "transmitted.sh").exists())

    def test_rfaa_storage_expiring_during_provisioning_cleans_worker_without_starting_model(self):
        result = subprocess.run(["bash", str(SCRIPT), "rfaa", "--fasta", str(self.input)],
                                env=dict(self.env, RFAA_DB_VOLUME="database-volume",
                                         RFAA_DB_NFS="database-server:/rfaa", EXPIRE_AFTER_LAUNCH="1"),
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual((self.root / "removals").read_text().count("rm "), 1)
        self.assertFalse((self.root / "transmitted.sh").exists())


if __name__ == "__main__":
    unittest.main()
