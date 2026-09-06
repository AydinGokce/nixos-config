"""Selection regressions and actual RDKit/AtomWorks coordinate-stereo checks."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import rf3_output as output


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.directory = self.root/'case'
        self.directory.mkdir()
        self.expected = {'name': 'case'}
        for suffix in ('model.cif', 'summary_confidences.json', 'confidences.json', 'ranking_scores.csv'):
            (self.directory/('case_'+suffix)).write_text('original '+suffix)
        self.original = {p.name: p.read_bytes() for p in self.directory.iterdir()}

    def tearDown(self):
        self.temporary.cleanup()

    def sample(self, number, score, passes):
        directory = self.directory/f'seed-101_sample-{number}'
        directory.mkdir()
        prefix = 'case_'+directory.name
        (directory/(prefix+'_model.cif')).write_text('valid' if passes else 'flipped')
        (directory/(prefix+'_summary_confidences.json')).write_text(json.dumps({'ranking_score': score}))
        (directory/(prefix+'_confidences.json')).write_text(json.dumps({'sample': number}))
        return directory

    @staticmethod
    def audit(path, expected):
        return {'passed': path.read_text() == 'valid'}

    def test_highest_passing_native_rank_and_raw_outputs_are_preserved(self):
        self.sample(0, .99, False)
        valid = self.sample(1, .8, True)
        self.sample(2, .7, True)
        raw = {str(p): p.read_bytes() for p in self.directory.glob('seed-*/*')}
        receipt = output._select_and_publish(self.root, self.expected, self.audit)
        self.assertEqual(receipt['selected']['raw_directory'], 'case/seed-101_sample-1')
        self.assertEqual(receipt['selected']['ranking_score'], .8)
        self.assertEqual((self.directory/'case_model.cif').read_text(), 'valid')
        self.assertEqual(json.loads((self.directory/'case_confidences.json').read_text()), {'sample': 1})
        self.assertEqual((self.directory/'case_ranking_scores.csv').read_bytes(), self.original['case_ranking_scores.csv'])
        self.assertTrue(all(Path(p).read_bytes() == raw[p] for p in raw))
        for name, contents in self.original.items():
            self.assertEqual((self.root/'rf3-native-ranking-original'/name).read_bytes(), contents)
        for evidence in receipt['selected']['files'].values():
            self.assertEqual(hashlib.sha256((self.root/evidence['path']).read_bytes()).hexdigest(), evidence['sha256'])

    def test_all_flipped_retains_diagnostics_and_original_canonical(self):
        self.sample(0, .9, False)
        self.sample(1, .8, False)
        with self.assertRaisesRegex(output.Error, 'No existing RF3 sample'):
            output._select_and_publish(self.root, self.expected, self.audit)
        receipt = json.loads((self.root/output.DIAGNOSTICS).read_text())
        self.assertEqual(receipt['status'], 'failed')
        self.assertIsNone(receipt['selected'])
        self.assertEqual(len(receipt['samples']), 2)
        self.assertTrue(all((self.directory/name).read_bytes() == data for name, data in self.original.items()))

    def test_nonfinite_score_and_missing_confidence_fail_closed(self):
        self.sample(0, float('nan'), True)
        other = self.sample(1, .9, True)
        (other/('case_'+other.name+'_confidences.json')).unlink()
        with self.assertRaises(output.Error):
            output._select_and_publish(self.root, self.expected, self.audit)
        receipt = json.loads((self.root/output.DIAGNOSTICS).read_text())
        self.assertTrue(all('error' in row for row in receipt['samples']))

    def test_symlinked_sample_and_second_audit_are_refused(self):
        sample = self.sample(0, .8, True)
        (self.directory/'seed-101_sample-1').symlink_to(sample, target_is_directory=True)
        receipt = output._select_and_publish(self.root, self.expected, self.audit)
        self.assertFalse(receipt['samples'][1]['passed'])
        before = (self.root/output.DIAGNOSTICS).read_bytes()
        with self.assertRaisesRegex(output.Error, 'already has evidence'):
            output._select_and_publish(self.root, self.expected, self.audit)
        self.assertEqual((self.root/output.DIAGNOSTICS).read_bytes(), before)


@unittest.skipUnless(os.environ.get('BIO_RF3_NATIVE_TESTS') == '1', 'requires actual pinned RF3/RDKit environment')
class NativeGeometryTests(unittest.TestCase):
    @staticmethod
    def molecule(smiles):
        from rdkit import Chem
        from rdkit.Chem import AllChem
        from atomworks.io.tools.rdkit import atom_array_from_rdkit
        mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
        assert AllChem.EmbedMolecule(mol, randomSeed=17) == 0
        array = atom_array_from_rdkit(mol, remove_hydrogens=True)
        array.chain_id[:] = 'L'
        array.res_name[:] = 'LIG'
        array.res_id[:] = 1
        return array

    @staticmethod
    def expected(array):
        atoms, bonds = output._inventory(array)
        return {'atoms': [dict(identity=k, element=v[0], charge=v[1]) for k, v in atoms.items()],
                'bonds': [dict(atoms=k, order=v) for k, v in bonds.items()], 'tetrahedral': [], 'double_bonds': []}

    def test_actual_e_and_z_coordinates_and_unassigned_stereo(self):
        e, z = self.molecule('F/C=C/F'), self.molecule('F/C=C\\F')
        expected = self.expected(e)
        named = {str(e.atom_name[i]): output._key(e, i) for i in range(len(e))}
        expected['double_bonds'] = [dict(atoms=[named[n] for n in ('F0', 'C0', 'C1', 'F1')], expected='trans')]
        self.assertTrue(output.audit_array(e, expected)['passed'])
        failed = output.audit_array(z, expected)
        self.assertFalse(failed['passed'])
        self.assertEqual(failed['double_bonds'][0]['observed'], 'cis')
        expected['double_bonds'] = []
        self.assertTrue(output.audit_array(z, expected)['passed'])

    def test_actual_tetrahedral_reflection_and_planarity_are_rejected(self):
        import numpy as np
        array = self.molecule('C[C@H](O)F')
        expected = self.expected(array)
        keys = [output._key(array, i) for i in range(4)]
        angle = output._dihedral(array.coord[:4])
        expected['tetrahedral'] = [dict(atoms=keys, expected_sign=1 if angle > 0 else -1)]
        self.assertTrue(output.audit_array(array, expected)['passed'])
        reflected = array.copy(); reflected.coord[:, 0] *= -1
        self.assertFalse(output.audit_array(reflected, expected)['passed'])
        planar = array.copy(); planar.coord[:, 2] = 0
        self.assertFalse(output.audit_array(planar, expected)['passed'])
        self.assertFalse(np.array_equal(array.coord, reflected.coord))

    def test_atom_names_elements_charges_bonds_and_finite_coordinates(self):
        import numpy as np
        array = self.molecule('C[NH2+]C')
        expected = self.expected(array)
        cases = []
        changed = array.copy(); changed.charge[1] = 0; cases.append(changed)
        changed = array.copy(); changed.element[0] = 'O'; cases.append(changed)
        changed = array.copy(); changed.atom_name[0] = 'other'; cases.append(changed)
        changed = array.copy(); changed.coord[0, 0] = np.nan; cases.append(changed)
        changed = array.copy(); changed.bonds.remove_bond(0, 1); cases.append(changed)
        for changed in cases:
            with self.subTest(array=changed):
                self.assertFalse(output.audit_array(changed, expected)['passed'])
        original = array.coord.copy()
        self.assertTrue(output.audit_array(array, expected)['passed'])
        np.testing.assert_array_equal(array.coord, original)

    def test_native_expected_preserves_explicit_ez_and_tetrahedral_constraints(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'input.json'
            path.write_text(json.dumps([{'name': 'proof', 'components': [
                {'chain_id': 'E', 'res_name': 'L:ez-proof', 'smiles': 'F/C=C/F'},
                {'chain_id': 'L', 'res_name': 'L:tetra-proof', 'smiles': 'C[C@H](O)F'}]}]))
            raw = path.read_bytes()
            result = output._expected(path)
            self.assertEqual(len(result['double_bonds']), 1)
            self.assertEqual(result['double_bonds'][0]['expected'], 'trans')
            self.assertGreater(len(result['tetrahedral']), 0)
            self.assertEqual(result['prepared_files_sha256']['input.json'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(path.read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
