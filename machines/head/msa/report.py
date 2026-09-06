#!/usr/bin/env python3
"""Reconcile frozen prediction panels, including failures and missing results.

Each supplied backend directory contains expected-runs.json and MODEL/CASE/JOB
folders produced by the replay runner. This is a descriptive audit, not a parity
test. It never selects a best sample or changes the production backend.
"""
import argparse
import collections
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import runpy
import statistics
import sys


PATTERNS = {
    "protenix": "*/seed_*/predictions/*.cif",
    "openfold3": "*/seed_*/*_model.cif",
    "boltz2": "boltz_results_input/predictions/input/input_model_*.pdb",
}
METRICS = ("ca_rmsd_angstrom", "lddt_ca_mean")
PREPARED = runpy.run_path(str(Path(__file__).with_name("prepared.py")))
SETTINGS = runpy.run_path(str(Path(__file__).with_name("settings.py")))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return PREPARED["load_json"](path)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(path, model, settings):
    sample = re.search(r"(?:sample_|model_)(\d+)", path.name)
    require(sample is not None, f"Unrecognized prediction sample: {path}")
    index = int(sample[1]) - (1 if model == "openfold3" else 0)
    if model == "boltz2":
        require(len(settings["seeds"]) == 1, "Boltz files require one declared seed per run")
        seed = settings["seeds"][0]
    else:
        seeds = [part[5:] for part in path.parts if re.fullmatch(r"seed_\d+", part)]
        require(len(seeds) == 1, f"Ambiguous prediction seed: {path}")
        seed = int(seeds[0])
    return seed, index


def reference_files(root, run):
    case_path = root / "references" / (run["case"] + ".json")
    require(sha(case_path) == run["experimental_case_sha256"], "Frozen reference case changed")
    case = read(case_path)
    require(case["version"] == 1 and case["name"] == run["case"], "Frozen reference identity mismatch")
    fasta = PREPARED["safe_file"](case_path.parent, case["fasta_file"])
    cif = PREPARED["safe_file"](case_path.parent, case["reference_file"])
    require(sha(fasta) == case["fasta_sha256"] == run["fasta_sha256"], "Frozen FASTA changed")
    require(sha(cif) == case["reference_sha256"], "Frozen experimental CIF changed")
    sequences = PREPARED["read_fasta"](fasta)
    require(len(sequences) == 1 and sequences[0][1] == case["sequence"], "Frozen sequence mismatch")
    positions = case["observed_positions"]
    require(len(positions) >= 3 and len(set(positions)) == len(positions)
            and all(type(i) is int and 0 <= i < len(case["sequence"]) for i in positions),
            "Invalid frozen residue mapping")
    coords = case["reference_ca"]
    require(len(coords) == len(positions) and all(len(x) == 3 and all(math.isfinite(v) for v in x) for x in coords),
            "Invalid frozen reference coordinates")
    return dict(fasta=str(fasta), reference_sha256=case["reference_sha256"],
                observed_fraction=len(positions) / len(case["sequence"]), sequence_length=len(case["sequence"]))


def effective_settings(folder, model, manifest, audit_path):
    original = f"/mnt/bio-shared/runs/{folder.name}/out/prepared-native"
    aliases = {str(Path(original) / PREPARED["get_at"](manifest["native_input"], b["pointer"])[len(PREPARED["MARKER"]):]):
               "<PREPARED:" + "/".join(map(str, b["pointer"])) + ">" for b in manifest["bindings"]}
    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            return aliases.get(value, value.replace(f"/mnt/bio-shared/runs/{folder.name}/", "<JOB>/"))
        return value
    if model == "openfold3":
        paths = [folder / "model_config.json", folder / "experiment_config.json"]
        settings = {p.name: read(p) for p in paths}
    else:
        paths = [folder / "resolved-settings.json"]
        saved = read(paths[0])
        require(saved["version"] == 1 and saved["model"] == model, "Resolved settings model mismatch")
        require(saved["runtime_audit_sha256"] == sha(audit_path), "Resolved settings reference another runtime audit")
        require(saved["sources"] and saved["helper_sha256"], "Resolved settings lack source provenance")
        settings = dict(settings=saved["settings"], sources=saved["sources"], helper_sha256=saved["helper_sha256"])
    return normalize(settings), {p.name: sha(p) for p in paths}


