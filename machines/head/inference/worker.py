"""Resident native model process; its durable spool needs no network API.

The supervisor may give this process a private network namespace. Inputs and
outputs travel through an already mounted shared filesystem. Each process
generation loads exactly one model/configuration and handles one job at a time.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).absolute().parent.parent))
from inference.common import atomic_json, configuration_id, digest, identifier, inventory, now, read, sha256, verify_inventory
from inference.resources import configure


class ResidentWorker:
    def __init__(self, session, *, adapter_factory=None):
        self.session = session
        self.worker_id = identifier(session['worker_id'])
        self.generation = identifier(os.environ.get('INVOCATION_ID') or uuid.uuid4().hex)
        self.config = session['config']
        self.config_id = configuration_id(self.config)
        if session['config_id'] != self.config_id:
            raise ValueError('Resident configuration hash differs')
        self.deadline = float(session['deadline_epoch'])
        if self.deadline <= now() or float(session.get('idle_seconds', 900)) <= 0:
            raise ValueError('Expired/invalid resident session')
        self.root = Path(session['spool_root']) / self.worker_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = (self.root / 'worker.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.stop = threading.Event()
        self.status = {'schema': 1, 'worker_id': self.worker_id, 'generation': self.generation,
                       'config_id': self.config_id, 'config_sha256': digest(self.config), 'state': 'loading', 'pid': os.getpid(),
                       'deadline_epoch': self.deadline, 'active': None, 'loaded': None}
        self.adapter_factory = adapter_factory
        self.adapter = None
        self.last_job = now()
        self.heartbeat_error = None
        self.publication_lock = threading.Lock()
        self.gpu_lock = None

    def publish(self):
        # Hold through publication: a slow heartbeat must not replace a newer
        # running/terminal snapshot with an older ready snapshot.
        with self.publication_lock:
            value = dict(self.status, heartbeat_epoch=now())
            atomic_json(self.root / 'status.json', value)

    def heartbeat(self):
        while not self.stop.wait(5):
            try:
                self.publish()
            except BaseException as exc:
                self.heartbeat_error = repr(exc)
                self.stop.set()

    def load(self):
        if self.session.get('gpu_lock'):
            self.gpu_lock = Path(self.session['gpu_lock']).open('a')
            fcntl.flock(self.gpu_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            active = subprocess.check_output(['nvidia-smi', '--query-compute-apps=gpu_uuid,pid',
                                              '--format=csv,noheader'], text=True, timeout=10)
            if any(line.split(',')[0].strip() == self.session['gpu_uuid'] for line in active.splitlines()):
                raise ValueError('Physical GPU became occupied before resident initialization')
        self.status['resources'] = configure(self.session.get('resources', {}))
        checkpoint = self.config.get('checkpoint', {})
        if checkpoint:
            path = Path(checkpoint['path'])
            if path.is_symlink() or sha256(path) != checkpoint['sha256']:
                raise ValueError('Pinned checkpoint differs')
        # Runtime trees are staged once, then mounted read-only by deployment.
        for name, expected in self.session.get('source_files', {}).items():
            if sha256(name) != expected:
                raise ValueError('Pinned worker source differs: ' + name)
        self.publish()
        start = now()
        if self.adapter_factory is None:
            model = self.config['model']
            if model not in ('protenix', 'openfold3', 'boltz2', 'rf3'):
                raise ValueError('Unsupported resident model')
            # BLAS/OpenMP environment has already been set before Torch import.
            import torch
            torch.set_num_threads(self.status['resources']['torch_threads'])
            torch.set_num_interop_threads(self.status['resources']['torch_interop_threads'])
            adapter_type = importlib.import_module('inference.adapters.' + model).Adapter
        else:
            adapter_type = self.adapter_factory
        self.adapter = adapter_type(json.loads(json.dumps(self.config)))
        metadata = self.adapter.load()
        self.last_job = now()
        self.status.update(state='ready', loaded={'seconds': now() - start, 'metadata': metadata})
        self.publish()

    def execute(self, request_path):
        request = read(request_path)
        token = identifier(request['token'])
        job = request['payload']
        identifier(job['id'])
        if request['worker_id'] != self.worker_id or request['generation'] != self.generation:
            raise ValueError('Request belongs to another worker generation')
        if job.get('required_worker_id') not in (None, self.worker_id):
            raise ValueError('Request requires another physical worker binding')
        if request['config_id'] != self.config_id or job['config_id'] != self.config_id:
            raise ValueError('Request/configuration mismatch')
        if request['deadline_epoch'] > self.deadline or request['deadline_epoch'] <= now():
            raise ValueError('Request expired or exceeds worker deadline')
        attempt_root = self.root / 'attempts' / token
        attempt_root.mkdir(parents=True, exist_ok=False)
        atomic_json(attempt_root / 'request.json', request, exclusive=True)
        self.status.update(state='running', active={'id': job['id'], 'token': token, 'attempt': request['attempt']})
        self.publish()
        started = now()
        output_dir = attempt_root / 'out'
        output_dir.mkdir()
        result = None
        error = None
        try:
            if job.get('input_files'):
                verify_inventory(job['input_files']['root'], job['input_files']['files'], exact=True)
            result = self.adapter.predict(json.loads(json.dumps(job)), output_dir)
            if job.get('input_files'):
                verify_inventory(job['input_files']['root'], job['input_files']['files'], exact=True)
            structures = []
            for item in result['structures']:
                path = Path(item)
                path = path if path.is_absolute() else output_dir / path
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(output_dir.resolve()):
                    raise ValueError('Adapter emitted an invalid structure path')
                structures.append(str(path.relative_to(output_dir)))
            if not structures:
                raise ValueError('Native prediction produced no structures')
            result = dict(result, structures=structures)
        except BaseException as exc:
            error = {'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc(),
                     'automatic_retry': False, 'worker_restart_required': True}
        files = inventory(output_dir)
        receipt = {'schema': 1, 'worker_id': self.worker_id, 'generation': self.generation,
                   'config_id': self.config_id, 'id': job['id'], 'attempt': request['attempt'],
                   'token': token, 'request_sha256': digest(request), 'started_epoch': started,
                   'finished_epoch': now(), 'output_dir': str(output_dir), 'files': files,
                   'result': result, 'error': error, 'status': 'failed' if error else 'predicted',
                   'load': self.status['loaded'], 'resources': self.status['resources']}
        atomic_json(attempt_root / 'result.json', receipt, exclusive=True)
        self.last_job = now()
        self.status.update(state='failed' if error else 'ready', active=None)
        self.publish()
        # Do not reuse possibly corrupted CUDA/native state after an exception.
        if error:
            self.stop.set()
        return receipt

    def run(self):
        thread = threading.Thread(target=self.heartbeat, daemon=True)
        thread.start()
        failed = False
        try:
            self.load()
            while not self.stop.is_set() and now() < self.deadline - 30:
                if now() - self.last_job > float(self.session.get('idle_seconds', 900)):
                    break
                requests = sorted((self.root / 'requests').glob('*.json'))
                for path in requests:
                    request = read(path)
                    if request.get('generation') != self.generation:
                        continue
                    if (self.root / 'attempts' / identifier(request['token']) / 'request.json').exists():
                        continue
                    if self.execute(path)['error'] is not None:
                        failed = True
                        break
                self.stop.wait(0.25)
            return 75 if failed or self.heartbeat_error else 0
        finally:
            self.stop.set()
            thread.join(timeout=6)
            self.status.update(state='failed' if failed else 'stopped', active=None)
            self.publish()
            self.lock.close()
            if self.gpu_lock is not None:
                self.gpu_lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    args = parser.parse_args()
    if sha256(args.session) != args.expected_sha256:
        raise ValueError('Worker session manifest changed')
    worker = ResidentWorker(read(args.session))
    def terminate(signum, frame):
        worker.stop.set()
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    raise SystemExit(worker.run())


if __name__ == '__main__':
    main()
