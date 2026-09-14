#!/usr/bin/env python3
"""Prepare/build the reviewed native source; never replace the pinned runtime.

Requires the source archive and Nix toolchain named in build-provenance.json.
Use --prepare-only to verify archive, patch application and exact source bytes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import native_runtime


def sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def prepare(archive, output):
    lock = native_runtime.manifest()
    proof_path = HERE / "build-provenance.json"
    patch = HERE / "mmseqs.patch"
    if sha(proof_path) != lock["build_provenance_sha256"] or sha(patch) != lock["patch_sha256"]:
        raise ValueError("Native source/build provenance changed")
    proof = json.loads(proof_path.read_text())
    if sha(archive) != proof["source"]["archive_sha256"]:
        raise ValueError("Native upstream source archive changed")
    output.mkdir()  # Refuse reuse: preserve every previous build and receipt.
    extracted = output / "upstream"; extracted.mkdir()
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(extracted, filter="data")
    roots = list(extracted.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError("Unexpected native source archive root")
    roots[0].rename(output / "mmseqs")
    for relative, expected in proof["source_changes"].items():
        path = output / "mmseqs" / relative
        if expected["before"] is None:
            if path.exists(): raise ValueError("Unexpected pre-existing native helper")
        elif sha(path) != expected["before"]:
            raise ValueError("Native upstream source member changed: " + relative)
    subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch)],
                   cwd=output/"mmseqs", check=True)
    for relative, expected in proof["source_changes"].items():
        if sha(output/"mmseqs"/relative) != expected["after"]:
            raise ValueError("Patched native source member changed: " + relative)
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    output = args.output.absolute()
    proof = prepare(args.archive.absolute(), output)
    receipt = dict(source_verified=True, build_completed=False,
                   tested_elf_sha256=native_runtime.manifest()["files"]["portable/mmseqs"]["sha256"])
    if not args.prepare_only:
        cmake = proof["toolchain"]["cmake"]
        configure = [cmake, *proof["configure"][1:],
                     "-DCMAKE_C_COMPILER="+proof["toolchain"]["cc"],
                     "-DCMAKE_CXX_COMPILER="+proof["toolchain"]["c++"]]
        subprocess.run(configure, cwd=output, check=True)
        subprocess.run([cmake, *proof["build"][1:]], cwd=output, check=True)
        subprocess.run([proof["toolchain"]["strip"], "--strip-debug", str(output/"build/src/mmseqs")], check=True)
        receipt.update(build_completed=True, rebuilt_elf_sha256=sha(output/"build/src/mmseqs"))
        receipt["matches_tested_elf"] = receipt["rebuilt_elf_sha256"] == receipt["tested_elf_sha256"]
    (output/"rebuild-receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True)+"\n")
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
