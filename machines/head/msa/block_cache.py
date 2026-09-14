#!/usr/bin/env python3
"""Resumable, byte-verified copies of the published full MSA database.

This helper never allocates, formats or mounts storage. The bootstrap attests the
provider volume ID and device; this code independently checks the mounted device,
filesystem UUID and access mode. Normal verification uses a head-pinned receipt,
portable filesystem metadata and file edges; --full requests complete hashing.
"""
import argparse
import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import tempfile
import threading
import time
import uuid

import databases

KIND = "msa-full-database-block-cache"
SIZE_BYTES = 1300 * 1024**3
CHUNK = 8 * 1024**2
EDGE = 65536
MAX_FILES = 500_000
MAX_JSON = 256 * 1024**2
STATE = ".block-cache"
PENDING = ".colabfold.pending"
GENERATION_FILES = (".msa-databases.json",) + tuple(
    ".components/" + name + ".json" for name in databases.COMPONENTS)


def require(value, message):
    if not value:
        raise RuntimeError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def safe_path(value, *, root=False):
    require(isinstance(value, str) and len(value) <= 4096 and value
            and not any(ord(c) < 32 for c in value), "Invalid cache relative path")
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts
            and str(path) == value and (value != "." or root), "Unsafe cache relative path")
    return value


def real_root(path):
    path = Path(path).absolute()
    require(path.is_dir() and path.resolve() == path, "Root must be an existing real directory")
    return path


def metadata(path):
    info = path.lstat()
    return dict(inode=info.st_ino, size=info.st_size, mode=stat.S_IMODE(info.st_mode),
                uid=info.st_uid, gid=info.st_gid, mtime_ns=info.st_mtime_ns,
                ctime_ns=info.st_ctime_ns)


def portable_metadata(path, kind):
    value = metadata(path)
    if kind == "directory":
        # NFS directory byte accounting is volatile and has no content meaning.
        value.pop("size")
    return value


def regular(path):
    require(stat.S_ISREG(path.lstat().st_mode), "Expected a regular file: " + str(path))


def read_bytes(path, limit=MAX_JSON):
    require(path.parent.resolve() == path.parent.absolute(), "Metadata parent is symlinked")
    regular(path)
    require(path.stat().st_size <= limit, "Oversized cache metadata: " + str(path))
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
        raw = handle.read(limit + 1)
    require(len(raw) <= limit, "Oversized cache metadata")
    return raw


def load(path, limit=MAX_JSON):
    return json.loads(read_bytes(path, limit))


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    if path.exists() or path.is_symlink():
        regular(path)
    raw = canonical(value) + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def owned_directory(path, device=None):
    if path.exists() or path.is_symlink():
        require(stat.S_ISDIR(path.lstat().st_mode), "Cache directory is not real: " + str(path))
    else:
        path.mkdir(mode=0o700)
    if device is not None:
        require(path.stat().st_dev == device, "Cache path crosses onto another filesystem")


def binding(value):
    require(isinstance(value, dict) and type(value.get("schema")) is int and value["schema"] == 1
            and re.fullmatch(r"[0-9a-f]{32}", str(value.get("cache_id", ""))), "Invalid cache ownership binding")
    for field in ("volume_id", "filesystem_uuid"):
        require(isinstance(value.get(field), str) and str(uuid.UUID(value[field])) == value[field],
                "Invalid cache " + field)
    for field in ("source_manifest_sha256", "source_receipt_sha256", "cache_generation"):
        require(sha(value.get(field)), "Missing cache binding: " + field)
    require(value["source_manifest_sha256"] == databases.MANIFEST_SHA256,
            "Cache is bound to another scientific database manifest")
    generation = dict(schema=1, **{field: value[field] for field in
        ("cache_id", "source_manifest_sha256", "source_receipt_sha256")})
    require(value["cache_generation"] == digest(generation), "Cache generation binding changed")
    ready = value.get("ready_receipt_sha256")
    require(ready is None or sha(ready), "Invalid pinned ready receipt SHA")
    return {field: value[field] for field in ("cache_id", "volume_id", "filesystem_uuid",
            "cache_generation", "source_manifest_sha256", "source_receipt_sha256")} | {
                "schema": 1, "ready_receipt_sha256": ready}


def identity(value):
    return {key: val for key, val in binding(value).items() if key != "ready_receipt_sha256"}


