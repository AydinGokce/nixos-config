"""Exercise orchestration failures with a fake cloud, never real credentials."""
import json
import hashlib
import fcntl
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("bio-submit.sh")
RFAA_DATABASES = runpy.run_path(str(SCRIPT.parent / "rfaa" / "databases.py"))


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
echo "storage:${0##*/}:$*" >> "$AUDIT/events"
case "$1" in
  check)
    count=$(grep -c '^check ' "$AUDIT/storage-checks")
    if [ "${EXPIRE_ON_CHECK:-0}" = "$count" ]; then
      echo 'RFAA storage expired or retiring' >&2; exit 2
    fi
    if [ "${REMOVE_DATABASE_RECEIPT_ON_CHECK:-0}" = "$count" ]; then
      rm -f -- "$DATABASE_RECEIPT_TO_REMOVE"
    fi
    if [ "$count" = 2 ] && [ -n "${MUTATE_PANEL_ON_CHECK:-}" ]; then
      python3 - "$MUTATE_PANEL_ON_CHECK" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text())
data['targets'][-1]['name'] = 'changed-after-queue'
path.write_text(json.dumps(data))
PY
    fi ;;
  track)
    if [[ "$*" != *--instance* ]] && [ "${EXPIRE_BEFORE_LAUNCH:-0}" = 1 ]; then
      echo 'RFAA storage expired or retiring' >&2; exit 2
    fi
    if [[ "$*" = *--instance* ]] && [ "${EXPIRE_AFTER_LAUNCH:-0}" = 1 ]; then
      echo 'RFAA storage expired or retiring' >&2; exit 2
    fi ;;
esac
''',
            "dc": '''#!/usr/bin/env bash
set -eu
if [ "$1" = launch ]; then
  echo "launch:$2" >> "$AUDIT/events"
  echo "$*" >> "$AUDIT/launch-args"
  if [ "${DENY_BUDGET:-0}" = 1 ]; then echo 'BUDGET HALT'; exit 4; fi
  echo "$2" >> "$AUDIT/launches"
  if [ "${NO_GPU_CAPACITY:-0}" = 1 ]; then exit 1; fi
  if [ "${NO_LARGE_A100:-0}" = 1 ] && [ "$2" = 1A100.22V ]; then exit 1; fi
  echo 'READY id=12345678-1234-1234-1234-123456789012 ip=127.0.0.1'
else
  echo "cleanup:$*" >> "$AUDIT/events"
  echo "$*" >> "$AUDIT/removals"
  if [ "${DELETE_FAIL:-0}" = 1 ]; then echo 'fake deletion failed' >&2; exit 1; fi
  echo "removed $2 (confirmed; managed OS permanently removed; shared volumes retained)"
fi
''',
            "ssh": '''#!/usr/bin/env bash
set -eu
if [ "${!#}" = true ]; then exit 0; fi
echo 'worker' >> "$AUDIT/events"
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
    echo 'fetch' >> "$AUDIT/events"
    [ "${FETCH_FAIL:-0}" != 1 ] || exit 23
    echo 'ATOM validated-result' > "${!#}/result.pdb"
    if [ "${FETCH_PREPARED:-0}" = 1 ]; then
      python3 - "${!#}" <<'PY'
import json, os, sys
from pathlib import Path
target = Path(sys.argv[1]) / 'prepared'
target.mkdir(exist_ok=True)
(target / 'complete.json').write_text(json.dumps({'model': os.environ.get('PREPARED_MODEL', 'boltz2'),
    'ready': os.environ.get('FETCH_BAD_PREPARED') != '1'}))
(target / 'native-input.bin').write_bytes(b'opaque native model input')
PY
    fi
    if [ -n "${FETCH_PANEL_DIR:-}" ]; then
      cp -R "$FETCH_PANEL_DIR" "${!#}/panel"
    fi ;;
esac
''',
            "bio-msa": '''#!/usr/bin/env bash
set -eu
echo "prepare:$*" >> "$AUDIT/events"
echo "$*" >> "$AUDIT/preparation-calls"
if [ "${NESTED_PREPARATION:-0}" = 1 ]; then
  shift
  exec bash "$BIO_SUBMIT_SCRIPT" msa --sub prepare "$@"
fi
[ "${PREPARATION_EXIT:-0}" = 0 ] || exit "$PREPARATION_EXIT"
python3 - "$@" <<'PY'
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
model = args[args.index('--model')+1]
receipt = Path(args[args.index('--bundle-result')+1])
bundle = Path(os.environ['AUDIT']) / "prepared bundle with 'quote"
if os.environ.get('PREPARATION_MISSING') != '1':
    bundle.mkdir(exist_ok=True)
    (bundle / 'complete.json').write_text(json.dumps({'model': model,
        'ready': os.environ.get('PREPARATION_BAD') != '1'}))
    (bundle / 'native-input.bin').write_bytes(b'opaque native model input')
receipt.write_text('invalid json' if os.environ.get('PREPARATION_BAD_RECEIPT') == '1'
                   else json.dumps({'bundle': str(bundle)}))
