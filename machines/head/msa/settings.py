#!/usr/bin/env python3
"""Resolve native inference options without loading weights or making queries.

Run in the recorded model environment on the same GPU type. The result is an
explicit reconstruction from observed argv, not a capture of a running model's
internal tensors. Protenix's native config builders run with downloads and model
construction intercepted; Boltz's Click parser runs without its callback.
"""
import argparse
import dataclasses
import datetime
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def observed_invocation(audit):
    processes = audit["inference_processes"]
    if not processes:
        raise ValueError("No recorded inference process")
    signatures = {json.dumps({key: p[key] for key in ("argv", "environment")}, sort_keys=True)
                  for p in processes}
    if len(signatures) != 1:
        raise ValueError("Recorded inference processes have divergent arguments or environment")
    # Forked dataloader workers retain their parent's invocation. Keep every
    # process in the audit while resolving that identical invocation once.
    return processes[0]


def native_arguments(audit, model):
    if audit["model"] != model:
        raise ValueError("Matching recorded inference model is required")
    argv = observed_invocation(audit)["argv"]
    name = "boltz" if model == "boltz2" else "protenix"
    commands = {"pred", "predict"} if model == "protenix" else {"predict"}
    positions = [i for i, value in enumerate(argv[:-1])
                 if Path(value).name == name and argv[i + 1] in commands]
    if len(positions) != 1:
        raise ValueError("Expected the observed native predict command")
    return argv[positions[0] + 2:]


def resolve_protenix(arguments, tokens):
    import runner.batch_inference as native
    import runner.inference as inference
    captured = {}
    original = native.inference_jsons

    def capture(*args, **kwargs):
        bound = inspect.signature(original).bind(*args, **kwargs)
        bound.apply_defaults()
        captured.update(bound.arguments)

    # Execute only native argument/default dispatch, replacing the entire
    # inference entry point before it can inspect inputs, query or load weights.
    with native.predict.make_context("predict", arguments) as context:
        parsed = dict(context.params)
        with patch.object(native, "inference_jsons", capture):
            native.predict.invoke(context)
    if not captured or not isinstance(tokens, int) or tokens <= 0:
        raise ValueError("Native dispatch and a positive observed token count are required")
    native.inference_configs["dump_dir"] = captured["out_dir"]
    runner_keys = inspect.signature(native.get_default_runner).parameters
    kwargs = {key: captured[key] for key in runner_keys if key in captured}
    with patch.object(native, "download_inference_cache", lambda configs: None), \
            patch.object(native, "InferenceRunner", lambda configs: configs):
        configs = native.get_default_runner(**kwargs)
    configs["input_json_path"] = captured["json_file"]
    initial = configs.to_dict()
    final = inference.update_inference_configs(configs, tokens).to_dict()
    return dict(cli_parameters=parsed, dispatched_parameters=captured,
                initial_config=initial, token_adjusted_config=final, tokens=tokens), [native.__file__, inference.__file__]


def resolve_boltz(arguments):
    import boltz.main as native
    with native.predict.make_context("predict", arguments) as context:
        params = dict(context.params)
    for key in ("msa_server_password", "msa_server_username", "api_key_value", "api_key_header"):
        if params.get(key):
            raise ValueError("Authenticated server arguments are not supported in this retained settings audit")
    if params["model"] != "boltz2":
        raise ValueError("This snapshot covers the pinned Boltz2 recipe")
    diffusion = native.Boltz2DiffusionParams()
    diffusion.step_scale = 1.5 if params["step_scale"] is None else params["step_scale"]
    msa = native.MSAModuleArgs(subsample_msa=params["subsample_msa"],
                              num_subsampled_msa=params["num_subsampled_msa"], use_paired_feature=True)
    return dict(cli_parameters=params, diffusion=dataclasses.asdict(diffusion),
                msa=dataclasses.asdict(msa), pairformer=dataclasses.asdict(native.PairformerArgsV2()),
                trainer_precision="bf16-mixed", float32_matmul_precision="highest",
                kernel_environment={k: os.environ.get(k, "1")
                                    for k in ("CUEQ_DEFAULT_CONFIG", "CUEQ_DISABLE_AOT_TUNING")}), [native.__file__]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("protenix", "boltz2"), required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--tokens", type=int)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text())
    current_versions = {d.metadata["Name"].lower().replace("_", "-"): d.version
                        for d in importlib.metadata.distributions() if d.metadata.get("Name")}
    expected = "protenix" if args.model == "protenix" else "boltz"
    version = importlib.metadata.version(expected)
    saved_versions = {key.lower().replace("_", "-"): value for key, value in audit["packages"].items()}
    if version != saved_versions[expected]:
        raise ValueError("Model version differs from recorded inference environment")
    if current_versions != saved_versions:
        raise ValueError("Installed package snapshot differs from recorded inference environment")
    recorded_env = observed_invocation(audit)["environment"]
    for key in ("PROTENIX_ROOT_DIR", "BOLTZ_CACHE", "MMSEQS_SERVICE_HOST_URL",
                "CUEQ_DEFAULT_CONFIG", "CUEQ_DISABLE_AOT_TUNING"):
        if key in recorded_env:
            os.environ[key] = recorded_env[key]
    arguments = native_arguments(audit, args.model)
    if args.model == "protenix":
        import torch
        expected_gpu = audit["gpu"].split(",", 1)[0].strip()
        if not torch.cuda.is_available() or torch.cuda.get_device_name(0) != expected_gpu:
            raise ValueError("Protenix config reconstruction needs the same actual GPU type")
        settings, sources = resolve_protenix(arguments, args.tokens)
    else:
        settings, sources = resolve_boltz(arguments)
    result = dict(version=1, model=args.model, model_version=version,
                  method="native resolved options reconstructed from recorded argv; no weight load or inference",
                  captured_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  runtime_audit_sha256=sha(args.audit), helper_sha256=sha(__file__),
                  sources={str(Path(path).resolve()): sha(path) for path in sources}, settings=settings)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False, default=str) + "\n")


if __name__ == "__main__":
    main()
