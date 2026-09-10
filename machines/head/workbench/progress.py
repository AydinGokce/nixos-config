"""Bounded observational startup telemetry; never an execution authority."""
from __future__ import annotations

from copy import deepcopy
import math
import time

from .common import canonical, parse, require

STAGES = {'runtime_package', 'allocating', 'base_setup', 'runtime_download', 'runtime_extract',
          'database_check', 'index_warm', 'ready', 'search', 'gpu_allocation',
          'model_setup', 'inference', 'result_transfer', 'cleanup'}
STALE_AFTER = 30
PREFIX = 'BIO_WORKER_STAGE '
MIRROR_PREFIX = '[worker progress] '


def finite(value, low=0, high=10**12):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def label(value, limit=2048):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(ord(c) < 32 or 127 <= ord(c) < 160 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def eta(value):
    require(isinstance(value, dict), 'Invalid ETA')
    require(set(value) <= {'state', 'seconds', 'lower_seconds', 'upper_seconds', 'basis', 'scope'}, 'Invalid ETA fields')
    require(isinstance(value.get('state'), str) and value['state'] in {'estimate', 'range', 'unknown'}, 'Invalid ETA state')
    require(isinstance(value.get('scope'), str) and value['scope'] in {'stage', 'startup', 'job'}, 'Invalid ETA scope')
    require(label(value.get('basis'), 256), 'Invalid ETA basis')
    if value['state'] == 'estimate':
        require(finite(value.get('seconds'), high=604800), 'Invalid ETA seconds')
    elif value['state'] == 'range':
        require(finite(value.get('lower_seconds'), high=604800)
                and finite(value.get('upper_seconds'), high=604800)
                and value['lower_seconds'] <= value['upper_seconds'], 'Invalid ETA range')
    else:
        require(not {'seconds', 'lower_seconds', 'upper_seconds'} & set(value), 'Unknown ETA has a duration')
    return deepcopy(value)


def event(value):
    require(isinstance(value, dict), 'Invalid progress event')
    require(set(value) <= {'schema', 'stage', 'scope', 'state', 'message', 'timestamp_ns',
                           'stage_id', 'completed', 'total', 'unit', 'eta'}, 'Invalid progress fields')
    require(value.get('schema') == 1 and type(value.get('schema')) is int, 'Invalid progress schema')
    require(isinstance(value.get('stage'), str) and value['stage'] in STAGES, 'Invalid progress stage')
    require(isinstance(value.get('scope'), str) and value['scope'] in {'msa', 'gpu'}, 'Invalid progress scope')
    require(isinstance(value.get('state'), str) and value['state'] in {'running', 'complete', 'failed'}, 'Invalid progress state')
    require(label(value.get('message')), 'Invalid progress message')
    require(type(value.get('timestamp_ns')) is int and 0 < value['timestamp_ns'] < 2**63, 'Invalid progress timestamp')
    if 'stage_id' in value:
        require(label(value['stage_id'], 128), 'Invalid stage identity')
    if {'completed', 'total', 'unit'} & set(value):
        require(type(value.get('completed')) is int and 0 <= value['completed'] <= 2**63 - 1
                and isinstance(value.get('unit'), str) and value['unit'] in {'bytes', 'items', 'steps'}, 'Invalid progress measurements')
        if 'total' in value:
            require(type(value['total']) is int and value['completed'] <= value['total'] <= 2**63 - 1,
                    'Invalid progress total')
    if 'eta' in value:
        eta(value['eta'])
    require(len(canonical(value)) + len(PREFIX) <= 4096, 'Progress event exceeds limit')
    return deepcopy(value)


def events(text):
    result = []
    for line in text.splitlines(keepends=True):
        # A partially written final JSON line is never an observation.
        if not line.endswith('\n') or not line.startswith(PREFIX) or len(line.encode()) > 4097:
            continue
        try:
            result.append(event(parse(line[len(PREFIX):])))
        except (ValueError, TypeError, RecursionError):
            continue
    return result


def unknown(basis='No measured remaining duration is available'):
    return {'state': 'unknown', 'scope': 'stage', 'basis': basis}


def estimated(value, history):
    if value.get('state') != 'running':
        return unknown('This stage has ended')
    if 'eta' in value:
        return eta(value['eta'])
    if not value.get('total') or 'completed' not in value:
        return unknown()
    identity = tuple(value.get(k) for k in ('scope', 'stage', 'stage_id', 'total', 'unit'))
    samples = sorted({v['timestamp_ns']: v for v in history if
        tuple(v.get(k) for k in ('scope', 'stage', 'stage_id', 'total', 'unit')) == identity
        and v.get('state') == 'running' and 'completed' in v
        and 0 <= value['timestamp_ns'] - v['timestamp_ns'] <= 120 * 10**9}.values(), key=lambda v: v['timestamp_ns'])
    rates = []
    for before, after in zip(samples, samples[1:]):
        seconds = (after['timestamp_ns'] - before['timestamp_ns']) / 10**9
        delta = after['completed'] - before['completed']
        if delta <= 0:
            rates.clear()
        elif seconds >= 2 and delta > 0:
            rates.append(delta / seconds)
    remaining = value['total'] - value['completed']
    if not rates or remaining / min(rates) > 604800:
        return unknown('Waiting for measured throughput')
    basis = 'Observed throughput over recent progress samples; this stage only'
    if len(rates) == 1 or min(rates) == max(rates):
        return {'state': 'estimate', 'scope': 'stage', 'seconds': round(remaining / rates[-1], 1), 'basis': basis}
    return {'state': 'range', 'scope': 'stage', 'lower_seconds': round(remaining / max(rates), 1),
            'upper_seconds': round(remaining / min(rates), 1), 'basis': basis}


def view(value, history=(), epoch=None):
    value = event(value)
    result = {k: deepcopy(v) for k, v in value.items() if k not in {'schema', 'state', 'eta'}}
    result['stage_state'] = value['state']
    result['eta'] = estimated(value, history)
    return freshness(result, epoch)


def freshness(progress, epoch=None):
    result = deepcopy(progress)
    timestamp = result.get('timestamp_ns')
    if type(timestamp) is not int:
        result.setdefault('eta', unknown())
        return result
    epoch = time.time() if epoch is None else epoch
    age = epoch - timestamp / 10**9
    result.update(age_seconds=round(max(0, age), 1), stale_after_seconds=STALE_AFTER,
                  stale=age > STALE_AFTER or age < -5, server_epoch=epoch)
    if result['stale'] and result.get('eta', {}).get('state') in {'estimate', 'range'}:
        result['eta'] = {**result['eta'], 'state': 'stale', 'basis': 'Progress observation is stale; remaining time is unknown'}
        for key in ('seconds', 'lower_seconds', 'upper_seconds'):
            result['eta'].pop(key, None)
    elif result.get('eta', {}).get('state') in {'estimate', 'range'}:
        estimate = result['eta']
        reference = estimate.get('as_of_epoch', timestamp / 10**9)
        elapsed = max(0, epoch - reference) if finite(reference, high=10**11) else 0
        for key in ('seconds', 'lower_seconds', 'upper_seconds'):
            if key in estimate:
                estimate[key] = round(max(0, estimate[key] - elapsed), 1)
        estimate['as_of_epoch'] = epoch
    return result
