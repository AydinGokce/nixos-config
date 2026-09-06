from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

WIRE = 2 * 1024 * 1024
CHUNK = 512 * 1024
UPLOAD = 256 * 1024 * 1024
TERMINAL = {'complete', 'failed', 'cancelled', 'interrupted'}


class Error(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(condition, message, code='invalid'):
    if not condition:
        raise Error(code, message)


def keys(value, required=(), optional=()):
    require(isinstance(value, dict), 'Expected an object')
    require(set(required) <= set(value), 'Missing fields: ' + ', '.join(sorted(set(required) - set(value))))
    require(not set(value) - set(required) - set(optional),
            'Unknown fields: ' + ', '.join(sorted(set(value) - set(required) - set(optional))))


def string(value, name, limit=256):
    require(isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value),
            name + ' must be nonempty text without control characters')
    return value


def number(value, name, low=0, high=CHUNK):
    require(type(value) is int and low <= value <= high, f'{name} must be an integer in {low}..{high}')
    return value


def identifier(value):
    require(isinstance(value, str) and re.fullmatch('[a-f0-9]{32}', value), 'Invalid resource ID')
    return value


def sha(value):
    require(isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value), 'Invalid SHA-256')
    return value


def uid():
    return uuid.uuid4().hex


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode()
    except (TypeError, ValueError) as exc:
        raise Error('invalid', 'Expected finite JSON') from exc


def parse(value):
    def pairs(items):
        out = {}
        for key, val in items:
            require(key not in out, 'Duplicate JSON key')
            out[key] = val
        return out
    def bad(_):
        raise Error('invalid', 'Nonfinite JSON number')
    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=bad)
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, Error):
            raise
        raise Error('invalid', 'Invalid JSON') from exc


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def no_links(path):
    path = Path(path).absolute()
    for item in reversed([path, *path.parents]):
        if item.exists() or item.is_symlink():
            require(not item.is_symlink(), 'Symlink in protected path', 'integrity')
    return path


def safe_file(path):
    path = no_links(path)
    require(path.is_file() and stat.S_ISREG(path.stat().st_mode), 'Expected regular file', 'integrity')
    return path


def file_sha(path):
    h = hashlib.sha256()
    with safe_file(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def atomic(path, data, exclusive=False):
    path = no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.publish-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, data, exclusive=False):
    atomic(path, canonical(data) + b'\n', exclusive)


def read_json(path):
    return parse(safe_file(path).read_bytes())


def decode_chunk(value):
    require(isinstance(value, str) and len(value) <= (CHUNK + 2) // 3 * 4, 'Oversized upload chunk', 'limit')
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise Error('invalid', 'Invalid base64 chunk') from exc
    require(0 < len(decoded) <= CHUNK, 'Empty or oversized upload chunk', 'limit')
    return decoded


def inventory(root):
    root = no_links(root)
    out = {}
    for path in sorted(root.rglob('*')):
        no_links(path)
        if path.is_dir():
            continue
        safe_file(path)
        out[str(path.relative_to(root))] = {'size': path.stat().st_size, 'sha256': file_sha(path)}
    return out


def verify_inventory(root, files):
    require(inventory(root) == files, 'Immutable file inventory changed', 'integrity')
