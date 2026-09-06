from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import uuid

MAX_WIRE = 2 * 1024 * 1024
CHUNK = 524288
HEAD = "31.56.109.100"
HOST_PIN = "31.56.109.100 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAn+9pjF2dRZ5hDvjWrvht+Q+vfUTjyORY58GT3Su35Q\n"
REMOTE = "env BIO_WORKBENCH_ACTOR=harrison /run/current-system/sw/bin/bio-workbench rpc"
METHODS = frozenset("catalog upload.begin upload.chunk upload.get upload.finish batch.validate batch.create batch.get batch.list batch.cancel job.get job.logs job.artifacts job.cancel artifact.read annotation.put annotation.list".split())


class TransportError(Exception):
    pass


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def encode(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":")).encode()


def read_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))


class Connection:
    def __init__(self, directory: Path):
        self.directory = private_dir(directory)
        self.path = directory / "connection.json"
        self.lock = threading.RLock()
        default_key = Path.home() / ".ssh/datacrunch_ed25519"
        self.value = {"host": HEAD, "user": "root", "port": 22,
                      "key_path": str(default_key) if default_key.is_file() else ""}
        if self.path.exists():
            self.value = self.validate(read_json(self.path.read_bytes()))
        self.known_hosts = directory / "cluster_known_hosts"
        self.known_hosts.write_text(HOST_PIN)
        self.known_hosts.chmod(0o600)

    @staticmethod
    def validate(value):
        if not isinstance(value, dict) or set(value) != {"host", "user", "port", "key_path"}:
            raise ValueError("Connection requires host, user, port and key_path")
        host, user, port, key = (value[k] for k in ("host", "user", "port", "key_path"))
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", host):
            raise ValueError("Invalid SSH hostname")
        if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", user):
            raise ValueError("Invalid SSH username")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid SSH port")
        if not isinstance(key, str) or any(ord(c) < 32 for c in key) or len(key) > 4096:
            raise ValueError("Invalid SSH key path")
        if key and not Path(key).expanduser().is_absolute():
            raise ValueError("SSH key path must be absolute")
        return dict(value, key_path=str(Path(key).expanduser()) if key else "")

    def get(self):
        with self.lock:
            return dict(self.value, configured=True)

    def update(self, value):
        value = self.validate(value)
        with self.lock:
            fd, path = tempfile.mkstemp(prefix="connection-", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(encode(value))
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(path, self.path)
            finally:
                Path(path).unlink(missing_ok=True)
            self.value = value
        return self.get()


class SSHTransport:
    def __init__(self, connection: Connection, cache: Path):
        self.connection = connection
        self.cache = private_dir(cache)
        # Keep Unix-domain socket paths short, even with long macOS home paths.
        self.sockets = Path(tempfile.mkdtemp(prefix="bio-ssh-"))
        self.slots = threading.BoundedSemaphore(6)
        self.download_lock = threading.Lock()

    def argv(self):
        value = self.connection.get()
        digest = hashlib.sha256(encode(value)).hexdigest()[:16]
        argv = [os.environ.get("BIO_SSH", "ssh"), "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=12", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
                "-o", "ControlMaster=auto", "-o", "ControlPersist=60",
                "-o", f"ControlPath={self.sockets / digest}", "-o", "ClearAllForwardings=yes"]
        if value["host"] == HEAD and value["port"] == 22:
            argv += ["-o", f"UserKnownHostsFile={self.connection.known_hosts}"]
        if value["key_path"]:
            argv += ["-o", "IdentitiesOnly=yes", "-i", value["key_path"]]
        return argv + ["-p", str(value["port"]), "-l", value["user"], "--", value["host"], REMOTE]

    def rpc(self, request):
        if not isinstance(request, dict) or set(request) != {"id", "method", "params"}:
            raise ValueError("Expected an RPC id, method and params")
        if not isinstance(request["id"], str) or not 1 <= len(request["id"]) <= 200:
            raise ValueError("Invalid request ID")
        if (not isinstance(request["method"], str) or request["method"] not in METHODS
                or not isinstance(request["params"], dict)):
            raise ValueError("Unknown RPC method or invalid params")
        body = encode(request) + b"\n"
        if len(body) > MAX_WIRE:
            raise ValueError("RPC exceeds 2 MiB")
        with self.slots:
            try:
                # File-backed output bounds memory even if a compromised endpoint floods stdout.
                with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                    result = subprocess.run(self.argv(), input=body, stdout=output, stderr=errors, timeout=75)
                    errors.seek(0)
                    detail = errors.read(4096).decode("utf-8", "replace").strip()
                    if result.returncode:
                        raise TransportError(f"SSH connection failed: {detail or 'no response'}")
                    output.seek(0)
                    data = output.read(MAX_WIRE + 1)
            except subprocess.TimeoutExpired as exc:
                raise TransportError("Connection timed out. The head may have accepted this request; reconnect using the same request key.") from exc
            except OSError as exc:
                raise TransportError(f"Unable to start SSH: {exc}") from exc
        if len(data) > MAX_WIRE:
            raise TransportError("Head response exceeds 2 MiB")
        try:
            response = read_json(data)
            if (not isinstance(response, dict) or response.get("id") != request["id"]
                    or ("result" in response) == ("error" in response)):
                raise ValueError("Mismatched RPC response")
            return response
        except (ValueError, UnicodeError) as exc:
            raise TransportError("Invalid or mismatched head response") from exc

    def call(self, method, params):
        response = self.rpc({"id": str(uuid.uuid4()), "method": method, "params": params})
        if "error" in response:
            error = response["error"]
            if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                raise TransportError("Invalid head error response")
            raise TransportError(error["message"])
        result = response["result"]
        if not isinstance(result, dict):
            raise TransportError("Invalid head result object")
        return result

    def artifact(self, artifact_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", artifact_id):
            raise ValueError("Invalid artifact ID")
        with self.download_lock:
            # Ask head even on cache hits: actor/resource access and sealed-byte integrity are authoritative.
            first = self.call("artifact.read", {"artifact_id": artifact_id, "offset": 0, "max_bytes": CHUNK})
            size, digest = first.get("size"), first.get("sha256")
            if type(size) is not int or not 0 <= size <= 2 * 1024**3 or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise TransportError("Invalid artifact size or digest (desktop limit 2 GiB)")
            path = self.cache / digest
            if path.is_file() and path.stat().st_size == size:
                with path.open("rb") as cached:
                    cache_digest = hashlib.file_digest(cached, "sha256").hexdigest()
                if cache_digest == digest:
                    return path, first
            fd, temporary = tempfile.mkstemp(prefix="artifact-", dir=self.cache)
            try:
                hasher, offset, part = hashlib.sha256(), 0, first
                with os.fdopen(fd, "wb") as file:
                    while True:
                        try:
                            raw = base64.b64decode(part["data_base64"], validate=True)
                        except (KeyError, ValueError) as exc:
                            raise TransportError("Invalid artifact bytes") from exc
                        if (part.get("artifact_id") != artifact_id or part.get("offset") != offset
                                or part.get("size") != size or part.get("sha256") != digest
                                or len(raw) > CHUNK or part.get("next_offset") != offset + len(raw)
                                or offset + len(raw) > size):
                            raise TransportError("Artifact chunk failed integrity checks")
                        file.write(raw)
                        hasher.update(raw)
                        offset += len(raw)
                        if part.get("eof"):
                            break
                        if not raw:
                            raise TransportError("Artifact transfer made no progress")
                        part = self.call("artifact.read", {"artifact_id": artifact_id, "offset": offset, "max_bytes": CHUNK})
                    if offset != size or hasher.hexdigest() != digest:
                        raise TransportError("Artifact SHA-256 or size mismatch")
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return path, first
