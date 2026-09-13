"""Lazy CPU-only gallery previews, separate from cloud and library mutations.

RPC requests authorize an existing gallery association and enqueue a derivative.
One dedicated service renders the durable queue with the desktop studio renderer.
Neither a user filename nor a renderer command can be supplied through the API.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import resource
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import zlib

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).absolute().parent.parent))
from workbench.common import (Error, atomic, canonical, digest, keys, no_links,
                              number, parse, require, safe_file, sha, string)

WIDTH, HEIGHT = 640, 480
MAX_SOURCE = 32 * 1024 * 1024
MAX_IMAGE = 2 * 1024 * 1024
MAX_RECEIPT = 128 * 1024
MAX_RECORD = 16 * 1024
MAX_QUEUED = 16
MAX_ENTRIES = 512
MAX_CACHE_BYTES = 256 * 1024 * 1024
CHUNK = 262144
RENDER_SECONDS = 60
HEARTBEAT_SECONDS = 90
FAILED_SECONDS = 300
IDLE_SECONDS = 2
DEFAULT_RENDERER = '/run/current-system/sw/bin/bio-render-headless'
STATES = {'queued', 'rendering', 'ready', 'failed'}


def _resolve(api, reference, entry_id):
    from workbench.library_structures import resolve_structure
    result = resolve_structure(api, reference, entry_id)
    require(result['ref'] == reference and result['entry_id'] == entry_id,
            'Gallery source identity changed', 'integrity')
    sha(result['source_sha256'])
    number(result['source_size'], 'Structure size', 1, MAX_SOURCE)
    require(result['source_format'] in ('pdb', 'cif', 'mmcif'),
            'Preview requires PDB or mmCIF coordinates')
    return result


def renderer_identity(executable=None):
    """Nix wrapper content binds its exact renderer/Mesa closure, not Cargo 0.3.0."""
    path = Path(executable or os.environ.get('BIO_WORKBENCH_RENDERER', DEFAULT_RENDERER))
    require(path.is_absolute(), 'Preview renderer is not configured', 'unavailable')
    try:
        path = path.resolve(strict=True)
        require(path.is_file() and os.access(path, os.X_OK),
                'Preview renderer is unavailable', 'unavailable')
        wrapper = _bytes(path, 1024 * 1024)
        helper = _bytes(Path(__file__).resolve(), 1024 * 1024)
    except OSError as exc:
        raise Error('unavailable', 'Preview renderer is unavailable') from exc
    fingerprint = digest({'schema': 1, 'executable': str(path),
        'wrapper_sha256': hashlib.sha256(wrapper).hexdigest(),
        'helper_sha256': hashlib.sha256(helper).hexdigest()})
    return str(path), fingerprint


def _bytes(path, limit):
    file = safe_file(path)
    require(0 < file.stat().st_size <= limit, 'Preview file exceeds its limit', 'limit')
    with file.open('rb') as stream:
        result = stream.read(limit + 1)
    require(0 < len(result) <= limit, 'Preview file exceeds its limit', 'limit')
    return result


def _root(state):
    root = no_links(Path(state) / 'structure-thumbnails')
    no_links(root / 'entries')
    (root / 'entries').mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


@contextmanager
def _lock(root, name='cache.lock', *, blocking=True):
    path = no_links(root / name)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def _spec(source, fingerprint):
    return {'schema': 1, 'source_sha256': source['source_sha256'],
            'source_format': source['source_format'], 'renderer_fingerprint': fingerprint,
            'width': WIDTH, 'height': HEIGHT, 'style': 'cartoon',
            'camera': 'studio-default-v1', 'title': 'Protein structure'}


def _record(folder):
    value = parse(_bytes(folder / 'record.json', MAX_RECORD))
    require(isinstance(value, dict) and value.get('state') in STATES,
            'Invalid preview cache state', 'integrity')
    spec = value.get('spec', {})
    sha(spec.get('source_sha256')); sha(spec.get('renderer_fingerprint'))
    require(spec == _spec({'source_sha256': spec['source_sha256'],
                          'source_format': spec.get('source_format')}, spec['renderer_fingerprint'])
            and spec['source_format'] in ('pdb', 'cif', 'mmcif')
            and value.get('cache_key') == digest(spec) == folder.name,
            'Invalid preview cache identity', 'integrity')
    number(value.get('source_size'), 'Structure size', 1, MAX_SOURCE)
    require(isinstance(value.get('source_path'), str) and Path(value['source_path']).is_absolute(),
            'Invalid preview source', 'integrity')
    for field in ('created_epoch', 'updated_epoch', 'last_access_epoch'):
        require(type(value.get(field)) in (int, float) and 0 <= value[field] < 10**11,
                'Invalid preview timestamp', 'integrity')
    number(value.get('attempts'), 'Preview attempts', 0, 1_000_000)
    return value


def _records(root):
    result = []
    for folder in (root / 'entries').iterdir():
        sha(folder.name)
        no_links(folder)
        require(folder.is_dir(), 'Invalid preview cache directory', 'integrity')
        try:
            record = _record(folder)
        except (Error, OSError, ValueError, TypeError, KeyError):
            # A crashed initial publication or corrupt derivative is dispensable.
            shutil.rmtree(folder)
            continue
        result.append((folder, record))
        require(len(result) <= MAX_ENTRIES + MAX_QUEUED,
                'Preview cache inventory exceeds its limit', 'limit')
    return result


def _write(folder, value):
    folder.mkdir(mode=0o700, exist_ok=True)
    raw = canonical(value)
    require(len(raw) <= MAX_RECORD, 'Preview receipt exceeds its limit', 'limit')
    atomic(folder / 'record.json', raw)


def _prune(root, *, reserve=0, keep=None):
    entries = _records(root)
    size = sum((folder / 'image.png').stat().st_size
               for folder, _ in entries if (folder / 'image.png').is_file())
    candidates = sorted(((folder, value) for folder, value in entries
                         if value['state'] in ('ready', 'failed') and folder.name != keep),
                        key=lambda item: item[1]['last_access_epoch'])
    count = len(entries)
    for folder, _ in candidates:
        if count + reserve <= MAX_ENTRIES and size <= MAX_CACHE_BYTES:
            break
        image = folder / 'image.png'
        size -= image.stat().st_size if image.is_file() else 0
        shutil.rmtree(folder)
        count -= 1
    require(count + reserve <= MAX_ENTRIES and size <= MAX_CACHE_BYTES,
            'Preview cache is busy; retry shortly', 'busy')


def _heartbeat(root, fingerprint):
    atomic(root / 'worker.json', canonical({'renderer_fingerprint': fingerprint,
                                           'updated_epoch': time.time()}))


def _worker_available(root, fingerprint, current):
    try:
        value = parse(_bytes(root / 'worker.json', 2048))
        return (value['renderer_fingerprint'] == fingerprint
                and type(value['updated_epoch']) in (int, float)
                and 0 <= current - value['updated_epoch'] <= HEARTBEAT_SECONDS)
    except (Error, OSError, ValueError, TypeError, KeyError):
        return False


def _envelope(source, fingerprint, key, state, **extra):
    return {'schema': 1, 'ref': source['ref'], 'entry_id': source['entry_id'],
            'source_sha256': source['source_sha256'], 'renderer_fingerprint': fingerprint,
            'cache_key': key, 'state': state, 'width': WIDTH, 'height': HEIGHT,
            'style': 'cartoon', 'retry_after_seconds': IDLE_SECONDS, **extra}


def _image(folder, record):
    receipt = record.get('image', {})
    sha(receipt.get('sha256'))
    number(receipt.get('size'), 'Preview size', 1, MAX_IMAGE)
    data = _bytes(folder / 'image.png', MAX_IMAGE)
    require(len(data) == receipt['size'] and hashlib.sha256(data).hexdigest() == receipt['sha256'],
            'Preview checksum changed', 'integrity')
    _png(data)
    return data


def status(api, params):
    keys(params, ('ref', 'entry_id'))
    string(params['ref'], 'ref', 256); string(params['entry_id'], 'entry_id', 256)
    source = _resolve(api, params['ref'], params['entry_id'])
    try:
        _, fingerprint = renderer_identity()
    except (Error, OSError):
        return _envelope(source, None, None, 'unavailable',
                         error='Preview renderer is not installed on this head.', retry_after_seconds=30)
    spec = _spec(source, fingerprint); key = digest(spec)
    root = _root(api.store.root); folder = root / 'entries' / key
    current = time.time()
    with _lock(root):
        _prune(root)
        record = _record(folder) if folder.exists() else None
        if record and record['state'] == 'ready':
            try:
                _image(folder, record)
            except (Error, OSError, ValueError, KeyError):
                record['state'] = 'failed'; record['updated_epoch'] = 0
                record.pop('image', None)
            else:
                record['last_access_epoch'] = current; _write(folder, record)
                return _envelope(source, fingerprint, key, 'ready', **record['image'])
        if record and record['state'] == 'failed' and current - record['updated_epoch'] < FAILED_SECONDS:
            return _envelope(source, fingerprint, key, 'failed',
                             error=record.get('error', 'Structure preview failed.'),
                             retry_after_seconds=max(1, int(FAILED_SECONDS - (current - record['updated_epoch']))))
        if not _worker_available(root, fingerprint, current):
            return _envelope(source, fingerprint, key, 'unavailable',
                             error='The CPU preview service is unavailable.', retry_after_seconds=5)
        if record and record['state'] in ('queued', 'rendering'):
            return _envelope(source, fingerprint, key, record['state'])
        count = sum(value['state'] in ('queued', 'rendering') for _, value in _records(root))
        if count >= MAX_QUEUED:
            return _envelope(source, fingerprint, key, 'busy',
                             error='The CPU preview queue is full; retry shortly.', retry_after_seconds=5)
        _prune(root, reserve=0 if record else 1, keep=key)
        record = {'cache_key': key, 'spec': spec, 'source_path': str(source['source_path']),
                  'source_size': source['source_size'], 'state': 'queued',
                  'created_epoch': current, 'updated_epoch': current, 'last_access_epoch': current,
                  'attempts': record['attempts'] if record else 0}
        _write(folder, record)
        return _envelope(source, fingerprint, key, 'queued')


def read(api, params):
    keys(params, ('ref', 'entry_id', 'cache_key', 'sha256', 'offset', 'length'))
    string(params['ref'], 'ref', 256); string(params['entry_id'], 'entry_id', 256)
    sha(params['cache_key']); sha(params['sha256'])
    offset = number(params['offset'], 'offset', 0, MAX_IMAGE)
    length = number(params['length'], 'length', 1, CHUNK)
    source = _resolve(api, params['ref'], params['entry_id'])
    _, fingerprint = renderer_identity(); key = digest(_spec(source, fingerprint))
    require(params['cache_key'] == key, 'Preview renderer or source changed; reload the gallery', 'conflict')
    root = _root(api.store.root)
    with _lock(root):
        folder = root / 'entries' / key
        require(folder.is_dir(), 'Preview expired; reload the gallery', 'not_found')
        record = _record(folder)
        require(record['state'] == 'ready' and record.get('image', {}).get('sha256') == params['sha256'],
                'Preview receipt changed; reload the gallery', 'conflict')
        data = _image(folder, record)
        require(offset <= len(data), 'Preview offset is beyond the image')
        chunk = data[offset:offset + length]; end = offset + len(chunk)
        record['last_access_epoch'] = time.time(); _write(folder, record)
        return _envelope(source, fingerprint, key, 'ready', **record['image'],
                         offset=offset, next_offset=end, eof=end == len(data),
                         data_base64=base64.b64encode(chunk).decode())


def _png(data):
    """Validate fixed-size PNG framing/CRCs before caching or returning bytes."""
    require(data.startswith(b'\x89PNG\r\n\x1a\n'), 'Renderer did not produce PNG', 'integrity')
    offset = 8; first = True; pixels = False
    while offset + 12 <= len(data):
        size, kind = struct.unpack('>I4s', data[offset:offset + 8])
        end = offset + 12 + size
        require(end <= len(data), 'Truncated preview PNG', 'integrity')
        payload = data[offset + 8:end - 4]
        require(struct.unpack('>I', data[end - 4:end])[0] == zlib.crc32(kind + payload),
                'Preview PNG chunk checksum failed', 'integrity')
        if first:
            require(kind == b'IHDR' and size == 13 and struct.unpack('>IIBBBBB', payload)
                    in ((WIDTH, HEIGHT, 8, 6, 0, 0, 0), (WIDTH, HEIGHT, 8, 2, 0, 0, 0)),
                    'Preview PNG has unexpected dimensions or encoding', 'integrity')
            first = False
        else:
            require(kind != b'IHDR', 'Repeated preview PNG header', 'integrity')
        pixels = pixels or kind == b'IDAT'
        if kind == b'IEND':
            require(size == 0 and end == len(data) and pixels, 'Invalid preview PNG end', 'integrity')
            return
        offset = end
    raise Error('integrity', 'Preview PNG lacks its end marker')


def _child_limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))


def _invoke(executable, manifest, output):
    environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'TMPDIR', 'LD_LIBRARY_PATH')
                   if key in os.environ}
    environment.update(LP_NUM_THREADS='2', OMP_NUM_THREADS='2')
    with tempfile.TemporaryFile() as stdout:
        process = subprocess.Popen([executable, '--manifest', str(manifest)], stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=subprocess.DEVNULL, env=environment,
            start_new_session=True, preexec_fn=_child_limits)
        try:
            returncode = process.wait(timeout=RENDER_SECONDS)
        finally:
            # The wrapper's Xvfb and every child are owned by this process group,
            # including a wrapper which exited without reaping its display.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        require(returncode == 0, 'Structure renderer failed', 'unavailable')
        require(stdout.tell() <= MAX_RECEIPT, 'Renderer receipt exceeds its limit', 'limit')
        stdout.seek(0)
        return parse(stdout.read(MAX_RECEIPT + 1)), _bytes(output, MAX_IMAGE)


def _render(root, record, executable, runner):
    data = _bytes(Path(record['source_path']), MAX_SOURCE)
    require(len(data) == record['source_size']
            and hashlib.sha256(data).hexdigest() == record['spec']['source_sha256'],
            'Retained structure changed before preview', 'integrity')
    with tempfile.TemporaryDirectory(prefix='.render-', dir=root) as temporary:
        work = Path(temporary); source = work / 'source.data'; source.write_bytes(data)
        output = work / 'image.png'; manifest = work / 'request.json'
        manifest.write_bytes(canonical({'structures': [{'path': str(source),
            'format': record['spec']['source_format'], 'title': record['spec']['title']}],
            'output': str(output), 'width': WIDTH, 'height': HEIGHT, 'style': 'cartoon'}))
        receipt, png = runner(executable, manifest, output)
    _png(png)
    require(isinstance(receipt, dict) and receipt.get('renderer') == 'bio-workbench-studio'
            and receipt.get('width') == WIDTH and receipt.get('height') == HEIGHT
            and receipt.get('sha256') == hashlib.sha256(png).hexdigest()
            and isinstance(receipt.get('structures'), list) and len(receipt['structures']) == 1
            and receipt['structures'][0].get('sha256') == record['spec']['source_sha256'],
            'Renderer output differs from its sealed source or PNG receipt', 'integrity')
    require(len(png) <= MAX_IMAGE, 'Preview image exceeds its limit', 'limit')
    return png


def tick(state, *, executable=None, runner=None):
    """Render at most one item. A cross-process lock excludes all other workers."""
    executable, fingerprint = renderer_identity(executable)
    root = _root(state)
    with _lock(root, 'worker.lock', blocking=False) as acquired:
        if not acquired:
            return False
        # Only an abandoned worker can leave these directories behind. Cleanup
        # happens under its exclusive lock before creating another 32 MiB copy.
        for leftover in root.glob('.render-*'):
            no_links(leftover)
            require(leftover.is_dir(), 'Invalid preview working directory', 'integrity')
            shutil.rmtree(leftover)
        _heartbeat(root, fingerprint)
        with _lock(root):
            # Holding worker.lock proves no previous render is still active.
            # An interrupted derivative can therefore be recovered safely.
            candidates = []
            for folder, record in _records(root):
                if record['state'] not in ('queued', 'rendering'):
                    continue
                if record['spec']['renderer_fingerprint'] != fingerprint:
                    record.update(state='failed', updated_epoch=time.time(),
                                  error='The preview renderer changed; reload the gallery.')
                    _write(folder, record)
                    continue
                candidates.append((folder, record))
            _prune(root)
            if not candidates:
                return False
            folder, record = min(candidates, key=lambda item: item[1]['created_epoch'])
            record.update(state='rendering', updated_epoch=time.time(), attempts=record['attempts'] + 1)
            _write(folder, record)
        try:
            png = _render(root, record, executable, runner or _invoke)
            with _lock(root):
                atomic(folder / 'image.png', png)
        except (Error, OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
            record.update(state='failed', updated_epoch=time.time(),
                          error='Structure preview failed; the original file remains available.')
            record.pop('image', None)
        else:
            record.update(state='ready', updated_epoch=time.time(),
                          image={'sha256': hashlib.sha256(png).hexdigest(), 'size': len(png)})
            record.pop('error', None)
        with _lock(root):
            _write(folder, record)
            _prune(root, keep=folder.name)
        _heartbeat(root, fingerprint)
        return True


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default=os.environ.get('BIO_WORKBENCH_STATE', '/var/lib/bio-workbench'))
    parser.add_argument('--renderer', default=os.environ.get('BIO_WORKBENCH_RENDERER', DEFAULT_RENDERER))
    parser.add_argument('--once', action='store_true', help='Process at most one derivative and exit')
    args = parser.parse_args(argv)
    while True:
        tick(args.state, executable=args.renderer)
        if args.once:
            return
        time.sleep(IDLE_SECONDS)


if __name__ == '__main__':
    try:
        main()
    except (Error, OSError) as exc:
        print('bio-structure-thumbnails: ' + str(exc), file=sys.stderr)
        sys.exit(2)
