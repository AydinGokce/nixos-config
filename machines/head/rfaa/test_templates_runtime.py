"""Optional integration tests against the installed, patched RFAA environment.

Run with RFAA_SOURCE=/path/to/rfaa /path/to/rfaa-venv/bin/python -m unittest
discover -s machines/head/rfaa -p test_templates_runtime.py. No GPU is needed.
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest


@unittest.skipUnless(os.environ.get("RFAA_SOURCE"), "set RFAA_SOURCE to test the installed parser")
class TemplateRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.environ["RFAA_SOURCE"])
        from hydra import compose, initialize_config_dir
        from rf2aa.chemical import initialize_chemdata
        with initialize_config_dir(version_base=None, config_dir=str(
                Path(os.environ["RFAA_SOURCE"]).resolve() / "rf2aa/config/inference")):
            config = compose(config_name="protein")
        initialize_chemdata(config.chem_params)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rfaa-templates-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def template(self, resolved):
        from collections import namedtuple
        from rf2aa.ffindex import read_data, read_index
        pdb = []
        for residue in range(1, resolved + 1):
            for atom in (" N  ", " CA ", " C  ", " O  "):
                pdb.append(f"ATOM  {len(pdb)+1:5d} {atom} ALA A{residue:4d}    "
                           f"{float(residue):8.3f}{2.:8.3f}{3.:8.3f}{1.:6.2f}{90.:6.2f}")
        data = ("\n".join(pdb) + "\n\0").encode()
        (self.root / "pdb.ffdata").write_bytes(data)
        (self.root / "pdb.ffindex").write_text(f"template\t0\t{len(data)}\n")
        # A worker must be able to consume a read-only database mount.
        for path in self.root.glob("pdb.*"):
            path.chmod(0o444)
        self.hhr = self.root / "hits.hhr"
        self.hhr.write_text(">template\nProbab=99.9 E-value=1e-10 Score=50.0 "
                            "Aligned_cols=20 Identities=100% Similarity=1.0 "
                            "Sum_probs=20.0 Template_Neff=1.0\n")
        self.atab = self.root / "hits.atab"
        self.atab.write_text(">template\ni j score SS probab\n" + "".join(
            f"{i} {i} 1.0 1.0 1.0\n" for i in range(1, 21)))
        database = namedtuple("FFDB", "index data")
        self.ffdb = database(read_index(str(self.root / "pdb.ffindex")),
                             read_data(str(self.root / "pdb.ffdata")))
        self.addCleanup(self.ffdb.data.close)

    def features(self):
        from rf2aa.data.protein import get_templates
        return get_templates(20, self.ffdb, str(self.hhr), str(self.atab),
                             seqID_cut=150, n_templ=4, deterministic=True)

    def test_short_resolved_hits_use_blank_templates(self):
        self.template(9)
        xyz, t1d, mask, ids = self.features()
        self.assertEqual(tuple(xyz.shape[:2]), (4, 20))
        self.assertEqual(int(mask.sum()), 0)

    def test_ten_resolved_residues_are_retained(self):
        self.template(10)
        xyz, t1d, mask, ids = self.features()
        self.assertGreater(int(mask.sum()), 0)
        self.assertEqual(ids.tolist(), ["template"])

    def test_malformed_tabulation_is_not_hidden(self):
        self.template(20)
        self.atab.write_text(">template\nnot-a-number 1 1.0 1.0 1.0\n")
        with self.assertRaises(ValueError):
            self.features()


if __name__ == "__main__":
    unittest.main()