PY
''',
        }
        stubs["bio-msa-storage"] = stubs["bio-rfaa-storage"].replace("storage-checks", "msa-storage-checks").replace("RFAA", "MSA")
        for name, body in stubs.items():
            path = commands / name
            path.write_text(body)
            path.chmod(0o700)
        self.input = self.root / "query with 'quote.fasta"
        self.input.write_text(">query\nNLYIQWLKDGGPSSGRPPPS\n")
        self.rfaa_root = self.root / "rfaa databases with 'quote"
        self.rfaa_root.mkdir()
        # Exercise the real fast validator with complete miniature FFindex/data
        # pairs and matching receipts. Actual HHsuite search fixtures live in rfaa/.
        for dataset in RFAA_DATABASES["DATASETS"].values():
            directory = self.rfaa_root / dataset["directory"]
            directory.mkdir()
            for component in dataset["components"]:
                stem = directory / (dataset["prefix"] + "_" + component)
                Path(str(stem) + ".ffdata").write_bytes(b"X" * 2048)
                Path(str(stem) + ".ffindex").write_text("first\t0\t1024\nlast\t1024\t1024\n")
            files = RFAA_DATABASES["validate_directory"](directory, dataset)
            RFAA_DATABASES["receipt"](directory, dataset, files)
        self.msa_root = self.root / "msa databases with 'quote"
        self.msa_root.mkdir()
        (self.msa_root / ".msa-databases.json").write_text('{"fixture": "completed installation"}\n')
        self.env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                        AUDIT=str(self.root), BIO_SHARED_MNT=str(self.root / "shared"),
                        BIO_TOOLS_SRC=str(self.root / "tools"),
                        BIO_CLUSTER_CONFIG=str(self.root / "cluster.sh"),
                        BIO_STATE_DIR=str(self.root / "state"),
                        BIO_RESULTS_DIR=str(self.root / "results"),
                        RFAA_DB_DIR=str(self.rfaa_root), MSA_DB_ROOT=str(self.msa_root),
                        BIO_SUBMIT_SCRIPT=str(SCRIPT))
        (self.root / "tools").mkdir()
        shutil.copytree(SCRIPT.parent / "rfaa", self.root / "tools" / "rfaa",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(SCRIPT.parent / "msa", self.root / "tools" / "msa",
                        ignore=shutil.ignore_patterns("__pycache__"))
        # Native formats are covered by msa/test_prepared.py. This boundary
        # validator ensures orchestration cannot ignore a rejected bundle.
        (self.root / "tools" / "msa" / "prepared.py").write_text('''import argparse, json, os
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('command', choices=['validate'])
p.add_argument('--bundle', required=True)
p.add_argument('--model', required=True)
p.add_argument('--fasta', required=True)
a=p.parse_args()
with (Path(os.environ['AUDIT'])/'events').open('a') as f: f.write('validate:'+a.model+'\\n')
with (Path(os.environ['AUDIT'])/'prepared-checks').open('a') as f: f.write(json.dumps(vars(a))+'\\n')
try:
    data=json.loads((Path(a.bundle)/'complete.json').read_text())
    assert data['ready'] and data['model']==a.model
    assert (Path(a.bundle)/'native-input.bin').is_file()
    assert Path(a.fasta).is_file()
except (OSError, ValueError, KeyError, AssertionError):
    raise SystemExit(64)
