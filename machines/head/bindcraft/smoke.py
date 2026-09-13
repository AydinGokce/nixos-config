#!/usr/bin/env python3
"""Run bounded, real BindCraft component diagnostics on its pinned public PDL1 fixture.

Run with the installed BindCraft Python through the managed runtime smoke recipe.
This performs one design-gradient step, one ProteinMPNN sample, one AF2 complex
prediction, one AF2 monomer prediction, native relaxation, interface scoring and
DSSP. It does not qualify a binder, change production filters, or run a campaign.
The managed worker supplies the overall execution timeout.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import gc
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
import traceback

PIN = "efb5bfeb8b4b1a5944256f979c34e0c8e6a82d9d"
FIXTURE_SHA256 = "d3c95434dcadf26d005340b15bd92be61e101ed921478c26f2a5550f198e61f6"
BINDER_LENGTH = 65
SEED = 17
REQUIRED_STAGES = (
    "runtime_and_gpu", "af2_design_gradient", "proteinmpnn",
    "af2_complex_prediction", "af2_monomer_prediction", "pyrosetta_relaxation",
    "interface_scoring_dalphaball", "dssp",
)


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 << 20), b""):
            checksum.update(block)
    return checksum.hexdigest()


def publish(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def finite_scalar(value, name):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite diagnostic metric: " + name)
    return number


def metrics(log, keys):
    return {key: finite_scalar(log[key], key) for key in keys}


def pdb_summary(path, expected):
    residues = {}
    atoms = 0
    for line in Path(path).read_text().splitlines():
        if not line.startswith("ATOM  "):
            continue
        atoms += 1
        if len(line) < 54:
            raise ValueError("Truncated PDB atom in " + str(path))
        for offset in (30, 38, 46):
            finite_scalar(line[offset:offset + 8], "PDB coordinate")
        if line[12:16].strip() == "CA":
            residues.setdefault(line[21], set()).add(line[22:27])
    counts = {chain: len(values) for chain, values in residues.items()}
    if counts != expected or atoms == 0:
        raise ValueError(f"Unexpected PDB chains/residues in {path}: {counts}, expected {expected}")
    return {"atoms": atoms, "residues_by_chain": counts, "coordinates_finite": True}


def forwarding_helper(binary, output, calls):
    """Run the unchanged native binary with inherited streams; log its exit status."""
    wrapper = output / binary.name
    script = (
        "#!" + sys.executable + "\n"
        "import json, os, subprocess, sys, time\n"
        f"binary = {str(binary)!r}\n"
        f"calls = {str(calls)!r}\n"
        "with open(calls, 'a') as stream:\n"
        "    stream.write(json.dumps({'binary': binary, 'argv': sys.argv[1:], "
        "'pid': os.getpid(), 'epoch': time.time(), 'event': 'started'}) + '\\n')\n"
        "code = subprocess.call([binary, *sys.argv[1:]])\n"
        "with open(calls, 'a') as stream:\n"
        "    stream.write(json.dumps({'binary': binary, 'argv': sys.argv[1:], "
        "'pid': os.getpid(), 'epoch': time.time(), 'event': 'completed', 'exit_code': code}) + '\\n')\n"
        "sys.exit(code if code >= 0 else 128 - code)\n"
    )
    wrapper.write_text(script)
    wrapper.chmod(0o755)
    return wrapper


def helper_invocations(path, binary):
    if not path.is_file():
        return []
    return [value for line in path.read_text().splitlines()
            if (value := json.loads(line))["binary"] == str(binary) and value.get("event") == "completed"]


def run(root, out):
    root, out = root.resolve(), out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError("Use a new, empty diagnostic output directory")
    started = time.monotonic()
    result_path = out / "smoke-result.json"
    result = {
        "schema": "bio-bindcraft-smoke.v1", "status": "running",
        "diagnostic_only": True, "accepted_binder": False,
        "native_cli_end_to_end_validated": False, "production_campaign_validated": False,
        "quality_scope": "Component execution only; no accepted-binder or production-quality claim",
        "production_thresholds_modified": False,
        "bindcraft_commit": PIN, "root": str(root),
        "started_epoch": time.time(), "stages": {},
    }
    publish(result_path, result)

    @contextmanager
    def stage(name):
        entry = {"status": "running", "started_epoch": time.time()}
        result["stages"][name] = entry
        publish(result_path, result)
        clock = time.monotonic()
        print("BindCraft diagnostic stage: " + name, flush=True)
        try:
            yield entry
            # Reject NaN/Infinity before describing this stage as operational.
            json.dumps(entry, allow_nan=False)
            entry["status"] = "passed"
        except Exception as error:
            entry.update(status="failed", error=f"{type(error).__name__}: {error}")
            raise
        finally:
            entry["elapsed_seconds"] = time.monotonic() - clock
            entry["finished_epoch"] = time.time()
            publish(result_path, result)

    try:
        with stage("runtime_and_gpu") as info:
            source = root / "src" / ("bindcraft-" + PIN)
            if Path(sys.executable).resolve() != (root / "env/bin/python").resolve():
                raise ValueError("Run this diagnostic with the installed BindCraft env/bin/python")
            if any(character.isspace() for character in str(out) + str(root)):
                raise ValueError("Native Rosetta helper paths require directories without whitespace")
            fixture = source / "example/PDL1.pdb"
            if digest(fixture) != FIXTURE_SHA256:
                raise ValueError("The public upstream PDL1 fixture does not match its pin")
            source_hashes = {str(path.relative_to(source)): digest(path) for path in (
                fixture, source / "bindcraft.py", source / "functions/colabdesign_utils.py",
                source / "functions/pyrosetta_utils.py", source / "functions/biopython_utils.py",
                source / "functions/generic_utils.py", source / "functions/dssp",
                source / "functions/DAlphaBall.gcc",
            )}
            info["source_sha256"] = source_hashes
            info["runtime_manifest_sha256"] = digest(root / "install-manifest.json")
            install = json.loads((root / "install-manifest.json").read_text())
            info["runtime_fingerprint"] = install.get("fingerprint")
            info["colabdesign_commit"] = install.get("components", {}).get("sources", {}).get("colabdesign_commit")
            info["pyrosetta_use_scope"] = install.get("components", {}).get("pyrosetta", {}).get("use_scope")
            info["fixture"] = pdb_summary(fixture, {"A": 115})
            target = out / "target.pdb"
            shutil.copyfile(fixture, target)
            os.environ.update(JAX_PLATFORMS="cuda", MPLBACKEND="Agg", PYTHONNOUSERSITE="1")
            os.environ.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", "false")
            os.environ.setdefault("OMP_NUM_THREADS", "8")
            sys.path.insert(0, str(source))
            os.chdir(out)
            import jax
            import jax.numpy as jnp
            import numpy as np
            from colabdesign import mk_afdesign_model, clear_mem
            from colabdesign.mpnn import weights_soluble
            from Bio.PDB import PDBParser, DSSP
            import pyrosetta as pr
            import functions as native

            devices = jax.devices()
            if not devices or any(device.platform != "gpu" for device in devices):
                raise ValueError("A real JAX GPU backend is required; CPU fallback is not diagnostic success")
            probe = jax.jit(lambda x: x @ x)(jnp.ones((128, 128))).block_until_ready()
            if finite_scalar(probe[0, 0], "GPU matmul") != 128 or any(d.platform != "gpu" for d in probe.devices()):
                raise ValueError("GPU execution probe failed")
            info["gpu_devices"] = [str(device) for device in devices]
            info["gpu_probe_devices"] = [str(device) for device in probe.devices()]
            info["versions"] = {name: importlib.metadata.version(name) for name in (
                "jax", "jaxlib", "colabdesign", "pyrosetta", "numpy", "biopython",
            )}
            info["versions"]["python"] = sys.version
            info["used_checkpoint_sha256"] = {str(path): digest(path) for path in (
                root / "params/params_model_1_multimer_v3.npz",
                root / "params/params_model_1_ptm.npz",
                Path(weights_soluble.__file__).parent / "v_48_020.pkl",
            )}
            binary_dir = out / "helper-wrappers"
            binary_dir.mkdir()
            calls = out / "helper-invocations.jsonl"
            dalphaball = source / "functions/DAlphaBall.gcc"
            dssp = source / "functions/dssp"
            advanced_path = source / "settings_advanced/default_4stage_multimer.json"
            filter_path = source / "settings_filters/default_filters.json"
            advanced = json.loads(advanced_path.read_text())
            overrides = {
                "design_algorithm": "3stage", "soft_iterations": 1,
                "temporary_iterations": 0, "hard_iterations": 0, "greedy_iterations": 0,
                "num_recycles_design": 0, "num_recycles_validation": 0,
                "sample_models": False, "num_seqs": 1,
                "optimise_beta": False, "save_design_animations": False,
                "save_design_trajectory_plots": False, "save_trajectory_pickle": False,
                "remove_unrelaxed_trajectory": False, "remove_unrelaxed_complex": False,
                "remove_binder_monomer": False, "af_params_dir": str(root),
                "dalphaball_path": str(forwarding_helper(dalphaball, binary_dir, calls)),
                "dssp_path": str(forwarding_helper(dssp, binary_dir, calls)),
            }
            advanced.update(overrides)
            filters = json.loads(filter_path.read_text())
            shutil.copyfile(filter_path, out / "production-filters.json")
            settings = {
                "fixture_sha256": FIXTURE_SHA256, "target_chain": "A", "hotspot": "56",
                "binder_length": BINDER_LENGTH, "design_seed": SEED, "prediction_seed": SEED,
                "mpnn_seed": None, "mpnn_seed_policy": "Unmodified upstream wrapper uses entropy seed; realized sequence is retained",
                "advanced_source_sha256": digest(advanced_path), "diagnostic_overrides": overrides,
                "advanced": advanced, "production_filters_sha256": digest(filter_path),
                "design_models": [0], "design_model_name": "model_1_multimer_v3",
                "prediction_models": [0], "prediction_model_name": "model_1_ptm",
                "prediction_use_multimer": False, "diagnostic_continues_after_quality_rejection": True,
                "native_helper_instrumentation": "Forwarding wrappers execute unchanged pinned binaries with inherited streams and log their exit codes",
            }
            publish(out / "diagnostic-settings.json", settings)
            result["settings"] = settings
            paths = native.generate_directories(str(out / "native"))
            failure_csv = str(out / "native/failures.csv")
            native.generate_filter_pass_csv(failure_csv, str(filter_path))
            name = "diagnostic_pdl1_l65_s17"

        with stage("af2_design_gradient") as info:
            trajectory = native.binder_hallucination(
                name, str(target), "A", "56", BINDER_LENGTH, SEED,
                advanced["weights_helicity"], [0], advanced, paths, failure_csv,
            )
            gradient = np.asarray(trajectory.aux["grad"]["seq"], dtype=np.float64)
            if not gradient.size or not np.isfinite(gradient).all():
                raise ValueError("AF2 design returned missing or nonfinite sequence gradients")
            norm = finite_scalar(np.linalg.norm(gradient), "design gradient norm")
            if norm <= 0:
                raise ValueError("AF2 sequence gradient was zero")
            info.update(gradient_shape=list(gradient.shape), gradient_l2_norm=norm,
                        optimizer_steps=int(trajectory._k))
            if info["optimizer_steps"] != 1:
                raise ValueError("Diagnostic exceeded or skipped its one design step")
            info["metrics"] = metrics(trajectory.aux["log"], ("loss", "plddt", "ptm", "i_ptm", "pae", "i_pae"))
            info["native_quality_termination"] = trajectory.aux["log"]["terminate"]
            info["quality_accepted"] = False
            candidates = list((out / "native/Trajectory").rglob(name + ".pdb"))
            if len(candidates) != 1:
                raise ValueError("Expected exactly one native design trajectory")
            trajectory_pdb = out / "trajectory.pdb"
            shutil.copyfile(candidates[0], trajectory_pdb)
            info["structure"] = pdb_summary(trajectory_pdb, {"A": 115, "B": BINDER_LENGTH})
            sequence = str(trajectory.get_seqs()[0])
            if len(sequence) != BINDER_LENGTH or set(sequence) - set("ACDEFGHIKLMNPQRSTVWY"):
                raise ValueError("Invalid hallucinated binder sequence")
            info["sequence"] = sequence
            del trajectory
            gc.collect()
            clear_mem()

        with stage("proteinmpnn") as info:
            interface = native.hotspot_residues(str(trajectory_pdb), "B")
            interface_ids = ",".join("B" + str(residue) for residue in sorted(interface))
            sampled = native.mpnn_gen_sequence(str(trajectory_pdb), "B", interface_ids, advanced)
            if len(sampled["seq"]) != 1:
                raise ValueError("ProteinMPNN did not return its one requested sequence")
            sequence = str(sampled["seq"][0])[-BINDER_LENGTH:]
            if len(sequence) != BINDER_LENGTH or set(sequence) - set("ACDEFGHIKLMNPQRSTVWY"):
                raise ValueError("ProteinMPNN returned an invalid binder sequence")
            info.update(sequence=sequence, score=finite_scalar(sampled["score"][0], "MPNN score"),
                        sequence_recovery=finite_scalar(sampled["seqid"][0], "MPNN recovery"),
                        fixed_interface_residues=interface_ids)
            (out / "binder.fasta").write_text(">diagnostic_only_not_an_accepted_binder\n" + sequence + "\n")
            del sampled
            gc.collect()
            clear_mem()
        publish(out / "mpnn-sample.json", result["stages"]["proteinmpnn"])

        with stage("af2_complex_prediction") as info:
            model = mk_afdesign_model(
                protocol="binder", data_dir=str(root), num_recycles=0, use_multimer=False,
                use_initial_guess=False, use_initial_atom_pos=False, model_names=["model_1_ptm"],
            )
            model.prep_inputs(pdb_filename=str(target), chain="A", binder_len=BINDER_LENGTH,
                              rm_target_seq=advanced["rm_template_seq_predict"],
                              rm_target_sc=advanced["rm_template_sc_predict"])
            model.predict(seq=sequence, models=[0], num_recycles=0, seed=SEED, verbose=False)
            complex_pdb = out / "complex.pdb"
            model.save_pdb(str(complex_pdb))
            info["metrics"] = metrics(model.aux["log"], ("plddt", "ptm", "i_ptm", "pae", "i_pae"))
            info["structure"] = pdb_summary(complex_pdb, {"A": 115, "B": BINDER_LENGTH})
            checks = {}
            for title, key in (("pLDDT", "plddt"), ("pTM", "ptm"), ("i_pTM", "i_ptm"), ("pAE", "pae"), ("i_pAE", "i_pae")):
                rule = filters["1_" + title]
                threshold = rule["threshold"]
                value = info["metrics"][key]
                passed = threshold is None or (value >= threshold if rule["higher"] else value <= threshold)
                checks["1_" + title] = {"value": value, "threshold": threshold, "higher": rule["higher"], "passed": passed}
            info["unchanged_native_af2_filter_checks"] = checks
            info["quality_accepted"] = False
            del model
            gc.collect()
            clear_mem()

        with stage("af2_monomer_prediction") as info:
            model = mk_afdesign_model(
                protocol="hallucination", use_templates=False, use_initial_guess=False,
                use_initial_atom_pos=False, num_recycles=0, data_dir=str(root),
                use_multimer=False, model_names=["model_1_ptm"],
            )
            model.prep_inputs(length=BINDER_LENGTH)
            model.predict(seq=sequence, models=[0], num_recycles=0, seed=SEED, verbose=False)
            monomer = out / "monomer.pdb"
            model.save_pdb(str(monomer))
            info["metrics"] = metrics(model.aux["log"], ("plddt", "ptm", "pae"))
            info["structure"] = pdb_summary(monomer, {"A": BINDER_LENGTH})
            del model
            gc.collect()
            clear_mem()

        with stage("pyrosetta_relaxation") as info:
            options = (
                "-ignore_unrecognized_res -ignore_zero_occupancy -mute all "
                "-holes:dalphaball " + advanced["dalphaball_path"] +
                " -corrections::beta_nov16 true -relax:default_repeats 1 -constant_seed -jran 17"
            )
            info["init_options"] = options
            info["native_fastrelax_max_iter"] = 200
            pr.init(options)
            relaxed = out / "complex-relaxed.pdb"
            native.pr_relax(str(complex_pdb), str(relaxed))
            info["structure"] = pdb_summary(relaxed, {"A": 115, "B": BINDER_LENGTH})
            pose = pr.pose_from_pdb(str(relaxed))
            info["relaxed_total_score"] = finite_scalar(pr.get_fa_scorefxn()(pose), "relaxed total score")

        with stage("interface_scoring_dalphaball") as info:
            previous_calls = len(helper_invocations(calls, dalphaball))
            scores, interface_aas, interface_ids = native.score_interface(str(relaxed), "B")
            info["metrics"] = {key: None if value is None else finite_scalar(value, key) for key, value in scores.items()}
            info["interface_amino_acid_counts"] = interface_aas
            info["interface_residues"] = interface_ids
            info["dalphaball_invocations"] = helper_invocations(calls, dalphaball)[previous_calls:]
            if not info["dalphaball_invocations"] or any(call["exit_code"] != 0 for call in info["dalphaball_invocations"]):
                raise ValueError("Interface scoring did not successfully execute the real DAlphaBall executable")
            info["nullable_metrics_note"] = "Native interface percentages are null when there are no contacting interface residues"
        publish(out / "interface-scores.json", result["stages"]["interface_scoring_dalphaball"])

        with stage("dssp") as info:
            values = native.calc_ss_percentage(str(complex_pdb), advanced, "B")
            info["metrics"] = {key: finite_scalar(value, key) for key, value in zip((
                "binder_helix_percent", "binder_sheet_percent", "binder_loop_percent",
                "interface_helix_percent", "interface_sheet_percent", "interface_loop_percent",
                "interface_plddt", "structured_residue_plddt",
            ), values)}
            structure = PDBParser(QUIET=True).get_structure("diagnostic", str(complex_pdb))
            assignments = DSSP(structure[0], str(complex_pdb), dssp=advanced["dssp_path"])
            counts = {chain: sum(key[0] == chain for key in assignments.keys()) for chain in ("A", "B")}
            if not all(counts.values()):
                raise ValueError("DSSP returned no assignments for one of the complex chains")
            info["assigned_residues_by_chain"] = counts
            info["native_invocations"] = helper_invocations(calls, dssp)
            if not info["native_invocations"] or any(call["exit_code"] != 0 for call in info["native_invocations"]):
                raise ValueError("DSSP did not execute its pinned native binary")
        publish(out / "secondary-structure.json", result["stages"]["dssp"])

        if any(result["stages"].get(name, {}).get("status") != "passed" for name in REQUIRED_STAGES):
            raise ValueError("Missing required component proof")
        result["status"] = "passed"
    except Exception as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        (out / "diagnostic-error.txt").write_text(traceback.format_exc())
    finally:
        result["finished_epoch"] = time.time()
        result["elapsed_seconds"] = time.monotonic() - started
        result["artifacts"] = [
            {"path": str(path.relative_to(out)), "size": path.stat().st_size, "sha256": digest(path)}
            for path in sorted(out.rglob("*")) if path.is_file() and path != result_path
            and not path.name.endswith(".tmp")
        ]
        publish(result_path, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/mnt/bio-shared/bindcraft"),
                        help="Installed runtime root; AF2 data_dir containing params/")
    parser.add_argument("--out", type=Path, required=True, help="New empty diagnostic output directory")
    args = parser.parse_args(argv)
    try:
        result = run(args.root, args.out)
    except (ValueError, OSError) as error:
        parser.exit(2, f"BindCraft diagnostic: {error}\n")
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    sys.exit(main())
