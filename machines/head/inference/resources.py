"""Choose thread budgets before numerical libraries are imported."""
from __future__ import annotations

import math
import os
from pathlib import Path


def effective_cpus(affinity=None, quota_file=None):
    available = len(os.sched_getaffinity(0)) if affinity is None else len(affinity)
    if quota_file is None:
        relative = next((line.split(":", 2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines()
                         if line.startswith('0::')), '/')
        quota_file = Path('/sys/fs/cgroup') / relative.lstrip('/') / 'cpu.max'
    try:
        quota, period = Path(quota_file).read_text().split()
        if quota != 'max':
            available = min(available, max(1, math.floor(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    return max(1, available)


def configure(policy):
    cpus = min(int(policy.get('cpus', 8)), effective_cpus())
    if cpus < 1:
        raise ValueError('CPU budget must be positive')
    # BLAS work in data-loader subprocesses must not multiply the parent's
    # entire CPU allocation. Model intra-op parallelism is set separately.
    blas = min(cpus, max(1, int(policy.get('blas_threads', 1))))
    for name in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        os.environ[name] = str(blas)
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    return {'effective_cpus': cpus, 'blas_threads': blas,
            'torch_threads': min(cpus, max(1, int(policy.get('torch_threads', cpus)))),
            'torch_interop_threads': min(cpus, max(1, int(policy.get('torch_interop_threads', 1)))),
            'recommended_loader_workers': min(cpus, max(0, int(policy.get('loader_workers', 2))))}
