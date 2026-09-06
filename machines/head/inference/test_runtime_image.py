from pathlib import Path
import json
import os
import shutil
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_image as image


class RuntimeImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.venv = self.root / "old-venv"
        self.site = self.venv / "lib/python3.12/site-packages"
        self.site.mkdir(parents=True)
        (self.venv / "bin").mkdir()
        self.python = Path(sys.executable).resolve()
        os.symlink(self.python, self.venv / "bin/python")
        (self.venv / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        metadata = self.site / "protenix-2.0.0.dist-info/METADATA"
        metadata.parent.mkdir(); metadata.write_text("Name: protenix\nVersion: 2.0.0\n")
        self.packages = {"protenix": "2.0.0"}
        self.spec = {"model": "protenix", "venv": str(self.venv), "packages": self.packages,
                     "system_python": {"path": str(self.python), "sha256": image.file_hash(self.python)}}

    def tearDown(self):
        for path in self.root.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() else 0o644)
        self.temp.cleanup()

    def stage(self, destination=None):
        with patch.object(image.subprocess, "check_output", return_value=json.dumps(self.packages)):
            return image.stage(self.spec, destination or self.root / "image")

    def test_image_is_private_copy_with_rewritten_shebang_and_source_untouched(self):
        script = self.venv / "bin/native"
        original = "#!" + str(self.venv / "bin/python") + "\nprint('hello')\n"
        script.write_text(original); script.chmod(0o755)
        (self.site / "module.py").write_text("value = 1\n")
        before = image.inventory(self.venv)
        receipt = self.stage()
        root = self.root / "image"
        self.assertEqual(image.inventory(self.venv), before)
        self.assertEqual(image.validate(root)["sha256"], receipt["sha256"])
        self.assertIn(str(root / "venv/bin/python"), (root / "venv/bin/native").read_text())
        self.assertEqual(script.read_text(), original)
        (self.site / "module.py").write_text("value = 2\n")
        self.assertEqual((root / "venv/lib/python3.12/site-packages/module.py").read_text(), "value = 1\n")

    def test_undeclared_external_symlink_and_pth_fail_before_publication(self):
        external = self.root / "external.py"; external.write_text("value=1\n")
        os.symlink(external, self.site / "module.py")
        with self.assertRaisesRegex(ValueError, "Undeclared external"):
            self.stage()
        self.assertFalse((self.root / "image").exists())
        (self.site / "module.py").unlink()
        (self.site / "evil.pth").write_text("import arbitrary_module; arbitrary_module.run()\n")
        with self.assertRaisesRegex(ValueError, "Unapproved executable"):
            self.stage()
        self.assertFalse((self.root / "image").exists())

    def test_nested_binary_checkpoint_pth_is_copied_without_decoding_or_rewriting(self):
        checkpoint = self.site / "native/resources/model.pth"
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_bytes(b"\x80\x02binary native checkpoint\xff")
        source = self.root / "editable-source"
        source.mkdir()
        (source / "weights.pth").write_bytes(b"\x80\x03not a Python startup hook")
        self.spec["source_roots"] = [{"name": "native", "path": str(source),
                                     "inventory_sha256": image.digest(image.inventory(source))}]
        receipt = self.stage()
        copied = self.root / "image/venv/lib/python3.12/site-packages/native/resources/model.pth"
        self.assertEqual(copied.read_bytes(), checkpoint.read_bytes())
        self.assertEqual((self.root / "image/sources/native/weights.pth").read_bytes(),
                         (source / "weights.pth").read_bytes())
        self.assertEqual(receipt["rewrites"]["native"], {})

    def test_declared_editable_source_is_copied_and_finder_paths_are_relocated(self):
        source = self.root / "editable"; source.mkdir()
        (source / "package").mkdir(); (source / "package/__init__.py").write_text("value=3\n")
        name = "__editable___package_1_0_finder"
        (self.site / (name + ".py")).write_text("MAPPING = {'package': " + repr(str(source / "package")) + "}\n")
        (self.site / "package.pth").write_text(f"import {name}; {name}.install()\n")
        self.spec["source_roots"] = [{"name": "package", "path": str(source), "inventory_sha256": image.digest(image.inventory(source))}]
        self.stage()
        finder = self.root / "image/venv/lib/python3.12/site-packages" / (name + ".py")
        self.assertIn(str(self.root / "image/sources/package/package"), finder.read_text())
        self.assertTrue((self.root / "image/sources/package/package/__init__.py").is_file())

    def test_failed_actual_package_probe_leaves_unready_image(self):
        with patch.object(image.subprocess, "check_output", return_value='{"protenix":"unexpected"}'):
            with self.assertRaisesRegex(ValueError, "package probe differs"):
                image.stage(self.spec, self.root / "image")
        self.assertFalse((self.root / "image/ready.json").exists())
        with self.assertRaises(FileNotFoundError):
            image.validate(self.root / "image")

    def test_image_and_system_interpreter_tampering_are_rejected(self):
        (self.site / "module.py").write_text("value=1\n")
        self.stage()
        file = self.root / "image/venv/lib/python3.12/site-packages/module.py"
        file.chmod(0o644); file.write_text("value=9\n")
        with self.assertRaisesRegex(ValueError, "image or base interpreter changed"):
            image.validate(self.root / "image")

    def test_source_package_pin_and_required_source_pin_are_mandatory(self):
        self.spec["packages"] = {"protenix": "bad"}
        with self.assertRaisesRegex(ValueError, "package inventory"):
            self.stage()
        self.spec["packages"] = self.packages
        self.spec["required_files"] = {"lib/python3.12/site-packages/model.py": "0" * 64}
        with self.assertRaisesRegex(ValueError, "Required native source"):
            self.stage()

    def test_real_same_host_python_uses_only_the_copied_venv_metadata(self):
        source = self.root / "real-venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(source)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        site = source / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        metadata = site / "protenix-2.0.0.dist-info/METADATA"
        metadata.parent.mkdir(parents=True)
        metadata.write_text("Name: protenix\nVersion: 2.0.0\n")
        spec = dict(self.spec, venv=str(source))
        receipt = image.stage(spec, self.root / "real-image")
        self.assertEqual(image.validate(self.root / "real-image")["sha256"], receipt["sha256"])
        observed = json.loads(subprocess.check_output([receipt["python"], "-I", "-B", "-c",
            "import sys,json; print(json.dumps({'prefix':sys.prefix}))"], text=True))
        self.assertEqual(observed["prefix"], str(self.root / "real-image/venv"))

    def test_reuse_links_only_verified_immutable_copy_and_failure_keeps_it_readonly(self):
        original = self.site / "large-library.so"
        original.write_bytes(b"verified content")
        first = self.stage(self.root / "first")
        copied = self.root / "first/venv/lib/python3.12/site-packages/large-library.so"
        self.spec["reuse_images"] = [{"path": str(self.root / "first"), "sha256": first["sha256"]}]
        second = self.stage(self.root / "second")
        second_copy = self.root / "second/venv/lib/python3.12/site-packages/large-library.so"
        self.assertEqual(copied.stat().st_ino, second_copy.stat().st_ino)
        self.assertNotEqual(copied.stat().st_ino, original.stat().st_ino)
        self.assertEqual(image.validate(self.root / "second")["sha256"], second["sha256"])
        (self.site / "zzz.pth").write_text("import unapproved\n")
        with self.assertRaisesRegex(ValueError, "Unapproved executable"):
            self.stage(self.root / "failed")
        self.assertFalse(copied.stat().st_mode & 0o222)
        self.assertEqual(image.validate(self.root / "first")["sha256"], first["sha256"])

    def test_rf3_wheel_pin_and_system_dependency_tampering(self):
        shutil.rmtree(self.site / "protenix-2.0.0.dist-info")
        metadata = self.site / "rc_foundry.dist-info/METADATA"
        metadata.parent.mkdir(); metadata.write_text("Name: rc-foundry\nVersion: 0.2.1.dev16+gb02eed6a6\n")
        self.packages = {"rc-foundry": "0.2.1.dev16+gb02eed6a6"}
        self.spec.update(model="rf3", packages=self.packages)
        dependency = self.root / "libsystem.so"; dependency.write_bytes(b"base runtime")
        self.spec["system_runtime"] = {"files": {str(dependency): image.file_hash(dependency)}}
        self.stage()
        dependency.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "system runtime file changed"):
            image.validate(self.root / "image")


if __name__ == "__main__":
    unittest.main()
