import json
import socket
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from public_proxy import HEALTH, Proxy


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.proxy = Proxy(("127.0.0.1", 0), set())
        self.thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.thread.start()
        self.address = self.proxy.server_address

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.thread.join(5)

    def test_health_identifies_transport_without_external_request(self):
        with urlopen(f"http://127.0.0.1:{self.address[1]}/health", timeout=2) as reply:
            self.assertEqual(json.load(reply), HEALTH)

    def test_plain_http_queries_are_not_forwarded(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"http://127.0.0.1:{self.address[1]}/ticket/msa", timeout=2)
        self.assertEqual(error.exception.code, 405)

    def test_unknown_connect_destination_is_rejected(self):
        with socket.create_connection(self.address, timeout=2) as client:
            client.sendall(b"CONNECT localhost:22 HTTP/1.1\r\nHost: localhost:22\r\n\r\n")
            self.assertIn(b"403", client.recv(4096).split(b"\r\n")[0])

    def test_connect_preserves_binary_bytes_bidirectionally(self):
        payload = bytes(range(256)) * 2048
        with socket.create_server(("127.0.0.1", 0)) as upstream:
            self.proxy.allowed = {upstream.getsockname()}
            errors = []

            def echo():
                try:
                    with upstream.accept()[0] as connection:
                        received = bytearray()
                        while len(received) < len(payload):
                            received.extend(connection.recv(65536))
                        self.assertEqual(bytes(received), payload)
                        connection.sendall(payload[::-1])
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=echo, daemon=True)
            thread.start()
            with socket.create_connection(self.address, timeout=3) as client:
                target = f"127.0.0.1:{upstream.getsockname()[1]}"
                client.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
                header = bytearray()
                while not header.endswith(b"\r\n\r\n"):
                    header.extend(client.recv(1))
                self.assertIn(b"200", bytes(header).split(b"\r\n")[0])
                client.sendall(payload)
                received = bytearray()
                while len(received) < len(payload):
                    data = client.recv(65536)
                    self.assertTrue(data)
                    received.extend(data)
                self.assertEqual(bytes(received), payload[::-1])
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_transport_cannot_bind_public_interface(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            Proxy(("0.0.0.0", 0))


if __name__ == "__main__":
    unittest.main()
