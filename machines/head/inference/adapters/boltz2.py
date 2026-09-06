"""Boltz 2.2.1 resident structure prediction, bound to one native CLI seed.

native_config is the complete resolved Click parameter mapping. A different
seed requires a separate config/worker: constructor RNG consumption is retained
by restoring the captured post-load state for every prediction job.
"""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import time

from ._common import AdapterBase, capture_rng, restore_rng, resident_strategy


class Adapter(AdapterBase):
    model, package, version = "boltz2", "boltz", "2.2.1"
    relocations = (("data",), ("out_dir",))

    def load(self):
        started = time.monotonic()
        checkpoint = self.check_load()
        params = self.base
        if params["model"] != "boltz2" or params["accelerator"] != "gpu" or params["devices"] != 1:
            raise ValueError("Resident Boltz2 requires one selected CUDA device")
        if type(params.get("seed")) is not int:
            raise ValueError("A fixed native Boltz seed must be part of the resident config")
        if params.get("use_msa_server"):
            raise ValueError("Resident Boltz requires precomputed MSAs")
        cache = Path(params["cache"]).resolve(strict=True)
        native_checkpoint = Path(params["checkpoint"]) if params.get("checkpoint") else cache / "boltz2_conf.ckpt"
        if native_checkpoint.resolve() != checkpoint:
            raise ValueError("Native Boltz configuration points to another checkpoint")
        # Boltz2's native process_inputs loads individual mols/*.pkl records.
        # ccd.pkl belongs to the Boltz1 branch and is absent from a valid
        # download_boltz2 cache.
        if not (cache / "mols").is_dir():
            raise ValueError("Boltz chemical cache must be installed before loading the adapter")
        import os
        import torch
        import numpy as np
        import boltz.main as native
        from rdkit import Chem

        native.load_canonicals(cache / "mols")

        self.torch, self.np, self.native = torch, np, native
        torch.set_grad_enabled(False)
        torch.set_float32_matmul_precision("highest")
        Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps)
        native.seed_everything(params["seed"])
        for key in ("CUEQ_DEFAULT_CONFIG", "CUEQ_DISABLE_AOT_TUNING"):
            os.environ.setdefault(key, "1")
        self.preprocess_rng = capture_rng(torch, np)
        diffusion = native.Boltz2DiffusionParams()
        diffusion.step_scale = 1.5 if params["step_scale"] is None else params["step_scale"]
        msa = native.MSAModuleArgs(subsample_msa=params["subsample_msa"],
                                  num_subsampled_msa=params["num_subsampled_msa"], use_paired_feature=True)
        steering = native.BoltzSteeringParams()
        steering.fk_steering = params["use_potentials"]
        steering.physical_guidance_update = params["use_potentials"]
        self.predict_args = {key: params[key] for key in (
            "recycling_steps", "sampling_steps", "diffusion_samples", "max_parallel_samples",
            "write_full_pae", "write_full_pde")}
        self.predict_args["write_confidence_summary"] = True
        self.module = native.Boltz2.load_from_checkpoint(
            checkpoint, strict=True, predict_args=deepcopy(self.predict_args), map_location="cpu",
            diffusion_process_args=asdict(diffusion), ema=False, use_kernels=not params["no_kernels"],
            pairformer_args=asdict(native.PairformerArgsV2()), msa_args=asdict(msa), steering_args=asdict(steering))
        self.module.eval().to(self.config.get("device", "cuda:0"))
        self.post_load_rng = capture_rng(torch, np)
        self.loaded = True
        return dict(model=self.model, version=self.version, checkpoint=self.config["checkpoint"],
                    timings_seconds={"load": time.monotonic() - started}, seed=params["seed"],
                    rng_policy="fixed CLI seed; restore full post-construction RNG before each native data iterator")

    def predict(self, job, output_dir):
        started = time.monotonic()
        with self.job(job, output_dir) as (params, entry, out, seeds):
            if seeds != [self.base["seed"]]:
                raise ValueError("Boltz seed differs from its resident configuration; select another config_id")
            import yaml
            data = yaml.safe_load(entry.read_text())
            if not isinstance(data, dict) or not data.get("sequences"):
                raise ValueError("Boltz prepared input must contain sequences")
            for item in data["sequences"]:
                if "protein" in item:
                    msa = item["protein"].get("msa")
                    if not isinstance(msa, str) or not msa or msa == "empty":
                        raise ValueError("Prepared Boltz protein input is missing its MSA")
                    path = Path(msa) if Path(msa).is_absolute() else entry.parent / msa
                    if not path.is_file() or not path.stat().st_size:
                        raise ValueError("Prepared Boltz MSA does not exist or is empty")
            native = self.native
            cache = Path(params["cache"])
            target_out = out / f"boltz_results_{entry.stem}"
            target_out.mkdir()
            restore_rng(self.preprocess_rng, self.torch, self.np)
            try:
                native.process_inputs(
                    data=[entry], out_dir=target_out, ccd_path=cache / "ccd.pkl", mol_dir=cache / "mols",
                    use_msa_server=False, msa_server_url="http://127.0.0.1:9",
                    msa_pairing_strategy=params["msa_pairing_strategy"], boltz2=True,
                    preprocessing_threads=params["preprocessing_threads"], max_msa_seqs=params["max_msa_seqs"])
                manifest = native.Manifest.load(target_out / "processed" / "manifest.json")
                if len(manifest.records) != 1 or manifest.records[0].id != entry.stem:
                    raise RuntimeError("Boltz preprocessing omitted or replaced the expected input")
                if any(record.affinity for record in manifest.records):
                    raise ValueError("This resident adapter provides structure predictions, not the separate affinity model")
                processed = target_out / "processed"
                optional = lambda name: processed / name if (processed / name).exists() else None
                module = native.Boltz2InferenceDataModule(
                    manifest=manifest, target_dir=processed / "structures", msa_dir=processed / "msa",
                    mol_dir=cache / "mols", num_workers=params["num_workers"],
                    constraints_dir=optional("constraints"), template_dir=optional("templates"),
                    extra_mols_dir=optional("mols"), override_method=params["method"])
                writer = native.BoltzWriter(data_dir=processed / "structures", output_dir=target_out / "predictions",
                                            output_format=params["output_format"], boltz2=True,
                                            write_embeddings=params["write_embeddings"])
                trainer = native.Trainer(default_root_dir=target_out,
                                         strategy=resident_strategy(self.config.get("device", "cuda:0")),
                                         callbacks=[writer], accelerator="gpu", devices=1, precision="bf16-mixed")
                restore_rng(self.post_load_rng, self.torch, self.np)
                self.module.predict_args = deepcopy(self.predict_args)
                trainer.predict(self.module, datamodule=module, return_predictions=False)
                structures = [p for p in (target_out / "predictions" / entry.stem).glob(f"{entry.stem}_model_*")
                              if p.suffix in {".pdb", ".cif"}]
                if len(structures) != params["diffusion_samples"]:
                    raise RuntimeError("Boltz did not produce every requested diffusion sample")
                effective = deepcopy(params)
                effective.update(data=str(entry), out_dir=str(out))
                return self.result(out, structures, effective, started,
                                   rng_policy="fixed native seed with full post-load RNG replay")
            finally:
                self.module.predict_args = deepcopy(self.predict_args)
                restore_rng(self.post_load_rng, self.torch, self.np)
