#!/usr/bin/env python3
"""AWS MSA bootstrap primitives; no AWS API, SSH, format, or mount execution.

The head owns creation, attachment, leases, transfer subprocesses and shutdown.
Device plans require fresh authenticated provider/guest observations. Data is
rsynced only into an unpublished staging tree, then fully hashed before publish.
The portable database inventory and Linux probes are shared with block_cache;
AWS receipts and EBS identity remain distinct from the Verda cache protocol.

AWS documents EBS volume IDs in NVMe serial numbers (names may change at boot):
https://docs.aws.amazon.com/ebs/latest/userguide/identify-nvme-ebs-device.html
"""
import argparse
import concurrent.futures
import contextlib
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import stat
import sys
import threading
import time

import block_cache as content
import block_cache_device as linux
import databases
import search_profile

KIND = "aws-private-msa-database"
MOUNTPOINT = Path("/mnt/bio-msa-databases")
PENDING = ".colabfold.pending"
STATE = ".aws-msa"
RUNTIME_ROOT = Path("/opt/bio-worker-runtime")
SIZE_BYTES = 1300 * 1024**3
RANGE_THRESHOLD = 4 * 1024**3
RANGE_STREAMS = 32
MAX_JSON = 256 * 1024**2
require = linux.require
canonical = linux.canonical
digest = linux.digest


def aws_id(value, prefix):
    require(isinstance(value, str) and re.fullmatch(prefix+r"-[0-9a-f]{8}(?:[0-9a-f]{9})?", value),
            "Invalid AWS "+prefix+" ID")
    return value


def binding(value):
    require(isinstance(value, dict) and type(value.get("schema")) is int and value["schema"] == 1
            and value.get("provider") == "aws", "Missing AWS database binding")
    require(isinstance(value.get("account_id"), str) and re.fullmatch(r"[0-9]{12}", value["account_id"]),
            "Invalid AWS account identity")
    require(value.get("region") == "us-east-1" and re.fullmatch(r"us-east-1[a-z]", value.get("availability_zone", "")),
            "AWS database region/AZ differs")
    require(re.fullmatch(r"[a-f0-9]{32}", value.get("cache_id", "")), "Invalid AWS database allocation token")
    aws_id(value.get("volume_id"), "vol")
    linux.identifier(value.get("filesystem_uuid"), "AWS filesystem UUID")
    for key in ("source_manifest_sha256", "source_receipt_sha256"):
        require(content.sha(value.get(key)), "Invalid "+key)
    require(value.get("source_manifest_sha256") == databases.MANIFEST_SHA256,
            "AWS database differs from the pinned complete scientific manifest")
    require(value.get("ready_receipt_sha256") is None or content.sha(value["ready_receipt_sha256"]),
            "Invalid AWS database ready pin")
    return value


def database_identity(owner):
    owner = binding(owner)
    return {key: owner[key] for key in ("schema", "provider", "account_id", "region", "availability_zone",
        "cache_id", "volume_id", "filesystem_uuid", "source_manifest_sha256", "source_receipt_sha256")}


def _tags(rows):
    require(isinstance(rows, list), "AWS resource tags are missing")
    result = {}
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get("Key"), str) and isinstance(row.get("Value"), str)
                and row["Key"] not in result, "Invalid or duplicate AWS resource tag")
        result[row["Key"]] = row["Value"]
    return result


def _unique(rows, key, prefix):
    require(isinstance(rows, list), "Incomplete AWS resource observations")
    result = {}
    for row in rows:
        require(isinstance(row, dict), "Malformed AWS resource observation")
        ident = aws_id(row.get(key), prefix)
        require(ident not in result, "Duplicate AWS resource identity")
        result[ident] = row
    return result


def provider_binding(owner, provider, now):
    owner = binding(owner)
    require(isinstance(provider, dict) and all(provider.get(k) == owner[k] for k in ("account_id", "region")),
            "AWS provider account/region mismatch")
    linux.fresh(provider.get("observed_epoch"), now, "AWS provider inventory", future_seconds=5)
    instances = _unique(provider.get("instances"), "InstanceId", "i")
    volumes = _unique(provider.get("volumes"), "VolumeId", "vol")
    managed = provider.get("managed", {})
    instance_id = aws_id(managed.get("instance_id"), "i")
    boot_id = linux.identifier(managed.get("boot_id"), "AWS managed guest boot ID")
    require(instance_id in instances and owner["volume_id"] in volumes, "AWS owned instance/volume absent")
    instance, volume = instances[instance_id], volumes[owner["volume_id"]]
    require(instance.get("State", {}).get("Name") == "running"
            and instance.get("Placement", {}).get("AvailabilityZone") == owner["availability_zone"],
            "AWS instance state or AZ differs")
    mappings = instance.get("BlockDeviceMappings")
    require(isinstance(mappings, list), "AWS instance block mappings missing")
    mapped = {}
    for row in mappings:
        if "Ebs" not in row:
            continue
        device, ebs = row.get("DeviceName"), row["Ebs"]
        vol = aws_id(ebs.get("VolumeId"), "vol")
        require(isinstance(device, str) and device not in mapped and ebs.get("Status") == "attached",
                "AWS instance attachment is ambiguous or incomplete")
        mapped[device] = vol
    root = mapped.get(instance.get("RootDeviceName"))
    require(root in volumes and root != owner["volume_id"] and list(mapped.values()).count(owner["volume_id"]) == 1,
            "AWS database conflicts with the OS/root mapping")
    for ident in (root, owner["volume_id"]):
        attachments = volumes[ident].get("Attachments")
        require(isinstance(attachments, list) and len(attachments) == 1, "AWS EBS attachment is not exclusive")
        attached = attachments[0]
        require(attached.get("InstanceId") == instance_id and attached.get("State") == "attached"
                and mapped.get(attached.get("Device")) == ident, "AWS EBS inverse attachment differs")
        if ident == owner["volume_id"]:
            require(attached.get("DeleteOnTermination") is False, "Persistent database volume would be deleted with worker")
        for other_id, other in instances.items():
            require(other_id == instance_id or not any(r.get("Ebs", {}).get("VolumeId") == ident
                    for r in other.get("BlockDeviceMappings", [])), "EBS volume appears on another instance")
    require(volume.get("VolumeType") == "gp3" and type(volume.get("Size")) is int and volume["Size"] == 1300
            and volume.get("AvailabilityZone") == owner["availability_zone"] and volume.get("State") == "in-use"
            and volume.get("MultiAttachEnabled", False) is False, "AWS database geometry/lifecycle differs")
    tags = _tags(volume.get("Tags"))
    require(tags.get("Project") == "gc-msa" and tags.get("allocation-token") == owner["cache_id"],
            "AWS database allocation tags differ")
    return dict(**database_identity(owner), instance_id=instance_id, os_volume_id=root, boot_id=boot_id,
                size_bytes=SIZE_BYTES, filesystem_type="ext4")