def mount_record(path):
    def unescape(value):
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)
    records = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        split = fields.index("-")
        point = Path(unescape(fields[4]))
        if path == point or path.is_relative_to(point):
            records.append(dict(mountpoint=str(point), major_minor=fields[2],
                options=fields[5].split(","), filesystem_type=fields[split+1],
                source=unescape(fields[split+2]), super_options=fields[split+3].split(",")))
    require(records, "No mount provenance for " + str(path))
    return max(records, key=lambda row: len(row["mountpoint"]))


def source_mount(source):
    row = mount_record(source)
    require(row["filesystem_type"] in {"nfs", "nfs4"} and "ro" in row["options"]
            and "vers=4.1" in row["super_options"], "Source must be a read-only NFS 4.1 mount")
    require(re.fullmatch(r"nfs\.fin-02\.(?:datacrunch\.io|verda\.com):/[A-Za-z0-9/_-]+", row["source"]),
            "Source is not the expected provider-managed FIN-02 NFS service")
    return row


def cache_mount(cache, owner, device, *, readonly):
    row = mount_record(cache)
    require(row["mountpoint"] == str(cache) and row["filesystem_type"] == "ext4",
            "Cache must be the root of its own ext4 mount")
    require(("ro" if readonly else "rw") in row["options"], "Cache mount has the wrong access mode")
    if readonly:
        require({"noload", "norecovery"} & set(row["super_options"]),
                "Read-only cache mount must disable ext4 journal replay")
    device = Path(device)
    info = device.stat()
    require(stat.S_ISBLK(info.st_mode), "Expected provider attachment is not a block device")
    uuid_device = Path("/dev/disk/by-uuid") / owner["filesystem_uuid"]
    require(uuid_device.stat().st_rdev == info.st_rdev, "Cache filesystem UUID identifies another device")
    major_minor = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
    require(row["major_minor"] == major_minor and cache.stat().st_dev == info.st_rdev,
            "Cache mount is not the expected provider-attested device")
    fd = os.open(device, os.O_RDONLY | os.O_CLOEXEC)
    try:
        size = struct.unpack("Q", fcntl.ioctl(fd, 0x80081272, struct.pack("Q", 0)))[0]
    finally:
        os.close(fd)
    require(size == SIZE_BYTES, "Cache device is not the approved 1300 GiB volume")
    return dict(**row, filesystem_uuid=owner["filesystem_uuid"], volume_id=owner["volume_id"],
                size_bytes=size, device=str(device), provider_identity_source="bootstrap-attested attachment")


def generation(source):
    raw_manifest = read_bytes(source / "manifest.json", 1024**2)
    require(json.loads(raw_manifest) == databases.MANIFEST, "Published database manifest changed")
    receipts = {name: hashlib.sha256(read_bytes(source / name, 8*1024**2)).hexdigest()
                for name in GENERATION_FILES}
    return dict(source_manifest_sha256=databases.MANIFEST_SHA256,
                source_receipt_sha256=digest(receipts), receipts=receipts,
                manifest_file_sha256=hashlib.sha256(raw_manifest).hexdigest())


def ready_source(value):
    return dict(manifest_sha256=value["source_manifest_sha256"],
                receipt_sha256=value["source_receipt_sha256"],
                receipts=value["receipts"], manifest_file_sha256=value["manifest_file_sha256"])


