#!/usr/bin/env python3
"""Head ownership, spending guard and exclusive lease for the full MSA cache.

Only create can POST one new, detached volume. No operation deletes, formats,
resizes, mounts or attaches storage. Compute attaches it through ordinary dc
launch, using the returned worker_name and durable cache lease identity.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import runpy
import sys
import time
import uuid

PROFILE = "msa-block-cache"
SIZE_GIB = 1300
GIB = 1 << 30
LOCATION = "FIN-02"
INTENT = "msa-block-cache-intent.json"
LEASE = "msa-block-cache-lease.json"
LOCK = "msa-block-cache.lock"


class Error(ValueError):
    pass


def require(value, message):
    if not value:
        raise Error(message)


def hex_id(value, size=64):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % size, value),
            "Invalid cache identity or SHA256")
    return value


def identifier(value):
    try:
        require(isinstance(value, str) and str(uuid.UUID(value)) == value,
                "A canonical resource UUID is required")
    except (ValueError, AttributeError, TypeError):
        raise Error("A canonical resource UUID is required") from None
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def generation(cache_id, manifest, receipts):
    return digest(dict(schema=1, cache_id=hex_id(cache_id, 32),
                       source_manifest_sha256=hex_id(manifest),
                       source_receipt_sha256=hex_id(receipts)))


def tags(row):
    values = row.get("tags")
    require(isinstance(values, list), "Provider volume tags are unavailable")
    result = {}
    for item in values:
        require(isinstance(item, dict) and isinstance(item.get("key"), str)
                and isinstance(item.get("value"), str) and item["key"] not in result,
                "Provider volume tags are ambiguous")
        result[item["key"]] = item["value"]
    return result


def attachments(row, inventory):
    """For NVMe, all three provider attachment representations must be known."""
    require("instance_id" in row and isinstance(row.get("instances"), list),
            "Provider block-volume attachment metadata is unavailable")
    attached = set()
    if row["instance_id"] is not None:
        attached.add(identifier(row["instance_id"]))
    for item in row["instances"]:
        attached.add(identifier(item.get("id") if isinstance(item, dict) else item))
    for instance in inventory[0]:
        volumes = instance.get("volume_ids")
        require(isinstance(volumes, list) and all(isinstance(x, str) for x in volumes),
                "Provider instance volume references are unavailable")
        if row["id"] in volumes or instance.get("os_volume_id") == row["id"]:
            attached.add(identifier(instance.get("id")))
    return attached


class Controller:
    def __init__(self, api, root, budget, storage, *, clock=time.time, owner=None):
        self.api, self.root, self.budget, self.storage, self.clock = api, Path(root), budget, storage, clock
        self.owner = os.geteuid() if owner is None else owner
        self.accounting = budget["Controller"](api, budget["Store"](root), clock=clock)
        self.intent = storage["Store"](self.root / INTENT, owner=self.owner)
        self.active = storage["Store"](self.root / LEASE, owner=self.owner)
        self.operation = storage["Store"](self.root / "msa-block-cache-operation.json",
                                           lock=self.root / LOCK, owner=self.owner)

    def lease_store(self, lease_id):
        return self.storage["Store"](self.root / ("msa-block-cache-lease-" + hex_id(lease_id, 32) + ".json"),
                                       owner=self.owner)

    @staticmethod
    def payload(token):
        return dict(type="NVMe", location_code=LOCATION, size=SIZE_GIB,
                    name="bio-msa-cache-" + token, instance_ids=[], tags=[
                        {"key": "purpose", "value": PROFILE},
                        {"key": "allocation-token", "value": token},
                        {"key": "retention", "value": "persistent"}])

    def validate(self, value):
        require(isinstance(value, dict) and type(value.get("version")) is int
                and value["version"] == 1 and value.get("profile") == PROFILE,
                "Invalid cache allocation intent")
        require(value.get("status") in {"planned", "creating", "uncertain", "allocated", "rejected"},
                "Invalid cache allocation state")
        token = hex_id(value.get("token"), 32)
        require(value.get("cache_id") == token and value.get("request") == self.payload(token)
                and value.get("name") == self.payload(token)["name"], "Cache allocation request changed")
        identifier(value.get("filesystem_uuid"))
        require(value.get("cache_generation") == generation(token, value.get("source_manifest_sha256"),
                                                              value.get("source_receipt_sha256")),
                "Cache generation differs from the pinned source")
        self.storage["utc"](value.get("planned_at"))
        for key in ("volume_id", "response_id"):
            if value.get(key) is not None:
                identifier(value[key])
        if value.get("ready_receipt_sha256") is not None:
            hex_id(value["ready_receipt_sha256"])
        require(value["status"] != "allocated" or value.get("volume_id"),
                "Allocated cache has no volume identity")
        return value

    def read(self, *, required=True):
        value = self.intent.read()
        require(value is not None or not required, "Cache allocation has not been planned")
        return self.validate(value) if value is not None else None

    def save(self, value, **changes):
        value.update(changes, updated_at=self.storage["stamp"](self.clock()))
        self.intent.save(self.validate(value))

    def ensure(self, manifest, receipts):
        existing = self.read(required=False)
        if existing is not None:
            if manifest is not None:
                require(existing["source_manifest_sha256"] == hex_id(manifest), "Cache source manifest changed")
            if receipts is not None:
                require(existing["source_receipt_sha256"] == hex_id(receipts), "Cache source receipts changed")
            return existing
        token = uuid.uuid4().hex
        request = self.payload(token)
        value = dict(version=1, profile=PROFILE, status="planned", token=token, cache_id=token,
                     name=request["name"], request=request, volume_id=None, response_id=None,
                     filesystem_uuid=str(uuid.uuid4()), source_manifest_sha256=hex_id(manifest),
                     source_receipt_sha256=hex_id(receipts), cache_generation=generation(token, manifest, receipts),
                     ready_receipt_sha256=None, planned_at=self.storage["stamp"](self.clock()))
        self.save(value)
        return value

    def quote(self, state):
        now = self.clock()
        horizon = max(24.0, self.accounting.persistent_hours,
                      max((max(0, j["deadline"] - now) / 3600 for j in state["jobs"].values()
                           if j["status"] != "closed"), default=0))
        choices = [r for r in self.api.request("GET", "/volume-types?currency=usd") if r.get("type") == "NVMe"]
        require(len(choices) == 1 and choices[0].get("price", {}).get("currency") == "usd",
                "Exactly one USD NVMe storage quote is required")
        raw = choices[0]["price"].get("cps_per_gb")
        require(type(raw) in {int, float} and math.isfinite(raw) and raw > 0,
                "A positive finite NVMe storage quote is required")
        rate, margin = raw * 3600 * SIZE_GIB, max(10.0, self.accounting.margin)
        require(math.isfinite(rate), "NVMe storage quote exceeds the supported numeric range")
        report = self.budget["summary"](state, now, horizon)
        blockers = []
        try:
            # Exercise the ordinary guard without creating a fake compute job.
            self.budget["check_storage_lifetime"](self.root, [], now)
            self.budget["reserve"](copy.deepcopy(state), "cache-quote", rate, 0, horizon, now,
                                     self.accounting.ceiling, margin, horizon)
            require(0 <= now - state["last_watchdog"] <= 180, "Budget watchdog time is invalid")
        except (self.budget["Error"], Error) as exc:
            blockers.append(str(exc))
        return dict(**report, new_hourly=rate, reserve_hours=horizon, new_reserve=rate * horizon,
                    margin=margin, ceiling=self.accounting.ceiling,
                    projected=report["spent"] + report["reserved"] + report["background_reserve"] + rate * horizon + margin,
                    allowed=not blockers, blockers=blockers)

    def verified(self, intent, row):
        ident = identifier(row.get("id"))
        expected = {x["key"]: x["value"] for x in intent["request"]["tags"]}
        observed = tags(row)
        require(row.get("name") == intent["name"] and all(observed.get(k) == v for k, v in expected.items()),
                "Provider cache name/token/tags do not match the allocation")
        require(intent.get("volume_id") in {None, ident} and intent.get("response_id") in {None, ident},
                "Provider cache volume ID changed")
        require(row.get("type") == "NVMe" and row.get("is_os_volume") is False
                and type(row.get("size")) is int and row["size"] == SIZE_GIB
                and row.get("location") == LOCATION and row.get("currency") == "usd"
                and row.get("contract") == "PAY_AS_YOU_GO", "Provider cache geometry or billing identity changed")
        created = self.storage["utc"](row.get("created_at"))
        require(intent.get("attempted_at") is not None
                and self.storage["utc"](intent["attempted_at"]) - 5 <= created <= self.clock(),
                "Provider cache creation time differs from the recorded POST attempt")
        if intent.get("verified"):
            require(row.get("created_at") == intent["verified"].get("created_at"), "Provider cache creation identity changed")
        rate = self.budget["number"](row.get("base_hourly_cost"), "allocated cache rate")
        require(rate > 0, "Allocated cache storage price is unavailable")
        return copy.deepcopy(row)

    def observe(self, intent, inventory):
        candidates = {}
        for row in inventory[2] + inventory[1]:
            values = tags(row)
            if (row.get("id") == intent.get("volume_id") or row.get("id") == intent.get("response_id")
                    or row.get("name") == intent["name"] or values.get("allocation-token") == intent["token"]):
                candidates[row.get("id")] = row
        if not candidates:
            if intent["status"] == "allocated":
                self.save(intent, status="uncertain", last_error="Allocated cache is absent from provider inventory")
            return None
        try:
            require(len(candidates) == 1, "Multiple provider volumes match the cache allocation")
            row = self.verified(intent, next(iter(candidates.values())))
            require(any(x.get("id") == row["id"] for x in inventory[1]) and not row.get("deleted_at")
                    and row.get("status") not in {"deleted", "deleting", "canceled", "canceling"},
                    "Cache is trashed or deleting; recreation is forbidden")
        except Error as exc:
            self.save(intent, status="uncertain", last_error=str(exc))
            raise
        self.save(intent, volume_id=row["id"], verified=row,
                  status="creating" if row.get("status") in {"ordered", "cloning", "restoring"} else "allocated",
                  last_error=None)
        return row

    def refresh(self, state, intent):
        inventory = self.accounting.refresh(state)
        row = self.observe(intent, inventory)
        # Register immediately, rather than depending on the next watchdog tick.
        self.budget["remember_database_allocations"](state, self.root, self.clock())
        return inventory, row

    def current_lease(self):
        pointer = self.active.read()
        if pointer is None:
            return None
        require(isinstance(pointer, dict) and type(pointer.get("schema")) is int
                and pointer["schema"] == 1, "Invalid cache lease pointer")
        lease = self.lease_store(pointer.get("lease_id")).read()
        require(isinstance(lease, dict) and type(lease.get("schema")) is int and lease["schema"] == 1
                and lease.get("lease_id") == pointer["lease_id"] and lease.get("mode") in {"populate", "serve"}
                and lease.get("status") in {"preparing", "launching", "bound", "released"},
                "Cache lease record is missing or corrupt")
        intent = self.read()
        require(all(lease.get(k) == intent.get(k) for k in self.identity(intent)), "Cache lease identity changed")
        require(lease.get("worker_name") == "bio-msa-cache-" + pointer["lease_id"], "Cache lease worker name changed")
        return lease

    def save_lease(self, lease):
        lease["updated_epoch"] = self.clock()
        self.lease_store(lease["lease_id"]).save(lease)
        self.active.save(dict(schema=1, lease_id=lease["lease_id"]))

    @staticmethod
    def identity(intent):
        return {key: intent.get(key) for key in ("cache_id", "volume_id", "filesystem_uuid", "cache_generation",
                                                "source_manifest_sha256", "source_receipt_sha256")}

    def envelope(self, intent, inventory=None, lease=None):
        result = dict(schema=1, status=intent["status"], profile=PROFILE, **self.identity(intent),
                      ready_receipt_sha256=intent.get("ready_receipt_sha256"), size_gib=SIZE_GIB,
                      size_bytes=SIZE_GIB * GIB, volume_type="NVMe", location=LOCATION,
                      intent_path=str(self.intent.path), lease_path=str(self.active.path), lease_lock_path=str(self.root / LOCK),
                      creation=dict(method="POST", path="/volumes", request=intent["request"],
                                    response_id=intent.get("response_id"), reconciled_id=intent.get("volume_id"),
                                    attempted_at=intent.get("attempted_at")), lease=lease)
        if inventory is not None:
            result["provider"] = dict(observed_epoch=self.clock(), instances=inventory[0], volumes=inventory[1], trash=inventory[2])
            if lease and lease.get("managed"):
                result["provider"]["managed"] = lease["managed"]
        if intent.get("quote"):
            result["quote"] = intent["quote"]
        return result

    def execute(self, command, manifest=None, receipts=None):
        with self.operation.locked():
            intent = self.ensure(manifest, receipts) if command in {"plan", "create"} else self.read()
            if manifest is not None:
                require(intent["source_manifest_sha256"] == hex_id(manifest), "Cache source manifest changed")
            if receipts is not None:
                require(intent["source_receipt_sha256"] == hex_id(receipts), "Cache source receipts changed")
            with self.accounting.store.admission(), self.accounting.store.locked(self.clock()) as state:
                inventory, _ = self.refresh(state, intent)
                if command in {"plan", "create"} and intent["status"] == "planned":
                    quote = self.quote(state)
                    self.save(intent, quote=quote)
                    if command == "create":
                        require(quote["allowed"], "Cache allocation blocked: " + "; ".join(quote["blockers"]))
                        self.save(intent, status="creating", attempted_at=self.storage["stamp"](self.clock()))
                        self.accounting.store.save(state)
                        try:
                            reply = self.api.request("POST", "/volumes", intent["request"])
                            ident = identifier(reply)
                        except Exception:
                            self.save(intent, status="uncertain", last_error="Create outcome is uncertain; reconcile the exact token before continuing")
                            return self.envelope(intent, inventory)
                        self.save(intent, volume_id=ident, response_id=ident)
                        inventory, _ = self.refresh(state, intent)
                return self.envelope(intent, inventory, self.current_lease())

    def owned(self, intent, row):
        require(intent["status"] == "allocated" and row is not None, "Cache volume ownership is not reconciled")

    def detached(self, intent, row, inventory):
        self.owned(intent, row)
        require(row.get("status") in {"created", "detached"} and not attachments(row, inventory),
                "Cache volume is attached, busy, or detachment is not confirmed")

    def cache_jobs(self, state, intent):
        return [dict(token=token, **job) for token, job in state["jobs"].items()
                if intent["volume_id"] in job.get("volumes", [])]

    def acquire(self, lease_id, mode="serve"):
        hex_id(lease_id, 32)
        require(mode in {"serve", "populate"}, "Unknown cache lease mode")
        with self.operation.locked(), self.accounting.store.locked(self.clock()) as state:
            intent = self.read()
            inventory, row = self.refresh(state, intent)
            old = self.current_lease()
            if old and old["status"] != "released":
                require(old["lease_id"] == lease_id and old["mode"] == mode, "Cache is exclusively leased to another worker")
                return self.envelope(intent, inventory, old)
            require(self.lease_store(lease_id).read() is None, "A released cache lease ID cannot be reused")
            self.detached(intent, row, inventory)
            require(not any(j["status"] != "closed" for j in self.cache_jobs(state, intent)),
                    "An unresolved managed job still owns this cache volume")
            require(mode == "populate" or intent.get("ready_receipt_sha256"), "The full cache has no verified ready receipt")
            require(mode != "populate" or not intent.get("ready_receipt_sha256"), "A completed cache cannot be repopulated")
            lease = dict(schema=1, lease_id=lease_id, status="preparing", mode=mode,
                         worker_name="bio-msa-cache-" + lease_id, created_epoch=self.clock(), managed=None,
                         **self.identity(intent))
            self.save_lease(lease)
            return self.envelope(intent, inventory, lease)

    def lease(self, lease_id):
        hex_id(lease_id, 32)
        lease = self.current_lease()
        require(lease is not None and lease["lease_id"] == lease_id, "This worker does not own the active cache lease")
        return lease

    def launching(self, lease_id):
        with self.operation.locked():
            lease = self.lease(lease_id)
            require(lease["status"] in {"preparing", "launching"}, "Cache lease has already been bound or released")
            lease["status"] = "launching"
            self.save_lease(lease)
            return self.envelope(self.read(), lease=lease)

    def matching_jobs(self, state, intent, lease):
        rows = [j for j in self.cache_jobs(state, intent) if j.get("cache_lease_id") == lease["lease_id"]]
        require(all(j.get("cache_generation") == intent["cache_generation"] for j in rows), "Managed cache generation changed")
        return rows

    def bind(self, lease_id, instance_id, boot_id=None):
        identifier(instance_id)
        if boot_id is not None:
            identifier(boot_id)
        with self.operation.locked(), self.accounting.store.locked(self.clock()) as state:
            intent, lease = self.read(), self.lease(lease_id)
            require(lease["status"] in {"launching", "bound"}, "Cache lease is not in its launch phase")
            inventory, row = self.refresh(state, intent)
            self.owned(intent, row)
            matches = [i for i in inventory[0] if i.get("id") == instance_id]
            jobs = [j for j in self.matching_jobs(state, intent, lease) if j.get("id") == instance_id]
            require(len(matches) == len(jobs) == 1, "Exact managed worker ownership is unavailable")
            instance, job = matches[0], jobs[0]
            require(instance.get("hostname") == lease["worker_name"] and job["status"] in {"starting", "running"}
                    and instance.get("description", "").startswith("bio-dc:" + job["token"] + " "),
                    "Managed worker name or reservation token does not match the lease")
            os_id = identifier(instance.get("os_volume_id"))
            require(job.get("os_id") == os_id and os_id != intent["volume_id"]
                    and row.get("status") == "attached" and row.get("instance_id") == instance_id
                    and attachments(row, inventory) == {instance_id}, "Exclusive cache attachment is not confirmed")
            previous = lease.get("managed")
            managed = dict(instance_id=instance_id, os_volume_id=os_id, token=job["token"])
            require(previous is None or all(previous.get(k) == v for k, v in managed.items()), "Cache lease worker identity changed")
            if boot_id is not None:
                require(previous is None or previous.get("boot_id") in {None, boot_id}, "Cache worker boot identity changed")
                managed["boot_id"] = boot_id
            elif previous and previous.get("boot_id"):
                managed["boot_id"] = previous["boot_id"]
            lease.update(status="bound", managed=managed)
            self.save_lease(lease)
            return self.envelope(intent, inventory, lease)

    def release(self, lease_id):
        with self.operation.locked(), self.accounting.store.locked(self.clock()) as state:
            intent, lease = self.read(), self.lease(lease_id)
            inventory, row = self.refresh(state, intent)
            self.detached(intent, row, inventory)
            require(not any(i.get("hostname") == lease["worker_name"] for i in inventory[0]),
                    "The lease worker still exists in provider inventory")
            require(not any(j["status"] != "closed" for j in self.cache_jobs(state, intent)),
                    "Managed cache launch or cleanup remains unresolved")
            jobs = self.matching_jobs(state, intent, lease)
            if lease["status"] not in {"preparing", "released"}:
                require(jobs, "Launch was authorized but no exact budget receipt exists; administrative reconciliation is required")
            managed = lease.get("managed")
            if managed:
                require(not any(i.get("id") == managed["instance_id"] for i in inventory[0]),
                        "The exact leased instance is still present")
                require(any(j["token"] == managed["token"] and j.get("id") == managed["instance_id"]
                            and j.get("os_id") == managed["os_volume_id"] for j in jobs),
                        "Closed budget receipt no longer matches the leased worker")
            for job in jobs:
                require(not job.get("id") or not any(i.get("id") == job["id"] for i in inventory[0]),
                        "A managed lease attempt is still present at the provider")
                require(not job.get("os_id") or not any(v.get("id") == job["os_id"]
                                                       for v in inventory[1] + inventory[2]),
                        "The exact managed OS disk remains in active storage or trash")
            lease.update(status="released", released_epoch=self.clock(), release_checks=dict(
                exact_workers_absent=True, exact_os_disks_absent=True,
                volume_detached=True, managed_jobs_closed=True),
                closed_job_tokens=[j["token"] for j in jobs])
            self.save_lease(lease)
            return self.envelope(intent, inventory, lease)

    def publish_ready(self, lease_id, raw, expected_sha256):
        require(isinstance(raw, bytes) and len(raw) <= 1024 * 1024, "Invalid ready receipt size")
        expected_sha256 = hex_id(expected_sha256)
        require(hashlib.sha256(raw).hexdigest() == expected_sha256, "Ready receipt bytes changed")
        try:
            ready = json.loads(raw)
        except (ValueError, UnicodeError):
            raise Error("Invalid ready receipt JSON") from None
        with self.operation.locked():
            intent, lease = self.read(), self.lease(lease_id)
            require(lease["mode"] == "populate" and lease["status"] == "bound", "A bound population lease is required")
            require(isinstance(ready, dict) and type(ready.get("schema")) is int and ready["schema"] == 1
                    and ready.get("status") == "ready"
                    and ready.get("kind") == "msa-full-database-block-cache"
                    and all(ready.get(k) == v for k, v in self.identity(intent).items()), "Ready receipt cache/source identity changed")
            completion = ready.get("completion", {})
            require(isinstance(completion, dict) and completion.get("full_readback") is True
                    and all(type(completion.get(k)) is int and completion[k] >= 0 for k in
                            ("files", "symlinks", "payload_bytes", "source_bytes_hashed", "destination_bytes_readback"))
                    and completion["files"] > 0 and completion["payload_bytes"] > 0
                    and completion["source_bytes_hashed"] == completion["destination_bytes_readback"] == completion["payload_bytes"],
                    "Full cache copy and readback completion is not proven")
            require(ready.get("filesystem_type") == "ext4" and type(ready.get("size_bytes")) is int
                    and ready["size_bytes"] == SIZE_GIB * GIB and ready.get("rootrel") == "colabfold",
                    "Ready cache filesystem layout changed")
            source = ready.get("source", {})
            require(isinstance(source, dict) and source.get("manifest_sha256") == intent["source_manifest_sha256"]
                    and source.get("receipt_sha256") == intent["source_receipt_sha256"], "Ready receipt source binding changed")
            hex_id(ready.get("content_manifest_sha256"))
            require(intent.get("ready_receipt_sha256") in {None, expected_sha256}, "A different ready generation is already sealed")
            self.save(intent, ready_receipt_sha256=expected_sha256, ready_receipt=ready)
            return self.envelope(intent, lease=lease)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "create", "reconcile", "status", "preflight", "acquire",
                                             "launching", "bind", "release", "publish-ready"))
    parser.add_argument("--state-root", default=os.environ.get("DC_STATE_DIR", "/var/lib/dc"))
    parser.add_argument("--source-manifest-sha256")
    parser.add_argument("--source-receipt-sha256")
    parser.add_argument("--lease-id")
    parser.add_argument("--mode", choices=("serve", "populate"), default="serve")
    parser.add_argument("--instance-id")
    parser.add_argument("--boot-id")
    parser.add_argument("--ready-receipt", type=Path)
    parser.add_argument("--ready-receipt-sha256")
    parser.add_argument("--expected-volume-id")
    parser.add_argument("--expected-filesystem-uuid")
    parser.add_argument("--expected-cache-generation")
    args = parser.parse_args(argv)
    require(os.geteuid() == 0, "Cache controls must run as root on the head")
    tools = Path(__file__).resolve().parent.parent
    budget = runpy.run_path(str(tools / "dc-budget.py"))
    storage = runpy.run_path(str(tools / "rfaa/storage.py"))
    controller = Controller(budget["API"](), args.state_root, budget, storage)
    expected = {key: value for key, value in dict(volume_id=args.expected_volume_id,
        filesystem_uuid=args.expected_filesystem_uuid, cache_generation=args.expected_cache_generation).items()
        if value is not None}
    if expected:
        retained = controller.read()
        require(all(retained.get(k) == v for k, v in expected.items()), "Configured cache identity differs from its allocation")
    if args.command in {"plan", "create", "reconcile", "status", "preflight"}:
        result = controller.execute("status" if args.command == "preflight" else args.command,
                                    args.source_manifest_sha256, args.source_receipt_sha256)
        if args.command == "preflight":
            require(result["status"] == "allocated" and result["ready_receipt_sha256"], "Full cache is not ready")
            if args.ready_receipt_sha256 is not None:
                require(result["ready_receipt_sha256"] == hex_id(args.ready_receipt_sha256), "Configured ready receipt changed")
    elif args.command == "acquire":
        result = controller.acquire(args.lease_id, args.mode)
    elif args.command == "launching":
        result = controller.launching(args.lease_id)
    elif args.command == "bind":
        result = controller.bind(args.lease_id, args.instance_id, args.boot_id)
    elif args.command == "release":
        result = controller.release(args.lease_id)
    else:
        require(args.ready_receipt is not None, "A ready receipt file is required")
        result = controller.publish_ready(args.lease_id, args.ready_receipt.read_bytes(), args.ready_receipt_sha256)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["status"] == "allocated" or args.command == "plan" else 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"bio-msa-block-cache: {exc}", file=sys.stderr)
        raise SystemExit(4)
