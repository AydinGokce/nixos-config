"""Small, dependency-free primitives shared by head and GPU workers."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def configuration_id(config):
    # Scratch location is per process, not a model/science setting. The full
    # config hash is also retained in each worker's initialization receipt.
    return digest({key: value for key, value in config.items() if key != 'work_dir'})


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read(path):
    with Path(path).open() as handle:
        return json.load(handle)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("Invalid request/worker identifier")
    return value


def atomic_json(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            os.link(temporary, path)
            os.unlink(temporary)
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def inventory(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Missing or unsafe artifact directory: " + str(root))
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlink in output artifact tree: " + str(path))
        if path.is_file():
            result[str(path.relative_to(root))] = {"sha256": sha256(path), "bytes": path.stat().st_size}
        elif not path.is_dir():
            raise ValueError("Special file in artifact tree: " + str(path))
    return result


def verify_inventory(root, files, *, exact=False):
    if exact:
        if inventory(root) != files:
            raise ValueError("Artifact inventory/checksums differ: " + str(root))
        return
    root = Path(root).resolve()
    for relative, expected in files.items():
        p = Path(relative)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError("Unsafe artifact path")
        path = root / p
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError("Missing or unsafe artifact: " + relative)
        if path.stat().st_size != expected["bytes"] or sha256(path) != expected["sha256"]:
            raise ValueError("Artifact checksum mismatch: " + relative)


def now():
    return time.time()