def runtime_signature(audit, model):
    require(audit["model"] == model, "Runtime audit model mismatch")
    require(audit["packages"] and audit["python"], "Missing measured runtime environment")
    require(re.fullmatch(r"[a-f0-9]{64}", audit["checkpoint"]["sha256"]), "Missing checkpoint hash")
    gpu = list(csv.reader(audit["gpu"].splitlines(), skipinitialspace=True))
    require(len(gpu) == 1 and len(gpu[0]) == 4, "Expected one measured inference GPU")
    process = SETTINGS["observed_invocation"](audit)
    # Preserve scientific arguments, replacing only each job's private path.
    argv = [re.sub(r"/mnt/bio-shared/runs/[^/]+/", "<JOB>/", item)
            for item in process["argv"]]
    thread_keys = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS")
    return dict(python=audit["python"], packages=audit["packages"],
                checkpoint_sha256=audit["checkpoint"]["sha256"],
                gpu=dict(name=gpu[0][0], driver=gpu[0][2], memory=gpu[0][3]),
                argv=argv, threads={key: process["environment"].get(key) for key in thread_keys})


def inspect_attempt(folder, run, reference, scorer_sha256):
    job = read(folder / "job.json")
    require(job["model"] == run["model"] and job["job"] == folder.name, "Job identity mismatch")
    files = sorted(folder.glob(PATTERNS[run["model"]]))
    row = dict(job=job["job"], path=str(folder), job_sha256=sha(folder / "job.json"),
               exit_status=job.get("exit_status"), generated_samples=len(files),
               status="pending" if "exit_status" not in job else "job_failed",
               prediction_files=[dict(path=str(p), sha256=sha(p)) for p in files])
    if job.get("exit_status") != 0:
        row["unscored_partial_samples"] = len(files)
        return row
    row["status"] = "validation_failed"
    try:
        require(job["gpu"] == run["settings"]["gpu"], "Actual GPU allocation differs from frozen setting")
        require(len(files) == run["expected_structure_count"], "Missing or extra prediction samples")
        expected = {(seed, index) for seed in run["settings"]["seeds"]
                    for index in range(run["settings"]["samples_per_seed"])}
        found = [identity(p, run["model"], run["settings"]) for p in files]
        require(len(found) == len(set(found)) and set(found) == expected, "Seed/sample contract mismatch")
        require(sha(folder / "prepared-native/source_manifest.json") == run["manifest_sha256"],
                "Prepared input differs from frozen bundle")
        manifest = PREPARED["validate_materialized"](folder / "prepared-native", run["model"],
                  fasta=reference["fasta"], original_out=f"/mnt/bio-shared/runs/{folder.name}/out/prepared-native")
        scores = read(folder / "accuracy.json")
        require(scores["version"] == 1 and scores["case"] == run["case"], "Score case mismatch")
        require(scores["scorer_sha256"] == scorer_sha256, "Score uses another scorer snapshot")
        require(scores["case_sha256"] == run["experimental_case_sha256"], "Experimental mapping hash mismatch")
        require(scores["reference_sha256"] == reference["reference_sha256"], "Score reference CIF mismatch")
        require(scores["observed_fraction"] == reference["observed_fraction"], "Score reference coverage mismatch")
        samples = scores["samples"]
        require(len(samples) == len(files), "Score omits or duplicates samples")
        scored = {str(Path(sample["path"]).resolve()): sample for sample in samples}
        require(len(scored) == len(samples) and set(scored) == {str(p.resolve()) for p in files},
                "Scored paths differ from generated predictions")
        for path in files:
            sample = scored[str(path.resolve())]
            require(sample["sha256"] == sha(path), "Prediction changed after scoring")
            for key in METRICS:
                value = sample[key]
                require(isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(value) and value >= 0, f"Invalid {key}")
            require(sample["lddt_ca_mean"] <= 1, "Invalid local distance score")
        coverage = scores["observed_fraction"]
        require(type(coverage) in (int, float) and math.isfinite(coverage) and 0 < coverage <= 1,
                "Invalid reference coverage")
        audit_path = folder / "runtime-audit.json"
        runtime = runtime_signature(read(audit_path), run["model"])
        config, config_hashes = effective_settings(folder, run["model"], manifest, audit_path)
        row.update(status="complete", samples=samples, observed_fraction=coverage,
                   reference_sha256=scores["reference_sha256"], runtime=runtime,
                   preparation_source=manifest["source"]["kind"],
                   effective_settings=config, config_sha256=config_hashes,
                   score_sha256=sha(folder / "accuracy.json"),
                   runtime_sha256=sha(folder / "runtime-audit.json"),
                   means={key: statistics.mean(s[key] for s in samples) for key in METRICS})
    except (ValueError, OSError, KeyError, TypeError) as exc:
        row["error"] = str(exc)
    return row


