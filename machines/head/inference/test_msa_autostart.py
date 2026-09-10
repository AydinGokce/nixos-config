"""Private-MSA caller integration, with every cloud/search boundary kept offline."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from inference import frontend
from inference import test_rf3_preparation_cache as cache_fixtures
import test_bio_submit as submission_fixtures

MSA_TOOLS = Path(__file__).resolve().parents[1] / "msa"


class PrivateMSACallerTests(unittest.TestCase):
    def cache_fixture(self):
        fixture = cache_fixtures.RF3PreparationCacheTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.database()
        fixture.args.backend = "private"
        return fixture

    def test_private_rf3_cache_replays_without_any_session_or_subprocess(self):
        fixture = self.cache_fixture()
        first = fixture.next()
        self.assertFalse(first["reused_preparation"])
        self.assertEqual(fixture.calls[0][:2], ["bio-msa", "prepare"])
        self.assertFalse((fixture.root / "sessions/active.json").exists())
        fixture.mock_run.side_effect = AssertionError("A cache hit must not start/check a session or launch a helper")
        with mock.patch.dict("os.environ", {"BIO_MSA_SESSIONS_ROOT": str(fixture.root / "sessions")}):
            second = fixture.next()
        self.assertTrue(second["reused_preparation"])
        self.assertEqual(first["cache_receipt"], second["cache_receipt"])
        self.assertEqual(first["original_search_completed_at_epoch"], second["original_search_completed_at_epoch"])
        self.assertEqual(
            (Path(first["bundle"]) / "msa-search/search.json").read_bytes(),
            (Path(second["bundle"]) / "msa-search/search.json").read_bytes(),
        )
        self.assertFalse((fixture.root / "sessions").exists())

    def test_rf3_lifecycle_only_source_edits_do_not_invalidate_prepared_cache(self):
        fixture = self.cache_fixture()
        identity = frontend.rf3_search_identity(fixture.args)[-1]
        original = frontend.sha256

        def lifecycle_changed(path):
            if Path(path).name in {"session_client.py", "session.py", "lifecycle.py", "bio-msa.sh"}:
                return "f" * 64
            return original(path)

        with mock.patch.object(frontend, "sha256", side_effect=lifecycle_changed):
            self.assertEqual(frontend.rf3_search_identity(fixture.args)[-1], identity)

    def test_non_rf3_explicit_bundles_bypass_session_even_when_prepare_is_unavailable(self):
        for model in ("openfold3", "boltz2", "protenix"):
            with self.subTest(model=model):
                fixture = submission_fixtures.SubmissionTests(methodName="runTest")
                fixture.setUp()
                try:
                    bundle = fixture.valid_bundle(model)
                    result = fixture.submit(model, "--fasta", fixture.input,
                        "--msa-backend", "private", "--msa-bundle", bundle,
                        PREPARATION_EXIT="97", BIO_PUBLIC_MSA_HEAD_PORT="0")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertFalse((fixture.root / "preparation-calls").exists())
                    self.assertNotIn("prepare:", (fixture.root / "events").read_text())
                    staged = next((fixture.root / "shared/runs").glob("*/in/prepared/native-input.bin"))
                    self.assertEqual(staged.read_bytes(), (bundle / "native-input.bin").read_bytes())
                    self.assertFalse((fixture.root / "msa-sessions/active.json").exists())
                finally:
                    fixture.doCleanups()

    def test_progress_survives_real_nested_caller_redirect_without_changing_stdout(self):
        # frontend.rf3_prepare_cached combines both child streams in a retained
        # private-search.log. The inherited sidechannel must still reach the
        # Workbench runner without modifying that cache-identity-bound frontend.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            progress = root / "msa-progress.log"
            progress.touch(mode=0o600)
            output = root / "private-search.log"
            child = (
                "import json, sys; sys.path.insert(0, sys.argv[1]); import lifecycle; "
                "lifecycle.validate_progress_log(); "
                "lifecycle.emit('warming', 'Loading the private database', 'a'*32); "
                "print(json.dumps({'bundle': 'retained-prepared-input'}))"
            )
            environment = dict(os.environ, BIO_MSA_PROGRESS_LOG=str(progress))
            with output.open("wb") as stream:
                result = subprocess.run([sys.executable, "-B", "-c", child, str(MSA_TOOLS)],
                    stdout=stream, stderr=subprocess.STDOUT, env=environment, timeout=10)
            self.assertEqual(result.returncode, 0, output.read_text())
            lines = output.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(progress.read_text(), lines[0] + "\n")
            prefix, stage, payload = lines[0].split(" ", 2)
            self.assertEqual((prefix, stage), ("BIO_MSA_SESSION_STAGE", "warming"))
            marker = json.loads(payload)
            self.assertEqual(marker["session_id"], "a" * 32)
            self.assertEqual(marker["message"], "Loading the private database")
            self.assertIsInstance(marker["timestamp_ns"], int)
            self.assertEqual(json.loads(lines[1]), {"bundle": "retained-prepared-input"})

            # Direct CLI callers keep machine-readable JSON stdout as before.
            direct = subprocess.run([sys.executable, "-B", "-c", child, str(MSA_TOOLS)],
                capture_output=True, text=True, env=environment, timeout=10)
            self.assertEqual(direct.returncode, 0, direct.stderr)
            self.assertEqual(json.loads(direct.stdout), {"bundle": "retained-prepared-input"})
            self.assertTrue(direct.stderr.startswith("BIO_MSA_SESSION_STAGE warming "))

    def test_real_cli_malformed_input_never_reaches_ensure_or_cloud_boundaries(self):
        # Import the actual CLI in an isolated interpreter so its top-level
        # module names do not interfere with other model parser test fixtures.
        child = r'''
import json, os, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
import session_client as client
root = Path(sys.argv[2])
queries = root / 'bad.json'
fasta = root / 'bad.fasta'
valid = root / 'valid.fasta'
valid.write_text('>valid\nACDE\n')
queries.write_text('{"A":"ACDE","B":"?"}')
fasta.write_text('>one\nACDE\n>two\nGHIK\n')
cases = [
    ['--model', 'rf3', '--json', str(queries)],
    ['--model', 'rf3', '--fasta', str(valid)],
    ['--model', 'boltz2', '--fasta', str(valid), '--name', '../unsafe'],
    ['--model', 'protenix', '--fasta', str(valid), '--worker', 'unsafe-override'],
    ['--model', 'openfold3', '--fasta', str(valid), '--timeout', '1'],
]
cases.extend(['--model', model, '--fasta', str(fasta)]
             for model in ('openfold3', 'boltz2', 'protenix'))
with mock.patch.dict(os.environ, {'BIO_MSA_PROGRESS_LOG': ''}), \
     mock.patch.object(client, 'ensure_session', side_effect=AssertionError('startup attempted')) as ensure, \
     mock.patch.object(client, 'ready_session', side_effect=AssertionError('session consulted')) as ready, \
     mock.patch.object(client.subprocess, 'run', side_effect=AssertionError('subprocess launched')) as run, \
     mock.patch.object(client.subprocess, 'check_output', side_effect=AssertionError('cloud/unit consulted')) as check:
    for arguments in cases:
        try:
            client.main(['prepare', '--root', str(root/'sessions'), *arguments])
        except ValueError:
            pass
        else:
            raise AssertionError('Malformed request was accepted: ' + repr(arguments))
    assert not (root/'sessions').exists()
    print(json.dumps({'invalid_cases': len(cases), 'ensure_calls': ensure.call_count,
        'ready_calls': ready.call_count, 'subprocess_calls': run.call_count + check.call_count}))
'''
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, "-B", "-c", child, str(MSA_TOOLS), temporary],
                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {"invalid_cases": 8,
            "ensure_calls": 0, "ready_calls": 0, "subprocess_calls": 0})


if __name__ == "__main__":
    unittest.main()
