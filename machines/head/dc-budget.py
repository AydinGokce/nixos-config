#!/usr/bin/env python3
"""Compute/storage cost estimates, reservations and ephemeral-node deadlines.

The public API supplies balance, not billed usage. Auto top-ups are therefore
ignored. Costs include every observed project instance/volume, creation-to-now
estimates for existing resources, and the old GPU ledger. Previously deleted,
unobserved resources, historical price changes, tax and other services are not
recoverable. DC_PRIOR_SPEND_USD adds a known correction at first initialization.

Run `dc watchdog` every minute. Only managed ephemeral instances and their OS
disks are deleted. The head/shared data remain and keep accruing costs after GPU
cutoff. This is NOT a provider hard spending cap; outages can delay teardown.
DC_MAX_INSTANCE_HOURLY optionally caps each fresh instance quote before launch;
OS/storage charges still count toward the separate overall spending guard.
DC_LAUNCH_COOLDOWN_SECONDS optionally waits after confirmed managed cleanup
before the next launch (default 0). Accounting/cleanup locks are not held while
waiting, and budget checks and quotes are refreshed after the wait.
A separate admission lock serializes reservation/create bookkeeping only;
accepted instances provision concurrently after their IDs are durable.
API schema: https://api.verda.com/v1/openapi.json (verified 2026-09-05).
"""
from __future__ import annotations

import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Project-wide authorization, cumulative across folding, MD, head and storage.
# Environment settings may make a launch more conservative, never raise this.
PROJECT_BUDGET_CEILING = 750.0


class Error(RuntimeError):
    pass


class LaunchBlocked(Error):
    """A budget/reconciliation stop: trying another GPU cannot resolve it."""


class APIError(Error):
    def __init__(self, method, path, status=None, detail=""):
        self.status = status
        message = f"{method} {path}: HTTP {status}" if status else \
                  f"{method} {path}: transport failed (outcome may be unknown)"
        super().__init__(message + (": " + detail if detail else ""))


