#!/usr/bin/env python3
"""Translate resolved assemblies into the pinned RFAA input interface.

Construction is stdlib-only. ``preflight`` runs in the existing RFAA Python
environment on CPU: no model weights, protein search, or database loading.
The source checkout and its packages are read-only inputs to this adapter.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import string
import subprocess
import sys
from types import SimpleNamespace


PIN = "d69ab3a73f8ede31a4cc005fbc076a341d848469"
CHAINS = string.ascii_uppercase + string.ascii_lowercase + string.digits
ALPHABETS = {"protein": set("ACDEFGHIKLMNPQRSTVWY"), "dna": set("ACGT"), "rna": set("ACGU")}
GROUPS = {"protein": "protein_inputs", "dna": "na_inputs", "rna": "na_inputs", "small_molecule": "sm_inputs"}
EMPTY_SEMANTICS = {"modifications": [], "linkages": [], "termini": {}, "bonds": [], "crosslinks": []}
MAX_ASSET_BYTES = 16 * 1024**2
MAX_PREFLIGHT_LIGAND_ATOMS = 512
TEMPLATE_CORRECTION = "expand-only-single-all-masked-protein-template-to-configured-count-v1"
PREPARATION_VERSION = "rfaa-2020_06-2021Mar03-v1"
SOURCE_FILES = ("rf2aa/chemical.py", "rf2aa/data/parsers.py", "rf2aa/data/protein.py",
                "rf2aa/data/small_molecule.py", "rf2aa/data/nucleic_acid.py", "rf2aa/data/merge_inputs.py")


class UnsupportedInput(ValueError):
    pass


def require(value, message):
    if not value:
        raise UnsupportedInput("RFAA: " + message)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def json_read(path):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key: " + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(UnsupportedInput("RFAA: nonfinite JSON value")))


def relative_file(root, name):
    require(isinstance(name, str) and name and "${" not in name, "invalid staged input path")
    path = Path(name)
    require(not path.is_absolute() and all(part not in ("", ".", "..") for part in name.split("/")),
            "input files must use relative paths within their bundle")
    root = Path(root).resolve()
    current = root
    for part in path.parts:
        current /= part
        require(not current.is_symlink(), "staged input must not traverse a symlink")
    require(current.is_file() and current.resolve().is_relative_to(root), "missing staged input: " + name)
    require(current.stat().st_size <= MAX_ASSET_BYTES, "staged input exceeds the 16 MiB input limit")
    return current


def sequence(value, molecule, chain):
    require(isinstance(value, str) and value and set(value) <= ALPHABETS[molecule],
            f"chain {chain} requires an uppercase canonical {molecule} sequence; ambiguity and modified residues need another representation")
    return value


def sdf_bytes(raw, chain):
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_ASSET_BYTES, f"chain {chain}: empty or oversized SDF")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedInput(f"RFAA: chain {chain}: SDF must be UTF-8 text") from exc
    blocks = text.split("$$$$")
    require(len(blocks) <= 2 and (len(blocks) == 1 or not blocks[1].strip()),
            f"chain {chain}: the pinned reader only accepts one SDF molecule; split multiple records explicitly")
    lines = blocks[0].splitlines()
    require(len(lines) >= 5 and "V2000" in lines[3] and "V3000" not in text,
            f"chain {chain}: the pinned SDF reader requires V2000; V3000 cannot be passed through safely")
    try:
        atoms, bonds = int(lines[3][:3]), int(lines[3][3:6])
    except ValueError as exc:
        raise UnsupportedInput(f"RFAA: chain {chain}: invalid V2000 counts") from exc
    require(atoms > 0 and bonds >= 0 and len(lines) >= 4 + atoms + bonds,
            f"chain {chain}: incomplete V2000 atom/bond block")
    require(any(line.startswith("M  END") for line in lines[4 + atoms + bonds:]),
            f"chain {chain}: missing SDF M  END")
    return raw


def smiles(value, chain):
    require(isinstance(value, str) and value and len(value) <= 100000
            and not any(c.isspace() for c in value) and "${" not in value,
            f"chain {chain}: provide one whitespace-free SMILES expression; titles and extra records would be discarded by the pinned reader")
    return value


def build(snapshot, destination, assets, options):
    """Write native inputs; common adapters.py owns snapshot/bundle verification."""
    require(isinstance(snapshot, dict) and type(snapshot.get("schema")) is int and snapshot["schema"] == 1
            and snapshot.get("kind") == "resolved-assembly", "expected a resolved-assembly schema 1 snapshot")
    require(isinstance(options, dict) and set(options) <= {"mode", "msa_backend"}, "unsupported native run options")
    require(options.get("msa_backend", "public") in ("public", "private"), "unsupported MSA backend envelope")
    require(options.get("mode", "full") in ("full", "single-seq"), "mode must be full or single-seq")
    require(snapshot.get("bonds", []) == [],
            "assembly bonds are not enabled for this pinned adapter: covalent SDF atom numbering/chirality must first pass an independently verified native bond fixture")
    components = snapshot.get("components")
    require(isinstance(components, list) and 0 < len(components) <= len(CHAINS), "provide 1..62 explicit molecular chains")
    config = {"protein_inputs": {}, "na_inputs": {}, "sm_inputs": {}, "covale_inputs": None, "residue_replacement": None}
    planned, mapping, seen = {}, [], set()
    for item in components:
        require(isinstance(item, dict), "invalid assembly component")
        chain, pin, record = item.get("chain_id"), item.get("construct_ref"), item.get("record")
        require(isinstance(chain, str) and len(chain) == 1 and chain in CHAINS and chain not in seen,
                "chain IDs must be unique single letters or digits across every molecule type")
        seen.add(chain)
        require(isinstance(pin, str) and isinstance(record, dict) and isinstance(record.get("identity"), dict),
                f"chain {chain}: missing pinned construct identity")
        identity = dict(record["identity"])
        molecule = identity.get("molecule_type")
        require(isinstance(molecule, str) and molecule in GROUPS, f"chain {chain}: mixed/custom polymers are not representable by the pinned native polymer loaders")
        if "circular" in identity:
            require(identity["circular"] is False, f"chain {chain}: circular polymers are not implemented by the native input loader")
            del identity["circular"]
        for key, empty in EMPTY_SEMANTICS.items():
            if key in identity:
                require(identity[key] == empty and type(identity[key]) is type(empty),
                        f"chain {chain}: explicit {key} are not supported; the identity was retained and will not be simplified")
                del identity[key]
        if molecule in ALPHABETS:
            require(set(identity) == {"molecule_type", "sequence"},
                    f"chain {chain}: unsupported polymer identity fields: {sorted(set(identity) - {'molecule_type', 'sequence'})}")
            seq = sequence(identity["sequence"], molecule, chain)
            name = f"sequences/{chain}.fasta"
            planned[name] = f">{chain}\n{seq}\n".encode()
            config[GROUPS[molecule]][chain] = ({"fasta_file": name} if molecule == "protein"
                                              else {"fasta": name, "input_type": molecule})
        elif "smiles" in identity:
            require(set(identity) == {"molecule_type", "smiles"}, f"chain {chain}: ambiguous or unsupported ligand identity fields")
            config["sm_inputs"][chain] = {"input": smiles(identity["smiles"], chain), "input_type": "smiles"}
        else:
            require(set(identity) == {"molecule_type", "structure_format", "structure_file"}
                    and identity.get("structure_format") == "sdf",
                    f"chain {chain}: use an explicit SDF or SMILES; CCD codes/custom monomers are not resolved by this adapter")
            name = identity["structure_file"]
            require(isinstance(name, str), f"chain {chain}: invalid SDF attachment reference")
            source = assets.get(pin, {}).get(name)
            require(source is not None and Path(source).is_file() and not Path(source).is_symlink(),
                    f"chain {chain}: the exact pinned SDF attachment is unavailable")
            require(Path(source).stat().st_size <= MAX_ASSET_BYTES, f"chain {chain}: SDF exceeds the 16 MiB input limit")
            raw = sdf_bytes(Path(source).read_bytes(), chain)
            matches = [entry for entry in record.get("attachments", []) if entry.get("path") == name]
            require(len(matches) == 1 and matches[0].get("bytes") == len(raw)
                    and matches[0].get("sha256") == hashlib.sha256(raw).hexdigest(),
                    f"chain {chain}: SDF attachment differs from its pinned record")
            relative = f"ligands/{chain}.sdf"
            planned[relative] = raw
            config["sm_inputs"][chain] = {"input": relative, "input_type": "sdf"}
        mapping.append({"chain_id": chain, "construct_ref": pin, "molecule_type": molecule})
    for group in ("protein_inputs", "na_inputs", "sm_inputs"):
        if not config[group]:
            config[group] = None
    destination = Path(destination)
    require(not destination.is_symlink(), "bundle destination must not be a symlink")
    destination.mkdir(parents=True, exist_ok=True)
    for name, raw in planned.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        require(not target.parent.is_symlink() and not target.exists() and not target.is_symlink(), "refusing to overwrite staged native inputs")
        target.write_bytes(raw)
    entry = destination / "rfaa.json"
    require(not entry.exists() and not entry.is_symlink(), "refusing to overwrite the native configuration")
    entry.write_text(json.dumps(config, indent=2, allow_nan=False) + "\n")
    native_order = [chain for group in ("protein_inputs", "na_inputs", "sm_inputs") for chain in (config[group] or {})]
    return {"entrypoint": "rfaa.json", "format": "rfaa-json", "has_protein": bool(config["protein_inputs"]),
            "native_source_pin": PIN, "model_version": PIN, "component_map": mapping, "native_component_order": native_order,
            "msa_backend": "local-hhsuite",
            "requested_search_mode": options.get("mode"), "native_cpu_preflight_required": True,
            "native_input_notes": ["Protein MSA preparation follows the selected existing RFAA mode for every protein chain.",
                                   "Nucleic acids use the native query-only input path; protein/RNA MSA pairing is unavailable.",
                                   "Ligand conformers are regenerated by native OpenBabel/MMFF94; SDF coordinates are not restraints.",
                                   "Native output chain labels follow polymer/ligand connectivity; retained runtime mapping identifies the source chains."]}


def load_config(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "missing native configuration")
    config = json_read(path)
    require(isinstance(config, dict) and set(config) == {"protein_inputs", "na_inputs", "sm_inputs", "covale_inputs", "residue_replacement"},
            "unexpected native configuration fields")
    require(config["covale_inputs"] is None and config["residue_replacement"] is None, "unvalidated covalent/residue replacement input")
    seen, inputs = set(), []
    for group in ("protein_inputs", "na_inputs", "sm_inputs"):
        values = config[group]
        require(values is None or isinstance(values, dict), "invalid native chain map")
        for chain, value in (values or {}).items():
            require(isinstance(chain, str) and len(chain) == 1 and chain in CHAINS and chain not in seen,
                    "duplicate or invalid native chain ID")
            require(isinstance(value, dict), "invalid native component")
            seen.add(chain)
            if group == "protein_inputs":
                require(set(value) == {"fasta_file"}, "unsupported protein configuration")
                kind, key = "protein", "fasta_file"
            elif group == "na_inputs":
                require(set(value) == {"fasta", "input_type"} and value["input_type"] in ("dna", "rna"), "unsupported nucleic acid configuration")
                kind, key = value["input_type"], "fasta"
            else:
                require(set(value) == {"input", "input_type"} and value["input_type"] in ("sdf", "smiles"), "unsupported ligand configuration")
                kind, key = value["input_type"], "input"
            if kind == "smiles":
                smiles(value[key], chain)
            else:
                staged = relative_file(path.parent, value[key])
                if kind == "sdf":
                    sdf_bytes(staged.read_bytes(), chain)
                else:
                    rows = staged.read_text().splitlines()
                    require(rows and rows[0].startswith(">") and sum(row.startswith(">") for row in rows) == 1,
                            f"chain {chain}: expected exactly one FASTA record")
                    sequence("".join(rows[1:]), kind, chain)
                value[key] = str(staged)
            inputs.append((chain, kind, value[key]))
    require(inputs and len(seen) <= len(CHAINS), "empty or excessive native chain set")
    return config, inputs


def source_directory(source=None):
    path = Path(source or os.environ.get("RFAA_SOURCE_DIR", "/mnt/bio-shared/src/rfaa")).resolve()
    commit = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    require(commit == PIN, "native source checkout differs from the supported pin")
    sys.path.insert(0, str(path))
    return path


def molecule_components(mol):
    adjacency = {i: set() for i in range(mol.NumAtoms())}
    from openbabel import openbabel
    for bond in openbabel.OBMolBondIter(mol):
        a, b = bond.GetBeginAtomIdx()-1, bond.GetEndAtomIdx()-1
        adjacency[a].add(b); adjacency[b].add(a)
    groups, unseen = [], set(adjacency)
    while unseen:
        pending, group = [min(unseen)], set()
        while pending:
            item = pending.pop()
            if item in group:
                continue
            group.add(item); pending.extend(adjacency[item] - group)
        unseen -= group
        ordered = sorted(group)
        require(ordered == list(range(ordered[0], ordered[-1]+1)),
                "disconnected SDF fragments have interleaved atom indices; native PDB chain assignment would be ambiguous")
        groups.append(ordered)
    return groups


def expand_blank_protein_templates(data, configured_count):
    """Repair the native 1-vs-n empty-template axis, without adding templates."""
    current = int(data.xyz_t.shape[0])
    if current == configured_count:
        return data
    require(current == 1 and configured_count > 1 and not bool(data.mask_t.any()),
            "mixed-input template axes disagree; real templates will not be replicated or discarded")
    require(int(data.mask_t.shape[0]) == 1 and int(data.t1d.shape[0]) == 1,
            "inconsistent empty-template tensors")
    # False masks, unknown-residue identities and zero confidence characterize
    # the native blank template. Never widen a masked-out real template here.
    require(bool((data.t1d[..., -1] == 0).all()), "blank-template confidence must be zero")
    require(data.t1d.shape[-1] > 21, "invalid native blank-template features")
    expected = data.t1d.new_zeros(data.t1d.shape)
    expected[..., 20] = 1
    require(bool((data.t1d == expected).all()), "only native unknown-residue blank templates may be expanded")
    for field in ("xyz_t", "mask_t", "t1d"):
        value = getattr(data, field)
        setattr(data, field, value.repeat(configured_count, *([1] * (value.ndim-1))))
    require(not bool(data.mask_t.any()), "expanded template unexpectedly contains observed atoms")
    return data


def preflight(bundle_dir, metadata):
    """Actual CPU parsers; proteins/NA are linear-size query parsing only."""
    entry = relative_file(bundle_dir, metadata.get("entrypoint", "rfaa.json"))
    config, inputs = load_config(entry)
    source = source_directory()
    import torch
    import numpy as np
    from omegaconf import OmegaConf
    from openbabel import openbabel
    from rf2aa.chemical import initialize_chemdata, ChemicalData
    from rf2aa.data.parsers import parse_a3m, parse_multichain_fasta, parse_mol
    from rf2aa.data.small_molecule import compute_features_from_obmol
    torch.set_num_threads(min(4, os.cpu_count() or 1))
    base = OmegaConf.load(source / "rf2aa/config/inference/base.yaml")
    initialize_chemdata(base.chem_params)
    runner = SimpleNamespace(config=base, deterministic=True)
    results, output_chains = [], []
    for chain, kind, value in inputs:
        result = {"chain_id": chain, "input_type": kind}
        if kind == "protein":
            msa, ins, _ = parse_a3m(value)
            require(msa.shape[0] == 1 and bool(np.all(msa < 20)), f"chain {chain}: protein parser changed the canonical query")
            result["residues"] = int(msa.shape[1])
            groups = [list(range(int(msa.shape[1])))]
        elif kind in ("dna", "rna"):
            msa, ins, lengths = parse_multichain_fasta(value, dna_alphabet=kind == "dna", rna_alphabet=kind == "rna")
            require(msa.shape[0] == 1 and len(lengths) == 1, f"chain {chain}: native NA query was split")
            expected = {22, 23, 24, 25} if kind == "dna" else {27, 28, 29, 30}
            require(set(int(v) for v in msa[0]) <= expected, f"chain {chain}: native nucleotide identities differ")
            result["residues"] = int(msa.shape[1])
            groups = [list(range(int(msa.shape[1])))]
        else:
            # RDKit checks annotations which OpenBabel can silently discard
            # (notably radicals). Its normalized serialization is validation
            # evidence only: native RFAA still receives the original bytes.
            from chemistry import ligand
            from rdkit import Chem
            identity = {"molecule_type": "small_molecule"}
            assets = {}
            if kind == "smiles":
                identity["smiles"] = value
            else:
                identity.update(structure_file="input.sdf", structure_format="sdf")
                assets = {"native": {"input.sdf": Path(value)}}
            component = {"chain_id": chain, "construct_ref": "native", "record": {"identity": identity}}
            try:
                checked, chemistry = ligand(component, assets)
            except ValueError as exc:
                raise UnsupportedInput(f"RFAA: chain {chain}: {exc}") from exc
            graph = Chem.MolFromSmiles(checked["smiles"])
            require(not any(atom.GetFormalCharge() for atom in graph.GetAtoms()),
                    f"chain {chain}: formal charges are not explicit features of the pinned RFAA ligand model")
            require(all(bond.GetStereo() == Chem.BondStereo.STEREONONE for bond in graph.GetBonds()),
                    f"chain {chain}: double-bond stereochemistry is not represented by the pinned RFAA ligand features")
            # Parse the graph before native conformer/symmetry/feature work so
            # an enormous SMILES cannot allocate an unbounded square tensor on
            # the head. This is an explicit preflight limit, never truncation.
            probe, probe_msa, probe_ins, probe_xyz, probe_mask = parse_mol(
                value, filetype=kind, string=kind == "smiles", generate_conformer=False, find_automorphs=False)
            require(0 < probe.NumAtoms() <= MAX_PREFLIGHT_LIGAND_ATOMS,
                    f"chain {chain}: CPU preflight supports 1..{MAX_PREFLIGHT_LIGAND_ATOMS} heavy atoms per ligand; a larger molecule requires a separate resource-reviewed validation path")
            require(all(probe.GetAtom(i).GetAtomicNum() in ChemicalData().atomnum2atomtype for i in range(1, probe.NumAtoms()+1)),
                    f"chain {chain}: an element would be converted to the generic ATM token")
            require(all(probe.GetAtom(i).GetIsotope() == 0 for i in range(1, probe.NumAtoms()+1)),
                    f"chain {chain}: isotope labels are not represented by the pinned model")
            del probe, probe_msa, probe_ins, probe_xyz, probe_mask
            mol, msa, ins, xyz, mask = parse_mol(value, filetype=kind, string=kind == "smiles", generate_conformer=True)
            require(mol.NumAtoms() > 0, f"chain {chain}: the native ligand parser returned no atoms")
            atoms = [mol.GetAtom(i) for i in range(1, mol.NumAtoms()+1)]
            require(all(atom.GetAtomicNum() in ChemicalData().atomnum2atomtype for atom in atoms),
                    f"chain {chain}: an element would be converted to the generic ATM token")
            require(all(atom.GetIsotope() == 0 for atom in atoms), f"chain {chain}: isotope labels are not represented by the pinned model")
            require(bool(torch.isfinite(xyz).all()), f"chain {chain}: native conformer has nonfinite coordinates")
            conversion = openbabel.OBConversion()
            require(conversion.SetOutFormat("can"), "OpenBabel canonical graph audit is unavailable")
            native_smiles = conversion.WriteString(mol).split()[0]
            native_graph = Chem.MolFromSmiles(native_smiles)
            require(native_graph is not None and Chem.MolToSmiles(Chem.RemoveHs(native_graph), isomericSmiles=True)
                    == Chem.MolToSmiles(Chem.RemoveHs(graph), isomericSmiles=True),
                    f"chain {chain}: native OpenBabel parsing/conformer generation changed chemical identity or stereochemistry")
            bonds = list(openbabel.OBMolBondIter(mol))
            require(all(bond.IsAromatic() or bond.GetBondOrder() in (1, 2, 3) for bond in bonds),
                    f"chain {chain}: unsupported native bond order")
            features = compute_features_from_obmol(mol, msa, xyz, runner)
            require(bool(torch.isfinite(features.chirals).all()), f"chain {chain}: native chiral features are nonfinite")
            groups = molecule_components(mol)
            require(mol.NumAtoms() == chemistry["heavy_atoms"]
                    and sum(atom.GetFormalCharge() for atom in atoms) == chemistry["formal_charge"]
                    and len(groups) == chemistry["fragments"],
                    f"chain {chain}: native parsing changed the ligand graph or charge")
            result.update(heavy_atoms=mol.NumAtoms(), formal_charge=sum(atom.GetFormalCharge() for atom in atoms),
                          atomic_numbers=[atom.GetAtomicNum() for atom in atoms],
                          atom_formal_charges=[atom.GetFormalCharge() for atom in atoms],
                          bonds=[[bond.GetBeginAtomIdx(), bond.GetEndAtomIdx(), 4 if bond.IsAromatic() else bond.GetBondOrder()] for bond in bonds],
                          chiral_features=features.chirals.tolist(), connected_components=[len(group) for group in groups])
        for group in groups:
            require(len(output_chains) < len(CHAINS), "ligand fragments exceed the native 62 output-chain limit")
            output_chains.append({"output_chain_id": CHAINS[len(output_chains)], "input_chain_id": chain,
                                  "input_token_start": group[0]+1, "input_token_count": len(group)})
        results.append(result)
    return {"status": "passed", "native_parser": True, "model_inference": False, "msa_queries": False,
            "scope": "CPU native input parsing and ligand features; no weights, protein search, full assembly tensors, or inference",
            "native_source_pin": PIN, "entrypoint_sha256": digest(entry), "has_protein": bool(config["protein_inputs"]),
            "mixed_blank_template_correction": TEMPLATE_CORRECTION if config["protein_inputs"] and (config["na_inputs"] or config["sm_inputs"]) else None,
            "configured_template_count": int(base.loader_params.n_templ),
            "torch_version": torch.__version__, "openbabel_version": openbabel.OBReleaseVersion(),
            "components": results, "output_chain_map": output_chains,
            "source_files_sha256": {name: digest(source/name) for name in SOURCE_FILES}}


def verified_runtime_source(config_path, source):
    """Bind worker parsers to the CPU check before search or weight loading."""
    source = source_directory(source)
    preflight = json_read(Path(config_path).parent / "preflight.json")
    require(preflight.get("native_parser") is True and preflight.get("native_source_pin") == PIN
            and preflight.get("entrypoint_sha256") == digest(config_path), "missing or changed native CPU preflight evidence")
    source_hashes = {name: digest(source/name) for name in SOURCE_FILES}
    require(preflight.get("source_files_sha256") == source_hashes,
            "native parser source differs from the head CPU check; recheck the construct against the deployed runtime")
    return source, preflight, source_hashes


def verified_preparation(inputs, output, name, mode):
    """Accept the existing mode-specific receipts; never start fallback search."""
    require(mode in ("full", "single-seq"), "invalid selected preparation mode")
    evidence = {}
    for chain, kind, fasta in inputs:
        if kind != "protein":
            continue
        directory = Path(output) / name / chain
        files = ("query.fasta", "t000_.msa0.a3m", "t000_.hhr", "t000_.atab", ".prepare-request.json")
        require(all((directory/file).is_file() and not (directory/file).is_symlink() for file in files),
                f"chain {chain}: recipe preparation is incomplete; native search fallback is forbidden")
        query = "".join(Path(fasta).read_text().splitlines()[1:])
        request = {"version": PREPARATION_VERSION, "mode": mode,
                   "sequence_sha256": hashlib.sha256(query.encode()).hexdigest()}
        require(json_read(directory/".prepare-request.json") == request,
                f"chain {chain}: prepared request differs from the selected mode or exact query")
        expected_query = f">query\n{query}\n".encode()
        require((directory/"query.fasta").read_bytes() == expected_query,
                f"chain {chain}: prepared query differs from the native input")
        with (directory/"t000_.msa0.a3m").open() as stream:
            count, first_query = 0, []
            for line in stream:
                if line.startswith(">"):
                    count += 1
                elif count == 1:
                    first_query.append(line.strip())
        require(count > 0, f"chain {chain}: preparation returned an empty alignment")
        require("".join(first_query) == query, f"chain {chain}: MSA query differs from the native input")
        if mode == "single-seq":
            require((directory/"t000_.msa0.a3m").read_bytes() == expected_query
                    and all((directory/file).stat().st_size == 0 for file in ("t000_.hhr", "t000_.atab")),
                    f"chain {chain}: explicit single-seq artifacts contain an unexpected alignment or templates")
        else:
            require((directory/"preparation.json").is_file() and not (directory/"preparation.json").is_symlink(),
                    f"chain {chain}: full preparation has no completed search receipt")
            complete = json_read(directory/"preparation.json")
            require(all(complete.get(key) == value for key, value in request.items())
                    and complete.get("templates_searched") is True and type(complete.get("msa_sequences")) is int
                    and complete["msa_sequences"] == count
                    and (directory/"t000_.hhr").stat().st_size > 0,
                    f"chain {chain}: full preparation receipt/search artifacts are incomplete")
            files += ("preparation.json",)
        evidence[chain] = dict(request, msa_sequences=count, templates_searched=mode == "full",
                               files_sha256={file: digest(directory/file) for file in files})
    return evidence


def run(config_path, source, output, name, weights, hhdb, mode="full"):
    """Run unchanged native inference after the recipe prepares every protein."""
    config, inputs = load_config(config_path)
    source, preflight, source_hashes = verified_runtime_source(config_path, source)
    preparation = verified_preparation(inputs, output, name, mode)
    from omegaconf import OmegaConf
    from rf2aa.run_inference import ModelRunner
    from rf2aa.data import protein
    config = OmegaConf.merge(OmegaConf.load(source / "rf2aa/config/inference/base.yaml"), config)
    config.output_path, config.job_name, config.checkpoint_path = str(Path(output).resolve()), name, str(Path(weights).resolve())
    config.database_params.hhdb = hhdb
    config.database_params.sequencedb = hhdb
    runner = ModelRunner(config)
    original_load_protein = protein.load_protein
    correction_counts = []
    if config.protein_inputs and (config.na_inputs or config.sm_inputs):
        def corrected_load_protein(*args, **kwargs):
            data = original_load_protein(*args, **kwargs)
            before = int(data.xyz_t.shape[0])
            expand_blank_protein_templates(data, int(config.loader_params.n_templ))
            if before != int(data.xyz_t.shape[0]):
                correction_counts.append({"before": before, "after": int(data.xyz_t.shape[0]), "all_atom_masks_false": True})
            return data
        protein.load_protein = corrected_load_protein
    try:
        runner.infer()
    finally:
        protein.load_protein = original_load_protein
    result = Path(output) / (name + ".pdb")
    require(result.is_file() and result.stat().st_size > 0, "native inference did not produce its final PDB")
    return {"native_source_pin": PIN, "configuration_sha256": digest(config_path), "pdb_sha256": digest(result),
            "mode": mode, "protein_preparation": preparation,
            "source_files_sha256": source_hashes, "output_chain_map": preflight["output_chain_map"],
            "configured_template_count": int(config.loader_params.n_templ),
            "mixed_blank_template_correction": {"version": TEMPLATE_CORRECTION, "applications": correction_counts},
            "native_input_chain_lengths": [[chain, int(length)] for chain, length in runner.raw_data.chain_lengths]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    info = sub.add_parser("info"); info.add_argument("--config", type=Path, required=True)
    info.add_argument("--field", choices=("has-protein", "proteins"), required=True)
    check = sub.add_parser("preflight"); check.add_argument("--bundle", type=Path, required=True)
    check.add_argument("--source", type=Path); check.add_argument("--out", type=Path)
    verify = sub.add_parser("verify-source")
    verify.add_argument("--config", type=Path, required=True); verify.add_argument("--source", type=Path, required=True)
    execute = sub.add_parser("run")
    for flag in ("config", "source", "output", "weights"):
        execute.add_argument("--" + flag, type=Path, required=True)
    for flag in ("name", "hhdb"):
        execute.add_argument("--" + flag, required=True)
    execute.add_argument("--receipt", type=Path, required=True)
    execute.add_argument("--mode", choices=("full", "single-seq"), required=True)
    args = parser.parse_args()
    if args.action == "verify-source":
        source, preflight, hashes = verified_runtime_source(args.config, args.source)
        print(json.dumps({"native_source_pin": PIN, "source_files_sha256": hashes}))
        return
    if args.action == "info":
        config, inputs = load_config(args.config)
        if args.field == "has-protein":
            print(int(bool(config["protein_inputs"])))
        else:
            for chain, kind, value in inputs:
                if kind == "protein":
                    print(chain + "\t" + value)
        return
    if args.action == "preflight":
        if args.source:
            os.environ["RFAA_SOURCE_DIR"] = str(args.source)
        with contextlib.redirect_stdout(sys.stderr):
            result = preflight(args.bundle, {"entrypoint": "rfaa.json"})
        if args.out:
            args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
        print(json.dumps(result, indent=2, allow_nan=False))
    else:
        result = run(args.config, args.source, args.output, args.name, args.weights, args.hhdb, args.mode)
        args.receipt.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    try:
        main()
    except (UnsupportedInput, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
