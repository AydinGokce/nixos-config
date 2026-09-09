"""Loss-aware native input adapter for pinned Protenix 2.0.0.

Translation uses only the standard library. ``preflight`` runs the installed
native CPU atom/feature parser; it never invokes search, model or weight code.
The registry can retain chemistry which this model cannot represent.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
from translation import materialized_identity

MODEL_VERSION = "2.0.0"
POLYMERS = {"protein": "proteinChain", "dna": "dnaSequence", "rna": "rnaSequence"}
ALPHABETS = {"protein": set("ACDEFGHIKLMNPQRSTVWYX"),
             "dna": set("ACGTNXIU"), "rna": set("ACGUNXI")}
COMMON_FIELDS = {"molecule_type", "bonds", "crosslinks"}


class Error(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Error("Protenix: " + message)


def keys(value, allowed, description):
    require(isinstance(value, dict), f"{description} must be an object")
    unsupported = set(value) - set(allowed)
    require(not unsupported, f"unsupported {description} fields: {sorted(unsupported)}")


def ccd_code(identity):
    fields = [key for key in ("ccd",) if key in identity]
    require(len(fields) == 1, "one unambiguous CCD identifier is required")
    code = identity[fields[0]]
    require(isinstance(code, str) and re.fullmatch(r"[A-Z0-9]{1,8}", code),
            "CCD identifier must be a single uppercase component code")
    # json_to_feature.SampleDictToFeatures.mse_to_met changes SE into sulfur.
    require(code != "MSE", "native MSE-to-MET conversion changes chemistry; MSE is unsupported")
    return code


def _polymer(identity, monomers):
    keys(identity, COMMON_FIELDS | {"sequence", "modifications", "circular"}, "polymer identity")
    molecule = identity["molecule_type"]
    sequence = identity.get("sequence")
    require(isinstance(sequence, str) and sequence and set(sequence) <= ALPHABETS[molecule],
            f"unsupported {molecule} sequence; specify supported letters and explicit CCD modifications")
    require(type(identity.get("circular", False)) is bool, "circular must be boolean")
    require(not identity.get("circular") or len(sequence) > 1, "circular polymer needs at least two residues")
    result = {"sequence": sequence}
    mods = identity.get("modifications", [])
    require(isinstance(mods, list), "modifications must be a list")
    positions = set()
    translated = []
    for mod in mods:
        keys(mod, {"position", "monomer_ref"}, "modification")
        pos = mod.get("position")
        require(type(pos) is int and 1 <= pos <= len(sequence), "modification position is out of range")
        require(pos not in positions, "multiple modifications at one residue cannot be merged")
        positions.add(pos)
        ref = mod.get("monomer_ref")
        require(isinstance(ref, str) and ref in monomers, "modification needs a resolved pinned monomer_ref")
        chemical = monomers[ref].get("identity")
        keys(chemical, {"ccd"}, "modified monomer identity")
        code = "CCD_" + ccd_code(chemical)
        translated.append({"ptmPosition": pos, "ptmType": code} if molecule == "protein" else
                          {"basePosition": pos, "modificationType": code})
    if translated:
        result["modifications"] = translated
    return result


def _ligand(identity, ref, destination, assets, entity_id):
    keys(identity, COMMON_FIELDS | {"smiles", "ccd", "structure_file", "structure_format"},
         "small molecule identity")
    representations = [name for name in ("smiles", "ccd", "structure_file") if name in identity]
    require(len(representations) == 1, "small molecule needs exactly one SMILES, CCD or SDF representation")
    representation = representations[0]
    if representation in ("ccd",):
        require("structure_format" not in identity, "structure_format is only valid for an SDF attachment")
        return {"ligand": "CCD_" + ccd_code(identity)}
    if representation == "smiles":
        require("structure_format" not in identity, "structure_format is only valid for an SDF attachment")
        smiles = identity["smiles"]
        require(isinstance(smiles, str) and smiles and not any(c in smiles for c in "\r\n\0"), "invalid SMILES")
        require(not smiles.startswith(("CCD_", "FILE_")), "SMILES collides with native ligand format prefix")
        return {"ligand": smiles}
    require(identity.get("structure_format", "sdf").lower() == "sdf", "only SDF structure attachments are supported")
    original = identity["structure_file"]
    require(isinstance(original, str) and original in assets.get(ref, {}), "SDF attachment is missing")
    path = PurePosixPath(original)
    require(not path.is_absolute() and ".." not in path.parts and path.suffix.lower() == ".sdf",
            "invalid SDF attachment path")
    source = Path(assets[ref][original])
    require(source.is_file() and source.stat().st_size > 0, "SDF attachment is missing or empty")
    relative = f"assets/entity-{entity_id}.sdf"
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    require(not target.exists(), "native SDF destination already exists")
    shutil.copyfile(source, target)
    return {"ligand": "FILE_" + relative}


def _bond(bond, components, chain=None):
    keys(bond, {"from", "to", "order"}, "covalent bond")
    require(bond.get("order", 1) in (1, "single") and type(bond.get("order", 1)) is not bool,
            "native explicit covalent bonds support single order only")
    result = {}
    for side, native in (("from", "left"), ("to", "right")):
        endpoint = bond.get(side)
        keys(endpoint, {"chain_id", "position", "atom"}, "bond endpoint")
        chain_id = endpoint.get("chain_id", chain)
        # Internal registry endpoints are validated using the placeholder A.
        if chain is not None and chain_id == "A":
            chain_id = chain
        require(chain_id in components, "bond references an unknown chain")
        component = components[chain_id]
        identity = component.get("resolved_identity", component["record"]["identity"])
        pos = endpoint.get("position", 1 if identity["molecule_type"] == "small_molecule" else None)
        limit = len(identity["sequence"]) if identity["molecule_type"] in POLYMERS else 1
        require(type(pos) is int and 1 <= pos <= limit, "bond residue position is out of range")
        atom = endpoint.get("atom")
        # Upstream integers switch between RDKit indices and atom-map labels.
        # Never reinterpret a registry index using that context-dependent rule.
        require(isinstance(atom, str) and atom.strip() == atom and atom and not atom.isdigit() and
                not any(x in atom for x in "\r\n\0"),
                "bond atoms require explicit native atom names; numeric indices/map labels are unsupported")
        result.update({f"{native}_entity": component["entity_id"], f"{native}_copy": 1,
                       f"{native}_position": pos, f"{native}_atom": atom})
    require(tuple(result[f"left_{x}"] for x in ("entity", "position", "atom")) !=
            tuple(result[f"right_{x}"] for x in ("entity", "position", "atom")), "self-bond is invalid")
    return result


def build(snapshot: dict, destination: Path, assets: dict, options: dict) -> dict:
    """Write one model-native job, retaining explicit chain order and chemistry."""
    keys(options, {"name", "msa_backend"}, "adapter option")
    require(options.get("msa_backend", "public") == "public",
            "mixed native input currently requires public MSA; no private fallback was performed")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    require(not (destination / "input.json").exists(), "input destination already exists")
    if "assembly_record" in snapshot:
        keys(snapshot["assembly_record"].get("identity"), {"components", "bonds"}, "assembly identity")
    components = snapshot.get("components")
    require(isinstance(components, list) and components, "snapshot has no components")
    monomers = snapshot.get("monomers", {})
    require(isinstance(monomers, dict), "snapshot monomers must be an object")
    name = options.get("name", "construct")
    require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name),
            "job name must be a simple filename component")
    mapping = {}
    sequences = []
    internal_bonds = []
    for index, component in enumerate(components, 1):
        keys(component, {"chain_id", "construct_ref", "record"}, "assembly component")
        chain = component.get("chain_id")
        require(isinstance(chain, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_]{0,31}", chain), "invalid chain ID")
        require(chain not in mapping, "duplicate chain ID")
        identity = materialized_identity(component.get("record", {}), snapshot)
        require(isinstance(identity, dict), "component identity is missing")
        molecule = identity.get("molecule_type")
        require(molecule in {*POLYMERS, "small_molecule"}, f"unsupported molecule type: {molecule}")
        if molecule in POLYMERS:
            native = _polymer(identity, monomers)
            kind = POLYMERS[molecule]
        else:
            native = _ligand(identity, component.get("construct_ref"), destination, assets, index)
            kind = "ligand"
        native.update(count=1, id=[chain])
        sequences.append({kind: native})
        mapping[chain] = dict(component, entity_id=index, resolved_identity=identity)
        if identity.get("circular"):
            # Registry circularity denotes canonical backbone closure. Use the
            # same C-N / O3'-P atoms that native _connect_inter_residue uses;
            # preflight confirms the modified residues retain these atoms.
            left_atom, right_atom = ("C", "N") if molecule == "protein" else ("O3'", "P")
            internal_bonds.append(({"from": {"chain_id": chain, "position": len(identity["sequence"]), "atom": left_atom},
                                    "to": {"chain_id": chain, "position": 1, "atom": right_atom}}, chain))
        for field in ("bonds", "crosslinks"):
            require(isinstance(identity.get(field, []), list), f"{field} must be a list")
            internal_bonds.extend((bond, chain) for bond in identity.get(field, []))
    bonds = snapshot.get("bonds", [])
    require(isinstance(bonds, list), "assembly bonds must be a list")
    covalent = [_bond(b, mapping) for b in bonds] + [_bond(b, mapping, chain) for b, chain in internal_bonds]
    seen = set()
    for bond in covalent:
        pair = frozenset(tuple(bond[f"{side}_{x}"] for x in ("entity", "position", "atom")) for side in ("left", "right"))
        require(pair not in seen, "duplicate covalent bond")
        seen.add(pair)
    query = {"name": name, "sequences": sequences}
    if covalent:
        query["covalent_bonds"] = covalent
    (destination / "input.json").write_text(json.dumps([query], indent=2) + "\n")
    return {"entrypoint": "input.json", "format": "protenix-json",
            "has_protein": any("proteinChain" in item for item in sequences),
            "model_version": MODEL_VERSION,
            "chain_map": [{"chain_id": chain, "entity_id": item["entity_id"], "copy_id": 1,
                           "construct_ref": item.get("construct_ref")} for chain, item in mapping.items()],
            "covalent_bond_count": len(covalent),
            "native_chemistry_policy": "CCD modifications; one 3D SDF molecule; named-atom single bonds; canonical backbone circularity; MSE rejected"}


def _sha(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def preflight(bundle_dir: Path, metadata: dict) -> dict:
    """Validate one complete chemical graph with the pinned native CPU parser."""
    require(importlib.metadata.version("protenix") == MODEL_VERSION, "native parser version mismatch")
    require(metadata.get("format") == "protenix-json", "native format mismatch")
    require(metadata.get("model_version") == MODEL_VERSION, "bundle model version mismatch")
    root = Path(bundle_dir).resolve()
    relative = metadata.get("entrypoint")
    require(isinstance(relative, str), "missing native entrypoint")
    source = (root / relative).resolve()
    require(source.is_relative_to(root) and source.is_file(), "native entrypoint leaves bundle or is absent")
    data = json.loads(source.read_text())
    require(isinstance(data, list) and len(data) == 1, "expected exactly one native prediction job")
    from rdkit import Chem
    import numpy as np
    import torch
    from protenix.data.inference import json_parser, json_to_feature
    from protenix.data.core import ccd
    def check_chemical_graph(mol, description):
        require(mol is not None and mol.GetNumAtoms() > 0, f"invalid {description} chemical graph")
        # Native ref features retain atomic element/formal charge, but have no
        # isotope or radical-electron channel (json_parser/ccd/featurizer).
        require(not any(atom.GetIsotope() for atom in mol.GetAtoms()),
                f"{description} isotope labels are not represented in native features")
        require(not any(atom.GetNumRadicalElectrons() for atom in mol.GetAtoms()),
                f"{description} radical electrons are not represented in native features")
    query = copy.deepcopy(data[0])
    expected_chains = []
    for item in query["sequences"]:
        require(len(item) == 1, "invalid native entity")
        kind, entity = next(iter(item.items()))
        require(entity.get("count") == 1 and isinstance(entity.get("id"), list) and len(entity["id"]) == 1,
                "every native entity must retain one explicit chain ID")
        expected_chains.extend(entity["id"])
        if kind == "ligand" and entity["ligand"].startswith("FILE_"):
            value = PurePosixPath(entity["ligand"][5:])
            require(not value.is_absolute() and ".." not in value.parts, "ligand path must be bundle-relative")
            ligand = (root / str(value)).resolve()
            require(ligand.is_relative_to(root) and ligand.is_file(), "ligand asset missing or outside bundle")
            supplier = Chem.SDMolSupplier(str(ligand))
            require(len(supplier) == 1, "SDF must contain exactly one molecule; native parser ignores later records")
            mol = supplier[0]
            check_chemical_graph(mol, "SDF")
            require(mol is not None and mol.GetNumConformers() == 1 and mol.GetConformer().Is3D(),
                    "SDF needs one valid 3D molecule")
            require(np.isfinite(mol.GetConformer().GetPositions()).all(), "SDF coordinates must be finite")
            entity["ligand"] = "FILE_" + str(ligand)
        elif kind == "ligand" and not entity["ligand"].startswith("CCD_"):
            check_chemical_graph(Chem.MolFromSmiles(entity["ligand"]), "SMILES")
    explicit_ccds = set()
    for item in query["sequences"]:
        kind, entity = next(iter(item.items()))
        if kind == "ligand" and entity["ligand"].startswith("CCD_"):
            explicit_ccds.add(entity["ligand"][4:])
        for modification in entity.get("modifications", []):
            explicit_ccds.add(modification.get("ptmType", modification.get("modificationType"))[4:])
    for code in explicit_ccds:
        check_chemical_graph(ccd.get_component_rdkit_mol(code), f"CCD {code}")
    parser = json_to_feature.SampleDictToFeatures(query)
    features, atoms, tokens = parser.get_feature_dict()
    require(len(atoms) > 0 and len(tokens) > 0, "native parser produced an empty structure")
    observed_chains = list(dict.fromkeys(atoms.chain_id.tolist()))
    require(observed_chains == expected_chains, "native parser changed chain order or omitted a component")
    require(np.isfinite(atoms.coord).all(), "native reference atom coordinates must be finite")
    for bond in query.get("covalent_bonds", []):
        endpoints = []
        for side in ("left", "right"):
            found = parser.get_a_bond_atom(atoms, bond[f"{side}_entity"], bond[f"{side}_position"],
                                          bond[f"{side}_atom"], bond[f"{side}_copy"])
            require(len(found) == 1, "native parser did not preserve the explicit bond atom")
            endpoints.append(int(found[0]))
        neighbors, orders = atoms.bonds.get_bonds(endpoints[0])
        require(endpoints[1] in neighbors and int(orders[list(neighbors).index(endpoints[1])]) == 1,
                "native parser did not preserve the requested single covalent bond")
    for name, value in features.items():
        if isinstance(value, torch.Tensor) and value.is_floating_point():
            require(bool(torch.isfinite(value).all()), f"non-finite native feature {name}")
    residues = {}
    for item in query["sequences"]:
        kind, entity = next(iter(item.items()))
        chain = entity["id"][0]
        subset = atoms[atoms.chain_id == chain]
        positions = list(dict.fromkeys(subset.res_id.tolist()))
        names = [sorted(set(subset.res_name[subset.res_id == pos].tolist())) for pos in positions]
        if kind in POLYMERS.values():
            require(positions == list(range(1, len(entity["sequence"]) + 1)), "native parser omitted polymer residues")
            mapping = {"proteinChain": json_parser.PROTEIN_1to3,
                       "dnaSequence": json_parser.DNA_1to3, "rnaSequence": json_parser.RNA_1to3}[kind]
            expected_names = [mapping[letter] for letter in entity["sequence"]]
            for modification in entity.get("modifications", []):
                position = modification.get("ptmPosition", modification.get("basePosition"))
                expected_names[position - 1] = modification.get("ptmType", modification.get("modificationType"))[4:]
            require(names == [[name] for name in expected_names], "native parser changed polymer/modified residue identity")
        elif entity["ligand"].startswith("CCD_"):
            require(names == [[entity["ligand"][4:]]], "native parser changed CCD ligand identity")
        residues[chain] = {"residue_count": len(positions), "atom_count": len(subset), "residue_names": names}
    return {"model": "protenix", "model_version": MODEL_VERSION, "native_parser": True,
            "model_inference": False, "msa_queries": False,
            "operation": "native CPU atom/feature construction; no MSA, checkpoint or model inference",
            "input_sha256": _sha(source), "python": sys.version, "interpreter": sys.executable,
            "atom_count": len(atoms), "token_count": len(tokens), "chain_order": observed_chains,
            "chains": residues, "covalent_bond_count": len(query.get("covalent_bonds", [])),
            "sources": {str(Path(module.__file__)): _sha(module.__file__) for module in (json_parser, json_to_feature)},
            "ccd": {"components_sha256": _sha(ccd.COMPONENTS_FILE), "rdkit_molecules_sha256": _sha(ccd.RKDIT_MOL_PKL)}}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("bundle", type=Path)
    args = p.parse_args()
    metadata = json.loads((args.bundle / "bundle.json").read_text())
    print(json.dumps(preflight(args.bundle, metadata), indent=2))
