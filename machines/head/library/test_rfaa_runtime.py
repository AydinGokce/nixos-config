"""Private runtime publication, artifact safety and CPU command tests, offline."""
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import rfaa_runtime as runtime


class PrivateRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "runtime"
        self.archive, self.wheel = self.base / "python.tar.gz", self.base / "rdkit.whl"
        self.tar({"bin/python3.10": (b"fake interpreter", 0o755), "lib/stdlib.py": (b"stdlib", 0o644)})
        with zipfile.ZipFile(self.wheel, "w") as wheel:
            wheel.writestr("rdkit/__init__.py", "__version__='2024.09.6'\n")
            wheel.writestr("rdkit.libs/library.so", b"chemistry")
        self.site, self.source, self.libraries = self.base / "shared-site", self.base / "shared-source", self.base / "nix-lib"
        for path in (self.site / "torch/lib", self.site / "nvidia/cublas/lib", self.site / "openbabel/lib", self.source, self.libraries):
            path.mkdir(parents=True)
        (self.site / "unchanged").write_text("shared packages must not change")
        self.loader = self.libraries / "ld-linux.so"
        self.loader.write_bytes(b"loader")
        self.options = {"root": self.root, "python_archive": self.archive, "python_sha256": runtime.digest(self.archive),
                        "rdkit_wheel": self.wheel, "shared_site": self.site, "source": self.source,
                        "loader": self.loader, "libraries": [self.libraries], "probe": self.fake_probe}
        self.probes = []
        self.patches = [patch.object(runtime, "RDKIT_WHEEL_SHA256", runtime.digest(self.wheel)),
                        patch.object(runtime.subprocess, "check_output", return_value=runtime.SOURCE_PIN + "\n")]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        # Generation directories are intentionally read only.
        for path in self.base.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
        self.tmp.cleanup()

    def tar(self, files):
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, (raw, mode) in files.items():
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(raw), mode
                archive.addfile(item, io.BytesIO(raw))

    def fake_probe(self, config, scratch):
        self.probes.append(config)
        self.assertTrue(Path(config["command"][-1]).is_file())
        self.assertTrue(scratch.is_dir())
        self.assertEqual(config["pythonpath"][1:], [str(self.site), str(self.source)])
        return {"python": runtime.PYTHON_VERSION, "rdkit": runtime.RDKIT_VERSION, "torch": "2.0.1+cu118",
                "rdkit_path": str(Path(config["pythonpath"][0]) / "rdkit/__init__.py"),
                "torch_path": str(self.site / "torch/__init__.py")}

    def test_success_pins_inputs_inventory_and_exact_private_command(self):
        config = runtime.provision(**self.options)
        generation = Path(config["generation"])
        receipt = runtime.verify_generation(generation)
        self.assertEqual(receipt["inputs"]["python_sha256"], self.options["python_sha256"])
        self.assertEqual(receipt["inputs"]["source_pin"], runtime.SOURCE_PIN)
        self.assertEqual(config["receipt_sha256"], runtime.digest(generation / "receipt.json"))
        self.assertEqual(config["command"][0], str(self.loader))
        self.assertEqual(config["command"][-1], str(generation / "python/bin/python3.10"))
        self.assertEqual(config["pythonpath"][0], str(generation / "chemistry"))
        self.assertIn(str(self.site / "nvidia/cublas/lib"), config["library_paths"])
        self.assertIn(str(self.site / "openbabel/lib"), config["library_paths"])
        self.assertEqual(config["environment"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(json.loads((self.root / "rfaa.json").read_text()), config)
        self.assertEqual(stat.S_IMODE((generation / "python/bin/python3.10").stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE(generation.stat().st_mode), 0o555)
        self.assertEqual((self.site / "unchanged").read_text(), "shared packages must not change")
        self.assertEqual(len(self.probes), 2)  # Staged and promoted locations.

    def test_existing_generation_is_verified_reused_and_reprobed(self):
        first = runtime.provision(**self.options)
        receipt = Path(first["generation"]) / "receipt.json"
        before = receipt.read_bytes()
        second = runtime.provision(**self.options)
        self.assertEqual(first, second)
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(len(self.probes), 3)
        self.assertEqual(len(list((self.root / "generations").iterdir())), 1)

    def test_hash_mismatch_fails_before_probe_or_publication(self):
        self.archive.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            runtime.provision(**self.options)
        self.assertFalse((self.root / "rfaa.json").exists())
        self.assertEqual(self.probes, [])

    def test_wrong_source_pin_fails_without_changing_shared_checkout(self):
        with patch.object(runtime.subprocess, "check_output", return_value="badpin\n"):
            with self.assertRaisesRegex(ValueError, "pinned commit"):
                runtime.provision(**self.options)
        self.assertEqual(list(self.source.iterdir()), [])
        self.assertFalse((self.root / "rfaa.json").exists())

    def test_failed_native_probe_never_publishes_config_or_partial_generation(self):
        def fail(config, scratch):
            raise ValueError("unsupported native version")
        with self.assertRaisesRegex(ValueError, "unsupported native"):
            runtime.provision(**{**self.options, "probe": fail})
        self.assertFalse((self.root / "rfaa.json").exists())
        self.assertEqual(list((self.root / "generations").iterdir()), [])
        self.assertEqual(list(self.root.glob(".rfaa-stage-*")), [])

    def test_existing_working_config_survives_failed_shared_reprobe(self):
        runtime.provision(**self.options)
        before = (self.root / "rfaa.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "shared packages changed"):
            runtime.provision(**{**self.options, "probe": lambda *_: (_ for _ in ()).throw(ValueError("shared packages changed"))})
        self.assertEqual((self.root / "rfaa.json").read_bytes(), before)

    def test_generation_tamper_and_untracked_nested_receipt_are_detected(self):
        config = runtime.provision(**self.options)
        generation = Path(config["generation"])
        added = generation / "chemistry/receipt.json"
        added.parent.chmod(0o755)
        added.write_text("must not bypass inventory")
        with self.assertRaisesRegex(ValueError, "inventory mismatch"):
            runtime.provision(**self.options)
        added.unlink()
        executable = generation / "python/bin/python3.10"
        executable.chmod(0o755)
        executable.write_bytes(b"tampered binary")
        with self.assertRaisesRegex(ValueError, "inventory mismatch"):
            runtime.verify_generation(generation)

    def test_private_root_and_lock_symlinks_are_refused(self):
        self.root.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            runtime.provision(**self.options)
        self.root.unlink()
        self.root.mkdir(mode=0o700)
        victim = self.base / "victim"
        victim.write_text("keep")
        (self.root / "provision.lock").symlink_to(victim)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            runtime.provision(**self.options)
        self.assertEqual(victim.read_text(), "keep")

    def test_world_readable_private_root_is_refused(self):
        self.root.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, "only to its owner"):
            runtime.provision(**self.options)

    def test_tar_traversal_links_and_duplicate_entries_are_refused(self):
        for case in ("../escape", "/absolute", "directory/../escape", "symlink", "hardlink", "duplicate"):
            with self.subTest(case=case):
                with tarfile.open(self.archive, "w:gz") as archive:
                    item = tarfile.TarInfo(case)
                    if case in ("symlink", "hardlink"):
                        item.type = tarfile.SYMTYPE if case == "symlink" else tarfile.LNKTYPE
                        item.linkname = "../../escape"
                    archive.addfile(item, io.BytesIO(b""))
                    if case == "duplicate":
                        archive.addfile(item, io.BytesIO(b""))
                with self.assertRaisesRegex(ValueError, "Unsafe|link|Duplicate"):
                    runtime.extract_archive(self.archive, self.base / ("out-" + str(len(list(self.base.iterdir())))))
        self.assertFalse((self.base.parent / "escape").exists())

    def test_wheel_link_and_expansion_limit_are_refused(self):
        with zipfile.ZipFile(self.wheel, "w") as wheel:
            member = zipfile.ZipInfo("link")
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
            wheel.writestr(member, "../../escape")
        with self.assertRaisesRegex(ValueError, "link or special"):
            runtime.extract_archive(self.wheel, self.base / "bad-wheel", wheel=True)
        with patch.object(runtime, "MAX_EXTRACTED_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "size limit"):
                runtime.extract_archive(self.archive, self.base / "too-big")

    def test_native_probe_hides_gpu_caps_threads_and_rejects_version_drift(self):
        config = runtime.describe(self.base / "private", self.site, self.source, self.loader, [self.libraries])
        report = {"python": runtime.PYTHON_VERSION, "rdkit": runtime.RDKIT_VERSION, "torch": "2.0.1+cu118",
                  "rdkit_path": str(self.base / "private/chemistry/rdkit/__init__.py"), "torch_path": str(self.site / "torch/__init__.py")}
        with patch.object(runtime.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "RFAA_RUNTIME_PROBE=" + json.dumps(report), "")) as run:
            self.assertEqual(runtime.native_probe(config, self.base), report)
        argv, options = run.call_args
        self.assertEqual(argv[0][-3:], ["-s", "-c", runtime.PROBE])
        self.assertEqual(options["env"]["CUDA_VISIBLE_DEVICES"], "")
        self.assertEqual(options["env"]["PYTHONNOUSERSITE"], "1")
        self.assertEqual(options["env"]["OMP_NUM_THREADS"], "2")
        self.assertEqual(options["timeout"], 180)
        self.assertNotIn("shell", options)
        report["rdkit"] = "different-version"
        with patch.object(runtime.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "RFAA_RUNTIME_PROBE=" + json.dumps(report), "")):
            with self.assertRaisesRegex(ValueError, "version differs"):
                runtime.native_probe(config, self.base)

    def test_nix_roots_pin_loader_and_libraries_without_replacing_existing_links(self):
        store = self.base / "nix/store"
        for name in ("hash-glibc", "hash-zlib"):
            (store / name / "lib").mkdir(parents=True)
        config = {"command": [str(store / "hash-glibc/lib/ld-linux.so")],
                  "library_paths": [str(store / "hash-glibc/lib"), str(store / "hash-zlib/lib"), str(self.site)]}
        directory = self.base / "nix/var/nix/gcroots/rfaa"
        roots = runtime.pin_nix_roots(config, directory, store=store)
        self.assertEqual(set(roots.values()), {str(store / "hash-glibc"), str(store / "hash-zlib")})
        self.assertEqual(runtime.pin_nix_roots(config, directory, store=store), roots)
        link = directory / "hash-zlib"
        link.unlink()
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Conflicting Nix GC root"):
            runtime.pin_nix_roots(config, directory, store=store)
        self.assertEqual(link.readlink(), self.source)

    def test_native_probe_rejects_a_module_path_that_traverses_outside_its_root(self):
        config = runtime.describe(self.base / "private", self.site, self.source, self.loader, [self.libraries])
        report = {"python": runtime.PYTHON_VERSION, "rdkit": runtime.RDKIT_VERSION, "torch": "2.0.1+cu118",
                  "rdkit_path": str(self.base / "private/chemistry/../../wrong-rdkit/__init__.py"),
                  "torch_path": str(self.site / "torch/__init__.py")}
        with patch.object(runtime.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "RFAA_RUNTIME_PROBE=" + json.dumps(report), "")):
            with self.assertRaisesRegex(ValueError, "expected package roots"):
                runtime.native_probe(config, self.base)


if __name__ == "__main__":
    unittest.main()
