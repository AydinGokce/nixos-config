#!/usr/bin/env python3
"""Explicit full-SSD route for mapped Verda MSA workers.

This adapter only leases an already published cache. Allocation, accounting,
device identity, read-only mounting and content checks remain owned by the
existing cache helpers. No cache creation, copying, formatting or NFS fallback.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import selectors
import signal
import subprocess
import sys
import time
import uuid

import block_cache as content
import block_cache_device as device
import search_profile

KIND = 'verda-msa-block-cache-route'
FIELDS = ('cache_id', 'volume_id', 'filesystem_uuid', 'cache_generation',
          'source_manifest_sha256', 'source_receipt_sha256', 'ready_receipt_sha256')
ENVIRONMENT = 'BIO_MSA_CACHE_ROUTE'


def require(value, message):
    if not value:
        raise ValueError(message)


def raw(value):
    return content.canonical(value) + b'\n'


def digest(value):
    return hashlib.sha256(raw(value)).hexdigest()


def load(path):
    value = content.load(Path(path), 4 * 1024**2)
    require(isinstance(value, dict), 'Cache route receipt is not an object')
    return value


def save(path, value):
    import session
    session.atomic(Path(path), value, exclusive=True)


def validate(route):
    require(isinstance(route, dict) and set(route) == {
        'schema', 'kind', 'lease_id', 'binding', 'ready_receipt'}, 'Invalid mapped cache route')
    require(type(route['schema']) is int and route['schema'] == 1 and route['kind'] == KIND
            and isinstance(route['lease_id'], str) and re.fullmatch('[a-f0-9]{32}', route['lease_id']),
            'Invalid mapped cache route identity')
    binding = content.binding(route['binding'])
    require(binding == route['binding'] and binding['ready_receipt_sha256'],
            'Mapped cache route has no published full ready receipt')
    ready = route['ready_receipt']
    require(isinstance(ready, dict) and digest(ready) == binding['ready_receipt_sha256'],
            'Published ready receipt bytes cannot be reconstructed exactly')
    require(ready.get('kind') == content.KIND and ready.get('status') == 'ready'
            and ready.get('rootrel') == 'colabfold' and ready.get('filesystem_type') == 'ext4'
            and type(ready.get('size_bytes')) is int and ready['size_bytes'] == content.SIZE_BYTES
            and all(ready.get(k) == v for k, v in content.identity(binding).items()),
            'Published ready receipt belongs to another cache')
    completed = ready.get('completion', {})
    require(completed.get('full_readback') is True and type(completed.get('files')) is int
            and completed['files'] > 0 and type(completed.get('payload_bytes')) is int
            and completed['payload_bytes'] > 0
            and completed.get('source_bytes_hashed') == completed.get('destination_bytes_readback')
            == completed['payload_bytes'], 'Full cache copy/readback is not proven')
    return route


def identity(route):
    route = validate(route)
    return dict(schema=1, kind=KIND, lease_id=route['lease_id'], route_sha256=digest(route),
                **{k: route['binding'][k] for k in FIELDS})


def controller(tools=None, state_root=None):
    import block_cache_control
    # Installed /etc/bio-tools/msa is a separate Nix symlink: its resolved
    # parent is not the full tools tree containing dc-budget.py and rfaa/.
    tools = Path(tools or os.environ.get('BIO_TOOLS_SRC', '/etc/bio-tools'))
    budget = runpy.run_path(str(tools/'dc-budget.py'))
    storage = runpy.run_path(str(tools/'rfaa/storage.py'))
    return block_cache_control.Controller(budget['API'](),
        state_root or os.environ.get('DC_STATE_DIR', '/var/lib/dc'), budget, storage)


def invoke_head(tools, action, *arguments, timeout=90):
    """Credentials remain in a head subprocess, never in a worker capsule."""
    command = [sys.executable, '-B', str(Path(tools)/'msa/session_cache.py'), action, *map(str, arguments)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                            env=dict(os.environ, BIO_TOOLS_SRC=str(Path(tools).absolute())))
    require(result.returncode == 0, 'Full SSD cache is unavailable or its lease needs inspection: '
            + result.stderr.strip()[-500:])
    return json.loads(result.stdout)


def same(route, envelope):
    require(all(envelope.get(k) == route['binding'][k] for k in FIELDS),
            'Published cache or source generation changed')


def select(tools=None, *, control=None):
    control = control or controller(tools)
    envelope = control.execute('status')
    require(envelope.get('status') == 'allocated' and envelope.get('ready_receipt_sha256'),
            'Mapped MSA requires the published full SSD cache; prepare it before submitting')
    lease = envelope.get('lease')
    require(not lease or lease.get('status') == 'released', 'The full SSD cache is already leased')
    retained = control.read()
    require(all(retained.get(k) == envelope.get(k) for k in FIELDS), 'Cache changed during selection')
    return validate(dict(schema=1, kind=KIND, lease_id=uuid.uuid4().hex,
        binding=content.binding(dict(schema=1, **{k: envelope[k] for k in FIELDS})),
        ready_receipt=retained.get('ready_receipt')))


def selected_session(state):
    state = Path(state).absolute()
    intent = load(state/'intent.json')
    require(intent.get('session_id') == state.name and intent.get('provider_name', 'verda') == 'verda'
            and intent.get('search_profile', {}).get('profile_id') in search_profile.MAPPED_PROFILES,
            'Cache route requires the exact mapped Verda session')
    return validate(intent.get('database_cache'))


def acquire(route_path, *, control=None):
    route_path = Path(route_path); route = validate(load(route_path))
    control = control or controller()
    save(route_path.parent/'cache-acquire-intent.json', identity(route))
    envelope = control.acquire(route['lease_id'], 'serve')
    same(route, envelope)
    require(envelope['lease']['mode'] == 'serve' and envelope['lease']['status'] == 'preparing',
            'Cache lease is not a fresh serve lease')
    save(route_path.parent/'cache-acquired.json', envelope)
    return envelope


def launching(route_path, *, control=None):
    route_path = Path(route_path); route = validate(load(route_path))
    control = control or controller()
    envelope = control.launching(route['lease_id']); same(route, envelope)
    save(route_path.parent/'cache-launching.json', envelope)
    return envelope


def release(route_path, *, control=None):
    route_path = Path(route_path); route = validate(load(route_path))
    control = control or controller()
    historical = control.lease_store(route['lease_id']).read()
    if historical is None:
        require(not (route_path.parent/'cache-launching.json').exists(),
                'Cache launch may have started without its retained lease')
        return dict(status='not-acquired', **identity(route))
    require(historical.get('lease_id') == route['lease_id'] and historical.get('mode') == 'serve'
            and all(historical.get(k) == route['binding'][k] for k in FIELDS if k != 'ready_receipt_sha256'),
            'Retained cache lease belongs to another route')
    if historical['status'] != 'released':
        envelope = control.release(route['lease_id']); same(route, envelope)
        historical = envelope['lease']
    require(historical.get('status') == 'released' and historical.get('release_checks') == dict(
        exact_workers_absent=True, exact_os_disks_absent=True, volume_detached=True, managed_jobs_closed=True),
        'Cache lease cleanup is not proven')
    result = dict(status='released', **identity(route), lease=historical)
    path = route_path.parent/'cache-released.json'
    if path.exists():
        require(load(path) == result, 'Retained cache release receipt changed')
    else:
        save(path, result)
    return result


def head_binding(route, job, boot_id, *, control=None):
    validate(route)
    require(isinstance(boot_id, str) and str(uuid.UUID(boot_id)) == boot_id, 'Invalid worker boot identity')
    require(job.get('database_cache') == identity(route), 'Worker job belongs to another cache route')
    control = control or controller()
    envelope = control.bind(route['lease_id'], job['instance'], boot_id)
    same(route, envelope)
    managed = envelope['provider']['managed']
    require(managed.get('instance_id') == job['instance'] and managed.get('boot_id') == boot_id,
            'Cache attachment belongs to another managed worker')
    workers = [row for row in envelope['provider']['instances'] if row.get('id') == job['instance']]
    require(len(workers) == 1 and workers[0].get('ip') == job.get('ip'),
            'Cache attachment address differs from the authenticated worker')
    return envelope


def marker(route):
    return 'BIO_MSA_CACHE_BIND_' + validate(route)['lease_id']


def stop_child(child):
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL); child.wait()


def run_worker(route_path, job_path, handoff, script, command, *, bind=None, output=None):
    """Forward SSH output and answer the one fresh boot handshake after apt."""
    route_path = Path(route_path); route = validate(load(route_path)); job = load(job_path)
    require(job.get('database_cache') == identity(route), 'Worker command cache binding changed')
    handoff = Path(handoff)
    require(not os.path.lexists(handoff) and handoff.parent.is_dir(), 'Cache handoff is not fresh')
    bind = bind or head_binding
    output = output or sys.stdout.buffer
    child = None; previous = {}; buffer = b''; answered = False
    def interrupted(sig, frame):
        raise InterruptedError('Cache worker transport interrupted')
    try:
        previous = {s: signal.signal(s, interrupted) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        with Path(script).open('rb') as source:
            child = subprocess.Popen(command, stdin=source, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                events = selector.select(.5)
                if not events:
                    if child.poll() is not None: break
                    continue
                block = os.read(child.stdout.fileno(), 65536)
                if not block: break
                output.write(block); output.flush(); buffer += block
                require(len(buffer) <= 1024**2, 'Worker output line is oversized')
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    prefix = (marker(route) + ' ').encode()
                    if not line.startswith(prefix): continue
                    require(not answered, 'Worker requested duplicate cache attachment binding')
                    boot = line[len(prefix):].decode('ascii')
                    envelope = bind(route, job, boot)
                    save(route_path.parent/'cache-bound.json', envelope)
                    save(handoff, envelope); answered = True
        status = child.wait()
        require(status != 0 or answered, 'Worker completed without mounting its bound full cache')
        return status
    finally:
        if child is not None:
            stop_child(child)
            if child.stdout: child.stdout.close()
        for sig, handler in previous.items(): signal.signal(sig, handler)


def mount_worker(route_path, handoff, output, *, wait_seconds=100):
    route = validate(load(route_path)); ready = raw(route['ready_receipt'])
    output, handoff = Path(output), Path(handoff)
    require(not output.exists() and not handoff.exists(), 'Cache mount evidence is not fresh')
    output.mkdir(mode=0o700, parents=True)
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    print(marker(route)+' '+boot, flush=True)
    until = time.monotonic() + wait_seconds
    while not handoff.is_file():
        require(time.monotonic() < until, 'Head did not deliver fresh cache attachment evidence')
        time.sleep(.25)
    owner = load(handoff); same(route, owner)
    require(owner['lease'].get('lease_id') == route['lease_id'] and owner['lease'].get('mode') == 'serve'
            and owner['lease'].get('status') == 'bound'
            and owner['provider']['managed'].get('boot_id') == boot,
            'Cache handoff belongs to another worker or lease')
    binding = device.provider_binding(owner, owner['provider'], time.time())
    observed = device.inspect(binding['device'])
    plan = device.mount_plan(owner, owner['provider'], observed, ready, route['binding']['ready_receipt_sha256'])
    target = Path(device.MOUNTPOINT); target.mkdir(parents=True, exist_ok=True)
    subprocess.run(plan['command'], check=True, timeout=60)
    observed = device.inspect(binding['device'])
    mounted = device.validate_mount(owner, owner['provider'], observed, ready, route['binding']['ready_receipt_sha256'])
    verified = content.verify(target, route['binding'], binding['device'])
    require((target/'ready.json').read_bytes() == ready, 'Mounted cache ready receipt changed')
    save(output/'route.json', route)
    save(output/'ownership.json', owner)
    save(output/'device.json', observed)
    save(output/'mount.json', mounted)
    save(output/'content.json', verified)
    receipt = dict(**identity(route), boot_id=boot, device=binding['device'], root=str(target/'colabfold'),
        mount_sha256=digest(mounted), content_sha256=digest(verified))
    save(output/'verified.json', receipt)
    return receipt


def worker_receipt(path, database):
    """Cheap live mount/receipt check; full inventory is checked once at mount."""
    path = Path(path); receipt = load(path); route = validate(load(path.parent/'route.json'))
    require(all(receipt.get(k) == v for k, v in identity(route).items())
            and receipt.get('boot_id') == Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            and Path(database).resolve() == Path(device.MOUNTPOINT)/'colabfold' == Path(receipt['root']),
            'Live cache receipt belongs to another source, boot, or database path')
    mounted = load(path.parent/'mount.json'); verified = load(path.parent/'content.json')
    require(digest(mounted) == receipt['mount_sha256'] and digest(verified) == receipt['content_sha256'],
            'Cache mount/content verification receipt changed')
    content.cache_mount(Path(device.MOUNTPOINT), route['binding'], receipt['device'], readonly=True)
    require((Path(device.MOUNTPOINT)/'ready.json').read_bytes() == raw(route['ready_receipt']),
            'Live cache ready receipt changed')
    return dict(**receipt, receipt=str(path), receipt_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def check_head(route, observed, *, boot_id=None):
    require(isinstance(observed, dict) and all(observed.get(k) == v for k, v in identity(route).items()),
            'MSA readiness does not use its frozen full SSD cache')
    require(boot_id is None or observed.get('boot_id') == boot_id, 'MSA cache was mounted on another boot')
    return observed


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('select', 'acquire', 'launching', 'release', 'job', 'run', 'mount'))
    p.add_argument('--session-state', type=Path); p.add_argument('--route', type=Path)
    p.add_argument('--job', type=Path); p.add_argument('--handoff', type=Path)
    p.add_argument('--script', type=Path); p.add_argument('--output', type=Path)
    p.add_argument('--command', nargs=argparse.REMAINDER, default=[])
    args = p.parse_args()
    if args.action not in ('mount', 'job') and not os.environ.get('BIO_MSA_CACHE_AUTHENTICATED'):
        script = ('set -e; source "${DC_CREDENTIALS_FILE:-${DATACRUNCH_ENV_FILE:-/root/.config/datacrunch/credentials.env}}"; '
                  'export DATACRUNCH_CLIENT_ID DATACRUNCH_CLIENT_SECRET BIO_MSA_CACHE_AUTHENTICATED=1; exec "$@"')
        os.execvp('bash', ['bash', '-c', script, 'msa-cache-auth', sys.executable, '-B',
                          str(Path(__file__).resolve()), *sys.argv[1:]])
    if args.action == 'select':
        result = selected_session(args.session_state) if args.session_state else select()
    elif args.action == 'run':
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        require(command, 'Worker command is missing')
        return run_worker(args.route, args.job, args.handoff, args.script, command)
    elif args.action == 'mount':
        result = mount_worker(args.route, args.handoff, args.output)
    elif args.action == 'job':
        route = validate(load(args.route)); job = load(args.job)
        require(job.get('model') == 'msa' and job.get('database_volume') == route['binding']['volume_id'],
                'Worker does not carry the exact cache volume')
        job['database_cache'] = identity(route)
        content.atomic_json(args.job, job); result = job['database_cache']
    else:
        result = globals()[args.action](args.route)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print('msa-cache: '+str(exc), file=sys.stderr)
        raise SystemExit(2)
