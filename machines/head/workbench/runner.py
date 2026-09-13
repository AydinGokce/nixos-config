"""Owned head executions; immutable inputs, no implicit inference retries."""
from __future__ import annotations

import mimetypes
import fcntl
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

from . import inputs, progress as telemetry
from msa.capacity import preparation_allowance
from .common import (TERMINAL, Error, atomic, canonical, digest, file_sha, inventory,
                     no_links, now, parse, read_json, require, safe_file, uid, write_json)


def verify_prepared(prepared):
    for path, expected in prepared['source_pins'].items():
        require(file_sha(path) == expected, 'Validated tool source changed; create a new preview', 'integrity')
    root = Path(prepared['input_root'])
    actual = {name: value for name, value in inventory(root).items()
              if not (name.startswith('registry/') and (name.endswith(('.sqlite', '.sqlite-wal', '.sqlite-shm')) or '/.registry.lock' in name))}
    require(actual == prepared['input_files'], 'Validated input changed; no inference was launched', 'integrity')
    for name, expected in prepared['input_files'].items():
        path = no_links(root / name)
        require(path.is_relative_to(root) and path.stat().st_size == expected['size'] and file_sha(path) == expected['sha256'],
                'Validated input changed; no inference was launched', 'integrity')


def validation(store, batch_id, config, compiler=inputs.native_compile):
    batch = store.read('batch', batch_id)
    if batch['state'] != 'validating':
        return
    is_md = batch.get('_workflow') == 'md'
    is_binder = batch.get('_workflow') == 'bindcraft'
    combinations = batch['pairs'] if is_md or is_binder else inputs.pairs(store, store.actor(batch_id), batch['_request'])
    with store.transaction() as db:
        batch = store.get(db, 'batch', batch_id)
        if batch['state'] != 'validating':
            return
        batch['pairs'] = combinations
        store.put(db, 'batch', batch)
    for pair in combinations:
        with store.transaction() as db:
            current = store.get(db, 'batch', batch_id)
            if current['state'] != 'validating':
                return
            entry = next(p for p in current['pairs'] if p['pair_id'] == pair['pair_id'])
            entry['state'] = 'validating'; store.put(db, 'batch', current)
        try:
            if is_binder:
                from .binder_api import validate_batch
                prepared = validate_batch(store, batch, config)
            elif is_md:
                from md.gateway import validate_batch
                prepared = validate_batch(store, batch, config)
            else:
                prepared = inputs.prepare_pair(store, batch, pair, config, compiler)
            outcome = {'state': 'compatible', 'reasons': [], '_prepared': prepared}
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            reason = str(exc)[-3000:]
            outcome = {'state': 'rejected', 'reasons': [reason]}
        with store.transaction() as db:
            current = store.get(db, 'batch', batch_id)
            if current['state'] != 'validating':
                return
            entry = next(p for p in current['pairs'] if p['pair_id'] == pair['pair_id'])
            entry.update(outcome)
            store.put(db, 'batch', current)
            store.event(db, batch_id, 'pair_validated', {'pair_id': pair['pair_id'], **{k: v for k, v in outcome.items() if not k.startswith('_')}})
    with store.transaction() as db:
        current = store.get(db, 'batch', batch_id)
        if current['state'] == 'validating':
            current['state'] = 'validated'; store.put(db, 'batch', current)


