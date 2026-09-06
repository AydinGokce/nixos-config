import gzip
import json
from pathlib import Path
import runpy
import tempfile
import unittest


m = runpy.run_path(str(Path(__file__).with_name("prepare.py")))


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text)
        return path

    def fasta(self):
        return self.write("query.fa", ">original name\nACDE\n>second\nFGHI\n")

    def mapping(self):
        a = self.write("A.a3m", ">query\nACDE\n>one TaxID=9606\nACdDE\n>environmental\nA-DE\n")
        b = self.write("B.a3m", ">query\nFGHI\n>one TaxID=9606\nFGHI\n")
        return self.write("map.json", json.dumps({"A":str(a), "B":str(b)}))

    def test_multichain_bytes_taxonomy_and_headers_survive_preparation(self):
        path = m["prepare"](fasta=self.fasta(), msa_map=self.mapping(), out=self.root/"prepared")
        manifest = m["validate"](path)
        self.assertEqual((path.parent/"msas/A.a3m").read_bytes(), (self.root/"A.a3m").read_bytes())
        self.assertEqual(manifest["original_fasta_headers"], {"A":"original name", "B":"second"})
        self.assertEqual(manifest["chain_msas"]["A"]["depth"], 3)
        self.assertEqual(manifest["chain_msas"]["A"]["taxonomy_rows"], 1)
        self.assertEqual(json.loads(path.read_text())[0]["components"][0]["msa_path"], "msas/A.a3m")

    def test_modified_protein_mixed_native_chemistry_and_sdf_bytes_preserved(self):
        sdf = self.write("ligand.sdf", "exact SDF fixture bytes\n")
        a3m = self.write("AX.a3m", ">query\nACDX\n>hit TaxID=2\nACDA\n")
        value = [{"name":"mixed", "components":[
            {"chain_id":"P", "seq":["(ALA)","(CYS)","(ASP)","(MSE)"], "chain_type":"polypeptide(L)", "msa_path":str(a3m)},
            {"chain_id":"D", "seq":["(DA)","(DC)","(DG)","(DT)"], "chain_type":"polydeoxyribonucleotide"},
            {"chain_id":"L", "path":str(sdf), "res_name":"L:abcd"}],
            "bonds":[["P/ALA/1/N", "L/L:abcd/1/C1"]]}]
        native = self.write("native.json", json.dumps(value))
        path = m["prepare"](native_json=native, out=self.root/"prepared")
        output = json.loads(path.read_text())[0]
        self.assertEqual(output["bonds"], value[0]["bonds"])
        self.assertEqual(output["components"][0]["seq"], ["(ALA)","(CYS)","(ASP)","(MSE)"])
        self.assertEqual((path.parent/output["components"][2]["path"]).read_bytes(), sdf.read_bytes())
        self.assertEqual(set(m["validate"](path)["chain_msas"]), {"P"})

    def test_compressed_msa_preserves_original_compressed_bytes(self):
        a3m = self.root / "A.a3m.gz"
        with gzip.open(a3m,"wt") as handle:
            handle.write(">query\nACDE\n>hit TaxID=123\nACDE\n")
        native = self.write("native.json", json.dumps({"name":"x", "components":[
            {"chain_id":"A", "seq":"ACDE", "chain_type":"POLYPEPTIDE(L)", "msa_path":str(a3m)}]}))
        path = m["prepare"](native_json=native,out=self.root/"prepared")
        self.assertEqual((path.parent/"msas/A.a3m.gz").read_bytes(), a3m.read_bytes())
        m["validate"](path)

    def test_missing_or_partial_mapping_never_falls_back_and_publication_is_atomic(self):
        fasta = self.fasta()
        with self.assertRaisesRegex(m["Error"], "no explicit A3M"):
            m["prepare"](fasta=fasta,out=self.root/"prepared")
        mapping = self.write("partial.json",json.dumps({"A":"missing"}))
        with self.assertRaisesRegex(m["Error"], "every protein"):
            m["prepare"](fasta=fasta,msa_map=mapping,out=self.root/"prepared")
        self.assertFalse((self.root/"prepared").exists())
        self.assertEqual(list(self.root.glob(".rf3-prepare-*")), [])

    def test_ambiguous_native_chain_type_and_repeated_ids_are_rejected(self):
        for components in ([{"chain_id":"A", "seq":"ACGT"}],
                           [{"chain_id":"A","ccd_code":"MG"},{"chain_id":"A","ccd_code":"ZN"}]):
            with self.assertRaises(m["Error"]):
                m["document"]({"name":"x", "components":components})

    def test_wrong_query_width_prepairing_and_malformed_taxonomy_are_rejected(self):
        cases = [">query\nAAAA\n", ">query\nACDE\n>hit\nACD\n", "#4,4\t1,1\n>query\nACDE\n",
                 ">query\nACDE\n>hit TaxID=unknown\nACDE\n", ">query\nACDE\n>hit TaxID=1 TaxID=2\nACDE\n"]
        for content in cases:
            with self.subTest(content=content), self.assertRaises(m["Error"]):
                m["validate_a3m"](self.write("bad.a3m",content),"ACDE")

    def test_query_only_is_recorded_as_depth_one_not_pretended_homology(self):
        result = m["validate_a3m"](self.write("single.a3m",">query\nACDE\n"),"ACDE")
        self.assertEqual(result["depth"],1)
        self.assertEqual(result["taxonomy_rows"],0)

    def test_short_native_polymers_are_rejected_before_model_can_remove_them(self):
        for kind,seq in (("POLYPEPTIDE(L)","ACD"), ("POLYRIBONUCLEOTIDE","ACG"),
                         ("POLYDEOXYRIBONUCLEOTIDE",["(DA)","(DC)","(DG)"])):
            with self.subTest(kind=kind), self.assertRaisesRegex(m["Error"],"shorter than four"):
                m["document"]({"name":"short","components":[{"chain_id":"A","seq":seq,"chain_type":kind}]})

    def test_tampered_alignment_manifest_extra_file_and_symlink_fail(self):
        path = m["prepare"](fasta=self.fasta(),msa_map=self.mapping(),out=self.root/"prepared")
        msa = path.parent/"msas/A.a3m"
        original = msa.read_bytes()
        msa.write_bytes(original.replace(b"9606",b"1234"))
        with self.assertRaisesRegex(m["Error"], "SHA256"):
            m["validate"](path)
        msa.write_bytes(original)
        extra = path.parent/"extra"
        extra.write_text("x")
        with self.assertRaisesRegex(m["Error"], "inventory"):
            m["validate"](path)
        extra.unlink()
        msa.unlink(); msa.symlink_to(self.root/"A.a3m")
        with self.assertRaisesRegex(m["Error"], "regular directory"):
            m["validate"](path)

    def test_existing_destination_and_duplicate_json_keys_are_rejected(self):
        out = self.root/"prepared";out.mkdir()
        self.write("sentinel","retained")
        with self.assertRaisesRegex(m["Error"],"already exists"):
            m["prepare"](fasta=self.fasta(),msa_map=self.mapping(),out=out)
        path = self.write("duplicate.json",'{"A":"one","A":"two"}')
        with self.assertRaisesRegex(m["Error"],"Duplicate"):
            m["read_json"](path)


if __name__ == "__main__":
    unittest.main()
