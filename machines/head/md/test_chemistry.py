"""Explicit modified-nucleotide graphs and GROMACS topology contract fixtures.

Numerical charges below are deliberately toy parameters, never a usable force
field. Compatibility is labelled unit-test-only; tests prove identity checks,
not scientific calibration of a fluorinated or phosphorothioate nucleotide.
"""

import copy
import hashlib
import json

import pytest
from rdkit import Chem

from md import chemistry

MODEL = {"force_field": "unit-test-only", "water_model": "unit-test-water"}


def manifest_fixture(tmp_path, *, phosphorothioate=False, specify_phosphorus=True):
    phosphorus = "[P@]" if phosphorothioate and specify_phosphorus else "P"
    nonbridging = "[S-]" if phosphorothioate else "[O-]"
    # An incorporated 2'-fluoro nucleoside, with explicit preceding O3'/next P
    # attachment dummies, and optional stereospecific phosphorothioate linkage.
    mol = Chem.AddHs(Chem.MolFromSmiles(f"*{phosphorus}(=O)({nonbridging})OC[C@H]1O[C@@H](N2C=CC(=O)NC2=O)[C@H](O*)[C@@H]1F"))
    for i, atom in enumerate(mol.GetAtoms(), 1): atom.SetAtomMapNum(i)
    Chem.AssignStereochemistry(mol, cleanIt=True, force=True)
    atoms, links = [], []
    for atom in mol.GetAtoms():
        if atom.GetAtomicNum() == 0:
            neighbor = atom.GetNeighbors()[0]
            previous = neighbor.GetAtomicNum() == 15
            links.append({"dummy_map": atom.GetAtomMapNum(), "atom_map": neighbor.GetAtomMapNum(),
                          "bond_order": 1, "partner_residue_offset": -1 if previous else 1,
                          "partner_atom_name": "O3prev" if previous else "Pnext", "partner_element": "O" if previous else "P"})
            continue
        stereo = atom.GetProp("_CIPCode") if atom.HasProp("_CIPCode") else "none"
        if chemistry._resonance_equivalent_phosphate(mol, atom): stereo = "resonance_equivalent"
        atoms.append({"map": atom.GetAtomMapNum(), "name": f"{atom.GetSymbol()}{atom.GetAtomMapNum()}",
                      "element": atom.GetSymbol(), "formal_charge": atom.GetFormalCharge(), "stereochemistry": stereo,
                      "isotope": atom.GetIsotope(), "atom_type": f"toy_{atom.GetSymbol()}",
                      "partial_charge_e": float(atom.GetFormalCharge()), "mass_da": atom.GetMass()})
    parameter = tmp_path/"toy-parameters.itp"
    parameter.write_text("; Test parameter identity fixture only. Not a scientifically calibrated force field.\n")
    return {"schema": chemistry.SCHEMA, "residue_name": "XFU", "polymer": "RNA", "chemical_form": "incorporated", "engine": "gromacs",
            "net_charge_e": sum(atom["formal_charge"] for atom in atoms),
            "graph": {"incorporated_smiles": Chem.MolToSmiles(mol, allHsExplicit=True), "atoms": atoms},
            "attachment_points": links,
            "compatibility": {"force_fields": [MODEL["force_field"]], "water_models": [MODEL["water_model"]],
                              "combination_rule": 2, "fudge_lj": 0.5, "fudge_qq": 0.8333333333},
            "parameterization": {"method": "toy identity test", "software": "test fixture", "reference": "No physical calibration",
                                 "validation_evidence": "Only parser and contract tests; never use these toy charges for prediction"},
            "parameter_files": [{"path": parameter.name, "sha256": hashlib.sha256(parameter.read_bytes()).hexdigest()}]}


