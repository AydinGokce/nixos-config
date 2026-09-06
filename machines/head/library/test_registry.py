"""Registry tests exercise immutable publication, chemical references and backups."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import registry as r


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "library"
        self.library = r.Registry(self.root)
        self.library.init()

    def tearDown(self):
        self.tmp.cleanup()

    def protein(self, ident="enzyme", **kwargs):
        document = {"kind": "construct", "id": ident, "name": "Enzyme",
                    "identity": {"molecule_type": "protein", "sequence": "ACDEFGHIK"}, **kwargs}
        return self.library.import_record(document)

    def monomer(self, ident="modified-a", **kwargs):
        return self.library.import_record({"kind": "monomer", "id": ident,
                                           "identity": {"description": "Unresolved custom residue", **kwargs}})

    def test_import_revision_and_original_bytes(self):
        source = self.base / "original.fasta"
        source.write_bytes(b">enzyme original\r\nACDE\r\nFGHIK\r\n")
        imported = self.library.import_record(r.fasta_document(source, "protein", "enzyme"),
                                             {"original.fasta": source})
        ref = r.reference(imported)
        self.assertEqual(ref, "construct:enzyme@1")
        self.assertEqual(imported["status"], "defined")
        self.assertEqual(self.library.attachment_path(ref, "attachments/original.fasta").read_bytes(), source.read_bytes())
        first_bytes = self.library.record_path(ref).read_bytes()
        second = self.library.revise(ref, {"identity": {"molecule_type": "protein", "sequence": "ACDEFGHIKL"}})
        self.assertEqual(second["revision"], 2)
        self.assertIn(ref, second["parents"])
        self.assertEqual(self.library.resolve("enzyme"), "construct:enzyme@2")
        self.assertEqual(self.library.record_path(ref).read_bytes(), first_bytes)
        self.assertEqual(self.library.show(ref)["identity"]["sequence"], "ACDEFGHIK")
        self.assertEqual(self.library.verify()["records"], 2)
        with self.assertRaisesRegex(r.Error, "stale revision"):
            self.library.revise(ref, {"notes": "This must not overwrite a later edit"})

    def test_namespace_reserves_historical_aliases_and_ids(self):
        self.protein(aliases=["target", "old-name"])
        self.library.revise("enzyme", {"aliases": ["new-name"]})
        self.assertEqual(self.library.resolve("old-name"), "construct:enzyme@2")
        self.assertEqual(self.library.resolve("construct:target"), "construct:enzyme@2")
        with self.assertRaisesRegex(r.Error, "collision"):
            self.protein("competitor", aliases=["old-name"])
        with self.assertRaisesRegex(r.Error, "collision"):
            self.monomer("enzyme")
        with self.assertRaisesRegex(r.Error, "revise"):
            self.protein()

    def test_declared_fasta_type_and_ambiguity_are_preserved(self):
        source = self.base / "input.fa"
        source.write_text(">oligo\nacgn\n")
        document = r.fasta_document(source, "dna", "oligo")
        self.assertEqual(document["identity"], {"molecule_type": "dna", "sequence": "ACGN"})
        self.assertEqual(document["status"], "draft")
        self.assertIn("uppercase", document["provenance"]["sequence_formatting"])
        self.library.import_record(document, {"source.fa": source})
        with self.assertRaisesRegex(r.Error, "explicit"):
            r.fasta_document(source, None, "unknown")
        source.write_text(">one\nACG\n>two\nACG\n")
        with self.assertRaisesRegex(r.Error, "one FASTA"):
            r.fasta_document(source, "dna", "multichain")

    def test_typed_sequences_reject_unrepresented_modification_syntax(self):
        for identity in [{"molecule_type": "dna", "sequence": "ACGU"},
                         {"molecule_type": "rna", "sequence": "ACGT"},
                         {"molecule_type": "dna", "sequence": "AC/iAmMC6T/G"},
                         {"molecule_type": "protein", "sequence": "ACD*"},
                         {"molecule_type": "dna", "sequence": "acgt"}]:
            with self.subTest(identity=identity), self.assertRaises(r.Error):
                self.library.import_record({"kind": "construct", "id": "bad", "identity": identity})

    def test_monomer_aliases_pin_and_snapshot_closes_transitive_references(self):
        base = self.monomer("base", description="An explicitly unresolved source monomer")
        self.monomer("modified-a", precursor={"monomer_ref": "base"})
        oligo = self.library.import_record({"kind": "construct", "id": "oligo",
            "identity": {"molecule_type": "rna", "sequence": "ACGU",
                         "modifications": [{"position": 1, "monomer_ref": "modified-a"}],
                         "termini": {"5_prime": {"monomer_ref": "base"}}}})
        self.assertEqual(oligo["identity"]["modifications"][0]["monomer_ref"], "monomer:modified-a@1")
        before = self.library.snapshot("oligo")
        self.assertEqual(set(before["monomers"]), {"monomer:base@1", "monomer:modified-a@1"})
        self.library.revise("base", {"identity": {"description": "A different monomer definition"}})
        after = self.library.snapshot("oligo")
        self.assertEqual(before, after)
        self.assertEqual(after["monomers"]["monomer:base@1"], base)
        r.verify_document(after)

    def test_reference_kind_and_missing_reference_rejected(self):
        self.protein()
        for ref in ["missing", "construct:enzyme@1", "monomer:missing@2"]:
            with self.subTest(ref=ref), self.assertRaises(r.Error):
                self.library.import_record({"kind": "construct", "id": "bad",
                    "identity": {"molecule_type": "dna", "sequence": "ACGT",
                                 "modifications": [{"position": 2, "monomer_ref": ref}]}})

    def test_positions_are_one_based_and_linkage_range_is_distinct(self):
        self.monomer()
        for field, pos in [("modifications", 0), ("modifications", True), ("modifications", 5),
                           ("linkages", 4), ("modifications", "2")]:
            with self.subTest(field=field, pos=pos), self.assertRaises(r.Error):
                self.library.import_record({"kind": "construct", "id": "bad",
                    "identity": {"molecule_type": "dna", "sequence": "ACGT",
                                 field: [{"position": pos, "monomer_ref": "modified-a"}]}})
        circular = self.library.import_record({"kind": "construct", "id": "circle",
            "identity": {"molecule_type": "dna", "sequence": "ACGT", "circular": True,
                         "linkages": [{"position": 4, "monomer_ref": "modified-a"}]}})
        self.assertTrue(circular["identity"]["circular"])

    def test_assembly_pins_copies_and_retains_bonds_and_metadata(self):
        protein = self.protein()
        ligand = self.library.import_record({"kind": "construct", "id": "ligand",
                                             "identity": {"molecule_type": "small_molecule", "smiles": "C[C@H](O)Cl"}})
        bond = {"from": {"chain_id": "A", "position": 2, "atom": "SG"},
                "to": {"chain_id": "L", "atom": 4}, "order": "single"}
        assembly = self.library.import_record({"kind": "assembly", "id": "complex",
            "identity": {"components": [{"chain_id": "A", "construct_ref": "enzyme"},
                                        {"chain_id": "B", "construct_ref": "enzyme"},
                                        {"chain_id": "L", "construct_ref": "ligand"}],
                         "bonds": [bond], "custom_assembly_note": "Preserve adapter-visible chemistry"}})
        snapshot = self.library.snapshot("complex")
        self.assertEqual(snapshot["bonds"], [bond])
        self.assertEqual(snapshot["components"][0]["record"], protein)
        self.assertEqual(snapshot["components"][2]["record"], ligand)
        self.assertIn("custom_assembly_note", snapshot["assembly_record"]["identity"])
        self.assertEqual(assembly["identity"]["components"][0]["construct_ref"], "construct:enzyme@1")
        self.library.revise("enzyme", {"notes": "Later revision must not change an assembly"})
        self.assertEqual(snapshot, self.library.snapshot("complex"))

    def test_assembly_rejects_duplicate_chains_unknown_bond_chain_or_bad_position(self):
        self.protein()
        for components, bond in [
            ([{"chain_id": "A", "construct_ref": "enzyme"}] * 2, None),
            ([{"chain_id": "A", "construct_ref": "enzyme"}],
             {"from": {"chain_id": "B", "position": 1, "atom": "N"}, "to": {"chain_id": "A", "position": 2, "atom": "C"}}),
            ([{"chain_id": "A", "construct_ref": "enzyme"}],
             {"from": {"chain_id": "A", "position": 10, "atom": "N"}, "to": {"chain_id": "A", "position": 2, "atom": "C"}}),
        ]:
            with self.subTest(components=components, bond=bond), self.assertRaises(r.Error):
                self.library.import_record({"kind": "assembly", "id": "bad", "identity": {
                    "components": components, "bonds": [bond] if bond else []}})

    def test_internal_bonds_survive_single_construct_snapshot(self):
        bond = {"from": {"position": 1, "atom": "N"}, "to": {"position": 9, "atom": "C"}}
        self.library.import_record({"kind": "construct", "id": "linked",
            "identity": {"molecule_type": "protein", "sequence": "ACDEFGHIK", "crosslinks": [bond]}})
        self.assertEqual(self.library.snapshot("linked")["components"][0]["record"]["identity"]["crosslinks"], [bond])

    def test_draft_smiles_and_original_sdf_are_never_chemically_normalized(self):
        smiles = "[13CH3][C@@H](O)[NH3+].[Cl-]"
        record = self.library.import_record({"kind": "construct", "id": "compound",
                                             "identity": {"molecule_type": "small_molecule", "smiles": smiles}})
        self.assertEqual(record["identity"]["smiles"], smiles)
        self.assertEqual(record["status"], "draft")
        sdf = self.base / "supplied.sdf"
        sdf.write_bytes(b"Original supplied representation\n  Not silently parsed or repaired\n$$$$\n")
        output = io.StringIO()
        with redirect_stdout(output):
            r.main(["--root", str(self.root), "import", "--sdf", str(sdf), "--id", "sdf-compound"])
        imported = json.loads(output.getvalue())
        self.assertEqual(imported["identity"]["structure_file"], "attachments/source.sdf")
        self.assertEqual(self.library.attachment_path("sdf-compound", "attachments/source.sdf").read_bytes(), sdf.read_bytes())
        self.assertEqual(imported["status"], "draft")

    def test_tampering_record_or_attachment_aborts_resolution(self):
        source = self.base / "source.txt"
        source.write_text("original")
        self.library.import_record({"kind": "construct", "id": "one",
            "identity": {"molecule_type": "protein", "sequence": "ACD"}}, {"source.txt": source})
        path = self.library.record_path("one")
        data = r.load_json(path)
        data["notes"] = "unaudited mutation"
        original_bytes = path.read_bytes()
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(r.Error, "SHA-256 mismatch"):
            self.library.snapshot("one")
        path.write_bytes(original_bytes)
        (path.parent / "attachments/source.txt").write_text("changed!")
        with self.assertRaisesRegex(r.Error, "integrity"):
            self.library.show("one")

    def test_symlink_and_traversal_attachment_rejected_without_publication(self):
        source = self.base / "source"
        source.write_text("content")
        link = self.base / "link"
        link.symlink_to(source)
        document = {"kind": "construct", "id": "unsafe",
                    "identity": {"molecule_type": "protein", "sequence": "ACD"}}
        for name, file in [("../escape", source), ("a/b", source), ("file", link), ("/absolute", source)]:
            with self.subTest(name=name), self.assertRaises(r.Error):
                self.library.import_record(document, {name: file})
        self.assertEqual(self.library.list(), [])
        self.assertEqual(list((self.root / ".staging").iterdir()), [])

    def test_symlink_registry_root_and_existing_revision_rejected(self):
        link = self.base / "library-link"
        link.symlink_to(self.root)
        with self.assertRaisesRegex(r.Error, "Symlinks"):
            r.Registry(link)
        self.protein()
        version = self.library.record_path("enzyme").parent
        real = version.with_name("saved")
        version.rename(real)
        version.symlink_to(real)
        with self.assertRaises(r.Error):
            self.library.verify()

    def test_atomic_publication_failure_has_no_visible_partial_revision(self):
        self.protein()
        with patch.object(r.os, "rename", side_effect=OSError("simulated pre-publication crash")):
            with self.assertRaises(OSError):
                self.library.revise("enzyme", {"notes": "Must remain unpublished"})
        self.assertEqual(self.library.resolve("enzyme"), "construct:enzyme@1")
        self.assertEqual(self.library.show("enzyme")["notes"], "")
        self.assertEqual(list((self.root / ".staging").iterdir()), [])

    def test_parallel_revisions_do_not_overwrite_or_share_revision_numbers(self):
        self.protein()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda n: self.library.revise("enzyme", {"notes": f"Revision writer {n}"}), range(8)))
        self.assertEqual(sorted(x["revision"] for x in results), list(range(2, 10)))
        self.assertEqual(self.library.verify()["records"], 9)

    def test_index_is_rebuildable_and_never_an_authority(self):
        self.protein(tags=["pilot", "protein"], aliases=["target"])
        index = self.root / "index.sqlite3"
        index.write_text("corrupt cache")
        self.assertEqual(self.library.resolve("target"), "construct:enzyme@1")
        self.assertEqual(len(self.library.list(molecule_type="protein", tag="pilot")), 1)
        self.assertEqual(self.library.reindex()["indexed"], 1)
        with closing(sqlite3.connect(index)) as db:
            self.assertEqual(db.execute("SELECT ref FROM records").fetchone(), ("construct:enzyme@1",))

    def test_backup_verified_restoration_preserves_pinned_snapshots(self):
        self.monomer()
        self.library.import_record({"kind": "construct", "id": "oligo",
            "identity": {"molecule_type": "rna", "sequence": "ACGU",
                         "modifications": [{"position": 1, "monomer_ref": "modified-a"}]}})
        self.library.revise("oligo", {"notes": "Second revision"})
        before = self.library.snapshot("oligo")
        backup = self.base / "backup.tar.gz"
        receipt = self.library.export_snapshot(backup)
        self.assertEqual(receipt["records"], 3)
        self.assertEqual(r.verify_backup(backup)["records"], 3)
        with tarfile.open(backup) as archive:
            self.assertFalse(any("sqlite" in entry.name or "lock" in entry.name for entry in archive))
        restored_root = self.base / "restored"
        result = r.restore_backup(backup, restored_root)
        self.assertTrue(result["restored"])
        restored = r.Registry(restored_root)
        self.assertEqual(before, restored.snapshot("oligo"))
        self.assertEqual(restored.verify()["records"], 3)
        with self.assertRaisesRegex(r.Error, "empty"):
            r.restore_backup(backup, restored_root)

    def test_empty_registry_backup_and_restore(self):
        backup = self.base / "empty.tar.gz"
        self.library.export_snapshot(backup)
        destination = self.base / "empty-restored"
        destination.mkdir()
        self.assertEqual(r.restore_backup(backup, destination)["records"], 0)
        self.assertEqual(r.Registry(destination).list(), [])

    def test_backup_tampering_does_not_replace_existing_backup_or_publish_restore(self):
        self.protein()
        backup = self.base / "backup.tar.gz"
        self.library.export_snapshot(backup)
        old = backup.read_bytes()
        with patch.object(r, "verify_backup", side_effect=r.Error("simulated corrupt backup")):
            with self.assertRaises(r.Error):
                self.library.export_snapshot(backup)
        self.assertEqual(backup.read_bytes(), old)
        backup.write_bytes(old[:len(old) // 2])
        with self.assertRaises((r.Error, OSError, EOFError, tarfile.TarError)):
            r.restore_backup(backup, self.base / "no-restore")
        self.assertFalse((self.base / "no-restore").exists())

    def test_backup_rejects_links_traversal_and_duplicate_files(self):
        for kind in ["symlink", "traversal", "duplicate"]:
            malicious = self.base / f"{kind}.tar.gz"
            with tarfile.open(malicious, "w:gz") as archive:
                if kind == "symlink":
                    entry = tarfile.TarInfo("constructs/test/1/record.json")
                    entry.type, entry.linkname = tarfile.SYMTYPE, "/etc/passwd"
                    archive.addfile(entry)
                elif kind == "traversal":
                    entry = tarfile.TarInfo("../outside")
                    entry.size = 1
                    archive.addfile(entry, io.BytesIO(b"x"))
                else:
                    for _ in range(2):
                        entry = tarfile.TarInfo("constructs/test/1/record.json")
                        entry.size = 1
                        archive.addfile(entry, io.BytesIO(b"x"))
            with self.subTest(kind=kind), self.assertRaises(r.Error):
                r.verify_backup(malicious)

    def test_missing_reference_is_detected_before_backup(self):
        self.protein()
        self.library.revise("enzyme", {"notes": "Rev2"})
        first = self.root / "constructs/enzyme/1"
        import shutil
        shutil.rmtree(first)
        with self.assertRaisesRegex(r.Error, "Missing parent"):
            self.library.export_snapshot(self.base / "incomplete.tar.gz")

    def test_cli_json_import_retains_original_and_metadata(self):
        source = self.base / "record.json"
        source.write_text('{ "kind": "monomer", "id": "custom", "identity": { "vendor_notation": "/custom/" } }\n')
        with redirect_stdout(io.StringIO()):
            r.main(["--root", str(self.root), "import", "--json", str(source), "--alias", "custom-residue", "--tag", "oligos"])
        imported = self.library.show("custom-residue")
        self.assertEqual(imported["identity"]["vendor_notation"], "/custom/")
        self.assertEqual(self.library.attachment_path("custom", "attachments/source.json").read_bytes(), source.read_bytes())
        self.assertEqual(imported["tags"], ["oligos"])

    def test_invalid_managed_metadata_and_nonfinite_json_are_rejected(self):
        for extra in [{"revision": 10}, {"sha256": "not authoritative"}, {"aliases": "one"},
                      {"status": "ready"}, {"provenance": {"undefined": float("nan")}}]:
            with self.subTest(extra=extra), self.assertRaises(r.Error):
                self.protein(**extra)
        duplicate = self.base / "duplicate.json"
        duplicate.write_text('{"id":"one","id":"two"}')
        with self.assertRaisesRegex(r.Error, "Duplicate"):
            r.load_json(duplicate)

    def test_snapshot_digest_binds_nested_monomer_revision(self):
        self.monomer()
        self.library.import_record({"kind": "construct", "id": "oligo",
            "identity": {"molecule_type": "rna", "sequence": "ACGU",
                         "modifications": [{"position": 1, "monomer_ref": "modified-a"}]}})
        snapshot = self.library.snapshot("oligo")
        changed = copy.deepcopy(snapshot)
        changed["monomers"]["monomer:modified-a@1"]["identity"]["description"] = "Another definition"
        self.assertNotEqual(r.digest_json(changed), snapshot["sha256"])
        with self.assertRaises(r.Error):
            r.verify_document(changed)


if __name__ == "__main__":
    unittest.main()
