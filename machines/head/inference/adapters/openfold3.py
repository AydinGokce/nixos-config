"""OpenFold3 0.5.0: fresh experiment/data objects around one loaded module.

native_config is the retained experiment_config; config.model_config is the
retained model_config. Prepared template directories may be relocated per job.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import time

from ._common import AdapterBase, capture_rng, restore_rng, resident_strategy


class Adapter(AdapterBase):
    model, package, version = "openfold3", "openfold3", "0.5.0"
    relocations = tuple(("experiment_settings", key) for key in ("output_dir", "log_dir")) + tuple(
        ("template_preprocessor_settings", key) for key in (
            "output_directory", "structure_directory", "cache_directory", "log_directory"))

    def _runner(self, config, out, seeds):
        config = deepcopy(config)
        config["experiment_settings"].update(output_dir=str(out), log_dir=str(out / "logs"), seeds=seeds)
        if config["experiment_settings"].get("use_msa_server") or config["experiment_settings"].get("use_templates"):
            raise ValueError("Resident OF3 requires complete precomputed MSAs/templates with remote preparation disabled")
        if config["experiment_settings"].get("skip_existing"):
            raise ValueError("Resident OF3 requires fresh outputs, not skip_existing")
        settings = self.ExperimentConfig.model_validate(config)
        runner = self.Runner(settings)
        # Cached properties must belong to this job. Reusing a whole runner
        # would retain its first datamodule, input query set, and writer paths.
        runner.model_config = self.ConfigDict(deepcopy(self.config["model_config"]))
        return runner

    def load(self):
        started = time.monotonic()
        checkpoint = self.check_load()
        if Path(self.base["inference_ckpt_path"]).resolve() != checkpoint:
            raise ValueError("Native OF3 configuration points to another checkpoint")
        args = self.base["pl_trainer_args"]
        if args["devices"] != 1 or args["num_nodes"] != 1 or args.get("deepspeed_config_path") or args.get("mpi_plugin"):
            raise ValueError("Resident OF3 supports one explicitly selected CUDA device")
        import torch
        import numpy as np
        from ml_collections import ConfigDict
        from openfold3.entry_points.experiment_runner import InferenceExperimentRunner
        from openfold3.entry_points.validator import InferenceExperimentConfig
        from openfold3.projects.of3_all_atom.config.inference_query_format import InferenceQuerySet
        from openfold3.run_openfold import _configure_torch_backend, _enable_tf32

        self.torch, self.np = torch, np
        self.ConfigDict, self.Runner = ConfigDict, InferenceExperimentRunner
        self.ExperimentConfig, self.QuerySet = InferenceExperimentConfig, InferenceQuerySet
        _configure_torch_backend()
        if self.config.get("settings", {}).get("use_tf32", True):
            _enable_tf32()
        seeds = list(self.base["experiment_settings"]["seeds"])
        runner = self._runner(self.base, Path(self.config["work_dir"]) / "load", seeds)
        runner.setup()
        self.module = runner.lightning_module.eval().to(self.config.get("device", "cuda:0"))
        self.initial_rng = capture_rng(torch, np)
        self.loaded = True
        return dict(model=self.model, version=self.version, checkpoint=self.config["checkpoint"],
                    timings_seconds={"load": time.monotonic() - started},
                    rng_policy="restore job-entry RNG; native OF3 reseeds each query before forward")

    def predict(self, job, output_dir):
        started = time.monotonic()
        with self.job(job, output_dir) as (config, entry, out, seeds):
            runner = self._runner(config, out, seeds)
            query_set = self.QuerySet.from_json(entry)
            if not query_set.queries:
                raise ValueError("OF3 prepared input contains no queries")
            # The native model, weights, precision and layers remain untouched;
            # only the job's control objects and mutable data are fresh.
            runner.lightning_module = self.module
            runner.strategy = resident_strategy(self.config.get("device", "cuda:0"))
            previous_log = self.module.log_dir
            self.module.log_dir = runner.log_dir
            restore_rng(self.initial_rng, self.torch, self.np)
            try:
                runner._log_experiment_config()
                runner._log_model_config()
                runner.run(query_set)
                runner.cleanup()
                summary = (out / "summary.txt").read_text()
                failed = re.search(r"Failed Queries:\s*(\d+)", summary)
                successful = re.search(r"Successful Queries:\s*(\d+)", summary)
                structures = list(out.rglob("*_model.cif"))
                samples = runner.num_diffusion_samples
                expected = len(query_set.queries) * len(seeds) * samples
                if not failed or int(failed[1]) or not successful or int(successful[1]) < 1 or len(structures) != expected:
                    raise RuntimeError(f"OF3 produced {len(structures)}/{expected} structures or reported failed queries")
                return self.result(out, structures, dict(experiment_config=runner.experiment_config.model_dump(mode="json"),
                                    model_config=runner.model_config.to_dict()), started,
                                   rng_policy="native query reseeding; fresh datamodule and callbacks per job")
            finally:
                self.module.log_dir = previous_log
                restore_rng(self.initial_rng, self.torch, self.np)