''')
        (self.root / "tools" / "recipes").mkdir()
        (self.root / "tools" / "recipes" / "boltz2.sh").write_text("# authoritative deployed recipe\n")
        (self.root / "tools" / "py").mkdir()
        (self.root / "tools" / "requirements").mkdir()

    def run_job(self, **settings):
        env = dict(self.env, **settings)
        return subprocess.run(["bash", str(SCRIPT), "boltz2", "--fasta", str(self.input),
                               "--", "--example", "a quoted value"], env=env,
                              text=True, capture_output=True, timeout=20)

    def submit(self, model, *arguments, **settings):
        return subprocess.run(["bash", str(SCRIPT), model, *map(str, arguments)],
                              env=dict(self.env, **settings), text=True, capture_output=True, timeout=20)

    def msa_settings(self, **settings):
        return dict(MSA_DB_VOLUME="msa-database-volume", MSA_DB_NFS="msa-server:/colabfold", **settings)

    def valid_bundle(self, model="boltz2"):
        path = self.root / "existing prepared bundle"
        path.mkdir(exist_ok=True)
        (path / "complete.json").write_text(json.dumps(dict(model=model, ready=True)))
        (path / "native-input.bin").write_bytes(b"opaque native model input")
        return path

    def panel_fixture(self):
        # Use the actual portable bundle validator for panel fetches; the
        # simpler single-preparation boundary stub remains unchanged elsewhere.
        msa = SCRIPT.parent / "msa"
        sys.path.insert(0, str(msa))
        self.addCleanup(sys.path.remove, str(msa))
        import panel
        import databases
        from test_panel import fixture_bundle, fixture_audit, target
        manifest = self.root / "frozen panel.json"
        databases.write_json(manifest, dict(version=1, targets=[target("one"), target("two")]))
        config = self.root / "panel-server.json"
        provenance = self.root / "panel-provenance.json"
        databases.write_json(config, dict(server=dict(address="127.0.0.1:8080")))
        databases.write_json(provenance, dict(namespace="fixture", database=dict(mode="full")))
        output = self.root / "panel-fixture"
        def prepare(item, directory, _tools, _config, provenance, _deadline):
            digest = fixture_bundle(item, directory, provenance)
            fixture_audit(directory)
            return digest
        with patch.object(panel, "prepare_target", side_effect=prepare):
            self.assertEqual(panel.run(manifest, output, SCRIPT.parent, config, provenance, 10**12), 0)
        shutil.copyfile(msa / "prepared.py", self.root / "tools/msa/prepared.py")
        return manifest, output

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

    def test_full_rfaa_missing_installation_or_receipt_never_rents_a_gpu(self):
        settings = dict(RFAA_DB_VOLUME="database-volume", RFAA_DB_NFS="database-server:/rfaa")
        receipt = self.rfaa_root / "bfd" / ".rfaa-database.json"
        result = self.submit("rfaa", "--fasta", self.input, RFAA_DB_DIR=str(self.root / "not-installed"), **settings)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("full RFAA databases are not ready", result.stderr)
        receipt.unlink()
        result = self.submit("rfaa", "--fasta", self.input, **settings)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Missing installation receipt", result.stderr)
        self.assertFalse((self.root / "launches").exists())
        self.assertFalse((self.root / "launch-args").exists())
        self.assertFalse((self.root / "transmitted.sh").exists())

    def test_full_rfaa_zero_filled_or_truncated_data_fails_before_paid_launch(self):
        dataset = RFAA_DATABASES["DATASETS"]["bfd"]
        path = self.rfaa_root / dataset["directory"] / (dataset["prefix"] + "_a3m.ffdata")
        for data, diagnostic in ((bytes(2048), "Zero-filled database"), (b"X"*1024, "Truncated data file")):
            with self.subTest(diagnostic=diagnostic):
                path.write_bytes(data)
                result = self.submit("rfaa", "--fasta", self.input,
                                     RFAA_DB_VOLUME="database-volume", RFAA_DB_NFS="database-server:/rfaa")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(diagnostic, result.stderr)
                self.assertFalse((self.root / "launch-args").exists())
                self.assertFalse((self.root / "transmitted.sh").exists())

    def test_rfaa_single_sequence_does_not_require_installed_databases(self):
        shutil.rmtree(self.rfaa_root)
        result = self.submit("rfaa", "--fasta", self.input, "--sub", "single-seq",
                             RFAA_DB_VOLUME="database-volume", RFAA_DB_NFS="database-server:/rfaa")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / "storage-checks").exists())
        self.assertNotIn("--volume database-volume", (self.root / "launch-args").read_text())

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
        self.assertIn("vers=4.1,nconnect=16,nolock,ro", transmitted)
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

    def execute_msa_mount_block(self, sub):
        """Run the generated mount branch against shell functions, not sudo/NFS."""
        source = (self.root / "transmitted.sh").read_text()
        block = source[source.index('if [ -n "$MSA_DB_NFS" ]; then'):source.index('command -v uv')]
        (self.root / "mounted").unlink(missing_ok=True)
        script = '''set -eu
