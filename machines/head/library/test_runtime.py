"""Bounded CPU invocation and compatibility-check command tests, offline."""
from contextlib import redirect_stdout, redirect_stderr
import io
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import runtime
import cli
import rfaa_runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.config = {"python": sys.executable, "shared": str(self.base / "shared"),
                       "state": str(self.base / "state"), "library_paths": ["/test/nix/lib"],
                       "path": "/test/nix-git/bin:/test/nix-coreutils/bin",
                       "rfaa_config": str(self.base / "rfaa.json")}
        self.arguments = ["--root", str(self.base / "library"), "--ref", "construct:enzyme@1",
                          "--model", "boltz2", "--out", str(self.base / "output"), "--msa-backend", "public"]
        self.scratch = self.base / "scratch"
        self.scratch.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def environment(self, name):
        site = Path(self.config["shared"]) / "envs" / name / "lib/python3.12/site-packages"
        (site / "nvidia/cuda_runtime/lib").mkdir(parents=True)
        (site / "torch/lib").mkdir(parents=True)
        return site

    def rfaa_fixture(self):
        """Actual private inventory and receipt; no scientific packages/GPU."""
        site, source, libraries = self.base/'rfaa-shared', self.base/'rfaa-source', self.base/'nix-libraries'
        for path in (site/'torch/lib', source, libraries):
            path.mkdir(parents=True, exist_ok=True)
        loader = libraries/'loader'
        loader.write_bytes(b'loader fixture')
        inputs = {'schema': 1, 'python_sha256': 'a'*64, 'rdkit_sha256': rfaa_runtime.RDKIT_WHEEL_SHA256,
                  'source_pin': rfaa_runtime.SOURCE_PIN, 'shared_site': str(site), 'source': str(source),
                  'loader': str(loader), 'libraries': [str(libraries)]}
        identity = hashlib.sha256(rfaa_runtime.canonical(inputs)).hexdigest()
        generation = Path(self.config['rfaa_config']).parent/'generations'/identity
        for path in (generation/'python/bin', generation/'python/lib', generation/'chemistry/rdkit', generation/'chemistry/rdkit.libs'):
            path.mkdir(parents=True)
        (generation/'python/bin/python3.10').write_bytes(b'private interpreter fixture')
        (generation/'chemistry/rdkit/__init__.py').write_bytes(b'private rdkit fixture')
        receipt = {'schema': 1, 'kind': 'rfaa-cpu-runtime', 'inputs': inputs, 'inventory': rfaa_runtime.inventory(generation)}
        (generation/'receipt.json').write_bytes(rfaa_runtime.canonical(receipt))
        description = rfaa_runtime.describe(generation, site, source, loader, [libraries])
        description.update(generation=str(generation), receipt_sha256=rfaa_runtime.digest(generation/'receipt.json'))
        Path(self.config['rfaa_config']).write_text(json.dumps(description))
        return description

    def test_plain_fasta_has_no_gpu_or_model_package_environment(self):
        argv, env = runtime.command([*self.arguments, "--plain-fasta"], self.config, self.scratch)
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[2:], ["compile", *self.arguments, "--plain-fasta"])
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "")
        self.assertEqual(env["PATH"], self.config["path"])
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertEqual(env["TMPDIR"], str(self.scratch))
        self.assertEqual(env["PYTHONPYCACHEPREFIX"], str(self.scratch / "python"))
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("LD_LIBRARY_PATH", env)

    def test_sequence_models_use_plain_cpu_interpreter_without_shared_environments(self):
        for model in ["esm", "evolvepro"]:
            arguments = list(self.arguments)
            arguments[arguments.index("--model") + 1] = model
            with self.subTest(model=model):
                _, env = runtime.command(arguments, self.config, self.scratch)
                self.assertNotIn("PYTHONPATH", env)

    def test_native_model_uses_matching_readonly_shared_packages_and_local_cache(self):
        for model, environment in [("boltz2", "boltz"), ("openfold3", "openfold3"), ("protenix", "protenix"), ("rf3", "rf3")]:
            site = self.environment(environment)
            arguments = list(self.arguments)
            arguments[arguments.index("--model") + 1] = model
            with self.subTest(model=model):
                argv, env = runtime.command(arguments, self.config, self.scratch)
                self.assertEqual(argv[0], sys.executable)
                self.assertEqual(env["PYTHONPATH"], str(site))
                self.assertIn(str(site / "nvidia/cuda_runtime/lib"), env["LD_LIBRARY_PATH"].split(":"))
                self.assertIn(str(site / "torch/lib"), env["LD_LIBRARY_PATH"].split(":"))
                self.assertEqual(env["OMP_NUM_THREADS"], "2")
                self.assertEqual(env["OPENBLAS_NUM_THREADS"], "2")
                self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "")

    def test_missing_native_environment_fails_without_bootstrapping_packages(self):
        with self.assertRaisesRegex(ValueError, "environment is missing"):
            runtime.command(self.arguments, self.config, self.scratch)
        self.assertFalse(Path(self.config["shared"]).exists())

    def test_rfaa_uses_private_interpreter_overlay_and_exact_shared_source(self):
        description = self.rfaa_fixture()
        arguments = list(self.arguments)
        arguments[arguments.index("--model") + 1] = "rfaa"
        argv, env = runtime.command(arguments, self.config, self.scratch)
        self.assertEqual(argv[:4], description["command"])
        self.assertEqual(env["PYTHONPATH"], ":".join(description["pythonpath"]))
        self.assertEqual(env["RFAA_SOURCE_DIR"], description['environment']['RFAA_SOURCE_DIR'])
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "")

    def test_rfaa_tampered_private_bytes_fail_before_any_native_subprocess(self):
        description = self.rfaa_fixture()
        (Path(description['generation'])/'python/bin/python3.10').write_bytes(b'tampered interpreter')
        config = self.base/'main-runtime.json'
        config.write_text(json.dumps(self.config))
        arguments = list(self.arguments)
        arguments[arguments.index('--model')+1] = 'rfaa'
        with patch.object(sys, 'argv', ['runtime.py', '--config', str(config), *arguments]), \
                patch.object(runtime.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'inventory mismatch'):
                runtime.main()
        run.assert_not_called()

    def test_rfaa_command_import_path_and_environment_cannot_escape_the_receipt(self):
        description = self.rfaa_fixture()
        variants = [dict(description, command=['/unverified/python']),
                    dict(description, pythonpath=['/unverified/packages']),
                    dict(description, library_paths=['/unverified/libraries']),
                    dict(description, environment={'RFAA_SOURCE_DIR': '/wrong-source'})]
        for altered in variants:
            with self.subTest(altered=next(key for key in altered if altered[key] != description[key])):
                Path(self.config['rfaa_config']).write_text(json.dumps(altered))
                with self.assertRaisesRegex(ValueError, 'differs from its verified generation'):
                    runtime.rfaa_description(self.config)

    def test_rfaa_receipt_hash_and_generation_location_are_bound(self):
        description = self.rfaa_fixture()
        changed = dict(description, receipt_sha256='0'*64)
        Path(self.config['rfaa_config']).write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'receipt SHA256 mismatch'):
            runtime.rfaa_description(self.config)
        changed = dict(description, generation=str(self.base/'other/generations'/Path(description['generation']).name))
        Path(self.config['rfaa_config']).write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'outside its configuration root'):
            runtime.rfaa_description(self.config)
        # Replacing both a receipt and its advertised hash cannot silently
        # retarget an existing generation to different pinned inputs.
        receipt_path = Path(description['generation'])/'receipt.json'
        receipt = json.loads(receipt_path.read_text())
        receipt['inputs']['python_sha256'] = 'b'*64
        receipt_path.write_bytes(rfaa_runtime.canonical(receipt))
        changed = dict(description, receipt_sha256=rfaa_runtime.digest(receipt_path))
        Path(self.config['rfaa_config']).write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'does not match its pinned inputs'):
            runtime.rfaa_description(self.config)

    def test_rfaa_config_and_private_generation_symlinks_are_refused(self):
        description = self.rfaa_fixture()
        config_path = Path(self.config['rfaa_config'])
        other = self.base/'other-config.json'
        config_path.rename(other)
        config_path.symlink_to(other)
        with self.assertRaises(ValueError):
            runtime.rfaa_description(self.config)
        config_path.unlink()
        other.rename(config_path)
        generation = Path(description['generation'])
        renamed = generation.with_name('private-real')
        generation.rename(renamed)
        generation.symlink_to(renamed, target_is_directory=True)
        with self.assertRaises(ValueError):
            runtime.rfaa_description(self.config)

    def test_main_bounds_cpu_process_and_preserves_return_status_and_arguments(self):
        config = self.base / "runtime-config.json"
        config.write_text(json.dumps(self.config))
        commands = []
        def run(argv, **kwargs):
            commands.append((argv, kwargs))
            scratch = next(value.split("=", 2)[2] for value in argv if value.startswith("--setenv=TMPDIR="))
            self.assertTrue(Path(scratch).is_dir())
            return subprocess.CompletedProcess(argv, 17)
        with patch.object(sys, "argv", ["runtime.py", "--config", str(config), *self.arguments, "--plain-fasta"]), \
                patch.object(runtime.subprocess, "run", side_effect=run):
            status = runtime.main()
        self.assertEqual(status, 17)
        argv, options = commands[0]
        self.assertEqual(argv[0], "systemd-run")
        for flag in ["--pipe", "--wait", "--collect", "--property=MemoryMax=6G", "--property=MemorySwapMax=0",
                     "--property=RuntimeMaxSec=600", "--property=KillMode=control-group", "--property=UMask=0077"]:
            self.assertIn(flag, argv)
        self.assertIn("--setenv=CUDA_VISIBLE_DEVICES=", argv)
        self.assertIn("--setenv=PATH=" + self.config["path"], argv)
        self.assertEqual(argv[argv.index("--") + 4:], [*self.arguments, "--plain-fasta"])
        self.assertFalse(options["check"])
        self.assertNotIn("shell", options)
        self.assertEqual(list(Path(self.config["state"]).glob("preflight-*")), [])

    def test_runtime_state_and_lock_symlinks_are_rejected(self):
        config = self.base / "runtime-config.json"
        config.write_text(json.dumps(self.config))
        state = Path(self.config["state"])
        state.mkdir()
        victim = self.base / "victim"
        victim.write_text("keep")
        (state / "preflight.lock").symlink_to(victim)
        with patch.object(sys, "argv", ["runtime.py", "--config", str(config), *self.arguments, "--plain-fasta"]), \
                patch.object(runtime.subprocess, "run") as run:
            with self.assertRaises((ValueError, OSError)):
                runtime.main()
        run.assert_not_called()
        self.assertEqual(victim.read_text(), "keep")


