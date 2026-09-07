"""Admission and prepared-topology checks for supplied noncanonical residues.

This validates an explicitly parameterized *incorporated* DNA/RNA fragment,
not a supplier's protected phosphoramidite. No residue substitution, bond-order
inference, charge fitting, or force-field parameter invention is performed.
The atom-mapped SMILES uses explicit hydrogen atoms and degree-one dummy atoms
only at declared polymer attachment points. Every real atom has a unique name,
element, formal charge, stereochemical assignment, atom type, partial charge,
and mass. Parameter files and compatible force-field/water models are pinned.

Graph validation uses RDKit's primary API documented at
https://www.rdkit.org/docs/RDKit_Book.html#stereochemistry . Force-field atom,
bond, and external-linkage distinctions follow explicit residue templates:
https://docs.openmm.org/latest/userguide/application/06_creating_ffs.html .
GROMACS topology syntax: https://manual.gromacs.org/current/reference-manual/topologies/topology-file-formats.html
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

SCHEMA = "bio-md-residue-parameters.v1"
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or any(ord(x) < 32 for x in value):
        raise ValueError(f"{field} requires nonempty printable text")
    return value


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} requires a finite number")
    return float(value)


def _rdkit():
    try:
        from rdkit import Chem
    except ImportError as exc:
        raise RuntimeError("Exact residue graph validation requires RDKit in the MD runtime") from exc
    return Chem


def _resonance_equivalent_phosphate(mol, atom):
    # A localized P=O / P-O(-) SMILES can appear stereogenic to a graph toolkit.
    # Unprotonated, same-isotope nonbridging oxygens are resonance equivalent.
    # This exception does NOT cover phosphorothioates or isotopic substitution.
    if atom.GetAtomicNum() != 15 or atom.GetDegree() != 4:
        return False
    oxygens = [neighbor for neighbor in atom.GetNeighbors()
               if neighbor.GetAtomicNum() == 8 and neighbor.GetDegree() == 1]
    if len(oxygens) != 2 or oxygens[0].GetIsotope() != oxygens[1].GetIsotope():
        return False
    return sorted((neighbor.GetFormalCharge(), mol.GetBondBetweenAtoms(atom.GetIdx(), neighbor.GetIdx()).GetBondTypeAsDouble())
                  for neighbor in oxygens) == [(-1, 1.0), (0, 2.0)]


def _graph(manifest):
    Chem = _rdkit()
    graph = manifest.get("graph")
    if not isinstance(graph, dict):
        raise ValueError("graph with atom-mapped incorporated_smiles and atoms is required")
    smiles = _text(graph.get("incorporated_smiles"), "graph.incorporated_smiles")
    options = Chem.SmilesParserParams()
    options.removeHs = False
    mol = Chem.MolFromSmiles(smiles, options)
    if mol is None or len(Chem.GetMolFrags(mol)) != 1:
        raise ValueError("Incorporated graph must be one chemically valid connected fragment")
    atoms = list(mol.GetAtoms())
    maps = [atom.GetAtomMapNum() for atom in atoms]
    if any(number <= 0 for number in maps) or len(set(maps)) != len(maps):
        raise ValueError("Every real and attachment atom must have a unique positive atom-map number")
    if len(atoms) > 4096 or any(atom.GetNumImplicitHs() or atom.GetNumExplicitHs() for atom in atoms):
        raise ValueError("All hydrogens must be explicit mapped atoms; maximum fragment size is 4096 atoms")
    if any(atom.GetNumRadicalElectrons() for atom in atoms):
        raise ValueError("Radical residue parameterization is outside this contract")
    Chem.AssignStereochemistry(mol, cleanIt=True, force=True)
    ignored_resonance = []
    for item in Chem.FindPotentialStereo(mol):
        if item.specified != Chem.StereoSpecified.Specified:
            if item.type == Chem.StereoType.Atom_Tetrahedral and _resonance_equivalent_phosphate(mol, mol.GetAtomWithIdx(item.centeredOn)):
                ignored_resonance.append(mol.GetAtomWithIdx(item.centeredOn).GetAtomMapNum())
            else:
                raise ValueError("Every stereogenic atom/bond must be specified; arbitrary amidite stereoisomers are not interchangeable")
    specifications = graph.get("atoms")
    real_atoms = {atom.GetAtomMapNum(): atom for atom in atoms if atom.GetAtomicNum() != 0}
    if not isinstance(specifications, list) or len(specifications) != len(real_atoms):
        raise ValueError("graph.atoms must cover every real atom exactly once, including hydrogen atoms")
    declared, names = {}, set()
    for spec in specifications:
        if not isinstance(spec, dict) or type(spec.get("map")) is not int or spec["map"] not in real_atoms or spec["map"] in declared:
            raise ValueError("Invalid, duplicate, or unmatched atom map in graph.atoms")
        atom = real_atoms[spec["map"]]
        name = _text(spec.get("name"), "atom.name")
        if len(name) > 16 or any(char.isspace() for char in name) or name in names:
            raise ValueError("Atom names must be unique tokens of at most 16 characters")
        names.add(name)
        if spec.get("element") != atom.GetSymbol() or type(spec.get("formal_charge")) is not int or spec["formal_charge"] != atom.GetFormalCharge():
            raise ValueError(f"Element or formal charge disagrees with exact graph for atom {name}")
        if spec.get("isotope", 0) != atom.GetIsotope():
            raise ValueError(f"Isotope disagrees with exact graph for atom {name}")
        cip = atom.GetProp("_CIPCode") if atom.HasProp("_CIPCode") else "none"
        if atom.GetAtomMapNum() in ignored_resonance:
            cip = "resonance_equivalent"
        if spec.get("stereochemistry") != cip:
            raise ValueError(f"Stereochemistry disagrees with incorporated graph for atom {name}: expected {cip}")
        _text(spec.get("atom_type"), "atom.atom_type")
        _number(spec.get("partial_charge_e"), "atom.partial_charge_e")
        if _number(spec.get("mass_da"), "atom.mass_da") <= 0:
            raise ValueError("Real atoms need positive masses")
        declared[spec["map"]] = dict(spec)
    dummy_atoms = {atom.GetAtomMapNum(): atom for atom in atoms if atom.GetAtomicNum() == 0}
    links = manifest.get("attachment_points")
    if not isinstance(links, list) or len(links) != len(dummy_atoms) or not 1 <= len(links) <= 4:
        raise ValueError("Every attachment dummy must have one explicit polymer linkage; one to four links are required")
    seen, validated_links = set(), []
    for link in links:
        if not isinstance(link, dict) or type(link.get("dummy_map")) is not int or link["dummy_map"] not in dummy_atoms or link["dummy_map"] in seen:
            raise ValueError("Invalid or duplicate attachment dummy")
        seen.add(link["dummy_map"])
        dummy = dummy_atoms[link["dummy_map"]]
        if dummy.GetDegree() != 1 or dummy.GetFormalCharge() != 0 or dummy.GetIsotope() != 0:
            raise ValueError("Attachment dummies must be neutral degree-one unlabelled atoms")
        neighbor = dummy.GetNeighbors()[0]
        if link.get("atom_map") != neighbor.GetAtomMapNum() or link["atom_map"] not in declared:
            raise ValueError("Attachment does not identify its exact residue anchor atom")
        order = mol.GetBondBetweenAtoms(dummy.GetIdx(), neighbor.GetIdx()).GetBondTypeAsDouble()
        if link.get("bond_order") != order or order != 1:
            raise ValueError("Only explicit single polymer linkages are currently supported")
        if type(link.get("partner_residue_offset")) is not int or link["partner_residue_offset"] not in {-1, 1}:
            raise ValueError("Polymer linkage must identify the preceding or following residue")
        _text(link.get("partner_atom_name"), "link.partner_atom_name")
        if link.get("partner_element") not in {"C", "N", "O", "P", "S"}:
            raise ValueError("Polymer linkage must declare its partner element")
        validated_links.append({**link, "atom_name": declared[link["atom_map"]]["name"]})
    bonds = []
    for bond in mol.GetBonds():
        a, b = bond.GetBeginAtom().GetAtomMapNum(), bond.GetEndAtom().GetAtomMapNum()
        if a in declared and b in declared:
            bonds.append({"atoms": [declared[a]["name"], declared[b]["name"]],
                          "order": bond.GetBondTypeAsDouble(), "stereo": str(bond.GetStereo())})
    return {"atoms": list(declared.values()), "bonds": bonds, "attachment_points": validated_links,
            "formal_charge_e": sum(atom.GetFormalCharge() for atom in real_atoms.values()),
            "mapped_graph_sha256": _hash({"smiles": smiles, "atoms": specifications, "links": links}),
            "resonance_equivalent_phosphate_maps": ignored_resonance,
            "rdkit_version": Chem.rdBase.rdkitVersion}


def validate_parameter_manifest(manifest, base_dir, expected_model=None):
    """Validate supplied chemistry/parameter identity; never certify accuracy."""
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ValueError(f"Expected schema {SCHEMA}")
    if manifest.get("chemical_form") != "incorporated" or manifest.get("polymer") not in {"DNA", "RNA"}:
        raise ValueError("Supply the incorporated DNA/RNA residue, not a protected synthesis reagent")
    name = _text(manifest.get("residue_name"), "residue_name")
    if len(name) > 16 or any(char.isspace() for char in name):
        raise ValueError("residue_name must be an exact topology token")
    if manifest.get("engine") != "gromacs":
        raise ValueError("This parameter contract currently supports supplied GROMACS parameters only")
    compatible = manifest.get("compatibility")
    if not isinstance(compatible, dict):
        raise ValueError("Explicit force-field/water compatibility is required")
    for field in ("force_fields", "water_models"):
        if not isinstance(compatible.get(field), list) or not compatible[field] or len(set(compatible[field])) != len(compatible[field]):
            raise ValueError(f"compatibility.{field} must be a unique nonempty list")
        for value in compatible[field]:
            _text(value, f"compatibility.{field}")
    if compatible.get("combination_rule") not in {1, 2, 3}:
        raise ValueError("GROMACS nonbonded combination_rule must be explicit")
    for field in ("fudge_lj", "fudge_qq"):
        number = _number(compatible.get(field), f"compatibility.{field}")
        if not 0 <= number <= 1:
            raise ValueError("1-4 scaling factors must lie between zero and one")
    if expected_model is not None:
        if expected_model.get("force_field") not in compatible["force_fields"] or expected_model.get("water_model") not in compatible["water_models"]:
            raise ValueError("Parameters are not declared compatible with this exact force-field/water combination")
    provenance = manifest.get("parameterization")
    if not isinstance(provenance, dict):
        raise ValueError("Parameterization provenance is required")
    for field in ("method", "software", "reference", "validation_evidence"):
        _text(provenance.get(field), f"parameterization.{field}")
    graph = _graph(manifest)
    charge = _number(manifest.get("net_charge_e"), "net_charge_e")
    if charge != graph["formal_charge_e"]:
        raise ValueError("Declared residue net charge disagrees with its incorporated formal chemical graph")
    partial = sum(atom["partial_charge_e"] for atom in graph["atoms"])
    if abs(partial-charge) > 1e-4:
        raise ValueError("Supplied partial charges do not sum to the declared incorporated residue charge")
    files = manifest.get("parameter_files")
    if not isinstance(files, list) or not files or len(files) > 128:
        raise ValueError("One to 128 SHA-pinned parameter files are required")
    root = Path(base_dir).resolve(strict=True)
    receipts, seen = [], set()
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str) or not SHA256.fullmatch(entry["sha256"]):
            raise ValueError("Each parameter file needs an exact SHA256")
        relative = Path(_text(entry.get("path"), "parameter file path"))
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() in seen:
            raise ValueError("Parameter paths must be unique relative paths inside the parameter bundle")
        path = (root/relative).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Parameter file escapes its supplied bundle")
        if not 0 < path.stat().st_size <= 128*1024*1024:
            raise ValueError("Parameter file is empty or exceeds 128 MiB")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            raise ValueError(f"Parameter SHA256 mismatch: {relative}")
        seen.add(relative.as_posix())
        receipts.append({"path": relative.as_posix(), "sha256": digest, "size": path.stat().st_size})
    return {"schema": "bio-md-residue-validation.v1", "status": "parameter_contract_validated",
            "residue_name": name, "polymer": manifest["polymer"], "manifest_sha256": _hash(manifest),
            "compatibility": compatible, "parameterization": provenance, "parameter_files": receipts,
            "net_charge_e": charge, "partial_charge_sum_e": partial, **graph,
            "limitations": ["Validation confirms supplied graph and parameter identity, not transferability or force-field accuracy.",
                            "Attachment dummies describe explicitly named polymer neighbors; complete construct stereochemistry and protonation must be retained during preparation.",
                            "A successful grompp and prepared-topology comparison are still required before simulation; arbitrary amidites without these parameters are unsupported."]}


def _processed_topology(path):
    path = Path(path)
    data = path.read_bytes()
    if not data or len(data) > 512*1024*1024:
        raise ValueError("Prepared topology is empty or exceeds 512 MiB")
    section, molecule = None, None
    molecules, atom_types, instances, defaults = {}, {}, {}, None
    for raw in data.decode("utf-8").splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("#"):
            raise ValueError("Topology must be the fully preprocessed grompp -pp output, with no unresolved directives")
        if line.startswith("["):
            if not re.fullmatch(r"\[\s*[a-zA-Z0-9_]+\s*\]", line):
                raise ValueError("Malformed GROMACS topology section")
            section = line.strip("[] ").lower()
            continue
        words = line.split()
        if section == "defaults":
            if defaults is not None or len(words) < 5 or int(words[0]) != 1:
                raise ValueError("Expected one explicit Lennard-Jones [defaults] section")
            defaults = {"combination_rule": int(words[1]), "fudge_lj": float(words[3]), "fudge_qq": float(words[4])}
        elif section == "atomtypes":
            # GROMACS atomtypes have optional bonded type/atomic number fields;
            # mass and charge immediately precede ptype, sigma and epsilon.
            ptype = len(words)-3
            if ptype < 3 or words[ptype] not in {"A", "S", "V"} or words[0] in atom_types:
                raise ValueError("Unsupported or duplicate GROMACS atom type definition")
            atom_types[words[0]] = {"mass_da": float(words[ptype-2]), "atomic_number": None}
            if ptype >= 4 and words[ptype-3].isdigit():
                atom_types[words[0]]["atomic_number"] = int(words[ptype-3])
        elif section == "moleculetype":
            molecule = words[0]
            if molecule in molecules:
                raise ValueError("Duplicate molecule type in prepared topology")
            molecules[molecule] = {"atoms": {}, "bonds": set()}
        elif section == "atoms":
            if molecule is None or len(words) < 7:
                raise ValueError("Malformed prepared [atoms] entry")
            index, atom_type, residue, residue_name, name = int(words[0]), words[1], int(words[2]), words[3], words[4]
            if index in molecules[molecule]["atoms"] or atom_type not in atom_types:
                raise ValueError("Duplicate atom or undefined atom type in prepared topology")
            charge = float(words[6])
            mass = float(words[7]) if len(words) >= 8 else atom_types[atom_type]["mass_da"]
            hybrid = len(words) > 8 and (words[8] != atom_type or (len(words)>9 and abs(float(words[9])-charge)>1e-8)
                                        or (len(words)>10 and abs(float(words[10])-mass)>1e-8))
            if not math.isfinite(charge) or not math.isfinite(mass):
                raise ValueError("Nonfinite prepared atomic parameters")
            molecules[molecule]["atoms"][index] = {"index": index, "atom_type": atom_type, "residue": residue,
                "residue_name": residue_name, "name": name, "partial_charge_e": charge, "mass_da": mass,
                "atomic_number": atom_types[atom_type]["atomic_number"], "hybrid": hybrid}
        elif section in {"bonds", "constraints"} and molecule is not None:
            molecules[molecule]["bonds"].add(tuple(sorted((int(words[0]), int(words[1])))))
        elif section == "molecules":
            if len(words) != 2 or words[0] not in molecules or int(words[1]) < 0:
                raise ValueError("Invalid prepared system molecule counts")
            instances[words[0]] = instances.get(words[0], 0)+int(words[1])
    if defaults is None or not molecules or not any(instances.values()):
        raise ValueError("Prepared topology lacks [defaults], molecule definitions, or actual [molecules] instances")
    for name, definition in molecules.items():
        definition["instances"] = instances.get(name, 0)
    return molecules, defaults, hashlib.sha256(data).hexdigest()


def validate_topology(manifest, topology_path, base_dir, expected_model=None):
    """Match the supplied fragment against every named prepared-topology residue.

    Bond order and 3D stereochemistry are not encoded by GROMACS bond records;
    this check verifies names/types/charges/masses/connectivity and explicit
    polymer neighbors. grompp must separately resolve all bonded parameters.
    """
    if expected_model is None:
        raise ValueError("Prepared topology validation requires the job's force_field and water_model")
    receipt = validate_parameter_manifest(manifest, base_dir, expected_model)
    molecules, defaults, topology_sha = _processed_topology(topology_path)
    for field, value in defaults.items():
        if not math.isclose(value, receipt["compatibility"][field], rel_tol=1e-6, abs_tol=1e-7):
            raise ValueError(f"Prepared topology {field} is incompatible with the supplied parameters")
    Chem = _rdkit()
    expected = {atom["name"]: atom for atom in receipt["atoms"]}
    expected_bonds = {tuple(sorted(bond["atoms"])) for bond in receipt["bonds"]}
    matches = []
    for molecule, topology in molecules.items():
        if not topology["instances"]:
            continue
        groups = {}
        for atom in topology["atoms"].values():
            if atom["residue_name"] == receipt["residue_name"]:
                groups.setdefault(atom["residue"], []).append(atom)
        for residue, atoms in groups.items():
            named = {atom["name"]: atom for atom in atoms}
            if len(named) != len(atoms) or set(named) != set(expected):
                raise ValueError(f"Prepared {molecule}:{residue} atom coverage does not exactly match {receipt['residue_name']}")
            ids = {atom["index"]: atom["name"] for atom in atoms}
            for name, actual in named.items():
                target = expected[name]
                if actual["hybrid"]:
                    raise ValueError("Custom residue hybrid A/B differences require explicit endpoint parameter manifests; automatic endpoint inference is unsupported")
                if actual["atom_type"] != target["atom_type"]:
                    raise ValueError(f"Prepared atom type mismatch for {molecule}:{residue}:{name}")
                if actual["atomic_number"] is None:
                    raise ValueError("Prepared custom atom types must declare atomic numbers so element coverage is verifiable")
                if actual["atomic_number"] != Chem.GetPeriodicTable().GetAtomicNumber(target["element"]):
                    raise ValueError(f"Prepared element mismatch for atom {name}")
                if not math.isclose(actual["partial_charge_e"], target["partial_charge_e"], abs_tol=1e-5, rel_tol=0):
                    raise ValueError(f"Prepared partial charge mismatch for atom {name}")
                if not math.isclose(actual["mass_da"], target["mass_da"], abs_tol=1e-3, rel_tol=0):
                    raise ValueError(f"Prepared mass mismatch for atom {name}; isotope/HMR changes need matching manifest masses")
            internal, external = set(), []
            for a, b in topology["bonds"]:
                if a in ids and b in ids:
                    internal.add(tuple(sorted((ids[a], ids[b]))))
                elif a in ids or b in ids:
                    anchor, other = (a, b) if a in ids else (b, a)
                    partner = topology["atoms"].get(other)
                    if partner is None:
                        raise ValueError("Prepared bond refers to an absent atom")
                    external.append((ids[anchor], partner["residue"]-residue, partner["name"], partner["atomic_number"]))
            if internal != expected_bonds:
                raise ValueError(f"Prepared internal bond connectivity differs for {molecule}:{residue}")
            expected_links = [(link["atom_name"], link["partner_residue_offset"], link["partner_atom_name"],
                               Chem.GetPeriodicTable().GetAtomicNumber(link["partner_element"])) for link in receipt["attachment_points"]]
            if sorted(external) != sorted(expected_links):
                raise ValueError(f"Prepared polymer linkage differs for {molecule}:{residue}")
            matches.append({"molecule_type": molecule, "residue_number": residue, "residue_name": receipt["residue_name"],
                            "molecule_instances": topology["instances"],
                            "atom_count": len(atoms), "internal_bonds": len(internal), "external_links": len(external),
                            "partial_charge_sum_e": sum(atom["partial_charge_e"] for atom in atoms)})
    if not matches:
        raise ValueError("The supplied custom residue is absent from the actual prepared topology")
    return {**receipt, "status": "prepared_topology_matched", "prepared_topology_sha256": topology_sha,
            "prepared_matches": matches, "job_model": dict(expected_model),
            "limitations": receipt["limitations"] + ["Prepared topology verifies connectivity and atomic parameters, not bond order or 3D stereochemistry; the source graph and preparation provenance remain necessary."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "validate-topology"):
        child = sub.add_parser(command)
        child.add_argument("--manifest", required=True)
        child.add_argument("--base-dir", required=True)
        child.add_argument("--force-field")
        child.add_argument("--water-model")
        child.add_argument("--output")
        if command == "validate-topology":
            child.add_argument("--topology", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(Path(args.manifest).read_text())
        if bool(args.force_field) != bool(args.water_model):
            raise ValueError("Supply both --force-field and --water-model")
        model = {"force_field": args.force_field, "water_model": args.water_model} if args.force_field else None
        result = (validate_topology(manifest, args.topology, args.base_dir, model) if args.command == "validate-topology"
                  else validate_parameter_manifest(manifest, args.base_dir, model))
        output = json.dumps(result, indent=2, allow_nan=False)+"\n"
        if args.output:
            Path(args.output).write_text(output)
        else:
            sys.stdout.write(output)
    except (ValueError, TypeError, KeyError, RuntimeError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
