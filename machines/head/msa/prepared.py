#!/usr/bin/env python3
"""Portable, versioned bundles of native model preparation outputs.

Validation/materialization use only the standard library and never query a
server. Capture runs in the pinned preparation/model environment. Alignment
files are copied byte-for-byte; this module does not generate model features.
"""
from __future__ import annotations

import argparse
import copy
import csv
import datetime
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
import zipfile
from urllib.parse import urlsplit

VERSIONS = {"openfold3": "0.5.0", "boltz2": "2.2.1", "protenix": "2.0.0"}
INPUT_NAMES = {"openfold3": "query.json", "boltz2": "input.yaml", "protenix": "input.json"}
MARKER = "bundle://"


class Error(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Error(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def json_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def runtime_versions():
    from importlib.metadata import distributions
    return {"python": sys.version, "packages": dict(sorted(
        (dist.metadata["Name"], dist.version) for dist in distributions() if dist.metadata.get("Name")))}


def load_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    def constant(value):
        raise Error(f"Invalid JSON number: {value}")
    return json.loads(Path(path).read_text(), object_pairs_hook=pairs, parse_constant=constant)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def load_yaml_or_json(path):
    try:
        return load_json(path)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError:
            raise Error("Reading YAML requires the pinned model environment (PyYAML)") from None
        return yaml.safe_load(Path(path).read_text())


def relative_path(value):
    require(isinstance(value, str) and value, "Bundle path must be a nonempty string")
    path = PurePosixPath(value)
    require(not path.is_absolute() and str(path) == value and
            all(p not in {".", ".."} for p in path.parts) and "\\" not in value,
            f"Unsafe bundle path: {value!r}")
    return path


def safe_file(root, value):
    path = root / relative_path(value)
    require(path.is_file(), f"Missing prepared file: {value}")
    require(not any(p.is_symlink() for p in [path, *path.parents] if p != root.parent),
            f"Prepared file must not use symlinks: {value}")
    require(path.resolve().is_relative_to(root.resolve()), f"File escaped bundle: {value}")
    return path


def get_at(value, pointer):
    for key in pointer:
        value = value[key]
    return value


def set_at(value, pointer, replacement):
    for key in pointer[:-1]:
        value = value[key]
    value[pointer[-1]] = replacement


def sequence(value):
    require(isinstance(value, str) and re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWYXBZUO]+", value),
            "Prepared protein sequence must be nonempty uppercase amino acids")
    return value


def chain_name(index):
    value = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(ord("A") + remainder) + value
    return value


def read_fasta(path):
    records = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            require(len(line) > 1, "Empty FASTA header")
            records.append([line[1:], ""])
        else:
            require(records, "FASTA sequence precedes its header")
            records[-1][1] += line
    require(records, "Empty FASTA")
    return [(header, sequence(seq)) for header, seq in records]


def alignment_rows(path):
    """Describe alignment text without deduplication, sorting or normalization."""
    if path.suffix == ".csv":
        reader = csv.DictReader(io.StringIO(path.read_text()))
        require(reader.fieldnames == ["key", "sequence"], "Boltz CSV requires key,sequence columns")
        rows = []
        for row in reader:
            require(None not in row and re.fullmatch(r"-1|[0-9]+", row["key"] or ""),
                    "Invalid Boltz pairing key")
            rows.append((row["key"], row["sequence"]))
    else:
        rows = []
        for line in path.read_text().splitlines():
            if not line:
                continue
            if line.startswith(">"):
                require(len(line) > 1, "Empty A3M header")
                rows.append([line[1:], ""])
            else:
                require(rows, "A3M sequence precedes its header")
                rows[-1][1] += line
    require(rows, f"Empty alignment: {path.name}")
    result = []
    for header, raw in rows:
        require(raw and re.fullmatch(r"[A-Za-z.\-]+", raw), "Invalid alignment characters")
        aligned, insertions, count = [], [], 0
        for char in raw:
            if char.islower():
                count += 1
            else:
                aligned.append(char)
                insertions.append(count)
                count = 0
        result.append({"header_or_pair_key": header, "raw": raw,
                       "aligned": "".join(aligned), "insertion_counts": insertions,
                       "trailing_insertions": count, "gap_count": raw.count("-") + raw.count(".")})
    return result


def text_alignment_summary(path, query, model=None):
    rows = alignment_rows(path)
    if model == "boltz2":
        require(all("." not in row["raw"] for row in rows), "Boltz does not accept '.' alignment tokens")
        if path.suffix != ".csv":
            lines = [line for line in path.read_text().splitlines() if line and not line.startswith(">")]
            require(len(lines) == len(rows), "Boltz native A3M parser requires one sequence line per header")
    require(rows[0]["aligned"] == query, f"Alignment query does not match chain sequence: {path.name}")
    require(all(len(r["aligned"]) == len(query) for r in rows),
            f"Alignment row width does not match query: {path.name}")
    return {"format": "boltz_csv" if path.suffix == ".csv" else "a3m",
            "rows": len(rows), "ordered_rows_sha256": json_digest(rows),
            "ordered_sequences_sha256": json_digest([r["aligned"] for r in rows]),
            "headers_or_pair_keys_sha256": json_digest([r["header_or_pair_key"] for r in rows]),
            "insertion_counts_sha256": json_digest([r["insertion_counts"] for r in rows]),
            "gap_count": sum(r["gap_count"] for r in rows),
            "insertion_count": sum(sum(r["insertion_counts"]) + r["trailing_insertions"] for r in rows),
            "pair_keys": [r["header_or_pair_key"] for r in rows] if path.suffix == ".csv" else None}


def npz_container(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(names and len(names) == len(set(names)), f"Empty/duplicate NPZ members: {path.name}")
        result = {}
        for name in names:
            require(name.endswith(".npy") and "/" not in name, f"Unexpected NPZ member: {name}")
            checksum = hashlib.sha256()
            with archive.open(name) as handle:
                magic = handle.read(6)
                require(magic == b"\x93NUMPY", f"Invalid NPY header: {name}")
                checksum.update(magic)
                # Head validation must not expand a large native MSA in memory.
                # Reading to EOF also verifies the ZIP member's CRC.
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    checksum.update(chunk)
            result[name] = checksum.hexdigest()
        return result


def inspect_of3_msa(path, query):
    """Only called by explicit trusted capture, never by head-side validation.

    OF3's own preparation writes dictionaries in object NPZ files. Its native
    loader also uses allow_pickle=True. Inputs here must be locally generated
    model outputs, not arbitrary downloaded bundles.
    """
    import numpy as np
    groups = {}
    with np.load(path, allow_pickle=True) as archive:
        for key in archive.files:
            data = archive[key].item()
            msa, deletions, metadata = data["msa"], data["deletion_matrix"], data["metadata"]
            require(msa.ndim == 2 and msa.shape == deletions.shape and len(msa) > 0,
                    "Malformed OF3 MSA arrays")
            require("".join(msa[0]) == query and msa.shape[1] == len(query), "OF3 MSA query mismatch")
            require(np.isfinite(deletions).all() and (deletions >= 0).all(), "Invalid OF3 deletion counts")
            headers = metadata.to_dict(orient="split") if hasattr(metadata, "to_dict") else metadata.tolist()
            groups[key] = {"rows": len(msa), "ordered_sequences_sha256": json_digest(msa.tolist()),
                           "headers_sha256": json_digest(headers),
                           "insertion_counts_sha256": json_digest(deletions.tolist()),
                           "insertion_count": int(deletions.sum()), "gap_count": int((msa == "-").sum())}
    return {"format": "of3_npz", "groups": groups, "semantic_validation": "trusted native preparation output"}


def coordinate_summary(path):
    try:
        import numpy as np
        from biotite.structure.io import pdbx, pdb
    except ImportError:
        return {"coordinate_sha256": None, "reason": "Biotite required for coordinate comparison"}
    if path.suffix.lower() == ".pdb":
        atoms = pdb.PDBFile.read(path).get_structure(model=1)
    else:
        atoms = pdbx.get_structure(pdbx.CIFFile.read(path), model=1)
    require(len(atoms) > 0 and np.isfinite(atoms.coord).all(), f"Invalid template coordinates: {path.name}")
    data = [atoms.chain_id.tolist(), atoms.res_id.tolist(), atoms.res_name.tolist(),
            atoms.atom_name.tolist(), atoms.coord.tolist()]
    return {"coordinate_sha256": json_digest(data), "atoms": len(atoms), "model": 1}


def local_template_mappings(pdb_ids, provenance, destination):
    """Supply native OF3 remapping with polymer IDs from the same local CIFs.

    pdbx_poly_seq_scheme includes unobserved residues/chains, unlike atom_site.
    OF3 retains its own sorted author-to-label inversion and hit selection.
    Missing/ambiguous mirror data aborts instead of dropping templates or fetching.
    """
    from biotite.structure.io.pdbx import CIFFile
    roots = [Path(provenance["database"][key]) for key in ("pdbdivided", "pdbobsolete")]
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    result = {}
    for pdb_id in sorted(pdb_ids):
        require(re.fullmatch(r"[0-9a-z]{4}", pdb_id) is not None, f"Unsupported PDB identifier: {pdb_id}")
        source = next((root / pdb_id[1:3] / (pdb_id + ".cif.gz") for root in roots
                       if (root / pdb_id[1:3] / (pdb_id + ".cif.gz")).is_file()), None)
        require(source is not None, f"Template {pdb_id} is missing from the private current/obsolete mirror")
        target = destination / (pdb_id + ".cif")
        with gzip.open(source, "rb") as incoming, target.open("wb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        block = CIFFile.read(target).block
        require("pdbx_poly_seq_scheme" in block, f"Template {pdb_id} lacks polymer chain mappings")
        scheme = block["pdbx_poly_seq_scheme"]
        labels, authors = scheme["asym_id"].as_array(), scheme["pdb_strand_id"].as_array()
        mappings = {}
        for label, author in zip(labels, authors, strict=True):
            require(label not in {"", ".", "?"} and author not in {"", ".", "?"},
                    f"Template {pdb_id} has incomplete polymer chain mappings")
            require(label not in mappings or mappings[label] == author,
                    f"Template {pdb_id} has ambiguous mapping for chain {label}")
            mappings[str(label)] = str(author)
        require(mappings, f"Template {pdb_id} has no polymer chain mappings")
        result[pdb_id] = mappings
    return result


def native_layout(model, native):
    """Return the exact supported native path fields and ordered protein chains."""
    document = native["document"]
    runtime = native.get("runtime", {})
    bindings, chains = [], []
    def bind(pointer, kind, query=None, chain=None):
        value = get_at(native, pointer)
        require(isinstance(value, str) and value, f"Missing native {kind} path at {pointer}")
        bindings.append({"pointer": pointer, "kind": kind, "sequence": query, "chain": chain})
    def chain(ids, seq, entity):
        seq = sequence(seq)
        require(isinstance(ids, list) and ids and all(isinstance(x, str) and x for x in ids), "Invalid chain IDs")
        chains.append({"ids": ids, "sequence": seq, "entity": entity})
        return seq
    if model == "protenix":
        require(isinstance(document, list) and len(document) == 1, "Bundle requires one Protenix query")
        for idx, entity in enumerate(document[0]["sequences"]):
            require(set(entity) == {"proteinChain"}, "Prepared v1 currently supports protein chains")
            protein = entity["proteinChain"]
            count = protein.get("count", 1)
            require(type(count) is int and 1 <= count <= 100, "Invalid protein copy count")
            seq = chain([f"entity_{idx}_copy_{i}" for i in range(count)], protein["sequence"], idx)
            found = False
            for key in ["unpairedMsaPath", "pairedMsaPath"]:
                if protein.get(key):
                    bind(["document", 0, "sequences", idx, "proteinChain", key], "a3m", seq, idx)
                    found = True
            require(found, f"Missing prepared MSA for Protenix entity {idx}")
            require("msa" not in protein, "Use Protenix's current explicit paired/unpaired MSA paths")
            if protein.get("templatesPath"):
                bind(["document", 0, "sequences", idx, "proteinChain", "templatesPath"], "template_alignment", chain=idx)
        # The current validated Protenix recipe does not use templates. Enabling
        # them additionally needs closed-over CIF/cache paths and a native adapter.
        require(runtime.get("use_templates") is False, "Protenix prepared templates are not yet supported; refusing to discard them")
        require(not any(x["kind"] == "template_alignment" for x in bindings), "Template inputs conflict with disabled templates")
    elif model == "boltz2":
        require(document.get("version") == 1, "Expected Boltz input schema version 1")
        for idx, entity in enumerate(document["sequences"]):
            require(set(entity) == {"protein"}, "Prepared v1 currently supports protein chains")
            protein = entity["protein"]
            ids = protein["id"] if isinstance(protein["id"], list) else [protein["id"]]
            seq = chain(ids, protein["sequence"], idx)
            require(protein.get("msa") not in (None, "", "empty", 0, -1), f"Missing prepared Boltz MSA for {ids}")
            kind = "boltz_csv" if str(protein["msa"]).endswith(".csv") else "a3m"
            bind(["document", "sequences", idx, "protein", "msa"], kind, seq, idx)
        for idx, template in enumerate(document.get("templates", [])):
            keys = [key for key in ["cif", "pdb"] if template.get(key)]
            require(len(keys) == 1, "Boltz template requires exactly one CIF/PDB path")
            bind(["document", "templates", idx, keys[0]], "template_coordinate")
        require(runtime.get("use_templates") == bool(document.get("templates")), "Boltz template policy mismatch")
    else:
        require(isinstance(document.get("queries"), dict) and len(document["queries"]) == 1,
                "Bundle requires one OpenFold3 query")
        name, query = next(iter(document["queries"].items()))
        require(query.get("use_msas", True) and query.get("use_main_msas", True), "OpenFold3 MSA usage must remain enabled")
        for idx, entity in enumerate(query["chains"]):
            require(entity.get("molecule_type", "").lower() == "protein", "Prepared v1 currently supports protein chains")
            seq = chain(entity["chain_ids"], entity["sequence"], idx)
            require(entity.get("main_msa_file_paths"), f"Missing OpenFold3 main MSA for entity {idx}")
            for key in ["main_msa_file_paths", "paired_msa_file_paths"]:
                for number, path in enumerate(entity.get(key) or []):
                    # Keep native NPZ format: A3M filenames also choose OF3's
                    # feature groups and require a separate verified adapter.
                    require(str(path).endswith(".npz"), "OpenFold3 prepared MSAs must retain native NPZ format")
                    bind(["document", "queries", name, "chains", idx, key, number], "of3_msa_npz", seq, idx)
            if entity.get("template_alignment_file_path"):
                require(str(entity["template_alignment_file_path"]).endswith(".npz"),
                        "OpenFold3 prepared templates require the final native NPZ cache, not raw hits")
                bind(["document", "queries", name, "chains", idx, "template_alignment_file_path"], "template_alignment", chain=idx)
            if entity.get("template_cif_paths"):
                for number, _ in enumerate(entity["template_cif_paths"]):
                    bind(["document", "queries", name, "chains", idx, "template_cif_paths", number], "template_coordinate", chain=idx)
            if entity.get("template_entry_chain_ids"):
                require(entity.get("template_alignment_file_path"), "OpenFold3 template IDs require the native alignment cache")
                require(runtime.get("use_templates") is True, "Cannot discard OpenFold3 template inputs")
        require(type(runtime.get("use_templates")) is bool, "Explicit OpenFold3 template policy required")
        if runtime["use_templates"]:
            settings = runtime.get("template_preprocessor_settings", {})
            require(not any(settings.get(key) for key in
                            ("precache_directory", "structure_array_directory", "ccd_file_path")),
                    "Custom OF3 template precache/structure-array/CCD paths need a separate portable adapter")
            for key in ["structure_directory", "cache_directory", "output_directory"]:
                require(settings.get(key), f"Missing OpenFold3 template {key}")
                bind(["runtime", "template_preprocessor_settings", key], "directory")
            if settings.get("log_directory"):
                bind(["runtime", "template_preprocessor_settings", "log_directory"], "directory")
            require(settings.get("fetch_missing_structures") is False, "Prepared template fetching must be disabled")
            require(not settings.get("preparse_structures"), "Preparsed OF3 template structures need a separate adapter")
    require(chains, "Prepared bundle has no protein chains")
    ids = [x for c in chains for x in c["ids"]]
    require(len(ids) == len(set(ids)), "Duplicate chain IDs")
    return bindings, chains


def validate(bundle, model=None, fasta=None):
    bundle = Path(bundle).resolve()
    manifest = load_json(bundle / "manifest.json")
    return _validate_manifest(bundle, manifest, model, fasta)


def _validate_manifest(bundle, manifest, model=None, fasta=None):
    require(manifest.get("schema_version") == 1, "Unsupported preparation bundle schema")
    name = manifest.get("model")
    require(name in VERSIONS and (model is None or name == model), "Prepared model mismatch")
    require(manifest.get("client_version") == VERSIONS[name], "Prepared client version mismatch")
    require(manifest.get("source", {}).get("kind") in {"public", "private"}, "Preparation source must be explicit")
    native = manifest["native_input"]
    bindings, chains = native_layout(name, native)
    require(chains == manifest.get("chains"), "Native chain order/sequence does not match manifest")
    if fasta:
        supplied = [seq for _, seq in read_fasta(fasta)]
        expected = [c["sequence"] for c in chains for _ in c["ids"]]
        require(supplied == expected, "FASTA chain sequence/order does not match prepared input")
    files = manifest["files"]
    require(isinstance(files, dict) and files, "Prepared bundle contains no files")
    for rel, entry in files.items():
        path = safe_file(bundle, rel)
        require(path.stat().st_size == entry.get("bytes") and digest(path) == entry.get("sha256"),
                f"Prepared file integrity mismatch: {rel}")
    for rel in manifest.get("directories", []):
        relative_path(rel)
        require((bundle / rel).is_dir() and not (bundle / rel).is_symlink(),
                f"Missing prepared directory: {rel}")
    provenance = manifest["source"].get("database_provenance")
    if provenance is not None:
        require(provenance in files, "Database provenance is not hash-bound to this bundle")
        require(isinstance(load_json(safe_file(bundle, provenance)), dict), "Invalid database provenance")
    require(manifest.get("bindings") == bindings, "Native file bindings were changed")
    for binding in bindings:
        marker = get_at(native, binding["pointer"])
        require(marker.startswith(MARKER), f"Native path is not portable: {marker}")
        rel = str(relative_path(marker[len(MARKER):]))
        if binding["kind"] == "directory":
            require((bundle / rel).is_dir() and (rel in manifest.get("directories", []) or
                    any(x.startswith(rel + "/") for x in files)),
                    f"Missing prepared template directory: {rel}")
            continue
        require(rel in files and files[rel]["bytes"] > 0, f"Missing/empty native input file: {rel}")
        path = safe_file(bundle, rel)
        if binding["kind"] in {"a3m", "boltz_csv"}:
            summary = text_alignment_summary(path, binding["sequence"], name)
            require(summary == files[rel].get("alignment"), f"Alignment semantics changed: {rel}")
        elif path.suffix == ".npz":
            require(npz_container(path) == files[rel].get("npz_members"), f"Native NPZ changed: {rel}")
            if binding["kind"] == "of3_msa_npz":
                require(files[rel].get("alignment", {}).get("semantic_validation") == "trusted native preparation output",
                        "OpenFold3 NPZ requires semantic validation in its preparation environment")
    return manifest


def validate_materialized(path, model, fasta=None, original_out=None):
    """Verify a native replay tree, including its exact expanded input/runtime.

    A fetched tree retains paths from its worker. In that case original_out is
    the original absolute materialization directory, which need not exist here.
    No native fields are removed or rewritten while comparing the documents.
    """
    root = Path(path).resolve()
    manifest = load_json(safe_file(root, "source_manifest.json"))
    _validate_manifest(root, manifest, model, fasta)
    destination = Path(original_out) if original_out is not None else Path(path).absolute()
    require(destination.is_absolute(), "Original materialization path must be absolute")
    native = copy.deepcopy(manifest["native_input"])
    for binding in manifest["bindings"]:
        rel = get_at(native, binding["pointer"])[len(MARKER):]
        set_at(native, binding["pointer"], str(destination / rel))
    for filename, expected in [(INPUT_NAMES[model], native["document"]),
                               ("runtime.json", native["runtime"])]:
        actual = load_json(safe_file(root, filename))
        require(json_digest(actual) == json_digest(expected),
                f"Materialized native document mismatch: {filename}")
    return manifest


def materialize(bundle, model, out, fasta=None):
    manifest = validate(bundle, model, fasta)
    destination = Path(out).absolute()
    require(not destination.exists(), f"Materialized output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=destination.name + ".", dir=destination.parent))
    try:
        for rel in manifest.get("directories", []):
            (temp / rel).mkdir(parents=True, exist_ok=True)
        for rel in manifest["files"]:
            target = temp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(bundle) / rel, target)
        native = copy.deepcopy(manifest["native_input"])
        for binding in manifest["bindings"]:
            rel = get_at(native, binding["pointer"])[len(MARKER):]
            set_at(native, binding["pointer"], str(destination / rel))
        write_json(temp / INPUT_NAMES[model], native["document"])
        write_json(temp / "runtime.json", native["runtime"])
        write_json(temp / "source_manifest.json", manifest)
        os.rename(temp, destination)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return destination / INPUT_NAMES[model]


def capture(model, run_dir, out, source="public", endpoint=None, path_maps=(), trust_native_npz=False,
            database_provenance=None):
    run_dir, out = Path(run_dir).absolute(), Path(out).absolute()
    require(not out.exists(), f"Capture output already exists: {out}")
    mappings = []
    for value in path_maps:
        old, new = value.split("=", 1)
        mappings.append((Path(old), Path(new)))
    def resolve(value):
        original = Path(value)
        candidate = original if original.is_absolute() else run_dir / original
        for old, new in mappings:
            if candidate.is_relative_to(old):
                candidate = new / candidate.relative_to(old)
                break
        require(candidate.exists(), f"Native preparation references missing file/directory: {value}")
        return candidate
    if model == "protenix":
        document = load_json(run_dir / "input-update-msa.json")
        runtime = {"use_templates": False}
    elif model == "boltz2":
        document = load_yaml_or_json(run_dir / "input.yaml")
        record = load_json(run_dir / "boltz_results_input/processed/records/input.json")
        by_id = {c["chain_name"]: c for c in record["chains"]}
        for item in document["sequences"]:
            if "protein" not in item:
                continue
            protein = item["protein"]
            ids = protein["id"] if isinstance(protein["id"], list) else [protein["id"]]
            records = [by_id[x] for x in ids]
            require(len({r["entity_id"] for r in records}) == 1, "Boltz entity mapping mismatch")
            if not protein.get("msa"):
                protein["msa"] = str(run_dir / f"boltz_results_input/msa/input_{records[0]['entity_id']}.csv")
        runtime = {"use_templates": bool(document.get("templates"))}
    else:
        document = load_json(run_dir / "inference_query_set.json")
        experiment = load_json(run_dir / "experiment_config.json")
        runtime = {"use_templates": experiment["experiment_settings"]["use_templates"]}
        if runtime["use_templates"]:
            settings = copy.deepcopy(experiment["template_preprocessor_settings"])
            settings["fetch_missing_structures"] = False
            runtime["template_preprocessor_settings"] = settings
    native = {"document": document, "runtime": runtime}
    bindings, chains = native_layout(model, native)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=out.name + ".", dir=out.parent))
    files, directories = {}, set()
    def copy_asset(original, kind=None, query=None):
        path = resolve(original)
        # Preserve original basename and lexical path order: OF3 uses both to
        # identify representative/group names and order multiple MSA files.
        absolute_original = Path(original) if Path(original).is_absolute() else run_dir / original
        rel = "files/" + absolute_original.as_posix().lstrip("/")
        relative_path(rel)
        if path.is_dir():
            require(not path.is_symlink(), f"Capture directory is a symlink: {path}")
            directories.add(rel)
            (temp / rel).mkdir(parents=True, exist_ok=True)
            for child in sorted(path.rglob("*")):
                if child.is_dir():
                    child_rel = rel + "/" + child.relative_to(path).as_posix()
                    directories.add(child_rel)
                    (temp / child_rel).mkdir(parents=True, exist_ok=True)
                elif child.is_file():
                    copy_asset(str(absolute_original / child.relative_to(path)))
        elif rel not in files:
            require(not path.is_symlink(), f"Capture file is a symlink: {path}")
            target = temp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            files[rel] = {"bytes": target.stat().st_size, "sha256": digest(target)}
            if path.suffix == ".npz":
                files[rel]["npz_members"] = npz_container(target)
            if path.suffix.lower() in {".cif", ".pdb"}:
                files[rel]["template_coordinates"] = coordinate_summary(target)
        if kind in {"a3m", "boltz_csv"}:
            files[rel]["alignment"] = text_alignment_summary(temp / rel, query, model)
        elif kind == "of3_msa_npz":
            require(trust_native_npz, "OF3 capture requires --trust-native-npz for its locally produced object arrays")
            files[rel]["alignment"] = inspect_of3_msa(temp / rel, query)
        return rel
    try:
        for binding in bindings:
            original = get_at(native, binding["pointer"])
            rel = copy_asset(original, binding["kind"], binding["sequence"])
            set_at(native, binding["pointer"], MARKER + rel)
        # Retain raw service responses, mappings and query text for comparison.
        raw_roots = {"openfold3": ["msas"], "boltz2": ["boltz_results_input/msa"], "protenix": []}[model]
        if model == "protenix":
            raw_roots = [str(p.relative_to(run_dir)) for p in run_dir.glob("*/msa")]
        for folder in raw_roots:
            if (run_dir / folder).exists():
                copy_asset(str(run_dir / folder))
        reference_files = []
        for filename in ["input.fasta", "reference_input.fasta", "query.json", "input.json", "input.yaml",
                         "input-update-msa.json", "query_msa.json", "inference_query_set.json", "job.json",
                         "model_config.json", "experiment_config.json", "reference_live_command.json",
                         "reference_effective_config.json", "reference_checkpoint_cache_sha256.json",
                         "preparation_metadata.json", "template_chain_mappings.json"]:
            if (run_dir / filename).is_file():
                reference_files.append(copy_asset(str(run_dir / filename)))
        manifest = {"schema_version": 1, "model": model, "client_version": VERSIONS[model],
                    "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "source": {"kind": source, "endpoint": endpoint,
                               "parity_status": "reference" if source == "public" else "unproven"},
                    "native_input": native, "reference_files": reference_files,
                    "chains": chains, "bindings": bindings, "files": files,
                    "directories": sorted(directories), "capture_runtime": runtime_versions()}
        if database_provenance:
            require(isinstance(load_json(database_provenance), dict), "Invalid database provenance")
            manifest["source"]["database_provenance"] = copy_asset(str(Path(database_provenance).absolute()))
        write_json(temp / "manifest.json", manifest)
        validate(temp, model)
        os.rename(temp, out)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return out