def topology_fixture(tmp_path, manifest):
    receipt = chemistry.validate_parameter_manifest(manifest, tmp_path, MODEL)
    atoms = receipt["atoms"]
    by_name = {atom["name"]: i+1 for i, atom in enumerate(atoms)}
    element_types = {atom["element"]: atom for atom in atoms}
    lines = ["[ defaults ]", "1 2 yes 0.5 0.8333333333", "[ atomtypes ]"]
    for element, atom in element_types.items():
        lines.append(f"{atom['atom_type']} {Chem.GetPeriodicTable().GetAtomicNumber(element)} {atom['mass_da']:.6f} 0 A 0.3 0.1")
    lines += ["[ moleculetype ]", "OLIGO 3", "[ atoms ]"]
    for index, atom in enumerate(atoms, 1):
        lines.append(f"{index} {atom['atom_type']} 2 XFU {atom['name']} {index} {atom['partial_charge_e']:.8f} {atom['mass_da']:.6f}")
    external = []
    for link in receipt["attachment_points"]:
        index = len(atoms)+len(external)+1
        atom = element_types[link["partner_element"]]
        lines.append(f"{index} {atom['atom_type']} {2+link['partner_residue_offset']} CTX {link['partner_atom_name']} {index} 0 {atom['mass_da']:.6f}")
        external.append(f"{by_name[link['atom_name']]} {index} 1")
    lines += ["[ bonds ]"]+[f"{by_name[bond['atoms'][0]]} {by_name[bond['atoms'][1]]} 1" for bond in receipt["bonds"]]+external
    lines += ["[ system ]", "Scientific identity contract fixture", "[ molecules ]", "OLIGO 1"]
    path = tmp_path/"processed.top"
    path.write_text("\n".join(lines)+"\n")
    return path


def test_fluorinated_incorporated_rna_graph_keeps_all_atoms_stereo_and_links(tmp_path):
    manifest = manifest_fixture(tmp_path)
    before = copy.deepcopy(manifest)
    receipt = chemistry.validate_parameter_manifest(manifest, tmp_path, MODEL)
    assert manifest == before
    assert receipt["status"] == "parameter_contract_validated"
    assert receipt["net_charge_e"] == receipt["partial_charge_sum_e"] == -1
    assert len(receipt["atoms"]) == len(manifest["graph"]["atoms"])
    assert any(atom["element"] == "F" for atom in receipt["atoms"])
    assert len(receipt["attachment_points"]) == 2
    assert len(receipt["resonance_equivalent_phosphate_maps"]) == 1
    assert sum(atom["stereochemistry"] in {"R", "S"} for atom in receipt["atoms"]) == 4
    assert "not transferability" in " ".join(receipt["limitations"])


def test_phosphorothioate_requires_its_actual_phosphorus_stereochemistry(tmp_path):
    unspecified = manifest_fixture(tmp_path, phosphorothioate=True, specify_phosphorus=False)
    with pytest.raises(ValueError, match="stereogenic"): chemistry.validate_parameter_manifest(unspecified, tmp_path, MODEL)
    specified = manifest_fixture(tmp_path, phosphorothioate=True)
    receipt = chemistry.validate_parameter_manifest(specified, tmp_path, MODEL)
    phosphorus = next(atom for atom in receipt["atoms"] if atom["element"] == "P")
    assert phosphorus["stereochemistry"] in {"R", "S"}
    assert not receipt["resonance_equivalent_phosphate_maps"]


def test_opposite_stereoisomer_is_not_silently_canonicalized(tmp_path):
    manifest = manifest_fixture(tmp_path)
    options = Chem.SmilesParserParams(); options.removeHs = False
    mol = Chem.MolFromSmiles(manifest["graph"]["incorporated_smiles"], options)
    atom = next(atom for atom in mol.GetAtoms() if atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED)
    atom.InvertChirality()
    manifest["graph"]["incorporated_smiles"] = Chem.MolToSmiles(mol, allHsExplicit=True)
    with pytest.raises(ValueError, match="Stereochemistry disagrees"): chemistry.validate_parameter_manifest(manifest, tmp_path, MODEL)


@pytest.mark.parametrize("change", ["protected", "partial_charge", "formal_charge", "missing_h", "duplicate_map", "missing_link", "wrong_anchor", "wrong_ff", "wrong_water", "bad_sha", "traversal"])
def test_incomplete_or_mismatched_parameter_manifests_are_rejected(tmp_path, change):
    manifest = manifest_fixture(tmp_path)
    expected = dict(MODEL)
    if change == "protected": manifest["chemical_form"] = "phosphoramidite-reagent"
    elif change == "partial_charge": manifest["graph"]["atoms"][0]["partial_charge_e"] += 0.1
    elif change == "formal_charge": manifest["net_charge_e"] = 0
    elif change == "missing_h": manifest["graph"]["atoms"].pop()
    elif change == "duplicate_map": manifest["graph"]["atoms"][1]["map"] = manifest["graph"]["atoms"][0]["map"]
    elif change == "missing_link": manifest["attachment_points"].pop()
    elif change == "wrong_anchor": manifest["attachment_points"][0]["atom_map"] = 999
    elif change == "wrong_ff": expected["force_field"] = "other"
    elif change == "wrong_water": expected["water_model"] = "opc"
    elif change == "bad_sha": manifest["parameter_files"][0]["sha256"] = "0"*64
    else: manifest["parameter_files"][0]["path"] = "../outside.itp"
    with pytest.raises(ValueError): chemistry.validate_parameter_manifest(manifest, tmp_path, expected)


