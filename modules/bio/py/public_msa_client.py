"""Keep native public MSA queries serial and route their HTTPS through the head.

The native CLI, arguments, query modes and scientific outputs are unchanged.
Only the installed clients' complete public-query functions hold the shared
lease; model loading and inference run after releasing it.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from functools import wraps
import fcntl
import importlib
import inspect
import ipaddress
import json
import math
import os
from pathlib import Path
import runpy
import stat
import sys
import time
import urllib.parse
import urllib.request


PUBLIC_HOSTS = frozenset({"api.colabfold.com", "api.mmseqs.com"})
HEALTH = {"service": "bio-public-msa-proxy", "schema": 1}
HOOKS = {
    "boltz2": (("boltz.data.msa.mmseqs2", "run_mmseqs2"),
               ("boltz.main", "run_mmseqs2")),
    "protenix": (("protenix.web_service.colab_request_utils", "run_mmseqs2_service"),
                 ("protenix.web_service.colab_request_parser", "run_mmseqs2_service")),
    "openfold3": (("openfold3.core.data.tools.colabfold_msa_server", "query_colabfold_msa_server"),),
}


def fatal(message):
    # Protenix catches Exception around its MSA functions and may continue.
    # Transport/lease failures must stop prediction instead of losing MSAs.
    raise SystemExit("bio-submit: public MSA guard: " + message)


def checked_deadline(value):
    if value is None:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        fatal("invalid query deadline")
    if not math.isfinite(result) or result <= 0:
        fatal("invalid query deadline")
    return result


def remaining(deadline):
    if deadline is None:
        return None
    value = deadline - time.time()
    if value <= 0:
        fatal("query deadline elapsed before a public query could start")
    return value


@contextmanager
def shared_query_lock(path, deadline=None):
    """Hold the same NFS flock for workers and RF3's head-side preparation."""
    deadline = checked_deadline(deadline)
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        fatal("query lock must be an absolute path without symlinks")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        fatal("cannot open the shared query lock: " + str(exc))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            fatal("shared query lock has unsafe ownership or permissions")
        while True:
            left = remaining(deadline)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(.2, left) if left is not None else .2)
            except OSError as exc:
                fatal("shared query locking failed: " + str(exc))
        yield
    finally:
        os.close(descriptor)


def proxy_configuration(environment=None):
    environment = os.environ if environment is None else environment
    proxy = environment.get("BIO_PUBLIC_MSA_PROXY", "")
    lock = environment.get("BIO_PUBLIC_MSA_LOCK", "")
    try:
        parsed = urllib.parse.urlsplit(proxy)
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        valid = (parsed.scheme == "http" and loopback and parsed.port is not None
                 and parsed.username is None and parsed.password is None
                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid or not lock or not Path(lock).is_absolute():
        fatal("BIO_PUBLIC_MSA_PROXY must name the loopback HTTP proxy and BIO_PUBLIC_MSA_LOCK an absolute shared path")
    return proxy.rstrip("/"), Path(lock), checked_deadline(environment.get("BIO_JOB_DEADLINE_EPOCH"))


def check_proxy(proxy, deadline=None):
    left = remaining(deadline)
    timeout = min(5, left) if left is not None else 5
    try:
        # The health check itself is local, irrespective of ambient proxy vars.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(proxy + "/health", timeout=timeout) as response:
            value = response.read(1025)
            if response.status != 200 or len(value) > 1024 or json.loads(value) != HEALTH:
                fatal("head proxy health response is invalid")
    except (OSError, ValueError) as exc:
        fatal("head proxy is unavailable: " + str(exc))


def public_endpoint(value):
    try:
        parsed = urllib.parse.urlsplit(str(value))
        valid = (parsed.scheme == "https" and parsed.hostname in PUBLIC_HOSTS
                 and parsed.port in {None, 443} and parsed.username is None and parsed.password is None)
    except ValueError:
        valid = False
    if not valid:
        fatal("native public query attempted an unexpected endpoint")


class ProxyRequests:
    """A module-local facade; unrelated model downloads keep their own routing."""
    def __init__(self, original, proxy, deadline):
        self.original, self.proxy, self.deadline = original, proxy, deadline

    def __getattr__(self, name):
        return getattr(self.original, name)

    def call(self, method, url, *args, **kwargs):
        public_endpoint(url)
        # Explicit per-request entries win over environment/session proxies,
        # including NO_PROXY=*. Both schemes remain set across redirects so a
        # redirected request cannot silently bypass the head's allowlist.
        forced = {"http": self.proxy, "https": self.proxy, "no_proxy": ""}
        for host in PUBLIC_HOSTS:
            forced["http://" + host] = self.proxy
            forced["https://" + host] = self.proxy
        kwargs["proxies"] = forced
        remaining(self.deadline)
        try:
            return getattr(self.original, method)(url, *args, **kwargs)
        except self.original.exceptions.ProxyError as exc:
            fatal("head proxy transport failed: " + str(exc))
        except self.original.exceptions.RequestException:
            # Preserve native timeout/retry behavior when the proxy is alive.
            # A dead tunnel must not become Protenix's swallowed MSA failure.
            check_proxy(self.proxy, self.deadline)
            raise

    def get(self, url, *args, **kwargs):
        return self.call("get", url, *args, **kwargs)

    def post(self, url, *args, **kwargs):
        return self.call("post", url, *args, **kwargs)


def guarded_query(original, proxy, lock, deadline):
    @wraps(original)
    def query(*args, **kwargs):
        bound = inspect.signature(original).bind(*args, **kwargs)
        bound.apply_defaults()
        public_endpoint(bound.arguments.get("host_url", "https://api.colabfold.com"))
        with shared_query_lock(lock, deadline):
            check_proxy(proxy, deadline)
            namespace = original.__globals__
            requests = namespace.get("requests")
            if requests is None:
                fatal("installed native MSA hook no longer uses the expected requests module")
            namespace["requests"] = ProxyRequests(requests, proxy, deadline)
            try:
                return original(*args, **kwargs)
            finally:
                namespace["requests"] = requests
    query.__bio_public_msa_original__ = original
    return query


def install_hooks(model, proxy, lock, deadline):
    wrapped = {}
    for module_name, function_name in HOOKS[model]:
        module = importlib.import_module(module_name)
        original = getattr(module, function_name, None)
        original = getattr(original, "__bio_public_msa_original__", original)
        if not inspect.isfunction(original):
            fatal("installed native MSA hook is missing: " + module_name + "." + function_name)
        if original not in wrapped:
            wrapped[original] = guarded_query(original, proxy, lock, deadline)
        setattr(module, function_name, wrapped[original])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=tuple(HOOKS), required=True)
    parser.add_argument("--entrypoint", type=Path, required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    proxy, lock, deadline = proxy_configuration()
    if not args.entrypoint.is_absolute() or not args.entrypoint.is_file():
        fatal("native CLI entrypoint is unavailable")
    with shared_query_lock(lock, deadline):
        check_proxy(proxy, deadline)
    install_hooks(args.model, proxy, lock, deadline)
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    sys.argv = [str(args.entrypoint), *arguments]
    runpy.run_path(str(args.entrypoint), run_name="__main__")


if __name__ == "__main__":
    main()