def resident_request(root, job_id, token):
    receipt = root / 'resident-request.json'
    if not receipt.exists():
        return None
    data = read_json(receipt)
    require(data['workbench_owner'] == {'job_id': job_id, 'token': token}, 'Resident ownership receipt differs', 'integrity')
    require(re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', data['request_id']) is not None, 'Invalid resident request ID', 'integrity')
    return data


MSA_PHASES = {'starting': 'private MSA startup', 'warming': 'private MSA warm-up',
              'ready': 'private MSA ready', 'waiting': 'private MSA waiting',
              'failed': 'private MSA failed'}
MSA_MARKER = re.compile(r'^BIO_MSA_SESSION_STAGE (starting|warming|ready|waiting|failed) ([^\r\n]{1,4096})$', re.M)
MSA_LOG_PREFIX = '[private MSA progress] '


def log_tail(log):
    """Read only a bounded tail of the runner's explicitly owned log paths."""
    if log is None or not log.exists():
        return '', 0
    with safe_file(log).open('rb') as stream:
        size = stream.seek(0, 2); offset = max(0, size - 32768)
        stream.seek(offset)
        raw = stream.read(32768)
        modified = os.fstat(stream.fileno()).st_mtime_ns
    if offset:
        # Never interpret a suffix of a truncated line as a complete marker.
        raw = raw.partition(b'\n')[2]
    return raw.decode(errors='replace'), modified


def session_stages(text, modified):
    """Markers describe progress only; malformed output never controls jobs."""
    events = []
    for match in MSA_MARKER.finditer(text):
        if len(match.group(0).encode()) > 4096:
            continue
        try:
            event = parse(match.group(2))
            if not isinstance(event, dict) or not set(event) <= {'message', 'session_id', 'code', 'timestamp_ns'}:
                continue
            message = event.get('message')
            timestamp = event.get('timestamp_ns', modified)
            if (not isinstance(message, str) or not 0 < len(message) <= 2048
                    or any(ord(char) < 32 or 127 <= ord(char) < 160 or 0xD800 <= ord(char) <= 0xDFFF
                           for char in message)
                    or type(timestamp) is not int or not 0 < timestamp < 2**63):
                continue
            if 'session_id' in event and (not isinstance(event['session_id'], str)
                    or re.fullmatch(r'[a-f0-9]{32}', event['session_id']) is None):
                continue
            if 'code' in event and (not isinstance(event['code'], str)
                    or re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', event['code']) is None):
                continue
        except (ValueError, RecursionError):
            continue
        events.append({**event, 'stage': match.group(1), 'timestamp_ns': timestamp, 'end': match.end()})
    return events


def log_observation(log, msa_progress=None):
    text, modified = log_tail(log)
    main = session_stages(text, modified)
    side_text, side_modified = log_tail(msa_progress)
    side = session_stages(side_text, side_modified)
    latest = max(([main[-1]] if main else []) + ([side[-1]] if side else []),
                 key=lambda event: event['timestamp_ns'], default=None)
    return text, modified, main, latest


def native_phase(text, generic_msa=True):
    text = '\n'.join(line for line in text.splitlines() if not line.startswith(
        (MSA_LOG_PREFIX, telemetry.MIRROR_PREFIX, telemetry.PREFIX)))
    if 'resident request' in text:
        return 'resident inference', 'Native resident request submitted; waiting for its durable result'
    binder_stages = []
    for pattern, phase in (
            (r'^Starting trajectory: [^\r\n]{1,250}$', 'binder design'),
            (r'^Stage [1-4]: (?:Test Logits|Additional Logits Optimisation|Softmax Optimisation|One-hot Optimisation|PSSM Semigreedy Optimisation)$', 'binder design'),
            (r'^Fixing interface residues: [^\r\n]{1,4096}$', 'binder redesign and validation'),
            (r'^Unmet filter conditions for [^\r\n]{1,250}$', 'binder candidate filtering'),
            (r'^Base AF2 filters not passed for [A-Za-z0-9_.-]{1,250}, skipping interface scoring$', 'binder candidate filtering'),
            (r'^Found [0-9]+ MPNN designs passing filters$', 'binder candidate filtering')):
        binder_stages.extend((match.start(), phase, match.group(0)) for match in re.finditer(pattern, text, re.M))
    if binder_stages:
        _, phase, message = max(binder_stages)
        return phase, message
    md_phases = re.findall(r'BIO_MD_STAGE ([A-Za-z0-9_.-]+) (starting|complete|failed|interrupted)', text)
    if md_phases:
        name, state = md_phases[-1]
        phase = ('worker setup' if any(s in name for s in ('grompp', 'topology', 'mutate', 'gentop', 'geometry', 'configure', 'prepare')) else
                 'MD analysis' if any(s in name for s in ('analysis', 'analyze', 'ddg', 'compare', 'pmf')) else
                 'MD equilibration' if any(s in name for s in ('minimiz', '_min', '_em', '_nvt', '_npt', 'equil')) else 'MD sampling')
        return phase, f'MD stage {name}: {state}'
    rules = [('prefilter', 'MSA search'), *([('MSA', 'MSA preparation')] if generic_msa else []), ('checkpoint', 'model initialization'),
             ('Inference', 'inference'), ('diffusion', 'inference'), ('sampling', 'inference'),
             ('removing ', 'worker cleanup'), ('result retrieval', 'result transfer')]
    for word, phase in reversed(rules):
        if word in text:
            return phase, phase[0].upper() + phase[1:] + ' observed in native log'
    return None


def observe_phase(log, msa_progress=None, *, native_modified=None):
    text, modified, main, latest = log_observation(log, msa_progress)
    if latest:
        # A later native phase clears the prerequisite wait. The sidechannel is
        # necessary when RF3 captures its child stderr in a preparation log;
        # no cache source change or nested path discovery is required.
        downstream = native_phase(text[main[-1]['end']:] if main else text, generic_msa=False)
        actual_modified = modified if native_modified is None else native_modified
        if downstream and actual_modified > latest['timestamp_ns'] and latest['stage'] != 'failed':
            return downstream
        return MSA_PHASES[latest['stage']], latest['message']
    if not log.exists():
        return 'starting', 'Starting the validated submission'
    native = native_phase(text)
    if native:
        return native
    return 'running', 'Managed submission is running; native logs are available'


class BinderProgress:
    """Accumulate bounded observations from complete native stdout lines."""
    def __init__(self):
        self.offset, self.partial = 0, b''
        self.current = None
        self.unavailable = None
        self.native_stage = None
        self.stage_event = None
        self.stage_identity = None
        self.started, self.completed, self.rejected, self.accepted = set(), set(), set(), {}
        self.rejection_screens = {'base_af2': set(), 'final_filters': set()}

    def observe(self, log):
        if self.unavailable:
            return {'source': 'native_stdout', 'state': 'unavailable', 'message': self.unavailable}
        with safe_file(log).open('rb') as stream:
            size = stream.seek(0, 2)
            if size < self.offset:
                self.unavailable = 'Native log was truncated; counters are unavailable'
                self.native_stage = None
                return {'source': 'native_stdout', 'state': 'unavailable', 'message': self.unavailable}
            stream.seek(self.offset); raw = stream.read(65536); self.offset += len(raw)
        lines = (self.partial + raw).split(b'\n')
        self.partial = lines.pop()[-8192:]
        for raw_line in lines:
            if len(raw_line) > 8192:
                continue
            line = raw_line.decode(errors='replace')
            events = telemetry.events(line + '\n')
            if events:
                event = events[-1]
                if self.stage_event is None or event['timestamp_ns'] >= self.stage_event['timestamp_ns']:
                    identity = tuple(event.get(key) for key in ('scope', 'stage', 'stage_id'))
                    if (identity != self.stage_identity or event['state'] != 'running' or
                            identity[:2] != ('gpu', 'inference')):
                        self.native_stage = None
                    self.stage_event, self.stage_identity = event, identity
                continue
            match = re.fullmatch(r'Starting trajectory: ([A-Za-z0-9_.-]{1,250})', line)
            if match:
                self.current = match.group(1); self.started.add(self.current)
            match = re.fullmatch(r'Unmet filter conditions for ([A-Za-z0-9_.-]{1,250})', line)
            if match:
                self.rejected.add(match.group(1))
                self.rejection_screens['final_filters'].add(match.group(1))
            match = re.fullmatch(r'Base AF2 filters not passed for ([A-Za-z0-9_.-]{1,250}), skipping interface scoring', line)
            if match:
                self.rejected.add(match.group(1))
                self.rejection_screens['base_af2'].add(match.group(1))
            match = re.fullmatch(r'Found ([0-9]{1,6}) MPNN designs passing filters', line)
            if match and self.current:
                self.accepted[self.current] = max(self.accepted.get(self.current, 0), int(match.group(1)))
            match = re.fullmatch(r'Design and validation of trajectory ([A-Za-z0-9_.-]{1,250}) took: .{1,200}', line)
            if match:
                self.completed.add(match.group(1))
            native = native_phase(line, generic_msa=False)
            if (self.current and native and native[0].startswith('binder ') and
                    self.stage_identity and self.stage_identity[:2] == ('gpu', 'inference') and
                    self.stage_event['state'] == 'running'):
                self.native_stage = native
            if len(self.started) + len(self.rejected) > 100000:
                self.unavailable = 'Native progress exceeds counter bounds; inspect final tables'
                self.native_stage = None
                return {'source': 'native_stdout', 'state': 'unavailable', 'message': self.unavailable}
        if not self.started:
            return None
        return {'source': 'native_stdout', 'attempts_started': len(self.started),
                'trajectories_completed': len(self.completed), 'candidates_accepted': sum(self.accepted.values()),
                'candidates_rejected': len(self.rejected), 'current_trajectory': self.current,
                'rejection_screens': {key: len(values) for key, values in self.rejection_screens.items()},
                'log_caught_up': self.offset == size,
                'counts_scope': 'Observed native milestones; final sealed candidate tables are authoritative'}


class JobProgress:
    """Expose nested preparation progress without treating our log mirror as
    new native work. A child append changes the size and releases that override.
    """
    def __init__(self, log, msa_progress, worker_progress=None):
        self.log, self.msa_progress = log, msa_progress
        self.worker_progress = worker_progress
        self.last_event = None
        self.last_worker_event = None
        self.mirror_signature = None
        self.native_modified = 0
        self.binder = BinderProgress()

    def _main_stat(self):
        stat = safe_file(self.log).stat()
        signature = (stat.st_size, stat.st_mtime_ns)
        if signature != self.mirror_signature:
            self.native_modified = stat.st_mtime_ns
        return stat

    def observe(self):
        phase, detail = self.details()
        return phase, detail['message']

    def details(self):
        phase, detail = self._details()
        binder = self.binder.observe(self.log)
        if binder:
            detail['binder'] = binder
            # The enclosing worker heartbeat proves inference is still active;
            # it must not erase the last explicit native BindCraft substage.
            # Retain it only for that exact, fresh stage identity, after the
            # incremental reader caught up. Completion, another stage/run,
            # stale telemetry or unavailable log evidence cannot inherit it.
            if (binder.get('log_caught_up') and self.binder.native_stage and
                    tuple(detail.get(key) for key in ('scope', 'stage', 'stage_id')) == self.binder.stage_identity and
                    detail.get('stage_state') == 'running' and detail.get('stale') is False):
                phase, message = self.binder.native_stage
                detail.update(message=message, eta=telemetry.unknown('No measured native substage duration is available'))
        return phase, detail

    def _details(self):
        self._main_stat()
        phase, message = observe_phase(self.log, self.msa_progress, native_modified=self.native_modified)
        text, _, _, msa = log_observation(self.log, self.msa_progress)
        worker_text, _ = log_tail(self.worker_progress)
        history = telemetry.events(text) + telemetry.events(worker_text)
        event = max(history, key=lambda value: value['timestamp_ns'], default=None)
        newer_msa = (msa and event and msa['timestamp_ns'] > event['timestamp_ns']
                     and (event['scope'] != 'msa' or msa['stage'] in {'ready', 'failed'}))
        if event is None or newer_msa:
            result = {'message': message, 'eta': telemetry.unknown()}
            if msa and phase in MSA_PHASES.values():
                result['timestamp_ns'] = msa['timestamp_ns']
            return phase, telemetry.freshness(result)
        # A direct marker was written just after its timestamp, so the log's
        # mtime alone cannot distinguish it from later native output. Ignore
        # native words before this exact event (including its own mirror).
        lines = text.splitlines(keepends=True)
        after = 0
        for index, line in enumerate(lines):
            raw = line.removeprefix(telemetry.MIRROR_PREFIX)
            values = telemetry.events(raw)
            if values and values[-1]['timestamp_ns'] >= event['timestamp_ns']:
                after = index + 1
        downstream = native_phase(''.join(lines[after:]), generic_msa=False)
        if downstream and self.native_modified > event['timestamp_ns'] and event['state'] != 'failed':
            return downstream[0], {'message': downstream[1], 'eta': telemetry.unknown()}
        stage = 'waiting for capacity' if event['stage'] == 'waiting_capacity' else event['stage'].replace('_', ' ')
        phase = ('private MSA ' if event['scope'] == 'msa' else '') + stage
        return phase, telemetry.view(event, history)

    def forward(self):
        text, modified = log_tail(self.msa_progress)
        events = session_stages(text, modified)
        if events:
            event = events[-1]
            payload = {key: value for key, value in event.items() if key not in {'stage', 'end'}}
            line = b'BIO_MSA_SESSION_STAGE ' + event['stage'].encode() + b' ' + canonical(payload)
            if line != self.last_event:
                self.last_event = line
                self._mirror(line, MSA_LOG_PREFIX)
        worker_text, _ = log_tail(self.worker_progress)
        values = telemetry.events(worker_text)
        if values:
            line = telemetry.PREFIX.encode() + canonical(values[-1])
            if line != self.last_worker_event:
                self.last_worker_event = line
                self._mirror(line, telemetry.MIRROR_PREFIX)

    def _mirror(self, line, prefix):
        main_text, _ = log_tail(self.log)
        if line.decode() in main_text:
            return
        before = self._main_stat()
        entry = prefix.encode() + line + b'\n'
        fd = os.open(self.log, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            require(os.write(fd, entry) == len(entry), 'Incomplete owned progress log append', 'integrity')
            after = os.fstat(fd)
        finally:
            os.close(fd)
        # If the child wrote concurrently, its append is genuine new native
        # output. Otherwise ignore just our mirror's later modification time.
        self.mirror_signature = ((after.st_size, after.st_mtime_ns)
                                 if after.st_size == before.st_size + len(entry) else None)


def submission_error(log, status, msa_progress=None, worker_progress=None):
    text, _, _, latest = log_observation(log, msa_progress)
    worker_text, _ = log_tail(worker_progress)
    event = max(telemetry.events(text) + telemetry.events(worker_text),
                key=lambda value: value['timestamp_ns'], default=None)
    error = {'message': 'Submission exited with status ' + str(status), 'automatic_retry': False}
    if event and event['state'] == 'failed' and (latest is None or event['timestamp_ns'] >= latest['timestamp_ns']):
        error.update(message=event['message'], stage=event['stage'], scope=event['scope'])
    elif latest and latest['stage'] == 'failed':
        error.update(message='Private MSA prerequisite failed: ' + latest['message'],
                     prerequisite='private_msa')
        for field in ('code', 'session_id'):
            if field in latest:
                error[field] = latest[field]
    else:
        # Known human-readable entrypoint errors remain useful even if the
        # session was already ready and a later native preparation step failed.
        causes = re.findall(r'^(?:msa-session-client|bio-msa|rf3-msa|prepared|bio-submit): ([^\r\n]{1,3000})$', text, re.M)
        if causes:
            error['message'] = causes[-1]
    return error


def cancel_resident(config, receipt):
    tools = str(Path(config['tools_dir']))
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from inference.job_queue import Queue
    from inference.common import digest as native_digest
    queue = Queue(Path(config['inference_state']) / 'jobs.sqlite')
    row = queue.get(receipt['request_id'])
    if row is None and receipt['state'] == 'intent':
        return {'state': 'intent'}
    require(row is not None and row['payload'].get('provenance', {}).get('workbench_owner') == receipt['workbench_owner'],
            'Resident request is not owned by this workbench execution', 'integrity')
    require(native_digest(row['payload']) == receipt['request_sha256'], 'Resident request payload binding differs', 'integrity')
    return queue.cancel_queued(receipt['request_id'])


def request_cancel(root, job_id, token, config):
    """Fence future resident enqueue before signalling an owned cold client."""
    path = no_links(root / 'resident-enqueue.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        marker = root / 'resident-cancel.json'
        owner = {'job_id': job_id, 'token': token}
        if not marker.exists():
            write_json(marker, {'workbench_owner': owner, 'requested_at': now()}, exclusive=True)
        else:
            require(read_json(marker)['workbench_owner'] == owner, 'Cancellation marker ownership differs', 'integrity')
        receipt = resident_request(root, job_id, token)
        return (receipt, cancel_resident(config, receipt)) if receipt else (None, None)
    finally:
        os.close(fd)


def owned_resident(config, receipt):
    tools = str(Path(config['tools_dir']))
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from inference.job_queue import Queue
    from inference.common import digest as native_digest
    queue = Queue(Path(config['inference_state']) / 'jobs.sqlite')
    row = queue.get(receipt['request_id'])
    if row is None and receipt['state'] == 'intent':
        return queue, {'id': receipt['request_id'], 'state': 'cancelled', 'never_enqueued': True}
    require(row is not None and row['payload'].get('provenance', {}).get('workbench_owner') == receipt['workbench_owner']
            and native_digest(row['payload']) == receipt['request_sha256'], 'Resident reconciliation ownership differs', 'integrity')
    return queue, row


def reconcile_resident(store, job):
    """Continue tracking an owned native request after its waiting client exits.

    This runs from the persistent head daemon, including after daemon restart.
    It performs no search, model call, new allocation, or automatic retry.
    """
    pending = job['_resident_pending']
    config, receipt = pending['config'], pending['receipt']
    queue, row = owned_resident(config, receipt)
    if row['state'] == 'queued':
        row = queue.cancel_queued(row['id'])
    root = store.directory('jobs', job['job_id'])
    write_json(root / 'resident-reconciliation.json', {'observed_at': now(), 'request': row,
                 'client_exit_code': pending['client_exit_code']})
    if row['state'] in {'running', 'predicted'}:
        with store.transaction() as db:
            current = store.get(db, 'job', job['job_id'])
            current['phase'] = 'resident execution continues' if row['state'] == 'running' else 'native output processing'
            current['progress'] = {'message': 'The waiting client ended; the owned native request is still tracked and its eventual outputs will be retained', 'observed_at': now()}
            store.put(db, 'job', current)
        return False
    require(row['state'] in {'complete', 'failed', 'interrupted', 'cancelled'}, 'Unknown resident reconciliation state', 'integrity')
    results = root / 'results'
    if row['state'] == 'complete':
        verify_prepared(job['_prepared'])
        from inference.common import verify_inventory as verify_native, atomic_json
        destination = results / row['id']
        if destination.exists():
            saved = read_json(destination / '.workbench-recovery.json')
            require(saved['request_sha256'] == receipt['request_sha256'], 'Recovered output belongs to another request', 'integrity')
            actual = {k: v for k, v in inventory(destination).items() if k != '.workbench-recovery.json'}
            require(actual == saved['files'], 'Recovered result changed', 'integrity')
        else:
            stage_root = root / ('recovery-transfer-' + uid())
            stage_root.mkdir(mode=0o700)
            stage = stage_root / 'result'
            if job['model'] == 'rf3':
                from inference.frontend import publish_rf3
                publish_rf3(row, row['payload'], stage)
            else:
                verify_native(row['result']['output_dir'], row['result']['files'], exact=True)
                shutil.copytree(row['result']['output_dir'], stage)
                atomic_json(stage / 'resident-result.json', row, exclusive=True)
                atomic_json(stage / 'job.json', row['payload'], exclusive=True)
            write_json(stage / '.workbench-recovery.json', {'request_sha256': receipt['request_sha256'], 'files': inventory(stage)}, exclusive=True)
            os.rename(stage, destination)
        state = 'complete'
    else:
        terminal = results / 'resident-terminal.json'
        if terminal.exists():
            require(read_json(terminal) == row, 'Resident terminal receipt changed', 'integrity')
        else:
            write_json(terminal, row, exclusive=True)
        state = 'cancelled' if row['state'] == 'cancelled' and job['state'] == 'cancel_requested' else 'interrupted' if row['state'] == 'cancelled' else row['state']
    artifacts = seal_results(store, job, results, 'resident-recovered')
    with store.transaction() as db:
        current = store.get(db, 'job', job['job_id'])
        current.update(state=state, phase=state, finished_at=now(), exit_code=0 if state == 'complete' else pending['client_exit_code'])
        current.pop('_resident_pending', None)
        current['provenance'].update(resident_reconciled=True, client_exit_code=pending['client_exit_code'],
                                     artifacts_sha256=digest(artifacts))
        if state != 'complete':
            current['error'] = {'message': 'Owned resident request ended as ' + row['state'], 'automatic_retry': False}
        elif current.get('error'):
            current['error'] = None
        store.put(db, 'job', current)
        store.event(db, job['job_id'], 'resident_reconciled', {'state': state, 'client_exit_code': pending['client_exit_code']})
    return True


def seal_results(store, job, root, source='native'):
    """Copy stopped execution artifacts; never trust a changing source tree."""
    root = no_links(root)
    before = inventory(root)
    actor = store.actor(job['job_id'])
    with store.connection() as db:
        from .common import parse
        existing = [parse(row['data']) for row in db.execute("SELECT data FROM objects WHERE kind='artifact' AND actor=? AND json_extract(data,'$.job_id')=?", (actor, job['job_id']))]
    existing = {a['name']: a for a in existing if a.get('_source') == source}
    results = []
    for relative, expected in before.items():
        if job['model'] == 'md' and 'input-bundle' in Path(relative).parts:
            # The simulation retains original inputs and the bundle manifest;
            # avoid archiving another entire copy of checkpoint-resume payloads.
            continue
        if relative.endswith(('.pyc', '.lock')):
            continue
        if relative in existing:
            old = existing[relative]
            require(old['sha256'] == expected['sha256'] and old['size'] == expected['size'], 'Previously sealed output changed', 'integrity')
            require(file_sha(store.directory('artifacts', old['artifact_id']) / 'content') == expected['sha256'], 'Retained artifact changed', 'integrity')
            results.append(old)
            continue
        ident = uid(); destination = store.directory('artifacts', ident)
        destination.mkdir(parents=True, mode=0o700)
        original = safe_file(root / relative)
        with original.open('rb') as incoming, (destination / 'content').open('xb') as outgoing:
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            outgoing.flush(); os.fsync(outgoing.fileno())
        os.chmod(destination / 'content', 0o400)
        require(file_sha(destination / 'content') == expected['sha256'], 'Output changed during archival', 'integrity')
        extension = original.suffix.lower().lstrip('.')
        excluded = {'prepared', 'prepared-bundle', 'features', 'template_data', 'template_structures', 'templates', 'library-input', 'native-input', 'input-bundle', 'inputs'}
        input_file = any(part in excluded for part in Path(relative).parts[:-1]) or original.stem.lower() in {'input', 'source', 'native-input'}
        known_output = (job['model'] == 'rf3' or 'predictions' in original.parts or
                        (job['model'] == 'bindcraft' and 'designs' in Path(relative).parts) or
                        (job['model'] == 'md' and original.name == 'final.pdb' and 'simulation' in original.parts) or
                        (job['model'] == 'openfold3' and re.search(r'_seed_\d+_sample_\d+_model$', original.stem)) or
                        (job['model'] == 'rfdiffusion' and original.stem.startswith('design')))
        role = ('target_structure' if source == 'binder-input' and extension in {'pdb', 'cif', 'mmcif'} else
                'structure' if extension in {'cif', 'mmcif', 'pdb'} and not input_file and known_output else
                'log' if extension == 'log' else 'confidence' if 'confidence' in original.name else 'provenance' if extension == 'json' else 'data')
        artifact = {'artifact_id': ident, 'job_id': job['job_id'], 'name': relative, 'size': expected['size'],
                    'sha256': expected['sha256'], 'media_type': mimetypes.guess_type(original.name)[0] or 'application/octet-stream',
                    'format': extension, 'role': role, 'model': job['model'],
                    'sample_id': str(Path(relative).with_suffix('')) if role == 'structure' else None,
                    'confidence': None, 'qa': None, '_source': source}
        if role == 'structure':
            from .artifact_metadata import structure_metadata
            artifact.update(structure_metadata(original, root, job['model']))
        results.append(artifact)
    require(inventory(root) == before, 'Output tree changed during archival', 'integrity')
    with store.transaction() as db:
        for artifact in results:
            store.put(db, 'artifact', artifact, actor)
    return [{'artifact_id': a['artifact_id'], 'sha256': a['sha256'], 'size': a['size'], 'name': a['name']} for a in results]


def run_job(store, job_id, config):
    job = store.read('job', job_id)
    require(job['state'] in {'starting', 'cancel_requested'}, 'Job is not admitted to this execution', 'conflict')
    root = store.directory('jobs', job_id)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared = job['_prepared']; verify_prepared(prepared)
    if job['model'] == 'bindcraft':
        from .binder_results import seal_inputs
        seal_inputs(store, job, root)
    results = root / 'results'; results.mkdir(mode=0o700, exist_ok=False)
    log = root / 'run.log'
    msa_progress = root / 'msa-session-progress.log'
    worker_progress = root / 'worker-progress.log'
    for path in (msa_progress, worker_progress):
        progress_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(progress_fd)
    cancelled = False
    process = None
    def signal_cancel(signum, frame):
        nonlocal cancelled
        cancelled = True
    old = {sig: signal.signal(sig, signal_cancel) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        with store.transaction() as db:
            current = store.get(db, 'job', job_id)
            if current['state'] == 'cancel_requested':
                current.update(state='cancelled', finished_at=now(), phase='cancelled')
                store.put(db, 'job', current)
                return
            current.update(state='running', started_at=now(), phase='starting'); store.put(db, 'job', current)
        env = os.environ.copy()
        # The actor and forced-command transport have no authority over launch
        # options. Only trusted config and validated generated paths enter env.
        env.pop('BIO_WORKBENCH_ACTOR', None)
        env.update(BIO_RESULTS_DIR=str(results), BIO_TOOLS_SRC=prepared['tools_dir'], PYTHONUNBUFFERED='1')
        env.update(prepared['environment'])
        # Only this waiting caller writes progress. The shared MSA lifecycle is
        # a separate owned service, never a member of this submission's group.
        env['BIO_MSA_PROGRESS_LOG'] = str(msa_progress)
        env['BIO_WORKER_PROGRESS_LOG'] = str(worker_progress)
        token = uid()
        write_json(root / 'resident-binding.json', {'schema': 1, 'job_id': job_id, 'token': token}, exclusive=True)
        env['BIO_WORKBENCH_BINDING_FILE'] = str(root / 'resident-binding.json')
        write_json(root / 'submission.json', {'argv': prepared['argv'], 'environment': {k: env[k] for k in ('BIO_RESULTS_DIR', 'BIO_TOOLS_SRC', 'BIO_LIBRARY_ROOT') if k in env},
                                             'prepared_sha256': digest(prepared), 'started_at': now()}, exclusive=True)
        with log.open('ab', buffering=0) as stream:
            process = subprocess.Popen(prepared['argv'], env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            write_json(root / 'head-process.json', {'pid': process.pid, 'start_ticks': Path(f'/proc/{process.pid}/stat').read_text().rsplit(')', 1)[1].split()[19],
                        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'created_at': now()}, exclusive=True)
            sent_term = None; resident = None; resident_cancelled = False
            progress = JobProgress(log, msa_progress, worker_progress)
            deadline = time.monotonic() + prepared['timeout'] + preparation_allowance(prepared) + 1200
            while process.poll() is None:
                current = store.read('job', job_id)
                cancelled = cancelled or current['state'] == 'cancel_requested'
                resident = resident_request(root, job_id, token) or resident
                phase, detail = progress.details()
                message = detail['message']
                progress.forward()
                row = None
                if cancelled:
                    resident, row = request_cancel(root, job_id, token, config)
                if cancelled and resident:
                    resident_cancelled = row['state'] == 'cancelled'
                    phase = 'cancellation requested'
                    message = ('Resident request cancelled before claim' if resident_cancelled else
                               'Resident enqueue was cancelled' if row['state'] == 'intent' else
                               'Resident prediction already claimed; its bounded execution is finishing and outputs will be retained')
                    # Never kill the waiting client for an already claimed
                    # durable request, or the shared resident worker.
                elif cancelled:
                    if sent_term is None:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        sent_term = time.monotonic()
                    phase = 'cancellation requested'
                    message = 'Cancellation signalled to the exact owned submission; waiting for managed cleanup'
                if sent_term is not None and time.monotonic() - sent_term > 840:
                    raise Error('unavailable', 'Owned submission cleanup exceeded its stop bound; retained for operator reconciliation')
                require(time.monotonic() < deadline, 'Submission exceeded its owned work and cleanup bound', 'unavailable')
                with store.transaction() as db:
                    current = store.get(db, 'job', job_id)
                    if cancelled:
                        current['state'] = 'cancel_requested'
                    current['phase'] = phase
                    current['progress'] = ({'message': message, 'eta': telemetry.unknown('Cancellation is being reconciled')}
                                           if cancelled else detail)
                    current['progress']['observed_at'] = now()
                    if resident:
                        current['provenance']['resident_job_id'] = resident['request_id']
                    store.put(db, 'job', current)
                time.sleep(1)
        status = process.returncode
        # A failed prerequisite can exit between polls. Retain its cause in the
        # ordinary job log before sealing evidence or resident reconciliation.
        progress.forward()
        latest_receipt = resident_request(root, job_id, token)
        if status != 0 and latest_receipt is not None:
            # Preserve any interrupted client copy separately. The authoritative
            # native result is fetched later only from its verified terminal
            # inventory, never from a partially copied directory.
            os.rename(results, root / 'client-partial')
            results.mkdir(mode=0o700)
            write_json(root / 'client-exit.json', {'exit_code': status, 'finished_at': now(),
                         'resident_receipt': latest_receipt}, exclusive=True)
            with store.transaction() as db:
                current = store.get(db, 'job', job_id)
                current.update(state='cancel_requested' if cancelled else 'running', phase='resident reconciliation')
                current['_resident_pending'] = {'receipt': latest_receipt, 'config': {**config, 'tools_dir': prepared['tools_dir']}, 'client_exit_code': status}
                current['provenance'].update(resident_job_id=latest_receipt['request_id'], client_exit_code=status)
                current['progress'] = {'message': 'Waiting client ended; reconciling the owned durable native request', 'observed_at': now()}
                store.put(db, 'job', current)
            # The head process has stopped. Its logs and partial bytes remain
            # durable, while the daemon continues native-request reconciliation.
            return
        verify_prepared(prepared)
        artifacts = seal_results(store, job, results)
        # Retain the head log and submit binding separately from native files.
        head = root / 'head-evidence'; head.mkdir(mode=0o700)
        for name in ('run.log', 'submission.json', 'head-process.json', 'msa-session-progress.log', 'worker-progress.log'):
            shutil.copyfile(root / name, head / name)
        head_artifacts = seal_results(store, job, head, 'head')
        artifacts += head_artifacts
        binder_observations = progress.binder.observe(log) if job['model'] == 'bindcraft' else None
        state = 'complete' if status == 0 else 'cancelled' if cancelled else 'failed'
        error = None if status == 0 else ({'message': 'Submission cancelled', 'automatic_retry': False}
                                        if cancelled else submission_error(log, status, msa_progress, worker_progress))
        with store.transaction() as db:
            current = store.get(db, 'job', job_id)
            current.update(state=state, exit_code=status, finished_at=now(), phase=state,
                           error=error)
            if error:
                current['progress'] = {'message': error['message'], 'observed_at': now()}
            else:
                current['progress'] = {'message': 'Run completed; outputs retained', 'observed_at': now()}
            if binder_observations:
                current['progress']['binder'] = binder_observations
                log_artifact = next(a for a in head_artifacts if a['name'] == 'run.log')
                current['provenance']['binder_observations'] = {
                    **binder_observations, 'log_artifact_id': log_artifact['artifact_id'],
                    'log_sha256': log_artifact['sha256']}
            if cancelled and status == 0:
                current['provenance']['cancel_requested_but_prediction_finished'] = True
            current['provenance']['artifacts_sha256'] = digest(artifacts)
            store.put(db, 'job', current); store.event(db, job_id, 'terminal', {'state': state, 'exit_code': status})
        write_json(root / 'result.json', {'job_id': job_id, 'state': state, 'exit_code': status, 'artifacts': artifacts, 'finished_at': now()}, exclusive=True)
    finally:
        for sig, previous in old.items():
            signal.signal(sig, previous)
        if process is not None and process.poll() is None:
            # Preserve uncertainty. The owning unit's mixed-stop grace and the
            # existing cloud watchdog remain authoritative; no unbound delete.
            with store.transaction() as db:
                current = store.get(db, 'job', job_id)
                current.update(state='interrupted', phase='cleanup reconciliation',
                               error={'message': 'Head runner interrupted with an owned process still present', 'automatic_retry': False})
                store.put(db, 'job', current)


def execute(store, kind, ident, intent_sha256, config):
    root = store.directory('operations', ident)
    intent = read_json(root / 'intent.json')
    require(file_sha(root / 'intent.json') == intent_sha256 and intent['object_id'] == ident and intent['kind'] == kind,
            'Execution intent binding failed', 'integrity')
    require(file_sha(root / 'config.json') == intent['config_sha256'], 'Trusted execution configuration changed', 'integrity')
    invocation = os.environ.get('INVOCATION_ID', '')
    require(re.fullmatch('[a-f0-9]{32}', invocation) is not None, 'Owned systemd InvocationID required', 'integrity')
    receipt = {'kind': kind, 'object_id': ident, 'unit': intent['unit'], 'invocation_id': invocation,
               'intent_sha256': intent_sha256, 'started_at': now()}
    write_json(root / 'started.json', receipt, exclusive=True)
    with store.transaction() as db:
        row = db.execute('SELECT * FROM operations WHERE object_id=?', (ident,)).fetchone()
        require(row is not None and row['intent_sha256'] == intent_sha256 and row['state'] == 'intent', 'Execution was already started or differs from its intent', 'conflict')
        db.execute("UPDATE operations SET invocation_id=?,state='running',updated=? WHERE object_id=?", (invocation, now(), ident))
    try:
        if kind == 'validation':
            validation(store, ident, config)
        else:
            run_job(store, ident, config)
        receipt.update(state='complete', finished_at=now())
    except BaseException as exc:
        receipt.update(state='failed', error=str(exc)[-4000:], finished_at=now())
        with store.transaction() as db:
            object_kind = 'batch' if kind == 'validation' else 'job'
            data = store.get(db, object_kind, ident)
            if data['state'] not in TERMINAL:
                data['state'] = 'validation_failed' if kind == 'validation' else 'interrupted'
                if kind == 'validation':
                    data['errors'].append(str(exc)[-3000:])
                else:
                    data['error'] = {'message': str(exc)[-3000:], 'automatic_retry': False}
                    data['finished_at'] = now()
                store.put(db, object_kind, data)
        raise
    finally:
        write_json(root / 'terminal.json', receipt, exclusive=True)
        with store.transaction() as db:
            db.execute('UPDATE operations SET state=?,data=?,updated=? WHERE object_id=?',
                       (receipt['state'], canonical(receipt).decode(), now(), ident))
