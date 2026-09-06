"""Control-state fixtures; GPU numerical equivalence is a separate live gate."""
from copy import deepcopy
from functools import cached_property
import importlib
import json
from pathlib import Path
import random
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters._common import AdapterBase, equivalent
from adapters.protenix import Adapter as Protenix
from adapters.openfold3 import Adapter as OpenFold
from adapters.boltz2 import Adapter as Boltz


class Config(dict):
    def __getattr__(self, key):
        value = self[key]
        return Config(value) if isinstance(value, dict) else value

    def __setattr__(self, key, value):
        self[key] = value

    def to_dict(self):
        return deepcopy(dict(self))


class AdapterStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def input(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return path

    def test_imports_do_not_load_native_model_packages(self):
        # This suite runs with ordinary stdlib Python; optional native packages
        # must not be required by dispatcher import or constructor validation.
        self.assertNotIn("runner.inference", sys.modules)
        self.assertNotIn("boltz.main", sys.modules)
        self.assertNotIn("openfold3.entry_points.experiment_runner", sys.modules)

    def test_scientific_override_rejected_before_job_state_changes(self):
        config = {"model": "protenix", "native_config": {"dump_dir": "old", "dtype": "bf16"}}
        adapter = Protenix(config)
        adapter.loaded = True
        entry = self.input("input.json", [])
        bad = {"id": "bad", "seeds": [101], "native_input": str(entry),
               "native_config": {"dump_dir": "new", "dtype": "fp32"}}
        with self.assertRaisesRegex(ValueError, "pinned native settings"):
            with adapter.job(bad, self.root / "bad"):
                self.fail("Rejected job reached inference")
        self.assertFalse((self.root / "bad").exists())
        self.assertEqual(adapter._jobs, set())
        good = dict(bad, id="good", native_config={"dump_dir": "new", "dtype": "bf16"})
        with adapter.job(good, self.root / "good"):
            pass
        with self.assertRaisesRegex(ValueError, "already attempted"):
            with adapter.job(good, self.root / "another"):
                pass

    def protenix(self):
        native = dict(input_json_path="unused", dump_dir="initial", use_msa=True,
                      sample_diffusion={"N_sample": 1}, need_atom_confidence=False,
                      sorted_by_ranking_score=True, skip_amp={"large": False})
        adapter = Protenix({"model": "protenix", "native_config": native})
        adapter.ConfigDict = Config
        adapter.need_msa_search = lambda q: False
        runner = SimpleNamespace(configs=Config(native), model=SimpleNamespace(configs=Config(native)),
                                 dump_dir="initial", error_dir="initial/ERR", dumper="initial-dumper")
        def basics():
            runner.dump_dir = runner.configs.dump_dir
            runner.error_dir = str(Path(runner.dump_dir) / "ERR")
        runner.init_basics = basics
        runner.init_dumper = lambda **kw: setattr(runner, "dumper", object())
        adapter.seen = []
        def infer(active, config):
            self.assertIs(active, runner)
            self.assertFalse(config.skip_amp.large)
            query = json.loads(Path(config.input_json_path).read_text())[0]
            adapter.seen.append(query["name"])
            # Simulate the real native token-dependent config mutation and a
            # consumed/mutated feature object, including a failed middle job.
            config["skip_amp"]["large"] = True
            if query["name"] == "fail":
                raise RuntimeError("native failure")
            for seed in config.seeds:
                random.seed(seed)
                value = random.random() + query["value"]
                target = Path(config.dump_dir) / query["name"] / f"seed_{seed}" / "predictions" / "sample.cif"
                target.parent.mkdir(parents=True)
                target.write_text(str(value))
        adapter.infer_predict, adapter.runner, adapter.loaded = infer, runner, True
        return adapter

    def test_protenix_isolated_and_resident_jobs_match_and_failure_restores_config(self):
        resident = self.protenix()
        original_config = resident.runner.configs
        original_model_config = resident.runner.model.configs
        for number, name in enumerate(("first", "fail", "second")):
            entry = self.input(f"{name}.json", [{"name": name, "value": number}])
            job = {"id": name, "seeds": [101], "native_input": str(entry)}
            if name == "fail":
                with self.assertRaisesRegex(RuntimeError, "native failure"):
                    resident.predict(job, self.root / "resident-fail")
            else:
                one = resident.predict(job, self.root / f"resident-{name}")
                isolated = self.protenix().predict(job, self.root / f"isolated-{name}")
                self.assertEqual(Path(one["structures"][0]).read_text(), Path(isolated["structures"][0]).read_text())
            self.assertIs(resident.runner.configs, original_config)
            self.assertIs(resident.runner.model.configs, original_model_config)
            self.assertEqual(resident.runner.dumper, "initial-dumper")
            self.assertFalse(resident.base["skip_amp"]["large"])
        self.assertEqual(resident.seen, ["first", "fail", "second"])

    def test_openfold_creates_new_cached_data_and_writer_objects_each_job(self):
        native = {"experiment_settings": {"use_msa_server": False, "use_templates": False,
                    "skip_existing": False, "seeds": [42], "output_dir": "old", "log_dir": "old"}}
        adapter = OpenFold({"model": "openfold3", "native_config": native, "model_config": {}})
        instances = []
        class Settings(Config):
            def model_dump(self, **kwargs):
                return self.to_dict()
        class Runner:
            def __init__(self, config):
                self.experiment_config = config
                self.out = Path(config["experiment_settings"]["output_dir"])
                self.log_dir = self.out / "logs"
                self.num_diffusion_samples = 1
                instances.append(self)
            @cached_property
            def data(self):
                return self.query
            def _log_experiment_config(self):
                pass
            def _log_model_config(self):
                pass
            def run(self, query):
                self.query = query
                self.out.mkdir(parents=True, exist_ok=True)
                (self.out / "summary.txt").write_text("Successful Queries: 1\nFailed Queries: 0\n")
                (self.out / "sample_model.cif").write_text(str(self.data.queries))
            def cleanup(self):
                pass
        adapter.Runner, adapter.ConfigDict = Runner, Config
        adapter.ExperimentConfig = SimpleNamespace(model_validate=lambda value: Settings(value))
        adapter.QuerySet = SimpleNamespace(from_json=lambda path: SimpleNamespace(queries=json.loads(path.read_text())))
        adapter.module = SimpleNamespace(log_dir="original")
        adapter.initial_rng, adapter.torch, adapter.np, adapter.loaded = {}, None, None, True
        with patch("adapters.openfold3.restore_rng"), patch("adapters.openfold3.resident_strategy"):
            for name in ("one", "two"):
                entry = self.input(name + ".json", {name: {}})
                result = adapter.predict({"id": name, "seeds": [42], "native_input": str(entry)}, self.root / name)
                self.assertIn(name, Path(result["structures"][0]).read_text())
                self.assertEqual(adapter.module.log_dir, "original")
        self.assertEqual(len(instances), 2)
        self.assertIsNot(instances[0].data, instances[1].data)
        self.assertIs(instances[0].lightning_module, instances[1].lightning_module)

    def test_boltz_different_seed_rejected_before_native_preprocessing(self):
        adapter = Boltz({"model": "boltz2", "native_config": {"seed": 42}})
        adapter.loaded = True
        entry = self.input("boltz.yaml", {})
        with self.assertRaisesRegex(ValueError, "select another config_id"):
            adapter.predict({"id": "wrong-seed", "seeds": [43], "native_input": str(entry)}, self.root / "boltz")


if __name__ == "__main__":
    unittest.main()
