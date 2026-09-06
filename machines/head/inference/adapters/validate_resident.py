"""Bounded native validation child: one cold adapter, one or more requests.

The external operator launches separate isolated A/B children and an A/B/A
resident child. All inputs, settings, sources and outputs are retained; this
helper never provisions resources, changes services, searches, or retries.
"""
import argparse
import datetime
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time
import traceback


def native_state(torch, numpy):
    import pickle
    import random
    value = {"python": hashlib.sha256(pickle.dumps(random.getstate())).hexdigest(),
             "numpy": hashlib.sha256(pickle.dumps(numpy.random.get_state())).hexdigest(),
             "torch_cpu": hashlib.sha256(torch.get_rng_state().numpy().tobytes()).hexdigest(),
             "torch_cuda": [hashlib.sha256(x.cpu().numpy().tobytes()).hexdigest() for x in torch.cuda.get_rng_state_all()],
             "matmul_precision": torch.get_float32_matmul_precision(),
             "matmul_allow_tf32": torch.backends.cuda.matmul.allow_tf32,
             "cudnn_allow_tf32": torch.backends.cudnn.allow_tf32,
             "cudnn_benchmark": torch.backends.cudnn.benchmark,
             "cudnn_deterministic": torch.backends.cudnn.deterministic,
             "deterministic_algorithms": torch.are_deterministic_algorithms_enabled(),
             "default_dtype": str(torch.get_default_dtype())}
    return value


def model_hash(native, torch):
    digest = hashlib.sha256()
    for name, value in sorted(native.state_dict().items()):
        digest.update(json.dumps([name, list(value.shape), str(value.dtype)]).encode())
        digest.update(value.detach().cpu().contiguous().reshape(-1).view(torch.uint8).numpy().tobytes())
    return digest.hexdigest()


