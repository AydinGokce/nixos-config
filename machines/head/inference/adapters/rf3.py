"""Resident pinned RF3 with cold-initialization RNG and mandatory chemistry QA.

``native_config`` contains the native constructor/run fields below. Omitted
fields use the pinned CLI defaults, including its 0.5 early-stop threshold and
pLDDT B factors. The seed belongs to the resident configuration: a different
seed requires another generation, because model initialization consumes RNG.

load() initializes the native model once and snapshots the exact post-load RNG,
pipeline-constructor RNG/configuration and metric state. Each request rebuilds
the native CPU pipeline under its original constructor RNG, then restores the
post-load RNG before the unchanged native run body.
No final tensors or mutable molecular features are shared between requests.

Default predict() includes mandatory output QA. An explicitly configured
postprocess_mode='deferred' returns status='awaiting_chemistry_validation'; the
coordinator must call postprocess(job, result, out) in the same pinned CPU
environment and require its status='complete' before publishing success.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

if __package__ in (None, ""):
    # Executing this file must not make its own rf3.py shadow the installed
    # native rf3 package when the CPU preparation child imports rf3.utils.
    script_directory = Path(__file__).absolute().parent
    sys.path[:] = [value for value in sys.path if Path(value or os.curdir).absolute() != script_directory]
    sys.path.insert(0, str(Path(__file__).absolute().parents[2]))
    from inference.adapters._common import AdapterBase, capture_rng, restore_rng, sha256
else:
    from ._common import AdapterBase, capture_rng, restore_rng, sha256


SOURCE_PIN = "b02eed6a6bdf8f44d14a80cc36e3da13c9f2291c"
WEIGHTS_SHA256 = "364ef592fd8042a9cf4176d045015190f8322f961ccca38d891b20ca578d3bb0"
RNG_POLICY = "rf3-native-cold-post-initialize-state-v1"
EXPECTED_FILE = "rf3-expected-chemistry.json"
EXPECTED_RECEIPT = "rf3-expected-chemistry-receipt.json"
TARGET = "rf3.inference_engines.rf3.RF3InferenceEngine"
METRICS = {"ptm": {"_target_": "rf3.metrics.predicted_error.ComputePTM"},
           "iptm": {"_target_": "rf3.metrics.predicted_error.ComputeIPTM"},
           "count_clashing_chains": {"_target_": "rf3.metrics.clashing_chains.CountClashingChains"}}
CONSTRUCTOR = {"num_nodes": 1, "devices_per_node": 1, "compress_outputs": False,
               "n_recycles": 10, "diffusion_batch_size": 5, "num_steps": 50,
               "template_noise_scale": 1e-5, "early_stopping_plddt_threshold": 0.5,
               "seed": 101, "verbose": False, "raise_if_missing_msa_for_protein_of_length_n": 1,
               "fallback_conformer_to_input_coords": True, "metrics_cfg": METRICS}
RUN = {"inputs": None, "out_dir": None, "dump_predictions": True,
       "dump_trajectories": False, "one_model_per_file": False,
       "annotate_b_factor_with_plddt": True, "sharding_pattern": None,
       "skip_existing": False, "template_selection": None,
       "ground_truth_conformer_selection": None, "cyclic_chains": [], "add_missing_atoms": True}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _json_digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _rng_receipt(state):
    """Portable hashes of complete RNG bytes; no tensor/storage object IDs."""
    array_hash = lambda value: hashlib.sha256(value.tobytes()).hexdigest()
    numpy_state = state["numpy"]
    return {"python_sha256": _json_digest(state["python"]),
            "numpy": {"algorithm": numpy_state[0], "state_sha256": array_hash(numpy_state[1]),
                      "position": int(numpy_state[2]), "has_gauss": int(numpy_state[3]),
                      "cached_gaussian": float(numpy_state[4])},
            "torch_sha256": array_hash(state["torch"].cpu().numpy()),
            "cuda_sha256": [array_hash(value.cpu().numpy()) for value in state["cuda"]]}


def default_native_config(checkpoint_path, *, seed=101):
    value = dict(_target_=TARGET, ckpt_path=str(checkpoint_path), **deepcopy(CONSTRUCTOR), **deepcopy(RUN))
    value["seed"] = seed
    return value


def _normalize_native(value, checkpoint):
    if not isinstance(value, dict):
        raise ValueError("RF3 native_config must be a JSON dictionary")
    default = default_native_config(checkpoint)
    if set(value) - set(default):
        raise ValueError("Unsupported native RF3 settings: " + ", ".join(sorted(set(value) - set(default))))
    result = dict(default, **deepcopy(value))
    if result["_target_"] != TARGET or Path(result["ckpt_path"]).absolute() != Path(checkpoint).absolute():
        raise ValueError("RF3 native config must use the pinned engine/checkpoint")
    for key, low, high in (("n_recycles", 1, 100), ("num_steps", 1, 1000),
                           ("diffusion_batch_size", 1, 20), ("seed", 0, 2**32 - 1)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError("Invalid RF3 native setting: " + key)
    # Broader native flags affect scientific behavior and/or data fallback. The
    # currently supported resident contract keeps the validated CLI defaults.
    variable = {"seed", "n_recycles", "num_steps", "diffusion_batch_size", "inputs", "out_dir", "cyclic_chains"}
    for key in set(default) - variable:
        if _canonical(result[key]) != _canonical(default[key]):
            raise ValueError("RF3 resident route requires its pinned native default: " + key)
    cyclic = result["cyclic_chains"]
    if (not isinstance(cyclic, list) or len(set(cyclic)) != len(cyclic)
            or any(not isinstance(chain, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", chain) for chain in cyclic)):
        raise ValueError("Invalid RF3 cyclic chain list")
    _canonical(result)
    return result


def _module(path, name):
    path = Path(path).resolve(strict=True)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write(path, value):
    path = Path(path)
    temporary = path.with_name("." + path.name + ".tmp")
    with temporary.open("x") as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _regular_tree(root):
    root = Path(root).absolute()
    for ancestor in (root, *root.parents):
        if ancestor.is_symlink():
            raise ValueError("RF3 job input/output cannot contain symlink paths")
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(directory) / name
            if path.is_symlink() or not (path.is_file() if name in names else path.is_dir()):
                raise ValueError("RF3 job tree contains a symlink or special file")
    return root


def _raw_inventory(out, name, seed, count):
    directory = out / name
    expected = {f"seed-{seed}_sample-{index}" for index in range(count)}
    actual = {path.name for path in directory.glob("seed-*_sample-*")}
    if actual != expected:
        raise RuntimeError(f"RF3 returned {len(actual)}/{count} raw sample directories; early-stop/partial outputs are failures")
    structures, files = [], {}
    for sample in sorted(expected):
        for suffix in ("_model.cif", "_summary_confidences.json", "_confidences.json"):
            path = directory / sample / (name + "_" + sample + suffix)
            if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                raise RuntimeError("RF3 raw sample is missing a required artifact: " + str(path))
            files[str(path.relative_to(out))] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
            if suffix == "_model.cif":
                structures.append(str(path))
    return structures, files


def _structure_paths(paths, out):
    result = []
    for item in paths:
        path = Path(item)
        path = path if path.is_absolute() else out / path
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(out.resolve()):
            raise ValueError("RF3 structure lies outside its exact output tree")
        result.append(str(path.absolute()))
    return result


def _tree_inventory(root):
    _regular_tree(root)
    return {str(path.relative_to(root)): {"bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in sorted(root.rglob("*")) if path.is_file()}


def expected_native(input_path, output_path, tools_dir, *, receipt_path=None, shared_root="/mnt/bio-shared"):
    """Standalone native CPU preparation; never call inside a resident model."""
    tools = Path(tools_dir).resolve(strict=True)
    prepare = _module(tools / "rf3/prepare.py", "bio_expected_prepare")
    manifest = prepare.validate(input_path)
    helper = _module(tools / "library/rf3_output.py", "bio_expected_output")
    _write(output_path, helper._expected(input_path))
    if receipt_path is not None:
        runtime = _module(tools / "rf3/runtime.py", "bio_expected_runtime")
        receipt = {"schema": 1, "kind": "rf3-expected-chemistry", "expected_sha256": sha256(output_path),
            "base_prepared_sha256": manifest["sha256"], "runtime": runtime.verify_install(shared_root),
            "sources": {relative: sha256(tools / relative) for relative in
                ("rf3/prepare.py", "library/rf3_output.py", "library/rf3_compat.py")}}
        receipt["sha256"] = _json_digest(receipt)
        _write(receipt_path, receipt)


def prepared_expected(input_path, tools_dir, *, runtime=None):
    """Verify coordinate-free expected chemistry derived during preparation."""
    entry = Path(input_path)
    paths = [entry.parent / name for name in (EXPECTED_FILE, EXPECTED_RECEIPT)]
    if not any(path.exists() for path in paths):
        return None
    if not all(path.is_file() and not path.is_symlink() for path in paths):
        raise ValueError("RF3 expected chemistry preparation is incomplete")
    tools = Path(tools_dir)
    prepare = _module(tools / "rf3/prepare.py", "bio_precomputed_expected_prepare")
    manifest = prepare.validate(entry)
    receipt = json.loads(paths[1].read_text())
    if (type(receipt.get("schema")) is not int or receipt["schema"] != 1
            or receipt.get("kind") != "rf3-expected-chemistry"
            or receipt.get("sha256") != _json_digest({k: v for k, v in receipt.items() if k != "sha256"})
            or receipt.get("expected_sha256") != sha256(paths[0])):
        raise ValueError("RF3 prepared expected chemistry receipt differs")
    base = deepcopy(manifest)
    base.pop("sha256")
    for name in (EXPECTED_FILE, EXPECTED_RECEIPT):
        base["files"].pop(name, None)
    if _json_digest(base) != receipt["base_prepared_sha256"]:
        raise ValueError("RF3 expected chemistry belongs to another prepared input")
    sources = {relative: sha256(tools / relative) for relative in
               ("rf3/prepare.py", "library/rf3_output.py", "library/rf3_compat.py")}
    if receipt.get("sources") != sources:
        raise ValueError("RF3 prepared expected chemistry helper changed")
    if runtime is not None and receipt.get("runtime") != runtime:
        raise ValueError("RF3 prepared expected chemistry native runtime differs")
    expected = json.loads(paths[0].read_text())
    document = prepare.document(prepare.read_json(entry))
    required = {"input.json"} | {component[key] for component in document["components"]
        for key in ("path", "msa_path") if key in component}
    if (expected.get("source_commit") != SOURCE_PIN or expected.get("name") != document["name"]
            or expected.get("prepared_files_sha256") != {key: manifest["files"][key] for key in required}):
        raise ValueError("RF3 expected chemistry does not bind its exact molecular files")
    return receipt


def generate_expected(input_path, output_path, tools_dir):
    """Keep RDKit's uncaptured native RNG/cache out of the model process."""
    output_path = Path(output_path)
    command = [sys.executable, "-B", str(Path(__file__).absolute()),
               "--expected-input", str(input_path), "--expected-output", str(output_path),
               "--tools-dir", str(tools_dir)]
    environment = dict(os.environ, CUDA_VISIBLE_DEVICES="", OMP_NUM_THREADS="1",
                       OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1")
    log = output_path.with_suffix(".log")
    with log.open("xb") as handle:
        subprocess.run(command, env=environment, stdout=handle, stderr=subprocess.STDOUT,
                       timeout=180, check=True)
    if output_path.is_symlink() or not output_path.is_file():
        raise RuntimeError("RF3 native CPU child did not publish its expected chemistry")
    return {"mode": "isolated-native-cpu-child-v1", "adapter_sha256": sha256(Path(__file__)),
            "command": command, "cuda_visible_devices": "", "log_sha256": sha256(log)}


@contextmanager
def _audit_scope(audit, classes, path):
    originals = {cls: cls.forward for cls in classes}
    try:
        audit.install(path)
        yield
    finally:
        for cls, original in originals.items():
            cls.forward = original


def postprocess(job, raw_result, output_dir):
    """Deterministic CPU chemistry selection, preserving every raw candidate.

    raw_result is the worker's integrity-bound result envelope. Its native phase
    retained the exact expected graph; this stage needs only NumPy/Biotite and
    never imports RF3, AtomWorks, Torch, RDKit, or model weights.
    """
    started = time.monotonic()
    result = deepcopy(raw_result)
    out = _regular_tree(Path(output_dir).resolve(strict=True))
    if result.get("status") != "awaiting_chemistry_validation" or result.get("job_id") != job.get("id"):
        raise ValueError("RF3 postprocess requires the exact pending job result")
    if result.get("job_sha256") != _json_digest(job):
        raise ValueError("RF3 postprocess job envelope changed")
    binding = result["postprocess"]
    tools = Path(binding["tools_dir"])
    for relative, expected in binding["sources"].items():
        if relative not in {"rf3/prepare.py", "library/rf3_output.py", "library/rf3_compat.py"} or sha256(tools / relative) != expected:
            raise ValueError("RF3 postprocess helper source binding differs")
    if set(binding["sources"]) != {"rf3/prepare.py", "library/rf3_output.py", "library/rf3_compat.py"}:
        raise ValueError("RF3 postprocess helper inventory is incomplete")
    entry = out / "prepared-native/input.json"
    prepare = _module(tools / "rf3/prepare.py", "bio_resident_rf3_prepare_postprocess")
    evidence = prepare.validate(entry)
    if evidence["sha256"] != result["prepared_sha256"]:
        raise ValueError("RF3 postprocess molecular input changed")
    document = prepare.document(prepare.read_json(entry))
    structures, files = _raw_inventory(out, document["name"], result["settings"]["seed"], result["settings"]["diffusion_batch_size"])
    if files != result["raw_files"] or structures != _structure_paths(result["structures"], out):
        raise ValueError("RF3 raw prediction changed before chemistry validation")
    result["structures"] = structures
    helper = _module(tools / "library/rf3_output.py", "bio_resident_rf3_output")
    expected_binding = result["expected_input"]
    if expected_binding["path"] != "rf3-native-expected-input.json":
        raise ValueError("RF3 expected chemistry path differs")
    expected_path = out / expected_binding["path"]
    if expected_path.is_symlink() or sha256(expected_path) != expected_binding["sha256"]:
        raise ValueError("RF3 expected chemistry checksum differs")
    if expected_binding["source_sha256"] != binding["sources"]["library/rf3_output.py"]:
        raise ValueError("RF3 expected chemistry parser source differs")
    expected = json.loads(expected_path.read_text())
    if expected.get("policy") != helper.POLICY or expected.get("source_commit") != SOURCE_PIN or expected.get("name") != document["name"]:
        raise ValueError("RF3 expected chemistry identity differs")
    required = {"input.json"} | {component[key] for component in document["components"]
                               for key in ("path", "msa_path") if key in component}
    if (set(expected["prepared_files_sha256"]) != required
            or any(evidence["files"].get(name) != value for name, value in expected["prepared_files_sha256"].items())):
        raise ValueError("RF3 expected chemistry does not bind its exact prepared files")
    if set(expected_binding["packages"]) != {"numpy", "biotite"}:
        raise ValueError("RF3 CPU parser version inventory is incomplete")
    for package, version in expected_binding["packages"].items():
        if importlib.metadata.version(package) != version:
            raise ValueError("RF3 CPU parser package differs: " + package)
    try:
        seed = result["settings"]["seed"]
        validation = helper._select_and_publish(out, expected)
        if validation.get("status") != "passed" or validation.get("audit_source_sha256") != binding["sources"]["library/rf3_output.py"]:
            raise RuntimeError("RF3 mandatory chemistry validation did not pass")
        if _raw_inventory(out, document["name"], seed, result["settings"]["diffusion_batch_size"])[1] != files:
            raise RuntimeError("RF3 chemistry selection modified a raw candidate")
        result.update(status="complete", output_validation=validation,
                      output_validation_sha256=sha256(out / "rf3-output-validation.json"),
                      selected_model=str(out / validation["selected"]["files"]["model"]["path"]))
        result["timings_seconds"]["postprocess"] = time.monotonic() - started
        _write(out / "rf3-resident-result.json", result)
        return result
    except Exception as exc:
        failure = dict(result, status="failed_output_chemistry", error=str(exc))
        if (out / "rf3-output-validation.json").is_file():
            failure["output_validation_sha256"] = sha256(out / "rf3-output-validation.json")
        _write(out / "rf3-resident-result.json", failure)
        raise


def postprocess_receipt(prediction_path, output_dir, *, tools_dir=None):
    """CPU command boundary: preserve the sealed GPU tree and validate a copy."""
    prediction_path = Path(prediction_path).resolve(strict=True)
    receipt = json.loads(prediction_path.read_text())
    if receipt.get("status") != "predicted" or receipt.get("error") is not None:
        raise ValueError("RF3 CPU stage requires a successful raw worker execution")
    source = _regular_tree(Path(receipt["output_dir"]).absolute())
    request = json.loads((source.parent / "request.json").read_text())
    if _json_digest(request) != receipt["request_sha256"]:
        raise ValueError("RF3 CPU stage request receipt changed")
    job = request["payload"]
    if job["id"] != receipt["id"] or job.get("model") != "rf3":
        raise ValueError("RF3 CPU stage belongs to another job/model")
    original_files = _tree_inventory(source)
    if original_files != receipt["files"]:
        raise ValueError("RF3 sealed GPU output checksum/inventory changed")
    output = _regular_tree(Path(output_dir).absolute())
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("RF3 CPU and GPU output trees must be separate")
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "validated-output"
    if destination.exists() or destination.is_symlink():
        raise ValueError("RF3 CPU validation output already exists; retain the prior attempt")
    shutil.copytree(source, destination, symlinks=False)
    if _tree_inventory(destination) != original_files or _tree_inventory(source) != original_files:
        raise ValueError("RF3 GPU artifact changed during CPU copy")
    raw = deepcopy(receipt["result"])
    paths = _structure_paths(raw["structures"], source)
    raw["structures"] = [str(Path(path).relative_to(source)) for path in paths]
    raw["execution_settings"] = deepcopy(raw["settings"])
    raw["settings"].update(inputs=str(destination / "prepared-native/input.json"), out_dir=str(destination))
    raw["cpu_copy"] = {"original_output_dir": str(source), "prediction_receipt_sha256": sha256(prediction_path),
                       "original_inventory_sha256": _json_digest(original_files)}
    if tools_dir is not None:
        raw["postprocess"]["tools_dir"] = str(Path(tools_dir).resolve(strict=True))
    checked = postprocess(job, raw, destination)
    if _tree_inventory(source) != original_files:
        raise RuntimeError("RF3 CPU validation modified the original GPU evidence")
    checked.update(output_dir=str(destination), files=_tree_inventory(destination),
                   prediction_receipt_sha256=sha256(prediction_path))
    _write(output / "result.json", checked)
    return checked


class Adapter(AdapterBase):
    model = "rf3"
    relocations = (("inputs",), ("out_dir",), ("cyclic_chains",))

    def __init__(self, config):
        config = deepcopy(config)
        config["native_config"] = _normalize_native(config.get("native_config", {}), config["checkpoint"]["path"])
        super().__init__(config)
        if config.get("postprocess_mode", "inline") not in {"inline", "deferred"}:
            raise ValueError("RF3 postprocess_mode must be inline or deferred")
        self.poisoned = False

    def load(self):
        if self.loaded or self.poisoned:
            raise RuntimeError("RF3 adapter can load only once; restart a failed resident process")
        started = time.monotonic()
        if self.config.get("device", "cuda:0") != "cuda:0":
            raise ValueError("Expose exactly the assigned RF3 CUDA device as logical cuda:0")
        if os.environ.get("LOCAL_MSA_DIRS"):
            raise ValueError("Ambient RF3 MSA directories are not allowed; every chain needs its exact prepared A3M")
        tools = Path(self.config["tools_dir"]).resolve(strict=True)
        runtime = _module(tools / "rf3/runtime.py", "bio_resident_rf3_runtime")
        self.prepare = _module(tools / "rf3/prepare.py", "bio_resident_rf3_prepare")
        self.audit = _module(tools / "rf3/feature_audit.py", "bio_resident_rf3_audit")
        self.expected_helper = _module(tools / "library/rf3_output.py", "bio_resident_rf3_expected")
        compat = _module(tools / "library/rf3_compat.py", "bio_resident_rf3_compat")
        shared = Path(self.config.get("shared_root", "/mnt/bio-shared"))
        environment = runtime.verify_install(shared)
        checkpoint = runtime.checkpoint(shared)
        if (self.config["checkpoint"]["sha256"] != WEIGHTS_SHA256
                or Path(self.config["checkpoint"]["path"]).resolve(strict=True) != checkpoint.resolve(strict=True)):
            raise ValueError("RF3 resident checkpoint does not match the verified pinned installation")
        compat.install()
        import numpy as np
        import torch
        from atomworks.ml.transforms.msa import msa
        from rf3.inference_engines.rf3 import RF3InferenceEngine
        self.np, self.torch = np, torch
        self.audit_classes = (msa.LoadPolymerMSAs, msa.PairAndMergePolymerMSAs, msa.FeaturizeMSALikeAF3)
        constructor = {key: deepcopy(self.base[key]) for key in CONSTRUCTOR}
        constructor["ckpt_path"] = str(checkpoint)
        work = _regular_tree(Path(self.config["work_dir"]).absolute())
        work.mkdir(parents=True, exist_ok=True)
        old_directory = Path.cwd()
        try:
            os.chdir(work)
            self.engine = RF3InferenceEngine(**constructor)
            self._build_pipeline = self.engine._construct_pipeline
            def capture_pipeline(cfg):
                self._pipeline_initial_rng = capture_rng(torch, np)
                self._pipeline_config = deepcopy(cfg)
                return self._build_pipeline(cfg)
            self.engine._construct_pipeline = capture_pipeline
            self.engine.initialize()
            self.engine._construct_pipeline = self._build_pipeline
            # Precisely the cold run's boundary between native initialize() and
            # parsing. Copying the controls cannot advance this saved state.
            self.initial_rng = capture_rng(torch, np)
            self._metrics = deepcopy(self.engine.trainer.metrics)
            self._initialize = self.engine.initialize
        except BaseException:
            self.poisoned = True
            raise
        finally:
            os.chdir(old_directory)
        self.loaded = True
        self.tools = tools
        self.load_receipt = dict(model="rf3", source_commit=SOURCE_PIN, environment=environment,
            checkpoint=deepcopy(self.config["checkpoint"]), settings=deepcopy(self.base),
            timings_seconds={"load": time.monotonic() - started}, rng_policy=RNG_POLICY,
            post_initialize_rng=_rng_receipt(self.initial_rng),
            pipeline_constructor_rng=_rng_receipt(self._pipeline_initial_rng),
            sources={relative: sha256(tools / relative) for relative in
                     ("rf3/runtime.py", "rf3/prepare.py", "rf3/feature_audit.py", "library/rf3_compat.py", "library/rf3_output.py")})
        restore_rng(self.initial_rng, torch, np)
        return deepcopy(self.load_receipt)

    def predict(self, job, output_dir):
        started = time.monotonic()
        # Validate fixed seed/science before AdapterBase marks a job attempted
        # or any mutable native state is touched.
        if job.get("seeds") != [self.base["seed"]] or any(type(seed) is not int for seed in job.get("seeds", [])):
            raise ValueError("RF3 job seed must match the resident's cold-initialization seed")
        candidate = dict(job)
        if "native_config" in job:
            candidate["native_config"] = _normalize_native(job["native_config"], self.config["checkpoint"]["path"])
        _regular_tree(Path(job["native_input"]).absolute().parent)
        _regular_tree(Path(output_dir).absolute())
        with self.job(candidate, output_dir) as (native, entry, out, seeds):
            _regular_tree(entry.parent)
            if out.is_relative_to(entry.parent) or entry.parent.is_relative_to(out):
                raise ValueError("RF3 prepared input and output must be separate trees")
            if entry.name != "input.json":
                raise ValueError("RF3 requires a complete prepared input.json and msa-manifest.json")
            evidence = self.prepare.validate(entry)
            precomputed = prepared_expected(entry, self.tools, runtime=self.load_receipt.get("environment"))
            prepared = out / "prepared-native"
            shutil.copytree(entry.parent, prepared, symlinks=False)
            copied = self.prepare.validate(prepared / "input.json")
            if copied != evidence:
                raise ValueError("RF3 prepared input changed during its job-owned copy")
            value = self.prepare.document(self.prepare.read_json(prepared / "input.json"))
            cyclic = value.get("cyclic_chains", [])
            if not isinstance(cyclic, list) or not set(cyclic) <= {c["chain_id"] for c in value["components"] if "seq" in c}:
                raise ValueError("RF3 cyclic metadata must name declared polymer chains")
            if "native_config" in job and native["cyclic_chains"] != cyclic:
                raise ValueError("RF3 job cyclic settings disagree with its exact molecular input")
            run = {key: deepcopy(native[key]) for key in RUN}
            run.update(inputs=str(prepared / "input.json"), out_dir=str(out), cyclic_chains=cyclic)
            settings = dict(native, **run)
            feature_path = out / "rf3-features.json"
            receipt = dict(schema=1, model="rf3", job_id=job["id"], job_sha256=_json_digest(job),
                           status="running", prepared_sha256=evidence["sha256"], settings=settings,
                           rng_policy=RNG_POLICY, load_receipt_sha256=_json_digest(self.load_receipt),
                           timings_seconds={"prepare": time.monotonic() - started},
                           postprocess={"tools_dir": str(self.tools), "sources": {
                               key: self.load_receipt["sources"][key] for key in
                               ("rf3/prepare.py", "library/rf3_output.py", "library/rf3_compat.py")}})
            _write(out / "rf3-resident-result.json", receipt)
            before_native = time.monotonic()
            old_directory = Path.cwd()
            try:
                # Some native transforms retain module references and cannot
                # be deep-copied. Recreate their original state with the native
                # factory and its exact pre-construction RNG, never the model.
                restore_rng(self._pipeline_initial_rng, self.torch, self.np)
                self._build_pipeline(deepcopy(self._pipeline_config))
                self.engine.trainer.metrics = deepcopy(self._metrics)
                # Native run calls initialize unconditionally. It was completed
                # above at the cold RNG boundary; skip only that redundant call.
                self.engine.initialize = lambda: None
                self.engine.seed = seeds[0]
                self.engine.trainer.state["model"].eval()
                with _audit_scope(self.audit, self.audit_classes, feature_path):
                    os.chdir(prepared)
                    restore_rng(self.initial_rng, self.torch, self.np)
                    self.engine.run(**run)
                if hasattr(self.torch, "cuda") and self.torch.cuda.is_available():
                    self.torch.cuda.synchronize()
                features = json.loads(feature_path.read_text())
                if {event["transform"] for event in features["events"]} != {
                        "LoadPolymerMSAs", "PairAndMergePolymerMSAs", "FeaturizeMSALikeAF3"}:
                    raise RuntimeError("RF3 prediction lacks its complete native MSA feature evidence")
                if self.prepare.validate(prepared / "input.json") != evidence:
                    raise RuntimeError("Native RF3 changed its prepared input")
                _regular_tree(out)
                structures, raw_files = _raw_inventory(out, value["name"], seeds[0], native["diffusion_batch_size"])
                # The native environment builds the unchanged helper's expected
                # chemistry. The head can then audit all samples without Torch
                # or loading native/model packages. This is per-job evidence,
                # not a cached stochastic feature tensor.
                expected_path = out / "rf3-native-expected-input.json"
                if precomputed is not None:
                    shutil.copyfile(prepared / EXPECTED_FILE, expected_path)
                    generation = {"mode": "verified-cpu-preparation-v1", "receipt_sha256": precomputed["sha256"]}
                else:
                    generation = generate_expected(prepared / "input.json", expected_path, self.tools)
                receipt.update(status="awaiting_chemistry_validation", structures=structures, raw_files=raw_files,
                               feature_audit_sha256=sha256(feature_path),
                               expected_input={"path": expected_path.name, "sha256": sha256(expected_path),
                                   "source_sha256": self.load_receipt["sources"]["library/rf3_output.py"],
                                   "generation": generation,
                                   "packages": {name: importlib.metadata.version(name) for name in ("numpy", "biotite")}})
                receipt["timings_seconds"].update(native_predict=time.monotonic() - before_native,
                                                   predict_total=time.monotonic() - started)
                _write(out / "rf3-resident-result.json", receipt)
            except BaseException as exc:
                self.poisoned = True
                self.loaded = False
                receipt.update(status="failed_native_prediction", error=str(exc), resident_restart_required=True)
                _write(out / "rf3-resident-result.json", receipt)
                raise
            finally:
                os.chdir(old_directory)
                self.engine.initialize = self._initialize
                self.engine.pipeline = None
                self.engine.trainer.metrics = self._metrics
                restore_rng(self.initial_rng, self.torch, self.np)
            if self.config.get("postprocess_mode", "inline") == "deferred":
                return receipt
            return postprocess(job, receipt, out)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Validate a private copy of a sealed RF3 worker result on CPU")
    parser.add_argument("--prediction", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-input", type=Path)
    parser.add_argument("--expected-output", type=Path)
    parser.add_argument("--expected-receipt", type=Path)
    parser.add_argument("--shared-root", default="/mnt/bio-shared")
    parser.add_argument("--tools-dir", type=Path)
    arguments = parser.parse_args()
    if arguments.expected_input or arguments.expected_output:
        if not arguments.expected_input or not arguments.expected_output or not arguments.tools_dir or arguments.prediction or arguments.output:
            parser.error("Expected chemistry requires --expected-input, --expected-output and --tools-dir only")
        expected_native(arguments.expected_input, arguments.expected_output, arguments.tools_dir,
                        receipt_path=arguments.expected_receipt, shared_root=arguments.shared_root)
    else:
        if not arguments.prediction or not arguments.output:
            parser.error("CPU output validation requires --prediction and --output")
        result = postprocess_receipt(arguments.prediction, arguments.output, tools_dir=arguments.tools_dir)
        print(json.dumps({"status": result["status"], "result": str(arguments.output / "result.json")}, allow_nan=False))
