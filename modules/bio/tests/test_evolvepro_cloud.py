"""Cloud input preflight, plus optional regression checks using an installed env."""

import argparse
import csv
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PY_DIR = Path(__file__).resolve().parents[1] / "py"
sys.path.insert(0, str(PY_DIR))
import evolvepro_cloud as cloud


class TemporaryFiles(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text)
        return path


class InputValidation(TemporaryFiles):
    def test_identifier_strings_and_multiline_sequences(self):
        fasta = self.write("variants.fasta", ">001 reference\nmkt\na\n>NA\nACBX\n")
        labels = self.write("labels.csv", "variant,activity\n001,1.5\n")
        variants = cloud.read_variants(fasta)
        self.assertEqual(variants, {"001": "MKTA", "NA": "ACBX"})
        self.assertEqual(cloud.read_labels(labels, variants), {"001": 1.5})

    def test_rejects_ambiguous_or_malformed_fasta(self):
        cases = [
            (">x\nAA\n>x\nGG\n", "duplicate"),
            (">x\n>x2\nAA\n", "empty sequence"),
            ("AA\n>x\nGG\n", "before first header"),
            (">x\nAC*D\n", "amino-acid letters"),
            (">\nAC\n", "missing variant"),
        ]
        for fasta, message in cases:
            with self.subTest(fasta=fasta), self.assertRaisesRegex(ValueError, message):
                cloud.read_variants(self.write("variants.fasta", fasta))

    def test_rejects_unknown_duplicate_and_nonfinite_labels(self):
        for rows, message in [
            ("missing,1\n", "absent from FASTA"),
            ("x,1\nx,2\n", "duplicate"),
            ("x,nan\n", "finite"),
            ("x,inf\n", "finite"),
            ("x,\n", "finite"),
        ]:
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, message):
                cloud.read_labels(self.write("labels.csv", "variant,activity\n" + rows), {"x": "AA"})

    def test_preflight_requires_unmeasured_candidates(self):
        fasta = self.write("variants.fasta", ">x\nAA\n")
        labels = self.write("labels.csv", "variant,activity\nx,1\n")
        args = cloud.parser().parse_args([
            "--validate-only", "--input", str(fasta), "--labels", str(labels),
        ])
        with self.assertRaisesRegex(ValueError, "all FASTA variants are measured"):
            cloud.run(args)

    def test_round_size_must_be_positive(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            cloud.positive_int("0")


@unittest.skipUnless(os.environ.get("EVOLVEPRO_CORE_PYTHON"), "set EVOLVEPRO_CORE_PYTHON for real regression")
class RegressionIntegration(TemporaryFiles):
    def evolve(self, embeddings, labels=None):
        source = self.write("embeddings.csv", embeddings)
        target = self.root / "next.csv"
        full = self.root / "full.csv"
        command = [
            os.environ["EVOLVEPRO_CORE_PYTHON"], str(PY_DIR / "evolvepro_cli.py"), "evolve",
            "--embeddings", str(source), "--out", str(target), "--full", str(full), "--n", "2",
        ]
        if labels is not None:
            command += ["--labels", str(self.write("labels.csv", labels))]
        subprocess.run(command, check=True, capture_output=True, text=True)
        with target.open() as source:
            selected = list(csv.DictReader(source))
        self.assertTrue(full.is_file())
        return selected

    def test_measured_numeric_id_excluded_and_na_id_preserved(self):
        selected = self.evolve(
            "variant,emb_0,emb_1\n001,0,1\n002,1,0\nNA,2,0\n",
            "variant,activity\n001,1.2\n",
        )
        self.assertEqual({row["variant"] for row in selected}, {"002", "NA"})
        self.assertTrue(all("predicted_activity" in row for row in selected))

    def test_identical_embeddings_have_nonempty_diverse_selection(self):
        selected = self.evolve("variant,emb_0,emb_1\na,1,1\nb,1,1\nc,1,1\n")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["strategy"], "first_round_kmeans_diverse")


if __name__ == "__main__":
    unittest.main()
