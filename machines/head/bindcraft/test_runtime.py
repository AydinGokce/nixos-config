"""Installation evidence and submitted-input checks before native execution."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.shared = Path(self.temp.name)
        self.root = self.shared / "bindcraft"
        self.source = "src/bindcraft-" + runtime.PIN
        self.files = {"env/bin/python": b"test fixture, never executed",
                      self.source + "/bindcraft.py": b"# pinned test fixture\n",
                      self.source + "/functions/dssp": b"fixture",
                      self.source + "/functions/DAlphaBall.gcc": b"fixture",
                      "cpu-install-check.json": b'{"status":"passed"}',
                      "params/params_model_1_ptm.npz": b"fixture"}
        for name, data in self.files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        identity = {"pins_sha256": runtime.file_digest(HERE / "pins.json"),
                    "lock_sha256": runtime.file_digest(HERE / "linux-64-cuda.lock.json"),
                    "installer_sha256": runtime.file_digest(HERE / "install.py")}
        self.manifest = {
            "schema": "bio-bindcraft-install.v1", "readiness": "ready", "python": "env/bin/python",
            "bindcraft_source": self.source, "params": "params",
            "fingerprint": runtime.digest(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()),
            "source_files": {name: runtime.digest(data) for name, data in self.files.items() if name.startswith("src/")},
            "environment_check": {"path": "cpu-install-check.json", "sha256": runtime.file_digest(self.root / "cpu-install-check.json")},
            "components": {name: {"ready": True} for name in ("environment", "sources", "af2", "pyrosetta")}}
        self.manifest["components"]["pyrosetta"].update(license_confirmed=False, use_scope="evaluation_requested_by_user; commercial_license_pending")
        self.manifest["components"]["af2"]["files"] = [{"path": "params/params_model_1_ptm.npz", "size": 7, "sha256": runtime.digest(b"fixture")}]
        self.write()

    def write(self):
        (self.root / "install-manifest.json").write_bytes(runtime.encoded(self.manifest))

    def test_evaluation_readiness_does_not_assert_a_commercial_license(self):
        _, manifest = runtime.installation(self.shared)
        self.assertEqual(manifest["readiness"], "ready")
        self.assertIs(manifest["components"]["pyrosetta"]["license_confirmed"], False)

    def test_missing_partial_or_differently_pinned_installation_is_not_ready(self):
        original = deepcopy(self.manifest)
        for changes, message in [({"readiness": "awaiting_pyrosetta_license"}, "incomplete"),
                                 ({"fingerprint": "0" * 64}, "differs"),
                                 ({"python": "../foreign/python"}, "paths differ")]:
            with self.subTest(changes=changes):
                self.manifest = {**original, **changes}
                self.write()
                with self.assertRaisesRegex(ValueError, message):
                    runtime.installation(self.shared)

    def test_source_and_native_check_tampering_is_detected(self):
        for name in (self.source + "/bindcraft.py", self.source + "/functions/DAlphaBall.gcc", "cpu-install-check.json"):
            with self.subTest(name=name):
                file = self.root / name
                original = file.read_bytes()
                file.write_bytes(original + b"x")
                with self.assertRaises(ValueError):
                    runtime.installation(self.shared)
                file.write_bytes(original)

    def test_weight_size_and_full_hash_validation(self):
        file = self.root / "params/params_model_1_ptm.npz"
        file.write_bytes(b"changed")
        runtime.installation(self.shared)
        with self.assertRaisesRegex(ValueError, "weight hash"):
            runtime.installation(self.shared, verify_assets=True)
        file.write_bytes(b"truncated")
        with self.assertRaisesRegex(ValueError, "changed AF2"):
            runtime.installation(self.shared)

    def test_changed_submitted_bundle_stops_before_preflight_or_native_execution(self):
        input_path = self.shared / "input.tar.gz"
        input_path.write_bytes(b"changed after head validation")
        with patch.dict(runtime.os.environ, {"BIO_BINDCRAFT_BUNDLE_SHA256": "0" * 64}), \
                patch.object(runtime, "preflight", side_effect=AssertionError("must not run")):
            for function in (runtime.run, runtime.smoke_run):
                with self.assertRaisesRegex(ValueError, "head-validated"):
                    function(input_path, self.shared, self.shared / "out")
        self.assertFalse((self.shared / "out").exists())


if __name__ == "__main__":
    unittest.main()
