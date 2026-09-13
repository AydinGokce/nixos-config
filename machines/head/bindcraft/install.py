#!/usr/bin/env python3
"""Install the pinned isolated BindCraft runtime, without any cloud allocation.

Package/source/model resolution never occurs here. Conda packages and upstream
archives are verified by SHA-256 before installation. --evaluation includes the
official PyRosetta wheel for the explicitly requested evaluation; it does not
assert or acquire a commercial license. The default installs only public core
components and cannot report complete BindCraft readiness.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request

HERE = Path(__file__).resolve().parent
GIB = 1024**3


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def publish(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(canonical(value) + b'\n'); stream.flush(); os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def fetch(item, cache):
    expected, filename = item['sha256'], item['filename']
    if (not re.fullmatch('[a-f0-9]{64}', expected) or Path(filename).name != filename
            or filename in ('', '.', '..') or not item['url'].startswith('https://')):
        raise ValueError('Invalid immutable download pin')
    directory = cache / expected
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / filename
    if destination.is_file() and digest(destination) == expected:
        return destination
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
            temporary = Path(output.name)
            with urllib.request.urlopen(item['url'], timeout=120) as response:
                shutil.copyfileobj(response, output, length=4 << 20)
        if (item.get('size') is not None and temporary.stat().st_size != item['size']) or digest(temporary) != expected:
            raise ValueError('Download failed pin verification: ' + filename)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def extract_source(archive, destination):
    with tempfile.TemporaryDirectory(prefix='source-', dir=destination.parent) as temporary:
        temporary = Path(temporary)
        with tarfile.open(archive) as source:
            for member in source.getmembers():
                name = PurePosixPath(member.name)
                if name.is_absolute() or '..' in name.parts or not (member.isfile() or member.isdir()):
                    raise ValueError('Unexpected source archive member')
            source.extractall(temporary, filter='data')
        roots = list(temporary.iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            raise ValueError('Source archive must contain one directory')
        if destination.exists():
            shutil.rmtree(destination)
        os.rename(roots[0], destination)


def extract_params(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    records, seen = [], set()
    with tarfile.open(archive) as source:
        for member in source:
            name = member.name.removeprefix('./')
            if name in seen or not member.isfile() or not re.fullmatch(r'LICENSE|params_model_[1-5](?:_ptm|_multimer_v3)?\.npz', name):
                raise ValueError('Unexpected AlphaFold parameter archive member')
            seen.add(name)
            path = destination / name
            hasher = hashlib.sha256()
            with source.extractfile(member) as incoming, path.open('wb') as outgoing:
                for block in iter(lambda: incoming.read(4 << 20), b''):
                    hasher.update(block); outgoing.write(block)
            path.chmod(0o644)
            if path.stat().st_size != member.size:
                raise ValueError('Incomplete AlphaFold parameter extraction')
            records.append({'path': 'params/' + name, 'sha256': hasher.hexdigest(), 'size': member.size})
    expected = {'params_model_' + str(n) + suffix + '.npz'
                for n in range(1, 6) for suffix in ('', '_ptm', '_multimer_v3')}
    if {Path(row['path']).name for row in records} != expected | {'LICENSE'}:
        raise ValueError('Incomplete AlphaFold parameter inventory')
    return records


def environment(prefix, *, cpu=False):
    env = dict(os.environ)
    env.update(PATH=str(prefix / 'env/bin') + ':' + env.get('PATH', ''), PYTHONNOUSERSITE='1', MPLBACKEND='Agg',
               LD_LIBRARY_PATH=str(prefix / 'env/lib') + (':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else ''))
    env.pop('PYTHONPATH', None)
    if Path('/run/opengl-driver/lib').is_dir():
        env['LD_LIBRARY_PATH'] = '/run/opengl-driver/lib:' + env['LD_LIBRARY_PATH']
    if cpu:
        env['JAX_PLATFORMS'] = 'cpu'
    return env


def command(argv, *, env=None, cwd=None):
    print('bio-bindcraft-install:', ' '.join(map(str, argv)), flush=True)
    subprocess.run(list(map(str, argv)), env=env, cwd=cwd, check=True)


CPU_PROBE = r'''
import importlib.metadata as metadata, json, pathlib, sys, tempfile, subprocess
import numpy as np
import jax, jax.numpy as jnp
import colabdesign, pdbfixer, openmm
from colabdesign.mpnn import mk_mpnn_model
from Bio.PDB import PDBParser, DSSP
root, source, receipt, include_pyrosetta = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), sys.argv[4]=='yes'
result = {'cpu_imports':True, 'gpu_tested':False, 'versions':{name:metadata.version(name) for name in
          ('jax','jaxlib','numpy','colabdesign','pdbfixer','openmm','biopython','dm-haiku','flax','optax')}}
result['versions']['python'] = platform_version = '.'.join(map(str,sys.version_info[:3]))
result['versions']['pyrosetta'] = None
assert jax.devices()[0].platform == 'cpu'
assert float(jax.jit(lambda x:jnp.sum(x*x))(jnp.arange(8))) == 140
pdb = source/'example/PDL1.pdb'
model=mk_mpnn_model(weights='soluble',model_name='v_48_020',seed=17)
model.prep_inputs(pdb_filename=str(pdb),chain='A')
samples=model.sample(num=1,batch=1,temperature=0.1)
assert len(samples['seq'])==1 and len(samples['seq'][0])==115
result['mpnn_cpu_sample']={'sequences':1,'length':115,'checkpoint':'soluble/v_48_020'}
parsed=PDBParser(QUIET=True).get_structure('upstream-example',str(pdb))
dssp=DSSP(parsed[0],str(pdb),dssp=str(source/'functions/dssp'))
assert len(dssp)>0
result['dssp_residues']=len(dssp)
# The helper's dynamic loader must resolve its GMP and Fortran dependencies.
# Actual molecular surface scoring is qualified by the later GPU full-pipeline test.
loader=subprocess.run(['ldd',str(source/'functions/DAlphaBall.gcc')],capture_output=True,text=True,check=True)
assert 'not found' not in loader.stdout
result['dalphaball_loader']='passed'
if include_pyrosetta:
    import pyrosetta as pr
    sys.path.insert(0,str(source))
    import functions
    pr.init('-mute all -ignore_unrecognized_res -constant_seed -jran 17 -holes:dalphaball '+str(source/'functions/DAlphaBall.gcc'))
    pose=pr.pose_from_pdb(str(pdb))
    assert pose.total_residue()==115
    score=float(pr.get_fa_scorefxn()(pose))
    assert np.isfinite(score)
    result['pyrosetta_cpu']={'residues':115,'score_finite':True,'bindcraft_imports':True}
    result['versions']['pyrosetta']=metadata.version('pyrosetta')
receipt.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,sort_keys=True))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path('/mnt/bio-shared/bindcraft'))
    parser.add_argument('--cache', type=Path, required=True, help='Dedicated build cache outside the runtime root')
    parser.add_argument('--state-root', type=Path, default=Path('/var/lib/dc'))
    parser.add_argument('--evaluation', action='store_true', help='Include PyRosetta for explicitly authorized evaluation; commercial license stays pending')
    parser.add_argument('--download-only', action='store_true')
    args = parser.parse_args(argv)
    prefix, cache, state = args.prefix.absolute(), args.cache.absolute(), args.state_root.absolute()
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        parser.error('This runtime is pinned for Linux x86_64')
    if (prefix in (Path('/'), Path.home()) or prefix.is_symlink() or cache.is_relative_to(prefix)
            or prefix.is_relative_to(cache)):
        parser.error('Use a dedicated ordinary runtime directory and separate cache')
    pins_path, lock_path = HERE / 'pins.json', HERE / 'linux-64-cuda.lock.json'
    pins, lock = json.loads(pins_path.read_text()), json.loads(lock_path.read_text())
    if pins['schema'] != 'bio-bindcraft-pins.v1' or lock['schema'] != 'bio-bindcraft-conda-lock.v1':
        raise ValueError('Unexpected BindCraft pins or package lock')
    identity = {'pins_sha256': digest(pins_path), 'lock_sha256': digest(lock_path)}
    fingerprint = hashlib.sha256(canonical({**identity, 'installer_sha256': digest(__file__)})).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    items = [pins['micromamba'], *lock['packages'], *pins['sources'].values(), pins['af2']]
    if args.evaluation:
        items.append(pins['pyrosetta'])
    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = list(pool.map(lambda item: fetch(item, cache / 'downloads'), items))
    downloaded = {item['sha256']: path for item, path in zip(items, fetched)}
    if args.download_only:
        print(json.dumps({'download_only': True, 'files': len(items), 'fingerprint': fingerprint})); return
    state.mkdir(parents=True, exist_ok=True)
    with ExitStack() as locks:
        # Match existing submission lock order. They protect archive snapshots
        # and full worker lifetimes; publication never changes an active runtime.
        for name in ('bio-submit.lock', 'msa-submit.lock', 'bindcraft-install.lock'):
            fd = os.open(state / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            stream = locks.enter_context(os.fdopen(fd, 'a'))
            fcntl.flock(stream, fcntl.LOCK_EX)
        prefix.parent.mkdir(parents=True, exist_ok=True)
        marker = prefix / '.managed.json'
        if prefix.exists() and any(prefix.iterdir()):
            if not marker.is_file() or json.loads(marker.read_text()) != identity:
                raise FileExistsError('Runtime contains unrelated or differently pinned assets')
        elif shutil.disk_usage(prefix.parent).free < 20 * GIB:
            raise OSError('At least 20 GiB free space is required beyond the downloaded cache')
        prefix.mkdir(parents=True, exist_ok=True); publish(marker, identity)
        (prefix / 'install-manifest.json').unlink(missing_ok=True)
        bootstrap = cache / 'micromamba'
        with tarfile.open(downloaded[pins['micromamba']['sha256']]) as archive:
            with archive.extractfile('bin/micromamba') as incoming, bootstrap.open('wb') as outgoing:
                shutil.copyfileobj(incoming, outgoing)
        bootstrap.chmod(0o700)
        explicit = cache / (identity['lock_sha256'] + '.explicit.txt')
        explicit.write_text('@EXPLICIT\n' + ''.join(downloaded[row['sha256']].as_uri() + '#' + row['md5'] + '\n'
                                                   for row in lock['packages']))
        env = dict(os.environ, MAMBA_ROOT_PREFIX=str(cache / 'mamba'), MAMBA_NO_BANNER='1', CONDA_OVERRIDE_CUDA=lock['cuda'])
        verb = 'install' if (prefix / 'env/conda-meta/history').exists() else 'create'
        command([bootstrap, verb, '--no-rc', '--offline', '--yes', '--prefix', prefix / 'env', '--file', explicit], env=env)
        source_paths = {}
        (prefix / 'src').mkdir(exist_ok=True)
        for name, pin in pins['sources'].items():
            destination = prefix / 'src' / (name + '-' + pin['commit'])
            extract_source(downloaded[pin['sha256']], destination)
            source_paths[name] = destination
        source = source_paths['bindcraft']
        for name in ('dssp', 'DAlphaBall.gcc'):
            (source / 'functions' / name).chmod(0o755)
        env = environment(prefix)
        python = prefix / 'env/bin/python'
        command([python, '-m', 'pip', 'install', '--no-index', '--no-deps', '--no-build-isolation',
                 source_paths['colabdesign']], env=env)
        if args.evaluation:
            command([python, '-m', 'pip', 'install', '--no-index', '--no-deps', downloaded[pins['pyrosetta']['sha256']]], env=env)
        command([python, '-m', 'pip', 'check'], env=env)
        af2_files = extract_params(downloaded[pins['af2']['sha256']], prefix / 'params')
        receipt = prefix / 'cpu-install-check.json'
        command([python, '-c', CPU_PROBE, prefix, source, receipt, 'yes' if args.evaluation else 'no'], env=environment(prefix, cpu=True))
        checked = json.loads(receipt.read_text())
        provenance = prefix / 'provenance'; provenance.mkdir(exist_ok=True)
        for path in (pins_path, lock_path, Path(__file__)):
            shutil.copyfile(path, provenance / path.name)
        (prefix / 'activate.sh').write_text(
            '# Source only in the BindCraft process.\n'
            'BIO_BINDCRAFT_ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)\n'
            'export BIO_BINDCRAFT_ROOT\nexport PATH="$BIO_BINDCRAFT_ROOT/env/bin:$PATH"\n'
            'export LD_LIBRARY_PATH="$BIO_BINDCRAFT_ROOT/env/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
            'export PYTHONNOUSERSITE=1 MPLBACKEND=Agg\nunset PYTHONPATH\n')
        source_files = {str(path.relative_to(prefix)): digest(path) for root in source_paths.values()
                        for path in sorted(root.rglob('*')) if path.is_file()
                        and not any(part in ('__pycache__', 'build') or part.endswith('.egg-info')
                                    for part in path.relative_to(root).parts)}
        pyrosetta = {'ready': args.evaluation, 'license_confirmed': False,
                    'version': checked['versions'].get('pyrosetta'),
                    'wheel_sha256': pins['pyrosetta']['sha256'] if args.evaluation else None,
                    'use_scope': 'evaluation_requested_by_user; commercial_license_pending' if args.evaluation else 'not_installed'}
        manifest = {'schema': 'bio-bindcraft-install.v1',
            'readiness': 'ready' if args.evaluation else 'awaiting_pyrosetta_license',
            'fingerprint': fingerprint, 'prefix': str(prefix), 'python': 'env/bin/python',
            'bindcraft_source': str(source.relative_to(prefix)),
            'colabdesign_source': str(source_paths['colabdesign'].relative_to(prefix)), 'params': 'params',
            'versions': checked['versions'], 'source_files': source_files,
            'environment_check': {'path': receipt.name, 'sha256': digest(receipt)},
            'components': {'environment': {'ready': True, 'lock_sha256': identity['lock_sha256']},
                'sources': {'ready': True, **{name + '_commit': pin['commit'] for name, pin in pins['sources'].items()}},
                'af2': {'ready': True, 'archive_sha256': pins['af2']['sha256'], 'files': af2_files},
                'pyrosetta': pyrosetta}, 'installed_epoch': time.time(), 'gpu_tested': False}
        publish(prefix / 'install-manifest.json', manifest)
        print(json.dumps({'prefix': str(prefix), 'readiness': manifest['readiness'], 'fingerprint': fingerprint,
                          'gpu_tested': False, 'license_confirmed': False}, sort_keys=True))


if __name__ == '__main__':
    main()
