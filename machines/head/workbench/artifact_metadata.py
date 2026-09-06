"""Read native confidence and hash-bound RF3 chemistry evidence for artifacts.

This module never runs models, recalculates confidence, or substitutes a native
rank for chemistry validation. Missing or inconsistent metadata leaves the
structure available for inspection, explicitly unverified and unselected.
"""
from __future__ import annotations

import math
from pathlib import Path, PurePosixPath
import re

from .common import Error, canonical, file_sha, no_links, read_json, require, safe_file

POLICY = "rf3-named-atoms-bonds-requested-stereo-v1"
RF3_SOURCE = "b02eed6a6bdf8f44d14a80cc36e3da13c9f2291c"
MAX_JSON = 16 * 1024 * 1024
QA_SCOPE = "Named atoms, elements, formal charges, declared bonds, finite coordinates and requested stereochemistry; not general structural accuracy"
SCALARS = {
    "rf3": {"overall_plddt", "overall_pde", "overall_pae", "ptm", "iptm", "ranking_score", "has_clash"},
    "protenix": {"plddt", "gpde", "ptm", "iptm", "ranking_score", "has_clash", "disorder", "num_recycles"},
    "openfold3": {"avg_plddt", "gpde", "ptm", "iptm", "sample_ranking_score", "disorder", "has_clash"},
    "boltz2": {"confidence_score", "ptm", "iptm", "ligand_iptm", "protein_iptm", "complex_plddt", "complex_iplddt", "complex_pde", "complex_ipde"},
}


def document(path):
    require(safe_file(path).stat().st_size <= MAX_JSON, "Native evidence JSON is too large", "limit")
    value = read_json(path)
    require(isinstance(value, dict), "Native evidence must be a JSON object", "integrity")
    return value


def evidence(path, root):
    return {"path": str(path.relative_to(root)), "sha256": file_sha(path)}


def bound_file(directory, row):
    require(isinstance(row, dict), "Missing native file binding", "integrity")
    relative = row.get("path")
    require(isinstance(relative, str) and relative and not PurePosixPath(relative).is_absolute()
            and ".." not in PurePosixPath(relative).parts and "\\" not in relative,
            "Native evidence path leaves its output directory", "integrity")
    path = no_links(directory / relative)
    require(path.is_relative_to(directory) and file_sha(path) == row.get("sha256"),
            "Native evidence file hash differs", "integrity")
    return path


def confidence(path, root, model, structure):
    values = document(path)
    metrics = {key: value for key, value in values.items() if key in SCALARS.get(model, set())
               and (type(value) is bool or type(value) in (int, float) and math.isfinite(value))}
    if not metrics:
        return None
    return {"metrics": metrics, "source": evidence(path, root),
            "structure_sha256": file_sha(structure), "scale": "native; fields are not normalized across models"}


def native_sidecar(path, root, model):
    """Only exact native basename conventions can bind a confidence sidecar."""
    name = path.name
    if model == "protenix":
        match = re.fullmatch(r"(.+)_sample_([0-9]+)\.cif", name)
        if match:
            return path.with_name(f"{match[1]}_summary_confidence_sample_{match[2]}.json")
    elif model == "openfold3" and re.fullmatch(r".+_seed_[0-9]+_sample_[0-9]+_model\.cif", name):
        return path.with_name(name.removesuffix("_model.cif") + "_confidences_aggregated.json")
    elif model == "boltz2" and re.fullmatch(r".+_model_[0-9]+\.(cif|pdb)", name):
        return path.with_name("confidence_" + path.stem + ".json")
    return None


