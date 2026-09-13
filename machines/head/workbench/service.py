"""Reconcile exact-owned systemd units; ambiguous launches never retry."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys

from .common import canonical, file_sha, no_links, now, parse, read_json, require, write_json
from msa.capacity import preparation_allowance


def configuration(path=None):
    data = read_json(Path(path).resolve()) if path else {}
    defaults = {'tools_dir': '/etc/bio-tools', 'bio_submit': '/run/current-system/sw/bin/bio-submit',
                'library_root': '/var/lib/bio-library', 'runtime_config': '/etc/bio-tools/library-runtime.json',
                'inference_state': '/var/lib/bio-inference', 'max_jobs': 1,
                'msa_sessions_root': '/var/lib/dc/msa-sessions',
                'capacity_helper': '/run/current-system/sw/bin/bio-msa-capacity',
                'bindcraft_shared': '/mnt/bio-shared',
                'md_runtime': '/var/lib/bio-md/runtime-cpu',
                'md_admissions': '/var/lib/bio-md/admissions',
                'md_runtime_archives': '/mnt/bio-shared/md-runtime',
                'systemd_run': '/run/current-system/sw/bin/systemd-run',
                'systemctl': '/run/current-system/sw/bin/systemctl'}
    require(isinstance(data, dict) and set(data) <= set(defaults), 'Invalid trusted workbench configuration')
    defaults.update(data)
    require(type(defaults['max_jobs']) is int and 1 <= defaults['max_jobs'] <= 32, 'max_jobs must be 1..32')
    if defaults['tools_dir'] == '/etc/bio-tools':
        # Preserve the complete deployed hierarchy: resolving only the
        # workbench source symlink would lose its sibling inference/library
        # packages. This closure survives subsequent /etc activation changes.
        deployed = Path('/run/current-system').resolve() / 'etc/bio-tools'
        require(deployed.is_dir(), 'Current deployed toolkit hierarchy is unavailable', 'unavailable')
        defaults['tools_dir'] = str(deployed)
    for key in ('bio_submit', 'runtime_config', 'systemd_run', 'systemctl', 'capacity_helper'):
        defaults[key] = str(Path(defaults[key]).resolve())
    defaults['tools_dir'] = str(Path(defaults['tools_dir']).absolute())
    return defaults


def unit_state(config, unit):
    require(re.fullmatch('bio-workbench-(validation|job)-[a-f0-9]{32}\\.service', unit), 'Unowned unit name')
    props = ['LoadState', 'ActiveState', 'SubState', 'InvocationID', 'ExecStart', 'MainPID', 'ControlGroup', 'ExecMainStatus', 'Result']
    result = subprocess.run([config['systemctl'], 'show', unit, '--no-pager', '--property=' + ','.join(props)],
                            capture_output=True, text=True, timeout=15, check=False)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    require(values.get('LoadState') in {'loaded', 'not-found'}, 'Cannot establish owned unit state', 'unavailable')
    return values


def matches(state, command, invocation=None):
    if state.get('LoadState') != 'loaded' or not re.fullmatch('[a-f0-9]{32}', state.get('InvocationID', '')):
        return False
    if invocation and state['InvocationID'] != invocation:
        return False
    match = re.search(r'argv\[\]=(.*?) ;', state.get('ExecStart', ''))
    return bool(match and match.group(1).split() == command)


class Daemon:
    def __init__(self, store, config, system=unit_state, launch=subprocess.run):
        self.store, self.config, self.system, self.launch_command = store, config, system, launch

    def start(self, kind, data):
        return self._start(kind, data)

    def _start(self, kind, data):
        ident = data['batch_id' if kind == 'validation' else 'job_id']
        capacity_allowance = 0 if kind == 'validation' else preparation_allowance(data['_prepared'])
        unit = 'bio-workbench-' + kind + '-' + ident + '.service'
        require(self.system(self.config, unit)['LoadState'] == 'not-found', 'Unit already exists without matching intent', 'conflict')
        folder = self.store.directory('operations', ident)
        folder.mkdir(parents=True, mode=0o700, exist_ok=True)
        write_json(folder / 'config.json', self.config, exclusive=True)
        base = [sys.executable, '-B', str(Path(self.config['tools_dir']) / 'workbench/cli.py'), '--state', str(self.store.root),
                '--config', str(folder / 'config.json'), 'execute', '--kind', kind, '--id', ident]
        require(all(not any(c.isspace() for c in arg) for arg in base), 'Trusted execution paths must not contain whitespace')
        intent = {'version': 1, 'object_id': ident, 'kind': kind, 'unit': unit, 'command_prefix': base,
                  'config_sha256': file_sha(folder / 'config.json'), 'created_at': now()}
        write_json(folder / 'intent.json', intent, exclusive=True)
        value = file_sha(folder / 'intent.json'); command = [*base, '--intent-sha256', value]
        operation = {**intent, 'command': command, 'intent_sha256': value}
        with self.store.transaction() as db:
            object_kind = 'batch' if kind == 'validation' else 'job'
            current = self.store.get(db, object_kind, ident)
            require(current['state'] == ('validating' if kind == 'validation' else 'queued'), 'Work cancelled before launch', 'conflict')
            db.execute('INSERT INTO operations(object_id,kind,unit,state,intent_sha256,invocation_id,data,updated) VALUES(?,?,?,?,?,?,?,?)',
                       (ident, kind, unit, 'intent', value, None, canonical(operation).decode(), now()))
            if kind == 'job':
                current['state'] = 'starting'; self.store.put(db, object_kind, current)
        seconds = (min(36000, 650 * len(data['pairs']) + 120) if kind == 'validation'
                   else data['_prepared']['timeout'] + capacity_allowance + 1800)
        args = [self.config['systemd_run'], '--quiet', '--unit=' + unit, '--service-type=exec',
                '--property=RemainAfterExit=yes', '--property=UMask=0077', '--property=TasksMax=4096',
                '--property=RuntimeMaxSec=' + str(seconds), '--property=KillMode=' + ('control-group' if kind == 'validation' else 'mixed'),
                '--property=TimeoutStopSec=' + ('20' if kind == 'validation' else '900')]
        if kind == 'job' and data['_prepared'].get('msa_backend') == 'private' and data['_prepared'].get('msa_applicable'):
            # Bind the same trusted policy in the owned unit, its runner, and
            # the nested MSA caller even when systemd drops the daemon's env.
            args += ['--setenv=BIO_MSA_CAPACITY_WAIT_SECONDS=' + str(capacity_allowance)]
        if kind == 'validation':
            args += ['--property=MemoryMax=6G', '--property=MemorySwapMax=0', '--property=CPUQuota=200%', '--property=PrivateNetwork=yes']
        write_json(folder / 'systemd-command.json', {'argv': [*args, '--', *command]}, exclusive=True)
        # An exception/timeout is ambiguous. The next tick reconciles this exact
        # recorded unit; it never issues another systemd-run for the same intent.
        result = self.launch_command([*args, '--', *command], capture_output=True, text=True, timeout=30, check=False)
        write_json(folder / 'launch-result.json', {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}, exclusive=True)

    def reconcile(self, row):
        folder = self.store.directory('operations', row['object_id'])
        operation = read_json(folder / 'intent.json')
        require(file_sha(folder / 'intent.json') == row['intent_sha256'] and
                operation.get('object_id') == row['object_id'] and operation.get('kind') == row['kind'] and
                operation.get('unit') == row['unit'], 'Execution intent binding differs', 'integrity')
        base = operation['command_prefix'] + ['--intent-sha256', row['intent_sha256']]
        state = self.system(self.config, row['unit'])
        write_json(folder / 'last-unit-state.json', {'observed_at': now(), **state})
        # execute() publishes its receipt before committing the operation row.
        # Recover that exact crash window, including after systemd has forgotten
        # the unit. A malformed or differently bound receipt keeps its slot.
        terminal = folder / 'terminal.json'
        receipt = self.terminal_receipt(row, folder) if terminal.exists() else None
        if receipt and state.get('LoadState') == 'not-found':
            self.finish_operation(row, receipt)
            return
        if not matches(state, base, row['invocation_id']):
            require(receipt is None, 'Terminal receipt owned unit identity differs', 'integrity')
            self.interrupted(row, 'Owned unit is absent or identity differs; no automatic retry')
            return
        with self.store.transaction() as db:
            db.execute('UPDATE operations SET invocation_id=COALESCE(invocation_id,?),updated=? WHERE object_id=?',
                       (state['InvocationID'], now(), row['object_id']))
        object_kind = 'batch' if row['kind'] == 'validation' else 'job'
        data = self.store.read(object_kind, row['object_id'])
        if row['kind'] == 'validation' and data['state'] == 'cancelled' and state['SubState'] == 'running':
            # Only the identity-bound, CPU-only validation cgroup is stopped.
            fresh = self.system(self.config, row['unit'])
            require(matches(fresh, base, state['InvocationID']), 'Validation unit changed before cancellation', 'integrity')
            self.launch_command([self.config['systemctl'], 'stop', '--no-block', row['unit']], timeout=15, check=True, capture_output=True, text=True)
        elif state['ActiveState'] in {'failed', 'inactive'} or state['SubState'] == 'exited':
            if receipt is None:
                self.interrupted(row, 'Owned unit ended without a terminal receipt; retained, never retried')
            else:
                require(receipt['invocation_id'] == state['InvocationID'], 'Terminal receipt invocation differs', 'integrity')
                self.finish_operation(row, receipt)

    def terminal_receipt(self, row, folder):
        receipt = read_json(folder / 'terminal.json')
        started = read_json(folder / 'started.json')
        binding = {'kind': row['kind'], 'object_id': row['object_id'], 'unit': row['unit'],
                   'invocation_id': row['invocation_id'], 'intent_sha256': row['intent_sha256']}
        require(isinstance(receipt, dict) and isinstance(started, dict) and
                re.fullmatch('[a-f0-9]{32}', row['invocation_id'] or '') is not None and
                all(receipt.get(key) == value and started.get(key) == value for key, value in binding.items()) and
                isinstance(receipt.get('started_at'), str) and bool(receipt['started_at']) and
                receipt['started_at'] == started.get('started_at') and
                isinstance(receipt.get('finished_at'), str) and bool(receipt['finished_at']) and
                isinstance(receipt.get('state'), str) and receipt['state'] in {'complete', 'failed'},
                'Terminal receipt binding differs', 'integrity')
        return receipt

    def finish_operation(self, row, receipt):
        with self.store.transaction() as db:
            object_kind = 'batch' if row['kind'] == 'validation' else 'job'
            data = self.store.get(db, object_kind, row['object_id'])
            terminal_states = ({'validated', 'validation_failed', 'cancelled'} if row['kind'] == 'validation'
                               else {'complete', 'failed', 'cancelled', 'interrupted'})
            require(data['state'] in terminal_states or
                    (row['kind'] == 'job' and data.get('_resident_pending') and
                     data['state'] in {'running', 'cancel_requested', 'interrupted'}),
                    'Terminal receipt has no matching completed or tracked execution', 'integrity')
            # The runner may have committed while systemctl was being queried.
            # Only a still-active operation is updated, so recovery is once-only.
            cursor = db.execute("UPDATE operations SET state=?,data=?,updated=? WHERE object_id=? "
                                "AND state IN ('intent','running') AND intent_sha256=? AND invocation_id=?",
                                (receipt['state'], canonical(receipt).decode(), now(), row['object_id'],
                                 receipt['intent_sha256'], receipt['invocation_id']))
            if cursor.rowcount:
                self.store.event(db, row['object_id'], 'terminal_receipt_recovered', {'state': receipt['state']})

    def interrupted(self, row, message):
        with self.store.transaction() as db:
            object_kind = 'batch' if row['kind'] == 'validation' else 'job'
            data = self.store.get(db, object_kind, row['object_id'])
            if data['state'] not in {'complete', 'failed', 'cancelled', 'interrupted', 'validated', 'validation_failed'}:
                data['state'] = 'validation_failed' if row['kind'] == 'validation' else 'interrupted'
                if row['kind'] == 'validation':
                    data['errors'].append(message)
                else:
                    data['error'] = {'message': message, 'automatic_retry': False}; data['finished_at'] = now()
                self.store.put(db, object_kind, data)
            db.execute("UPDATE operations SET state='interrupted',updated=? WHERE object_id=?", (now(), row['object_id']))
            self.store.event(db, row['object_id'], 'execution_uncertain', {'message': message})

    def start_failed(self, kind, data, error):
        ident = data['batch_id' if kind == 'validation' else 'job_id']
        with self.store.transaction() as db:
            if db.execute('SELECT 1 FROM operations WHERE object_id=?', (ident,)).fetchone():
                return  # A committed intent is reconciled against the unit.
            object_kind = 'batch' if kind == 'validation' else 'job'
            current = self.store.get(db, object_kind, ident)
            message = 'Head launch preparation failed before a committed submission intent: ' + str(error)
            if current['state'] not in {'cancelled', 'complete', 'failed', 'interrupted'}:
                current['state'] = 'validation_failed' if kind == 'validation' else 'interrupted'
                if kind == 'validation':
                    current['errors'].append(message)
                else:
                    current['error'] = {'message': message, 'automatic_retry': False}; current['finished_at'] = now()
                self.store.put(db, object_kind, current)
            db.execute('INSERT INTO operations(object_id,kind,unit,state,intent_sha256,invocation_id,data,updated) VALUES(?,?,?,?,?,?,?,?)',
                       (ident, kind, 'bio-workbench-' + kind + '-' + ident + '.service', 'interrupted', '0' * 64,
                        None, canonical({'error': message, 'launch_intent_committed': False}).decode(), now()))
            self.store.event(db, ident, 'launch_preparation_failed', {'message': message, 'automatic_retry': False})

    def tick(self):
        errors = []
        pending_resident = [job for job in self.store.listing('job', states=['running', 'cancel_requested', 'interrupted']) if job.get('_resident_pending')]
        for job in pending_resident:
            try:
                from .runner import reconcile_resident
                reconcile_resident(self.store, job)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                errors.append({'id': job['job_id'], 'error': str(exc)})
        operations, _, _ = self.snapshot()
        for row in operations:
            try:
                self.reconcile(row)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                errors.append({'id': row['object_id'], 'error': str(exc)})
        # Run intent belongs to the head. A disconnected client is never needed
        # to turn completed validation into a single atomic set of queued jobs.
        from .api import API
        for batch in self.store.listing('batch', states=['validated']):
            if batch.get('auto_run'):
                try:
                    API(self.store, self.store.actor(batch['batch_id']))._automatic_run(batch['batch_id'])
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    errors.append({'id': batch['batch_id'], 'error': str(exc)})
        # Reconciliation may release operations or finish an orphaned native
        # request. Refresh all slot evidence before admitting the next work.
        operations, known, active_jobs = self.snapshot()
        running_validation = any(row['kind'] == 'validation' for row in operations)
        if not running_validation:
            candidates = [b for b in self.store.listing('batch', states=['validating']) if b['batch_id'] not in known]
            if candidates:
                try:
                    self.start('validation', candidates[0])
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    self.start_failed('validation', candidates[0], exc)
                    errors.append({'id': candidates[0]['batch_id'], 'error': str(exc)})
        slots = self.config['max_jobs'] - len(active_jobs)
        for job in [j for j in self.store.listing('job', states=['queued']) if j['job_id'] not in known][:max(0, slots)]:
            try:
                self.start('job', job)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                self.start_failed('job', job, exc)
                errors.append({'id': job['job_id'], 'error': str(exc)})
        self.queued_progress()
        # Controls have their own durable IDs and do not consume inference
        # slots. A retry reconciles the same idempotent session command.
        from .worker_api import reconcile
        try:
            reconcile(self.store, self.config)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            errors.append({'id': 'worker-controls', 'error': str(exc)})
        return {'observed_at': now(), 'errors': errors}

    def snapshot(self, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.snapshot(connection)
        operations = [dict(row) for row in db.execute("SELECT * FROM operations WHERE state IN ('intent','running')")]
        known = {row['object_id'] for row in db.execute('SELECT object_id FROM operations')}
        active_jobs = {row['object_id'] for row in operations if row['kind'] == 'job'}
        # A client and its continuing resident request are one job. This also
        # retains the slot after the client operation has a terminal receipt.
        for row in db.execute("SELECT id,data FROM objects WHERE kind='job' AND state IN ('running','cancel_requested','interrupted')"):
            if parse(row['data']).get('_resident_pending'):
                active_jobs.add(row['id'])
        return operations, known, active_jobs

    def queued_progress(self):
        with self.store.transaction() as db:
            _, _, active_jobs = self.snapshot(db)
            active, capacity = len(active_jobs), self.config['max_jobs']
            queued = list(db.execute("SELECT data FROM objects WHERE kind='job' AND state='queued' ORDER BY created,id"))
            for position, row in enumerate(queued, 1):
                job = parse(row['data'])
                message = f'Queue position {position}; {active} of {capacity} execution slots occupied'
                if active < capacity:
                    message += '; the dispatcher will admit queued jobs on its next pass'
                progress = {'message': message, 'queue_position': position, 'active_jobs': active, 'max_jobs': capacity}
                previous = {key: value for key, value in job.get('progress', {}).items() if key != 'observed_at'}
                if previous != progress:
                    job['phase'] = 'queued'
                    job['progress'] = {**progress, 'observed_at': now()}
                    self.store.put(db, 'job', job)
