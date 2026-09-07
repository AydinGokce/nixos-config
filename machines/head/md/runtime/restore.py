#!/usr/bin/env python3
"""Restore a verified conda-pack runtime on a worker; no downloads or builds."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import subprocess
import tarfile
import tempfile


HERE = Path(__file__).resolve().parent


def expected_fingerprint(variant):
    pins = json.loads((HERE / "pins.json").read_text())
    lock = json.loads((HERE / ("linux-64-" + variant + ".lock.json")).read_text())
    payload = {"pins": pins, "lock": lock, "installer_sha256": file_sha256(HERE / "install.py"),
               "smoke_sha256": file_sha256(HERE / "smoke.py"),
               "packer_sha256": file_sha256(HERE / "pack.py")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(4 << 20), b""):
            value.update(block)
    return value.hexdigest()


def validate_members(members):
    seen = set()
    for member in members:
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts or member.name in seen:
            raise ValueError("Unsafe or duplicate runtime archive member: " + member.name)
        seen.add(member.name)
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise ValueError("Special files are not allowed in a runtime archive")
        if member.issym() or member.islnk():
            if PurePosixPath(member.linkname).is_absolute():
                raise ValueError("Absolute runtime archive links are forbidden")
            base = str(name.parent) if member.issym() else "."
            target = posixpath.normpath(posixpath.join(base, member.linkname))
            if target == ".." or target.startswith("../"):
                raise ValueError("Runtime archive link escapes its prefix")


def portable_dispatcher(prefix):
    # conda-pack reconstructs conda-managed text files from the package cache.
    # Reapply the manifest's shell-only NixOS portability patch after unpacking.
    path = prefix / "bin/gmx"
    source = path.read_text()
    if source.startswith("#! /bin/bash\n"):
        path.write_text(source.replace("#! /bin/bash\n", "#!/usr/bin/env bash\n", 1))
    elif not source.startswith("#!/usr/bin/env bash\n"):
        raise ValueError("Unexpected GROMACS dispatcher in pinned runtime")


def pmx_preprocessor(prefix):
    # pmx executes `cpp`; conda deliberately installs its compiler under a
    # target-qualified name. Always select our pinned compiler, never the host's.
    target = "x86_64-conda-linux-gnu-cpp"
    compiler = prefix / "bin" / target
    if not compiler.is_file() or not os.access(compiler, os.X_OK):
        raise ValueError("Runtime is missing its pinned C preprocessor")
    alias = prefix / "bin/cpp"
    if alias.is_symlink():
        if os.readlink(alias) != target:
            raise ValueError("Unexpected C preprocessor alias in runtime")
    elif alias.exists():
        raise ValueError("Unexpected C preprocessor file in runtime")
    else:
        alias.symlink_to(target)


def restore(archive_path, expected_sha, prefix, fingerprint=None, smoke=False, gpu=False, variant=None):
    if variant is not None:
        committed = expected_fingerprint(variant)
        if fingerprint and fingerprint != committed:
            raise ValueError("Deployment fingerprint differs from committed runtime pins")
        fingerprint = committed
    if len(expected_sha) != 64 or any(c not in "0123456789abcdef" for c in expected_sha):
        raise ValueError("An explicit SHA-256 archive pin is required")
    archive_path, prefix = Path(archive_path).resolve(strict=True), Path(prefix).absolute()
    if prefix == Path("/") or prefix == Path.home() or prefix.is_symlink():
        raise ValueError("Choose a dedicated non-symlink prefix")
    if file_sha256(archive_path) != expected_sha:
        raise ValueError("Runtime archive SHA-256 differs from its deployment pin")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    lease_path = prefix.parent / ("." + prefix.name + ".restore.lock")
    with lease_path.open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX)
        marker = prefix / ".bio-md-archive.json"
        if prefix.exists():
            if not marker.is_file() or json.loads(marker.read_text()).get("archive_sha256") != expected_sha:
                raise FileExistsError("Destination contains another runtime: " + str(prefix))
            manifest = json.loads((prefix / "manifest.json").read_text())
        else:
            temporary = Path(tempfile.mkdtemp(prefix="." + prefix.name + "-", dir=prefix.parent))
            try:
                with tarfile.open(archive_path) as archive:
                    validate_members(archive.getmembers())
                    archive.extractall(temporary, filter="data")
                manifest = json.loads((temporary / "manifest.json").read_text())
                if manifest.get("schema") != "bio-md-runtime.v1":
                    raise ValueError("Missing runtime manifest schema")
                if fingerprint and manifest.get("fingerprint") != fingerprint:
                    raise ValueError("Runtime manifest fingerprint differs from deployment pin")
                if variant and manifest.get("variant") != variant:
                    raise ValueError("Runtime archive is the wrong CPU/CUDA variant")
                for executable in ("python", "gmx", "pmx", "plumed", "conda-unpack"):
                    if not (temporary / "bin" / executable).is_file():
                        raise ValueError("Runtime is missing " + executable)
                if not manifest.get("cpu_smoke", {}).get("passed"):
                    raise ValueError("Runtime has not passed its required coupled-engine smoke")
                os.rename(temporary, prefix)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            env = dict(os.environ)
            env["PATH"] = str(prefix / "bin") + os.pathsep + env.get("PATH", "")
            env["LD_LIBRARY_PATH"] = str(prefix / "lib") + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
            env["PYTHONNOUSERSITE"] = "1"
            env.pop("PYTHONPATH", None)
            subprocess.run([str(prefix / "bin/python"), str(prefix / "bin/conda-unpack")], env=env, check=True)
            marker.write_text(json.dumps({"archive_sha256": expected_sha,
                                          "fingerprint": manifest["fingerprint"]}) + "\n")
        if fingerprint and manifest.get("fingerprint") != fingerprint:
            raise ValueError("Existing runtime fingerprint differs from deployment pin")
        if variant and manifest.get("variant") != variant:
            raise ValueError("Existing runtime is the wrong CPU/CUDA variant")
        portable_dispatcher(prefix)
        pmx_preprocessor(prefix)
        if smoke:
            env = dict(os.environ)
            env["PATH"] = str(prefix / "bin") + os.pathsep + env.get("PATH", "")
            env["LD_LIBRARY_PATH"] = str(prefix / "lib") + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
            env["PYTHONNOUSERSITE"] = "1"
            env.pop("PYTHONPATH", None)
            args = [str(prefix / "bin/python"), str(prefix / "share/bio-md-runtime/smoke.py"),
                    "--prefix", str(prefix), "--output", str(prefix / ("worker-smoke-gpu" if gpu else "worker-smoke-cpu"))]
            subprocess.run(args + (["--gpu"] if gpu else []), env=env, check=True)
        return {"prefix": str(prefix), "fingerprint": manifest["fingerprint"], "archive_sha256": expected_sha}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--fingerprint")
    parser.add_argument("--variant", choices=("cpu", "cuda"), help="Also require this checkout's committed runtime fingerprint")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--gpu", action="store_true", help="Require --smoke and real GPU offload")
    args = parser.parse_args()
    if args.gpu and not args.smoke:
        parser.error("--gpu requires --smoke")
    print(json.dumps(restore(args.archive, args.sha256, args.prefix, args.fingerprint, args.smoke, args.gpu, args.variant)))


if __name__ == "__main__":
    main()