def inspect(source):
    """Inventory only the published complete snapshot, never download/build trees."""
    source = real_root(source)
    before = generation(source)
    databases.validate(source)
    selected = {"manifest.json", *GENERATION_FILES}
    expected = {}
    for component in databases.COMPONENTS:
        receipt = load(source / ".components" / (component + ".json"), 8*1024**2)
        internal = source / component / ".component.json"
        if internal.exists() or internal.is_symlink():
            require(load(internal, 8*1024**2) == receipt, "Internal published component receipt differs")
            selected.add(component + "/.component.json")
        if component == "mmcif":
            selected.add("mmcif/mmcif-content.jsonl.gz")
            expected["mmcif/mmcif-content.jsonl.gz"] = {
                "sha256": receipt["files"]["content_manifest_sha256"]}
            for folder in ("divided", "obsolete"):
                for path in (source / component / folder).glob("*/*.cif.gz"):
                    selected.add(str(path.relative_to(source)))
                    require(len(selected) <= MAX_FILES, "Published snapshot exceeds file bound")
        else:
            for name, info in receipt["files"].items():
                safe_path(name)
                path = component + "/" + name
                selected.add(path)
                expected[path] = info
    require(len(selected) <= MAX_FILES, "Published snapshot exceeds file bound")
    directories = {"."}
    entries = []
    for name in sorted(selected):
        safe_path(name)
        path = source / name
        info = path.lstat()
        require(info.st_dev == source.stat().st_dev, "Published source crosses onto another filesystem")
        require(stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode), "Unsupported published entry")
        require(not stat.S_IMODE(info.st_mode) & 0o7000, "Special file permissions are not database data")
        parents = PurePosixPath(name).parents
        for parent in parents:
            directories.add(str(parent))
            require((source / str(parent)).resolve() == source / str(parent), "Symlinked source directory")
        kind = "symlink" if stat.S_ISLNK(info.st_mode) else "file"
        entry = dict(path=name, kind=kind, source_metadata=portable_metadata(path, kind))
        if kind == "symlink":
            target = os.readlink(path)
            require(target and not PurePosixPath(target).is_absolute(), "Published aliases must be relative")
            resolved = path.resolve(strict=True)
            component = PurePosixPath(name).parts[0]
            require(resolved.is_relative_to(source / component)
                    and str(resolved.relative_to(source)) in selected and resolved.is_file(),
                    "Published alias escapes its component or selected files")
            entry["target"] = target
        else:
            entry["expected"] = expected.get(name, {})
            require(info.st_size >= 0, "Invalid source file size")
        entries.append(entry)
    for name in sorted(directories):
        path = source / name
        require(stat.S_ISDIR(path.lstat().st_mode), "Source directory is not real")
        entries.append(dict(path=name, kind="directory", source_metadata=portable_metadata(path, "directory")))
    require(len(directories) <= 10000, "Published snapshot exceeds directory bound")
    require(generation(source) == before, "Source generation changed during inventory")
    return dict(schema=1, kind=KIND, source=before, entries=sorted(entries, key=lambda row: row["path"]),
        payload_bytes=sum(row["source_metadata"]["size"] for row in entries if row["kind"] == "file"))


def summary(plan):
    return dict(schema=1, kind=KIND, **plan["source"], payload_bytes=plan["payload_bytes"],
                files=sum(row["kind"] == "file" for row in plan["entries"]),
                symlinks=sum(row["kind"] == "symlink" for row in plan["entries"]), plan_sha256=digest(plan))


class Progress:
    def __init__(self, total, callback=None):
        self.total, self.callback = total, callback
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.read = self.verified = self.reused = self.written = self.files = 0
        self.last = 0.0

    def add(self, *, read=0, verified=0, reused=0, written=0, files=0, force=False):
        with self.lock:
            self.read += read
            self.verified += verified
            self.reused += reused
            self.written += written
            self.files += files
            elapsed = max(0.001, time.monotonic() - self.started)
            worked = self.read + self.verified
            remaining = max(0, 2*self.total - worked - 2*self.reused)
            rate = worked / elapsed
            eta = [remaining/(rate*1.5), remaining/(rate*0.5)] if rate > 0 and elapsed >= 1 else None
            event = dict(schema=1, stage="copy-and-readback", total_bytes=self.total,
                source_bytes_hashed=self.read, destination_bytes_readback=self.verified,
                completed_reused_bytes=self.reused, destination_bytes_written=self.written,
                completed_files=self.files, work_total_bytes=2*self.total,
                work_completed_bytes=min(2*self.total, worked+2*self.reused),
                bytes_per_second=rate, elapsed_seconds=elapsed, eta_seconds=eta)
            if self.callback and (force or elapsed-self.last >= 1):
                self.callback(event)
                self.last = elapsed
            return event


def edge_fd(handle, size):
    first = os.pread(handle.fileno(), min(EDGE, size), 0)
    last = os.pread(handle.fileno(), min(EDGE, size), max(0, size-EDGE))
    return hashlib.sha256(first + last).hexdigest()


def hash_handle(handle, progress=None):
    result = hashlib.sha256()
    handle.seek(0)
    while data := handle.read(CHUNK):
        if progress:
            require(not progress.cancelled.is_set(), "Readback cancelled after another file failed")
        result.update(data)
        if progress:
            progress.add(verified=len(data))
    return result.hexdigest()


