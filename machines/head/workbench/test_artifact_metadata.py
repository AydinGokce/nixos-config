import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).absolute().parent.parent))
from workbench.artifact_metadata import POLICY, RF3_SOURCE, structure_metadata
from workbench.common import file_sha


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) if isinstance(value, (dict, list)) else value)
        return {"path": relative, "sha256": file_sha(path)}

    def rf3_fixture(self):
        expected = self.write("rf3-output-expected.json", {"name": "protein"})
        samples = []
        for number, rank, passed in ((0, 0.95, False), (1, 0.8, True), (2, 0.7, True)):
            directory = f"protein/seed-101_sample-{number}"
            prefix = directory + f"/protein_seed-101_sample-{number}"
            files = {
                "model": self.write(prefix + "_model.cif", f"data_sample_{number}\n"),
                "summary": self.write(prefix + "_summary_confidences.json", {"ranking_score": rank, "overall_plddt": 0.91}),
                "confidences": self.write(prefix + "_confidences.json", {"plddt": [0.91]}),
            }
            samples.append({"directory": directory, "passed": passed, "ranking_score": rank, "files": files,
                            "chemistry": {"passed": passed, "atoms": 4, "expected_atoms": 4,
                                "identity_and_connectivity_passed": True, "finite_coordinates": True,
                                "double_bonds": [] if passed else [{"expected": "trans", "observed": "cis", "passed": False}]}})
        selected = {"raw_directory": samples[1]["directory"], "ranking_score": 0.8, "files": {}}
        for kind, suffix in {"model": "_model.cif", "summary": "_summary_confidences.json", "confidences": "_confidences.json"}.items():
            source = samples[1]["files"][kind]
            name = "protein/protein" + suffix
            shutil.copyfile(self.root / source["path"], self.root / name)
            selected["files"][kind] = {"path": name, "raw_path": source["path"], "sha256": file_sha(self.root / name)}
        qa = {"schema": 1, "policy": POLICY, "source_commit": RF3_SOURCE, "status": "passed",
              "expected_source": expected["path"], "expected_sha256": expected["sha256"],
              "audit_source_sha256": "a" * 64, "samples": samples, "selected": selected}
        self.publish_qa(qa)
        return qa

    def publish_qa(self, qa, *, resident=False):
        binding = self.write("rf3-output-validation.json", qa)
        runtime = {"model": "rf3", "status": "complete", "output_validation": qa,
                   "output_validation_sha256": binding["sha256"], "output_validation_source_sha256": "a" * 64,
                   "outputs": {"model": qa["selected"]["files"]["model"]["path"],
                               "model_sha256": qa["selected"]["files"]["model"]["sha256"]}}
        self.write("rf3-resident-result.json" if resident else "rf3-runtime.json", runtime)

    def frontend_fixture(self):
        from inference.common import digest as native_digest
        qa = self.rf3_fixture()
        job = {"id": "rf3-resident-test", "model": "rf3", "postprocess": {"kind": "pinned-command"},
               "provenance": {"name": "évidence"}}
        request_sha = native_digest(job)
        runtime = json.loads((self.root / "rf3-runtime.json").read_text())
        cpu = {**runtime, "job_id": job["id"], "job_sha256": request_sha}
        cpu_file = self.write("rf3-resident-result.json", cpu)
        checked = {**cpu, "files": {cpu_file["path"]: {"sha256": cpu_file["sha256"]}}}
        completion = {"state": "complete", "id": job["id"], "payload": job, "result": {
            "postprocess": {"result": checked, "job_sha256": request_sha,
                            "stage_sha256": native_digest(job["postprocess"])}}}
        runtime.pop("output_validation_source_sha256")
        runtime.update(execution="resident", request_sha256=request_sha,
                       resident_completion_sha256=native_digest(completion))
        self.write("rf3-runtime.json", runtime)
        self.write("resident-result.json", completion)
        self.write("job.json", job)
        return qa

    def test_canonical_selection_uses_verified_passing_rank_not_highest_failed_sample(self):
        qa = self.rf3_fixture()
        result = structure_metadata(self.root / "protein/protein_model.cif", self.root, "rf3")
        self.assertTrue(result["selected"])
        self.assertEqual(result["qa"]["status"], "passed")
        self.assertEqual(result["sample_id"], "protein/seed-101_sample-1")
        self.assertEqual(result["confidence"]["metrics"], {"ranking_score": 0.8, "overall_plddt": 0.91})
        self.assertNotIn(str(self.root), json.dumps(result))
        failed = structure_metadata(self.root / qa["samples"][0]["files"]["model"]["path"], self.root, "rf3")
        self.assertEqual(failed["qa"]["status"], "failed")
        self.assertEqual(failed["qa"]["failed_checks"]["double_bond_stereochemistry"], 1)
        self.assertFalse(failed["selected"])
        passing = structure_metadata(self.root / qa["samples"][2]["files"]["model"]["path"], self.root, "rf3")
        self.assertEqual(passing["qa"]["status"], "passed")
        self.assertFalse(passing["selected"])

    def test_selected_coordinate_or_companion_tampering_removes_selected_marker(self):
        for relative in ("protein/protein_model.cif", "protein/protein_summary_confidences.json"):
            with self.subTest(relative=relative):
                self.rf3_fixture()
                (self.root / relative).write_text("changed")
                result = structure_metadata(self.root / "protein/protein_model.cif", self.root, "rf3")
                self.assertFalse(result["selected"])
                self.assertEqual(result["qa"]["status"], "unverified")

    def test_missing_or_mismatched_runtime_binding_never_selects(self):
        self.rf3_fixture()
        runtime = self.root / "rf3-runtime.json"
        value = json.loads(runtime.read_text()); value["output_validation_sha256"] = "b" * 64
        runtime.write_text(json.dumps(value))
        result = structure_metadata(self.root / "protein/protein_model.cif", self.root, "rf3")
        self.assertFalse(result["selected"])
        self.assertEqual(result["qa"]["status"], "unverified")
        runtime.unlink()
        self.assertFalse(structure_metadata(self.root / "protein/protein_model.cif", self.root, "rf3")["selected"])

    def test_expected_chemistry_and_runtime_selected_output_must_match(self):
        self.rf3_fixture()
        (self.root / "rf3-output-expected.json").write_text("changed")
        self.assertFalse(structure_metadata(self.root / "protein/protein_model.cif", self.root, "rf3")["selected"])
        self.rf3_fixture()
        path = self.root / "rf3-runtime.json"; value = json.loads(path.read_text()); value["outputs"]["model_sha256"] = "0" * 64
        path.write_text(json.dumps(value))
        self.assertFalse(structure_metadata(self.root / "protein/protein_model.cif", self.root, "rf3")["selected"])

    def test_forged_selection_of_lower_ranked_passing_sample_is_not_promoted(self):
        qa = self.rf3_fixture()
        qa["samples"][2]["ranking_score"] = 0.85
        # Even a self-consistent runtime/QA hash cannot hide a conflicting declared native rank.
        self.publish_qa(qa)
        result = structure_metadata(self.root / "protein/protein_model.cif", self.root, "rf3")
        self.assertFalse(result["selected"])
        self.assertEqual(result["qa"]["status"], "unverified")

    def test_path_escape_and_malformed_report_leave_file_unverified(self):
        qa = self.rf3_fixture(); qa["expected_source"] = "../outside.json"; self.publish_qa(qa)
        self.assertFalse(structure_metadata(self.root / "protein/protein_model.cif", self.root, "rf3")["selected"])
        self.write("rf3-output-validation.json", {"samples": [1], "selected": []})
        result = structure_metadata(self.root / "protein/protein_model.cif", self.root, "rf3")
        self.assertEqual(result["qa"]["status"], "unverified")

    def test_resident_receipt_binds_same_qa_and_selected_copy(self):
        qa = self.rf3_fixture()
        (self.root / "rf3-runtime.json").unlink()
        self.publish_qa(qa, resident=True)
        self.assertTrue(structure_metadata(self.root / "protein/protein_model.cif", self.root, "rf3")["selected"])

    def test_missing_audit_source_or_contradictory_chemistry_cannot_pass(self):
        qa = self.rf3_fixture()
        del qa["audit_source_sha256"]
        self.publish_qa(qa)
        self.assertFalse(structure_metadata(self.root / "protein/protein_model.cif", self.root, "rf3")["selected"])
        qa = self.rf3_fixture()
        qa["samples"][1]["chemistry"]["finite_coordinates"] = False
        self.publish_qa(qa)
        value = structure_metadata(self.root / "protein/protein_model.cif", self.root, "rf3")
        self.assertFalse(value["selected"])
        self.assertEqual(value["qa"]["status"], "unverified")

    def test_frontend_dual_runtime_publication_is_selected_only_with_complete_binding(self):
        self.frontend_fixture()
        path = self.root / "protein/protein_model.cif"
        value = structure_metadata(path, self.root, "rf3")
        self.assertTrue(value["selected"])
        self.assertEqual(value["qa"]["runtime"]["path"], "rf3-runtime.json")
        self.assertEqual(value["qa"]["resident_runtime"]["path"], "rf3-resident-result.json")
        self.assertEqual(value["qa"]["resident_completion"]["path"], "resident-result.json")
        self.write("resident-result.json", {"state": "complete"})
        self.assertFalse(structure_metadata(path, self.root, "rf3")["selected"])
        self.frontend_fixture()
        cpu_path = self.root / "rf3-resident-result.json"
        cpu = json.loads(cpu_path.read_text()); cpu["job_sha256"] = "0" * 64
        cpu_path.write_text(json.dumps(cpu))
        self.assertFalse(structure_metadata(path, self.root, "rf3")["selected"])
        self.frontend_fixture()
        runtime = self.root / "rf3-runtime.json"
        value = json.loads(runtime.read_text()); value["execution"] = "ephemeral"
        runtime.write_text(json.dumps(value))
        self.assertFalse(structure_metadata(path, self.root, "rf3")["selected"])

    def test_nearby_unrelated_summary_is_not_associated(self):
        self.write("input.cif", "input coordinates")
        self.write("confidence_unrelated_model_0.json", {"confidence_score": 0.99})
        self.assertIsNone(structure_metadata(self.root / "input.cif", self.root, "boltz2")["confidence"])

    def test_native_metrics_keep_names_scales_and_exclude_nonfinite_values(self):
        cases = [
            ("protenix", "test_sample_0.cif", "test_summary_confidence_sample_0.json", {"plddt": 91.0, "ranking_score": 0.8}),
            ("openfold3", "test_seed_42_sample_1_model.cif", "test_seed_42_sample_1_confidences_aggregated.json", {"avg_plddt": 94.0, "sample_ranking_score": 0.6}),
            ("boltz2", "test_model_0.cif", "confidence_test_model_0.json", {"complex_plddt": 0.95, "confidence_score": 0.91}),
        ]
        for model, name, sidecar, values in cases:
            self.write(name, "data_structure\n"); self.write(sidecar, {**values, "unknown": 0.9, "ptm": None})
            result = structure_metadata(self.root / name, self.root, model)
            self.assertEqual(result["confidence"]["metrics"], values)
            self.assertFalse(result["selected"])
        self.write("test_model_0.cif", "data_structure\n")
        self.write("confidence_test_model_0.json", {"confidence_score": float("nan")})
        self.assertIsNone(structure_metadata(self.root / "test_model_0.cif", self.root, "boltz2")["confidence"])


if __name__ == "__main__":
    unittest.main()
