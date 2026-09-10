"""Bounded, read-only parallel index loading with temporary BDI read-ahead."""
import argparse
import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import stat
import threading
import time
import uuid

CHUNK = 16*1024**2
READERS = 4
READ_AHEAD_KB = 15360


def atomic(path, value):
    path = Path(path)
    temporary = path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    with temporary.open('x') as stream:
        json.dump(value, stream, sort_keys=True); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def restore(state):
    """Crash cleanup entry point; never overwrite unrelated BDI tuning."""
    state = Path(state)
    if not (state/'claim.json').exists(): return {'restored': True, 'devices': []}
    claim = json.loads((state/'claim.json').read_text())
    if claim['boot_id'] != boot_id(): raise ValueError('Read-ahead claim belongs to another boot')
    locks = []
    try:
        for row in claim['devices']:
            fd = os.open(row['lock'], os.O_RDWR | os.O_NOFOLLOW)
            locks.append(fd); fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return restore_claim(state, claim)
    finally:
        for fd in locks: os.close(fd)


def restore_claim(state, claim):
    if claim['boot_id'] != boot_id(): raise ValueError('Read-ahead claim belongs to another boot')
    for row in claim['devices']:
        path = Path(row['path'])
        current = int(path.read_text())
        if current not in (row['old'], row['temporary']):
            raise ValueError('Read-ahead was independently modified; refusing to overwrite it')
        if current != row['old']: path.write_text(str(row['old'])+'\n')
        if int(path.read_text()) != row['old']: raise ValueError('Read-ahead restoration failed')
    result = {'restored': True, 'devices': claim['devices'], 'checked_epoch': time.time()}
    atomic(Path(state)/'restored.json', result)
    return result


class ReadAhead:
    def __init__(self, paths, state, sysfs=Path('/sys/class/bdi'), lock_root=Path('/run/lock')):
        self.state = Path(state) if state else None
        self.paths = paths; self.sysfs = sysfs; self.lock_root = lock_root
        self.locks = []; self.claim = {'boot_id': boot_id(), 'devices': []}
        self.skipped = []

    def __enter__(self):
        if self.state is None: return self
        self.state.mkdir(mode=0o700, parents=True, exist_ok=False)
        try:
            for device in sorted({path.stat().st_dev for path in self.paths}):
                key = str(os.major(device))+':'+str(os.minor(device))
                path = self.sysfs/key/'read_ahead_kb'
                if not path.exists() or not os.access(path, os.W_OK):
                    self.skipped.append({'device': key, 'reason': 'read_ahead unavailable'}); continue
                lock = self.lock_root/('bio-msa-prefetch-'+key.replace(':','-')+'.lock')
                fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                self.locks.append(fd); fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                old = int(path.read_text())
                self.claim['devices'].append({'device': key, 'path': str(path), 'lock': str(lock),
                                               'old': old, 'temporary': max(old, READ_AHEAD_KB)})
            atomic(self.state/'claim.json', self.claim)
            for row in self.claim['devices']:
                path = Path(row['path']); path.write_text(str(row['temporary'])+'\n')
                if int(path.read_text()) != row['temporary']: raise ValueError('Read-ahead tuning failed')
            return self
        except BaseException:
            self.__exit__(None, None, None); raise

    def __exit__(self, *_):
        try:
            if self.state is not None and (self.state/'claim.json').exists():
                restore_claim(self.state, self.claim)
        finally:
            for fd in self.locks: os.close(fd)
            self.locks.clear()


def load(paths, deadline, cancelled=lambda: False, *, state=None, readers=READERS, progress=None):
    """Four disjoint buffered readers; success still requires the caller's mincore check."""
    if type(readers) is not int or not 1 <= readers <= READERS: raise ValueError('Prefetch readers must be1..4')
    paths = [Path(path) for path in paths]
    stop = threading.Event(); lock = threading.Lock(); read_bytes = 0
    descriptors = []; jobs = []
    try:
        for path in paths:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            descriptors.append(fd); info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0: raise ValueError('Invalid prefetch index')
            width = ((info.st_size+readers-1)//readers+4095)//4096*4096
            binding = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            jobs.extend((path, start, min(start+width, info.st_size), binding) for start in range(0, info.st_size, width))
        total = sum(os.fstat(fd).st_size for fd in descriptors)
        def check():
            if stop.is_set() or cancelled() or time.time() >= deadline:
                stop.set(); raise ValueError('Index warm-up interrupted or timed out')
        def read(job):
            nonlocal read_bytes
            path, offset, end, binding = job
            # Each independent open has its own kernel readahead state. Sharing
            # one descriptor across distant ranges defeats sequential prefetch.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != binding:
                    raise ValueError('Index identity changed before prefetch')
                while offset < end:
                    check()
                    data = os.pread(fd, min(CHUNK, end-offset), offset)
                    if not data: raise ValueError('Unexpected EOF in prefetch index')
                    offset += len(data)
                    with lock: read_bytes += len(data)
                if os.fstat(fd).st_mtime_ns != binding[-1]: raise ValueError('Index changed during prefetch')
                return offset
            finally: os.close(fd)
        check()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=readers, thread_name_prefix='msa-prefetch')
        try:
            with ReadAhead(paths, state) as tuning:
                futures = [pool.submit(read, job) for job in jobs]
                while True:
                    done, pending = concurrent.futures.wait(futures, timeout=.2)
                    for future in done: future.result()
                    check()
                    if progress:
                        with lock: value = read_bytes
                        progress({'read_bytes': value, 'total_bytes': total, 'readers': readers, 'checked_epoch': time.time()})
                    if not pending: break
                if read_bytes != total: raise ValueError('Prefetch byte count differs from index sizes')
                result = {'readers': readers, 'read_bytes': read_bytes, 'total_bytes': total,
                          'read_only': True, 'read_ahead_devices': tuning.claim['devices'],
                          'read_ahead_skipped': tuning.skipped}
            result['read_ahead_restored'] = True
            return result
        finally:
            # Restore tuning before waiting for a blocked NFS read to return.
            stop.set(); pool.shutdown(wait=True, cancel_futures=True)
    finally:
        for fd in descriptors: os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['restore']); parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args(); print(json.dumps(restore(args.state), sort_keys=True))


if __name__ == '__main__': main()
