#!/usr/bin/env python3
"""Write the private reference API configuration for a fully validated snapshot."""
import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

import databases
import search_profile


HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}
SECRET_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key"}


def audit_proxy(audit, listen_port=8081, upstream_port=8080):
    """Capture exact target-specific API bodies without buffering large templates."""
    audit.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.0 connection-close framing also handles decoded upstream chunking.
        protocol_version = "HTTP/1.0"

        def forward(self):
            if not self.path.startswith("/") or self.path.startswith("//") or self.headers.get("Transfer-Encoding"):
                self.send_error(400, "A local path and Content-Length body are required")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0:
                    raise ValueError()
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            record = audit / (str(time.time_ns()) + "-" + uuid.uuid4().hex[:8])
            record.mkdir()
            metadata = dict(method=self.command, path=self.path, started_utc=time.time(),
                            request_headers={k: v for k, v in self.headers.items() if k.lower() not in SECRET_HEADERS},
                            complete=False)
            databases.write_json(record / "exchange.json", metadata)
            connection = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=300)
            response_started = False
            try:
                databases.free_space(audit, length + 16 * 1024**2)
                request_sha = hashlib.sha256()
                with (record / "request.body").open("wb") as output:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 4*1024**2))
                        if not chunk:
                            raise OSError("Incomplete request body")
                        output.write(chunk)
                        request_sha.update(chunk)
                        remaining -= len(chunk)
                metadata["request_sha256"] = request_sha.hexdigest()
                metadata["request_bytes"] = length
                excluded = HOP_HEADERS | {"host"}
                excluded |= {v.strip().lower() for v in self.headers.get("Connection", "").split(",")}
                headers = {k: v for k, v in self.headers.items() if k.lower() not in excluded}
                headers["Host"] = f"127.0.0.1:{upstream_port}"
                with (record / "request.body").open("rb") as body:
                    connection.request(self.command, self.path, body=body if length else None, headers=headers)
                response = connection.getresponse()
                metadata["status"] = response.status
                metadata["response_headers"] = {k: v for k, v in response.getheaders() if k.lower() not in SECRET_HEADERS}
                expected_length = response.getheader("Content-Length")
                if expected_length:
                    databases.free_space(audit, int(expected_length) + 16 * 1024**2)
                response_sha, response_bytes = hashlib.sha256(), 0
                with (record / "response.body").open("wb") as output:
                    while chunk := response.read(4*1024**2):
                        output.write(chunk)
                        response_sha.update(chunk)
                        response_bytes += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if expected_length and int(expected_length) != response_bytes:
                    raise OSError("Incomplete upstream response body")
                metadata.update(response_sha256=response_sha.hexdigest(), response_bytes=response_bytes,
                                complete=True)
                # Persist before replying: a caller may stop this proxy as soon as
                # preparation completes. Disk spooling bounds RAM for big bundles.
                databases.write_json(record / "exchange.json", metadata)
                try:
                    self.send_response(response.status, response.reason)
                    for key, value in response.getheaders():
                        if key.lower() not in HOP_HEADERS:
                            self.send_header(key, value)
                    self.end_headers()
                    response_started = True
                    with (record / "response.body").open("rb") as body:
                        shutil.copyfileobj(body, self.wfile, 4*1024**2)
                except (BrokenPipeError, ConnectionResetError):
                    metadata["downstream_disconnected"] = True
            except Exception as exc:
                metadata["error"] = str(exc)
                if not response_started:
                    try:
                        self.send_error(502, "Private API/audit request failed")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            finally:
                connection.close()
                metadata["finished_utc"] = time.time()
                databases.write_json(record / "exchange.json", metadata)

        do_GET = forward
        do_POST = forward

    return ThreadingHTTPServer(("127.0.0.1", listen_port), Handler)


def export_jobs(audit, config_path, output):
    """Retain only tickets actually observed in this job's native client traffic."""
    results = Path(databases.load(config_path)["paths"]["results"]).resolve()
    tickets = set()
    for exchange in sorted(audit.glob("*/exchange.json")):
        metadata = databases.load(exchange)
        body = exchange.parent / "response.body"
        if not metadata.get("complete") or not metadata.get("path", "").startswith("/ticket/"):
            continue
        if not body.is_file() or body.stat().st_size > 1024**2:
            continue
        try:
            ticket = databases.load(body).get("id", "")
        except (ValueError, AttributeError):
            continue
        if isinstance(ticket, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ticket):
            tickets.add(ticket)
    retained = {}
    for ticket in sorted(tickets):
        source = results / ticket
        if source.is_symlink():
            databases.fail(f"Unsafe backend cache directory: {source}")
        target = output / ticket
        target.mkdir(parents=True, exist_ok=True)
        retained[ticket] = {}
        for name in ("job.json", "job.fasta", "msa.sh", "pair.sh", f"mmseqs_results_{ticket}.tar.gz"):
            path = source / name
            if path.is_symlink():
                databases.fail(f"Unsafe backend cache file: {path}")
            if path.is_file():
                databases.free_space(output, path.stat().st_size + 16*1024**2)
                shutil.copyfile(path, target / name)
                retained[ticket][name] = dict(bytes=path.stat().st_size, sha256=databases.digest(target / name)[0])
    databases.write_json(output / "tickets.json", dict(results=str(results), tickets=retained))
    return retained


