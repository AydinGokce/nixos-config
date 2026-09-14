"""Shared MSA observations and durable, exactly targeted keep-warm/drain intent.

API calls never allocate workers. The dispatcher retries the same control ID;
the session control journal is the side-effect authority across lost responses.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from . import progress
from .common import (Error, canonical, identifier, keys, now, parse, require, sha,
                     string, uid)

PINS = ('session_id', 'invocation_id', 'intent_sha256', 'launch_sha256')
STATES = {'absent', 'starting', 'warming', 'ready', 'busy', 'idle', 'closing', 'failed', 'uncertain'}


def target(value):
    keys(value, PINS)
    identifier(value['session_id']); identifier(value['invocation_id'])
    sha(value['intent_sha256']); sha(value['launch_sha256'])
    return dict(value)


def invoke(config, action, params=None):
    """A bounded CLI bridge isolates the MSA module's imports and trusted root."""
    command = [sys.executable, '-B', str(Path(config['tools_dir']) / 'msa/session_client.py'),
               'worker-status' if action == 'status' else 'worker-control',
               '--root', str(config.get('msa_sessions_root', '/var/lib/dc/msa-sessions'))]
    if params is not None:
        command += ['--command-id', params['command_id'], '--action', action]
        for name in PINS:
            command += ['--' + name.replace('_', '-'), params[name]]
    # Trusted helper stdout is still size-limited before it can enter RPC.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        result = subprocess.run(command, stdout=output, stderr=error, timeout=20, check=False)
        require(result.returncode == 0, 'Worker observation/control is unavailable; exact intent is retained', 'unavailable')
        require(output.tell() <= 65536, 'Worker response exceeds its limit', 'limit')
        output.seek(0)
        value = parse(output.read(65537))
        require(isinstance(value, dict), 'Invalid worker response', 'integrity')
        return value


def configuration(api):
    if api.worker_config is not None:
        return api.worker_config
    from .service import configuration as configured
    return configured(os.environ.get('BIO_WORKBENCH_CONFIG'))


def epoch(value):
    return value if progress.finite(value, high=10**11) else None


def text(value, fallback, limit=2048):
    return value if progress.label(value, limit) else fallback


def normalize_status(value, current=None):
    current = time.time() if current is None else current
    require(type(value.get('schema')) is int and value['schema'] == 1
            and isinstance(value.get('state'), str) and value['state'] in STATES, 'Invalid worker status', 'integrity')
    try:
        binding = target({key: value.get(key) for key in PINS})
    except ValueError:
        binding = None
    checked = epoch(value.get('checked_epoch'))
    stale = checked is None or not -5 <= current - checked <= progress.STALE_AFTER
    reason = text(value.get('control_reason'), 'This worker does not expose verified controls')
    if stale:
        reason = 'Worker observation is stale; refresh before changing it'
    if binding is None:
        reason = 'Exact worker generation is not available'
    controls = {}
    for action in ('extend', 'shutdown'):
        enabled = value.get('can_' + action) is True and not stale and binding is not None
        controls[action] = {'enabled': enabled,
                            'reason': text(value.get(action + '_reason'), reason) if not enabled else ''}
    controls['extend']['seconds'] = 900
    controls['shutdown']['mode'] = 'drain'
    result = {'schema': 1, 'state': value['state'], 'shared': True, 'target': binding,
              'server_epoch': current, 'checked_epoch': checked, 'stale': stale,
              'stale_after_seconds': progress.STALE_AFTER,
              'message': text(value.get('message'), value['state'].replace('_', ' ').capitalize()),
              'controls': controls, 'shutdown_reason': text(value.get('shutdown_reason'), 'Unknown'),
              'active_request_id': value.get('active_request_id') if progress.label(value.get('active_request_id'), 128) else None,
              'queued_requests': value.get('queued_requests') if type(value.get('queued_requests')) is int and 0 <= value['queued_requests'] <= 100000 else None}
    if value.get('provider_name') in ('aws', 'verda'):
        result['provider_name'] = value['provider_name']
        result['compute_kind'] = 'cpu'
    for key in ('shutdown_epoch', 'hard_deadline_epoch', 'idle_deadline_epoch'):
        result[key] = epoch(value.get(key))
    if type(value.get('idle_credit_seconds')) is int and 0 <= value['idle_credit_seconds'] <= 86400:
        result['idle_credit_seconds'] = value['idle_credit_seconds']
    startup = value.get('progress', value.get('startup_progress'))
    if isinstance(startup, dict):
        try:
            history = []
            raw_history = value.get('startup_history', [])
            if isinstance(raw_history, list):
                for sample in raw_history[-64:]:
                    try:
                        history.append(progress.event(sample))
                    except (ValueError, TypeError, RecursionError):
                        continue
            result['progress'] = progress.view(startup, history, epoch=current)
        except (ValueError, TypeError, RecursionError):
            pass
    return result