def _new_volume(owner, provider):
    creation = owner.get("creation", {})
    request, response = creation.get("request", {}), creation.get("response", {})
    require(creation.get("reconciled") is not True and creation.get("observation_only") is not True
            and isinstance(request, dict) and isinstance(response, dict)
            and request.get("VolumeType") == "gp3" and type(request.get("Size")) is int and request["Size"] == 1300
            and request.get("AvailabilityZone") == owner["availability_zone"] and not request.get("SnapshotId")
            and response.get("VolumeId") == owner["volume_id"] and not response.get("SnapshotId"),
            "Formatting requires the successful exact blank CreateVolume request/response")
    specs = [r for r in request.get("TagSpecifications", []) if r.get("ResourceType") == "volume"]
    require(len(specs) == 1, "CreateVolume allocation tags are missing")
    tags = _tags(specs[0].get("Tags"))
    require(tags.get("Project") == "gc-msa" and tags.get("allocation-token") == owner["cache_id"],
            "CreateVolume belongs to another allocation")
    volume = next(v for v in provider["volumes"] if v["VolumeId"] == owner["volume_id"])
    created, observed = (linux.epoch(row.get("CreateTime")) for row in (response, volume))
    # CreateVolume can truncate to seconds while DescribeVolumes retains
    # milliseconds (observed .000 versus .003). Only a whole-second value
    # permits this precision difference; conflicting fractional times fail.
    matching_creation = created == observed or (
        math.floor(created) == math.floor(observed)
        and (created.is_integer() or observed.is_integer()))
    require(matching_creation and not volume.get("SnapshotId"),
            "CreateVolume timestamp or blank-volume receipt differs")
    require(owner.get("ready_receipt_sha256") is None, "A published AWS database must never be reformatted")


def _ebs_serial(value):
    require(isinstance(value, str), "Missing EBS NVMe serial")
    serial = value.strip()
    if re.fullmatch(r"vol[0-9a-f]{8}(?:[0-9a-f]{9})?", serial):
        serial = "vol-"+serial[3:]
    return aws_id(serial, "vol")


def _guest_devices(result, evidence, now, *, mounted=False):
    nodes = linux.device_nodes(evidence.get("lsblk"))
    matches = {}
    for path, (node, parents) in nodes.items():
        if node.get("type") != "disk" or parents:
            continue
        serial = (node.get("serial") or "").strip()
        if not re.fullmatch(r"vol-?[0-9a-f]{8}(?:[0-9a-f]{9})?", serial):
            continue
        ident = _ebs_serial(serial)
        require(ident not in matches, "Duplicate EBS serial in guest")
        matches[ident] = path
    require(result["volume_id"] in matches and result["os_volume_id"] in matches,
            "Exact AWS EBS database or OS serial absent from guest; no disk guessing")
    result.update(device=matches[result["volume_id"]], os_device=matches[result["os_volume_id"]])
    identity = evidence.get("nvme_identity", {})
    require(identity.get("model", "").strip() == "Amazon Elastic Block Store"
            and _ebs_serial(identity.get("serial")) == result["volume_id"],
            "Native NVMe identity is not the exact EBS database volume")
    return linux.guest_binding(result, evidence, now, mounted=mounted)


