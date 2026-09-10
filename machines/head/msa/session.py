#!/usr/bin/env python3
"""Bounded worker-local private MSA service and durable serial request spool.

The enclosing bio-submit owns the cloud reservation and resource cleanup.
This process owns only its API and request children, never provider resources.
"""
import argparse
from contextlib import contextmanager
import ctypes
import errno
import fcntl
import hashlib
import importlib.util
import json
import math
import mmap
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import uuid

import databases
import panel
import prefetch
import server
import worker_controls

SCHEMA = 1
RESERVE = 60
MODELS = panel.MODELS | {"rf3"}
STOP = None
API_LOCK = Path("/run/lock/bio-private-msa-api.lock")


def require(value, message):
    if not value:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 16*1024**2,
            "Missing, unsafe, or oversized session document")
    return json.loads(path.read_bytes(), object_pairs_hook=panel.unique_keys)


def atomic(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name("."+path.name+"."+uuid.uuid4().hex)
    try:
        with temp.open("xb") as stream:
            os.chmod(temp, 0o600)
            stream.write(canonical(value)+b"\n"); stream.flush(); os.fsync(stream.fileno())
        if exclusive:
            os.link(temp, path)
            temp.unlink()
        else:
            os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def finite(value, name, low, high):
    require(type(value) in (int, float) and math.isfinite(value) and low <= value <= high,
            "Invalid "+name)
    return value


def identity(pid=None):
    pid = pid or os.getpid()
    root = Path("/proc")/str(pid)
    stat = (root/"stat").read_text().rsplit(") ", 1)[1].split()
    return dict(pid=pid, start_ticks=int(stat[19]),
                boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip())


class BorrowedAPI:
    """Observe an exact external API; deliberately has no wait/terminate method."""
    def __init__(self, expected, argv):
        self.expected = expected; self.pid = expected["pid"]; self.argv = argv
        require(self.poll() is None, "Borrowed API identity is not live")

    def poll(self):
        try:
            root = Path("/proc")/str(self.pid)
            found = [v.decode() for v in (root/"cmdline").read_bytes().split(b"\0") if v]
            state = (root/"stat").read_text().rsplit(") ", 1)[1].split()[0]
            return None if identity(self.pid) == self.expected and found == self.argv and state not in {"Z", "X", "x", "T", "t"} else 1
        except (OSError, ValueError): return 1


def adopted_api(path, database, deadline):
    value = load(path)
    require(value.get("schema") == SCHEMA and value.get("kind") == "borrowed-private-msa-api"
            and time.time() < deadline <= value["original_work_deadline_epoch"], "Invalid borrowed API deadline contract")
    for name in ("config", "provenance"):
        require(sha(value[name]["path"]) == value[name]["sha256"], "Borrowed API "+name+" changed")
    config = load(value["config"]["path"]); provenance = load(value["provenance"]["path"])
    require(config["server"]["address"] == "127.0.0.1:8080" and Path(config["paths"]["databases"]).resolve() == database.resolve(),
            "Borrowed API is not the full local database service")
    require(provenance["database"] == databases.validate(database.resolve()), "Borrowed API full database receipt differs")
    require(provenance["tools"]["server"] == value["api_argv"][0]
            and value["api_argv"] == [provenance["tools"]["server"], "-local", "-config", value["config"]["path"]],
            "Borrowed API native command differs")
    require(value["panel"]["identity"]["boot_id"] == value["api"]["boot_id"], "Panel/API boot mismatch")
    panel.manifest(Path(value["panel"]["manifest"]), value["panel"]["manifest_sha256"])
    return value, config, provenance, BorrowedAPI(value["api"], value["api_argv"])


def request_gate(adoption):
    """Wait for the original native panel to finish before using its proxy port."""
    if adoption:
        original = adoption["panel"]; pid = original["identity"]["pid"]
        require(identity(pid) == original["identity"], "Original panel identity changed")
        fields = (Path("/proc")/str(pid)/"stat").read_text().rsplit(") ", 1)[1].split()
        receipt = load(original["receipt"])
        require(receipt["manifest_sha256"] == original["manifest_sha256"]
                and receipt["deadline_utc"] == adoption["original_work_deadline_epoch"], "Original panel receipt binding changed")
        if fields[0] not in {"Z", "X", "x"}: return "waiting_original_panel"
        require(fields[0] == "Z" and int(fields[49]) == 0 and receipt.get("complete") is True
                and receipt.get("targets") and all(v["status"] == "complete" for v in receipt["targets"]),
                "Original panel did not complete successfully")
    with socket.socket() as probe:
        try: probe.bind(("127.0.0.1", 8081))
        except OSError: return "waiting_proxy_owner"
    return None


def stop_request(process):
    if process is None: return
    if process.poll() is None:
        try: os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: process.wait(timeout=50)  # Native child shutdown + retained audit export.
        except subprocess.TimeoutExpired: pass
    panel.stop(process)


def sources(tools):
    names = ["msa/session.py", "msa/session_client.py", "msa/panel.py", "msa/prepared.py", "msa/server.py",
             "msa/databases.py", "recipes/_common.sh", "rf3/msa.py"]
    # Existing frozen sessions predate the head lifecycle helper. New snapshots
    # include and bind it without invalidating those immutable old tool trees.
    for extra in ('msa/lifecycle.py', 'msa/prefetch.py', 'msa/worker_controls.py', 'msa/head_controls.py', 'py/worker_progress.py'):
        if (tools / extra).exists(): names.append(extra)
    return {name: sha(tools/name) for name in names}


def index_paths(database, provenance):
    paths = []
    for key in ("uniref30", "environmental", "pdb100"):
        path = Path(provenance["database"]["prefixes"][key]+".idx")
        require(path.resolve().is_relative_to(database.resolve()) and path.is_file()
                and not path.is_symlink() and path.stat().st_size > 0, "Invalid full CPU index")
        paths.append(path)
    return paths


class IndexCache:
    """Measure Linux page residency; optional read-only prefetch or mlock.

    ACCESS_COPY permits ctypes to obtain an address without writing the file.
    Only reads, mincore, mlock and munlock operate on these private mappings.
    """
    def __init__(self, paths):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.page = mmap.PAGESIZE
        self.entries = []
        self.locked = []
        try:
            for path in paths:
                with path.open("rb") as stream:
                    mapping = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_COPY)
                address = ctypes.addressof(ctypes.c_char.from_buffer(mapping))
                self.entries.append((path, mapping, address))
        except BaseException:
            self.close(); raise

    def residency(self, deadline):
        rows = []
        for path, mapping, address in self.entries:
            count = 0; total = (len(mapping)+self.page-1)//self.page
            for start in range(0, len(mapping), 256*1024**2):
                require(STOP is None and time.time() < deadline, "Index residency inspection interrupted or timed out")
                length = min(256*1024**2, len(mapping)-start)
                pages = (length+self.page-1)//self.page
                vector = (ctypes.c_ubyte*pages)()
                status = self.libc.mincore(ctypes.c_void_p(address+start), ctypes.c_size_t(length), vector)
                if status: raise OSError(ctypes.get_errno(), "Index residency inspection failed")
                count += sum(byte & 1 for byte in vector)
            rows.append(dict(path=str(path), bytes=len(mapping), pages=total, resident_pages=count))
        return dict(indexes=rows, total_bytes=sum(r["bytes"] for r in rows),
                    fully_resident=all(r["pages"] == r["resident_pages"] for r in rows))

    def warm(self, mode, deadline, headroom, *, prefetch_state=None, progress=None):
        require(mode in {"report", "prefetch", "lock"}, "Unknown index warm mode")
        before = self.residency(deadline)
        info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        available = int(info["MemAvailable"].split()[0])*1024
        total = int(info["MemTotal"].split()[0])*1024
        nonresident = sum((r["pages"]-r["resident_pages"])*self.page for r in before["indexes"])
        if mode != "report":
            require(total >= before["total_bytes"]+headroom and available >= nonresident+headroom,
                    "Full index warm-up requires index bytes plus requested RAM headroom")
            loading = prefetch.load([path for path, _, _ in self.entries], deadline,
                lambda: STOP is not None, state=prefetch_state, progress=progress)
            if mode == "lock":
                for _, mapping, address in self.entries:
                    require(STOP is None and time.time() < deadline, "Index warm-up interrupted or timed out")
                    if self.libc.mlock(ctypes.c_void_p(address), ctypes.c_size_t(len(mapping))):
                        raise OSError(ctypes.get_errno(), "Cannot lock full indexes; configure sufficient LimitMEMLOCK")
                    self.locked.append((address, len(mapping)))
        after = self.residency(deadline)
        require(mode == "report" or after["fully_resident"], "Index pages were evicted during warm-up")
        return dict(mode=mode, checked_epoch=time.time(), before=before, after=after,
                    headroom_bytes=headroom, locked=mode == "lock",
                    loading=loading if mode != 'report' else None,
                    residency_guarantee="session lifetime" if mode == "lock" else "observation only")

    def close(self):
        for address, length in self.locked:
            self.libc.munlock(ctypes.c_void_p(address), ctypes.c_size_t(length))
        self.locked.clear()
        for _, mapping, _ in self.entries: mapping.close()
        self.entries.clear()


