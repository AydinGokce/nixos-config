#!/usr/bin/env python3
"""Independent, bounded SSH streams into an UNPUBLISHED database file.

The existing bootstrap must own the RW filesystem and stop all other writers.
This is a transfer mechanism, never a publication/ready implementation.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time

CHUNK = 8 * 1024**2


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def split(size, streams):
    require(type(size) is int and size > 0, "Invalid file length")
    require(type(streams) is int and 1 <= streams <= 32, "Invalid stream count")
    width = (size + streams - 1) // streams
    return [{"offset": start, "length": min(width, size-start)}
            for start in range(0, size, width)]


def safe_file(root, relative, *, private=True):
    root = Path(root).absolute()
    name = PurePosixPath(relative)
    require(relative and not name.is_absolute() and str(name) == relative
            and ".." not in name.parts and relative != "."
            and not any(ord(c) < 32 for c in relative), "Unsafe relative file")
    require(root.is_dir() and root.resolve() == root, "Root is not a real directory")
    path = root / relative
    require(path.parent.is_dir() and path.parent.resolve() == path.parent,
            "Destination parents missing or symlinked")
    require(path.parent.stat().st_dev == root.stat().st_dev, "Destination crosses filesystem")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and (not private or info.st_nlink == 1)
                and info.st_dev == root.stat().st_dev, "Destination is not a private regular file")
    return path


def initialize(root, relative, size, parents=()):
    """Caller holds the existing bootstrap lease; no other writer may be live."""
    require(type(size) is int and size > 0, "Invalid file length")
    for row in sorted(parents, key=lambda r: len(PurePosixPath(r['path']).parts)):
        name = row['path']
        require(row['kind'] == 'directory' and name in {str(p) for p in PurePosixPath(relative).parents},
                'Unexpected range parent directory')
        if name == '.':
            continue
        path = Path(root)/name
        require(path.parent.resolve() == path.parent and path.parent.stat().st_dev == Path(root).stat().st_dev,
                'Range parent escaped pending filesystem')
        path.mkdir(mode=row['source_metadata']['mode'], exist_ok=True)
        require(path.is_dir() and not path.is_symlink() and path.stat().st_dev == Path(root).stat().st_dev,
                'Range parent is not a real directory on the pending filesystem')
    path = safe_file(root, relative)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        # Always reset then recopy all ranges on retry. No partial data is trusted.
        os.ftruncate(fd, 0)
        os.ftruncate(fd, size)
        os.fsync(fd)
    finally:
        os.close(fd)


def receive(root, relative, size, span, stream, cancelled=None, ready=None):
    offset, length = span["offset"], span["length"]
    require(type(size) is int and size > 0 and type(offset) is int and type(length) is int
            and offset >= 0 and length > 0 and offset + length <= size, "Range is out of bounds")
    path = safe_file(root, relative)
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    result = hashlib.sha256()
    try:
        require(os.fstat(fd).st_size == size, "Destination size changed")
        if ready:
            ready(dict(status='ready', **span))
        position = 0
        while position < length:
            require(cancelled is None or not cancelled.is_set(), "Range transfer cancelled")
            data = stream.read(min(CHUNK, length-position))
            require(data, "Short range stream")
            require(len(data) <= length-position, "Oversized range stream")
            result.update(data)
            written = 0
            while written < len(data):
                count = os.pwrite(fd, data[written:], offset+position+written)
                require(count > 0, "Short destination write")
                written += count
            position += len(data)
        require(stream.read(1) == b"", "Extra bytes after range")
        require(cancelled is None or not cancelled.is_set(), "Range transfer cancelled")
        os.fsync(fd)
        require(os.fstat(fd).st_size == size, "Destination size changed")
        return dict(**span, sha256=result.hexdigest(), bytes=position)
    finally:
        os.close(fd)


def send(fd, span, output, cancelled, progress=None):
    result = hashlib.sha256()
    position = 0
    while position < span["length"]:
        require(not cancelled.is_set(), "Range transfer cancelled")
        data = os.pread(fd, min(CHUNK, span["length"]-position), span["offset"]+position)
        require(data, "Source ended before requested range")
        result.update(data)
        written = 0
        while written < len(data):
            count = output.write(data[written:])
            require(count is not None and count > 0, "Short stream write")
            written += count
        position += len(data)
        if progress:
            progress(position)
    output.flush()
    return result.hexdigest()


def ssh_argv(host, identity, known_hosts):
    import ipaddress
    require(host.startswith("root@"), "Expected pinned root worker")
    ipaddress.IPv4Address(host[5:])
    for path in (identity, known_hosts):
        require(Path(path).is_absolute() and "\n" not in str(path), "SSH path must be absolute")
    return ["ssh", "-i", str(identity), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile="+str(known_hosts),
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "UpdateHostKeys=no",
            "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ControlPersist=no",
            "-o", "Compression=no", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3", host]


def make_plan(worker, source, inventory, inventory_sha256, binding, device, relative, streams=32):
    """Use the existing aws_worker module and exact frozen source inventory."""
    source = worker.content.real_root(Path(source))
    inventory = worker.read_pinned(Path(inventory), inventory_sha256)
    require(inventory.get('kind') == worker.KIND and inventory.get('stage') == 'source-inventory'
            and worker.content.generation(source) == inventory['source'], "Source generation changed")
    rows = [row for row in inventory['entries'] if row['path'] == relative]
    require(len(rows) == 1 and rows[0]['kind'] == 'file', "Not one exact regular inventory entry")
    worker.content.check_source(source, rows[0])
    owner = worker.binding(binding)
    require(owner['source_manifest_sha256'] == inventory['source']['source_manifest_sha256']
            and owner['source_receipt_sha256'] == inventory['source']['source_receipt_sha256'],
            "Source differs from owned cache binding")
    parent_names = {str(p) for p in PurePosixPath(relative).parents}
    parents = [r for r in inventory['entries'] if r['path'] in parent_names and r['kind'] == 'directory']
    require({r['path'] for r in parents} == parent_names, 'Inventory lacks exact range parent directories')
    return dict(schema=1, kind='aws-database-range-transfer', inventory_sha256=inventory_sha256, source=inventory['source'],
                entry=rows[0], binding=owner, device=device, streams=streams,
                parents=parents,
                ranges=split(rows[0]['source_metadata']['size'], streams))


def transfer(source, plan, command_for_slot, *, cancelled=None, callback=None, log_root=None):
    """command_for_slot supplies exact authenticated SSH argv, or local tests."""
    cancelled = cancelled or threading.Event()
    path = safe_file(source, plan["entry"]["path"], private=False)
    size = plan["entry"]["source_metadata"]["size"]
    require(plan["ranges"] == split(size, plan["streams"]), "Range coverage differs")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    initial = os.fstat(fd)
    processes = set()
    lock = threading.Lock()
    handshakes = threading.BoundedSemaphore(4)
    progress = [dict(**span, bytes_sent=0, state='waiting') for span in plan['ranges']]
    started = time.monotonic()

    def update(slot, **values):
        with lock:
            progress[slot].update(values)

    def stop():
        cancelled.set()
        with lock:
            children = list(processes)
        for child in children:
            if child.poll() is None:
                try: os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError: pass
        end = time.monotonic()+5
        for child in children:
            try: child.wait(timeout=max(0, end-time.monotonic()))
            except subprocess.TimeoutExpired:
                try: os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError: pass

    def snapshot():
        with lock:
            rows = [dict(row) for row in progress]
        sent = sum(row['bytes_sent'] for row in rows)
        elapsed = max(.001, time.monotonic()-started)
        return dict(path=plan['entry']['path'], streams=plan['streams'], total_bytes=size,
                    bytes_sent=sent, completed_range_bytes=sum(r['length'] for r in rows if r['state']=='complete'),
                    elapsed_seconds=elapsed, average_bytes_per_second=sent/elapsed,
                    transfer_eta_seconds=(size-sent)/(sent/elapsed) if sent else None, ranges=rows)

    def one(slot):
        span = plan["ranges"][slot]
        # At most four unauthenticated connections: Ubuntu's default MaxStartups
        # can otherwise reject a burst of legitimate SSH connections.
        while not handshakes.acquire(timeout=.2):
            require(not cancelled.is_set(), 'Range transfer cancelled')
        acquired, child, errors = True, None, None
        try:
            require(not cancelled.is_set(), "Range transfer cancelled")
            update(slot, state='connecting')
            if log_root:
                errors = (Path(log_root)/f'range-{slot:02d}.log').open('ab')
            with lock:
                require(not cancelled.is_set(), 'Range transfer cancelled before launch')
                child = subprocess.Popen(command_for_slot(slot), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=errors or subprocess.DEVNULL,
                                         start_new_session=True)
                processes.add(child)
            hello = child.stdout.readline(4097)
            require(len(hello) <= 4096 and json.loads(hello) == dict(status='ready', **span),
                    'Receiver did not authenticate and prepare the exact range')
            handshakes.release(); acquired = False
            update(slot, state='sending')
            digest = send(fd, span, child.stdin, cancelled, lambda n: update(slot, bytes_sent=n))
            update(slot, state='flushing')
            child.stdin.close()
            child.stdin = None
            raw, _ = child.communicate(timeout=120)
            require(child.returncode == 0, "Receiver failed; inspect retained range log")
            require(len(raw) <= 4096, 'Oversized range receipt')
            receipt = json.loads(raw)
            require(receipt == dict(**span, sha256=digest, bytes=span["length"]),
                    "Receiver range receipt differs")
            update(slot, state='complete')
            return receipt
        except BaseException:
            cancelled.set()
            update(slot, state='failed')
            raise
        finally:
            if acquired:
                handshakes.release()
            if child:
                if child.poll() is None:
                    try: os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                child.wait()
                for pipe in (child.stdin, child.stdout):
                    if pipe:
                        pipe.close()
                with lock:
                    processes.discard(child)
            if errors:
                errors.close()

    try:
        actual = dict(inode=initial.st_ino, size=initial.st_size, mode=stat.S_IMODE(initial.st_mode),
                      uid=initial.st_uid, gid=initial.st_gid, mtime_ns=initial.st_mtime_ns, ctime_ns=initial.st_ctime_ns)
        require(actual == plan['entry']['source_metadata'], 'Source metadata differs from inventory')
        if log_root:
            Path(log_root).mkdir(parents=True, exist_ok=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=plan["streams"]) as pool:
            futures = [pool.submit(one, slot) for slot in range(len(plan['ranges']))]
            try:
                while True:
                    require(not cancelled.is_set(), 'Range transfer cancelled or another range failed')
                    if callback:
                        callback(snapshot())
                    done, pending = concurrent.futures.wait(futures, timeout=1)
                    for future in done:
                        future.result()
                    if not pending:
                        break
                results = [future.result() for future in futures]
            except BaseException:
                stop()
                raise
        current = os.fstat(fd)
        require((initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns)
                == (current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns),
                "Source changed during transfer")
        if callback:
            callback(snapshot())
        return results
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["initialize", "receive", "finish"])
    parser.add_argument("--tools", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--slot", type=int)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.tools)/"msa"))
    import aws_worker
    plan = aws_worker.read_pinned(args.plan, args.plan_sha256)
    require(plan.get('schema') == 1 and plan.get('kind') == 'aws-database-range-transfer', 'Wrong range transfer plan')
    owner = aws_worker.binding(plan["binding"])
    aws_worker.mounted_cache(aws_worker.MOUNTPOINT, owner, plan["device"], False)
    root = aws_worker.MOUNTPOINT/aws_worker.PENDING
    require(root.is_dir() and root.resolve() == root
            and root.stat().st_dev == aws_worker.MOUNTPOINT.stat().st_dev,
            "Pending directory is not on the owned filesystem")
    require(all(not (aws_worker.MOUNTPOINT/name).exists()
                and not (aws_worker.MOUNTPOINT/name).is_symlink()
                for name in ("colabfold", "ready.json")), "Database is already published")
    row = plan["entry"]
    require(row["kind"] == "file", "Only regular inventory files may use ranges")
    size = row["source_metadata"]["size"]
    require(plan["ranges"] == split(size, plan["streams"]), "Noncanonical range coverage")
    if args.command == "initialize":
        initialize(root, row["path"], size, plan['parents'])
        result = {"status": "unpublished", "size": size}
    elif args.command == "receive":
        require(args.slot is not None and 0 <= args.slot < len(plan["ranges"]), "Invalid range slot")
        result = receive(root, row["path"], size, plan["ranges"][args.slot], sys.stdin.buffer,
                         ready=lambda row: print(json.dumps(row, sort_keys=True), flush=True))
    else:
        path = safe_file(root, row["path"])
        require(path.stat().st_size == size, "Destination size changed")
        aws_worker.content.preserve(path, row["source_metadata"])
        result = {"status": "unpublished", "requires_full_source_and_destination_hash": True}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
