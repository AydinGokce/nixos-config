"""Offline lifecycle tests: no network, provider mutations, signals or mounts."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("rfaa_storage", Path(__file__).with_name("storage.py"))
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)

DB = "01234567-89ab-4cde-8fab-0123456789ab"
WORKER = "11234567-89ab-4cde-8fab-0123456789ab"
BOOT = "21234567-89ab-4cde-8fab-0123456789ab"
SHARED = "b8b3b446-e464-44dd-9e01-6402489f8c5a"


class APIError(s.Error):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}")


class API:
    def __init__(self, clock, events):
        self.clock, self.events = clock, events
        self.row = dict(id=DB, name="bio-rfaa-db-20260906-test", type="NVMe_Shared", size=3300,
                        location="FIN-02", is_os_volume=False, contract="PAY_AS_YOU_GO", currency="usd",
                        created_at=s.stamp(clock() - 60), status="exported", instance_id=s.HEAD_ID,
                        instances=[dict(id=s.HEAD_ID)], tags=[dict(key="purpose", value="rfaa-databases")],
                        pseudo_path="/bio-rfaa-db-test", mount_command=
                        "sudo mount -t nfs -o nconnect=16 nfs.fin-02.datacrunch.io:/bio-rfaa-db-test /mnt/example")
        self.instances = [dict(id=s.HEAD_ID, volume_ids=[SHARED, DB])]
        self.deleted = False
        self.get404 = False
        self.delete_lands = True
        self.calls = []

    def inventory(self):
        current = [dict(id=SHARED, name="bio-shared")]
        trash = []
        if self.deleted:
            trash.append(self.row)
        else:
            current.append(self.row)
        return copy.deepcopy((self.instances, current, trash))

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path == "/volumes/" + DB:
            if self.get404:
                raise APIError(404)
            return copy.deepcopy(self.row)
        if method == "PUT" and path == "/volumes":
            self.events.append("detach")
            assert body == dict(id=DB, action="detach", instance_id=s.HEAD_ID)
            self.row.update(instance_id=None, instances=[])
            for instance in self.instances:
                if instance["id"] == s.HEAD_ID:
                    instance["volume_ids"] = [ident for ident in instance["volume_ids"] if ident != DB]
            return None
        if method == "DELETE" and path == "/volumes/" + DB:
            self.events.append("delete")
            if self.delete_lands:
                self.deleted = True
                self.row.update(status="deleted", deleted_at=s.stamp(self.clock()),
                                is_permanently_deleted=body["is_permanent"])
            return None
        raise AssertionError((method, path, body))


class Ops:
    def __init__(self, events, api, budget):
        self.events, self.api, self.budget = events, api, budget
        self.busy_mount = False
        self.copy_error = False

    def stop_installer(self):
        self.events.append("stop-installer")

    def stop_process(self, job):
        self.events.append("stop-process")

    def collect(self, job, instance, results_root):
        self.events.append("collect-job")
        if self.copy_error:
            raise s.Error("copy failed; original shared output retained")

    def collect_database_receipts(self, receipt, results_root):
        self.events.append("collect-receipts")

    def remove_worker(self, ident):
        assert ident == WORKER
        self.events.append("remove-worker")
        with self.budget.locked() as state:
            for job in state["jobs"].values():
                if job.get("id") == ident:
                    job["status"] = "closed"
            self.budget.save(state)
        self.api.instances = [row for row in self.api.instances if row["id"] != ident]
        self.api.row["instances"] = [row for row in self.api.row["instances"] if row["id"] != ident]

    def unmount(self, source):
        assert source == "nfs.fin-02.datacrunch.io:/bio-rfaa-db-test"
        self.events.append("unmount")
        if self.busy_mount:
            raise s.Error("busy mount")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.results = self.root / "results"
        self.results.mkdir()
        self.clock_value = 100000
        self.clock = lambda: self.clock_value
        self.events = []
        self.store = s.Store(self.root / "rfaa-storage.json", owner=os.getuid())
        self.budget = s.Store(self.root / "budget.json", owner=os.getuid())
        self.budget.save(dict(version=1, jobs={}))
        self.api = API(self.clock, self.events)
        self.ops = Ops(self.events, self.api, self.budget)
        self.controller = s.Controller(self.store, self.budget, self.api, operations=self.ops,
                                      clock=self.clock, start=lambda pid: "123", boot=lambda: BOOT,
                                      results_root=self.results)
        self.controller.register(DB, self.api.row["name"], s.stamp(self.clock() + 3600))

    def receipt(self):
        return self.store.read()

    def due(self):
        self.clock_value += 3601

    def no_api_mutations(self):
        self.assertFalse(any(method != "GET" for method, _, _ in self.api.calls))

    def tracked_worker(self):
        path = self.results / "rfaa-test-job"
        path.mkdir()
        self.controller.track(DB, path, 424242, WORKER)
        with self.budget.locked() as state:
            state["jobs"]["token"] = dict(id=WORKER, status="running", volumes=[SHARED, DB])
            self.budget.save(state)
        self.api.instances.append(dict(id=WORKER, ip="192.0.2.1", volume_ids=[SHARED, DB]))
        self.api.row["instances"].append(dict(id=WORKER))
        return path

    def test_register_is_read_only_and_records_exact_identity_expiry_and_private_permissions(self):
        self.no_api_mutations()
        receipt = self.receipt()
        self.assertEqual(receipt["volume_id"], DB)
        self.assertEqual(receipt["created_at"], self.api.row["created_at"])
        self.assertEqual(s.utc(receipt["expires_at"]), self.clock() + 3600)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(s.Error, "already exists"):
            self.controller.register(DB, self.api.row["name"], s.stamp(self.clock() + 3600))

    def test_check_and_track_fail_closed_for_expiry_retirement_and_wrong_volume(self):
        self.controller.check(DB)
        self.controller.track(DB, self.results / "rfaa-job", 424242)
        self.assertEqual(self.receipt()["jobs"]["rfaa-job"]["boot_id"], BOOT)
        with self.assertRaises(s.Error):
            self.controller.check(WORKER)
        self.due()
        for action in (lambda: self.controller.check(DB),
                       lambda: self.controller.track(DB, self.results / "rfaa-next", 424242)):
            with self.assertRaisesRegex(s.Error, "expired"):
                action()
        with self.store.locked() as receipt:
            receipt.update(status="retiring", expires_at=s.stamp(self.clock() + 3600))
            self.store.save(receipt)
        with self.assertRaisesRegex(s.Error, "retiring"):
            self.controller.check(DB)
        self.no_api_mutations()

    def test_no_receipt_or_future_expiry_performs_no_cleanup(self):
        self.assertFalse(self.controller.expire())
        self.store.path.unlink()
        self.assertFalse(self.controller.expire())
        self.assertEqual(self.events, [])
        self.no_api_mutations()

    def test_wrong_owner_permissions_or_symlink_receipt_is_rejected(self):
        self.store.path.chmod(0o644)
        with self.assertRaisesRegex(s.Error, "owned by root"):
            self.controller.check(DB)
        self.store.path.chmod(0o600)
        other = self.root / "other.json"
        self.store.path.rename(other)
        self.store.path.symlink_to(other)
        with self.assertRaisesRegex(s.Error, "safely open"):
            self.controller.expire(now=True, volume=DB)
        self.no_api_mutations()

    def test_protected_uuid_and_provider_identity_mismatch_stop_before_any_cleanup(self):
        self.due()
        self.api.row["name"] = "some-other-data"
        with self.assertRaisesRegex(s.Error, "identity differs"):
            self.controller.expire()
        self.assertEqual(self.events, [])
        self.no_api_mutations()
        with self.store.locked() as receipt:
            receipt["volume_id"] = SHARED
            self.store.save(receipt)
        with self.assertRaisesRegex(s.Error, "protected"):
            self.controller.expire()
        self.assertEqual(self.events, [])

    def test_exact_deadline_unshares_then_confirms_only_the_registered_volume_deleted(self):
        self.due()
        with self.assertRaisesRegex(s.Error, "unshare requested"):
            self.controller.expire()
        self.assertEqual(self.receipt()["status"], "retiring")
        self.assertNotIn("delete", self.events)
        self.assertTrue(self.controller.expire())
        self.assertEqual(self.receipt()["status"], "complete")
        self.assertEqual([row["id"] for row in self.api.inventory()[1]], [SHARED])
        self.assertEqual([row["id"] for row in self.api.instances], [s.HEAD_ID])
        deletes = [(path, body) for method, path, body in self.api.calls if method == "DELETE"]
        self.assertEqual(deletes, [("/volumes/" + DB, {"is_permanent": False})])
        self.assertLess(self.events.index("collect-receipts"), self.events.index("unmount"))

    def test_early_retirement_requires_exact_uuid_and_uses_the_same_checks(self):
        for options in ({"now": True}, {"now": True, "volume": WORKER}):
            with self.assertRaises(s.Error):
                self.controller.expire(**options)
        self.assertEqual(self.receipt()["status"], "active")
        self.no_api_mutations()
        with self.assertRaisesRegex(s.Error, "unshare requested"):
            self.controller.expire(now=True, volume=DB)

    def test_stale_singular_head_id_does_not_repeat_confirmed_shared_detach(self):
        self.due()
        with self.assertRaisesRegex(s.Error, "unshare requested"):
            self.controller.expire()
        # Provider leaves the legacy singular field unchanged even though both
        # shared recipients and the head's inverse references confirm detach.
        self.api.row["instance_id"] = s.HEAD_ID
        self.assertEqual(self.api.row["instances"], [])
        self.assertNotIn(DB, self.api.instances[0]["volume_ids"])
        self.assertTrue(self.controller.expire())
        self.assertEqual(self.events.count("detach"), 1)
        self.assertEqual(self.events.count("delete"), 1)
        self.assertEqual(self.receipt()["status"], "complete")

    def test_inverse_live_volume_reference_blocks_even_if_shared_recipients_are_empty(self):
        self.api.row.update(instances=[], instance_id=None)
        self.api.instances[0]["volume_ids"] = [SHARED]
        self.api.instances.append(dict(id=WORKER, volume_ids=[DB]))
        self.due()
        with self.assertRaisesRegex(s.Error, "unrecognized live attachment"):
            self.controller.expire()
        self.no_api_mutations()
        self.assertNotIn("unmount", self.events)

    def test_missing_shared_attachment_or_live_volume_metadata_fails_closed(self):
        self.due()
        for field in ("shared", "inverse"):
            with self.subTest(field=field):
                self.api.row["instances"] = None if field == "shared" else []
                self.api.instances[0]["volume_ids"] = None if field == "inverse" else [SHARED]
                with self.assertRaisesRegex(s.Error, "Cannot verify"):
                    self.controller.expire()
        self.no_api_mutations()

    def test_collects_partial_outputs_before_signal_and_exact_managed_worker_cleanup(self):
        self.tracked_worker()
        self.due()
        with self.assertRaisesRegex(s.Error, "unshare requested"):
            self.controller.expire()
        self.assertLess(self.events.index("collect-job"), self.events.index("stop-process"))
        self.assertLess(self.events.index("collect-job"), self.events.index("remove-worker"))
        self.assertLess(self.events.index("remove-worker"), self.events.index("unmount"))
        self.assertEqual(self.budget.read()["jobs"]["token"]["status"], "closed")

    def test_completed_result_is_not_overwritten_from_shared_storage(self):
        path = self.tracked_worker()
        (path / "job.json").write_text(json.dumps(dict(exit_status=0)))
        self.due()
        with self.assertRaisesRegex(s.Error, "unshare requested"):
            self.controller.expire()
        self.assertNotIn("collect-job", self.events)
        self.assertIn("remove-worker", self.events)

    def test_copy_warnings_survive_detach_retry_and_confirmed_retirement(self):
        self.tracked_worker()
        self.ops.copy_error = True
        self.due()
        with self.assertRaisesRegex(s.Error, "unshare requested"):
            self.controller.expire()
        self.assertTrue(self.receipt()["copy_warnings"])
        self.ops.copy_error = False
        self.assertTrue(self.controller.expire())
        self.assertIn("copy failed", self.receipt()["copy_warnings"][0])

    def test_unresolved_reservation_blocks_volume_teardown_and_new_submissions(self):
        with self.budget.locked() as state:
            state["jobs"]["uncertain"] = dict(id=None, status="uncertain", volumes=[DB])
            self.budget.save(state)
        self.due()
        with self.assertRaisesRegex(s.Error, "unresolved managed"):
            self.controller.expire()
        self.no_api_mutations()
        with self.assertRaisesRegex(s.Error, "retiring"):
            self.controller.check(DB)

    def test_timer_and_manual_expiry_are_serialized_without_holding_receipt_lock(self):
        entered, release = threading.Event(), threading.Event()
        failures = []
        def pause():
            entered.set()
            if not release.wait(3):
                raise AssertionError("test cleanup not released")
        def expire():
            try:
                self.controller.expire()
            except s.Error as exc:
                failures.append(str(exc))
        self.due()
        with patch.object(self.ops, "stop_installer", side_effect=pause):
            thread = threading.Thread(target=expire)
            thread.start()
            self.assertTrue(entered.wait(3))
            self.assertFalse(self.controller.expire(now=True, volume=DB))
            with self.assertRaisesRegex(s.Error, "retiring"):
                self.controller.check(DB)
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIn("unshare requested", failures[0])
        self.assertEqual(self.events.count("detach"), 1)

    def test_retiring_waits_for_a_concurrent_launcher_to_commit_its_reservation(self):
        checked, retiring = threading.Event(), threading.Event()
        failures = []
        def launch():
            try:
                with self.budget.locked() as state:
                    self.controller.check(DB)
                    checked.set()
                    if not retiring.wait(3):
                        raise AssertionError("expiry did not mark retiring")
                    state["jobs"]["new"] = dict(id=None, status="pending", volumes=[DB])
                    self.budget.save(state)
            except Exception as exc:
                failures.append(exc)
        thread = threading.Thread(target=launch)
        thread.start()
        self.assertTrue(checked.wait(3))
        save = self.store.save
        def notify(value):
            save(value)
            if value["status"] == "retiring":
                retiring.set()
        with patch.object(self.store, "save", side_effect=notify):
            with self.assertRaisesRegex(s.Error, "unresolved managed"):
                self.controller.expire(now=True, volume=DB)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.no_api_mutations()

    def test_unknown_live_attachment_or_busy_mount_preserves_storage(self):
        # Either API direction is sufficient to block an unknown attachment.
        self.api.instances.append(dict(id=WORKER, volume_ids=[]))
        self.api.row["instances"].append(dict(id=WORKER))
        self.due()
        with self.assertRaisesRegex(s.Error, "unrecognized live attachment"):
            self.controller.expire()
        self.no_api_mutations()
        self.api.instances.pop()
        self.api.row["instances"].pop()
        self.ops.busy_mount = True
        with self.assertRaisesRegex(s.Error, "busy mount"):
            self.controller.expire()
        self.no_api_mutations()

    def test_accepted_but_unconfirmed_delete_stays_retiring_and_retries(self):
        self.api.row.update(instances=[], instance_id=None)
        self.api.instances[0]["volume_ids"] = [SHARED]
        self.api.delete_lands = False
        self.due()
        with self.assertRaisesRegex(s.Error, "deletion unconfirmed"):
            self.controller.expire()
        self.assertEqual(self.receipt()["status"], "retiring")
        self.api.delete_lands = True
        self.assertTrue(self.controller.expire())
        self.assertEqual(self.receipt()["status"], "complete")

    def test_get404_with_matching_deleted_trash_confirms_soft_delete(self):
        self.api.get404 = True
        self.api.deleted = True
        self.api.row.update(status="deleted", deleted_at=s.stamp(self.clock()), instances=[], instance_id=None)
        self.due()
        self.assertTrue(self.controller.expire())
        self.assertEqual(self.receipt()["status"], "complete")
        self.assertEqual(self.events, [])
        self.no_api_mutations()

    def test_get404_trash_identity_mismatch_is_not_confirmation(self):
        self.api.get404 = True
        self.api.deleted = True
        self.api.row.update(status="deleted", deleted_at=s.stamp(self.clock()), name="different-data")
        self.due()
        with self.assertRaisesRegex(s.Error, "identity differs"):
            self.controller.expire()
        self.assertEqual(self.events, [])
        self.no_api_mutations()


class ProcessTests(unittest.TestCase):
    def test_reused_pid_or_previous_boot_is_never_signalled(self):
        job = dict(pid=424242, start_ticks="123", boot_id=BOOT)
        for current_boot, ticks in ((DB, "123"), (BOOT, "456")):
            with self.subTest(boot=current_boot, ticks=ticks), patch.object(s, "boot_id", return_value=current_boot), \
                 patch.object(s, "process_start", return_value=ticks), patch.object(s.os, "pidfd_open", return_value=44), \
                 patch.object(s.os, "close"), patch.object(s.signal, "pidfd_send_signal") as kill:
                s.Operations().stop_process(job)
                kill.assert_not_called()

    def test_matching_process_gets_individual_signal_and_stops_without_group_kill(self):
        job = dict(pid=424242, start_ticks="123", boot_id=BOOT)
        with patch.object(s, "boot_id", return_value=BOOT), patch.object(s, "process_start", side_effect=["123", None]), \
             patch.object(s.os, "pidfd_open", return_value=44), patch.object(s.os, "close"), \
             patch.object(s.signal, "pidfd_send_signal") as kill:
            s.Operations().stop_process(job)
            kill.assert_called_once_with(44, s.signal.SIGTERM)

    def test_wrong_nfs_source_is_not_unmounted(self):
        result = s.subprocess.CompletedProcess([], 0, json.dumps(dict(filesystems=[
            dict(source="nfs.fin-02.datacrunch.io:/bio-shared", fstype="nfs4")])))
        with patch.object(s.Operations, "run", return_value=result) as run:
            with self.assertRaisesRegex(s.Error, "another filesystem"):
                s.Operations().unmount("nfs.fin-02.datacrunch.io:/bio-rfaa-db-test")
            self.assertEqual(run.call_count, 1)

    def test_automount_stops_before_plain_unmount_without_force(self):
        source = "nfs.fin-02.datacrunch.io:/bio-rfaa-db-test"
        mounted = s.subprocess.CompletedProcess([], 0, json.dumps(dict(filesystems=[dict(source=source, fstype="nfs4")])))
        loaded = s.subprocess.CompletedProcess([], 0, "loaded\n")
        done = s.subprocess.CompletedProcess([], 0, "")
        with patch.object(s.Operations, "run", side_effect=[mounted, loaded, done, mounted, done]) as run:
            s.Operations().unmount(source)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[2], ["systemctl", "stop", r"mnt-bio\x2ddatabases.automount"])
            self.assertEqual(commands[-1], ["umount", s.MOUNT])


if __name__ == "__main__":
    unittest.main()