def published_resident_runtime(directory, runtime, receipt, qa, root):
    """Validate the normal frontend's wrapper and its retained CPU receipt."""
    from inference.common import digest as native_digest

    job_path = directory / "job.json"
    completion_path = directory / "resident-result.json"
    cpu_path = directory / "rf3-resident-result.json"
    job, completion, cpu = document(job_path), document(completion_path), document(cpu_path)
    request_sha = native_digest(job)
    require(runtime.get("request_sha256") == request_sha
            and runtime.get("resident_completion_sha256") == native_digest(completion)
            and completion.get("state") == "complete" and job.get("model") == "rf3"
            and completion.get("id") == job.get("id")
            and canonical(completion.get("payload")) == canonical(job),
            "RF3 published runtime does not bind its completed resident request", "integrity")
    stage = completion.get("result", {}).get("postprocess", {})
    checked = stage.get("result", {})
    require(checked.get("model") == "rf3" and checked.get("status") == "complete"
            and checked.get("job_id") == job.get("id") and checked.get("job_sha256") == request_sha
            and stage.get("job_sha256") == request_sha
            and stage.get("stage_sha256") == native_digest(job.get("postprocess"))
            and checked.get("output_validation_sha256") == file_sha(directory / "rf3-output-validation.json")
            and canonical(checked.get("output_validation")) == canonical(receipt)
            and checked.get("files", {}).get(cpu_path.name, {}).get("sha256") == file_sha(cpu_path),
            "RF3 published runtime lacks its bound successful CPU chemistry stage", "integrity")
    require(cpu.get("model") == "rf3" and cpu.get("status") == "complete"
            and cpu.get("job_id") == job.get("id") and cpu.get("job_sha256") == request_sha
            and cpu.get("output_validation_sha256") == checked["output_validation_sha256"]
            and canonical(cpu.get("output_validation")) == canonical(receipt),
            "RF3 retained CPU runtime differs from its completed publication", "integrity")
    qa["resident_completion"] = evidence(completion_path, root)
    qa["resident_runtime"] = evidence(cpu_path, root)
    qa["request_sha256"] = request_sha
    return cpu