def check_ready(state, expected=None, now=None):
    now = time.time() if now is None else now
    path = state/"ready.json"; ready = load(path)
    require(expected is None or sha(path) == expected, "Session generation changed")
    require(ready["schema"] == SCHEMA and ready["kind"] == "private-msa-session"
            and re.fullmatch(r"[a-f0-9]{32}", ready["session_id"]), "Invalid session readiness")
    require(identity(ready["owner"]["pid"]) == ready["owner"], "Session owner process changed")
    require(identity(ready["api"]["pid"]) == ready["api"], "Private API process changed")
    require(not (state/"closed.json").exists(), "Private MSA session has closed")
    health = load(state/"health.json")
    require(health["session_id"] == ready["session_id"] and health["ready_sha256"] == sha(path)
            and health["status"] in {"ready", "busy", "waiting"} and 0 <= now-health["checked_epoch"] <= 30,
            "Private MSA session health is stale or closing")
    require(now < ready["deadline_epoch"]-RESERVE, "Private MSA session time is exhausted")
    require(sha(ready["config"]) == ready["config_sha256"]
            and sha(ready["provenance"]) == ready["provenance_sha256"], "Session API provenance changed")
    return ready


def request_document(value, ready, expected):
    common = {"schema", "request_id", "session_id", "ready_sha256", "model", "name", "timeout_seconds", "deadline_epoch"}
    require(isinstance(value, dict) and value.get("model") in MODELS, "Invalid preparation model")
    field = "queries" if value["model"] == "rf3" else "sequence"
    require(set(value) == common | {field} and value["schema"] == SCHEMA,
            "Unknown or missing preparation request fields")
    require(re.fullmatch(r"[a-f0-9]{32}", value["request_id"] or "") is not None, "Invalid request ID")
    require(value["session_id"] == ready["session_id"] and value["ready_sha256"] == expected,
            "Preparation request belongs to another session generation")
    require(isinstance(value["name"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value["name"]),
            "Invalid target name")
    finite(value["timeout_seconds"], "request timeout", 60, 85500)
    finite(value["deadline_epoch"], "request absolute deadline", ready["created_epoch"], ready["deadline_epoch"]-RESERVE)
    if field == "sequence":
        seq = value[field]
        require(isinstance(seq, str) and seq and set(seq) <= panel.AMINO, "Canonical protein sequence required")
    else:
        queries = value[field]
        require(isinstance(queries, dict) and queries and all(isinstance(k, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", k) and isinstance(v, str) and v
                and set(v) <= panel.AMINO | {"X"} for k, v in queries.items()), "Invalid RF3 per-chain queries")
    return hashlib.sha256(canonical(value)).hexdigest()


def execute(state, request_id):
    def interrupted(sig, frame):
        panel._signal = sig
        raise panel.Interrupted(sig)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(sig, interrupted)
    ready = check_ready(state)
    path = state/"requests"/(request_id+".json"); value = load(path)
    digest = request_document(value, ready, sha(state/"ready.json"))
    output = Path(ready["output"])/"requests"/request_id
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    atomic(output/"request.json", value, exclusive=True)
    deadline = min(ready["deadline_epoch"], value["deadline_epoch"])
    tools = Path(ready["tools"])
    require(sources(tools) == ready["sources"], "Session preparation source changed")
    result = dict(schema=SCHEMA, request_id=request_id, request_sha256=digest,
                  session_id=ready["session_id"], ready_sha256=sha(state/"ready.json"),
                  started_epoch=time.time(), deadline_epoch=deadline, status="running")
    atomic(output/"status.json", result)
    try:
        require(time.time()+panel.CLEANUP_SECONDS < deadline, "Queued preparation deadline is exhausted")
        config, provenance = Path(ready["config"]), Path(ready["provenance"])
        if value["model"] in panel.MODELS:
            target = {k:value[k] for k in ("model", "name", "sequence")}
            manifest = panel.prepare_target(target, output, tools, config, provenance, deadline)
        else:
            atomic(output/"queries.json", value["queries"], exclusive=True)
            proxy = panel.start_proxy(tools, output, deadline)
            try:
                panel.command([sys.executable, tools/"rf3/msa.py", "search", "--queries", output/"queries.json",
                    "--out", output/"prepared", "--server-url", "http://127.0.0.1:8081", "--source", "private",
                    "--database-provenance", provenance, "--deadline", str(deadline)], output/"prepare.log", deadline)
            finally:
                panel.stop(proxy)
                panel.command([sys.executable, tools/"msa/server.py", "export", "--audit", output/"api-audit",
                    "--config", config, "--output", output/"api-jobs"], output/"api-export.log", deadline, reserve=5)
            panel.command([sys.executable, tools/"rf3/msa.py", "validate-search", "--input", output/"prepared",
                           "--queries", output/"queries.json"], output/"validate.log", deadline)
            manifest = sha(output/"prepared/search.json")
        audit = panel.audit_evidence(output)
        require(sources(tools) == ready["sources"], "Preparation source changed during request")
        result.update(status="complete", bundle=str(output/"prepared"), bundle_manifest_sha256=manifest,
                      audit_sha256=audit)
    except BaseException as exc:
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    result["finished_epoch"] = time.time()
    atomic(output/"status.json", result)
    return 0 if result["status"] == "complete" else 1


def startup_progress(output, session_id, stage, state, message, **extra):
    event = dict(schema=1, stage=stage, scope='msa', state=state, message=message,
                 stage_id=session_id+':'+stage, timestamp_ns=time.time_ns(), **extra)
    line = 'BIO_WORKER_STAGE '+canonical(event).decode()
    require(len(line.encode()) <= 4096, 'Worker progress event too large')
    atomic(Path(output)/'startup-progress.json', event)
    if os.environ.get('BIO_WORKER_PROGRESS_LOG'):
        import head_controls
        head_controls.forward_progress(event)
    else: print(line, file=sys.stderr, flush=True)


class WarmProgress:
    """Estimate from actual read deltas, excluding initial residency inspection."""
    def __init__(self):
        self.first_read = None
        self.last_emitted = None
        self.verifying = False

    def observe(self, value, now):
        completed, total = value['read_bytes'], value['total_bytes']
        verifying = completed == total
        if self.first_read is None and completed > 0 and not verifying:
            self.first_read = (now, completed)
        if (self.last_emitted is not None and now-self.last_emitted < 1
                and verifying == self.verifying):
            return None
        self.last_emitted = now
        self.verifying = verifying
        eta = {'state': 'unknown', 'scope': 'stage',
               'basis': 'Waiting for at least five seconds of increasing buffered-read observations'}
        message = 'Prefetching private MSA index pages'
        if verifying:
            message = 'All index bytes read; verifying full page residency'
            eta['basis'] = 'Final page-residency verification has no measured completion rate'
        elif self.first_read is not None:
            elapsed = now-self.first_read[0]
            delta = completed-self.first_read[1]
            if elapsed >= 5 and delta > 0:
                seconds = (total-completed)*elapsed/delta
                if math.isfinite(seconds) and 0 <= seconds <= 604800:
                    eta = {'state': 'estimate', 'seconds': seconds, 'scope': 'stage',
                           'basis': 'Observed buffered-read byte deltas; residency verification follows'}
        return dict(message=message, completed=completed, total=total, unit='bytes', eta=eta)


@contextmanager
def startup_activity(tools, output, session_id, stage, message):
    helper = Path(tools)/'py/worker_progress.py'
    if helper.is_file():
        spec = importlib.util.spec_from_file_location('msa_worker_progress', helper)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        previous = os.environ.get('BIO_WORKER_PROGRESS_JSON')
        os.environ['BIO_WORKER_PROGRESS_JSON'] = str(Path(output)/'startup-progress.json')
        try:
            with module.Activity(stage, scope='msa', message=message, stage_id=session_id+':'+stage): yield
        finally:
            if previous is None: os.environ.pop('BIO_WORKER_PROGRESS_JSON', None)
            else: os.environ['BIO_WORKER_PROGRESS_JSON'] = previous
    else:
        startup_progress(output, session_id, stage, 'running', message)
        yield
        startup_progress(output, session_id, stage, 'complete', message)


def serve(args):
    global STOP
    require(re.fullmatch(r"[a-f0-9]{32}", args.session_id), "Invalid session ID")
    finite(args.deadline, "session deadline", time.time()+RESERVE, time.time()+85500)
    finite(args.idle_seconds, "idle timeout", 60, 86400)
    finite(args.headroom_gib, "RAM headroom", 16, 1024)
    if args.warm_seconds is not None:
        finite(args.warm_seconds, "warm-up timeout", 1, 85500)
    state = args.state.resolve(); output = args.out.resolve(); tools = args.tools.resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=True)
    lock = open(API_LOCK, "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    adoption = None
    if not getattr(args, "adopt", None):
        with socket.socket() as probe: probe.bind(("127.0.0.1", 8080))
    api = child = cache = None; ready = None; reason = "failed"; current = None; stage = 'database_check'
    def interrupted(sig, frame):
        global STOP
        STOP = sig
    previous = {s:signal.signal(s, interrupted) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        os.environ["MMSEQS_NUM_THREADS"] = "16"
        os.environ["BIO_TOOLS_DIR"] = str(tools)
        atomic(output/'session-starting.json', dict(schema=1, session_id=args.session_id, owner=identity(),
            deadline_epoch=args.deadline, idle_seconds=args.idle_seconds), exclusive=True)
        with startup_activity(tools, output, args.session_id, 'database_check', 'Verifying full private MSA database indexes'):
            if getattr(args, "adopt", None):
                adoption, config, provenance, api = adopted_api(args.adopt, args.database, args.deadline)
            else:
                config, provenance = server.configuration(args.database.resolve(), args.results.resolve(), args.tools_root.resolve())
        atomic(output/"msa-server.json", config, exclusive=True)
        atomic(output/"msa-server.provenance.json", provenance, exclusive=True)
        cache = IndexCache(index_paths(args.database, provenance))
        warm_deadline = args.deadline-RESERVE
        if args.warm_seconds is not None:
            warm_deadline = min(warm_deadline, time.time()+args.warm_seconds)
        warm_progress = WarmProgress()
        def loading_progress(value):
            fields = warm_progress.observe(value, time.monotonic())
            if fields is not None:
                startup_progress(output, args.session_id, 'index_warm', 'running', **fields)
        stage = 'index_warm'
        startup_progress(output, args.session_id, stage, 'running', 'Loading and checking full private MSA index residency')
        warm = cache.warm(args.warm, warm_deadline, int(args.headroom_gib*1024**3),
                         prefetch_state=Path('/tmp')/('bio-msa-prefetch-'+args.session_id), progress=loading_progress)
        atomic(output/"warm-index.json", warm, exclusive=True)
        startup_progress(output, args.session_id, 'index_warm', 'complete',
                         'Full private MSA index residency verified' if warm['after']['fully_resident'] else 'Private MSA index residency observation complete',
                         completed=warm['after']['total_bytes'], total=warm['after']['total_bytes'], unit='bytes')
        if not adoption:
            log = (output/"msa-server.log").open("ab")
            api = subprocess.Popen([provenance["tools"]["server"], "-local", "-config", str(output/"msa-server.json")],
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            log.close()
        until = min(time.time()+120, args.deadline-RESERVE)
        while True:
            require(STOP is None and api.poll() is None and time.time() < until, "Private API startup failed")
            try:
                with socket.create_connection(("127.0.0.1", 8080), timeout=.2): break
            except OSError: time.sleep(.2)
        ready = dict(schema=SCHEMA, kind="private-msa-session", session_id=args.session_id,
                     owner=identity(), api=identity(api.pid), created_epoch=time.time(),
                     deadline_epoch=args.deadline, idle_seconds=args.idle_seconds,
                     endpoint="http://127.0.0.1:8080", endpoint_scope="worker loopback via pinned SSH",
                     state=str(state), output=str(output), tools=str(tools), sources=sources(tools),
                     config=str(output/"msa-server.json"), config_sha256=sha(output/"msa-server.json"),
                     provenance=str(output/"msa-server.provenance.json"),
                     provenance_sha256=sha(output/"msa-server.provenance.json"), namespace=provenance["namespace"],
                     warm_sha256=sha(output/"warm-index.json"), database=provenance["database"])
        ready.update(lifecycle="borrowed-api" if adoption else "owned-api", adoption=adoption,
                     adoption_sha256=sha(args.adopt) if adoption else None, controls_version=0 if adoption else 1)
        atomic(state/"ready.json", ready, exclusive=True)
        ready_sha = sha(state/"ready.json")
        (state/"requests").mkdir(mode=0o700)
        worker_controls.initialize(ready, ready_sha)
        atomic(state/"health.json", dict(schema=SCHEMA, session_id=args.session_id, ready_sha256=ready_sha,
            checked_epoch=time.time(), status="ready", request_id=None))
        atomic(output/"session-ready.json", ready, exclusive=True)
        stage = 'ready'
        startup_progress(output, args.session_id, stage, 'complete', 'Shared private MSA worker is ready')
        processed = set()
        monotonic_deadline = time.monotonic()+max(0,args.deadline-time.time())
        while STOP is None and time.time() < args.deadline-RESERVE and time.monotonic() < monotonic_deadline-RESERVE:
            require(api.poll() is None, "Private API exited")
            if child is not None and child.poll() is not None:
                terminal = load(output/"requests"/current/"status.json")
                require(terminal["status"] in {"complete", "failed"} and terminal["request_id"] == current,
                        "Request supervisor exited without a terminal receipt; closing session")
                stop_request(child); child = None; current = None
            waiting = request_gate(adoption) if child is None else None
            with worker_controls.locked(state):
                queued = sorted(p for p in (state/"requests").glob("*.json") if p.stem not in processed)
                control, close = worker_controls.update_locked(ready, ready_sha,
                    busy=child is not None or waiting is not None, queued=len(queued), active=current)
                if close:
                    reason = 'graceful_shutdown' if control['shutdown_requested'] else 'idle_timeout'
                    break
                if child is None and queued and waiting is None:
                    item = queued[0]; request = load(item)
                    request_document(request, ready, ready_sha)
                    processed.add(item.stem); current = item.stem
                    atomic(state/"claims"/(item.stem+".json"), dict(request_sha256=sha(item), claimed_epoch=time.time()), exclusive=True)
                    with (output/("request-"+item.stem+".log")).open("ab") as log:
                        child = subprocess.Popen([sys.executable, str(tools/"msa/session.py"), "execute", "--state", str(state),
                                                  "--request-id", item.stem], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    control, _ = worker_controls.update_locked(ready, ready_sha,
                        busy=True, queued=len(queued)-1, active=current)
                live_status = worker_controls.snapshot(control)
                atomic(output/'worker-status.json', dict(schema=1, session_id=args.session_id, ready_sha256=ready_sha,
                    owner=ready['owner'], **live_status))
            atomic(state/"health.json", dict(schema=SCHEMA, session_id=args.session_id, ready_sha256=ready_sha,
                checked_epoch=time.time(), status="busy" if child else "waiting" if waiting else "ready", request_id=current, waiting_reason=waiting))
            time.sleep(.5)
        else: reason = "cancelled" if STOP else "maximum_lifetime"
        return 0
    except BaseException as error:
        try: startup_progress(output, args.session_id, stage, 'failed', type(error).__name__+': '+str(error)[:512])
        except Exception: pass
        raise
    finally:
        stop_request(child)
        if not adoption: panel.stop(api)
        if cache: cache.close()
        closed = dict(schema=SCHEMA, session_id=args.session_id, reason=reason, closed_epoch=time.time(),
                      request_id=current, borrowed_api_preserved=bool(adoption), provider_cleanup_owner="enclosing managed bio-submit")
        atomic(state/"closed.json", closed, exclusive=True)
        atomic(output/"session-closed.json", closed, exclusive=True)
        for s, handler in previous.items(): signal.signal(s, handler)
        lock.close()


def submit(state, expected, value, wait_seconds):
    ready = check_ready(state, expected)
    digest = request_document(value, ready, expected)
    finite(wait_seconds, "request wait", 0, 85500)
    require(time.time() < value["deadline_epoch"] <= time.time()+value["timeout_seconds"]+5,
            "Request deadline is expired or extends its declared timeout")
    path = state/"requests"/(value["request_id"]+".json")
    if ready.get('controls_version') == 1:
        with worker_controls.locked(state):
            if path.exists():
                require(load(path) == value, 'Existing request ID has different input; never overwrite or retry')
            else:
                control = worker_controls.checked(ready, expected)
                require(not control['shutdown_requested'] and not control['closing'], 'Shared MSA worker is draining; new searches are not accepted')
                require(control['idle_deadline_epoch'] is None or time.time() < control['idle_deadline_epoch'], 'Shared MSA idle lease expired')
                atomic(path, value, exclusive=True)
                queued = sum(not (state/'claims'/(p.stem+'.json')).exists() for p in (state/'requests').glob('*.json'))
                worker_controls.update_locked(ready, expected, busy=control['busy'], queued=queued, active=control['active_request_id'])
    else:
        try: atomic(path, value, exclusive=True)
        except FileExistsError:
            require(load(path) == value, "Existing request ID has different input; never overwrite or retry")
    until = min(time.monotonic()+wait_seconds, time.monotonic()+ready["deadline_epoch"]-time.time()-RESERVE)
    status_path = Path(ready["output"])/"requests"/value["request_id"]/"status.json"
    while True:
        if status_path.exists():
            result = load(status_path)
            require(result["request_sha256"] == digest and result["ready_sha256"] == expected, "Request result binding changed")
            if result["status"] in {"complete", "failed"}: return result
        require(time.monotonic() < until, "Request remains pending; retain its ID and inspect status, never silently retry")
        check_ready(state, expected); time.sleep(.5)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["serve", "submit", "execute", "status", "control"])
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--session-id"); p.add_argument("--request-id"); p.add_argument("--expected-ready-sha256")
    p.add_argument("--out", type=Path); p.add_argument("--database", type=Path)
    p.add_argument("--tools", type=Path); p.add_argument("--tools-root", type=Path)
    p.add_argument("--results", type=Path); p.add_argument("--deadline", type=float)
    p.add_argument("--idle-seconds", type=float, default=900)
    p.add_argument("--warm", choices=["report", "prefetch", "lock"], default="prefetch")
    p.add_argument("--warm-seconds", type=float, default=None,
                   help="Optional shorter warm-up timeout; default uses remaining session lifetime minus cleanup reserve")
    p.add_argument("--headroom-gib", type=float, default=64)
    p.add_argument("--adopt", type=Path, help="Explicit operational binding to an existing API; never owns its shutdown")
    p.add_argument("--wait-seconds", type=float, default=7200)
    args = p.parse_args(argv)
    if args.action == "serve": return serve(args)
    if args.action == "execute": return execute(args.state, args.request_id)
    if args.action == "status":
        print(json.dumps(check_ready(args.state, args.expected_ready_sha256), sort_keys=True)); return 0
    if args.action == 'control':
        ready = check_ready(args.state, args.expected_ready_sha256)
        require(ready.get('controls_version') == 1, 'This worker generation does not support shared controls')
        value = json.loads(sys.stdin.buffer.read(8193), object_pairs_hook=panel.unique_keys)
        result = worker_controls.apply(ready, args.expected_ready_sha256, value)
        print(json.dumps(result, sort_keys=True)); return 0
    value = json.loads(sys.stdin.buffer.read(16*1024**2+1), object_pairs_hook=panel.unique_keys)
    result = submit(args.state, args.expected_ready_sha256, value, args.wait_seconds)
    print(json.dumps(result, sort_keys=True)); return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    try: sys.exit(main())
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print("msa-session: "+str(exc), file=sys.stderr); sys.exit(2)