def preserve(path, info, *, link=False):
    current = path.lstat()
    if (current.st_uid, current.st_gid) != (info["uid"], info["gid"]):
        os.chown(path, info["uid"], info["gid"], follow_symlinks=False)
    if not link:
        os.chmod(path, info["mode"], follow_symlinks=False)
    os.utime(path, ns=(info["mtime_ns"], info["mtime_ns"]), follow_symlinks=False)


def tree_paths(root):
    paths = {"."}
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            paths.add(str((Path(parent) / name).relative_to(root)))
            require(len(paths) <= MAX_FILES + 10000, "Cache tree exceeds bound")
    return paths


def check_source(source, entry):
    path = source / entry["path"]
    require(portable_metadata(path, entry["kind"]) == entry["source_metadata"],
            "Published source changed: " + entry["path"])
    if entry["kind"] == "symlink":
        require(os.readlink(path) == entry["target"], "Published source alias changed")


def check_file(path, item, *, full=False, device=None):
    regular(path)
    require(metadata(path) == item["metadata"], "Cached file metadata changed: " + str(path))
    if device is not None:
        require(path.stat().st_dev == device, "Cached file is on another mounted filesystem")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
        require(edge_fd(handle, item["metadata"]["size"]) == item["edge_sha256"], "Cached file edges changed")
        if full:
            require(hash_handle(handle) == item["sha256"], "Cached content SHA changed")
    require(metadata(path) == item["metadata"], "Cached file changed during verification")


def completed_path(state, name):
    key = hashlib.sha256(name.encode()).hexdigest()
    directory = state / "completed" / key[:2]
    owned_directory(directory, state.stat().st_dev)
    return directory / (key + ".json")


def read_completed(path, entry, plan_sha):
    value = load(path, 16384)
    checksum = value.pop("receipt_sha256", None)
    require(checksum == digest(value) and value.get("plan_sha256") == plan_sha
            and value.get("source") == entry and value.get("full_readback") is True,
            "Completed file receipt changed")
    item = value["content"]
    require(item.get("path") == entry["path"] and sha(item.get("sha256")) and sha(item.get("edge_sha256")),
            "Invalid completed file content receipt")
    return item


def copy_file(source, target_root, state, entry, plan_sha, progress):
    check_source(source, entry)
    target = target_root / entry["path"]
    done = completed_path(state, entry["path"])
    if done.exists() or done.is_symlink():
        item = read_completed(done, entry, plan_sha)
        check_file(target, item, device=target_root.stat().st_dev)
        progress.add(reused=entry["source_metadata"]["size"], files=1)
        return item
    key = hashlib.sha256(entry["path"].encode()).hexdigest()
    partial = state / "partial" / (key + ".part")
    marker = state / "partial" / (key + ".json")
    claim = dict(schema=1, plan_sha256=plan_sha, source=entry)
    if marker.exists() or marker.is_symlink():
        require(load(marker, 16384) == claim, "Partial file belongs to a different source")
    else:
        require(not partial.exists() and not partial.is_symlink()
                and not target.exists() and not target.is_symlink(), "Unowned cache file or partial exists")
        atomic_json(marker, claim)
    # A crash after rename but before checkpoint can reuse the final file too.
    candidate = target if target.exists() or target.is_symlink() else partial
    if candidate.exists() or candidate.is_symlink():
        regular(candidate)
        require(candidate.stat().st_dev == target_root.stat().st_dev, "Partial crosses cache filesystem")
    source_path = source / entry["path"]
    original = metadata(source_path)
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    with os.fdopen(os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as src, \
            os.fdopen(os.open(candidate, flags, 0o600), "r+b") as dst:
        source_size = entry["source_metadata"]["size"]
        prefix_size = os.fstat(dst.fileno()).st_size
        require(prefix_size <= source_size, "Partial file is larger than its source")
        result = hashlib.sha256()
        position = 0
        while data := src.read(CHUNK):
            require(not progress.cancelled.is_set(), "Copy cancelled after another file failed")
            require(position+len(data) <= source_size, "Source grew during copy")
            result.update(data)
            overlap = min(len(data), max(0, prefix_size-position))
            if overlap:
                require(os.pread(dst.fileno(), overlap, position) == data[:overlap], "Partial prefix differs from source")
            if overlap < len(data):
                dst.seek(position+overlap)
                dst.write(data[overlap:])
                progress.add(written=len(data)-overlap)
            position += len(data)
            progress.add(read=len(data))
        require(position == source_size and metadata(source_path) == original, "Source changed during copy")
        check_source(source, entry)
        content_sha = result.hexdigest()
        source_edge = edge_fd(src, source_size)
        expected = entry["expected"]
        require(expected.get("bytes", source_size) == source_size
                and expected.get("edge_sha256", source_edge) == source_edge
                and expected.get("sha256", content_sha) == content_sha, "Copied source differs from published receipt")
        dst.flush()
        os.fsync(dst.fileno())
        require(hash_handle(dst, progress) == content_sha, "Destination readback differs from source")
        require(edge_fd(dst, source_size) == source_edge, "Destination readback edges differ")
    check_source(source, entry)
    preserve(candidate, entry["source_metadata"])
    fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)  # Persist the preserved inode metadata before its checkpoint.
    finally:
        os.close(fd)
    if candidate != target:
        os.replace(candidate, target)
        sync_directory(target.parent)
    item = dict(path=entry["path"], kind="file", sha256=content_sha,
                edge_sha256=source_edge, metadata=metadata(target))
    receipt = dict(schema=1, plan_sha256=plan_sha, source=entry, content=item, full_readback=True)
    atomic_json(done, dict(receipt, receipt_sha256=digest(receipt)))
    progress.add(files=1)
    return item


