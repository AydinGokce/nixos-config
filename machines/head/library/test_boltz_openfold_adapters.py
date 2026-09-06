"""Lossless input projection and fail-closed chemistry checks, without inference."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import registry
import boltz_adapter
import openfold3_adapter

try:
    from rdkit import Chem
except ImportError:
    Chem = None


ADAPTERS = (boltz_adapter, openfold3_adapter)


class ProjectionFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.registry = registry.Registry(self.base / "registry")
        self.registry.init()
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self, identities, monomers=None, bonds=None):
        for ident, identity in (monomers or {}).items():
            self.registry.import_record({"kind": "monomer", "id": ident, "identity": identity})
        components = []
        for index, (chain, identity) in enumerate(identities):
            ident = f"entity{index}"
            self.registry.import_record({"kind": "construct", "id": ident, "identity": identity})
            components.append({"chain_id": chain, "construct_ref": ident})
        self.registry.import_record({"kind": "assembly", "id": "assembly",
                                     "identity": {"components": components, "bonds": bonds or []}})
        return self.registry.snapshot("assembly")

    def build(self, adapter, snapshot, assets=None):
        self.counter += 1
        output = self.base / f"output-{self.counter}"
        output.mkdir()
        metadata = adapter.build(snapshot, output, assets or {}, {})
        return json.loads((output / metadata["entrypoint"]).read_text()), metadata

    @staticmethod
    def protein(sequence="ACDEFGHIK", **fields):
        return {"molecule_type": "protein", "sequence": sequence, **fields}

    def assert_rejected_by_both(self, snapshot, pattern=None, assets=None):
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter.__name__):
                context = self.assertRaisesRegex(ValueError, pattern) if pattern else self.assertRaises(ValueError)
                with context:
                    self.build(adapter, snapshot, assets)


class ProjectionTests(ProjectionFixture, unittest.TestCase):
    def test_canonical_protein_dna_rna_copies_keep_chain_types_order_and_sequences(self):
        snapshot = self.snapshot([
            ("A", self.protein()), ("B", self.protein()),
            ("D", {"molecule_type": "dna", "sequence": "ACGT"}),
            ("R", {"molecule_type": "rna", "sequence": "ACGU"})])
        original = copy.deepcopy(snapshot)
        boltz, metadata = self.build(boltz_adapter, snapshot)
        self.assertEqual([next(iter(x)) for x in boltz["sequences"]], ["protein", "protein", "dna", "rna"])
        self.assertEqual([next(iter(x.values()))["id"] for x in boltz["sequences"]], ["A", "B", "D", "R"])
        self.assertEqual(metadata["expected_chains"], ["A", "B", "D", "R"])
        self.assertTrue(metadata["has_protein"])
        of3, metadata = self.build(openfold3_adapter, snapshot)
        chains = of3["queries"]["construct"]["chains"]
        self.assertEqual([x["molecule_type"] for x in chains], ["protein", "protein", "dna", "rna"])
        self.assertEqual([x["sequence"] for x in chains], ["ACDEFGHIK", "ACDEFGHIK", "ACGT", "ACGU"])
        self.assertEqual(snapshot, original)

    def test_ccd_residue_modifications_preserve_positions_and_pinned_monomers(self):
        snapshot = self.snapshot([("R", {"molecule_type": "rna", "sequence": "ACGU",
            "modifications": [{"position": 4, "monomer_ref": "pseudouridine"},
                              {"position": 2, "ccd": "5MC"}]})], monomers={"pseudouridine": {"ccd": "PSU"}})
        original = copy.deepcopy(snapshot)
        boltz, _ = self.build(boltz_adapter, snapshot)
        self.assertEqual(boltz["sequences"][0]["rna"]["modifications"],
                         [{"position": 4, "ccd": "PSU"}, {"position": 2, "ccd": "5MC"}])
        of3, _ = self.build(openfold3_adapter, snapshot)
        self.assertEqual(of3["queries"]["construct"]["chains"][0]["non_canonical_residues"], {"4": "PSU", "2": "5MC"})
        self.assertEqual(snapshot, original)

    def test_cyclic_intent_is_explicit_in_native_input(self):
        snapshot = self.snapshot([("A", self.protein(circular=True))])
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter.__name__):
                native, _ = self.build(adapter, snapshot)
                chain = native["sequences"][0]["protein"] if adapter is boltz_adapter else native["queries"]["construct"]["chains"][0]
                self.assertIs(chain["cyclic"], True)

    def test_models_reject_grouped_equal_sequences_with_different_modifications(self):
        snapshot = self.snapshot([
            ("A", self.protein()),
            ("B", self.protein(modifications=[{"position": 2, "ccd": "CSO"}]))])
        with self.assertRaisesRegex(ValueError, "merges identical"):
            self.build(boltz_adapter, snapshot)
        with self.assertRaisesRegex(ValueError, "assigns one entity"):
            self.build(openfold3_adapter, snapshot)

    def test_of3_rejects_same_text_used_for_different_polymer_types(self):
        snapshot = self.snapshot([("A", self.protein(sequence="ACG")),
                                  ("D", {"molecule_type": "dna", "sequence": "ACG"})])
        with self.assertRaisesRegex(ValueError, "assigns one entity"):
            self.build(openfold3_adapter, snapshot)
        boltz, _ = self.build(boltz_adapter, snapshot)
        self.assertEqual([next(iter(x)) for x in boltz["sequences"]], ["protein", "dna"])

    def test_boltz_rejects_grouped_equal_sequences_with_different_circularity(self):
        snapshot = self.snapshot([("A", self.protein()), ("B", self.protein(circular=True))])
        with self.assertRaisesRegex(ValueError, "merges identical"):
            self.build(boltz_adapter, snapshot)

    def test_boltz_accepts_identical_modified_copies_without_losing_chain_ids(self):
        identity = self.protein(modifications=[{"position": 2, "ccd": "CSO"}])
        snapshot = self.snapshot([("A", identity), ("B", identity)])
        boltz, metadata = self.build(boltz_adapter, snapshot)
        self.assertEqual([x["protein"]["id"] for x in boltz["sequences"]], ["A", "B"])
        self.assertEqual(metadata["expected_chains"], ["A", "B"])

    def test_unknown_identity_terminal_and_backbone_chemistry_are_never_discarded(self):
        base = self.snapshot([("R", {"molecule_type": "rna", "sequence": "ACGU"})])
        for extra in [{"termini": {"5_prime": "phosphate"}},
                      {"linkages": [{"position": 1, "chemistry": "phosphorothioate"}]},
                      {"backbone_stereochemistry": "unresolved"}, {"unexpected_feature": "explicit intent"},
                      {"residues": ["A", "C", "G", "U"]}]:
            with self.subTest(extra=extra):
                snapshot = copy.deepcopy(base)
                snapshot["components"][0]["record"]["identity"].update(extra)
                self.assert_rejected_by_both(snapshot)

    def test_ambiguous_letters_and_mixed_polymers_are_not_replaced(self):
        base = self.snapshot([("R", {"molecule_type": "rna", "sequence": "ACGU"})])
        for identity in [{"molecule_type": "rna", "sequence": "ACGN"},
                         {"molecule_type": "protein", "sequence": "ACDX"},
                         {"molecule_type": "mixed_polymer", "residues": ["dA", "rC"]}]:
            with self.subTest(identity=identity):
                snapshot = copy.deepcopy(base)
                snapshot["components"][0]["record"]["identity"] = identity
                self.assert_rejected_by_both(snapshot)

    def test_custom_monomer_graph_is_not_silently_replaced_with_a_base_or_ccd(self):
        snapshot = self.snapshot([("R", {"molecule_type": "rna", "sequence": "ACGU",
            "modifications": [{"position": 2, "monomer_ref": "custom"}]})],
            monomers={"custom": {"smiles": "CCO", "description": "Incomplete attachment definition"}})
        self.assert_rejected_by_both(snapshot, "unsupported fields")
        snapshot["monomers"]["monomer:custom@1"]["identity"] = {"ccd": "5MC", "smiles": "CCO"}
        self.assert_rejected_by_both(snapshot, "unsupported fields")

    def test_duplicate_ambiguous_and_invalid_modification_positions_fail(self):
        base = self.snapshot([("R", {"molecule_type": "rna", "sequence": "ACGU"})])
        for modifications in [[{"position": 0, "ccd": "PSU"}], [{"position": 5, "ccd": "PSU"}],
                              [{"position": True, "ccd": "PSU"}], [{"position": "1", "ccd": "PSU"}],
                              [{"position": 1, "ccd": "PSU"}, {"position": 1, "ccd": "5MC"}],
                              [{"position": 1, "ccd": "PSU", "monomer_ref": "monomer:x@1"}],
                              [{"position": 1, "ccd": "psu"}]]:
            with self.subTest(modifications=modifications):
                snapshot = copy.deepcopy(base)
                snapshot["components"][0]["record"]["identity"]["modifications"] = modifications
                self.assert_rejected_by_both(snapshot)

    def test_unknown_assembly_or_component_semantics_fail(self):
        base = self.snapshot([("A", self.protein())])
        snapshot = copy.deepcopy(base)
        snapshot["assembly_record"]["identity"]["unknown_restraint"] = {"explicit": "meaning"}
        self.assert_rejected_by_both(snapshot, "unsupported fields")
        snapshot = copy.deepcopy(base)
        snapshot["components"][0]["orientation"] = "explicit intent"
        self.assert_rejected_by_both(snapshot, "unsupported fields")

    def test_explicit_polymer_bond_is_preserved_by_boltz_and_rejected_by_of3(self):
        bond = {"from": {"chain_id": "A", "position": 2, "atom": "SG"},
                "to": {"chain_id": "B", "position": 2, "atom": "SG"}, "order": "single"}
        snapshot = self.snapshot([("A", self.protein()), ("B", self.protein())], bonds=[bond])
        native, _ = self.build(boltz_adapter, snapshot)
        self.assertEqual(native["constraints"], [{"bond": {"atom1": ["A", 2, "SG"], "atom2": ["B", 2, "SG"]}}])
        with self.assertRaisesRegex(ValueError, "does not apply"):
            self.build(openfold3_adapter, snapshot)

    def test_construct_local_crosslinks_are_remapped_to_the_assembly_chain(self):
        bond = {"from": {"position": 2, "atom": "SG"}, "to": {"position": 4, "atom": "SG"}, "order": 1}
        snapshot = self.snapshot([("P", self.protein(sequence="ACDCG", crosslinks=[bond]))])
        original = copy.deepcopy(snapshot)
        native, _ = self.build(boltz_adapter, snapshot)
        self.assertEqual(native["constraints"][0]["bond"], {"atom1": ["P", 2, "SG"], "atom2": ["P", 4, "SG"]})
        with self.assertRaisesRegex(ValueError, "does not apply"):
            self.build(openfold3_adapter, snapshot)
        self.assertEqual(snapshot, original)

    def test_bond_order_boolean_numeric_atom_and_self_bond_fail(self):
        bond = {"from": {"chain_id": "A", "position": 2, "atom": "SG"},
                "to": {"chain_id": "B", "position": 2, "atom": "SG"}, "order": 1}
        base = self.snapshot([("A", self.protein()), ("B", self.protein())], bonds=[bond])
        for order in [True, "double", 2]:
            snapshot = copy.deepcopy(base)
            snapshot["bonds"][0]["order"] = order
            with self.subTest(order=order):
                self.assert_rejected_by_both(snapshot)
        snapshot = copy.deepcopy(base)
        snapshot["bonds"][0]["from"]["atom"] = 3
        self.assert_rejected_by_both(snapshot)
        snapshot = copy.deepcopy(base)
        snapshot["bonds"][0]["to"] = copy.deepcopy(snapshot["bonds"][0]["from"])
        self.assert_rejected_by_both(snapshot)


@unittest.skipIf(Chem is None, "Run ligand projections in the existing RDKit-enabled audit environment")
class LigandProjectionTests(ProjectionFixture, unittest.TestCase):
    # These cases use real chemical parsing without any model execution.
    def test_stereochemistry_charge_and_fragments_are_preserved_in_smiles(self):
        smiles = "C[C@@H](O)[NH3+].[Cl-]"
        snapshot = self.snapshot([("L", {"molecule_type": "small_molecule", "smiles": smiles})])
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter.__name__):
                native, metadata = self.build(adapter, snapshot)
                ligand = native["sequences"][0]["ligand"] if adapter is boltz_adapter else native["queries"]["construct"]["chains"][0]
                self.assertEqual(ligand["smiles"], smiles)
                self.assertEqual(metadata["ligand_graphs"]["L"]["formal_charge"], 0)
                self.assertEqual(metadata["ligand_graphs"]["L"]["fragments"], 2)
                self.assertFalse(metadata["has_protein"])

    def test_ccd_ligand_stays_a_ccd_reference(self):
        snapshot = self.snapshot([("L", {"molecule_type": "small_molecule", "ccd": "ATP"})])
        boltz, _ = self.build(boltz_adapter, snapshot)
        self.assertEqual(boltz["sequences"][0]["ligand"], {"id": "L", "ccd": "ATP"})
        of3, _ = self.build(openfold3_adapter, snapshot)
        self.assertEqual(of3["queries"]["construct"]["chains"][0]["ccd_codes"], ["ATP"])

    def test_sdf_conversion_preserves_chemical_graph_and_original_bytes(self):
        molecule = Chem.MolFromSmiles("C[C@@H](O)[NH3+].[Cl-]")
        source = self.base / "source.sdf"
        with Chem.SDWriter(str(source)) as writer:
            writer.write(molecule)
        original = source.read_bytes()
        record = self.registry.import_record({"kind": "construct", "id": "ligand",
            "identity": {"molecule_type": "small_molecule", "structure_format": "sdf",
                         "structure_file": "attachments/source.sdf"}}, {"source.sdf": source})
        snapshot = self.registry.snapshot("ligand")
        assets = {registry.reference(record): {"attachments/source.sdf": source}}
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter.__name__):
                native, metadata = self.build(adapter, snapshot, assets)
                value = native["sequences"][0]["ligand"] if adapter is boltz_adapter else native["queries"]["construct"]["chains"][0]
                self.assertEqual(Chem.MolToSmiles(Chem.MolFromSmiles(value["smiles"]), isomericSmiles=True),
                                 Chem.MolToSmiles(molecule, isomericSmiles=True))
                self.assertIs(metadata["ligand_graphs"]["A"]["original_coordinates_used"], False)
        self.assertEqual(source.read_bytes(), original)

    def test_sdf_with_multiple_records_never_drops_all_but_the_first(self):
        source = self.base / "multiple.sdf"
        with Chem.SDWriter(str(source)) as writer:
            writer.write(Chem.MolFromSmiles("CCO"))
            writer.write(Chem.MolFromSmiles("CCN"))
        record = self.registry.import_record({"kind": "construct", "id": "ligand",
            "identity": {"molecule_type": "small_molecule", "structure_format": "sdf",
                         "structure_file": "attachments/source.sdf"}}, {"source.sdf": source})
        self.assert_rejected_by_both(self.registry.snapshot("ligand"), "exactly one molecule",
                                     {registry.reference(record): {"attachments/source.sdf": source}})

    def test_explicit_hydrogen_3d_sdf_preserves_stereo_without_false_graph_mismatch(self):
        from rdkit.Chem import AllChem
        molecule = Chem.AddHs(Chem.MolFromSmiles("C[C@@H](O)C(=O)O"))
        self.assertEqual(AllChem.EmbedMolecule(molecule, randomSeed=20260906), 0)
        self.assertTrue(molecule.GetConformer().Is3D())
        source = self.base / "explicit-hydrogen-3d.sdf"
        with Chem.SDWriter(str(source)) as writer:
            writer.write(molecule)
        original = source.read_bytes()
        record = self.registry.import_record({"kind": "construct", "id": "ligand",
            "identity": {"molecule_type": "small_molecule", "structure_format": "sdf",
                         "structure_file": "attachments/source.sdf"}}, {"source.sdf": source})
        snapshot = self.registry.snapshot("ligand")
        assets = {registry.reference(record): {"attachments/source.sdf": source}}
        expected = Chem.MolToSmiles(Chem.RemoveHs(molecule), isomericSmiles=True)
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter.__name__):
                native, metadata = self.build(adapter, snapshot, assets)
                value = native["sequences"][0]["ligand"] if adapter is boltz_adapter else native["queries"]["construct"]["chains"][0]
                parsed = Chem.MolFromSmiles(value["smiles"])
                self.assertEqual(Chem.MolToSmiles(Chem.RemoveHs(parsed), isomericSmiles=True), expected)
                self.assertEqual(metadata["ligand_graphs"]["A"]["heavy_atoms"], molecule.GetNumHeavyAtoms())
                self.assertIs(metadata["ligand_graphs"]["A"]["original_coordinates_used"], False)
        self.assertEqual(source.read_bytes(), original)

    def test_isotopes_radicals_unspecified_and_dative_bonds_fail_closed(self):
        base = self.snapshot([("L", {"molecule_type": "small_molecule", "smiles": "CCO"})])
        for smiles in ["[13CH3]CO", "[CH3]", "C~C", "N->[Cu]"]:
            snapshot = copy.deepcopy(base)
            snapshot["components"][0]["record"]["identity"]["smiles"] = smiles
            with self.subTest(smiles=smiles):
                self.assert_rejected_by_both(snapshot)

    def test_wildcards_atom_maps_trailing_names_and_bad_smiles_fail_closed(self):
        base = self.snapshot([("L", {"molecule_type": "small_molecule", "smiles": "CCO"})])
        for smiles in ["C*", "[CH3:1]CO", "CCO ethanol", "CC("]:
            snapshot = copy.deepcopy(base)
            snapshot["components"][0]["record"]["identity"]["smiles"] = smiles
            with self.subTest(smiles=smiles):
                self.assert_rejected_by_both(snapshot)

    def test_two_competing_authoritative_ligand_representations_fail(self):
        snapshot = self.snapshot([("L", {"molecule_type": "small_molecule", "smiles": "CCO", "ccd": "ATP"})])
        self.assert_rejected_by_both(snapshot, "exactly one authoritative")

    def test_covalent_ccd_ligand_names_preserved_but_smiles_names_never_guessed(self):
        bond = {"from": {"chain_id": "A", "position": 2, "atom": "SG"},
                "to": {"chain_id": "L", "atom": "C1"}, "order": "single"}
        snapshot = self.snapshot([("A", self.protein()),
                                  ("L", {"molecule_type": "small_molecule", "ccd": "ATP"})], bonds=[bond])
        boltz, _ = self.build(boltz_adapter, snapshot)
        self.assertEqual(boltz["constraints"][0]["bond"]["atom2"], ["L", 1, "C1"])
        snapshot["components"][1]["record"]["identity"] = {"molecule_type": "small_molecule", "smiles": "CCO"}
        with self.assertRaisesRegex(ValueError, "atom names.*not silently guessed"):
            self.build(boltz_adapter, snapshot)
        with self.assertRaisesRegex(ValueError, "does not apply"):
            self.build(openfold3_adapter, snapshot)

    def test_enhanced_sdf_stereochemistry_is_not_flattened_to_one_isomer(self):
        molecule = Chem.MolFromSmiles("C[C@H](F)[C@H](O)Cl |&1:1,3|")
        self.assertTrue(molecule.GetStereoGroups())
        source = self.base / "enhanced.sdf"
        with Chem.SDWriter(str(source)) as writer:
            writer.SetForceV3000(True)
            writer.write(molecule)
        record = self.registry.import_record({"kind": "construct", "id": "ligand",
            "identity": {"molecule_type": "small_molecule", "structure_format": "sdf",
                         "structure_file": "attachments/source.sdf"}}, {"source.sdf": source})
        self.assert_rejected_by_both(self.registry.snapshot("ligand"), "stereochemistry",
                                     {registry.reference(record): {"attachments/source.sdf": source}})


if __name__ == "__main__":
    unittest.main()
