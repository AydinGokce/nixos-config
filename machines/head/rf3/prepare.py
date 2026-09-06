#!/usr/bin/env python3
"""Bind RF3's explicit protein-chain MSAs without performing a sequence search."""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile


class Error(RuntimeError):
    pass


AA = "ACDEFGHIKLMNPQRSTVWY"
CANONICAL_CCD = dict(zip(("ALA", "CYS", "ASP", "GLU", "PHE", "GLY", "HIS", "ILE", "LYS", "LEU",
                          "MET", "ASN", "PRO", "GLN", "ARG", "SER", "THR", "VAL", "TRP", "TYR"), AA))
CHAIN_TYPES = {"POLYPEPTIDE(L)", "POLYDEOXYRIBONUCLEOTIDE", "POLYRIBONUCLEOTIDE"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(8*1024*1024):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Error("Duplicate JSON key: " + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=unique)


def chain_name(index):
    result = ""
    while True:
        result = chr(65+index % 26) + result
        index = index // 26 - 1
        if index < 0:
            return result


def fasta_document(path, name="rf3_job"):
    records = []
    for line in Path(path).read_text().splitlines():
        if line.startswith(">"):
            if not line[1:].strip():
                raise Error("FASTA headers must not be empty")
            records.append([line[1:].strip(), ""])
        elif line.strip():
            if not records or re.search(r"[^" + AA + r"]", line.strip()):
                raise Error("FASTA input requires declared canonical protein sequences")
            records[-1][1] += line.strip()
    if not records or any(not seq for _, seq in records):
        raise Error("FASTA requires a nonempty sequence for every header")
    document = {"name": name, "components": [dict(chain_id=chain_name(i), seq=seq, chain_type="POLYPEPTIDE(L)")
                                              for i, (_, seq) in enumerate(records)]}
    return document, {chain_name(i): header for i, (header, _) in enumerate(records)}


def protein_query(sequence):
    if isinstance(sequence, list):
        if not sequence or any(not isinstance(x, str) or not re.fullmatch(r"[A-Z]|\([A-Za-z0-9_:+-]+\)", x) for x in sequence):
            raise Error("Invalid explicit CCD protein sequence")
        return protein_query("".join(sequence))
    if not isinstance(sequence, str):
        raise Error("RF3 preparation requires generalized FASTA or explicit CCD sequences")
    tokens = re.findall(r"[A-Z]|\([A-Za-z0-9_:+-]+\)", sequence)
    if not tokens or "".join(tokens) != sequence:
        raise Error("Invalid generalized protein sequence")
    result = ""
    for token in tokens:
        if token.startswith("("):
            result += CANONICAL_CCD.get(token[1:-1], "X")
        elif token in AA + "X":
            result += token
        else:
            raise Error("Unsupported protein one-letter residue: " + token)
    return result


def document(value):
    if isinstance(value, list):
        if len(value) != 1:
            raise Error("Each submitted RF3 job must contain exactly one assembly")
        value = value[0]
    if (not isinstance(value, dict) or not isinstance(value.get("name"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value["name"])
            or not isinstance(value.get("components"), list) or not value["components"]):
        raise Error("RF3 input needs a safe name and a nonempty component list")
    if value.get("msa_paths"):
        raise Error("Use explicit component msa_path fields, not ambiguous top-level msa_paths")
    seen = set()
    for component in value["components"]:
        chain = component.get("chain_id") if isinstance(component, dict) else None
        if not isinstance(chain, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", chain) or chain in seen:
            raise Error("Every component requires a unique explicit chain_id")
        seen.add(chain)
        if "seq" in component:
            if str(component.get("chain_type")).upper() not in CHAIN_TYPES:
                raise Error("Every polymer requires its explicit supported RF3 chain_type")
            seq = component["seq"]
            tokens = seq if isinstance(seq, list) else re.findall(r"[A-Z]|\([A-Za-z0-9_:+-]+\)", seq) if isinstance(seq, str) else []
            if len(tokens) < 4:
                raise Error("Pinned RF3 removes polymers shorter than four residues; this input is unsupported")
        elif component.get("msa_path"):
            raise Error("An MSA cannot be attached to a non-polymer component")
    return copy.deepcopy(value)


def protein_queries(value):
    value = document(value)
    return {c["chain_id"]: protein_query(c["seq"]) for c in value["components"]
            if str(c.get("chain_type")).upper() == "POLYPEPTIDE(L)"}


def validate_a3m(path, query):
    path = Path(path)
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        text = handle.read()
    records = []
    for line in text.splitlines():
        if line.startswith(">"):
            if not line[1:].strip():
                raise Error("A3M header is empty")
            records.append([line[1:], ""])
        elif line:
            if not records:
                if line.startswith("#"):
                    raise Error("Concatenated/pre-paired A3M headers are not supported by this RF3 route")
                raise Error("A3M sequence appears before its header")
            if re.search(r"[^A-Za-z-]", line):
                raise Error("A3M contains unsupported alignment characters")
            records[-1][1] += line
    if not records or records[0][1] != query:
        raise Error("A3M query differs from its exact protein chain sequence")
    taxonomy = []
    for header, seq in records:
        aligned = re.sub("[a-z]", "", seq)
        if len(aligned) != len(query) or re.search(r"[^" + AA + r"XBZUO-]", aligned):
            raise Error("A3M aligned rows differ in width or contain unsupported residues")
        matches = re.findall(r"(?:^|\s)TaxID=([^\s]+)", header)
        if len(matches) > 1 or any(not re.fullmatch(r"[0-9]+", item) for item in matches):
            raise Error("A3M taxonomy identifiers must be unambiguous numeric TaxID values")
        taxonomy.append(matches[0] if matches else "")
    return {"query": query, "depth": len(records), "taxonomy_rows": sum(bool(x) for x in taxonomy[1:]),
            "taxonomy_ids_sha256": digest(taxonomy[1:]), "sha256": file_hash(path), "size": path.stat().st_size}


def prepare(*, out, fasta=None, native_json=None, msa_map=None, name="rf3_job"):
    if bool(fasta) == bool(native_json):
        raise Error("Specify exactly one FASTA or native JSON input")
    source = Path(fasta or native_json).resolve()
    value, headers = fasta_document(source, name) if fasta else (document(read_json(source)), {})
    queries = protein_queries(value)
    mapping = read_json(msa_map) if msa_map else None
    if mapping is not None and (not isinstance(mapping, dict) or set(mapping) != set(queries)):
        raise Error("MSA mapping must name every protein chain exactly once and no other components")
    destination = Path(out).absolute()
    if destination.exists() or destination.is_symlink():
        raise Error("Prepared destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".rf3-prepare-", dir=destination.parent))
    try:
        chain_msas = {}
        for component in value["components"]:
            chain = component["chain_id"]
            if chain in queries:
                path = mapping[chain] if mapping is not None else component.get("msa_path")
                if not isinstance(path, str) or not path:
                    raise Error("Protein chain " + chain + " has no explicit A3M; sequence-only fallback is disabled")
                origin = Path(path)
                if not origin.is_absolute():
                    origin = (Path(msa_map).resolve().parent if mapping is not None else source.parent) / origin
                evidence = validate_a3m(origin, queries[chain])
                relative = "msas/" + chain + (".a3m.gz" if origin.name.endswith(".gz") else ".a3m")
                (stage / relative).parent.mkdir(exist_ok=True)
                shutil.copyfile(origin, stage / relative)
                component["msa_path"] = relative
                chain_msas[chain] = dict(evidence, path=relative, source_sha256=evidence["sha256"])
            elif component.get("msa_path"):
                raise Error("This RF3 route only accepts protein MSAs")
            if component.get("path"):
                origin = Path(component["path"])
                origin = origin if origin.is_absolute() else source.parent / origin
                if origin.suffix.lower() not in {".sdf", ".cif"}:
                    raise Error("Native RF3 component assets must be SDF or CIF")
                relative = "assets/" + chain + origin.suffix.lower()
                (stage / relative).parent.mkdir(exist_ok=True)
                shutil.copyfile(origin, stage / relative)
                component["path"] = relative
        (stage / "input.json").write_text(json.dumps([value], indent=2, allow_nan=False) + "\n")
        files = {str(p.relative_to(stage)): file_hash(p) for p in sorted(stage.rglob("*")) if p.is_file()}
        manifest = dict(schema=1, kind="rf3-prepared", source_sha256=file_hash(source),
                        original_fasta_headers=headers, chain_msas=chain_msas, files=files,
                        pairing="RF3 native pairing by preserved header TaxID keys; provenance must distinguish biological taxonomy from synthetic pairing identifiers", source_format="fasta" if fasta else "rf3-json")
        manifest["sha256"] = digest(manifest)
        (stage / "msa-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        validate(stage / "input.json")
        os.rename(stage, destination)
        return destination / "input.json"
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def validate(input_path):
    input_path = Path(input_path).absolute()
    root = input_path.parent
    manifest = read_json(root / "msa-manifest.json")
    clean = {k:v for k,v in manifest.items() if k != "sha256"}
    if (type(manifest.get("schema")) is not int or manifest["schema"] != 1
            or manifest.get("kind") != "rf3-prepared" or manifest.get("sha256") != digest(clean)):
        raise Error("RF3 preparation manifest integrity check failed")
    actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and p.name != "msa-manifest.json"}
    if actual != set(manifest["files"]):
        raise Error("RF3 preparation file inventory differs from the manifest")
    for relative, expected in manifest["files"].items():
        path = root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts or any(p.is_symlink() for p in (path, *path.parents)):
            raise Error("RF3 prepared files must remain inside their regular directory")
        if file_hash(path) != expected:
            raise Error("RF3 prepared file SHA256 mismatch: " + relative)
    value = document(read_json(input_path))
    queries = protein_queries(value)
    if set(queries) != set(manifest["chain_msas"]):
        raise Error("RF3 prepared chain/MSA bindings differ")
    for component in value["components"]:
        chain = component["chain_id"]
        if chain in queries:
            evidence = manifest["chain_msas"][chain]
            if component.get("msa_path") != evidence["path"] or evidence["path"] not in manifest["files"]:
                raise Error("RF3 prepared MSA path binding differs")
            checked = validate_a3m(root / evidence["path"], queries[chain])
            if any(evidence.get(k) != v for k,v in checked.items()):
                raise Error("RF3 prepared alignment/taxonomy evidence differs")
        if component.get("path") and component["path"] not in manifest["files"]:
            raise Error("RF3 component asset is outside the prepared inventory")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("prepare")
    inputs = make.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--fasta")
    inputs.add_argument("--native-json")
    make.add_argument("--msa-map")
    make.add_argument("--name", default="rf3_job")
    make.add_argument("--out", required=True)
    check = commands.add_parser("validate")
    check.add_argument("--input", required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(prepare(out=args.out, fasta=args.fasta, native_json=args.native_json, msa_map=args.msa_map, name=args.name))
    else:
        print(json.dumps(validate(args.input), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (Error, OSError, ValueError, KeyError, TypeError) as exc:
        print("rf3-prepare: " + str(exc), file=sys.stderr)
        sys.exit(2)