def prepare(model, fasta, out, server_url, source="private", database_provenance=None):
    """Invoke the pinned clients' preparation functions without constructing a model."""
    from importlib.metadata import version
    package = "boltz" if model == "boltz2" else model
    require(version(package) == VERSIONS[model], f"Preparation requires {package}=={VERSIONS[model]}")
    url = urlsplit(server_url)
    require(url.scheme in {"http", "https"} and url.netloc and not url.username and
            not url.password and not url.query and not url.fragment, "Use a plain HTTP(S) MSA server URL")
    require(source != "private" or database_provenance is not None,
            "Private preparation requires --database-provenance from the validated server snapshot")
    provenance = load_json(database_provenance) if database_provenance else None
    require(provenance is None or isinstance(provenance, dict), "Invalid database provenance")
    records = read_fasta(fasta)
    out = Path(out).absolute()
    require(not out.exists(), f"Preparation bundle already exists: {out}")
    # Retain the native workspace on both success and failure for diagnosis.
    work = out.with_name(out.name + ".native-work")
    require(not work.exists(), f"Native preparation workspace already exists: {work}")
    work.mkdir(parents=True)
    shutil.copyfile(fasta, work / "input.fasta")
    metadata = {"model": model, "client_version": VERSIONS[model], "source": source,
                "msa_endpoint": server_url, "inference_run": False,
                "template_source": "not_used"}
    if model == "protenix":
        os.environ["MMSEQS_SERVICE_HOST_URL"] = server_url.rstrip("/")
        from runner.msa_search import update_infer_json
        from protenix.web_service import colab_request_parser
        require(colab_request_parser.MMSEQS_SERVICE_HOST_URL == server_url.rstrip("/"),
                "Protenix was imported before its MSA endpoint was set")
        document = [{"name": "protenix", "sequences": [
            {"proteinChain": {"sequence": seq, "count": 1}} for _, seq in records]}]
        write_json(work / "input.json", document)
        updated, performed = update_infer_json(str(work / "input.json"), str(work),
                                              use_msa=True, mode="colabfold")
        require(performed and Path(updated).name == "input-update-msa.json", "Protenix did not prepare an MSA")
    elif model == "boltz2":
        from boltz.main import process_inputs
        cache = Path(os.environ.get("BOLTZ_CACHE", Path.home() / ".boltz"))
        require((cache / "mols").is_dir(), "Boltz preparation needs the existing BOLTZ_CACHE/mols CCD cache")
        document = {"version": 1, "sequences": [{"protein": {"id": chain_name(i), "sequence": seq}}
                                                  for i, (_, seq) in enumerate(records)]}
        write_json(work / "input.yaml", document)  # JSON is a YAML subset.
        process_inputs(data=[work / "input.yaml"], out_dir=work / "boltz_results_input",
                       ccd_path=cache / "ccd.pkl", mol_dir=cache / "mols",
                       msa_server_url=server_url, msa_pairing_strategy="greedy",
                       max_msa_seqs=8192, use_msa_server=True, boltz2=True,
                       preprocessing_threads=1)
        # 2.2.1 annotates this function -> Manifest but actually returns None.
        # It also catches per-input errors, so inspect its written manifest.
        result = load_json(work / "boltz_results_input/processed/manifest.json")
        require(len(result["records"]) == 1 and result["records"][0]["id"] == "input",
                "Boltz preparation failed; inspect the native preprocessing errors")
    else:
        from openfold3.core.data.tools import colabfold_msa_server
        from openfold3.core.data.tools.colabfold_msa_server import MsaComputationSettings, preprocess_colabfold_msas
        from openfold3.projects.of3_all_atom.config.inference_query_format import InferenceQuerySet
        from openfold3.core.data.pipelines.preprocessing.template import TemplatePreprocessor, TemplatePreprocessorSettings
        document = {"seeds": [42], "queries": {"openfold3": {"chains": [
            {"molecule_type": "protein", "chain_ids": [chain_name(i)], "sequence": seq}
            for i, (_, seq) in enumerate(records)]}}}
        write_json(work / "query.json", document)
        settings = MsaComputationSettings(server_url=server_url, msa_output_directory=work / "msas",
                                         save_openfold_outputs=True, save_colabfold_outputs=True)
        template_root = work / "template_data"
        original_mapping = colabfold_msa_server.fetch_label_to_author_chain_ids
        recorded_mappings = {}
        def recorded_mapping(pdb_ids):
            if source == "private":
                result = local_template_mappings(pdb_ids, provenance, template_root / "template_structures")
            else:
                result = original_mapping(pdb_ids)
            recorded_mappings.update(result)
            write_json(work / "template_chain_mappings.json", recorded_mappings)
            return result
        colabfold_msa_server.fetch_label_to_author_chain_ids = recorded_mapping
        if source == "private":
            metadata["template_source"] = "local_current_then_obsolete_PDB_mirror"
        else:
            metadata["template_source"] = "native_RCSB_mapping_and_CIF_download"
        try:
            query = preprocess_colabfold_msas(InferenceQuerySet.from_json(work / "query.json"), settings)
        finally:
            colabfold_msa_server.fetch_label_to_author_chain_ids = original_mapping
            settings.cleanup_workspace()
        (work / "query_msa.json").write_text(query.model_dump_json(indent=2))
        templates = TemplatePreprocessorSettings(output_directory=template_root,
                                                fetch_missing_structures=source != "private")
        TemplatePreprocessor(input_set=query, config=templates)()
        (work / "inference_query_set.json").write_text(query.model_dump_json(indent=2))
        write_json(work / "experiment_config.json", {
            "experiment_settings": {"use_templates": True},
            "template_preprocessor_settings": templates.model_dump(mode="json"),
            "msa_computation_settings": settings.model_dump(mode="json")})
    write_json(work / "preparation_metadata.json", metadata)
    return capture(model, work, out, source, server_url, trust_native_npz=model == "openfold3",
                   database_provenance=database_provenance)


