"""Offline allocation tests; all provider requests are fake and recordable."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import runpy
import tempfile
import threading
import unittest
from unittest.mock import patch

DIRECTORY = Path(__file__).parent
dc = runpy.run_path(str(DIRECTORY / "dc-budget.py"))
s = runpy.run_path(str(DIRECTORY / "rfaa/storage.py"))
spec = importlib.util.spec_from_file_location("database_volume", DIRECTORY / "database-volume.py")
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)
SHARED = "b8b3b446-e464-44dd-9e01-6402489f8c5a"
FIRST = "01234567-89ab-4cde-8fab-0123456789ab"


class API:
    def __init__(self, clock):
        self.clock = clock
        self.instances = [dict(id=s["HEAD_ID"], instance_type="CPU.4V.16G", status="running",
                               price_per_hour=.048, created_at=s["stamp"](clock()-3600),
                               volume_ids=[SHARED], currency="usd", contract="PAY_AS_YOU_GO")]
        self.volumes = [dict(id=SHARED, name="bio-shared", type="NVMe_Shared", size=100,
                            is_os_volume=False, status="exported", base_hourly_cost=.03,
                            created_at=s["stamp"](clock()-3600), currency="usd", contract="PAY_AS_YOU_GO", tags=[])]
        self.trash, self.calls = [], []
        self.error, self.lands, self.visible = None, False, True
        self.bad_reply = False
        self.before_post = lambda body: None
        self.next_id = 0

    def inventory(self):
        rows = self.volumes if self.visible else self.volumes[:1]
        return copy.deepcopy((self.instances, rows, self.trash))

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET" and path == "/volume-types?currency=usd":
            return [{"type": "NVMe_Shared", "price": {"currency": "usd", "cps_per_gb": .2/730/3600}}]
        if method == "POST" and path == "/volumes":
            self.before_post(body)
            if self.error and not self.lands:
                raise self.error
            import uuid
            ident = str(uuid.UUID(int=uuid.UUID(FIRST).int+self.next_id))
            self.next_id += 1
            row = dict(id=ident, name=body["name"], type=body["type"], size=body["size"],
                       location=body["location_code"], tags=copy.deepcopy(body["tags"]),
                       is_os_volume=False, contract="PAY_AS_YOU_GO", currency="usd", status="exported",
                       created_at=s["stamp"](self.clock()), base_hourly_cost=body["size"]*.2/730,
                       monthly_price=body["size"]*.2, pseudo_path="/"+body["name"],
                       mount_command=f'mount -t nfs nfs.fin-02.datacrunch.io:/{body["name"]} /mnt/test',
                       instances=[{"id": s["HEAD_ID"]}])
            self.volumes.append(row)
            self.instances[0]["volume_ids"].append(ident)
            if self.error:
                raise self.error
            return "not-an-id" if self.bad_reply else ident
        raise AssertionError((method, path, body))


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 100000
        self.clock = lambda: self.now
        self.env = patch.dict(os.environ, {"DC_BUDGET_CEILING": "500", "DC_BUDGET_MARGIN": "10",
                            "DC_PERSISTENT_RESERVE_HOURS": "24", "DC_STATE_DIR": str(self.root)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = API(self.clock)
        self.c = self.controller()
        state = dc["new_state"](self.root / "absent-ledger", self.clock(), "0")
        state["last_watchdog"] = self.clock()
        self.c.accounting.store.save(state)

    def controller(self):
        return v.Controller(self.api, self.root, dc, s, clock=self.clock, owner=os.getuid())

    def posts(self):
        return [call for call in self.api.calls if call[0] == "POST"]

    def test_plan_is_read_only_with_fixed_request_private_intent_and_complete_quote(self):
        result = self.c.execute("plan", "rfaa")
        self.assertEqual(result["status"], "planned")
        self.assertEqual(self.posts(), [])
        self.assertEqual(result["request"]["size"], 3300)
        self.assertEqual(result["request"]["instance_ids"], [s["HEAD_ID"]])
        self.assertEqual(result["name"], "bio-rfaa-db-"+result["token"])
        self.assertEqual(self.c.stores["rfaa"].path.stat().st_mode & 0o777, 0o600)
        quote = result["quote"]
        self.assertEqual(quote["reserve_hours"], 24)
        self.assertGreater(quote["new_reserve"], 21)
        self.assertGreater(quote["background_reserve"], 1)
        self.assertEqual(quote["margin"], 10)
        self.assertTrue(quote["allowed"])
        self.assertFalse(s["receipt_path"]("rfaa", self.root).exists())

    def test_create_both_profiles_preserves_existing_resources_and_returns_verified_exports(self):
        protected = copy.deepcopy(self.api.volumes[0])
        for profile, size in (("rfaa", 3300), ("colabfold", 3000)):
            result = self.c.execute("create", profile)
            self.assertEqual(result["status"], "allocated")
            self.assertEqual(result["verified"]["size"], size)
            self.assertEqual(result["verified"]["id"], result["volume_id"])
            self.assertEqual(result["verified"]["nfs"], "nfs.fin-02.datacrunch.io:/"+result["name"])
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(self.api.volumes[0], protected)
        self.assertTrue(all(method in {"GET", "POST"} for method, _, _ in self.api.calls))
        self.assertGreater(self.c.read("colabfold")["quote"]["background_reserve"], 22)

    def test_durable_creating_marker_precedes_post_and_new_launch_cannot_take_budget_lock(self):
        blocked = threading.Event()
        release = threading.Event()
        thread = None
        def post(body):
            nonlocal thread
            self.assertEqual(self.c.read("rfaa")["status"], "creating")
            def competing_launch():
                blocked.set()
                with self.c.accounting.store.locked(self.clock()):
                    release.set()
            thread = threading.Thread(target=competing_launch)
            thread.start()
            self.assertTrue(blocked.wait(1))
            self.assertFalse(release.wait(.05))
        self.api.before_post = post
        self.c.execute("create", "rfaa")
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(release.is_set())

    def test_concurrent_create_commands_are_serialized_and_allocate_once(self):
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        results, errors = [], []
        def post(body):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release creation")
        self.api.before_post = post
        def create(second=False):
            try:
                if second:
                    second_started.set()
                results.append(self.controller().execute("create", "rfaa")["status"])
            except Exception as exc:
                errors.append(exc)
        first = threading.Thread(target=create)
        second = threading.Thread(target=create, args=(True,))
        first.start()
        self.assertTrue(entered.wait(3))
        second.start()
        self.assertTrue(second_started.wait(3))
        release.set()
        first.join(3)
        second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, ["allocated", "allocated"])
        self.assertEqual(len(self.posts()), 1)

    def test_timeout_after_creation_is_adopted_on_restart_without_another_post(self):
        self.api.error, self.api.lands = TimeoutError("response lost"), True
        result = self.c.execute("create", "rfaa")
        self.assertEqual(result["status"], "uncertain")
        self.api.error = None
        result = self.controller().execute("create", "rfaa")
        self.assertEqual(result["status"], "allocated")
        self.assertEqual(len(self.posts()), 1)

    def test_ambiguous_request_without_visible_resource_never_reposts_or_allows_other_profile(self):
        self.api.error = TimeoutError("request outcome unknown")
        self.c.execute("create", "rfaa")
        self.api.error = None
        for command in ("create", "reconcile", "create"):
            self.assertEqual(self.controller().execute(command, "rfaa")["status"], "uncertain")
        with self.assertRaisesRegex(v.Error, "Unresolved rfaa"):
            self.c.execute("create", "colabfold")
        self.assertEqual(len(self.posts()), 1)

    def test_invalid_reply_and_provider_lag_both_require_reconciliation(self):
        self.api.bad_reply, self.api.visible = True, False
        self.assertEqual(self.c.execute("create", "rfaa")["status"], "uncertain")
        self.api.bad_reply = False
        self.assertEqual(self.c.execute("create", "rfaa")["status"], "uncertain")
        self.api.visible = True
        self.assertEqual(self.c.execute("reconcile", "rfaa")["status"], "allocated")
        self.assertEqual(len(self.posts()), 1)

    def test_acknowledged_id_stays_creating_until_inventory_export_is_ready(self):
        self.api.visible = False
        result = self.c.execute("create", "colabfold")
        self.assertEqual(result["status"], "creating")
        self.assertEqual(result["volume_id"], FIRST)
        self.api.visible = True
        row = self.api.volumes[-1]
        command = row.pop("mount_command")
        self.assertEqual(self.c.execute("reconcile", "colabfold")["status"], "creating")
        row["mount_command"] = command
        self.assertEqual(self.c.execute("reconcile", "colabfold")["status"], "allocated")
        self.assertEqual(len(self.posts()), 1)

    def test_definitive_quota_rejection_can_retry_only_after_fresh_checks(self):
        self.api.error = dc["APIError"]("POST", "/volumes", 400, "Storage limit exceeded")
        result = self.c.execute("create", "rfaa")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(len(self.api.volumes), 1)
        self.api.error = None
        self.now += 181
        with self.assertRaisesRegex(v.Error, "watchdog"):
            self.c.execute("create", "rfaa")
        self.assertEqual(len(self.posts()), 1)
        with self.c.accounting.store.locked(self.clock()) as state:
            state["last_watchdog"] = self.clock()
        result = self.c.execute("create", "rfaa")
        self.assertEqual(result["status"], "allocated")
        self.assertEqual(len(self.posts()), 2)

    def test_budget_uncertain_jobs_and_stale_watchdog_block_before_post(self):
        for condition in ("ceiling", "uncertain", "watchdog"):
            with self.subTest(condition=condition):
                with self.c.accounting.store.locked(self.clock()) as state:
                    state["historical_correction"] = 490 if condition == "ceiling" else 0
                    state["last_watchdog"] = 0 if condition == "watchdog" else self.clock()
                    state["jobs"] = {} if condition != "uncertain" else {
                        "pending": dict(id=None, status="uncertain", created=self.clock(), deadline=self.clock()+3600,
                                        rate=1, os_rate=.1, os_id=None)}
                with self.assertRaisesRegex(v.Error, "Allocation blocked"):
                    self.c.execute("create", "rfaa")
        self.assertEqual(self.posts(), [])

    def test_existing_reservations_are_included_in_full_budget_projection(self):
        with self.c.accounting.store.locked(self.clock()) as state:
            state["jobs"]["running"] = dict(id="worker", status="running", created=self.clock(),
                                           deadline=self.clock()+86400, rate=20, os_rate=0, os_id=None)
        with self.assertRaisesRegex(v.Error, "budget ceiling"):
            self.c.execute("create", "colabfold")
        self.assertEqual(self.posts(), [])

    def test_allocation_quote_never_uses_less_than_ten_dollars_safety_margin(self):
        self.c.accounting.margin = 0
        result = self.c.execute("plan", "colabfold")
        self.assertEqual(result["quote"]["margin"], 10)

    def test_missing_prior_allocation_blocks_second_profile_and_retains_its_budget_reserve(self):
        self.c.execute("create", "rfaa")
        self.api.visible = False
        result = self.c.execute("plan", "colabfold")
        self.assertFalse(result["quote"]["allowed"])
        self.assertTrue(result["quote"]["storage_uncertain"])
        self.assertGreater(result["quote"]["background_reserve"], 23)
        with self.assertRaisesRegex(v.Error, "storage accounting is unresolved"):
            self.c.execute("create", "colabfold")
        self.assertEqual(len(self.posts()), 1)
        self.api.visible = True
        self.assertEqual(self.c.execute("create", "colabfold")["status"], "allocated")
        self.assertEqual(len(self.posts()), 2)

    def test_confirmed_retirement_allows_other_profile_without_charging_retired_volume(self):
        result = self.c.execute("create", "rfaa")
        self.api.volumes.pop()
        receipt = s["Store"](s["receipt_path"]("rfaa", self.root), owner=os.getuid())
        receipt.save(dict(version=1, profile="rfaa", status="complete", volume_id=result["volume_id"],
                          name=result["name"], created_at=result["verified"]["created_at"],
                          completed_at=s["stamp"](self.clock()), retention="persistent", expires_at=None))
        plan = self.c.execute("plan", "colabfold")
        self.assertTrue(plan["quote"]["allowed"])
        self.assertFalse(plan["quote"]["storage_uncertain"])
        self.assertLess(plan["quote"]["background_reserve"], 2)
        self.assertEqual(self.c.execute("create", "colabfold")["status"], "allocated")

    def test_matching_trash_or_multiple_matches_never_creates_replacement(self):
        self.c.execute("create", "rfaa")
        row = self.api.volumes.pop()
        row.update(status="deleted", deleted_at=s["stamp"](self.clock()))
        self.api.trash.append(row)
        with self.assertRaisesRegex(v.Error, "trashed"):
            self.c.execute("create", "rfaa")
        other = copy.deepcopy(row)
        other["id"] = "11234567-89ab-4cde-8fab-0123456789ab"
        self.api.trash.append(other)
        with self.assertRaisesRegex(v.Error, "Multiple provider"):
            self.c.execute("reconcile", "rfaa")
        self.assertEqual(len(self.posts()), 1)

    def test_exact_name_with_wrong_tags_or_protected_reply_is_never_adopted(self):
        result = self.c.execute("create", "rfaa")
        self.api.volumes[-1]["tags"] = [{"key": "purpose", "value": "unrelated"}]
        with self.assertRaisesRegex(v.Error, "name/token/tags"):
            self.c.execute("create", "rfaa")
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.c.read("rfaa")["status"], "uncertain")
        self.api.volumes[-1]["tags"] = result["request"]["tags"]
        self.api.volumes[-1]["id"] = SHARED
        with self.assertRaisesRegex(s["Error"], "protected"):
            self.c.execute("reconcile", "rfaa")

    def test_unsafe_intent_and_existing_operational_receipt_fail_before_post(self):
        self.c.execute("plan", "rfaa")
        path = self.c.stores["rfaa"].path
        path.chmod(0o644)
        with self.assertRaisesRegex(s["Error"], "owned by root"):
            self.c.execute("create", "rfaa")
        path.chmod(0o600)
        receipt = s["Store"](s["receipt_path"]("rfaa", self.root), owner=os.getuid())
        receipt.save({"existing": "registered allocation"})
        with self.assertRaisesRegex(v.Error, "operational receipt"):
            self.c.execute("create", "rfaa")
        self.assertEqual(self.posts(), [])

    def test_no_intent_reconciliation_is_read_only_and_refuses_to_invent_one(self):
        with self.assertRaisesRegex(v.Error, "No allocation intent"):
            self.c.execute("reconcile", "rfaa")
        self.assertFalse(self.c.stores["rfaa"].path.exists())
        self.assertEqual(self.api.calls, [])


if __name__ == "__main__":
    unittest.main()
