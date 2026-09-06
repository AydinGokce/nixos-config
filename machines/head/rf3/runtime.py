#!/usr/bin/env python3
"""Pinned RF3 checkpoint, isolated-runtime verification, and prepared inference."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

SOURCE_PIN = "b02eed6a6bdf8f44d14a80cc36e3da13c9f2291c"
CHECKPOINT_NAME = "rf3_foundry_01_24_latest_remapped.ckpt"
CHECKPOINT_URL = "https://files.ipd.uw.edu/pub/rf3/" + CHECKPOINT_NAME
CHECKPOINT_SIZE = 3038876446
CHECKPOINT_SHA256 = "364ef592fd8042a9cf4176d045015190f8322f961ccca38d891b20ca578d3bb0"
HERE = Path(__file__).resolve().parent
DEFAULTS = {"n_recycles":10, "num_steps":50, "diffusion_batch_size":5, "seed":101}
LIMITS = {"n_recycles":(1,100), "num_steps":(1,1000), "diffusion_batch_size":(1,20), "seed":(0,2**32-1)}


class Error(RuntimeError):
    pass


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(8*1024*1024):
            h.update(block)
    return h.hexdigest()


def checkpoint(shared, *, download=False):
    directory = Path(shared)/"rf3/checkpoints"
    path = directory/CHECKPOINT_NAME
    if not path.exists():
        if not download:
            raise Error("Pinned RF3 checkpoint is missing; run runtime.py download first")
        directory.mkdir(parents=True,exist_ok=True)
        import fcntl
        with (directory/"download.lock").open("a") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if not path.exists():
                with tempfile.TemporaryDirectory(prefix=".rf3-download-",dir=directory) as tmp:
                    parts=Path(tmp);block=128*1024*1024
                    def fetch(index):
                        first=index*block;last=min(CHECKPOINT_SIZE,first+block)-1
                        target=parts/str(index)
                        for attempt in range(3):
                            try:
                                request=urllib.request.Request(CHECKPOINT_URL,headers={"Range":f"bytes={first}-{last}"})
                                with urllib.request.urlopen(request,timeout=120) as response,target.open("wb") as output:
                                    if response.status != 206 or response.headers.get("Content-Range") != f"bytes {first}-{last}/{CHECKPOINT_SIZE}":
                                        raise Error("Checkpoint server returned an unexpected byte range")
                                    shutil.copyfileobj(response,output,8*1024*1024)
                                if target.stat().st_size != last-first+1:
                                    raise Error("Checkpoint range is incomplete")
                                return target
                            except (OSError,Error):
                                if attempt==2:
                                    raise
                    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                        downloaded=list(pool.map(fetch,range(math.ceil(CHECKPOINT_SIZE/block))))
                    combined=parts/"checkpoint"
                    with combined.open("wb") as output:
                        for part in downloaded:
                            with part.open("rb") as data:
                                shutil.copyfileobj(data,output,8*1024*1024)
                        output.flush();os.fsync(output.fileno())
                    if combined.stat().st_size != CHECKPOINT_SIZE or file_hash(combined) != CHECKPOINT_SHA256:
                        raise Error("Official RF3 checkpoint differs from its pinned SHA256")
                    os.rename(combined,path)
    if path.is_symlink() or path.stat().st_size != CHECKPOINT_SIZE or file_hash(path) != CHECKPOINT_SHA256:
        raise Error("RF3 checkpoint size/SHA256 mismatch; refusing to overwrite it")
    return path


def verify_install(shared):
    shared=Path(shared)
    source=shared/"src"/("foundry-rf3-"+SOURCE_PIN)
    git=["git","-c","safe.directory="+str(source),"-C",str(source)]
    actual=subprocess.check_output([*git,"rev-parse","HEAD"],text=True).strip()
    if actual != SOURCE_PIN or subprocess.run([*git,"diff","--quiet","HEAD","--"],check=False).returncode:
        raise Error("RF3 source checkout differs from the supported commit")
    if sys.version_info[:2] != (3,12):
        raise Error("The RF3 runtime requires Python 3.12")
    lock=HERE/"requirements.lock"
    versions={}
    for name,expected in re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\;]+)",lock.read_text(),re.M):
        try:
            version=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            raise Error("RF3 dependency is missing: "+name) from None
        if version != expected:
            raise Error(f"RF3 dependency differs from its lock: {name} {version} != {expected}")
        versions[name]=version
    distribution=importlib.metadata.distribution("rc-foundry")
    installed=Path(distribution.locate_file(""))
    paths=((source/"src/foundry",installed/"foundry"),
           (source/"src/foundry_cli",installed/"foundry_cli"),
           (source/"models/rf3/src/rf3",installed/"rf3"),
           (source/"models/rf3/configs",installed/"rf3/configs"))
    for origin,destination in paths:
        for item in origin.rglob("*"):
            if not item.is_file() or item.suffix not in {".py",".yaml"}:
                continue
            relative=item.relative_to(origin)
            if relative==Path("version.py"):
                continue  # Build-generated package version; source commit is pinned above.
            target=destination/relative
            if not target.is_file() or file_hash(item)!=file_hash(target):
                raise Error("Installed RF3 code differs from pinned source: "+str(relative))
    return {"source_commit":SOURCE_PIN,"requirements_sha256":file_hash(lock),"versions":versions,
            "foundry_version":distribution.version}


def settings(extra):
    result=dict(DEFAULTS)
    seen=set()
    for arg in extra:
        if arg=="--" and not seen:
            continue
        if "=" not in arg:
            raise Error("RF3 options use key=value syntax")
        key,value=arg.split("=",1)
        if key not in LIMITS or key in seen or not re.fullmatch(r"[0-9]+",value):
            raise Error("Unsupported or repeated RF3 option: "+key)
        lower,upper=LIMITS[key]
        if not lower<=int(value)<=upper:
            raise Error("RF3 option is outside its supported range: "+key)
        result[key]=int(value);seen.add(key)
    return result


def command(input_path,output_path,checkpoint_path,extra,cyclic_chains=None):
    options=settings(extra)
    cyclic_chains=[] if cyclic_chains is None else cyclic_chains
    if (not isinstance(cyclic_chains,list) or len(cyclic_chains)!=len(set(cyclic_chains))
            or any(not isinstance(x,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}",x) for x in cyclic_chains)):
        raise Error("Invalid native cyclic chain identities")
    return [sys.executable,str(HERE/"runtime.py"),"native-fold",
            "inputs="+str(input_path),"out_dir="+str(output_path),"ckpt_path="+str(checkpoint_path),
            "devices_per_node=1","num_nodes=1","raise_if_missing_msa_for_protein_of_length_n=1",
            "skip_existing=false","compress_outputs=false","cyclic_chains="+json.dumps(cyclic_chains,separators=(",",":")),
            *[f"{key}={value}" for key,value in options.items()]]


def output_check(out,name):
    model=Path(out)/name/(name+"_model.cif")
    if not model.is_file() or model.stat().st_size==0:
        raise Error("RF3 did not produce one ranked model; an early-stopped/skipped input is not a successful prediction")
    scores=model.with_name(name+"_summary_confidences.json")
    data=json.loads(scores.read_text())
    rank=data.get("ranking_score")
    if isinstance(rank,bool) or not isinstance(rank,(int,float)) or not math.isfinite(rank):
        raise Error("RF3 ranked model lacks a finite native confidence score")
    if "_atom_site." not in model.read_text():
        raise Error("RF3 output has no atom-site structure records")
    return {"model":str(model.relative_to(out)),"model_sha256":file_hash(model),
            "summary":str(scores.relative_to(out)),"summary_sha256":file_hash(scores),"ranking_score":rank}


def output_chemistry(out,input_path):
    path=HERE.parent/"library/rf3_output.py"
    spec=importlib.util.spec_from_file_location("rf3_output",path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    return module.validate_and_select(out,input_path)


def predict(shared,input_path,out,extra):
    options=settings(extra)  # Reject input/config overrides before touching the environment.
    prepare=runpy.run_path(str(HERE/"prepare.py"))
    source=Path(input_path).resolve()
    evidence=prepare["validate"](source)
    runtime=verify_install(shared)
    weights=checkpoint(shared)
    destination=Path(out).resolve();destination.mkdir(parents=True,exist_ok=True)
    prepared=destination/"prepared-native"
    if prepared.exists():
        raise Error("RF3 output already contains prepared input; use a new run directory")
    shutil.copytree(source.parent,prepared,symlinks=False)
    prepare["validate"](prepared/"input.json")
    value=prepare["document"](prepare["read_json"](prepared/"input.json"))
    cyclic=value.get("cyclic_chains",[])
    if not isinstance(cyclic,list) or not set(cyclic)<={c['chain_id'] for c in value['components'] if 'seq' in c}:
        raise Error("Native cyclic chain identities must refer to existing polymers")
    invocation=command(prepared/"input.json",destination,weights,extra,cyclic)
    provenance=dict(schema=1,model="rf3",runtime=runtime,checkpoint_sha256=CHECKPOINT_SHA256,
                    checkpoint_url=CHECKPOINT_URL,prepared_sha256=evidence["sha256"],settings=options,
                    compatibility={"id":"rf3-atomworks22-transient-atom-id-v1",
                                   "sha256":file_hash(HERE.parent/"library/rf3_compat.py")},
                    output_validation_source_sha256=file_hash(HERE.parent/"library/rf3_output.py"),
                    feature_audit_source_sha256=file_hash(HERE/"feature_audit.py"),
                    cyclic_chains=cyclic,argv=invocation,started=time.time())
    receipt=destination/"rf3-runtime.json"
    receipt.write_text(json.dumps(provenance,indent=2)+"\n")
    env=dict(os.environ)
    env.setdefault("CUDA_VISIBLE_DEVICES","0")
    env.setdefault("OMP_NUM_THREADS","4")
    env.setdefault("OPENBLAS_NUM_THREADS","4")
    env["BIO_RF3_FEATURE_AUDIT"]=str(destination/"rf3-features.json")
    subprocess.run(invocation,cwd=prepared,env=env,check=True)
    feature_path=destination/"rf3-features.json"
    feature_data=json.loads(feature_path.read_text())
    if {event['transform'] for event in feature_data['events']} != {"LoadPolymerMSAs","PairAndMergePolymerMSAs","FeaturizeMSALikeAF3"}:
        raise Error("RF3 prediction lacks its native MSA feature audit")
    validation_path=destination/"rf3-output-validation.json"
    try:
        prepare["validate"](prepared/"input.json")
        validation=output_chemistry(destination,prepared/"input.json")
        if validation.get("status") != "passed" or validation.get("audit_source_sha256") != provenance["output_validation_source_sha256"]:
            raise Error("RF3 output validation did not pass with its recorded source")
    except Exception as exc:
        provenance.update(status="failed_output_chemistry",completed=time.time(),
                          output_validation_error=str(exc),feature_audit_sha256=file_hash(feature_path))
        if validation_path.is_file():
            provenance["output_validation_sha256"]=file_hash(validation_path)
        receipt.write_text(json.dumps(provenance,indent=2)+"\n")
        raise Error("RF3 output chemistry validation failed: "+str(exc)) from exc
    provenance.update(outputs=output_check(destination,value["name"]),feature_audit_sha256=file_hash(feature_path),
                      output_validation=validation,output_validation_sha256=file_hash(validation_path),
                      completed=time.time(),status="complete")
    receipt.write_text(json.dumps(provenance,indent=2)+"\n")
    return provenance


def native_fold(argv):
    # The same version-pinned, annotation-only compatibility shim is included
    # in the library compiler provenance and applied during CPU preflight.
    path=HERE.parent/"library/rf3_compat.py"
    spec=importlib.util.spec_from_file_location("rf3_compat",path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.install()
    from feature_audit import install as install_audit
    install_audit(os.environ['BIO_RF3_FEATURE_AUDIT'])
    from rf3.cli import app
    sys.argv=["rf3","fold",*argv]
    app()


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    if argv and argv[0]=="native-fold":
        return native_fold(argv[1:])
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("environment","check","download","predict"))
    parser.add_argument("--shared",default="/mnt/bio-shared")
    parser.add_argument("--input")
    parser.add_argument("--out")
    args,extra=parser.parse_known_args(argv)
    if args.action=="download":
        result={"checkpoint":str(checkpoint(args.shared,download=True)),"sha256":CHECKPOINT_SHA256}
    elif args.action in {"environment","check"}:
        result=verify_install(args.shared)
        if args.action=="check":
            result["checkpoint"]=str(checkpoint(args.shared))
    else:
        if not args.input or not args.out:
            parser.error("predict requires --input and --out")
        result=predict(args.shared,args.input,args.out,extra)
    print(json.dumps(result,indent=2))


if __name__=="__main__":
    try:
        main()
    except (RuntimeError,OSError,ValueError,KeyError,subprocess.CalledProcessError) as exc:
        print("rf3-runtime: "+str(exc),file=sys.stderr)
        sys.exit(2)
