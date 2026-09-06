"""Protenix 2.0.0: one InferenceRunner, a fresh native input/config per job.

native_config is the resolver's complete initial_config dictionary. Seeds are
reset by native infer_predict after model construction and before data iteration.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import time

from ._common import AdapterBase


class Adapter(AdapterBase):
    model, package, version = "protenix", "protenix", "2.0.0"
    relocations = (("input_json_path",), ("dump_dir",))

    def load(self):
        started = time.monotonic()
        checkpoint = self.check_load()
        # Native protein MSA imports read this once. Prediction never preprocesses.
        os.environ["MMSEQS_SERVICE_HOST_URL"] = "http://127.0.0.1:9"
        from ml_collections import ConfigDict
        from runner.inference import InferenceRunner, infer_predict
        from runner.msa_search import need_msa_search

        self.ConfigDict, self.infer_predict, self.need_msa_search = ConfigDict, infer_predict, need_msa_search
        config = ConfigDict(deepcopy(self.base))
        expected = Path(config.load_checkpoint_dir) / f"{config.model_name}.pt"
        if expected.resolve() != checkpoint:
            raise ValueError("Native Protenix config points to another checkpoint")
        if config.use_seeds_in_json:
            raise ValueError("Explicit per-job seeds require use_seeds_in_json=false")
        config.dump_dir = str(Path(self.config["work_dir"]) / "load")
        self.runner = InferenceRunner(config)
        self.loaded = True
        return dict(model=self.model, version=self.version, checkpoint=self.config["checkpoint"],
                    timings_seconds={"load": time.monotonic() - started},
                    rng_policy="native per-JSON seed reset after model initialization")

    def predict(self, job, output_dir):
        started = time.monotonic()
        with self.job(job, output_dir) as (config, entry, out, seeds):
            queries = json.loads(entry.read_text())
            if not isinstance(queries, list) or not queries:
                raise ValueError("Protenix requires a nonempty native query list")
            names = [q["name"] for q in queries]
            if len(set(names)) != len(names) or any(not isinstance(n, str) or "/" in n or n in {"", ".", ".."} for n in names):
                raise ValueError("Distinct safe native query names are required")
            if config["use_msa"] and any(self.need_msa_search(q) for q in queries):
                raise ValueError("Prepared Protenix input is missing native MSA paths")
            config.update(input_json_path=str(entry), dump_dir=str(out), seeds=seeds)
            current = self.ConfigDict(config)
            runner = self.runner
            previous = {key: getattr(runner, key) for key in ("configs", "dump_dir", "error_dir", "dumper")}
            previous_model_config = runner.model.configs
            try:
                runner.configs = current
                runner.model.configs = current
                runner.init_basics()
                runner.init_dumper(need_atom_confidence=current.need_atom_confidence,
                                   sorted_by_ranking_score=current.sorted_by_ranking_score)
                # Native loader creates fresh mutable features; predict destroys
                # some MSA/template keys, so these objects are never cached.
                self.infer_predict(runner, current)
                errors = list((out / "ERR").glob("*.txt"))
                structures = list(out.glob("*/seed_*/predictions/*.cif"))
                expected = len(names) * len(seeds) * current.sample_diffusion.N_sample
                if any(p.stat().st_size for p in errors) or len(structures) != expected:
                    raise RuntimeError(f"Protenix produced {len(structures)}/{expected} structures or logged an input error")
                return self.result(out, structures, current.to_dict(), started,
                                   rng_policy="native per-JSON seed reset; fresh feature objects")
            finally:
                for key, value in previous.items():
                    setattr(runner, key, value)
                runner.model.configs = previous_model_config
