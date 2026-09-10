"""Bounded, shared on-demand startup; allocation remains in budgeted bio-submit."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import time

import session

MAX_PROGRESS = 1024 * 1024
STAGES = {'starting', 'warming', 'ready', 'waiting', 'failed'}


class SessionError(ValueError):
    def __init__(self, code, message, session_id=None):
        self.code, self.message, self.session_id = code, message, session_id
        super().__init__(f'{code}: {message}')


def document(path, *, optional=False):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise SessionError('session_missing', f'Required session document is missing: {path}') from None
    if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024**2:
        raise SessionError('session_uncertain', f'Unsafe or oversized session document: {path}')
    try:
        return session.load(path)
    except (ValueError, OSError) as exc:
        raise SessionError('session_uncertain', f'Cannot verify session document: {path}') from exc


def registry_root(path):
    path = Path(path)
    if '..' in path.parts:
        raise SessionError('session_uncertain', 'Session registry may not contain parent traversal')
    path = path.absolute()
    for item in [*reversed(path.parents), path]:
        if item.is_symlink() or (item.exists() and not item.is_dir()):
            raise SessionError('session_uncertain', f'Unsafe session registry path: {item}')
    return path


def timeout(deadline, maximum):
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SessionError('session_starting', 'The private MSA request deadline was exhausted; no additional operation was started')
    return min(maximum, remaining)


def _progress_fd(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size > MAX_PROGRESS):
            os.close(fd)
            raise ValueError('unsafe progress file')
        return fd
    except (OSError, ValueError) as exc:
        raise SessionError('session_invalid', 'MSA progress log must be an existing private regular file owned by this caller') from exc


def validate_progress_log():
    path = os.environ.get('BIO_MSA_PROGRESS_LOG')
    if path:
        os.close(_progress_fd(path))


def emit(stage, message, session_id=None, code=None):
    if stage not in STAGES:
        raise ValueError('Unknown MSA progress stage')
    value = {'message': ' '.join(str(message).split())[:1600], 'timestamp_ns': time.time_ns()}
    if session_id:
        value['session_id'] = session_id
    if code:
        value['code'] = code
    def encoded():
        return ('BIO_MSA_SESSION_STAGE ' + stage + ' ' + json.dumps(value, separators=(',', ':')) + '\n').encode()
    data = encoded()
    while len(data) > 4096:
        value['message'] = value['message'][:len(value['message']) // 2]
        data = encoded()
    path = os.environ.get('BIO_MSA_PROGRESS_LOG')
    if path:
        fd = _progress_fd(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            # A bounded diagnostic tail is sufficient; never grow the file
            # without limit or truncate another caller's retained evidence.
            if os.fstat(fd).st_size + len(data) <= MAX_PROGRESS:
                os.write(fd, data)
        finally:
            os.close(fd)
    sys.stderr.write(data.decode())
    sys.stderr.flush()


@contextmanager
def registration_lock(root, deadline, *, clock=time.monotonic, sleep=time.sleep):
    root = registry_root(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise SessionError('session_uncertain', f'Unsafe session registry: {root}')
    fd = os.open(root / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise SessionError('session_uncertain', 'Unsafe session registration lock')
        while True:
            if clock() >= deadline:
                raise SessionError('session_starting', 'Timed out waiting for shared MSA startup coordination; no new session was started')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                sleep(min(.2, max(0, deadline - clock())))
        yield
    finally:
        os.close(fd)


def ensure(root, deadline, *, observe, start, retire, progress=emit,
           clock=time.monotonic, sleep=time.sleep, interval=3):
    """Observe callbacks never allocate; mutations run under one shared lock.

    A caller joins at most one startup generation. A failed startup never causes
    the same caller to allocate another worker or replay a search request.
    """
    joined = None
    retired = False
    last_message = None
    last_emitted = -float('inf')
    while True:
        if clock() >= deadline:
            raise SessionError('session_starting', 'Timed out waiting for private MSA readiness; shared startup was retained and no search was submitted', joined)
        try:
            state = observe()
        except (SessionError, OSError, ValueError):
            # start/adopt publish several immutable files before their external
            # launch returns, all under this lock. A concurrent observation of
            # that in-flight publication must wait for its stable outcome.
            with registration_lock(root, deadline, clock=clock, sleep=sleep):
                state = observe()
        phase, ident = state['state'], state.get('session_id')
        if joined is not None and ident not in {joined, None}:
            raise SessionError('session_uncertain', 'The shared session generation changed while this request was waiting; no search was submitted', joined)
        if phase == 'ready':
            progress('ready', 'Private MSA session is ready; preparing the requested search', ident)
            return state['ready']
        if phase == 'missing':
            if joined is not None:
                raise SessionError('session_uncertain', 'The joined session registration disappeared; no replacement was allocated', joined)
            progress('starting', 'Starting one shared private MSA worker through the existing budget and hourly price gates')
            with registration_lock(root, deadline, clock=clock, sleep=sleep):
                current = observe()
                if current['state'] != 'missing':
                    continue
                try:
                    started = start()
                except Exception as exc:
                    active = document(Path(root) / 'active.json', optional=True)
                    if active is not None:
                        raise SessionError('session_uncertain', 'Private MSA start failed or its reply was lost; the saved startup will be inspected without another allocation', active.get('session_id')) from exc
                    raise SessionError('session_failed', 'Private MSA startup was rejected before registration: ' + str(exc)) from exc
                joined = started['session_id']
            continue
        if phase == 'terminal':
            if joined == ident:
                raise SessionError('session_failed', 'The shared MSA startup ended before it became ready; its records were retained and this request will not allocate a replacement', ident)
            if retired:
                raise SessionError('session_failed', 'A replacement session also ended; no further startup will be attempted for this request', ident)
            progress('waiting', 'Confirming cleanup of the previous private MSA session before replacement', ident)
            with registration_lock(root, deadline, clock=clock, sleep=sleep):
                current = observe()
                if current.get('session_id') != ident or current['state'] != 'terminal':
                    continue
                try:
                    retire(current)
                except Exception as exc:
                    raise SessionError('session_uncertain', 'Exact private MSA worker and temporary-disk cleanup could not be confirmed; registration retained and no replacement allocated', ident) from exc
                retired = True
            continue
        if phase not in {'starting', 'warming', 'closing'}:
            raise SessionError(state.get('code', 'session_uncertain'), state['message'], ident)
        if phase in {'starting', 'warming'}:
            joined = ident
        stage = 'waiting' if phase == 'closing' else phase
        message = state['message']
        marker = (stage, ident, message)
        if marker != last_message or clock() - last_emitted >= 15:
            progress(stage, message, ident)
            last_message, last_emitted = marker, clock()
        sleep(min(interval, max(0, deadline - clock())))