@contextlib.contextmanager
def cache_lock(state, *, write):
    path = state / "lock"
    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_CREAT if write else 0)
    fd = os.open(path, flags, 0o600)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), "Cache lock is not a regular file")
        fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _initialize(cache, owner, plan):
    state = cache / STATE
    if not state.exists():
        require(set(p.name for p in cache.iterdir()) <= {"lost+found"}, "Cache filesystem is not new or owned")
    owned_directory(state, cache.stat().st_dev)
    require(state.stat().st_uid == os.geteuid() and not state.stat().st_mode & 0o022,
            "Cache state is not privately owned by this service")
    intent = dict(schema=1, identity=identity(owner), plan_sha256=digest(plan))
    path = state / "intent.json"
    if path.exists() or path.is_symlink():
        require(load(path, 16384) == intent, "Cache copy ownership or source plan changed")
    else:
        require(set(p.name for p in state.iterdir()) <= {"lock"}, "Unowned cache state exists")
        atomic_json(path, intent)
    if not (state / "plan.json").exists():
        atomic_json(state / "plan.json", plan)
    require(digest(load(state / "plan.json")) == intent["plan_sha256"], "Stored copy plan changed")
    for folder in ("completed", "partial"):
        owned_directory(state / folder, cache.stat().st_dev)
    return state, intent