def device_plan(action, owner, provider, evidence, *, now=None, ready=None, ready_sha256=None):
    now = time.time() if now is None else now
    result = provider_binding(owner, provider, now)
    node, uses = _guest_devices(result, evidence, now, mounted=action == "validate-mount")
    result.update(schema=1, kind=KIND, action=action, status="validated_plan", issued_epoch=now,
        expires_epoch=min(now+30, provider["observed_epoch"]+120, evidence["observed_epoch"]+120),
        ownership_sha256=digest(owner), provider_sha256=digest(provider), evidence_sha256=digest(evidence),
        major_minor=node["maj:min"], execution="not_executed; revalidate immediately before command")
    wipes, identified = linux.probe(evidence)
    if action == "initialize":
        _new_volume(owner, provider)
        require(node.get("ro") is False and node.get("fstype") in (None, "") and node.get("uuid") in (None, "")
                and wipes["signatures"] == [] and identified["returncode"] == 2 and identified["fields"] == {},
                "EBS is not blank: never erase an existing filesystem, partition or signature")
        result["command"] = ["mkfs.ext4", "-U", result["filesystem_uuid"], "-L", "bio-msa-aws", "-m", "0",
            "-E", "lazy_itable_init=0,lazy_journal_init=0,nodiscard", result["device"]]
        return result
    require(node.get("fstype") == "ext4" and node.get("uuid") == result["filesystem_uuid"]
            and identified["returncode"] == 0 and identified["fields"].get("TYPE") == "ext4"
            and identified["fields"].get("UUID") == result["filesystem_uuid"]
            and not any(k.startswith("PT") for k in identified["fields"])
            and wipes["signatures"] and all(r.get("type") == "ext4" and r.get("uuid") == result["filesystem_uuid"]
                for r in wipes["signatures"]), "EBS filesystem UUID/type/probes disagree")
    require(sum(n.get("uuid") == result["filesystem_uuid"] for n, _ in linux.device_nodes(evidence["lsblk"]).values()) == 1,
            "Filesystem UUID is duplicated")
    readonly = action in ("mount-serve", "validate-mount")
    if readonly:
        check_ready(owner, ready, ready_sha256)
    else:
        require(action == "mount-populate" and owner.get("ready_receipt_sha256") is None,
                "Read-write bootstrap requires an unpublished database")
    if action == "validate-mount":
        require(len(uses) == 1 and uses[0].get("target") == str(MOUNTPOINT), "EBS database mounted elsewhere")
        opts = set(uses[0].get("options", []))
        require({"ro", "nodev", "nosuid", "noexec"} <= opts and "rw" not in opts and {"noload", "norecovery"} & opts,
                "EBS database must be read-only without journal replay")
        result["status"] = "validated_mount"
    else:
        target = evidence.get("mountpoint", {})
        require(target.get("path") == str(MOUNTPOINT) and target.get("is_symlink") is False
                and target.get("entries") == [] and (target.get("exists") is False or target.get("is_directory") is True)
                and not any(m.get("target") == str(MOUNTPOINT) or m.get("target", "").startswith(str(MOUNTPOINT)+"/")
                    for m in evidence["mounts"]), "AWS database mountpoint is not an unused real directory")
        result["command"] = ["mount", "-t", "ext4", "-o",
            "ro,noload,nodev,nosuid,noexec" if readonly else "rw,nodev,nosuid,noexec",
            "UUID="+result["filesystem_uuid"], str(MOUNTPOINT)]
    return result


def inspect_device(volume_id):
    volume_id = aws_id(volume_id, "vol")
    snapshot = json.loads(linux._run(["lsblk", "--json", "--bytes", "--output", linux.LSBLK_COLUMNS]).stdout)
    matches = [p for p, (n, a) in linux.device_nodes(snapshot).items() if n.get("type") == "disk" and not a
        and (n.get("serial") or "").strip() in (volume_id, volume_id.replace("-", ""))]
    require(len(matches) == 1, "Exact EBS NVMe serial not unique/present; inspection required")
    evidence = linux.inspect(matches[0])
    raw = json.loads(linux._run(["nvme", "id-ctrl", "--output-format=json", matches[0]]).stdout)
    evidence["nvme_identity"] = dict(model=raw.get("mn"), serial=raw.get("sn"))
    return evidence


def read_pinned(path, expected):
    raw = content.read_bytes(Path(path), MAX_JSON)
    require(content.sha(expected) and hashlib.sha256(raw).hexdigest() == expected, "Document bytes differ from external SHA pin")
    return json.loads(raw)