def test_prepared_topology_matches_actual_atom_coverage_and_polymer_connections(tmp_path):
    manifest = manifest_fixture(tmp_path)
    path = topology_fixture(tmp_path, manifest)
    receipt = chemistry.validate_topology(manifest, path, tmp_path, MODEL)
    assert receipt["status"] == "prepared_topology_matched"
    assert receipt["prepared_topology_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert receipt["prepared_matches"] == [{"molecule_type": "OLIGO", "molecule_instances": 1, "residue_number": 2,
        "residue_name": "XFU", "atom_count": len(manifest["graph"]["atoms"]), "internal_bonds": len(receipt["bonds"]),
        "external_links": 2, "partial_charge_sum_e": -1.0}]


@pytest.mark.parametrize("change", ["charge", "mass", "atom_type", "element", "missing_atom", "missing_bond", "missing_link", "wrong_partner", "hybrid", "scaling", "unprocessed", "unused_residue"])
def test_prepared_topology_cannot_substitute_or_drop_custom_chemistry(tmp_path, change):
    manifest = manifest_fixture(tmp_path)
    path = topology_fixture(tmp_path, manifest)
    lines = path.read_text().splitlines()
    first = lines.index("[ atoms ]")+1
    words = lines[first].split()
    if change == "charge": words[6] = "0.1"
    elif change == "mass": words[7] = "99"
    elif change == "atom_type": words[1] = "toy_F"
    elif change == "element": lines[lines.index("[ atomtypes ]")+1] = lines[lines.index("[ atomtypes ]")+1].replace(" 15 ", " 8 ")
    elif change == "hybrid": words += [words[1], "0.2", words[7]]
    elif change == "missing_atom": words[4] = "CANONICALIZED"
    elif change == "missing_bond": lines.pop(lines.index("[ bonds ]")+1)
    elif change == "missing_link": lines.pop(lines.index("[ system ]")-1)
    elif change == "wrong_partner": lines[first+len(manifest["graph"]["atoms"])]=lines[first+len(manifest["graph"]["atoms"])].replace("O3prev", "WRONG")
    elif change == "scaling": lines[1] = "1 2 yes 0.5 1.0"
    elif change == "unprocessed": lines.append('#include "unresolved.itp"')
    elif change == "unused_residue": lines[-1] = "OLIGO 0"
    lines[first] = " ".join(words)
    path.write_text("\n".join(lines)+"\n")
    with pytest.raises(ValueError): chemistry.validate_topology(manifest, path, tmp_path, MODEL)


def test_parameter_change_after_registration_is_detected_during_topology_validation(tmp_path):
    manifest = manifest_fixture(tmp_path)
    path = topology_fixture(tmp_path, manifest)
    (tmp_path/manifest["parameter_files"][0]["path"]).write_text("changed after admission")
    with pytest.raises(ValueError, match="SHA256 mismatch"): chemistry.validate_topology(manifest, path, tmp_path, MODEL)


def test_chemistry_cli_writes_auditable_receipt(tmp_path):
    manifest = manifest_fixture(tmp_path)
    topology = topology_fixture(tmp_path, manifest)
    source, target = tmp_path/"manifest.json", tmp_path/"receipt.json"
    source.write_text(json.dumps(manifest))
    assert chemistry.main(["validate-topology", "--manifest", str(source), "--topology", str(topology),
                           "--base-dir", str(tmp_path), "--force-field", MODEL["force_field"], "--water-model", MODEL["water_model"], "--output", str(target)]) == 0
    assert json.loads(target.read_text())["status"] == "prepared_topology_matched"