def status(api, params):
    keys(params)
    return normalize_status(invoke(configuration(api), 'status'))


def envelope(record):
    return {key: value for key, value in record.items() if not key.startswith('_') and key != 'worker_control_id'}


def submit(api, params, action):
    keys(params, ('request_key', 'target'))
    string(params['request_key'], 'request_key', 200)
    binding = target(params['target'])
    method = 'worker.' + action
    document = {'request_key': params['request_key'], 'target': binding}
    with api.store.transaction() as db:
        old = api.store.idem(db, api.actor, method, params['request_key'], document)
        if old:
            return envelope(api.store.get(db, 'worker_control', old, api.actor))
        count = db.execute("SELECT COUNT(*) FROM objects WHERE kind='worker_control' AND state='pending'").fetchone()[0]
        require(count < 64, 'Too many worker controls await reconciliation', 'limit')
        ident = uid()
        record = {'worker_control_id': ident, 'control_id': ident, 'request_key': params['request_key'],
                  'action': action, 'target': binding, 'state': 'pending', 'result': None, 'error': None,
                  '_attempts': 0, '_retry_epoch': 0}
        api.store.put(db, 'worker_control', record, api.actor)
        api.store.idem(db, api.actor, method, params['request_key'], document, ident)
        api.store.event(db, ident, 'worker_control_requested', {'action': action, 'target': binding})
        return envelope(record)


def get(api, params):
    keys(params, ('control_id',))
    return envelope(api.store.read('worker_control', params['control_id'], api.actor))


def receipt(value, record):
    require(type(value.get('schema')) is int and value['schema'] == 1
            and value.get('command_id') == record['control_id'] and value.get('action') == record['action']
            and all(value.get(key) == record['target'][key] for key in PINS)
            and isinstance(value.get('status'), str) and value['status'] in {'applied', 'rejected'}, 'Worker receipt binding differs', 'integrity')
    seconds = value.get('applied_seconds', 0)
    require(type(seconds) is int and 0 <= seconds <= 900, 'Invalid applied keep-warm interval', 'integrity')
    require(record['action'] == 'extend' or seconds == 0, 'Shutdown receipt changed the keep-warm lease', 'integrity')
    result = {key: value[key] for key in ('schema', 'command_id', 'action', *PINS, 'status')}
    result.update(applied_seconds=seconds, reason=text(value.get('reason'), value['status']),
                  shutdown_requested=value.get('shutdown_requested') is True)
    for key in ('checked_epoch', 'hard_deadline_epoch', 'idle_deadline_epoch', 'shutdown_epoch'):
        result[key] = epoch(value.get(key))
    revision = value.get('control_revision')
    if type(revision) is int and 0 <= revision < 2**63:
        result['control_revision'] = revision
    return result


def reconcile(store, config, *, bridge=invoke, current=None):
    """At most one bounded attempt per dispatcher tick, no new command IDs."""
    current = time.time() if current is None else current
    candidates = [item for item in store.listing('worker_control', states=['pending'])
                  if item.get('_retry_epoch', 0) <= current]
    if not candidates:
        return
    ident = candidates[0]['control_id']
    folder = store.directory('operations', ident)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(folder / 'worker-control.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        record = store.read('worker_control', ident)
        if record['state'] != 'pending':
            return
        try:
            result = receipt(bridge(config, record['action'], {'command_id': ident, **record['target']}), record)
            error = None
        except (ValueError, OSError, subprocess.SubprocessError):
            # A failed transport can follow an applied command. Never turn it
            # into a new +15 command, or call an unbound shutdown as fallback.
            result = None
            error = {'code': 'uncertain', 'message': 'Waiting to reconcile the exact worker command; it will not be applied twice'}
        with store.transaction() as db:
            retained = store.get(db, 'worker_control', ident)
            if retained['state'] != 'pending':
                return
            retained['_attempts'] += 1
            retained['_retry_epoch'] = current + min(60, 5 * 2 ** min(4, retained['_attempts'] - 1))
            retained.update(result=result, error=error)
            if result is not None:
                retained.update(state='complete', finished_at=now())
                store.event(db, ident, 'worker_control_completed', result)
            store.put(db, 'worker_control', retained)
    finally:
        os.close(fd)
