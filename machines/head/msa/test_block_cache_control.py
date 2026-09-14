"""Offline ownership, allocation recovery and lease tests; no cloud calls."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import block_cache_control as cache
import block_cache_device as device

ROOT = Path(__file__).resolve().parents[1]
budget = runpy.run_path(str(ROOT / "dc-budget.py"))
storage = runpy.run_path(str(ROOT / "rfaa/storage.py"))
HEAD = "10000000-0000-4000-8000-000000000001"
HEAD_OS = "10000000-0000-4000-8000-000000000002"
VOLUME = "10000000-0000-4000-8000-000000000003"
WORKER = "10000000-0000-4000-8000-000000000004"
WORKER_OS = "10000000-0000-4000-8000-000000000005"
BOOT = "10000000-0000-4000-8000-000000000006"
SOURCE, RECEIPTS, LEASE, TOKEN = "a" * 64, "b" * 64, "c" * 32, "d" * 32


class API:
    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.instances = [dict(id=HEAD, hostname="head", location="FIN-02", status="running",
            price_per_hour=.05, created_at=storage["stamp"](clock() - 3600), volume_ids=[HEAD_OS],
            os_volume_id=HEAD_OS, currency="usd", contract="PAY_AS_YOU_GO")]
        self.volumes = [dict(id=HEAD_OS, name="head-os", type="NVMe", size=50,
            status="attached", is_os_volume=True, instance_id=HEAD, instances=[{"id": HEAD}],
            created_at=storage["stamp"](clock() - 3600), base_hourly_cost=.02,
            currency="usd", contract="PAY_AS_YOU_GO", tags=[])]
        self.trash, self.error, self.lands, self.hide = [], None, False, False
        self.price, self.before_post, self.bad_reply = .1 / 730 / 3600, lambda body: None, False

    def inventory(self):
        rows = [v for v in self.volumes if not (self.hide and v["id"] == VOLUME)]
        return copy.deepcopy((self.instances, rows, self.trash))

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if (method, path) == ("GET", "/volume-types?currency=usd"):
            return [{"type": "NVMe", "price": {"currency": "usd", "cps_per_gb": self.price}}]
        if (method, path) == ("POST", "/volumes"):
            self.before_post(body)
            if self.error and not self.lands:
                raise self.error
            row = dict(id=VOLUME, name=body["name"], type=body["type"], size=body["size"],
                location=body["location_code"], tags=copy.deepcopy(body["tags"]), is_os_volume=False,
                currency="usd", contract="PAY_AS_YOU_GO", created_at=storage["stamp"](self.clock()),
                base_hourly_cost=self.price * 3600 * body["size"], status="detached", instance_id=None,
                instances=[], target="")
            self.volumes.append(row)
            if self.error:
                raise self.error
            return "invalid-provider-uuid" if self.bad_reply else VOLUME
        raise AssertionError((method, path, body))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1789340000.0
        self.clock = lambda: self.now
        self.env = patch.dict(os.environ, {"DC_STATE_DIR": str(self.root), "DC_BUDGET_CEILING": "750",
            "DC_BUDGET_MARGIN": "10", "DC_PERSISTENT_RESERVE_HOURS": "24"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = API(self.clock)
        self.c = self.controller()
        state = budget["new_state"](self.root / "missing-ledger", self.now)
        state["last_watchdog"] = self.now
        self.c.accounting.store.save(state)

    def controller(self):
        return cache.Controller(self.api, self.root, budget, storage, clock=self.clock, owner=os.geteuid())

    def posts(self):
        return [r for r in self.api.calls if r[0] == "POST"]

    def create(self):
        return self.c.execute("create", SOURCE, RECEIPTS)

    def volume(self):
        return next(v for v in self.api.volumes if v["id"] == VOLUME)

    def state(self):
        return json.loads((self.root / "budget.json").read_text())

    def test_fixed_plan_quotes_background_and_does_not_create(self):
        result = self.c.execute("plan", SOURCE, RECEIPTS)
        self.assertEqual(self.posts(), [])
        self.assertEqual(result["creation"]["request"]["instance_ids"], [])
        self.assertEqual(result["creation"]["request"]["type"], "NVMe")
        self.assertEqual(result["size_bytes"], 1300 * 2**30)
        self.assertEqual(result["location"], "FIN-02")
        self.assertEqual(result["quote"]["reserve_hours"], 24)
        self.assertGreater(result["quote"]["background_reserve"], 0)
        self.assertAlmostEqual(result["quote"]["new_hourly"], 130 / 730)
        self.assertEqual(result["cache_generation"], cache.generation(result["cache_id"], SOURCE, RECEIPTS))
        self.assertEqual((self.root / cache.INTENT).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state()["jobs"], {})

    def test_create_intent_is_fsynced_before_post_and_replay_creates_once(self):
        def before(body):
            intent = self.c.read()
            self.assertEqual(intent["status"], "creating")
            self.assertEqual(intent["request"], body)
            self.assertIsNotNone(intent["attempted_at"])
            self.assertIsNotNone(intent["filesystem_uuid"])
            self.assertEqual(self.state()["jobs"], {})
        self.api.before_post = before
        first = self.create()
        second = self.controller().execute("create", SOURCE, RECEIPTS)
        self.assertEqual(first["volume_id"], VOLUME)
        self.assertEqual(first["filesystem_uuid"], second["filesystem_uuid"])
        self.assertEqual(len(self.posts()), 1)
        self.assertIn(VOLUME, self.state()["expected_database_volumes"])
        self.assertEqual(self.api.volumes[0]["id"], HEAD_OS)

    def test_two_creators_cannot_allocate_two_volumes(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.controller().execute("create", SOURCE, RECEIPTS), range(2)))
        self.assertEqual([r["volume_id"] for r in results], [VOLUME, VOLUME])
        self.assertEqual(len(self.posts()), 1)

    def test_lost_response_is_reconciled_by_exact_token_without_retry(self):
        self.api.error, self.api.lands = RuntimeError("PRIVATE_SECRET"), True
        first = self.create()
        self.assertEqual(first["status"], "uncertain")
        self.assertNotIn("PRIVATE_SECRET", (self.root / cache.INTENT).read_text())
        self.api.error = None
        second = self.controller().execute("reconcile")
        self.assertEqual(second["volume_id"], VOLUME)
        self.assertIsNone(second["creation"]["response_id"])
        self.assertEqual(second["creation"]["reconciled_id"], VOLUME)
        self.assertEqual(len(self.posts()), 1)

    def test_no_visible_uncertain_allocation_never_retries(self):
        self.api.error = RuntimeError("outcome unknown")
        self.assertEqual(self.create()["status"], "uncertain")
        self.api.error = None
        self.assertEqual(self.create()["status"], "uncertain")
        self.assertEqual(len(self.posts()), 1)
        with self.assertRaises(budget["LaunchBlocked"]):
            budget["check_storage_lifetime"](self.root, [], self.now)

    def test_malformed_success_response_can_be_reconciled(self):
        self.api.bad_reply = True
        self.assertEqual(self.create()["status"], "uncertain")
        self.assertEqual(self.c.execute("reconcile")["volume_id"], VOLUME)
        self.assertEqual(len(self.posts()), 1)

    def test_missing_previously_allocated_volume_preserves_billing_and_blocks(self):
        self.create()
        self.api.hide = True
        self.now += 60
        result = self.c.execute("reconcile")
        self.assertEqual(result["status"], "uncertain")
        self.assertTrue(self.state()["resources"]["volume:" + VOLUME]["active"])
        self.assertIn(VOLUME, self.state()["unresolved_database_volumes"])

    def test_ambiguous_token_or_changed_identity_cannot_be_adopted(self):
        self.create()
        duplicate = copy.deepcopy(self.volume())
        duplicate["id"] = WORKER_OS
        self.api.volumes.append(duplicate)
        with self.assertRaisesRegex(cache.Error, "Multiple"):
            self.c.execute("reconcile")
        self.assertEqual(self.c.read()["status"], "uncertain")
        self.assertEqual(len(self.posts()), 1)

    def test_deleted_or_wrong_geometry_is_never_recreated(self):
        self.create()
        original_volume, original_intent = copy.deepcopy(self.volume()), self.c.read()
        for change in ({"size": 1299}, {"is_os_volume": True}, {"location": "FIN-01"},
                       {"type": "NVMe_Shared"}, {"deleted_at": storage["stamp"](self.now)},
                       {"tags": []}, {"status": "deleted"}):
            with self.subTest(change=change):
                volume = self.volume()
                volume.clear()
                volume.update(copy.deepcopy(original_volume))
                self.c.intent.save(copy.deepcopy(original_intent))
                self.volume().update(change)
                with self.assertRaises(cache.Error):
                    self.c.execute("reconcile")
        self.assertEqual(len(self.posts()), 1)

    def test_stale_watchdog_budget_and_uncertain_jobs_block_before_post(self):
        state = self.state()
        state["last_watchdog"] = self.now - 181
        self.c.accounting.store.save(state)
        with self.assertRaisesRegex(cache.Error, "blocked"):
            self.create()
        self.assertEqual(self.posts(), [])
        state["last_watchdog"], state["historical_correction"] = self.now, 748
        self.c.accounting.store.save(state)
        with self.assertRaisesRegex(cache.Error, "blocked"):
            self.create()
        self.assertEqual(self.posts(), [])

    def test_other_uncertain_storage_fences_cache_creation(self):
        self.c.storage["Store"](self.root / "msa-storage-intent.json", owner=os.geteuid()).save(
            dict(version=1, profile="colabfold", status="uncertain"))
        with self.assertRaisesRegex(cache.Error, "Unresolved colabfold"):
            self.create()
        self.assertEqual(self.posts(), [])

    def test_invalid_quote_values_and_higher_env_ceiling_cannot_bypass_guard(self):
        for value in [None, True, 0, -1, float("nan"), float("inf"), "0.001"]:
            self.api.price = value
            with self.subTest(value=value), self.assertRaises(cache.Error):
                self.create()
        self.assertEqual(self.posts(), [])
        with patch.dict(os.environ, {"DC_BUDGET_CEILING": "1000000"}):
            self.assertEqual(self.controller().accounting.ceiling, 1000)

    def test_source_and_request_changes_are_rejected(self):
        self.create()
        for command in ["create", "plan", "status", "reconcile"]:
            with self.assertRaisesRegex(cache.Error, "source"):
                self.c.execute(command, "f" * 64, RECEIPTS)
        value = self.c.read()
        value["request"]["size"] = 2000
        self.c.intent.save(value)
        with self.assertRaisesRegex(cache.Error, "request changed"):
            self.c.execute("reconcile")

    def acquire(self):
        self.create()
        return self.c.acquire(LEASE, "populate")

    def managed_worker(self):
        result = self.acquire()
        self.c.launching(LEASE)
        self.api.instances.append(dict(id=WORKER, hostname=result["lease"]["worker_name"],
            description="bio-dc:" + TOKEN + " ephemeral deadline=1", status="running", location="FIN-02",
            price_per_hour=3.25, created_at=storage["stamp"](self.now), volume_ids=[WORKER_OS, VOLUME],
            os_volume_id=WORKER_OS, currency="usd", contract="PAY_AS_YOU_GO"))
        self.api.volumes.append(dict(id=WORKER_OS, name="managed-os", type="NVMe", size=92,
            status="attached", is_os_volume=True, instance_id=WORKER, instances=[WORKER],
            created_at=storage["stamp"](self.now), base_hourly_cost=.025,
            currency="usd", contract="PAY_AS_YOU_GO", tags=[], target="vda"))
        self.volume().update(status="attached", instance_id=WORKER, instances=[{"id": WORKER}], target="vdb")
        state = self.state()
        state["jobs"][TOKEN] = dict(id=WORKER, os_id=WORKER_OS, created=self.now, deadline=self.now + 3600,
            rate=3.25, os_rate=.025, status="running", volumes=[VOLUME], cache_lease_id=LEASE,
            cache_generation=result["cache_generation"])
        self.c.accounting.store.save(state)
        return self.c.bind(LEASE, WORKER, BOOT)

    def close_worker(self):
        self.api.instances = [i for i in self.api.instances if i["id"] != WORKER]
        self.api.volumes = [v for v in self.api.volumes if v["id"] != WORKER_OS]
        self.volume().update(status="detached", instance_id=None, instances=[], target="")
        state = self.state()
        state["jobs"][TOKEN]["status"] = "closed"
        self.c.accounting.store.save(state)

    def test_unready_cache_cannot_serve_and_leases_cannot_be_overwritten(self):
        self.create()
        with self.assertRaisesRegex(cache.Error, "ready"):
            self.c.acquire(LEASE)
        first = self.c.acquire(LEASE, "populate")
        self.assertEqual(self.c.acquire(LEASE, "populate")["lease"], first["lease"])
        with self.assertRaisesRegex(cache.Error, "exclusively leased"):
            self.c.acquire("e" * 32, "populate")
        self.now += 100000
        with self.assertRaisesRegex(cache.Error, "exclusively leased"):
            self.c.acquire("e" * 32, "populate")

    def test_provider_attachment_metadata_must_prove_detachment(self):
        self.create()
        original = copy.deepcopy(self.volume())
        for change in ({"instances": None}, {"instance_id": HEAD}, {"instances": [HEAD]},
                       {"status": "detaching"}, {"status": "attaching"}):
            with self.subTest(change=change):
                self.volume().update(change)
                with self.assertRaises(cache.Error):
                    self.c.acquire(LEASE, "populate")
                self.volume().clear()
                self.api.volumes[-1].update(copy.deepcopy(original))

    def test_bound_lease_has_exact_worker_os_token_and_boot(self):
        result = self.managed_worker()
        self.assertEqual(result["provider"]["managed"], dict(instance_id=WORKER, os_volume_id=WORKER_OS,
                                                            token=TOKEN, boot_id=BOOT))
        self.assertEqual(result["lease"]["status"], "bound")
        with self.assertRaisesRegex(cache.Error, "boot identity changed"):
            self.c.bind(LEASE, WORKER, str(__import__("uuid").uuid4()))
        self.api.instances[-1]["hostname"] = "different-worker"
        with self.assertRaisesRegex(cache.Error, "name or reservation"):
            self.c.bind(LEASE, WORKER)

    def test_real_budget_gate_consumes_controller_lease_shape(self):
        receipt = self.acquire()
        args = SimpleNamespace(volume=[VOLUME], name=receipt["lease"]["worker_name"], loc="FIN-02")
        with patch.dict(os.environ, {"BIO_MSA_CACHE_LEASE_ID": LEASE,
                                     "BIO_MSA_CACHE_GENERATION": receipt["cache_generation"]}):
            with self.assertRaisesRegex(budget["LaunchBlocked"], "does not authorize"):
                budget["cache_launch_context"](self.root, args, self.state())
            self.c.launching(LEASE)
            self.assertEqual(budget["cache_launch_context"](self.root, args, self.state()),
                dict(cache_lease_id=LEASE, cache_generation=receipt["cache_generation"]))
            state = self.state()
            state["jobs"][TOKEN] = dict(status="uncertain", volumes=[VOLUME])
            with self.assertRaisesRegex(budget["LaunchBlocked"], "may own"):
                budget["cache_launch_context"](self.root, args, state)

    def test_controller_envelope_matches_exact_device_helper_contract(self):
        receipt = self.managed_worker()
        binding = device.provider_binding(receipt, receipt["provider"], self.now)
        self.assertEqual(binding["device"], "/dev/vdb")
        self.assertEqual(binding["os_device"], "/dev/vda")
        self.assertEqual(binding["volume_id"], VOLUME)
        self.assertEqual(binding["boot_id"], BOOT)
        altered = copy.deepcopy(receipt)
        altered["creation"]["response_id"] = None
        with self.assertRaisesRegex(device.InspectionRequired, "POST receipt"):
            device.provider_binding(altered, altered["provider"], self.now)

    def test_release_waits_for_exact_vm_budget_and_detachment(self):
        self.managed_worker()
        with self.assertRaises(cache.Error):
            self.c.release(LEASE)
        self.volume().update(status="detached", instance_id=None, instances=[])
        with self.assertRaises(cache.Error):
            self.c.release(LEASE)
        self.api.instances = self.api.instances[:1]
        with self.assertRaisesRegex(cache.Error, "unresolved"):
            self.c.release(LEASE)
        self.close_worker()
        result = self.c.release(LEASE)
        self.assertEqual(result["lease"]["status"], "released")
        self.assertEqual(result["lease"]["closed_job_tokens"], [TOKEN])
        self.assertEqual(self.volume()["id"], VOLUME)
        with self.assertRaisesRegex(cache.Error, "cannot be reused"):
            self.c.acquire(LEASE, "populate")
        self.assertEqual(self.c.acquire("e" * 32, "populate")["lease"]["lease_id"], "e" * 32)

    def test_unknown_launch_cannot_be_released_from_elapsed_time(self):
        self.acquire()
        self.c.launching(LEASE)
        self.now += 100000
        with self.assertRaisesRegex(cache.Error, "no exact budget receipt"):
            self.c.release(LEASE)
        self.assertEqual(self.c.current_lease()["status"], "launching")

    def test_closed_job_with_lingering_os_trash_cannot_release(self):
        self.managed_worker()
        os_row = copy.deepcopy(next(v for v in self.api.volumes if v["id"] == WORKER_OS))
        self.close_worker()
        os_row.update(status="deleted", deleted_at=storage["stamp"](self.now), instance_id=None, instances=[])
        self.api.trash.append(os_row)
        with self.assertRaisesRegex(cache.Error, "OS disk remains"):
            self.c.release(LEASE)
        self.assertEqual(self.c.current_lease()["status"], "bound")
        self.api.trash.clear()
        self.assertEqual(self.c.release(LEASE)["lease"]["status"], "released")

    def test_preparing_cancellation_and_closed_rejection_release_safely(self):
        self.acquire()
        self.assertEqual(self.c.release(LEASE)["lease"]["status"], "released")
        other = "e" * 32
        self.c.acquire(other, "populate")
        self.c.launching(other)
        state = self.state()
        state["jobs"][TOKEN] = dict(id=None, os_id=None, created=self.now, deadline=self.now + 3600,
            rate=3.25, os_rate=.025, status="closed", volumes=[VOLUME], cache_lease_id=other,
            cache_generation=self.c.read()["cache_generation"])
        self.c.accounting.store.save(state)
        self.assertEqual(self.c.release(other)["lease"]["status"], "released")

    def ready_bytes(self, binding):
        intent = self.c.read()
        ready = dict(schema=1, status="ready", kind="msa-full-database-block-cache", **self.c.identity(intent),
            completion=dict(files=2, symlinks=0, payload_bytes=1000, source_bytes_hashed=1000,
                            destination_bytes_readback=1000, full_readback=True),
            source=dict(manifest_sha256=SOURCE, receipt_sha256=RECEIPTS), content_manifest_sha256="e" * 64,
            filesystem_type="ext4", size_bytes=1300 * 2**30, created_epoch=self.now, rootrel="colabfold")
        return ready

    def test_ready_requires_full_readback_and_exact_generation_then_seals_once(self):
        binding = self.managed_worker()
        ready = self.ready_bytes(binding)
        for key, value in [("volume_id", HEAD_OS), ("source_manifest_sha256", "f" * 64)]:
            changed = dict(ready, **{key: value})
            raw = json.dumps(changed).encode()
            with self.assertRaisesRegex(cache.Error, "identity changed"):
                self.c.publish_ready(LEASE, raw, hashlib.sha256(raw).hexdigest())
        changed = copy.deepcopy(ready)
        changed["completion"]["full_readback"] = False
        raw = json.dumps(changed).encode()
        with self.assertRaisesRegex(cache.Error, "readback"):
            self.c.publish_ready(LEASE, raw, hashlib.sha256(raw).hexdigest())
        raw = json.dumps(ready).encode()
        sha = hashlib.sha256(raw).hexdigest()
        self.c.publish_ready(LEASE, raw, sha)
        self.assertEqual(self.c.read()["ready_receipt_sha256"], sha)
        self.c.publish_ready(LEASE, raw, sha)
        changed = dict(ready, created_epoch=self.now + 1)
        changed_raw = json.dumps(changed).encode()
        with self.assertRaisesRegex(cache.Error, "already sealed"):
            self.c.publish_ready(LEASE, changed_raw, hashlib.sha256(changed_raw).hexdigest())
        self.close_worker()
        self.c.release(LEASE)
        self.assertEqual(self.c.acquire("e" * 32, "serve")["ready_receipt_sha256"], sha)

    def test_only_new_volume_post_is_a_resource_mutation(self):
        self.managed_worker()
        self.close_worker()
        self.c.release(LEASE)
        self.assertEqual([(m, p) for m, p, _ in self.api.calls if m != "GET"], [("POST", "/volumes")])

    def retired_cache(self):
        self.managed_worker()
        self.close_worker()
        self.c.release(LEASE)
        self.now += 60
        removed = copy.deepcopy(self.volume())
        removed.update(status="deleted", deleted_at=storage["stamp"](self.now))
        self.api.volumes = [v for v in self.api.volumes if v["id"] != VOLUME]
        self.api.trash.append(removed)
        state = self.state()
        budget["reconcile"](state, self.api.inventory(), self.now)
        self.assertEqual(state["resources"]["volume:" + VOLUME]["deleted"], self.now)
        state["expected_database_volumes"][VOLUME]["retired_at"] = self.now
        self.c.accounting.store.save(state)
        self.api.trash.clear()
        self.c.save(self.c.read(), status="rejected", retired_at=self.now,
                    permanent_removal_confirmed_epoch=self.now)
        return self.c.read()

    def test_reset_retired_preserves_evidence_and_budget_then_plans_new_identity_once(self):
        intent = self.retired_cache()
        old_budget = self.state()
        lease_path = self.c.lease_store(LEASE).path
        old_lease = lease_path.read_bytes()
        result = self.c.reset_retired(VOLUME, intent["token"])
        archive_path = Path(result["archive_path"])
        archived = json.loads(archive_path.read_text())
        self.assertEqual(result["archive_sha256"], hashlib.sha256(archive_path.read_bytes()).hexdigest())
        self.assertEqual(archived["intent"], intent)
        self.assertEqual(archived["leases"][LEASE], json.loads(old_lease))
        self.assertEqual(archived["active_pointer"], dict(schema=1, lease_id=LEASE))
        self.assertEqual(lease_path.read_bytes(), old_lease)
        self.assertIsNone(self.c.read(required=False))
        self.assertIsNone(self.c.active.read())
        for key in ("jobs", "expected_database_volumes"):
            self.assertEqual(self.state()[key], old_budget[key])
        self.assertEqual(self.state()["resources"]["volume:" + VOLUME], old_budget["resources"]["volume:" + VOLUME])
        planned = self.c.execute("plan", SOURCE, RECEIPTS)
        self.assertNotEqual(planned["cache_id"], intent["cache_id"])
        self.assertNotEqual(planned["filesystem_uuid"], intent["filesystem_uuid"])
        self.assertNotEqual(planned["cache_generation"], intent["cache_generation"])
        self.assertEqual(planned["status"], "planned")
        self.assertEqual(self.c.reset_retired(VOLUME, intent["token"]), result)
        self.assertEqual(self.c.read()["cache_id"], planned["cache_id"])
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual([(m, p) for m, p, _ in self.api.calls if m != "GET"], [("POST", "/volumes")])

    def test_reset_retired_recovers_after_either_pointer_unlink_without_losing_history(self):
        intent = self.retired_cache()
        original_unlink = Path.unlink
        fail_on = [self.c.intent.path]
        def interrupted(path, *args, **kwargs):
            if fail_on and path == fail_on[0]:
                fail_on.clear()
                raise OSError("simulated interrupted reset")
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", interrupted), self.assertRaisesRegex(OSError, "interrupted"):
            self.c.reset_retired(VOLUME, intent["token"])
        self.assertIsNone(self.c.active.read())
        self.assertEqual(self.c.read(), intent)
        archive = self.root / ("msa-block-cache-retired-" + intent["token"] + ".json")
        retained = archive.read_bytes()
        original_save = storage["Store"].save
        def interrupt_completion(store, value):
            if store.path.name.startswith("msa-block-cache-reset-"):
                raise OSError("completion interrupted")
            return original_save(store, value)
        with patch.object(storage["Store"], "save", interrupt_completion), self.assertRaisesRegex(OSError, "completion"):
            self.c.reset_retired(VOLUME, intent["token"])
        self.assertIsNone(self.c.read(required=False))
        result = self.controller().reset_retired(VOLUME, intent["token"])
        self.assertEqual(result["status"], "reset")
        self.assertEqual(archive.read_bytes(), retained)
        self.assertIsNotNone(self.c.lease_store(LEASE).read())

    def test_reset_retired_requires_exact_identity_rejected_state_and_budget_deletion(self):
        intent = self.retired_cache()
        state = self.state()
        for changed in (dict(status="uncertain"), dict(status="allocated"),
                        dict(status="creating"), dict(retired_at=None),
                        dict(permanent_removal_confirmed_epoch=self.now + 1)):
            with self.subTest(changed=changed):
                self.c.intent.save(dict(intent, **changed))
                with self.assertRaises(cache.Error):
                    self.c.reset_retired(VOLUME, intent["token"])
        self.c.intent.save(intent)
        for volume, token in ((WORKER_OS, intent["token"]), (VOLUME, "f" * 32)):
            with self.assertRaisesRegex(cache.Error, "exact rejected"):
                self.c.reset_retired(volume, token)
        for resource_changes, allocation_changes in ((dict(active=True), {}), (dict(deleted=None), {}),
                (dict(name="wrong"), {}), ({}, dict(retired_at=None)), ({}, dict(profile="colabfold"))):
            with self.subTest(resource=resource_changes, allocation=allocation_changes):
                altered = copy.deepcopy(state)
                altered["resources"]["volume:" + VOLUME].update(resource_changes)
                altered["expected_database_volumes"][VOLUME].update(allocation_changes)
                self.c.accounting.store.save(altered)
                with self.assertRaisesRegex(cache.Error, "budget ownership"):
                    self.c.reset_retired(VOLUME, intent["token"])
        self.assertEqual(self.c.read(), intent)
        self.assertFalse(list(self.root.glob("msa-block-cache-retired-*.json")))

    def test_reset_retired_blocks_live_or_ambiguous_provider_identity_and_old_workers(self):
        intent = self.retired_cache()
        clear = self.api.inventory()
        for where, row in [(1, intent["verified"]), (2, intent["verified"]),
                (1, dict(intent["verified"], id=WORKER_OS)),
                (1, dict(intent["verified"], id=WORKER_OS, name="different-name")),
                (1, dict(intent["verified"], id=WORKER_OS, name="old-os", tags=[])),
                (0, dict(id=WORKER, hostname="unrelated-name", volume_ids=[], os_volume_id=WORKER_OS)),
                (0, dict(id=HEAD, hostname="head", volume_ids=[VOLUME], os_volume_id=HEAD_OS)),
                (0, dict(id=HEAD, hostname="bio-msa-cache-" + LEASE, volume_ids=[], os_volume_id=HEAD_OS))]:
            with self.subTest(where=where, row=row):
                self.api.instances, self.api.volumes, self.api.trash = copy.deepcopy(clear)
                (self.api.instances, self.api.volumes, self.api.trash)[where].append(copy.deepcopy(row))
                with self.assertRaises(cache.Error):
                    self.c.reset_retired(VOLUME, intent["token"])
        self.assertEqual(self.c.read(), intent)
        self.assertFalse(list(self.root.glob("msa-block-cache-retired-*.json")))

    def test_reset_retired_requires_all_released_leases_and_exact_closed_budget_jobs(self):
        intent = self.retired_cache()
        original_lease = self.c.lease_store(LEASE).read()
        state = self.state()
        for changes in (dict(status="bound"), dict(status="launching"), dict(release_checks={}),
                        dict(closed_job_tokens=[]), dict(managed=dict(instance_id=HEAD, os_volume_id=HEAD_OS, token=TOKEN))):
            with self.subTest(changes=changes):
                self.c.lease_store(LEASE).save(dict(original_lease, **changes))
                with self.assertRaises(cache.Error):
                    self.c.reset_retired(VOLUME, intent["token"])
        self.c.lease_store(LEASE).save(original_lease)
        for changes in (dict(status="uncertain"), dict(cache_generation="f" * 64),
                        dict(volumes=[]), dict(os_id=HEAD_OS), dict(cache_lease_id="f" * 32)):
            with self.subTest(changes=changes):
                changed = copy.deepcopy(state)
                changed["jobs"][TOKEN].update(changes)
                self.c.accounting.store.save(changed)
                with self.assertRaises(cache.Error):
                    self.c.reset_retired(VOLUME, intent["token"])
        self.c.accounting.store.save(state)
        old = dict(original_lease, lease_id="e" * 32, worker_name="bio-msa-cache-" + "e" * 32, status="bound")
        self.c.lease_store(old["lease_id"]).save(old)
        with self.assertRaisesRegex(cache.Error, "Historical"):
            self.c.reset_retired(VOLUME, intent["token"])
        self.assertEqual(self.c.read(), intent)

    def test_reset_retired_archive_or_completion_changes_fail_closed(self):
        intent = self.retired_cache()
        result = self.c.reset_retired(VOLUME, intent["token"])
        archive = self.root / Path(result["archive_path"]).name
        value = json.loads(archive.read_text())
        value["intent"]["volume_id"] = HEAD_OS
        storage["Store"](archive, owner=os.geteuid()).save(value)
        with self.assertRaisesRegex(cache.Error, "receipt changed"):
            self.c.reset_retired(VOLUME, intent["token"])


if __name__ == "__main__":
    unittest.main()
