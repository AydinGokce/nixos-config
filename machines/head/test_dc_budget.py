"""Offline behavioral tests for cost accounting and paid-operation boundaries.

Run: python3 -m unittest discover -s machines/head -p 'test_dc_budget.py' -v
No credentials/network/cloud resources are used.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("dc_budget", Path(__file__).with_name("dc-budget.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def instance(ident, now, rate=2, **extra):
    return dict(id=ident, created_at=stamp(now - 3600), price_per_hour=rate,
                status="running", instance_type="GPU", hostname=ident,
                os_volume_id="os-" + ident, description="", ip="192.0.2.1", **extra)


def volume(ident, now, rate=.01, **extra):
    return dict(id=ident, created_at=stamp(now - 3600), base_hourly_cost=rate,
                status="attached", currency="usd", is_os_volume=True,
                instance_id=None, instances=[], type="NVMe", contract="PAY_AS_YOU_GO", **extra)


class Clock:
    def __init__(self):
        self.now = 100000

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeAPI:
    ID = "01234567-89ab-4cde-8fab-0123456789ab"

    def __init__(self, clock):
        self.clock = clock
        self.instances = [instance("head", clock(), .05)]
        self.volumes = [volume("os-head", clock(), .01), volume("shared", clock(), .1)]
        self.volumes[1]["is_os_volume"] = False
        self.trash = []
        self.calls = []
        self.post_error = None
        self.land_before_error = False
        self.provisioning_status = "running"
        self.delete_lands = True
        self.keep_os = False
        self.purge_lands = True
        self.inventory_error = False
        self.created = 0
        self.ondemand_quote = "2"
        self.spot_quote = "1"

    def inventory(self):
        if self.inventory_error:
            raise dc.Error("inventory unavailable")
        return copy.deepcopy((self.instances, self.volumes, self.trash))

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path == "/instance-types":
            return [{"instance_type": "GPU", "price_per_hour": self.ondemand_quote,
                     "spot_price": self.spot_quote, "currency": "usd"}]
        if method == "GET" and path == "/volume-types":
            return [{"type": "NVMe", "price": {"currency": "usd", "cps_per_gb": .01 / 3600 / 50}}]
        if method == "GET" and path == "/sshkeys":
            return [{"id": "key"}]
        if method == "POST" and path == "/instances":
            if self.post_error and not self.land_before_error:
                raise self.post_error
            ident = str(dc.uuid.UUID(int=dc.uuid.UUID(self.ID).int + self.created))
            self.created += 1
            row = instance(ident, self.clock(), float(self.spot_quote if body["is_spot"] else self.ondemand_quote))
            row.update(description=body["description"], created_at=stamp(self.clock()), status=self.provisioning_status)
            self.instances.append(row)
            disk = volume("os-" + ident, self.clock())
            disk.update(created_at=stamp(self.clock()), name=body["os_volume"]["name"])
            self.volumes.append(disk)
            if self.post_error:
                raise self.post_error
            return ident
        if method == "GET" and path.startswith("/instances/"):
            row = next((r for r in self.instances if r["id"] == path.rsplit("/", 1)[1]), None)
            if not row:
                raise dc.APIError(method, path, 404)
            return copy.deepcopy(row)
        if method == "PUT" and path == "/instances":
            if self.delete_lands:
                self.instances = [r for r in self.instances if r["id"] != body["id"]]
                for disk in list(self.volumes):
                    if disk["id"] in body["volume_ids"] and not self.keep_os:
                        disk.update(deleted_at=stamp(self.clock()), status="deleted")
                        self.trash.append(disk)
                        self.volumes.remove(disk)
                    elif disk["id"] in body["volume_ids"]:
                        disk["status"] = "detached"
            return [{"status": "success"}]
        if method == "DELETE" and path.startswith("/volumes/"):
            disk = next((r for r in self.volumes + self.trash if r["id"] == path.rsplit("/", 1)[1]), None)
            if not disk:
                raise dc.APIError(method, path, 404)
            if body["is_permanent"]:
                if self.purge_lands:
                    self.volumes = [r for r in self.volumes if r["id"] != disk["id"]]
                    self.trash = [r for r in self.trash if r["id"] != disk["id"]]
                return None
            disk.update(deleted_at=stamp(self.clock()), status="deleted")
            self.trash.append(disk)
            self.volumes.remove(disk)
            return None
        raise AssertionError((method, path))


class BudgetTests(unittest.TestCase):
    def setUp(self):
        cooldown = patch.dict(os.environ, {"DC_LAUNCH_COOLDOWN_SECONDS": "0"})
        cooldown.start()
        self.addCleanup(cooldown.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = dc.Store(self.tmp.name)
        self.api = FakeAPI(self.clock)
        self.c = dc.Controller(self.api, self.store, self.clock, self.clock.sleep)

    def state(self):
        return json.loads(self.store.path.read_text())

    def args(self):
        return dc.parser().parse_args(["launch", "GPU", "--volume", "shared", "--max-hours", "1"])

    def init(self):
        self.c.watchdog()

    def allocated_database(self, profile="rfaa"):
        ident = "11234567-89ab-4cde-8fab-0123456789ab"
        name = ("bio-rfaa-db-" if profile == "rfaa" else "bio-colabfold-db-") + "a" * 32
        row = volume(ident, self.clock(), .9, name=name)
        row.update(type="NVMe_Shared", is_os_volume=False)
        self.api.volumes.append(row)
        intent = dict(version=1, profile=profile, status="allocated", volume_id=ident, name=name,
                      verified=dict(id=ident, name=name, created_at=row["created_at"], base_hourly_cost=.9))
        filename = "rfaa-storage-intent.json" if profile == "rfaa" else "msa-storage-intent.json"
        path = self.store.root / filename
        path.write_text(json.dumps(intent))
        path.chmod(0o600)
        return row, intent

    def closed_trash(self):
        """A previous-version managed job whose OS disk remains in trash."""
        self.init()
        ident = self.c.launch(self.args())
        self.api.request("PUT", "/instances", {"id": ident, "action": "delete",
                         "volume_ids": ["os-" + ident], "delete_permanently": False})
        with self.store.locked(self.clock()) as state:
            dc.reconcile(state, self.api.inventory(), self.clock())
            token = next(iter(state["jobs"]))
            state["jobs"][token]["status"] = "closed"
        return token, self.api.trash[0]

    def test_baseline_includes_head_shared_os_and_old_closed_gpu_and_trash(self):
        (Path(self.tmp.name) / "ledger.tsv").write_text("old\tGPU\t2\t96400\t100000\n")
        disk = volume("old-os", self.clock(), .02)
        disk.update(deleted_at=stamp(self.clock() - 1800), status="deleted")
        self.api.trash.append(disk)
        self.init()
        self.assertAlmostEqual(dc.summary(self.state(), self.clock())["spent"], 2 + .05 + .01 + .1 + .01)
        self.clock.sleep(3600)
        self.c.watchdog()
        self.assertAlmostEqual(dc.summary(self.state(), self.clock())["spent"], 2 + .01 + 2 * .16)
        self.assertFalse(any(path == "/balance" for _, path, _ in self.api.calls))

    def test_closed_old_ledger_live_instance_is_still_charged(self):
        (Path(self.tmp.name) / "ledger.tsv").write_text("head\tCPU\t.05\t96400\t98200\n")
        self.init()
        self.assertAlmostEqual(self.state()["resources"]["instance:head"]["cost"], .05)

    def test_rate_change_applies_to_future_intervals(self):
        self.init()
        self.clock.sleep(3600)
        self.api.volumes[1]["base_hourly_cost"] = .5
        self.c.watchdog()
        self.clock.sleep(3600)
        self.c.watchdog()
        self.assertAlmostEqual(self.state()["resources"]["volume:shared"]["cost"], .7)

    def test_restored_volume_charges_the_full_trash_gap_once(self):
        self.init()
        disk = self.api.volumes.pop()
        disk.update(status="deleted", deleted_at=stamp(self.clock()))
        self.api.trash.append(disk)
        self.c.watchdog()
        self.clock.sleep(3600)
        self.c.watchdog()
        self.clock.sleep(3600)
        restored = copy.deepcopy(disk)
        restored.pop("deleted_at")
        restored["status"] = "detached"
        self.api.volumes.append(restored)
        # Deliberate overlap simulates endpoints disagreeing during restore.
        self.c.watchdog()
        self.assertAlmostEqual(self.state()["resources"]["volume:shared"]["cost"], .3)
        self.c.watchdog()
        self.assertAlmostEqual(self.state()["resources"]["volume:shared"]["cost"], .3)

    def test_fail_closed_on_missing_rates_bad_currency_or_inventory(self):
        for changed in ({"base_hourly_cost": None}, {"currency": "eur"}, {"base_hourly_cost": "nan"}):
            self.api.volumes[0].update(changed)
            with self.assertRaises(dc.Error):
                self.c.launch(self.args())
            self.api = FakeAPI(self.clock)
            self.c.api = self.api
        self.api.inventory_error = True
        with self.assertRaises(dc.Error):
            self.c.launch(self.args())
        self.assertFalse(any(m == "POST" and p == "/instances" for m, p, _ in self.api.calls))

    def test_stale_watchdog_blocks_before_create(self):
        with self.assertRaisesRegex(dc.Error, "watchdog"):
            self.c.launch(self.args())
        self.init()
        self.clock.sleep(181)
        with self.assertRaisesRegex(dc.Error, "watchdog"):
            self.c.launch(self.args())
        self.assertFalse(any(m == "POST" and p == "/instances" for m, p, _ in self.api.calls))

    def test_slow_inventory_cannot_bypass_watchdog_freshness(self):
        self.init()
        inventory = self.api.inventory
        def slow_inventory():
            self.clock.sleep(181)
            return inventory()
        self.api.inventory = slow_inventory
        with self.assertRaisesRegex(dc.LaunchBlocked, "watchdog"):
            self.c.launch(self.args())
        self.assertFalse(any(m == "POST" and p == "/instances" for m, p, _ in self.api.calls))

    def test_full_runtime_and_background_reserve_blocks_launch(self):
        self.init()
        self.c.ceiling = 15
        with self.assertRaisesRegex(dc.Error, "BUDGET HALT"):
            self.c.launch(self.args())
        self.assertFalse(any(m == "POST" and p == "/instances" for m, p, _ in self.api.calls))

    def test_malformed_hourly_ceiling_blocks_without_quote_post_or_reservation(self):
        self.init()
        before = self.store.path.read_bytes()
        for value in ("", "not-a-rate", "nan", "NaN", "inf", "-inf", "1e309", "-0.01"):
            with self.subTest(value=value), patch.dict(os.environ, DC_MAX_INSTANCE_HOURLY=value):
                self.api.calls.clear()
                with self.assertRaisesRegex(dc.LaunchBlocked, "DC_MAX_INSTANCE_HOURLY"):
                    self.c.launch(self.args())
                self.assertEqual(self.api.calls, [])
                self.assertEqual(self.store.path.read_bytes(), before)
                self.assertEqual(self.api.created, 0)

    def test_hourly_ceiling_uses_fresh_spot_quote_before_any_post_or_reservation(self):
        self.init()
        before = self.store.path.read_bytes()
        self.assertEqual(self.c.quote("GPU", True, 50)[0], 1)
        self.api.spot_quote = "13.0001"
        args = self.args()
        args.spot = True
        self.api.calls.clear()
        with patch.dict(os.environ, DC_MAX_INSTANCE_HOURLY="13"):
            with self.assertRaisesRegex(dc.LaunchBlocked, "13.0001/h exceeds.*13/h"):
                self.c.launch(args)
        self.assertEqual([method for method, _, _ in self.api.calls], ["GET", "GET"])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.api.created, 0)

    def test_hourly_ceiling_allows_exact_boundary_and_uses_requested_contract(self):
        self.init()
        for spot, ceiling in ((False, "2"), (True, "1"), (True, "0")):
            with self.subTest(spot=spot), patch.dict(os.environ, DC_MAX_INSTANCE_HOURLY=ceiling):
                args = self.args()
                args.spot = spot
                if spot:
                    self.api.spot_quote = ceiling
                ident = self.c.launch(args)
                job = next(j for j in self.state()["jobs"].values() if j.get("id") == ident)
                self.assertEqual(job["rate"], float(ceiling))
                self.assertGreater(job["os_rate"], 0)
                self.c.remove(ident)

    def test_hourly_ceiling_does_not_replace_overall_runtime_storage_budget(self):
        self.init()
        self.c.ceiling = 15
        before = copy.deepcopy(self.state()["jobs"])
        with patch.dict(os.environ, DC_MAX_INSTANCE_HOURLY="2"):
            with self.assertRaisesRegex(dc.LaunchBlocked, "BUDGET HALT"):
                self.c.launch(self.args())
        self.assertEqual(self.state()["jobs"], before)
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))

    def test_unset_hourly_ceiling_preserves_existing_launch_behavior(self):
        self.init()
        self.api.ondemand_quote = "13.01"
        environment = {k: v for k, v in os.environ.items() if k != "DC_MAX_INSTANCE_HOURLY"}
        with patch.dict(os.environ, environment, clear=True):
            ident = self.c.launch(self.args())
        self.assertEqual(next(j for j in self.state()["jobs"].values() if j.get("id") == ident)["rate"], 13.01)

    def test_locked_reservations_prevent_concurrent_overspend(self):
        self.init()
        def attempt(token):
            try:
                with self.store.locked(self.clock()) as state:
                    dc.reserve(state, token, 5, 0, 4, self.clock(), 50, 10, 24)
                    state["jobs"][token].update(id=token, status="running")
                return True
            except dc.Error:
                return False
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(attempt, ["one", "two"]))
        self.assertEqual(sum(results), 1)

    def test_ten_launches_serialize_admission_but_provision_together(self):
        self.init()
        started = threading.Barrier(10, timeout=10)
        readiness = threading.Barrier(10, timeout=10)
        create_entered, allow_create = threading.Event(), threading.Event()
        original = self.api.request
        first = True

        def request(method, path, body=None):
            nonlocal first
            if method == "POST" and path == "/instances" and first:
                first = False
                create_entered.set()
                self.assertTrue(allow_create.wait(10))
            if method == "GET" and path.startswith("/instances/"):
                # Every admitted create must reach readiness before any is
                # allowed to finish. A whole-provisioning lock deadlocks here.
                readiness.wait()
            return original(method, path, body)

        self.api.request = request

        def launch(_):
            controller = dc.Controller(self.api, dc.Store(self.tmp.name), self.clock, self.clock.sleep)
            started.wait()
            return controller.launch(self.args())

        with ThreadPoolExecutor(10) as pool:
            pending = [pool.submit(launch, index) for index in range(10)]
            try:
                self.assertTrue(create_entered.wait(10))
                # Provider POST is still pending, but the independent budget
                # lock remains available to the watchdog/accounting process.
                with self.store.locked(self.clock()) as state:
                    self.assertEqual(len(state["jobs"]), 1)
                    self.assertEqual(next(iter(state["jobs"].values()))["status"], "pending")
                    state["last_watchdog"] = self.clock()
            finally:
                allow_create.set()
            results = [item.result(timeout=15) for item in pending]
        self.assertEqual(len(set(results)), 10)
        self.assertEqual(sum(method == "POST" for method, _, _ in self.api.calls), 10)
        state = self.state()
        self.assertEqual(len(state["jobs"]), 10)
        self.assertEqual({job["status"] for job in state["jobs"].values()}, {"running"})
        self.assertAlmostEqual(dc.summary(state, self.clock())["reserved"], 20.1)

    def test_ten_launches_share_the_500_dollar_cumulative_guard(self):
        self.init()
        with self.store.locked(self.clock()) as state:
            state["historical_correction"] = 475
        started = threading.Barrier(10, timeout=10)

        def launch(_):
            controller = dc.Controller(self.api, dc.Store(self.tmp.name), self.clock, self.clock.sleep)
            controller.ceiling = 500
            started.wait()
            try:
                return controller.launch(self.args())
            except dc.LaunchBlocked as exc:
                self.assertIn("BUDGET HALT", str(exc))
                return None

        with ThreadPoolExecutor(10) as pool:
            results = list(pool.map(launch, range(10)))
        self.assertEqual(sum(result is not None for result in results), 5)
        self.assertEqual(sum(method == "POST" for method, _, _ in self.api.calls), 5)
        state = self.state()
        self.assertEqual(len(state["jobs"]), 5)
        report = dc.summary(state, self.clock())
        projected = report["spent"] + report["reserved"] + report["background_reserve"] + self.c.margin
        self.assertLess(projected, 500)
        self.assertGreaterEqual(projected + 2.01, 500)

    def test_ten_waiting_launches_cannot_bypass_an_ambiguous_create(self):
        self.init()
        self.api.post_error = dc.APIError("POST", "/instances")
        self.api.land_before_error = True
        started = threading.Barrier(10, timeout=10)

        def launch(_):
            controller = dc.Controller(self.api, dc.Store(self.tmp.name), self.clock, self.clock.sleep)
            started.wait()
            try:
                controller.launch(self.args())
            except dc.LaunchBlocked as exc:
                return str(exc)
            self.fail("An ambiguous create must fence every waiting launch")

        with ThreadPoolExecutor(10) as pool:
            errors = list(pool.map(launch, range(10)))
        self.assertEqual(sum("outcome unknown" in error for error in errors), 1)
        self.assertEqual(sum("Unresolved launch or cleanup" in error for error in errors), 9)
        self.assertEqual(sum(method == "POST" for method, _, _ in self.api.calls), 1)
        state = self.state()
        self.assertEqual(len(state["jobs"]), 1)
        self.assertEqual(next(iter(state["jobs"].values()))["status"], "uncertain")
        self.assertTrue(dc.summary(state, self.clock())["uncertain"])

    def recent_cleanup(self):
        self.init()
        ident = self.c.launch(self.args())
        self.c.remove(ident)
        self.api.calls.clear()
        return self.clock()

    def test_cooldown_waiters_overlap_and_reset_after_new_confirmed_cleanup(self):
        self.init()
        first = self.c.launch(self.args())
        second = self.c.launch(self.args())
        self.c.remove(first)
        start = self.clock()
        self.api.calls.clear()
        progress = []
        def tick():
            self.clock.sleep(30)
            if self.clock() == start + 60:
                self.c.remove(second)
            # This takes the accounting lock while both callers are sleeping.
            # It also proves cleanup/watchdog make progress during the wait.
            self.c.watchdog()
            progress.append(self.clock())
        barrier = threading.Barrier(2, action=tick, timeout=5)
        def sleep(seconds):
            self.assertEqual(seconds, 30)
            self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))
            barrier.wait()
        controllers = [dc.Controller(self.api, self.store, self.clock, sleep) for _ in range(2)]
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda controller: controller.wait_for_launch_cooldown(180), controllers))
        self.assertEqual(results, [None, None])
        self.assertEqual(self.clock(), start + 240)  # New cleanup at +60, then 180 seconds.
        self.assertEqual(len(progress), 8)
        self.assertTrue(all(job['status'] == 'closed' for job in self.state()['jobs'].values()))
        with patch.dict(os.environ, {"DC_LAUNCH_COOLDOWN_SECONDS": "180"}):
            ident = self.c.launch(self.args())
        self.assertEqual(self.clock(), start + 240)
        job = next(job for job in self.state()['jobs'].values() if job.get('id') == ident)
        self.assertEqual(job['created'], start + 240)
        self.assertEqual(job['deadline'], start + 240 + 3600)

    def test_cooldown_rechecks_cleanup_under_reservation_lock_and_requotes(self):
        self.init()
        first = self.c.launch(self.args())
        second = self.c.launch(self.args())
        self.c.remove(first)
        start = self.clock()
        self.api.calls.clear()
        quote, quotations = self.c.quote, []
        def quoted(*args):
            result = quote(*args)
            quotations.append((self.clock(), result[0]))
            if len(quotations) == 1:
                # Another workflow finishes after the outer wait but before
                # this launch obtains its reservation lock.
                self.c.remove(second)
                self.api.ondemand_quote = "3"
            return result
        self.c.quote = quoted
        with patch.dict(os.environ, {"DC_LAUNCH_COOLDOWN_SECONDS": "60"}):
            ident = self.c.launch(self.args())
        self.assertEqual(quotations, [(start + 60, 2), (start + 120, 3)])
        self.assertEqual(len([call for call in self.api.calls if call[0] == 'POST']), 1)
        job = next(job for job in self.state()['jobs'].values() if job.get('id') == ident)
        self.assertEqual(job['created'], start + 120)
        self.assertEqual(job['rate'], 3)

    def test_cooldown_uses_only_confirmed_managed_cleanup_timestamps(self):
        state = {'jobs': {
            'rejected': {'id': None, 'status': 'closed', 'created': 900, 'cleanup_confirmed_at': 900},
            'unconfirmed': {'id': 'pending-cleanup', 'status': 'cleanup', 'created': 800, 'last': 900},
            'historical': {'id': 'old', 'status': 'closed', 'created': 500},
            'purged': {'id': 'confirmed', 'status': 'closed', 'os_purged_at': 100},
            'removed': {'id': 'confirmed-soft-trash', 'status': 'closed', 'cleanup_confirmed_at': 120},
        }, 'last_inventory': 999}
        self.assertEqual(dc.launch_not_before(state, 180), 300)
        self.assertEqual(dc.launch_not_before(state, 0), 0)
        state['jobs']['purged']['os_purged_at'] = 'unknown'
        with self.assertRaisesRegex(dc.LaunchBlocked, 'Invalid confirmed cleanup timestamp'):
            dc.launch_not_before(state, 180)

    def test_cooldown_defaults_to_zero_and_never_delays_cleanup_or_watchdog(self):
        start = self.recent_cleanup()
        with patch.dict(os.environ, {key: value for key, value in os.environ.items()
                                   if key != 'DC_LAUNCH_COOLDOWN_SECONDS'}, clear=True):
            ident = self.c.launch(self.args())
        self.assertEqual(self.clock(), start)
        with patch.dict(os.environ, {'DC_LAUNCH_COOLDOWN_SECONDS': 'invalid-for-launch'}):
            self.c.remove(ident)
            self.c.watchdog()
        self.assertEqual(self.clock(), start)
        self.assertTrue(all('cleanup_confirmed_at' in job for job in self.state()['jobs'].values()))

    def test_cooldown_preserves_fresh_quote_ceiling_and_watchdog_guards(self):
        start = self.recent_cleanup()
        def sleep(seconds):
            self.clock.sleep(seconds)
            self.api.ondemand_quote = '9'
        self.c.sleep = sleep
        with patch.dict(os.environ, {'DC_LAUNCH_COOLDOWN_SECONDS': '60', 'DC_MAX_INSTANCE_HOURLY': '5'}):
            with self.assertRaisesRegex(dc.LaunchBlocked, 'exceeds DC_MAX_INSTANCE_HOURLY'):
                self.c.launch(self.args())
        self.assertEqual(self.clock(), start + 60)
        self.assertFalse(any(method == 'POST' for method, _, _ in self.api.calls))
        self.api.ondemand_quote = '2'
        with patch.dict(os.environ, {'DC_LAUNCH_COOLDOWN_SECONDS': '181'}):
            self.c.sleep = self.clock.sleep
            with self.assertRaisesRegex(dc.LaunchBlocked, 'watchdog'):
                self.c.launch(self.args())
        self.assertEqual(self.clock(), start + 181)
        self.assertFalse(any(method == 'POST' for method, _, _ in self.api.calls))
        self.assertEqual(len(self.state()['jobs']), 1)

    def test_invalid_or_interrupted_cooldown_creates_no_reservation_or_post(self):
        self.recent_cleanup()
        for invalid in ('-1', 'nan', 'inf', '', 'bad'):
            with self.subTest(value=invalid), patch.dict(os.environ, {'DC_LAUNCH_COOLDOWN_SECONDS': invalid}):
                with self.assertRaisesRegex(dc.LaunchBlocked, 'DC_LAUNCH_COOLDOWN_SECONDS'):
                    self.c.launch(self.args())
        self.assertEqual(self.api.calls, [])
        def interrupted(seconds):
            raise KeyboardInterrupt
        self.c.sleep = interrupted
        with patch.dict(os.environ, {'DC_LAUNCH_COOLDOWN_SECONDS': '180'}):
            with self.assertRaises(KeyboardInterrupt):
                self.c.launch(self.args())
        self.assertEqual(self.api.calls, [])
        self.assertEqual(len(self.state()['jobs']), 1)

    def test_launch_and_delete_preserve_shared_volume_and_confirm(self):
        self.init()
        ident = self.c.launch(self.args())
        self.clock.sleep(300)
        self.c.remove(ident)
        deletion = next(b for m, p, b in self.api.calls if m == "PUT")
        self.assertEqual(deletion["volume_ids"], ["os-" + ident])
        self.assertIn("shared", [v["id"] for v in self.api.volumes])
        self.assertFalse(self.api.trash)
        self.assertTrue(any(method == "DELETE" and body == {"is_permanent": True}
                            for method, _, body in self.api.calls))
        self.assertEqual({j["status"] for j in self.state()["jobs"].values()}, {"closed"})
        self.assertIn("os_purged_at", next(iter(self.state()["jobs"].values())))
        self.assertGreater(self.state()["resources"]["instance:" + ident]["cost"], .16)

    def test_spot_removal_policy_is_sent_only_for_spot_contracts(self):
        self.init()
        for spot in (False, True):
            with self.subTest(spot=spot):
                args = self.args()
                args.spot = spot
                ident = self.c.launch(args)
                body = [body for method, path, body in self.api.calls
                        if method == "POST" and path == "/instances"][-1]
                self.assertEqual(body["is_spot"], spot)
                self.assertEqual(body["os_volume"]["size"], args.os_size)
                if spot:
                    self.assertEqual(body["os_volume"]["on_spot_discontinue"], "move_to_trash")
                else:
                    self.assertNotIn("on_spot_discontinue", body["os_volume"])
                self.c.remove(ident)

    def test_unconfirmed_delete_remains_billable_and_retryable(self):
        self.init()
        ident = self.c.launch(self.args())
        self.api.delete_lands = False
        with self.assertRaisesRegex(dc.Error, "unconfirmed"):
            self.c.remove(ident)
        state = self.state()
        self.assertTrue(state["resources"]["instance:" + ident]["active"])
        self.assertEqual(next(iter(state["jobs"].values()))["status"], "cleanup")
        with self.assertRaisesRegex(dc.LaunchBlocked, "Unresolved"):
            self.c.launch(self.args())

    def test_detached_os_disk_is_cleaned_when_instance_delete_left_it_behind(self):
        self.init()
        ident = self.c.launch(self.args())
        self.api.keep_os = True
        self.c.remove(ident)
        self.assertEqual([(p, b["is_permanent"]) for m, p, b in self.api.calls if m == "DELETE"],
                         [("/volumes/os-" + ident, False), ("/volumes/os-" + ident, True)])
        self.assertIn("shared", [v["id"] for v in self.api.volumes])

    def test_cleanup_waits_for_os_attachment_metadata_to_clear(self):
        self.init()
        ident = self.c.launch(self.args())
        self.api.keep_os = True
        inventory = self.api.inventory
        polls = 0
        def delayed_detach():
            nonlocal polls
            snapshot = inventory()
            if not any(row["id"] == ident for row in snapshot[0]):
                polls += 1
                if polls <= 2:
                    disk = next(row for row in snapshot[1] if row["id"] == "os-" + ident)
                    disk.update(status="detaching", instance_id=ident, instances=[{"id": ident}])
            return snapshot
        self.api.inventory = delayed_detach
        start = self.clock()
        self.c.remove(ident)
        self.assertGreaterEqual(self.clock() - start, 10)
        self.assertEqual([(path, body["is_permanent"]) for method, path, body in self.api.calls if method == "DELETE"],
                         [("/volumes/os-" + ident, False), ("/volumes/os-" + ident, True)])
        self.assertEqual(next(iter(self.state()["jobs"].values()))["status"], "closed")

    def test_cleanup_never_deletes_a_still_attached_os_disk(self):
        self.init()
        ident = self.c.launch(self.args())
        self.api.keep_os = True
        inventory = self.api.inventory
        def still_attached():
            snapshot = inventory()
            for disk in snapshot[1]:
                if disk["id"] == "os-" + ident:
                    disk.update(status="attached", instance_id="another-instance", instances=[{"id": "another-instance"}])
            return snapshot
        self.api.inventory = still_attached
        with self.assertRaisesRegex(dc.Error, "unconfirmed"):
            self.c.remove(ident)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.api.calls))
        self.assertEqual(next(iter(self.state()["jobs"].values()))["status"], "cleanup")

    def test_inventory_outage_still_attempts_deadline_cleanup(self):
        self.init()
        ident = self.c.launch(self.args())
        self.clock.sleep(3601)
        self.api.inventory_error = True
        with self.assertRaisesRegex(dc.Error, "inventory unavailable"):
            self.c.watchdog()
        self.assertTrue(any(m == "PUT" and b["id"] == ident for m, _, b in self.api.calls))
        self.assertEqual(next(iter(self.state()["jobs"].values()))["status"], "cleanup")

    def test_provision_failure_removes_partial_launch(self):
        self.init()
        self.api.provisioning_status = "installation_failed"
        with self.assertRaisesRegex(dc.Error, "failed during provisioning"):
            self.c.launch(self.args())
        self.assertEqual([r["id"] for r in self.api.instances], ["head"])
        self.assertEqual(next(iter(self.state()["jobs"].values()))["status"], "closed")

    def test_ambiguous_post_blocks_retry_and_watchdog_finds_and_cleans_orphan(self):
        self.init()
        self.api.post_error = dc.APIError("POST", "/instances")
        self.api.land_before_error = True
        with self.assertRaisesRegex(dc.Error, "outcome unknown"):
            self.c.launch(self.args())
        with self.assertRaisesRegex(dc.Error, "Unresolved"):
            self.c.launch(self.args())
        self.clock.sleep(601)
        self.c.watchdog()
        self.assertEqual([r["id"] for r in self.api.instances], ["head"])

    def test_capacity_rejection_releases_reservation(self):
        self.init()
        self.api.post_error = dc.APIError("POST", "/instances", 503, "service_unavailable; No capacity available")
        with self.assertRaisesRegex(dc.Error, "HTTP 503.*No capacity available"):
            self.c.launch(self.args())
        self.assertEqual(next(iter(self.state()["jobs"].values()))["status"], "closed")

    def test_storage_quota_rejection_stops_gpu_fallback_and_releases_reservation(self):
        self.init()
        self.api.post_error = dc.APIError("POST", "/instances", 400, "invalid_request; Storage limit exceeded")
        with self.assertRaisesRegex(dc.LaunchBlocked, "Storage quota.*dc gc"):
            self.c.launch(self.args())
        self.assertEqual(next(iter(self.state()["jobs"].values()))["status"], "closed")

    def test_storage_retirement_blocks_new_matching_reservations_only(self):
        self.init()
        receipt = self.store.root / "rfaa-storage.json"
        receipt.write_text(json.dumps(dict(volume_id="database", status="retiring",
                                           expires_at=stamp(self.clock() + 3600))))
        receipt.chmod(0o600)
        args = self.args()
        args.volume = ["shared", "database"]
        with self.assertRaisesRegex(dc.LaunchBlocked, "expired or retiring"):
            self.c.launch(args)
        self.assertFalse(self.state()["jobs"])
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))
        self.c.launch(self.args())

    def test_storage_expiry_blocks_launch_and_live_allocation_records_volume_before_post(self):
        self.init()
        receipt = self.store.root / "rfaa-storage.json"
        args = self.args()
        args.volume = ["shared", "database"]
        receipt.write_text(json.dumps(dict(volume_id="database", status="active",
                                           expires_at=stamp(self.clock()))))
        receipt.chmod(0o600)
        with self.assertRaisesRegex(dc.LaunchBlocked, "expired or retiring"):
            self.c.launch(args)
        receipt.write_text(json.dumps(dict(volume_id="database", status="active",
                                           expires_at=stamp(self.clock() + 3600))))
        original = self.api.request
        def request(method, path, body=None):
            if method == "POST" and path == "/instances":
                job = next(iter(self.state()["jobs"].values()))
                self.assertEqual(job["status"], "pending")
                self.assertEqual(job["volumes"], ["shared", "database"])
            return original(method, path, body)
        self.api.request = request
        self.c.launch(args)

    def test_storage_receipt_with_unsafe_permissions_blocks_launch(self):
        self.init()
        receipt = self.store.root / "rfaa-storage.json"
        receipt.write_text(json.dumps(dict(volume_id="database", status="active",
                                           expires_at=stamp(self.clock() + 3600))))
        receipt.chmod(0o666)
        with self.assertRaisesRegex(dc.LaunchBlocked, "private"):
            self.c.launch(self.args())
        self.assertFalse(self.state()["jobs"])

    def test_malformed_storage_expiry_fails_closed_without_gpu_fallback(self):
        self.init()
        receipt = self.store.root / "rfaa-storage.json"
        receipt.write_text(json.dumps(dict(volume_id="database", status="active", expires_at="bad-date")))
        receipt.chmod(0o600)
        args = self.args()
        args.volume = ["database"]
        with self.assertRaisesRegex(dc.LaunchBlocked, "Invalid RFAA storage receipt"):
            self.c.launch(args)
        self.assertFalse(self.state()["jobs"])

    def test_storage_receipt_override_cannot_bypass_retirement_gate(self):
        self.init()
        receipt = self.store.root / "custom-storage.json"
        receipt.write_text(json.dumps(dict(volume_id="database", status="retiring",
                                           expires_at=stamp(self.clock() + 3600))))
        receipt.chmod(0o600)
        args = self.args()
        args.volume = ["database"]
        with patch.dict(os.environ, RFAA_STORAGE_RECEIPT=str(receipt)):
            with self.assertRaisesRegex(dc.LaunchBlocked, "expired or retiring"):
                self.c.launch(args)
        self.assertFalse(self.state()["jobs"])

    def test_persistent_colabfold_retirement_fences_launch_under_budget_lock(self):
        self.init()
        receipt = self.store.root / "msa-storage.json"
        data = dict(profile="colabfold", volume_id="database", status="retiring",
                    retention="persistent", expires_at=None)
        receipt.write_text(json.dumps(data))
        receipt.chmod(0o600)
        args = self.args()
        args.volume = ["shared", "database"]
        with self.assertRaisesRegex(dc.LaunchBlocked, "ColabFold.*retiring"):
            self.c.launch(args)
        self.assertFalse(self.state()["jobs"])
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))
        data["status"] = "active"
        receipt.write_text(json.dumps(data))
        self.c.launch(args)
        self.assertEqual(next(iter(self.state()["jobs"].values()))["volumes"], args.volume)

    def test_uncertain_database_allocation_blocks_unrelated_compute_until_reconciled(self):
        self.init()
        intent = self.store.root / "msa-storage-intent.json"
        data = dict(version=1, profile="colabfold", status="creating")
        for status in ("creating", "uncertain"):
            data["status"] = status
            intent.write_text(json.dumps(data))
            intent.chmod(0o600)
            with self.assertRaisesRegex(dc.LaunchBlocked, "Unresolved colabfold database allocation"):
                self.c.launch(self.args())
            self.assertFalse(self.state()["jobs"])
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))
        self.allocated_database("colabfold")
        self.c.launch(self.args())

    def test_allocated_database_omission_keeps_cost_and_blocks_compute_until_reappearance(self):
        row, intent = self.allocated_database()
        self.init()
        self.api.volumes.remove(row)
        self.clock.sleep(3600)
        self.c.watchdog()
        state = self.state()
        resource = state["resources"]["volume:" + row["id"]]
        self.assertTrue(resource["active"])
        self.assertAlmostEqual(resource["cost"], 1.8)
        self.assertAlmostEqual(dc.summary(state, self.clock())["background_hourly"], 1.06)
        self.assertEqual(state["unresolved_database_volumes"], [row["id"]])
        with self.assertRaisesRegex(dc.LaunchBlocked, "storage accounting is unresolved"):
            self.c.launch(self.args())
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))
        self.api.volumes.append(row)
        self.c.launch(self.args())
        self.assertEqual(self.state()["unresolved_database_volumes"], [])
        self.assertAlmostEqual(self.state()["resources"]["volume:" + row["id"]]["cost"], 1.8)

    def test_allocated_intent_recovers_storage_cost_if_budget_save_was_interrupted(self):
        row, _ = self.allocated_database()
        self.api.volumes.remove(row)
        self.init()
        resource = self.state()["resources"]["volume:" + row["id"]]
        self.assertTrue(resource["active"])
        self.assertAlmostEqual(resource["cost"], .9)
        with self.assertRaisesRegex(dc.LaunchBlocked, "storage accounting is unresolved"):
            self.c.launch(self.args())

    def test_storage_omission_does_not_prevent_existing_worker_cleanup(self):
        row, _ = self.allocated_database()
        self.init()
        ident = self.c.launch(self.args())
        self.api.volumes.remove(row)
        self.c.remove(ident)
        self.assertEqual(next(iter(self.state()["jobs"].values()))["status"], "closed")
        self.assertTrue(self.state()["resources"]["volume:" + row["id"]]["active"])
        self.assertFalse(any(item["id"] == ident for item in self.api.instances))

    def test_only_exact_completed_retirement_releases_missing_database_fence(self):
        row, intent = self.allocated_database("colabfold")
        self.init()
        self.api.volumes.remove(row)
        receipt = self.store.root / "msa-storage.json"
        data = dict(version=1, profile="colabfold", volume_id=row["id"], name=row["name"],
                    created_at=row["created_at"], status="retiring", retention="persistent", expires_at=None)
        for changes in ({}, {"status": "complete", "completed_at": stamp(self.clock()), "name": "other"},
                        {"status": "complete", "completed_at": stamp(self.clock()), "profile": "rfaa"}):
            receipt.write_text(json.dumps(data | changes))
            receipt.chmod(0o600)
            with self.assertRaises(dc.LaunchBlocked):
                self.c.launch(self.args())
            self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))
        data.update(status="complete", completed_at=stamp(self.clock()))
        receipt.write_text(json.dumps(data))
        self.c.launch(self.args())
        self.assertFalse(self.state()["resources"]["volume:" + row["id"]]["active"])
        self.assertEqual(self.state()["unresolved_database_volumes"], [])

    def test_observed_deleted_database_can_disappear_after_purge_without_false_fence(self):
        row, _ = self.allocated_database()
        self.init()
        self.api.volumes.remove(row)
        row.update(status="deleted", deleted_at=stamp(self.clock()))
        self.api.trash.append(row)
        self.c.watchdog()
        self.api.trash.clear()
        self.c.launch(self.args())
        self.assertFalse(self.state()["resources"]["volume:" + row["id"]]["active"])
        self.assertEqual(self.state()["unresolved_database_volumes"], [])

    def test_completed_retirement_recovers_historical_cost_without_prior_budget_snapshot(self):
        row, _ = self.allocated_database()
        self.api.volumes.remove(row)
        receipt = self.store.root / "rfaa-storage.json"
        receipt.write_text(json.dumps(dict(version=1, profile="rfaa", volume_id=row["id"], name=row["name"],
            created_at=row["created_at"], status="complete", completed_at=stamp(self.clock()),
            retention="persistent", expires_at=None)))
        receipt.chmod(0o600)
        self.init()
        resource = self.state()["resources"]["volume:" + row["id"]]
        self.assertFalse(resource["active"])
        self.assertAlmostEqual(resource["cost"], .9)
        self.c.launch(self.args())

    def test_allocated_intent_without_verified_identity_never_allows_compute(self):
        row, intent = self.allocated_database()
        del intent["verified"]
        path = self.store.root / "rfaa-storage-intent.json"
        path.write_text(json.dumps(intent))
        self.init()
        with self.assertRaisesRegex(dc.LaunchBlocked, "storage accounting is unresolved"):
            self.c.launch(self.args())
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))

    def test_corrupt_or_unsafe_allocation_intent_blocks_compute(self):
        self.init()
        intent = self.store.root / "rfaa-storage-intent.json"
        for contents, mode in (("{}", 0o600),
                               (json.dumps(dict(version=1, profile="rfaa", status="planned")), 0o666)):
            intent.write_text(contents)
            intent.chmod(mode)
            with self.assertRaisesRegex(dc.LaunchBlocked, "Invalid database allocation intent"):
                self.c.launch(self.args())
            self.assertFalse(self.state()["jobs"])

    def test_persistent_rfaa_and_timed_colabfold_are_independent(self):
        self.init()
        for filename, data in (
            ("rfaa-storage.json", dict(profile="rfaa", volume_id="rfaa-db", status="active",
                                       retention="persistent", expires_at=None)),
            ("msa-storage.json", dict(profile="colabfold", volume_id="msa-db", status="active",
                                      retention="timed", expires_at=stamp(self.clock()))),
        ):
            receipt = self.store.root / filename
            receipt.write_text(json.dumps(data))
            receipt.chmod(0o600)
        dc.check_storage_lifetime(self.store.root, ["rfaa-db"], self.clock())
        with self.assertRaisesRegex(dc.LaunchBlocked, "ColabFold.*expired"):
            dc.check_storage_lifetime(self.store.root, ["msa-db"], self.clock())

    def test_unknown_retention_or_conflicting_persistent_expiry_blocks_launch(self):
        self.init()
        receipt = self.store.root / "msa-storage.json"
        for extra in (dict(retention="forever", expires_at=None),
                      dict(retention="persistent", expires_at=stamp(self.clock() + 3600)),
                      dict(retention="persistent", expires_at=None, profile="rfaa")):
            with self.subTest(extra=extra):
                data = dict(profile="colabfold", volume_id="database", status="active")
                data.update(extra)
                receipt.write_text(json.dumps(data))
                receipt.chmod(0o600)
                with self.assertRaisesRegex(dc.LaunchBlocked, "Invalid ColabFold storage receipt"):
                    self.c.launch(self.args())
                self.assertFalse(self.state()["jobs"])
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))

    def test_colabfold_receipt_override_cannot_bypass_retirement_gate(self):
        self.init()
        receipt = self.store.root / "custom-msa-storage.json"
        receipt.write_text(json.dumps(dict(profile="colabfold", volume_id="database",
                                           status="retiring", retention="persistent", expires_at=None)))
        receipt.chmod(0o600)
        args = self.args()
        args.volume = ["database"]
        with patch.dict(os.environ, MSA_STORAGE_RECEIPT=str(receipt)):
            with self.assertRaisesRegex(dc.LaunchBlocked, "ColabFold.*retiring"):
                self.c.launch(args)
        self.assertFalse(self.state()["jobs"])

    def test_gc_purges_matching_closed_job_and_preserves_legacy_unmanaged_and_shared(self):
        token, disk = self.closed_trash()
        for ident, name, is_os, kind in (("old-os", "OS-NVMe-old", True, "NVMe"),
                                          ("unmanaged-os", "bio-os-" + "f" * 32, True, "NVMe"),
                                          ("data", "data", False, "NVMe_Shared")):
            other = volume(ident, self.clock())
            other.update(name=name, is_os_volume=is_os, type=kind, status="deleted", deleted_at=stamp(self.clock()))
            self.api.trash.append(other)
        with self.store.locked(self.clock()) as state:
            state["jobs"]["legacy-old"] = dict(state["jobs"][token], id="old", os_id="old-os")
        self.assertEqual(self.c.gc(), 1)
        self.assertEqual({row["id"] for row in self.api.trash}, {"old-os", "unmanaged-os", "data"})
        self.assertEqual({row["id"] for row in self.api.volumes}, {"os-head", "shared"})
        self.assertEqual(self.c.gc(), 0)
        self.assertIn("os_purged_at", self.state()["jobs"][token])
        self.assertEqual([path for method, path, body in self.api.calls
                          if method == "DELETE" and body["is_permanent"]], ["/volumes/" + disk["id"]])

    def test_gc_refuses_unproven_os_identity_or_attachment(self):
        token, disk = self.closed_trash()
        original = copy.deepcopy(disk)
        for changed in ({"name": "OS-NVMe-old"}, {"is_os_volume": False},
                        {"type": "NVMe_Shared"}, {"contract": "LONG_TERM"},
                        {"instance_id": "head"}, {"instances": [{"id": "head"}]},
                        {"instances": None}, {"deleted_at": None}, {"status": "detached"}):
            with self.subTest(changed=changed):
                disk.clear()
                disk.update(original, **changed)
                if changed.get("contract") == "LONG_TERM":
                    with self.assertRaisesRegex(dc.Error, "pay-as-you-go"):
                        self.c.gc()
                else:
                    self.assertEqual(self.c.gc(), 0)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.api.calls))
        self.assertNotIn("os_purged_at", self.state()["jobs"][token])

    def test_gc_refuses_duplicate_job_ownership_and_mismatched_recorded_id(self):
        token, disk = self.closed_trash()
        with self.store.locked(self.clock()) as state:
            state["jobs"]["f" * 32] = copy.deepcopy(state["jobs"][token])
        self.assertEqual(self.c.gc(), 0)
        with self.store.locked(self.clock()) as state:
            del state["jobs"]["f" * 32]
            state["jobs"][token]["os_id"] = "different-disk"
        self.assertEqual(self.c.gc(), 0)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.api.calls))

    def test_gc_refuses_open_job_live_instance_or_restored_volume(self):
        token, disk = self.closed_trash()
        with self.store.locked(self.clock()) as state:
            state["jobs"][token]["status"] = "cleanup"
        self.assertEqual(self.c.gc(), 0)
        with self.store.locked(self.clock()) as state:
            state["jobs"][token]["status"] = "closed"
        self.api.instances.append(instance(FakeAPI.ID, self.clock()))
        self.assertEqual(self.c.gc(), 0)
        self.api.instances.pop()
        restored = copy.deepcopy(disk)
        restored.update(status="detached")
        restored.pop("deleted_at")
        self.api.volumes.append(restored)
        self.assertEqual(self.c.gc(), 0)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.api.calls))

    def test_gc_unconfirmed_permanent_delete_does_not_claim_success_and_can_retry(self):
        token, disk = self.closed_trash()
        self.api.purge_lands = False
        with self.assertRaisesRegex(dc.Error, "Permanent removal.*unconfirmed"):
            self.c.gc()
        self.assertNotIn("os_purged_at", self.state()["jobs"][token])
        self.assertTrue(self.api.trash)
        self.api.purge_lands = True
        self.assertEqual(self.c.gc(), 1)
        self.assertFalse(self.api.trash)

    def test_concurrent_purge_already_deleted_response_requires_inventory_confirmation(self):
        self.closed_trash()
        original = self.api.request
        def request(method, path, body=None):
            result = original(method, path, body)
            if method == "DELETE" and body.get("is_permanent"):
                raise dc.APIError(method, path, 400, "invalid_request; Volume is already permanently deleted")
            return result
        self.api.request = request
        self.assertEqual(self.c.gc(), 1)
        self.assertFalse(self.api.trash)
        self.assertIn("os_purged_at", next(iter(self.state()["jobs"].values())))

    def test_already_deleted_response_alone_is_not_purge_confirmation(self):
        self.closed_trash()
        original = self.api.request
        def request(method, path, body=None):
            if method == "DELETE" and body.get("is_permanent"):
                raise dc.APIError(method, path, 400, "invalid_request; Volume is already permanently deleted")
            return original(method, path, body)
        self.api.request = request
        with self.assertRaisesRegex(dc.Error, "Permanent removal.*unconfirmed"):
            self.c.gc()
        self.assertTrue(self.api.trash)
        self.assertNotIn("os_purged_at", next(iter(self.state()["jobs"].values())))

    def test_normal_cleanup_retains_reservation_until_managed_os_purge_confirmed(self):
        self.init()
        ident = self.c.launch(self.args())
        self.api.purge_lands = False
        with self.assertRaisesRegex(dc.Error, "Permanent removal.*unconfirmed"):
            self.c.remove(ident)
        self.assertEqual(next(iter(self.state()["jobs"].values()))["status"], "cleanup")
        with self.assertRaisesRegex(dc.LaunchBlocked, "Unresolved"):
            self.c.launch(self.args())
        self.api.purge_lands = True
        self.c.remove(ident)
        self.assertEqual(next(iter(self.state()["jobs"].values()))["status"], "closed")

    def test_normal_cleanup_retains_soft_trash_when_os_identity_not_proven(self):
        self.init()
        ident = self.c.launch(self.args())
        next(row for row in self.api.volumes if row["id"] == "os-" + ident)["name"] = "legacy-os-name"
        self.c.remove(ident)
        self.assertEqual(next(iter(self.state()["jobs"].values()))["status"], "closed")
        self.assertEqual(next(iter(self.state()["jobs"].values()))["cleanup_confirmed_at"], self.clock())
        self.assertEqual([row["id"] for row in self.api.trash], ["os-" + ident])
        self.assertFalse(any(method == "DELETE" and body["is_permanent"] for method, _, body in self.api.calls))

    def test_watchdog_kills_expired_job_not_head(self):
        self.init()
        self.c.launch(self.args())
        self.clock.sleep(3601)
        self.c.watchdog()
        self.assertEqual([r["id"] for r in self.api.instances], ["head"])

    def test_watchdog_requests_all_ten_deletes_before_bounded_confirmation(self):
        self.init()
        identifiers = [self.c.launch(self.args()) for _ in range(10)]
        self.clock.sleep(3601)
        confirm = self.c.confirm_remove
        initial_confirmations = threading.Barrier(4, timeout=10)
        monitor = threading.Lock()
        active = maximum = entered = 0

        def held_confirmation(ident, token, os_id):
            nonlocal active, maximum, entered
            deletes = [body["id"] for method, _, body in self.api.calls if method == "PUT"]
            self.assertCountEqual(deletes, identifiers)
            with monitor:
                active += 1
                entered += 1
                position = entered
                maximum = max(maximum, active)
            try:
                if position <= 4:
                    initial_confirmations.wait()
                return confirm(ident, token, os_id)
            finally:
                with monitor:
                    active -= 1

        self.c.confirm_remove = held_confirmation
        self.c.watchdog()
        self.assertEqual(maximum, 4)
        self.assertEqual(entered, 10)
        self.assertEqual([row["id"] for row in self.api.instances], ["head"])
        self.assertEqual({row["id"] for row in self.api.volumes}, {"os-head", "shared"})
        self.assertEqual(self.api.trash, [])
        self.assertEqual({job["status"] for job in self.state()["jobs"].values()}, {"closed"})

    def test_watchdog_one_delete_rejection_does_not_starve_other_targets(self):
        self.init()
        identifiers = [self.c.launch(self.args()) for _ in range(10)]
        self.clock.sleep(3601)
        original = self.api.request

        def reject_one(method, path, body=None):
            if method == "PUT" and path == "/instances" and body["id"] == identifiers[0]:
                self.api.calls.append((method, path, body))
                raise dc.APIError(method, path, 429, "retry after 5s")
            return original(method, path, body)

        self.api.request = reject_one
        with self.assertRaisesRegex(dc.Error, "429.*retry after 5s"):
            self.c.watchdog()
        self.assertCountEqual([body["id"] for method, _, body in self.api.calls if method == "PUT"], identifiers)
        self.assertEqual({row["id"] for row in self.api.instances}, {"head", identifiers[0]})
        jobs = {job["id"]: job for job in self.state()["jobs"].values()}
        self.assertEqual(jobs[identifiers[0]]["status"], "cleanup")
        self.assertEqual({jobs[ident]["status"] for ident in identifiers[1:]}, {"closed"})
        self.assertTrue(dc.summary(self.state(), self.clock())["uncertain"])

    def test_watchdog_inventory_outage_still_requests_all_ten_deletes(self):
        self.init()
        identifiers = [self.c.launch(self.args()) for _ in range(10)]
        self.clock.sleep(3601)
        self.api.inventory_error = True
        with self.assertRaisesRegex(dc.Error, "inventory unavailable"):
            self.c.watchdog()
        self.assertCountEqual([body["id"] for method, _, body in self.api.calls if method == "PUT"], identifiers)
        self.assertEqual([row["id"] for row in self.api.instances], ["head"])
        self.assertEqual({job["status"] for job in self.state()["jobs"].values()}, {"cleanup"})

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "Head cleanup uses POSIX process locks")
    def test_ten_independent_processes_share_four_confirmation_slots(self):
        self.init()
        context = multiprocessing.get_context("fork")
        start, release = context.Event(), context.Event()
        arrivals = context.Queue()
        active, maximum = context.Value("i", 0), context.Value("i", 0)

        def confirm(index):
            store = dc.Store(self.tmp.name)
            if not start.wait(10):
                raise AssertionError("Confirmation start was not released")
            with store.confirming("process-" + str(index)), store.confirmation_slot():
                with active.get_lock():
                    active.value += 1
                    maximum.value = max(maximum.value, active.value)
                arrivals.put(index)
                if not release.wait(10):
                    raise AssertionError("Confirmation was not released")
                with active.get_lock():
                    active.value -= 1

        children = [context.Process(target=confirm, args=(index,)) for index in range(10)]
        for child in children:
            child.start()
        start.set()
        try:
            first = [arrivals.get(timeout=10) for _ in range(4)]
            self.assertEqual(len(set(first)), 4)
            with self.assertRaises(queue.Empty):
                arrivals.get(timeout=.2)
            # Neither occupied slots nor waiting contenders hold budget.lock.
            with self.store.locked(self.clock()) as state:
                self.assertIn("last_watchdog", state)
        finally:
            release.set()
            for child in children:
                child.join(timeout=10)
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
        self.assertEqual([child.exitcode for child in children], [0] * 10)
        self.assertEqual(maximum.value, 4)
        self.assertEqual(len({*first, *(arrivals.get(timeout=5) for _ in range(6))}), 10)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "Head cleanup uses POSIX process locks")
    def test_ten_processes_deduplicate_one_confirmed_cleanup(self):
        self.init()
        ident = self.c.launch(self.args())
        token, os_id = self.c.request_remove(ident)
        context = multiprocessing.get_context("fork")
        start, entered, release = context.Event(), context.Event(), context.Event()
        confirmations = context.Value("i", 0)

        def confirm():
            controller = dc.Controller(self.api, dc.Store(self.tmp.name), self.clock, time.sleep)
            def complete(_ident, _token, _os_id):
                with confirmations.get_lock():
                    confirmations.value += 1
                entered.set()
                if not release.wait(10):
                    raise AssertionError("Confirmation was not released")
                with controller.store.locked(self.clock()) as state:
                    state["jobs"][token]["status"] = "closed"
            controller._confirm_remove = complete
            if not start.wait(10):
                raise AssertionError("Confirmation start was not released")
            controller.confirm_remove(ident, token, os_id)

        children = [context.Process(target=confirm) for _ in range(10)]
        for child in children:
            child.start()
        start.set()
        try:
            self.assertTrue(entered.wait(10))
            with self.store.locked(self.clock()) as state:
                self.assertEqual(state["jobs"][token]["status"], "cleanup")
        finally:
            release.set()
            for child in children:
                child.join(timeout=10)
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
        self.assertEqual([child.exitcode for child in children], [0] * 10)
        self.assertEqual(confirmations.value, 1)
        self.assertEqual(self.state()["jobs"][token]["status"], "closed")
        calls = len(self.api.calls)
        self.c.remove(ident)
        self.assertEqual(len(self.api.calls), calls)

    def test_confirmation_refuses_changed_owned_identity_before_provider_reads(self):
        self.init()
        ident = self.c.launch(self.args())
        token, os_id = self.c.request_remove(ident)
        calls = len(self.api.calls)
        with self.assertRaisesRegex(dc.Error, "identity changed"):
            self.c.confirm_remove(ident, token, "another-os")
        self.assertEqual(len(self.api.calls), calls)

    def test_watchdog_budget_cutoff_accounts_for_storage(self):
        self.init()
        self.c.launch(self.args())
        self.c.ceiling = 12
        self.c.watchdog()
        self.assertEqual([r["id"] for r in self.api.instances], ["head"])

    def test_unmanaged_instance_cannot_be_removed(self):
        self.init()
        with self.assertRaisesRegex(dc.Error, "unmanaged"):
            self.c.remove("head")
        self.assertFalse(any(m == "PUT" for m, _, _ in self.api.calls))

    def test_corrupt_state_does_not_reset_spending(self):
        self.store.path.write_text("broken")
        with self.assertRaisesRegex(dc.Error, "corrupt"):
            self.c.launch(self.args())
        self.assertEqual(self.store.path.read_text(), "broken")


class APIErrorTests(unittest.TestCase):
    def test_create_accepts_only_json_or_strict_plain_uuid(self):
        ident = FakeAPI.ID
        for path in ("/instances", "/volumes"):
            for body in (ident.encode(), ("\n" + ident + "\n").encode(), json.dumps(ident).encode()):
                with self.subTest(body=body, path=path):
                    self.assertEqual(dc.response_value(body, "POST", path, 202), ident)
        for body, method, path in ((b"not-a-uuid", "POST", "/instances"),
                                   (b"\xff" + ident.encode(), "POST", "/instances"),
                                   (ident.encode(), "GET", "/instances"),
                                   (ident.encode(), "GET", "/volumes"),
                                   (b"not-a-uuid", "POST", "/volumes"),
                                   (ident.encode(), "POST", "/oauth2/token")):
            with self.subTest(body=body, method=method, path=path):
                with self.assertRaisesRegex(dc.APIError, "HTTP 202.*not valid JSON"):
                    dc.response_value(body, method, path, 202)

    def test_error_extracts_diagnostics_redacts_secrets_and_ignores_other_fields(self):
        body = json.dumps({"code": "invalid_request", "message": [
            "Unsupported os_volume policy; client secret example-secret",
            "Bearer access-example; password=other-secret\nsecond line"],
            "jupyter_token": "never-display-this", "details": {"raw": "also-private"}}).encode()
        api = dc.API()
        api.token = "access-example"
        api.token_expires = float("inf")
        failure = dc.urllib.error.HTTPError(api.base + "/instances", 400, "Bad request", {}, io.BytesIO(body))
        with patch.dict(os.environ, DATACRUNCH_CLIENT_SECRET="example-secret"), \
             patch.object(dc.urllib.request, "urlopen", side_effect=failure):
            with self.assertRaises(dc.APIError) as caught:
                api.request("POST", "/instances", {})
        message = str(caught.exception)
        self.assertIn("HTTP 400", message)
        self.assertIn("Unsupported os_volume policy", message)
        for secret in ("example-secret", "access-example", "other-secret", "never-display-this", "also-private"):
            self.assertNotIn(secret, message)
        self.assertNotIn("\n", message)

    def test_diagnostics_are_bounded_and_do_not_dump_non_json_responses(self):
        self.assertLessEqual(len(dc.error_detail(json.dumps({"message": "a" * 5000}))), 300)
        self.assertEqual(dc.error_detail(b"<html>private proxy error</html>"), "")
        self.assertEqual(dc.error_detail(json.dumps({"access_token": "private"})), "")

    def test_rate_limit_exposes_bounded_retry_delay_without_repeating_request(self):
        api = dc.API()
        api.token, api.token_expires = "test-token", float("inf")
        for method, path in (("POST", "/instances"), ("PUT", "/instances"), ("GET", "/volumes")):
            with self.subTest(method=method):
                failure = dc.urllib.error.HTTPError(api.base + path, 429, "Too Many Requests",
                    {"Retry-After": "49"}, io.BytesIO(b'{"code":"rate_limit_exceeded"}'))
                with patch.object(dc.urllib.request, "urlopen", side_effect=failure) as request:
                    with self.assertRaisesRegex(dc.APIError, "429.*retry after 49s"):
                        api.request(method, path, {} if method != "GET" else None)
                self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
