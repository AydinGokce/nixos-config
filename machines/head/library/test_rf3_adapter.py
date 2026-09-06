"""Projection invariants plus opt-in actual RF3/AtomWorks chemistry tests."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import rf3_adapter as a


def component(chain, kind='protein', **identity):
    if kind == 'protein':
        identity.setdefault('sequence', 'ACGM')
    return {'chain_id': chain, 'construct_ref': 'construct:case-'+chain+'@1',
            'record': {'identity': {'molecule_type': kind, **identity}}}


def snapshot(*items, bonds=None, monomers=None):
    return {'components': list(items), 'bonds': bonds or [], 'monomers': monomers or {}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root/'bundle'

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, source, assets=None):
        return a.build(source, self.bundle, assets or {}, {'msa_backend': 'private'})


class ProjectionTests(Fixture):
    def test_explicit_types_parenthesized_ccd_and_modified_msa_query(self):
        metadata = self.build(snapshot(component('P', modifications=[{'position': 4, 'ccd': 'MSE'}]),
                                       component('D', 'dna', sequence='ACGT'),
                                       component('R', 'rna', sequence='ACGU')))
        native = json.loads((self.bundle/'input.json').read_text())
        self.assertEqual(native[0]['components'][0]['seq'], ['(ALA)', '(CYS)', '(GLY)', '(MSE)'])
        self.assertEqual(native[0]['components'][1]['seq'], ['(DA)', '(DC)', '(DG)', '(DT)'])
        self.assertEqual(native[0]['components'][2]['seq'], ['(A)', '(C)', '(G)', '(U)'])
        self.assertEqual(metadata['msa_queries'], [{'chain_id': 'P', 'sequence': 'ACGX'}])
        self.assertEqual(metadata['msa_queries'], a.protein_queries(native))
        self.assertTrue(all(c['is_polymer'] for c in native[0]['components']))

    def test_ccd_monomer_revision_is_resolved_without_mutating_snapshot(self):
        monomer = 'monomer:selenomethionine@4'
        source = snapshot(component('P', modifications=[{'position': 4, 'monomer_ref': monomer}]),
                          monomers={monomer: {'identity': {'ccd': 'MSE'}}})
        original = copy.deepcopy(source)
        metadata = self.build(source)
        self.assertEqual(metadata['expected_chains']['P']['residues'][-1], 'MSE')
        self.assertEqual(source, original)

    def test_circular_peptide_emits_explicit_named_backbone_bond(self):
        metadata = self.build(snapshot(component('A', sequence='ACGG', circular=True)))
        self.assertEqual(metadata['circular_bonds'], [['A/GLY/4/C', 'A/ALA/1/N']])
        self.assertEqual(json.loads((self.bundle/'input.json').read_text())[0]['bonds'], metadata['circular_bonds'])

    def test_disulfide_endpoints_use_exact_modified_residue_names_and_positions(self):
        bond = {'from': {'chain_id': 'A', 'position': 2, 'atom': 'SG'},
                'to': {'chain_id': 'B', 'position': 1, 'atom': 'SG'}, 'order': 1}
        self.build(snapshot(component('A'), component('B', sequence='CGGA'), bonds=[bond]))
        self.assertEqual(json.loads((self.bundle/'input.json').read_text())[0]['bonds'], [['A/CYS/2/SG', 'B/CYS/1/SG']])

    def test_unrepresented_custom_backbone_terminal_and_monomer_fail(self):
        cases = [dict(termini={'N': 'acetyl'}), dict(linkages=[{'position': 1, 'monomer_ref': 'monomer:custom@1'}]),
                 dict(unknown_chemistry='retained')]
        for identity in cases:
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.build(snapshot(component('A', **identity)))
        custom = 'monomer:custom@1'
        with self.assertRaisesRegex(ValueError, 'unsupported fields'):
            self.build(snapshot(component('A', modifications=[{'position': 1, 'monomer_ref': custom}]),
                                monomers={custom: {'identity': {'smiles': 'NCC(=O)O'}}}))
        self.assertFalse(self.bundle.exists())

    def test_bad_atom_selection_and_non_single_bonds_fail(self):
        bond = {'from': {'chain_id': 'A', 'position': 2, 'atom': 'SG/*'},
                'to': {'chain_id': 'B', 'position': 1, 'atom': 'SG'}}
        with self.assertRaisesRegex(ValueError, 'selection syntax'):
            self.build(snapshot(component('A'), component('B', sequence='CGGA'), bonds=[bond]))
        bond['from']['atom'] = 'SG'; bond['order'] = 2
        with self.assertRaisesRegex(ValueError, 'single covalent'):
            self.build(snapshot(component('A'), component('B', sequence='CGGA'), bonds=[bond]))

    def test_short_polymer_is_rejected_without_silent_native_filtering(self):
        for kind, sequence in [('protein', 'ACG'), ('dna', 'ACG'), ('rna', 'ACG')]:
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'shorter than four'):
                self.build(snapshot(component('A', kind, sequence=sequence)))


@unittest.skipUnless(os.environ.get('BIO_RF3_NATIVE_TESTS') == '1', 'requires the pinned actual RF3 environment')
class NativeTests(Fixture):
    def validate(self, source, assets=None):
        metadata = self.build(source, assets)
        result = a.preflight(self.bundle, metadata)
        self.assertTrue(result['native_parser'])
        self.assertFalse(result['msa_queries'])
        self.assertFalse(result['model_inference'])
        self.assertIn('AddAF3ChiralFeatures', result['chemistry_transforms'])
        return result

    def test_mixed_modified_polymer_ccd_charge_tetrahedral_and_ez(self):
        result = self.validate(snapshot(component('P', modifications=[{'position': 4, 'ccd': 'MSE'}]),
             component('D', 'dna', sequence='ACGT', modifications=[{'position': 2, 'ccd': '5CM'}]),
             component('R', 'rna', sequence='ACGU', modifications=[{'position': 4, 'ccd': 'PSU'}]),
             component('L', 'small_molecule', smiles='C[C@H](O)F'),
             component('Q', 'small_molecule', smiles='C[NH2+]C'),
             component('E', 'small_molecule', smiles='F/C=C/F'), component('Z', 'small_molecule', ccd='ZN')))
        self.assertEqual(result['ligands']['Q']['formal_charge'], 1)
        self.assertEqual(len(result['chains']), 7)

    def test_native_circular_backbone_is_present_in_model_token_features(self):
        result = self.validate(snapshot(component('A', sequence='ACGG', circular=True)))
        self.assertEqual(len(result['connections']['requested_bonds']), 1)
        self.assertEqual(len(result['token_bonds']), 1)

    def test_polymer_crosslink_is_refused_when_native_model_features_ignore_it(self):
        bond = {'from': {'chain_id': 'A', 'position': 2, 'atom': 'SG'},
                'to': {'chain_id': 'B', 'position': 1, 'atom': 'SG'}}
        metadata = self.build(snapshot(component('A'), component('B', sequence='CGGA'), bonds=[bond]))
        with self.assertRaisesRegex(ValueError, 'does not condition this requested bond'):
            a.preflight(self.bundle, metadata)

    def test_named_custom_ligand_covalent_bond_survives_actual_native_features(self):
        bond = {'from': {'chain_id': 'P', 'position': 2, 'atom': 'SG'},
                'to': {'chain_id': 'L', 'position': 1, 'atom': 'S0'}}
        result = self.validate(snapshot(component('P'), component('L', 'small_molecule', smiles='CS'), bonds=[bond]))
        self.assertTrue(result['ligands']['L']['reference_graph_verified'])
        self.assertTrue(result['ligands']['L']['covalently_connected'])

    def test_runtime_annotation_correction_preserves_original_and_every_chemical_array(self):
        import numpy as np
        from rf3.utils.inference import InferenceInput
        import rf3_compat
        rf3_compat.install()
        parsed = InferenceInput.from_json_dict({'name': 'ligand', 'components': [{'chain_id': 'A', 'smiles': 'C[C@H](O)F'}]})
        before = parsed.atom_array.copy()
        raw = InferenceInput.to_pipeline_input.__wrapped__(parsed)['atom_array']
        corrected = parsed.to_pipeline_input()['atom_array']
        self.assertIn('atom_id', raw.get_annotation_categories())
        self.assertNotIn('atom_id', corrected.get_annotation_categories())
        self.assertEqual(set(raw.get_annotation_categories())-{'atom_id'}, set(corrected.get_annotation_categories()))
        for name in corrected.get_annotation_categories():
            np.testing.assert_array_equal(raw.get_annotation(name), corrected.get_annotation(name))
        np.testing.assert_array_equal(raw.coord, corrected.coord)
        np.testing.assert_array_equal(raw.bonds.as_array(), corrected.bonds.as_array())
        np.testing.assert_array_equal(before.coord, parsed.atom_array.coord)
        np.testing.assert_array_equal(before.atom_id, parsed.atom_array.atom_id)

    def test_native_exact_sdf_stereochemistry_and_disconnected_graph(self):
        from rdkit import Chem
        from rdkit.Chem import AllChem
        mol = Chem.AddHs(Chem.MolFromSmiles('C[C@@H](O)F.[Na+]'))
        self.assertEqual(AllChem.EmbedMolecule(mol, randomSeed=4), 0)
        source = self.root/'ligand.sdf'
        with Chem.SDWriter(str(source)) as writer:
            writer.write(mol)
        raw = source.read_bytes()
        item = component('L', 'small_molecule', structure_file='attachments/source.sdf', structure_format='sdf')
        result = self.validate(snapshot(item), {item['construct_ref']: {'attachments/source.sdf': source}})
        self.assertEqual((self.bundle/'ligands/L.sdf').read_bytes(), raw)
        self.assertEqual(result['ligands']['L']['formal_charge'], 1)

    def test_unsafe_isotope_radical_and_enhanced_stereo_refused_before_native_transform(self):
        for smiles in ('[13CH4]', '[CH3]', 'C[C@H](O)F |&1:1|'):
            with self.subTest(smiles=smiles), self.assertRaises(ValueError):
                self.build(snapshot(component('L', 'small_molecule', smiles=smiles)))

    def test_missing_named_covalent_atom_rejected(self):
        bond = {'from': {'chain_id': 'A', 'position': 2, 'atom': 'DOES_NOT_EXIST'},
                'to': {'chain_id': 'B', 'position': 1, 'atom': 'SG'}}
        metadata = self.build(snapshot(component('A'), component('B', sequence='CGGA'), bonds=[bond]))
        with self.assertRaises(Exception):
            a.preflight(self.bundle, metadata)


if __name__ == '__main__':
    unittest.main()
