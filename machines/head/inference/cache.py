"""Verified immutable search/preparation artifacts, never stochastic features.

An identity names the exact chemistry, parser, search backend/database/settings,
and template/pairing modes. Its canonical JSON is the lookup key. Identical byte
trees may share an object, but different identities retain separate receipts.
Request/attempt IDs, timings, and result provenance belong in the caller's job
envelope, not in this cache. A hit is reuse of evidence, not another experiment.

Only regular files are accepted. Files are copied (never linked) into a fresh
job-writable tree. All reads recheck content; a damaged entry fails closed.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile


class CacheError(RuntimeError):
    pass


MAX_FILES = 100_000
MAX_BYTES = 512 * 1024**3
ARTIFACT_SCHEMA = 2
HEX = re.compile(r"[0-9a-f]{64}")


def canonical(value):
    def check(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise CacheError("Cache JSON keys must be strings")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise CacheError("Cache identities and receipts must be JSON values")
    check(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CacheError("Invalid cache JSON: " + str(exc)) from exc


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _sha(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def identity_key(identity):
    """Validate mandatory semantics, then hash all fields (including extensions)."""
    if not isinstance(identity, dict) or type(identity.get("schema")) is not int or identity["schema"] != 1:
        raise CacheError("Cache identity requires schema=1")
    if identity.get("artifact_kind") not in {"search", "prepared"}:
        raise CacheError("Only raw search and deterministic prepared artifacts may be cached")
    molecular, model, search = (identity.get(key) for key in ("input", "model", "search"))
    if not isinstance(molecular, dict) or not all(_sha(molecular.get(k)) for k in ("sha256", "chemistry_sha256")):
        raise CacheError("Cache identity must bind exact input and chemistry SHA256")
    if (not isinstance(model, dict) or not _text(model.get("name"))
            or not all(_sha(model.get(k)) for k in ("adapter_sha256", "parser_sha256"))):
        raise CacheError("Cache identity must bind the model adapter and parser")
    if (not isinstance(search, dict) or not all(_text(search.get(k)) for k in ("backend", "template_mode", "pairing_mode"))
            or not all(_sha(search.get(k)) for k in ("provenance_sha256", "settings_sha256"))):
        raise CacheError("Cache identity must bind search provenance, settings, pairing and templates")
    database = search.get("database")
    if not isinstance(database, dict) or not (
        database.get("status") == "verified" and _sha(database.get("sha256"))
        or database.get("status") == "provider-unreported" and _text(database.get("provider"))
    ):
        raise CacheError("Cache database identity must be verified or explicitly provider-unreported")
    # v1 cached file inventories but omitted empty directories. A separate key
    # namespace keeps those historical receipts immutable without treating them
    # as complete native input trees.
    return digest({"artifact_schema": ARTIFACT_SCHEMA, "identity": identity})


def _safe_relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or value != str(PurePosixPath(value)) or PurePosixPath(value).is_absolute()
            or any(part in {".", ".."} for part in PurePosixPath(value).parts)):
        raise CacheError("Unsafe cache relative path")
    return value


def _no_symlinks(path):
    path = Path(path).absolute()
    for item in (*reversed(path.parents), path):
        if item.is_symlink():
            raise CacheError("Cache paths must not contain symlinks: " + str(item))
    return path


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_directory_exclusive(source, destination):
    """NFS fallback: reserve the name without replacing any existing entry.

    NFS does not implement RENAME_NOREPLACE. The destination can be visible
    during this transfer, so callers must publish their completion receipt only
    after return. Cache lookup already requires that receipt. A failed transfer
    leaves its incomplete destination for diagnosis and cannot be read as a hit
    or overwritten by another attempt.

    Hardlinks here transfer our private staging files, never the cache's source
    or a previous job. The staging tree is removed only after all transfers and
    directory fsyncs succeed. mkdir/link both refuse racing existing names.
    """
    source, destination = _no_symlinks(source), _no_symlinks(destination)
    if not stat.S_ISDIR(source.lstat().st_mode):
        raise CacheError("Directory publication requires a regular directory")
    marker_name = ".publication-incomplete"
    if (source / marker_name).exists():
        raise CacheError("Directory publication source contains a reserved marker")
    destination.mkdir(mode=0o700, exist_ok=False)
    marker = destination / marker_name
    _write_json(marker, {"kind": "incomplete-directory-publication", "source": str(source)})
    _sync_dir(destination)
    _sync_dir(destination.parent)
    def transfer(old, new, top=False):
        for item in sorted(old.iterdir()):
            target = new / item.name
            mode = item.lstat().st_mode
            if stat.S_ISDIR(mode):
                target.mkdir(mode=0o700, exist_ok=False)
                transfer(item, target)
            elif stat.S_ISREG(mode):
                os.link(item, target, follow_symlinks=False)
            else:
                raise CacheError("Directory publication refuses symlinks and special files")
        if not top:
            new.chmod(stat.S_IMODE(old.lstat().st_mode))
        _sync_dir(new)
    final_mode = stat.S_IMODE(source.lstat().st_mode)
    try:
        transfer(source, destination, top=True)
        _sync_dir(destination.parent)
        # Files now have another name in destination. Do not chmod their shared
        # inode while deleting the private staging names. Keep the failure
        # marker until that cleanup also succeeds.
        for directory, _, _ in os.walk(source):
            Path(directory).chmod(0o700)
        shutil.rmtree(source)
        marker.unlink()
        destination.chmod(final_mode)
        _sync_dir(destination)
        _sync_dir(destination.parent)
    except BaseException:
        if not marker.exists():
            try:
                destination.chmod(0o700)
                _write_json(marker, {"kind": "incomplete-directory-publication", "source": str(source)})
                _sync_dir(destination)
            except OSError:
                pass  # The caller still must not publish a completion receipt.
        raise


def _rename_new(source, destination):
    """Publish without replacement; receipt-gated NFS fallback when needed."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        if error in (errno.EINVAL, errno.EOPNOTSUPP, errno.ENOSYS):
            return _publish_directory_exclusive(source, destination)
        raise OSError(error, os.strerror(error), str(destination))


