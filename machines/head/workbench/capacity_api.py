"""Shared durable capacity cache, independent of five-second worker telemetry."""
from __future__ import annotations

import fcntl
import math
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time

from .common import atomic, canonical, digest, keys, no_links, parse, require
from .worker_api import configuration

CACHE_SECONDS = 5
ERROR_CACHE_SECONDS = 30
STALE_SECONDS = 120
MAX_BYTES = 1024 * 1024


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value < 10**11


def text(value, limit=1024):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and all(character.isprintable() for character in value))


def normalize(value):
    """Whitelist provider-helper output; no configuration or raw API body escapes."""
    require(isinstance(value, dict) and type(value.get('schema')) is int and value['schema'] == 1,
            'Invalid capacity schema', 'integrity')
    require(value.get('state') in ('ready', 'partial', 'error') and finite(value.get('observed_epoch')),
            'Invalid capacity observation', 'integrity')
    require(value.get('msa_available') is None or type(value['msa_available']) is bool,
            'Invalid MSA availability', 'integrity')
    require(text(value.get('msa_message')), 'Invalid MSA explanation', 'integrity')
    rows = value.get('gpus')
    require(isinstance(rows, list) and len(rows) <= 2048, 'Invalid GPU capacity list', 'integrity')
    result = {key: value[key] for key in ('schema', 'state', 'observed_epoch', 'msa_available', 'msa_message')}
    result['gpus'] = []
    for row in rows:
        require(isinstance(row, dict) and all(text(row.get(key), 200)
                for key in ('instance_type', 'name', 'location', 'reason')),
                'Invalid GPU capacity description', 'integrity')
        require(row.get('contract') in ('regular', 'spot') and type(row.get('msa_eligible')) is bool
                and type(row.get('gpu_count')) is int and 1 <= row['gpu_count'] <= 64
                and finite(row.get('ram_gib')) and finite(row.get('price_hourly')) and row['price_hourly'] > 0
                and (row.get('gpu_memory_gib') is None or finite(row['gpu_memory_gib'])),
                'Invalid GPU capacity metadata', 'integrity')
        result['gpus'].append({key: row[key] for key in ('instance_type', 'name', 'location', 'reason',
            'contract', 'msa_eligible', 'gpu_count', 'ram_gib', 'price_hourly', 'gpu_memory_gib')})
    if 'error' in value:
        require(text(value['error']), 'Invalid capacity error', 'integrity')
        result['error'] = value['error']
    require(result['state'] != 'ready' or result['msa_available'] is not None,
            'Complete capacity evidence has unknown MSA availability', 'integrity')
    require(result['state'] != 'error' or result['msa_available'] is None,
            'Failed capacity evidence claims known MSA availability', 'integrity')
    return result


def invoke(config):
    command = [config.get('capacity_helper', '/run/current-system/sw/bin/bio-msa-capacity'),
               '--tools-root', str(config['tools_dir'])]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        completed = subprocess.run(command, stdout=output, stderr=error, timeout=20, check=False)
        require(completed.returncode == 0, 'Read-only capacity helper failed', 'unavailable')
        require(output.tell() <= MAX_BYTES, 'Capacity response exceeds its limit', 'limit')
        output.seek(0)
        return normalize(parse(output.read(MAX_BYTES + 1)))


def failed(current, *, refreshing=False):
    return {'schema': 1, 'state': 'error', 'observed_epoch': current, 'msa_available': None, 'gpus': [],
            'msa_message': ('Checking Verda MSA availability' if refreshing else
                            'MSA availability is unknown; provider check failed'),
            'error': ('Another capacity check is running' if refreshing else
                      'Verda capacity check is unavailable; refresh to retry')}


def read_cache(path, identity):
    if not path.exists():
        return None
    try:
        no_links(path)
        require(path.stat().st_size <= MAX_BYTES, 'Oversized capacity cache', 'integrity')
        value = parse(path.read_bytes())
        require(isinstance(value, dict) and value.get('identity') == identity
                and finite(value.get('attempt_epoch'))
                and (value.get('checked_epoch') is None or finite(value['checked_epoch'])),
                'Capacity cache no longer matches configuration', 'integrity')
        value['snapshot'] = normalize(value['snapshot'])
        return value
    except (ValueError, TypeError, KeyError, OSError):
        return None


def envelope(record, current, *, refreshing=False):
    result = dict(record['snapshot']) if record else failed(current, refreshing=refreshing)
    checked = record['checked_epoch'] if record else None
    stale = checked is None or not -5 <= current - checked <= STALE_SECONDS
    if stale and result['state'] == 'ready':
        result['msa_available'] = None
        result['msa_message'] = 'MSA availability is unknown; the last complete check is stale'
    result.update(server_epoch=current, checked_epoch=checked, stale=stale,
                  stale_after_seconds=STALE_SECONDS,
                  refresh_after_seconds=(CACHE_SECONDS if result['state'] == 'ready' else ERROR_CACHE_SECONDS),
                  refreshing=refreshing)
    return result


def capacity(api, params, *, bridge=None, clock=None):
    keys(params, optional=('refresh',))
    require(type(params.get('refresh', False)) is bool, 'refresh must be a boolean')
    bridge = invoke if bridge is None else bridge
    clock = time.time if clock is None else clock
    config = configuration(api)
    identity = digest({'schema': 1, 'tools_dir': str(config['tools_dir']),
        'capacity_helper': config.get('capacity_helper', '/run/current-system/sw/bin/bio-msa-capacity')})
    path = no_links(api.store.root / 'worker-capacity.json')
    lock_path = no_links(api.store.root / 'worker-capacity.lock')
    current = clock()
    record = read_cache(path, identity)
    ttl = CACHE_SECONDS if record and record['snapshot']['state'] == 'ready' else ERROR_CACHE_SECONDS
    if (not params.get('refresh', False) and record
            and 0 <= current - record['attempt_epoch'] < ttl):
        return envelope(record, current)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o022,
                'Unsafe capacity lock', 'integrity')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return envelope(read_cache(path, identity), clock(), refreshing=True)
        # Another process may have published after our initial read but before
        # this lock. A fresh result coalesces overlapping manual refreshes too.
        latest = read_cache(path, identity)
        if latest and (record is None or latest['attempt_epoch'] != record['attempt_epoch']):
            return envelope(latest, clock())
        try:
            value = normalize(bridge(config))
            require(-5 <= clock() - value['observed_epoch'] <= 30,
                    'Capacity helper returned an old observation', 'integrity')
        except Exception:
            # Never return a provider or subprocess exception: it may contain
            # credential-bearing stderr or a token in a transport diagnostic.
            value = failed(clock())
        current = clock()
        record = {'identity': identity, 'snapshot': value, 'attempt_epoch': current,
                  'checked_epoch': (value['observed_epoch'] if value['state'] == 'ready' else
                                    latest['checked_epoch'] if latest else None)}
        atomic(path, canonical(record))
        return envelope(record, current)
    finally:
        os.close(fd)
