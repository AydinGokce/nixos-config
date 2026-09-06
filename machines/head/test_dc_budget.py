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
import os
from pathlib import Path
import tempfile
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
                instance_id=None, **extra)


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
        self.inventory_error = False

    def inventory(self):
        if self.inventory_error:
            raise dc.Error("inventory unavailable")
        return copy.deepcopy((self.instances, self.volumes, self.trash))

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path == "/instance-types":
            return [{"instance_type": "GPU", "price_per_hour": "2", "spot_price": "1", "currency": "usd"}]
        if method == "GET" and path == "/volume-types":
            return [{"type": "NVMe", "price": {"currency": "usd", "cps_per_gb": .01 / 3600 / 50}}]
        if method == "GET" and path == "/sshkeys":
            return [{"id": "key"}]
        if method == "POST" and path == "/instances":
            if self.post_error and not self.land_before_error:
                raise self.post_error
            row = instance(self.ID, self.clock())
            row.update(description=body["description"], created_at=stamp(self.clock()), status=self.provisioning_status)
            self.instances.append(row)
            disk = volume("os-" + self.ID, self.clock())
            disk["created_at"] = stamp(self.clock())
            self.volumes.append(disk)
            if self.post_error:
                raise self.post_error
            return self.ID
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
            disk = next(r for r in self.volumes if r["id"] == path.rsplit("/", 1)[1])
            disk.update(deleted_at=stamp(self.clock()), status="deleted")
            self.trash.append(disk)
            self.volumes.remove(disk)
            return None
        raise AssertionError((method, path))


class BudgetTests(unittest.TestCase):
    def setUp(self):
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

    def test_launch_and_delete_preserve_shared_volume_and_confirm(self):
        self.init()
        ident = self.c.launch(self.args())
        self.clock.sleep(300)
        self.c.remove(ident)
        deletion = next(b for m, p, b in self.api.calls if m == "PUT")
        self.assertEqual(deletion["volume_ids"], ["os-" + ident])
        self.assertIn("shared", [v["id"] for v in self.api.volumes])
        self.assertEqual({j["status"] for j in self.state()["jobs"].values()}, {"closed"})
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
        self.assertEqual([p for m, p, _ in self.api.calls if m == "DELETE"], ["/volumes/os-" + ident])
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
        self.assertEqual([path for method, path, _ in self.api.calls if method == "DELETE"], ["/volumes/os-" + ident])
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

    def test_watchdog_kills_expired_job_not_head(self):
        self.init()
        self.c.launch(self.args())
        self.clock.sleep(3601)
        self.c.watchdog()
        self.assertEqual([r["id"] for r in self.api.instances], ["head"])

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
        for body in (ident.encode(), ("\n" + ident + "\n").encode(), json.dumps(ident).encode()):
            with self.subTest(body=body):
                self.assertEqual(dc.response_value(body, "POST", "/instances", 202), ident)
        for body, method, path in ((b"not-a-uuid", "POST", "/instances"),
                                   (b"\xff" + ident.encode(), "POST", "/instances"),
                                   (ident.encode(), "GET", "/instances"),
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


if __name__ == "__main__":
    unittest.main()
