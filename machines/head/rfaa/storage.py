#!/usr/bin/env python3
"""One explicitly registered RFAA database volume and its storage deadline.

This does not create volumes. check/track use only the local receipt. register
reads provider identity; expire retires exactly that receipt's volume, while
preserving the head, original shared volume and all job result directories.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import signal
import stat
import subprocess
import tempfile
import time
import uuid


HEAD_ID = "340a396b-19a1-4969-833e-2ddc80d5729b"
PROTECTED_IDS = {HEAD_ID, "d48e5cae-cbbc-4d76-af0d-6faa275b5959",
                 "b8b3b446-e464-44dd-9e01-6402489f8c5a"}
MOUNT = "/mnt/bio-databases"


class Error(RuntimeError):
    pass


def utc(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError
        return parsed.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise Error("Storage expiry/creation time must be an ISO timestamp with UTC timezone") from None


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def volume_id(value):
    try:
        if str(uuid.UUID(value)) != value or value in PROTECTED_IDS:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise Error("Invalid or protected database volume UUID") from None
    return value


def process_start(pid):
    """Linux starttime distinguishes our submission from a reused numeric PID."""
    if not isinstance(pid, int) or pid <= 1:
        raise Error("Submission PID must be greater than one")
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return text.rsplit(") ", 1)[1].split()[19]
    except FileNotFoundError:
        return None
    except (OSError, IndexError):
        raise Error("Cannot verify submission process identity") from None


def boot_id():
    try:
        return str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()))
    except (OSError, ValueError):
        raise Error("Cannot verify head boot identity") from None


class Store:
    def __init__(self, path, *, owner=0, lock=None):
        self.path = Path(path)
        self.lock = Path(lock) if lock else self.path.with_suffix(".lock")
        self.owner = owner

    def secure(self, info, *, directory=False):
        forbidden = 0o022 if directory else 0o077
        if info.st_uid != self.owner or info.st_mode & forbidden:
            raise Error("Storage state must be privately writable and owned by root")
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise Error("Storage state must not be a symlink or special file")

    @contextlib.contextmanager
    def locked(self):
        self.secure(self.path.parent.lstat(), directory=True)
        try:
            fd = os.open(self.lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise Error("Cannot safely open storage state lock") from None
        with os.fdopen(fd, "r+") as stream:
            self.secure(os.fstat(stream.fileno()))
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield self.read()

    @contextlib.contextmanager
    def expiry_operation(self):
        """Separate lock: serialize timer/manual cleanup without fencing readers."""
        self.secure(self.path.parent.lstat(), directory=True)
        try:
            fd = os.open(self.path.with_suffix(".expiry.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise Error("Cannot safely open storage expiry operation lock") from None
        with os.fdopen(fd, "r+") as stream:
            self.secure(os.fstat(stream.fileno()))
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True

    def read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            raise Error("Cannot safely open storage state") from None
        with os.fdopen(fd) as stream:
            self.secure(os.fstat(stream.fileno()))
            try:
                value = json.load(stream)
            except (ValueError, UnicodeError):
                raise Error("Storage state is corrupt; refusing to replace or ignore it") from None
        if not isinstance(value, dict):
            raise Error("Storage state must be a JSON object")
        return value

    def save(self, value):
        fd, name = tempfile.mkstemp(prefix=".rfaa-storage-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def validate_receipt(receipt):
    if not receipt or receipt.get("version") != 1:
        raise Error("Full RFAA requires a registered database storage receipt")
    volume_id(receipt.get("volume_id"))
    if (receipt.get("status") not in {"active", "retiring", "complete"}
            or not re.fullmatch(r"bio-rfaa-db-[A-Za-z0-9-]{8,64}", receipt.get("name", ""))
            or receipt.get("location") != "FIN-02" or receipt.get("size_gb") != 3300
            or not isinstance(receipt.get("permanent"), bool)
            or not isinstance(receipt.get("jobs"), dict)
            or not re.fullmatch(r"nfs\.fin-02\.(?:datacrunch\.io|verda\.com):/[A-Za-z0-9/_-]+", receipt.get("nfs", ""))):
        raise Error("Invalid database storage receipt; refusing lifecycle operations")
    utc(receipt.get("expires_at"))
    utc(receipt.get("created_at"))
    for name, job in receipt["jobs"].items():
        if (not re.fullmatch(r"rfaa-[A-Za-z0-9-]+", name) or not isinstance(job, dict)
                or job.get("job") != name or not isinstance(job.get("pid"), int) or job["pid"] <= 1
                or not re.fullmatch(r"[0-9]+", job.get("start_ticks", ""))):
            raise Error("Invalid tracked submission identity")
        try:
            uuid.UUID(job.get("boot_id", ""))
        except (ValueError, TypeError, AttributeError):
            raise Error("Invalid tracked submission boot identity") from None
        if job.get("instance_id") is not None:
            volume_id(job["instance_id"])


def check_identity(receipt, row):
    volume_id(receipt["volume_id"])
    if (row.get("id") != receipt["volume_id"] or row.get("name") != receipt["name"]
            or row.get("type") != "NVMe_Shared" or row.get("is_os_volume") is not False
            or row.get("size") != receipt["size_gb"] or row.get("location") != receipt["location"]
            or row.get("contract") != "PAY_AS_YOU_GO" or row.get("currency") != "usd"
            or utc(row.get("created_at")) != utc(receipt["created_at"])
            or not any(tag.get("key") == "purpose" and tag.get("value") == "rfaa-databases"
                       for tag in row.get("tags", []))):
        raise Error("Database volume identity differs from the registered allocation")


class Operations:
    def run(self, command, timeout=60, *, check=True):
        try:
            result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise Error(f"Storage cleanup command failed or timed out: {command[0]}") from None
        if check and result.returncode:
            raise Error(f"Storage cleanup command failed: {command[0]} (exit {result.returncode})")
        return result

    def stop_installer(self):
        unit = "rfaa-database-install.service"
        loaded = self.run(["systemctl", "show", "--property=LoadState", "--value", unit], check=False)
        if loaded.returncode == 0 and loaded.stdout.strip() == "not-found":
            return
        self.run(["systemctl", "stop", unit])

    def stop_process(self, job):
        pid, expected = job["pid"], job["start_ticks"]
        if boot_id() != job["boot_id"]:
            return
        try:
            # Bind the signal to this process, closing the PID-reuse race
            # between reading /proc and sending a numeric-PID signal.
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        except (OSError, AttributeError):
            raise Error("Cannot bind a safe process handle for submission cleanup") from None
        try:
            if process_start(pid) != expected:
                return
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            for _ in range(15):
                if process_start(pid) != expected:
                    return
                time.sleep(1)
            if process_start(pid) == expected:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            os.close(fd)

    def collect(self, job, instance, results_root):
        target = results_root / job["job"]
        target.mkdir(mode=0o700, exist_ok=True)
        source = f"/mnt/bio-shared/runs/{job['job']}/out/"
        if instance and instance.get("ip"):
            ip = str(ipaddress.ip_address(instance["ip"]))
            key = os.environ.get("DC_SSH_KEY", "/root/.ssh/datacrunch_ed25519")
            remote_shell = shlex.join(["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                      "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"])
            self.run(["rsync", "-a", "--timeout=30", "-e", remote_shell,
                      f"root@{ip}:{source}", str(target) + "/"], timeout=45)
        else:
            self.run(["rsync", "-a", "--timeout=30", source, str(target) + "/"], timeout=45)

    def remove_worker(self, ident):
        self.run(["dc", "rm", ident], timeout=180)

    def collect_database_receipts(self, receipt, results_root):
        target = results_root / ("rfaa-database-receipts-" + receipt["volume_id"])
        target.mkdir(mode=0o700, exist_ok=True)
        (target / "allocation.json").write_text(json.dumps(receipt, indent=2) + "\n")
        failed = False
        for label, directory in (("uniref30", "UniRef30_2020_06"), ("bfd", "bfd"), ("pdb100", "pdb100_2021Mar03")):
            try:
                self.run(["rsync", "-a", "--timeout=15", f"{MOUNT}/rfaa/{directory}/.rfaa-database.json",
                          str(target / (label + ".json"))], timeout=20)
            except Error:
                failed = True
        if failed:
            raise Error("Some database installation receipts could not be copied; allocation receipt was retained")

    def unmount(self, expected_source):
        def mounted():
            result = self.run(["findmnt", "--json", "--mountpoint", MOUNT, "-o", "SOURCE,FSTYPE"], check=False)
            if result.returncode == 1:
                return None
            if result.returncode:
                raise Error("Cannot inspect database mount")
            entries = json.loads(result.stdout).get("filesystems", [])
            if len(entries) != 1:
                raise Error("Ambiguous database mount")
            item = entries[0]
            if item.get("fstype") == "autofs":
                return None
            if item.get("source") != expected_source or item.get("fstype") not in {"nfs", "nfs4"}:
                raise Error("Database mount belongs to another filesystem; refusing to unmount")
            return item
        mounted()
        unit = r"mnt-bio\x2ddatabases.automount"
        loaded = self.run(["systemctl", "show", "--property=LoadState", "--value", unit], check=False)
        if not (loaded.returncode == 0 and loaded.stdout.strip() == "not-found"):
            self.run(["systemctl", "stop", unit])
        if mounted():
            self.run(["umount", MOUNT])


class Controller:
    def __init__(self, store, budget, api, *, operations=None, clock=time.time,
                 start=process_start, boot=boot_id, results_root="/var/lib/bio-runs"):
        self.store, self.budget, self.api = store, budget, api
        self.ops, self.clock, self.start, self.boot = operations or Operations(), clock, start, boot
        self.results_root = Path(results_root)

    def active(self, receipt, ident):
        validate_receipt(receipt)
        if receipt["volume_id"] != ident or receipt["status"] != "active" or utc(receipt["expires_at"]) <= self.clock():
            raise Error("Database allocation is expired, retiring, or does not match; full RFAA is disabled")

    def check(self, ident):
        with self.store.locked() as receipt:
            self.active(receipt, ident)

    def track(self, ident, job_dir, pid, instance=None):
        directory = Path(job_dir)
        if (directory.parent.resolve() != self.results_root.resolve() or directory.is_symlink()
                or not re.fullmatch(r"rfaa-[A-Za-z0-9-]+", directory.name)):
            raise Error("RFAA job directory must be a direct result directory for this model")
        ticks = self.start(pid)
        if ticks is None:
            raise Error("Submission process exited before it could be tracked")
        if instance is not None:
            try:
                uuid.UUID(instance)
            except (ValueError, AttributeError):
                raise Error("Invalid managed worker ID") from None
            if instance == HEAD_ID:
                raise Error("The head cannot be registered as a disposable worker")
        with self.store.locked() as receipt:
            self.active(receipt, ident)
            existing = receipt["jobs"].get(directory.name)
            if existing and (existing["pid"], existing["start_ticks"], existing["boot_id"]) != (pid, ticks, self.boot()):
                raise Error("Job directory already belongs to a different submission process")
            receipt["jobs"][directory.name] = dict(job=directory.name, pid=pid, start_ticks=ticks, boot_id=self.boot(),
                                                   instance_id=instance or (existing or {}).get("instance_id"))
            self.store.save(receipt)

    def register(self, ident, name, expires_at, permanent=False):
        volume_id(ident)
        if utc(expires_at) <= self.clock():
            raise Error("Database storage expiry must be in the future")
        row = self.api.request("GET", "/volumes/" + ident)
        receipt = dict(version=1, volume_id=ident, name=name, location="FIN-02", size_gb=3300,
                       status="active", created_at=row.get("created_at"), expires_at=expires_at,
                       permanent=permanent, jobs={})
        sources = [part for part in shlex.split(row.get("mount_command") or "")
                   if re.fullmatch(r"nfs\.fin-02\.(?:datacrunch\.io|verda\.com):/[A-Za-z0-9/_-]+", part)]
        if len(sources) != 1 or sources[0].split(":", 1)[1] != row.get("pseudo_path"):
            raise Error("Cannot verify the database NFS export")
        receipt["nfs"] = sources[0]
        validate_receipt(receipt)
        check_identity(receipt, row)
        if row.get("status") in {"deleted", "deleting", "canceled"}:
            raise Error("Cannot register deleted database storage")
        with self.store.locked() as old:
            if old:
                raise Error("A storage receipt already exists; preserve its audit record before registering another allocation")
            self.store.save(receipt)
        return receipt

    def managed_jobs(self, ident):
        # Expire releases the receipt lock before acquiring budget.lock. A
        # launcher checking the receipt while holding budget.lock can therefore
        # finish recording its reservation before we inspect that reservation.
        with self.budget.locked() as state:
            if not state or state.get("version") != 1 or not isinstance(state.get("jobs"), dict):
                raise Error("Managed job inventory is unavailable; refusing storage teardown")
            return [dict(job) for job in state["jobs"].values()
                    if ident in job.get("volumes", []) and job.get("status") != "closed"]

    def expire(self, *, now=False, volume=None):
        if now and not volume:
            raise Error("Early retirement requires --now --volume with the exact registered UUID")
        if not self.store.path.exists() and not self.store.path.is_symlink():
            if now:
                raise Error("No registered database volume to retire")
            return False
        with self.store.expiry_operation() as acquired:
            if not acquired:
                return False
            return self.expire_locked(now=now, volume=volume)

    def expire_locked(self, *, now=False, volume=None):
        with self.store.locked() as receipt:
            validate_receipt(receipt)
            if volume is not None and volume != receipt["volume_id"]:
                raise Error("Early retirement volume does not match the registered allocation")
            if receipt["status"] == "complete" or (receipt["status"] == "active" and not now
                                                       and utc(receipt["expires_at"]) > self.clock()):
                return False
            receipt.update(status="retiring", last_attempt=stamp(self.clock()))
            self.store.save(receipt)
        try:
            self.retire(receipt)
        except Exception as exc:
            with self.store.locked() as current:
                current["last_error"] = str(exc)[:500]
                self.store.save(current)
            raise
        return True

    def retire(self, receipt):
        ident = receipt["volume_id"]
        row = self.get_volume(ident)
        if row:
            check_identity(receipt, row)
        inventory = self.api.inventory()
        for observed in inventory[1] + inventory[2]:
            if observed.get("id") == ident:
                check_identity(receipt, observed)
        jobs = self.managed_jobs(ident)
        for job in jobs:
            if job.get("id"):
                volume_id(job["id"])
        if not jobs and self.deleted(receipt, row, inventory):
            self.complete([])
            return
        workers = {job.get("id") for job in jobs if job.get("id")}
        tracked = [job for job in receipt["jobs"].values() if job.get("instance_id") in workers
                   or (job["boot_id"] == self.boot() and self.start(job["pid"]) == job["start_ticks"])]
        self.ops.stop_installer()
        warnings = []
        if row and row.get("status") not in {"deleted", "deleting"}:
            try:
                self.ops.collect_database_receipts(receipt, self.results_root)
            except Error as exc:
                warnings.append(str(exc))
        for job in tracked:
            if self.job_completed(job):
                continue
            instance = next((item for item in inventory[0] if item.get("id") == job.get("instance_id")), None)
            try:
                self.ops.collect(job, instance, self.results_root)
            except Error as exc:
                # Outputs are on the separately retained original shared FS.
                # An offline laptop or failed copy must not extend DB rental.
                warnings.append(str(exc))
        for job in tracked:
            self.ops.stop_process(job)
        if warnings:
            with self.store.locked() as current:
                current["copy_warnings"] = list(dict.fromkeys(current.get("copy_warnings", []) + warnings))
                self.store.save(current)
        unresolved = False
        for job in jobs:
            worker = job.get("id")
            if not worker:
                unresolved = True
            elif worker == HEAD_ID:
                raise Error("Protected head appears in managed database jobs; refusing cleanup")
            else:
                volume_id(worker)
                try:
                    self.ops.remove_worker(worker)
                except Error:
                    # The submission's exit trap may have closed it first.
                    if any(row.get("id") == worker for row in self.managed_jobs(ident)):
                        raise
        if unresolved or self.managed_jobs(ident):
            raise Error("Database volume has unresolved managed reservations; next timer run will retry")
        row = self.get_volume(ident)
        if row:
            check_identity(receipt, row)
        inventory = self.api.inventory()
        if self.deleted(receipt, row, inventory):
            self.complete(warnings)
            return
        if row is None:
            raise Error("Database volume disappeared inconsistently; refusing to guess its deletion state")
        attached = self.shared_attachments(row, inventory)
        if attached - {HEAD_ID}:
            raise Error("Database volume has an unrecognized live attachment; refusing teardown")
        self.ops.unmount(receipt["nfs"])
        if row.get("status") not in {"deleted", "deleting"}:
            if attached:
                self.api.request("PUT", "/volumes", {"id": ident, "action": "detach", "instance_id": HEAD_ID})
                # A detach acceptance does not prove the export was revoked.
                # Keep this timer run bounded and inspect again on the next run.
                raise Error("Database unshare requested; waiting for provider confirmation")
            self.api.request("DELETE", "/volumes/" + ident, {"is_permanent": receipt["permanent"]})
        elif row.get("status") == "deleted" and receipt["permanent"] and row.get("is_permanently_deleted") is not True:
            self.api.request("DELETE", "/volumes/" + ident, {"is_permanent": True})
        row = self.get_volume(ident)
        if row:
            check_identity(receipt, row)
        inventory = self.api.inventory()
        if not self.deleted(receipt, row, inventory):
            raise Error("Database deletion unconfirmed; next timer run will retry")
        self.complete(warnings)

    @staticmethod
    def shared_attachments(row, inventory):
        # For NVMe_Shared, singular instance_id can still identify the first
        # recipient after unsharing it. Use the shared recipients list and the
        # inverse live-instance volume_ids references, never that legacy field.
        recipients = row.get("instances")
        if not isinstance(recipients, list):
            raise Error("Cannot verify shared volume attachment metadata")
        attached = set()
        for item in recipients:
            ident = item.get("id") if isinstance(item, dict) else item
            if not isinstance(ident, str) or not ident:
                raise Error("Cannot verify shared volume attachment metadata")
            attached.add(ident)
        live = [item for item in inventory[0]
                if item.get("status") not in {"deleted", "notfound", "discontinued"}]
        attached.intersection_update(item["id"] for item in live)
        for item in live:
            volumes = item.get("volume_ids")
            if not isinstance(volumes, list) or not all(isinstance(ident, str) for ident in volumes):
                raise Error("Cannot verify live instance volume references")
            if row["id"] in volumes or item.get("os_volume_id") == row["id"]:
                attached.add(item["id"])
        return attached

    def get_volume(self, ident):
        try:
            return self.api.request("GET", "/volumes/" + ident)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise

    @staticmethod
    def deleted(receipt, row, inventory):
        ident = receipt["volume_id"]
        if any(item.get("id") == ident for item in inventory[1]):
            return False
        if row is None:
            matches = [item for item in inventory[2] if item.get("id") == ident]
            if not matches:
                return True
            if len(matches) != 1:
                return False
            row = matches[0]
            check_identity(receipt, row)
        return (row.get("status") == "deleted" and bool(row.get("deleted_at"))
                and (not receipt["permanent"] or row.get("is_permanently_deleted") is True))

    def job_completed(self, job):
        try:
            value = json.loads((self.results_root / job["job"] / "job.json").read_text())
            return type(value.get("exit_status")) is int and value["exit_status"] == 0
        except (OSError, ValueError, AttributeError):
            return False

    def complete(self, warnings):
        with self.store.locked() as receipt:
            warnings = list(dict.fromkeys(receipt.get("copy_warnings", []) + warnings))
            receipt.update(status="complete", completed_at=stamp(self.clock()), copy_warnings=warnings)
            receipt.pop("last_error", None)
            self.store.save(receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--volume", required=True)
    track = commands.add_parser("track")
    track.add_argument("--volume", required=True)
    track.add_argument("--job-dir", required=True)
    track.add_argument("--pid", required=True, type=int)
    track.add_argument("--instance")
    register = commands.add_parser("register")
    register.add_argument("--volume", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--expires-at", required=True)
    register.add_argument("--permanent", action="store_true")
    expire = commands.add_parser("expire")
    expire.add_argument("--now", action="store_true", help="retire early after validation/results retrieval")
    expire.add_argument("--volume", help="exact registered UUID, required with --now")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise Error("Storage lifecycle commands must run as root on the head")
    budget_path = Path(os.environ.get("DC_STATE_DIR", "/var/lib/dc")) / "budget.json"
    path = Path(os.environ.get("RFAA_STORAGE_RECEIPT", str(budget_path.with_name("rfaa-storage.json"))))
    api = None
    if args.command in {"register", "expire"}:
        api = runpy.run_path(os.environ.get("DC_HELPER", "/etc/bio-tools/dc-budget.py"))["API"]()
    controller = Controller(Store(path), Store(budget_path, lock=budget_path.with_name("budget.lock")), api,
                            results_root=os.environ.get("BIO_RESULTS_DIR", "/var/lib/bio-runs"))
    if args.command == "check":
        controller.check(args.volume)
    elif args.command == "track":
        controller.track(args.volume, args.job_dir, args.pid, args.instance)
    elif args.command == "register":
        controller.register(args.volume, args.name, args.expires_at, args.permanent)
        print("Registered database storage identity and UTC expiry; no volume was created")
    else:
        if controller.expire(now=args.now, volume=args.volume):
            print("Database storage retirement completed; head and original shared storage retained")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"bio-rfaa-storage: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
