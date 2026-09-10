"""Durable idle leases and graceful drain controls for an owned MSA generation."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

RESERVE = 60
EXTENSION = 900


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def atomic(path, value):
    path = Path(path); temporary = path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    with temporary.open('x') as stream:
        stream.write(canonical(value).decode()+'\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def load(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4*1024**2:
        raise ValueError('Unsafe worker-control document')
    return json.loads(path.read_bytes())


@contextmanager
def locked(state):
    fd = os.open(Path(state)/'control.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        until = time.monotonic()+5
        while True:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.monotonic() >= until: raise ValueError('Worker control lock is busy; retry exact command')
                time.sleep(.02)
        yield
    finally: os.close(fd)


def path(ready):
    return Path(ready['output'])/'worker-controls.json'


def initialize(ready, digest):
    value = {'schema': 1, 'session_id': ready['session_id'], 'ready_sha256': digest,
             'owner': ready['owner'], 'hard_deadline_epoch': ready['deadline_epoch'],
             'idle_seconds': ready['idle_seconds'], 'idle_deadline_epoch': ready['created_epoch']+ready['idle_seconds'],
             'keep_until_epoch': 0, 'idle_credit_seconds': 0, 'busy': False,
             'queued_requests': 0, 'active_request_id': None, 'shutdown_requested': False,
             'closing': False, 'revision': 0, 'commands': {}}
    with locked(ready['state']):
        if path(ready).exists(): raise ValueError('Worker control generation already exists')
        atomic(path(ready), value)
    return value


def checked(ready, digest):
    value = load(path(ready))
    if (value['session_id'] != ready['session_id'] or value['ready_sha256'] != digest
            or value['owner'] != ready['owner'] or value['hard_deadline_epoch'] != ready['deadline_epoch']):
        raise ValueError('Worker control generation changed')
    return value


def snapshot(value, now=None):
    now = time.time() if now is None else now
    hard = value['hard_deadline_epoch']-RESERVE
    draining = value['shutdown_requested']
    occupied = value['busy'] or value['queued_requests'] > 0
    idle = min(hard, value['idle_deadline_epoch']) if value['idle_deadline_epoch'] is not None else None
    expired = now >= hard or (not occupied and idle is not None and now >= idle)
    if draining:
        state = 'closing'; shutdown = None if occupied else now; reason = 'draining accepted searches' if occupied else 'graceful shutdown'
    elif occupied:
        state = 'busy'; shutdown = hard; reason = 'maximum lifetime; idle countdown begins after accepted searches'
    else:
        state = 'idle'; shutdown = idle; reason = 'maximum lifetime' if idle == hard else 'idle timeout'
    base = now+value['idle_seconds']+value['idle_credit_seconds'] if occupied else max(now, idle or now)
    extend = not draining and not value['closing'] and not expired and base+EXTENSION <= hard
    return {'state': state, 'checked_epoch': now, 'hard_deadline_epoch': value['hard_deadline_epoch'],
            'shutdown_epoch': shutdown, 'shutdown_reason': reason, 'idle_deadline_epoch': idle,
            'idle_credit_seconds': value['idle_credit_seconds'], 'active_request_id': value['active_request_id'],
            'queued_requests': value['queued_requests'], 'shutdown_requested': draining,
            'control_revision': value['revision'], 'controls_version': 1,
            'can_extend': extend, 'can_shutdown': not draining and not value['closing'] and not expired,
            'control_reason': 'shutdown already requested' if draining else 'worker lease has expired' if expired else '' if extend else 'less than 15 minutes remain within the original lifetime'}


def update_locked(ready, digest, *, busy, queued, active, now=None):
    now = time.time() if now is None else now
    value = checked(ready, digest); before = canonical(value)
    occupied = busy or queued > 0
    was_occupied = value['busy'] or value['queued_requests'] > 0
    if occupied:
        value['idle_deadline_epoch'] = None
    elif was_occupied:
        value['idle_deadline_epoch'] = min(value['hard_deadline_epoch']-RESERVE,
            max(now+value['idle_seconds']+value['idle_credit_seconds'], value['keep_until_epoch']))
        value['idle_credit_seconds'] = 0
    value.update(busy=busy, queued_requests=queued, active_request_id=active)
    should_close = value['shutdown_requested'] and not occupied
    if not occupied and value['idle_deadline_epoch'] is not None and now >= value['idle_deadline_epoch']:
        should_close = True
    if should_close: value['closing'] = True
    if canonical(value) != before: atomic(path(ready), value)
    return value, should_close


def apply(ready, digest, request, now=None):
    now = time.time() if now is None else now
    expected = {'schema', 'command_id', 'action', 'session_id', 'invocation_id', 'intent_sha256', 'launch_sha256'}
    if (set(request) != expected or request['schema'] != 1 or request['action'] not in {'extend','shutdown'}
            or not re.fullmatch(r'[a-f0-9]{32}', request['command_id'] or '')
            or request['session_id'] != ready['session_id']): raise ValueError('Invalid worker control request')
    digest_request = hashlib.sha256(canonical(request)).hexdigest()
    with locked(ready['state']):
        value = checked(ready, digest)
        old = value['commands'].get(request['command_id'])
        if old:
            if old['request_sha256'] != digest_request: raise ValueError('Control command ID already has different payload')
            return old['receipt']
        view = snapshot(value, now); accepted = False; delta = 0
        reason = ''
        if ready.get('lifecycle') != 'owned-api': reason = 'Borrowed sessions do not permit shared worker controls'
        elif len(value['commands']) >= 256: raise ValueError('Worker control command limit reached')
        elif value['closing'] or value['shutdown_requested'] or not view['can_shutdown']: reason = view['control_reason'] or 'Shared worker is already closing'
        elif request['action'] == 'extend':
            if not view['can_extend']: reason = view['control_reason']
            else:
                delta = EXTENSION; accepted = True
                if value['busy'] or value['queued_requests']:
                    value['idle_credit_seconds'] += delta
                    reason = 'Added 15 minutes to the next idle period within the original lifetime'
                else:
                    value['idle_deadline_epoch'] = max(now, value['idle_deadline_epoch'])+delta
                    value['keep_until_epoch'] = value['idle_deadline_epoch']
                    reason = 'Keep-warm extended by 15 minutes within the original lifetime'
        else:
            value['shutdown_requested'] = True; accepted = True
            reason = 'Accepted searches will drain before shutdown' if value['busy'] or value['queued_requests'] else 'Idle worker will shut down now'
        value['revision'] += 1
        result = {**request, 'status': 'applied' if accepted else 'rejected', 'applied_seconds': delta,
                  'reason': reason, **snapshot(value, now)}
        value['commands'][request['command_id']] = {'request_sha256': digest_request, 'receipt': result}
        atomic(path(ready), value)
        return result
