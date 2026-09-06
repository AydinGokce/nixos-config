#!/usr/bin/env python3
"""Provision a private RFAA CPU parser runtime without changing GPU packages.

The interpreter input is a pinned, dereferenced portable CPython archive. The
RDKit input is the unmodified pinned CP310 wheel. Both are extracted as ordinary
files into an immutable, inventoried generation. Shared RFAA packages and source
are read-only dependencies, never installation targets. The final rfaa.json is
published only after its native CPU import probe succeeds.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile
import zipfile


SOURCE_PIN = "d69ab3a73f8ede31a4cc005fbc076a341d848469"
PYTHON_VERSION = "3.10.20"
RDKIT_VERSION = "2024.09.6"
RDKIT_WHEEL_SHA256 = "2b5573055c8defbad7ce25db10786a56e0699faf3282daea21100c59f7af7298"
RDKIT_WHEEL_URL = "https://files.pythonhosted.org/packages/a3/f6/3a2278acc1d3831fd1d5b81ae355297be1c2ea8d8d0e200291aa2edfbdeb/rdkit-2024.9.6-cp310-cp310-manylinux_2_28_x86_64.whl"
MAX_EXTRACTED_BYTES = 512 * 1024**2
MAX_ENTRIES = 30000
CUDA_LIBRARIES = ("cuda_runtime", "cusparse", "curand", "cublas", "cudnn", "cufft", "cusolver", "nccl", "nvtx", "cuda_cupti")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def no_symlinks(path):
    path = Path(os.path.abspath(path))
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise ValueError("Private runtime paths must not traverse symlinks: " + str(part))
    return path


def relative(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("Unsafe archive path")
    name = name.rstrip("/")
    parts = name.split("/")
    if not name or any(part in ("", ".", "..") for part in parts) or PurePosixPath(name).is_absolute():
        raise ValueError("Unsafe archive path: " + name)
    return Path(*parts)


def check_artifact(path, expected):
    path = no_symlinks(path)
    if not re.fullmatch("[0-9a-f]{64}", expected) or not path.is_file() or digest(path) != expected:
        raise ValueError("Artifact SHA256 mismatch: " + str(path))
    return path


def extract_archive(archive, destination, *, wheel=False):
    """No archive extraction API: allow only regular files/directories."""
    destination = no_symlinks(destination)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    seen, total = set(), 0
    with (zipfile.ZipFile(archive) if wheel else tarfile.open(archive, "r:gz")) as container:
        entries = container.infolist() if wheel else container.getmembers()
        if len(entries) > MAX_ENTRIES:
            raise ValueError("Archive has too many entries")
        for item in entries:
            name = relative(item.filename if wheel else item.name)
            if name in seen:
                raise ValueError("Duplicate archive path: " + str(name))
            seen.add(name)
            if wheel:
                mode = item.external_attr >> 16
                directory = item.is_dir()
                if stat.S_IFMT(mode) not in (0, stat.S_IFDIR if directory else stat.S_IFREG):
                    raise ValueError("Archive contains a link or special file")
                size = item.file_size
            else:
                if not (item.isdir() or item.isreg()):
                    raise ValueError("Archive contains a link or special file")
                mode, directory, size = item.mode, item.isdir(), item.size
            total += size
            if size < 0 or total > MAX_EXTRACTED_BYTES:
                raise ValueError("Archive exceeds the extracted size limit")
            target = destination / name
            if directory:
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = container.open(item) if wheel else container.extractfile(item)
            with source, target.open("xb") as output:
                remaining = size
                while remaining:
                    chunk = source.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise ValueError("Truncated archive member")
                    output.write(chunk)
                    remaining -= len(chunk)
                if source.read(1):
                    raise ValueError("Archive member exceeds its declared size")
                output.flush()
                os.fsync(output.fileno())
            target.chmod(0o555 if mode & 0o111 else 0o444)


def inventory(generation):
    result = {}
    for path in sorted(Path(generation).rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("Private runtime contains a link or special file")
        if path.is_file() and path != Path(generation) / "receipt.json":
            result[str(path.relative_to(generation))] = {"bytes": path.stat().st_size,
                                                       "mode": stat.S_IMODE(path.stat().st_mode), "sha256": digest(path)}
    return result


def atomic_json(path, value):
    path = no_symlinks(path)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def describe(generation, shared_site, source, loader, libraries):
    generation = Path(generation)
    shared_site, source, loader = (Path(path).resolve(strict=True) for path in (shared_site, source, loader))
    if not shared_site.is_dir() or not source.is_dir() or not loader.is_file():
        raise ValueError("Missing shared packages, source, or dynamic loader")
    system = [str(Path(path).resolve(strict=True)) for path in libraries]
    if any(not Path(path).is_dir() for path in system):
        raise ValueError("System library path must be a directory")
    private_python, chemistry = generation / "python", generation / "chemistry"
    runtime_libraries = [str(private_python / "lib"), str(chemistry / "rdkit.libs"), str(shared_site / "torch/lib")]
    runtime_libraries.extend(str(shared_site / "nvidia" / name / "lib") for name in CUDA_LIBRARIES
                             if (shared_site / "nvidia" / name / "lib").is_dir())
    if (shared_site / "openbabel/lib").is_dir():
        runtime_libraries.append(str(shared_site / "openbabel/lib"))
    runtime_libraries.extend([str(loader.parent), *system])
    runtime_libraries = list(dict.fromkeys(runtime_libraries))
    return {"schema": 1, "command": [str(loader), "--library-path", os.pathsep.join(runtime_libraries),
                                     str(private_python / "bin/python3.10")],
            "pythonpath": [str(chemistry), str(shared_site), str(source)], "library_paths": runtime_libraries,
            "environment": {"RFAA_SOURCE_DIR": str(source), "PYTHONDONTWRITEBYTECODE": "1"}}


PROBE = r'''
import importlib, json, platform, sys
import numpy, torch, scipy, rdkit
from omegaconf import OmegaConf
from openbabel import openbabel
for name in ["rf2aa.chemical", "rf2aa.data.parsers", "rf2aa.data.protein", "rf2aa.data.small_molecule", "rf2aa.data.nucleic_acid", "rf2aa.data.merge_inputs"]:
    importlib.import_module(name)
assert "rf2aa.run_inference" not in sys.modules and "dgl" not in sys.modules
print("RFAA_RUNTIME_PROBE=" + json.dumps({"python":platform.python_version(), "rdkit":rdkit.__version__, "torch":torch.__version__, "numpy":numpy.__version__, "scipy":scipy.__version__, "openbabel":openbabel.OBReleaseVersion(), "rdkit_path":rdkit.__file__, "torch_path":torch.__file__},sort_keys=True))
'''


def native_probe(config, scratch):
    """CPU imports only. The provisioning caller can add a systemd memory cap."""
    environment = {"PATH": os.environ.get("PATH", "/run/current-system/sw/bin:/usr/bin:/bin"),
                   "HOME": str(scratch), "TMPDIR": str(scratch), "CUDA_VISIBLE_DEVICES": "",
                   "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": str(scratch / "python"),
                   "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2",
                   "PYTHONPATH": os.pathsep.join(config["pythonpath"]),
                   "LD_LIBRARY_PATH": os.pathsep.join(config["library_paths"]), **config["environment"]}
    result = subprocess.run([*config["command"], "-s", "-c", PROBE], env=environment, cwd=scratch,
                            capture_output=True, text=True, timeout=180, check=False)
    if result.returncode:
        raise ValueError("RFAA CPU import probe failed: " + result.stderr[-12000:])
    rows = [line.removeprefix("RFAA_RUNTIME_PROBE=") for line in result.stdout.splitlines()
            if line.startswith("RFAA_RUNTIME_PROBE=")]
    if len(rows) != 1:
        raise ValueError("RFAA CPU import probe returned no unique receipt")
    report = json.loads(rows[0])
    if report.get("python") != PYTHON_VERSION or report.get("rdkit") != RDKIT_VERSION or report.get("torch") != "2.0.1+cu118":
        raise ValueError("RFAA CPU runtime version differs from its supported pins")
    if not Path(report["rdkit_path"]).resolve().is_relative_to(Path(config["pythonpath"][0]).resolve()) or not Path(report["torch_path"]).resolve().is_relative_to(Path(config["pythonpath"][1]).resolve()):
        raise ValueError("RFAA imports escaped their expected package roots")
    return report


def pin_nix_roots(config, directory, *, store="/nix/store"):
    """Direct roots in /nix/var/nix/gcroots keep loader/library closures alive."""
    store = Path(store)
    dependencies = set()
    for item in [config["command"][0], *config["library_paths"]]:
        path = Path(item)
        if path.is_relative_to(store):
            dependency = store / path.relative_to(store).parts[0]
            if not dependency.is_dir():
                raise ValueError("Missing Nix dependency: " + str(dependency))
            dependencies.add(dependency)
    directory = no_symlinks(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    roots = {}
    for dependency in sorted(dependencies):
        target = directory / dependency.name
        if os.path.lexists(target):
            if not target.is_symlink() or target.readlink() != dependency:
                raise ValueError("Conflicting Nix GC root: " + str(target))
        else:
            target.symlink_to(dependency, target_is_directory=True)
        roots[str(target)] = str(dependency)
    return roots


def verify_generation(generation):
    generation = no_symlinks(generation)
    no_symlinks(generation / "receipt.json")
    receipt = json.loads((generation / "receipt.json").read_text())
    if receipt.get("schema") != 1 or receipt.get("inventory") != inventory(generation):
        raise ValueError("Private runtime inventory mismatch")
    return receipt


def provision(*, root, python_archive, python_sha256, rdkit_wheel, shared_site, source, loader,
              libraries, probe=native_probe, source_pin=SOURCE_PIN, gc_root=None):
    root = no_symlinks(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise ValueError("Private runtime root must be accessible only to its owner")
    interpreter = check_artifact(python_archive, python_sha256)
    wheel = check_artifact(rdkit_wheel, RDKIT_WHEEL_SHA256)
    source = Path(source).resolve(strict=True)
    actual_pin = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual_pin != source_pin:
        raise ValueError("Shared RFAA source differs from its pinned commit")
    spec = {"schema": 1, "python_sha256": python_sha256, "rdkit_sha256": RDKIT_WHEEL_SHA256,
            "source_pin": source_pin, "shared_site": str(Path(shared_site).resolve(strict=True)), "source": str(source),
            "loader": str(Path(loader).resolve(strict=True)), "libraries": [str(Path(x).resolve(strict=True)) for x in libraries]}
    identity = hashlib.sha256(canonical(spec)).hexdigest()
    generation = root / "generations" / identity
    lock = no_symlinks(root / "provision.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("Provision lock must be a regular file")
    with os.fdopen(fd, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        # Register roots before using store objects so a later system upgrade
        # and GC cannot strand the explicit loader/library paths.
        gc_roots = (pin_nix_roots(describe(generation, shared_site, source, loader, libraries), Path(gc_root) / identity)
                    if gc_root is not None else {})
        no_symlinks(generation)
        generation.parent.mkdir(exist_ok=True, mode=0o700)
        if generation.exists():
            receipt = verify_generation(generation)
            if receipt.get("inputs") != spec:
                raise ValueError("Existing runtime generation has different pinned inputs")
        else:
            with tempfile.TemporaryDirectory(prefix=".rfaa-stage-", dir=root) as temporary:
                stage = Path(temporary) / "generation"
                stage.mkdir(mode=0o700)
                extract_archive(interpreter, stage / "python")
                extract_archive(wheel, stage / "chemistry", wheel=True)
                if not (stage / "python/bin/python3.10").is_file() or not (stage / "chemistry/rdkit/__init__.py").is_file():
                    raise ValueError("Artifacts do not contain the expected CPython/RDKit layout")
                report = probe(describe(stage, shared_site, source, loader, libraries), Path(temporary))
                receipt = {"schema": 1, "kind": "rfaa-cpu-runtime", "inputs": spec, "probe": report, "inventory": inventory(stage)}
                atomic_json(stage / "receipt.json", receipt)
                (stage / "receipt.json").chmod(0o444)
                os.rename(stage, generation)
                for path in sorted(generation.rglob("*"), reverse=True):
                    if path.is_dir():
                        path.chmod(0o555)
                generation.chmod(0o555)
        # Probe the final absolute paths as well; a relocatable archive must work
        # after promotion, and mutable shared packages may have changed since use.
        config = describe(generation, shared_site, source, loader, libraries)
        with tempfile.TemporaryDirectory(prefix=".rfaa-probe-", dir=root) as temporary:
            report = probe(config, Path(temporary))
        config["generation"] = str(generation)
        config["receipt_sha256"] = digest(generation / "receipt.json")
        config["probe"] = report
        config["gc_roots"] = gc_roots
        atomic_json(root / "rfaa.json", config)
        return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/var/lib/bio-library-runtime")
    parser.add_argument("--python-archive", required=True)
    parser.add_argument("--python-sha256", required=True)
    parser.add_argument("--rdkit-wheel", required=True)
    parser.add_argument("--shared-site", default="/mnt/bio-shared/envs/rfaa/lib/python3.10/site-packages")
    parser.add_argument("--source", default="/mnt/bio-shared/src/rfaa")
    parser.add_argument("--loader", required=True)
    parser.add_argument("--library", action="append", default=[])
    parser.add_argument("--gc-root", default="/nix/var/nix/gcroots/bio-library-rfaa")
    args = vars(parser.parse_args())
    args["libraries"] = args.pop("library")
    print(json.dumps(provision(**args), indent=2))


if __name__ == "__main__":
    main()
