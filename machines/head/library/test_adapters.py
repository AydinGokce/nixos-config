"""Bundle integrity, source closure and publication tests without GPU execution.

Native adapter lifecycle tests mock only the native build/parser callbacks. The
real per-model CPU parser validations are separate integration checks.
"""
import copy
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import adapters
import registry


class AdapterBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "registry"
        self.registry = registry.Registry(self.root)
        self.registry.init()
        self.registry.import_record({"kind": "construct", "id": "enzyme", "aliases": ["target"],
            "identity": {"molecule_type": "protein", "sequence": "ACDEFGHIK"}})

    def tearDown(self):
        self.tmp.cleanup()

    def compile(self, destination="bundle", ref="enzyme", **kwargs):
        return adapters.compile_input(self.root, ref, kwargs.pop("model", "boltz2"),
                                      self.base / destination, plain_fasta=kwargs.pop("plain_fasta", True), **kwargs)

    def write_rehashed(self, path, document):
        document["sha256"] = registry.digest_json(document)
        path.write_text(json.dumps(document))

    def modified_oligo(self):
        source = self.base / "supplier.txt"
        source.write_bytes(b"Original supplier notation: unresolved-custom-base\r\n")
        self.registry.import_record({"kind": "monomer", "id": "base",
            "identity": {"description": "Unresolved incorporated group"}}, {"supplier.txt": source})
        self.registry.import_record({"kind": "monomer", "id": "modified",
            "identity": {"precursor": {"monomer_ref": "base"}}})
        self.registry.import_record({"kind": "construct", "id": "oligo",
            "identity": {"molecule_type": "rna", "sequence": "ACGU",
                         "modifications": [{"position": 2, "monomer_ref": "modified"}]}})
        return self.registry.snapshot("oligo")

    def test_compile_plain_fasta_binds_source_and_all_files(self):
        result = self.compile(msa_backend="private")
        bundle = Path(result["bundle"])
        self.assertEqual((bundle / "input.fasta").read_text(), ">construct\nACDEFGHIK\n")
        manifest = adapters.validate_bundle(bundle, "boltz2", result["sha256"])
        self.assertEqual(manifest["msa_backend"], "private")
        self.assertEqual(manifest["source_ref"], "construct:enzyme@1")
        self.assertEqual(set(manifest["files"]), {"input.fasta", "source.json", "preflight.json",
                         "assets/construct/enzyme/1/attachments/description.md"})
        self.assertEqual((bundle/"assets/construct/enzyme/1/attachments/description.md").read_bytes(),
                         self.registry.attachment_path("enzyme", "attachments/description.md").read_bytes())
        self.assertEqual(manifest["files"], adapters.manifest_files(bundle))
        self.assertEqual(json.loads((bundle / "source.json").read_text()), self.registry.snapshot("enzyme"))
        self.assertTrue(result["preflight"]["canonical_single_protein"])
        self.assertFalse(result["preflight"]["native_parser"])
        self.assertIn("adapters.py", manifest["adapter_sources"])

    def test_sequence_models_automatically_use_single_protein_path(self):
        for model in ["esm", "evolvepro"]:
            with self.subTest(model=model):
                result = self.compile(destination=model, model=model, plain_fasta=False)
                self.assertEqual(result["format"], "protein-fasta")

    def test_fasta_reports_explicit_mapping_for_single_chain_assembly(self):
        self.registry.import_record({"kind": "assembly", "id": "single-chain",
            "identity": {"components": [{"chain_id": "Z", "construct_ref": "enzyme"}], "bonds": []}})
        result = self.compile(ref="single-chain")
        document = adapters.validate_bundle(result["bundle"], "boltz2")
        self.assertEqual(document["expected_chains"], ["A"])
        self.assertEqual(document["chain_mapping"], {"A": "Z"})

    def test_plain_fasta_never_simplifies_oligo_modifications_or_multichain(self):
        self.modified_oligo()
        with self.assertRaises(ValueError):
            self.compile(ref="oligo")
        self.registry.import_record({"kind": "assembly", "id": "complex",
            "identity": {"components": [{"chain_id": "A", "construct_ref": "enzyme"},
                                        {"chain_id": "B", "construct_ref": "enzyme"}]}})
        with self.assertRaises(ValueError):
            self.compile(ref="complex")
        self.assertFalse((self.base / "bundle").exists())
        self.assertFalse(list(self.base.glob(".native-input-*")))

    def test_private_native_input_is_rejected_without_public_fallback(self):
        with patch.object(adapters.importlib, "import_module") as imported:
            with self.assertRaisesRegex(ValueError, "No fallback"):
                self.compile(plain_fasta=False, msa_backend="private")
        imported.assert_not_called()
        self.assertFalse((self.base / "bundle").exists())

    def test_native_callback_lifecycle_gets_full_monomer_attachment_closure(self):
        snapshot = self.modified_oligo()
        calls = []
        def build(received, destination, assets, options):
            self.assertEqual(received, snapshot)
            source = assets["monomer:base@1"]["attachments/supplier.txt"]
            self.assertEqual(source.read_bytes(), (self.base / "supplier.txt").read_bytes())
            self.assertTrue(source.is_relative_to(destination))
            self.assertEqual(options, {"msa_backend": "public"})
            (destination / "native.json").write_text('{"mock_parser_input":true}\n')
            calls.append("build")
            return {"entrypoint": "native.json", "format": "mock-native", "has_protein": False,
                    "expected_chains": ["A"], "model_version": "test-only"}
        def preflight(destination, metadata):
            self.assertTrue((destination / metadata["entrypoint"]).is_file())
            calls.append("preflight")
            return {"native_parser": True, "model_inference": False, "msa_queries": False,
                    "test_fixture": "Mock lifecycle callback; not native model proof"}
        with patch.object(adapters.importlib, "import_module", return_value=SimpleNamespace(build=build, preflight=preflight)):
            result = self.compile(ref="oligo", plain_fasta=False)
        self.assertEqual(calls, ["build", "preflight"])
        document = adapters.validate_bundle(result["bundle"], "boltz2", result["sha256"])
        self.assertIn("assets/monomer/base/1/attachments/supplier.txt", document["files"])
        self.assertEqual(document["source_snapshot_sha256"], snapshot["sha256"])

    def test_failed_native_preflight_leaves_no_published_or_staged_bundle(self):
        def build(snapshot, destination, assets, options):
            (destination / "input.json").write_text("{}")
            return {"entrypoint": "input.json", "format": "mock"}
        for behavior in [ValueError("mock parser failed"), {"native_parser": False}]:
            def preflight(*_, behavior=behavior):
                if isinstance(behavior, Exception):
                    raise behavior
                return behavior
            with self.subTest(behavior=behavior), patch.object(adapters.importlib, "import_module", return_value=SimpleNamespace(build=build, preflight=preflight)):
                with self.assertRaises(ValueError):
                    self.compile(plain_fasta=False)
            self.assertFalse((self.base / "bundle").exists())
            self.assertFalse(list(self.base.glob(".native-input-*")))

    def test_compile_does_not_replace_nonempty_existing_destination(self):
        destination = self.base / "bundle"
        destination.mkdir()
        source = destination / "owned.txt"
        source.write_text("unchanged user file")
        with self.assertRaisesRegex(ValueError, "absent or empty"):
            self.compile()
        self.assertEqual(source.read_text(), "unchanged user file")

    def test_snapshot_rejects_missing_extra_or_replaced_monomer_closure(self):
        snapshot = self.modified_oligo()
        adapters.verify_snapshot(snapshot)
        for edit in ["missing", "unrelated", "wrong-reference"]:
            with self.subTest(edit=edit):
                changed = copy.deepcopy(snapshot)
                if edit == "missing":
                    del changed["monomers"]["monomer:base@1"]
                elif edit == "unrelated":
                    extra = self.registry.import_record({"kind": "monomer", "id": "unused", "identity": {"ccd": "PSU"}})
                    changed["monomers"][registry.reference(extra)] = extra
                else:
                    changed["monomers"]["monomer:base@8"] = changed["monomers"].pop("monomer:base@1")
                changed["sha256"] = registry.digest_json(changed)
                with self.assertRaises(ValueError):
                    adapters.verify_snapshot(changed)

    def test_snapshot_rejects_assembly_resolution_changed_under_a_valid_hash(self):
        self.registry.import_record({"kind": "assembly", "id": "complex", "identity": {
            "components": [{"chain_id": "A", "construct_ref": "enzyme"},
                           {"chain_id": "B", "construct_ref": "enzyme"}], "bonds": []}})
        original = self.registry.snapshot("complex")
        for field in ["components", "bonds"]:
            changed = copy.deepcopy(original)
            if field == "components":
                changed["components"][0]["chain_id"] = "Z"
            else:
                changed["bonds"] = [{"from": {"chain_id": "A", "position": 2, "atom": "SG"},
                                     "to": {"chain_id": "B", "position": 2, "atom": "SG"}, "order": 1}]
            changed["sha256"] = registry.digest_json(changed)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "pinned source"):
                adapters.verify_snapshot(changed)

    def test_snapshot_rejects_source_ref_provenance_and_boolean_schema(self):
        original = self.registry.snapshot("enzyme")
        for field, value in [("source_ref", "construct:other@1"), ("provenance", {}), ("schema", True)]:
            changed = copy.deepcopy(original)
            changed[field] = value
            changed["sha256"] = registry.digest_json(changed)
            with self.subTest(field=field), self.assertRaises(ValueError):
                adapters.verify_snapshot(changed)

    def test_manifest_detects_modified_added_and_deleted_files(self):
        for change in ["modified", "added", "deleted"]:
            with self.subTest(change=change):
                result = self.compile(destination=change)
                bundle = Path(result["bundle"])
                if change == "modified":
                    (bundle / "input.fasta").write_text(">changed\nACD\n")
                elif change == "added":
                    (bundle / "extra.json").write_text("{}")
                else:
                    (bundle / "preflight.json").unlink()
                with self.assertRaisesRegex(ValueError, "inventory/checksums"):
                    adapters.validate_bundle(bundle, "boltz2", result["sha256"])

    def test_model_and_expected_digest_bind_the_submission(self):
        result = self.compile()
        with self.assertRaisesRegex(ValueError, "different model"):
            adapters.validate_bundle(result["bundle"], "protenix")
        with self.assertRaisesRegex(ValueError, "submitted snapshot"):
            adapters.validate_bundle(result["bundle"], "boltz2", "0" * 64)

    def test_repacked_content_cannot_pass_the_original_submitted_digest(self):
        result = self.compile()
        bundle = Path(result["bundle"])
        (bundle / "input.fasta").write_text(">substituted\nYYYY\n")
        document = registry.load_json(bundle / "bundle.json")
        document["files"] = adapters.manifest_files(bundle)
        self.write_rehashed(bundle / "bundle.json", document)
        with self.assertRaisesRegex(ValueError, "submitted snapshot"):
            adapters.validate_bundle(bundle, "boltz2", result["sha256"])

    def test_bundle_rejects_unbound_entrypoint_and_boolean_schema(self):
        for field, value in [("entrypoint", "../outside"), ("schema", True)]:
            result = self.compile(destination=field)
            bundle = Path(result["bundle"])
            document = registry.load_json(bundle / "bundle.json")
            document[field] = value
            self.write_rehashed(bundle / "bundle.json", document)
            with self.subTest(field=field), self.assertRaises(ValueError):
                adapters.validate_bundle(bundle, "boltz2")

    def test_bundle_and_output_symlinks_are_rejected(self):
        result = self.compile()
        bundle = Path(result["bundle"])
        input_file = bundle / "input.fasta"
        original = self.base / "original.fasta"
        input_file.rename(original)
        input_file.symlink_to(original)
        with self.assertRaisesRegex(ValueError, "Symlinks"):
            adapters.validate_bundle(bundle, "boltz2")
        link = self.base / "linked-output"
        link.symlink_to(self.base / "elsewhere")
        with self.assertRaisesRegex(ValueError, "Symlinks"):
            self.compile(destination="linked-output")

    def test_materialize_preserves_every_file_and_manifest_hash(self):
        result = self.compile()
        runtime = self.base / "runtime"
        entrypoint = adapters.materialize(result["bundle"], "boltz2", runtime, result["sha256"])
        self.assertEqual(entrypoint, runtime / "input.fasta")
        self.assertEqual(adapters.manifest_files(runtime), adapters.manifest_files(Path(result["bundle"])))
        self.assertEqual(adapters.validate_bundle(runtime, "boltz2")["sha256"], result["sha256"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            adapters.materialize(result["bundle"], "boltz2", runtime, result["sha256"])

    def test_changed_copy_fails_revalidation_before_materialize_publication(self):
        result = self.compile()
        original_copy = shutil.copytree
        def corrupt_copy(source, destination, *args, **kwargs):
            value = original_copy(source, destination, *args, **kwargs)
            # copytree recursively calls itself for the bound purpose assets.
            if (Path(destination)/"input.fasta").exists():
                (Path(destination) / "input.fasta").write_text(">copy changed\nACD\n")
            return value
        runtime = self.base / "runtime"
        with patch.object(adapters.shutil, "copytree", side_effect=corrupt_copy):
            with self.assertRaisesRegex(ValueError, "inventory/checksums"):
                adapters.materialize(result["bundle"], "boltz2", runtime, result["sha256"])
        self.assertFalse(runtime.exists())
        self.assertFalse(list(self.base.glob(".native-copy-*")))

    def test_source_binding_rejects_an_independently_valid_other_snapshot(self):
        result = self.compile()
        bundle = Path(result["bundle"])
        self.registry.revise("enzyme", {"identity": {"molecule_type": "protein", "sequence": "YYYY"}})
        snapshot = self.registry.snapshot("enzyme")
        (bundle / "source.json").write_text(json.dumps(snapshot))
        document = registry.load_json(bundle / "bundle.json")
        document["files"] = adapters.manifest_files(bundle)
        self.write_rehashed(bundle / "bundle.json", document)
        with self.assertRaisesRegex(ValueError, "source binding"):
            adapters.validate_bundle(bundle, "boltz2")

    def test_attachment_changed_during_compile_fails_before_publication(self):
        source = self.base / "notes.txt"
        source.write_text("bound original")
        self.registry.revise("enzyme", {}, {"notes.txt": source})
        original_copy = shutil.copyfile
        def corrupt_copy(source, destination, **kwargs):
            result = original_copy(source, destination, **kwargs)
            Path(destination).write_text("changed while copying")
            return result
        with patch.object(adapters.shutil, "copyfile", side_effect=corrupt_copy):
            with self.assertRaisesRegex(ValueError, "Attachment changed"):
                self.compile()
        self.assertFalse((self.base / "bundle").exists())


if __name__ == "__main__":
    unittest.main()
