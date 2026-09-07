"""Native query guards and local proxy transport; no public MSA requests."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).parents[1] / "py/public_msa_client.py"
spec = importlib.util.spec_from_file_location("public_msa_client", SOURCE)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)

try:
    import requests as real_requests
    import urllib3.util.connection as urllib3_connection
except ImportError:
    real_requests = None


class RequestException(Exception):
    pass


class ProxyError(RequestException):
    pass


class NativeRequests:
    exceptions = types.SimpleNamespace(RequestException=RequestException, ProxyError=ProxyError)

    def __init__(self):
        self.calls = []
        self.error = None
        self.result = object()

    def post(self, url, *args, **kwargs):
        self.calls.append(("POST", url, args, kwargs))
        if self.error:
            raise self.error
        return self.result

    get = post


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.lock = self.root / "coordination/public-msa.lock"
        self.events = []
        self.health = client.HEALTH
        events, owner = self.events, self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                events.append(("GET", self.path))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(owner.health).encode())

            def do_CONNECT(self):
                events.append(("CONNECT", self.path))
                # A real requests client reaches this local fixture; no tunnel
                # to any remote host is ever opened by the test.
                self.send_error(502, "intentional fixture transport failure")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.proxy = "http://127.0.0.1:" + str(self.server.server_port)
        self.requests = NativeRequests()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def native(self, body=None):
        namespace = {"requests": self.requests}
        exec(body or '''def native(x, prefix="tmp", use_pairing=False, host_url="https://api.colabfold.com"):
    return requests.post(host_url + "/ticket/msa", data={"q": x, "mode": "paired" if use_pairing else "env"}, timeout=6.02)
''', namespace)
        return namespace["native"]

    def guard(self, function, deadline=None):
        return client.guarded_query(function, self.proxy, self.lock, deadline)

    def test_native_arguments_results_and_requests_module_are_preserved(self):
        original = self.native()
        with patch.dict(os.environ, {"NO_PROXY": "*", "HTTPS_PROXY": "http://bad.invalid:1"}):
            result = self.guard(original)("ACDE\nFGHI", prefix="native path", use_pairing=True)
        self.assertIs(result, self.requests.result)
        self.assertIs(original.__globals__["requests"], self.requests)
        method, url, positional, kwargs = self.requests.calls[0]
        self.assertEqual((method, url, positional), ("POST", "https://api.colabfold.com/ticket/msa", ()))
        self.assertEqual(kwargs["data"], {"q": "ACDE\nFGHI", "mode": "paired"})
        self.assertEqual(kwargs["timeout"], 6.02)
        self.assertEqual(kwargs["proxies"]["https"], self.proxy)
        self.assertEqual(kwargs["proxies"]["https://api.colabfold.com"], self.proxy)
        self.assertEqual(kwargs["proxies"]["no_proxy"], "")

    def test_health_check_occurs_after_shared_lease_and_wait_has_deadline(self):
        self.lock.parent.mkdir()
        with self.lock.open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaisesRegex(SystemExit, "deadline elapsed"):
                self.guard(self.native(), time.time() + .05)("ACDE")
        self.assertEqual(self.requests.calls, [])
        self.assertEqual(self.events, [])

    def test_two_native_queries_never_overlap(self):
        entered, release = threading.Event(), threading.Event()
        namespace = {"requests": self.requests, "entered": entered, "release": release}
        exec('''def native(x, host_url="https://api.colabfold.com"):
    if x == "first":
        entered.set()
        if not release.wait(5):
            raise RuntimeError("fixture did not release first query")
    return requests.post(host_url + "/ticket/msa", data={"q": x})
''', namespace)
        first, second = self.guard(namespace["native"]), self.guard(self.native())
        with ThreadPoolExecutor(2) as pool:
            one = pool.submit(first, "first")
            try:
                self.assertTrue(entered.wait(5))
                two = pool.submit(second, "second")
                time.sleep(.05)
                self.assertEqual(self.requests.calls, [])
                self.assertEqual(self.events, [("GET", "/health")])
            finally:
                release.set()
            one.result(timeout=5)
            two.result(timeout=5)
        self.assertEqual([call[3]["data"]["q"] for call in self.requests.calls], ["first", "second"])

    def test_native_exception_and_keyboard_interrupt_release_and_restore(self):
        for statement, expected in (("raise ValueError('native science error')", ValueError),
                                    ("raise KeyboardInterrupt()", KeyboardInterrupt)):
            with self.subTest(expected=expected):
                original = self.native("def native(x):\n    " + statement + "\n")
                with self.assertRaises(expected):
                    self.guard(original)("ACDE")
                self.assertIs(original.__globals__["requests"], self.requests)
                with self.lock.open("a") as available:
                    fcntl.flock(available, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_proxy_failure_cannot_be_swallowed_into_missing_msa_prediction(self):
        self.requests.error = ProxyError("fixture proxy unavailable")
        original = self.native('''def native(x, host_url="https://api.colabfold.com"):
    try:
        return requests.post(host_url + "/ticket/msa", data={"q": x})
    except Exception:
        return "would silently continue without MSA"
''')
        with self.assertRaisesRegex(SystemExit, "proxy transport failed"):
            self.guard(original)("ACDE")
        self.assertIs(original.__globals__["requests"], self.requests)

    def test_native_request_failure_is_preserved_when_proxy_is_healthy(self):
        self.requests.error = RequestException("native upstream timeout")
        with self.assertRaisesRegex(RequestException, "upstream timeout"):
            self.guard(self.native())("ACDE")
        self.assertEqual(self.events.count(("GET", "/health")), 2)

    def test_unhealthy_proxy_missing_config_and_bad_endpoints_never_submit(self):
        self.health = {"service": "another application", "schema": 1}
        with self.assertRaisesRegex(SystemExit, "health response"):
            self.guard(self.native())("ACDE")
        for environment in ({}, {"BIO_PUBLIC_MSA_PROXY": "http://outside.example:123", "BIO_PUBLIC_MSA_LOCK": str(self.lock)}):
            with self.assertRaises(SystemExit):
                client.proxy_configuration(environment)
        for endpoint in ("https://another.example", "http://api.colabfold.com", "https://api.colabfold.com:invalid"):
            with self.assertRaisesRegex(SystemExit, "unexpected endpoint"):
                self.guard(self.native())("ACDE", host_url=endpoint)
        self.assertEqual(self.requests.calls, [])

    def test_symlinked_shared_lock_and_unsafe_permissions_fail_closed(self):
        target = self.root / "target"
        target.write_text("")
        alias = self.root / "alias"
        alias.symlink_to(target)
        with self.assertRaisesRegex(SystemExit, "without symlinks"):
            with client.shared_query_lock(alias):
                self.fail("symlink lock was accepted")
        target.chmod(0o666)
        with self.assertRaisesRegex(SystemExit, "unsafe ownership"):
            with client.shared_query_lock(target):
                self.fail("world-writable lock was accepted")

    def test_all_installed_aliases_share_the_same_guard(self):
        for model, hooks in client.HOOKS.items():
            with self.subTest(model=model):
                original = self.native()
                modules = {}
                for module_name, function_name in hooks:
                    module = types.ModuleType(module_name)
                    setattr(module, function_name, original)
                    modules[module_name] = module
                with patch.dict(sys.modules, modules):
                    client.install_hooks(model, self.proxy, self.lock, None)
                    wrappers = [getattr(modules[m], name) for m, name in hooks]
                    self.assertTrue(all(function is wrappers[0] for function in wrappers))
                    self.assertIs(wrappers[0]("ACDE"), self.requests.result)

    def test_original_console_script_receives_exact_arguments(self):
        cli = self.root / "native-cli"
        out = self.root / "received.json"
        cli.write_text("import json,sys\nfrom pathlib import Path\nPath(" + repr(str(out)) + ").write_text(json.dumps(sys.argv))\n")
        arguments = ["predict", "--input", "a sequence file.json", "--setting=$(literal)"]
        environment = {"BIO_PUBLIC_MSA_PROXY": self.proxy, "BIO_PUBLIC_MSA_LOCK": str(self.lock)}
        with patch.dict(os.environ, environment), patch.object(client, "install_hooks") as install, patch.object(sys, "argv", [str(SOURCE)]):
            client.main(["--model", "boltz2", "--entrypoint", str(cli), "--", *arguments])
        self.assertEqual(json.loads(out.read_text()), [str(cli), *arguments])
        install.assert_called_once_with("boltz2", self.proxy, self.lock, None)

    @unittest.skipUnless(real_requests is not None, "requests package needed for local CONNECT transport check")
    def test_real_requests_uses_connect_proxy_even_with_no_proxy_star(self):
        original_connect = urllib3_connection.create_connection
        def loopback_only(address, *args, **kwargs):
            self.assertEqual(address, ("127.0.0.1", self.server.server_port), "Test attempted a nonlocal connection")
            return original_connect(address, *args, **kwargs)
        facade = client.ProxyRequests(real_requests, self.proxy, None)
        with patch.dict(os.environ, {"NO_PROXY": "*", "HTTPS_PROXY": "http://bad.invalid:1"}), \
             patch.object(urllib3_connection, "create_connection", side_effect=loopback_only):
            with self.assertRaisesRegex(SystemExit, "proxy transport failed"):
                facade.get("https://api.colabfold.com/ticket/fixture", timeout=2)
        self.assertIn(("CONNECT", "api.colabfold.com:443"), self.events)


if __name__ == "__main__":
    unittest.main()
