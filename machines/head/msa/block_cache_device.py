#!/usr/bin/env python3
"""Read-only device evidence and plans for one owned 1300 GiB MSA cache.

This module never formats, mounts, unmounts, or modifies a block device. The
trusted head controller supplies sealed creation/managed-lease receipts and
fresh, complete provider inventory. An explicit initialize-plan can produce a
mkfs command only for the exact newly created, attached, blank non-OS device.
The controller must revalidate immediately before executing any returned plan;
a JSON plan does not lock a device or authorize unrelated operations.

Verda documents the volume ``target`` field in GetVolumePublicResponseDto:
https://api.verda.com/v1/openapi.json
https://docs.verda.com/storage/block-volumes/attaching-a-block-volume/
No documented volume-ID-to-serial transformation is assumed. Missing targets,
conflicting identities, or inconclusive probes require inspection. The CLI
calls volume sizes GiB while the API schema says GB, so both provider size=1300
and the exact guest byte count are required; no decimal/binary guess is made.
"""
import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid


SCHEMA = 1
SIZE_GIB = 1300
SIZE_BYTES = SIZE_GIB * 1024**3
MOUNTPOINT = "/mnt/bio-msa-databases"
MAX_AGE_SECONDS = 120
# Provider envelopes are stamped on the head, while this check runs on a
# newly booted guest. Bound ordinary clock skew without increasing the maximum
# past age or allowing future timestamps in guest-local device observations.
MAX_PROVIDER_CLOCK_SKEW_SECONDS = 5
DEVICE = re.compile(r"/dev/(?:vd[a-z]+|sd[a-z]+|nvme[0-9]+n[0-9]+)\Z")
MAJOR_MINOR = re.compile(r"[0-9]+:[0-9]+\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
LSBLK_COLUMNS = "NAME,KNAME,PATH,TYPE,SIZE,MAJ:MIN,FSTYPE,UUID,PTTYPE,MOUNTPOINTS,PKNAME,SERIAL,WWN,RO"


class InspectionRequired(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InspectionRequired(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def identifier(value, label):
    require(isinstance(value, str), f"Missing {label}")
    try:
        require(str(uuid.UUID(value)) == value, f"Noncanonical {label}")
    except ValueError as exc:
        raise InspectionRequired(f"Invalid {label}") from exc
    return value


def fresh(value, now, label, *, future_seconds=0):
    require(type(value) in (int, float) and math.isfinite(value)
            and type(now) in (int, float) and math.isfinite(now), f"Invalid {label} timestamp")
    age = now-value
    require(-future_seconds <= age <= MAX_AGE_SECONDS,
            f"Stale or invalid {label}: age {age:.6f}s outside {-future_seconds}..{MAX_AGE_SECONDS}s")
    return age


def epoch(value):
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(moment.tzinfo is not None, "Creation timestamp has no timezone")
        return moment.timestamp()
    except (AttributeError, TypeError, ValueError) as exc:
        raise InspectionRequired("Invalid creation timestamp") from exc


def unique(rows, label):
    require(isinstance(rows, list), f"Incomplete {label} inventory")
    found = {}
    for row in rows:
        require(isinstance(row, dict), f"Malformed {label} row")
        ident = identifier(row.get("id"), label+" ID")
        require(ident not in found, f"Duplicate {label} ID")
        found[ident] = row
    return found


def attachments(volume, instances):
    linked = set()
    recipients = volume.get("instances")
    require(isinstance(recipients, list), "Missing volume attachments")
    for item in recipients:
        linked.add(identifier(item.get("id") if isinstance(item, dict) else item, "attachment ID"))
    if volume.get("instance_id"):
        linked.add(identifier(volume["instance_id"], "attached instance ID"))
    for ident, instance in instances.items():
        refs = instance.get("volume_ids")
        require(isinstance(refs, list) and len(refs) == len(set(refs)), "Incomplete instance volume references")
        for ref in refs:
            identifier(ref, "instance volume ID")
        if volume["id"] in refs or instance.get("os_volume_id") == volume["id"]:
            linked.add(ident)
    return linked


def provider_binding(ownership, provider, now):
    """Require the trusted creation and lease to match fresh provider objects."""
    require(isinstance(ownership, dict) and type(ownership.get("schema")) is int and ownership["schema"] == SCHEMA,
            "Missing cache ownership receipt")
    cache = ownership.get("cache_id")
    require(isinstance(cache, str) and re.fullmatch(r"[0-9a-f]{32}", cache), "Invalid cache ID")
    volume_id = identifier(ownership.get("volume_id"), "cache volume ID")
    filesystem_uuid = identifier(ownership.get("filesystem_uuid"), "filesystem UUID")
    for key in ("cache_generation", "source_manifest_sha256", "source_receipt_sha256"):
        require(isinstance(ownership.get(key), str) and SHA256.fullmatch(ownership[key]), f"Invalid {key}")
    creation = ownership.get("creation")
    require(isinstance(creation, dict) and creation.get("method") == "POST"
            and creation.get("path") == "/volumes" and creation.get("response_id") == volume_id,
            "Cache is not bound to an exact new-volume POST receipt")
    request = creation.get("request")
    require(isinstance(request, dict)
            and set(request) <= {"type", "size", "location_code", "name", "instance_ids", "tags"}
            and request.get("type") == "NVMe" and type(request.get("size")) is int
            and request["size"] == SIZE_GIB and request.get("location_code") == "FIN-02"
            and request.get("name") == "bio-msa-cache-"+cache
            and request.get("instance_ids", []) == [], "New-volume creation parameters differ")
    attempted = epoch(creation.get("attempted_at"))
    require(isinstance(provider, dict), "Missing provider inventory")
    fresh(provider.get("observed_epoch"), now, "provider inventory",
          future_seconds=MAX_PROVIDER_CLOCK_SKEW_SECONDS)
    instances = unique(provider.get("instances"), "instance")
    volumes = unique(provider.get("volumes"), "volume")
    trash = unique(provider.get("trash"), "trash")
    require(volume_id not in trash, "Cache volume appears in trash")
    managed = provider.get("managed")
    require(isinstance(managed, dict) and isinstance(managed.get("token"), str)
            and re.fullmatch(r"[0-9a-f]{32}", managed["token"]), "Missing managed worker ownership")
    instance_id = identifier(managed.get("instance_id"), "managed instance ID")
    os_id = identifier(managed.get("os_volume_id"), "managed OS volume ID")
    boot_id = identifier(managed.get("boot_id"), "managed guest boot ID")
    require(volume_id != os_id and instance_id in instances and os_id in volumes and volume_id in volumes,
            "Cache/OS/instance identity is missing or conflicts")
    instance, os_volume, volume = instances[instance_id], volumes[os_id], volumes[volume_id]
    require(instance.get("status") == "running" and instance.get("os_volume_id") == os_id,
            "Managed instance or its OS volume changed")
    require(volume_id in instance.get("volume_ids", []), "Instance does not reference the cache volume")
    require(volume.get("type") == "NVMe" and volume.get("is_os_volume") is False
            and volume.get("status") == "attached" and volume.get("instance_id") == instance_id
            and type(volume.get("size")) in (int, float) and volume["size"] == SIZE_GIB
            and volume.get("name") == request["name"] and volume.get("location") == "FIN-02",
            "Cache volume type, size, attachment, or creation identity differs")
    require(attempted-5 <= epoch(volume.get("created_at")) <= now,
            "Volume predates its new-volume allocation receipt")
    require(os_volume.get("is_os_volume") is True and os_volume.get("type") == "NVMe"
            and os_volume.get("status") == "attached" and os_volume.get("instance_id") == instance_id,
            "OS volume ownership cannot be established")
    require(attachments(volume, instances) == {instance_id}
            and attachments(os_volume, instances) == {instance_id}, "Volume attachments are not exclusive")
    target, os_target = "/dev/"+str(volume.get("target", "")), "/dev/"+str(os_volume.get("target", ""))
    require(DEVICE.fullmatch(target) and DEVICE.fullmatch(os_target) and target != os_target,
            "Missing, unsupported, or conflicting provider device targets")
    for ident, other in volumes.items():
        if ident != volume_id and instance_id in attachments(other, instances):
            require(other.get("target") != volume["target"], "Provider assigns the cache target to another volume")
    return dict(cache_id=cache, volume_id=volume_id, instance_id=instance_id, os_volume_id=os_id,
                device=target, os_device=os_target, filesystem_uuid=filesystem_uuid,
                filesystem_type="ext4", size_bytes=SIZE_BYTES, boot_id=boot_id)


def device_nodes(snapshot):
    require(isinstance(snapshot, dict) and isinstance(snapshot.get("blockdevices"), list), "Missing lsblk inventory")
    nodes = {}

    def add(row, ancestors):
        require(isinstance(row, dict), "Malformed lsblk row")
        path, major = row.get("path"), row.get("maj:min")
        require(isinstance(path, str) and path.startswith("/dev/") and path not in nodes
                and isinstance(major, str) and MAJOR_MINOR.fullmatch(major), "Ambiguous guest block-device topology")
        nodes[path] = (row, ancestors)
        children = row.get("children", [])
        require(isinstance(children, list), "Invalid block-device children")
        for child in children:
            add(child, (*ancestors, path))
    for row in snapshot["blockdevices"]:
        add(row, ())
    return nodes


def guest_binding(binding, evidence, now, *, mounted=False):
    require(isinstance(evidence, dict) and type(evidence.get("schema")) is int and evidence["schema"] == SCHEMA,
            "Missing guest evidence")
    fresh(evidence.get("observed_epoch"), now, "guest device evidence")
    require(identifier(evidence.get("boot_id"), "guest boot ID") == binding["boot_id"],
            "Evidence belongs to another guest or boot")
    require(evidence.get("device") == binding["device"], "Guest probe targets another device")
    nodes = device_nodes(evidence.get("lsblk"))
    path = binding["device"]
    require(path in nodes and binding["os_device"] in nodes, "Provider targets are absent from guest")
    node, ancestors = nodes[path]
    require(node.get("type") == "disk" and not ancestors and not node.get("children")
            and type(node.get("size")) is int and node["size"] == SIZE_BYTES
            and node.get("kname") == Path(path).name and node.get("pkname") in (None, ""),
            "Cache must be an exact-size whole disk without partitions or parents")
    check = evidence.get("stat")
    require(isinstance(check, dict) and check.get("is_block") is True
            and check.get("is_symlink") is False and check.get("major_minor") == node["maj:min"],
            "Guest device path/stat identity changed")
    require(evidence.get("holders") == [], "Cache device has holders or incomplete holder evidence")
    mounts = evidence.get("mounts")
    require(isinstance(mounts, list) and all(isinstance(m, dict) for m in mounts), "Missing guest mount inventory")
    root = [m for m in mounts if m.get("target") == "/"]
    require(len(root) == 1, "Cannot establish guest root filesystem")
    root_paths = [p for p, (n, a) in nodes.items() if n["maj:min"] == root[0].get("major_minor")
                  and (p == binding["os_device"] or binding["os_device"] in a)]
    require(len(root_paths) == 1 and root[0]["major_minor"] != node["maj:min"],
            "Provider OS target is not the actual root filesystem")
    uses = [m for m in mounts if m.get("major_minor") == node["maj:min"]]
    points = node.get("mountpoints")
    require(isinstance(points, list) and all(p is None or isinstance(p, str) for p in points),
            "Missing lsblk mountpoint evidence")
    if not mounted:
        require(not uses and not any(points), "Cache disk is already mounted or used as swap")
    require(node.get("pttype") in (None, ""), "Cache disk contains a partition table")
    for key in ("serial", "wwn"):
        require(key in node and (node[key] is None or isinstance(node[key], str)), f"Missing {key} evidence")
    return node, uses


def probe(evidence):
    result = evidence.get("probe")
    require(isinstance(result, dict), "Missing independent filesystem probes")
    wipefs, blkid = result.get("wipefs"), result.get("blkid")
    require(isinstance(wipefs, dict) and type(wipefs.get("returncode")) is int
            and wipefs["returncode"] == 0 and isinstance(wipefs.get("signatures"), list),
            "wipefs read-only probe was inconclusive")
    require(isinstance(blkid, dict) and type(blkid.get("returncode")) is int
            and isinstance(blkid.get("fields"), dict), "blkid probe was inconclusive")
    return wipefs, blkid


def plan_base(action, ownership, provider, evidence, now):
    binding = provider_binding(ownership, provider, now)
    require(isinstance(evidence, dict), "Missing guest evidence")
    fresh(evidence.get("observed_epoch"), now, "guest device evidence")
    return dict(schema=SCHEMA, status="validated_plan", action=action, **binding,
                ownership_sha256=digest(ownership), provider_sha256=digest(provider),
                evidence_sha256=digest(evidence),
                provider_age_seconds=now-provider["observed_epoch"],
                provider_clock_skew_allowance_seconds=MAX_PROVIDER_CLOCK_SKEW_SECONDS,
                issued_epoch=now, expires_epoch=min(now+30, provider["observed_epoch"]+MAX_AGE_SECONDS,
                                                   evidence.get("observed_epoch", 0)+MAX_AGE_SECONDS),
                execution="not_executed; controller must revalidate before use")


def initialize_plan(ownership, provider, evidence, *, now=None):
    now = time.time() if now is None else now
    result = plan_base("initialize", ownership, provider, evidence, now)
    require(ownership.get("ready_receipt_sha256") is None, "A previously ready cache can never be initialized again")
    node, _ = guest_binding(result, evidence, now)
    require(node.get("ro") is False and node.get("fstype") in (None, "")
            and node.get("uuid") in (None, ""), "Cache is read-only or already contains a filesystem")
    wipefs, blkid = probe(evidence)
    require(wipefs["signatures"] == [] and blkid["returncode"] == 2 and blkid["fields"] == {},
            "Cache is not a blank newly created device; never erase existing signatures")
    result["device_identity"] = {"major_minor": node["maj:min"], "serial": node["serial"], "wwn": node["wwn"]}
    result["command"] = ["mkfs.ext4", "-U", result["filesystem_uuid"], "-L", "bio-msa-cache", "-m", "0",
                         "-E", "lazy_itable_init=0,lazy_journal_init=0,nodiscard", result["device"]]
    return result


def filesystem_binding(ownership, provider, evidence, *, now=None, mounted=False):
    now = time.time() if now is None else now
    result = plan_base("verify_filesystem", ownership, provider, evidence, now)
    node, uses = guest_binding(result, evidence, now, mounted=mounted)
    wipefs, blkid = probe(evidence)
    require(node.get("fstype") == "ext4" and node.get("uuid") == result["filesystem_uuid"]
            and blkid["returncode"] == 0 and blkid["fields"].get("TYPE") == "ext4"
            and blkid["fields"].get("UUID") == result["filesystem_uuid"]
            and not any(k.startswith("PT") for k in blkid["fields"]), "Actual filesystem identity differs")
    require(wipefs["signatures"] and all(isinstance(s, dict) and s.get("type") == "ext4"
            and s.get("uuid") == result["filesystem_uuid"] for s in wipefs["signatures"]),
            "Filesystem signatures are missing, conflicting, or ambiguous")
    matches = [n for n, _ in device_nodes(evidence["lsblk"]).values() if n.get("uuid") == result["filesystem_uuid"]]
    require(len(matches) == 1, "Filesystem UUID is duplicated on guest devices")
    result["major_minor"] = node["maj:min"]
    result["device_identity"] = {"major_minor": node["maj:min"], "serial": node["serial"], "wwn": node["wwn"]}
    return result, uses


def check_ready(ownership, ready_bytes, ready_sha256):
    require(isinstance(ready_bytes, bytes) and isinstance(ready_sha256, str) and SHA256.fullmatch(ready_sha256)
            and hashlib.sha256(ready_bytes).hexdigest() == ready_sha256,
            "Cache ready receipt bytes do not match their external pin")
    ready = json.loads(ready_bytes)
    require(isinstance(ready, dict) and type(ready.get("schema")) is int and ready["schema"] == SCHEMA
            and ready.get("kind") == "msa-full-database-block-cache" and ready.get("status") == "ready"
            and isinstance(ready.get("completion"), dict) and ready["completion"].get("full_readback") is True,
            "Cache receipt does not describe a completed full readback")
    for key in ("cache_id", "volume_id", "filesystem_uuid", "cache_generation",
                "source_manifest_sha256", "source_receipt_sha256"):
        require(ready.get(key) == ownership[key], f"Ready cache {key} differs")
    require(ready.get("filesystem_type") == "ext4" and type(ready.get("size_bytes")) is int
            and ready["size_bytes"] == SIZE_BYTES, "Ready cache filesystem/size differs")
    expected = ownership.get("ready_receipt_sha256")
    require(expected is None or expected == ready_sha256, "Registered ready receipt changed")


def mount_plan(ownership, provider, evidence, ready, ready_sha256, *, now=None):
    check_ready(ownership, ready, ready_sha256)
    result, _ = filesystem_binding(ownership, provider, evidence, now=now)
    target = evidence.get("mountpoint")
    require(isinstance(target, dict) and target.get("path") == MOUNTPOINT
            and target.get("is_symlink") is False and target.get("entries") == []
            and (target.get("exists") is False or (target.get("exists") is True and target.get("is_directory") is True)),
            "Cache mountpoint is not an empty, real directory or absent path")
    require(not any(m.get("target") == MOUNTPOINT or m.get("target", "").startswith(MOUNTPOINT+"/")
                    for m in evidence["mounts"]), "Cache mountpoint already contains a mount")
    result.update(action="mount_read_only", ready_sha256=ready_sha256,
                  command=["mount", "-t", "ext4", "-o", "ro,noload,nodev,nosuid,noexec",
                           "UUID="+result["filesystem_uuid"], MOUNTPOINT])
    return result


def validate_mount(ownership, provider, evidence, ready, ready_sha256, *, now=None):
    check_ready(ownership, ready, ready_sha256)
    result, uses = filesystem_binding(ownership, provider, evidence, now=now, mounted=True)
    require(len(uses) == 1 and uses[0].get("target") == MOUNTPOINT and uses[0].get("fstype") == "ext4",
            "Cache is mounted elsewhere or more than once")
    options = set(uses[0].get("options", []))
    require({"ro", "nodev", "nosuid", "noexec"} <= options and "rw" not in options
            and bool({"noload", "norecovery"} & options), "Cache mount is not read-only without journal replay")
    result.update(action="validate_read_only_mount", status="validated_mount", ready_sha256=ready_sha256)
    return result


def _run(arguments, accepted=(0,)):
    result = subprocess.run(arguments, text=True, capture_output=True, timeout=30, check=False)
    require(result.returncode in accepted and len(result.stdout) <= 8*1024**2,
            f"Read-only probe failed: {arguments[0]}")
    return result


def _mounts(text):
    def unescape(value):
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)
    rows = []
    for line in text.splitlines():
        left, right = line.split(" - ", 1)
        fields, filesystem = left.split(), right.split()
        require(len(fields) >= 6 and len(filesystem) >= 3, "Malformed mountinfo")
        rows.append(dict(major_minor=fields[2], target=unescape(fields[4]),
                         options=sorted(set(fields[5].split(",")+filesystem[2].split(","))),
                         fstype=filesystem[0], source=unescape(filesystem[1])))
    return rows


def inspect(device):
    """Read only. Inconclusive commands/device changes fail closed."""
    require(isinstance(device, str) and DEVICE.fullmatch(device), "An exact supported whole-device path is required")
    path = Path(device)
    before = path.lstat()
    require(stat.S_ISBLK(before.st_mode), "Target must be a real block device, not a file or symlink")
    lsblk = json.loads(_run(["lsblk", "--json", "--bytes", "--output", LSBLK_COLUMNS]).stdout)
    wipes = json.loads(_run(["wipefs", "--no-act", "--json", "--output", "TYPE,UUID", device]).stdout)
    identified = _run(["blkid", "--probe", "--output", "export", device], (0, 2))
    require(identified.returncode != 2 or not identified.stderr.strip(), "blkid reported a probe error")
    fields = {}
    for line in identified.stdout.splitlines():
        key, value = line.split("=", 1)
        require(key not in fields, "Duplicate blkid output")
        fields[key] = value
    holders = sorted(p.name for p in Path("/sys/class/block", path.name, "holders").iterdir())
    mountinfo = Path("/proc/self/mountinfo").read_text()
    # A concurrent partition/mount/identity change invalidates the observation.
    require(lsblk == json.loads(_run(["lsblk", "--json", "--bytes", "--output", LSBLK_COLUMNS]).stdout)
            and mountinfo == Path("/proc/self/mountinfo").read_text(), "Guest device/mount inventory changed during inspection")
    after = path.lstat()
    require((before.st_mode, before.st_rdev, before.st_ino) == (after.st_mode, after.st_rdev, after.st_ino),
            "Guest device node changed during inspection")
    target = Path(MOUNTPOINT)
    real_path = target.resolve(strict=False) == target
    exists = target.exists()
    directory = target.is_dir() if exists else None
    entries = sorted(p.name for p in target.iterdir())[:2] if directory and real_path else []
    return dict(schema=SCHEMA, observed_epoch=time.time(), boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                device=device, stat=dict(is_block=True, is_symlink=False,
                    major_minor=f"{os.major(after.st_rdev)}:{os.minor(after.st_rdev)}"),
                lsblk=lsblk, holders=holders, mounts=_mounts(mountinfo),
                probe=dict(wipefs=dict(returncode=0, signatures=wipes.get("signatures")),
                           blkid=dict(returncode=identified.returncode, fields=fields)),
                mountpoint=dict(path=MOUNTPOINT, exists=exists, is_directory=directory,
                                is_symlink=not real_path, entries=entries))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "initialize-plan", "filesystem", "mount-plan", "validate-mount"))
    parser.add_argument("--device")
    parser.add_argument("--ownership", type=Path); parser.add_argument("--provider", type=Path)
    parser.add_argument("--evidence", type=Path); parser.add_argument("--ready", type=Path)
    parser.add_argument("--ready-sha256")
    args = parser.parse_args()
    if args.action == "inspect":
        result = inspect(args.device)
    else:
        require(args.ownership is not None and args.provider is not None and args.evidence is not None,
                "Plans require ownership, provider, and guest evidence documents")
        ownership, provider, evidence = [json.loads(p.read_text()) for p in (args.ownership, args.provider, args.evidence)]
        if args.action == "initialize-plan":
            result = initialize_plan(ownership, provider, evidence)
        elif args.action == "filesystem":
            result, _ = filesystem_binding(ownership, provider, evidence)
        else:
            require(args.ready is not None and args.ready_sha256 is not None, "Mounting requires the pinned ready receipt")
            operation = mount_plan if args.action == "mount-plan" else validate_mount
            result = operation(ownership, provider, evidence, args.ready.read_bytes(), args.ready_sha256)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (InspectionRequired, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps(dict(schema=SCHEMA, status="inspection_required", error=str(exc))), file=sys.stdout)
        sys.exit(2)
