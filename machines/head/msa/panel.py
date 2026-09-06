#!/usr/bin/env python3
"""Prepare a complete, frozen protein panel serially using one private MSA API."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlsplit

import databases

MODELS = {"openfold3", "boltz2", "protenix"}
AMINO = set("ACDEFGHIKLMNPQRSTVWY")
CLEANUP_SECONDS = 60
_signal = None
NATIVE_RUNNER = r'''set -euo pipefail
source "$1/recipes/_common.sh"
model="$2"; shift 2
case "$model" in
  openfold3) venv="$SHARED/envs/openfold3"; export OPENFOLD_CACHE="$SHARED/openfold3/home/.openfold3" ;;
  boltz2) venv="$SHARED/envs/boltz"; export BOLTZ_CACHE="$SHARED/cache/boltz" ;;
  protenix) venv="$SHARED/envs/protenix"; export PROTENIX_ROOT_DIR="$SHARED/protenix/release_data" ;;
  *) exit 2 ;;
esac
[ -x "$venv/bin/python" ] || { echo "msa-panel: missing pinned $model environment at $venv" >&2; exit 2; }
export PATH="$venv/bin:/usr/local/cuda/bin:$PATH"
export LD_LIBRARY_PATH="$(venv_ld "$venv")${LD_LIBRARY_PATH:-}"
export CUDA_VISIBLE_DEVICES=""
exec "$venv/bin/python" "$TOOLS/msa/prepared.py" "$@"
'''


def require(value, message):
    if not value:
        raise ValueError(message)


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def manifest(path, expected=None):
    with path.open("rb") as source:
        raw = source.read(16*1024**2 + 1)
    require(len(raw) <= 16*1024**2, "Panel manifest exceeds 16 MiB")
    value = json.loads(raw, object_pairs_hook=unique_keys)
    require(isinstance(value, dict) and set(value) == {"version", "targets"},
            "Panel must contain exactly version and targets")
    require(type(value["version"]) is int and value["version"] == 1, "Panel version must be 1")
    targets = value["targets"]
    require(isinstance(targets, list) and targets, "Panel targets must be a nonempty list")
    seen, sequences = set(), {}
    for target in targets:
        require(isinstance(target, dict) and set(target) == {"name", "model", "sequence"},
                "Each target must contain exactly name, model and sequence")
        name, model, sequence = (target[k] for k in ("name", "model", "sequence"))
        require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", name),
                "Target name must be 1..96 safe letters, digits, dots, underscores or hyphens")
        require(isinstance(model, str) and model in MODELS, "Target model must be openfold3, boltz2 or protenix")
        require(isinstance(sequence, str) and sequence and set(sequence) <= AMINO,
                f"{model}/{name}: sequence must contain uppercase standard amino acids only")
        require((model, name) not in seen, f"Duplicate panel target: {model}/{name}")
        require(name not in sequences or sequences[name] == sequence, f"Inconsistent sequence for case name: {name}")
        seen.add((model, name)); sequences[name] = sequence
    digest = hashlib.sha256(databases.canonical(value)).hexdigest()
    require(expected is None or expected == digest, "Panel manifest changed after validation")
    return value, digest


class Interrupted(Exception):
    def __init__(self, signum):
        self.code = 128 + signum
        super().__init__(f"Panel interrupted by signal {signum}")


def stop(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=3)
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # The group belongs to our start_new_session child. A native client
        # can exit before its subprocesses; terminate those descendants too.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


def command(arguments, log, deadline, reserve=CLEANUP_SECONDS, ignore_signal=False):
    remaining = deadline - time.time() - reserve
    if remaining <= 0:
        raise TimeoutError("Worker time budget is exhausted; preserving cleanup time")
    with log.open("ab") as output:
        process = subprocess.Popen(list(map(str, arguments)), stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            while True:
                if _signal is not None and not ignore_signal:
                    raise Interrupted(_signal)
                remaining = deadline - time.time() - reserve
                if remaining <= 0:
                    raise TimeoutError("Native preparation exceeded the remaining worker time budget")
                try:
                    status = process.wait(timeout=min(remaining, 1))
                    break
                except subprocess.TimeoutExpired:
                    pass
            if status:
                raise subprocess.CalledProcessError(status, arguments)
        finally:
            stop(process)


def native(tools, model, arguments):
    # Input data is passed as positional arguments, never interpolated as shell code.
    return ["bash", "-c", NATIVE_RUNNER, "msa-panel-native", str(tools), model, *map(str, arguments)]


def start_proxy(tools, target, deadline):
    with (target / "api-audit.log").open("ab") as log:
        proxy = subprocess.Popen([sys.executable, str(tools / "msa/server.py"), "proxy", "--audit",
                                  str(target / "api-audit")], stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    try:
        until = min(deadline - CLEANUP_SECONDS, time.time() + 15)
        while time.time() < until:
            if _signal is not None:
                raise Interrupted(_signal)
            if proxy.poll() is not None:
                raise RuntimeError("Target audit proxy exited before becoming ready")
            try:
                with socket.create_connection(("127.0.0.1", 8081), timeout=.2):
                    return proxy
            except OSError:
                time.sleep(.1)
        raise TimeoutError("Target audit proxy did not become ready within the remaining budget")
    except BaseException:
        stop(proxy)
        raise


def prepare_target(target, directory, tools, config, provenance, deadline):
    fasta = directory / "input.fasta"
    fasta.write_text(f'>{target["name"]}\n{target["sequence"]}\n')
    proxy = None
    original_error = None
    try:
        proxy = start_proxy(tools, directory, deadline)
        command(native(tools, target["model"], ["prepare", "--model", target["model"],
                "--fasta", fasta, "--out", directory / "prepared", "--server-url", "http://127.0.0.1:8081",
                "--source", "private", "--database-provenance", provenance]), directory / "prepare.log", deadline)
        command(native(tools, target["model"], ["validate", "--model", target["model"],
                "--fasta", fasta, "--bundle", directory / "prepared"]), directory / "validate.log", deadline)
        require((directory / "prepared/manifest.json").is_file(), "Native preparation returned without a bundle")
    except BaseException as exc:
        original_error = exc
    finally:
        stop(proxy)
        try:
            command([sys.executable, tools / "msa/server.py", "export", "--audit", directory / "api-audit",
                     "--config", config, "--output", directory / "api-jobs"],
                    directory / "api-export.log", min(deadline, time.time()+45), reserve=5, ignore_signal=True)
        except Exception as exc:
            if original_error is None:
                original_error = exc
    if original_error is not None:
        raise original_error
    return databases.digest(directory / "prepared/manifest.json")[0]


def audit_evidence(directory):
    """Check retained request/response bytes and completed ticket exports."""
    exchanges, downloaded = {}, {}
    for path in sorted((directory / "api-audit").glob("*/exchange.json")):
        record = databases.load(path)
        exchanges[path.parent.name] = databases.digest(path)[0]
        if not record.get("complete"):
            continue  # Failed retries remain visible; successful results are required below.
        for kind in ("request", "response"):
            body = path.with_name(kind + ".body")
            require(body.is_file() and body.stat().st_size == record.get(kind + "_bytes")
                    and databases.digest(body)[0] == record.get(kind + "_sha256"),
                    "Retained API body is missing or changed")
        route = urlsplit(record.get("path", "")).path
        if record.get("method") == "GET" and record.get("status") == 200 and route.startswith("/result/download/"):
            ticket = route.removeprefix("/result/download/")
            require(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ticket), "Unsafe result ticket")
            downloaded.setdefault(ticket, set()).add(record["response_sha256"])
    require(downloaded, "No successful raw private API result download was retained")
    exported = databases.load(directory / "api-jobs/tickets.json")
    for ticket, raw_hashes in downloaded.items():
        files = exported.get("tickets", {}).get(ticket, {})
        archive = f"mmseqs_results_{ticket}.tar.gz"
        require({"job.json", "job.fasta", archive} <= set(files), "Completed API ticket is missing raw results")
        require(raw_hashes == {files[archive].get("sha256")}, "API download differs from retained backend result")
        # The official backend puts scripts inside the successful result tar
        # and deletes the loose files. Read headers without extracting anything.
        if not ({"msa.sh", "pair.sh"} & set(files)):
            with tarfile.open(directory / "api-jobs" / ticket / archive, "r|gz") as source:
                require(any(member.name in {"msa.sh", "pair.sh"} and member.isfile() and member.size > 0
                            for member in source), "Completed API archive is missing its generated script")
    for ticket, files in exported.get("tickets", {}).items():
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ticket), "Unsafe exported ticket")
        for name, metadata in files.items():
            require(Path(name).name == name and name not in {".", ".."}, "Unsafe exported filename")
            path = directory / "api-jobs" / ticket / name
            require(path.is_file() and not path.is_symlink() and path.stat().st_size == metadata.get("bytes")
                    and databases.digest(path)[0] == metadata.get("sha256"), "Retained API job file is missing or changed")
    return hashlib.sha256(databases.canonical(dict(exchanges=exchanges, exported=exported))).hexdigest()


def run(path, output, tools, config, provenance, deadline, expected=None):
    value, digest = manifest(path, expected)
    require(math.isfinite(deadline) and deadline > 0, "A finite worker deadline is required")
    configuration = databases.load(config)
    require(configuration.get("server", {}).get("address") == "127.0.0.1:8080",
            "Panel requires the managed private API on localhost:8080")
    require(isinstance(databases.load(provenance), dict), "Missing validated database provenance")
    output.mkdir(parents=True, exist_ok=True)
    require(not (output / "panel.json").exists() and not (output / "targets").exists(),
            "Panel output already exists; retain it and use a fresh job directory")
    databases.write_json(output / "manifest.json", value)
    shutil.copyfile(provenance, output / "preparation-provenance.json")
    receipt = dict(version=1, kind="private-msa-panel", manifest_sha256=digest,
                   provenance_sha256=databases.digest(provenance)[0], started_utc=time.time(),
                   deadline_utc=deadline, inference_run=False, complete=False, targets=[])
    for target in value["targets"]:
        receipt["targets"].append(dict(name=target["name"], model=target["model"],
            sequence_sha256=hashlib.sha256(target["sequence"].encode()).hexdigest(), status="pending",
            directory=f'targets/{target["model"]}/{target["name"]}'))
    databases.write_json(output / "panel.json", receipt)
    interrupted = None
    for target, status in zip(value["targets"], receipt["targets"]):
        directory = output / status["directory"]
        directory.mkdir(parents=True)
        (directory / "input.fasta").write_text(f'>{target["name"]}\n{target["sequence"]}\n')
        status["started_utc"] = time.time()
        status["status"] = "running"
        databases.write_json(directory / "status.json", status)
        databases.write_json(output / "panel.json", receipt)
        try:
            if _signal is not None:
                raise Interrupted(_signal)
            if interrupted is not None:
                raise Interrupted(interrupted.code - 128)
            if deadline - time.time() <= CLEANUP_SECONDS:
                raise TimeoutError("No worker time remains for this target")
            status["bundle_manifest_sha256"] = prepare_target(target, directory, tools, config, provenance, deadline)
            status["audit_sha256"] = audit_evidence(directory)
            status["status"] = "complete"
        except Interrupted as exc:
            interrupted = exc
            status.update(status="interrupted", error=str(exc), exit_status=exc.code)
        except Exception as exc:
            status.update(status="timeout" if isinstance(exc, TimeoutError) else "failed",
                          error=f"{type(exc).__name__}: {exc}", exit_status=getattr(exc, "returncode", 1))
        status["finished_utc"] = time.time()
        databases.write_json(directory / "status.json", status)
        databases.write_json(output / "panel.json", receipt)
        print(f'msa-panel: {target["model"]}/{target["name"]}: {status["status"]}', flush=True)
    receipt.update(complete=all(t["status"] == "complete" for t in receipt["targets"]), finished_utc=time.time())
    databases.write_json(output / "panel.json", receipt)
    return interrupted.code if interrupted else (0 if receipt["complete"] else 1)


def verify(path, output, expected=None):
    value, digest = manifest(path, expected)
    receipt = databases.load(output / "panel.json")
    require(receipt.get("manifest_sha256") == digest and receipt.get("complete") is True,
            "Panel is incomplete or belongs to a different manifest")
    require(receipt.get("inference_run") is False, "Panel receipt does not describe preparation only")
    require(receipt.get("provenance_sha256") == databases.digest(output / "preparation-provenance.json")[0],
            "Panel database provenance changed")
    require(len(receipt.get("targets", [])) == len(value["targets"]), "Panel target count differs from manifest")
    import prepared
    for target, status in zip(value["targets"], receipt["targets"]):
        relative = f'targets/{target["model"]}/{target["name"]}'
        require(status.get("name") == target["name"] and status.get("model") == target["model"]
                and status.get("directory") == relative and status.get("status") == "complete",
                "Panel target identity/order/status changed")
        directory = output / relative
        require(directory.resolve().is_relative_to(output.resolve()), "Panel target escapes result directory")
        require(databases.load(directory / "status.json") == status, "Target status differs from panel receipt")
        require(status.get("sequence_sha256") == hashlib.sha256(target["sequence"].encode()).hexdigest(),
                "Panel query digest changed")
        fasta = directory / "input.fasta"
        require(fasta.read_text() == f'>{target["name"]}\n{target["sequence"]}\n', "Panel target sequence changed")
        require(status.get("bundle_manifest_sha256") == databases.digest(directory / "prepared/manifest.json")[0],
                "Panel bundle manifest changed")
        bundle = prepared.validate(directory / "prepared", target["model"], fasta)
        require(bundle["source"].get("kind") == "private", "Panel target contains a non-private preparation")
        provenance = bundle["source"].get("database_provenance")
        require(provenance and databases.digest(prepared.safe_file(directory / "prepared", provenance))[0]
                == receipt["provenance_sha256"], "Panel target uses different database provenance")
        require(status.get("audit_sha256") == audit_evidence(directory), "Panel API evidence changed")
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "run", "verify"))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--hash-only", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--tools", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--deadline", type=float)
    args = parser.parse_args(argv)
    value, digest = manifest(args.manifest, args.expected_sha256)
    if args.action == "validate":
        print(digest if args.hash_only else json.dumps(dict(manifest_sha256=digest, targets=len(value["targets"]))))
        return 0
    require(args.out is not None, "--out is required")
    if args.action == "verify":
        print(json.dumps(dict(manifest_sha256=digest, targets=len(verify(args.manifest, args.out, digest)["targets"]))))
        return 0
    require(args.config is not None and args.provenance is not None and args.deadline is not None,
            "run requires --config, --provenance and --deadline")
    def interrupt(signum, _frame):
        global _signal
        _signal = signum
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, interrupt)
    return run(args.manifest, args.out, args.tools, args.config, args.provenance, args.deadline, digest)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print(f"msa-panel: ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
