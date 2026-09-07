"""Equilibrium alchemical analysis with explicit sampling and model boundaries.

Input ``bio-md-alchemical.v1`` contains ``protocol``, ordered ``states``,
``energy_units`` (kT or kJ/mol), and one ``windows`` item per state. A window's
``u_kn[k][n]`` evaluates configuration n from ``state_index`` at every state k.
Windows must be independent simulations at a common temperature. This module
does not silently treat exchanged walkers or different force fields as replicas.

Scientific basis (primary documentation and authors' best-practice review):
https://pymbar.readthedocs.io/en/stable/mbar.html
https://pymbar.readthedocs.io/en/stable/timeseries.html
https://pymbar.readthedocs.io/en/stable/other_estimators.html
https://alchemlyb.readthedocs.io/en/stable/preprocessing/alchemlyb.preprocessing.subsampling.html
https://pmc.ncbi.nlm.nih.gov/articles/PMC8388617/
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import sys
import warnings

R_KJ_MOL_K = 0.00831446261815324
LIMITATIONS = [
    "Sampling uncertainties are conditional on the chosen Hamiltonian and sampled configurations; unsampled basins and force-field error are not included.",
    "Automatic equilibration and decorrelation are diagnostics of the supplied observables, not proof of equilibrium or convergence.",
    "Overlap and effective sample sizes are diagnostics, not evidence of agreement with experiment.",
    "Different force fields are distinct models; agreement between them does not establish accuracy.",
]


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _number(value, field, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{field} must be finite" + (" and positive" if positive else ""))
    return float(value)


def _protocol(value):
    if not isinstance(value, dict):
        raise ValueError("protocol must be an object")
    result = copy.deepcopy(value)
    for field in ("model_id", "comparison_id", "force_field", "water_model", "replicate_id"):
        _text(result.get(field), f"protocol.{field}")
    result["temperature_kelvin"] = _number(result.get("temperature_kelvin"), "temperature_kelvin", positive=True)
    transform = result.get("transformation")
    if not isinstance(transform, dict):
        raise ValueError("protocol.transformation must identify the same directed chemical change in both legs")
    _text(transform.get("from"), "transformation.from")
    _text(transform.get("to"), "transformation.to")
    if transform["from"] == transform["to"]:
        raise ValueError("Transformation endpoints must differ")
    if result.get("leg") not in {"bound", "unbound"}:
        raise ValueError("protocol.leg must be bound or unbound")
    if result.get("sampling_method") != "independent_windows":
        raise ValueError("Only independent_windows are supported; replica-exchange correlations require a different analysis")
    for field in ("independent_replica", "legs_independent"):
        if type(result.get(field, False)) is not bool:
            raise ValueError(f"protocol.{field} must be a boolean")
    if result.get("independent_replica"):
        _text(result.get("independence_id"), "protocol.independence_id")
    if result.get("ensemble") not in {"NVT", "NPT"}:
        raise ValueError("protocol.ensemble must be NVT or NPT")
    if result["ensemble"] == "NPT":
        _number(result.get("pressure_bar"), "protocol.pressure_bar", positive=True)
    if not isinstance(result.get("provenance"), dict) or not result["provenance"]:
        raise ValueError("protocol.provenance must record the simulation sources")
    if abs(_number(result.get("charge_change_e", 0), "protocol.charge_change_e")) > 1e-8:
        raise ValueError("Charge-changing cycles require an explicit finite-size correction protocol; unsupported here")
    return result


def _model_key(protocol):
    # A model label alone cannot make physically different Hamiltonians replicas.
    fields = ("model_id", "force_field", "water_model", "temperature_kelvin", "ensemble", "pressure_bar",
              "ionic_strength_molar", "protonation", "parameter_manifest_sha256", "hamiltonian", "charge_change_e")
    return _digest({field: protocol.get(field) for field in fields})


def _dependencies():
    try:
        import numpy as np
        from pymbar import MBAR, other_estimators, timeseries
    except ImportError as exc:
        raise RuntimeError("Analysis requires NumPy and pyMBAR 4; use the MD runtime analysis environment") from exc
    return np, MBAR, other_estimators, timeseries


def _decorrelate(window, k, count, scale, options):
    np, _, _, timeseries = _dependencies()
    try:
        values = np.asarray(window["u_kn"], dtype=float) * scale
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"Window {k} must contain a numerical u_kn matrix") from exc
    if values.ndim != 2 or values.shape[0] != count or values.shape[1] < 4 or not np.all(np.isfinite(values)):
        raise ValueError(f"Window {k} requires a complete finite {count} by N energy matrix with N>=4")
    size = values.shape[1]
    times = window.get("sample_times_ps")
    if times is not None:
        times = np.asarray(times, dtype=float)
        if times.shape != (size,) or not np.all(np.isfinite(times)) or np.any(np.diff(times) <= 0):
            raise ValueError(f"Window {k} times must be finite and strictly increasing; overlapping restarts must be resolved explicitly")
        increments = np.diff(times)
        if not np.allclose(increments, increments[0], rtol=1e-5, atol=1e-8):
            raise ValueError("Autocorrelation analysis requires uniformly spaced samples")
    burnin = window.get("equilibration_samples", 0)
    if type(burnin) is not int or not 0 <= burnin <= size - 4:
        raise ValueError(f"Window {k} equilibration_samples leaves fewer than four samples")
    if options["equilibration"] == "fixed" and "equilibration_samples" not in window:
        raise ValueError("Fixed equilibration requires an explicit equilibration_samples for every window")
    series = {"sampled_state_potential": values[k]}
    for neighbor in (k-1, k+1):
        if 0 <= neighbor < count:
            series[f"delta_u_to_{neighbor}"] = values[neighbor] - values[k]
    extra = window.get("observables", {})
    if not isinstance(extra, dict) or len(extra) > 16:
        raise ValueError("At most 16 extra decorrelation observables are supported")
    for name, data in extra.items():
        _text(name, "observable name")
        array = np.asarray(data, dtype=float)
        if array.shape != (size,) or not np.all(np.isfinite(array)):
            raise ValueError(f"Observable {name} must have one finite value per sample")
        series[f"observable:{name}"] = array
    diagnostics, constants = {}, []
    start = burnin
    for name, array in series.items():
        production = array[burnin:]
        if np.ptp(production) <= 1e-12 * max(1.0, np.max(np.abs(production))):
            constants.append(name)
            continue
        if options["equilibration"] == "auto":
            t0, g, effective = timeseries.detect_equilibration(production, nskip=max(1, len(production)//200))
            start = max(start, burnin + int(t0))
            diagnostics[name] = {"detected_start": burnin+int(t0), "detected_g": float(g), "detected_ess": float(effective)}
    if size-start < 4:
        raise ValueError(f"Window {k} has fewer than four post-equilibration samples")
    inefficiencies = []
    for name, array in series.items():
        production = array[start:]
        if np.ptp(production) <= 1e-12 * max(1.0, np.max(np.abs(production))):
            continue
        g = float(timeseries.statistical_inefficiency(production))
        if not math.isfinite(g) or g < 1:
            raise ValueError(f"Window {k} has invalid statistical inefficiency")
        diagnostics.setdefault(name, {})["common_region_g"] = g
        inefficiencies.append(g)
    g = max(inefficiencies, default=1.0)
    indices = np.asarray(timeseries.subsample_correlated_data(values[k, start:], g=g, conservative=True), dtype=int) + start
    if len(indices) < 2:
        raise ValueError(f"Window {k} has fewer than two decorrelated samples")
    receipt = {"state_index": k, "original_samples": size, "equilibration_samples": start,
               "statistical_inefficiency": g, "timeseries_ess": (size-start)/g,
               "retained_samples": len(indices), "retained_indices": indices.tolist(),
               "constant_diagnostics": constants, "diagnostics": diagnostics,
               "all_diagnostics_constant": not inefficiencies}
    if times is not None:
        receipt.update(equilibration_time_ps=float(times[start]), retained_time_range_ps=[float(times[indices[0]]), float(times[indices[-1]])])
    return values[:, indices], receipt


def _mbar(arrays):
    np, MBAR, _, _ = _dependencies()
    counts = np.asarray([array.shape[1] for array in arrays], dtype=int)
    energies = np.concatenate(arrays, axis=1)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        estimator = MBAR(energies, counts, solver_protocol="robust", relative_tolerance=1e-10)
        result = estimator.compute_free_energy_differences()
        overlap = estimator.compute_overlap()
        ess = estimator.compute_effective_sample_number()
    return result, overlap, ess, sorted({str(item.message) for item in caught})


def _bar(forward, reverse, *, compute_uncertainty=True):
    np, _, estimators, _ = _dependencies()
    from pymbar.utils import BoundsError, ConvergenceError, ParameterError
    if not np.all(np.isfinite(forward)) or not np.all(np.isfinite(reverse)):
        raise ValueError("BAR work differences must be finite")
    try:
        # Bisection also handles the exactly constant-offset, zero-width bracket
        # for which pyMBAR 4.0.3's false-position implementation can divide by 0.
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            return estimators.bar(forward, reverse, uncertainty_method="MBAR", method="bisection",
                                  compute_uncertainty=compute_uncertainty)
    except (BoundsError, ConvergenceError, ParameterError, RuntimeWarning) as exc:
        raise ValueError(f"BAR failed: {type(exc).__name__}") from exc


def _connected(matrix, threshold=1e-8):
    reached, pending = {0}, [0]
    while pending:
        i = pending.pop()
        for j in range(len(matrix)):
            if j not in reached and min(matrix[i][j], matrix[j][i]) > threshold:
                reached.add(j)
                pending.append(j)
    return len(reached) == len(matrix)


def analyze(document):
    """Analyze one independent replica. Energies and sampling remain auditable."""
    np, _, _, _ = _dependencies()
    if not isinstance(document, dict) or document.get("schema") != "bio-md-alchemical.v1":
        raise ValueError("Expected schema bio-md-alchemical.v1")
    protocol = _protocol(document.get("protocol"))
    states = document.get("states")
    if not isinstance(states, list) or not 2 <= len(states) <= 256 or len({_digest(s) for s in states}) != len(states):
        raise ValueError("Two to 256 unique ordered states are required")
    if document.get("energy_units") not in {"kT", "kJ/mol"}:
        raise ValueError("energy_units must explicitly be kT or kJ/mol")
    rt = R_KJ_MOL_K * protocol["temperature_kelvin"]
    options = {"equilibration": "auto", "bar_bootstraps": 100, "bootstrap_seed": 0, **document.get("analysis_options", {})}
    if options["equilibration"] not in {"auto", "fixed"}:
        raise ValueError("equilibration must be auto or fixed")
    if type(options["bar_bootstraps"]) is not int or not 0 <= options["bar_bootstraps"] <= 2000:
        raise ValueError("bar_bootstraps must be between zero and 2000")
    if options["bar_bootstraps"] and options["bar_bootstraps"] < 50:
        raise ValueError("At least 50 joint bootstrap resamples are required when enabled")
    if type(options["bootstrap_seed"]) is not int or options["bootstrap_seed"] < 0:
        raise ValueError("bootstrap_seed must be a nonnegative integer")
    windows = document.get("windows")
    if not isinstance(windows, list) or len(windows) != len(states):
        raise ValueError("One independent window is required for every evaluated state")
    indexed = {}
    for window in windows:
        if not isinstance(window, dict) or type(window.get("state_index")) is not int or not 0 <= window["state_index"] < len(states):
            raise ValueError("Every window requires a valid state_index")
        if window["state_index"] in indexed:
            raise ValueError("Duplicate window state; replicas must be analyzed independently")
        for field in ("model_id", "force_field", "water_model", "temperature_kelvin"):
            if field in window and window[field] != protocol[field]:
                raise ValueError("Cannot mix force-field models or temperatures in an analysis")
        indexed[window["state_index"]] = window
    arrays, receipts = [], []
    for k in range(len(states)):
        array, receipt = _decorrelate(indexed[k], k, len(states), 1 if document["energy_units"] == "kT" else 1/rt, options)
        arrays.append(array)
        receipts.append(receipt)
    result, overlap, ess, numerical_warnings = _mbar(arrays)
    delta = float(result["Delta_f"][0, -1])
    error = float(result["dDelta_f"][0, -1])
    matrix = np.asarray(overlap["matrix"]).tolist()
    connected = _connected(matrix)
    reasons = []
    if not connected:
        reasons.append("overlap_graph_disconnected")
    if any(min(matrix[i][i+1], matrix[i+1][i]) < 0.03 for i in range(len(states)-1)):
        reasons.append("weak_adjacent_overlap")
    if any(receipt["retained_samples"] < 50 for receipt in receipts) or np.min(ess) < 50:
        reasons.append("low_effective_sample_count")
    if any(receipt["all_diagnostics_constant"] for receipt in receipts):
        reasons.append("constant_observables_cannot_demonstrate_sampling")
    if not math.isfinite(delta) or not math.isfinite(error) or error < 0:
        reasons.append("nonfinite_estimate_or_uncertainty")
    supported = connected and math.isfinite(delta) and math.isfinite(error) and error >= 0
    edges = []
    for i in range(len(states)-1):
        forward = arrays[i][i+1] - arrays[i][i]
        reverse = arrays[i+1][i] - arrays[i+1][i+1]
        try:
            if min(matrix[i][i+1], matrix[i+1][i]) <= 1e-8:
                raise ValueError("Adjacent states have insufficient overlap")
            edge = _bar(forward, reverse)
            value, sigma = float(edge["Delta_f"]), float(edge["dDelta_f"])
            if not math.isfinite(value) or not math.isfinite(sigma) or sigma < 0:
                raise ValueError("Nonfinite BAR estimate")
            edges.append({"from_index": i, "to_index": i+1, "delta_f": value, "sampling_se_kT": sigma,
                          "delta_g_kj_mol": value*rt, "sampling_se_kj_mol": sigma*rt})
        except (ValueError, RuntimeError, FloatingPointError) as exc:
            edges.append({"from_index": i, "to_index": i+1, "error": type(exc).__name__, "message": str(exc)})
    if any("error" in edge for edge in edges):
        reasons.append("bar_edge_estimate_or_uncertainty_unavailable")
    chain = {"delta_f": None, "sampling_se_kT": None, "uncertainty_method": None}
    if all("delta_f" in edge for edge in edges):
        chain["delta_f"] = sum(edge["delta_f"] for edge in edges)
        if len(edges) == 1:
            chain.update(sampling_se_kT=edges[0]["sampling_se_kT"], uncertainty_method="two-state BAR asymptotic")
        elif options["bar_bootstraps"]:
            # Resample whole configurations within each window, preserving the
            # covariance of adjacent edges that share an intermediate window.
            rng = np.random.default_rng(options["bootstrap_seed"])
            boot = []
            for _ in range(options["bar_bootstraps"]):
                sample = [array[:, rng.integers(array.shape[1], size=array.shape[1])] for array in arrays]
                try:
                    value = sum(float(_bar(sample[i][i+1]-sample[i][i], sample[i+1][i]-sample[i+1][i+1], compute_uncertainty=False)["Delta_f"])
                                for i in range(len(states)-1))
                    if math.isfinite(value):
                        boot.append(value)
                except (ValueError, RuntimeError, FloatingPointError):
                    pass
            chain["bootstrap_successes"] = len(boot)
            chain["bootstrap_requested"] = options["bar_bootstraps"]
            if len(boot) == options["bar_bootstraps"]:
                chain.update(sampling_se_kT=float(np.std(boot, ddof=1)),
                             percentile_interval_95_kT=np.quantile(boot, [0.025, 0.975]).tolist(),
                             uncertainty_method="joint within-window bootstrap after decorrelation")
            else:
                reasons.append("bar_bootstrap_failures_uncertainty_unavailable")
        else:
            chain["uncertainty_note"] = "Adjacent BAR errors share intermediate-window samples and must not be added in quadrature; enable joint bootstrapping."
    stability = {}
    if min(array.shape[1] for array in arrays) >= 8 and connected:
        for label, selection in (("first_half", lambda a: a[:, :a.shape[1]//2]), ("last_half", lambda a: a[:, a.shape[1]//2:])):
            try:
                half, _, _, _ = _mbar([selection(array) for array in arrays])
                value, sigma = float(half["Delta_f"][0, -1]), float(half["dDelta_f"][0, -1])
                if math.isfinite(value) and math.isfinite(sigma):
                    stability[label] = {"delta_f": value, "sampling_se_kT": sigma}
            except (ValueError, RuntimeError, FloatingPointError):
                stability[label] = {"unavailable": True}
        if all("delta_f" in stability.get(part, {}) for part in ("first_half", "last_half")):
            difference = stability["last_half"]["delta_f"] - stability["first_half"]["delta_f"]
            sigma = math.hypot(stability["first_half"]["sampling_se_kT"], stability["last_half"]["sampling_se_kT"])
            stability["difference_kT"] = difference
            if abs(difference) > max(2*sigma, 0.5):
                reasons.append("first_last_half_disagreement")
    mbar = {"delta_f": delta if supported else None, "sampling_se_kT": error if supported else None,
            "delta_g_kj_mol": delta*rt if supported else None, "sampling_se_kj_mol": error*rt if supported else None,
            "delta_g_kcal_mol": delta*rt/4.184 if supported else None,
            "uncertainty_method": "MBAR asymptotic covariance after decorrelation",
            "overlap_matrix": matrix, "overlap_scalar": float(overlap["scalar"]), "overlap_connected": connected,
            "weight_ess": np.asarray(ess).tolist(), "total_retained_samples": sum(a.shape[1] for a in arrays)}
    return {"schema": "bio-md-analysis.v1", "quantity": "alchemical_leg", "protocol": protocol,
            "model_fingerprint": _model_key(protocol), "states": copy.deepcopy(states),
            "input_sha256": _digest(document),
            "sampling_data_sha256": _digest({"states": states, "units": document["energy_units"], "windows": [indexed[k] for k in range(len(states))]}),
            "software": {name: version(name) for name in ("numpy", "pymbar")},
            "mbar": mbar, "bar": {"edges": edges, "chain": chain}, "decorrelation": receipts,
            "time_stability": stability, "analysis_options": options,
            "evidence": {"status": "insufficient_overlap" if not supported else "sampling_limited" if reasons else "diagnostics_available",
                         "flags": reasons, "thresholds": {"adjacent_overlap_warning": 0.03, "ess_warning": 50, "connectivity": 1e-8},
                         "threshold_note": "Heuristic diagnostic thresholds; passing them is not proof of convergence."},
            "numerical_warnings": numerical_warnings, "limitations": list(LIMITATIONS)}


def binding_ddg(bound, unbound, *, covariance_kj2_mol2=None):
    """Directed relative binding free energy: ΔΔG(A→B)=ΔG_bound−ΔG_unbound.

    Negative values favor binding of B relative to A. Independent-leg errors
    combine in quadrature only when declared; correlated legs need covariance.
    """
    if any(not isinstance(report, dict) or report.get("quantity") != "alchemical_leg" for report in (bound, unbound)):
        raise ValueError("Two alchemical-leg analysis reports are required")
    bp, up = _protocol(bound["protocol"]), _protocol(unbound["protocol"])
    if bp["leg"] != "bound" or up["leg"] != "unbound":
        raise ValueError("Pass the bound leg first and unbound leg second")
    for field in ("comparison_id", "transformation", "replicate_id"):
        if bp[field] != up[field]:
            raise ValueError(f"Bound/unbound {field} mismatch")
    if _model_key(bp) != _model_key(up):
        raise ValueError("Bound/unbound Hamiltonian or thermodynamic conditions differ")
    numbers = []
    for report in (bound, unbound):
        numbers.append((_number(report["mbar"].get("delta_g_kj_mol"), "leg free energy"),
                        _number(report["mbar"].get("sampling_se_kj_mol"), "leg uncertainty")))
    (b, sb), (u, su) = numbers
    if sb < 0 or su < 0:
        raise ValueError("Sampling uncertainties cannot be negative")
    note, sigma = "Independent-leg quadrature", None
    if covariance_kj2_mol2 is not None:
        covariance = _number(covariance_kj2_mol2, "leg covariance")
        if abs(covariance) > sb*su + 1e-10:
            raise ValueError("Leg covariance exceeds the Cauchy-Schwarz bound")
        sigma = math.sqrt(max(0.0, sb*sb+su*su-2*covariance))
        note = "Supplied paired-leg covariance; its estimation provenance must be retained by the caller"
    elif bp.get("legs_independent") and up.get("legs_independent"):
        sigma = math.hypot(sb, su)
    else:
        note = "Unavailable: leg independence is not declared and no covariance was supplied"
    protocol = copy.deepcopy(bp)
    protocol["leg"] = "binding_ddg"
    protocol["independent_replica"] = bool(bp.get("independent_replica") and up.get("independent_replica"))
    if bp.get("independence_id") != up.get("independence_id"):
        protocol["independence_id"] = _digest([bp.get("independence_id"), up.get("independence_id")])
    flags = sorted(set(bound.get("evidence", {}).get("flags", []) + unbound.get("evidence", {}).get("flags", [])))
    if sigma is None:
        flags.append("paired_leg_uncertainty_unavailable")
    return {"schema": "bio-md-binding-ddg.v1", "quantity": "binding_ddg", "protocol": protocol,
            "model_fingerprint": _model_key(bp), "delta_g_kj_mol": b-u, "delta_g_kcal_mol": (b-u)/4.184,
            "sampling_se_kj_mol": sigma, "uncertainty_method": note,
            "sign_convention": "ΔΔG_bind(A→B) = ΔG_bound(A→B) − ΔG_unbound(A→B); negative favors B binding relative to A",
            "source_reports_sha256": [_digest(bound), _digest(unbound)],
            "source_data_sha256": [bound["sampling_data_sha256"], unbound["sampling_data_sha256"]],
            "evidence": {"status": "sampling_limited" if flags else "diagnostics_available", "flags": flags},
            "limitations": list(LIMITATIONS)}


def summarize_models(reports):
    """Keep each model separate; independent replica scatter is not FF error."""
    if not isinstance(reports, list) or not reports:
        raise ValueError("At least one binding ΔΔG report is required")
    groups, seen = {}, set()
    reference = None
    for report in reports:
        if report.get("quantity") != "binding_ddg":
            raise ValueError("Comparison requires binding_ddg reports, not individual leg free energies")
        identity = _digest(report)
        if identity in seen:
            raise ValueError("Duplicate report cannot be counted as another replica")
        seen.add(identity)
        protocol = report["protocol"]
        target = [protocol["comparison_id"], protocol["transformation"], protocol["temperature_kelvin"]]
        if reference is not None and target != reference:
            raise ValueError("Cannot compare different targets, directed transformations, or temperatures")
        reference = target
        fingerprint = _model_key(protocol)
        if report.get("model_fingerprint") != fingerprint:
            raise ValueError("Report model fingerprint does not match its protocol")
        groups.setdefault(fingerprint, []).append(report)
    models = []
    for fingerprint, entries in sorted(groups.items()):
        values = [_number(entry["delta_g_kj_mol"], "replica estimate") for entry in entries]
        protocols = [entry["protocol"] for entry in entries]
        replica_ids = [p["replicate_id"] for p in protocols]
        independence_ids = [p.get("independence_id") for p in protocols]
        sources = [tuple(entry.get("source_data_sha256", [])) for entry in entries]
        independent = (all(p.get("independent_replica") is True for p in protocols)
                       and all(isinstance(i, str) and i for i in independence_ids)
                       and len(set(independence_ids)) == len(entries) and len(set(replica_ids)) == len(entries)
                       and all(sources) and len(set(sources)) == len(entries)
                       and len({source for pair in sources for source in pair}) == sum(len(pair) for pair in sources))
        mean = sum(values)/len(values)
        result = {"model_fingerprint": fingerprint, "model_id": protocols[0]["model_id"],
                  "replicas": [{"replicate_id": p["replicate_id"], "estimate_kj_mol": value,
                                "sampling_se_kj_mol": entry.get("sampling_se_kj_mol"), "evidence": entry.get("evidence")}
                               for entry, p, value in zip(entries, protocols, values)],
                  "independence_declared_and_nonduplicate": independent,
                  "descriptive_mean_kj_mol": mean, "within_model_sampling_se_kj_mol": None,
                  "replica_scatter_sd_kj_mol": None, "replica_scatter_se_kj_mol": None}
        errors = [entry.get("sampling_se_kj_mol") for entry in entries]
        if independent and all(isinstance(error, (int, float)) and not isinstance(error, bool) and math.isfinite(error) and error >= 0 for error in errors):
            result["within_model_sampling_se_kj_mol"] = math.sqrt(sum(error*error for error in errors))/len(errors)
        if independent and len(values) > 1:
            scatter = math.sqrt(sum((value-mean)**2 for value in values)/(len(values)-1))
            result.update(replica_scatter_sd_kj_mol=scatter, replica_scatter_se_kj_mol=scatter/math.sqrt(len(values)))
        result["uncertainty_note"] = ("Unweighted independent-replica mean; estimator SE and observed replica scatter are reported separately. Independence is declared, not proven."
                                      if independent else "Descriptive mean only: independence is absent, repeated, or shares source data. No combined sampling precision is claimed.")
        models.append(result)
    means = [model["descriptive_mean_kj_mol"] for model in models]
    return {"schema": "bio-md-model-comparison.v1", "quantity": "binding_ddg", "models": models,
            "between_model_disagreement": {"range_kj_mol": max(means)-min(means) if len(means)>1 else None,
                                           "sample_sd_kj_mol": math.sqrt(sum((x-sum(means)/len(means))**2 for x in means)/(len(means)-1)) if len(means)>1 else None,
                                           "note": "Descriptive spread across distinct models, not a standard error or an accuracy estimate; force fields are not pooled as replicas."},
            "pooled_estimate": None, "limitations": list(LIMITATIONS)}


def _numeric_state_literal(text):
    # alchemlyb 2.5.0 uses eval on XVG state headers. Admit only numeric literals
    # before invoking the native parser; scientific labels cannot execute code.
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError) as exc:
        raise ValueError("GROMACS lambda headers must contain numerical literals only") from exc
    values = value if isinstance(value, tuple) else (value,)
    if not 1 <= len(values) <= 16 or any(isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x) for x in values):
        raise ValueError("Invalid GROMACS lambda state vector")
    return tuple(float(x) for x in values)


def analyze_gromacs(paths, protocol, temperature_kelvin=None, *, analysis_options=None):
    """Parse unfiltered native dhdl.xvg files; never invent missing energies."""
    _protocol(protocol)
    temperature = protocol["temperature_kelvin"] if temperature_kelvin is None else temperature_kelvin
    if temperature != protocol["temperature_kelvin"]:
        raise ValueError("Parser temperature and protocol temperature must match")
    from alchemlyb.parsing.gmx import extract_u_nk
    records, sources, columns = [], [], None
    for path in paths:
        path = Path(path)
        data = path.read_bytes()
        if not data or len(data) > 512*1024*1024:
            raise ValueError("XVG source is empty or exceeds 512 MiB")
        content = data.decode("utf-8")
        subtitle_seen = False
        for line in content.splitlines():
            if line.lstrip().startswith("@") and "subtitle" in line and "state" in line:
                if " = " not in line:
                    raise ValueError("Missing fixed-window state vector")
                _numeric_state_literal(line.strip().split(" = ")[-1].strip('"'))
                subtitle_seen = True
            if line.lstrip().startswith("@") and "legend" in line and r"\xD\f{}H \xl\f{}" in line:
                if " to " not in line:
                    raise ValueError("Invalid GROMACS energy-difference legend")
                _numeric_state_literal(line.strip().split(" to ")[-1].strip('"'))
        if not subtitle_seen:
            raise ValueError("A fixed independent-window GROMACS state subtitle is required")
        # Work on the exact bytes validated above, preventing parser rereads of
        # a changing source from bypassing numeric-header validation.
        import tempfile
        with tempfile.TemporaryDirectory(prefix="md-xvg-") as temporary:
            snapshot = Path(temporary)/"dhdl.xvg"
            snapshot.write_bytes(data)
            frame = extract_u_nk(str(snapshot), T=temperature, filter=False)
        labels = [tuple(x) if isinstance(x, tuple) else (float(x),) for x in frame.columns]
        if columns is None:
            columns = labels
        elif labels != columns:
            raise ValueError("All windows must evaluate the same complete, ordered lambda states (calc-lambda-neighbors=-1)")
        sampled = {tuple(index[1:]) for index in frame.index}
        if len(sampled) != 1 or next(iter(sampled)) not in labels:
            raise ValueError("XVG must represent one sampled state present in its energy columns")
        records.append({"state_index": labels.index(next(iter(sampled))), "u_kn": frame.to_numpy(dtype=float).T.tolist(),
                        "sample_times_ps": [float(index[0]) for index in frame.index]})
        sources.append({"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    if not records:
        raise ValueError("At least two native GROMACS window files are required")
    metadata = copy.deepcopy(protocol)
    metadata["provenance"]["gromacs_dhdl_sources"] = sources
    document = {"schema": "bio-md-alchemical.v1", "protocol": metadata, "states": [list(x) for x in columns],
                "energy_units": "kT", "windows": records, "analysis_options": analysis_options or {}}
    result = analyze(document)
    result["software"]["alchemlyb"] = version("alchemlyb")
    result["native_adapter"] = {"format": "GROMACS dhdl.xvg", "filter_bad_rows": False, "source_files": sources,
                                "reduced_energy_note": "alchemlyb converts native kJ/mol at the declared temperature; common per-configuration energy terms cancel between states."}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("analyze", "compare"):
        child = sub.add_parser(command)
        child.add_argument("input")
        child.add_argument("--output")
    child = sub.add_parser("ddg")
    child.add_argument("bound")
    child.add_argument("unbound")
    child.add_argument("--covariance-kj2-mol2", type=float)
    child.add_argument("--output")
    child = sub.add_parser("gromacs")
    child.add_argument("--protocol", required=True)
    child.add_argument("--output")
    child.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    read = lambda path: json.loads(Path(path).read_text())
    try:
        if args.command == "analyze":
            result = analyze(read(args.input))
        elif args.command == "compare":
            result = summarize_models(read(args.input))
        elif args.command == "ddg":
            result = binding_ddg(read(args.bound), read(args.unbound), covariance_kj2_mol2=args.covariance_kj2_mol2)
        else:
            result = analyze_gromacs(args.paths, read(args.protocol))
        output = json.dumps(result, indent=2, allow_nan=False)+"\n"
        if args.output:
            Path(args.output).write_text(output)
        else:
            sys.stdout.write(output)
    except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
