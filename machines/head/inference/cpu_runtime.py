#!/usr/bin/env python3
"""Copy a pinned CPU import environment without model packages or installers."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).absolute().parent.parent))
from inference.common import atomic_json, digest, inventory, read, sha256


def normalized(name):
    return name.lower().replace('_', '-')


def stage(spec, destination):
    destination = Path(destination).absolute()
    if destination.exists():
        raise FileExistsError('CPU runtime generation already exists')
    source = Path(spec['source_site']).resolve(strict=True)
    python = Path(spec['python']['path'])
    if sha256(python) != spec['python']['sha256']:
        raise ValueError('CPU interpreter differs from its pin')
    expected = {normalized(key): value for key, value in spec['packages'].items()}
    if set(expected) & {'torch', 'rc-foundry', 'atomworks', 'rdkit', 'protenix', 'openfold3', 'boltz'}:
        raise ValueError('CPU output validation must not install native model packages')
    distributions = {normalized(dist.metadata['Name']): dist
                     for dist in importlib.metadata.distributions(path=[str(source)])}
    selected, skipped = {}, []
    for name, version in expected.items():
        dist = distributions.get(name)
        if dist is None or dist.version != version or not dist.files:
            raise ValueError('Pinned CPU distribution missing or different: ' + name)
        for item in dist.files:
            relative = Path(str(item))
            if relative.is_absolute() or '..' in relative.parts or '__pycache__' in relative.parts or relative.suffix == '.pyc':
                skipped.append(str(item))
                continue
            path = source / relative
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(source):
                raise ValueError('Unsafe or missing CPU distribution file: ' + str(relative))
            selected[str(relative)] = {'sha256': sha256(path), 'bytes': path.stat().st_size}
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = Path(tempfile.mkdtemp(prefix='.cpu-runtime-', dir=destination.parent))
    try:
        for relative, value in selected.items():
            target = pending / 'site' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, target)
            if sha256(target) != value['sha256'] or sha256(source / relative) != value['sha256']:
                raise ValueError('CPU package changed during copy: ' + relative)
        environment = {'PYTHONPATH': str(destination / 'site'),
            'LD_LIBRARY_PATH': ':'.join(spec['library_paths']), 'CUDA_VISIBLE_DEVICES': '',
            'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'OPENBLAS_NUM_THREADS': '1'}
        receipt = {'schema': 1, 'kind': 'cpu-import-runtime', 'specification_sha256': digest(spec),
                   'packages': expected, 'python': spec['python'], 'files': inventory(pending),
                   'source_site': str(source), 'omitted_non_import_files': sorted(set(skipped)), 'environment': environment}
        receipt['sha256'] = digest(receipt)
        atomic_json(pending / 'runtime.json', receipt, exclusive=True)
        os.rename(pending, destination)
        # -S skips all startup .pth hooks; this is a pure import environment.
        probe = '''import importlib.metadata,json,sys
import numpy
from biotite.structure.io.pdbx import CIFFile
assert not any(n.split('.')[0] in {'torch','rf3','foundry','atomworks','rdkit'} for n in sys.modules)
print(json.dumps({n:importlib.metadata.version(n) for n in json.loads(sys.argv[1])}))'''
        observed = json.loads(subprocess.check_output([str(python), '-S', '-B', '-c', probe, json.dumps(expected)],
                             env=environment, text=True, timeout=90))
        if observed != expected:
            raise ValueError('Copied CPU packages differ at import')
        atomic_json(destination / 'ready.json', {'status': 'ready', 'runtime_sha256': receipt['sha256'],
                                               'observed_packages': observed}, exclusive=True)
        for path in destination.rglob('*'):
            path.chmod(0o555 if path.is_dir() else 0o444)
        destination.chmod(0o555)
        return receipt
    finally:
        if pending.exists():
            shutil.rmtree(pending)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(stage(read(args.spec), args.out), allow_nan=False))


if __name__ == '__main__':
    main()
