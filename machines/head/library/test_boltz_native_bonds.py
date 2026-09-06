"""Actual pinned Boltz feature-array round trips; no query or inference."""
import copy
import importlib.metadata
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import boltz_adapter

try:
    PINNED = importlib.metadata.version('boltz') == '2.2.1'
except importlib.metadata.PackageNotFoundError:
    PINNED = False


@unittest.skipUnless(PINNED, 'requires the pinned Boltz 2.2.1 native environment and canonical CCD cache')
class NativeBondTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from boltz.data import const
        from boltz.data.mol import load_canonicals
        from boltz.data.parse.schema import parse_boltz_schema
        cls.covalent = const.bond_type_ids['COVALENT']
        cls.native = {'version': 1, 'sequences': [
            {'protein': {'id': 'A', 'sequence': 'CAC', 'msa': 'empty'}},
            {'protein': {'id': 'B', 'sequence': 'GC', 'msa': 'empty'}},
            {'protein': {'id': 'C', 'sequence': 'C', 'msa': 'empty'}}],
            'constraints': [
                {'bond': {'atom1': ['A', 1, 'SG'], 'atom2': ['A', 3, 'SG']}},
                {'bond': {'atom1': ['B', 2, 'SG'], 'atom2': ['C', 1, 'SG']}}]}
        moldir = Path(os.environ.get('BOLTZ_CACHE', '/mnt/bio-shared/cache/boltz'))/'mols'
        cls.target = parse_boltz_schema('explicit-bond-fixture', cls.native,
                                       load_canonicals(str(moldir)), moldir, boltz_2=True)

    def test_native_intra_and_inter_chain_bonds_preserve_named_atoms(self):
        proof = boltz_adapter._verify_explicit_bonds(self.native, self.target.structure, self.covalent)
        self.assertEqual(len(proof), 2)
        self.assertEqual((proof[0]['native']['res_1'], proof[0]['native']['res_2']), (0, 2))
        self.assertEqual((proof[1]['native']['chain_1'], proof[1]['native']['chain_2']), (1, 2))
        self.assertEqual((proof[1]['native']['res_1'], proof[1]['native']['res_2']), (4, 5))
        for bond in proof:
            self.assertEqual(bond['native']['type'], self.covalent)
            for side in ('1', '2'):
                self.assertEqual(str(self.target.structure.atoms[bond['native']['atom_' + side]]['name']), 'SG')
        reversed_input = copy.deepcopy(self.native)
        for constraint in reversed_input['constraints']:
            bond = constraint['bond']
            bond['atom1'], bond['atom2'] = bond['atom2'], bond['atom1']
        self.assertEqual(len(boltz_adapter._verify_explicit_bonds(reversed_input, self.target.structure, self.covalent)), 2)

    def test_preflight_rejects_dropped_or_redirected_feature_bond(self):
        structure = self.target.structure
        original = structure.bonds
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path/'input.yaml').write_text(json.dumps(self.native))
            metadata = {'entrypoint': 'input.yaml', 'expected_chains': ['A', 'B', 'C']}
            mutations = {'dropped': original[:-1].copy(), 'redirected': original.copy(), 'wrong_type': original.copy()}
            mutations['redirected'][-1]['atom_2'] -= 1
            mutations['wrong_type'][-1]['type'] = 1
            for name, modified in mutations.items():
                with self.subTest(name=name):
                    audited = SimpleNamespace(chains=structure.chains, residues=structure.residues,
                                              atoms=structure.atoms, bonds=modified)
                    with patch('boltz.data.parse.schema.parse_boltz_schema', return_value=SimpleNamespace(structure=audited)):
                        with self.assertRaisesRegex(ValueError, 'exact requested covalent bond'):
                            boltz_adapter.preflight(path, metadata)


if __name__ == '__main__':
    unittest.main()