def feature_tree(value, torch, numpy):
    if torch.is_tensor(value):
        data = value.detach().cpu().contiguous()
        return {"kind": "torch", "dtype": str(data.dtype), "shape": list(data.shape),
                "sha256": hashlib.sha256(data.reshape(-1).view(torch.uint8).numpy().tobytes()).hexdigest()}
    if isinstance(value, numpy.ndarray):
        if value.dtype.hasobject:
            return {"kind": "numpy-object", "shape": list(value.shape), "items": feature_tree(value.tolist(), torch, numpy)}
        return {"kind": "numpy", "dtype": str(value.dtype), "shape": list(value.shape),
                "sha256": hashlib.sha256(numpy.ascontiguousarray(value).tobytes()).hexdigest()}
    if isinstance(value, dict):
        return {str(key): feature_tree(item, torch, numpy) for key, item in sorted(value.items(), key=lambda row: str(row[0]))}
    if isinstance(value, (list, tuple)):
        return [feature_tree(item, torch, numpy) for item in value]
    if value is None or isinstance(value, (str, bool, int, float, numpy.generic)):
        return {"kind": type(value).__name__, "value": repr(value)}
    if hasattr(value, "get_annotation_categories"):
        result = {"kind": type(value).__name__, "coord": feature_tree(value.coord, torch, numpy),
                  "annotations": {name: feature_tree(value.get_annotation(name), torch, numpy) for name in value.get_annotation_categories()}}
        if value.bonds is not None:
            result["bonds"] = feature_tree(value.bonds.as_array(), torch, numpy)
        return result
    raise TypeError("Unrecognized native feature object: " + type(value).__module__ + "." + type(value).__name__)


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as handle:
        json.dump(value, handle, indent=2, default=str, allow_nan=False)
        handle.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    out = Path(request["output"])
    out.mkdir(parents=True, exist_ok=True)
    if time.time() >= request["deadline_epoch"]:
        raise RuntimeError("Validation deadline has passed")
    for relative, digest in request["source_files"].items():
        path = Path(request["source_root"]) / relative
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Validation source changed: {relative}")
    # Environment caps are set by the owning unit before any native import.
    import torch
    torch.set_num_threads(request["torch_threads"])
    torch.set_num_interop_threads(1)
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("Validation requires exactly the selected visible CUDA device")
    if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != request["boot_id"]:
        raise RuntimeError("Managed worker boot identity changed")
    audit = dict(version=1, started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 invocation_id=os.environ.get("INVOCATION_ID"), pid=os.getpid(),
                 argv=sys.argv, gpu=torch.cuda.get_device_name(0),
                 selected_gpu=os.environ.get("CUDA_VISIBLE_DEVICES"),
                 torch_threads=torch.get_num_threads(), torch_interop_threads=torch.get_num_interop_threads(),
                 packages={d.metadata["Name"]: d.version for d in importlib.metadata.distributions() if d.metadata.get("Name")},
                 environment={k: os.environ[k] for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                              "PYTHONPYCACHEPREFIX", "TORCH_EXTENSIONS_DIR", "TRITON_CACHE_DIR", "LD_LIBRARY_PATH") if k in os.environ},
                 request_sha256=hashlib.sha256(args.request.read_bytes()).hexdigest())
    if audit["packages"] != request["packages"]:
        raise RuntimeError("Native validation package snapshot differs from the frozen reference")
    write(out / "runtime-audit.json", audit)
    module = importlib.import_module("inference.adapters." + request["config"]["model"])
    adapter = module.Adapter(request["config"])
    started = time.monotonic()
    try:
        load = adapter.load()
        write(out / "load.json", load)
        import numpy as np
        native = getattr(adapter, "module", None)
        if native is None:
            native = adapter.runner.model
        write(out / "loaded-model-audit.json", {"state_dict_sha256": model_hash(native, torch),
              "native_state": native_state(torch, np)})
        seed_events = []
        forward_events = []
        current_output = [None]
        # Observation only: invoke the exact native seeding function unchanged,
        # then retain a hash of each RNG stream before any forward computation.
        if request["config"]["model"] == "protenix":
            import runner.inference as native_runner
            original_seed = native_runner.seed_everything
            def observe_seed(*args, **kwargs):
                result = original_seed(*args, **kwargs)
                seed_events.append({"args": args, "kwargs": kwargs, "state": native_state(torch, np)})
                return result
            native_runner.seed_everything = observe_seed
            original_forward = adapter.runner.predict
            def observe_forward(data):
                event = {"features": feature_tree(data["input_feature_dict"], torch, np),
                         "pre_forward_rng": native_state(torch, np), "pre_forward_model_sha256": model_hash(native, torch)}
                result = original_forward(data)
                event["post_forward_model_sha256"] = model_hash(native, torch)
                if "coordinate" in result:
                    path = current_output[0] / f"native-draw-coordinates-{len(forward_events)}.npy"
                    np.save(path, result["coordinate"].detach().float().cpu().numpy(), allow_pickle=False)
                    event["native_draw_coordinates"] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                forward_events.append(event)
                return result
            adapter.runner.predict = observe_forward
        elif request["config"]["model"] == "openfold3":
            original_seed = native.reseed
            def observe_seed(*args, **kwargs):
                result = original_seed(*args, **kwargs)
                seed_events.append({"args": args, "kwargs": kwargs, "state": native_state(torch, np)})
                return result
            native.reseed = observe_seed
            original_forward = native.forward
            def observe_forward(batch):
                event = {"features": feature_tree(batch, torch, np), "pre_forward_rng": native_state(torch, np),
                         "pre_forward_model_sha256": model_hash(native.model, torch)}
                result = original_forward(batch)
                event["post_forward_model_sha256"] = model_hash(native.model, torch)
                forward_events.append(event)
                return result
            native.forward = observe_forward
        identities = []
        for index, job in enumerate(request["jobs"]):
            if time.time() >= request["deadline_epoch"]:
                raise RuntimeError("Validation work deadline reached")
            native = getattr(adapter, "module", None)
            if native is None:
                native = adapter.runner.model
            identity = id(native)
            seed_events.clear()
            forward_events.clear()
            current_output[0] = out / f"job-{index + 1}"
            entry_state = native_state(torch, np)
            result = adapter.predict(job, out / f"job-{index + 1}")
            torch.cuda.synchronize()
            result["resident_model_identity"] = identity
            result["parameters_on_cuda"] = all(p.is_cuda for p in native.parameters())
            result["gpu_allocated_bytes"] = torch.cuda.memory_allocated()
            result["gpu_reserved_bytes"] = torch.cuda.memory_reserved()
            result["job_entry_native_state"] = entry_state
            result["native_seed_events"] = list(seed_events)
            result["native_forward_events"] = list(forward_events)
            if not result["parameters_on_cuda"]:
                raise RuntimeError("Prediction offloaded the resident model parameters")
            identities.append(identity)
            write(out / f"job-{index + 1}" / "adapter-result.json", result)
        if len(set(identities)) != 1:
            raise RuntimeError("Resident model object was replaced between jobs")
        write(out / "complete.json", dict(status="complete", jobs=len(identities),
                                          elapsed_seconds=time.monotonic() - started, model_identity=identities[0]))
    except BaseException as exc:
        write(out / "failure.json", dict(status="failed", error=str(exc), traceback=traceback.format_exc(),
                                         elapsed_seconds=time.monotonic() - started))
        raise


if __name__ == "__main__":
    main()