def copy(source, cache, owner, device, *, workers=4, callback=None):
    require(type(workers) is int and 1 <= workers <= 8, "Copy workers must be between one and eight")
    owner = binding(owner)
    require(owner["ready_receipt_sha256"] is None, "Population requires an unready cache binding")
    source, cache = real_root(source), real_root(cache)
    require(not source.is_relative_to(cache) and not cache.is_relative_to(source), "Cache overlaps source")
    source_evidence = source_mount(source)
    mounted = cache_mount(cache, owner, device, readonly=False)
    plan = inspect(source)
    require(all(plan["source"][key] == owner[key] for key in
                ("source_manifest_sha256", "source_receipt_sha256")), "Source generation differs from cache allocation")
    state = cache / STATE
    if not state.exists():
        require(set(p.name for p in cache.iterdir()) <= {"lost+found"}, "Cache filesystem is not new or owned")
    owned_directory(state, cache.stat().st_dev)
    require(state.stat().st_uid == os.geteuid() and not state.stat().st_mode & 0o022,
            "Cache state is not privately owned by this service")
    with cache_lock(state, write=True):
        state, intent = _initialize(cache, owner, plan)
        ready_path = cache / "ready.json"
        if ready_path.exists() or ready_path.is_symlink():
            ready = load(ready_path, 65536)
            require(ready.get("plan_sha256") == intent["plan_sha256"]
                    and all(ready.get(k) == v for k, v in identity(owner).items()), "Ready cache belongs to another copy")
            ready_sha = hashlib.sha256(read_bytes(ready_path, 65536)).hexdigest()
            _verify_contents(cache, dict(owner, ready_receipt_sha256=ready_sha), full=False)
            return dict(ready=ready, ready_receipt_sha256=ready_sha,
                        cache_root=str(cache / "colabfold"), already_ready=True)
        final = cache / "colabfold"
        tree = final if final.exists() or final.is_symlink() else cache / PENDING
        owned_directory(tree, cache.stat().st_dev)
        dirs = [row for row in plan["entries"] if row["kind"] == "directory"]
        for entry in sorted(dirs, key=lambda row: len(PurePosixPath(row["path"]).parts)):
            owned_directory(tree / entry["path"], cache.stat().st_dev)
        files = sorted((row for row in plan["entries"] if row["kind"] == "file"),
                       key=lambda row: (-row["source_metadata"]["size"], row["path"]))
        available = os.statvfs(cache)
        # Partial/completed bytes already occupy the filesystem; check incremental
        # space per write through the kernel rather than demand a second full copy.
        require(available.f_bavail * available.f_frsize > min(plan["payload_bytes"], 16*1024**2),
                "Cache filesystem has no copy headroom")
        progress = Progress(plan["payload_bytes"], callback)
        content = []
        iterator = iter(files)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            pending = set()
            for _ in range(workers):
                if (entry := next(iterator, None)) is not None:
                    pending.add(pool.submit(copy_file, source, tree, state, entry, intent["plan_sha256"], progress))
            while pending:
                done, pending = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    try:
                        content.append(future.result())
                    except BaseException:
                        progress.cancelled.set()
                        for other in pending:
                            other.cancel()
                        raise
                    if (entry := next(iterator, None)) is not None:
                        pending.add(pool.submit(copy_file, source, tree, state, entry, intent["plan_sha256"], progress))
        for entry in plan["entries"]:
            check_source(source, entry)
            if entry["kind"] == "symlink":
                target = tree / entry["path"]
                if target.exists() or target.is_symlink():
                    require(target.is_symlink() and os.readlink(target) == entry["target"], "Cached alias was changed")
                else:
                    target.symlink_to(entry["target"])
                preserve(target, entry["source_metadata"], link=True)
                content.append(dict(path=entry["path"], kind="symlink", target=entry["target"], metadata=metadata(target)))
        require(generation(source) == plan["source"], "Published source generation changed during copy")
        databases.validate(tree)
        require(tree_paths(tree) == {entry["path"] for entry in plan["entries"]},
                "Cache staging contains missing or unpublished entries")
        require(generation(source) == plan["source"], "Source receipts changed before cache publication")
        for entry in sorted(dirs, key=lambda row: -len(PurePosixPath(row["path"]).parts)):
            preserve(tree / entry["path"], entry["source_metadata"])
            sync_directory(tree / entry["path"])
        if tree != final:
            tree.rename(final)
            sync_directory(cache)
        for entry in dirs:
            content.append(dict(path=entry["path"], kind="directory",
                                metadata=portable_metadata(final / entry["path"], "directory")))
        manifest = state / "content.jsonl"
        fd, temporary = tempfile.mkstemp(prefix=".content-", dir=state)
        content_hash = hashlib.sha256()
        content_size = 0
        try:
            with os.fdopen(fd, "wb") as output:
                for row in sorted(content, key=lambda item: item["path"]):
                    data = canonical(row) + b"\n"
                    output.write(data)
                    content_hash.update(data)
                    content_size += len(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, manifest)
            sync_directory(state)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        result = dict(**identity(owner), kind=KIND, status="ready", rootrel="colabfold",
            filesystem_type="ext4", size_bytes=SIZE_BYTES, plan_sha256=intent["plan_sha256"],
            source=ready_source(plan["source"]), content_manifest_sha256=content_hash.hexdigest(),
            content_manifest_bytes=content_size, created_epoch=time.time(),
            completion=dict(files=len(files), symlinks=sum(row["kind"] == "symlink" for row in content),
                directories=len(dirs), payload_bytes=plan["payload_bytes"],
                source_bytes_hashed=plan["payload_bytes"], destination_bytes_readback=plan["payload_bytes"],
                full_readback=True), population_mount=mounted, source_mount=source_evidence)
        atomic_json(ready_path, result)
        event = progress.add(force=True)
        return dict(ready=result, ready_receipt_sha256=hashlib.sha256(read_bytes(ready_path, 65536)).hexdigest(),
                    cache_root=str(final), progress=event, already_ready=False)


def verify(cache, owner, device, *, full=False):
    cache, owner = real_root(cache), binding(owner)
    require(sha(owner["ready_receipt_sha256"]), "Serve requires an externally pinned ready receipt SHA")
    mounted = cache_mount(cache, owner, device, readonly=True)
    state = cache / STATE
    require(state.is_dir() and not state.is_symlink(), "Cache state is missing or symlinked")
    require(state.stat().st_uid == os.geteuid() and not state.stat().st_mode & 0o022,
            "Cache state is not privately owned by this service")
    with cache_lock(state, write=False):
        result = _verify_contents(cache, owner, full=full)
        return dict(result, mount=mounted)


