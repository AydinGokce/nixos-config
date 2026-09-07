"""Known analytical free energies and native XVG fixtures; no cloud calls."""

import copy
import hashlib
import json
import math

import numpy as np
import pytest

from md import analysis


def protocol(**changes):
    return {"model_id": "amber-test", "comparison_id": "target-A-to-B", "force_field": "amber-test",
            "water_model": "tip3p", "temperature_kelvin": 300.0, "ensemble": "NPT", "pressure_bar": 1.0,
            "transformation": {"from": "A", "to": "B", "chain": "X", "resid": 7}, "leg": "bound",
            "replicate_id": "r0", "independence_id": "independent-r0", "independent_replica": True,
            "legs_independent": True, "sampling_method": "independent_windows", "charge_change_e": 0,
            "provenance": {"purpose": "analytical harmonic-oscillator test"}, **changes}


def harmonic(*, count=3, samples=1400, seed=842, centers=None, options=None):
    rng = np.random.default_rng(seed)
    stiffness = np.linspace(1, 2, count)
    centers = np.linspace(0, 0.4, count) if centers is None else np.asarray(centers)
    offsets = np.linspace(0, 0.7, count)
    windows = []
    for i in range(count):
        x = rng.normal(centers[i], 1/math.sqrt(stiffness[i]), samples)
        values = 0.5*stiffness[:, None]*(x[None, :]-centers[:, None])**2+offsets[:, None]
        windows.append({"state_index": i, "u_kn": values.tolist(), "sample_times_ps": (np.arange(samples)*0.1).tolist(),
                        "equilibration_samples": 0})
    return {"schema": "bio-md-alchemical.v1", "protocol": protocol(), "states": list(np.linspace(0, 1, count)),
            "energy_units": "kT", "windows": windows,
            "analysis_options": {"equilibration": "fixed", "bar_bootstraps": 50, **(options or {})}}


def test_mbar_bar_and_joint_uncertainty_match_analytical_partition_functions():
    report = analysis.analyze(harmonic())
    expected = 0.7+0.5*math.log(2)
    mbar, bar = report["mbar"], report["bar"]["chain"]
    assert abs(mbar["delta_f"]-expected) < 5*mbar["sampling_se_kT"]+0.02
    assert abs(bar["delta_f"]-expected) < 5*bar["sampling_se_kT"]+0.02
    assert 0 < mbar["sampling_se_kT"] < 0.1
    assert bar["bootstrap_successes"] == bar["bootstrap_requested"] == 50
    assert "joint" in bar["uncertainty_method"]
    assert mbar["overlap_connected"] and min(mbar["weight_ess"]) > 50
    assert mbar["delta_g_kj_mol"] == pytest.approx(mbar["delta_f"]*analysis.R_KJ_MOL_K*300)
    assert report["time_stability"]["first_half"]["sampling_se_kT"] > 0
    assert "accuracy" in " ".join(report["limitations"])
    json.dumps(report, allow_nan=False)


def test_two_state_bar_matches_mbar_and_energy_units():
    reduced = harmonic(count=2, samples=500)
    dimensional = copy.deepcopy(reduced)
    dimensional["energy_units"] = "kJ/mol"
    for window in dimensional["windows"]:
        window["u_kn"] = (np.asarray(window["u_kn"])*analysis.R_KJ_MOL_K*300).tolist()
    first, second = analysis.analyze(reduced), analysis.analyze(dimensional)
    assert first["mbar"]["delta_f"] == pytest.approx(second["mbar"]["delta_f"], abs=1e-10)
    assert first["bar"]["chain"]["delta_f"] == pytest.approx(first["mbar"]["delta_f"], abs=1e-7)
    assert first["bar"]["chain"]["sampling_se_kT"] == pytest.approx(first["mbar"]["sampling_se_kT"], rel=1e-6)


def test_correlated_observable_drives_common_equilibration_and_subsampling():
    document = harmonic(count=2, samples=3000, options={"equilibration": "auto"})
    rng = np.random.default_rng(73)
    for window in document["windows"]:
        series = np.zeros(3000)
        for i in range(1, len(series)):
            series[i] = 0.97*series[i-1]+rng.normal()
        series[:450] += np.linspace(50, 0, 450)
        window["observables"] = {"slow_collective_variable": series.tolist()}
    report = analysis.analyze(document)
    for window in report["decorrelation"]:
        assert window["equilibration_samples"] > 150
        assert window["statistical_inefficiency"] > 5
        assert window["retained_samples"] < window["original_samples"]//5
        assert min(window["retained_indices"]) >= window["equilibration_samples"]


