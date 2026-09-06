"""Native-boundary contract tests without weights, native packages, or GPUs."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import random
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


# Package import preserves the same relative _common import used in production.
parent = Path(__file__).parent
package_name = "resident_contract_test_adapters"
package = types.ModuleType(package_name)
package.__path__ = [str(parent / "adapters")]
sys.modules[package_name] = package
spec = importlib.util.spec_from_file_location(package_name + ".rf3", parent / "adapters/rf3.py")
rf3 = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rf3
spec.loader.exec_module(rf3)
prepare_spec = importlib.util.spec_from_file_location("real_rf3_prepare_test", parent.parent / "rf3/prepare.py")
prepare = importlib.util.module_from_spec(prepare_spec)
prepare_spec.loader.exec_module(prepare)


class TensorState:
    def __init__(self, value):
        self.value = value
    def clone(self):
        return TensorState(self.value)


class FakeTorch:
    def __init__(self):
        self.state = 7
        self.cuda = types.SimpleNamespace(is_available=lambda: False)
    def get_rng_state(self):
        return TensorState(self.state)
    def set_rng_state(self, state):
        self.state = state.value
    def manual_seed(self, seed):
        self.state = seed


class FakeNumpy:
    def __init__(self):
        self.state = 11
        self.random = self
    def get_state(self):
        return self.state
    def set_state(self, state):
        self.state = state
    def seed(self, seed):
        self.state = seed


class FakeModel:
    def eval(self):
        return self


class FakeEngine:
    def __init__(self, adapter):
        self.adapter = adapter
        self.pipeline = {"inputs_seen": []}
        self.trainer = types.SimpleNamespace(metrics={"seen": []}, state={"model": FakeModel()})
        self.initialize_calls = 1
        self.results = []
        self.fail = False
        self.partial = False
    def _construct_pipeline(self, config):
        # Native transforms may retain import modules: a whole-pipeline
        # deepcopy is invalid. The real native factory is the safe boundary.
        self.pipeline = {"inputs_seen": [], "module": types.ModuleType("native_module"),
                         "constructor_draw": random.random()}
    def initialize(self):
        self.initialize_calls += 1
    def run(self, **params):
        self.initialize()
        if self.pipeline["inputs_seen"] or self.trainer.metrics["seen"]:
            raise AssertionError("Mutable controls leaked from the previous job")
        value = prepare.document(prepare.read_json(params["inputs"]))
        self.pipeline["inputs_seen"].append(value["name"])
        self.trainer.metrics["seen"].append(value["name"])
        draw = random.random()
        self.results.append((draw, self.adapter.torch.state, self.adapter.np.state, deepcopy(params)))
        self.adapter.torch.state += 123
        self.adapter.np.state += 456
        if self.fail:
            raise RuntimeError("native CUDA failure")
        out = Path(params["out_dir"])
        count = self.adapter.base["diffusion_batch_size"] - int(self.partial)
        for index in range(count):
            sample = f"seed-{self.seed}_sample-{index}"
            directory = out / value["name"] / sample
            directory.mkdir(parents=True)
            for suffix in ("_model.cif", "_summary_confidences.json", "_confidences.json"):
                (directory / (value["name"] + "_" + sample + suffix)).write_text(str(draw))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = dict(model="rf3", checkpoint={"path": str(self.root / "weights.ckpt"), "sha256": rf3.WEIGHTS_SHA256},
                           native_config={}, work_dir=str(self.root / "work"), tools_dir=str(parent.parent),
                           postprocess_mode="deferred")
        self.adapter = rf3.Adapter(self.config)
        self.adapter.torch, self.adapter.np = FakeTorch(), FakeNumpy()
        random.seed(101)
        random.random()  # Simulate a native initialization RNG draw.
        self.adapter.initial_rng = rf3.capture_rng(self.adapter.torch, self.adapter.np)
        self.adapter.engine = FakeEngine(self.adapter)
        self.adapter._pipeline_initial_rng = rf3.capture_rng(self.adapter.torch, self.adapter.np)
        self.adapter._pipeline_config = {"native": "pipeline-config"}
        self.adapter._build_pipeline = self.adapter.engine._construct_pipeline
        self.adapter._metrics = deepcopy(self.adapter.engine.trainer.metrics)
        self.adapter._initialize = self.adapter.engine.initialize
        self.adapter.prepare = prepare
        def expected(entry):
            manifest = prepare.validate(entry)
            value = prepare.document(prepare.read_json(entry))
            required = {"input.json"} | {component[key] for component in value["components"]
                                       for key in ("path", "msa_path") if key in component}
            return {"policy": "rf3-named-atoms-bonds-requested-stereo-v1", "source_commit": rf3.SOURCE_PIN,
                    "name": value["name"], "prepared_files_sha256": {key: manifest["files"][key] for key in required}}
        self.adapter.expected_helper = types.SimpleNamespace(_expected=expected)
        def generated(entry, output, tools):
            rf3._write(output, expected(entry))
            return {"mode": "isolated-native-cpu-child-v1"}
        self.expected_patcher = patch.object(rf3, "generate_expected", side_effect=generated)
        self.expected_patcher.start()
        self.addCleanup(self.expected_patcher.stop)
        versions = {"numpy": "2.5.3", "biotite": "1.4.0"}
        patcher = patch.object(rf3.importlib.metadata, "version", side_effect=lambda name: versions[name])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.adapter.tools = parent.parent
        self.adapter.load_receipt = {"sources": {relative: rf3.sha256(parent.parent / relative) for relative in
                                   ("rf3/prepare.py", "library/rf3_output.py", "library/rf3_compat.py")}}
        self.adapter.audit_classes = tuple(type(name, (), {"forward": lambda self, value: value}) for name in
                                          ("LoadPolymerMSAs", "PairAndMergePolymerMSAs", "FeaturizeMSALikeAF3"))
        def install_audit(path):
            Path(path).write_text(json.dumps({"events": [{"transform": cls.__name__} for cls in self.adapter.audit_classes]}))
            for cls in self.adapter.audit_classes:
                cls.forward = lambda self, value: "observed"
        self.adapter.audit = types.SimpleNamespace(install=install_audit)
        self.adapter.loaded = True
        fasta = self.root / "input.fasta"
        fasta.write_text(">protein\nACDE\n")
        alignment = self.root / "A.a3m"
        alignment.write_text(">query\nACDE\n>hit TaxID=1\nACdDE\n")
        mapping = self.root / "map.json"
        mapping.write_text(json.dumps({"A": str(alignment)}))
        self.entry = prepare.prepare(out=self.root / "prepared", fasta=fasta, msa_map=mapping)

    def job(self, name="one"):
        return dict(id=name, model="rf3", native_input=str(self.entry), seeds=[101], provenance={"backend": "private"})

    def test_defaults_match_full_pinned_cli_including_early_stop_and_b_factors(self):
        value = self.adapter.base
        self.assertEqual((value["n_recycles"], value["num_steps"], value["diffusion_batch_size"], value["seed"]), (10, 50, 5, 101))
        self.assertEqual(value["early_stopping_plddt_threshold"], 0.5)
        self.assertTrue(value["annotate_b_factor_with_plddt"])
        self.assertFalse(value["skip_existing"])
        self.assertEqual(value["raise_if_missing_msa_for_protein_of_length_n"], 1)

    def test_expected_chemistry_runs_in_a_bounded_cuda_hidden_child(self):
        self.expected_patcher.stop()
        target = self.root / "expected.json"
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            target.write_text('{"expected":"native child"}')
        with patch.object(rf3.subprocess, "run", side_effect=run):
            receipt = rf3.generate_expected(self.entry, target, self.adapter.tools)
        command, options = calls[0]
        self.assertIn("--expected-input", command)
        self.assertEqual(options["env"]["CUDA_VISIBLE_DEVICES"], "")
        self.assertEqual(options["env"]["OMP_NUM_THREADS"], "1")
        self.assertEqual(options["timeout"], 180)
        self.assertTrue(options["check"])
        self.assertEqual(receipt["mode"], "isolated-native-cpu-child-v1")

    def test_standalone_expected_child_does_not_shadow_native_rf3_package(self):
        native = self.root / "native/rf3"
        native.mkdir(parents=True)
        (native / "__init__.py").write_text("")
        (native / "utils.py").write_text("VALUE = 'native-package'\n")
        tools = self.root / "tools"
        (tools / "rf3").mkdir(parents=True)
        (tools / "library").mkdir()
        (tools / "rf3/prepare.py").write_text("def validate(path): return {}\n")
        (tools / "library/rf3_output.py").write_text(
            "def _expected(path):\n from rf3.utils import VALUE\n return {'import': VALUE}\n")
        target = self.root / "native-expected.json"
        subprocess.run([sys.executable, "-B", str(Path(rf3.__file__)), "--expected-input", str(self.entry),
            "--expected-output", str(target), "--tools-dir", str(tools)],
            env=dict(os.environ, PYTHONPATH=str(native.parent)), check=True, capture_output=True)
        self.assertEqual(json.loads(target.read_text()), {"import": "native-package"})

    def prepared_chemistry(self):
        manifest = prepare.validate(self.entry)
        payload = self.adapter.expected_helper._expected(self.entry)
        rf3._write(self.entry.parent / rf3.EXPECTED_FILE, payload)
        receipt = {"schema": 1, "kind": "rf3-expected-chemistry",
            "expected_sha256": rf3.sha256(self.entry.parent / rf3.EXPECTED_FILE),
            "base_prepared_sha256": manifest["sha256"], "runtime": {"native": "pinned"},
            "sources": deepcopy(self.adapter.load_receipt["sources"])}
        receipt["sha256"] = rf3._json_digest(receipt)
        rf3._write(self.entry.parent / rf3.EXPECTED_RECEIPT, receipt)
        manifest["files"] = {str(path.relative_to(self.entry.parent)): prepare.file_hash(path)
            for path in sorted(self.entry.parent.rglob("*")) if path.is_file() and path.name != "msa-manifest.json"}
        manifest.pop("sha256")
        manifest["sha256"] = prepare.digest(manifest)
        (self.entry.parent / "msa-manifest.json").write_text(json.dumps(manifest))
        return receipt

    def test_precomputed_chemistry_is_bound_and_copied_without_native_child(self):
        receipt = self.prepared_chemistry()
        self.adapter.load_receipt["environment"] = {"native": "pinned"}
        with patch.object(rf3, "generate_expected", side_effect=AssertionError("unexpected native child")):
            result = self.adapter.predict(self.job(), self.root / "ready")
        self.assertEqual(result["expected_input"]["generation"],
            {"mode": "verified-cpu-preparation-v1", "receipt_sha256": receipt["sha256"]})
        self.assertEqual((self.root / "ready/rf3-native-expected-input.json").read_bytes(),
            (self.entry.parent / rf3.EXPECTED_FILE).read_bytes())

    def test_precomputed_runtime_mismatch_fails_before_native_forward(self):
        self.prepared_chemistry()
        self.adapter.load_receipt["environment"] = {"native": "different"}
        with self.assertRaisesRegex(ValueError, "native runtime differs"):
            self.adapter.predict(self.job(), self.root / "wrong")
        self.assertEqual(self.adapter.engine.results, [])

    def test_precomputed_receipt_cannot_rebind_changed_base_input(self):
        receipt = self.prepared_chemistry()
        receipt['base_prepared_sha256'] = '0' * 64
        receipt['sha256'] = rf3._json_digest({k: v for k, v in receipt.items() if k != 'sha256'})
        (self.entry.parent / rf3.EXPECTED_RECEIPT).write_text(json.dumps(receipt))
        manifest = json.loads((self.entry.parent / 'msa-manifest.json').read_text())
        manifest['files'][rf3.EXPECTED_RECEIPT] = prepare.file_hash(self.entry.parent / rf3.EXPECTED_RECEIPT)
        manifest.pop('sha256'); manifest['sha256'] = prepare.digest(manifest)
        (self.entry.parent / 'msa-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'another prepared input'):
            rf3.prepared_expected(self.entry, self.adapter.tools)

    def test_load_initializes_once_before_rng_capture_and_restores_working_directory(self):
        adapter = rf3.Adapter(self.config)
        torch, numpy = FakeTorch(), FakeNumpy()
        checkpoint = Path(self.config["checkpoint"]["path"])
        checkpoint.write_text("verified by fake installation boundary")
        original_cwd = Path.cwd()
        events = []
        class Engine:
            def __init__(self, **kwargs):
                events.append(("construct", kwargs, str(Path.cwd())))
                random.seed(kwargs["seed"])
                random.random()
                self.pipeline = {"fresh": True}
                self.trainer = types.SimpleNamespace(metrics={"fresh": True})
            def _construct_pipeline(self, config):
                self.pipeline = {"module": types.ModuleType("native_module")}
            def initialize(self):
                events.append(("initialize",))
                self._construct_pipeline({"val": "exact-native-pipeline-config"})
                torch.state = 444
                numpy.state = 555
                random.random()
        runtime = types.SimpleNamespace(verify_install=lambda shared: {"source_commit": rf3.SOURCE_PIN},
                                        checkpoint=lambda shared: checkpoint)
        fake_helpers = {"runtime.py": runtime, "prepare.py": prepare, "feature_audit.py": self.adapter.audit,
                        "rf3_compat.py": types.SimpleNamespace(install=lambda: events.append(("compat",))),
                        "rf3_output.py": self.adapter.expected_helper}
        native_msa = types.SimpleNamespace(**{cls.__name__: cls for cls in self.adapter.audit_classes})
        native_modules = {"torch": torch, "numpy": numpy,
                          "atomworks.ml.transforms.msa": types.SimpleNamespace(msa=native_msa),
                          "rf3.inference_engines.rf3": types.SimpleNamespace(RF3InferenceEngine=Engine)}
        with patch.object(rf3, "_module", side_effect=lambda path, name: fake_helpers[Path(path).name]), \
                patch.object(rf3, "_rng_receipt", return_value={"state": "verified"}), \
                patch.dict(sys.modules, native_modules), patch.dict(rf3.os.environ, {"LOCAL_MSA_DIRS": ""}):
            receipt = adapter.load()
        self.assertEqual([event[0] for event in events], ["compat", "construct", "initialize"])
        self.assertEqual(events[1][2], self.config["work_dir"])
        self.assertEqual((adapter.initial_rng["torch"].value, adapter.initial_rng["numpy"]), (444, 555))
        self.assertEqual(receipt["post_initialize_rng"], {"state": "verified"})
        self.assertEqual(Path.cwd(), original_cwd)
        self.assertTrue(adapter.loaded)
        with self.assertRaises(RuntimeError):
            adapter.load()

    def test_repeated_requests_restore_cold_rng_and_fresh_controls_without_reloading(self):
        original = prepare.validate(self.entry)
        one = self.adapter.predict(self.job(), self.root / "one")
        random.random()
        self.adapter.torch.state = 99
        self.adapter.np.state = 99
        two = self.adapter.predict(self.job("two"), self.root / "two")
        engine = self.adapter.engine
        self.assertEqual(engine.results[0][:3], engine.results[1][:3])
        self.assertEqual(engine.initialize_calls, 1)
        self.assertEqual(prepare.validate(self.entry), original)
        self.assertIsNone(engine.pipeline)
        self.assertEqual(engine.trainer.metrics, {"seen": []})
        self.assertEqual(len(one["structures"]), 5)
        self.assertEqual(len(two["structures"]), 5)
        self.assertEqual(one["status"], "awaiting_chemistry_validation")
        self.assertNotEqual(one["job_sha256"], two["job_sha256"])
        self.assertEqual(one["prepared_sha256"], two["prepared_sha256"])

    def test_native_failure_restores_wrappers_cwd_state_and_poisoned_worker_cannot_continue(self):
        original_methods = [cls.forward for cls in self.adapter.audit_classes]
        original_cwd = Path.cwd()
        self.adapter.engine.fail = True
        with self.assertRaisesRegex(RuntimeError, "native CUDA"):
            self.adapter.predict(self.job(), self.root / "failed")
        self.assertEqual(Path.cwd(), original_cwd)
        self.assertEqual([cls.forward for cls in self.adapter.audit_classes], original_methods)
        self.assertFalse(self.adapter.loaded)
        self.assertTrue(self.adapter.poisoned)
        receipt = json.loads((self.root / "failed/rf3-resident-result.json").read_text())
        self.assertTrue(receipt["resident_restart_required"])
        with self.assertRaises(RuntimeError):
            self.adapter.predict(self.job("later"), self.root / "later")

    def test_partial_native_samples_fail_but_raw_outputs_remain(self):
        self.adapter.engine.partial = True
        with self.assertRaisesRegex(RuntimeError, "4/5"):
            self.adapter.predict(self.job(), self.root / "partial")
        self.assertEqual(len(list((self.root / "partial").glob("*/seed-*_sample-*/*_model.cif"))), 4)

    def test_science_or_seed_change_refused_before_native_or_output(self):
        jobs = [self.job(str(index)) for index in range(4)]
        jobs[0]["seeds"] = [102]
        jobs[1]["seeds"] = [101, 102]
        jobs[2]["native_config"] = dict(self.adapter.base, num_steps=51)
        jobs[3]["native_config"] = dict(self.adapter.base, skip_existing=True)
        for index, job in enumerate(jobs):
            out = self.root / f"bad-{index}"
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.adapter.predict(job, out)
            self.assertFalse(out.exists())
        self.assertEqual(self.adapter.engine.results, [])

    def test_repeated_job_id_and_concurrent_job_are_refused(self):
        self.adapter.predict(self.job(), self.root / "one")
        with self.assertRaises(ValueError):
            self.adapter.predict(self.job(), self.root / "duplicate")
        self.adapter._lock.acquire()
        try:
            with self.assertRaises(RuntimeError):
                self.adapter.predict(self.job("busy"), self.root / "busy")
        finally:
            self.adapter._lock.release()

    def test_tampered_or_symlinked_prepared_input_refused(self):
        (self.entry.parent / "msas/A.a3m").write_text(">query\nWRONG\n")
        with self.assertRaises(RuntimeError):
            self.adapter.predict(self.job(), self.root / "tampered")
        self.assertEqual(self.adapter.engine.results, [])

    def test_default_inline_path_requires_postprocess_pass(self):
        self.adapter.config.pop("postprocess_mode")
        with patch.object(rf3, "postprocess", side_effect=RuntimeError("no chemically valid sample")) as check:
            with self.assertRaisesRegex(RuntimeError, "chemically valid"):
                self.adapter.predict(self.job(), self.root / "inline")
        check.assert_called_once()
        self.assertEqual(len(list((self.root / "inline").glob("*/seed-*_sample-*/*_model.cif"))), 5)

    def test_postprocess_binds_job_helper_and_raw_artifact_before_native_qa(self):
        job = self.job()
        result = self.adapter.predict(job, self.root / "raw")
        altered = dict(job, provenance={"backend": "public"})
        with self.assertRaisesRegex(ValueError, "envelope"):
            rf3.postprocess(altered, result, self.root / "raw")
        changed = deepcopy(result)
        changed["postprocess"]["sources"]["library/rf3_output.py"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source"):
            rf3.postprocess(job, changed, self.root / "raw")
        Path(result["structures"][0]).write_text("tampered")
        with self.assertRaisesRegex(ValueError, "prediction changed"):
            rf3.postprocess(job, result, self.root / "raw")

    def test_postprocess_preserves_all_samples_while_exposing_selected_candidate(self):
        job = self.job()
        out = self.root / "qa"
        result = self.adapter.predict(job, out)
        real_module = rf3._module
        selected = str(Path(result["structures"][3]).relative_to(out))
        audit_source = result["postprocess"]["sources"]["library/rf3_output.py"]
        def validate(destination, entry):
            value = {"status": "passed", "audit_source_sha256": audit_source,
                     "selected": {"files": {"model": {"path": selected}}},
                     "samples": [{"passed": index != 0} for index in range(5)]}
            (destination / "rf3-output-validation.json").write_text(json.dumps(value))
            return value
        def modules(path, name):
            if Path(path).name == "rf3_output.py":
                return types.SimpleNamespace(_select_and_publish=validate, POLICY="rf3-named-atoms-bonds-requested-stereo-v1")
            return real_module(path, name)
        with patch.object(rf3, "_module", side_effect=modules), patch.dict(sys.modules, {"torch": self.adapter.torch, "numpy": self.adapter.np}):
            verified = rf3.postprocess(job, result, out)
        self.assertEqual(verified["status"], "complete")
        self.assertEqual(verified["structures"], result["structures"])
        self.assertEqual(verified["selected_model"], str(out / selected))
        self.assertEqual(verified["raw_files"], result["raw_files"])

    def test_cpu_copy_wrapper_accepts_relative_worker_paths_and_preserves_original_inventory(self):
        job = self.job()
        source = self.root / "spool/token/out"
        raw = self.adapter.predict(job, source)
        raw["structures"] = [str(Path(path).relative_to(source)) for path in raw["structures"]]
        request = {"payload": job, "token": "execution"}
        (source.parent / "request.json").write_text(json.dumps(request))
        files = rf3._tree_inventory(source)
        envelope = {"status": "predicted", "error": None, "id": job["id"], "output_dir": str(source),
                    "request_sha256": rf3._json_digest(request), "files": files, "result": raw}
        prediction = self.root / "prediction.json"
        prediction.write_text(json.dumps(envelope))
        actual_module = rf3._module
        def select(destination, expected):
            structures, _ = rf3._raw_inventory(destination, expected["name"], 101, 5)
            value = {"status": "passed", "audit_source_sha256": raw["postprocess"]["sources"]["library/rf3_output.py"],
                     "selected": {"files": {"model": {"path": str(Path(structures[0]).relative_to(destination))}}}}
            (destination / "rf3-output-validation.json").write_text(json.dumps(value))
            return value
        def modules(path, name):
            if Path(path).name == "rf3_output.py":
                return types.SimpleNamespace(_select_and_publish=select, POLICY="rf3-named-atoms-bonds-requested-stereo-v1")
            return actual_module(path, name)
        import builtins
        original_import = builtins.__import__
        def no_native(name, *args, **kwargs):
            if name.split('.')[0] in {"torch", "numpy", "rdkit", "atomworks", "rf3"}:
                raise AssertionError("CPU wrapper imported a native/model dependency")
            return original_import(name, *args, **kwargs)
        with patch.object(rf3, "_module", side_effect=modules), patch.object(builtins, "__import__", side_effect=no_native):
            result = rf3.postprocess_receipt(prediction, self.root / "cpu")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(rf3._tree_inventory(source), files)
        self.assertTrue(all(Path(path).is_relative_to(self.root / "cpu/validated-output") for path in result["structures"]))
        self.assertEqual(result["execution_settings"], raw["settings"])
        self.assertEqual(result["prediction_receipt_sha256"], rf3.sha256(prediction))
        with self.assertRaisesRegex(ValueError, "already exists"):
            rf3.postprocess_receipt(prediction, self.root / "cpu")

    def test_output_nested_in_prepared_tree_is_refused(self):
        with self.assertRaisesRegex(ValueError, "separate"):
            self.adapter.predict(self.job(), self.entry.parent / "output")
        self.assertEqual(self.adapter.engine.results, [])


if __name__ == "__main__":
    unittest.main()