sudo() {
  if [ "$1" = mount ]; then
    echo "$*" >> "$AUDIT/mounts"
    touch "$AUDIT/mounted"
  fi
}
mountpoint() { test -f "$AUDIT/mounted"; }
sleep() { :; }
''' + block
        result = subprocess.run(["bash", "-c", script], env=dict(self.env, SUB=sub, MSA_DB_NFS="msa-server:/colabfold"),
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        return (self.root / "mounts").read_text().splitlines()[-1]

    def test_msa_database_modes_require_only_prepare_input_and_mount_only_writers_writable(self):
        for sub in ("install", "convert", "serve", "prepare"):
            with self.subTest(sub=sub):
                arguments = [] if sub != "prepare" else ["--model", "boltz2", "--fasta", self.input]
                result = self.submit("msa", "--sub", sub, *arguments,
                                     **self.msa_settings(FETCH_PREPARED="1"))
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                mount = self.execute_msa_mount_block(sub)
                self.assertEqual(mount, "mount -t nfs -o vers=4.1,nconnect=16,nolock" +
                                 ("" if sub in ("install", "convert") else ",ro") +
                                 " msa-server:/colabfold /mnt/bio-msa-databases")
        self.assertEqual((self.root / "launches").read_text().splitlines(),
                         ["CPU.360V.1440G", "CPU.16V.64G", "CPU.360V.1440G", "CPU.360V.1440G"])
        self.assertTrue(all("--volume msa-database-volume" in line
                            for line in (self.root / "launch-args").read_text().splitlines()))
        self.assertTrue(all("--image ubuntu-24.04 --max-hours" in line
                            for line in (self.root / "launch-args").read_text().splitlines()))

    def test_msa_prepare_requires_model_fasta_and_configured_storage_before_rental(self):
        cases = [([], self.msa_settings()), (["--model", "boltz2"], self.msa_settings()),
                 (["--fasta", self.input], self.msa_settings()),
                 (["--model", "rfaa", "--fasta", self.input], self.msa_settings()),
                 (["--model", "boltz2", "--fasta", self.input], dict(MSA_DB_VOLUME="", MSA_DB_NFS=""))]
        for arguments, settings in cases:
            with self.subTest(arguments=arguments, settings=settings):
                result = self.submit("msa", "--sub", "prepare", *arguments, **settings)
                self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
                self.assertFalse((self.root / "launches").exists())
                self.assertFalse((self.root / "transmitted.sh").exists())

    def test_msa_prepare_and_serve_require_a_nonempty_regular_final_receipt_before_rental(self):
        receipt = self.msa_root / ".msa-databases.json"
        for condition in ("missing", "empty", "directory"):
            receipt.unlink(missing_ok=True)
            if condition == "empty":
                receipt.touch()
            elif condition == "directory":
                receipt.mkdir()
            for sub in ("prepare", "serve"):
                with self.subTest(condition=condition, sub=sub):
                    arguments = ["--model", "boltz2", "--fasta", self.input] if sub == "prepare" else []
                    result = self.submit("msa", "--sub", sub, *arguments, **self.msa_settings())
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("nonempty final .msa-databases.json", result.stderr)
                    self.assertFalse((self.root / "launch-args").exists())
                    self.assertFalse((self.root / "transmitted.sh").exists())
            if condition == "directory":
                receipt.rmdir()

    def test_msa_install_and_convert_can_run_without_a_final_database_receipt(self):
        (self.msa_root / ".msa-databases.json").unlink()
        for sub in ("install", "convert"):
            with self.subTest(sub=sub):
                result = self.submit("msa", "--sub", sub, **self.msa_settings())
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "launches").read_text().splitlines(),
                         ["CPU.360V.1440G", "CPU.16V.64G"])

    def test_msa_convert_worker_failure_preserves_status_and_cleans_exact_worker(self):
        (self.msa_root / ".msa-databases.json").unlink()
        result = self.submit("msa", "--sub", "convert", **self.msa_settings(MODEL_EXIT="17"))
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertNotIn("DONE", result.stdout)
        self.assertEqual((self.root / "removals").read_text().splitlines(),
                         ["rm 12345678-1234-1234-1234-123456789012"])
        metadata = json.loads(next((self.root / "results").glob("*/job.json")).read_text())
        self.assertEqual(metadata["exit_status"], 17)
        self.assertEqual(metadata["database_volume"], "msa-database-volume")
        self.assertEqual((self.root / "msa-storage-checks").read_text().count("check --volume"), 2)
        self.assertEqual((self.root / "msa-storage-checks").read_text().count("track --volume"), 2)
        self.assertFalse((self.root / "prepared-checks").exists())

    def test_msa_convert_cli_forwards_options_and_documents_incomplete_indexing(self):
        wrapper = SCRIPT.with_name("bio-msa.sh")
        forwarded = self.root / "bin" / "bio-submit"
        forwarded.write_text('#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n')
        forwarded.chmod(0o700)
        result = subprocess.run(["bash", str(wrapper), "convert", "--timeout", "3600",
                                 "--name", "conversion with spaces"], env=self.env,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         ["msa", "--sub", "convert", "--timeout", "3600", "--name", "conversion with spaces"])
        help_result = subprocess.run(["bash", str(wrapper), "--help"], env=self.env,
                                     text=True, capture_output=True, timeout=5)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("bio-msa convert", help_result.stdout)
        self.assertIn("CPU.16V.64G", help_result.stdout)
        self.assertIn("without full search indexes", help_result.stdout)
        submit_help = self.submit("msa", "--help")
        self.assertEqual(submit_help.returncode, 0, submit_help.stderr)
        self.assertIn("install|convert|panel|prepare|serve", submit_help.stdout)

    def test_panel_validates_all_rows_before_any_worker_rental(self):
        manifest = self.root / "bad-panel.json"
        manifest.write_text(json.dumps(dict(version=1, targets=[dict(name="valid", model="protenix", sequence="ACDE"),
                                                               dict(name="bad", model="boltz2", sequence="ACDX")])) )
        for sub in ("panel", "install"):
            with self.subTest(sub=sub):
                result = self.submit("msa", "--sub", sub, "--json", manifest, **self.msa_settings())
                self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertIn("standard amino acids", result.stderr)
                self.assertFalse((self.root / "launches").exists())
                self.assertFalse((self.root / "transmitted.sh").exists())

    def test_panel_uses_one_readonly_worker_binds_input_and_verifies_retrieved_bundles(self):
        manifest, fixture = self.panel_fixture()
        result = self.submit("msa", "--sub", "panel", "--json", manifest, "--timeout", "14400",
                             **self.msa_settings(FETCH_PANEL_DIR=str(fixture)))
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn("DONE", result.stdout)
        self.assertEqual((self.root / "launches").read_text().splitlines(), ["CPU.360V.1440G"])
        self.assertIn(",ro", self.execute_msa_mount_block("panel"))
        remote = (self.root / "transmitted.sh").read_text()
        digest = json.loads((fixture / "panel.json").read_text())["manifest_sha256"]
        self.assertIn("export BIO_MSA_PANEL_SHA256="+digest, remote)
        self.assertIn("BIO_JOB_DEADLINE_EPOCH=$(( $(date +%s) + 10#14400 ))", remote)
        self.assertLess(remote.index("BIO_JOB_DEADLINE_EPOCH"), remote.index("# END VERIFIED TOOL BUNDLE"))
        self.assertIn('export PYTHONPYCACHEPREFIX="$BIO_TOOLS_DIR/cache/python"', remote)
        self.assertEqual((self.root / "removals").read_text().count("rm "), 1)
        copied = next((self.root / "results").glob("*/panel/panel.json"))
        self.assertTrue(json.loads(copied.read_text())["complete"])

    def test_panel_manifest_changed_while_queued_is_rejected_before_rental(self):
        manifest, _ = self.panel_fixture()
        result = self.submit("msa", "--sub", "panel", "--json", manifest,
                             **self.msa_settings(MUTATE_PANEL_ON_CHECK=str(manifest)))
        self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn("changed after validation", result.stdout+result.stderr)
        self.assertFalse((self.root / "launches").exists())
        self.assertFalse((self.root / "transmitted.sh").exists())

    def test_combined_install_panel_allows_uninstalled_data_and_verifies_after_one_writable_worker(self):
        manifest, fixture = self.panel_fixture()
        (self.msa_root / ".msa-databases.json").unlink()
        result = self.submit("msa", "--sub", "install", "--json", manifest, "--timeout", "21600",
                             **self.msa_settings(FETCH_PANEL_DIR=str(fixture)))
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn("DONE", result.stdout)
        self.assertEqual((self.root / "launches").read_text().splitlines(), ["CPU.360V.1440G"])
        self.assertNotIn(",ro", self.execute_msa_mount_block("install"))
        remote = (self.root / "transmitted.sh").read_text()
        self.assertIn("SUB=install", remote)
        self.assertIn("BIO_JOB_DEADLINE_EPOCH=$(( $(date +%s) + 10#21600 ))", remote)
        self.assertEqual((self.root / "removals").read_text().count("rm "), 1)

    def test_panel_missing_database_receipt_never_rents(self):
        manifest, _ = self.panel_fixture()
        (self.msa_root / ".msa-databases.json").unlink()
        result = self.submit("msa", "--sub", "panel", "--json", manifest, **self.msa_settings())
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertFalse((self.root / "launches").exists())

    def test_panel_worker_failure_retains_partial_receipt_before_exact_cleanup(self):
        manifest, fixture = self.panel_fixture()
        receipt = json.loads((fixture / "panel.json").read_text())
        receipt["complete"] = False
        receipt["targets"][1]["status"] = "failed"
        (fixture / "panel.json").write_text(json.dumps(receipt))
        result = self.submit("msa", "--sub", "panel", "--json", manifest,
                             **self.msa_settings(FETCH_PANEL_DIR=str(fixture), MODEL_EXIT="17"))
        self.assertEqual(result.returncode, 17, result.stdout+result.stderr)
        self.assertNotIn("DONE", result.stdout)
        copied = next((self.root / "results").glob("*/panel/panel.json"))
        self.assertEqual(json.loads(copied.read_text()), receipt)
        events = (self.root / "events").read_text().splitlines()
        self.assertLess(events.index("fetch"), next(i for i, e in enumerate(events) if e.startswith("cleanup:rm ")))
        self.assertEqual((self.root / "removals").read_text().count("rm "), 1)

    def test_panel_fetch_corruption_prevents_completion_and_still_cleans_worker(self):
        manifest, fixture = self.panel_fixture()
        (fixture / "targets/protenix/two/api-audit/1/response.body").write_bytes(b"corruption")
        for sub in ("panel", "install"):
            with self.subTest(sub=sub):
                result = self.submit("msa", "--sub", sub, "--json", manifest,
                                     **self.msa_settings(FETCH_PANEL_DIR=str(fixture)))
                self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertIn("API body", result.stdout+result.stderr)
                self.assertNotIn("DONE", result.stdout)
        self.assertEqual((self.root / "removals").read_text().count("rm "), 2)

    def test_panel_cli_forwards_manifest_and_rejects_global_model_overrides(self):
        manifest, _ = self.panel_fixture()
        result = self.submit("msa", "--sub", "panel", "--json", manifest, "--model", "boltz2", **self.msa_settings())
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertFalse((self.root / "launches").exists())
        forwarded = self.root / "bin/bio-submit"
        forwarded.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
        forwarded.chmod(0o700)
        result = subprocess.run(["bash", str(SCRIPT.with_name("bio-msa.sh")), "panel", "--json", str(manifest),
                                 "--spot"], env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout), ["msa", "--sub", "panel", "--json", str(manifest), "--spot"])

    def test_nested_private_preparation_with_uninstalled_databases_rents_no_worker(self):
        (self.msa_root / ".msa-databases.json").unlink()
        result = self.submit("boltz2", "--fasta", self.input, "--msa-backend", "private",
                             **self.msa_settings(NESTED_PREPARATION="1"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("private MSA databases are not ready", result.stderr)
        self.assertFalse((self.root / "launch-args").exists())
        self.assertFalse((self.root / "transmitted.sh").exists())

    def test_database_readiness_is_checked_after_the_queued_storage_recheck(self):
        for model, receipt, settings, arguments in (
            ("rfaa", self.rfaa_root / "bfd" / ".rfaa-database.json",
             dict(RFAA_DB_VOLUME="database-volume", RFAA_DB_NFS="database-server:/rfaa"),
             ["--fasta", self.input]),
            ("msa", self.msa_root / ".msa-databases.json", self.msa_settings(),
             ["--sub", "prepare", "--model", "boltz2", "--fasta", self.input]),
        ):
            with self.subTest(model=model):
                result = self.submit(model, *arguments, REMOVE_DATABASE_RECEIPT_ON_CHECK="2",
                                     DATABASE_RECEIPT_TO_REMOVE=str(receipt), **settings)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse(receipt.exists())
                self.assertFalse((self.root / "launch-args").exists())

    def test_msa_storage_checks_and_tracking_surround_the_exact_worker_launch(self):
        result = self.submit("msa", "--sub", "serve", **self.msa_settings())
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        events = (self.root / "events").read_text().splitlines()
        checks = [i for i, event in enumerate(events) if event.startswith("storage:bio-msa-storage:check ")]
        tracks = [i for i, event in enumerate(events) if event.startswith("storage:bio-msa-storage:track ")]
        launch = events.index("launch:CPU.360V.1440G")
        self.assertEqual(len(checks), 2)
        self.assertEqual(len(tracks), 2)
        self.assertLess(checks[0], checks[1])
        self.assertLess(checks[1], tracks[0])
        self.assertLess(tracks[0], launch)
        self.assertLess(launch, tracks[1])
        self.assertLess(tracks[1], events.index("worker"))
        self.assertNotIn("--instance", events[tracks[0]])
        self.assertIn("--instance 12345678-1234-1234-1234-123456789012", events[tracks[1]])
        metadata = json.loads(next((self.root / "results").glob("*/job.json")).read_text())
        self.assertEqual(metadata["database_volume"], "msa-database-volume")

    def test_msa_storage_retirement_before_or_during_queue_and_prelaunch_track_prevents_rental(self):
        for settings in ({"EXPIRE_ON_CHECK": "1"}, {"EXPIRE_ON_CHECK": "2"}, {"EXPIRE_BEFORE_LAUNCH": "1"}):
            with self.subTest(settings=settings):
                (self.root / "msa-storage-checks").unlink(missing_ok=True)
                result = self.submit("msa", "--sub", "install", **self.msa_settings(**settings))
                self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
                self.assertFalse((self.root / "launches").exists())
                self.assertFalse((self.root / "transmitted.sh").exists())

    def test_msa_retirement_after_launch_cleans_worker_without_running_search(self):
        result = self.submit("msa", "--sub", "serve", **self.msa_settings(EXPIRE_AFTER_LAUNCH="1"))
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertEqual((self.root / "removals").read_text().count("rm "), 1)
        self.assertFalse((self.root / "transmitted.sh").exists())

    def test_msa_uses_its_own_lock_while_inference_lock_is_held(self):
        state = self.root / "state"
        state.mkdir()
        with (state / "bio-submit.lock").open("w") as locked:
            fcntl.flock(locked.fileno(), fcntl.LOCK_EX)
            result = self.submit("msa", "--sub", "convert", **self.msa_settings())
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertTrue((state / "msa-submit.lock").exists())

    def test_private_preparation_validates_canonical_model_bundle_before_inference_rental(self):
        for alias, model in (("of3", "openfold3"), ("boltz", "boltz2"), ("protenix", "protenix")):
            with self.subTest(alias=alias):
                (self.root / "events").unlink(missing_ok=True)
                result = self.submit(alias, "--fasta", self.input, "--msa-backend", "private")
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                events = (self.root / "events").read_text().splitlines()
                self.assertIn("--model "+model, events[0])
                self.assertLess(events.index("validate:"+model), events.index("launch:1A100.22V"))
                self.assertNotIn("--volume msa-database-volume", (self.root / "launch-args").read_text())
                staged = list((self.root / "shared" / "runs").glob(model+"-*/in/prepared/native-input.bin"))
                self.assertEqual(len(staged), 1)
                self.assertEqual(staged[0].read_bytes(), b"opaque native model input")
        self.assertFalse(list((self.root / "state").glob("msa-result.*.json")))

    def test_failed_missing_or_invalid_private_preparation_never_falls_back_or_rents_inference_gpu(self):
        cases = ({"PREPARATION_EXIT": "29"}, {"PREPARATION_MISSING": "1"},
                 {"PREPARATION_BAD": "1"}, {"PREPARATION_BAD_RECEIPT": "1"})
        for settings in cases:
            with self.subTest(settings=settings):
                shutil.rmtree(self.root / "prepared bundle with 'quote", ignore_errors=True)
                result = self.submit("boltz2", "--fasta", self.input, "--msa-backend", "private", **settings)
                self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
                if "PREPARATION_EXIT" in settings:
                    self.assertEqual(result.returncode, 29)
                self.assertNotIn("DONE", result.stdout)
                self.assertFalse((self.root / "launches").exists())
                self.assertFalse((self.root / "transmitted.sh").exists())

    def test_explicit_prepared_bundle_is_validated_staged_and_includes_verified_msa_code(self):
        bundle = self.valid_bundle()
        result = self.submit("boltz2", "--fasta", self.input, "--msa-bundle", bundle, EXECUTE_BUNDLE="1")
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertFalse((self.root / "preparation-calls").exists())
        event = (self.root / "events").read_text().splitlines()
        self.assertLess(event.index("validate:boltz2"), event.index("launch:1A100.22V"))
        staged = next((self.root / "shared" / "runs").glob("*/in/prepared/native-input.bin"))
        self.assertEqual(staged.read_bytes(), (bundle / "native-input.bin").read_bytes())
        archive = next((self.root / "results").glob("*/tools.tar.gz"))
        with tarfile.open(archive) as packed:
            self.assertEqual(packed.extractfile("msa/prepared.py").read(),
                             (self.root / "tools" / "msa" / "prepared.py").read_bytes())
            self.assertIn("msa/databases.py", packed.getnames())
        self.assertIn("BIO_MSA_BUNDLE=", (self.root / "transmitted.sh").read_text())

    def test_invalid_explicit_bundle_stops_before_gpu_without_calling_preparation(self):
        bundle = self.valid_bundle("openfold3")
        result = self.submit("boltz2", "--fasta", self.input, "--msa-bundle", bundle)
        self.assertEqual(result.returncode, 64, result.stdout+result.stderr)
        self.assertFalse((self.root / "launches").exists())
        self.assertFalse((self.root / "preparation-calls").exists())

    def test_nested_private_preparation_finishes_its_worker_before_inference_launch(self):
        result = self.submit("boltz2", "--fasta", self.input, "--msa-backend", "private",
                             **self.msa_settings(NESTED_PREPARATION="1", FETCH_PREPARED="1"))
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual((self.root / "launches").read_text().splitlines(), ["CPU.360V.1440G", "1A100.22V"])
        events = (self.root / "events").read_text().splitlines()
        cleanup = next(i for i, event in enumerate(events) if event.startswith("cleanup:"))
        self.assertLess(cleanup, events.index("launch:1A100.22V"))
        self.assertEqual((self.root / "removals").read_text().count("rm "), 2)

    def test_bundle_result_is_preparation_only_and_published_after_success(self):
        receipt = self.root / "bundle-result.json"
        for sub in ("install", "convert", "serve"):
            with self.subTest(sub=sub):
                result = self.submit("msa", "--sub", sub, "--bundle-result", receipt, **self.msa_settings())
                self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
                self.assertFalse((self.root / "launches").exists())
        result = self.submit("msa", "--sub", "prepare", "--model", "boltz2", "--fasta", self.input,
                             "--bundle-result", receipt, **self.msa_settings(FETCH_PREPARED="1"))
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        bundle = Path(json.loads(receipt.read_text())["bundle"])
        self.assertTrue((bundle / "complete.json").is_file())
        self.assertEqual((self.root / "removals").read_text().count("rm "), 1)

    def test_worker_fetch_validation_and_cleanup_failures_never_publish_bundle_success(self):
        cases = ({"MODEL_EXIT": "17"}, {"FETCH_FAIL": "1"}, {"FETCH_BAD_PREPARED": "1"}, {"DELETE_FAIL": "1"})
        for number, settings in enumerate(cases):
            with self.subTest(settings=settings):
                receipt = self.root / ("bundle-result-"+str(number)+".json")
                before = set((self.root / "results").glob("*/job.json"))
                result = self.submit("msa", "--sub", "prepare", "--model", "boltz2", "--fasta", self.input,
                                     "--bundle-result", receipt, **self.msa_settings(FETCH_PREPARED="1", **settings))
                self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertNotIn("DONE", result.stdout)
                self.assertFalse(receipt.exists())
                metadata, = set((self.root / "results").glob("*/job.json")) - before
                self.assertNotEqual(json.loads(metadata.read_text())["exit_status"], 0)


class MsaRecipeTests(unittest.TestCase):
    """Execute the actual recipe with synthetic host RAM and inert database tools."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        commands = self.root / "bin"
        commands.mkdir()
        # Only /proc/meminfo is replaced. The actual recipe's Python preflight
        # and shell control flow execute, including errexit and early return.
        python = commands / "python3"
        python.write_text(f'#!{sys.executable}\n' + '''import os, sys
