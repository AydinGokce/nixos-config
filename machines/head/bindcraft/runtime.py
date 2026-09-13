"""Inspect and execute the isolated, pinned BindCraft installation."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from bundle import PIN, digest, encoded, inspect, materialize, read_json

HERE = Path(__file__).resolve().parent


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def installation(shared, *, require_ready=True, verify_assets=False):
    root = Path(shared) / "bindcraft"
    path = root / "install-manifest.json"
    if not path.is_file():
        raise ValueError("BindCraft is not installed; no worker launched")
    value = read_json(path.read_bytes())
    if not isinstance(value, dict) or value.get("schema") != "bio-bindcraft-install.v1":
        raise ValueError("Unsupported BindCraft installation manifest")
    if require_ready and value.get("readiness") != "ready":
        raise ValueError("BindCraft runtime is incomplete: " + str(value.get("readiness")))
    expected = {"python": "env/bin/python", "bindcraft_source": "src/bindcraft-" + PIN, "params": "params"}
    if any(value.get(key) != item for key, item in expected.items()):
        raise ValueError("BindCraft runtime paths differ from the pinned installation")
    identity = {"pins_sha256": file_digest(HERE / "pins.json"),
                "lock_sha256": file_digest(HERE / "linux-64-cuda.lock.json"),
                "installer_sha256": file_digest(HERE / "install.py")}
    fingerprint = digest(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode())
    if value.get("fingerprint") != fingerprint:
        raise ValueError("BindCraft installation differs from the deployed pins or installer")
    required = [expected["python"], expected["bindcraft_source"] + "/bindcraft.py",
                expected["bindcraft_source"] + "/functions/dssp",
                expected["bindcraft_source"] + "/functions/DAlphaBall.gcc"]
    for name in required:
        item = root / name
        if not item.is_file() or not item.resolve().is_relative_to(root.resolve()):
            raise ValueError("Missing or redirected BindCraft runtime asset: " + name)
    components = value.get("components", {})
    for component in ("environment", "sources", "af2", "pyrosetta"):
        if require_ready and components.get(component, {}).get("ready") is not True:
            raise ValueError("Incomplete BindCraft component: " + component)
    source_files = value.get("source_files", {})
    if not isinstance(source_files, dict) or expected["bindcraft_source"] + "/bindcraft.py" not in source_files:
        raise ValueError("Missing BindCraft source inventory")
    for name, expected_sha in source_files.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "src":
            raise ValueError("Unsafe BindCraft source path")
        file = root / relative
        if not file.resolve().is_relative_to(root.resolve()) or not file.is_file() or file_digest(file) != expected_sha:
            raise ValueError("Changed BindCraft source: " + name)
    check = value.get("environment_check", {})
    if check.get("path") != "cpu-install-check.json" or file_digest(root / "cpu-install-check.json") != check.get("sha256"):
        raise ValueError("Missing or changed BindCraft installation check")
    files = components.get("af2", {}).get("files", [])
    if require_ready and not files:
        raise ValueError("No verified AF2 weights in the BindCraft installation")
    for item in files:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe BindCraft asset path")
        file = root / relative
        if not file.is_file() or file.stat().st_size != item["size"] or not file.resolve().is_relative_to(root.resolve()):
            raise ValueError("Missing or changed AF2 weight: " + str(relative))
        if verify_assets:
            if file_digest(file) != item["sha256"]:
                raise ValueError("Changed AF2 weight hash: " + str(relative))
    return root, value


def preflight(bundle, shared):
    manifest, assets = inspect(bundle)
    root, value = installation(shared)
    return {"schema": 1, "status": "ready", "bindcraft_commit": PIN,
            "input_manifest": manifest, "runtime_fingerprint": value["fingerprint"],
            "runtime_manifest_sha256": digest((root / "install-manifest.json").read_bytes()),
            "licensing": value["components"]["pyrosetta"].get("use_scope"),
            "msa_required": False}


def environment(root):
    env = dict(os.environ)
    prefix = root / "env"
    env.update(PATH=str(prefix / "bin") + ":" + env.get("PATH", ""),
               LD_LIBRARY_PATH=str(prefix / "lib") + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else ""),
               PYTHONNOUSERSITE="1", MPLBACKEND="Agg", JAX_PLATFORMS="cuda",
               XLA_PYTHON_CLIENT_PREALLOCATE="false", OMP_NUM_THREADS="8")
    env.pop("PYTHONPATH", None)
    return env


def gpu_check(root):
    code = """
import importlib.metadata as m, json
import jax, jax.numpy as jnp
import colabdesign, pyrosetta
devices = jax.devices()
assert devices and all(d.platform == 'gpu' for d in devices), devices
value = jax.jit(lambda x: x @ x)(jnp.ones((128, 128))).block_until_ready()
assert float(value[0, 0]) == 128
print(json.dumps({'status':'passed', 'devices':[str(d) for d in devices],
 'versions':{name:m.version(name) for name in ['jax','jaxlib','colabdesign','pyrosetta']}}))