def _verify_contents(cache, owner, *, full):
    state = cache / STATE
    raw = read_bytes(cache / "ready.json", 65536)
    require(hashlib.sha256(raw).hexdigest() == owner["ready_receipt_sha256"], "Ready receipt SHA changed")
    ready = json.loads(raw)
    require(all(ready.get(key) == value for key, value in identity(owner).items())
            and ready.get("status") == "ready" and ready.get("kind") == KIND
            and ready.get("rootrel") == "colabfold" and ready.get("filesystem_type") == "ext4"
            and ready.get("size_bytes") == SIZE_BYTES and ready.get("completion", {}).get("full_readback") is True,
            "Cache completion or identity is invalid")
    root = real_root(cache / "colabfold")
    require(ready_source(generation(root)) == ready["source"], "Cached generation receipts changed")
    manifest = state / "content.jsonl"
    regular(manifest)
    require(manifest.stat().st_size == ready["content_manifest_bytes"] <= MAX_JSON,
            "Cached content manifest size changed")
    with manifest.open("rb") as handle:
        require(hash_handle(handle) == ready["content_manifest_sha256"], "Cached content manifest SHA changed")
        handle.seek(0)
        names = set()
        counted = dict(files=0, symlinks=0, directories=0, payload_bytes=0)
        for line in handle:
            require(len(line) <= 16384 and len(names) <= MAX_FILES + 10000, "Oversized cache content inventory")
            item = json.loads(line)
            name = safe_path(item["path"], root=True)
            require(name not in names, "Duplicate content manifest path")
            names.add(name)
            path = root / name
            require(path.parent.resolve() == path.parent and path.lstat().st_dev == root.stat().st_dev,
                    "Cache entry escaped its filesystem")
            if item["kind"] == "file":
                require(sha(item.get("sha256")) and sha(item.get("edge_sha256")), "Invalid cached content SHA")
                check_file(path, item, full=full, device=root.stat().st_dev)
                counted["files"] += 1
                counted["payload_bytes"] += item["metadata"]["size"]
            elif item["kind"] == "directory":
                require(stat.S_ISDIR(path.lstat().st_mode)
                        and portable_metadata(path, "directory") == item["metadata"], "Cached directory metadata changed")
                counted["directories"] += 1
            else:
                require(item["kind"] == "symlink" and path.is_symlink()
                        and os.readlink(path) == item["target"] and metadata(path) == item["metadata"]
                        and path.resolve(strict=True).is_relative_to(root / PurePosixPath(name).parts[0]),
                        "Cached alias changed or escaped")
                counted["symlinks"] += 1
    actual = tree_paths(root)
    require(actual == names and all(ready["completion"].get(k) == v for k, v in counted.items()),
            "Cache contains missing, extra or uncompleted entries")
    databases.validate(root)
    require(read_bytes(cache / "ready.json", 65536) == raw, "Ready receipt changed during verification")
    return dict(schema=1, status="verified", kind=KIND, cache_root=str(root),
        ready_receipt_sha256=owner["ready_receipt_sha256"], **{k: v for k, v in identity(owner).items() if k != "schema"},
        verification="full-sha256" if full else "pinned-receipt-metadata-edges",
        completion=counted, checked_epoch=time.time())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspection = sub.add_parser("inspect")
    inspection.add_argument("--source", type=Path, required=True)
    for command in ("copy", "verify"):
        p = sub.add_parser(command)
        p.add_argument("--cache", type=Path, required=True)
        p.add_argument("--binding", type=Path, required=True)
        p.add_argument("--device", required=True)
        if command == "copy":
            p.add_argument("--source", type=Path, required=True)
            p.add_argument("--workers", type=int, default=4)
        else:
            p.add_argument("--full", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "inspect":
        result = summary(inspect(args.source))
    elif args.command == "copy":
        result = copy(args.source, args.cache, load(args.binding, 65536), args.device,
                      workers=args.workers, callback=lambda row: print(json.dumps(row), file=sys.stderr, flush=True))
    else:
        result = verify(args.cache, load(args.binding, 65536), args.device, full=args.full)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print("msa-block-cache: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
