from __future__ import annotations

import argparse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
from urllib.parse import urlsplit, unquote, quote

from .transport import Connection, SSHTransport, TransportError, MAX_WIRE, encode, read_json


class DesktopServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, transport, assets):
        self.transport = transport
        self.assets = Path(assets).resolve()
        self.session = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        super().__init__(address, Handler)
        self.authority = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.authority}"


class Handler(BaseHTTPRequestHandler):
    server_version = "BioWorkbench"

    def setup(self):
        super().setup()
        self.connection.settimeout(90)

    def log_message(self, *_):
        # No sequence bodies, filenames, SSH paths or annotations in desktop logs.
        pass

    def authorized(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
            return secrets.compare_digest(cookies["bio_session"].value, self.server.session)
        except (KeyError, ValueError):
            return False

    def send_headers(self, status=200, content_type="application/json", size=None, cookie=False, disposition=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if cookie:
            self.send_header("Set-Cookie", f"bio_session={self.server.session}; HttpOnly; SameSite=Strict; Path=/")
        if size is not None:
            self.send_header("Content-Length", str(size))
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()

    def json(self, value, status=200, cookie=False):
        data = encode(value)
        self.send_headers(status, size=len(data), cookie=cookie)
        self.wfile.write(data)

    def fail(self, message, status=400):
        self.json({"error": {"code": "transport" if status == 502 else "invalid", "message": message}}, status)

    def guard(self, mutate=False):
        if self.headers.get("Host") != self.server.authority:
            self.fail("Invalid local host", 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            self.fail("Invalid local origin", 403)
            return False
        if mutate:
            if not self.authorized() or not secrets.compare_digest(self.headers.get("X-Bio-Workbench-Token", ""), self.server.csrf):
                self.fail("Desktop session expired; reopen the app", 403)
                return False
        return True

    def body(self):
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ValueError("Expected JSON with a content length")
        length = int(self.headers.get("Content-Length", "-1"))
        if not 0 <= length <= MAX_WIRE:
            raise ValueError("Request exceeds 2 MiB")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("Incomplete request")
        return read_json(raw)

    def do_GET(self):
        if not self.guard():
            return
        route = unquote(urlsplit(self.path).path)
        try:
            if route == "/api/v1/session":
                self.json({"csrf_token": self.server.csrf, "connection": self.server.transport.connection.get(), "desktop": True}, cookie=True)
            elif route.startswith("/api/"):
                if not self.authorized():
                    self.fail("Desktop session required", 403)
                elif route == "/api/v1/connection":
                    self.json(self.server.transport.connection.get())
                elif route.startswith("/api/v1/artifacts/"):
                    path, meta = self.server.transport.artifact(route[len("/api/v1/artifacts/"):])
                    safe_type = meta.get("media_type", "application/octet-stream")
                    if not isinstance(safe_type, str) or any(c in safe_type for c in "\r\n"):
                        safe_type = "application/octet-stream"
                    # Structures are fetched as text by the viewer; downloads remain attachments.
                    self.send_headers(content_type=safe_type, size=path.stat().st_size,
                                 disposition="attachment; filename*=UTF-8''" + quote(str(meta.get("name", "result")), safe=""))
                    with path.open("rb") as file:
                        shutil.copyfileobj(file, self.wfile)
                else:
                    self.fail("Unknown endpoint", 404)
            else:
                candidate = (self.server.assets / (route.lstrip("/") or "index.html")).resolve()
                if not candidate.is_relative_to(self.server.assets) or not candidate.is_file():
                    self.fail("Asset not found", 404)
                    return
                self.send_headers(content_type=mimetypes.guess_type(candidate)[0] or "application/octet-stream", size=candidate.stat().st_size)
                with candidate.open("rb") as file:
                    shutil.copyfileobj(file, self.wfile)
        except (ValueError, UnicodeError) as exc:
            self.fail(str(exc))
        except TransportError as exc:
            self.fail(str(exc), 502)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def mutate(self):
        if not self.guard(mutate=True):
            return
        route = urlsplit(self.path).path
        try:
            value = self.body()
            if route == "/api/v1/rpc" and self.command == "POST":
                self.json(self.server.transport.rpc(value))
            elif route == "/api/v1/connection" and self.command == "PATCH":
                self.json(self.server.transport.connection.update(value))
            elif route == "/api/v1/connection/check" and self.command == "POST":
                if value != {}:
                    raise ValueError("Connection check takes no parameters")
                self.json({"ok": True, "catalog": self.server.transport.call("catalog", {})})
            else:
                self.fail("Unknown endpoint", 404)
        except (ValueError, UnicodeError) as exc:
            self.fail(str(exc))
        except TransportError as exc:
            self.fail(str(exc), 502)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_POST = mutate
    do_PATCH = mutate


def main():
    parser = argparse.ArgumentParser(description="Internal desktop transport (started by Electron)")
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "bio-workbench")
    parser.add_argument("--cache", type=Path, default=Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "bio-workbench")
    args = parser.parse_args()
    if not (args.assets / "index.html").is_file():
        parser.error("Bundled desktop assets missing; build the frontend first")
    os.umask(0o077)
    server = DesktopServer(("127.0.0.1", 0), SSHTransport(Connection(args.config), args.cache), args.assets)
    print(json.dumps({"url": server.origin}), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
