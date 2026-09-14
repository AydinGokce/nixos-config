"""Offline cache integrity/resume tests; tiny fixtures do not qualify real MSA."""
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import block_cache as cache
import databases
from test_databases import fake_database


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / "source"
        self.target = self.base / "cache"
        self.source.mkdir()
        self.target.mkdir()
        production_component = databases.validate_component

        def mini_component(path, name):
            if name != "mmcif":
                return production_component(path, name)
            sizes = {}
            for folder in ("divided", "obsolete"):
                paths = list((path / folder).glob("*/*.cif.gz"))
                if not paths:
                    raise RuntimeError("Incomplete tiny coordinate fixture")
                for member in paths:
                    if member.is_symlink() or member.stat().st_size == 0:
                        raise RuntimeError("Invalid coordinate fixture")
                    sizes[str(member.relative_to(path))] = member.stat().st_size
            return dict(files=len(sizes), bytes=sum(sizes.values()),
                        path_size_sha256=cache.digest(sizes),
                        content_manifest_sha256=databases.digest(path / "mmcif-content.jsonl.gz")[0])

        self.addCleanup(patch.stopall)
        patch.object(databases, "validate_component", side_effect=mini_component).start()
        patch.object(cache, "source_mount", return_value={"source": "read-only synthetic fixture"}).start()
        patch.object(cache, "cache_mount", side_effect=lambda p, b, d, readonly: dict(
            filesystem_uuid=b["filesystem_uuid"], volume_id=b["volume_id"],
            filesystem_type="ext4", size_bytes=cache.SIZE_BYTES, readonly=readonly)).start()

        for component, prefix in databases.PREFIXES.items():
            base = self.source / component / prefix
            fake_database(base, expanded=component != "pdb100")
            if component != "pdb100":
                for suffix in ("", ".index", ".dbtype"):
                    alias = Path(str(base) + "_h" + suffix)
                    alias.unlink()
                    alias.symlink_to(prefix + "_seq_h" + suffix)
            if component == "uniref30":
                for suffix in ("_mapping", "_taxonomy"):
                    Path(str(base) + suffix).write_bytes(b"taxonomy fixture\n")
                    Path(str(base) + ".idx" + suffix).symlink_to(prefix + suffix)
        self.large_name = "environmental/" + databases.PREFIXES["environmental"] + ".idx"
        self.large = self.source / self.large_name
        self.large.write_bytes(b"header\0" + bytes(range(256))*1600 + b"footer\0")
        os.chmod(self.large, 0o640)
        os.utime(self.large, ns=(1_700_000_000_123456789, 1_700_000_000_123456789))
        templates = self.source / "templates"
        templates.mkdir()
        (templates / "pdb100_a3m.ffdata").write_bytes(b"ABC\0")
        (templates / "pdb100_a3m.ffindex").write_bytes(b"1abc_A\t0\t4\n")
        mmcif = self.source / "mmcif"
        lines = []
        for folder, code in (("divided", "1abc"), ("obsolete", "2abc")):
            path = mmcif / folder / "ab" / (code + ".cif.gz")
            path.parent.mkdir(parents=True)
            content = b"data_" + code.encode() + b"\n"
            path.write_bytes(gzip.compress(content, mtime=0))
            lines.append(cache.canonical(dict(path=str(path.relative_to(mmcif)),
                compressed_bytes=path.stat().st_size, mmcif_sha256=hashlib.sha256(content).hexdigest())) + b"\n")
        (mmcif / "mmcif-content.jsonl.gz").write_bytes(gzip.compress(b"".join(lines), mtime=0))
        components = {}
        for name in databases.COMPONENTS:
            receipt = dict(component=name, manifest_sha256=databases.MANIFEST_SHA256,
                           files=databases.validate_component(self.source / name, name))
            databases.write_json(self.source / name / ".component.json", receipt)
            databases.write_json(self.source / ".components" / (name + ".json"), receipt)
            components[name] = cache.digest(receipt)
        databases.write_json(self.source / "manifest.json", databases.MANIFEST)
        databases.write_json(self.source / ".msa-databases.json", dict(
            manifest_sha256=databases.MANIFEST_SHA256, components=components))
        for folder in (".archives", ".staging", ".conversions"):
            (self.source / folder).mkdir()
            (self.source / folder / "not-published").write_bytes(b"NEVER COPY")
        (self.source / "uniref30/.indexed.json").write_bytes(b"build marker")
        gen = cache.generation(self.source)
        generation = dict(schema=1, cache_id="a"*32,
                          source_manifest_sha256=gen["source_manifest_sha256"],
                          source_receipt_sha256=gen["source_receipt_sha256"])
        self.owner = dict(generation, volume_id="11111111-1111-4111-8111-111111111111",
            filesystem_uuid="22222222-2222-4222-8222-222222222222", cache_generation=cache.digest(generation),
            ready_receipt_sha256=None)

    def populate(self, **kwargs):
        return cache.copy(self.source, self.target, self.owner, "/dev/test", workers=kwargs.pop("workers", 2), **kwargs)

    def served(self, result):
        return dict(self.owner, ready_receipt_sha256=result["ready_receipt_sha256"])

    def test_published_inventory_excludes_builds_and_preserves_eight_aliases(self):
        plan = cache.inspect(self.source)
        names = {row["path"] for row in plan["entries"]}
        self.assertIn("mmcif/mmcif-content.jsonl.gz", names)
        self.assertNotIn(".archives/not-published", names)
        self.assertNotIn("uniref30/.indexed.json", names)
        self.assertEqual(cache.summary(plan)["symlinks"], 8)
        self.assertEqual(set(plan["source"]["receipts"]), set(cache.GENERATION_FILES))

    def test_roundtrip_is_exact_preserves_metadata_and_passes_normal_validator(self):
        result = self.populate()
        ready = result["ready"]
        self.assertEqual(ready["rootrel"], "colabfold")
        self.assertEqual(ready["source"]["manifest_sha256"], self.owner["source_manifest_sha256"])
        self.assertEqual(ready["source"]["receipt_sha256"], self.owner["source_receipt_sha256"])
        destination = self.target / "colabfold" / self.large_name
        self.assertEqual(destination.read_bytes(), self.large.read_bytes())
        for field in ("mode", "mtime_ns", "uid", "gid"):
            self.assertEqual(cache.metadata(destination)[field], cache.metadata(self.large)[field])
        self.assertEqual(ready["completion"]["symlinks"], 8)
        self.assertTrue(ready["completion"]["full_readback"])
        self.assertEqual(ready["completion"]["source_bytes_hashed"], ready["completion"]["payload_bytes"])
        verified = cache.verify(self.target, self.served(result), "/dev/test")
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["verification"], "pinned-receipt-metadata-edges")
        self.assertEqual(databases.validate(self.target / "colabfold")["components"],
                         databases.validate(self.source)["components"])
        again = self.populate()
        self.assertTrue(again["already_ready"])
        self.assertEqual(again["ready_receipt_sha256"], result["ready_receipt_sha256"])

    def test_interruption_reuses_completed_bytes_without_rewriting_large_file(self):
        actual_copy = cache.copy_file
        calls = 0

        def stop_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected interruption")
            return actual_copy(*args)

        with patch.object(cache, "copy_file", side_effect=stop_second):
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                self.populate(workers=1)
        self.assertFalse((self.target / "ready.json").exists())
        destination = self.target / cache.PENDING / self.large_name
        original = cache.metadata(destination)
        result = self.populate(workers=1)
        self.assertEqual(cache.metadata(self.target / "colabfold" / self.large_name), original)
        self.assertGreaterEqual(result["progress"]["completed_reused_bytes"], self.large.stat().st_size)
        self.assertLess(result["progress"]["destination_bytes_written"], result["ready"]["completion"]["payload_bytes"])
        cache.verify(self.target, self.served(result), "/dev/test", full=True)

    def test_owned_partial_prefix_is_checked_then_resumed(self):
        with patch.object(cache.Progress, "add", autospec=True) as progress:
            # Interrupt from the source read accounting after the first write.
            def fail(self, **kwargs):
                if kwargs.get("read"):
                    raise RuntimeError("interrupted after write")
                return {}
            progress.side_effect = fail
            with self.assertRaisesRegex(RuntimeError, "after write"):
                self.populate(workers=1)
        partials = list((self.target / cache.STATE / "partial").glob("*.part"))
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0].read_bytes(), self.large.read_bytes())
        result = self.populate(workers=1)
        self.assertLess(result["progress"]["destination_bytes_written"], result["ready"]["completion"]["payload_bytes"])
        cache.verify(self.target, self.served(result), "/dev/test")

    def test_corrupted_partial_is_retained_and_never_published(self):
        actual = cache.Progress.add
        def fail(self, **kwargs):
            if kwargs.get("read"):
                raise RuntimeError("stop")
            return actual(self, **kwargs)
        with patch.object(cache.Progress, "add", fail):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                self.populate(workers=1)
        partial = next((self.target / cache.STATE / "partial").glob("*.part"))
        with partial.open("r+b") as handle:
            handle.write(b"BAD")
        with self.assertRaisesRegex(RuntimeError, "Partial prefix differs"):
            self.populate(workers=1)
        self.assertTrue(partial.exists())
        self.assertFalse((self.target / "ready.json").exists())

    def test_readback_failure_never_marks_a_file_complete(self):
        with patch.object(cache, "hash_handle", return_value="f"*64):
            with self.assertRaisesRegex(RuntimeError, "readback differs"):
                self.populate(workers=1)
        self.assertFalse((self.target / "ready.json").exists())
        self.assertFalse(list((self.target / cache.STATE / "completed").glob("*/*.json")))

    def test_changed_source_during_copy_is_rejected(self):
        actual = cache.Progress.add
        changed = False
        def mutate(progress, **kwargs):
            nonlocal changed
            if kwargs.get("read") and not changed:
                changed = True
                with self.large.open("r+b") as handle:
                    handle.seek(cache.EDGE+32)
                    handle.write(b"CHANGED")
            return actual(progress, **kwargs)
        with patch.object(cache.Progress, "add", mutate):
            with self.assertRaisesRegex(RuntimeError, "Source changed"):
                self.populate(workers=1)
        self.assertFalse((self.target / "ready.json").exists())

    def test_wrong_source_generation_rejected_before_cache_state_is_created(self):
        receipt = self.source / ".msa-databases.json"
        value = json.loads(receipt.read_text())
        value["installed_utc"] = 123
        databases.write_json(receipt, value)
        with self.assertRaisesRegex(RuntimeError, "Source generation differs"):
            self.populate()
        self.assertEqual(list(self.target.iterdir()), [])

    def test_unowned_cache_and_escaping_source_alias_are_rejected(self):
        (self.target / "somebody-elses-data").write_bytes(b"keep")
        with self.assertRaisesRegex(RuntimeError, "not new or owned"):
            self.populate()
        self.assertEqual((self.target / "somebody-elses-data").read_bytes(), b"keep")
        link = self.source / "uniref30" / (databases.PREFIXES["uniref30"] + "_h")
        link.unlink()
        link.symlink_to(self.large)
        with self.assertRaisesRegex(RuntimeError, "escapes|relative|changed|validation"):
            cache.inspect(self.source)

    def test_ready_and_content_manifest_tampering_fail_external_sha_checks(self):
        result = self.populate()
        path = self.target / "ready.json"
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with self.assertRaisesRegex(RuntimeError, "Ready receipt SHA changed"):
            cache.verify(self.target, self.served(result), "/dev/test")
        path.write_bytes(original)
        manifest = self.target / cache.STATE / "content.jsonl"
        data = manifest.read_bytes()
        manifest.write_bytes(data.replace(b'"file"', b'"fyle"', 1))
        with self.assertRaisesRegex(RuntimeError, "content manifest SHA changed"):
            cache.verify(self.target, self.served(result), "/dev/test")

    def test_cached_file_edit_is_rejected(self):
        result = self.populate()
        destination = self.target / "colabfold" / self.large_name
        with destination.open("r+b") as handle:
            handle.seek(cache.EDGE+64)
            handle.write(b"edited")
        with self.assertRaisesRegex(RuntimeError, "metadata changed"):
            cache.verify(self.target, self.served(result), "/dev/test")

    def test_cached_alias_edit_is_rejected(self):
        result = self.populate()
        alias = self.target / "colabfold/uniref30" / (databases.PREFIXES["uniref30"] + "_h")
        alias.unlink()
        alias.symlink_to(self.large)
        with self.assertRaisesRegex(RuntimeError, "alias changed|metadata changed"):
            cache.verify(self.target, self.served(result), "/dev/test")

    def test_extra_cached_file_is_rejected(self):
        result = self.populate()
        (self.target / "colabfold/uniref30/unexpected").write_bytes(b"not a published entry")
        with self.assertRaisesRegex(RuntimeError, "metadata changed|extra"):
            cache.verify(self.target, self.served(result), "/dev/test")

    def test_symlinked_metadata_or_staging_parent_is_rejected_without_external_write(self):
        external = self.base / "external"
        external.mkdir()
        (self.target / cache.STATE).symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "not real"):
            self.populate()
        self.assertEqual(list(external.iterdir()), [])
        (self.target / cache.STATE).unlink()
        (self.source / ".components").rename(self.base / "components")
        (self.source / ".components").symlink_to(self.base / "components", target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "parent is symlinked"):
            cache.inspect(self.source)

    def test_failed_thread_cancels_other_streams(self):
        progress = cache.Progress(100)
        progress.cancelled.set()
        from io import BytesIO
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            cache.hash_handle(BytesIO(b"unfinished"), progress)

    def test_copy_rejects_unpublished_hidden_staging_files_before_ready_publication(self):
        actual = cache.copy_file
        injected = False
        def inject(*args):
            nonlocal injected
            result = actual(*args)
            if not injected:
                injected = True
                (args[1] / ".unpublished-build-state").write_bytes(b"unexpected")
            return result
        with patch.object(cache, "copy_file", side_effect=inject):
            with self.assertRaisesRegex(RuntimeError, "unpublished entries"):
                self.populate(workers=1)
        self.assertFalse((self.target / "ready.json").exists())

    def test_nonprivate_state_rejected_before_creating_lock(self):
        state = self.target / cache.STATE
        state.mkdir(mode=0o777)
        os.chmod(state, 0o777)
        with self.assertRaisesRegex(RuntimeError, "privately owned"):
            self.populate()
        self.assertEqual(list(state.iterdir()), [])

    def test_full_verification_detects_middle_corruption_even_if_metadata_is_unchanged(self):
        result = self.populate()
        destination = self.target / "colabfold" / self.large_name
        old = cache.metadata(destination)
        with destination.open("r+b") as handle:
            handle.seek(cache.EDGE+64)
            handle.write(b"bitrot")
        original_metadata = cache.metadata
        def unchanged(path):
            return old if path == destination else original_metadata(path)
        with patch.object(cache, "metadata", side_effect=unchanged):
            # Model silent media corruption separately from edits detectable by
            # ctime. Fast checks intentionally are not a full-content audit.
            cache.verify(self.target, self.served(result), "/dev/test")
            with self.assertRaisesRegex(RuntimeError, "content SHA changed"):
                cache.verify(self.target, self.served(result), "/dev/test", full=True)

    def test_verify_needs_registered_ready_receipt_and_copy_cannot_use_serve_binding(self):
        with self.assertRaisesRegex(RuntimeError, "externally pinned"):
            cache.verify(self.target, self.owner, "/dev/test")
        result = self.populate()
        with self.assertRaisesRegex(RuntimeError, "unready"):
            cache.copy(self.source, self.target, self.served(result), "/dev/test")

    def test_completed_receipt_change_is_rejected_on_resume(self):
        actual = cache.copy_file
        count = 0
        def stop(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("stop")
            return actual(*args)
        with patch.object(cache, "copy_file", side_effect=stop):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                self.populate(workers=1)
        receipt = next((self.target / cache.STATE / "completed").glob("*/*.json"))
        obj = json.loads(receipt.read_text())
        obj["full_readback"] = False
        receipt.write_text(json.dumps(obj))
        with self.assertRaisesRegex(RuntimeError, "receipt changed"):
            self.populate(workers=1)

    def test_final_rename_interruption_can_publish_without_recopy(self):
        actual = cache.atomic_json
        def stop(path, value):
            if path.name == "ready.json":
                raise RuntimeError("publication interrupted")
            return actual(path, value)
        with patch.object(cache, "atomic_json", side_effect=stop):
            with self.assertRaisesRegex(RuntimeError, "publication interrupted"):
                self.populate(workers=1)
        self.assertTrue((self.target / "colabfold").is_dir())
        result = self.populate(workers=1)
        self.assertEqual(result["progress"]["destination_bytes_written"], 0)
        cache.verify(self.target, self.served(result), "/dev/test")

    def test_progress_has_finite_work_units_and_eta(self):
        progress = cache.Progress(1000)
        progress.started -= 10
        event = progress.add(read=100, verified=50, reused=200, files=2, force=True)
        self.assertEqual(event["work_total_bytes"], 2000)
        self.assertEqual(event["work_completed_bytes"], 550)
        self.assertTrue(0 < event["eta_seconds"][0] < event["eta_seconds"][1])
        self.assertTrue(all(math.isfinite(x) for x in event["eta_seconds"]))


class MountTests(unittest.TestCase):
    def test_source_requires_provider_nfs41_and_readonly_mount(self):
        valid = dict(filesystem_type="nfs4", options=["ro"], super_options=["rw", "vers=4.1"],
                     source="nfs.fin-02.datacrunch.io:/published-db")
        with patch.object(cache, "mount_record", return_value=valid):
            self.assertEqual(cache.source_mount(Path("/source")), valid)
        for change in (dict(options=["rw"]), dict(super_options=["vers=4.2"]),
                       dict(source="attacker.invalid:/db"), dict(filesystem_type="ext4")):
            with self.subTest(change=change), patch.object(cache, "mount_record", return_value=valid | change):
                with self.assertRaises(RuntimeError):
                    cache.source_mount(Path("/source"))

    def test_device_uuid_size_accessmode_and_mount_are_all_checked(self):
        root = Path("/cache")
        device = os.makedev(253, 17)
        owner = dict(volume_id="provider-volume", filesystem_uuid="fs-uuid")
        row = dict(mountpoint="/cache", filesystem_type="ext4", options=["ro"],
                   super_options=["ro", "norecovery"], major_minor="253:17")

        def path_stat(path, **kwargs):
            if str(path) == "/cache":
                return SimpleNamespace(st_dev=device)
            return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=device)

        with patch.object(Path, "stat", path_stat), patch.object(cache, "mount_record", return_value=row), \
                patch.object(os, "open", return_value=9), patch.object(os, "close"), \
                patch.object(fcntl_module := cache.fcntl, "ioctl", return_value=struct.pack("Q", cache.SIZE_BYTES)):
            value = cache.cache_mount(root, owner, "/dev/vdb", readonly=True)
            self.assertEqual(value["size_bytes"], cache.SIZE_BYTES)
            with self.assertRaisesRegex(RuntimeError, "access mode"):
                cache.cache_mount(root, owner, "/dev/vdb", readonly=False)
            with patch.object(fcntl_module, "ioctl", return_value=struct.pack("Q", 92*1024**3)):
                with self.assertRaisesRegex(RuntimeError, "1300 GiB"):
                    cache.cache_mount(root, owner, "/dev/vdb", readonly=True)
            with patch.object(cache, "mount_record", return_value=row | {"major_minor": "253:18"}):
                with self.assertRaisesRegex(RuntimeError, "expected provider"):
                    cache.cache_mount(root, owner, "/dev/vdb", readonly=True)
            with patch.object(cache, "mount_record", return_value=row | {"super_options": ["ro"]}):
                with self.assertRaisesRegex(RuntimeError, "journal replay"):
                    cache.cache_mount(root, owner, "/dev/vdb", readonly=True)


if __name__ == "__main__":
    unittest.main()
