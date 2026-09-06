#!/usr/bin/env python3
"""Plan, create once, or reconcile the two approved persistent database volumes.

An uncertain request is reconciled, never blindly repeated. No command deletes,
resizes, restores or otherwise changes an existing volume.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import sys
import time
import uuid


class Error(RuntimeError):
    pass


INTENTS = {
    "rfaa": ("rfaa-storage-intent.json", "RFAA_STORAGE_INTENT"),
    "colabfold": ("msa-storage-intent.json", "MSA_STORAGE_INTENT"),
}


def intent_path(profile, root):
    filename, variable = INTENTS[profile]
    return Path(os.environ.get(variable, str(Path(root) / filename)))


class Controller:
    def __init__(self, api, root, budget, storage, *, clock=time.time, owner=0):
        self.api, self.root, self.budget, self.storage, self.clock = api, Path(root), budget, storage, clock
        self.accounting = budget["Controller"](api, budget["Store"](root), clock=clock)
        self.stores = {profile: storage["Store"](intent_path(profile, root), owner=owner) for profile in INTENTS}
        # A single lock serializes allocation operations across both profiles.
        self.operation = storage["Store"](self.root / "database-volume.operation.json", owner=owner)
        for profile, store in self.stores.items():
            if store.path.resolve() == storage["receipt_path"](profile, root).resolve():
                raise Error("Allocation intent must be separate from the operational receipt")
            for config in storage["PROFILES"].values():
                if store.path.resolve().is_relative_to(Path(config["mount"])):
                    raise Error("Allocation intent must be stored outside database volumes")
        if len({store.path.resolve() for store in self.stores.values()}) != len(self.stores):
            raise Error("Database profiles require separate allocation intents")

    def payload(self, profile, token):
        config = self.storage["profile_config"](profile)
        return {
            "type": "NVMe_Shared", "location_code": "FIN-02", "size": config["size_gb"],
            "name": config["name_prefix"] + token, "instance_ids": [self.storage["HEAD_ID"]],
            "tags": [{"key": "purpose", "value": config["purpose"]},
                     {"key": "allocation-token", "value": token},
                     {"key": "retention", "value": "persistent"}],
        }

    def validate(self, value, profile):
        if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1
                or value.get("profile") != profile
                or value.get("status") not in {"planned", "creating", "uncertain", "allocated", "rejected"}
                or not isinstance(value.get("token"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", value["token"])):
            raise Error("Invalid database allocation intent")
        body = self.payload(profile, value["token"])
        if value.get("name") != body["name"] or value.get("request") != body:
            raise Error("Allocation intent differs from its fixed approved profile")
        self.storage["utc"](value.get("planned_at"))
        if value.get("volume_id") is not None:
            self.storage["volume_id"](value["volume_id"])
        if value["status"] == "allocated" and not value.get("volume_id"):
            raise Error("Allocated intent lacks its exact volume ID")
        return value

    def read(self, profile):
        value = self.stores[profile].read()
        return self.validate(value, profile) if value is not None else None

    def save(self, intent, **changes):
        intent.update(changes, updated_at=self.storage["stamp"](self.clock()))
        self.validate(intent, intent["profile"])
        self.stores[intent["profile"]].save(intent)

    def ensure_intent(self, profile):
        intent = self.read(profile)
        if intent is not None:
            return intent
        receipt = self.storage["Store"](
            self.storage["receipt_path"](profile, self.root), owner=self.stores[profile].owner).read()
        if receipt is not None:
            raise Error("An operational receipt already exists; refusing a second allocation")
        token = uuid.uuid4().hex
        request = self.payload(profile, token)
        intent = dict(version=1, profile=profile, token=token, name=request["name"], request=request,
                      status="planned", planned_at=self.storage["stamp"](self.clock()), volume_id=None)
        self.save(intent)
        return intent

    def quote(self, state, profile):
        now = self.clock()
        horizon = max(24.0, self.accounting.persistent_hours,
                      max((max(0, job["deadline"]-now)/3600 for job in state["jobs"].values()
                           if job["status"] != "closed"), default=0))
        report = self.budget["summary"](state, now, horizon)
        types = self.api.request("GET", "/volume-types?currency=usd")
        choices = [row for row in types if row.get("type") == "NVMe_Shared"]
        if len(choices) != 1 or choices[0].get("price", {}).get("currency") != "usd":
            raise Error("Exactly one USD shared-storage quote is required")
        per_gb = self.budget["number"](choices[0]["price"].get("cps_per_gb"), "database storage quote")
        if per_gb <= 0:
            raise Error("A positive shared-storage price is required")
        rate = per_gb * 3600 * self.storage["profile_config"](profile)["size_gb"]
        margin = max(10.0, self.accounting.margin)
        projected = report["spent"] + report["reserved"] + report["background_reserve"] + rate*horizon + margin
        reasons = []
        if not 0 <= now - state["last_watchdog"] <= 180:
            reasons.append("Budget watchdog is stale/not running")
        if report["uncertain"]:
            reasons.append("Managed compute has an unresolved launch or cleanup")
        if report["storage_uncertain"]:
            reasons.append("Database storage accounting is unresolved; reconcile the exact allocation")
        for other_profile in INTENTS:
            other = self.read(other_profile)
            if other and other["status"] in {"creating", "uncertain"}:
                reasons.append(f"Unresolved {other_profile} volume allocation")
        if projected >= self.accounting.ceiling:
            reasons.append("Projected spending reaches the approved budget ceiling")
        return dict(**report, new_hourly=rate, reserve_hours=horizon, new_reserve=rate*horizon,
                    margin=margin, ceiling=self.accounting.ceiling,
                    projected=projected, allowed=not reasons, blockers=reasons)

    @staticmethod
    def tags(row):
        result = {}
        for tag in row.get("tags", []):
            if not isinstance(tag, dict) or not isinstance(tag.get("key"), str) or tag["key"] in result:
                raise Error("Ambiguous provider volume tags")
            result[tag["key"]] = tag.get("value")
        return result

    def verified(self, intent, row, inventory):
        config = self.storage["profile_config"](intent["profile"])
        ident = self.storage["volume_id"](row.get("id"))
        expected_tags = {tag["key"]: tag["value"] for tag in intent["request"]["tags"]}
        tags = self.tags(row)
        if (row.get("name") != intent["name"] or any(tags.get(k) != v for k, v in expected_tags.items())
                or (intent.get("volume_id") is not None and intent["volume_id"] != ident)):
            raise Error("Provider volume does not match the exact allocation name/token/tags")
        receipt = dict(volume_id=ident, name=intent["name"], size_gb=config["size_gb"],
                       location="FIN-02", created_at=intent.get("verified", {}).get("created_at", row.get("created_at")))
        self.storage["check_identity"](receipt, row, intent["profile"])
        sources = [value for value in shlex.split(row.get("mount_command") or "")
                   if re.fullmatch(r"nfs\.fin-02\.(?:datacrunch\.io|verda\.com):/[A-Za-z0-9/_-]+", value)]
        if len(sources) != 1 or sources[0].split(":", 1)[1] != row.get("pseudo_path"):
            return None  # The provider may still be creating the export.
        attached = self.storage["Controller"].shared_attachments(row, inventory)
        if self.storage["HEAD_ID"] not in attached:
            return None
        rate = self.budget["number"](row.get("base_hourly_cost"), "allocated storage rate")
        if rate <= 0:
            raise Error("Allocated storage has no positive hourly rate")
        return {key: row.get(key) for key in ("id", "name", "size", "type", "location", "tags",
                "created_at", "status", "base_hourly_cost", "monthly_price", "pseudo_path", "mount_command")} | {"nfs": sources[0]}

    def observe(self, intent, inventory):
        """Adopt one exact matching allocation, never infer a deletion candidate."""
        current, trash = inventory[1:]
        candidates = {}
        for row in trash + current:
            tags = self.tags(row)
            if (row.get("name") == intent["name"] or tags.get("allocation-token") == intent["token"]
                    or (intent.get("volume_id") is not None and row.get("id") == intent["volume_id"])):
                candidates[row.get("id")] = row
        if not candidates:
            if intent["status"] == "allocated":
                self.save(intent, status="uncertain", last_error="Previously allocated volume is absent from inventory")
            return intent
        if len(candidates) != 1:
            self.save(intent, status="uncertain", last_error="Multiple provider volumes match the allocation intent")
            raise Error(intent["last_error"])
        row = next(iter(candidates.values()))
        try:
            verified = self.verified(intent, row, inventory)
        except Exception as exc:
            self.save(intent, status="uncertain", last_error=str(exc)[:500])
            raise
        self.save(intent, volume_id=row["id"])
        if (row.get("deleted_at") or row.get("status") in {"deleted", "deleting", "canceled"}
                or not any(item.get("id") == row["id"] for item in current)):
            self.save(intent, status="uncertain", last_error="Matching allocation is trashed or deleting; recreation is forbidden")
            raise Error(intent["last_error"])
        if verified is None:
            self.save(intent, status="creating", last_error="Waiting for verified shared export and head attachment")
        else:
            self.save(intent, status="allocated", verified=verified, last_error=None)
        return intent

    def execute(self, command, profile):
        with self.operation.locked():
            if command == "reconcile":
                intent = self.read(profile)
                if intent is None:
                    raise Error("No allocation intent exists to reconcile")
            else:
                intent = self.ensure_intent(profile)
            with self.accounting.store.locked(self.clock()) as state:
                inventory = self.accounting.refresh(state)
                self.observe(intent, inventory)
                if command == "reconcile" or intent["status"] not in {"planned", "rejected"}:
                    return intent
                quote = self.quote(state, profile)
                self.save(intent, quote=quote)
                if command == "plan":
                    return intent
                if not quote["allowed"]:
                    raise Error("Allocation blocked: " + "; ".join(quote["blockers"]))
                receipt = self.storage["Store"](self.storage["receipt_path"](profile, self.root),
                                                owner=self.stores[profile].owner).read()
                if receipt is not None:
                    raise Error("An operational receipt already exists; refusing a second allocation")
                self.save(intent, status="creating", attempted_at=self.storage["stamp"](self.clock()), last_error=None)
                # Persist the inventory/accounting refresh before any paid request.
                self.accounting.store.save(state)
                try:
                    reply = self.api.request("POST", "/volumes", intent["request"])
                    ident = self.storage["volume_id"](reply)
                except Exception as exc:
                    rejected = getattr(exc, "status", None) == 400 and "Storage limit exceeded" in str(exc)
                    self.save(intent, status="rejected" if rejected else "uncertain", last_error=str(exc)[:500])
                    return intent
                self.save(intent, volume_id=ident)
                inventory = self.accounting.refresh(state)
                return self.observe(intent, inventory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "create", "reconcile"))
    parser.add_argument("profile", choices=tuple(INTENTS))
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise Error("Database allocation commands must run as root on the head")
    directory = Path(__file__).resolve().parent
    budget = runpy.run_path(os.environ.get("DC_HELPER", str(directory / "dc-budget.py")))
    storage = runpy.run_path(os.environ.get("DATABASE_STORAGE_HELPER", str(directory / "rfaa/storage.py")))
    controller = Controller(budget["API"](), os.environ.get("DC_STATE_DIR", "/var/lib/dc"), budget, storage)
    result = controller.execute(args.command, args.profile)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["status"] == "allocated" or args.command == "plan" else 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"bio-database-volume: {exc}", file=sys.stderr)
        raise SystemExit(4)
