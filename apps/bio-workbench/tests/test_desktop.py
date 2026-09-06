import base64
import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from bio_desktop.transport import Connection, SSHTransport, TransportError, read_json
from bio_desktop.server import DesktopServer


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.connection = Connection(self.root / "config")
        self.transport = SSHTransport(self.connection, self.root / "cache")
        self.addCleanup(lambda: __import__('shutil').rmtree(self.transport.sockets, ignore_errors=True))

    def test_connection_fixed_argv_no_shell(self):
        for host in ["-oProxyCommand=evil", "head;touch /tmp/pwn", "a\nb", "$(id)", "a b"]:
            with self.assertRaises(ValueError):
                self.connection.update(dict(self.connection.value, host=host))
        key = str(self.root / "key with spaces $(literal)")
        self.connection.update(dict(self.connection.value, key_path=key))
        argv = self.transport.argv()
        self.assertIn(key, argv)
        self.assertEqual(argv[-1], "env BIO_WORKBENCH_ACTOR=harrison /run/current-system/sw/bin/bio-workbench rpc")
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertEqual(self.connection.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Connection(self.root / "config").value["key_path"], key)

    def test_rpc_rejects_mismatched_id_and_does_not_retry(self):
        def fake(argv, **kwargs):
            kwargs["stdout"].write(b'{"id":"wrong","result":{}}\n')
            return subprocess.CompletedProcess(argv, 0)
        with patch("bio_desktop.transport.subprocess.run", side_effect=fake) as run:
            with self.assertRaises(TransportError):
                self.transport.rpc({"id": "wanted", "method": "catalog", "params": {}})
            self.assertEqual(run.call_count, 1)
        with self.assertRaises(ValueError):
            read_json('{"a":1,"a":2}')
        with self.assertRaises(ValueError):
            read_json('{"a":NaN}')

    def test_chunked_artifact_integrity_and_no_corrupt_cache(self):
        raw = b"ATOM structure\n" * 40000
        digest = hashlib.sha256(raw).hexdigest()
        def call(method, params):
            offset = params["offset"]
            data = raw[offset:offset + params["max_bytes"]]
            return {"artifact_id": "artifact-test", "offset": offset, "next_offset": offset + len(data),
                    "data_base64": base64.b64encode(data).decode(), "eof": offset + len(data) == len(raw),
                    "size": len(raw), "sha256": digest, "name": "test.cif", "media_type": "chemical/x-mmcif"}
        with patch.object(self.transport, "call", side_effect=call):
            path, _ = self.transport.artifact("artifact-test")
            self.assertEqual(path.read_bytes(), raw)
            path.write_bytes(b"corrupt")
            self.assertEqual(self.transport.artifact("artifact-test")[0].read_bytes(), raw)
        path.unlink()
        def corrupt(method, params):
            result = call(method, params)
            result["data_base64"] = base64.b64encode(b"X" * (result["next_offset"] - result["offset"])).decode()
            return result
        with patch.object(self.transport, "call", side_effect=corrupt), self.assertRaises(TransportError):
            self.transport.artifact("artifact-test")
        self.assertEqual(list(self.transport.cache.iterdir()), [])

    def test_http_session_csrf_and_path_confinement(self):
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "index.html").write_text("<html>workbench</html>")
        (self.root / "private.txt").write_text("secret")
        server = DesktopServer(("127.0.0.1", 0), self.transport, assets)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        self.addCleanup(conn.close)
        def request(method, path, body=None, headers=None):
            conn.request(method, path, body, headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        status, headers, raw = request("GET", "/api/v1/session")
        self.assertEqual(status, 200)
        session = json.loads(raw)
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(request("GET", "/api/v1/session", headers={"Host": "evil.invalid"})[0], 403)
        self.assertEqual(request("GET", "/api/v1/session", headers={"Origin": "https://evil.invalid"})[0], 403)
        self.assertEqual(request("GET", "/api/v1/connection")[0], 403)
        body = json.dumps(self.connection.value)
        self.assertEqual(request("PATCH", "/api/v1/connection", body, {"Cookie": cookie, "Content-Type": "application/json"})[0], 403)
        status, _, _ = request("PATCH", "/api/v1/connection", body, {"Cookie": cookie, "Content-Type": "application/json", "X-Bio-Workbench-Token": session["csrf_token"]})
        self.assertEqual(status, 200)
        self.assertEqual(request("GET", "/%2e%2e/private.txt")[0], 404)
        self.assertEqual(request("GET", "/")[0], 200)


if __name__ == "__main__":
    unittest.main()
