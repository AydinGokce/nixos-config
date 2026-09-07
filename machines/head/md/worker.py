"""Execute a generated MD stage graph, retaining native logs and checkpoints."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from .bundle import canonical, digest, relative_name, unpack


def save(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.md-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical(value) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fingerprint(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'Expected an ordinary stage file: {path.name}')
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024**2), b''):
            h.update(block)
    return {'size': path.stat().st_size, 'sha256': h.hexdigest()}


def owned_path(root, name):
    path = root / relative_name(name)
    if path.resolve() != path.absolute():
        raise ValueError('Symlink in MD stage path')
    return path


def signal_process(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        # The native engine may exit between poll() and delivery.
        pass


def _binding(stage, work, plan_sha):
    return {'stage': stage,
            'inputs': {name: fingerprint(owned_path(work, name)) for name in stage.get('inputs', [])},
            'plan_sha256': plan_sha}


def _completed_outputs(stage, previous, work):
    actual = {name: fingerprint(owned_path(work, name)) for name in stage['outputs']}
    if actual != previous.get('outputs'):
        raise ValueError(f"Completed stage output changed: {stage['id']}")


def _sealed_checkpoint(stage, previous, work):
    checkpoint = stage.get('checkpoint')
    if not checkpoint:
        return None
    path = owned_path(work, checkpoint)
    if not path.exists():
        raise ValueError(f"Stopped dynamics has no checkpoint: {stage['id']}; no automatic restart")
    if previous.get('state') == 'running' or not previous.get('checkpoint') or fingerprint(path) != previous['checkpoint']:
        raise ValueError(f"Checkpoint changed or lacks a sealed stage receipt: {stage['id']}")
    for name in stage.get('restart_files', []):
        saved = previous.get('restart_files', {}).get(name)
        if not saved or fingerprint(owned_path(work, name)) != saved:
            raise ValueError(f"Bias restart history changed or is unsealed: {stage['id']}/{name}")
    if stage['argv'][:2] != ['gmx', 'mdrun']:
        raise ValueError('Only mdrun stages may resume a native checkpoint')
    return path


def verify_resume(plan, work):
    """Verify retained execution evidence without changing files or running tools.

    Used on the head before rental and again at the native worker boundary.
    Pending stages may lack outputs; partial output without a stage receipt is
    not an admitted checkpoint. A failed dynamics stage cannot restart at zero.
    """
    work = Path(work).absolute()
    retained_plan = owned_path(work, 'execution-plan.json')
    fingerprint(retained_plan)
    if json.loads(retained_plan.read_text()) != plan:
        raise ValueError('Resume plan differs from retained execution; create a new run')
    from .runtime.restore import expected_fingerprint
    runtime_path = owned_path(work, '_runtime.json')
    fingerprint(runtime_path)
    runtime = json.loads(runtime_path.read_text())
    variant = runtime.get('variant')
    if (runtime.get('schema') != 'bio-md-runtime.v1' or variant not in {'cpu', 'cuda'} or
            runtime.get('fingerprint') != expected_fingerprint(variant)):
        raise ValueError('Resume runtime fingerprint differs from the currently pinned engine/force-field source')
    stages = plan['stages']
    ids = [relative_name(stage['id']) for stage in stages]
    if len(set(ids)) != len(ids) or any('/' in ident for ident in ids):
        raise ValueError('Invalid or duplicate resume stage identifier')
    records = owned_path(work, '.stages')
    if not records.is_dir():
        raise ValueError('Resume has no sealed stage receipts')
    unexpected = {p.stem for p in records.glob('*.json')} - set(ids)
    if unexpected:
        raise ValueError('Resume includes receipts outside its retained plan')
    plan_sha = digest(canonical(plan))
    complete, verified, checkpoints = set(), [], []
    for stage in stages:
        ident = stage['id']
        receipt_path = owned_path(work, '.stages/' + ident + '.json')
        if not receipt_path.exists():
            produced = list(stage.get('outputs', [])) + ([stage['checkpoint']] if stage.get('checkpoint') else [])
            if any(owned_path(work, name).exists() for name in produced):
                raise ValueError(f'Unsealed stage output/checkpoint has no receipt: {ident}')
            continue
        fingerprint(receipt_path)
        previous = json.loads(receipt_path.read_text())
        if not set(stage.get('dependencies', [])) <= complete:
            raise ValueError(f'Resume stage has incomplete dependencies: {ident}')
        if previous.get('binding') != _binding(stage, work, plan_sha):
            raise ValueError(f'Stage input or command changed: {ident}')
        state = previous.get('state')
        if state not in {'complete', 'failed', 'interrupted', 'running'}:
            raise ValueError(f'Invalid retained stage state: {ident}')
        if state == 'running':
            raise ValueError(f'Unsealed running stage cannot resume: {ident}')
        if state == 'complete':
            _completed_outputs(stage, previous, work)
            complete.add(ident)
        if stage.get('checkpoint'):
            _sealed_checkpoint(stage, previous, work)
            checkpoints.append(ident)
        verified.append({'stage_id': ident, 'state': state})
    if not checkpoints:
        raise ValueError('Resume has no sealed native checkpoint')
    return {'schema': 'bio-md-resume-validation.v1', 'plan_sha256': plan_sha,
            'source_runtime_fingerprint': runtime['fingerprint'], 'source_runtime_variant': variant,
            'verified_stages': verified, 'checkpoint_stages': checkpoints,
            'molecular_dynamics_executed': False, 'paid_compute_requested': False}


def execute(plan, work, runtime, *, deadline=None, cancel_file=None):
    """Only trusted protocol generators produce commands. Input JSON is not a plan."""
    work, runtime = Path(work).absolute(), Path(runtime).absolute()
    records = work / '.stages'
    records.mkdir(mode=0o700, exist_ok=True)
    plan_sha = digest(canonical(plan))
    old_plan = work / 'execution-plan.json'
    if old_plan.exists() and json.loads(old_plan.read_text()) != plan:
        raise ValueError('Resume plan differs from retained execution; create a new run')
    save(old_plan, plan)
    complete = set()
    interrupted = False
    process = None

    def stop(signum, frame):
        nonlocal interrupted
        interrupted = True

    old_handlers = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        for stage in plan['stages']:
            ident = relative_name(stage['id'])
            if '/' in ident or not set(stage.get('dependencies', [])) <= complete:
                raise ValueError('Invalid stage identifier or unsatisfied dependency')
            cwd = work if stage['cwd'] == '.' else owned_path(work, stage['cwd'])
            cwd.mkdir(parents=True, exist_ok=True)
            argv = list(stage['argv'])
            if not argv or argv[0] not in {'gmx', 'pmx', 'plumed', 'python', 'python3'}:
                raise ValueError('Stage executable is not a pinned MD tool')
            executable = 'python' if argv[0] == 'python3' else argv[0]
            argv[0] = str(runtime / 'bin' / executable)
            if not Path(argv[0]).is_file():
                raise ValueError(f'Runtime lacks required executable: {executable}')
            receipt_path = records / (ident + '.json')
            binding = _binding(stage, work, plan_sha)
            previous = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
            if previous and previous.get('binding') != binding:
                raise ValueError(f'Stage input or command changed: {ident}')
            if previous and previous['state'] == 'complete':
                _completed_outputs(stage, previous, work)
                complete.add(ident)
                continue
            interrupted = interrupted or (cancel_file is not None and Path(cancel_file).exists())
            if interrupted or (deadline is not None and time.time() >= deadline - 30):
                return 75
            checkpoint = stage.get('checkpoint')
            if previous and checkpoint:
                checkpoint_path = _sealed_checkpoint(stage, previous, work)
                if '-cpi' not in argv:
                    argv += ['-cpi', os.path.relpath(checkpoint_path, cwd), '-append']
            receipt = {'binding': binding, 'state': 'running', 'started_at': time.time(),
                       'argv': argv, 'attempt': (previous or {}).get('attempt', 0) + 1}
            save(receipt_path, receipt)
            print(f'BIO_MD_STAGE {ident} starting', flush=True)
            env = os.environ.copy()
            env['PATH'] = str(runtime / 'bin') + os.pathsep + env.get('PATH', '')
            env['PYTHONPATH'] = str(Path(__file__).absolute().parent.parent)
            env['PYTHONUNBUFFERED'] = '1'
            # GROMACS treats a mismatching OMP_NUM_THREADS / -ntomp as fatal.
            # The admitted protocol owns the thread count on every replica.
            if executable == 'gmx' and len(argv) > 1 and argv[1] == 'mdrun' and '-ntomp' in argv:
                env['OMP_NUM_THREADS'] = argv[argv.index('-ntomp') + 1]
            else:
                env.pop('OMP_NUM_THREADS', None)
            with (records / (ident + '.log')).open('ab', buffering=0) as log:
                process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                           stdin=subprocess.PIPE if 'stdin' in stage else subprocess.DEVNULL,
                                           start_new_session=True)
                if 'stdin' in stage:
                    try:
                        process.stdin.write(stage['stdin'].encode())
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
                sent = None
                while process.poll() is None:
                    interrupted = interrupted or (cancel_file is not None and Path(cancel_file).exists())
                    if deadline is not None and time.time() >= deadline - 30:
                        interrupted = True
                    if interrupted and sent is None:
                        signal_process(process, signal.SIGTERM)
                        sent = time.monotonic()
                    if sent is not None and time.monotonic() - sent > 60:
                        signal_process(process, signal.SIGKILL)
                    time.sleep(0.2)
                status = process.returncode
                process = None
            state = 'interrupted' if interrupted else ('complete' if status == 0 else 'failed')
            receipt.update(state=state, exit_code=status, finished_at=time.time())
            if state == 'complete':
                try:
                    receipt['outputs'] = {name: fingerprint(owned_path(work, name)) for name in stage['outputs']}
                except (ValueError, OSError) as exc:
                    receipt.update(state='failed', error=str(exc))
            if checkpoint and owned_path(work, checkpoint).exists():
                receipt['checkpoint'] = fingerprint(owned_path(work, checkpoint))
                receipt['restart_files'] = {name: fingerprint(owned_path(work, name))
                    for name in stage.get('restart_files', []) if owned_path(work, name).exists()}
            save(receipt_path, receipt)
            print(f"BIO_MD_STAGE {ident} {receipt['state']}", flush=True)
            if receipt['state'] != 'complete':
                with (records / (ident + '.log')).open('rb') as stream:
                    stream.seek(max(0, stream.seek(0, 2) - 8192))
                    print(stream.read().decode(errors='replace'), flush=True)
                return 75 if interrupted else 1
            complete.add(ident)
        return 0
    finally:
        if process is not None and process.poll() is None:
            signal_process(process, signal.SIGTERM)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                signal_process(process, signal.SIGKILL)
                process.wait()
        for s, handler in old_handlers.items():
            signal.signal(s, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--runtime', required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (args.out / '.worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from .protocols import validate_request, prepare
        incoming = args.out / 'input-bundle'
        request, manifest = unpack(args.bundle, incoming, validate_only=incoming.exists())
        saved = args.out / 'input-manifest.json'
        if saved.exists() and json.loads(saved.read_text()) != manifest:
            raise ValueError('Worker input differs from retained job')
        save(saved, manifest)
        normalized = validate_request(request, incoming / 'assets')
        work = args.out / 'simulation'
        plan_file = args.out / 'plan.json'
        if plan_file.exists():
            plan = json.loads(plan_file.read_text())
        else:
            plan = prepare(normalized, incoming / 'assets', work)
            resume = incoming / 'resume'
            if resume.exists():
                verify_resume(plan, resume)
                shutil.copytree(resume, work, dirs_exist_ok=True)
            save(plan_file, plan)
        runtime_manifest = args.runtime / 'manifest.json'
        if runtime_manifest.exists():
            shutil.copyfile(runtime_manifest, args.out / 'runtime.json')
        deadline = float(os.environ['BIO_JOB_DEADLINE_EPOCH']) if os.environ.get('BIO_JOB_DEADLINE_EPOCH') else None
        status = execute(plan, work, args.runtime, deadline=deadline, cancel_file=args.out / '.cancel-requested')
        save(args.out / 'result.json', {'schema': 'bio-md-result.v1', 'state': 'complete' if status == 0 else 'interrupted' if status == 75 else 'failed',
                                      'exit_code': status, 'request_sha256': digest(canonical(normalized)),
                                      'claims': plan.get('claims', []), 'finished_at': time.time()})
        return status


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print('bio-md worker: ' + str(exc), file=sys.stderr)
        sys.exit(2)