from pathlib import Path
from unittest.mock import patch
if sys.argv[1] == '-':
    code = sys.stdin.read()
    sys.argv = sys.argv[1:]
    read_text = Path.read_text
    def synthetic_memory(path, *args, **kwargs):
        if str(path) == '/proc/meminfo':
            return 'MemAvailable: ' + str(int(os.environ['AVAILABLE_GIB']) * 1024**2) + ' kB\\n'
        return read_text(path, *args, **kwargs)
    with patch.object(Path, 'read_text', synthetic_memory):
        exec(compile(code, '<recipe stdin>', 'exec'), {'__name__': '__main__'})
else:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
''')
        python.chmod(0o700)
        nproc = commands / "nproc"
        nproc.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "${TEST_CORES:-64}"\n')
        nproc.chmod(0o700)
        tools = self.root / "tools with spaces"
        (tools / "msa").mkdir(parents=True)
        (tools / "msa" / "tools.sh").write_text(
            'echo bootstrap >> "$AUDIT/events"\nexport MSA_TOOLS_ROOT="$AUDIT/pinned tools"\n')
        stub = '''import json, os, sys
from pathlib import Path
with (Path(os.environ['AUDIT']) / 'calls.jsonl').open('a') as output:
    output.write(json.dumps([Path(__file__).name, *sys.argv[1:]]) + '\\n')