def write_json(path, value):
    content.atomic_json(Path(path), value)
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextlib.contextmanager
def state_lock(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(path.resolve() == path and path.stat().st_uid == os.geteuid() and not path.stat().st_mode & 0o022,
            "Bootstrap state must be privately owned without symlink parents")
    fd = os.open(path/"lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield path
    finally:
        os.close(fd)


def source_inventory(source, output):
    plan = content.inspect(Path(source))
    plan.update(kind=KIND, stage="source-inventory")
    with state_lock(Path(output)):
        pin = write_json(Path(output)/"inventory.json", plan)
        shards = [[] for _ in range(4)]
        totals = [0]*4
        for entry in sorted(plan["entries"], key=lambda r: -r["source_metadata"].get("size", 0)):
            if entry["kind"] == "directory" or (entry["kind"] == "file"
                    and entry["source_metadata"]["size"] >= RANGE_THRESHOLD):
                continue
            index = min(range(4), key=lambda k: totals[k])
            shards[index].append(entry["path"])
            totals[index] += entry["source_metadata"]["size"] if entry["kind"] == "file" else 0
        for index, names in enumerate(shards):
            path = Path(output)/f"files-{index}.txt0"
            require(not path.is_symlink(), "Rsync file list is symlinked")
            path.write_bytes(b"".join(name.encode()+b"\0" for name in sorted(names)))
        return dict(schema=1, inventory_sha256=pin, payload_bytes=plan["payload_bytes"], shard_bytes=totals,
                    source=plan["source"], inventory=str(Path(output)/"inventory.json"))


def transfer_plan(source, inventory, inventory_sha256, output, host, identity, known_hosts):
    source = content.real_root(Path(source))
    plan = read_pinned(inventory, inventory_sha256)
    require(plan.get("kind") == KIND and plan.get("stage") == "source-inventory"
            and content.generation(source) == plan["source"], "Transfer source generation changed")
    require(re.fullmatch(r"root@(?:[0-9]{1,3}\.){3}[0-9]{1,3}", host), "Transfer requires the exact root@IPv4 worker")
    ipaddress.IPv4Address(host.removeprefix("root@"))
    for path in (identity, known_hosts):
        require(Path(path).is_absolute() and "\n" not in str(path), "SSH identity/known-host path must be absolute")
    ssh = ["ssh", "-i", str(identity), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
           "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile="+str(known_hosts),
           "-o", "GlobalKnownHostsFile=/dev/null", "-o", "UpdateHostKeys=no", "-o", "ConnectTimeout=10",
           "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
    lists = Path(output)
    names = set()
    commands = []
    for index in range(4):
        file = lists/f"files-{index}.txt0"
        raw = content.read_bytes(file, MAX_JSON)
        require(raw == b"" or raw.endswith(b"\0"), "Malformed rsync file list")
        for name in raw.split(b"\0")[:-1]:
            name = name.decode()
            content.safe_path(name)
            require(name not in names, "Duplicate rsync shard entry")
            names.add(name)
        commands.append(["rsync", "--links", "--perms", "--times", "--dirs", "--relative", "--protect-args",
            "--inplace", "--partial", "--fsync", "--no-whole-file", "--from0", "--files-from="+str(file),
            "--info=progress2", "--human-readable", "-e", shlex.join(ssh), str(source)+"/",
            host+":"+str(MOUNTPOINT/PENDING)+"/"])
    entries = plan["entries"]
    require(len({r["path"] for r in entries}) == len(entries), "Duplicate published inventory entry")
    ranged = sorted((r for r in entries if r["kind"] == "file" and r["source_metadata"]["size"] >= RANGE_THRESHOLD),
                    key=lambda r: (-r["source_metadata"]["size"], r["path"]))
    range_names = {r["path"] for r in ranged}
    expected = {r["path"] for r in entries if r["kind"] != "directory"}
    require(not names & range_names and names | range_names == expected,
            "Rsync and range lists do not exactly and exclusively cover the published snapshot")
    return dict(schema=1, kind=KIND, action="parallel-transfer-unpublished", parallel_workers=4, commands=commands,
                range_streams=RANGE_STREAMS, range_threshold_bytes=RANGE_THRESHOLD, ranged_entries=ranged,
                range_bytes=sum(r["source_metadata"]["size"] for r in ranged),
                rsync_bytes=sum(r["source_metadata"]["size"] for r in entries if r["kind"] == "file" and r["path"] in names),
                inventory_sha256=inventory_sha256, payload_bytes=plan["payload_bytes"],
                destination=str(MOUNTPOINT/PENDING), publication="forbidden until full source/destination SHA verification")


class HashProgress:
    def __init__(self):
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.read = 0

    def add(self, *, verified):
        with self.lock:
            self.read += verified


@contextlib.contextmanager
def hash_readers(workers, progress):
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        yield pool
    finally:
        progress.cancelled.set()
        pool.shutdown(wait=True, cancel_futures=True)


def _hash_file(root, row, source, cached, progress):
    path = root/row["path"]
    require(path.parent.resolve() == path.parent and path.lstat().st_dev == root.stat().st_dev,
            "Content path escaped its filesystem")
    content.regular(path)
    before = content.metadata(path)
    require(before["size"] == row["source_metadata"]["size"], "File size differs from published inventory: "+row["path"])
    if source:
        content.check_source(root, row)
    if cached and cached.get("metadata") == before:
        result = cached
    else:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
            if not source:
                os.fsync(handle.fileno())
            result = dict(metadata=before, sha256=content.hash_handle(handle, progress),
                          edge_sha256=content.edge_fd(handle, before["size"]))
    require(content.metadata(path) == before, "File changed while hashing")
    expected = row.get("expected", {}) if source else row
    require(expected.get("sha256", result["sha256"]) == result["sha256"]
            and expected.get("edge_sha256", result["edge_sha256"]) == result["edge_sha256"]
            and expected.get("bytes", before["size"]) == before["size"], "Full content hash differs: "+row["path"])
    return dict(result, path=row["path"], kind="file"), bool(cached and cached.get("metadata") == before)


def _hash_tree(root, plan, checkpoint, *, source, workers=4, callback=None):
    require(type(workers) is int and 1 <= workers <= 4, "Hash readers must be 1..4")
    root = content.real_root(Path(root))
    require(isinstance(plan.get("entries"), list) and len(plan["entries"]) <= content.MAX_FILES+10000,
            "Invalid/oversized published inventory")
    seen = set()
    for row in plan["entries"]:
        name = content.safe_path(row["path"], root=True)
        require(name not in seen and row.get("kind") in ("file", "symlink", "directory"),
                "Duplicate or invalid inventory entry")
        seen.add(name)
    require(not Path(checkpoint).is_symlink(), "Hash checkpoint is symlinked")
    db = sqlite3.connect(checkpoint)
    db.execute("PRAGMA synchronous=FULL")
    db.execute("CREATE TABLE IF NOT EXISTS hashes (key TEXT PRIMARY KEY, receipt TEXT NOT NULL)")
    scope = digest(plan)
    completed, reused, records = 0, 0, {}
    pending = {}
    progress = HashProgress()
    iterator = iter(plan["entries"])
    started, last = time.monotonic(), 0.0
    try:
        with hash_readers(workers, progress) as pool:
            exhausted = False
            while not exhausted or pending:
                while len(pending) < workers*2 and not exhausted:
                    row = next(iterator, None)
                    if row is None:
                        exhausted = True
                        break
                    path = root/row["path"]
                    require(path.parent.resolve() == path.parent and path.lstat().st_dev == root.stat().st_dev,
                            "Content escaped its filesystem")
                    if row["kind"] == "file":
                        key = digest(dict(scope=scope, source=source, path=row["path"]))
                        old = db.execute("SELECT receipt FROM hashes WHERE key=?", (key,)).fetchone()
                        pending[pool.submit(_hash_file, root, row, source, json.loads(old[0]) if old else None, progress)] = (row, key)
                    elif row["kind"] == "directory":
                        require(stat.S_ISDIR(path.lstat().st_mode), "Expected a real database directory")
                        records[row["path"]] = dict(path=row["path"], kind="directory")
                    else:
                        require(row["kind"] == "symlink" and path.is_symlink() and os.readlink(path) == row["target"]
                                and path.resolve(strict=True).is_relative_to(root/Path(row["path"]).parts[0]),
                                "Database alias changed or escaped")
                        records[row["path"]] = dict(path=row["path"], kind="symlink", target=row["target"])
                if not pending:
                    continue
                done, _ = concurrent.futures.wait(pending, timeout=1, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    row, key = pending.pop(future)
                    result, was_reused = future.result()
                    records[row["path"]] = result
                    completed += result["metadata"]["size"]
                    reused += result["metadata"]["size"] if was_reused else 0
                    db.execute("INSERT OR REPLACE INTO hashes VALUES (?, ?)", (key, canonical(result).decode()))
                elapsed = time.monotonic()-started
                if elapsed-last >= 1 or exhausted and not pending:
                    db.commit()
                    if callback:
                        callback(dict(stage="source-sha256" if source else "destination-readback", total_bytes=plan["payload_bytes"],
                            completed_bytes=progress.read+reused, completed_file_bytes=completed,
                            bytes_read=progress.read, reused_verified_bytes=reused, elapsed_seconds=elapsed))
                    last = elapsed
        require(completed == plan["payload_bytes"], "Full hash byte count differs")
        require(content.generation(root) == plan["source"], "Database generation changed during full hashing")
        if source:
            for row in plan["entries"]:
                content.check_source(root, row)
        else:
            require(content.tree_paths(root) == set(records), "Transferred tree has extra/missing published entries")
            for row in records.values():
                if row["kind"] == "file":
                    require(content.metadata(root/row["path"]) == row["metadata"],
                            "Destination changed after its full readback")
        return [records[name] for name in sorted(records)]
    finally:
        progress.cancelled.set()
        db.close()


def seal_source(source, inventory, inventory_sha256, output, *, workers=4, callback=None):
    plan = read_pinned(inventory, inventory_sha256)
    require(plan.get("kind") == KIND and plan.get("stage") == "source-inventory", "Invalid source inventory")
    with state_lock(Path(output)) as state:
        records = _hash_tree(Path(source), plan, state/"source-hashes.sqlite", source=True, workers=workers, callback=callback)
        lookup = {row["path"]: row for row in records}
        entries = [dict(row, **{k: lookup[row["path"]][k] for k in ("sha256", "edge_sha256")})
                   if row["kind"] == "file" else row for row in plan["entries"]]
        result = dict(schema=1, kind=KIND, stage="sealed-source", source=plan["source"], entries=entries,
                      payload_bytes=plan["payload_bytes"], inventory_sha256=inventory_sha256)
        pin = write_json(state/"source-manifest.json", result)
        return dict(schema=1, source_manifest=str(state/"source-manifest.json"), source_manifest_sha256=pin,
                    payload_bytes=plan["payload_bytes"], source=plan["source"], full_source_sha256=True)


def reuse_file(cache, owner, device, range_plan, range_plan_sha256, *, manifest=None,
               manifest_sha256=None, callback=None):
    """Reuse only a finished candidate after a fresh full read of its current bytes.

    Matching size/mode/mtime merely avoids hashing obvious partial files. A
    prior checkpoint is never evidence for admission; publication may reuse the
    new readback receipt only while every destination metadata field remains equal.
    """
    import aws_transfer
    cache = Path(cache)
    mounted_cache(cache, owner, device, False)
    owner = binding(owner)
    transfer = read_pinned(range_plan, range_plan_sha256)
    require(transfer.get("kind") == "aws-database-range-transfer" and transfer.get("schema") == 1
            and database_identity(binding(transfer["binding"])) == database_identity(owner)
            and transfer.get("device") == device, "Existing-file transfer identity differs")
    require(transfer.get("source", {}).get("source_manifest_sha256") == owner["source_manifest_sha256"]
            and transfer["source"].get("source_receipt_sha256") == owner["source_receipt_sha256"],
            "Existing-file source generation differs")
    root = cache/PENDING
    require(root.is_dir() and root.resolve() == root and root.stat().st_dev == cache.stat().st_dev,
            "Pending directory is not on the owned filesystem")
    require(all(not (cache/name).exists() and not (cache/name).is_symlink()
                for name in ("colabfold", "ready.json")), "Database is already published")
    row = transfer["entry"]
    require(row.get("kind") == "file", "Only regular inventory files may be reused")
    name = content.safe_path(row["path"])
    expected = row["source_metadata"]
    result = dict(schema=1, status="copy_required", path=name, size=expected["size"],
                  reused_verified_bytes=0, readback_bytes=0)
    parent = root
    for part in Path(name).parts[:-1]:
        parent = parent/part
        if not parent.exists() and not parent.is_symlink():
            return dict(result, reason="absent")
        require(parent.is_dir() and not parent.is_symlink() and parent.stat().st_dev == root.stat().st_dev,
                "Existing-file parent escaped its filesystem")
    path = aws_transfer.safe_file(root, name)
    if not path.exists():
        return dict(result, reason="absent")
    actual = content.metadata(path)
    if any(actual[key] != expected[key] for key in ("size", "mode", "mtime_ns")):
        return dict(result, reason="unfinished_metadata")
    if manifest is None:
        return dict(result, status="candidate", reason="full_readback_required")
    plan = read_pinned(manifest, manifest_sha256)
    require(plan.get("kind") == KIND and plan.get("stage") == "sealed-source"
            and plan.get("source") == transfer["source"]
            and plan.get("inventory_sha256") == transfer["inventory_sha256"],
            "Existing-file sealed source identity differs")
    entries = [entry for entry in plan["entries"] if entry["path"] == name]
    require(len(entries) == 1 and {k: v for k, v in entries[0].items() if k not in ("sha256", "edge_sha256")} == row
            and content.sha(entries[0].get("sha256")) and content.sha(entries[0].get("edge_sha256")),
            "Existing-file sealed source entry differs")
    progress = HashProgress()
    with state_lock(cache/STATE) as state, hash_readers(1, progress) as readers:
        # Deliberately ignore any older checkpoint, even when metadata matches.
        future = readers.submit(_hash_file, root, row, False, None, progress)
        started = time.monotonic()
        while not future.done():
            concurrent.futures.wait([future], timeout=1)
            if callback:
                callback(dict(stage="existing-file-readback", path=name, bytes_read=progress.read,
                              total_bytes=expected["size"], elapsed_seconds=time.monotonic()-started))
        checked, _ = future.result()
        require(content.metadata(path) == actual, "Existing file changed before reuse")
        if any(checked[key] != entries[0][key] for key in ("sha256", "edge_sha256")):
            return dict(result, reason="checksum_mismatch", readback_bytes=progress.read)
        checkpoint = state/"destination-hashes.sqlite"
        require(not checkpoint.is_symlink(), "Hash checkpoint is symlinked")
        db = sqlite3.connect(checkpoint)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS hashes (key TEXT PRIMARY KEY, receipt TEXT NOT NULL)")
            key = digest(dict(scope=digest(plan), source=False, path=name))
            db.execute("INSERT OR REPLACE INTO hashes VALUES (?, ?)", (key, canonical(checked).decode()))
            db.commit()
        finally:
            db.close()
    # Do not preserve/chmod/utime here: that would invalidate the real receipt.
    return dict(result, status="reused", reason="fresh_full_sha256", reused_verified_bytes=expected["size"],
                readback_bytes=progress.read, source_manifest_sha256=manifest_sha256,
                sha256=checked["sha256"], metadata=checked["metadata"])


def mounted_cache(cache, owner, device, readonly):
    """Portable mount checks use AWS identity, never a fabricated Verda receipt."""
    owner = binding(owner)
    cache = content.real_root(Path(cache))
    require(cache == MOUNTPOINT, "AWS cache mount path changed")
    observed = content.mount_record(cache)
    require(observed.get("filesystem_type") == "ext4" and observed.get("mountpoint") == str(cache), "AWS cache not an exact ext4 mount")
    info = Path(device).lstat()
    require(stat.S_ISBLK(info.st_mode) and observed.get("major_minor") == f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}",
            "AWS mount does not belong to the selected block device")
    uses = [row for row in linux._mounts(Path("/proc/self/mountinfo").read_text())
            if row["major_minor"] == observed["major_minor"]]
    require(len(uses) == 1 and uses[0]["target"] == str(cache), "EBS database has another active mount")
    native = json.loads(linux._run(["nvme", "id-ctrl", "--output-format=json", str(device)]).stdout)
    require(native.get("mn", "").strip() == "Amazon Elastic Block Store"
            and _ebs_serial(native.get("sn")) == owner["volume_id"], "Mounted EBS serial/model differs")
    require((Path("/dev/disk/by-uuid")/owner["filesystem_uuid"]).stat().st_rdev == info.st_rdev,
            "Mounted AWS filesystem UUID differs")
    require(int(linux._run(["blockdev", "--getsize64", str(device)]).stdout) == SIZE_BYTES, "AWS device size changed")
    require(cache.stat().st_dev == info.st_rdev, "AWS mount root belongs to another filesystem")
    options = set(observed.get("options", [])+observed.get("super_options", []))
    require({"nodev", "nosuid", "noexec"} <= options and ("ro" if readonly else "rw") in options
            and ("rw" if readonly else "ro") not in options
            and (not readonly or bool({"noload", "norecovery"} & options)), "AWS cache mount access mode differs")
    return observed


def check_ready(owner, raw, pin):
    owner = binding(owner)
    require(isinstance(raw, bytes) and len(raw) <= 65536 and content.sha(pin)
            and hashlib.sha256(raw).hexdigest() == pin, "AWS ready receipt differs from external pin")
    ready = json.loads(raw)
    require(all(ready.get(k) == v for k, v in database_identity(owner).items()) and ready.get("kind") == KIND
            and ready.get("status") == "ready" and ready.get("filesystem_type") == "ext4"
            and type(ready.get("size_bytes")) is int and ready["size_bytes"] == SIZE_BYTES
            and ready.get("completion", {}).get("full_readback") is True
            and ready.get("rootrel") == "colabfold" and content.sha(ready.get("content_manifest_sha256"))
            and content.sha(ready.get("source_content_manifest_sha256"))
            and (owner.get("ready_receipt_sha256") is None or owner["ready_receipt_sha256"] == pin),
            "AWS ready receipt identity/completion differs")
    return ready


def population_state(cache, owner, device):
    """Inspect owned staging; published is a claim requiring publish/verify proof."""
    cache = Path(cache)
    mounted_cache(cache, owner, device, False)
    roots = {}
    for name in (PENDING, "colabfold"):
        path = cache/name
        require(not path.is_symlink(), "AWS population root is symlinked")
        if path.exists():
            require(path.is_dir() and path.stat().st_dev == cache.stat().st_dev, "Population root crosses filesystem")
        roots[name] = path.exists()
    require(not all(roots.values()), "Both pending and final databases exist; do not recopy")
    result = dict(schema=1, kind=KIND, population_state="renamed" if roots["colabfold"] else
                  "pending" if roots[PENDING] else "absent", publication_verified=False)
    if (cache/"ready.json").exists() or (cache/"ready.json").is_symlink():
        raw = content.read_bytes(cache/"ready.json", 65536)
        pin = hashlib.sha256(raw).hexdigest()
        ready = check_ready(owner, raw, pin)
        require(roots["colabfold"] and not roots[PENDING], "Ready receipt has no sole final database")
        result.update(population_state="published", ready_receipt_sha256=pin,
                      source_content_manifest_sha256=ready["source_content_manifest_sha256"],
                      required_next_step="publish with the externally pinned sealed source; inspect alone cannot adopt readiness")
    else:
        require(owner.get("ready_receipt_sha256") is None, "Head-pinned ready receipt is missing on volume")
    return result


def publish(cache, owner, manifest, manifest_sha256, device, *, workers=4, callback=None):
    owner = binding(owner)
    cache = Path(cache)
    observed = population_state(cache, owner, device)
    plan = read_pinned(manifest, manifest_sha256)
    require(plan.get("kind") == KIND and plan.get("stage") == "sealed-source"
            and all(plan.get("source", {}).get(k) == owner[k] for k in ("source_manifest_sha256", "source_receipt_sha256")),
            "Source content manifest belongs to another full database")
    with state_lock(cache/STATE) as state:
        # Recover a crash after final rename but before ready publication without recopying.
        pending, final = cache/PENDING, cache/"colabfold"
        require(not (pending.exists() and final.exists()), "Both unpublished and final databases exist")
        root = final if final.exists() else pending
        records = _hash_tree(root, plan, state/"destination-hashes.sqlite", source=False, workers=workers, callback=callback)
        databases.validate(root)
        if observed["population_state"] == "published":
            raw = content.read_bytes(cache/"ready.json", 65536)
            ready = check_ready(owner, raw, observed["ready_receipt_sha256"])
            require(ready["source_content_manifest_sha256"] == manifest_sha256 and ready["source"] == plan["source"]
                    and read_pinned(state/"content.json", ready["content_manifest_sha256"]) == dict(schema=1, entries=records)
                    and ready["completion"].get("payload_bytes") == plan["payload_bytes"]
                    and ready["completion"].get("files") == sum(r["kind"] == "file" for r in records)
                    and ready["completion"].get("source_bytes_hashed") == plan["payload_bytes"]
                    and ready["completion"].get("destination_bytes_readback") == plan["payload_bytes"],
                    "Existing publication does not match the independently sealed source and full readback")
            return dict(ready=ready, ready_receipt_sha256=observed["ready_receipt_sha256"], already_ready=True)
        manifest_pin = write_json(state/"content.json", dict(schema=1, entries=records))
        if root == pending:
            os.rename(pending, final)
            content.sync_directory(cache)
        ready = dict(**database_identity(owner), kind=KIND, status="ready", rootrel="colabfold",
            filesystem_type="ext4", size_bytes=SIZE_BYTES, source=plan["source"],
            source_content_manifest_sha256=manifest_sha256, content_manifest_sha256=manifest_pin,
            completion=dict(full_readback=True, files=sum(r["kind"] == "file" for r in records),
                payload_bytes=plan["payload_bytes"], source_bytes_hashed=plan["payload_bytes"],
                destination_bytes_readback=plan["payload_bytes"]), completed_epoch=time.time())
        pin = write_json(cache/"ready.json", ready)
        return dict(ready=ready, ready_receipt_sha256=pin)


def verify(cache, owner, device, *, full=False):
    cache = Path(cache)
    mounted_cache(cache, owner, device, True)
    raw = content.read_bytes(cache/"ready.json", 65536)
    ready = check_ready(owner, raw, owner.get("ready_receipt_sha256"))
    manifest = read_pinned(cache/STATE/"content.json", ready["content_manifest_sha256"])
    root = content.real_root(cache/"colabfold")
    require(content.generation(root) == ready["source"], "Ready AWS database generation changed")
    names, size, files = set(), 0, 0
    for row in manifest["entries"]:
        name = content.safe_path(row["path"], root=True)
        require(name not in names, "Duplicate AWS content path")
        names.add(name)
        path = root/name
        require(path.parent.resolve() == path.parent and path.lstat().st_dev == root.stat().st_dev, "AWS content escaped mount")
        if row["kind"] == "file":
            content.check_file(path, row, full=full, device=root.stat().st_dev)
            size += row["metadata"]["size"]
            files += 1
        elif row["kind"] == "directory":
            require(stat.S_ISDIR(path.lstat().st_mode), "AWS database directory changed")
        else:
            require(row["kind"] == "symlink" and path.is_symlink() and os.readlink(path) == row["target"]
                    and path.resolve(strict=True).is_relative_to(root/Path(name).parts[0]), "AWS alias differs")
    require(content.tree_paths(root) == names and ready["completion"]["payload_bytes"] == size
            and ready["completion"]["files"] == files, "AWS ready content coverage/count differs")
    databases.validate(root)
    return dict(status="verified", kind=KIND, **database_identity(owner),
                ready_receipt_sha256=owner["ready_receipt_sha256"], database=str(root),
                verification="full-sha256" if full else "pinned-receipt-metadata-edges")


def restore_runtime(archive, plan_path, plan_sha256, tools, runtime_root):
    """Extract an externally pinned package once; return original-path bind plans."""
    plan = read_pinned(plan_path, plan_sha256)
    package = plan.get("package", {})
    require(plan.get("recipe") == "msa" and package.get("format") == "bio-runtime-tar-zstd-v1"
            and all(package.get(k) == plan.get(k) for k in ("paths", "source_bytes", "source_files", "source_fingerprint"))
            and type(package.get("archive_bytes")) is int and content.sha(package.get("archive_sha256"))
            and "envs/msa-tools-v1" in plan.get("paths", []), "Invalid pinned MSA runtime package plan")
    for relative in plan["paths"]:
        content.safe_path(relative)
    require(len(plan["paths"]) == len(set(plan["paths"])), "Duplicate packed runtime root")
    archive, root, tools = Path(archive), Path(runtime_root), Path(tools)
    require(root == RUNTIME_ROOT and root.resolve() == root and archive.resolve() == archive,
            "Runtime staging requires the dedicated real OS paths")
    content.regular(archive)
    before = content.metadata(archive)
    require(before["size"] == package["archive_bytes"], "Runtime archive size differs")
    with archive.open("rb") as handle:
        require(content.hash_handle(handle) == package["archive_sha256"], "Runtime archive SHA differs")
    require(content.metadata(archive) == before, "Runtime archive changed during verification")
    py = tools/"py"
    sys.path.insert(0, str(py))
    spec = importlib.util.spec_from_file_location("aws_pinned_runtime_package", py/"runtime_package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    marker = root/".runtime-archive-receipt.json"
    expected = dict(schema=1, plan_sha256=plan_sha256, archive_sha256=package["archive_sha256"])
    if root.exists():
        require(content.load(marker) == expected, "Existing runtime is not bound to this immutable archive")
    else:
        require(shutil.disk_usage(root.parent).free >= plan["source_bytes"]+20*1024**3, "OS disk lacks runtime plus scratch reserve")
        pending = root.parent/".bio-worker-runtime.pending"
        require(not pending.exists() and not pending.is_symlink(), "Interrupted runtime extraction requires explicit owned cleanup")
        pending.mkdir(mode=0o700)
        module.extract(archive, pending, plan)
        write_json(pending/marker.name, expected)
        os.rename(pending, root)
        content.sync_directory(root.parent)
    native = databases.tools(root/"envs/msa-tools-v1")
    require(all((root/p).resolve() == root/p and (root/p).is_dir() for p in plan["paths"]),
            "Packed runtime selected root is redirected or missing")
    commands = [["mount", "--bind", str(root/relative), "/mnt/bio-shared/"+relative] for relative in plan["paths"]]
    return dict(schema=1, status="runtime_restored", receipt=expected, native_tools=native,
                runtime_root=str(root), bind_commands=commands,
                search_profile=search_profile.resolve(search_profile.LEGACY_PROFILE))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect-device", "initialize-plan", "mount-plan", "validate-mount",
        "source-inventory", "transfer-plan", "seal-source", "population-state", "reuse-file", "publish", "verify", "restore-runtime"))
    for name in ("binding", "provider-evidence", "evidence", "ready", "source", "out", "inventory", "identity",
                 "known-hosts", "cache", "source-manifest", "archive", "plan", "range-plan", "tools", "runtime-root"):
        parser.add_argument("--"+name, type=Path)
    for name in ("volume-id", "ready-sha256", "inventory-sha256", "ssh-host", "source-manifest-sha256", "device", "plan-sha256", "range-plan-sha256"):
        parser.add_argument("--"+name)
    parser.add_argument("--mode", choices=("populate", "serve"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args(argv)
    progress = lambda row: print(json.dumps(row, sort_keys=True), file=sys.stderr, flush=True)
    if args.action == "inspect-device":
        result = inspect_device(args.volume_id)
    elif args.action in ("initialize-plan", "mount-plan", "validate-mount"):
        owner, provider, evidence = [content.load(p) for p in (args.binding, args.provider_evidence, args.evidence)]
        action = "initialize" if args.action == "initialize-plan" else "mount-"+str(args.mode) if args.action == "mount-plan" else args.action
        result = device_plan(action, owner, provider, evidence,
            ready=content.read_bytes(args.ready, 65536) if args.ready else None, ready_sha256=args.ready_sha256)
    elif args.action == "source-inventory":
        result = source_inventory(args.source, args.out)
    elif args.action == "transfer-plan":
        result = transfer_plan(args.source, args.inventory, args.inventory_sha256, args.out, args.ssh_host, args.identity, args.known_hosts)
    elif args.action == "seal-source":
        result = seal_source(args.source, args.inventory, args.inventory_sha256, args.out, workers=args.workers, callback=progress)
    elif args.action == "publish":
        result = publish(args.cache, content.load(args.binding), args.source_manifest, args.source_manifest_sha256,
                         args.device, workers=args.workers, callback=progress)
    elif args.action == "population-state":
        result = population_state(args.cache, content.load(args.binding), args.device)
    elif args.action == "reuse-file":
        result = reuse_file(args.cache, content.load(args.binding), args.device, args.range_plan, args.range_plan_sha256,
                            manifest=args.source_manifest, manifest_sha256=args.source_manifest_sha256, callback=progress)
    elif args.action == "verify":
        result = verify(args.cache, content.load(args.binding), args.device, full=args.full)
    else:
        result = restore_runtime(args.archive, args.plan, args.plan_sha256, args.tools, args.runtime_root)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
        print(json.dumps(dict(schema=1, status="bootstrap_failed", error=str(exc))), file=sys.stderr)
        sys.exit(2)
