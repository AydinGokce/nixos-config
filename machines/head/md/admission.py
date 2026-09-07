"""Cached native topology admission shared by RPC and low-level bio-submit."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from workbench.common import digest, file_sha, inventory, read_json, require, write_json
from .bundle import unpack
from .protocols import prepare, validate_request
from .worker import verify_resume


def check(bundle, tools, runtime, state='/var/lib/bio-md/admissions'):
    bundle, tools, runtime, state = map(Path, (bundle, tools, runtime, state))
    require((runtime / 'manifest.json').is_file() and (runtime / 'activate.sh').is_file(),
            'Pinned CPU MD runtime is not installed; no worker was launched', 'unavailable')
    identity = {'bundle_sha256': file_sha(bundle), 'runtime_manifest_sha256': file_sha(runtime / 'manifest.json'),
                'source_files': {p.name: file_sha(p.resolve()) for p in (tools / 'md').glob('*.py') if not p.name.startswith('test_')}}
    key = digest(identity)
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (state / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        root = state / key
        receipt_path = root / 'receipt.json'
        if receipt_path.exists():
            receipt = read_json(receipt_path)
            require(receipt['identity'] == identity and receipt['state'] == 'complete',
                    'Cached MD preflight was unsuccessful; inspect its retained evidence', 'conflict')
            actual = {k: v for k, v in inventory(root).items() if k != 'receipt.json'}
            require(actual == receipt['files'], 'Cached MD preflight evidence changed', 'integrity')
            return receipt
        require(not root.exists(), 'Previous MD preflight ended without a receipt; inspect retained state', 'conflict')
        root.mkdir(mode=0o700)
        request, manifest = unpack(bundle, root / 'input')
        normalized = validate_request(request, root / 'input/assets')
        plan = prepare(normalized, root / 'input/assets', root / 'work')
        resume = root / 'input/resume'
        resume_validation = verify_resume(plan, resume) if resume.exists() else None
        write_json(root / 'plan.json', plan)
        env = os.environ.copy()
        # systemd transient units do not inherit the CLI wrapper's PATH. NixOS
        # exposes its installed shell/core tools here instead of /usr/bin.
        if Path('/run/current-system/sw/bin').is_dir():
            env['PATH'] = '/run/current-system/sw/bin' + os.pathsep + env.get('PATH', os.defpath)
        shell = shutil.which('bash', path=env.get('PATH', os.defpath))
        require(shell is not None, 'MD admission requires an installed Bash shell', 'unavailable')
        with (root / 'native-preflight.log').open('wb') as log:
            result = subprocess.run([shell, '-c', 'source "$1/activate.sh" || exit; shift; exec "$@"',
                 'bio-md-preflight', str(runtime), 'env', 'PYTHONPATH=' + str(tools),
                 str(runtime / 'bin/python'), '-m', 'md.preflight', '--plan', str(root / 'plan.json'),
                 '--work', str(root / 'work'), '--runtime', str(runtime)],
                 stdout=log, stderr=subprocess.STDOUT, timeout=500, check=False, env=env)
        receipt = {'schema': 'bio-md-admission.v1', 'state': 'complete' if result.returncode == 0 else 'failed',
                   'identity': identity, 'request_sha256': plan['request_sha256'],
                   'resume_validation': resume_validation,
                   'files': inventory(root), 'paid_compute_requested': False, 'molecular_dynamics_executed': False}
        write_json(receipt_path, receipt)
        if result.returncode:
            tail = (root / 'native-preflight.log').read_bytes()[-5000:].decode(errors='replace')
            raise ValueError('Native MD topology preflight failed; no worker launched: ' + tail)
        return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--tools', default=str(Path(__file__).absolute().parent.parent))
    parser.add_argument('--runtime', default='/var/lib/bio-md/runtime-cpu')
    parser.add_argument('--state', default='/var/lib/bio-md/admissions')
    args = parser.parse_args()
    check(args.bundle, args.tools, args.runtime, args.state)
    print('bio-md: native preflight passed; no paid work has started')
