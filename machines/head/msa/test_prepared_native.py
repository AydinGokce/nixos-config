"""Native parser checks; run in a pinned model environment with Biotite installed."""
import gzip
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("prepared", Path(__file__).with_name("prepared.py"))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

try:
    import biotite.structure.io.pdbx
    HAS_BIOTITE = True
except ImportError:
    HAS_BIOTITE = False

try:
    from boltz.data.parse.csv import parse_csv
except ImportError:
    parse_csv = None


@unittest.skipUnless(HAS_BIOTITE, "Run in pinned native environment for Biotite CIF parser")
class NativeMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.current, self.obsolete = self.root / "current", self.root / "obsolete"
        self.receipt = {"database": {"pdbdivided": str(self.current), "pdbobsolete": str(self.obsolete)}}

    def cif(self, root, rows):
        path = root / "ab/1abc.cif.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt") as handle:
            handle.write("data_1abc\nloop_\n_pdbx_poly_seq_scheme.asym_id\n_pdbx_poly_seq_scheme.pdb_strand_id\n" + rows + "\n#\n")

    def test_preserves_unobserved_chains_and_multiple_labels_per_author(self):
        # No atom_site at all: an observed-atom-only mapping would lose every row.
        self.cif(self.current, "A X\nA X\nAA X\nB 7")
        result = p.local_template_mappings({"1abc"}, self.receipt, self.root / "out")
        self.assertEqual(result, {"1abc": {"A": "X", "AA": "X", "B": "7"}})
        self.assertTrue((self.root / "out/1abc.cif").is_file())

    def test_current_precedes_obsolete_and_obsolete_fallback_is_explicit(self):
        self.cif(self.obsolete, "A OLD")
        self.assertEqual(p.local_template_mappings({"1abc"}, self.receipt, self.root / "old")["1abc"], {"A": "OLD"})
        self.cif(self.current, "A NEW")
        self.assertEqual(p.local_template_mappings({"1abc"}, self.receipt, self.root / "new")["1abc"], {"A": "NEW"})

    def test_missing_and_ambiguous_mapping_fail_without_remote_fallback(self):
        with self.assertRaisesRegex(p.Error, "missing from the private"):
            p.local_template_mappings({"1abc"}, self.receipt, self.root / "missing")
        self.cif(self.current, "A X\nA Y")
        with self.assertRaisesRegex(p.Error, "ambiguous mapping"):
            p.local_template_mappings({"1abc"}, self.receipt, self.root / "ambiguous")

    def test_materialized_native_npz_members_remain_integrity_bound(self):
        import numpy as np
        run = self.root / "of3"
        run.mkdir()
        archive = run / "msa.npz"
        data = {"msa": np.array([list("ACDE"), list("AC-E")]),
                "deletion_matrix": np.zeros((2, 4)), "metadata": np.array(["query", "hit"])}
        np.savez(archive, main=data)
        p.write_json(run / "inference_query_set.json", {"queries": {"query": {"chains": [{
            "molecule_type": "protein", "chain_ids": ["A"], "sequence": "ACDE",
            "main_msa_file_paths": [str(archive)]}]}}})
        p.write_json(run / "experiment_config.json", {"experiment_settings": {"use_templates": False}})
        bundle = self.root / "bundle"
        p.capture("openfold3", run, bundle, trust_native_npz=True)
        output = self.root / "native"
        p.materialize(bundle, "openfold3", output)
        manifest = p.validate_materialized(output, "openfold3")
        rel = next(rel for rel in manifest["files"] if rel.endswith(".npz"))
        target = output / rel
        data["deletion_matrix"][1, 1] = 2
        np.savez(target, main=data)
        manifest["files"][rel].update(bytes=target.stat().st_size, sha256=p.digest(target))
        p.write_json(output / "source_manifest.json", manifest)
        with self.assertRaisesRegex(p.Error, "Native NPZ changed"):
            p.validate_materialized(output, "openfold3")


@unittest.skipUnless(parse_csv is not None, "Run in Boltz environment for its actual CSV parser")
class NativeBoltzTests(unittest.TestCase):
    def test_pair_keys_duplicates_and_insertions_follow_native_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            path.write_text("key,sequence\n0,ACDE\n7,AcC-E\n-1,ACDE\n-1,-CDE\n")
            parsed = parse_csv(path)
            self.assertEqual(parsed.sequences["taxonomy"].tolist(), [0, 7, -1])
            self.assertEqual(parsed.deletions["res_idx"].tolist(), [1])
            self.assertEqual(parsed.deletions["deletion"].tolist(), [1])
            self.assertEqual(len(parsed.residues), 12)


if __name__ == "__main__":
    unittest.main()