def configuration(root, results, tools_root, profile=None):
    # Unprofiled historical/direct callers retain their existing environment.
    # New managed searches explicitly select and freeze a reviewed profile.
    selected = profile if profile is not None else os.environ.get(search_profile.ENVIRONMENT_KEY)
    selected = search_profile.resolve(selected) if selected is not None else None
    if selected is not None:
        search_profile.configure_environment(selected)
    ready = databases.validate(root)
    provenance = databases.tools(tools_root, profile=selected) if selected is not None else databases.tools(tools_root)
    # Release18 Parameters.cpp reads MMSEQS_NUM_THREADS before calling
    # omp_set_num_threads; otherwise it uses _SC_NPROCESSORS_ONLN.
    thread_limit = int(os.environ.get("MMSEQS_NUM_THREADS", os.sysconf("SC_NPROCESSORS_ONLN")))
    if thread_limit < 1:
        databases.fail("MMSEQS_NUM_THREADS must be positive")
    runtime = dict(mmseqs_threads=thread_limit,
                   environment={name: os.environ.get(name) for name in
                                search_profile.environment_fields(selected)})
    config = dict(app="colabfold", verbose=True,
                  server=dict(address="127.0.0.1:8080", pathprefix="", dbmanagment=False, cors=False, checkold=True),
                  worker=dict(gracefulexit=True, paralleldatabases=1),
                  local=dict(workers=1, checkold=True), mail=dict(type="null"),
                  paths=dict(databases=str(root), mmseqs=provenance["mmseqs"],
                             colabfold=dict(parallelstages=False, uniref=ready["prefixes"]["uniref30"],
                                            pdb=ready["prefixes"]["pdb100"], environmental=ready["prefixes"]["environmental"],
                                            pdb70=ready["pdb70"], pdbdivided=ready["pdbdivided"],
                                            pdbobsolete=ready["pdbobsolete"])))
    binding = dict(database=ready, tools=provenance, configuration=config, runtime=runtime)
    if selected is not None:
        binding["search_profile"] = selected
    namespace = hashlib.sha256(databases.canonical(binding)).hexdigest()
    jobs = results / namespace
    jobs.mkdir(parents=True, exist_ok=True)
    config["paths"]["results"] = str(jobs)
    receipt = dict(namespace=namespace, database=ready, tools=provenance, runtime=runtime,
                   search_settings="unmodified backend CPU MsaJob/PairJob pipelines; environmental pairing disabled")
    if selected is not None:
        receipt["search_profile"] = selected
        search_profile.validate_configuration(config, receipt, selected)
    return config, receipt


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["config", "proxy", "export"])
    p.add_argument("--root", type=Path, default=Path(os.environ.get("MSA_DB_ROOT", databases.DEFAULT_ROOT)))
    p.add_argument("--tools-root", type=Path, default=os.environ.get("MSA_TOOLS_ROOT"))
    p.add_argument("--results", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--audit", type=Path)
    p.add_argument("--config", type=Path)
    p.add_argument("--search-profile", default=os.environ.get(search_profile.ENVIRONMENT_KEY))
    args = p.parse_args()
    if args.action == "proxy":
        if args.audit is None:
            p.error("proxy requires --audit")
        with audit_proxy(args.audit.resolve()) as proxy:
            proxy.serve_forever()
        return
    if args.action == "export":
        if args.audit is None or args.config is None or args.output is None:
            p.error("export requires --audit, --config, and --output")
        print(json.dumps(export_jobs(args.audit.resolve(), args.config.resolve(), args.output.resolve()), indent=2))
        return
    if args.results is None or args.output is None:
        p.error("config requires --results and --output")
    if args.tools_root is None:
        args.tools_root = Path(databases.DEFAULT_TOOLS).with_name(search_profile.tools_directory(args.search_profile))
    config, receipt = configuration(args.root.resolve(), args.results.resolve(), args.tools_root.resolve(),
                                    profile=args.search_profile)
    databases.write_json(args.output, config)
    databases.write_json(args.output.with_suffix(".provenance.json"), receipt)
    print(json.dumps(dict(config=str(args.output), namespace=receipt["namespace"],
                          command=[receipt["tools"]["server"], "-local", "-config", str(args.output)]), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"msa-server: ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
