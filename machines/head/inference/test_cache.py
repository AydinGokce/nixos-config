import copy
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("artifact_cache_test_module", Path(__file__).with_name("cache.py"))
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


def identity():
    return {"schema": 1, "artifact_kind": "prepared",
            "input": {"sha256": "a" * 64, "chemistry_sha256": "b" * 64},
            "model": {"name": "rf3", "adapter_sha256": "c" * 64, "parser_sha256": "d" * 64},
            "search": {"backend": "private", "provenance_sha256": "e" * 64,
                       "database": {"status": "verified", "sha256": "f" * 64},
                       "settings_sha256": "0" * 64, "template_mode": "disabled", "pairing_mode": "pairgreedy"}}


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "msas").mkdir()
        (self.source / "input.json").write_text('{"charge":1,"stereo":"R"}\n')
        (self.source / "msas/A.a3m").write_bytes(b">q\nACDE\n>hit TaxID=1\nACdDE\n")
        self.cache = cache.ArtifactCache(self.root / "cache")
        # Immutable cache trees deliberately remove write permission.
        self.addCleanup(lambda: cache._remove_stage(self.root / "cache"))

    def test_roundtrip_fresh_writable_copies_leave_cache_and_source_unchanged(self):
        original = cache.inventory(self.source)
        receipt = self.cache.publish(self.source, identity())
        self.assertEqual(self.cache.lookup(identity()), receipt)
        one = self.cache.materialize(receipt, self.root / "job1")
        two = self.cache.materialize(receipt, self.root / "job2")
        self.assertEqual(cache.inventory(one), original)
        (one / "msas/A.a3m").write_text("job-local mutation")
        self.assertEqual(cache.inventory(two), original)
        self.assertEqual(cache.inventory(self.source), original)
        self.assertEqual(self.cache.lookup(identity()), receipt)
        self.assertNotEqual((one / "input.json").stat().st_ino, (two / "input.json").stat().st_ino)
        self.assertFalse((self.root / "cache/objects" / receipt["content_sha256"] / "data/input.json").stat().st_mode & 0o222)

    def test_key_separates_chemistry_backend_database_parser_and_pairing(self):
        first = identity()
        changes = [("input", "chemistry_sha256", "1" * 64), ("model", "parser_sha256", "2" * 64),
                   ("search", "backend", "public"), ("search", "template_mode", "pdb100"),
                   ("search", "pairing_mode", "none"), ("search", "database", {"status": "verified", "sha256": "3" * 64})]
        for section, field, value in changes:
            with self.subTest(field=field):
                changed = copy.deepcopy(first)
                changed[section][field] = value
                self.assertNotEqual(cache.identity_key(first), cache.identity_key(changed))

    def test_public_unknown_database_is_explicit_and_distinct(self):
        value = identity()
        value["search"].update(backend="public", database={"status": "provider-unreported", "provider": "example.org"})
        self.assertIsNone(self.cache.lookup(value))
        self.cache.publish(self.source, value)
        self.assertIsNone(self.cache.lookup(identity()))

    def test_json_key_order_does_not_change_lookup(self):
        value = identity()
        receipt = self.cache.publish(self.source, value)
        self.assertEqual(self.cache.lookup(dict(reversed(list(value.items())))), receipt)

    def test_payload_deduplicated_but_provenance_receipts_distinct(self):
        one = self.cache.publish(self.source, identity())
        value = identity()
        value["search"]["backend"] = "public"
        two = self.cache.publish(self.source, value)
        self.assertNotEqual(one["key"], two["key"])
        self.assertEqual(one["content_sha256"], two["content_sha256"])
        self.assertEqual(len(list((self.root / "cache/objects").iterdir())), 1)
        self.assertEqual(len(list((self.root / "cache/entries").iterdir())), 2)

    def test_stochastic_feature_artifact_is_refused(self):
        value = identity()
        value["artifact_kind"] = "native-final-tensors"
        with self.assertRaises(cache.CacheError):
            self.cache.publish(self.source, value)

    def test_incomplete_or_nonfinite_identity_refused(self):
        values = [identity() for _ in range(4)]
        del values[0]["search"]["database"]
        values[1]["model"]["adapter_sha256"] = "unpinned"
        values[2]["schema"] = True
        values[3]["extra"] = float("nan")
        for value in values:
            with self.subTest(value=value), self.assertRaises(cache.CacheError):
                self.cache.publish(self.source, value)

    def test_same_identity_different_payload_never_overwrites(self):
        receipt = self.cache.publish(self.source, identity())
        (self.source / "input.json").write_text("changed")
        with self.assertRaisesRegex(cache.CacheError, "different content"):
            self.cache.publish(self.source, identity())
        self.assertEqual(self.cache.lookup(identity()), receipt)

    def test_concurrent_identical_publications_are_atomic(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            values = list(workers.map(lambda _: self.cache.publish(self.source, identity()), range(8)))
        self.assertTrue(all(value == values[0] for value in values))
        self.assertEqual(list((self.root / "cache/staging").iterdir()), [])

    def test_failure_before_object_publish_does_not_create_entry(self):
        with patch.object(cache, "_rename_new", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.cache.publish(self.source, identity())
        self.assertIsNone(self.cache.lookup(identity()))
        self.assertEqual(list((self.root / "cache/staging").iterdir()), [])

    def test_failure_between_object_and_identity_is_recoverable(self):
        with patch.object(cache.os, "link", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.cache.publish(self.source, identity())
        self.assertIsNone(self.cache.lookup(identity()))
        receipt = self.cache.publish(self.source, identity())
        self.assertEqual(receipt, self.cache.lookup(identity()))

    def test_content_and_inventory_tamper_fail_closed(self):
        receipt = self.cache.publish(self.source, identity())
        data = self.root / "cache/objects" / receipt["content_sha256"] / "data"
        item = data / "input.json"
        item.chmod(0o600)
        item.write_text("tamper")
        with self.assertRaisesRegex(cache.CacheError, "checksum/inventory"):
            self.cache.lookup(identity())
        with self.assertRaises(cache.CacheError):
            self.cache.materialize(receipt, self.root / "bad-copy")
        self.assertFalse((self.root / "bad-copy").exists())

    def test_modified_receipt_refused(self):
        receipt = self.cache.publish(self.source, identity())
        receipt["files"]["input.json"]["bytes"] += 1
        with self.assertRaises(cache.CacheError):
            self.cache.materialize(receipt, self.root / "bad-copy")

    def test_symlink_files_directories_and_cache_ancestors_refused(self):
        (self.source / "link").symlink_to(self.source / "input.json")
        with self.assertRaises(cache.CacheError):
            self.cache.publish(self.source, identity())
        (self.source / "link").unlink()
        (self.source / "link").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(cache.CacheError):
            self.cache.publish(self.source, identity())
        (self.root / "alias").symlink_to(self.root / "cache", target_is_directory=True)
        with self.assertRaises(cache.CacheError):
            cache.ArtifactCache(self.root / "alias/nested")

    def test_special_files_and_limits_refused(self):
        os.mkfifo(self.source / "fifo")
        with self.assertRaises(cache.CacheError):
            self.cache.publish(self.source, identity())
        (self.source / "fifo").unlink()
        with patch.object(cache, "MAX_BYTES", 2), self.assertRaises(cache.CacheError):
            self.cache.publish(self.source, identity())

    def test_no_overwrite_even_empty_directory_or_racing_destination(self):
        receipt = self.cache.publish(self.source, identity())
        out = self.root / "out"
        out.mkdir()
        with self.assertRaises(cache.CacheError):
            self.cache.materialize(receipt, out)
        out.rmdir()
        original = cache._rename_new
        def raced(source, destination):
            destination.mkdir()
            return original(source, destination)
        with patch.object(cache, "_rename_new", side_effect=raced), self.assertRaises(FileExistsError):
            self.cache.materialize(receipt, out)
        self.assertEqual(list(out.iterdir()), [])

    def test_cache_and_source_must_be_separate(self):
        with self.assertRaises(cache.CacheError):
            self.cache.publish(self.root, identity())
        receipt = self.cache.publish(self.source, identity())
        with self.assertRaises(cache.CacheError):
            self.cache.materialize(receipt, self.root / "cache/job")

    def test_nfs_unsupported_rename_roundtrip_and_permissions(self):
        with patch.object(cache.ctypes, "CDLL") as libc, patch.object(cache.ctypes, "get_errno", return_value=errno.EINVAL):
            libc.return_value.renameat2.return_value = -1
            receipt = self.cache.publish(self.source, identity())
            output = self.cache.materialize(receipt, self.root / "nfs-job")
        self.assertEqual(cache.inventory(output), cache.inventory(self.source))
        self.assertEqual(self.cache.lookup(identity()), receipt)
        obj = self.root / "cache/objects" / receipt["content_sha256"]
        self.assertFalse((obj / "data/input.json").stat().st_mode & 0o222)
        (output / "input.json").write_text("writable private job")
        self.assertEqual(self.cache.lookup(identity()), receipt)

    def test_nfs_exclusive_claim_preserves_racing_empty_directory(self):
        destination = self.root / "reserved"
        destination.mkdir()
        inode = destination.stat().st_ino
        with self.assertRaises(FileExistsError):
            cache._publish_directory_exclusive(self.source, destination)
        self.assertEqual(destination.stat().st_ino, inode)
        self.assertEqual(list(destination.iterdir()), [])

    def test_nfs_interruption_has_no_entry_and_retains_partial_object(self):
        original = cache.os.link
        calls = []
        def interrupted(source, target, **kwargs):
            calls.append(target)
            if len(calls) == 2:
                raise OSError("injected transfer interruption")
            return original(source, target, **kwargs)
        with patch.object(cache.ctypes, "CDLL") as libc, patch.object(cache.ctypes, "get_errno", return_value=errno.EOPNOTSUPP), patch.object(cache.os, "link", side_effect=interrupted):
            libc.return_value.renameat2.return_value = -1
            with self.assertRaisesRegex(OSError, "injected transfer"):
                self.cache.publish(self.source, identity())
        self.assertIsNone(self.cache.lookup(identity()))
        objects = list((self.root / "cache/objects").iterdir())
        self.assertEqual(len(objects), 1)
        self.assertTrue((objects[0] / ".publication-incomplete").is_file())
        self.assertTrue((self.source / "input.json").is_file())

    def test_rename_permission_failure_never_uses_fallback(self):
        with patch.object(cache.ctypes, "CDLL") as libc, patch.object(cache.ctypes, "get_errno", return_value=errno.EACCES), patch.object(cache, "_publish_directory_exclusive") as fallback:
            libc.return_value.renameat2.return_value = -1
            with self.assertRaises(PermissionError):
                cache._rename_new(self.source, self.root / "unavailable")
            fallback.assert_not_called()

    def test_nfs_post_transfer_cleanup_failure_keeps_incomplete_marker(self):
        destination = self.root / "unfinished"
        with patch.object(cache.shutil, "rmtree", side_effect=OSError("cleanup interrupted")):
            with self.assertRaisesRegex(OSError, "cleanup interrupted"):
                cache._publish_directory_exclusive(self.source, destination)
        self.assertTrue((destination / ".publication-incomplete").is_file())
        self.assertTrue((destination / "input.json").is_file())
        self.assertTrue(self.source.is_dir())

    def test_empty_native_directories_survive_and_are_verified(self):
        (self.source / "msas/paired/empty").mkdir(parents=True)
        with patch.object(cache.ctypes, "CDLL") as libc, patch.object(cache.ctypes, "get_errno", return_value=errno.EINVAL):
            libc.return_value.renameat2.return_value = -1
            receipt = self.cache.publish(self.source, identity())
            result = self.cache.materialize(receipt, self.root / "native")
        self.assertTrue((result / "msas/paired/empty").is_dir())
        self.assertEqual(cache.directories(result), cache.directories(self.source))
        data = self.root / "cache/objects" / receipt["content_sha256"] / "data"
        (data / "msas/paired").chmod(0o700)
        (data / "msas/paired/empty").rmdir()
        with self.assertRaisesRegex(cache.CacheError, "checksum/inventory"):
            self.cache.lookup(identity())

    def test_v1_cache_namespace_is_retained_but_never_reused(self):
        old_key = cache.digest(identity())
        legacy = self.root / "cache/entries" / (old_key + ".json")
        legacy.write_text('{"schema":1,"historical":"file-only receipt"}\n')
        original = legacy.read_bytes()
        self.assertIsNone(self.cache.lookup(identity()))
        receipt = self.cache.publish(self.source, identity())
        self.assertEqual(receipt["schema"], 2)
        self.assertNotEqual(receipt["key"], old_key)
        self.assertEqual(legacy.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
