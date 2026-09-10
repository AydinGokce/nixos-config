#!/usr/bin/env python3
"""Head registration and private preparation through one managed MSA session.

Private preparation ensures one shared budgeted session on demand.
Uncertain startups and submitted searches are never silently repeated.
"""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import time
import uuid

import session
import lifecycle

DEFAULT_ROOT = Path(os.environ.get("BIO_MSA_SESSIONS_ROOT", "/var/lib/dc/msa-sessions"))
TOOLS = Path(os.environ.get("BIO_TOOLS_SRC", "/etc/bio-tools"))
KEY = "/root/.ssh/datacrunch_ed25519"


def require(value, message):
    session.require(value, message)


def unit_state(unit, timeout=15):
    fields = ["LoadState", "ActiveState", "SubState", "InvocationID", "MainPID", "ControlPID", "Description", "ExecStart"]
    text = subprocess.check_output(["systemctl", "show", unit]+[a for f in fields for a in ["-p", f]], text=True, timeout=timeout)
    return dict(line.split("=", 1) for line in text.splitlines())


def active(root):
    pointer = lifecycle.document(root/"active.json")
    ident = pointer["session_id"]
    require(isinstance(ident, str) and re.fullmatch(r"[a-f0-9]{32}", ident), "Invalid registered session ID")
    state = root/ident
    intent = lifecycle.document(state/"intent.json")
    require(pointer["intent_sha256"] == session.sha(state/"intent.json") and intent["session_id"] == ident,
            "Active session intent changed")
    return state, intent


def provider_check(tools, instance, ip, closing_os=None, deadline=None):
    """Read-only inventory with the existing private dc credentials wrapper."""
    script = 'set -euo pipefail; source "${DC_CREDENTIALS_FILE:-/root/.config/datacrunch/credentials.env}"; '
    script += 'export DATACRUNCH_CLIENT_ID DATACRUNCH_CLIENT_SECRET; exec "$@"'
    command = ["bash", "-c", script, "msa-session-auth", sys.executable, str(Path(__file__).resolve()),
               "provider-close" if closing_os else "provider-check", "--tools", str(tools), "--instance", instance, "--ip", ip]
    if closing_os: command += ["--os-id", closing_os]
    bound = lifecycle.timeout(deadline, 60)
    try: return json.loads(subprocess.check_output(command, text=True, timeout=bound, stderr=subprocess.PIPE))
    except Exception: raise ValueError("Session provider/budget check failed; no request was submitted") from None


def provider(args):
    helper = runpy.run_path(str(args.tools/"dc-budget.py"))
    controller = helper["Controller"](helper["API"](), helper["Store"](os.environ.get("DC_STATE_DIR", "/var/lib/dc")))
    with controller.store.locked(controller.clock()) as state:
        inventory = controller.refresh(state)
        matches = [(token, job) for token, job in state["jobs"].items() if job.get("id") == args.instance]
        require(len(matches) == 1, "Session worker is not uniquely managed")
        token, job = matches[0]
        if args.action == "provider-close":
            require(job["os_id"] == args.os_id and job["status"] == "closed" and not job.get("cleanup_error"),
                    "Session managed cleanup is not confirmed")
            require(not any(r["id"] == args.instance for r in inventory[0])
                    and not any(r["id"] == args.os_id for r in inventory[1]+inventory[2]),
                    "Session worker or temporary OS is still present")
            return dict(status="closed", instance=args.instance, os_id=args.os_id, token=token,
                        checked_epoch=time.time(), exact_worker_absent=True, exact_os_absent_active_and_trash=True)
        report = helper["summary"](state, controller.clock(), controller.persistent_hours)
        require(not report["uncertain"] and not report["storage_uncertain"]
                and report["spent"]+report["reserved"]+report["background_reserve"]+controller.margin < controller.ceiling,
                "Budget cannot admit session work")
        require(0 <= time.time()-state.get("last_watchdog", 0) < 180, "Budget watchdog is stale")
        require(job["status"] == "running" and time.time()+session.RESERVE < job["deadline"], "Managed worker is closing")
        rows = [r for r in inventory[0] if r["id"] == args.instance]
        require(len(rows) == 1 and rows[0].get("ip") == args.ip
                and rows[0].get("hostname") == "bio-"+token[:12], "Session provider identity differs")
        require(any(r["id"] == job["os_id"] for r in inventory[1])
                and not any(r["id"] == job["os_id"] for r in inventory[2]), "Managed OS disk is not active")
        return dict(instance=args.instance, ip=args.ip, os_id=job["os_id"], token=token,
                    reservation_deadline=job["deadline"], checked_epoch=time.time(),
                    hostname=rows[0]["hostname"], budget=report)