def collect(root):
    root = Path(root).resolve()
    manifest_path = root / "expected-runs.json"
    manifest = read(manifest_path)
    require(manifest["version"] == 1 and manifest["runs"], "Missing frozen expected runs")
    require(sha(root / "quality.py") == manifest["quality_source_sha256"], "Scorer snapshot changed")
    result = {}
    jobs = set()
    for run in manifest["runs"]:
        key = run["model"], run["case"]
        require(run["model"] in PATTERNS and key not in result, "Unsupported or duplicate expected run")
        require(re.fullmatch(r"[A-Za-z0-9_]+", run["case"]), "Invalid case identifier")
        settings = run["settings"]
        require(len(set(settings["seeds"])) == len(settings["seeds"])
                and all(type(seed) is int and seed >= 0 for seed in settings["seeds"])
                and type(settings["samples_per_seed"]) is int and settings["samples_per_seed"] > 0,
                "Invalid frozen seed/sample settings")
        require(run["expected_structure_count"] == len(settings["seeds"]) * settings["samples_per_seed"],
                "Expected sample count disagrees with settings")
        folder = root / run["model"] / run["case"]
        reference = reference_files(root, run)
        attempts = []
        for path in sorted(folder.glob("*/job.json")):
            attempt = inspect_attempt(path.parent, run, reference, manifest["quality_source_sha256"])
            require(attempt["job"] not in jobs, "Duplicate job ID in panel")
            jobs.add(attempt["job"])
            attempts.append(attempt)
        # A preparation/launch failure may not create a cloud job. Keep its
        # explicit record even in a case which eventually succeeds.
        failures = [dict(path=str(p), sha256=sha(p), record=read(p))
                    for p in sorted(folder.glob("attempt-*-failure.json"))]
        inventory = read(folder / "inventory.json") if (folder / "inventory.json").is_file() else {}
        for attempt in attempts:
            records = [r for r in inventory.get("jobs", []) if r.get("job", {}).get("job") == attempt["job"]]
            attempt["cleanup"] = "unverified"
            if len(records) == 1:
                account, actual = records[0].get("accounting", {}), records[0].get("inventory", {})
                if account.get("status") == "closed" and account.get("os_purged_at") and actual:
                    gone = not any(v.get("status") not in ("deleted", "notfound") for v in actual.get("instances", []))
                    gone &= not actual.get("os_active") and not any(
                        v.get("status") != "deleted" or v.get("is_permanently_deleted") is not True
                        for v in actual.get("os_trash", []))
                    if gone:
                        attempt["cleanup"] = "confirmed_at_saved_inventory"
        result[key] = dict(expected=run, reference=reference, attempts=attempts, failure_records=failures,
                           status="missing" if not attempts else "observed")
    discovered = set(root.glob("*/*/*/job.json"))
    expected_paths = {Path(a["path"]) / "job.json" for row in result.values() for a in row["attempts"]}
    require(discovered == expected_paths, "Results include jobs outside the frozen panel")
    return dict(root=str(root), manifest_sha256=sha(manifest_path),
                scorer_sha256=manifest["quality_source_sha256"], cases=result)