def error_detail(raw, secrets=()):
    """Extract only bounded diagnostics, never serialize a provider response."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    fields = []
    for key in ("code", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            fields.append(value)
        elif key == "message" and isinstance(value, list):
            fields.extend(item for item in value[:5] if isinstance(item, str))
    diagnostic = "; ".join(fields)
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        for value in {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}:
            diagnostic = diagnostic.replace(value, "[REDACTED]")
    diagnostic = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", diagnostic)
    diagnostic = re.sub(r"(?i)\b(access_token|refresh_token|client_secret|authorization|password|api[_-]?key)"
                        r'''["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
                        r"\1=[REDACTED]", diagnostic)
    diagnostic = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
                        "[REDACTED]", diagnostic)
    # Drop terminal controls and flatten multiline/validation-list diagnostics.
    diagnostic = " ".join("".join(c if c.isprintable() else " " for c in diagnostic).split())
    return diagnostic[:300]


def response_value(raw, method, path, status):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        # Verda's instance/volume create endpoints can return UUIDs as plain text,
        # despite the OpenAPI JSON content declaration. Accept only that exact
        # shape at this endpoint; other malformed responses stay uncertain.
        if method == "POST" and path in {"/instances", "/volumes"}:
            candidate = raw.strip()
            if re.fullmatch(rb"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", candidate):
                return str(uuid.UUID(candidate.decode("ascii")))
        raise APIError(method, path, status, "response was not valid JSON or an expected resource UUID") from None


def number(value, label):
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise Error(f"Missing/invalid {label}; refusing to underestimate spend") from None
    if not math.isfinite(n) or n < 0:
        raise Error(f"Invalid {label}; refusing to underestimate spend")
    return n


def epoch(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError):
        raise Error("Missing/invalid resource creation time; cannot initialize cost") from None


class API:
    def __init__(self):
        self.base = os.environ.get("DC_API_URL", "https://api.datacrunch.io/v1").rstrip("/")
        self.token = None
        self.token_expires = 0
        self.auth_lock = threading.Lock()

    def request(self, method, path, body=None, *, authenticate=True):
        if authenticate:
            with self.auth_lock:
                if self.token is None or time.time() + 60 >= self.token_expires:
                    auth = self.request("POST", "/oauth2/token", {
                        "grant_type": "client_credentials",
                        "client_id": os.environ.get("DATACRUNCH_CLIENT_ID", ""),
                        "client_secret": os.environ.get("DATACRUNCH_CLIENT_SECRET", ""),
                    }, authenticate=False)
                    self.token = auth.get("access_token")
                    if not self.token:
                        raise Error("API did not return an access token")
                    self.token_expires = time.time() + number(auth.get("expires_in", 3600), "token lifetime")
        headers = {"Content-Type": "application/json"}
        if authenticate:
            headers["Authorization"] = "Bearer " + self.token
        payload = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                value = response_value(raw, method, path, response.status)
                total = response.headers.get("X-Total-Count")
                if isinstance(value, list) and total and int(total) > len(value):
                    raise Error(f"{path}: incomplete inventory; pagination needs handling")
                return value
        except urllib.error.HTTPError as exc:
            try:
                detail = error_detail(exc.read(8192), (self.token,
                    os.environ.get("DATACRUNCH_CLIENT_ID"), os.environ.get("DATACRUNCH_CLIENT_SECRET")))
            except (OSError, ValueError):
                detail = ""
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            if exc.code == 429 and re.fullmatch(r"[0-9]{1,4}", retry_after):
                detail = (detail + "; " if detail else "") + "retry after " + retry_after + "s"
            raise APIError(method, path, exc.code, detail) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            # Only the transport exception's diagnostic, never the response body.
            cause = str(getattr(exc, "reason", exc))
            detail = error_detail(json.dumps({"message": type(exc).__name__ + ": " + cause}),
                                  (self.token, os.environ.get("DATACRUNCH_CLIENT_ID"),
                                   os.environ.get("DATACRUNCH_CLIENT_SECRET")))
            raise APIError(method, path, detail=detail) from None

    def inventory(self):
        rows = [self.request("GET", path) for path in
                ("/instances", "/volumes", "/volumes/trash")]
        if not all(isinstance(row, list) for row in rows):
            raise Error("API inventory was not a list; budget check unavailable")
        return rows


def new_state(legacy, now, prior=0):
    state = {"version": 1, "resources": {}, "jobs": {}, "last_watchdog": 0,
             "historical_correction": number(prior, "historical spend"), "initialized_at": now}
    if legacy.exists():
        for line in legacy.read_text().splitlines():
            if not line.strip():
                continue
            try:
                ident, kind, rate, start, end = line.split("\t")
                rate = number(rate, "legacy rate")
                start, end = number(start, "legacy start"), number(end, "legacy end")
            except ValueError:
                raise Error("Malformed legacy ledger; refusing to discard historical cost") from None
            until = end or now
            state["resources"]["instance:" + ident] = {
                "rate": rate, "cost": rate * max(0, until - start) / 3600,
                "last": until, "active": not bool(end), "created": start,
                "kind": "instance", "name": kind, "legacy": True,
            }
            if not end:
                state["jobs"]["legacy-" + ident] = {
                    "id": ident, "created": start, "deadline": now + 4 * 3600,
                    "rate": rate, "os_rate": 0, "status": "running", "os_id": None,
                }
    return state


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "budget.json"

    @contextlib.contextmanager
    def admission(self):
        # A pending reservation is deliberately an uncertainty fence. Keep
        # another healthy submitter out of that ordinary reserve-to-create
        # window without holding the accounting lock during network I/O.
        # Process exit releases this lock; a retained ambiguous reservation
        # still blocks new spending until its exact outcome is reconciled.
        with (self.root / "launch-admission.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @contextlib.contextmanager
    def confirming(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token):
            raise Error("Invalid managed cleanup token")
        # Submitters and the watchdog may finish the same instance together.
        # This is deliberately separate from budget.lock and the slot pool.
        with (self.root / ("cleanup-job-" + token + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @contextlib.contextmanager
    def confirmation_slot(self, sleep=time.sleep):
        # All dc processes share four confirmation slots. Delete requests are
        # issued first; only inventory/purge confirmation waits for a slot.
        with contextlib.ExitStack() as stack:
            slots = [stack.enter_context((self.root / f"cleanup-slot-{index}.lock").open("a"))
                     for index in range(4)]
            while True:
                for slot in slots:
                    try:
                        fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    yield
                    return
                sleep(1)

    @contextlib.contextmanager
    def locked(self, now):
        with (self.root / "budget.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.exists():
                try:
                    state = json.loads(self.path.read_text())
                    if state["version"] != 1:
                        raise ValueError("version")
                except (ValueError, KeyError):
                    raise Error("Budget state is corrupt; refusing to reset spending") from None
            else:
                state = new_state(self.root / "ledger.tsv", now,
                                  os.environ.get("DC_PRIOR_SPEND_USD", "0"))
            yield state
            self.save(state)

    def save(self, state):
        fd, name = tempfile.mkstemp(prefix=".budget-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream, sort_keys=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            dfd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def reconcile(state, inventory, now):
    instances, volumes, trash = inventory
    seen = set()
    # During a restore the inventory endpoints can briefly overlap. Prefer the
    # current live entry and charge each resource once per snapshot.
    all_volumes = {row["id"]: row for row in trash + volumes}
    for kind, rows in (("instance", instances), ("volume", all_volumes.values())):
        for row in rows:
            ident = str(row["id"])
            key = kind + ":" + ident
            if row.get("currency", "usd").lower() != "usd" or row.get("contract") == "LONG_TERM":
                raise Error("USD pay-as-you-go/spot resources required for budget accounting")
            rate = number(row.get("price_per_hour" if kind == "instance" else "base_hourly_cost"),
                          kind + " hourly rate")
            created = min(now, epoch(row.get("created_at")))
            deleted = epoch(row["deleted_at"]) if row.get("deleted_at") else None
            active = deleted is None and row.get("status") not in {"deleted", "canceled", "discontinued", "notfound"}
            until = min(now, deleted) if deleted is not None else now
            previous = state["resources"].get(key)
            if previous:
                if previous.pop("legacy", False) and active:
                    # Old rm stamped an end time without confirming deletion.
                    # A live node must be charged even when that row is closed.
                    previous["cost"] = max(previous["cost"], rate * max(0, now - created) / 3600)
                    previous["last"] = now
                if previous["active"]:
                    previous["cost"] += previous["rate"] * max(0, until - previous["last"]) / 3600
                elif active:
                    # Restoring a trashed disk incurs the full PAYG price for
                    # its time in trash. Also conservatively fill an inventory
                    # disappearance/reappearance gap for live resources.
                    since = previous.get("deleted") or previous["last"]
                    previous["cost"] += previous["rate"] * max(0, now - since) / 3600
                previous.update(rate=rate, last=now, active=active, deleted=deleted)
            else:
                state["resources"][key] = {
                    "rate": rate, "cost": rate * max(0, until - created) / 3600,
                    "last": now, "active": active, "created": created, "kind": kind, "deleted": deleted,
                    "name": row.get("hostname", row.get("name", ident)),
                }
            seen.add(key)
    missing_databases = []
    expected = state.get("expected_database_volumes", {})
    for ident, allocation in expected.items():
        key = "volume:" + ident
        previous = state["resources"].get(key)
        retired = allocation.get("retired_at")
        if key not in seen and previous is None and retired is not None:
            state["resources"][key] = dict(rate=allocation["rate"],
                cost=allocation["rate"] * max(0, retired - allocation["created"]) / 3600,
                last=now, active=False, created=allocation["created"], kind="volume",
                deleted=retired, name=allocation["name"])
            continue
        if (key in seen or allocation.get("retired_at") is not None
                or (previous and previous.get("deleted") is not None)):
            continue
        # A missing inventory row is not proof that persistent storage stopped
        # billing. The durable allocation intent survives a crash before budget.save.
        if previous:
            previous["cost"] += previous["rate"] * max(0, now - previous["last"]) / 3600
            previous.update(active=True, last=now)
        else:
            state["resources"][key] = dict(rate=allocation["rate"],
                cost=allocation["rate"] * max(0, now - allocation["created"]) / 3600,
                last=now, active=True, created=allocation["created"], kind="volume",
                deleted=None, name=allocation["name"])
        missing_databases.append(ident)
    state["unresolved_database_volumes"] = missing_databases
    for key, resource in state["resources"].items():
        if key not in seen and resource["active"]:
            ident = key.removeprefix("volume:")
            if key.startswith("volume:") and ident in missing_databases:
                continue
            retired = expected.get(ident, {}).get("retired_at") if key.startswith("volume:") else None
            until = min(now, retired) if retired is not None else now
            resource["cost"] += resource["rate"] * max(0, until - resource["last"]) / 3600
            resource.update(active=False, last=now)
    current = {row["id"]: row for row in instances}
    for token, job in state["jobs"].items():
        marker = "bio-dc:" + token
        matches = [row for row in instances if row.get("description", "").startswith(marker + " ")]
        if not job.get("id") and matches:
            if len(matches) != 1:
                raise Error("Multiple nodes match a reservation; manual reconciliation needed")
            job["id"] = matches[0]["id"]
        row = current.get(job.get("id"))
        if row:
            job["os_id"] = row.get("os_volume_id") or job.get("os_id")
            job["rate"] = number(row.get("price_per_hour"), "instance rate")
            disk = state["resources"].get("volume:" + str(job.get("os_id")))
            if disk:
                job["os_rate"] = disk["rate"]
            if job["status"] == "pending":
                job["status"] = "starting"
        elif job.get("id") and job["status"] not in {"pending", "starting", "uncertain", "closed"}:
            job["status"] = "cleanup"
    state["last_inventory"] = now


def summary(state, now, persistent_hours=24):
    spent = state["historical_correction"]
    active_rate = 0.0
    for resource in state["resources"].values():
        spent += resource["cost"]
        if resource["active"]:
            spent += resource["rate"] * max(0, now - resource["last"]) / 3600
            active_rate += resource["rate"]
    managed = set()
    reserved = 0.0
    horizon = persistent_hours * 3600
    uncertain = False
    for job in state["jobs"].values():
        if job["status"] == "closed":
            continue
        for kind, ident in (("instance", job.get("id")), ("volume", job.get("os_id"))):
            if ident:
                managed.add(kind + ":" + ident)
        left = max(0, job["deadline"] - now)
        horizon = max(horizon, left)
        reserved += (job["rate"] + job["os_rate"]) * left / 3600
        uncertain |= job["status"] in {"pending", "uncertain", "cleanup"}
        for kind, ident, rate in (("instance", job.get("id"), job["rate"]),
                                  ("volume", job.get("os_id"), job["os_rate"])):
            if kind + ":" + str(ident) not in state["resources"]:
                spent += rate * max(0, now - job["created"]) / 3600
    background_rate = sum(row["rate"] for key, row in state["resources"].items()
                          if row["active"] and key not in managed)
    return {"spent": spent, "hourly": active_rate, "reserved": reserved,
            "background_reserve": background_rate * horizon / 3600,
            "background_hourly": background_rate, "uncertain": uncertain,
            "storage_uncertain": bool(state.get("unresolved_database_volumes") or
                                      state.get("database_allocation_errors"))}


def reserve(state, token, rate, os_rate, hours, now, ceiling, margin, persistent_hours):
    report = summary(state, now, max(persistent_hours, hours))
    if now - state["last_watchdog"] > 180:
        raise LaunchBlocked("Budget watchdog is stale/not running; refusing a paid launch")
    if report["storage_uncertain"]:
        raise LaunchBlocked("Database storage accounting is unresolved; reconcile the exact allocation before a paid launch")
    if report["uncertain"]:
        raise LaunchBlocked("Unresolved launch or cleanup; reconcile it before another paid launch")
    projected = report["spent"] + report["reserved"] + report["background_reserve"] + (rate + os_rate) * hours + margin
    if projected >= ceiling:
        raise LaunchBlocked(f"BUDGET HALT: projected ${projected:.2f} including runtime/background reserves >= ${ceiling:.2f}")
    state["jobs"][token] = {"id": None, "created": now, "deadline": now + hours * 3600,
                            "rate": rate, "os_rate": os_rate, "os_id": None, "status": "pending"}


def scoped_job_cost(state, scope, now):
    """Include earlier attempts and any live reservation in one caller's cap."""
    total, seen, limits = 0.0, set(), []
    for job in state['jobs'].values():
        if job.get('cost_scope') != scope:
            continue
        limits.append(number(job.get('max_cost_usd'), 'retained scoped job cap'))
        if job['status'] != 'closed':
            total += (job['rate'] + job['os_rate']) * max(0, job['deadline'] - now) / 3600
        for kind, ident, rate in (('instance', job.get('id'), job['rate']),
                                  ('volume', job.get('os_id'), job['os_rate'])):
            key = kind + ':' + str(ident)
            if ident and key not in seen:
                seen.add(key)
                resource = state['resources'].get(key)
                if resource is None:
                    raise LaunchBlocked('Scoped job accounting is incomplete; reconcile it before another launch')
                total += resource['cost']
                if resource['active']:
                    total += resource['rate'] * max(0, now - resource['last']) / 3600
            elif ident is None and job['status'] != 'closed':
                total += rate * max(0, now - job['created']) / 3600
    return total, min(limits) if limits else None


