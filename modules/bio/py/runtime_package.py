"""Immutable compressed runtime snapshots, verified before worker extraction."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time

from worker_progress import Activity

FORMAT = "bio-runtime-tar-zstd-v1"
GIB = 1024**3
CHUNK = 8 * 1024**2


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def scope(value):
    return "msa" if value["recipe"] == "msa" else "gpu"


def safe_root(path):
    if path.resolve() != path or path.is_symlink():
        raise ValueError("Runtime archive root contains a symlink")


def archive_key(value):
    return hashlib.sha256(canonical(dict(format=FORMAT, source=value["source_fingerprint"],
        paths=value["paths"], bytes=value["source_bytes"], files=value["source_files"],
        builder_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))).hexdigest()


def read_receipt(root, key, value):
    directory = root / key
    path = directory / "receipt.json"
    if not path.exists():
        return None
    safe_root(directory)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("Invalid runtime package receipt")
    receipt = json.loads(path.read_text())
    archive = directory / "runtime.tar.zst"
    if (receipt.get("format") != FORMAT or receipt.get("key") != key
            or receipt.get("source_fingerprint") != value["source_fingerprint"]
            or receipt.get("source_bytes") != value["source_bytes"]
            or receipt.get("source_files") != value["source_files"]
            or receipt.get("paths") != value["paths"]
            or not archive.is_file() or archive.is_symlink()
            or archive.stat().st_size != receipt.get("archive_bytes")):
        raise ValueError("Runtime package cache is incomplete or changed; rebuild the affected package")
    return receipt


def publish(shared, value, inspect_source):
    """Called on the head under the existing leases excluding runtime writers."""
    shared = shared.absolute()
    root = shared / "runtime-packages" / "v1"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    safe_root(root)
    key = archive_key(value)
    fd = os.open(root / (key + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        receipt = read_receipt(root, key, value)
        if receipt is None:
            # Compression can expand incompressible data slightly. Keep room for
            # regular result publication while this first-time snapshot is built.
            required = math.ceil(value["source_bytes"] * 1.02) + 8 * GIB
            if shutil.disk_usage(root).free < required:
                raise ValueError("Shared storage lacks space for a runtime archive plus result headroom")
            temporary = Path(tempfile.mkdtemp(prefix=".building-", dir=root))
            compressor = archiver = None
            try:
                archive = temporary / "runtime.tar.zst"
                present = [name for name in value["paths"] if (shared / name).exists()]
                with Activity("runtime_package", scope=scope(value),
                              message="Building the reusable runtime archive on the head",
                              eta={"state": "unknown", "scope": "stage", "basis": "First archive build for these runtime files"}):
                    with archive.open("xb") as output:
                        compressor = subprocess.Popen(["zstd", "-q", "-1", "-T2", "-c"],
                            stdin=subprocess.PIPE, stdout=output)
                        command = ["tar", "--create", "--format=pax", "--hard-dereference",
                                   "--sort=name", "--file=-", "--directory", str(shared)]
                        command += ["--", *present] if present else ["--files-from=/dev/null"]
                        archiver = subprocess.Popen(command, stdout=compressor.stdin)
                        compressor.stdin.close()
                        if archiver.wait() or compressor.wait():
                            raise ValueError("Runtime archive creation failed")
                        output.flush(); os.fsync(output.fileno())
                    after = inspect_source()
                    if any(after[k] != value[k] for k in ("source_fingerprint", "source_bytes", "source_files", "paths")):
                        raise ValueError("Runtime sources changed during archive creation")
                    digest = hashlib.sha256()
                    with archive.open("rb") as stream:
                        while data := stream.read(CHUNK):
                            digest.update(data)
                    receipt = dict(format=FORMAT, key=key, source_fingerprint=value["source_fingerprint"],
                        source_bytes=value["source_bytes"], source_files=value["source_files"],
                        paths=value["paths"], archive_bytes=archive.stat().st_size,
                        archive_sha256=digest.hexdigest(), created_epoch=time.time())
                    (temporary / "receipt.json").write_bytes(canonical(receipt) + b"\n")
                    archive.chmod(0o400)
                    (temporary / "receipt.json").chmod(0o400)
                    os.rename(temporary, root / key)
            finally:
                for process in (archiver, compressor):
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.wait()
                if temporary.exists():
                    shutil.rmtree(temporary)
        result = dict(value, package=dict(receipt, relative_archive=f"runtime-packages/v1/{key}/runtime.tar.zst"))
        result["os_size_gb"] = max(50, math.ceil((value["source_bytes"] * 1.15 + receipt["archive_bytes"]
                                                  + value["setup_reserve_bytes"]) / 10**9))
        if result["os_size_gb"] > 200:
            raise ValueError("Selected packed runtime exceeds the 200 GB worker disk cap; no worker launched")
        return result
    finally:
        os.close(fd)


def member_path(name, selected):
    relative = PurePosixPath(name.rstrip("/"))
    if (relative.is_absolute() or not relative.parts or any(p in {".", ".."} for p in relative.parts)
            or str(relative) != name.rstrip("/") or "\\" in name):
        raise ValueError("Unsafe path in runtime archive")
    if not any(str(relative) == root or str(relative).startswith(root + "/") for root in selected):
        raise ValueError("Archive member is outside the selected runtime")
    return relative


def extract(archive, destination, value):
    """Materialize only regular files/directories/leaf symlinks in a fresh tree."""
    seen = set()
    leaves = set()
    directories = []
    copied = files = 0
    process = subprocess.Popen(["zstd", "-q", "-d", "-c", str(archive)], stdout=subprocess.PIPE)
    try:
        with Activity("runtime_extract", scope=scope(value), message="Unpacking the verified runtime onto local disk",
                      completed=0, total=value["source_bytes"], unit="bytes") as progress:
            with tarfile.open(fileobj=process.stdout, mode="r|") as package:
                for member in package:
                    relative = member_path(member.name, value["paths"])
                    key = str(relative)
                    if key in seen or any(str(parent) in leaves for parent in relative.parents):
                        raise ValueError("Duplicate or redirected runtime archive member")
                    seen.add(key)
                    target = destination / key
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if member.isdir():
                        target.mkdir(mode=0o700, exist_ok=True)
                        directories.append((target, member.mode & 0o777, member.mtime))
                    elif member.issym():
                        if "\0" in member.linkname:
                            raise ValueError("Invalid runtime symlink")
                        # These must retain original absolute venv interpreter and
                        # editable-import paths. Nothing may be extracted beneath
                        # a symlink; internal shared targets must remain selected.
                        resolved = (target.parent / member.linkname).resolve(strict=False)
                        original = Path("/mnt/bio-shared")
                        if resolved.is_relative_to(original):
                            member_path(str(resolved.relative_to(original)), value["paths"])
                        os.symlink(member.linkname, target)
                        leaves.add(key); files += 1
                    elif member.isfile():
                        if member.size < 0 or copied + member.size > value["source_bytes"]:
                            raise ValueError("Runtime archive exceeds its declared size")
                        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        try:
                            with os.fdopen(fd, "wb") as output, package.extractfile(member) as source:
                                remaining = member.size
                                while remaining:
                                    data = source.read(min(CHUNK, remaining))
                                    if not data:
                                        raise ValueError("Truncated runtime archive member")
                                    output.write(data); remaining -= len(data); copied += len(data)
                                    progress.update(completed=copied)
                                output.flush(); os.fchmod(output.fileno(), member.mode & 0o777)
                            os.utime(target, (member.mtime, member.mtime), follow_symlinks=False)
                        except BaseException:
                            # fdopen owns fd after entering; the fresh destination
                            # is discarded by the caller if any member fails.
                            raise
                        leaves.add(key); files += 1
                    else:
                        raise ValueError("Unsupported special or hard-linked runtime archive member")
                    if files > value["source_files"]:
                        raise ValueError("Runtime archive exceeds its declared file count")
            if process.wait() != 0:
                raise ValueError("Runtime archive decompression failed")
            if copied != value["source_bytes"] or files != value["source_files"]:
                raise ValueError("Runtime archive contents differ from the source inventory")
            for path, mode, modified in reversed(directories):
                path.chmod(mode)
                os.utime(path, (modified, modified))
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()


def stage(shared, destination, value, roots):
    receipt = value["package"]
    relative = receipt.get("relative_archive", "")
    expected = f"runtime-packages/v1/{receipt.get('key')}/runtime.tar.zst"
    if (receipt.get("format") != FORMAT or relative != expected
            or not isinstance(receipt.get("key"), str) or len(receipt["key"]) != 64
            or any(ch not in "0123456789abcdef" for ch in receipt["key"])
            or receipt.get("source_bytes") != value["source_bytes"]
            or receipt.get("source_files") != value["source_files"]
            or receipt.get("paths") != value["paths"]
            or receipt.get("source_fingerprint") != value.get("source_fingerprint")
            or type(receipt.get("archive_bytes")) is not int or receipt["archive_bytes"] < 1):
        raise ValueError("Invalid packed runtime plan")
    archive = shared / relative
    safe_root(archive)
    required = value["source_bytes"] + receipt["archive_bytes"] + 8 * GIB
    if shutil.disk_usage(destination.parent).free < required:
        raise ValueError("Worker disk lacks space for the runtime archive and extracted files")
    temporary = Path(tempfile.mkdtemp(prefix=".runtime-download-", dir=destination.parent))
    destination.mkdir()
    try:
        downloaded = temporary / "runtime.tar.zst"
        digest = hashlib.sha256()
        read_bytes = 0
        with Activity("runtime_download", scope=scope(value), message="Downloading the runtime archive",
                      completed=0, total=receipt["archive_bytes"], unit="bytes") as progress:
            with archive.open("rb") as source, downloaded.open("xb") as target:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("Runtime archive is not a regular file")
                os.posix_fadvise(source.fileno(), 0, 0, os.POSIX_FADV_SEQUENTIAL)
                while data := source.read(CHUNK):
                    read_bytes += len(data)
                    if read_bytes > receipt["archive_bytes"]:
                        raise ValueError("Runtime archive is larger than its receipt")
                    digest.update(data); target.write(data); progress.update(completed=read_bytes)
                target.flush()
            if read_bytes != receipt["archive_bytes"] or digest.hexdigest() != receipt["archive_sha256"]:
                raise ValueError("Runtime archive checksum mismatch")
        extract(downloaded, destination, value)
        for relative_root in roots:
            (destination / relative_root).mkdir(mode=0o755, parents=True, exist_ok=True)
        (destination.parent / "runtime-package-receipt.json").write_bytes(canonical(dict(receipt,
            verified_epoch=time.time(), extracted_bytes=value["source_bytes"], downloaded_bytes=read_bytes)) + b"\n")
    except BaseException:
        shutil.rmtree(destination)
        raise
    finally:
        shutil.rmtree(temporary)