def _regular_copy(source, target=None):
    """Read a stable regular inode without following links, optionally copying."""
    _no_symlinks(source)
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    output = None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise CacheError("Cache input is not a regular file: " + str(source))
        if target is not None:
            output = open(target, "xb")
        value = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as handle:
            for block in iter(lambda: handle.read(8 << 20), b""):
                value.update(block)
                if output is not None:
                    output.write(block)
        after = os.fstat(fd)
        current = os.stat(source, follow_symlinks=False)
        signature = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if signature(before) != signature(after) or signature(after) != signature(current):
            raise CacheError("Cache source changed while reading: " + str(source))
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
        return {"bytes": before.st_size, "sha256": value.hexdigest()}
    finally:
        if output is not None:
            output.close()
        os.close(fd)


def inventory(root, destination=None):
    root = _no_symlinks(root)
    if not root.is_dir():
        raise CacheError("Artifact source must be a directory")
    files, total = {}, 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs.sort()
        names.sort()
        for name in dirs:
            item = Path(directory) / name
            if item.is_symlink() or not item.is_dir():
                raise CacheError("Artifact directory contains a symlink or special entry")
        for name in names:
            item = Path(directory) / name
            relative = _safe_relative(item.relative_to(root).as_posix())
            if not stat.S_ISREG(item.lstat().st_mode):
                raise CacheError("Artifact contains a symlink or nonregular file: " + relative)
            size = item.stat().st_size
            total += size
            if len(files) >= MAX_FILES or total > MAX_BYTES:
                raise CacheError("Artifact exceeds cache file/byte limit")
            target = None
            if destination is not None:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
            files[relative] = _regular_copy(item, target)
    if not files:
        raise CacheError("Artifact tree must contain at least one regular file")
    return files


def directories(root):
    """Bind empty directories too: native MSA parsers can require their paths."""
    root = _no_symlinks(root)
    if not root.is_dir():
        raise CacheError("Artifact source must be a directory")
    result = []
    for directory, dirs, _ in os.walk(root, followlinks=False):
        for name in sorted(dirs):
            item = Path(directory) / name
            if not stat.S_ISDIR(item.lstat().st_mode):
                raise CacheError("Artifact directory contains a symlink or special entry")
            result.append(_safe_relative(item.relative_to(root).as_posix()))
            if len(result) > MAX_FILES:
                raise CacheError("Artifact exceeds cache directory limit")
    return sorted(result)


