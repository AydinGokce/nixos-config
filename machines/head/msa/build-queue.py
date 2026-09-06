#!/usr/bin/env python3
"""One-shot head ticks for guarded, resumable private MSA database builds."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
STATES = {'waiting_downloads', 'waiting_manual', 'waiting_capacity', 'dispatching',
          'running', 'waiting_cleanup', 'uncertain', 'blocked', 'exhausted', 'ready'}
JOB_STATES = {'pending', 'starting', 'running', 'uncertain', 'cleanup', 'closed'}
WORK_SECONDS = 21600
MAX_ATTEMPTS = 3
MAX_PRICE = 13


class Error(RuntimeError):
    pass


class Unavailable(Error):
    pass


def require(value, message):
    if not value:
        raise Error(message)


def loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    def constant(value):
        raise Error('Nonfinite JSON value')
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def read(path):
    require(not path.is_symlink(), 'Metadata must not be a symlink: ' + str(path))
    return loads(path.read_text())


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def tools_pin(path, *, store_root=Path('/nix/store'), owner=0):
    """Verify an explicitly retained immutable submission/source snapshot."""
    path = Path(path)
    require(path.is_absolute(), 'Tool pin must be an absolute path')
    data = read(path)
    require(path.stat().st_uid == owner and not path.stat().st_mode & 0o022, 'Unsafe tool pin ownership/mode')
    require(data.get('version') == 1, 'Unsupported tool pin version')
    root = Path(data['tools_src'])
    immutable = Path(data['immutable_store'])
    require(root.resolve() == immutable and immutable.is_relative_to(store_root.resolve()), 'Tool pin must resolve to retained Nix storage')
    require(immutable.is_dir() and not immutable.stat().st_mode & 0o222, 'Pinned source root is not immutable')
    manifest_path = Path(data['source_manifest'])
    require(sha(manifest_path) == data['source_manifest_sha256'], 'Pinned source manifest changed')
    manifest = read(manifest_path)
    require(manifest.get('version') == 1 and manifest.get('content_sha256') == data['content_sha256'], 'Pinned content identity changed')
    actual = {}
    for item in sorted(immutable.rglob('*')):
        require(not item.is_symlink() and not item.stat().st_mode & 0o222, 'Pinned source must be immutable ordinary files/directories')
        if item.is_dir():
            continue
        require(item.is_file(), 'Unsupported pinned source file type')
        actual[item.relative_to(immutable).as_posix()] = dict(sha256=sha(item), bytes=item.stat().st_size)
    require(actual == manifest['files'], 'Pinned source bytes or file inventory changed')
    digest = hashlib.sha256(json.dumps(actual, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    require(digest == data['content_sha256'], 'Pinned source content digest mismatch')
    submit = Path(data['bio_submit'])
    require(submit.is_file() and submit.resolve().is_relative_to(store_root.resolve())
            and not submit.stat().st_mode & 0o222 and os.access(submit, os.X_OK), 'Pinned bio-submit is not an immutable executable')
    require(sha(submit) == data['bio_submit_sha256'], 'Pinned bio-submit executable changed')
    require(Path(data['cluster_config']) == root / 'cluster.sh', 'Pinned cluster configuration differs from source tree')
    return dict(path=str(path), sha256=sha(path), content_sha256=digest,
                tools_src=str(root), bio_submit=str(submit), bio_submit_sha256=data['bio_submit_sha256'],
                cluster_config=data['cluster_config'], source_manifest_sha256=data['source_manifest_sha256'])


def verified_inputs(root, db):
    """Bounded archive metadata checks; promoted data uses the full validator."""
    if not (root / 'manifest.json').exists():
        return dict(stage='waiting_downloads', reason='Pinned database manifest is absent')
    require(read(root / 'manifest.json') == db['MANIFEST'], 'Database manifest differs from pinned version')
    final = root / '.msa-databases.json'
    if final.exists():
        return dict(stage='database_ready', validation=db['validate'](root))
    if not (root / '.downloads.json').exists():
        return dict(stage='waiting_downloads', reason='Complete pinned downloads receipt is absent')
    receipt = read(root / '.downloads.json')
    require(receipt.get('stage') == 'sources-downloaded' and receipt.get('production_ready') is False
            and receipt.get('manifest_sha256') == db['MANIFEST_SHA256']
            and set(receipt.get('sources', {})) == set(db['SOURCES']), 'Incomplete or mismatched downloads receipt')
    promoted, sources = {}, {}
    for name, source in db['SOURCES'].items():
        saved = receipt['sources'][name]
        require(isinstance(saved, dict) and all(saved.get(k) == v for k, v in source.items())
                and re.fullmatch(r'[0-9a-f]{64}', saved.get('sha256', ''))
                and re.fullmatch(r'[0-9a-f]{32}', saved.get('actual_md5', ''))
                and (not source.get('md5') or saved['actual_md5'] == source['md5']), 'Invalid pinned source receipt: ' + name)
        archive = root / '.archives' / source['archive']
        require(read(archive.with_suffix(archive.suffix + '.json')) == saved, 'Archive/download receipts disagree: ' + name)
        if archive.exists():
            require(archive.is_file() and not archive.is_symlink() and archive.stat().st_size == source['bytes'],
                    'Archive is missing bytes or differs from pinned size: ' + name)
            sources[name] = dict(status='archive_verified', bytes=source['bytes'], sha256=saved['sha256'])
            continue
        owner = 'uniref30' if name == 'taxonomy' else name
        require(owner in db['COMPONENTS'], 'No exact promoted owner for missing archive')
        if owner not in promoted:
            component = read(root / '.components' / (owner + '.json'))
            require(component == read(root / owner / '.component.json')
                    and component.get('manifest_sha256') == db['MANIFEST_SHA256']
                    and component.get('component') == owner
                    and component.get('files') == db['validate_component'](root / owner, owner),
                    'Missing archive has no fully validated promoted component: ' + name)
            promoted[owner] = component
        require(promoted[owner].get('sources', {}).get(name) == saved,
                'Promoted component differs from original downloaded source: ' + name)
        sources[name] = dict(status='validated_promoted_component', component=owner, sha256=saved['sha256'])
    return dict(stage='sources_ready', manifest_sha256=db['MANIFEST_SHA256'], sources=sources)


def choose(catalog, regular, spot):
    def available(value):
        require(isinstance(value, list) and all(isinstance(r, dict) for r in value), 'Invalid availability response')
        rows = [r for r in value if r.get('location_code') == 'FIN-02']
        require(len(rows) == 1 and isinstance(rows[0].get('availabilities'), list)
                and all(isinstance(t, str) for t in rows[0]['availabilities']), 'Missing FIN-02 availability')
        return set(rows[0]['availabilities'])
    available_sets = {False: available(regular), True: available(spot)}
    require(isinstance(catalog, list) and all(isinstance(r, dict) for r in catalog), 'Invalid instance catalog')
    candidates, seen = [], set()
    for row in catalog:
        kind = row['instance_type']
        require(kind not in seen, 'Duplicate catalog instance type')
        seen.add(kind)
        require(type(row['memory']['size_in_gigabytes']) in (int, float, str), 'Invalid catalog memory')
        memory = float(row['memory']['size_in_gigabytes'])
        require(math.isfinite(memory) and memory >= 0, 'Invalid catalog memory')
        # Supported tools are Linux x86_64 AVX2; Grace/GB ARM and confidential
        # image variants are outside this fixed, reviewed worker family list.
        supported = bool(re.fullmatch(r'CPU\.[0-9]+V\.[0-9]+G|[1248](?:A100|H100|H200|B200|B300)\.[A-Za-z0-9.]+', kind))
        cpu = kind.startswith('CPU.')
        image = 'ubuntu-24.04' if cpu else 'ubuntu-24.04-cuda-12.8-open-docker'
        if not supported or kind.endswith('.CC') or memory * 10**9 < 768 * 1024**3 or image not in row['supported_os']:
            continue
        require(row['currency'] == 'usd', 'USD worker pricing required')
        for is_spot, types in available_sets.items():
            require(type(row['spot_price' if is_spot else 'price_per_hour']) in (int, float, str), 'Invalid worker price')
            rate = float(row['spot_price' if is_spot else 'price_per_hour'])
            require(math.isfinite(rate) and rate > 0, 'Invalid worker price')
            if kind in types and rate <= MAX_PRICE:
                candidates.append(dict(instance_type=kind, spot=is_spot, price_per_hour=rate,
                                       advertised_gb=memory, conservative_gib=memory*10**9/1024**3,
                                       image=image, supported_os=row['supported_os'], cpu=cpu))
    return min(candidates, key=lambda c: (c['price_per_hour'], not c['cpu'], c['spot'], c['instance_type']), default=None)


class Operations:
    def __init__(self, state_root, database_root, storage, budget, *, owner=0):
        self.root, self.database_root = Path(state_root), Path(database_root)
        self.storage, self.budget, self.owner = storage, budget, owner
        self.api = budget['API']()
        self.results = Path(os.environ.get('BIO_RESULTS_DIR', '/var/lib/bio-runs'))

    def run(self, args, timeout=30):
        return subprocess.run(list(map(str, args)), text=True, capture_output=True, timeout=timeout)

    def boot(self):
        return self.storage['boot_id']()

    def unit(self, name):
        props = ['LoadState', 'ActiveState', 'SubState', 'Result', 'ExecMainStatus', 'ExecMainCode',
                 'ExecMainPID', 'InvocationID', 'Description']
        value = self.run(['systemctl', 'show', name, '--no-pager', *['--property=' + p for p in props]])
        data = dict(line.split('=', 1) for line in value.stdout.splitlines() if '=' in line)
        require(data.get('LoadState') in {'loaded', 'not-found'}, 'Cannot resolve service state: ' + name)
        require(value.returncode == 0 or data['LoadState'] == 'not-found', 'systemctl show failed')
        return data

    def storage_receipt(self):
        path = self.storage['receipt_path']('colabfold', self.root)
        value = self.storage['Store'](path, owner=self.owner).read()
        self.storage['check_active'](value, value.get('volume_id') if value else None, time.time(), 'colabfold')
        require(value.get('retention') == 'persistent', 'Queue requires explicitly persistent storage')
        result = self.run(['bio-msa-storage', 'check', '--volume', value['volume_id']])
        require(result.returncode == 0, 'Storage wrapper denied database use')
        return value

    def mounted(self, receipt):
        result = self.run(['findmnt', '--json', '--target', self.database_root.parent, '--output', 'SOURCE,FSTYPE,OPTIONS'])
        require(result.returncode == 0, 'Cannot verify database mount')
        rows = loads(result.stdout).get('filesystems', [])
        # systemd automounts appear alongside their real mounted NFS export.
        # Ignore only that autofs placeholder, never a competing data mount.
        require(isinstance(rows, list) and all(isinstance(row, dict) and row.get('fstype') in {'autofs', 'nfs', 'nfs4'}
                                             for row in rows), 'Unexpected database mount stack')
        rows = [row for row in rows if row.get('fstype') != 'autofs']
        require(len(rows) == 1 and rows[0].get('source') == receipt['nfs']
                and rows[0].get('fstype') in {'nfs', 'nfs4'}
                and 'vers=4.1' in rows[0].get('options', '').split(','), 'Wrong database NFS export or protocol')

    @contextlib.contextmanager
    def submit_lock(self):
        path = self.root / 'msa-submit.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == self.owner and not info.st_mode & 0o022,
                    'Unsafe MSA submit lock')
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True

    def jobs(self):
        path = self.root / 'budget.json'
        value = self.storage['Store'](path, owner=self.owner).read()
        require(value is not None and value.get('version') == 1 and isinstance(value.get('jobs'), dict), 'Missing or malformed budget state')
        for token, row in value['jobs'].items():
            require(isinstance(token, str) and isinstance(row, dict) and row.get('status') in JOB_STATES
                    and isinstance(row.get('volumes', []), list), 'Invalid managed job record')
        return value['jobs']

    def inputs(self):
        result = self.run([sys.executable, Path(__file__), '--database-root', self.database_root, 'check-inputs'], timeout=150)
        require(result.returncode == 0, 'Database prerequisites failed: ' + result.stderr[-1200:])
        return loads(result.stdout)

    def choice(self):
        try:
            return choose(self.api.request('GET', '/instance-types?currency=usd'),
                          self.api.request('GET', '/instance-availability?location_code=FIN-02&is_spot=false'),
                          self.api.request('GET', '/instance-availability?location_code=FIN-02&is_spot=true'))
        except self.budget['APIError'] as exc:
            raise Unavailable('Read-only capacity check unavailable: ' + str(exc)) from None

    def submission_pin(self):
        path = os.environ.get('BIO_MSA_QUEUE_TOOLS_PIN')
        return tools_pin(path, owner=self.owner) if path else None

    def launch(self, attempt, panel):
        pin = self.submission_pin()
        require(pin == attempt.get('tools_pin'), 'Submission source pin changed after durable launch intent')
        command = ([pin['bio_submit'], 'msa', '--sub', attempt['stage']] if pin else ['bio-msa', attempt['stage']])
        command += ['--worker', attempt['worker']['instance_type'], '--timeout', str(WORK_SECONDS)]
        environment = [] if pin is None else ['--setenv=BIO_TOOLS_SRC=' + pin['tools_src'],
                                              '--setenv=BIO_CLUSTER_CONFIG=' + pin['cluster_config']]
        if attempt['worker']['spot']:
            command.append('--spot')
        if panel:
            command += ['--json', panel['path']]
        return self.run(['systemd-run', '--no-block', '--unit=' + attempt['unit'],
                         '--description=' + attempt['description'], '--property=Type=exec',
                         '--property=RemainAfterExit=yes', '--property=RuntimeMaxSec=22500',
                         '--property=TimeoutStopSec=180', '--property=KillMode=mixed',
                         '--property=StandardOutput=append:' + attempt['log'], '--property=StandardError=journal',
                         '--setenv=DC_STATE_DIR=' + str(self.root), '--setenv=BIO_STATE_DIR=' + str(self.root),
                         '--setenv=DC_MAX_INSTANCE_HOURLY=13',
                         *environment, '--', *command])

    def cleanup(self, attempt, receipt, jobs, unit):
        involved = {token: row for token, row in jobs.items() if receipt['volume_id'] in row.get('volumes', [])
                    and token not in attempt['jobs_before']}
        for token in attempt.get('job_tokens', []):
            require(token in jobs, 'Recorded managed job disappeared from budget ledger')
            involved[token] = jobs[token]
        attempt['job_tokens'] = sorted(involved)
        if any(row['status'] != 'closed' for row in jobs.values() if receipt['volume_id'] in row.get('volumes', [])):
            return False
        instances, volumes, trash = self.api.inventory()
        ids = {row.get('id') for row in involved.values()} - {None}
        disks = {row.get('os_id') for row in involved.values()} - {None}
        if any(row.get('id') in ids for row in instances) or any(row.get('id') in disks for row in volumes):
            return False
        if any(row.get('id') in disks and row.get('is_permanently_deleted') is not True for row in trash):
            return False
        pid = int(unit.get('ExecMainPID', '0'))
        names = [name for name, row in receipt['jobs'].items() if name not in attempt['tracked_before']
                 and row['pid'] == pid and row['boot_id'] == attempt['boot_id']]
        records = []
        for name in names:
            path = self.results / name / 'job.json'
            if path.exists():
                job = read(path)
                require(job.get('job') == name and job.get('model') == 'msa'
                        and job.get('database_volume') == receipt['volume_id'] and job.get('instance') in ids
                        and type(job.get('exit_status')) is int, 'Managed result/cleanup identity is unresolved')
                records.append(dict(job=name, path=str(path.parent), exit_status=job['exit_status'], sha256=sha(path)))
        attempt['results'] = records
        return True

    def verify_panel(self, panel, result):
        value = self.run([sys.executable, HERE / 'panel.py', 'verify', '--manifest', panel['path'],
                          '--expected-sha256', panel['canonical_sha256'], '--out', Path(result['path']) / 'panel'], timeout=150)
        require(value.returncode == 0, 'Frozen panel validation failed: ' + value.stderr[-1200:])
        return loads(value.stdout)


class Queue:
    def __init__(self, store, ops, *, clock=time.time):
        self.store, self.ops, self.clock = store, ops, clock

    def validate(self, state):
        require(isinstance(state, dict) and type(state.get('version')) is int and state['version'] == 1
                and state.get('status') in STATES and isinstance(state.get('token'), str)
                and re.fullmatch(r'[a-f0-9]{32}', state['token'])
                and isinstance(state.get('attempts'), list) and len(state['attempts']) <= MAX_ATTEMPTS,
                'Invalid private build queue receipt')
        require(str(uuid.UUID(state['volume_id'])) == state['volume_id'], 'Invalid queue volume UUID')
        for index, attempt in enumerate(state['attempts'], 1):
            require(isinstance(attempt, dict) and type(attempt.get('number')) is int and attempt['number'] == index
                    and attempt.get('unit') == f"bio-msa-build-{state['token']}-a{index}.service"
                    and attempt.get('description') == f"Bio MSA build {state['token']} attempt {index}"
                    and attempt.get('stage') in {'install', 'panel'}
                    and attempt.get('log') == str(self.store.path.parent / (attempt['unit'] + '.log')),
                    'Queue attempt identity changed')
            require(str(uuid.UUID(attempt['boot_id'])) == attempt['boot_id'], 'Invalid attempt boot identity')
            for key in ('jobs_before', 'tracked_before', 'job_tokens'):
                require(isinstance(attempt.get(key), list) and all(isinstance(v, str) for v in attempt[key])
                        and len(set(attempt[key])) == len(attempt[key]), 'Invalid attempt reconciliation ledger')
            require('closed' not in attempt or type(attempt['closed']) is bool, 'Invalid attempt closure flag')
            if 'tools_pin' in attempt:
                pin = attempt['tools_pin']
                hashes = {'sha256', 'content_sha256', 'bio_submit_sha256', 'source_manifest_sha256'}
                paths = {'path', 'tools_src', 'bio_submit', 'cluster_config'}
                require(isinstance(pin, dict) and set(pin) == hashes | paths
                        and all(isinstance(pin[k], str) and re.fullmatch(r'[a-f0-9]{64}', pin[k]) for k in hashes)
                        and all(isinstance(pin[k], str) and Path(pin[k]).is_absolute() for k in paths),
                        'Invalid frozen submission source identity')
        panel = state.get('panel')
        if panel is not None:
            require(isinstance(panel, dict) and set(panel) == {'path', 'sha256', 'canonical_sha256'}
                    and all(isinstance(panel[k], str) for k in panel)
                    and re.fullmatch(r'[a-f0-9]{64}', panel['sha256'])
                    and re.fullmatch(r'[a-f0-9]{64}', panel['canonical_sha256']), 'Invalid frozen panel identity')
            path = Path(panel['path'])
            require(path.parent == self.store.path.parent and re.fullmatch(r'msa-build-panel-[a-f0-9]{32}\.json', path.name),
                    'Invalid frozen panel snapshot path')
            self.store.secure(path.lstat())
            require(path.stat().st_size <= 16 * 1024**2 and sha(path) == panel['sha256'],
                    'Frozen panel snapshot changed')
        return state

    def save(self, state, status, reason, **values):
        state.update(status=status, reason=reason, updated_at=self.clock(), **values)
        self.validate(state)
        self.store.save(state)
        return state

    def init(self, panel=None):
        with self.store.locked() as old:
            require(old is None, 'Queue already exists; inspect status or explicitly resume it')
            receipt = self.ops.storage_receipt()
            state = dict(version=1, token=uuid.uuid4().hex, volume_id=receipt['volume_id'], created_at=self.clock(),
                         attempts=[], stage='downloads', panel=panel, status='waiting_downloads')
            return self.save(state, 'waiting_downloads', 'Awaiting verified pinned sources')

    def tick(self):
        with self.store.expiry_operation() as acquired:
            if not acquired:
                return dict(status='busy', reason='Another queue operation is active')
            with self.store.locked() as state:
                self.validate(state)
                if state['status'] in {'ready', 'blocked', 'exhausted'}:
                    return state
                try:
                    return self.advance(state)
                except (Error, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, RuntimeError) as exc:
                    return self.save(state, 'blocked', str(exc))

    def advance(self, state):
        receipt = self.ops.storage_receipt()
        require(receipt['volume_id'] == state['volume_id'], 'Registered database volume changed')
        jobs = self.ops.jobs()
        attempt = state['attempts'][-1] if state['attempts'] else None
        if attempt and not attempt.get('closed'):
            if attempt['boot_id'] != self.ops.boot():
                return self.save(state, 'uncertain', 'Head rebooted after dispatch; exact service/worker reconciliation requires operator review')
            unit = self.ops.unit(attempt['unit'])
            attempt['last_unit'] = unit
            if unit['LoadState'] == 'not-found':
                return self.save(state, 'uncertain', 'Submitted service is absent; never repeat its launch automatically')
            require(unit.get('Description') == attempt['description'], 'Deterministic service belongs to another operation')
            if attempt.get('invocation'):
                require(unit.get('InvocationID') == attempt['invocation'], 'Service invocation changed')
            elif unit.get('InvocationID'):
                attempt['invocation'] = unit['InvocationID']
            # Include reservations even when a launch has no known instance ID.
            attempt['job_tokens'] = sorted(set(attempt.get('job_tokens', [])) | {
                token for token, row in jobs.items() if state['volume_id'] in row.get('volumes', []) and token not in attempt['jobs_before']})
            if unit.get('SubState') not in {'exited', 'dead', 'failed'} or int(unit.get('ExecMainCode', '0')) == 0:
                return self.save(state, 'running', 'Exact service remains active')
            if not self.ops.cleanup(attempt, receipt, jobs, unit):
                return self.save(state, 'waiting_cleanup', 'Exact managed workers or OS cleanup remain unresolved')
            code = int(unit['ExecMainStatus'])
            if int(unit['ExecMainCode']) != 1:
                code += 128
            attempt.update(exit_status=code, closed=True, closed_at=self.clock())
            if code == 4:
                return self.save(state, 'blocked', 'Guard refused paid work (exit 4); explicit operator resume required')
            if code == 0:
                require(len(attempt.get('results', [])) == 1 and attempt['results'][0]['exit_status'] == 0,
                        'Successful service lacks one exact successful managed result')
                inputs = self.ops.inputs()
                require(inputs['stage'] == 'database_ready', 'Successful installation lacks validated final databases')
                if state.get('panel'):
                    state['panel_validation'] = self.ops.verify_panel(state['panel'], attempt['results'][0])
                return self.save(state, 'ready', 'Requested database/panel work and exact worker cleanup are complete', stage='done')
            # Failures consume an attempt, even if the provider rejected before
            # creating a worker. Their unit snapshots, logs and results remain.
            self.save(state, 'waiting_downloads', 'Prior attempt failed; cleanup is confirmed')
        downloader = self.ops.unit('msa-database-install.service')
        if downloader.get('ActiveState') not in {'inactive', 'failed'} and downloader['LoadState'] != 'not-found':
            return self.save(state, 'waiting_downloads', 'Head database downloader/installer is active')
        with self.ops.submit_lock() as acquired:
            if not acquired:
                return self.save(state, 'waiting_manual', 'Another MSA submission holds the shared submit lock')
            if any(row['status'] != 'closed' for row in jobs.values() if state['volume_id'] in row.get('volumes', [])):
                return self.save(state, 'waiting_cleanup', 'Existing MSA worker or uncertain reservation remains open')
            self.ops.mounted(receipt)
            try:
                inputs = self.ops.inputs()
            except FileNotFoundError:
                return self.save(state, 'waiting_downloads', 'Pinned download metadata is not yet present')
            state['input_validation'] = inputs
            if inputs['stage'] == 'waiting_downloads':
                return self.save(state, 'waiting_downloads', inputs['reason'], stage='downloads')
            require(inputs['stage'] in {'database_ready', 'sources_ready'}, 'Unknown database validation stage')
            stage = 'panel' if inputs['stage'] == 'database_ready' else 'install'
            if stage == 'panel' and not state.get('panel'):
                return self.save(state, 'ready', 'Final databases validate; no panel requested', stage='done')
            if len(state['attempts']) >= MAX_ATTEMPTS:
                return self.save(state, 'exhausted', 'Three attempts consumed; operator review required', stage=stage)
            try:
                candidate = self.ops.choice()
            except Unavailable as exc:
                return self.save(state, 'waiting_capacity', str(exc), stage=stage)
            if candidate is None:
                return self.save(state, 'waiting_capacity', 'No supported FIN-02 host meets RAM/price constraints', stage=stage)
            number = len(state['attempts']) + 1
            unit = f"bio-msa-build-{state['token']}-a{number}.service"
            require(self.ops.unit(unit)['LoadState'] == 'not-found', 'Attempt service already exists before dispatch')
            attempt = dict(number=number, unit=unit, description=f"Bio MSA build {state['token']} attempt {number}",
                           stage=stage, worker=candidate, log=str(self.store.path.parent / (unit + '.log')),
                           created_at=self.clock(), boot_id=self.ops.boot(), jobs_before=sorted(jobs),
                           tracked_before=sorted(receipt['jobs']), job_tokens=[])
            pin = getattr(self.ops, 'submission_pin', lambda: None)()
            if pin is not None:
                attempt['tools_pin'] = pin
            state['attempts'].append(attempt)
            self.save(state, 'dispatching', 'Durable intent saved before systemd submission', stage=stage)
            try:
                response = self.ops.launch(attempt, state.get('panel'))
                attempt['systemd_run_exit_status'] = response.returncode
            except (OSError, subprocess.SubprocessError) as exc:
                attempt['submission_error'] = str(exc)
            return self.save(state, 'uncertain', 'Reconcile the deterministic service on the next tick; never repeat POST/start')


def private_store(storage, path, owner=0):
    """Reuse the lifecycle's secure locks/atomic writes, with strict queue JSON."""
    class StrictStore(storage['Store']):
        def read(self):
            try:
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                return None
            with os.fdopen(fd) as stream:
                self.secure(os.fstat(stream.fileno()))
                value = loads(stream.read())
            require(isinstance(value, dict), 'Queue state must be a JSON object')
            return value
    return StrictStore(path, owner=owner)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path, default=Path(os.environ.get('DC_STATE_DIR', '/var/lib/dc')))
    parser.add_argument('--database-root', type=Path, default=Path(os.environ.get('MSA_DB_ROOT', '/mnt/bio-msa-databases/colabfold')))
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--panel-manifest', type=Path)
    for name in ('tick', 'status', 'resume', 'check-inputs'):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    if args.command == 'check-inputs':
        print(json.dumps(verified_inputs(args.database_root, runpy.run_path(str(HERE / 'databases.py')))))
        return
    # Nix deploys msa/, rfaa/ and dc-budget.py as separate store paths. Resolve
    # siblings through the deployment tree, never through HERE's store parent.
    tools_root = Path(os.environ.get('BIO_TOOLS_SRC', '/etc/bio-tools'))
    storage = runpy.run_path(str(tools_root / 'rfaa/storage.py'))
    budget = runpy.run_path(str(tools_root / 'dc-budget.py'))
    store = private_store(storage, args.state_root / 'msa-build-queue.json')
    ops = Operations(args.state_root, args.database_root, storage, budget)
    queue = Queue(store, ops)
    if args.command == 'init':
        panel = None
        if args.panel_manifest:
            store.secure(args.state_root.lstat(), directory=True)
            target = args.state_root / ('msa-build-panel-' + uuid.uuid4().hex + '.json')
            with args.panel_manifest.open('rb') as source:
                raw = source.read(16 * 1024**2 + 1)
            require(len(raw) <= 16 * 1024**2, 'Panel manifest exceeds 16 MiB')
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            checked = ops.run([sys.executable, HERE / 'panel.py', 'validate', '--manifest', target])
            require(checked.returncode == 0, 'Invalid panel manifest snapshot')
            panel = dict(path=str(target), sha256=sha(target), canonical_sha256=loads(checked.stdout)['manifest_sha256'])
        result = queue.init(panel)
    elif args.command == 'tick':
        result = queue.tick()
    else:
        with store.locked() as result:
            queue.validate(result)
            if args.command == 'resume':
                require(result['status'] == 'blocked', 'Only an explicitly blocked queue can be resumed')
                result = queue.save(result, 'waiting_downloads', 'Explicit operator resume; attempts and uncertain identities retained')
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except (Error, OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print('msa-build-queue: ' + str(exc), file=sys.stderr)
        sys.exit(1)
