"""Private panel behavior with real portable bundles and local-only HTTP/processes."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import databases
import panel
import prepared


def target(name, model="protenix", sequence="ACDE"):
    return dict(name=name, model=model, sequence=sequence)


def fixture_bundle(item, directory, provenance):
    """Real native-layout capture and validation; synthetic sequences are not search evidence."""
    (directory / "input.fasta").write_text(f'>{item["name"]}\n{item["sequence"]}\n')
    work = directory / "prepared.native-work"
    msa = work / "protenix/msa/unpaired.a3m"
    msa.parent.mkdir(parents=True)
    msa.write_text(f'>query\n{item["sequence"]}\n>hit species=42\n{item["sequence"]}\n')
    prepared.write_json(work / "input-update-msa.json", [dict(name=item["name"], sequences=[dict(
        proteinChain=dict(sequence=item["sequence"], count=1, unpairedMsaPath=str(msa)))])])
    prepared.capture("protenix", work, directory / "prepared", source="private", database_provenance=provenance)
    return databases.digest(directory / "prepared/manifest.json")[0]


def fixture_audit(directory):
    body = b"synthetic raw response"
    exchange = directory / "api-audit/1"
    exchange.mkdir(parents=True)
    (exchange / "request.body").write_bytes(b"")
    (exchange / "response.body").write_bytes(body)
    databases.write_json(exchange / "exchange.json", dict(complete=True, method="GET", status=200,
        path="/result/download/ticket1", request_bytes=0, response_bytes=len(body),
        request_sha256=hashlib.sha256(b"").hexdigest(), response_sha256=hashlib.sha256(body).hexdigest()))
    files = {}
    job = directory / "api-jobs/ticket1"
    job.mkdir(parents=True)
    for name in ("job.json", "job.fasta", "msa.sh", "mmseqs_results_ticket1.tar.gz"):
        (job / name).write_bytes(body)
        files[name] = dict(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
    databases.write_json(job.parent / "tickets.json", dict(results="/fixture-cache", tickets=dict(ticket1=files)))


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "panel.json"
        self.config = self.root / "server.json"
        self.provenance = self.root / "provenance.json"
        self.output = self.root / "output"
        self.tools = Path(__file__).resolve().parent.parent
        databases.write_json(self.config, dict(server=dict(address="127.0.0.1:8080"),
                                             paths=dict(results=str(self.root / "cache"))))
        databases.write_json(self.provenance, dict(namespace="fixture", database=dict(mode="full")))
        self.write([target("one"), target("two")])
        panel._signal = None
        self.addCleanup(setattr, panel, "_signal", None)

    def write(self, items):
        databases.write_json(self.path, dict(version=1, targets=items))

    def run_panel(self, **kwargs):
        return panel.run(self.path, self.output, self.tools, self.config, self.provenance,
                         kwargs.get("deadline", time.time()+300))

    def complete(self, item, directory, _tools, _config, provenance, _deadline):
        digest = fixture_bundle(item, directory, provenance)
        fixture_audit(directory)
        return digest

    def test_manifest_rejects_invalid_later_rows_before_output_or_any_preparation(self):
        invalid = [target("../escape"), target("bad", "rfaa"), target("bad", sequence="ACDX"),
                   target("one"), target("one", "boltz2", "ACDF"), dict(target("bad"), seed=7)]
        for item in invalid:
            with self.subTest(item=item), patch.object(panel, "prepare_target") as launch:
                self.write([target("one"), item])
                with self.assertRaises(ValueError):
                    self.run_panel()
                launch.assert_not_called()
                self.assertFalse(self.output.exists())
        self.path.write_text('{"version":1,"targets":[],"targets":[{}]}')
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            panel.manifest(self.path)

    def test_manifest_allows_same_query_across_models_and_binds_canonical_content(self):
        self.write([target("one", model) for model in sorted(panel.MODELS)])
        value, digest = panel.manifest(self.path)
        self.path.write_text(json.dumps(value, separators=(",", ":")))
        self.assertEqual(panel.manifest(self.path, digest)[1], digest)
        value["targets"][2]["name"] = "different"
        databases.write_json(self.path, value)
        with self.assertRaisesRegex(ValueError, "changed after validation"):
            panel.manifest(self.path, digest)

    def test_success_is_relocatable_and_detects_changed_native_or_raw_api_bytes(self):
        with patch.object(panel, "prepare_target", side_effect=self.complete):
            self.assertEqual(self.run_panel(), 0)
        receipt = panel.verify(self.path, self.output)
        self.assertEqual(len(receipt["targets"]), 2)
        relocated = self.root / "relocated"
        self.output.rename(relocated)
        panel.verify(self.path, relocated)
        one = relocated / "targets/protenix/one"
        api_body = one / "api-audit/1/response.body"
        saved = api_body.read_bytes()
        api_body.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "API body"):
            panel.verify(self.path, relocated)
        api_body.write_bytes(saved)
        bundle = prepared.validate(one / "prepared")
        alignment = next(k for k, v in bundle["files"].items() if "alignment" in v)
        (one / "prepared" / alignment).write_text(">query\nACDF\n")
        with self.assertRaisesRegex(prepared.Error, "integrity mismatch"):
            panel.verify(self.path, relocated)

    def test_missing_raw_evidence_is_failure_even_with_valid_native_bundle(self):
        def without_audit(item, directory, _tools, _config, provenance, _deadline):
            return fixture_bundle(item, directory, provenance)
        with patch.object(panel, "prepare_target", side_effect=without_audit):
            self.assertEqual(self.run_panel(), 1)
        receipt = databases.load(self.output / "panel.json")
        self.assertEqual([x["status"] for x in receipt["targets"]], ["failed", "failed"])
        self.assertTrue(all("raw private API result" in x["error"] for x in receipt["targets"]))

    def test_successful_backend_may_retain_its_script_only_inside_the_result_archive(self):
        directory = self.root / "archive-only"
        fixture_audit(directory)
        ticket = directory / "api-jobs/ticket1"
        (ticket / "msa.sh").unlink()
        archive = ticket / "mmseqs_results_ticket1.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            script = b"#!/bin/sh\n# synthetic original pairing pipeline\n"
            header = tarfile.TarInfo("pair.sh")
            header.size = len(script)
            output.addfile(header, io.BytesIO(script))
        digest, size = hashlib.sha256(archive.read_bytes()).hexdigest(), archive.stat().st_size
        exported = databases.load(ticket.parent / "tickets.json")
        del exported["tickets"]["ticket1"]["msa.sh"]
        exported["tickets"]["ticket1"][archive.name] = dict(bytes=size, sha256=digest)
        databases.write_json(ticket.parent / "tickets.json", exported)
        exchange = directory / "api-audit/1"
        (exchange / "response.body").write_bytes(archive.read_bytes())
        metadata = databases.load(exchange / "exchange.json")
        metadata.update(response_bytes=size, response_sha256=digest)
        databases.write_json(exchange / "exchange.json", metadata)
        panel.audit_evidence(directory)
        exported["tickets"]["ticket1"][archive.name]["sha256"] = "0"*64
        databases.write_json(ticket.parent / "tickets.json", exported)
        with self.assertRaisesRegex(ValueError, "differs from retained backend"):
            panel.audit_evidence(directory)

    def test_middle_failure_keeps_all_cases_and_continues_serially(self):
        self.write([target("one"), target("failed"), target("three")])
        order = []
        def attempt(*args):
            order.append(args[0]["name"])
            if order[-1] == "failed":
                raise subprocess.CalledProcessError(17, ["native", "prepare"])
            return self.complete(*args)
        with patch.object(panel, "prepare_target", side_effect=attempt):
            self.assertEqual(self.run_panel(), 1)
        receipt = databases.load(self.output / "panel.json")
        self.assertEqual(order, ["one", "failed", "three"])
        self.assertEqual([x["status"] for x in receipt["targets"]], ["complete", "failed", "complete"])
        self.assertEqual(receipt["targets"][1]["exit_status"], 17)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            panel.verify(self.path, self.output)

    def test_deadline_records_every_unstarted_case_and_does_not_launch(self):
        with patch.object(panel, "prepare_target") as launch:
            self.assertEqual(self.run_panel(deadline=time.time()+20), 1)
            launch.assert_not_called()
        receipt = databases.load(self.output / "panel.json")
        self.assertEqual([x["status"] for x in receipt["targets"]], ["timeout", "timeout"])

    def test_interruption_stops_new_preparations_but_records_all_cases(self):
        with patch.object(panel, "prepare_target", side_effect=panel.Interrupted(signal.SIGTERM)) as launch:
            self.assertEqual(self.run_panel(), 143)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual([x["status"] for x in databases.load(self.output / "panel.json")["targets"]],
                         ["interrupted", "interrupted"])

    def test_deadline_kills_owned_native_descendants_and_retains_log(self):
        pidfile = self.root / "child.pid"
        child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
        code = ("import subprocess,sys,time; from pathlib import Path; "
                "p=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
                "Path(sys.argv[1]).write_text(str(p.pid)); print('started child',flush=True); time.sleep(30)")
        log = self.root / "process.log"
        with self.assertRaises(TimeoutError):
            panel.command([sys.executable, "-c", code, pidfile, child], log, time.time()+.7, reserve=0)
        self.assertIn("started child", log.read_text())
        pid = int(pidfile.read_text())
        for _ in range(50):
            state = Path(f"/proc/{pid}/stat")
            if not state.exists() or state.read_text().split()[2] == "Z":
                break
            time.sleep(.02)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("Native descendant survived its panel deadline")

    def test_failed_native_command_exports_audit_without_losing_original_status(self):
        directory = self.root / "target"
        directory.mkdir()
        calls = []
        def command(args, *_args, **_kwargs):
            calls.append(args)
            raise subprocess.CalledProcessError(17 if len(calls) == 1 else 29, args)
        proxy = object()
        with patch.object(panel, "start_proxy", return_value=proxy), patch.object(panel, "stop") as stop, \
             patch.object(panel, "command", side_effect=command):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                panel.prepare_target(target("one"), directory, self.tools, self.config, self.provenance, time.time()+300)
        self.assertEqual(caught.exception.returncode, 17)
        self.assertEqual(len(calls), 2)
        self.assertIn("export", calls[1])
        stop.assert_called_once_with(proxy)

    def test_real_proxy_isolates_serial_targets_and_exports_only_their_tickets(self):
        cache = self.root / "cache"
        cache.mkdir()
        payload = b"fixture tar response"
        class Backend(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                ticket = self.rfile.read(int(self.headers["Content-Length"])).decode()
                folder = cache / ticket
                folder.mkdir()
                for name in ("job.json", "job.fasta", "msa.sh", f"mmseqs_results_{ticket}.tar.gz"):
                    (folder / name).write_bytes(payload)
                body = json.dumps(dict(id=ticket, status="COMPLETE")).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                self.send_response(200); self.send_header("Content-Length", str(len(payload))); self.end_headers()
                self.wfile.write(payload)
        server = ThreadingHTTPServer(("127.0.0.1", 8080), Backend)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # Actual proxy and export processes are the managed code; only the model
        # dependency is replaced with a CPU fixture client and native capture.
        fixture = self.root / "native.py"
        fixture.write_text('''import json, sys, urllib.request
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import prepared
from test_panel import fixture_bundle, target
args = sys.argv[2:]
get = lambda key: args[args.index(key)+1]
fasta = Path(get('--fasta'))
name, sequence = fasta.read_text().strip().splitlines()
name = name[1:]
if args[0] == 'validate':
    prepared.validate(get('--bundle'), 'protenix', fasta)
else:
    endpoint = get('--server-url')
    with urllib.request.urlopen(endpoint+'/ticket/msa', data=name.encode()) as r:
        ticket = json.load(r)['id']
    with urllib.request.urlopen(endpoint+'/result/download/'+ticket) as r:
        r.read()
    fixture_bundle(target(name, sequence=sequence), Path(get('--out')).parent, Path(get('--database-provenance')))
''')
        def native(_tools, _model, args):
            return [sys.executable, fixture, Path(__file__).parent, *map(str, args)]
        with patch.object(panel, "native", side_effect=native):
            self.assertEqual(self.run_panel(), 0)
        panel.verify(self.path, self.output)
        for name in ("one", "two"):
            directory = self.output / "targets/protenix" / name
            self.assertEqual(set(databases.load(directory / "api-jobs/tickets.json")["tickets"]), {name})
            self.assertEqual(len(list((directory / "api-audit").glob("*/exchange.json"))), 2)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", 8081), timeout=.2)


if __name__ == "__main__":
    unittest.main()
