#!/usr/bin/env python3
"""Install or verify the separately pinned MMseqs mapped-prefetch runtime.

Installation consumes already-built local assets. It neither compiles binaries
nor downloads substitutes; scientific-runtime packaging transports this tree.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

LOCK = Path(__file__).with_name("native-runtime.json")
LOCK_SHA256 = "65e7a0e6f7184bc8c0f4aa35c9f28af960b7b4ba59d9f21fad518e9fe4289bca"
VARIANT = "gc-mmseqs-posting-prefetch-v2"
TOOLS_DIRECTORY = "msa-tools-prefetch-v1"


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def manifest():
    if digest(LOCK) != LOCK_SHA256:
        raise ValueError("Mapped-prefetch runtime lock changed")
    return json.loads(LOCK.read_text())


def _regular(path):
    if path.resolve() != path or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"Runtime member must be a regular file without symlink parents: {path}")


def _files(root, lock, *, installed):
    for relative, expected in lock["files"].items():
        path = root / relative
        _regular(path)
        if path.stat().st_size != expected["bytes"] or digest(path) != expected["sha256"]:
            raise ValueError(f"Mapped-prefetch runtime member changed: {relative}")
        if installed and stat.S_IMODE(path.stat().st_mode) != expected["mode"]:
            raise ValueError(f"Mapped-prefetch runtime member mode changed: {relative}")
    if installed:
        installed_lock = root / "native-runtime.json"
        _regular(installed_lock)
        if digest(installed_lock) != LOCK_SHA256:
            raise ValueError("Installed mapped-prefetch runtime manifest changed")
        expected_names = set(lock["files"]) | {"native-runtime.json"}
        actual_names = set()
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Unexpected runtime symlink: {path}")
            if not path.is_dir():
                _regular(path)
                actual_names.add(str(path.relative_to(root)))
        if actual_names != expected_names:
            raise ValueError("Installed mapped-prefetch runtime inventory changed")


def validate(root, *, check_versions=True):
    root = Path(root).absolute()
    lock = manifest()
    _files(root, lock, installed=True)
    if check_versions:
        for name, flag, expected in (("mmseqs", "version", lock["mmseqs_version"]),
                                     ("mmseqs-server", "-version", lock["backend_version"])):
            actual = subprocess.check_output([str(root/"bin"/name), flag], text=True, timeout=30).strip()
            if actual != expected:
                raise ValueError(f"Mapped-prefetch runtime version changed: {name}")
    return dict(mmseqs=str(root/"bin/mmseqs"), server=str(root/"bin/mmseqs-server"),
                mmseqs_sha256=lock["files"]["portable/mmseqs"]["sha256"],
                server_sha256=lock["files"]["bin/mmseqs-server"]["sha256"],
                wrapper_sha256=lock["files"]["bin/mmseqs"]["sha256"],
                native_variant=VARIANT, runtime_manifest_sha256=LOCK_SHA256,
                runtime_files=lock["files"], build_provenance_sha256=lock["build_provenance_sha256"],
                patch_sha256=lock["patch_sha256"])


def validate_provenance(value):
    """Validate saved tool identity without requiring its original path to exist."""
    lock = manifest()
    expected = dict(mmseqs_sha256=lock["files"]["portable/mmseqs"]["sha256"],
                    server_sha256=lock["files"]["bin/mmseqs-server"]["sha256"],
                    wrapper_sha256=lock["files"]["bin/mmseqs"]["sha256"],
                    native_variant=VARIANT, runtime_manifest_sha256=LOCK_SHA256,
                    runtime_files=lock["files"], build_provenance_sha256=lock["build_provenance_sha256"],
                    patch_sha256=lock["patch_sha256"])
    if not isinstance(value, dict) or set(value) != set(expected) | {"mmseqs", "server"}:
        raise ValueError("Incomplete mapped-prefetch runtime provenance")
    observed = {key: value[key] for key in expected}
    if json.dumps(observed, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError("Mapped-prefetch runtime provenance changed")
    for name in ("mmseqs", "server"):
        if not isinstance(value[name], str) or not Path(value[name]).is_absolute():
            raise ValueError("Invalid mapped-prefetch runtime executable path")


def install(source, root):
    root, source = Path(root).absolute(), Path(source).absolute()
    if root.name != TOOLS_DIRECTORY or root.resolve() != root:
        raise ValueError(f"Install into a separate regular {TOOLS_DIRECTORY} directory")
    lock = manifest()
    root.parent.mkdir(parents=True, exist_ok=True)
    with root.with_name(root.name+".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if root.exists():
            return validate(root)  # Never replace an existing runtime in place.
        _files(source, lock, installed=False)
        stage = Path(tempfile.mkdtemp(prefix=root.name+".staging-", dir=root.parent))
        try:
            for relative, entry in lock["files"].items():
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, target)
                target.chmod(entry["mode"])
            shutil.copyfile(LOCK, stage/"native-runtime.json")
            (stage/"native-runtime.json").chmod(0o644)
            validate(stage)
            stage.rename(root)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return validate(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "verify"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    if args.action == "install" and args.source is None:
        parser.error("install requires --source with the exact pinned build assets")
    print(json.dumps(install(args.source, args.root) if args.action == "install" else validate(args.root),
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