def launch_not_before(state, seconds):
    """Use observed cleanup completion, never creation/reconciliation times."""
    if seconds == 0:
        return 0
    completed = []
    for job in state["jobs"].values():
        if not job.get("id"):
            continue  # A rejected create is not a cleanup event.
        for key in ("cleanup_confirmed_at", "os_purged_at"):
            if key in job:
                value = job[key]
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise LaunchBlocked(f"Invalid confirmed cleanup timestamp {key}; reconcile the budget ledger before launch")
                completed.append(value)
    return max(completed) + seconds if completed else 0


def purgeable_os(state, token, job, disk, inventory):
    """Prove a detached trash item is this new managed job's disposable OS."""
    if not re.fullmatch(r"[0-9a-f]{32}", token) or not job.get("id") or job.get("legacy"):
        return False
    ident = job.get("os_id")
    if not ident or disk.get("id") != ident or disk.get("name") != "bio-os-" + token:
        return False
    if (disk.get("is_os_volume") is not True or disk.get("type") != "NVMe"
            or disk.get("contract") != "PAY_AS_YOU_GO" or disk.get("status") != "deleted"
            or disk.get("is_permanently_deleted") is True
            or not disk.get("deleted_at") or disk.get("instance_id")
            or disk.get("instances") != []):
        return False
    if any(other != token and row.get("os_id") == ident for other, row in state["jobs"].items()):
        return False
    instances, volumes, _ = inventory
    return not (any(row.get("id") == job["id"] or row.get("os_volume_id") == ident
                    for row in instances) or any(row.get("id") == ident for row in volumes))