if Path(__file__).name != 'databases.py' or sys.argv[1] != 'convert':
    raise SystemExit('conversion must not start the server, install, validate or prepare')
if int(os.environ.get('DATABASE_EXIT', '0')):
    raise SystemExit(int(os.environ['DATABASE_EXIT']))
print(json.dumps({'stage': 'databases-converted', 'production_ready': False}))
'''
        for name in ("databases.py", "server.py", "prepared.py"):
            (tools / "msa" / name).write_text(stub)
        out = self.root / "output with spaces"
        out.mkdir()
        self.env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                        AUDIT=str(self.root), TOOLS=str(tools), OUT=str(out),
                        MSA_DB_ROOT=str(self.root / "database with spaces"))

    def recipe(self, sub, available_gib, **settings):
        return subprocess.run(["bash", "-c", 'EXTRA_ARGS=(); source "$1"', "recipe-test",
                               str(SCRIPT.parent / "recipes" / "msa.sh")],
                              env=dict(self.env, SUB=sub, AVAILABLE_GIB=str(available_gib), **settings),
                              text=True, capture_output=True, timeout=5)

    def test_conversion_accepts_56_gib_caps_threads_and_exits_before_indexing_or_serving(self):
        result = self.recipe("convert", 56)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "events").read_text().splitlines(), ["bootstrap"])
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(calls, [["databases.py", "convert", "--root", self.env["MSA_DB_ROOT"],
                                 "--tools-root", str(self.root / "pinned tools"), "--threads", "8"]])
        artifact = Path(self.env["OUT"]) / "database-conversion.json"
        self.assertEqual(json.loads(artifact.read_text()),
                         dict(stage="databases-converted", production_ready=False))
        self.assertEqual(list(Path(self.env["OUT"]).iterdir()), [artifact])

    def test_conversion_uses_fewer_available_threads_and_propagates_failure(self):
        result = self.recipe("convert", 56, TEST_CORES="4", DATABASE_EXIT="23")
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        call, = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(call[-2:], ["--threads", "4"])
        self.assertEqual(call[0:2], ["databases.py", "convert"])

    def test_conversion_rejects_insufficient_ram_before_tools_bootstrap(self):
        result = self.recipe("convert", 55)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("database conversion requires at least 56 GiB", result.stderr)
        self.assertFalse((self.root / "events").exists())
        self.assertFalse((self.root / "calls.jsonl").exists())

    def test_full_install_prepare_and_serve_still_require_768_gib(self):
        for sub in ("install", "panel", "prepare", "serve"):
            with self.subTest(sub=sub):
                result = self.recipe(sub, 767)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("full indexed CPU reference requires at least 768 GiB", result.stderr)
                self.assertFalse((self.root / "events").exists())
                self.assertFalse((self.root / "calls.jsonl").exists())

    def test_panel_starts_one_server_routes_manifest_and_preserves_failure_during_cleanup(self):
        tools = Path(self.env["TOOLS"])
        with (tools / "msa/tools.sh").open("a") as output:
            output.write('export MMSEQS_SERVER="$AUDIT/server"\n')
        server = self.root / "server"
        server.write_text(f'#!{sys.executable}\n' + '''import os
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
root = Path(os.environ['AUDIT'])
(root/'server.pid').write_text(str(os.getpid()))
with (root/'events').open('a') as f: f.write('server-start\\n')
HTTPServer(('127.0.0.1',8080),BaseHTTPRequestHandler).serve_forever()
''')
        server.chmod(0o700)
        (tools / "msa/server.py").write_text('''import json,os,sys
