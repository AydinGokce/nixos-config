#!/usr/bin/env python3
"""Loopback-only CONNECT transport for serial native public MSA clients.

Native clients retain HTTPS certificate verification and all request/response
bytes. The shared query lock is held by public_msa_client.py (or RF3 on the
head) for the entire search, not merely for an individual HTTP request.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import select
import socket
import time


HEALTH = {"service": "bio-public-msa-proxy", "schema": 1}
PUBLIC_HOSTS = {"api.colabfold.com", "api.mmseqs.com"}


class Proxy(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, allowed=None):
        if address[0] != "127.0.0.1":
            raise ValueError("Public MSA transport must bind to head loopback")
        self.allowed = allowed if allowed is not None else {(host, 443) for host in PUBLIC_HOSTS}
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30

    def log_message(self, *_):
        pass  # No query paths, input sequences or transport headers in logs.

    def do_GET(self):
        if self.path != "/health":
            self.send_error(405, "Use CONNECT for native HTTPS requests")
            return
        body = json.dumps(HEALTH).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_CONNECT(self):
        self.close_connection = True
        try:
            host, port = self.path.rsplit(":", 1)
            destination = (host.lower(), int(port))
        except (ValueError, TypeError):
            self.send_error(400, "Invalid CONNECT destination")
            return
        if destination not in self.server.allowed:
            self.send_error(403, "Destination is outside the configured MSA transport")
            return
        try:
            upstream = socket.create_connection(destination, timeout=15)
        except OSError:
            self.send_error(502, "Public MSA upstream unavailable")
            return
        with upstream:
            upstream.settimeout(30)
            self.connection.settimeout(30)
            self.send_response(200, "Connection established")
            self.end_headers()
            self.wfile.flush()
            last_activity = time.monotonic()
            try:
                while time.monotonic() - last_activity < 120:
                    readable, _, _ = select.select([self.connection, upstream], [], [], 10)
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        destination_socket = upstream if source is self.connection else self.connection
                        destination_socket.sendall(data)
                        last_activity = time.monotonic()
            except (OSError, ValueError):
                return  # Native client receives a transport error; no direct fallback.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18763)
    args = parser.parse_args()
    with Proxy(("127.0.0.1", args.port)) as server:
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