def openfold_runner_config(out, prepared_runtime=None, base=None):
    """JSON/YAML override for OF3's native runner, preserving template data."""
    out = Path(out).absolute()
    config = {}
    if base:
        config = load_yaml_or_json(base)
        require(isinstance(config, dict), "OpenFold3 runner YAML must be a mapping")
    config.setdefault("msa_computation_settings", {})["cleanup_msa_dir"] = False
    if prepared_runtime:
        runtime = load_json(prepared_runtime)
        settings = runtime.get("template_preprocessor_settings", {})
        config["template_preprocessor_settings"] = settings
        return config
    root = out / "template_data"
    config.setdefault("template_preprocessor_settings", {}).update({
        "output_directory": str(root), "structure_directory": str(root / "template_structures"),
        "cache_directory": str(root / "template_cache")})
    return config


def compare(left, right):
    a, b = validate(left), validate(right)
    require(a["model"] == b["model"], "Compare the same model's preparations")
    def summary(manifest):
        aligned, templates = [], []
        for binding in manifest["bindings"]:
            if binding["kind"] == "directory":
                continue
            rel = get_at(manifest["native_input"], binding["pointer"])[len(MARKER):]
            entry = manifest["files"][rel]
            if "alignment" in entry:
                aligned.append({"chain": binding["chain"], "role": binding["pointer"][-2:], "summary": entry["alignment"]})
            elif binding["kind"].startswith("template"):
                templates.append({"kind": binding["kind"], "sha256": entry["sha256"], "coordinates": entry.get("template_coordinates")})
        native = manifest["native_input"]
        used_pdb_ids = None
        if manifest["model"] == "openfold3":
            query = next(iter(native["document"]["queries"].values()))
            mapping = [{"entity": i, "template_entry_chain_ids": item.get("template_entry_chain_ids")}
                       for i, item in enumerate(query["chains"])]
            used_pdb_ids = {template.split("_")[0] for item in query["chains"]
                            for template in (item.get("template_entry_chain_ids") or [])}
        elif manifest["model"] == "boltz2":
            mapping = [{k: v for k, v in item.items() if k not in {"cif", "pdb"}}
                       for item in native["document"].get("templates", [])]
        else:
            mapping = []
        coordinate_files = sorted(((Path(p).name, row["template_coordinates"]) for p, row in manifest["files"].items()
                                   if "template_coordinates" in row and
                                   (used_pdb_ids is None or Path(p).stem in used_pdb_ids)),
                                  key=lambda row: (row[0], json_digest(row[1])))
        settings = {k: v for k, v in native["runtime"].get("template_preprocessor_settings", {}).items()
                    if not k.endswith("_directory") and k not in {"create_logs", "fetch_missing_structures"}}
        return {"chains": manifest["chains"], "alignments": aligned, "templates": templates,
                "coordinate_files": coordinate_files, "template_mappings": mapping,
                "template_settings": settings, "use_templates": native["runtime"]["use_templates"]}
    left_summary, right_summary = summary(a), summary(b)
    fields = {key: left_summary[key] == right_summary[key] for key in left_summary}
    fields["template_coordinates_validated"] = all(
        row.get("coordinate_sha256") for data in (left_summary, right_summary)
        for _, row in data["coordinate_files"])
    return {"schema_version": 1, "model": a["model"], "client_version": a["client_version"],
            "left_source": a["source"], "right_source": b["source"],
            "preparation_equivalent": all(fields.values()), "checks": fields,
            "left": left_summary, "right": right_summary,
            "accuracy_benchmark": {"status": "not_run", "required": "same checkpoint, model settings/seeds, representative monomer/multimer targets and independent reference structures; compare structural accuracy and confidence, not confidence alone"}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ["validate", "materialize"]:
        p = commands.add_parser(command)
        p.add_argument("--bundle", required=True)
        p.add_argument("--model", choices=VERSIONS, required=True)
        p.add_argument("--fasta")
        if command == "materialize":
            p.add_argument("--out", required=True)
    p = commands.add_parser("validate-materialized")
    p.add_argument("--path", required=True)
    p.add_argument("--model", choices=VERSIONS, required=True)
    p.add_argument("--fasta")
    p.add_argument("--original-out")
    p = commands.add_parser("capture")
    p.add_argument("--model", choices=VERSIONS, required=True)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--source", choices=["public", "private"], default="public")
    p.add_argument("--endpoint")
    p.add_argument("--path-map", action="append", default=[])
    p.add_argument("--trust-native-npz", action="store_true")
    p.add_argument("--database-provenance")
    p = commands.add_parser("compare")
    p.add_argument("--left", required=True)
    p.add_argument("--right", required=True)
    p.add_argument("--out")
    p = commands.add_parser("prepare")
    p.add_argument("--model", choices=VERSIONS, required=True)
    p.add_argument("--fasta", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--server-url", required=True)
    p.add_argument("--source", choices=["public", "private"], default="private")
    p.add_argument("--database-provenance")
    p = commands.add_parser("openfold-runner-config")
    p.add_argument("--out", required=True)
    p.add_argument("--prepared-runtime")
    p.add_argument("--base")
    p.add_argument("--write", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            manifest = validate(args.bundle, args.model, args.fasta)
            print(json.dumps({"valid": True, "model": manifest["model"], "chains": manifest["chains"]}))
        elif args.command == "validate-materialized":
            manifest = validate_materialized(args.path, args.model, args.fasta, args.original_out)
            print(json.dumps({"valid": True, "model": manifest["model"], "chains": manifest["chains"]}))
        elif args.command == "materialize":
            print(materialize(args.bundle, args.model, args.out, args.fasta))
        elif args.command == "capture":
            print(capture(args.model, args.run_dir, args.out, args.source, args.endpoint, args.path_map,
                          args.trust_native_npz, args.database_provenance))
        elif args.command == "prepare":
            print(prepare(args.model, args.fasta, args.out, args.server_url, args.source, args.database_provenance))
        elif args.command == "openfold-runner-config":
            write_json(args.write, openfold_runner_config(args.out, args.prepared_runtime, args.base))
        else:
            report = compare(args.left, args.right)
            if args.out:
                write_json(args.out, report)
            print(json.dumps(report, indent=2))
            return 0 if report["preparation_equivalent"] else 1
    except (Error, OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"prepared: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