def ssh(launch):
    known = Path(launch["known_hosts"])
    require(session.sha(known) == launch["known_hosts_sha256"], "Pinned session host key changed")
    ipaddress.IPv4Address(launch["ip"])
    return ["ssh", "-i", KEY, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile="+str(known),
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            "root@"+launch["ip"]]


def register_launch(state, job_path, remote_out):
    state = state.resolve(); intent = session.load(state/"intent.json"); job = session.load(job_path)
    require(state.name == intent["session_id"] and job["model"] == "msa", "Wrong session launch")
    require(os.environ.get("INVOCATION_ID") and os.environ.get("BIO_MSA_SESSION_ID") == intent["session_id"],
            "Session must launch under its managed systemd unit")
    live = unit_state(intent["unit"])
    require(live["InvocationID"] == os.environ["INVOCATION_ID"] and live["ActiveState"] == "active",
            "Session launcher unit identity changed")
    require(Path(remote_out).is_absolute() and str(remote_out).startswith("/mnt/bio-shared/runs/msa-")
            and Path(remote_out).name == "out", "Invalid session result path")
    proof = provider_check(Path(intent["tools"]), job["instance"], job["ip"])
    # This fresh allocation uses the project's existing first-connection trust;
    # subsequent requests require these exact recorded host-key bytes and boot.
    known = state/"known_hosts"
    raw = subprocess.check_output(["ssh-keyscan", "-T", "10", "-t", "ed25519", job["ip"]], timeout=15, stderr=subprocess.PIPE)
    require(raw.strip() and all(line.startswith((job["ip"]+" ").encode()) for line in raw.splitlines()), "Invalid SSH host-key scan")
    with known.open("xb") as stream: os.chmod(known, 0o600); stream.write(raw)
    launch = dict(schema=1, session_id=intent["session_id"], job=job["job"], instance=job["instance"], ip=job["ip"],
                  known_hosts=str(known), known_hosts_sha256=session.sha(known), provider=proof,
                  unit=intent["unit"], invocation_id=live["InvocationID"], remote_out=str(remote_out),
                  job_file=str(job_path), job_file_sha256=session.sha(job_path))
    code = "import json,pathlib,socket;print(json.dumps({'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'hostname':socket.gethostname()}))"
    identity = json.loads(subprocess.check_output(ssh(launch)+[shlex.join(["python3", "-B", "-c", code])], text=True, timeout=20))
    require(identity["hostname"] == proof["hostname"], "Worker hostname differs from managed allocation")
    launch.update(identity)
    session.atomic(state/"launch.json", launch, exclusive=True)
    return launch


