"""Detect selective reporting, artifact changes, and incomparable runtimes."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("report", Path(__file__).with_name("report.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def panel(self, backend, values=(0.8,), failed=False):
        root = self.root / backend
        root.mkdir()
        (root / "quality.py").write_text("# frozen scorer\n")
        refs = root / "references"
        refs.mkdir()
        fasta, cif, case_path = refs / "test_A.fasta", refs / "test.cif", refs / "test_A.json"
        fasta.write_text(">test_A\nACDE\n")
        cif.write_text("frozen CIF bytes; structural parsing is tested in test_quality.py")
        write(case_path, dict(version=1, name="test_A", sequence="ACDE", fasta_file=fasta.name,
                             fasta_sha256=r.sha(fasta), reference_file=cif.name, reference_sha256=r.sha(cif),
                             observed_positions=[0, 1, 2, 3], reference_ca=[[0, 0, 0], [1, 1, 1], [2, 2, 2], [3, 3, 3]]))
        fixture = root / ".fixture"
        fixture.mkdir()
        (fixture / "input.a3m").write_text(">query\nACDE\n>hit\nAC-E\n")
        write(fixture / "input.yaml", dict(version=1, sequences=[dict(protein=dict(
            id="A", sequence="ACDE", msa=str(fixture / "input.a3m")))]))
        write(fixture / "boltz_results_input/processed/records/input.json", dict(chains=[dict(chain_name="A", entity_id=0)]))
        bundle = root / ".bundle"
        r.PREPARED["capture"]("boltz2", fixture, bundle, source="private" if backend == "private" else "public")
        run = dict(model="boltz2", case="test_A", fasta_sha256=r.sha(fasta),
                   experimental_case_sha256=r.sha(case_path), manifest_sha256=r.sha(bundle / "manifest.json"),
                   expected_structure_count=1, settings=dict(gpu="test-gpu", seeds=[42], samples_per_seed=1))
        for index, value in enumerate(values):
            folder = root / "boltz2/test_A" / f"job-{index}"
            folder.mkdir(parents=True)
            r.PREPARED["materialize"](bundle, "boltz2", folder / "prepared-native")
            document = folder / "prepared-native/input.yaml"
            document.write_text(document.read_text().replace(str(folder / "prepared-native"),
                                f"/mnt/bio-shared/runs/{folder.name}/out/prepared-native"))
            source = folder / "prepared-native/source_manifest.json"
            run["manifest_sha256"] = r.sha(source)
            write(folder / "job.json", dict(job=folder.name, model="boltz2", gpu="test-gpu", exit_status=0))
            prediction = folder / "boltz_results_input/predictions/input/input_model_0.pdb"
            prediction.parent.mkdir(parents=True)
            prediction.write_text("retained prediction bytes " + str(value))
            write(folder / "accuracy.json", dict(version=1, scorer_sha256=r.sha(root / "quality.py"),
                  case="test_A", case_sha256=r.sha(case_path),
                  reference_sha256=r.sha(cif), observed_fraction=1,
                  samples=[dict(path=str(prediction), sha256=r.sha(prediction), ca_rmsd_angstrom=1,
                                lddt_ca_mean=value)]))
            write(folder / "runtime-audit.json", dict(model="boltz2", python="3.12", packages={"torch": "2.8"},
                  checkpoint=dict(sha256="a" * 64), gpu="Test GPU, UUID, driver1, 80000 MiB",
                  inference_processes=[dict(argv=["boltz", "predict", f"/mnt/bio-shared/runs/{folder.name}/input.yaml"],
                                            environment={})]))
            write(folder / "resolved-settings.json", dict(version=1, model="boltz2", settings=dict(seed=42),
                  runtime_audit_sha256=r.sha(folder / "runtime-audit.json"), sources={"native.py": "b" * 64},
                  helper_sha256="a" * 64))
        if failed:
            write(root / "boltz2/test_A/job-failed/job.json",
                  dict(job="job-failed", model="boltz2", gpu="test-gpu", exit_status=143))
        if not values:
            run["manifest_sha256"] = "b" * 64
        write(root / "expected-runs.json", dict(version=1, runs=[run], quality_source_sha256=r.sha(root / "quality.py")))
        return root

    def test_failed_attempts_and_missing_backend_remain_visible(self):
        root = self.panel("public", failed=True)
        result = r.compare(r.collect(root))
        case = result["cases"][0]
        self.assertEqual(case["status"], "incomplete")
        self.assertEqual([a["status"] for a in case["backends"]["public"]["attempts"]],
                         ["complete", "job_failed"])
        self.assertIsNone(result["summary"]["boltz2"]["mean_private_minus_public"])

    def test_every_complete_retry_contributes_without_best_sample_selection(self):
        public = self.panel("public", (0.2, 0.8), failed=True)
        private = self.panel("private", (0.6,))
        result = r.compare(r.collect(public), r.collect(private))
        case = result["cases"][0]
        self.assertEqual(case["complete_attempt_counts"], dict(public=2, private=1))
        self.assertAlmostEqual(case["private_minus_public"]["lddt_ca_mean"], 0.1)
        self.assertEqual(result["scientific_parity"], "not_established")

    def test_omitted_or_changed_predictions_are_not_accepted(self):
        for mode in ("omitted", "changed", "wrong_seed"):
            with self.subTest(mode=mode):
                root = self.panel(mode)
                folder = root / "boltz2/test_A/job-0"
                scores = r.read(folder / "accuracy.json")
                if mode == "omitted":
                    scores["samples"] = []
                elif mode == "changed":
                    Path(scores["samples"][0]["path"]).write_text("mutated")
                else:
                    source = Path(scores["samples"][0]["path"])
                    target = source.with_name("input_model_1.pdb")
                    source.rename(target)
                    scores["samples"][0]["path"] = str(target)
                write(folder / "accuracy.json", scores)
                attempt = r.collect(root)["cases"][("boltz2", "test_A")]["attempts"][0]
                self.assertEqual(attempt["status"], "validation_failed")
                self.assertTrue(attempt["error"])

    def test_runtime_or_reference_mismatch_prevents_pairing(self):
        public = r.collect(self.panel("public"))
        private = r.collect(self.panel("private"))
        for key, replacement in (("runtime", {}), ("effective_settings", {}),
                                 ("reference_sha256", "d" * 64), ("observed_fraction", 0.95)):
            with self.subTest(key=key):
                changed = copy.deepcopy(private)
                changed["cases"][("boltz2", "test_A")]["attempts"][0][key] = replacement
                result = r.compare(public, changed)
                self.assertEqual(result["cases"][0]["status"], "incompatible")
                self.assertIn(key, result["cases"][0]["mismatches"])
                self.assertEqual(result["summary"]["boltz2"]["paired_cases"], 0)

    def test_equal_case_weighting_and_missing_case_denominator(self):
        public = r.collect(self.panel("public", (0.2, 0.4)))
        private = r.collect(self.panel("private", (0.6,)))
        for panel in (public, private):
            case = copy.deepcopy(panel["cases"][("boltz2", "test_A")])
            panel["cases"][("boltz2", "second_A")] = case
            case["attempts"] = case["attempts"][:1]
            case["attempts"][0]["means"]["lddt_ca_mean"] = 0.5
        public["cases"][("boltz2", "missing_A")] = dict(expected={}, attempts=[], failure_records=[])
        result = r.compare(public, private)
        self.assertEqual(result["summary"]["boltz2"]["expected_cases"], 3)
        self.assertEqual(result["summary"]["boltz2"]["paired_cases"], 2)
        self.assertAlmostEqual(result["summary"]["boltz2"]["mean_private_minus_public"]["lddt_ca_mean"], 0.15)

    def test_unknown_jobs_duplicate_expectations_and_changed_scorer_are_rejected(self):
        for mode in ("unknown", "duplicate", "scorer"):
            with self.subTest(mode=mode):
                root = self.panel(mode)
                manifest = root / "expected-runs.json"
                if mode == "unknown":
                    write(root / "boltz2/unplanned_A/job-extra/job.json", {})
                elif mode == "duplicate":
                    value = r.read(manifest)
                    value["runs"].append(value["runs"][0])
                    write(manifest, value)
                else:
                    (root / "quality.py").write_text("modified")
                with self.assertRaises(ValueError):
                    r.collect(root)

    def test_frozen_reference_and_materialized_input_changes_are_detected(self):
        for mode in ("cif", "fasta", "runtime", "msa", "config_binding", "coverage"):
            with self.subTest(mode=mode):
                root = self.panel(mode)
                folder = root / "boltz2/test_A/job-0"
                if mode in ("cif", "fasta"):
                    path = root / "references" / ("test.cif" if mode == "cif" else "test_A.fasta")
                    path.write_text(path.read_text() + "changed")
                    with self.assertRaises(ValueError):
                        r.collect(root)
                    continue
                if mode == "runtime":
                    write(folder / "prepared-native/runtime.json", dict(use_templates=True))
                elif mode == "msa":
                    path = next((folder / "prepared-native/files").rglob("*.a3m"))
                    path.write_text(path.read_text() + "\n")
                elif mode == "config_binding":
                    saved = r.read(folder / "resolved-settings.json")
                    saved["runtime_audit_sha256"] = "0" * 64
                    write(folder / "resolved-settings.json", saved)
                else:
                    saved = r.read(folder / "accuracy.json")
                    saved["observed_fraction"] = 0.5
                    write(folder / "accuracy.json", saved)
                attempt = r.collect(root)["cases"][("boltz2", "test_A")]["attempts"][0]
                self.assertEqual(attempt["status"], "validation_failed")

    def test_identical_forked_invocations_are_accepted_but_divergence_is_rejected(self):
        root = self.panel("public")
        path = root / "boltz2/test_A/job-0/runtime-audit.json"
        audit = r.read(path)
        signature = r.runtime_signature(audit, "boltz2")
        audit["inference_processes"] += [copy.deepcopy(audit["inference_processes"][0]) for _ in range(2)]
        self.assertEqual(r.runtime_signature(audit, "boltz2"), signature)
        audit["inference_processes"][-1]["environment"]["OMP_NUM_THREADS"] = "99"
        with self.assertRaisesRegex(ValueError, "divergent"):
            r.runtime_signature(audit, "boltz2")

    def test_cli_cache_basename_does_not_confuse_executable_identity(self):
        argv = ["/venv/python", "/venv/bin/boltz", "predict", "input.yaml", "--cache", "/cache/boltz"]
        audit = dict(model="boltz2", inference_processes=[dict(argv=argv, environment={})])
        self.assertEqual(r.SETTINGS["native_arguments"](audit, "boltz2"), argv[3:])
        audit["inference_processes"][0]["argv"] = ["/cache/boltz"]
        with self.assertRaises(ValueError):
            r.SETTINGS["native_arguments"](audit, "boltz2")


if __name__ == "__main__":
    unittest.main()
