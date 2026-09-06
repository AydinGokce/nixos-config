"""Assembly fidelity tests; optional real CPU parser tests need the pinned env."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

import rfaa_adapter as a


SDF = b"""ethanol
  native-fixture

  3  2  0  0  0  0  0  0  0  0999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.5000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.5000    1.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0  0  0  0
  2  3  1  0  0  0  0
M  END
$$$$
"""


def component(chain, molecule="protein", sequence="ACDEFGHIK", **identity):
    if molecule in a.ALPHABETS:
        identity["sequence"] = sequence
    identity["molecule_type"] = molecule
    return {"chain_id": chain, "construct_ref": f"construct:example-{chain}@1",
            "record": {"identity": identity, "attachments": []}}


def assembly(*components):
    return {"schema": 1, "kind": "resolved-assembly", "name": "fixture", "source_ref": "assembly:fixture@1",
            "sha256": "1"*64, "components": list(components), "bonds": [], "monomers": {}, "provenance": {}}


class Fixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.destination = self.root / "bundle"

    def tearDown(self):
        self.temp.cleanup()

    def sdf_component(self, chain="L", raw=SDF):
        source = self.root / (chain + ".sdf")
        source.write_bytes(raw)
        item = component(chain, "small_molecule", structure_format="sdf", structure_file="attachments/source.sdf")
        item["record"]["attachments"] = [{"path": "attachments/source.sdf", "bytes": len(raw),
                                             "sha256": hashlib.sha256(raw).hexdigest()}]
        return item, {item["construct_ref"]: {"attachments/source.sdf": source}}


class AdapterTests(Fixture, unittest.TestCase):
    def test_actual_single_sequence_preparation_without_full_receipt_is_accepted(self):
        metadata = a.build(assembly(component("P"), component("Q", sequence="LMNPQRSTV")), self.destination, {}, {})
        config, inputs = a.load_config(self.destination / metadata["entrypoint"])
        helper = Path(__file__).parents[1] / "rfaa/prepare.py"
        output = self.root / "output"
        for chain, kind, fasta in inputs:
            directory = output / "fixture" / chain
            subprocess.run([sys.executable, str(helper), "--fasta", fasta, "--out", str(directory),
                            "--mode", "single-seq"], check=True, capture_output=True)
            self.assertFalse((directory / "preparation.json").exists())
        result = a.verified_preparation(inputs, output, "fixture", "single-seq")
        self.assertEqual(set(result), {"P", "Q"})
        self.assertTrue(all(value["msa_sequences"] == 1 and value["templates_searched"] is False for value in result.values()))
        with self.assertRaisesRegex(a.UnsupportedInput, "selected mode"):
            a.verified_preparation(inputs, output, "fixture", "full")
        (output / "fixture/P/t000_.hhr").unlink()
        with self.assertRaisesRegex(a.UnsupportedInput, "incomplete"):
            a.verified_preparation(inputs, output, "fixture", "single-seq")

    def test_completed_full_receipt_and_exact_query_are_required(self):
        metadata = a.build(assembly(component("P")), self.destination, {}, {})
        config, inputs = a.load_config(self.destination / metadata["entrypoint"])
        helper = Path(__file__).parents[1] / "rfaa/prepare.py"
        output = self.root / "output"; directory = output / "fixture/P"
        subprocess.run([sys.executable, str(helper), "--fasta", inputs[0][2], "--out", str(directory),
                        "--mode", "single-seq"], check=True, capture_output=True)
        request_path = directory / ".prepare-request.json"
        original = json.loads(request_path.read_text())
        request = dict(original, mode="full"); request_path.write_text(json.dumps(request))
        with self.assertRaisesRegex(a.UnsupportedInput, "completed search receipt"):
            a.verified_preparation(inputs, output, "fixture", "full")
        (directory / "preparation.json").write_text(json.dumps(dict(request, templates_searched=False, msa_sequences=1)))
        with self.assertRaisesRegex(a.UnsupportedInput, "incomplete"):
            a.verified_preparation(inputs, output, "fixture", "full")
        request_path.write_text(json.dumps(original))
        (directory / "t000_.msa0.a3m").write_text(">query\nACDEFGHIL\n")
        with self.assertRaisesRegex(a.UnsupportedInput, "MSA query"):
            a.verified_preparation(inputs, output, "fixture", "single-seq")

    def test_mixed_assembly_retains_all_chains_sequence_identity_and_exact_sdf(self):
        ligand, assets = self.sdf_component()
        items = [component("d", "dna", "ACGT"), component("X"), ligand,
                 component("Y", sequence="LMNPQRSTV"), component("r", "rna", "ACGU"),
                 component("S", "small_molecule", smiles="C[C@H](O)F")]
        metadata = a.build(assembly(*items), self.destination, assets, {})
        config, inputs = a.load_config(self.destination / metadata["entrypoint"])
        self.assertEqual([x[0] for x in inputs], ["X", "Y", "d", "r", "L", "S"])
        self.assertEqual([x["chain_id"] for x in metadata["component_map"]], ["d", "X", "L", "Y", "r", "S"])
        self.assertEqual((self.destination / "ligands/L.sdf").read_bytes(), SDF)
        self.assertEqual((self.destination / "sequences/Y.fasta").read_text(), ">Y\nLMNPQRSTV\n")
        self.assertEqual(config["sm_inputs"]["S"]["input"], "C[C@H](O)F")
        self.assertTrue(metadata["has_protein"])

    def test_late_unsupported_chain_does_not_write_a_partial_bundle(self):
        bad = component("B"); bad["record"]["identity"]["modifications"] = [{"position": 2, "monomer_ref": "monomer:mse@1"}]
        with self.assertRaisesRegex(a.UnsupportedInput, "modifications"):
            a.build(assembly(component("A"), bad), self.destination, {}, {})
        self.assertFalse(self.destination.exists())

    def test_custom_backbone_termini_circular_ccd_and_unknown_semantics_rejected(self):
        for field, value in (("backbone", "peptoid"), ("termini", {"N": "acetyl"}), ("circular", True),
                             ("linkages", [{"position": 1, "monomer_ref": "monomer:phosphorothioate@1"}]),
                             ("unrecognized_chemistry", {})):
            with self.subTest(field=field):
                item = component("A"); item["record"]["identity"][field] = value
                with self.assertRaises(a.UnsupportedInput):
                    a.build(assembly(item), self.destination, {}, {})
        with self.assertRaisesRegex(a.UnsupportedInput, "CCD"):
            a.build(assembly(component("L", "small_molecule", ccd="ATP")), self.destination, {}, {})

    def test_explicit_empty_optional_fields_and_linear_default_are_safe(self):
        item = component("A")
        item["record"]["identity"].update(copy.deepcopy(a.EMPTY_SEMANTICS), circular=False)
        metadata = a.build(assembly(item), self.destination, {}, {"mode": "single-seq"})
        self.assertEqual(metadata["requested_search_mode"], "single-seq")

    def test_cross_type_duplicate_ids_and_noncanonical_polymers_rejected(self):
        for snap in (assembly(component("A"), component("A", "dna", "ACGT")),
                     assembly(component("AA")), assembly(component("A", sequence="ACDX")),
                     assembly(component("D", "dna", "ACGU")), assembly(component("R", "rna", "ACGT"))):
            with self.subTest(snapshot=snap):
                with self.assertRaises(a.UnsupportedInput):
                    a.build(snap, self.destination, {}, {})

    def test_pinned_asset_hash_mismatch_or_missing_asset_fails(self):
        ligand, assets = self.sdf_component()
        for supplied in ({}, assets):
            if supplied:
                next(iter(supplied.values()))["attachments/source.sdf"].write_bytes(SDF.replace(b"ethanol", b"changed"))
            with self.assertRaises(a.UnsupportedInput):
                a.build(assembly(ligand), self.destination, supplied, {})

    def test_native_reader_truncation_cases_are_rejected(self):
        for raw in (SDF + SDF, SDF.replace(b"V2000", b"V3000"), SDF.replace(b"M  END", b"M  BAD")):
            with self.subTest(raw=raw[:20]):
                ligand, assets = self.sdf_component(raw=raw)
                with self.assertRaises(a.UnsupportedInput):
                    a.build(assembly(ligand), self.destination, assets, {})
        for value in ("CCO molecule-title", "CCO\nCCN", "${oc.env:SECRET}"):
            with self.assertRaises(a.UnsupportedInput):
                a.build(assembly(component("L", "small_molecule", smiles=value)), self.destination, {}, {})

    def test_covalent_bond_and_unreviewed_run_options_fail_before_files_exist(self):
        snap = assembly(component("A"), component("B", "small_molecule", smiles="CCO"))
        snap["bonds"] = [{"from": {"chain_id": "A", "position": 1, "atom": "N"},
                          "to": {"chain_id": "B", "atom": 1}, "order": 1}]
        with self.assertRaisesRegex(a.UnsupportedInput, "covalent"):
            a.build(snap, self.destination, {}, {})
        with self.assertRaisesRegex(a.UnsupportedInput, "options"):
            a.build(assembly(component("A")), self.destination, {}, {"checkpoint_path": "other"})
        self.assertFalse(self.destination.exists())

    def test_runtime_refuses_escape_symlink_extra_native_parameters_and_multifasta(self):
        a.build(assembly(component("A")), self.destination, {}, {})
        entry = self.destination / "rfaa.json"
        original = json.loads(entry.read_text())
        for path in ("../outside.fasta", "/etc/passwd", "sequences/../../outside"):
            config = copy.deepcopy(original); config["protein_inputs"]["A"]["fasta_file"] = path
            entry.write_text(json.dumps(config))
            with self.assertRaises(a.UnsupportedInput): a.load_config(entry)
        entry.write_text(json.dumps(original))
        fasta = self.destination / "sequences/A.fasta"
        fasta.unlink(); fasta.symlink_to(self.root / "outside.fasta")
        (self.root / "outside.fasta").write_text(">A\nACD\n")
        with self.assertRaises(a.UnsupportedInput): a.load_config(entry)
        fasta.unlink(); fasta.write_text(">A\nACD\n>B\nACD\n")
        with self.assertRaises(a.UnsupportedInput): a.load_config(entry)
        config = copy.deepcopy(original); config["loader_params"] = {"n_templ": 0}
        entry.write_text(json.dumps(config))
        with self.assertRaises(a.UnsupportedInput): a.load_config(entry)


@unittest.skipUnless(os.environ.get("RFAA_NATIVE_TESTS") == "1", "requires the pinned RFAA CPU environment")
class NativeParserTests(Fixture, unittest.TestCase):
    # These are real native parser/feature tests; no weights or inference.
    def setUp(self):
        super().setUp()
        self.old_source = os.environ.get("RFAA_SOURCE_DIR")
        os.environ["RFAA_SOURCE_DIR"] = os.environ.get("RFAA_TEST_SOURCE", "/opt/bio/src/rfaa")
        a.source_directory()

    def tearDown(self):
        if self.old_source is None:
            os.environ.pop("RFAA_SOURCE_DIR", None)
        else:
            os.environ["RFAA_SOURCE_DIR"] = self.old_source
        super().tearDown()

    def test_real_dna_rna_and_neutral_chiral_smiles_features(self):
        metadata = a.build(assembly(component("A"), component("D", "dna", "ACGT"), component("R", "rna", "ACGU"),
                                    component("L", "small_molecule", smiles="NCC[C@@H](O)F")), self.destination, {}, {"msa_backend": "public"})
        result = a.preflight(self.destination, metadata)
        self.assertEqual([row.get("residues") for row in result["components"][:3]], [9, 4, 4])
        ligand = result["components"][-1]
        self.assertEqual(ligand["formal_charge"], 0)
        self.assertTrue(result["native_parser"])
        self.assertTrue(ligand["chiral_features"])
        self.assertEqual(len(result["output_chain_map"]), 4)
        self.assertEqual(result["configured_template_count"], 4)

    def test_real_sdf_and_smiles_retain_equivalent_heavy_atom_graphs(self):
        ligand, assets = self.sdf_component()
        metadata = a.build(assembly(ligand, component("S", "small_molecule", smiles="CCO")), self.destination, assets, {})
        result = a.preflight(self.destination, metadata)
        left, right = result["components"]
        for key in ("atomic_numbers", "atom_formal_charges", "bonds", "connected_components"):
            self.assertEqual(left[key], right[key])

    def test_disconnected_native_smiles_preserve_two_output_fragments(self):
        metadata = a.build(assembly(component("A"), component("Z", "small_molecule", smiles="CCO.CCN")), self.destination, {}, {})
        result = a.preflight(self.destination, metadata)
        self.assertEqual(result["components"][-1]["connected_components"], [3, 3])
        self.assertEqual([(x["output_chain_id"], x["input_chain_id"]) for x in result["output_chain_map"]],
                         [("A", "A"), ("B", "Z"), ("C", "Z")])

    def test_isotope_generic_element_and_oversize_ligand_are_rejected(self):
        for index, value in enumerate(("[13CH4]", "[Xe]", "C" * (a.MAX_PREFLIGHT_LIGAND_ATOMS + 1))):
            target = self.root / str(index)
            metadata = a.build(assembly(component("L", "small_molecule", smiles=value)), target, {}, {})
            with self.assertRaises(a.UnsupportedInput): a.preflight(target, metadata)

    def test_native_unsupported_annotations_fail_before_conformer_generation(self):
        from unittest.mock import patch
        examples = ("[CH3]", "[C]", "[O]", "[NH4+]", "[NH3+]CCC(=O)[O-]", "F/C=C/F", "F/C=C\\F", "[13CH4]")
        for index, value in enumerate(examples):
            target = self.root / str(index)
            metadata = a.build(assembly(component("L", "small_molecule", smiles=value)), target, {}, {})
            with patch("rf2aa.data.parsers.parse_mol", side_effect=AssertionError("native conformer must not run")):
                with self.subTest(smiles=value), self.assertRaises(a.UnsupportedInput):
                    a.preflight(target, metadata)

    def test_enantiomer_feature_sign_is_retained(self):
        values = []
        for index, smiles in enumerate(("C[C@H](O)F", "C[C@@H](O)F")):
            target = self.root / str(index)
            metadata = a.build(assembly(component("L", "small_molecule", smiles=smiles)), target, {}, {})
            values.append(a.preflight(target, metadata)["components"][0])
        self.assertEqual(values[0]["atomic_numbers"], values[1]["atomic_numbers"])
        edges = lambda rows: sorted((min(a, b), max(a, b), order) for a, b, order in rows)
        self.assertEqual(edges(values[0]["bonds"]), edges(values[1]["bonds"]))
        self.assertEqual([row[:4] for row in values[0]["chiral_features"]],
                         [row[:4] for row in values[1]["chiral_features"]])
        self.assertTrue(values[0]["chiral_features"])
        for left, right in zip(values[0]["chiral_features"], values[1]["chiral_features"]):
            self.assertAlmostEqual(left[-1], -right[-1], places=4)

    def test_native_features_discard_formal_charge_and_double_bond_stereo(self):
        """Evidence for the adapter refusals, using actual native tensors."""
        import torch
        from types import SimpleNamespace
        from omegaconf import OmegaConf
        from rf2aa.chemical import initialize_chemdata
        from rf2aa.data.parsers import parse_mol
        from rf2aa.data.small_molecule import compute_features_from_obmol
        base = OmegaConf.load(Path(os.environ["RFAA_SOURCE_DIR"]) / "rf2aa/config/inference/base.yaml")
        initialize_chemdata(base.chem_params)
        runner = SimpleNamespace(config=base, deterministic=True)
        for pair in (("CN", "C[NH3+]"), ("F/C=C/F", "F/C=C\\F")):
            features = []
            for value in pair:
                mol, msa, ins, xyz, mask = parse_mol(value, filetype="smiles", string=True, generate_conformer=True)
                features.append(compute_features_from_obmol(mol, msa, xyz, runner))
            self.assertEqual(set(vars(features[0])), set(vars(features[1])))
            for field, value in vars(features[0]).items():
                other = getattr(features[1], field)
                if isinstance(value, torch.Tensor):
                    torch.testing.assert_close(value, other, equal_nan=True)
                else:
                    self.assertEqual(value, other)

    def test_runtime_refuses_changed_native_parser_before_weights_or_search(self):
        metadata = a.build(assembly(component("A")), self.destination, {}, {})
        report = a.preflight(self.destination, metadata)
        report["source_files_sha256"]["rf2aa/data/protein.py"] = "0"*64
        (self.destination / "preflight.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(a.UnsupportedInput, "parser source differs"):
            a.run(self.destination / "rfaa.json", Path(os.environ["RFAA_SOURCE_DIR"]),
                  self.root / "never-created", "test", self.root / "no-weights", "no-db")
        self.assertFalse((self.root / "never-created").exists())

    def test_real_mixed_assembly_merges_after_only_empty_template_expansion(self):
        import torch
        from types import SimpleNamespace
        from omegaconf import OmegaConf
        from rf2aa.chemical import initialize_chemdata
        from rf2aa.data.protein import load_protein
        from rf2aa.data.nucleic_acid import load_nucleic_acid
        from rf2aa.data.small_molecule import load_small_molecule
        from rf2aa.data.merge_inputs import merge_all
        metadata = a.build(assembly(component("A"), component("B", sequence="LMNPQRSTV"),
                                    component("D", "dna", "ACGT"), component("R", "rna", "ACGU"),
                                    component("L", "small_molecule", smiles="CCO")), self.destination, {}, {})
        config, inputs = a.load_config(self.destination / metadata["entrypoint"])
        base = OmegaConf.load(Path(os.environ["RFAA_SOURCE_DIR"]) / "rf2aa/config/inference/base.yaml")
        initialize_chemdata(base.chem_params)
        runner = SimpleNamespace(config=base, deterministic=True)
        proteins, nas, molecules = {}, {}, {}
        for chain, kind, value in inputs:
            if kind == "protein":
                raw = load_protein(value, None, None, runner)
                self.assertEqual(raw.xyz_t.shape[0], 1)
                first = raw.xyz_t[0].clone()
                a.expand_blank_protein_templates(raw, 4)
                self.assertFalse(raw.mask_t.any())
                torch.testing.assert_close(first, raw.xyz_t[0], equal_nan=True)
                proteins[chain] = raw
            elif kind in ("dna", "rna"):
                nas[chain] = load_nucleic_acid(value, kind, runner)
            else:
                molecules[chain] = load_small_molecule(value, kind, runner)
        merged = merge_all(proteins, nas, molecules, [], deterministic=True)
        self.assertEqual(merged.length(), 29)
        self.assertEqual(merged.xyz_t.shape[0], 4)
        self.assertEqual([x[0] for x in merged.chain_lengths], ["A", "B", "D", "R", "L"])
        from rf2aa.util_module import XYZConverter
        runner.xyz_converter = XYZConverter()
        features = merged.construct_features(runner)
        self.assertEqual(features.seq_unmasked.shape[-1], 29)
        self.assertEqual(features.t1d.shape[0], 4)
        for field in ("t1d", "t2d", "xyz_t", "alpha_t", "chirals"):
            self.assertTrue(torch.isfinite(getattr(features, field)).all(), field)

    def test_real_templates_and_masked_real_identities_are_never_replicated(self):
        from types import SimpleNamespace
        import torch
        from rf2aa.data.data_loader_utils import blank_template
        from rf2aa.chemical import initialize_chemdata
        from omegaconf import OmegaConf
        base = OmegaConf.load(Path(os.environ["RFAA_SOURCE_DIR"]) / "rf2aa/config/inference/base.yaml")
        initialize_chemdata(base.chem_params)
        for observed in (True, False):
            xyz, t1d, mask, _ = blank_template(1, 3)
            if observed:
                mask[0, 0, 0] = True
            else:
                t1d[0, 0, 20] = 0; t1d[0, 0, 0] = 1
            value = SimpleNamespace(xyz_t=xyz, t1d=t1d, mask_t=mask)
            with self.assertRaises(a.UnsupportedInput): a.expand_blank_protein_templates(value, 4)
            self.assertEqual(value.xyz_t.shape[0], 1)
        xyz, t1d, mask, _ = blank_template(4, 3)
        mask[0, 0, 0] = True
        value = SimpleNamespace(xyz_t=xyz, t1d=t1d, mask_t=mask)
        self.assertIs(a.expand_blank_protein_templates(value, 4).xyz_t, xyz)


if __name__ == "__main__":
    unittest.main()