def ready_session(root, deadline=None):
    state, intent = active(root); launch = session.load(state/"launch.json")
    live = unit_state(intent["unit"], timeout=lifecycle.timeout(deadline, 15))
    require(live["LoadState"] == "loaded" and live["ActiveState"] == "active"
            and live["InvocationID"] == launch["invocation_id"], "Managed private MSA session is not active")
    provider_check(Path(intent["tools"]), launch["instance"], launch["ip"], deadline=deadline)
    ready_path = Path(launch["remote_out"])/"session-ready.json"
    ready = session.load(ready_path); digest = session.sha(ready_path)
    require(ready["session_id"] == intent["session_id"] and ready["owner"]["boot_id"] == launch["boot_id"]
            and ready["output"] == launch["remote_out"] and ready["endpoint"] == "http://127.0.0.1:8080"
            and ready["deadline_epoch"] <= launch["provider"]["reservation_deadline"], "Session readiness identity differs")
    require(ready["sources"] == intent["sources"] == session.sources(Path(intent["tools"])), "Session tools differ from registered source")
    require(ready["state"] == "/tmp/bio-msa-session-"+intent["session_id"], "Unexpected worker session state path")
    command = ["python3", "-B", ready["tools"]+"/msa/session.py", "status", "--state", ready["state"],
               "--expected-ready-sha256", digest]
    observed = json.loads(subprocess.check_output(ssh(launch)+[shlex.join(command)], text=True, timeout=lifecycle.timeout(deadline, 30)))
    require(observed == ready, "Worker and head session readiness differ")
    if intent.get("lifecycle") == "borrowed-api":
        binding = launch["worker_session"]
        command = ["systemctl", "show", binding["unit"], "-p", "InvocationID", "-p", "ActiveState", "-p", "MainPID"]
        text = subprocess.check_output(ssh(launch)+[shlex.join(command)], text=True, timeout=lifecycle.timeout(deadline, 20))
        worker_unit = dict(line.split("=", 1) for line in text.splitlines())
        require(worker_unit["ActiveState"] == "active" and worker_unit["InvocationID"] == binding["invocation_id"]
                and worker_unit["MainPID"] == str(ready["owner"]["pid"]), "Borrowed session control unit changed")
    return state, intent, launch, ready, digest