class CheckCommandTests(unittest.TestCase):
    def test_successful_check_reports_compatibility_without_expired_temp_paths(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            directory = Path(argv[argv.index("--out") + 1])
            self.assertTrue(directory.parent.is_dir())
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({
                "bundle": str(directory), "entrypoint": str(directory / "input.json"),
                "source_ref": "construct:enzyme@1", "sha256": "a" * 64,
                "model": "boltz2", "preflight": {"native_parser": True}}), stderr="")
        out = io.StringIO()
        with patch.object(sys, "argv", ["bio-library", "check", "enzyme", "--model", "boltz2"]), \
                patch.object(cli.subprocess, "run", side_effect=run), redirect_stdout(out):
            status = cli.main()
        result = json.loads(out.getvalue())
        self.assertEqual(status, 0)
        self.assertTrue(result["compatible"])
        self.assertNotIn("bundle", result)
        self.assertNotIn("entrypoint", result)
        self.assertFalse(Path(calls[0][calls[0].index("--out") + 1]).parent.exists())

    def test_failed_check_returns_native_status_without_false_compatibility(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["bio-library", "check", "enzyme", "--model", "boltz2"]), \
                patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 23, stdout="", stderr="native parser failed\n")), \
                redirect_stdout(out), redirect_stderr(err):
            status = cli.main()
        self.assertEqual(status, 23)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("native parser failed", err.getvalue())

    def test_private_check_explicitly_uses_plain_fasta(self):
        with patch.object(sys, "argv", ["bio-library", "check", "enzyme", "--model", "openfold3", "--msa-backend", "private"]), \
                patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, stdout="", stderr="")) as run:
            cli.main()
        self.assertIn("--plain-fasta", run.call_args.args[0])

    def test_rf3_private_check_keeps_native_multichain_chemistry(self):
        with patch.object(sys, "argv", ["bio-library", "check", "assembly:complex@1", "--model", "rf3", "--msa-backend", "private"]), \
                patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, stdout="", stderr="")) as run:
            cli.main()
        self.assertNotIn("--plain-fasta", run.call_args.args[0])
        self.assertIn("private", run.call_args.args[0])

    def test_rfaa_private_check_cannot_report_a_unsupported_submission_as_compatible(self):
        with patch.object(sys, "argv", ["bio-library", "check", "enzyme", "--model", "rfaa", "--msa-backend", "private"]), \
                patch.object(cli.subprocess, "run") as run:
            with self.assertRaises((ValueError, SystemExit)):
                cli.main()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
