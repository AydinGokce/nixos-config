"""Transport and local-backup tests using actual registry archives and records."""
from contextlib import redirect_stdout
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[3]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


client_module = module("test_bio_library_client", REPO / "modules/bio/py/library_client.py")
registry = module("test_bio_library_registry", REPO / "machines/head/library/registry.py")


class LocalTransport:
    """Interpret the SSH argv without a shell; scp copies into an isolated head."""
    def __init__(self, folder):
        self.folder = folder
        self.remote = folder / "remote-tmp"
        self.remote.mkdir()
        self.root = folder / "head-library"
        self.library = registry.Registry(self.root)
        self.library.init()
        self.calls = []
        self.fail_copy_to = False
        self.before_copy_from = None
        self.stdout_override = None

    def translate(self, text):
        if "=/tmp/bio-library-client-" in text:
            key, value = text.split("=", 1)
            return key + "=" + self.translate(value)
        if text.startswith("/tmp/bio-library-client-"):
            return str(self.remote / text.removeprefix("/tmp/"))
        return text

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == "scp":
            source, destination = argv[-2:]
            if source.startswith("root@example.invalid:"):
                if self.before_copy_from:
                    self.before_copy_from()
                shutil.copyfile(self.translate(source.split(":", 1)[1]), destination)
            else:
                if self.fail_copy_to:
                    raise subprocess.CalledProcessError(1, argv)
                shutil.copyfile(source, self.translate(destination.split(":", 1)[1]))
            return subprocess.CompletedProcess(argv, 0)
        if argv[0] != "ssh":
            raise AssertionError(f"Unexpected executable: {argv}")
        arguments = shlex.split(argv[-1])
        if arguments[0] == "env":
            assert arguments[1].startswith("BIO_LIBRARY_ROOT=")
            arguments = arguments[2:]
        translated = [self.translate(x) for x in arguments]
        stdout = ""
        if arguments[0] == "mkdir":
            Path(translated[-1]).mkdir(mode=0o700)
        elif arguments[0] == "rm":
            shutil.rmtree(translated[-1])
        elif arguments[0] == "bio-library":
            if self.stdout_override is not None:
                stdout = self.stdout_override
            else:
                stream = io.StringIO()
                with redirect_stdout(stream):
                    registry.main(["--root", str(self.root), *translated[1:]])
                stdout = stream.getvalue()
        else:
            raise AssertionError(f"Unexpected remote operation: {arguments}")
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.key = self.base / "key with spaces"
        self.key.write_text("test fixture; never an actual credential")
        self.env = patch.dict(os.environ, {
            "BIO_CLUSTER_HEAD": "example.invalid", "BIO_CLUSTER_USER": "root",
            "BIO_CLUSTER_SSHKEY": str(self.key),
            "BIO_LIBRARY_REGISTRY": str(REPO / "machines/head/library/registry.py"),
            "BIO_LIBRARY_REMOTE_ROOT": "",
        })
        self.env.start()
        self.transport = LocalTransport(self.base)
        self.client = client_module.Client(runner=self.transport)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def create_protein(self):
        return self.transport.library.import_record({
            "kind": "construct", "id": "enzyme", "aliases": ["target"],
            "identity": {"molecule_type": "protein", "sequence": "ACDEFGHIK"}})

    def quiet_forward(self, arguments):
        with redirect_stdout(io.StringIO()):
            return self.client.forward(arguments)

    def test_ssh_preserves_metacharacters_as_literal_arguments(self):
        captured = []
        self.client.runner = lambda args, **kwargs: captured.append((args, kwargs))
        expected = ["bio-library", "show", "x' ; $(touch /tmp/unsafe) `echo unsafe`"]
        self.client.ssh(expected)
        argv, options = captured[0]
        self.assertEqual(shlex.split(argv[-1]), expected)
        self.assertEqual(argv[-2], "root@example.invalid")
        self.assertEqual(argv[argv.index("-i") + 1], str(self.key))
        self.assertTrue(options["check"])
        self.assertNotIn("shell", options)

    def test_import_stages_local_files_and_cleans_remote_directory(self):
        source = self.base / "a sequence ' with spaces.fa"
        source.write_bytes(b">example\r\nACDEFGHIK\r\n")
        notes = self.base / "notes with spaces.txt"
        notes.write_text("preserve exactly: $() `nothing executes`\n")
        self.quiet_forward(["import", "--fasta", str(source), "--type", "protein", "--id", "enzyme",
                            "--notes", "literal ' ; $()", "--attachment", "notes.txt=" + str(notes)])
        record = self.transport.library.show("enzyme")
        self.assertEqual(record["notes"], "literal ' ; $()")
        self.assertEqual(record["provenance"]["original_filename"], source.name)
        self.assertEqual(self.transport.library.attachment_path("enzyme", "attachments/source.fasta").read_bytes(), source.read_bytes())
        self.assertEqual(self.transport.library.attachment_path("enzyme", "attachments/notes.txt").read_bytes(), notes.read_bytes())
        self.assertEqual(list(self.transport.remote.iterdir()), [])
        copies = [argv for argv, _ in self.transport.calls if argv[0] == "scp"]
        self.assertEqual(len(copies), 2)

    def test_upload_failure_cleans_only_created_temporary_directory(self):
        source = self.base / "input.fa"
        source.write_text(">enzyme\nACD\n")
        self.transport.fail_copy_to = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.quiet_forward(["import", "--fasta", str(source), "--type", "protein", "--id", "enzyme"])
        self.assertEqual(list(self.transport.remote.iterdir()), [])
        self.assertEqual(self.transport.library.list(), [])

    def test_missing_input_is_rejected_before_remote_staging(self):
        with self.assertRaises(OSError):
            self.quiet_forward(["import", "--json", str(self.base / "missing.json")])
        self.assertEqual(self.transport.calls, [])

    def test_cleanup_refuses_embedded_traversal_or_invalid_uuid(self):
        for path in ["/tmp/unowned", "/tmp/bio-library-client-../../other-" + "a" * 32,
                     "/tmp/bio-library-client-" + "z" * 32,
                     "/tmp/bio-library-client-" + "a" * 32 + "/nested"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.client.remove_temporary(path)
        self.assertEqual(self.transport.calls, [])

    def test_snapshot_verifies_hash_and_creates_requested_local_json(self):
        self.create_protein()
        output = self.base / "results with spaces" / "frozen.json"
        self.quiet_forward(["snapshot", "target", "--out", str(output)])
        actual = json.loads(output.read_text())
        self.assertEqual(actual, self.transport.library.snapshot("target"))
        command = shlex.split(self.transport.calls[-1][0][-1])
        self.assertNotIn("--out", command)
        registry.verify_document(actual)

    def test_invalid_snapshot_receipt_does_not_publish_a_file(self):
        self.transport.stdout_override = json.dumps({"schema": 1, "sha256": "invalid"})
        output = self.base / "must-not-exist.json"
        with self.assertRaises(ValueError):
            self.quiet_forward(["snapshot", "target", "--out", str(output)])
        self.assertFalse(output.exists())

    def test_snapshot_refuses_existing_output_or_symlink_parent(self):
        self.create_protein()
        output = self.base / "existing.json"
        output.write_text("owned by user")
        with self.assertRaises(ValueError):
            self.quiet_forward(["snapshot", "target", "--out", str(output)])
        self.assertEqual(output.read_text(), "owned by user")
        real = self.base / "real"
        real.mkdir()
        link = self.base / "parent-link"
        link.symlink_to(real)
        with self.assertRaises(ValueError):
            self.quiet_forward(["snapshot", "target", "--out", str(link / "unsafe.json")])
        self.assertFalse((real / "unsafe.json").exists())

    def test_export_verifies_real_archive_and_removes_remote_staging(self):
        self.create_protein()
        output = self.base / "backups" / "export.tar.gz"
        result = self.client.export(output)
        self.assertEqual(result["path"], str(output))
        self.assertEqual(result["sha256"], registry.file_digest(output))
        self.assertEqual(result["origin"], {"head": "example.invalid", "library_root": "/var/lib/bio-library"})
        self.assertEqual(registry.verify_backup(output)["records"], 1)
        self.assertEqual(list(self.transport.remote.iterdir()), [])
        self.assertFalse(list(output.parent.glob("*.part")))

    def test_export_does_not_overwrite_existing_or_concurrently_created_file(self):
        self.create_protein()
        output = self.base / "existing.tar.gz"
        output.write_bytes(b"preexisting bytes")
        with self.assertRaises(ValueError):
            self.client.export(output)
        self.assertEqual(output.read_bytes(), b"preexisting bytes")
        output.unlink()
        self.transport.before_copy_from = lambda: output.write_bytes(b"created while transfer was in progress")
        with self.assertRaises((ValueError, FileExistsError)):
            self.client.export(output)
        self.assertEqual(output.read_bytes(), b"created while transfer was in progress")
        self.assertEqual(list(self.transport.remote.iterdir()), [])

    def test_corrupt_export_is_not_published_and_remote_staging_is_cleaned(self):
        self.create_protein()
        output = self.base / "corrupt.tar.gz"
        module = client_module.registry_module()
        with patch.object(client_module, "registry_module", return_value=module):
            with patch.object(module, "verify_backup", side_effect=ValueError("corrupt transfer")):
                with self.assertRaises(ValueError):
                    self.client.export(output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.transport.remote.iterdir()), [])
        self.assertFalse(list(self.base.glob(".*.part")))

    def test_backup_restores_real_monomer_closure_before_marking_latest(self):
        self.transport.library.import_record({"kind": "monomer", "id": "custom",
                                               "identity": {"description": "Unknown incorporated residue"}})
        self.transport.library.import_record({"kind": "construct", "id": "oligo",
            "identity": {"molecule_type": "rna", "sequence": "ACGU",
                         "modifications": [{"position": 2, "monomer_ref": "custom"}]}})
        destination = self.base / "backups"
        when = dt.datetime(2026, 9, 6, 12, tzinfo=dt.timezone.utc)
        result = client_module.backup(destination, client=self.client, now=when)
        self.assertTrue(result["restore_checked"])
        self.assertEqual(json.loads((destination / "latest.json").read_text()), result)
        self.assertEqual(registry.verify_backup(destination / result["filename"])["records"], 2)
        restored = self.base / "manual-restore"
        registry.restore_backup(destination / result["filename"], restored)
        self.assertEqual(registry.Registry(restored).snapshot("oligo"), self.transport.library.snapshot("oligo"))
        self.assertEqual(list(destination.glob(".restore-check-*")), [])
        self.assertEqual(json.loads((destination / "origin.json").read_text())["origin"], self.client.origin)

    def test_custom_library_origin_is_normalized_and_explicit_in_remote_command(self):
        with patch.dict(os.environ, {"BIO_CLUSTER_HEAD": "EXAMPLE.invalid", "BIO_LIBRARY_REMOTE_ROOT": "/var/lib/project//"}):
            client = client_module.Client(runner=self.transport)
        self.assertEqual(client.origin, {"head": "example.invalid", "library_root": "/var/lib/project"})
        self.assertEqual(client.library_command, ["env", "BIO_LIBRARY_ROOT=/var/lib/project", "bio-library"])

    def test_reusing_backup_destination_for_another_head_or_root_fails_before_transfer(self):
        self.create_protein()
        destination = self.base / "backups"
        first = client_module.backup(destination, client=self.client)
        archive = (destination / first["filename"]).read_bytes()
        latest = (destination / "latest.json").read_bytes()
        for changed in ({"BIO_CLUSTER_HEAD": "another.invalid"}, {"BIO_LIBRARY_REMOTE_ROOT": "/var/lib/other-library"}):
            with self.subTest(changed=changed), patch.dict(os.environ, changed):
                other = client_module.Client(runner=self.transport)
                self.transport.calls.clear()
                with self.assertRaisesRegex(ValueError, "different head/library root; select a separate"):
                    client_module.backup(destination, client=other)
                self.assertEqual(self.transport.calls, [])
        self.assertEqual((destination / first["filename"]).read_bytes(), archive)
        self.assertEqual((destination / "latest.json").read_bytes(), latest)

    def test_direct_export_cannot_enter_a_destination_bound_to_another_origin(self):
        self.create_protein()
        destination = self.base / "backups"
        client_module.backup(destination, client=self.client)
        with patch.dict(os.environ, {"BIO_LIBRARY_REMOTE_ROOT": "/var/lib/other-library"}):
            other = client_module.Client(runner=self.transport)
        self.transport.calls.clear()
        with self.assertRaisesRegex(ValueError, "select a separate backup destination"):
            other.export(destination / "manual.tar.gz")
        self.assertEqual(self.transport.calls, [])
        self.assertFalse((destination / "manual.tar.gz").exists())

    def test_receipt_origin_protects_a_directory_even_without_its_marker(self):
        self.create_protein()
        destination = self.base / "backups"
        client_module.backup(destination, client=self.client)
        (destination / "origin.json").unlink()
        with patch.dict(os.environ, {"BIO_LIBRARY_REMOTE_ROOT": "/var/lib/other-library"}):
            other = client_module.Client(runner=self.transport)
        self.transport.calls.clear()
        with self.assertRaisesRegex(ValueError, "different head/library root"):
            client_module.backup(destination, client=other)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse((destination / "origin.json").exists())

    def test_originless_history_requires_explicit_migration_and_preserves_archive_bytes(self):
        self.create_protein()
        destination = self.base / "backups"
        first = client_module.backup(destination, client=self.client)
        archive = (destination / first["filename"]).read_bytes()
        legacy = dict(first)
        legacy.pop("origin")
        receipt_path = destination / (first["filename"] + ".json")
        for path in (receipt_path, destination / "latest.json"):
            path.write_text(json.dumps(legacy))
        (destination / "origin.json").unlink()
        self.transport.calls.clear()
        with self.assertRaisesRegex(ValueError, "no recorded origin"):
            client_module.backup(destination, client=self.client)
        self.assertEqual(self.transport.calls, [])
        # An operator can restore a known origin to the receipt without touching
        # the archive; unknown origins are never inferred from molecular data.
        receipt_path.write_text(json.dumps(dict(legacy, origin=self.client.origin)))
        result = client_module.backup(destination, client=self.client,
                                      now=dt.datetime.fromisoformat(first["created_at"]) + dt.timedelta(hours=1))
        self.assertEqual(result["origin"], self.client.origin)
        self.assertEqual((destination / first["filename"]).read_bytes(), archive)

    def test_originless_archive_in_a_bound_directory_is_excluded_from_retention(self):
        self.create_protein()
        destination = self.base / "backups"
        first = client_module.backup(destination, client=self.client)
        receipt_path = destination / (first["filename"] + ".json")
        legacy = dict(first)
        legacy.pop("origin")
        receipt_path.write_text(json.dumps(legacy))
        seen = []
        def keep(receipts, now):
            seen.extend(receipts)
            return {item["filename"] for item in receipts}
        with patch.object(client_module, "retained_receipts", side_effect=keep):
            client_module.backup(destination, client=self.client)
        self.assertNotIn(first["filename"], [item["filename"] for item in seen])
        self.assertTrue((destination / first["filename"]).exists())
        self.assertTrue(receipt_path.exists())

    def test_export_origin_drift_does_not_advance_latest_or_prune(self):
        self.create_protein()
        destination = self.base / "backups"
        first = client_module.backup(destination, client=self.client)
        previous = (destination / "latest.json").read_bytes()
        export = self.client.export
        def changed(output):
            result = export(output)
            result['origin'] = {"head": "unexpected.invalid", "library_root": "/var/lib/bio-library"}
            return result
        self.client.export = changed
        with self.assertRaisesRegex(ValueError, "origin changed during backup"):
            client_module.backup(destination, client=self.client)
        self.assertEqual((destination / "latest.json").read_bytes(), previous)
        self.assertTrue((destination / first["filename"]).exists())

    def test_injected_archive_only_client_still_supports_isolated_restore_tests(self):
        self.create_protein()
        archive = self.base / "fixture-export.tar.gz"
        self.transport.library.export_snapshot(archive)
        class ArchiveClient:
            def export(self, output):
                shutil.copyfile(archive, output)
                return {"path": str(output), "sha256": registry.file_digest(output), "bytes": output.stat().st_size}
        result = client_module.backup(self.base / "isolated-test-backups", client=ArchiveClient())
        self.assertTrue(result["restore_checked"])
        self.assertNotIn("origin", result)
        self.assertEqual(self.transport.calls, [])

    def test_failed_new_backup_never_prunes_old_files(self):
        destination = self.base / "backups"
        destination.mkdir()
        sentinel = destination / "old-owned.tar.gz"
        sentinel.write_bytes(b"existing backup")
        previous_latest = destination / "latest.json"
        previous_latest.write_text('{"previous":"good"}')
        failing = client_module.Client(runner=self.transport)
        failing.export = lambda _: (_ for _ in ()).throw(RuntimeError("simulated download failure"))
        with self.assertRaises(RuntimeError):
            client_module.backup(destination, client=failing)
        self.assertEqual(sentinel.read_bytes(), b"existing backup")
        self.assertEqual(previous_latest.read_text(), '{"previous":"good"}')

    def test_failed_restore_never_marks_latest_or_prunes(self):
        self.create_protein()
        destination = self.base / "backups"
        destination.mkdir()
        sentinel = destination / "user-file.tar.gz"
        sentinel.write_bytes(b"untouched")
        module = client_module.registry_module()
        with patch.object(client_module, "registry_module", return_value=module):
            with patch.object(module, "restore_backup", side_effect=ValueError("broken reference closure")):
                with self.assertRaises(ValueError):
                    client_module.backup(destination, client=self.client)
        self.assertFalse((destination / "latest.json").exists())
        self.assertEqual(sentinel.read_bytes(), b"untouched")
        self.assertEqual(list(destination.glob(".restore-check-*")), [])

    def test_retention_keeps_latest_bucket_members_and_rejects_bad_timestamps(self):
        now = dt.datetime(2026, 9, 6, 12, tzinfo=dt.timezone.utc)
        receipts = []
        for index in range(100):
            when = now - dt.timedelta(hours=index)
            receipts.append({"filename": f"hour-{index}", "created_at": when.isoformat()})
        # More recent receipt in the same hour takes that bucket.
        receipts.append({"filename": "latest", "created_at": (now + dt.timedelta(seconds=1)).isoformat()})
        retained = client_module.retained_receipts(receipts, now)
        self.assertIn("latest", retained)
        self.assertNotIn("hour-0", retained)
        self.assertTrue({f"hour-{i}" for i in range(1, 24)} <= retained)
        self.assertLess(len(retained), len(receipts))
        for timestamp in ["2026-09-06T12:00:00", (now + dt.timedelta(days=1)).isoformat()]:
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                client_module.retained_receipts([{"filename": "bad", "created_at": timestamp}], now)

    def test_retention_prunes_only_owned_checked_archives(self):
        self.create_protein()
        destination = self.base / "backups"
        destination.mkdir()
        now = dt.datetime(2026, 9, 6, 12, tzinfo=dt.timezone.utc)
        fixture = self.base / "fixture.tar.gz"
        self.transport.library.export_snapshot(fixture)
        # 36 monthly snapshots exceed the combined 30 distinct day buckets.
        old_names = []
        for index in range(36):
            year = 2026 - index // 12
            month = 12 - index % 12
            # All timestamps precede now.
            when = dt.datetime(year - 1, month, 1, tzinfo=dt.timezone.utc)
            name = f"library-fixture-{index:02}.tar.gz"
            old_names.append(name)
            shutil.copyfile(fixture, destination / name)
            receipt = {"schema": 1, "kind": "bio-library-local-backup", "filename": name,
                       "created_at": when.isoformat(), "restore_checked": True,
                       "origin": self.client.origin,
                       "sha256": registry.file_digest(fixture), "bytes": fixture.stat().st_size}
            (destination / (name + ".json")).write_text(json.dumps(receipt))
        unowned = destination / "unrelated-backup.tar.gz"
        unowned.write_bytes(b"user data")
        result = client_module.backup(destination, client=self.client, now=now)
        self.assertTrue((destination / result["filename"]).exists())
        self.assertEqual(unowned.read_bytes(), b"user data")
        self.assertTrue(any(not (destination / name).exists() for name in old_names))
        for name in old_names:
            self.assertEqual((destination / name).exists(), (destination / (name + ".json")).exists())


if __name__ == "__main__":
    unittest.main()