from pathlib import Path
assert sys.argv[1] == 'config', 'recipe must leave per-target auditing to panel.py'
path = Path(sys.argv[sys.argv.index('--output')+1])
path.write_text('{}')
path.with_suffix('.provenance.json').write_text('{}')
with (Path(os.environ['AUDIT'])/'events').open('a') as f: f.write('config\\n')
''')
        (tools / "msa/panel.py").write_text('''import json,os,sys
from pathlib import Path
root = Path(os.environ['AUDIT'])
(root/'panel-args.json').write_text(json.dumps(sys.argv[1:]))
assert os.environ['MMSEQS_NUM_THREADS'] == '16'
with (root/'events').open('a') as f: f.write('panel\\n')
raise SystemExit(17)
''')
        (tools / "msa/databases.py").write_text('''import os,sys
from pathlib import Path
assert sys.argv[1] in ('install','validate')
with (Path(os.environ['AUDIT'])/'events').open('a') as f: f.write('database-'+sys.argv[1]+'\\n')
''')
        for sub in ("panel", "install"):
            with self.subTest(sub=sub):
                (self.root / "events").unlink(missing_ok=True)
                result = self.recipe(sub, 800, SHARED=str(self.root / "shared"), IN="manifest.json",
                                     BIO_MSA_PANEL_SHA256="expected-manifest-sha", BIO_JOB_DEADLINE_EPOCH="1234567890")
                self.assertEqual(result.returncode, 17, result.stdout+result.stderr)
                expected = ["bootstrap"] + (["database-install", "database-validate"] if sub == "install" else [])
                self.assertEqual((self.root / "events").read_text().splitlines(), expected+["config", "server-start", "panel"])
                pid = int((self.root / "server.pid").read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
        args = json.loads((self.root / "panel-args.json").read_text())
        self.assertEqual(args[0], "run")
        self.assertEqual(args[args.index("--manifest")+1], "manifest.json")
        self.assertEqual(args[args.index("--expected-sha256")+1], "expected-manifest-sha")
        self.assertEqual(args[args.index("--deadline")+1], "1234567890")
        (self.root / "events").unlink()
        result = self.recipe("install", 800, BIO_MSA_PANEL_SHA256="")
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual((self.root / "events").read_text().splitlines(), ["bootstrap", "database-install", "database-validate"])


if __name__ == "__main__":
    unittest.main()
