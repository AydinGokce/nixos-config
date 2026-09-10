import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import session


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.ready = dict(session_id="a"*32, created_epoch=time.time()-10, deadline_epoch=time.time()+600,
                          output=str(self.root/"out"))
        self.expected = "b"*64
        self.request = dict(schema=1, request_id="c"*32, session_id="a"*32, ready_sha256=self.expected,
                            model="protenix", name="protein", sequence="ACDE", timeout_seconds=120,
                            deadline_epoch=time.time()+120)

    def tearDown(self): self.tmp.cleanup()

    def test_request_binds_exact_generation_and_rejects_unknown_semantics(self):
        session.request_document(self.request, self.ready, self.expected)
        for changes in ({"ready_sha256":"d"*64}, {"extra":True}, {"sequence":"ACD*"},
                        {"timeout_seconds":float("nan")}, {"deadline_epoch":self.ready["deadline_epoch"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                session.request_document(dict(self.request, **changes), self.ready, self.expected)

    def test_atomic_publication_does_not_replace_prior_intent(self):
        path = self.root/"intent.json"; session.atomic(path, {"first":1}, exclusive=True)
        with self.assertRaises(FileExistsError): session.atomic(path, {"second":2}, exclusive=True)
        self.assertEqual(session.load(path), {"first":1})
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_completed_request_retrieval_does_not_rerun_or_overwrite(self):
        digest = session.request_document(self.request, self.ready, self.expected)
        path = self.root/"requests"/(self.request["request_id"]+".json")
        session.atomic(path, self.request, exclusive=True)
        result = dict(status="complete", request_sha256=digest, ready_sha256=self.expected)
        session.atomic(Path(self.ready["output"])/"requests"/self.request["request_id"]/"status.json", result)
        with mock.patch.object(session, "check_ready", return_value=self.ready):
            self.assertEqual(session.submit(self.root, self.expected, self.request, 0), result)
            with self.assertRaises(ValueError):
                session.submit(self.root, self.expected, dict(self.request, sequence="AAAA"), 0)
        self.assertEqual(session.load(path), self.request)

    def test_uncertain_request_keeps_exact_intent(self):
        with mock.patch.object(session, "check_ready", return_value=self.ready):
            with self.assertRaisesRegex(ValueError, "pending"):
                session.submit(self.root, self.expected, self.request, 0)
        self.assertEqual(session.load(self.root/"requests"/("c"*32+".json")), self.request)

    def test_expired_request_is_not_enqueued(self):
        self.request["deadline_epoch"] = time.time()-1
        with mock.patch.object(session, "check_ready", return_value=self.ready), self.assertRaisesRegex(ValueError, "expired"):
            session.submit(self.root, self.expected, self.request, 0)
        self.assertFalse((self.root/"requests").exists())

    def test_actual_file_residency_prefetch_preserves_bytes(self):
        path = self.root/"index.idx"; data = os.urandom(1024*1024+3); path.write_bytes(data)
        cache = session.IndexCache([path])
        try:
            receipt = cache.warm("prefetch", time.time()+10, 0)
            self.assertTrue(receipt["after"]["fully_resident"])
            self.assertEqual(receipt["after"]["total_bytes"], len(data))
            self.assertFalse(receipt["locked"])
            with self.assertRaisesRegex(ValueError, "headroom"):
                cache.warm("prefetch", time.time()+10, 2**100)
        finally: cache.close()
        self.assertEqual(path.read_bytes(), data)

    def test_failed_mlock_does_not_claim_residency_guarantee(self):
        path = self.root/"index.idx"; path.write_bytes(b"x"*4096)
        cache = session.IndexCache([path])
        try:
            with mock.patch.object(cache.libc, "mlock", return_value=-1), self.assertRaises(OSError):
                cache.warm("lock", time.time()+10, 0)
        finally: cache.close()

    def test_residency_observation_checks_cancel_and_deadline_between_chunks(self):
        path = self.root/"index.idx"
        with path.open("wb") as stream: stream.truncate(256*1024**2+4096)
        cache = session.IndexCache([path])
        try:
            with mock.patch.object(session.time, "time", side_effect=[100, 102]), \
                 mock.patch.object(cache.libc, "mincore", return_value=0) as mincore, \
                 self.assertRaisesRegex(ValueError, "residency inspection interrupted or timed out"):
                cache.residency(101)
            self.assertEqual(mincore.call_count, 1)
            def cancel(*args):
                session.STOP = signal.SIGTERM
                return 0
            with mock.patch.object(session, "STOP", None), \
                 mock.patch.object(cache.libc, "mincore", side_effect=cancel) as mincore, \
                 self.assertRaisesRegex(ValueError, "residency inspection interrupted or timed out"):
                cache.residency(time.time()+10)
            self.assertEqual(mincore.call_count, 1)
        finally: cache.close()

    def test_prefetch_checks_expiry_and_cancellation_between_read_chunks(self):
        path = self.root/"index.idx"
        with path.open("wb") as stream: stream.truncate(16*1024**2+4096)
        cache = session.IndexCache([path])
        resident = dict(total_bytes=path.stat().st_size, indexes=[])
        try:
            with mock.patch.object(cache, "residency", return_value=resident) as residency, \
                 mock.patch.object(session.time, "time", side_effect=[100, 102]) as clock, \
                 self.assertRaisesRegex(ValueError, "Index warm-up interrupted or timed out"):
                cache.warm("prefetch", 101, 0)
            self.assertEqual(clock.call_count, 2)
            residency.assert_called_once_with(101)  # No final success/residency receipt.
            def cancel():
                session.STOP = signal.SIGTERM
                return 100
            with mock.patch.object(session, "STOP", None), \
                 mock.patch.object(cache, "residency", return_value=resident) as residency, \
                 mock.patch.object(session.time, "time", side_effect=cancel) as clock, \
                 self.assertRaisesRegex(ValueError, "Index warm-up interrupted or timed out"):
                cache.warm("prefetch", 101, 0)
            self.assertEqual(clock.call_count, 1)
            residency.assert_called_once_with(101)
        finally: cache.close()

    def test_default_cli_warm_limit_uses_session_lifetime(self):
        with mock.patch.object(session, "serve", return_value=0) as serve:
            self.assertEqual(session.main(["serve", "--state", str(self.root)]), 0)
        self.assertIsNone(serve.call_args.args[0].warm_seconds)
        with mock.patch.object(session, "serve", return_value=0) as serve:
            session.main(["serve", "--state", str(self.root), "--warm-seconds", "1800"])
        self.assertEqual(serve.call_args.args[0].warm_seconds, 1800)


class ServiceTests(unittest.TestCase):
    """Actual owned API process cleanup, without model/search execution."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        session.STOP = None
        with socket.socket() as probe:
            try: probe.bind(("127.0.0.1", 8080))
            except OSError: self.skipTest("Local8080 is already in use")
        self.database = self.root/"database"; self.database.mkdir()
        prefixes = {}
        for name in ("uniref30", "environmental", "pdb100"):
            prefixes[name] = str(self.database/name)
            Path(prefixes[name]+".idx").write_bytes(b"fixture"*4096)
        self.api = self.root/"api"
        self.api.write_text("#!"+sys.executable+"\nfrom http.server import HTTPServer,BaseHTTPRequestHandler\nHTTPServer(('127.0.0.1',8080),BaseHTTPRequestHandler).serve_forever()\n")
        self.api.chmod(0o700)
        self.provenance = dict(database=dict(prefixes=prefixes), tools=dict(server=str(self.api)), namespace="fixture-only")
        self.args = argparse.Namespace(session_id="a"*32, deadline=time.time()+180, idle_seconds=60, headroom_gib=16,
            warm_seconds=10, warm="report", state=self.root/"state", out=self.root/"out", tools=self.root/"tools",
            tools_root=self.root/"tools", database=self.database, results=self.root/"results")

    def tearDown(self): session.STOP = None; self.tmp.cleanup()

    def assert_warm_deadline(self, seconds, expected):
        self.args.deadline = 8200  # Existing reservation; startup has already consumed 1800s.
        self.args.warm_seconds = seconds
        self.args.warm = "prefetch"
        cache = mock.Mock()
        cache.warm.side_effect = ValueError("fixture interrupted during warm-up")
        with mock.patch.object(session.time, "time", return_value=2800), \
             mock.patch.object(session, "API_LOCK", self.root/"api.lock"), \
             mock.patch.object(session.server, "configuration", return_value=({}, self.provenance)), \
             mock.patch.object(session, "IndexCache", return_value=cache), \
             mock.patch.object(session.subprocess, "Popen") as launch, \
             self.assertRaisesRegex(ValueError, "fixture interrupted"):
            session.serve(self.args)
        self.assertEqual(cache.warm.call_count, 1)
        self.assertEqual(cache.warm.call_args.args, ("prefetch", expected, 16*1024**3))
        self.assertEqual(cache.warm.call_args.kwargs['prefetch_state'], Path('/tmp/bio-msa-prefetch-'+self.args.session_id))
        cache.close.assert_called_once_with()
        launch.assert_not_called()
        self.assertFalse((self.args.out/"warm-index.json").exists())
        self.assertFalse((self.args.out/"session-ready.json").exists())
        self.assertEqual(session.load(self.args.out/"session-closed.json")["reason"], "failed")

    def test_default_warm_deadline_preserves_original_session_lifetime_and_reserve(self):
        self.assert_warm_deadline(None, 8200-session.RESERVE)

    def test_explicit_warm_deadline_can_be_shorter(self):
        self.assert_warm_deadline(1800, 2800+1800)

    def test_explicit_warm_deadline_cannot_extend_session_lifetime(self):
        self.assert_warm_deadline(85500, 8200-session.RESERVE)

    def test_cancellation_closes_owned_api_and_records_original_deadline(self):
        timer = threading.Timer(1, os.kill, args=(os.getpid(), signal.SIGTERM))
        with mock.patch.object(session, "API_LOCK", self.root/"api.lock"), \
             mock.patch.object(session.server, "configuration", return_value=({}, self.provenance)), \
             mock.patch.object(session, "sources", return_value={"fixture": "not-production"}):
            timer.start()
            try: self.assertEqual(session.serve(self.args), 0)
            finally: timer.cancel(); timer.join()
        ready = session.load(self.args.state/"ready.json")
        self.assertEqual(ready["deadline_epoch"], self.args.deadline)
        self.assertEqual(session.load(self.args.state/"closed.json")["reason"], "cancelled")
        with self.assertRaises(ProcessLookupError): os.kill(ready["api"]["pid"], 0)
        with self.assertRaises(FileExistsError): session.serve(self.args)

    def test_graceful_control_closes_idle_owned_api_without_signalling_worker(self):
        errors=[];receipts=[]
        def request_shutdown():
            try:
                until=time.monotonic()+5
                while not (self.args.out/'session-ready.json').exists():
                    if time.monotonic() >= until:raise AssertionError('Fixture session did not become ready')
                    time.sleep(.02)
                ready=session.load(self.args.state/'ready.json')
                value=dict(schema=1,command_id='c'*32,action='shutdown',session_id=self.args.session_id,
                    invocation_id='d'*32,intent_sha256='e'*64,launch_sha256='f'*64)
                receipts.append(session.worker_controls.apply(ready,session.sha(self.args.state/'ready.json'),value))
            except BaseException as error:
                errors.append(error);os.kill(os.getpid(),signal.SIGTERM)
        thread=threading.Thread(target=request_shutdown)
        with mock.patch.object(session,'API_LOCK',self.root/'api.lock'), \
             mock.patch.object(session.server,'configuration',return_value=({},self.provenance)), \
             mock.patch.object(session,'sources',return_value={'fixture':'not-production'}):
            thread.start()
            try:self.assertEqual(session.serve(self.args),0)
            finally:thread.join(timeout=6)
        self.assertEqual(errors,[]);self.assertEqual(receipts[0]['status'],'applied')
        self.assertEqual(session.load(self.args.out/'session-closed.json')['reason'],'graceful_shutdown')
        ready=session.load(self.args.state/'ready.json')
        with self.assertRaises(ProcessLookupError):os.kill(ready['api']['pid'],0)

    def test_cancellation_of_borrowed_spool_preserves_real_api(self):
        import subprocess
        import sys
        api=subprocess.Popen([sys.executable,"-m","http.server","8080","--bind","127.0.0.1"],
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        try:
            until=time.time()+3
            while True:
                try:
                    with socket.create_connection(("127.0.0.1",8080),timeout=.1):break
                except OSError:
                    if time.time()>until:raise
                    time.sleep(.03)
            self.args.adopt=self.root/"adopt.json";session.atomic(self.args.adopt,{"fixture_only":True})
            argv=[v.decode()for v in (Path('/proc')/str(api.pid)/'cmdline').read_bytes().split(b'\0')if v]
            observed=session.BorrowedAPI(session.identity(api.pid),argv)
            timer=threading.Timer(.5,os.kill,args=(os.getpid(),signal.SIGTERM))
            with mock.patch.object(session,"API_LOCK",self.root/"api.lock"), \
                 mock.patch.object(session,"adopted_api",return_value=({"fixture_only":True},{},self.provenance,observed)), \
                 mock.patch.object(session,"request_gate",return_value="waiting_original_panel"), \
                 mock.patch.object(session,"sources",return_value={"fixture":"not-production"}):
                timer.start()
                try:self.assertEqual(session.serve(self.args),0)
                finally:timer.cancel();timer.join()
            self.assertIsNone(api.poll())
            self.assertTrue(session.load(self.args.out/'session-closed.json')['borrowed_api_preserved'])
            self.assertEqual(session.load(self.args.out/'session-ready.json')['lifecycle'],'borrowed-api')
            with socket.create_connection(("127.0.0.1",8080),timeout=.3):pass
        finally:session.panel.stop(api)

    def test_borrowed_identity_change_refused_without_signals(self):
        import sys
        argv=[v.decode()for v in Path('/proc/self/cmdline').read_bytes().split(b'\0')if v]
        wrong=dict(session.identity(),start_ticks=1)
        with mock.patch.object(session.os,"killpg")as killed,self.assertRaisesRegex(ValueError,"not live"):
            session.BorrowedAPI(wrong,argv)
        killed.assert_not_called()


if __name__ == "__main__": unittest.main()