def test_constant_energy_differences_return_exact_offset_without_claiming_sampling():
    document = harmonic(count=2, samples=100)
    for window in document["windows"]:
        window["u_kn"] = [[0.0]*100, [2.0]*100]
    report = analysis.analyze(document)
    assert report["mbar"]["delta_f"] == pytest.approx(2)
    assert "constant_observables_cannot_demonstrate_sampling" in report["evidence"]["flags"]
    assert report["evidence"]["status"] != "converged"


def test_disconnected_overlap_does_not_report_an_identifiable_free_energy():
    document = harmonic(count=2, samples=200, centers=[0, 30])
    report = analysis.analyze(document)
    assert not report["mbar"]["overlap_connected"]
    assert report["mbar"]["delta_g_kj_mol"] is None
    assert "overlap_graph_disconnected" in report["evidence"]["flags"]


@pytest.mark.parametrize("mutation", ["nan", "missing_state", "duplicate_state", "other_ff", "irregular_time", "rex", "charge"])
def test_invalid_or_physically_incompatible_sampling_is_rejected(mutation):
    document = harmonic(count=2, samples=30)
    if mutation == "nan": document["windows"][0]["u_kn"][0][0] = float("nan")
    elif mutation == "missing_state": document["windows"].pop()
    elif mutation == "duplicate_state": document["windows"][1]["state_index"] = 0
    elif mutation == "other_ff": document["windows"][0]["force_field"] = "different"
    elif mutation == "irregular_time": document["windows"][0]["sample_times_ps"][1] = 0.101
    elif mutation == "rex": document["protocol"]["sampling_method"] = "replica_exchange"
    else: document["protocol"]["charge_change_e"] = 1
    with pytest.raises(ValueError): analysis.analyze(document)


def leg_report(leg, value, error, **changes):
    metadata = protocol(leg=leg, **changes)
    digest = hashlib.sha256(json.dumps([metadata, value, error], sort_keys=True).encode()).hexdigest()
    return {"quantity": "alchemical_leg", "protocol": metadata,
            "mbar": {"delta_g_kj_mol": value, "sampling_se_kj_mol": error},
            "input_sha256": digest, "sampling_data_sha256": digest, "evidence": {"flags": []}}


def ddg(value, model="amber-test", replica="r0", **changes):
    common = {"model_id": model, "force_field": model, "replicate_id": replica, "independence_id": f"independent-{replica}", **changes}
    return analysis.binding_ddg(leg_report("bound", value+4, 0.3, **common), leg_report("unbound", 4, 0.4, **common))


def test_binding_cycle_direction_units_and_covariance_are_explicit():
    bound, unbound = leg_report("bound", 7, 0.6), leg_report("unbound", 10, 0.8)
    result = analysis.binding_ddg(bound, unbound)
    assert result["delta_g_kj_mol"] == -3
    assert result["sampling_se_kj_mol"] == pytest.approx(1)
    assert result["delta_g_kcal_mol"] == pytest.approx(-3/4.184)
    assert "negative favors B" in result["sign_convention"]
    assert analysis.binding_ddg(bound, unbound, covariance_kj2_mol2=0.48)["sampling_se_kj_mol"] == pytest.approx(0.2)
    with pytest.raises(ValueError): analysis.binding_ddg(bound, unbound, covariance_kj2_mol2=0.49)
    bound["protocol"]["legs_independent"] = False
    assert analysis.binding_ddg(bound, unbound)["sampling_se_kj_mol"] is None


@pytest.mark.parametrize("field,value", [("comparison_id", "other-target"), ("water_model", "opc"), ("temperature_kelvin", 310),
                                        ("replicate_id", "r9"), ("transformation", {"from": "B", "to": "A"})])
def test_unmatched_legs_are_rejected(field, value):
    bound, unbound = leg_report("bound", 1, 0.2), leg_report("unbound", 0, 0.3)
    unbound["protocol"][field] = value
    with pytest.raises(ValueError): analysis.binding_ddg(bound, unbound)


