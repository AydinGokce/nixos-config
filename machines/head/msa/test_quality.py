"""Numerical score invariants and strict prediction sequence validation."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

try:
    spec = importlib.util.spec_from_file_location("quality", Path(__file__).with_name("quality.py"))
    q = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(q)
except ImportError:
    q = None


@unittest.skipIf(q is None, "NumPy and Biopython analysis environment required")
class QualityTests(unittest.TestCase):
    def setUp(self):
        self.reference = q.np.asarray([[0., 0., 0.], [3., 0., 0.], [1., 4., 0.], [1., 1., 5.]])

    def test_rigid_rotation_and_translation_do_not_change_accuracy(self):
        rotation = q.np.asarray([[0., 1., 0.], [-1., 0., 0.], [0., 0., 1.]])
        result = q.ca_metrics(self.reference, self.reference @ rotation + 123.45)
        self.assertAlmostEqual(result["ca_rmsd_angstrom"], 0.)
        self.assertEqual(result["lddt_ca_mean"], 1.)
        self.assertEqual(result["reference_contact_pairs"], 6)

    def test_mirror_is_not_permitted_as_a_rigid_superposition(self):
        mirror = self.reference.copy()
        mirror[:, 0] *= -1
        result = q.ca_metrics(self.reference, mirror)
        self.assertGreater(result["ca_rmsd_angstrom"], 1.)
        # Distance scores alone cannot detect chirality. Report both metrics.
        self.assertEqual(result["lddt_ca_mean"], 1.)

    def test_local_distortion_reduces_score_and_increases_rmsd(self):
        altered = self.reference.copy()
        altered[-1] += [7., 7., 7.]
        result = q.ca_metrics(self.reference, altered)
        self.assertLess(result["lddt_ca_mean"], 0.8)
        self.assertGreater(result["ca_rmsd_angstrom"], 1.)

    def test_missing_and_nonfinite_coordinates_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Corresponding"):
            q.ca_metrics(self.reference, self.reference[:-1])
        broken = self.reference.copy()
        broken[1, 0] = q.np.nan
        with self.assertRaisesRegex(ValueError, "Nonfinite"):
            q.ca_metrics(self.reference, broken)

    def test_prediction_sequence_mismatch_or_missing_atom_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "predicted.pdb"
            lines = []
            for idx, (residue, coords) in enumerate(zip(["ALA", "CYS", "ASP", "GLU"], self.reference), 1):
                x, y, z = coords
                lines.append(f"ATOM  {idx:5d}  CA  {residue} A{idx:4d}    {x:8.3f}{y:8.3f}{z:8.3f}  1.00 90.00           C  \n")
            path.write_text("".join(lines) + "END\n")
            self.assertTrue(q.np.array_equal(q.prediction_ca(path, "ACDE"), self.reference))
            path.write_text("MODEL        1\n" + "".join(lines) + "ENDMDL\nMODEL        2\n" + "".join(lines) + "ENDMDL\nEND\n")
            with self.assertRaisesRegex(ValueError, "Multi-model"):
                q.prediction_ca(path, "ACDE")
            path.write_text(lines[0] + "".join(lines) + "END\n")
            with self.assertRaises(q.PDBConstructionException):
                q.prediction_ca(path, "ACDE")
            path.write_text("".join(lines) + "END\n")
            with self.assertRaisesRegex(ValueError, "complete submitted sequence"):
                q.prediction_ca(path, "ACDF")
            path.write_text("".join(lines).replace("  CA  GLU", "  N   GLU") + "END\n")
            with self.assertRaisesRegex(ValueError, "missing C-alpha"):
                q.prediction_ca(path, "ACDE")

    def test_native_cif_without_occupancy_preserves_coordinates_and_source_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.pdb"
            lines = []
            for index, (residue, coords) in enumerate(zip(["ALA", "CYS", "ASP", "GLU"], self.reference), 1):
                x, y, z = coords
                lines.append(f"ATOM  {index:5d}  CA  {residue} A{index:4d}    {x:8.3f}{y:8.3f}{z:8.3f}  1.00 90.00           C  \n")
            path.write_text("".join(lines) + "END\n")
            cif = path.with_suffix(".cif")
            writer = q.MMCIFIO()
            writer.set_structure(q.structure(path))
            writer.save(str(cif))
            data = q.MMCIF2Dict(str(cif))
            del data["_atom_site.occupancy"]
            writer.set_dict(data)
            writer.save(str(cif))
            original = cif.read_bytes()
            self.assertTrue(q.np.array_equal(q.prediction_ca(cif, "ACDE"), self.reference))
            self.assertEqual(cif.read_bytes(), original)
            data["_atom_site.label_alt_id"][0] = "A"
            writer.set_dict(data)
            writer.save(str(cif))
            with self.assertRaisesRegex(ValueError, "alternate conformers"):
                q.prediction_ca(cif, "ACDE")


if __name__ == "__main__":
    unittest.main()
