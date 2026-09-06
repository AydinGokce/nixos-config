"""Substantive transport regressions found during the independent app review."""

import base64
import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from bio_desktop.server import DesktopServer
from bio_desktop.transport import Connection, SSHTransport, TransportError


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.transport = SSHTransport(Connection(self.directory / "connection"), self.directory / "cache")

    def check_bad_method(self, method):
        with patch.object(subprocess, "run", side_effect=AssertionError("malformed request reached SSH")):
            with self.assertRaisesRegex(ValueError, "Unknown RPC method"):
                self.transport.rpc({"id": "one", "method": method, "params": {}})

    def check_bad_response(self, response):
        with patch.object(self.transport, "rpc", return_value=response):
            with self.assertRaisesRegex(TransportError, "Invalid head"):
                self.transport.call("catalog", {})

    def test_verified_artifact_cache_closes_file_and_corruption_is_replaced(self):
        data = b"data_model\n# immutable structure bytes\n"
        digest = hashlib.sha256(data).hexdigest()
        cached = self.transport.cache / digest
        cached.write_bytes(data)
        meta = {"artifact_id": "artifact-1", "offset": 0, "size": len(data), "sha256": digest,
                "data_base64": base64.b64encode(data).decode(), "next_offset": len(data), "eof": True}
        calls, opened = [], []
        def call(method, params):
            calls.append((method, params))
            return meta
        original_open = Path.open
        def tracked_open(path, *args, **kwargs):
            file = original_open(path, *args, **kwargs)
            if path == cached and args == ("rb",):
                opened.append(file)
            return file
        with patch.object(self.transport, "call", side_effect=call), patch.object(Path, "open", tracked_open):
            self.assertEqual(self.transport.artifact("artifact-1")[0], cached)
            self.assertEqual(len(calls), 1)  # A cache hit still checks current head access.
            self.assertTrue(opened and all(file.closed for file in opened))
            cached.write_bytes(b"x" * len(data))
            self.assertEqual(self.transport.artifact("artifact-1")[0].read_bytes(), data)
            self.assertEqual(len(calls), 2)

    def test_http_malformed_method_returns_json_error_and_service_stays_usable(self):
        server = DesktopServer(("127.0.0.1", 0), self.transport, self.directory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = http.client.HTTPConnection("127.0.0.1", server.server_port)
        try:
            client.request("GET", "/api/v1/session")
            response = client.getresponse()
            token = json.loads(response.read())["csrf_token"]
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            with patch.object(subprocess, "run", side_effect=AssertionError("malformed request reached SSH")):
                client.request("POST", "/api/v1/rpc", json.dumps({"id": "malformed", "method": [], "params": {}}),
                               {"Cookie": cookie, "X-Bio-Workbench-Token": token, "Content-Type": "application/json"})
                response = client.getresponse()
                self.assertEqual(response.status, 400)
                self.assertIn("Unknown RPC method", json.loads(response.read())["error"]["message"])
            client.request("GET", "/api/v1/connection", headers={"Cookie": cookie})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["host"], "31.56.109.100")
        finally:
            client.close(); server.shutdown(); server.server_close(); thread.join(timeout=5)


for _name, _method in (("list", []), ("object", {}), ("null", None), ("number", 12)):
    setattr(ReviewRegressions, "test_invalid_method_" + _name,
            lambda self, method=_method: self.check_bad_method(method))
for _name, _response in (("list_error", {"error": []}), ("text_error", {"error": "plain text"}),
                         ("null_message", {"error": {"message": None}}), ("list_result", {"result": []})):
    setattr(ReviewRegressions, "test_invalid_response_" + _name,
            lambda self, response=_response: self.check_bad_response(response))


if __name__ == "__main__":
    unittest.main()