def test_model_disagreement_stays_separate_from_independent_replica_uncertainty():
    result = analysis.summarize_models([ddg(-2, replica="r0"), ddg(-4, replica="r1"), ddg(2, model="charmm-test")])
    amber = next(model for model in result["models"] if model["model_id"] == "amber-test")
    assert amber["descriptive_mean_kj_mol"] == -3
    assert amber["within_model_sampling_se_kj_mol"] == pytest.approx(math.sqrt(0.5)/2)
    assert amber["replica_scatter_se_kj_mol"] == pytest.approx(1)
    assert result["between_model_disagreement"]["range_kj_mol"] == 5
    assert result["pooled_estimate"] is None


@pytest.mark.parametrize("shared", ["independence_id", "source_leg", "not_declared"])
def test_correlated_or_reused_replicas_never_gain_precision(shared):
    first, second = ddg(1, replica="r0"), ddg(1.2, replica="r1")
    if shared == "independence_id": second["protocol"]["independence_id"] = first["protocol"]["independence_id"]
    elif shared == "source_leg": second["source_data_sha256"][0] = first["source_data_sha256"][0]
    else: second["protocol"]["independent_replica"] = False
    result = analysis.summarize_models([first, second])["models"][0]
    assert not result["independence_declared_and_nonduplicate"]
    assert result["within_model_sampling_se_kj_mol"] is None and result["replica_scatter_se_kj_mol"] is None
    with pytest.raises(ValueError): analysis.summarize_models([first, first])


def native_xvg_files(tmp_path, document):
    paths = []
    for window in document["windows"]:
        state = window["state_index"]
        values = np.asarray(window["u_kn"])*analysis.R_KJ_MOL_K*300
        header = [r'@ title "dH/d\xl\f{}"', '@ xaxis label "Time (ps)"',
                  f'@ subtitle "T = 300 (K), state {state}: lambda = {document["states"][state]}"',
                  '@ s0 legend "Potential Energy (kJ/mol)"']
        header += [f'@ s{i+1} legend "'+r'\xD\f{}H \xl\f{} to '+f'{value}"' for i, value in enumerate(document["states"])]
        rows = []
        for n, time in enumerate(window["sample_times_ps"]):
            rows.append(" ".join(f"{x:.12g}" for x in [time, values[state, n], *(values[:, n]-values[state, n])]))
        path = tmp_path/f"lambda-{state}.xvg"
        path.write_text("\n".join(header+rows)+"\n")
        paths.append(path)
    return paths


def test_native_gromacs_xvg_adapter_uses_real_parser_and_known_free_energy(tmp_path):
    document = harmonic(count=2, samples=350)
    paths = native_xvg_files(tmp_path, document)
    report = analysis.analyze_gromacs(paths, document["protocol"])
    expected = 0.7+0.5*math.log(2)
    assert abs(report["mbar"]["delta_f"]-expected) < 5*report["mbar"]["sampling_se_kT"]+0.03
    assert report["software"]["alchemlyb"]
    assert [item["sha256"] for item in report["native_adapter"]["source_files"]] == [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    assert report["native_adapter"]["filter_bad_rows"] is False


def test_native_parser_rejects_bad_rows_instead_of_silently_filtering(tmp_path):
    paths = native_xvg_files(tmp_path, harmonic(count=2, samples=50))
    with paths[0].open("a") as stream: stream.write("999 nan 0 1\n")
    with pytest.raises((ValueError, RuntimeError)): analysis.analyze_gromacs(paths, protocol())


def test_native_state_headers_cannot_execute_python(tmp_path):
    marker = tmp_path/"must-not-exist"
    path = tmp_path/"injected.xvg"
    path.write_text('@ subtitle "T = 300 (K), state 0: lambda = '+f"__import__('pathlib').Path({str(marker)!r}).touch()"+'"\n')
    with pytest.raises(ValueError, match="numerical literals"): analysis.analyze_gromacs([path], protocol())
    assert not marker.exists()


def test_cli_report_is_finite_json(tmp_path):
    source, target = tmp_path/"samples.json", tmp_path/"result.json"
    source.write_text(json.dumps(harmonic(count=2, samples=120)))
    assert analysis.main(["analyze", str(source), "--output", str(target)]) == 0
    assert json.loads(target.read_text())["schema"] == "bio-md-analysis.v1"