"""
    output = subprocess.check_output([str(root / "env/bin/python"), "-c", code],
                                     env=environment(root), text=True, timeout=180)
    return read_json(output.splitlines()[-1])


def rows(path):
    if not path.is_file():
        return 0
    with path.open() as stream:
        return sum(1 for _ in csv.DictReader(stream))


def run(bundle, shared, out):
    expected = os.environ.get("BIO_BINDCRAFT_BUNDLE_SHA256")
    actual = digest(Path(bundle).read_bytes())
    if not expected or actual != expected:
        raise ValueError("BindCraft bundle differs from the head-validated input")
    receipt = preflight(bundle, shared)
    root, value = installation(shared)
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / "bindcraft-result.json").exists() or (out / "input").exists():
        raise ValueError("BindCraft output already contains a run; use a new job")
    manifest = materialize(bundle, out / "input")
    if digest(Path(bundle).read_bytes()) != expected or manifest != receipt["input_manifest"]:
        raise ValueError("BindCraft input changed while being materialized")
    (out / "bindcraft-preflight.json").write_bytes(encoded(receipt))
    (out / "bindcraft-install.json").write_bytes(encoded(value))
    started = time.time()
    result = {"schema": 1, "kind": "bindcraft-result", "started_epoch": started,
              "bindcraft_commit": PIN, "status": "failed", "msa_required": False,
              "input_manifest": receipt["input_manifest"],
              "runtime_fingerprint": value["fingerprint"]}
    try:
        result["gpu"] = gpu_check(root)
        for name, version in result["gpu"]["versions"].items():
            if version != value["versions"].get(name):
                raise ValueError("BindCraft native package version differs from its installation: " + name)
        (out / "bindcraft-gpu.json").write_bytes(encoded(result["gpu"]))
        source = root / value["bindcraft_source"]
        settings = read_json((out / "input/settings.json").read_bytes())
        settings.update(starting_pdb=str(out / "input/target.pdb"), design_path=str(out / "designs"))
        advanced = read_json((out / "input/advanced.json").read_bytes())
        advanced.update(af_params_dir=str(root), dssp_path=str(source / "functions/dssp"),
                        dalphaball_path=str(source / "functions/DAlphaBall.gcc"))
        (out / "effective-settings.json").write_bytes(encoded(settings))
        (out / "effective-advanced.json").write_bytes(encoded(advanced))
        command = [str(root / "env/bin/python"), "-u", str(source / "bindcraft.py"),
                   "--settings", str(out / "effective-settings.json"),
                   "--advanced", str(out / "effective-advanced.json"),
                   "--filters", str(out / "input/filters.json")]
        execution_path = out / 'input/execution.json'
        if execution_path.is_file():
            result['execution'] = read_json(execution_path.read_bytes())
            result['seed_scope'] = 'Python/NumPy campaign RNG; native trajectory seeds retained in CSV; no bitwise reproducibility guarantee'
            command = [str(root / 'env/bin/python'), '-u', str(HERE / 'native_entry.py'),
                       '--source', str(source / 'bindcraft.py'), '--seed', str(result['execution']['seed']),
                       *command[3:]]
        result["command"] = command
        process = subprocess.run(command, cwd=source, env=environment(root), check=False)
        result["exit_code"] = process.returncode
        designs = out / "designs"
        result["trajectory_rows"] = rows(designs / "trajectory_stats.csv")
        result["mpnn_rows"] = rows(designs / "mpnn_design_stats.csv")
        result["final_rows"] = rows(designs / "final_design_stats.csv")
        result["pdb_files"] = sorted(str(path.relative_to(out)) for path in designs.rglob("*.pdb"))
        result["accepted_pdb_files"] = sorted(str(path.relative_to(out)) for path in (designs / "Accepted").glob("*.pdb"))
        result["status"] = "completed" if process.returncode == 0 and result["pdb_files"] else "failed"
        if result["status"] == "failed":
            raise ValueError("BindCraft did not complete with structure outputs; inspect run.log")
        return result
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        result["finished_epoch"] = time.time()
        result["elapsed_seconds"] = result["finished_epoch"] - started
        (out / "bindcraft-result.json").write_bytes(encoded(result))


def smoke_run(bundle, shared, out):
    expected = os.environ.get("BIO_BINDCRAFT_BUNDLE_SHA256")
    if not expected or digest(Path(bundle).read_bytes()) != expected:
        raise ValueError("BindCraft smoke input differs from the head-validated bundle")
    receipt = preflight(bundle, shared)
    root, value = installation(shared)
    _, assets = inspect(bundle)
    source = root / value["bindcraft_source"]
    if assets["target.pdb"] != (source / "example/PDL1.pdb").read_bytes():
        raise ValueError("The installation smoke test uses only the pinned upstream PDL1 fixture")
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = materialize(bundle, out / "input")
    if digest(Path(bundle).read_bytes()) != expected or manifest != receipt["input_manifest"]:
        raise ValueError("BindCraft smoke input changed while being materialized")
    (out / "bindcraft-preflight.json").write_bytes(encoded(receipt))
    (out / "bindcraft-install.json").write_bytes(encoded(value))
    command = [str(root / "env/bin/python"), "-u", str(HERE / "smoke.py"),
               "--root", str(root), "--out", str(out / "diagnostic")]
    subprocess.run(command, env=environment(root), check=True)
    result = read_json((out / "diagnostic/smoke-result.json").read_bytes())
    if result.get("status") != "passed":
        raise ValueError("BindCraft component diagnostic did not pass")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preflight", "doctor", "run", "smoke"])
    parser.add_argument("--shared", type=Path, default=Path("/mnt/bio-shared"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--verify-assets", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            _, result = installation(args.shared, require_ready=False, verify_assets=args.verify_assets)
        elif args.command == "preflight":
            result = preflight(args.bundle, args.shared)
        elif args.command == "smoke":
            result = smoke_run(args.bundle, args.shared, args.out)
        else:
            result = run(args.bundle, args.shared, args.out)
        print(json.dumps(result, indent=2))
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(2, f"bindcraft: {error}\n")


if __name__ == "__main__":
    main()
