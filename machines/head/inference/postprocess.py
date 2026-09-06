"""CPU stages run after the GPU execution lease has been released."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import signal
import subprocess
import uuid

from .common import atomic_json, digest, inventory, read, sha256, verify_inventory


def process(job, receipt):
    stage = job.get('postprocess')
    if not stage:
        if receipt['result'].get('status') == 'awaiting_chemistry_validation':
            raise ValueError('RF3 chemistry validation is required before completion')
        return receipt
    if stage['kind'] != 'pinned-command':
        raise ValueError('Unsupported CPU postprocessing stage')
    for path, expected in stage['source_files'].items():
        if sha256(path) != expected:
            raise ValueError('Postprocessor source changed: ' + path)
    for runtime in stage.get('runtime_trees', []):
        verify_inventory(runtime['root'], runtime['files'], exact=runtime.get('exact', False))
    verify_inventory(receipt['output_dir'], receipt['files'], exact=True)
    destination = Path(stage['output_dir']) / job['id'] / ('attempt-' + str(receipt['attempt']))
    destination.mkdir(parents=True, exist_ok=True)
    # The child inherits this lock. A surviving child after head failure cannot
    # race a restarted dispatcher, which waits then rechecks completion.
    with (destination / 'stage.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return locked_process(job, receipt, stage, destination, lock.fileno())


def locked_process(job, receipt, stage, destination, lock_fd):
    binding = {'job_sha256': digest(job), 'prediction_sha256': digest(receipt),
               'stage_sha256': digest(stage)}
    input_receipt = destination / 'prediction.json'
    if input_receipt.exists() and read(input_receipt) != receipt:
        raise ValueError('Postprocessing attempt has different prediction input')
    if not input_receipt.exists():
        atomic_json(input_receipt, receipt, exclusive=True)
    completion = destination / 'complete.json'
    if completion.exists():
        value = read(completion)
        detail = value['postprocess']
        if {key: detail[key] for key in binding} != binding or {key: value[key] for key in receipt} != receipt:
            raise ValueError('CPU completion identity changed')
        verify_inventory(detail['output_dir'], detail['files'], exact=True)
        if sha256(detail['result_file']) != detail['sha256'] or read(detail['result_file']) != detail['result']:
            raise ValueError('CPU result changed after completion')
        return value
    work = destination / ('execution-' + uuid.uuid4().hex)
    work.mkdir()
    argv = [part.replace('{prediction}', str(input_receipt)).replace('{job_id}', job['id'])
            .replace('{output}', str(work)) for part in stage['argv']]
    if not argv or not Path(argv[0]).is_absolute() or argv[0] not in stage['source_files']:
        raise ValueError('CPU executable must be absolute and pinned')
    relative = Path(stage.get('result_file', 'result.json'))
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('CPU result must be within its own execution output')
    timeout = int(stage.get('timeout_seconds', 600))
    if not 1 <= timeout <= 1800:
        raise ValueError('CPU stage timeout must be 1..1800 seconds')
    environment = dict(os.environ, **stage.get('environment', {}))
    environment.update(CUDA_VISIBLE_DEVICES='', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
                       MKL_NUM_THREADS='1', NUMEXPR_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
    with (work / 'stage.log').open('ab') as log:
        child = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                 pass_fds=(lock_fd,), start_new_session=True, env=environment)
        try:
            code = child.wait(timeout=timeout)
            if code:
                raise subprocess.CalledProcessError(code, argv)
        except BaseException:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            raise
    expected = work / relative
    result = read(expected)
    if result.get('status') not in ('complete', 'passed'):
        raise ValueError('CPU validation did not pass')
    value = dict(receipt, postprocess={**binding, 'result': result, 'sha256': sha256(expected),
        'result_file': str(expected), 'source_files': stage['source_files'],
        'output_dir': str(work), 'files': inventory(work)})
    atomic_json(completion, value, exclusive=True)
    return value
