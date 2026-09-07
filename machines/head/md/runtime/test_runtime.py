import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("md_runtime_restore", HERE / "restore.py")
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)
pack_spec = importlib.util.spec_from_file_location("md_runtime_pack", HERE / "pack.py")
pack = importlib.util.module_from_spec(pack_spec)
pack_spec.loader.exec_module(pack)


class RuntimeTests(unittest.TestCase):
    def test_bad_archive_hash_does_not_create_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "runtime.tar.gz"
            archive.write_bytes(b"not the pinned archive")
            with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
                restore.restore(archive, "0" * 64, root / "destination")
            self.assertFalse((root / "destination").exists())

    def test_archive_traversal_and_escape_links_are_rejected_before_extraction(self):
        for name, target in [("../escape", None), ("/escape", None), ("lib/bad", "../../escape"), ("lib/bad", "/etc/passwd")]:
            member = tarfile.TarInfo(name)
            if target is not None:
                member.type, member.linkname = tarfile.SYMTYPE, target
            with self.subTest(name=name, target=target), self.assertRaises(ValueError):
                restore.validate_members([member])

    def test_internal_library_links_are_supported(self):
        member = tarfile.TarInfo("lib/libblas.so")
        member.type, member.linkname = tarfile.SYMTYPE, "../lib/libopenblas.so"
        restore.validate_members([member])

    def test_duplicate_archive_paths_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            restore.validate_members([tarfile.TarInfo("lib/file.a"), tarfile.TarInfo("lib/file.a")])

    def test_pmx_preprocessor_uses_bundled_compiler_and_rejects_substitutions(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            (prefix / "bin").mkdir()
            with self.assertRaisesRegex(ValueError, "missing"):
                restore.pmx_preprocessor(prefix)
            compiler = prefix / "bin/x86_64-conda-linux-gnu-cpp"
            compiler.write_text("pinned compiler fixture")
            compiler.chmod(0o755)
            restore.pmx_preprocessor(prefix)
            alias = prefix / "bin/cpp"
            self.assertEqual(alias.resolve(), compiler)
            restore.pmx_preprocessor(prefix)
            alias.unlink()
            alias.symlink_to("/usr/bin/cpp")
            with self.assertRaisesRegex(ValueError, "Unexpected"):
                restore.pmx_preprocessor(prefix)

    def test_restore_smoke_supplies_runtime_path_without_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "runtime.tar.gz"
            archive.write_bytes(b"verified archive already restored")
            sha = hashlib.sha256(archive.read_bytes()).hexdigest()
            prefix = root / "runtime"
            (prefix / "bin").mkdir(parents=True)
            (prefix / "manifest.json").write_text(json.dumps({"fingerprint": "fixture", "variant": "cpu"}))
            (prefix / ".bio-md-archive.json").write_text(json.dumps({"archive_sha256": sha}))
            (prefix / "bin/gmx").write_text("#!/usr/bin/env bash\n")
            compiler = prefix / "bin/x86_64-conda-linux-gnu-cpp"
            compiler.write_text("pinned compiler fixture")
            compiler.chmod(0o755)
            with patch.dict(restore.os.environ, {"PATH": "/minimal-host-tools", "PYTHONPATH": "/unrelated"}), \
                 patch.object(restore.subprocess, "run") as run:
                restore.restore(archive, sha, prefix, fingerprint="fixture", smoke=True)
            run.assert_called_once()
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["PATH"].split(restore.os.pathsep)[0], str(prefix / "bin"))
            self.assertEqual(env["PYTHONNOUSERSITE"], "1")
            self.assertNotIn("PYTHONPATH", env)

    def test_pack_chooses_owner_matching_installed_relocated_file(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "installed"
            prefix.mkdir()
            (prefix / "header.h").write_bytes(str(prefix).encode() + b"/correct")
            candidates = []
            for name, content in [("older", b"PLACE/wrong"), ("current", b"PLACE/correct")]:
                path = Path(directory) / name
                path.write_bytes(content)
                candidates.append(SimpleNamespace(source=str(path), target="header.h", file_mode="text", prefix_placeholder="PLACE"))
            files, overlaps = pack.unique_installed_files(prefix, candidates,
                lambda data, mode, old, new: data.replace(old.encode(), new.encode()))
            self.assertEqual(files, [candidates[1]])
            self.assertEqual(overlaps[0]["owners"], 2)
            (prefix / "header.h").write_bytes(b"unexpected modification")
            with self.assertRaisesRegex(ValueError, "No cached owner"):
                pack.unique_installed_files(prefix, candidates, lambda data, *args: data)

    def test_unqualified_runtime_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "runtime.tar.gz"
            with tarfile.open(archive, "w:gz") as out:
                blob = json.dumps({"schema": "bio-md-runtime.v1", "fingerprint": "abc"}).encode()
                item = tarfile.TarInfo("manifest.json")
                item.size = len(blob)
                out.addfile(item, io.BytesIO(blob))
            with self.assertRaises(ValueError):
                restore.restore(archive, hashlib.sha256(archive.read_bytes()).hexdigest(), root / "destination")
            self.assertFalse((root / "destination").exists())

    def test_locks_have_complete_unique_cryptographic_pins(self):
        for variant in ("cpu", "cuda"):
            lock = json.loads((HERE / ("linux-64-" + variant + ".lock.json")).read_text())
            packages = lock["packages"]
            self.assertEqual(len({p["name"] for p in packages}), len(packages))
            self.assertTrue(all(len(p["sha256"]) == 64 and len(p["md5"]) == 32 for p in packages))
            engines = {p["name"]: p for p in packages}
            self.assertEqual(engines["gromacs"]["version"], "2026.3")
            self.assertEqual(engines["plumed"]["version"], "2.10.1")
            self.assertEqual("cuda" in engines["gromacs"]["build"], variant == "cuda")


if __name__ == "__main__":
    unittest.main()
