"""A dedicated NFS namespace for promptly visible cross-host queue messages.

Use this mount exclusively for its exported control subtree. Model packages,
weights and databases keep their normal data/metadata caches on other mounts.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

OPTIONS = 'vers=4.1,hard,nconnect=16,nosharecache,lookupcache=none,actimeo=0'


def mount_rows(value, mountpoint):
    result = []
    def visit(rows):
        for row in rows:
            if row['target'] == mountpoint and row['fstype'] in ('nfs', 'nfs4'):
                result.append(row)
            visit(row.get('children', []))
    visit(value.get('filesystems', []))
    return result


def validate(row, source, mountpoint):
    options = set(row['options'].split(','))
    required = {'rw', 'hard', 'nosharecache', 'lookupcache=none', 'vers=4.1',
                'acregmin=0', 'acregmax=0', 'acdirmin=0', 'acdirmax=0'}
    if (row['source'] != source or row['target'] != mountpoint
            or row['fstype'] not in ('nfs', 'nfs4') or not required <= options):
        raise ValueError('Control storage source or cache-coherence options differ')
    return row


def ensure(source, mountpoint, *, allow_mount=False):
    root = Path(mountpoint)
    if (not root.is_absolute() or root.is_symlink() or any(c.isspace() for c in mountpoint)
            or ':' not in source or not source.endswith('/inference-control')):
        raise ValueError('Control storage requires an explicit dedicated NFS subtree')
    if root.is_dir():
        # Trigger an existing head automount before querying its actual NFS row.
        with os.scandir(root):
            pass
    def probe():
        result = subprocess.run(['findmnt', '--json', '--mountpoint', mountpoint,
                                 '-o', 'SOURCE,TARGET,FSTYPE,OPTIONS'], capture_output=True, text=True)
        return mount_rows(json.loads(result.stdout), mountpoint) if result.returncode == 0 else []
    rows = probe()
    if not rows and allow_mount:
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()):
            raise ValueError('Refusing to hide existing local control files behind a mount')
        subprocess.run(['mount', '-t', 'nfs', '-o', OPTIONS, source, mountpoint], check=True, timeout=60)
        rows = probe()
    if len(rows) != 1:
        raise ValueError('Exactly one real control NFS mount is required')
    return validate(rows[0], source, mountpoint)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--mountpoint', required=True)
    parser.add_argument('--allow-mount', action='store_true')
    args = parser.parse_args()
    print(json.dumps(ensure(args.source, args.mountpoint, allow_mount=args.allow_mount)))
