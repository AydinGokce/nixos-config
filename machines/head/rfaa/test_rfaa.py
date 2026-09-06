"""Small local fixtures exercise installation, failure recovery, and search flow."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import databases
import prepare


def fixture_database(directory, dataset):
    directory.mkdir(parents=True, exist_ok=True)
    for component in dataset["components"]:
        stem = directory / f'{dataset["prefix"]}_{component}'
        Path(f"{stem}.ffdata").write_bytes(b"HEADER\n" + b"A" * 2041)
        Path(f"{stem}.ffindex").write_text("first\t0\t1024\nlast\t1024\t1024\n")
    return databases.validate_directory(directory, dataset)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rfaa-databases-test-")
        self.root = Path(self.temp.name)
        self.dataset = {"directory": "small", "prefix": "small", "components": ["a3m"],
                        "archive": "small.tar.gz", "space_gib": 0, "url": "unused"}
        source = self.root / "source"
        fixture_database(source, self.dataset)
        self.archive = self.root / "fixture.tar.gz"
        with tarfile.open(self.archive, "w:gz") as archive:
            for item in source.iterdir():
                archive.add(item, arcname=item.name)
        self.dataset.update(bytes=self.archive.stat().st_size,
                            md5=hashlib.md5(self.archive.read_bytes()).hexdigest())
        self.target = self.root / "target"
        self.target.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def stage_archive(self, partial=False):
        archives = self.target / "archives"
        archives.mkdir()
        path = archives / (self.dataset["archive"] + (".part" if partial else ""))
        shutil.copyfile(self.archive, path)
        return path

    def test_finished_partial_is_promoted_without_redownload(self):
        self.stage_archive(partial=True)
        with patch.dict(databases.DATASETS, {"small": self.dataset}):
            databases.install(self.target, "small", False)
            databases.validate(self.target, ["small"])
            databases.install(self.target, "small", False)
        self.assertFalse((self.target / "archives" / self.dataset["archive"]).exists())

    def test_md5_failure_never_promotes_database(self):
        self.stage_archive()
        self.dataset["md5"] = "0" * 32
        with patch.dict(databases.DATASETS, {"small": self.dataset}):
            with self.assertRaisesRegex(RuntimeError, "MD5 mismatch"):
                databases.install(self.target, "small", False)
        self.assertFalse((self.target / "small").exists())

    def test_receipt_detects_truncation(self):
        final = self.target / "small"
        files = fixture_database(final, self.dataset)
        databases.receipt(final, self.dataset, files)
        (final / "small_a3m.ffdata").write_bytes(b"x" * 1024)
        with patch.dict(databases.DATASETS, {"small": self.dataset}):
            with self.assertRaisesRegex(RuntimeError, "Truncated"):
                databases.validate(self.target, ["small"])

    def test_single_sequence_placeholder_is_rejected(self):
        final = self.target / "small"
        fixture_database(final, self.dataset)
        (final / "small_a3m.ffdata").write_bytes(b"\0")
        with self.assertRaisesRegex(RuntimeError, "placeholder"):
            databases.validate_directory(final, self.dataset)


FAKE_TOOL = r'''
import json, os
from pathlib import Path
import sys
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as handle:
    handle.write(json.dumps([tool, args]) + "\n")
if os.environ.get("FAIL_TOOL") == tool:
    sys.exit(7)
def opt(name):
    return args[args.index(name) + 1]
if tool == "hhblits":
    n = 2001 if os.environ.get("DEEP_MSA") else 2
    Path(opt("-oa3m")).write_text((">query\nACDEFGHIKLMNPQRSTVWY\n") * n)
elif tool == "hhfilter":
    Path(opt("-o")).write_bytes(Path(opt("-i")).read_bytes())
elif tool == "csbuild":
    Path(opt("-o")).write_text("checkpoint")
elif tool == "makemat":
    Path(opt("-P") + ".mtx").write_text("matrix")
elif tool == "psipred":
    print("pass1")
elif tool == "psipass2":
    print("Pred: " + "C" * 20)
    print("Conf: " + "9" * 20)
elif tool == "hhsearch":
    Path(opt("-o")).write_text("HHsearch results: no templates\n")
    Path(opt("-atab")).write_text("")
'''


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rfaa preparation ")
        self.root = Path(self.temp.name)
        self.db = self.root / "databases"
        for dataset in databases.DATASETS.values():
            folder = self.db / dataset["directory"]
            databases.receipt(folder, dataset, fixture_database(folder, dataset))
        self.query = self.root / "query with spaces.fasta"
        self.query.write_text(">test\nACDEFGHIKLMNPQRSTVWY\n")
        self.out = self.root / "output with spaces"
        bindir = self.root / "bin"
        bindir.mkdir()
        for tool in ("hhblits", "hhfilter", "hhsearch", "csbuild", "makemat", "psipred", "psipass2"):
            path = bindir / tool
            path.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
            path.chmod(0o755)
        self.env = patch.dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                              FAKE_LOG=str(self.root / "commands.jsonl"),
                              PSIPRED_DATA=str(self.root), CSBLAST_DATA=str(self.root))
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def commands(self):
        return [json.loads(row) for row in (self.root / "commands.jsonl").read_text().splitlines()]

    def test_full_search_and_cached_retry(self):
        prepare.prepare(self.query, self.out, "full", self.db, 2, 8)
        commands = self.commands()
        self.assertEqual(sum(tool == "hhblits" for tool, _ in commands), 4)
        self.assertTrue(any("bfd" in " ".join(args) for tool, args in commands if tool == "hhblits"))
        self.assertTrue((self.out / "t000_.hhr").is_file())
        self.assertEqual(json.loads((self.out / "preparation.json").read_text())["msa_sequences"], 2)
        prepare.prepare(self.query, self.out, "full", self.db, 2, 8)
        self.assertEqual(self.commands(), commands)

    def test_deep_uniref_alignment_skips_bfd(self):
        with patch.dict(os.environ, DEEP_MSA="1"):
            prepare.prepare(self.query, self.out, "full", self.db, 2, 8)
        self.assertEqual(sum(tool == "hhblits" for tool, _ in self.commands()), 1)

    def test_search_failure_does_not_fallback(self):
        with patch.dict(os.environ, FAIL_TOOL="hhblits"):
            with self.assertRaises(subprocess.CalledProcessError):
                prepare.prepare(self.query, self.out, "full", self.db, 2, 8)
        self.assertFalse((self.out / "t000_.msa0.a3m").exists())
        self.assertFalse((self.out / "preparation.json").exists())
        prepare.prepare(self.query, self.out, "full", self.db, 2, 8)
        self.assertTrue((self.out / "preparation.json").exists())

    def test_single_seq_files_cannot_suppress_full_search(self):
        prepare.prepare(self.query, self.out, "single-seq", self.db, 2, 8)
        with self.assertRaisesRegex(ValueError, "different query or mode"):
            prepare.prepare(self.query, self.out, "full", self.db, 2, 8)

    def test_multiple_fasta_records_are_rejected(self):
        self.query.write_text(">a\nAAAA\n>b\nCCCC\n")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            prepare.fasta_sequence(self.query)


if __name__ == "__main__":
    unittest.main()
