"""Read-only shared-worker observation and durable exact-generation controls."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time

import lifecycle
import session
import worker_controls

PINS = ('session_id', 'invocation_id', 'intent_sha256', 'launch_sha256')
LAST_PROGRESS = None


def startup_snapshot(state, launch=None):
    paths = [Path(state)/'startup-progress.json']
    if launch: paths.append(Path(launch['remote_out'])/'startup-progress.json')
    values = []
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096: continue
            value = json.loads(path.read_bytes())
            if value.get('schema') == 1 and type(value.get('timestamp_ns')) is int:
                values.append(value)
        except (OSError, ValueError, TypeError): pass
    if launch and launch.get('job_file'):
        log = Path(launch['job_file']).parent/'run.log'
        try:
            if log.is_file() and not log.is_symlink():
                with log.open('rb') as stream:
                    stream.seek(max(0, log.stat().st_size-16384)); lines = stream.read(16384).splitlines(keepends=True)
                for line in lines:
                    if not line.startswith(b'BIO_WORKER_STAGE ') or not line.endswith(b'\n') or len(line)>4096: continue
                    try:
                        value=json.loads(line[len(b'BIO_WORKER_STAGE '):])
                        if value.get('schema') == 1 and type(value.get('timestamp_ns')) is int: values.append(value)
                    except (ValueError, TypeError): pass
        except OSError: pass
    return max(values, key=lambda value: value['timestamp_ns']) if values else None


def forward_progress(value):
    global LAST_PROGRESS
    if value is None: return
    raw = b'BIO_WORKER_STAGE '+session.canonical(value)+b'\n'
    if len(raw) > 4096 or raw == LAST_PROGRESS: return
    LAST_PROGRESS = raw
    sys.stderr.write(raw.decode()); sys.stderr.flush()
    path = os.environ.get('BIO_WORKER_PROGRESS_LOG')
    if not path: return
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError('Worker progress log is not a private owned file')
        if info.st_size < 4*1024**2 and os.write(descriptor, raw) != len(raw):
            raise OSError('Partial worker progress append')
    finally: os.close(descriptor)


def target(state, intent, launch):
    return dict(session_id=intent['session_id'], invocation_id=launch['invocation_id'],
                intent_sha256=session.sha(state/'intent.json'), launch_sha256=session.sha(state/'launch.json'))


def ready_binding(state, intent, launch):
    ready_path = Path(launch['remote_out'])/'session-ready.json'
    ready = session.load(ready_path); digest = session.sha(ready_path)
    session.require(ready['session_id'] == intent['session_id'] and ready['owner']['boot_id'] == launch['boot_id']
        and ready['output'] == launch['remote_out'] and ready['state'] == '/tmp/bio-msa-session-'+intent['session_id']
        and ready['sources'] == intent['sources'] == session.sources(Path(intent['tools']))
        and ready['deadline_epoch'] <= launch['provider']['reservation_deadline'], 'Worker readiness binding changed')
    return ready, digest


def worker_status(root, deadline=None):
    import session_client as client
    deadline = deadline or time.monotonic()+15
    root = lifecycle.registry_root(root)
    value = dict(schema=1, state='absent', checked_epoch=time.time(), server_epoch=time.time(),
                 session_id=None, invocation_id=None, intent_sha256=None, launch_sha256=None,
                 shutdown_epoch=None, shutdown_reason=None, hard_deadline_epoch=None, idle_deadline_epoch=None,
                 active_request_id=None, queued_requests=0, controls_version=0, can_extend=False,
                 can_shutdown=False, control_reason='No shared MSA worker is registered', startup_progress=None)
    try:
        if lifecycle.document(root/'active.json', optional=True) is None: return value
        state, intent = client.active(root)
        value.update(state='starting', session_id=intent['session_id'], intent_sha256=session.sha(state/'intent.json'),
                     control_reason='Controls become available after this worker is ready')
        launch = lifecycle.document(state/'launch.json', optional=True)
        value['startup_progress'] = startup_snapshot(state, launch)
        live = client.unit_state(intent['unit'], timeout=lifecycle.timeout(deadline, 15))
        if launch is None:
            session.require(live.get('Description') == 'Managed private MSA session '+intent['session_id'], 'Startup unit identity changed')
            if client._terminal_unit(live): value.update(state='failed', control_reason='Shared worker startup ended')
            return value
        value.update(target(state, intent, launch))
        if live.get('LoadState') != 'not-found':
            session.require(live['InvocationID'] == launch['invocation_id'], 'Shared worker invocation changed')
        value['hard_deadline_epoch'] = launch['provider']['reservation_deadline']
        if client._terminal_unit(live):
            value.update(state='failed', control_reason='Shared worker has stopped; cleanup registration is retained'); return value
        if live.get('ActiveState') == 'deactivating' or (Path(launch['remote_out'])/'session-closed.json').exists():
            value.update(state='closing', control_reason='Shared worker cleanup is in progress'); return value
        session.require(live.get('ActiveState') == 'active', 'Shared worker is not active')
        output = Path(launch['remote_out'])
        starting = lifecycle.document(output/'session-starting.json', optional=True)
        if starting:
            session.require(starting['session_id'] == intent['session_id'] and starting['owner']['boot_id'] == launch['boot_id']
                and starting['deadline_epoch'] <= value['hard_deadline_epoch'], 'Worker startup deadline binding changed')
            value['hard_deadline_epoch'] = starting['deadline_epoch']
        if not (output/'session-ready.json').exists(): value['state']='warming'; return value
        ready, digest = ready_binding(state, intent, launch)
        value.update(state='ready', hard_deadline_epoch=ready['deadline_epoch'],
                     control_reason='This frozen worker generation predates shared controls')
        if ready.get('controls_version') != 1 or ready.get('lifecycle') != 'owned-api': return value
        status = session.load(output/'worker-status.json')
        session.require(status['session_id'] == intent['session_id'] and status['ready_sha256'] == digest
            and status['owner'] == ready['owner'] and -5 <= time.time()-status['checked_epoch'] <= 30,
            'Shared worker heartbeat is stale or belongs to another generation')
        value.update({k:v for k,v in status.items() if k not in {'schema','session_id','ready_sha256','owner'}})
        return value
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        value.update(state='uncertain', can_extend=False, can_shutdown=False,
                     control_reason=str(error) if type(error) is ValueError else 'Cannot verify shared worker: '+type(error).__name__)
        return value


def worker_control(root, action, params, deadline=None):
    import session_client as client
    deadline = deadline or time.monotonic()+15
    root = lifecycle.registry_root(root)
    session.require(set(params) == {'command_id',*PINS}, 'Invalid worker control parameters')
    request = {'schema':1, 'action':action, **params}
    session.require(set(request) == {'schema','action','command_id',*PINS} and action in {'extend','shutdown'}, 'Invalid worker control fields')
    for field in ('command_id','session_id','invocation_id'):
        session.require(isinstance(request[field],str) and re.fullmatch(r'[a-f0-9]{32}',request[field]), 'Invalid '+field)
    for field in ('intent_sha256','launch_sha256'):
        session.require(isinstance(request[field],str) and re.fullmatch(r'[a-f0-9]{64}',request[field]), 'Invalid '+field)
    directory = root/'worker-control-commands'; directory.mkdir(mode=0o700, exist_ok=True)
    journal = directory/(request['command_id']+'.json')
    with lifecycle.registration_lock(root, deadline):
        prior = lifecycle.document(journal, optional=True)
        if prior:
            session.require(prior['request'] == request, 'Worker command ID already has a different payload')
            if 'receipt' in prior: return prior['receipt']
        else: session.atomic(journal, {'request':request}, exclusive=True)
        def finish(receipt):
            session.require(all(receipt.get(key) == request[key] for key in request), 'Worker receipt does not match exact command')
            session.atomic(journal, {'request':request,'receipt':receipt}); return receipt
        def reject(reason):
            return finish({**request,'status':'rejected','applied_seconds':0,'reason':reason,'checked_epoch':time.time()})
        # Find a retained exact receipt before consulting the active pointer. A
        # completed drain can remove the worker before its SSH reply arrives.
        state = root/request['session_id']
        try:
            intent = session.load(state/'intent.json'); launch = session.load(state/'launch.json')
            if target(state,intent,launch) != {key:request[key] for key in PINS}:
                return reject('The requested worker generation no longer matches its retained identity')
        except (OSError, ValueError, KeyError): return reject('The requested worker generation is unavailable')
        output = Path(launch['remote_out'])
        if (output/'worker-controls.json').exists() and (output/'session-ready.json').exists():
            ready, digest = ready_binding(state,intent,launch)
            ledger = worker_controls.checked(ready,digest)
            old = ledger['commands'].get(request['command_id'])
            if old:
                session.require(old['request_sha256'] == hashlib.sha256(session.canonical(request)).hexdigest(), 'Worker command payload changed')
                return finish(old['receipt'])
        observed = worker_status(root,deadline)
        if any(observed.get(key) != request[key] for key in PINS): return reject('The requested worker generation is no longer active')
        enabled = observed['can_extend'] if action == 'extend' else observed['can_shutdown']
        if not enabled: return reject(observed['control_reason'] or 'This shared worker cannot accept the control')
        ready, digest = ready_binding(state,intent,launch)
        command = ['python3','-B',ready['tools']+'/msa/session.py','control','--state',ready['state'],'--expected-ready-sha256',digest]
        result = subprocess.run(client.ssh(launch)+[shlex.join(command)], input=session.canonical(request),
                                capture_output=True, timeout=lifecycle.timeout(deadline,12))
        session.require(result.returncode == 0, 'Shared worker control reply was not confirmed; retry the exact command')
        session.require(len(result.stdout) <= 65536, 'Worker control reply is oversized')
        return finish(json.loads(result.stdout))
