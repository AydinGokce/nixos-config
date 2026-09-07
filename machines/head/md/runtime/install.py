#!/usr/bin/env python3
"""Install an explicitly locked, worker-local Linux MD environment.

Package resolution never occurs here. All downloads, including the bootstrap
executable and source archives, are checked against committed SHA-256 pins.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request


HERE = Path(__file__).resolve().parent


def digest(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def fetch(item, cache):
    expected = item["sha256"]
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("Invalid SHA-256 pin")
    url = item["url"]
    if not url.startswith("https://"):
        raise ValueError("Only HTTPS package sources are allowed")
    filename = item.get("filename", url.rsplit("/", 1)[-1])
    if Path(filename).name != filename or filename in ("", ".", ".."):
        raise ValueError("Invalid cache filename")
    directory = cache / expected
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / filename
    if destination.is_file() and digest(destination) == expected:
        return destination
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
            temporary = Path(output.name)
            with urllib.request.urlopen(url, timeout=120) as response:
                shutil.copyfileobj(response, output, length=4 << 20)
        if digest(temporary) != expected:
            raise ValueError("Download failed SHA-256 verification: " + filename)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def extract_source(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                raise ValueError("Unexpected source archive entry: " + member.name)
        source.extractall(destination, filter="data")
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError("Expected one source root")
    return roots[0]


def command(argv, env=None, cwd=None):
    print("bio-md-runtime:", " ".join(map(str, argv)), flush=True)
    subprocess.run(list(map(str, argv)), env=env, cwd=cwd, check=True)


def runtime_environment(prefix):
    env = dict(os.environ)
    env.update(PATH=str(prefix / "bin") + os.pathsep + env.get("PATH", ""),
               PYTHONNOUSERSITE="1", MPLBACKEND="Agg", QT_QPA_PLATFORM="offscreen",
               LD_LIBRARY_PATH=str(prefix / "lib") + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else ""))
    env.pop("PYTHONPATH", None)
    if Path("/run/opengl-driver/lib").is_dir():
        env["LD_LIBRARY_PATH"] = "/run/opengl-driver/lib:" + env["LD_LIBRARY_PATH"]
    return env


def write_activation(prefix, kernel, ff):
    # Relative paths survive conda-pack relocation. No user's shell files change.
    (prefix / "activate.sh").write_text(
        '# Source this file in the MD worker process.\n'
        'BIO_MD_PREFIX=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)\n'
        'export BIO_MD_PREFIX\nexport PATH="$BIO_MD_PREFIX/bin:$PATH"\n'
        f'export PLUMED_KERNEL="$BIO_MD_PREFIX/{kernel.relative_to(prefix)}"\n'
        f'export GMXLIB="$BIO_MD_PREFIX/{ff.relative_to(prefix)}"\n'
        'export LD_LIBRARY_PATH="$BIO_MD_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
        'if [ -d /run/opengl-driver/lib ]; then export LD_LIBRARY_PATH="/run/opengl-driver/lib:$LD_LIBRARY_PATH"; fi\n'
        'export PYTHONNOUSERSITE=1 MPLBACKEND=Agg QT_QPA_PLATFORM=offscreen\n'
        'unset PYTHONPATH\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--variant", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/bio-md-runtime")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--gpu-smoke", action="store_true", help="Also require real CUDA offload (CUDA variant only)")
    parser.add_argument("--pack", type=Path, help="Create a relocatable conda-pack tar.gz after smoke tests")
    args = parser.parse_args()
    if args.gpu_smoke and args.variant != "cuda":
        parser.error("--gpu-smoke requires --variant cuda")
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        parser.error("This lock targets Linux x86_64 (cloud Ubuntu 24.04 or compatible glibc).")
    prefix, cache = args.prefix.absolute(), args.cache.absolute()
    if prefix == Path("/") or prefix == Path.home() or prefix.is_symlink():
        parser.error("Choose a dedicated, non-symlink runtime prefix")
    pins = json.loads((HERE / "pins.json").read_text())
    lock_path = HERE / ("linux-64-" + args.variant + ".lock.json")
    lock = json.loads(lock_path.read_text())
    if lock["schema"] != "bio-md-conda-lock.v1" or lock["variant"] != args.variant:
        raise ValueError("Wrong runtime package lock")
    fingerprint = hashlib.sha256(canonical({"pins": pins, "lock": lock,
        "installer_sha256": digest(__file__), "smoke_sha256": digest(HERE / "smoke.py"),
        "packer_sha256": digest(HERE / "pack.py")})).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / ("install-" + hashlib.sha256(str(prefix).encode()).hexdigest() + ".lock")).open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX)
        started = time.monotonic()
        print(f"bio-md-runtime: {args.variant} fingerprint {fingerprint}", flush=True)
        items = [pins["micromamba"], *lock["packages"], *pins["sources"].values(), *pins["wheels"].values()]
        with ThreadPoolExecutor(max_workers=6) as pool:
            downloaded = list(pool.map(lambda item: fetch(item, cache / "downloads"), items))
        by_sha = {item["sha256"]: path for item, path in zip(items, downloaded)}
        if args.download_only:
            print(json.dumps({"fingerprint": fingerprint, "cache": str(cache), "download_only": True}))
            return
        marker = prefix.parent / ("." + prefix.name + ".bio-md-managed.json")
        if prefix.exists() and any(prefix.iterdir()):
            if not marker.is_file() or json.loads(marker.read_text()).get("fingerprint") != fingerprint:
                raise FileExistsError("Runtime prefix already contains another environment: " + str(prefix))
        prefix.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"fingerprint": fingerprint, "variant": args.variant}))
        bootstrap = cache / ("micromamba-" + pins["micromamba"]["sha256"])
        # Re-extract from the verified archive every time: do not trust a mutable cached executable.
        with tarfile.open(by_sha[pins["micromamba"]["sha256"]]) as archive:
            with archive.extractfile("bin/micromamba") as source, bootstrap.open("wb") as output:
                shutil.copyfileobj(source, output)
        bootstrap.chmod(0o700)
        explicit = cache / (fingerprint + ".explicit.txt")
        explicit.write_text("@EXPLICIT\n" + "".join(
            by_sha[p["sha256"]].as_uri() + "#" + p["md5"] + "\n" for p in lock["packages"]))
        env = dict(os.environ)
        env.update(MAMBA_ROOT_PREFIX=str(cache / "mamba"), MAMBA_NO_BANNER="1")
        # Explicit locks may be provisioned before a GPU is attached. Actual GPU
        # availability is established only by smoke.py --gpu on the worker.
        if args.variant == "cuda":
            env["CONDA_OVERRIDE_CUDA"] = "12.9"
        verb = "install" if (prefix / "conda-meta/history").exists() else "create"
        command([bootstrap, verb, "--no-rc", "--offline", "--yes", "--prefix", prefix,
                 "--file", explicit], env)
        # conda-forge's SIMD selector hardcodes /bin/bash, absent on NixOS.
        # Keep the selector and all native engines; only make its shell lookup portable.
        dispatcher = prefix / "bin/gmx"
        dispatch_source = dispatcher.read_text()
        if dispatch_source.startswith("#! /bin/bash\n"):
            dispatcher.write_text(dispatch_source.replace("#! /bin/bash\n", "#!/usr/bin/env bash\n", 1))
        elif not dispatch_source.startswith("#!/usr/bin/env bash\n"):
            raise ValueError("Unexpected pinned GROMACS dispatcher format")
        env = runtime_environment(prefix)
        env["CC"] = str(prefix / "bin/x86_64-conda-linux-gnu-cc")
        python = prefix / "bin/python"
        for wheel in pins["wheels"].values():
            command([python, "-m", "pip", "install", "--no-index", "--no-deps",
                     "--force-reinstall", by_sha[wheel["sha256"]]], env)
        wheel_records = {}
        with tempfile.TemporaryDirectory(prefix="bio-md-build-", dir=cache) as temporary:
            build = Path(temporary)
            wheelhouse = build / "wheels"
            wheelhouse.mkdir()
            for name, source_pin in pins["sources"].items():
                source = extract_source(by_sha[source_pin["sha256"]], build / name)
                if name == "pmx":
                    fixtures = prefix / "share/bio-md-runtime/fixtures"
                    fixtures.mkdir(parents=True, exist_ok=True)
                    for fixture in ("protein.pdb", "topol.top"):
                        shutil.copyfile(source / "tests/data/alchemy" / fixture, fixtures / fixture)
                    setup = source / "setup.py"
                    before = setup.read_text()
                    old = 'setup_requires=["setuptools~=46.0.0"],'
                    if before.count(old) != 1:
                        raise ValueError("Pinned pmx packaging patch no longer applies")
                    for field in ("version=versioneer.get_version(),", "cmdclass=versioneer.get_cmdclass(),"):
                        if before.count(field) != 1:
                            raise ValueError("Pinned pmx version metadata patch no longer applies")
                    setup.write_text(before.replace(old, "setup_requires=[],")
                        .replace("version=versioneer.get_version(),", "version=" + repr(source_pin["version"]) + ",")
                        .replace("cmdclass=versioneer.get_cmdclass(),", "cmdclass={},"))
                    version_data = {"version": source_pin["version"], "full-revisionid": source_pin["commit"],
                                    "dirty": False, "error": None, "date": None}
                    (source / "src/pmx/_version.py").write_text(
                        "# Pinned archive metadata; algorithms are unchanged.\ndef get_versions():\n    return " + repr(version_data) + "\n")
                licenses = prefix / "share/bio-md-runtime/licenses"
                licenses.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / "LICENSE", licenses / (name + "-LICENSE"))
                command([python, "-m", "pip", "wheel", "--no-index", "--no-deps",
                         "--no-build-isolation", "--wheel-dir", wheelhouse, source], env)
                distribution = source_pin.get("distribution", name)
                matches = list(wheelhouse.glob(distribution + "-*.whl"))
                if len(matches) != 1:
                    raise ValueError("Missing or ambiguous source wheel: " + name)
                wheel = matches[0]
                command([python, "-m", "pip", "install", "--no-index", "--no-deps",
                         "--force-reinstall", wheel], env)
                wheel_records[name] = {"wheel_filename": wheel.name, "wheel_sha256": digest(wheel), **source_pin}
        command([python, "-m", "pip", "check"], env)
        ff = Path(subprocess.check_output([python, "-c",
            "import pathlib,pmx; print(pathlib.Path(pmx.__file__).parent/'data/mutff')"], env=env, text=True).strip())
        kernels = list((prefix / "lib").glob("lib*lum*Kernel.so"))
        if len(kernels) != 1 or not ff.is_dir():
            raise ValueError("PLUMED kernel or pmx hybrid force fields were not installed")
        write_activation(prefix, kernels[0], ff)
        env.update(PLUMED_KERNEL=str(kernels[0]), GMXLIB=str(ff))
        provenance = prefix / "share/bio-md-runtime"
        provenance.mkdir(parents=True, exist_ok=True)
        for path in (HERE / "pins.json", lock_path, HERE / "smoke.py"):
            shutil.copyfile(path, provenance / path.name)
        manifest = {"schema": "bio-md-runtime.v1", "fingerprint": fingerprint,
            "variant": args.variant, "package_lock_sha256": digest(lock_path),
            "sources": wheel_records, "packages": lock["packages"], "wheels": pins["wheels"],
            "packaging_patches": ["pmx setup_requires uses the locked setuptools; version metadata records the exact Git archive revision",
                                  "GROMACS SIMD dispatcher's /bin/bash shebang uses /usr/bin/env bash for NixOS"],
            "bfee3": {"version": "3.2.1", "python_module": "BFEE2", "gromacs_route": "geometric",
                      "namd_routes": "not installed", "synthetic_residue_parameters": "explicit validated inputs required"}}
        (prefix / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        command([python, provenance / "smoke.py", "--prefix", prefix,
                 "--output", prefix / "smoke-cpu"], env)
        manifest["cpu_smoke"] = json.loads((prefix / "smoke-cpu/report.json").read_text())
        manifest["gpu_qualification"] = "not performed; run smoke.py --gpu on the target worker"
        if args.gpu_smoke:
            command([python, provenance / "smoke.py", "--prefix", prefix,
                     "--output", prefix / "smoke-gpu", "--gpu"], env)
            manifest["gpu_qualification"] = json.loads((prefix / "smoke-gpu/report.json").read_text())
        manifest["installation_seconds"] = round(time.monotonic() - started, 2)
        (prefix / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if args.pack:
            args.pack.parent.mkdir(parents=True, exist_ok=True)
            command([python, HERE / "pack.py", "--prefix", prefix, "--output", args.pack], env)
            args.pack.with_suffix(args.pack.suffix + ".sha256").write_text(digest(args.pack) + "  " + args.pack.name + "\n")
        print(json.dumps({"prefix": str(prefix), "fingerprint": fingerprint,
                          "installation_seconds": manifest["installation_seconds"], "cpu_smoke": "passed"}))


if __name__ == "__main__":
    main()
