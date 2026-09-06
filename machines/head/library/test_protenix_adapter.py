"""Chemical identity guards plus opt-in tests against installed Protenix 2.0.0."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import protenix_adapter as adapter


def component(chain, molecule, **chemical):
    return {"chain_id": chain, "construct_ref": f"construct:{chain.lower()}@1",
            "record": {"identity": {"molecule_type": molecule, **chemical}}}


def snapshot(*components, bonds=None, monomers=None):
    return {"components": list(components), "bonds": bonds or [], "monomers": monomers or {}}


def mixed_snapshot():
    return snapshot(component("P", "protein", sequence="ACSG", modifications=[{"position": 3, "monomer_ref": "monomer:phosphoserine@1"}]),
                    component("D", "dna", sequence="ACG", modifications=[{"position": 2, "monomer_ref": "monomer:methylcytosine@1"}]),
                    component("R", "rna", sequence="ACU", modifications=[{"position": 3, "monomer_ref": "monomer:pseudouridine@1"}]),
                    component("L", "small_molecule", ccd="ATP"),
                    component("S", "small_molecule", smiles="CCO"),
                    monomers={"monomer:phosphoserine@1": {"identity": {"ccd": "SEP"}},
                              "monomer:methylcytosine@1": {"identity": {"ccd": "5CM"}},
                              "monomer:pseudouridine@1": {"identity": {"ccd": "PSU"}}})


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def build(self, value, **kwargs):
        out = self.root / str(len(list(self.root.iterdir())))
        metadata = adapter.build(value, out, kwargs.get("assets", {}), {})
        return out, metadata, json.loads((out / metadata["entrypoint"]).read_text())[0]

    def test_mixed_components_and_modifications_preserve_chain_order(self):
        source = mixed_snapshot()
        original = copy.deepcopy(source)
        out, metadata, query = self.build(source)
        self.assertEqual(source, original)
        self.assertTrue(metadata["has_protein"])
        self.assertEqual([next(iter(x.values()))["id"][0] for x in query["sequences"]], ["P", "D", "R", "L", "S"])
        self.assertEqual(query["sequences"][0]["proteinChain"]["modifications"], [{"ptmPosition": 3, "ptmType": "CCD_SEP"}])
        self.assertEqual(query["sequences"][1]["dnaSequence"]["modifications"], [{"basePosition": 2, "modificationType": "CCD_5CM"}])
        self.assertEqual(query["sequences"][2]["rnaSequence"]["modifications"], [{"basePosition": 3, "modificationType": "CCD_PSU"}])

    def test_sdf_is_copied_exactly_and_portable(self):
        raw = b'original SDF bytes, parsed during native preflight\n'
        source = self.root / "source.sdf"
        source.write_bytes(raw)
        value = snapshot(component("L", "small_molecule", structure_format="sdf", structure_file="attachments/source.sdf"))
        out, metadata, query = self.build(value, assets={"construct:l@1": {"attachments/source.sdf": source}})
        self.assertFalse(metadata["has_protein"])
        ligand = query["sequences"][0]["ligand"]["ligand"]
        self.assertEqual(ligand, "FILE_assets/entity-1.sdf")
        self.assertEqual((out / ligand[5:]).read_bytes(), raw)

    def test_explicit_internal_and_assembly_bonds_use_correct_entity(self):
        internal = {"from": {"chain_id": "A", "position": 1, "atom": "SG"},
                    "to": {"chain_id": "A", "position": 3, "atom": "SG"}, "order": "single"}
        linked = {"from": {"chain_id": "R", "position": 1, "atom": "O2'"},
                  "to": {"chain_id": "L", "atom": "P"}}
        value = snapshot(component("P", "protein", sequence="CGC", crosslinks=[internal]),
                         component("R", "rna", sequence="A"), component("L", "small_molecule", ccd="ATP"), bonds=[linked])
        _, _, query = self.build(value)
        self.assertEqual(query["covalent_bonds"][0]["left_entity"], 2)
        self.assertEqual(query["covalent_bonds"][0]["right_entity"], 3)
        self.assertEqual(query["covalent_bonds"][1]["left_entity"], 1)
        self.assertEqual(query["covalent_bonds"][1]["right_position"], 3)

    def test_unsupported_chemistry_is_not_silently_dropped(self):
        values = [component("P", "protein", sequence="AG", circular="true"),
                  component("P", "protein", sequence="AG", termini={"n": "acetyl"}),
                  component("P", "protein", sequence="AG", linkages=[{"position": 1}]),
                  component("P", "protein", sequence="AG", residues=["ALA", "GLY"]),
                  component("P", "protein", sequence="BJ"),
                  component("D", "dna", sequence="ARY"),
                  component("L", "small_molecule", ccd="MSE"),
                  component("L", "small_molecule", ccd="ATP", smiles="CCC"),
                  component("L", "small_molecule", smiles="CCC", charge=2),
                  component("X", "mixed_polymer", residues=["ALA", "DA"])]
        for item in values:
            with self.subTest(item=item), self.assertRaises(adapter.Error):
                self.build(snapshot(item))

    def test_bond_indices_orders_duplicates_and_unknown_chains_reject(self):
        for atom, order, chain in [(0, 1, "P"), ("0", 1, "P"), ("SG", 2, "P"), ("SG", 1, "Q")]:
            value = snapshot(component("P", "protein", sequence="CGC"), bonds=[
                {"from": {"chain_id": chain, "position": 1, "atom": atom},
                 "to": {"chain_id": "P", "position": 3, "atom": "SG"}, "order": order}])
            with self.subTest(atom=atom, order=order, chain=chain), self.assertRaises(adapter.Error):
                self.build(value)
        value = snapshot(component("P", "protein", sequence="CGC"), bonds=[
            {"from": {"chain_id": "P", "position": 1, "atom": "SG"},
             "to": {"chain_id": "P", "position": 3, "atom": "SG"}}] * 2)
        with self.assertRaisesRegex(adapter.Error, "duplicate covalent"):
            self.build(value)

    def test_missing_monomer_invalid_modification_and_asset_fail(self):
        for pos, monomer in [(0, "monomer:phosphoserine@1"), (5, "monomer:phosphoserine@1"), (2, "monomer:missing@1")]:
            value = mixed_snapshot()
            value["components"][0]["record"]["identity"]["modifications"] = [{"position": pos, "monomer_ref": monomer}]
            with self.subTest(pos=pos, monomer=monomer), self.assertRaises(adapter.Error):
                self.build(value)
        with self.assertRaisesRegex(adapter.Error, "missing"):
            self.build(snapshot(component("L", "small_molecule", structure_file="attachments/source.sdf", structure_format="sdf")))

    def test_recipe_rejects_native_input_overrides_before_setup(self):
        recipe = Path(__file__).resolve().parents[1] / "recipes/protenix.sh"
        if not recipe.exists():
            self.skipTest("recipe is not part of this isolated native parser fixture")
        for argument in ("-i", "-i/another.json", "--input=another.json", "-o/tmp/elsewhere", "--out_dir=/tmp/elsewhere"):
            result = subprocess.run(["bash", "-c", 'set -eu; BIO_NATIVE_BUNDLE=/bundle; EXTRA_ARGS=("$1"); source "$2"',
                                     "test", argument, str(recipe)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("native input conflicts", result.stderr)

    @unittest.skipUnless(os.environ.get("PROTENIX_NATIVE_TEST") == "1", "requires pinned native CPU environment and CCD cache")
    def test_native_mixed_graph_and_single_covalent_bond(self):
        value = mixed_snapshot()
        value["components"].append(component("C", "protein", sequence="CGC", bonds=[
            {"from": {"position": 1, "atom": "SG"}, "to": {"position": 3, "atom": "SG"}}]))
        out, metadata, _ = self.build(value)
        proof = adapter.preflight(out, metadata)
        self.assertEqual(proof["chain_order"], ["P", "D", "R", "L", "S", "C"])
        self.assertEqual(proof["covalent_bond_count"], 1)
        self.assertGreater(proof["atom_count"], 100)
        self.assertGreater(proof["token_count"], 10)

    @unittest.skipUnless(os.environ.get("PROTENIX_NATIVE_TEST") == "1", "requires pinned native CPU environment and CCD cache")
    def test_native_circular_backbone_bonds_are_present(self):
        value = snapshot(component("P", "protein", sequence="AGG", circular=True),
                         component("D", "dna", sequence="ACG", circular=True),
                         component("R", "rna", sequence="ACU", circular=True))
        out, metadata, query = self.build(value)
        self.assertEqual([b["left_atom"] for b in query["covalent_bonds"]], ["C", "O3'", "O3'"])
        proof = adapter.preflight(out, metadata)
        self.assertEqual(proof["covalent_bond_count"], 3)

    @unittest.skipUnless(os.environ.get("PROTENIX_NATIVE_TEST") == "1", "requires pinned native CPU environment and CCD cache")
    def test_native_sdf_single_three_dimensional_molecule_required(self):
        from rdkit import Chem
        from rdkit.Chem import AllChem
        mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
        self.assertEqual(AllChem.EmbedMolecule(mol, randomSeed=42), 0)
        source = self.root / "source.sdf"
        def native_case(count, three_d):
            mol.GetConformer().Set3D(three_d)
            with Chem.SDWriter(str(source)) as writer:
                for _ in range(count):
                    writer.write(mol)
            value = snapshot(component("L", "small_molecule", structure_format="sdf", structure_file="attachments/source.sdf"))
            out, metadata, _ = self.build(value, assets={"construct:l@1": {"attachments/source.sdf": source}})
            return adapter.preflight(out, metadata)
        self.assertGreater(native_case(1, True)["atom_count"], 0)
        with self.assertRaisesRegex(adapter.Error, "exactly one"):
            native_case(2, True)
        # A valid 2D record has no nonzero Z coordinates; preserve that distinction.
        AllChem.Compute2DCoords(mol)
        with self.assertRaisesRegex(adapter.Error, "3D"):
            native_case(1, False)

    @unittest.skipUnless(os.environ.get("PROTENIX_NATIVE_TEST") == "1", "requires pinned native CPU environment and CCD cache")
    def test_native_isotope_and_radical_annotations_reject(self):
        for smiles, message in [("[13CH3]CO", "isotope"), ("[CH3]", "radical")]:
            out, metadata, _ = self.build(snapshot(component("L", "small_molecule", smiles=smiles)))
            with self.subTest(smiles=smiles), self.assertRaisesRegex(adapter.Error, message):
                adapter.preflight(out, metadata)


if __name__ == "__main__":
    unittest.main()