def compare(public, private=None):
    keys = set(public["cases"]) | (set(private["cases"]) if private else set())
    rows, deltas = [], collections.defaultdict(list)
    for key in sorted(keys):
        row = dict(model=key[0], case=key[1], status="incomplete", backends={})
        complete = {}
        for backend, panel in (("public", public), ("private", private)):
            value = panel["cases"].get(key) if panel else None
            row["backends"][backend] = value
            if value:
                complete[backend] = [a for a in value["attempts"] if a["status"] == "complete"]
        if all(complete.get(backend) for backend in ("public", "private")):
            left, right = row["backends"]["public"], row["backends"]["private"]
            mismatches = [field for field in ("settings", "fasta_sha256", "experimental_case_sha256", "expected_structure_count")
                          if left["expected"][field] != right["expected"][field]]
            if public["scorer_sha256"] != private["scorer_sha256"]:
                mismatches.append("scorer_sha256")
            if any(a["preparation_source"] != backend for backend, attempts in complete.items() for a in attempts):
                mismatches.append("preparation_backend")
            all_runs = complete["public"] + complete["private"]
            for field in ("runtime", "effective_settings", "reference_sha256", "observed_fraction"):
                if any(a[field] != all_runs[0][field] for a in all_runs[1:]):
                    mismatches.append(field)
            # Every complete retry participates; none is chosen by accuracy.
            means = {backend: {metric: statistics.mean(a["means"][metric] for a in attempts)
                               for metric in METRICS} for backend, attempts in complete.items()}
            row.update(status="incompatible" if mismatches else "paired", mismatches=mismatches,
                       means=means, complete_attempt_counts={b: len(a) for b, a in complete.items()})
            if not mismatches:
                row["private_minus_public"] = {m: means["private"][m] - means["public"][m] for m in METRICS}
                deltas[key[0]].append(row["private_minus_public"])
        rows.append(row)
    summary = {}
    for model in sorted({key[0] for key in keys}):
        cases = [r for r in rows if r["model"] == model]
        summary[model] = dict(expected_cases=len(cases), paired_cases=len(deltas[model]),
                             statuses=dict(collections.Counter(r["status"] for r in cases)),
                             mean_private_minus_public={m: statistics.mean(d[m] for d in deltas[model])
                                                        for m in METRICS} if deltas[model] else None)
        summary[model]["backends"] = {}
        for backend in ("public", "private"):
            values = [c["backends"][backend] for c in cases if c["backends"][backend]]
            attempts = [a for value in values for a in value["attempts"]]
            summary[model]["backends"][backend] = dict(
                declared_cases=len(values),
                complete_cases=sum(any(a["status"] == "complete" for a in value["attempts"]) for value in values),
                observed_attempts=len(attempts), attempt_statuses=dict(collections.Counter(a["status"] for a in attempts)),
                unscored_partial_samples=sum(a.get("unscored_partial_samples", 0) for a in attempts))
    return dict(version=1, scientific_parity="not_established", cases=rows, summary=summary,
                attempt_coverage="Observed local job/failure records only; no immutable prelaunch attempt ledger. Entirely missing or deleted attempts cannot be detected.",
                inputs={b: {k: v for k, v in p.items() if k != "cases"}
                        for b, p in (("public", public), ("private", private)) if p},
                interpretation="Descriptive complete-pair subset; equal case weights after averaging every complete attempt's samples. Missing and failed attempts remain visible and can bias this subset. Lower RMSD and higher lDDT-CA are better. No automatic parity approval.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", type=Path, required=True)
    parser.add_argument("--private", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = compare(collect(args.public), collect(args.private) if args.private else None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"report: {exc}", file=sys.stderr)
        sys.exit(1)
