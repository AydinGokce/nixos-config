"""Bounded progress events; observation never grants control over a worker."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

PREFIX = "BIO_WORKER_STAGE "
LIMIT = 4096
STAGES = {"waiting_capacity", "allocating", "base_setup", "runtime_package", "runtime_download", "runtime_extract",
          "database_check", "index_warm", "ready", "search", "gpu_allocation", "model_setup",
          "inference", "result_transfer", "cleanup"}
WRITE_LOCK = threading.RLock()


def emit(stage, *, scope="msa", state="running", message="", stage_id=None,
         completed=None, total=None, unit=None, eta=None):
    if stage not in STAGES or scope not in {"msa", "gpu"} or state not in {"running", "complete", "failed"}:
        raise ValueError("Invalid worker progress stage")
    if stage == "waiting_capacity":
        # A retry deadline bounds how long we wait; it does not predict when
        # provider capacity becomes available or represent completed work.
        if any(value is not None for value in (completed, total, unit)):
            raise ValueError("Capacity waiting has no measurable completion counter")
        if eta is not None and (not isinstance(eta, dict) or eta.get("state") != "unknown"):
            raise ValueError("Worker availability has no reliable estimate")
        if eta is None:
            eta = dict(state="unknown", scope="stage", basis="Worker availability has no reliable estimate")
    value = dict(schema=1, stage=stage, scope=scope, state=state,
                 message=str(message)[:1000], timestamp_ns=time.time_ns())
    if stage_id is not None:
        if not isinstance(stage_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", stage_id):
            raise ValueError("Invalid progress activity ID")
        value["stage_id"] = stage_id
    for name, number in (("completed", completed), ("total", total)):
        if number is not None:
            if type(number) is not int or not 0 <= number < 2**63:
                raise ValueError("Invalid worker progress counter")
            value[name] = number
    if completed is not None and total is not None and completed > total:
        raise ValueError("Progress exceeds total")
    if unit is not None:
        if unit not in {"bytes", "items", "steps"}:
            raise ValueError("Invalid progress unit")
        value["unit"] = unit
    if eta is not None:
        if (not isinstance(eta, dict) or eta.get("state") not in {"unknown", "estimate", "range"}
                or eta.get("scope") not in {"stage", "startup", "job"}
                or not isinstance(eta.get("basis"), str) or not 1 <= len(eta["basis"]) <= 500):
            raise ValueError("Invalid worker progress estimate")
        keys = {"unknown": set(), "estimate": {"seconds"}, "range": {"lower_seconds", "upper_seconds"}}[eta["state"]]
        if set(eta) != keys | {"state", "scope", "basis"}:
            raise ValueError("Invalid worker progress estimate fields")
        if any(type(eta[k]) not in (int, float) or not math.isfinite(eta[k]) or not 0 <= eta[k] <= 86400 for k in keys):
            raise ValueError("Invalid worker progress estimate duration")
        if eta["state"] == "range" and eta["lower_seconds"] > eta["upper_seconds"]:
            raise ValueError("Invalid worker progress estimate range")
        value["eta"] = eta
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    line = PREFIX.encode() + raw
    if len(line) > LIMIT:
        raise ValueError("Worker progress event too large")
    with WRITE_LOCK:
        sys.stderr.write(line.decode()); sys.stderr.flush()
        log = os.environ.get("BIO_WORKER_PROGRESS_LOG")
        if log:
            fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError("Worker progress log is not an owned private regular file")
                if info.st_size < 4 * 1024**2 and os.write(fd, line) != len(line):
                    raise OSError("Incomplete progress append")
            finally:
                os.close(fd)
        snapshot = os.environ.get("BIO_WORKER_PROGRESS_JSON")
        if snapshot:
            path = Path(snapshot)
            fd, pending = tempfile.mkstemp(prefix=".worker-progress-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                os.replace(pending, path)
            finally:
                if os.path.lexists(pending):
                    os.unlink(pending)
    return value


class Activity:
    """Emit at most one counter update per second and heartbeat every ten seconds."""
    def __init__(self, stage, *, interval=10, **fields):
        self.stage = stage
        self.fields = dict(fields, stage_id=fields.get("stage_id") or uuid.uuid4().hex)
        self.interval = interval
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.last_emit = 0.
        self.eta_observed = time.monotonic()
        self.error = None
        self.thread = None

    def _emit(self, state="running"):
        fields = dict(self.fields)
        if isinstance(fields.get("eta"), dict):
            eta = dict(fields["eta"])
            elapsed = max(0, int(time.monotonic() - self.eta_observed))
            for key in ("seconds", "lower_seconds", "upper_seconds"):
                if type(eta.get(key)) in (int, float):
                    eta[key] = max(0, eta[key] - elapsed)
            remaining = eta.get("upper_seconds", eta.get("seconds"))
            if state == "running" and remaining == 0:
                eta = dict(state="unknown", scope=eta.get("scope", "stage"), basis="Taking longer than the initial estimate")
            fields["eta"] = eta
        emit(self.stage, state=state, **fields)
        self.last_emit = time.monotonic()

    def _heartbeat(self):
        while not self.stopped.wait(self.interval):
            with self.lock:
                try:
                    self._emit()
                except BaseException as error:
                    self.error = error
                    self.stopped.set()

    def __enter__(self):
        with self.lock:
            self._emit()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()
        return self

    def update(self, **fields):
        with self.lock:
            if self.error:
                raise self.error
            self.fields.update(fields)
            if "eta" in fields:
                self.eta_observed = time.monotonic()
            if time.monotonic() - self.last_emit >= 1:
                self._emit()

    def __exit__(self, kind, error, traceback):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=2)
        # Never leave an old running heartbeat after the next activity starts.
        if self.thread and self.thread.is_alive():
            raise RuntimeError("Worker progress writer did not stop")
        with self.lock:
            self._emit("failed" if kind or self.error else "complete")
            if kind is None and self.error:
                raise self.error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["emit", "run"])
    parser.add_argument("--stage", required=True, choices=sorted(STAGES))
    parser.add_argument("--scope", choices=["msa", "gpu"], default="msa")
    parser.add_argument("--message", default="")
    parser.add_argument("--state", choices=["running", "complete", "failed"], default="running")
    parser.add_argument("--eta-lower", type=int)
    parser.add_argument("--eta-upper", type=int)
    args, command = parser.parse_known_args(argv)
    fields = dict(scope=args.scope, message=args.message)
    if args.eta_lower is not None:
        if args.eta_upper is None or not 0 <= args.eta_lower <= args.eta_upper <= 86400:
            parser.error("Invalid estimated duration range")
        fields["eta"] = dict(state="range", lower_seconds=args.eta_lower, upper_seconds=args.eta_upper,
                             basis="Previous startup observations; capacity and I/O can vary", scope="stage")
    if args.command == "emit":
        if command:
            parser.error("Unexpected emit arguments")
        emit(args.stage, state=args.state, **fields)
        return 0
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("A command is required")
    child = None
    def interrupted(sig, frame):
        if child is not None:
            child.send_signal(sig)
        raise InterruptedError("Worker setup interrupted")
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        with Activity(args.stage, **fields):
            child = subprocess.Popen(command)
            status = child.wait()
            if status:
                raise subprocess.CalledProcessError(status, command)
        return 0
    except subprocess.CalledProcessError as error:
        return error.returncode if error.returncode > 0 else 128-error.returncode
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()


if __name__ == "__main__":
    sys.exit(main())
