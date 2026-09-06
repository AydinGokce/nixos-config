"""Reconcile exact-owned systemd units; ambiguous launches never retry."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys

from .common import canonical, file_sha, no_links, now, parse, read_json, require, write_json


def configuration(path=None):
    data = read_json(Path(path).resolve()) if path else {}
    defaults = {'tools_dir': '/etc/bio-tools', 'bio_submit': '/run/current-system/sw/bin/bio-submit',
                'library_root': '/var/lib/bio-library', 'runtime_config': '/etc/bio-tools/library-runtime.json',
                'inference_state': '/var/lib/bio-inference', 'max_jobs': 1,
                'systemd_run': '/run/current-system/sw/bin/systemd-run',
                'systemctl': '/run/current-system/sw/bin/systemctl'}
    require(isinstance(data, dict) and set(data) <= set(defaults), 'Invalid trusted workbench configuration')
    defaults.update(data)
    require(type(defaults['max_jobs']) is int and 1 <= defaults['max_jobs'] <= 4, 'max_jobs must be1..4')
    if defaults['tools_dir'] == '/etc/bio-tools':
        # Preserve the complete deployed hierarchy: resolving only the
        # workbench source symlink would lose its sibling inference/library
        # packages. This closure survives subsequent /etc activation changes.
        deployed = Path('/run/current-system').resolve() / 'etc/bio-tools'
        require(deployed.is_dir(), 'Current deployed toolkit hierarchy is unavailable', 'unavailable')
        defaults['tools_dir'] = str(deployed)
    for key in ('bio_submit', 'runtime_config', 'systemd_run', 'systemctl'):
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
        seconds = min(36000, 650 * len(data['pairs']) + 120) if kind == 'validation' else data['_prepared']['timeout'] + 1800
        args = [self.config['systemd_run'], '--quiet', '--unit=' + unit, '--service-type=exec',
                '--property=RemainAfterExit=yes', '--property=UMask=0077', '--property=TasksMax=4096',
                '--property=RuntimeMaxSec=' + str(seconds), '--property=KillMode=' + ('control-group' if kind == 'validation' else 'mixed'),
                '--property=TimeoutStopSec=' + ('20' if kind == 'validation' else '900')]
        if kind == 'validation':
            args += ['--property=MemoryMax=6G', '--property=MemorySwapMax=0', '--property=CPUQuota=200%', '--property=PrivateNetwork=yes']
        write_json(folder / 'systemd-command.json', {'argv': [*args, '--', *command]}, exclusive=True)
        # An exception/timeout is ambiguous. The next tick reconciles this exact
        # recorded unit; it never issues another systemd-run for the same intent.
        result = self.launch_command([*args, '--', *command], capture_output=True, text=True, timeout=30, check=False)
        write_json(folder / 'launch-result.json', {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}, exclusive=True)

    def reconcile(self, row):
        operation = read_json(self.store.directory('operations', row['object_id']) / 'intent.json')
        base = operation['command_prefix'] + ['--intent-sha256', row['intent_sha256']]
        state = self.system(self.config, row['unit'])
        folder = self.store.directory('operations', row['object_id'])
        write_json(folder / 'last-unit-state.json', {'observed_at': now(), **state})
        if not matches(state, base, row['invocation_id']):
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
            terminal = folder / 'terminal.json'
            if not terminal.exists():
                self.interrupted(row, 'Owned unit ended without a terminal receipt; retained, never retried')
            else:
                receipt = read_json(terminal)
                require(receipt['invocation_id'] == state['InvocationID'] and receipt['intent_sha256'] == row['intent_sha256'], 'Terminal receipt binding differs', 'integrity')

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
        with self.store.connection() as db:
            operations = [dict(row) for row in db.execute("SELECT * FROM operations WHERE state IN ('intent','running')")]
            known = {row['object_id'] for row in db.execute('SELECT object_id FROM operations')}
        for row in operations:
            try:
                self.reconcile(row)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                errors.append({'id': row['object_id'], 'error': str(exc)})
        running_validation = any(row['kind'] == 'validation' for row in operations)
        if not running_validation:
            candidates = [b for b in self.store.listing('batch', states=['validating']) if b['batch_id'] not in known]
            if candidates:
                try:
                    self.start('validation', candidates[0])
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    self.start_failed('validation', candidates[0], exc)
                    errors.append({'id': candidates[0]['batch_id'], 'error': str(exc)})
        slots = self.config['max_jobs'] - len([row for row in operations if row['kind'] == 'job']) - len(pending_resident)
        for job in [j for j in self.store.listing('job', states=['queued']) if j['job_id'] not in known][:max(0, slots)]:
            try:
                self.start('job', job)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                self.start_failed('job', job, exc)
                errors.append({'id': job['job_id'], 'error': str(exc)})
        return {'observed_at': now(), 'errors': errors}
