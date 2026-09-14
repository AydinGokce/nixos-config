import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import session
import search_profile


class IndexCacheMappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        stop = mock.patch.object(session, "STOP", None)
        stop.start(); self.addCleanup(stop.stop)

    def mapping(self, address):
        """Inspect this exact native VMA, without dereferencing the mapped file."""
        result = None
        for line in Path("/proc/self/smaps").read_text().splitlines():
            header = re.match(r"^([0-9a-f]+)-([0-9a-f]+) (\S+) ", line)
            if header:
                if result is not None: break
                if int(header[1], 16) <= address < int(header[2], 16):
                    result = dict(start=int(header[1], 16), end=int(header[2], 16),
                                  permissions=header[3], header=line)
            elif result is not None and ":" in line:
                key, value = line.split(":", 1); result[key] = value.strip()
        return result

    def test_native_read_only_mapping_observes_actual_file_pages_and_closes(self):
        path = self.root/"small.idx"; data = os.urandom(8193); path.write_bytes(data)
        before = path.stat()
        cache = session.IndexCache([path]); self.addCleanup(cache.close)
        _, size, address = cache.entries[0]
        self.assertEqual(size, len(data))
        self.assertGreater(address, 2**32)  # Detect a truncated native pointer ABI.
        area = self.mapping(address)
        self.assertEqual(area["permissions"], "r--p")
        self.assertNotIn("ac", area["VmFlags"].split())
        self.assertEqual(area["Rss"], "0 kB")
        receipt = cache.residency(time.time()+10)
        self.assertEqual(receipt["indexes"][0]["pages"], 3)
        self.assertEqual(receipt["indexes"][0]["resident_pages"], 3)
        # mincore observes the page cache; it does not fault the mapping itself.
        self.assertEqual(self.mapping(address)["Rss"], "0 kB")
        cache.close(); cache.close()
        self.assertEqual(cache.entries, [])
        self.assertFalse(any(str(path) in line for line in Path("/proc/self/maps").read_text().splitlines()))
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), (before.st_size, before.st_mtime_ns))

    def test_huge_sparse_read_only_mapping_has_no_private_commit_residency_or_file_reads(self):
        # Exceeds the entire 700 GB production index set, without disk allocation
        # or dereferencing a single file page. This must work without MAP_NORESERVE.
        path = self.root/"full-index-scale.idx"; size = 700*1024**3+3
        with path.open("wb") as stream: stream.truncate(size)
        self.assertEqual(path.stat().st_blocks, 0)
        def read_bytes():
            fields = dict(line.split(":", 1) for line in Path("/proc/self/io").read_text().splitlines())
            return int(fields["read_bytes"])
        before_reads = read_bytes()
        cache = session.IndexCache([path]); self.addCleanup(cache.close)
        _, actual, address = cache.entries[0]
        self.assertEqual(actual, size)
        area = self.mapping(address)
        self.assertEqual(area["permissions"], "r--p")
        self.assertGreaterEqual(area["end"]-area["start"], size)
        self.assertNotIn("ac", area["VmFlags"].split())
        self.assertNotIn("wr", area["VmFlags"].split())
        for key in ("Rss", "Private_Dirty", "Anonymous", "Swap"):
            self.assertEqual(area[key], "0 kB", key)
        for offset in (0, (size//cache.page)*cache.page):
            vector = (ctypes.c_ubyte*1)()
            self.assertEqual(cache.libc.mincore(address+offset, min(cache.page, size-offset), vector), 0)
            self.assertEqual(vector[0] & 1, 0)
        self.assertEqual(self.mapping(address)["Rss"], "0 kB")
        self.assertEqual(read_bytes(), before_reads)
        self.assertEqual(path.stat().st_blocks, 0)
        cache.close()
        self.assertFalse(any(str(path) in line for line in Path("/proc/self/maps").read_text().splitlines()))

    def test_partial_constructor_failure_unmaps_prior_entries_and_closes_files(self):
        first = self.root/"first.idx"; first.write_bytes(b"x"*4096)
        empty = self.root/"empty.idx"; empty.touch()
        for second, exception in [(empty, ValueError), (self.root/"missing.idx", FileNotFoundError)]:
            with self.subTest(second=second), self.assertRaises(exception):
                session.IndexCache([first, second])
            self.assertNotIn(str(first), Path("/proc/self/maps").read_text())
            descriptors = []
            for path in Path("/proc/self/fd").iterdir():
                try: descriptors.append(os.readlink(path))
                except FileNotFoundError: pass
            self.assertNotIn(str(first), descriptors)
            self.assertNotIn(str(second), descriptors)

    def test_mmap_failed_pointer_preserves_errno_and_unmaps_previous_success(self):
        paths = [self.root/"first.idx", self.root/"second.idx"]
        for path in paths: path.write_bytes(b"x"*4096)
        api = session.IndexCache([]).libc
        native_mmap = api.mmap; addresses = []
        def attempt(*args):
            if not addresses:
                addresses.append(native_mmap(*args)); return addresses[0]
            ctypes.set_errno(errno.ENOMEM)
            return ctypes.c_void_p(-1).value
        with mock.patch.object(session.ctypes, "CDLL", return_value=api), \
             mock.patch.object(api, "mmap", side_effect=attempt), \
             mock.patch.object(api, "munmap", wraps=api.munmap) as unmap, \
             self.assertRaises(OSError) as failure:
            session.IndexCache(paths)
        self.assertEqual(failure.exception.errno, errno.ENOMEM)
        self.assertEqual(failure.exception.filename, str(paths[1]))
        unmap.assert_called_once_with(addresses[0], 4096)
        self.assertNotIn(str(paths[0]), Path("/proc/self/maps").read_text())

    def test_failed_mincore_does_not_publish_a_residency_receipt(self):
        path = self.root/"index.idx"; path.write_bytes(b"x"*4096)
        cache = session.IndexCache([path]); self.addCleanup(cache.close)
        def fail(*args): ctypes.set_errno(errno.ENOMEM); return -1
        with mock.patch.object(cache.libc, "mincore", side_effect=fail), self.assertRaises(OSError) as failure:
            cache.residency(time.time()+10)
        self.assertEqual(failure.exception.errno, errno.ENOMEM)

    def test_partial_lock_failure_unlocks_and_unmaps_every_owned_mapping(self):
        paths = [self.root/"first.idx", self.root/"second.idx"]
        for path in paths: path.write_bytes(b"x"*4096)
        cache = session.IndexCache(paths); self.addCleanup(cache.close)
        first = cache.entries[0]
        with mock.patch.object(cache.libc, "mlock", side_effect=[0, -1]), \
             self.assertRaises(OSError):
            cache.warm("lock", time.time()+10, 0)
        self.assertEqual(cache.locked, [(first[2], first[1])])
        with mock.patch.object(cache.libc, "munlock", wraps=cache.libc.munlock) as unlock:
            cache.close()
        self.assertEqual(unlock.call_count, 1)
        self.assertEqual(unlock.call_args.args[0].value, first[2])
        self.assertEqual(unlock.call_args.args[1].value, first[1])
        self.assertEqual(cache.entries, [])
        for path in paths: self.assertNotIn(str(path), Path("/proc/self/maps").read_text())


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

    def test_report_observes_sparse_index_without_prefetch_lock_or_full_residency_requirement(self):
        path = self.root/"index.idx"
        with path.open("wb") as stream: stream.truncate(32*1024**2)
        cache = session.IndexCache([path])
        try:
            with mock.patch.object(session.prefetch, "load") as prefetch, \
                 mock.patch.object(cache.libc, "mlock") as lock:
                receipt = cache.warm("report", time.time()+10, 2**100)
            prefetch.assert_not_called(); lock.assert_not_called()
            self.assertIsNone(receipt["loading"])
            self.assertFalse(receipt["locked"])
            self.assertFalse(receipt["after"]["fully_resident"])
            self.assertEqual(receipt["after"]["total_bytes"], 32*1024**2)
            self.assertEqual(path.stat().st_blocks, 0)
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
        self.assertIsNone(serve.call_args.args[0].warm)
        self.assertEqual(serve.call_args.args[0].search_profile, search_profile.DEFAULT_PROFILE)
        with mock.patch.object(session, "serve", return_value=0) as serve:
            session.main(["serve", "--state", str(self.root), "--warm-seconds", "1800"])
        self.assertEqual(serve.call_args.args[0].warm_seconds, 1800)


class ServiceTests(unittest.TestCase):
    """Actual owned API process cleanup, without model/search execution."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        session.STOP = None
        environment = mock.patch.dict(os.environ, {})
        environment.start(); self.addCleanup(environment.stop)
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
        self.profile = search_profile.resolve(search_profile.LEGACY_PROFILE)
        self.guest = search_profile.check_guest(self.profile,
            dict(MemTotal=f"{1024*1024**2} kB", MemAvailable=f"{900*1024**2} kB"))
        guest = mock.patch.object(search_profile, "check_guest", return_value=self.guest)
        self.guest_check = guest.start(); self.addCleanup(guest.stop)
        self.config = dict(app="colabfold", local=dict(workers=1), worker=dict(paralleldatabases=1),
                           paths=dict(colabfold=dict(parallelstages=False)))
        self.provenance = dict(database=dict(prefixes=prefixes), tools=dict(server=str(self.api)), namespace="fixture-only",
            search_profile=self.profile, runtime=dict(mmseqs_threads=16,
                environment=search_profile.configure_environment(self.profile, {})))
        self.args = argparse.Namespace(session_id="a"*32, deadline=time.time()+180, idle_seconds=60, headroom_gib=16,
            warm_seconds=10, warm="report", state=self.root/"state", out=self.root/"out", tools=self.root/"tools",
            tools_root=self.root/"tools", database=self.database, results=self.root/"results", search_profile=self.profile)

    def tearDown(self): session.STOP = None; self.tmp.cleanup()

    def assert_warm_deadline(self, seconds, expected):
        self.args.deadline = 8200  # Existing reservation; startup has already consumed 1800s.
        self.args.warm_seconds = seconds
        self.args.warm = "prefetch"
        cache = mock.Mock()
        cache.warm.side_effect = ValueError("fixture interrupted during warm-up")
        with mock.patch.object(session.time, "time", return_value=2800), \
             mock.patch.object(session, "API_LOCK", self.root/"api.lock"), \
             mock.patch.object(session.server, "configuration", return_value=(self.config, self.provenance)), \
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
             mock.patch.object(session.server, "configuration", return_value=(self.config, self.provenance)), \
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
             mock.patch.object(session.server,'configuration',return_value=(self.config,self.provenance)), \
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
                 mock.patch.object(session,"adopted_api",return_value=({"fixture_only":True},self.config,self.provenance,observed)), \
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

    def test_mapped_service_binds_memory_threads_and_reports_mapping_not_full_loading(self):
        self.profile = search_profile.resolve(search_profile.MAPPED_PROFILE)
        self.args.search_profile = self.profile
        self.args.warm = None
        self.guest = dict(schema=1, kind="private-msa-guest-memory", profile_sha256=self.profile["profile_sha256"],
            total_bytes=118*1024**3, available_bytes=109*1024**3,
            minimum_total_bytes=110*1024**3, minimum_available_bytes=100*1024**3)
        self.guest_check.return_value = self.guest
        self.provenance.update(search_profile=self.profile,
            runtime=dict(mmseqs_threads=4, environment=search_profile.configure_environment(self.profile, {})))
        limit = dict(schema=1, kind="private-msa-cgroup-memory", profile_sha256=self.profile["profile_sha256"],
            cgroup_path="/fixture.scope", memory_max_bytes=96*1024**3, memory_swap_max_bytes=0)
        timer = threading.Timer(1, os.kill, args=(os.getpid(), signal.SIGTERM))
        with mock.patch.object(session, "API_LOCK", self.root/"api.lock"), \
             mock.patch.object(session.server, "configuration", return_value=(self.config, self.provenance)), \
             mock.patch.object(search_profile, "check_cgroup", return_value=limit) as checked_limit, \
             mock.patch.object(session.prefetch, "load") as prefetch, \
             mock.patch.object(session, "startup_progress", wraps=session.startup_progress) as progress, \
             mock.patch.object(session, "sources", return_value={"fixture": "not-production"}):
            timer.start()
            try: self.assertEqual(session.serve(self.args), 0)
            finally: timer.cancel(); timer.join()
        ready = session.load(self.args.state/"ready.json")
        self.assertEqual(ready["search_profile"], self.profile)
        self.assertEqual(ready["guest_memory"], self.guest)
        self.assertEqual(ready["memory_limit"], limit)
        self.assertGreaterEqual(checked_limit.call_count, 2)
        prefetch.assert_not_called()
        warm = session.load(self.args.out/"warm-index.json")
        self.assertEqual(warm["mode"], "report")
        self.assertIsNone(warm["loading"])
        complete = [call for call in progress.call_args_list
                    if call.args[2:4] == ("index_warm", "complete")]
        self.assertEqual(len(complete), 1)
        self.assertIn("on demand", complete[0].args[4])
        self.assertNotIn("completed", complete[0].kwargs)
        with self.assertRaises(ProcessLookupError): os.kill(ready["api"]["pid"], 0)

    def test_mapped_memory_or_warm_rejection_prevents_api_launch(self):
        self.args.search_profile = search_profile.resolve(search_profile.MAPPED_PROFILE)
        self.args.warm = "prefetch"
        with mock.patch.object(session.subprocess, "Popen") as launch, self.assertRaisesRegex(ValueError, "report-only"):
            session.serve(self.args)
        launch.assert_not_called()
        self.assertFalse(self.args.state.exists())
        self.args.warm = "report"
        with mock.patch.object(session, "API_LOCK", self.root/"api.lock"), \
             mock.patch.object(search_profile, "check_cgroup", side_effect=ValueError("uncapped fixture")), \
             mock.patch.object(session.subprocess, "Popen") as launch, self.assertRaisesRegex(ValueError, "uncapped"):
            session.serve(self.args)
        launch.assert_not_called()
        self.assertFalse((self.args.out/"session-ready.json").exists())


if __name__ == "__main__": unittest.main()
