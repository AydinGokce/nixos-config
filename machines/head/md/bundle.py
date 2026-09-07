"""Immutable MD input bundles. Archive names are data, never extraction paths."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile

MAX_FILES = 32768  # Includes retained stage receipts in explicit checkpoint resumes.
MAX_BYTES = 2 * 1024**3


def relative_name(value):
    if (not isinstance(value, str) or not value or len(value) > 240 or
            '\\' in value or any(ord(c) < 32 for c in value)):
        raise ValueError('Invalid asset path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {'', '.', '..'} for p in value.split('/')):
        raise ValueError('Assets must have normalized relative paths without traversal')
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def pack(request, assets, target, *, resume=None):
    """assets maps relative names to exact regular files; no directory traversal."""
    resume = resume or {}
    if len(assets) + len(resume) > MAX_FILES:
        raise ValueError('Too many input files')
    contents = {'request.json': canonical(request) + b'\n'}
    total = len(contents['request.json'])
    files = {('assets/' + relative_name(name)): path for name, path in assets.items()}
    files.update({('resume/' + relative_name(name)): path for name, path in resume.items()})
    for name, path in sorted(files.items()):
        relative_name(name)
        path = Path(path)
        if not path.is_file() or path.is_symlink():
            raise ValueError('Every asset must be a regular file')
        total += path.stat().st_size
        if total > MAX_BYTES:
            raise ValueError('Input bundle exceeds 2 GiB')
        contents[name] = path.read_bytes()
    manifest = {'schema': 'bio-md-bundle.v1', 'files': {
        name: {'size': len(data), 'sha256': digest(data)} for name, data in contents.items()}}
    contents['manifest.json'] = canonical(manifest) + b'\n'
    with tarfile.open(target, 'w:gz') as archive:
        for name, data in sorted(contents.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o400, 0
            archive.addfile(info, io.BytesIO(data))
    return manifest


def unpack(source, destination, *, validate_only=False):
    """Validate all members and hashes before writing even one asset."""
    destination = Path(destination)
    contents = {}
    total = 0
    with tarfile.open(source, 'r:gz') as archive:
        for member in archive:
            name = relative_name(member.name)
            if (not member.isfile() or name in contents or len(contents) >= MAX_FILES + 2 or
                    not (name in {'request.json', 'manifest.json'} or name.startswith(('assets/', 'resume/')))):
                raise ValueError('Invalid, duplicate, linked or unexpected archive member')
            total += member.size
            if member.size < 0 or total > MAX_BYTES + 1024**2:
                raise ValueError('Input archive exceeds size limit')
            contents[name] = archive.extractfile(member).read()
    try:
        manifest = json.loads(contents.pop('manifest.json'))
        request = json.loads(contents['request.json'])
    except (KeyError, ValueError) as exc:
        raise ValueError('Missing or invalid MD request/manifest') from exc
    expected = {name: {'size': len(data), 'sha256': digest(data)} for name, data in contents.items()}
    if manifest != {'schema': 'bio-md-bundle.v1', 'files': expected}:
        raise ValueError('MD bundle manifest or source hashes differ')
    if not validate_only:
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name, data in contents.items():
            path = destination / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(data)
    return request, manifest


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    args = parser.parse_args()
    _, manifest = unpack(args.source, '.', validate_only=True)
    print(json.dumps(manifest, sort_keys=True))
