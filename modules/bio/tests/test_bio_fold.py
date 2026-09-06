"""Exercise the local orchestrator's completion and failure propagation offline.

Run: python3 -m unittest discover -s modules/bio/tests -p test_bio_fold.py -v
SSH, upload, download, and visualization are replaced with temporary commands.
"""
import json
import os
from pathlib import Path
import shlex
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
        if os.environ.get("BIO_TEST_TEMPLATES"):
            for directory in ("prepared-bundle", "prepared-native", "template_data"):
                template = out / directory / "template.cif"
                template.parent.mkdir()
                template.write_text("template, not a prediction\n")
        (out / "prediction.pdb").write_text("END\n")
        if os.environ.get("BIO_TEST_NESTED_PREDICTION"):
            predicted = out / "boltz_results_input" / "predictions" / "input" / "input_model_0.cif"
            predicted.parent.mkdir(parents=True)
            predicted.write_text("prediction\n")
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

    def run_arguments(self, *arguments, **env):
        return subprocess.run(["bash", str(SCRIPT), *arguments, "--out", str(self.out)],
                              env=dict(self.env, **env), text=True, capture_output=True, timeout=15)

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

    def test_private_backend_is_forwarded_as_submission_option(self):
        result = self.run_fold("--msa-backend", "private", "--", "--seed", "5")
        self.assert_success(result)
        remote = shlex.split(self.commands("ssh")[0][-1])
        self.assertEqual(remote[remote.index("--msa-backend") + 1], "private")
        self.assertLess(remote.index("--msa-backend"), remote.index("--"))

    def test_invalid_backend_fails_before_upload(self):
        result = self.run_fold("--msa-backend", "unknown")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.commands("scp"), [])
        self.assertEqual(self.commands("ssh"), [])

    def test_prepared_templates_are_excluded_from_render_fallback(self):
        result = self.run_fold("--render", BIO_TEST_TEMPLATES="1")
        self.assert_success(result)
        self.assertEqual(self.commands("bio-viz")[0][1], str(self.out / "prediction.pdb"))

    def test_model_prediction_is_preferred_to_other_structures(self):
        result = self.run_fold("--render", BIO_TEST_TEMPLATES="1", BIO_TEST_NESTED_PREDICTION="1")
        self.assert_success(result)
        self.assertEqual(self.commands("bio-viz")[0][1], str(
            self.out / "boltz_results_input" / "predictions" / "input" / "input_model_0.cif"))

    def test_construct_reference_is_forwarded_without_upload(self):
        result = self.run_arguments("boltz2", "--construct", "construct:enzyme@3", "--render")
        self.assert_success(result)
        self.assertEqual(self.commands("scp"), [])
        remote = shlex.split(self.commands("ssh")[0][-1])
        self.assertEqual(remote, ["bio-submit", "boltz2", "--construct", "construct:enzyme@3"])
        self.assertEqual(len(self.commands("bio-viz")), 1)

    def test_assembly_reference_and_private_backend_are_forwarded_separately(self):
        result = self.run_arguments("openfold3", "--assembly", "enzyme-oligo", "--msa-backend", "private")
        self.assert_success(result)
        self.assertEqual(self.commands("scp"), [])
        remote = shlex.split(self.commands("ssh")[0][-1])
        self.assertEqual(remote[remote.index("--assembly") + 1], "enzyme-oligo")
        self.assertEqual(remote[remote.index("--msa-backend") + 1], "private")

    def test_construct_input_still_stages_evolvepro_measurement_labels(self):
        labels = self.root / "labels with spaces.csv"
        labels.write_text("variant,activity\nACD,1.0\n")
        result = self.run_arguments("evolvepro", "--construct", "enzyme", "--labels", str(labels))
        self.assert_success(result)
        self.assertEqual(len(self.commands("scp")), 1)
        self.assertIn(str(labels), self.commands("scp")[0])
        remote = shlex.split(self.commands("ssh")[0][-1])
        self.assertEqual(remote[remote.index("--construct") + 1], "enzyme")
        self.assertIn("--labels", remote)
        self.assertNotIn("--fasta", remote)

    def test_library_reference_cannot_override_an_existing_input(self):
        combinations = [
            ["--construct", "a", "--assembly", "b"],
            ["--construct", "a", "--construct", "b"],
            ["--construct", "a", "--seq", "ACD"],
            ["--assembly", "a", "--fasta", str(self.fasta)],
            ["--json", str(self.fasta), "--construct", "a"],
            ["--construct", "a", "--contigs", "[20-20]"],
            ["--seq", "ACD", "--fasta", str(self.fasta)],
        ]
        for arguments in combinations:
            with self.subTest(arguments=arguments):
                result = self.run_arguments("boltz2", *arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.commands("scp"), [])
        self.assertEqual(self.commands("ssh"), [])

    def test_library_reference_missing_or_empty_value_fails_before_network(self):
        for flag in ["--construct", "--assembly"]:
            with self.subTest(flag=flag):
                result = subprocess.run(["bash", str(SCRIPT), "boltz2", flag], env=self.env,
                                        text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 2)
                self.assertIn("needs a value", result.stderr)
                result = self.run_arguments("boltz2", flag, "")
                self.assertEqual(result.returncode, 2)
                self.assertIn("nonempty", result.stderr)
        self.assertEqual(self.commands("ssh"), [])

    def test_library_reference_rejected_for_backbone_tools(self):
        for model in ["mpnn", "rfdiffusion"]:
            with self.subTest(model=model):
                result = self.run_arguments(model, "--construct", "enzyme")
                self.assertEqual(result.returncode, 2)
                self.assertIn("structure input", result.stderr)
        self.assertEqual(self.commands("scp"), [])
        self.assertEqual(self.commands("ssh"), [])

    def test_json_input_keeps_its_type_in_remote_submission(self):
        source = self.root / "native input.json"
        source.write_text('{"queries":{}}\n')
        result = self.run_arguments("openfold3", "--json", str(source))
        self.assert_success(result)
        remote = shlex.split(self.commands("ssh")[0][-1])
        self.assertIn("--json", remote)
        self.assertNotIn("--fasta", remote)
        self.assertTrue(remote[remote.index("--json") + 1].endswith(".json"))
        self.assertEqual(len(self.commands("scp")), 1)

    def test_library_ref_is_shell_quoted_without_interpretation(self):
        ref = "enzyme'; touch /tmp/unwanted; echo '"
        result = self.run_arguments("boltz2", "--construct", ref)
        self.assert_success(result)
        remote = shlex.split(self.commands("ssh")[0][-1])
        self.assertEqual(remote, ["bio-submit", "boltz2", "--construct", ref])
        self.assertEqual(self.commands("scp"), [])

    def test_rfdiffusion_contigs_and_optional_pdb_remain_supported(self):
        source = self.root / "backbone.pdb"
        source.write_text("END\n")
        result = self.run_arguments("rfdiffusion", "--contigs", "[20-20]", "--pdb", str(source))
        self.assert_success(result)
        remote = shlex.split(self.commands("ssh")[0][-1])
        self.assertEqual(remote[remote.index("--contigs") + 1], "[20-20]")
        self.assertIn("--input-pdb", remote)

    def test_explicit_sequence_temporary_file_is_removed_after_submission(self):
        result = self.run_arguments("esm", "--seq", "ACDEFGHIK")
        self.assert_success(result)
        uploaded = Path(self.commands("scp")[0][-2])
        self.assertFalse(uploaded.exists())


if __name__ == "__main__":
    unittest.main()