def _read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CacheError("Duplicate cache JSON key")
            result[key] = value
        return result
    _no_symlinks(path)
    if not path.is_file() or path.stat().st_size > 64 * 1024**2:
        raise CacheError("Invalid cache manifest")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def _write_json(path, value):
    with path.open("xb") as handle:
        handle.write(canonical(value) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _make_readonly(root):
    for directory, dirs, names in os.walk(root, topdown=False):
        for name in names:
            (Path(directory) / name).chmod(0o444)
        Path(directory).chmod(0o555)
        _sync_dir(directory)


def _remove_stage(path):
    if path.exists():
        for directory, dirs, names in os.walk(path):
            Path(directory).chmod(0o700)
            for name in names:
                (Path(directory) / name).chmod(0o600)
        shutil.rmtree(path)


class ArtifactCache:
    def __init__(self, root):
        self.root = _no_symlinks(root)
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("objects", "entries", "staging"):
            path = _no_symlinks(self.root / name)
            path.mkdir(mode=0o700, exist_ok=True)

    @contextmanager
    def _lock(self, exclusive=False):
        _no_symlinks(self.root)
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise CacheError("Cache lock is not a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)

    def _verify(self, receipt):
        if (not isinstance(receipt, dict) or type(receipt.get("schema")) is not int or receipt["schema"] != ARTIFACT_SCHEMA
                or receipt.get("kind") != "artifact-cache-receipt"
                or receipt.get("key") != identity_key(receipt.get("identity"))
                or not _sha(receipt.get("content_sha256")) or not _sha(receipt.get("manifest_sha256"))):
            raise CacheError("Malformed cache receipt")
        obj = _no_symlinks(self.root / "objects" / receipt["content_sha256"])
        manifest_path = obj / "manifest.json"
        manifest = _read_json(manifest_path)
        if _regular_copy(manifest_path)["sha256"] != receipt["manifest_sha256"]:
            raise CacheError("Cache manifest checksum mismatch")
        if (manifest != {"schema": ARTIFACT_SCHEMA, "kind": "artifact-payload", "files": receipt.get("files"),
                         "directories": receipt.get("directories")}
                or digest(manifest) != receipt["content_sha256"]):
            raise CacheError("Cache manifest/receipt content binding mismatch")
        if inventory(obj / "data") != manifest["files"] or directories(obj / "data") != manifest["directories"]:
            raise CacheError("Cache payload checksum/inventory mismatch")
        if {p.name for p in obj.iterdir()} != {"data", "manifest.json"}:
            raise CacheError("Unexpected cache object files")
        return obj / "data"

    def _lookup(self, identity):
        key = identity_key(identity)
        path = _no_symlinks(self.root / "entries" / (key + ".json"))
        if not path.exists():
            return None
        receipt = _read_json(path)
        if receipt.get("key") != key or canonical(receipt.get("identity")) != canonical(identity):
            raise CacheError("Cache lookup identity mismatch")
        self._verify(receipt)
        return receipt

    def lookup(self, identity):
        with self._lock():
            result = self._lookup(identity)
            return deepcopy(result)

    def publish(self, source, identity):
        identity = deepcopy(identity)
        key = identity_key(identity)
        source = _no_symlinks(source)
        if source == self.root or self.root.is_relative_to(source) or source.is_relative_to(self.root):
            raise CacheError("Artifact source and cache must be separate trees")
        stage = Path(tempfile.mkdtemp(prefix="publish-", dir=_no_symlinks(self.root / "staging")))
        try:
            data = stage / "data"
            data.mkdir()
            directory_paths = directories(source)
            for relative in directory_paths:
                (data / relative).mkdir(parents=True, exist_ok=True)
            files = inventory(source, data)
            if (inventory(source) != files or inventory(data) != files
                    or directories(source) != directory_paths or directories(data) != directory_paths):
                raise CacheError("Artifact source changed during publication")
            manifest = {"schema": ARTIFACT_SCHEMA, "kind": "artifact-payload", "files": files,
                        "directories": directory_paths}
            _write_json(stage / "manifest.json", manifest)
            receipt = {"schema": ARTIFACT_SCHEMA, "kind": "artifact-cache-receipt", "key": key, "identity": identity,
                       "content_sha256": digest(manifest), "manifest_sha256": _regular_copy(stage / "manifest.json")["sha256"],
                       "files": files, "directories": directory_paths}
            _make_readonly(stage)
            # Moving a directory across parents updates its '..' entry; Linux
            # requires owner write permission until the move has completed.
            stage.chmod(0o700)
            with self._lock(exclusive=True):
                existing = self._lookup(identity)
                if existing is not None:
                    if canonical(existing) != canonical(receipt):
                        raise CacheError("Same cache identity produced different content; retain separate search provenance")
                    return existing
                target = _no_symlinks(self.root / "objects" / receipt["content_sha256"])
                if target.exists():
                    self._verify(receipt)
                else:
                    _rename_new(stage, target)
                    target.chmod(0o555)
                    _sync_dir(target)
                    _sync_dir(target.parent)
                # Publish the identity only after the complete object is durable.
                entry = self.root / "entries" / (key + ".json")
                fd, temporary = tempfile.mkstemp(prefix="entry-", dir=self.root / "staging")
                os.close(fd)
                temporary = Path(temporary)
                try:
                    temporary.unlink()
                    _write_json(temporary, receipt)
                    temporary.chmod(0o444)
                    os.link(temporary, entry)  # Atomic no-replace publication, not an artifact hardlink.
                    _sync_dir(entry.parent)
                finally:
                    temporary.unlink(missing_ok=True)
            return deepcopy(receipt)
        finally:
            _remove_stage(stage)

    def materialize(self, receipt, destination):
        destination = _no_symlinks(destination)
        if destination.exists() or destination.is_symlink():
            raise CacheError("Materialization destination already exists")
        if destination == self.root or destination.is_relative_to(self.root) or self.root.is_relative_to(destination):
            raise CacheError("Job output and cache must be separate trees")
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".artifact-copy-", dir=destination.parent))
        try:
            with self._lock():
                expected = self._lookup(receipt.get("identity")) if isinstance(receipt, dict) else None
                if expected is None or canonical(receipt) != canonical(expected):
                    raise CacheError("Materialization requires the exact published cache receipt")
                source = self.root / "objects" / receipt["content_sha256"] / "data"
                for relative in receipt["directories"]:
                    (stage / relative).mkdir(parents=True, exist_ok=True)
                if (inventory(source, stage) != receipt["files"] or inventory(stage) != receipt["files"]
                        or directories(source) != receipt["directories"] or directories(stage) != receipt["directories"]):
                    raise CacheError("Cache changed during materialization")
                _rename_new(stage, destination)
                _sync_dir(destination.parent)
            return destination
        finally:
            _remove_stage(stage)
