"""Observe RF3's native MSA transforms without modifying their inputs/results."""
import hashlib
import json
import os
from pathlib import Path


def array_evidence(value):
    return {"shape":list(value.shape),"dtype":str(value.dtype),
            "sha256":hashlib.sha256(value.tobytes()).hexdigest()}


def install(path):
    from atomworks.ml.transforms.msa import msa as native
    path=Path(path)
    events=[]

    def wrap(cls):
        original=cls.forward
        def forward(instance,data):
            result=original(instance,data)
            event={"transform":cls.__name__,"example_id":str(result.get("example_id","")),"chains":{}}
            if cls.__name__ in {"LoadPolymerMSAs","PairAndMergePolymerMSAs"}:
                for chain,alignment in result.get("polymer_msas_by_chain_id",{}).items():
                    row={key:array_evidence(alignment[key]) for key in ("msa","ins","tax_ids") if key in alignment}
                    for key in ("any_paired","all_paired"):
                        if key in alignment:
                            row[key+"_rows"]=int(alignment[key].sum())
                    source=result.get("chain_info",{}).get(chain,{}).get("msa_path")
                    if source:
                        row["msa_path"]=str(source)
                    event["chains"][str(chain)]=row
                if cls.__name__=="LoadPolymerMSAs":
                    event["native_max_msa_sequences"]=instance.max_msa_sequences
            else:
                features=result["msa_features"]
                event["recycle_msa_shapes"]=[list(value.shape) for value in features["msa_features_per_recycle_dict"]["msa"]]
                event["native_n_msa"]=instance.n_msa
                event["native_n_recycles"]=instance.n_recycles
                event["encoded_input_shape"]=list(result["encoded"]["msa"].shape)
            events.append(event)
            temporary=path.with_suffix(path.suffix+f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps({"schema":1,"kind":"rf3-native-msa-feature-audit","events":events},indent=2)+"\n")
            os.replace(temporary,path)
            return result
        cls.forward=forward
    for cls in (native.LoadPolymerMSAs,native.PairAndMergePolymerMSAs,native.FeaturizeMSALikeAF3):
        wrap(cls)
