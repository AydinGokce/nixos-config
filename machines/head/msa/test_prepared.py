"""Preparation integrity and semantic regression checks; no network or GPU."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("prepared", Path(__file__).with_name("prepared.py"))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

QUERY = "ACDE"
MSA = ">query\nACDE\n>hit species=42\nAcC-E\n>other species=77\n-CDE\n"


class PreparedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def protenix(self, name="run", msa=MSA, paired=None, copies=1):
        run = self.root / name
        run.mkdir()
        folder = run / "protenix/msa"
        folder.mkdir(parents=True)
        path = folder / "unpaired.a3m"
        path.write_text(msa)
        protein = {"sequence": QUERY, "count": copies, "unpairedMsaPath": str(path)}
        if paired is not None:
            paired_path = folder / "paired.a3m"
            paired_path.write_text(paired)
            protein["pairedMsaPath"] = str(paired_path)
        p.write_json(run / "input-update-msa.json", [{"name": "query", "sequences": [{"proteinChain": protein}]}])
        return run

    def bundle(self, name="bundle", **kw):
        run = self.protenix(name=name + "-run", **kw)
        dest = self.root / name
        p.capture("protenix", run, dest, endpoint="https://api.colabfold.com")
        return dest

    def test_relocation_preserves_alignment_bytes_headers_and_insertions(self):
        bundle = self.bundle(paired=MSA)
        native = p.materialize(bundle, "protenix", self.root / "relocated")
        chain = p.load_json(native)[0]["sequences"][0]["proteinChain"]
        for field in ["unpairedMsaPath", "pairedMsaPath"]:
            self.assertEqual(Path(chain[field]).read_text(), MSA)
            self.assertTrue(Path(chain[field]).is_relative_to(self.root / "relocated"))
        report = p.compare(bundle, bundle)
        self.assertTrue(report["preparation_equivalent"])
        alignment = report["left"]["alignments"][0]["summary"]
        self.assertEqual(alignment["rows"], 3)
        self.assertEqual(alignment["insertion_count"], 1)
        self.assertEqual(alignment["gap_count"], 2)
        self.assertEqual(report["accuracy_benchmark"]["status"], "not_run")

    def test_materialized_validation_preserves_original_worker_paths_after_fetch(self):
        bundle = self.bundle(paired=MSA)
        original = self.root / "worker-out"
        p.materialize(bundle, "protenix", original)
        self.assertEqual(p.validate_materialized(original, "protenix"), p.validate(bundle))
        fetched = self.root / "fetched"
        shutil.copytree(original, fetched)
        before = {str(f.relative_to(fetched)): f.read_bytes() for f in fetched.rglob("*") if f.is_file()}
        p.validate_materialized(fetched, "protenix", original_out=original)
        after = {str(f.relative_to(fetched)): f.read_bytes() for f in fetched.rglob("*") if f.is_file()}
        self.assertEqual(before, after)
        with self.assertRaisesRegex(p.Error, "native document mismatch"):
            p.validate_materialized(fetched, "protenix")
        with self.assertRaisesRegex(p.Error, "must be absolute"):
            p.validate_materialized(fetched, "protenix", original_out="relative")

    def test_materialized_validation_rejects_asset_input_and_runtime_changes(self):
        bundle = self.bundle()
        original = self.root / "worker-out"
        p.materialize(bundle, "protenix", original)
        manifest = p.validate_materialized(original, "protenix")
        asset = original / next(iter(manifest["files"]))
        saved = asset.read_bytes()
        asset.write_bytes(saved + b"\n")
        with self.assertRaisesRegex(p.Error, "integrity mismatch"):
            p.validate_materialized(original, "protenix")
        asset.write_bytes(saved)
        for filename in ["input.json", "runtime.json"]:
            target = original / filename
            before = target.read_bytes()
            changed = p.load_json(target)
            if isinstance(changed, list):
                changed[0]["unexpected_field"] = {"seed": 17}
            else:
                changed["unexpected_field"] = {"seed": 17}
            p.write_json(target, changed)
            with self.assertRaisesRegex(p.Error, "native document mismatch"):
                p.validate_materialized(original, "protenix")
            target.write_bytes(before)
        actual = p.load_json(original / "input.json")
        actual[0]["sequences"][0]["proteinChain"]["count"] = True
        p.write_json(original / "input.json", actual)
        with self.assertRaisesRegex(p.Error, "native document mismatch"):
            p.validate_materialized(original, "protenix")

    def test_materialized_validation_rechecks_native_semantics_and_chain_identity(self):
        bundle = self.bundle()
        original = self.root / "worker-out"
        p.materialize(bundle, "protenix", original)
        fasta = self.root / "wrong.fasta"
        fasta.write_text(">query\nACDF\n")
        with self.assertRaisesRegex(p.Error, "FASTA chain"):
            p.validate_materialized(original, "protenix", fasta=fasta)
        with self.assertRaisesRegex(p.Error, "model mismatch"):
            p.validate_materialized(original, "boltz2")
        manifest = p.load_json(original / "source_manifest.json")
        rel = next(rel for rel, entry in manifest["files"].items() if "alignment" in entry)
        manifest["files"][rel]["alignment"]["rows"] += 1
        p.write_json(original / "source_manifest.json", manifest)
        with self.assertRaisesRegex(p.Error, "Alignment semantics changed"):
            p.validate_materialized(original, "protenix")

    def test_public_private_same_native_preparation_can_compare_equal(self):
        left = self.bundle()
        right = self.root / "private"
        p.capture("protenix", self.root / "bundle-run", right, source="private", endpoint="http://127.0.0.1:8080")
        self.assertTrue(p.compare(left, right)["preparation_equivalent"])

    def test_database_receipt_is_bound_and_retained(self):
        run = self.protenix()
        receipt = self.root / "database-receipt.json"
        p.write_json(receipt, {"namespace": "snapshot123", "database": {"mode": "full"}})
        bundle = self.root / "private"
        p.capture("protenix", run, bundle, source="private", database_provenance=receipt)
        manifest = p.validate(bundle)
        self.assertEqual(manifest["source"]["parity_status"], "unproven")
        stored = bundle / manifest["source"]["database_provenance"]
        self.assertEqual(stored.read_bytes(), receipt.read_bytes())
        stored.write_text('{}')
        with self.assertRaisesRegex(p.Error, "integrity mismatch"):
            p.validate(bundle)

    def test_row_order_change_is_reported(self):
        left = self.bundle("left")
        right = self.bundle("right", msa=">query\nACDE\n>other species=77\n-CDE\n>hit species=42\nAcC-E\n")
        self.assertFalse(p.compare(left, right)["checks"]["alignments"])

    def test_header_mapping_change_is_reported(self):
        left = self.bundle("left")
        right = self.bundle("right", msa=MSA.replace("species=42", "species=43"))
        result = p.compare(left, right)
        self.assertFalse(result["preparation_equivalent"])
        a = result["left"]["alignments"][0]["summary"]
        b = result["right"]["alignments"][0]["summary"]
        self.assertEqual(a["ordered_sequences_sha256"], b["ordered_sequences_sha256"])
        self.assertNotEqual(a["headers_or_pair_keys_sha256"], b["headers_or_pair_keys_sha256"])

    def test_insertion_counts_are_not_normalized_away(self):
        left = self.bundle("left")
        right = self.bundle("right", msa=MSA.replace("AcC-E", "AccC-E"))
        a = p.compare(left, right)
        self.assertFalse(a["preparation_equivalent"])
        self.assertEqual(a["right"]["alignments"][0]["summary"]["insertion_count"], 2)

    def test_corrupted_asset_fails_before_materialization(self):
        bundle = self.bundle()
        manifest = p.load_json(bundle / "manifest.json")
        asset = bundle / next(iter(manifest["files"]))
        asset.write_text(MSA.replace("ACDE", "ACDF"))
        with self.assertRaisesRegex(p.Error, "integrity mismatch"):
            p.materialize(bundle, "protenix", self.root / "output")
        self.assertFalse((self.root / "output").exists())

    def test_missing_alignment_rejected_without_query_only_fallback(self):
        run = self.protenix()
        doc = p.load_json(run / "input-update-msa.json")
        del doc[0]["sequences"][0]["proteinChain"]["unpairedMsaPath"]
        p.write_json(run / "input-update-msa.json", doc)
        with self.assertRaisesRegex(p.Error, "Missing prepared MSA"):
            p.capture("protenix", run, self.root / "bundle")

    def test_wrong_query_or_row_width_rejected(self):
        for idx, text in enumerate([MSA.replace("ACDE", "ACDF"), MSA.replace("AcC-E", "AcC-DE")]):
            run = self.protenix(str(idx), text)
            with self.assertRaises(p.Error):
                p.capture("protenix", run, self.root / f"bundle{idx}")

    def test_boltz_rejects_native_unsupported_wrapping_and_dot_tokens(self):
        path = self.root / "input.a3m"
        for raw in [">query\nAC\nDE\n", ">query\nACDE\n>other\nA.DE\n"]:
            path.write_text(raw)
            with self.assertRaises(p.Error):
                p.text_alignment_summary(path, QUERY, "boltz2")

    def test_version_model_and_fasta_binding_rejected(self):
        bundle = self.bundle(copies=2)
        fasta = self.root / "input.fasta"
        fasta.write_text(">first\nACDE\n>second\nACDF\n")
        with self.assertRaisesRegex(p.Error, "FASTA chain"):
            p.validate(bundle, "protenix", fasta)
        with self.assertRaisesRegex(p.Error, "model mismatch"):
            p.validate(bundle, "boltz2")
        data = p.load_json(bundle / "manifest.json")
        data["client_version"] = "future"
        p.write_json(bundle / "manifest.json", data)
        with self.assertRaisesRegex(p.Error, "version mismatch"):
            p.validate(bundle)

    def test_traversal_and_unbound_native_path_rejected(self):
        bundle = self.bundle()
        data = p.load_json(bundle / "manifest.json")
        pointer = data["bindings"][0]["pointer"]
        for value in ["/outside/msa.a3m", "bundle://../outside.a3m"]:
            changed = copy.deepcopy(data)
            p.set_at(changed["native_input"], pointer, value)
            p.write_json(bundle / "manifest.json", changed)
            with self.assertRaises(p.Error):
                p.validate(bundle)

    def test_symlink_asset_rejected(self):
        bundle = self.bundle()
        data = p.load_json(bundle / "manifest.json")
        path = bundle / next(iter(data["files"]))
        outside = self.root / "outside.a3m"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(p.Error, "symlink"):
            p.validate(bundle)

    def test_templates_never_silently_discarded(self):
        run = self.protenix()
        doc = p.load_json(run / "input-update-msa.json")
        doc[0]["sequences"][0]["proteinChain"]["templatesPath"] = "template.hhr"
        p.write_json(run / "input-update-msa.json", doc)
        with self.assertRaisesRegex(p.Error, "Template inputs conflict"):
            p.capture("protenix", run, self.root / "bundle")

    def test_boltz_native_csv_pair_keys_and_duplicates_preserved(self):
        run = self.root / "boltz"
        msa = run / "boltz_results_input/msa"
        records = run / "boltz_results_input/processed/records"
        msa.mkdir(parents=True)
        records.mkdir(parents=True)
        p.write_json(run / "input.yaml", {"version": 1, "sequences": [{"protein": {"id": "A", "sequence": QUERY}}]})
        p.write_json(records / "input.json", {"chains": [{"chain_name": "A", "entity_id": 0}]})
        raw = "key,sequence\n0,ACDE\n1,AcC-E\n-1,ACDE\n-1,-CDE\n"
        (msa / "input_0.csv").write_text(raw)
        dest = self.root / "boltz-bundle"
        p.capture("boltz2", run, dest)
        native = p.load_json(p.materialize(dest, "boltz2", self.root / "replay"))
        self.assertEqual(Path(native["sequences"][0]["protein"]["msa"]).read_text(), raw)
        summary = p.compare(dest, dest)["left"]["alignments"][0]["summary"]
        self.assertEqual(summary["pair_keys"], ["0", "1", "-1", "-1"])
        self.assertEqual(summary["rows"], 4)

    def test_template_chain_mapping_and_unknown_coordinates_prevent_equivalence(self):
        run = self.root / "boltz"
        records = run / "boltz_results_input/processed/records"
        records.mkdir(parents=True)
        msa = run / "input.csv"
        msa.write_text("key,sequence\n-1,ACDE\n")
        template = run / "1abc.cif"
        template.write_text("placeholder coordinate bytes for manifest fixture")
        doc = {"version": 1, "sequences": [{"protein": {"id": "A", "sequence": QUERY, "msa": str(msa)}}],
               "templates": [{"cif": str(template), "chain_id": "A"}]}
        p.write_json(run / "input.yaml", doc)
        p.write_json(records / "input.json", {"chains": [{"chain_name": "A", "entity_id": 0}]})
        with patch.object(p, "coordinate_summary", return_value={"coordinate_sha256": None}):
            p.capture("boltz2", run, self.root / "unknown")
        self.assertFalse(p.compare(self.root / "unknown", self.root / "unknown")["preparation_equivalent"])
        with patch.object(p, "coordinate_summary", return_value={"coordinate_sha256": "fixture-hash", "atoms": 1}):
            p.capture("boltz2", run, self.root / "left")
            doc["templates"][0]["chain_id"] = "B"
            p.write_json(run / "input.yaml", doc)
            p.capture("boltz2", run, self.root / "right")
        result = p.compare(self.root / "left", self.root / "right")
        self.assertTrue(result["checks"]["coordinate_files"])
        self.assertFalse(result["checks"]["template_mappings"])

    def test_of3_missing_template_cache_fails_closed(self):
        run = self.root / "of3"
        run.mkdir()
        doc = {"queries": {"query": {"chains": [{"molecule_type": "protein", "chain_ids": ["A"],
              "sequence": QUERY, "main_msa_file_paths": ["missing.npz"],
              "template_entry_chain_ids": ["1abc_A"], "template_alignment_file_path": None}]}}}
        p.write_json(run / "inference_query_set.json", doc)
        p.write_json(run / "experiment_config.json", {"experiment_settings": {"use_templates": True},
                      "template_preprocessor_settings": {"output_directory": "missing", "structure_directory": "missing", "cache_directory": "missing"}})
        with self.assertRaisesRegex(p.Error, "template IDs require"):
            p.capture("openfold3", run, self.root / "of3-bundle", trust_native_npz=True)

    def test_of3_retention_merges_scientific_runner_settings(self):
        base = self.root / "runner.json"
        p.write_json(base, {"dataset_config_kwargs": {"template": {"n_templates": 8}},
                           "template_preprocessor_settings": {"max_release_date": "2020-01-01"}})
        config = p.openfold_runner_config(self.root / "output", base=base)
        self.assertEqual(config["dataset_config_kwargs"]["template"]["n_templates"], 8)
        self.assertEqual(config["template_preprocessor_settings"]["max_release_date"], "2020-01-01")
        self.assertFalse(config["msa_computation_settings"]["cleanup_msa_dir"])


if __name__ == "__main__":
    unittest.main()
