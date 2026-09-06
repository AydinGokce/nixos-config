#!/usr/bin/env python3
"""Run native CPU input checks in a bounded, serialized head process."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import stat
import sys
import tempfile
import uuid
from registry import no_symlinks
import rfaa_runtime


def rfaa_description(config):
    """Bind the interpreter and import paths to a verified private generation."""
    path = Path(config['rfaa_config']).absolute()
    no_symlinks(path)
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError('Missing or oversized private RFAA runtime configuration')
    description = json.loads(path.read_text())
    if not isinstance(description, dict) or type(description.get('schema')) is not int or description['schema'] != 1:
        raise ValueError('Unsupported private RFAA runtime schema')
    generation = Path(description['generation'])
    if generation.parent != path.parent/'generations' or not re.fullmatch('[0-9a-f]{64}', generation.name):
        raise ValueError('Private RFAA runtime generation is outside its configuration root')
    no_symlinks(generation)
    receipt_path = generation/'receipt.json'
    no_symlinks(receipt_path)
    if rfaa_runtime.digest(receipt_path) != description.get('receipt_sha256'):
        raise ValueError('Private RFAA runtime receipt SHA256 mismatch')
    receipt = rfaa_runtime.verify_generation(generation)
    inputs = receipt['inputs']
    identity = hashlib.sha256(rfaa_runtime.canonical(inputs)).hexdigest()
    if generation != path.parent/'generations'/identity or receipt.get('kind') != 'rfaa-cpu-runtime':
        raise ValueError('Private RFAA runtime generation does not match its pinned inputs')
    if inputs.get('source_pin') != rfaa_runtime.SOURCE_PIN or inputs.get('rdkit_sha256') != rfaa_runtime.RDKIT_WHEEL_SHA256:
        raise ValueError('Private RFAA runtime dependencies differ from their supported pins')
    expected = rfaa_runtime.describe(generation, inputs['shared_site'], inputs['source'], inputs['loader'], inputs['libraries'])
    for key in ('command', 'pythonpath', 'library_paths', 'environment'):
        if description.get(key) != expected[key]:
            raise ValueError('Private RFAA runtime '+key+' differs from its verified generation')
    return description


def command(arguments, config, scratch):
    model = arguments[arguments.index('--model') + 1]
    plain = '--plain-fasta' in arguments or model in {'esm', 'evolvepro'}
    env = {'CUDA_VISIBLE_DEVICES': '', 'PYTHONNOUSERSITE': '1',
           'PATH': config.get('path', '/run/current-system/sw/bin'),
           'PYTHONPYCACHEPREFIX': str(scratch/'python'), 'TMPDIR': str(scratch),
           'OMP_NUM_THREADS': '2', 'MKL_NUM_THREADS': '2', 'OPENBLAS_NUM_THREADS': '2'}
    interpreter = [config['python']]
    if not plain:
        if model == 'rfaa':
            runtime = rfaa_description(config)
            interpreter = runtime['command']
            env.update(runtime.get('environment', {}))
            env['PYTHONPATH'] = os.pathsep.join(runtime['pythonpath'])
            env['LD_LIBRARY_PATH'] = os.pathsep.join(runtime['library_paths'])
        else:
            name = {'boltz2': 'boltz', 'openfold3': 'openfold3', 'protenix': 'protenix'}[model]
            site = Path(config['shared'])/'envs'/name/'lib/python3.12/site-packages'
            if not site.is_dir():
                raise ValueError(f'Native parser environment is missing: {site}')
            env['PYTHONPATH'] = str(site)
            env['LD_LIBRARY_PATH'] = os.pathsep.join([*(str(p) for p in site.glob('nvidia/*/lib')),
                                                     str(site/'torch/lib'), *config['library_paths']])
            env['BOLTZ_CACHE'] = str(Path(config['shared'])/'cache/boltz')
            env['PROTENIX_ROOT_DIR'] = str(Path(config['shared'])/'protenix/release_data')
    return interpreter + [str(Path(__file__).with_name('adapters.py')), 'compile', *arguments], env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('BIO_LIBRARY_RUNTIME_CONFIG', '/etc/bio-tools/library-runtime.json'))
    args, rest = parser.parse_known_args()
    config = json.loads(Path(args.config).read_text())
    state = Path(config.get('state', '/var/lib/bio-library-runtime'))
    no_symlinks(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Native CCD parsers can require several GiB. Never overlap their peak use
    # on the head, and never allow a malformed input to exhaust host memory.
    lock_fd = os.open(state/'preflight.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
        os.close(lock_fd)
        raise ValueError('Preflight lock must be a regular file')
    with os.fdopen(lock_fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix='preflight-', dir=state) as tmp:
            invocation, env = command(rest, config, Path(tmp))
            systemd = ['systemd-run', '--quiet', '--pipe', '--wait', '--collect',
                       '--unit=bio-library-check-'+uuid.uuid4().hex, '--service-type=exec',
                       '--property=MemoryMax=6G', '--property=MemorySwapMax=0',
                       '--property=RuntimeMaxSec=600', '--property=TimeoutStopSec=20',
                       '--property=KillMode=control-group', '--property=UMask=0077']
            for key, value in env.items():
                systemd.append('--setenv='+key+'='+value)
            result = subprocess.run([*systemd, '--', *invocation], check=False)
            return result.returncode


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError) as error:
        print('bio-library preflight: '+str(error), file=sys.stderr)
        sys.exit(2)