def rf3(path, root, result):
    directory = path.parent
    while True:
        qa_path = directory / "rf3-output-validation.json"
        if qa_path.exists() or qa_path.is_symlink():
            break
        if directory == root:
            result["qa"] = {"status": "unverified", "issues": ["No retained RF3 output chemistry evidence applies to this file"]}
            return
        directory = directory.parent
    qa = {"status": "unverified", "issues": []}
    result["qa"] = qa
    try:
        receipt = document(qa_path)
        qa["source"] = evidence(qa_path, root)
        require(receipt.get("schema") == 1 and receipt.get("policy") == POLICY
                and receipt.get("source_commit") == RF3_SOURCE
                and isinstance(receipt.get("audit_source_sha256"), str)
                and re.fullmatch("[a-f0-9]{64}", receipt["audit_source_sha256"]),
                "Unsupported RF3 chemistry evidence policy", "integrity")
        qa["policy"] = POLICY
        qa["scope"] = QA_SCOPE
        bound_file(directory, {"path": receipt.get("expected_source"), "sha256": receipt.get("expected_sha256")})
        samples = receipt.get("samples")
        require(isinstance(samples, list), "RF3 chemistry evidence has no sample list", "integrity")
        selected = receipt.get("selected")
        selected_files = selected.get("files", {}) if isinstance(selected, dict) else {}
        relative = str(path.relative_to(directory))
        canonical_selected = selected_files.get("model", {}).get("path") == relative
        candidates = [sample for sample in samples if isinstance(sample, dict)
                      and sample.get("files", {}).get("model", {}).get("path") == relative]
        if canonical_selected:
            candidates = [sample for sample in samples if isinstance(sample, dict)
                          and sample.get("directory") == selected.get("raw_directory")]
        require(len(candidates) == 1, "Structure is not an unambiguous audited RF3 sample", "integrity")
        sample = candidates[0]
        files = sample.get("files", {})
        model_path = bound_file(directory, files.get("model"))
        summary_path = bound_file(directory, files.get("summary"))
        bound_file(directory, files.get("confidences"))
        if not canonical_selected:
            require(model_path == path, "RF3 sample file binding differs", "integrity")
        else:
            for kind in ("model", "summary", "confidences"):
                copied = bound_file(directory, selected_files.get(kind))
                require(selected_files[kind].get("raw_path") == files[kind]["path"]
                        and selected_files[kind]["sha256"] == files[kind]["sha256"],
                        "RF3 selected output differs from its raw sample", "integrity")
                if kind == "model":
                    require(copied == path, "RF3 selected model binding differs", "integrity")
                elif kind == "summary":
                    summary_path = copied
        result["sample_id"] = sample["directory"]
        result["confidence"] = confidence(summary_path, root, "rf3", path)
        require(document(summary_path).get("ranking_score") == sample.get("ranking_score"),
                "RF3 summary disagrees with its audited native rank", "integrity")
        chemistry = sample.get("chemistry")
        require(isinstance(chemistry, dict) and type(sample.get("passed")) is bool
                and sample["passed"] == chemistry.get("passed"), "RF3 sample chemistry result is incomplete", "integrity")
        qa["chemistry"] = {key: chemistry[key] for key in ("atoms", "expected_atoms", "finite_coordinates",
            "identity_and_connectivity_passed", "tetrahedral_checked")
            if key in chemistry and type(chemistry[key]) in (bool, int)}
        qa["failed_checks"] = {key: len(value) for key, value in chemistry.items()
            if isinstance(value, list) and key in {"missing_atoms", "extra_atoms", "changed_elements_or_charges",
                "missing_or_changed_bonds", "extra_bonds", "tetrahedral_failures"} and value}
        if chemistry.get("double_bonds"):
            qa["failed_checks"]["double_bond_stereochemistry"] = sum(row.get("passed") is not True
                for row in chemistry["double_bonds"] if isinstance(row, dict))
        # A failed sample remains explicitly failed even when no completed
        # runtime receipt exists; absence must never turn failure into success.
        if sample["passed"] is False:
            qa["status"] = "failed"
            qa["issues"] = ["Native RF3 named-atom, bond or requested-stereochemistry audit failed"]
            return
        require(chemistry.get("finite_coordinates") is True
                and chemistry.get("identity_and_connectivity_passed") is True
                and not any(qa["failed_checks"].values()),
                "RF3 passing label contradicts its chemistry checks", "integrity")
        runtimes = [directory / name for name in ("rf3-runtime.json", "rf3-resident-result.json")
                    if (directory / name).exists()]
        require(runtimes, "Missing RF3 runtime binding", "integrity")
        runtime_path = runtimes[0]
        runtime = document(runtime_path)
        require(len(runtimes) == 1 or runtime_path.name == "rf3-runtime.json" and runtime.get("execution") == "resident",
                "Ambiguous RF3 runtime binding", "integrity")
        require(runtime.get("model") == "rf3" and runtime.get("status") == "complete"
                and runtime.get("output_validation_sha256") == file_sha(qa_path)
                and canonical(runtime.get("output_validation")) == canonical(receipt),
                "RF3 runtime does not bind this completed chemistry validation", "integrity")
        audited_runtime = (published_resident_runtime(directory, runtime, receipt, qa, root)
            if runtime_path.name == "rf3-runtime.json" and runtime.get("execution") == "resident" else runtime)
        helper = audited_runtime.get("output_validation_source_sha256") or audited_runtime.get("postprocess", {}).get("sources", {}).get("library/rf3_output.py")
        require(helper == receipt.get("audit_source_sha256"), "RF3 runtime audit source differs", "integrity")
        if runtime_path.name == "rf3-runtime.json":
            outputs = runtime.get("outputs", {})
            require(outputs.get("model") == selected_files.get("model", {}).get("path")
                    and outputs.get("model_sha256") == selected_files.get("model", {}).get("sha256"),
                    "RF3 runtime selected model differs from chemistry selection", "integrity")
        qa["runtime"] = evidence(runtime_path, root)
        qa["status"] = "passed"
        if canonical_selected:
            require(receipt.get("status") == "passed", "RF3 did not select a passing output", "integrity")
            # The native QA selector chooses the highest native rank among
            # passing samples. Verify that declared choice, never reselect.
            passing = [row for row in samples if isinstance(row, dict) and row.get("passed") is True]
            require(all(type(row.get("ranking_score")) in (int, float) and math.isfinite(row["ranking_score"])
                        and isinstance(row.get("directory"), str) for row in passing),
                    "RF3 passing samples have invalid native ranks", "integrity")
            winner = sorted(passing, key=lambda row: (-row["ranking_score"], row["directory"]))[0]
            require(winner["directory"] == sample["directory"] and selected.get("ranking_score") == sample["ranking_score"],
                    "RF3 selected output disagrees with its declared passing-sample rank", "integrity")
            result["selected"] = True
    except (Error, OSError, KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        qa["status"] = "unverified"
        qa["issues"] = [str(exc).replace(str(root), "<result>")[:500]]
        result["selected"] = False


def structure_metadata(path, output_root, model):
    root, path = no_links(output_root), no_links(path)
    require(path.is_relative_to(root), "Structure lies outside its result tree", "integrity")
    result = {"sample_id": str(path.relative_to(root).with_suffix("")), "confidence": None,
              "qa": None, "selected": False}
    if model == "rf3":
        rf3(path, root, result)
    else:
        sidecar = native_sidecar(path, root, model)
        if sidecar is not None and sidecar.exists():
            try:
                result["confidence"] = confidence(sidecar, root, model, path)
            except (Error, OSError, KeyError, TypeError, ValueError):
                pass  # Retain native files; absent valid confidence is null.
    return result
