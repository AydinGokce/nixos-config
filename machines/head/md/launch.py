"""Restore the pinned MD binary archive on a disposable Ubuntu worker."""
import argparse
import json
import os
from pathlib import Path
import subprocess

from .runtime.restore import restore, expected_fingerprint


def select(root, variant):
    root = Path(root)
    registry = json.loads((root / 'current.json').read_text())
    if registry.get('schema') != 'bio-md-runtime-deployment.v1':
        raise ValueError('MD runtime deployment registry is unavailable or invalid')
    entry = registry[variant]
    archive = entry['archive']
    if (not isinstance(archive, str) or Path(archive).name != archive or
            archive in {'', '.', '..'} or entry['fingerprint'] != expected_fingerprint(variant)):
        raise ValueError('MD archive path or runtime fingerprint differs from deployed source')
    if not (root / archive).is_file():
        raise ValueError('The pinned MD archive has not been published')
    return root / archive, entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle')
    parser.add_argument('--out')
    parser.add_argument('--check', action='store_true', help='Verify deployment availability without execution')
    parser.add_argument('--runtime-root', default='/mnt/bio-shared/md-runtime')
    parser.add_argument('--variant', choices=('cpu', 'cuda'), required=True)
    args = parser.parse_args()
    archive, entry = select(args.runtime_root, args.variant)
    if args.check:
        print(json.dumps({'variant': args.variant, 'fingerprint': entry['fingerprint'], 'archive': archive.name,
                          'paid_compute_requested': False}))
        return
    if not args.bundle or not args.out:
        parser.error('--bundle and --out are required for worker execution')
    prefix = Path('/opt/bio-md') / entry['fingerprint']
    restore(archive, entry['sha256'], prefix, fingerprint=entry['fingerprint'], variant=args.variant)
    tools = str(Path(__file__).absolute().parent.parent)
    # activate.sh is part of the verified archive, not caller-supplied shell.
    os.execvp('bash', ['bash', '-c', 'source "$1/activate.sh" || exit; shift; exec "$@"',
                      'bio-md-worker', str(prefix), 'env', 'PYTHONPATH=' + tools,
                      str(prefix / 'bin/python'), '-m', 'md.worker', '--bundle', args.bundle,
                      '--out', args.out, '--runtime', str(prefix)])


if __name__ == '__main__':
    main()
