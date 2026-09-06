"""Durable head dispatcher, independent of worker and postprocessing lifetime."""
from __future__ import annotations

import concurrent.futures
import fcntl
import json
from pathlib import Path
import time

from .common import atomic_json, digest, identifier, now, read, verify_inventory
from .job_queue import Queue


class Dispatcher:
    def __init__(self, queue, registry, *, postprocessor=None, postprocess_workers=2):
        self.queue = queue
        self.registry = Path(registry)
        self.postprocessor = postprocessor
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=postprocess_workers)
        self.processing = {}

    @staticmethod
    def publish_request(root, row, status, deadline):
        request = {'schema': 1, 'worker_id': row['worker'], 'generation': row['generation'],
                   'config_id': row['config_id'], 'token': row['token'], 'attempt': row['attempt'],
                   'deadline_epoch': deadline, 'payload': row['payload']}
        path = root / 'requests' / (row['token'] + '.json')
        if path.exists():
            if read(path) != request:
                raise ValueError('Existing request differs from its durable lease')
            return
        atomic_json(path, request, exclusive=True)

    def collect(self, worker):
        root = Path(worker['spool_root']) / identifier(worker['worker_id'])
        for row in self.queue.list('running'):
            if row['worker'] != worker['worker_id']:
                continue
            path = root / 'attempts' / row['token'] / 'result.json'
            if not path.is_file():
                continue
            receipt = read(path)
            if (receipt['id'], receipt['token'], receipt['generation'], receipt['config_id']) != (
                    row['id'], row['token'], row['generation'], row['config_id']):
                raise ValueError('Result belongs to a different execution')
            output_dir = root / 'attempts' / row['token'] / 'out'
            if Path(receipt['output_dir']) != output_dir:
                raise ValueError('Unexpected worker output path')
            request = read(root / 'requests' / (row['token'] + '.json'))
            if digest(request) != receipt['request_sha256']:
                raise ValueError('Executed request changed')
            verify_inventory(output_dir, receipt['files'], exact=True)
            self.queue.finish(row['id'], row['token'], row['generation'], receipt, error=receipt['error'])

    def cpu_attempt(self, row):
        lock_path = self.queue.path.parent / 'cpu-locks' / (row['token'] + '.lock')
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            current = self.queue.get(row['id'])
            if current['state'] != 'predicted' or current['token'] != row['token']:
                return
            try:
                result = self.postprocessor(row['payload'], row['result']) if self.postprocessor else row['result']
            except Exception as exc:
                self.queue.postprocessed(row['id'], row['result'], token=row['token'],
                    error={'stage': 'postprocess', 'type': type(exc).__name__, 'message': str(exc)})
            else:
                self.queue.postprocessed(row['id'], result, token=row['token'])

    def postprocess(self):
        for job_id, future in list(self.processing.items()):
            if not future.done():
                continue
            future.result()
            del self.processing[job_id]
        for row in self.queue.list('predicted'):
            if row['id'] in self.processing:
                continue
            if self.postprocessor is None:
                if row['payload'].get('postprocess') or row['result']['result'].get('status') == 'awaiting_chemistry_validation':
                    continue
                self.cpu_attempt(row)
            else:
                self.processing[row['id']] = self.pool.submit(self.cpu_attempt, row)

    def tick(self):
        errors = []
        workers = [read(path) for path in sorted(self.registry.glob('*.json'))]
        for worker in workers:
            try:
                # Gather a terminal result before expiring the heartbeat lease.
                self.collect(worker)
                root = Path(worker['spool_root']) / identifier(worker['worker_id'])
                status_path = root / 'status.json'
                if not status_path.is_file():
                    continue
                status = read(status_path)
                if status['config_id'] != worker['config_id'] or status['worker_id'] != worker['worker_id']:
                    raise ValueError('Registered worker/configuration mismatch')
                if not -5 <= now() - status['heartbeat_epoch'] <= 30:
                    continue
                deadline = min(worker['deadline_epoch'], status['deadline_epoch'])
                if status.get('active'):
                    active = status['active']
                    self.queue.renew(active['id'], active['token'], status['generation'], deadline=deadline)
                if status['state'] != 'ready' or deadline <= now() + 120:
                    continue
                pending = [row for row in self.queue.list('running')
                           if row['worker'] == worker['worker_id'] and row['generation'] == status['generation']]
                if pending:
                    # A crash can occur between SQLite commit and spool publish.
                    # Reconcile that same execution token, never create a retry.
                    if pending[0]['lease_until'] > now():
                        self.publish_request(root, pending[0], status, deadline)
                    continue
                row = self.queue.claim(worker['config_id'], worker['worker_id'], status['generation'], deadline=deadline)
                if row is None:
                    continue
                self.publish_request(root, row, status, deadline)
            except (OSError, ValueError, KeyError) as exc:
                errors.append({'worker': worker.get('worker_id'), 'error': repr(exc)})
        self.queue.expire()
        self.postprocess()
        return {'utc_epoch': now(), 'errors': errors, 'postprocessing': len(self.processing)}

    def close(self):
        self.pool.shutdown(wait=True)