def adopt(args):
    """Register a separately supervised spool on an already managed worker."""
    root=args.root.resolve();root.mkdir(mode=0o700,parents=True,exist_ok=True)
    ready=session.load(args.ready);require(ready["lifecycle"]=="borrowed-api", "Explicit borrowed API readiness is required")
    ident=ready["session_id"];require(re.fullmatch(r"[a-f0-9]{32}",ident),"Invalid session ID")
    require(args.owner_unit and re.fullmatch(r"bio-[a-z0-9-]+\.service",args.owner_unit)
            and re.fullmatch(r"[a-f0-9]{32}",args.owner_invocation or "")
            and re.fullmatch(r"bio-msa-session-[a-z0-9-]+\.service",args.session_unit or "")
            and re.fullmatch(r"[a-f0-9]{32}",args.session_invocation or ""),"Exact owner/session unit bindings required")
    with(root/"lock").open("a")as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        require(not(root/"active.json").exists(),"A session registration already exists")
        job=session.load(args.job);proof=provider_check(args.tools,job["instance"],job["ip"])
        owner=unit_state(args.owner_unit)
        require(owner["ActiveState"]=="active" and owner["InvocationID"]==args.owner_invocation,"Original owner unit is not active")
        require(ready["deadline_epoch"]<=proof["reservation_deadline"] and ready["state"]=="/tmp/bio-msa-session-"+ident,
                "Borrowed session exceeds managed deadline or state path")
        require(Path(ready["output"]).resolve()==args.ready.resolve().parent and args.ready.name=="session-ready.json",
                "Borrowed readiness output path differs")
        require(ready["sources"]==session.sources(args.tools.resolve()),"Borrowed spool tools differ")
        state=root/ident;state.mkdir(mode=0o700)
        tools=state/"tools";shutil.copytree(args.tools.resolve(),tools,symlinks=False,ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
        intent=dict(schema=1,kind="managed-private-msa-session",lifecycle="borrowed-api",session_id=ident,unit=args.owner_unit,
                    created_epoch=time.time(),tools=str(tools),sources=session.sources(tools),
                    timeout_seconds=max(0,ready["deadline_epoch"]-time.time()),no_resources_allocated=True)
        known=state/"known_hosts";source=Path(args.known_hosts)
        require(session.sha(source)==args.known_hosts_sha256,"Borrowed worker host key changed")
        shutil.copyfile(source,known);os.chmod(known,0o600)
        launch=dict(schema=1,session_id=ident,job=job["job"],instance=job["instance"],ip=job["ip"],known_hosts=str(known),
                    known_hosts_sha256=session.sha(known),provider=proof,unit=args.owner_unit,invocation_id=args.owner_invocation,
                    remote_out=ready["output"],boot_id=ready["owner"]["boot_id"],hostname=proof["hostname"],job_file=str(args.job),
                    worker_session=dict(unit=args.session_unit,invocation_id=args.session_invocation))
        session.atomic(state/"intent.json",intent,exclusive=True);session.atomic(state/"launch.json",launch,exclusive=True)
        session.atomic(root/"active.json",dict(session_id=ident,intent_sha256=session.sha(state/"intent.json")),exclusive=True)
        # A failed final check leaves the registration for explicit inspection.
        ready_session(root)
        return dict(status="adopted",session_id=ident,no_resources_allocated=True,original_cleanup_owner=args.owner_unit)


def start(args):
    root = args.root.absolute()
    with lifecycle.registration_lock(root, time.monotonic() + 30):
        return _start_locked(args)


def _start_locked(args, deadline=None):
    session.finite(args.timeout, "session timeout", 120, 85500)
    session.finite(args.idle_seconds, "idle timeout", 60, 86400)
    root = args.root.absolute()
    require(not os.path.lexists(root/"active.json"), "A session registration exists; inspect/close it before another start")
    ident = uuid.uuid4().hex; state = root/ident; state.mkdir(mode=0o700)
    submit = shutil.which("bio-submit"); require(submit, "bio-submit is unavailable")
    unit = "bio-msa-session-"+ident+".service"
    cap = float(os.environ.get("DC_MAX_INSTANCE_HOURLY", "13"))
    session.finite(cap, "instance hourly ceiling", 0, 1e9)
    source = args.tools.resolve()
    tools = state/"tools"
    shutil.copytree(source, tools, symlinks=False, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    argv = [str(Path(submit).resolve()), "msa", "--sub", "session", "--timeout", str(args.timeout)]
    if args.worker: argv += ["--worker", args.worker]
    if args.spot: argv += ["--spot"]
    intent = dict(schema=1, kind="managed-private-msa-session", session_id=ident, unit=unit,
                  created_epoch=time.time(), timeout_seconds=args.timeout, idle_seconds=args.idle_seconds,
                  warm=args.warm, tools=str(tools), sources=session.sources(tools),
                  submit_sha256=session.sha(Path(submit).resolve()), argv=argv)
    session.atomic(state/"intent.json", intent, exclusive=True)
    session.atomic(root/"active.json", dict(session_id=ident, intent_sha256=session.sha(state/"intent.json")), exclusive=True)
    command = ["systemd-run", "--unit", unit, "--description", "Managed private MSA session "+ident,
               "--service-type=exec", "--property=Restart=no", "--property=KillMode=mixed",
               "--property=TimeoutStopSec=180", "--property=RuntimeMaxSec="+str(args.timeout+1200),
               "--setenv=PATH=/run/current-system/sw/bin:/run/wrappers/bin",
               "--setenv=BIO_TOOLS_SRC="+str(tools), "--setenv=BIO_MSA_SESSION_ID="+ident,
               "--setenv=BIO_MSA_SESSION_STATE="+str(state),
               "--setenv=BIO_MSA_SESSION_IDLE_SECONDS="+str(args.idle_seconds),
               "--setenv=BIO_MSA_SESSION_WARM="+args.warm,
               "--setenv=DC_MAX_INSTANCE_HOURLY="+str(min(cap, 13)), *argv]
    session.atomic(state/"start-intent.json", dict(command=command, started_epoch=time.time()), exclusive=True)
    result = subprocess.run(command, capture_output=True, text=True, timeout=lifecycle.timeout(deadline, 30))
    session.atomic(state/"start-result.json", dict(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr), exclusive=True)
    require(result.returncode == 0, "Session start failed or is uncertain; registration retained, never automatically retried")
    return dict(session_id=ident, unit=unit, state=str(state), status="starting")


def _terminal_unit(live):
    return live.get('LoadState') == 'not-found' or (
        live.get('ActiveState') in {'inactive', 'failed'}
        and live.get('MainPID') == '0' and live.get('ControlPID') == '0')


def _starting_binding(state, intent, live):
    """A successful or lost systemd reply may be joined, never replayed."""
    started = lifecycle.document(state / 'start-intent.json')
    invocation = live.get('InvocationID', '')
    command = re.search(r'argv\[\]=(.*?)(?:\s*;|\s*})', live.get('ExecStart', ''))
    require(re.fullmatch(r'[a-f0-9]{32}', invocation)
            and live.get('Description') == 'Managed private MSA session ' + intent['session_id']
            and command is not None and shlex.split(command.group(1)) == intent['argv']
            and started['command'][-len(intent['argv']):] == intent['argv'],
            'Starting MSA unit does not match its exact saved command and identity')
    expected = {'session_id': intent['session_id'], 'unit': intent['unit'],
                'invocation_id': invocation, 'intent_sha256': session.sha(state / 'intent.json')}
    path = state / 'observed-start.json'
    try:
        session.atomic(path, expected, exclusive=True)
    except FileExistsError:
        pass
    require(lifecycle.document(path) == expected, 'Starting MSA unit invocation was replaced')


def observe_session(root, deadline=None):
    """Report startup separately from corruption; observing never allocates."""
    root = lifecycle.registry_root(root)
    pointer = lifecycle.document(root / 'active.json', optional=True)
    if pointer is None:
        return {'state': 'missing', 'message': 'No private MSA session is registered'}
    ident = pointer.get('session_id') if isinstance(pointer, dict) else None
    try:
        state, intent = active(Path(root))
        ident = intent['session_id']
        launch = lifecycle.document(state / 'launch.json', optional=True)
        live = unit_state(intent['unit'], timeout=lifecycle.timeout(deadline, 15))
        if launch is None:
            if _terminal_unit(live):
                raise lifecycle.SessionError('session_uncertain',
                    'The saved MSA startup has no exact worker registration and its unit is absent or stopped; inspect its retained allocation records before replacement', ident)
            _starting_binding(state, intent, live)
            return {'state': 'starting', 'session_id': ident,
                    'message': 'Waiting for the shared private MSA worker to be allocated and registered'}
        require(launch.get('session_id', ident) == ident
                and launch.get('unit', intent['unit']) == intent['unit'], 'MSA launch registration differs')
        if live.get('LoadState') != 'not-found':
            require(live.get('InvocationID') == launch['invocation_id'], 'Managed MSA unit invocation was replaced')
        observed = lifecycle.document(state / 'observed-start.json', optional=True)
        if observed is not None:
            require(observed['invocation_id'] == launch['invocation_id']
                    and observed['intent_sha256'] == session.sha(state / 'intent.json'),
                    'Registered MSA worker differs from the observed startup')
        if _terminal_unit(live):
            return {'state': 'terminal', 'session_id': ident, 'intent_sha256': session.sha(state / 'intent.json'),
                    'launch_sha256': session.sha(state / 'launch.json'),
                    'message': 'Previous managed MSA session ended; exact cleanup must be confirmed'}
        if live.get('ActiveState') in {'deactivating', 'activating'}:
            return {'state': 'closing' if live['ActiveState'] == 'deactivating' else 'starting',
                    'session_id': ident, 'message': 'Waiting for the managed private MSA service transition'}
        require(live.get('LoadState') == 'loaded' and live.get('ActiveState') == 'active',
                'Managed MSA unit state is not recognized')
        output = Path(launch['remote_out'])
        closed = lifecycle.document(output / 'session-closed.json', optional=True)
        if closed is not None:
            require(closed['session_id'] == ident, 'MSA closure belongs to another session')
            return {'state': 'closing', 'session_id': ident,
                    'message': 'Waiting for the previous private MSA worker and its temporary disk to finish cleanup'}
        if lifecycle.document(output / 'session-ready.json', optional=True) is None:
            return {'state': 'warming', 'session_id': ident,
                    'message': 'Private MSA worker is registered; loading the full database indexes and starting its search service'}
        return {'state': 'ready', 'session_id': ident, 'ready': ready_session(Path(root), deadline=deadline)}
    except lifecycle.SessionError:
        raise
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        # Provider and SSH response bodies can contain credentials; never echo
        # them through progress or turn uncertain health into another launch.
        message = str(exc) if type(exc) is ValueError else type(exc).__name__
        raise lifecycle.SessionError('session_uncertain',
            'Cannot verify the retained private MSA session: ' + message, ident) from exc


def _retire_locked(root, observed, deadline=None):
    """Retire only exact, already-terminal resources; never stop a live unit."""
    state, intent = active(Path(root))
    launch = lifecycle.document(state / 'launch.json')
    require(intent['session_id'] == observed['session_id']
            and session.sha(state / 'intent.json') == observed['intent_sha256']
            and session.sha(state / 'launch.json') == observed['launch_sha256'],
            'MSA registration changed before cleanup reconciliation')
    live = unit_state(intent['unit'], timeout=lifecycle.timeout(deadline, 15))
    require(_terminal_unit(live) and (live.get('LoadState') == 'not-found'
            or live.get('InvocationID') == launch['invocation_id']), 'MSA unit is still live or was replaced')
    proof = provider_check(Path(intent['tools']), launch['instance'], launch['ip'], launch['provider']['os_id'], deadline=deadline)
    require(proof.get('status') == 'closed' and proof.get('instance') == launch['instance']
            and proof.get('os_id') == launch['provider']['os_id'] and proof.get('exact_worker_absent') is True
            and proof.get('exact_os_absent_active_and_trash') is True,
            'Exact private MSA worker and temporary disk cleanup is not confirmed')
    receipt = dict(schema=1, session_id=intent['session_id'], closed_epoch=time.time(),
                   intent_sha256=observed['intent_sha256'], launch_sha256=observed['launch_sha256'], proof=proof)
    if not (state / 'closed.json').exists():
        session.atomic(state / 'closed.json', receipt, exclusive=True)
    # A crash after the receipt leaves active.json in place. The next caller
    # performs fresh identity/provider checks again before removing it.
    (Path(root) / 'active.json').unlink()
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_session(args, deadline, progress=lifecycle.emit):
    settings = argparse.Namespace(**vars(args))
    settings.timeout = getattr(args, 'session_timeout', 7200)
    return lifecycle.ensure(args.root, deadline, observe=lambda: observe_session(args.root, deadline),
        start=lambda: _start_locked(settings, deadline), retire=lambda value: _retire_locked(args.root, value, deadline),
        progress=progress)


def preparation_input(args):
    """Reject malformed molecular requests before any managed startup."""
    require(not args.worker and not args.spot, 'Choose worker overrides only with explicit session start')
    session.finite(args.timeout, 'request timeout', 60, 85500)
    name = args.name or Path(args.fasta or args.json or 'query').stem
    require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', name), 'Invalid target name')
    require(args.model in session.MODELS, 'Invalid preparation model')
    if args.model == "rf3":
        require(args.json and not args.fasta, "RF3 preparation requires per-chain --json")
        queries = lifecycle.document(args.json)
        require(isinstance(queries, dict) and queries and all(isinstance(k, str)
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,31}', k) and isinstance(v, str) and v
                and set(v) <= session.panel.AMINO | {'X'} for k, v in queries.items()), 'Invalid RF3 per-chain queries')
        return name, {'queries': queries}
    else:
        require(args.fasta and not args.json, "Preparation requires --fasta")
        require(args.fasta.is_file() and not args.fasta.is_symlink() and args.fasta.stat().st_size <= 16*1024**2,
                'Canonical protein FASTA must be a regular file no larger than 16 MiB')
        lines = [line.strip() for line in args.fasta.read_text().splitlines() if line.strip()]
        require(lines and lines[0].startswith(">") and sum(line.startswith(">") for line in lines) == 1,
                "Use exactly one canonical protein FASTA")
        sequence = ''.join(lines[1:])
        require(sequence and set(sequence) <= session.panel.AMINO, 'Canonical protein sequence required')
        return name, {'sequence': sequence}


def prepare(args):
    lifecycle.validate_progress_log()
    name, molecular = preparation_input(args)
    deadline = time.monotonic() + args.timeout
    state, intent, launch, ready, digest = (ready_session(args.root, deadline=deadline) if getattr(args, 'require_session', False)
        else ensure_session(args, deadline))
    remaining = min(args.timeout, math.ceil(deadline - time.monotonic()))
    require(remaining >= 60, 'Private MSA startup exhausted the request timeout; no search was submitted')
    ident = uuid.uuid4().hex
    value = dict(schema=1, request_id=ident, session_id=ready['session_id'], ready_sha256=digest,
                 model=args.model, name=name, timeout_seconds=remaining,
                 deadline_epoch=min(time.time()+remaining, ready['deadline_epoch']-session.RESERVE), **molecular)
    request_sha = session.request_document(value, ready, digest)
    local = state/"requests"/ident; local.mkdir(mode=0o700, parents=True)
    session.atomic(local/"request.json", value, exclusive=True)
    command = ["python3", "-B", ready["tools"]+"/msa/session.py", "submit", "--state", ready["state"],
               "--expected-ready-sha256", digest, "--wait-seconds", str(remaining)]
    session.atomic(local/"submission-intent.json", dict(request_sha256=request_sha, command=command,
        launch_sha256=session.sha(state/"launch.json"), epoch=time.time()), exclusive=True)
    try:
        lifecycle.emit('waiting', 'Private MSA search is queued or running on the shared service', ready['session_id'])
        result = subprocess.run(ssh(launch)+[shlex.join(command)], input=session.canonical(value), capture_output=True,
                                timeout=min(remaining+45, max(1, ready["deadline_epoch"]-time.time())))
        with (local/"stdout").open("xb") as out: out.write(result.stdout)
        with (local/"stderr").open("xb") as out: out.write(result.stderr)
        require(result.returncode == 0, "Preparation failed or outcome is uncertain; retained request must be reconciled before retry")
        value = json.loads(result.stdout)
        require(value["status"] == "complete" and value["request_id"] == ident
                and value["request_sha256"] == request_sha and value["ready_sha256"] == digest,
                "Preparation response binding differs")
        bundle = Path(launch["remote_out"])/"requests"/ident/"prepared"
        require(value["bundle"] == str(bundle), "Unexpected prepared bundle location")
        manifest = bundle/("search.json" if args.model == "rf3" else "manifest.json")
        require(session.sha(manifest) == value["bundle_manifest_sha256"], "Retained bundle manifest differs")
        if args.model == "rf3":
            check = [sys.executable, str(Path(intent["tools"])/"rf3/msa.py"), "validate-search", "--input", str(bundle), "--queries", str(args.json)]
        else:
            check = [sys.executable, str(Path(intent["tools"])/"msa/prepared.py"), "validate", "--bundle", str(bundle), "--model", args.model, "--fasta", str(args.fasta)]
        subprocess.run(check, check=True, stdout=subprocess.DEVNULL, timeout=60)
        session.atomic(local/"complete.json", value, exclusive=True)
        receipt = dict(bundle=str(bundle), session_id=ready["session_id"], request_id=ident,
                       ready_sha256=digest, complete_sha256=session.sha(local/"complete.json"))
        if args.bundle_result: session.atomic(args.bundle_result, receipt, exclusive=True)
        lifecycle.emit('ready', 'Private MSA search and prepared input are complete', ready['session_id'])
        return receipt
    except BaseException as exc:
        session.atomic(local/"failure.json", dict(error=type(exc).__name__, request_id=ident,
            message="Request retained; no automatic retry", epoch=time.time()), exclusive=True)
        raise


def stop(root):
    with (root/"lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state, intent = active(root); launch = session.load(state/"launch.json")
        borrowed=intent.get("lifecycle")=="borrowed-api"
        binding=launch["worker_session"] if borrowed else dict(unit=intent["unit"],invocation_id=launch["invocation_id"])
        retired_proof=None
        if borrowed:
            try:
                text=subprocess.check_output(ssh(launch)+[shlex.join(["systemctl","show",binding["unit"],"-p","LoadState","-p","InvocationID","-p","MainPID","-p","ControlPID"])],text=True,timeout=20)
                live=dict(line.split("=",1)for line in text.splitlines())
            except (OSError,subprocess.SubprocessError):
                # The original owner may already have removed this borrowed worker.
                # Failed SSH alone is never evidence of completed cleanup.
                retired_proof=provider_check(Path(intent["tools"]),launch["instance"],launch["ip"],launch["provider"]["os_id"])
                require(retired_proof.get("status")=="closed" and retired_proof.get("instance")==launch["instance"]
                        and retired_proof.get("os_id")==launch["provider"]["os_id"]
                        and retired_proof.get("exact_worker_absent") is True
                        and retired_proof.get("exact_os_absent_active_and_trash") is True,
                        "Borrowed worker cleanup is not confirmed")
                live=dict(LoadState="not-found")
        else:live=unit_state(binding["unit"])
        require(live["LoadState"]=="not-found" or live["InvocationID"]==binding["invocation_id"],"Refusing to stop a replaced session unit")
        if not(state/"stop-intent.json").exists():
            session.atomic(state/"stop-intent.json",dict(**binding,epoch=time.time(),borrowed_api=borrowed),exclusive=True)
        if live["LoadState"]!="not-found":
            command=["systemctl","stop",binding["unit"]]
            if borrowed:command=ssh(launch)+[shlex.join(command)]
            subprocess.run(command,check=True,timeout=200)
        if retired_proof is not None:
            proof=dict(original_worker_cleanup_owner=intent["unit"],provider=retired_proof,
                       original_worker_already_removed=True)
        elif borrowed:
            closed=session.load(Path(launch["remote_out"])/"session-closed.json")
            require(closed["session_id"]==intent["session_id"] and closed["borrowed_api_preserved"] is True,"Borrowed session shutdown unconfirmed")
            proof=dict(borrowed_api_preserved=True,original_worker_cleanup_owner=intent["unit"],spool_closed=closed)
        else:
            proof=provider_check(Path(intent["tools"]),launch["instance"],launch["ip"],launch["provider"]["os_id"])
        if not(state/"closed.json").exists():session.atomic(state/"closed.json",dict(closed_epoch=time.time(),proof=proof),exclusive=True)
        (root/"active.json").unlink()
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        return dict(status="closed",session_id=intent["session_id"],
                    borrowed_api_preserved=borrowed and retired_proof is None,
                    original_worker_already_removed=retired_proof is not None)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["start", "status", "stop", "prepare", "adopt", "register-launch", "provider-check", "provider-close"])
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT); p.add_argument("--tools", type=Path, default=TOOLS)
    p.add_argument("--state", type=Path); p.add_argument("--job", type=Path); p.add_argument("--remote-out")
    p.add_argument("--instance"); p.add_argument("--ip"); p.add_argument("--worker"); p.add_argument("--spot", action="store_true")
    p.add_argument("--timeout", type=int, default=7200); p.add_argument("--idle-seconds", type=int, default=900)
    p.add_argument('--session-timeout', type=int, default=7200,
                   help='Maximum lifetime for a newly started on-demand session; request timeout still includes startup')
    p.add_argument('--require-session', action='store_true',
                   help='Preparation only: require an already-ready session and never start compute')
    p.add_argument("--warm", choices=["report", "prefetch", "lock"], default="prefetch")
    p.add_argument("--model", choices=sorted(session.MODELS)); p.add_argument("--fasta", type=Path)
    p.add_argument("--json", type=Path); p.add_argument("--name"); p.add_argument("--bundle-result", type=Path)
    p.add_argument("--os-id");p.add_argument("--ready",type=Path);p.add_argument("--owner-unit");p.add_argument("--owner-invocation")
    p.add_argument("--session-unit");p.add_argument("--session-invocation");p.add_argument("--known-hosts");p.add_argument("--known-hosts-sha256")
    args = p.parse_args(argv)
    if args.action == "start": value = start(args)
    elif args.action == "register-launch": value = register_launch(args.state, args.job, args.remote_out)
    elif args.action in {"provider-check","provider-close"}: value = provider(args)
    elif args.action == "adopt": value = adopt(args)
    elif args.action == "prepare": value = prepare(args)
    elif args.action == "stop": value = stop(args.root)
    else:
        state, intent, launch, ready, digest = ready_session(args.root)
        value = dict(status="ready", session_id=intent["session_id"], ready=ready, ready_sha256=digest)
    print(json.dumps(value, sort_keys=True, allow_nan=False)); return 0


if __name__ == "__main__":
    try: sys.exit(main())
    except Exception as exc:
        # Provider failures may carry response text; this path never prints it.
        message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else type(exc).__name__
        if len(sys.argv) > 1 and sys.argv[1] == 'prepare':
            try:
                lifecycle.emit('failed', message, getattr(exc, 'session_id', None), getattr(exc, 'code', 'session_failed'))
            except Exception:
                pass
        print("msa-session-client: "+message, file=sys.stderr); sys.exit(2)
