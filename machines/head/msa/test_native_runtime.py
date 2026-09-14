"""Pinned native identity, atomic installation and explicit profile selection."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import databases
import native_runtime as native
import search_profile as profiles
import server


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "assets"
        self.target = self.root / native.TOOLS_DIRECTORY
        self.lock = copy.deepcopy(native.manifest())
        # Byte fixtures test the installer, without pretending to run MMseqs.
        for name, entry in self.lock["files"].items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((name + " fixture\n").encode())
            entry.update(sha256=native.digest(path), bytes=path.stat().st_size)
        lockfile = self.root / "lock.json"
        lockfile.write_text(json.dumps(self.lock, sort_keys=True))
        for name, value in (("LOCK", lockfile), ("LOCK_SHA256", native.digest(lockfile))):
            replacement = patch.object(native, name, value)
            replacement.start(); self.addCleanup(replacement.stop)
        self.version = patch.object(native.subprocess, "check_output", side_effect=lambda argv, **kw:
                                    self.lock["mmseqs_version" if argv[-1] == "version" else "backend_version"] + "\n")
        self.run = self.version.start(); self.addCleanup(self.version.stop)

    def test_atomic_install_is_repeatable_and_preserves_existing_official_runtime(self):
        old = self.root / "msa-tools-v1"; old.mkdir()
        (old / "unchanged").write_bytes(b"official")
        first = native.install(self.source, self.target)
        self.assertEqual(native.install(self.source, self.target), first)
        self.assertEqual(native.validate(self.target), first)
        native.validate_provenance(first)
        self.assertEqual((old / "unchanged").read_bytes(), b"official")
        self.assertEqual(first["mmseqs_sha256"], self.lock["files"]["portable/mmseqs"]["sha256"])
        self.assertNotEqual(first["mmseqs_sha256"], first["wrapper_sha256"])
        with self.assertRaisesRegex(ValueError, "separate"):
            native.install(self.source, old)

    def test_damaged_library_or_extra_entry_is_rejected_before_execution(self):
        native.install(self.source, self.target)
        self.run.reset_mock()
        name = "portable/lib/libgomp.so.1"
        path = self.target / name
        original = path.read_bytes()
        path.write_bytes(b"X" + original[1:])
        with self.assertRaisesRegex(ValueError, "member changed"):
            native.validate(self.target)
        self.run.assert_not_called()
        path.write_bytes(original)
        (self.target / "unreviewed").write_bytes(b"payload")
        with self.assertRaisesRegex(ValueError, "inventory"):
            native.validate(self.target)
        self.run.assert_not_called()

    def test_symlinked_input_and_changed_copy_leave_no_installed_runtime(self):
        source = self.source / "portable/mmseqs"
        backup = self.root / "outside"; backup.write_bytes(source.read_bytes())
        source.unlink(); source.symlink_to(backup)
        with self.assertRaisesRegex(ValueError, "regular"):
            native.install(self.source, self.target)
        self.assertFalse(self.target.exists())
        source.unlink(); source.write_bytes(backup.read_bytes())
        original = native.shutil.copyfile
        def corrupt_after_copy(src, dst):
            result = original(src, dst)
            if str(dst).endswith("portable/mmseqs"):
                Path(dst).write_bytes(b"changed during copy")
            return result
        with patch.object(native.shutil, "copyfile", side_effect=corrupt_after_copy):
            with self.assertRaisesRegex(ValueError, "member changed"):
                native.install(self.source, self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.glob("*.staging-*")), [])

    def test_saved_provenance_rejects_incomplete_or_changed_dependency_pins(self):
        receipt = native.install(self.source, self.target)
        for key in ("runtime_files", "runtime_manifest_sha256", "wrapper_sha256", "build_provenance_sha256"):
            changed = copy.deepcopy(receipt); changed.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                native.validate_provenance(changed)
        changed = copy.deepcopy(receipt)
        changed["runtime_files"]["portable/lib/libgomp.so.1"]["sha256"] = "0"*64
        with self.assertRaises(ValueError):
            native.validate_provenance(changed)


class ProfileTests(unittest.TestCase):
    def receipt(self):
        with patch.object(native, "_files"):
            return native.validate(Path("/pinned/msa-tools-prefetch-v1"), check_versions=False)

    def test_new_profile_keeps_legacy_receipts_and_runtime_selection_distinct(self):
        self.assertEqual(profiles.resolve(profiles.MAPPED_PROFILE)["profile_sha256"],
                         "e3dfc3af6e0669fb7cc0ba39bcf472c7e896e38e6aeb45dbb9fa784a668f27a8")
        new = profiles.resolve(profiles.PREFETCH_PROFILE)
        self.assertEqual(new["runtime_manifest_sha256"], native.LOCK_SHA256)
        self.assertEqual(new["posting_readers"], 32)
        self.assertTrue(profiles.is_mapped(new))
        with self.assertRaises(ValueError): profiles.warm_mode(new, "prefetch")
        with patch.object(native, "validate", return_value=self.receipt()) as check:
            databases.tools(Path("/new"), profile=new)
            check.assert_called_once_with(Path("/new"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp); (path/"bin").mkdir(); (path/"bin/mmseqs").write_text("wrapper")
            for profile in (None, profiles.LEGACY_PROFILE, profiles.MAPPED_PROFILE):
                with self.subTest(profile=profile), self.assertRaisesRegex(RuntimeError, "pinned upstream"):
                    databases.tools(path, profile=profile)

    def test_environment_switch_clears_flags_and_live_adoption_rejects_leaks(self):
        environment = dict(os.environ)
        expected = profiles.configure_environment(profiles.PREFETCH_PROFILE, environment)
        self.assertEqual({key: expected[key] for key in profiles.PREFETCH_ENVIRONMENT}, profiles.PREFETCH_ENVIRONMENT)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], env=environment)
        try:
            self.assertEqual(profiles.check_process_environment(child.pid, profiles.PREFETCH_PROFILE), expected)
            with self.assertRaises(ValueError):
                profiles.check_process_environment(child.pid, profiles.MAPPED_PROFILE)
        finally:
            child.terminate(); child.wait(timeout=5)
        self.assertEqual(len(profiles.configure_environment(profiles.MAPPED_PROFILE, environment)), 4)
        self.assertFalse(set(environment) & set(profiles.PREFETCH_ENVIRONMENT))

    def test_server_pins_new_runtime_and_keeps_scientific_configuration(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            ready = dict(prefixes=dict(uniref30="uniref", environmental="env", pdb100="pdb"),
                         pdb70="pdb70", pdbdivided="divided", pdbobsolete="obsolete")
            with patch.object(databases, "validate", return_value=ready), \
                 patch.object(databases, "tools", return_value=self.receipt()) as check:
                config, receipt = server.configuration(root, root/"results", root, profiles.PREFETCH_PROFILE)
                check.assert_called_once_with(root, profile=profiles.resolve(profiles.PREFETCH_PROFILE))
            profiles.validate_configuration(config, receipt, profiles.PREFETCH_PROFILE)
            self.assertEqual(receipt["runtime"]["environment"]["GC_MMSEQS_POSTING_READERS"], "32")
            self.assertFalse(config["paths"]["colabfold"]["parallelstages"])
            for key in profiles.PREFETCH_ENVIRONMENT:
                changed = copy.deepcopy(receipt); changed["runtime"]["environment"][key] = "0"
                with self.subTest(key=key), self.assertRaises(ValueError):
                    profiles.validate_configuration(config, changed, profiles.PREFETCH_PROFILE)
            changed = copy.deepcopy(config); changed["paths"]["mmseqs"] = "/other/mmseqs"
            with self.assertRaises(ValueError):
                profiles.validate_configuration(changed, receipt, profiles.PREFETCH_PROFILE)

    def test_profile_rejects_a_tool_validator_attesting_a_different_runtime(self):
        profile = profiles.resolve(profiles.PREFETCH_PROFILE)
        tools = self.receipt()
        tools['runtime_manifest_sha256'] = '0'*64
        config = dict(app='colabfold', local=dict(workers=1), worker=dict(paralleldatabases=1),
                      paths=dict(mmseqs=tools['mmseqs'], colabfold=dict(parallelstages=False)))
        provenance = dict(tools=tools, search_profile=profile,
            runtime=dict(mmseqs_threads=4, environment=profiles.configure_environment(profile, {})))
        # The profile must independently bind the runtime that a copied helper
        # attests; a self-consistent different helper/lock is insufficient.
        with patch.object(native, 'validate_provenance'), self.assertRaisesRegex(ValueError, 'search profile'):
            profiles.validate_configuration(config, provenance, profile)


if __name__ == "__main__":
    unittest.main()