def private_json(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("state must be private and owned by the controller user")
    return json.loads(path.read_text())


def allocation_intents(root):
    intents = {}
    for profile, filename, override in (
        ("rfaa", "rfaa-storage-intent.json", "RFAA_STORAGE_INTENT"),
        ("colabfold", "msa-storage-intent.json", "MSA_STORAGE_INTENT"),
    ):
        path = Path(os.environ.get(override, str(Path(root) / filename)))
        try:
            intent = private_json(path)
            if intent is None:
                continue
            status = intent["status"]
            if intent.get("version") != 1 or intent.get("profile") != profile or status not in {
                    "planned", "creating", "uncertain", "allocated", "rejected"}:
                raise ValueError("invalid allocation profile or status")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LaunchBlocked(f"Invalid database allocation intent: {exc}") from None
        intents[profile] = intent
    return intents


def remember_database_allocations(state, root, now):
    """Keep proven allocation costs through inventory gaps without blocking cleanup."""
    expected = state.setdefault("expected_database_volumes", {})
    errors = []
    try:
        for profile, intent in allocation_intents(root).items():
            if intent["status"] != "allocated":
                continue
            ident = str(uuid.UUID(intent["volume_id"]))
            verified = intent["verified"]
            rate = number(verified["base_hourly_cost"], "allocated storage rate")
            created = epoch(verified["created_at"])
            if (ident != intent["volume_id"] or verified["id"] != ident or rate <= 0
                    or not isinstance(intent["name"], str) or not intent["name"]
                    or verified["name"] != intent["name"] or created > now):
                raise ValueError("allocated storage identity/rate does not match its receipt")
            previous = expected.get(ident)
            value = dict(profile=profile, name=intent["name"], created=created, rate=rate)
            if previous and any(previous[key] != value[key] for key in ("profile", "name", "created")):
                raise ValueError("allocated storage identity changed")
            expected[ident] = {**(previous or {}), **value}
    except (Error, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(str(exc))
    for ident, allocation in expected.items():
        profile = allocation["profile"]
        filename, override = (("rfaa-storage.json", "RFAA_STORAGE_RECEIPT") if profile == "rfaa"
                              else ("msa-storage.json", "MSA_STORAGE_RECEIPT"))
        try:
            receipt = private_json(Path(os.environ.get(override, str(Path(root) / filename))))
            if receipt and receipt.get("status") == "complete":
                completed = epoch(receipt["completed_at"])
                if (receipt.get("version") != 1 or receipt.get("profile", "rfaa") != profile
                        or receipt.get("volume_id") != ident or receipt.get("name") != allocation["name"]
                        or epoch(receipt["created_at"]) != allocation["created"]
                        or not allocation["created"] <= completed <= now):
                    raise ValueError("completed storage receipt does not match the exact allocation")
                allocation["retired_at"] = completed
        except (Error, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            errors.append(str(exc))
    state["database_allocation_errors"] = errors


def check_storage_lifetime(root, volumes, now):
    """Gate new reservations after database expiry, while holding budget.lock.

    Expiry marks its receipt retiring before taking this same accounting lock.
    It therefore sees every older reservation, and later ones cannot launch.
    """
    # An accepted storage create can be temporarily absent from inventory.
    # Fence compute until its durable allocation intent has been reconciled.
    for profile, intent in allocation_intents(root).items():
        status = intent["status"]
        if status in {"creating", "uncertain"}:
            raise LaunchBlocked(f"Unresolved {profile} database allocation; reconcile storage before launching compute")
    for profile, label, filename, override in (
        ("rfaa", "RFAA", "rfaa-storage.json", "RFAA_STORAGE_RECEIPT"),
        ("colabfold", "ColabFold", "msa-storage.json", "MSA_STORAGE_RECEIPT"),
    ):
        path = Path(os.environ.get(override, str(Path(root) / filename)))
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        try:
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise ValueError("receipt must be private and owned by the controller user")
            receipt = json.loads(path.read_text())
            volume = receipt["volume_id"]
            if not isinstance(volume, str) or not volume:
                raise ValueError("invalid volume identity")
            if receipt.get("profile", "rfaa") != profile:
                raise ValueError("receipt belongs to a different storage profile")
            retention, expiry = receipt.get("retention", "timed"), receipt["expires_at"]
            if retention == "persistent":
                if expiry is not None:
                    raise ValueError("persistent retention requires a null expiry")
                expired = False
            elif retention == "timed":
                expired = epoch(expiry) <= now
            else:
                raise ValueError("unknown retention policy")
            blocked = volume in volumes and (receipt["status"] != "active" or expired)
        except (OSError, ValueError, KeyError, TypeError, Error) as exc:
            raise LaunchBlocked(f"Invalid {label} storage receipt: {exc}") from None
        if blocked:
            raise LaunchBlocked(f"{label} database storage is expired or retiring; launch refused")


class Controller:
    def __init__(self, api, store, clock=time.time, sleep=time.sleep):
        self.api, self.store, self.clock, self.sleep = api, store, clock, sleep
        self.ceiling = min(PROJECT_BUDGET_CEILING, number(
            os.environ.get("DC_BUDGET_CEILING", str(PROJECT_BUDGET_CEILING)), "ceiling"))
        self.margin = number(os.environ.get("DC_BUDGET_MARGIN", "10"), "safety margin")
        self.persistent_hours = number(os.environ.get("DC_PERSISTENT_RESERVE_HOURS", "24"), "background reserve")

    def refresh(self, state):
        inventory = self.api.inventory()
        remember_database_allocations(state, self.store.root, self.clock())
        reconcile(state, inventory, self.clock())
        return inventory

    def quote(self, kind, spot, size):
        types = self.api.request("GET", "/instance-types")
        choices = [row for row in types if row["instance_type"] == kind]
        if len(choices) != 1:
            raise Error("Unknown instance type")
        if choices[0].get("currency") != "usd":
            raise Error("USD instance quote unavailable")
        rate = number(choices[0].get("spot_price" if spot else "price_per_hour"), "instance quote")
        volumes = self.api.request("GET", "/volume-types")
        storage = next((row["price"] for row in volumes if row["type"] == "NVMe"), {})
        if storage.get("currency") != "usd":
            raise Error("USD storage quote unavailable")
        os_rate = number(storage.get("cps_per_gb"), "OS storage quote") * 3600 * size
        return rate, os_rate

    def wait_for_launch_cooldown(self, seconds):
        if seconds == 0:
            return
        reported_deadline = None
        while True:
            with self.store.locked(self.clock()) as state:
                deadline = launch_not_before(state, seconds)
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            if deadline != reported_deadline:
                print(f"dc: waiting {remaining:.0f}s after confirmed managed cleanup before launch", file=sys.stderr, flush=True)
                reported_deadline = deadline
            # Releases budget.lock so watchdog/cleanup and other waiters can
            # progress; a newer completion moves the deadline on the next read.
            self.sleep(min(remaining, 30))

    def launch(self, args):
        if not 0 < args.max_hours <= 24 or args.os_size <= 0:
            raise Error("--max-hours must be in (0,24]; --os-size must be positive")
        if not args.image.startswith("ubuntu-"):
            raise Error("Ephemeral launches require an Ubuntu image type, not an existing OS volume")
        maximum_hourly = None
        maximum_job_cost = None
        if 'DC_MAX_JOB_COST_USD' in os.environ:
            try:
                maximum_job_cost = number(os.environ['DC_MAX_JOB_COST_USD'], 'DC_MAX_JOB_COST_USD')
                if maximum_job_cost <= 0:
                    raise Error('DC_MAX_JOB_COST_USD must be positive')
            except Error as exc:
                raise LaunchBlocked(str(exc)) from None
        cost_scope = os.environ.get('DC_JOB_COST_SCOPE')
        if cost_scope is not None and (maximum_job_cost is None or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', cost_scope)):
            raise LaunchBlocked('DC_JOB_COST_SCOPE requires a valid bounded identity and DC_MAX_JOB_COST_USD')
        if "DC_MAX_INSTANCE_HOURLY" in os.environ:
            try:
                maximum_hourly = number(os.environ["DC_MAX_INSTANCE_HOURLY"], "DC_MAX_INSTANCE_HOURLY")
            except Error as exc:
                raise LaunchBlocked(str(exc)) from None
        try:
            cooldown = number(os.environ.get("DC_LAUNCH_COOLDOWN_SECONDS", "0"), "DC_LAUNCH_COOLDOWN_SECONDS")
        except Error as exc:
            raise LaunchBlocked(str(exc)) from None
        token = uuid.uuid4().hex
        ident = None
        complete = False
        try:
            while True:
                self.wait_for_launch_cooldown(cooldown)
                with self.store.admission():
                    # Requote after both cooldown and admission waits. This
                    # lock ends after the provider outcome is durably recorded,
                    # before readiness polling or model execution begins.
                    rate, os_rate = self.quote(args.type, args.spot, args.os_size)
                    if maximum_hourly is not None and rate > maximum_hourly:
                        raise LaunchBlocked(f"Instance quote ${rate:g}/h exceeds DC_MAX_INSTANCE_HOURLY="
                                            f"${maximum_hourly:g}/h; launch refused")
                    reservation_cost = (rate + os_rate) * args.max_hours
                    if maximum_job_cost is not None and reservation_cost > maximum_job_cost:
                        # A cheaper GPU may fit, so permit the existing launcher
                        # to try its next candidate. No reservation or POST has
                        # happened for this unaffordable quote.
                        raise Error(f'GPU plus OS reservation ${reservation_cost:.4f} exceeds '
                                    f'DC_MAX_JOB_COST_USD=${maximum_job_cost:g}; launch refused')
                    keys = self.api.request("GET", "/sshkeys")
                    now = self.clock()
                    with self.store.locked(now) as state:
                        self.refresh(state)
                        now = self.clock()
                        if launch_not_before(state, cooldown) > now:
                            continue  # Cleanup advanced while admission waited.
                        check_storage_lifetime(self.store.root, args.volume, now)
                        if cost_scope is not None:
                            committed_cost, previous_limit = scoped_job_cost(state, cost_scope, now)
                            effective_limit = min(maximum_job_cost, previous_limit) if previous_limit is not None else maximum_job_cost
                            if committed_cost + reservation_cost > effective_limit:
                                raise Error(f'Run cost including earlier attempts and new reservation '
                                            f'${committed_cost + reservation_cost:.4f} exceeds '
                                            f'DC_MAX_JOB_COST_USD=${effective_limit:g}; launch refused')
                            maximum_job_cost = effective_limit
                        reserve(state, token, rate, os_rate, args.max_hours, now,
                                self.ceiling, self.margin, self.persistent_hours)
                        state["jobs"][token]["volumes"] = list(args.volume)
                        if maximum_job_cost is not None:
                            state['jobs'][token].update(max_cost_usd=maximum_job_cost,
                                                       quoted_reservation_cost_usd=reservation_cost)
                            if cost_scope is not None:
                                state['jobs'][token]['cost_scope'] = cost_scope
                    body = {"instance_type": args.type, "image": args.image,
                            "hostname": args.name or "bio-" + token[:12],
                            "description": f"bio-dc:{token} ephemeral deadline={int(now + args.max_hours * 3600)}",
                            "ssh_key_ids": [row["id"] for row in keys], "location_code": args.loc,
                            "is_spot": args.spot, "existing_volumes": args.volume,
                            "os_volume": {"name": "bio-os-" + token, "size": args.os_size}}
                    if args.spot:
                        body["os_volume"]["on_spot_discontinue"] = "move_to_trash"
                    try:
                        reply = self.api.request("POST", "/instances", body)
                        uuid.UUID(str(reply))
                        ident = reply
                    except (APIError, ValueError, TypeError) as exc:
                        with self.store.locked(self.clock()) as state:
                            rejected = isinstance(exc, APIError) and exc.status in {400, 401, 402, 403, 404, 409, 422, 429, 503}
                            state["jobs"][token]["status"] = "closed" if rejected else "uncertain"
                        if rejected:
                            if exc.status == 400 and "storage limit exceeded" in str(exc).lower():
                                raise LaunchBlocked(f"Storage quota blocked launch: {exc}; run dc gc for owned disposable OS disks "
                                                    "or review the provider quota before retrying") from None
                            raise Error(f"Launch rejected: {exc}") from None
                        reason = str(exc) if isinstance(exc, APIError) else "invalid create response"
                        raise LaunchBlocked(f"Launch outcome unknown ({reason}); reservation retained for watchdog reconciliation") from None
                    with self.store.locked(self.clock()) as state:
                        state["jobs"][token].update(id=ident, status="starting")
                break
            print(f"launched {ident} ({args.type} @ ${rate}/hr; max {args.max_hours:g}h)", flush=True)
            deadline = min(now + args.max_hours * 3600, self.clock() + 600)
            while self.clock() < deadline:
                row = self.api.request("GET", "/instances/" + ident)
                with self.store.locked(self.clock()) as state:
                    state["jobs"][token]["os_id"] = row.get("os_volume_id")
                if row.get("status") == "running" and row.get("ip"):
                    with self.store.locked(self.clock()) as state:
                        self.refresh(state)
                        state["jobs"][token]["status"] = "running"
                    complete = True
                    print(f"READY id={ident} ip={row['ip']}  (ssh: dc ssh {ident})", flush=True)
                    return ident
                if row.get("status") in {"error", "installation_failed", "no_capacity", "discontinued", "deleting"}:
                    raise Error("Instance failed during provisioning")
                self.sleep(10)
            raise Error("Provisioning timed out; cleaning up partial launch")
        finally:
            if ident and not complete:
                try:
                    self.remove(ident)
                except Error as exc:
                    print(f"dc: cleanup unconfirmed: {exc}; watchdog will retry", file=sys.stderr)

    def request_remove(self, ident):
        """Record ownership and request deletion without waiting for teardown."""
        with self.store.locked(self.clock()) as state:
            jobs = [(token, job) for token, job in state["jobs"].items() if job.get("id") == ident]
            if not jobs:
                raise Error(f"Refusing to remove unmanaged instance {ident}")
            if len(jobs) != 1:
                raise Error(f"Ambiguous managed instance {ident}; cleanup needs review")
            token, job = jobs[0]
            if job["status"] == "closed":
                return token, job.get("os_id")
            job["status"] = "cleanup"
            try:
                row = self.api.request("GET", "/instances/" + ident)
                job["os_id"] = row.get("os_volume_id") or job.get("os_id")
            except APIError as exc:
                if exc.status != 404:
                    self.store.save(state)
                    raise
            os_id = job.get("os_id")
            self.store.save(state)
        # Explicit volume_ids prevents provider defaults deleting shared data.
        try:
            self.api.request("PUT", "/instances", {"id": ident, "action": "delete",
                             "volume_ids": [os_id] if os_id else [], "delete_permanently": False})
        except APIError as exc:
            if exc.status != 404:
                raise
        return token, os_id

    def confirm_remove(self, ident, token, os_id):
        """Confirm one previously requested exact-owned instance/OS cleanup."""
        with self.store.confirming(token):
            with self.store.locked(self.clock()) as state:
                job = state["jobs"].get(token)
                if not job or job.get("id") != ident or job.get("os_id") != os_id:
                    raise Error("Managed cleanup identity changed before confirmation")
                if job["status"] == "closed":
                    return  # Another exact-owned confirmer completed teardown.
                if job["status"] != "cleanup":
                    raise Error("Managed cleanup request is not recorded")
            with self.store.confirmation_slot(self.sleep):
                self._confirm_remove(ident, token, os_id)

    def _confirm_remove(self, ident, token, os_id):
        for _ in range(9):
            inventory = self.api.inventory()
            present = any(row["id"] == ident and row.get("status") not in {"deleted", "notfound"}
                          for row in inventory[0])
            disk = next((row for row in inventory[1] if row["id"] == os_id and
                         row.get("status") not in {"deleted", "canceled"}), None)
            if not present and disk:
                if disk.get("is_os_volume") is not True:
                    raise Error("OS volume is unrecognized; cleanup needs review")
                # Instance deletion can disappear before its disk finishes
                # detaching. Wait for affirmative detach evidence; never send
                # DELETE while any attachment or transition is still reported.
                if disk.get("instance_id") or disk.get("instances") or disk.get("status") != "detached":
                    self.sleep(10)
                    continue
                self.api.request("DELETE", "/volumes/" + os_id, {"is_permanent": False})
            if not present and not disk:
                purged = self.purge_os(token)
                with self.store.locked(self.clock()) as state:
                    self.refresh(state)
                    for job in state["jobs"].values():
                        if job.get("id") == ident:
                            job["status"] = "closed"
                            job.setdefault("cleanup_confirmed_at", self.clock())
                detail = "; managed OS permanently removed" if purged else ""
                print(f"removed {ident} (confirmed{detail}; shared volumes retained)", flush=True)
                return
            self.sleep(10)
        raise Error(f"Deletion of {ident} unconfirmed; cost remains active and watchdog will retry")

    def remove(self, ident):
        token, os_id = self.request_remove(ident)
        self.confirm_remove(ident, token, os_id)

    def purge_os(self, token, *, closed_only=False):
        with self.store.locked(self.clock()) as state:
            inventory = self.refresh(state)
            job = state["jobs"].get(token)
            if not job or job["status"] not in ({"closed"} if closed_only else {"closed", "cleanup"}):
                return False
            ident = job.get("os_id")
            matches = [row for row in inventory[2] if row.get("id") == ident]
            if len(matches) != 1 or not purgeable_os(state, token, job, matches[0], inventory):
                return False
            # Save reconciled costs before the only irreversible request. The
            # UUID/name/token/OS/type/attachment checks above exclude legacy,
            # shared, live, restored and ambiguous volumes from this operation.
            self.store.save(state)
        try:
            self.api.request("DELETE", "/volumes/" + ident, {"is_permanent": True})
        except APIError as exc:
            # The watchdog and submitter can race after both prove ownership.
            # This response is only a reason to reconcile; success still needs
            # the fresh active/trash inventory confirmation below.
            already_deleted = exc.status == 400 and "already permanently deleted" in str(exc).lower()
            if exc.status != 404 and not already_deleted:
                raise
        for _ in range(9):
            inventory = self.api.inventory()
            active = any(row.get("id") == ident for row in inventory[1])
            recoverable = any(row.get("id") == ident and not (
                row.get("status") == "deleted" and row.get("is_permanently_deleted") is True)
                for row in inventory[2])
            if not active and not recoverable:
                with self.store.locked(self.clock()) as state:
                    reconcile(state, inventory, self.clock())
                    job = state["jobs"][token]
                    if job.get("os_id") != ident:
                        raise Error("Managed OS identity changed during purge; cleanup needs review")
                    job["os_purged_at"] = self.clock()
                print(f"purged managed OS {ident} (permanent removal confirmed)", flush=True)
                return True
            self.sleep(10)
        raise Error(f"Permanent removal of managed OS {ident} unconfirmed; retry dc gc or cleanup")

    def remove_many(self, targets):
        """Issue every owned delete before any slow confirmation can hold it up."""
        if not targets:
            return []
        failures, requested = [], []
        # Admission/accounting uses independent locks. Four bounded workers
        # avoid a ten-instance watchdog burst multiplying inventory polling
        # beyond the provider's per-endpoint limits.
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {ident: pool.submit(self.request_remove, ident) for ident in dict.fromkeys(targets)}
            for ident, future in pending.items():
                try:
                    token, os_id = future.result()
                    requested.append((ident, token, os_id))
                except Error as exc:
                    failures.append(str(exc))
            confirmations = [pool.submit(self.confirm_remove, *request) for request in requested]
            for future in confirmations:
                try:
                    future.result()
                except Error as exc:
                    failures.append(str(exc))
        return failures

    def gc(self):
        with self.store.locked(self.clock()) as state:
            inventory = self.refresh(state)
            tokens = [token for token, job in state["jobs"].items() if job["status"] == "closed"
                      and any(purgeable_os(state, token, job, disk, inventory) for disk in inventory[2])]
        count, failures = 0, []
        for token in tokens:
            try:
                count += bool(self.purge_os(token, closed_only=True))
            except Error as exc:
                failures.append(str(exc))
        print(f"purged {count} managed ephemeral OS volume(s); other storage retained", flush=True)
        if failures:
            raise Error("; ".join(failures))
        return count

    def watchdog(self):
        targets = []
        try:
            with self.store.locked(self.clock()) as state:
                self.refresh(state)
                now = self.clock()
                report = summary(state, now, self.persistent_hours)
                halt = report["spent"] + report["background_reserve"] + self.margin >= self.ceiling
                for job in state["jobs"].values():
                    if job.get("id") and job["status"] != "closed" and (
                            halt or now >= job["deadline"] or job["status"] == "cleanup" or
                            (job["status"] in {"pending", "starting", "uncertain"} and now - job["created"] >= 600)):
                        targets.append(job["id"])
                state["last_watchdog"] = now
                self.print_spend(report)
                if halt:
                    print("dc: BUDGET HALT; managed GPUs terminating. Head/storage remain billable.", file=sys.stderr)
                if report["storage_uncertain"]:
                    print("dc: database storage accounting unresolved; new paid launches blocked; "
                          "unconfirmed storage remains billable in the estimate.", file=sys.stderr)
        except Error:
            # Storage API trouble must not suppress deadline cleanup attempts.
            with self.store.locked(self.clock()) as state:
                targets = [j["id"] for j in state["jobs"].values() if j.get("id") and
                           j["status"] != "closed" and (self.clock() >= j["deadline"] or
                           j["status"] == "cleanup" or (j["status"] in {"pending", "starting", "uncertain"}
                           and self.clock() - j["created"] >= 600))]
            self.remove_many(targets)
            raise
        failures = self.remove_many(targets)
        if failures:
            raise Error("; ".join(failures))

    def print_spend(self, report):
        print(f"estimated project compute+storage spend: ${report['spent']:.2f} / ${self.ceiling:.2f}; "
              f"active ${report['hourly']:.4f}/h; job reservations ${report['reserved']:.2f}; "
              f"background reserve ${report['background_reserve']:.2f}; safety margin ${self.margin:.2f}")
        print("Estimate includes observed head/OS/shared storage and imported GPU history; "
              "not provider billing. Head/storage continue after GPU cutoff.")
        if report["background_hourly"]:
            daily = report["background_hourly"] * 24
            remaining = max(0, self.ceiling - report["spent"] - report["reserved"] - self.margin)
            print(f"Persistent background: ${daily:.2f}/day; remaining allowance covers at most "
                  f"{remaining / daily:.2f} days at that rate before additional jobs.")


def parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    launch = sub.add_parser("launch", help="reserve bounded spend and launch an ephemeral GPU")
    launch.add_argument("type")
    launch.add_argument("--spot", action="store_true")
    launch.add_argument("--name")
    launch.add_argument("--image", default="ubuntu-24.04-cuda-12.8-open-docker")
    launch.add_argument("--loc", default="FIN-02")
    launch.add_argument("--volume", action="append", default=[])
    launch.add_argument("--max-hours", type=float, default=float(os.environ.get("DC_MAX_JOB_HOURS", "4")))
    launch.add_argument("--os-size", type=int, default=50)
    sub.add_parser("spend")
    sub.add_parser("watchdog", help="enforce deadlines/budget; run every minute via systemd")
    sub.add_parser("gc", help="permanently purge proven disposable OS disks of closed managed jobs")
    sub.add_parser("ls")
    types = sub.add_parser("types")
    types.add_argument("--gpu", action="store_true")
    types.add_argument("--cpu", action="store_true")
    rm = sub.add_parser("rm")
    rm.add_argument("target", help="managed id/name, or all managed nodes (head retained)")
    ssh = sub.add_parser("ssh")
    ssh.add_argument("target")
    ssh.add_argument("args", nargs=argparse.REMAINDER)
    run = sub.add_parser("run", help="launch options -- command; clean up afterward")
    run.add_argument("args", nargs=argparse.REMAINDER)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    controller = Controller(API(), Store(os.environ.get("DC_STATE_DIR", "/var/lib/dc")))
    api = controller.api
    if args.command == "launch":
        controller.launch(args)
    elif args.command == "watchdog":
        controller.watchdog()
    elif args.command == "gc":
        controller.gc()
    elif args.command == "spend":
        with controller.store.locked(controller.clock()) as state:
            controller.refresh(state)
            controller.print_spend(summary(state, controller.clock(), controller.persistent_hours))
    elif args.command == "types":
        for row in api.request("GET", "/instance-types"):
            gpu = (row.get("gpu") or {}).get("description") or "cpu"
            if args.gpu and gpu == "cpu" or args.cpu and gpu != "cpu":
                continue
            print(row["instance_type"], gpu, row["price_per_hour"], row["spot_price"], sep="\t")
    elif args.command == "ls":
        for row in api.request("GET", "/instances"):
            print(row["id"][:8], row["instance_type"], row["status"], row.get("ip"), row["hostname"], sep="\t")
    elif args.command == "rm":
        with controller.store.locked(controller.clock()) as state:
            inventory = controller.refresh(state)
            targets = [j["id"] for j in state["jobs"].values() if j.get("id") and j["status"] != "closed"]
            if args.target != "all":
                match = next((r["id"] for r in inventory[0] if r["hostname"] == args.target), args.target)
                targets = [ident for ident in targets if ident == match]
                if not targets:
                    raise Error("No matching managed node; head/unmanaged instances cannot be removed by dc")
        for ident in targets:
            controller.remove(ident)
    elif args.command == "ssh":
        row = next((r for r in api.request("GET", "/instances") if args.target in (r["id"], r["hostname"])), None)
        if not row or not row.get("ip"):
            raise Error("No matching reachable instance")
        cmd = ["ssh", "-i", os.environ.get("DC_SSH_KEY", "/root/.ssh/datacrunch_ed25519"),
               "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=10", "root@" + row["ip"]]
        cmd.extend(args.args[1:] if args.args[:1] == ["--"] else args.args)
        return subprocess.call(cmd)
    elif args.command == "run":
        if "--" not in args.args:
            raise Error("Usage: dc run TYPE [launch options] -- command")
        split = args.args.index("--")
        launch_args = parser().parse_args(["launch"] + args.args[:split])
        command = args.args[split + 1:]
        if not command:
            raise Error("dc run requires a command")
        ident = controller.launch(launch_args)
        try:
            base = [sys.executable, __file__, "ssh", ident, "--"]
            for _ in range(30):
                if subprocess.call(base + ["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
                    break
                time.sleep(8)
            else:
                raise Error("sshd never became ready")
            return subprocess.call(base + command)
        finally:
            controller.remove(ident)
    return 0


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("dc: interrupted; recorded jobs remain subject to watchdog cleanup", file=sys.stderr)
        sys.exit(130)
    except Error as exc:
        print(f"dc: {exc}", file=sys.stderr)
        sys.exit(4 if isinstance(exc, LaunchBlocked) else 1)
