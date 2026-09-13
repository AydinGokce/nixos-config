"""Real archive round trips, immutable reuse, corruption and extraction fences."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / "py"
sys.path.insert(0, str(SOURCE))
import runtime_package as package
import worker_runtime as runtime


@unittest.skipUnless(shutil.which("zstd") and shutil.which("tar"), "GNU tar and zstd required")
class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.shared = self.root / "shared"
        self.bin = self.shared / "envs/boltz/bin"
        self.bin.mkdir(parents=True)
        (self.shared / "cache/boltz").mkdir(parents=True)
        self.payload = self.shared / "cache/boltz/weights"
        self.payload.write_bytes(b"scientific-weights\0" * 1000)
        (self.bin / "predict").write_text(f"#!{self.shared}/envs/boltz/bin/python\n")
        (self.bin / "predict").chmod(0o755)
        (self.bin / "python").symlink_to(sys.executable)
        os.link(self.bin / "predict", self.bin / "other")
        self.env = patch.dict(os.environ, {"BIO_WORKER_PROGRESS_JSON": "", "BIO_WORKER_PROGRESS_LOG": ""})
        self.env.start(); self.addCleanup(self.env.stop)

    def plan(self):
        return runtime.packaged_plan(self.shared, "boltz2")

    def test_real_round_trip_preserves_bytes_modes_links_and_worker_isolation(self):
        value = self.plan()
        for name in ("one", "two"):
            runtime.stage(self.shared, self.root / name, value)
            prefix = self.root / name
            self.assertEqual((prefix / "cache/boltz/weights").read_bytes(), self.payload.read_bytes())
            self.assertEqual(os.readlink(prefix / "envs/boltz/bin/python"), sys.executable)
            self.assertEqual((prefix / "envs/boltz/bin/predict").stat().st_mode & 0o777, 0o755)
            self.assertNotEqual((prefix / "envs/boltz/bin/predict").stat().st_ino,
                                (prefix / "envs/boltz/bin/other").stat().st_ino)
        (self.root / "one/cache/boltz/weights").write_bytes(b"local-change")
        self.assertEqual((self.root / "two/cache/boltz/weights").read_bytes(), self.payload.read_bytes())
        self.assertFalse(list(self.root.glob(".runtime-download-*")))

    def test_unchanged_sources_reuse_archive_without_recompression(self):
        one = self.plan()
        with patch.object(package.subprocess, "Popen", side_effect=AssertionError("Must not rebuild")):
            two = self.plan()
        self.assertEqual(one["package"], two["package"])

    def test_bindcraft_archive_contains_only_its_pinned_tree_and_is_private_per_worker(self):
        prefix = self.shared / 'bindcraft'
        (prefix / 'env/bin').mkdir(parents=True)
        (prefix / 'env/bin/python').write_bytes(b'portable interpreter fixture')
        (prefix / 'params').mkdir()
        (prefix / 'params/weights.npz').write_bytes(b'BindCraft parameter fixture')
        (prefix / 'install-manifest.json').write_text('{"fixture":true}')
        value = runtime.packaged_plan(self.shared, 'bindcraft')
        self.assertEqual(value['paths'], ['bindcraft'])
        for name in ('bindcraft-one', 'bindcraft-two'):
            runtime.stage(self.shared, self.root / name, value)
            self.assertEqual((self.root / name / 'bindcraft/params/weights.npz').read_bytes(),
                             b'BindCraft parameter fixture')
            self.assertFalse((self.root / name / 'cache/boltz/weights').exists())
        (self.root / 'bindcraft-one/bindcraft/params/weights.npz').write_bytes(b'private worker change')
        self.assertEqual((self.root / 'bindcraft-two/bindcraft/params/weights.npz').read_bytes(),
                         (prefix / 'params/weights.npz').read_bytes())

    def test_same_size_changed_bytes_invalidate_snapshot_even_if_mtime_restored(self):
        before = self.plan()
        info = self.payload.stat()
        self.payload.write_bytes(b"X" * info.st_size)
        os.utime(self.payload, ns=(info.st_atime_ns, info.st_mtime_ns))
        after = self.plan()
        self.assertNotEqual(before["package"]["key"], after["package"]["key"])
        self.assertNotEqual(before["package"]["archive_sha256"], after["package"]["archive_sha256"])

    def test_source_mutation_during_build_is_not_published(self):
        value = runtime.plan(self.shared, "boltz2", fingerprint=True)
        def changed():
            self.payload.write_bytes(b"changed")
            return runtime.plan(self.shared, "boltz2", fingerprint=True)
        with self.assertRaisesRegex(ValueError, "changed during"):
            package.publish(self.shared, value, changed)
        self.assertFalse(list((self.shared / "runtime-packages/v1").glob("*/receipt.json")))
        self.assertFalse(list((self.shared / "runtime-packages/v1").glob(".building-*")))

    def test_corrupted_download_is_refused_before_extraction(self):
        value = self.plan()
        archive = self.shared / value["package"]["relative_archive"]
        archive.chmod(0o600)
        data = archive.read_bytes()
        archive.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        with patch.object(package, "extract") as extract, self.assertRaisesRegex(ValueError, "checksum"):
            runtime.stage(self.shared, self.root / "worker", value)
        extract.assert_not_called()
        self.assertFalse((self.root / "worker").exists())

    def malicious(self, members):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as archive:
            for name, kind, link in members:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.REGTYPE:
                    info.size = 1
                    archive.addfile(info, io.BytesIO(b"x"))
                else:
                    info.linkname = link
                    archive.addfile(info)
        path = self.root / "malicious.tar.zst"
        path.write_bytes(subprocess.check_output(["zstd", "-q", "-c"], input=raw.getvalue()))
        return path

    def test_archive_paths_cannot_escape_or_write_through_symlink_ancestors(self):
        fixtures = [
            [("../outside", tarfile.REGTYPE, "")],
            [("/tmp/outside", tarfile.REGTYPE, "")],
            [("envs/boltz/link", tarfile.SYMTYPE, str(self.root / "outside")),
             ("envs/boltz/link/owned", tarfile.REGTYPE, "")],
            [("envs/boltz/link", tarfile.LNKTYPE, "/etc/passwd")],
        ]
        value = runtime.plan(self.shared, "boltz2")
        (self.root / "outside").mkdir()
        for number, fixture in enumerate(fixtures):
            target = self.root / f"extract-{number}"
            target.mkdir()
            with self.subTest(fixture=number), self.assertRaises(ValueError):
                package.extract(self.malicious(fixture), target, value)
        self.assertEqual(list((self.root / "outside").iterdir()), [])

    def test_package_plan_cannot_redirect_archive_or_overcommit_worker_disk(self):
        value = self.plan()
        changed = dict(value, package=dict(value["package"], relative_archive="../elsewhere"))
        with self.assertRaisesRegex(ValueError, "Invalid packed"):
            runtime.stage(self.shared, self.root / "worker", changed)
        with patch.object(package.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1)), \
             self.assertRaisesRegex(ValueError, "lacks space"):
            runtime.stage(self.shared, self.root / "worker", value)
        self.assertFalse((self.root / "worker").exists())

    def test_concurrent_head_publishers_share_one_immutable_archive(self):
        command = [sys.executable, str(SOURCE / "worker_runtime.py"), "plan", "--package",
                   "--shared", str(self.shared), "--recipe", "boltz2"]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        results = []
        for process in processes:
            output, error = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, error.decode())
            results.append(json.loads(output))
        self.assertEqual(results[0]["package"], results[1]["package"])
        self.assertEqual(len(list((self.shared / "runtime-packages/v1").glob("*/receipt.json"))), 1)


if __name__ == "__main__":
    unittest.main()
