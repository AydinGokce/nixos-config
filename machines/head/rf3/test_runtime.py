import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

r=runpy.run_path(str(Path(__file__).with_name("runtime.py")))
p=runpy.run_path(str(Path(__file__).with_name("prepare.py")))


class RuntimeTests(unittest.TestCase):
    def test_default_scientific_settings_and_missing_msa_guard_are_explicit(self):
        command=r["command"]("/input/input.json","/out","/weights",[])
        for arg in ("n_recycles=10","num_steps=50","diffusion_batch_size=5","seed=101",
                    "raise_if_missing_msa_for_protein_of_length_n=1","devices_per_node=1","num_nodes=1"):
            self.assertIn(arg,command)
        changed=r["command"]("/i","/o","/w",["seed=42"])
        self.assertIn("seed=42",changed)
        self.assertNotIn("seed=101",changed)

    def test_cyclic_chain_binding_uses_typed_internal_override_only(self):
        args=r["command"]("/i","/o","/w",[],["A","1"])
        self.assertIn('cyclic_chains=["A","1"]',args)
        with self.assertRaises(r["Error"]):
            r["command"]("/i","/o","/w",["cyclic_chains=[A]"])
        with self.assertRaises(r["Error"]):
            r["command"]("/i","/o","/w",[],["A","A"])

    def test_user_extra_cannot_replace_input_checkpoint_gpu_count_or_silent_fallback(self):
        for args in (["inputs=/elsewhere"],["ckpt_path=other"],["devices_per_node=8"],
                     ["raise_if_missing_msa_for_protein_of_length_n=null"],["n_recycles=0"],
                     ["num_timesteps=2"],["seed=1","seed=2"],["num_steps=nan"]):
            with self.subTest(args=args),self.assertRaises(r["Error"]):
                r["settings"](args)

    def test_checkpoint_tamper_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);directory=root/"rf3/checkpoints";directory.mkdir(parents=True)
            path=directory/r["CHECKPOINT_NAME"];path.write_bytes(b"correct bytes")
            globals_=r["checkpoint"].__globals__
            with patch.dict(globals_,CHECKPOINT_SIZE=13,CHECKPOINT_SHA256=hashlib.sha256(b"correct bytes").hexdigest()):
                self.assertEqual(r["checkpoint"](root),path)
                path.write_bytes(b"changed bytes")
                with self.assertRaisesRegex(r["Error"],"mismatch"):
                    r["checkpoint"](root,download=True)
                self.assertEqual(path.read_bytes(),b"changed bytes")

    def test_zero_exit_or_nonfinite_confidence_is_not_a_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with self.assertRaisesRegex(r["Error"],"did not produce"):
                r["output_check"](root,"job")
            (root/"job").mkdir()
            (root/"job/job_model.cif").write_text("data_job\n_atom_site.id 1\n")
            (root/"job/job_summary_confidences.json").write_text('{"ranking_score":NaN}')
            with self.assertRaisesRegex(r["Error"],"finite"):
                r["output_check"](root,"job")
            (root/"job/job_summary_confidences.json").write_text('{"ranking_score":0.4}')
            self.assertEqual(r["output_check"](root,"job")["ranking_score"],.4)

    def test_predict_copies_search_evidence_and_uses_prepared_files_without_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            fasta=root/"input.fasta";fasta.write_text(">original\nACDE\n")
            msa=root/"msa.a3m";msa.write_text(">query\nACDE\n>hit TaxID=7\nACDE\n")
            mapping=root/"map.json";mapping.write_text(json.dumps({"A":str(msa)}))
            source=p["prepare"](fasta=fasta,msa_map=mapping,out=root/"prepared",name="job")
            raw=source.parent/"raw-response.bin";raw.write_bytes(b"unchanged search response")
            manifest_path=source.parent/"msa-manifest.json"
            manifest=json.loads(manifest_path.read_text())
            manifest["files"][raw.name]=p["file_hash"](raw)
            manifest.pop("sha256");manifest["sha256"]=p["digest"](manifest)
            manifest_path.write_text(json.dumps(manifest))
            def native(args,*,cwd,env,check):
                self.assertEqual(cwd,root/"out/prepared-native")
                self.assertIn("inputs="+str(cwd/"input.json"),args)
                self.assertEqual((cwd/raw.name).read_bytes(),raw.read_bytes())
                (root/"out/job").mkdir()
                (root/"out/job/job_model.cif").write_text("data_job\n_atom_site.id 1\n")
                (root/"out/job/job_summary_confidences.json").write_text('{"ranking_score":0.5}')
                (root/"out/rf3-features.json").write_text(json.dumps({"events":[{"transform":key} for key in
                    ("LoadPolymerMSAs","PairAndMergePolymerMSAs","FeaturizeMSALikeAF3")]}))
            def chemistry(out,prepared):
                self.assertEqual(prepared,root/"out/prepared-native/input.json")
                self.assertTrue((out/"job/job_model.cif").is_file())
                backup=out/"original-native-rank";backup.mkdir()
                (backup/"job_model.cif").write_bytes((out/"job/job_model.cif").read_bytes())
                # The chemistry-valid sample can rank below the raw native winner.
                (out/"job/job_summary_confidences.json").write_text('{"ranking_score":0.4}')
                result={"status":"passed","selected_sample":"seed-101_sample-1",
                        "audit_source_sha256":r["file_hash"](Path(r["HERE"]).parent/"library/rf3_output.py")}
                (out/"rf3-output-validation.json").write_text(json.dumps(result))
                return result
            globals_=r["predict"].__globals__
            with patch.dict(globals_,verify_install=lambda _: {"source_commit":r["SOURCE_PIN"]},
                            checkpoint=lambda _: Path("/pinned/weights"),output_chemistry=chemistry),patch.object(globals_["subprocess"],"run",side_effect=native):
                result=r["predict"](root,source,root/"out",[])
            self.assertEqual(result["status"],"complete")
            self.assertEqual(result["prepared_sha256"],manifest["sha256"])
            self.assertEqual(result["outputs"]["ranking_score"],.4)
            self.assertEqual(result["output_validation"]["selected_sample"],"seed-101_sample-1")
            self.assertEqual(result["output_validation_sha256"],r["file_hash"](root/"out/rf3-output-validation.json"))

    def test_output_chemistry_failure_keeps_raw_prediction_and_records_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            fasta=root/"input.fasta";fasta.write_text(">protein\nACDE\n")
            msa=root/"input.a3m";msa.write_text(">query\nACDE\n")
            mapping=root/"map.json";mapping.write_text(json.dumps({"A":str(msa)}))
            source=p["prepare"](fasta=fasta,msa_map=mapping,out=root/"prepared",name="job")
            def native(args,*,cwd,env,check):
                (root/"out/job").mkdir()
                (root/"out/job/job_model.cif").write_bytes(b"original native output")
                (root/"out/rf3-features.json").write_text(json.dumps({"events":[{"transform":key} for key in
                    ("LoadPolymerMSAs","PairAndMergePolymerMSAs","FeaturizeMSALikeAF3")]}))
            def chemistry(out,prepared):
                (out/"rf3-output-validation.json").write_text('{"status":"failed","reason":"alkene stereo"}')
                raise RuntimeError("no chemically valid native sample")
            globals_=r["predict"].__globals__
            with patch.dict(globals_,verify_install=lambda _: {"source_commit":r["SOURCE_PIN"]},
                            checkpoint=lambda _: Path("/pinned/weights"),output_chemistry=chemistry),patch.object(globals_["subprocess"],"run",side_effect=native):
                with self.assertRaisesRegex(r["Error"],"output chemistry validation failed"):
                    r["predict"](root,source,root/"out",[])
            receipt=json.loads((root/"out/rf3-runtime.json").read_text())
            self.assertEqual(receipt["status"],"failed_output_chemistry")
            self.assertNotIn("outputs",receipt)
            self.assertIn("no chemically valid",receipt["output_validation_error"])
            self.assertEqual(receipt["output_validation_sha256"],r["file_hash"](root/"out/rf3-output-validation.json"))
            self.assertEqual((root/"out/job/job_model.cif").read_bytes(),b"original native output")


if __name__=="__main__":
    unittest.main()
