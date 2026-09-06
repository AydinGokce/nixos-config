#!/usr/bin/env python3
"""Retain experimental references and score single-protein prediction panels.

Requires NumPy and Biopython in the analysis environment, never on the head.
Reports C-alpha metrics; it does not attest all-atom, assembly or ligand accuracy.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import urllib.request

import numpy as np
from Bio.PDB import MMCIFIO, MMCIFParser, PDBParser
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from Bio.PDB.PDBExceptions import PDBException, PDBConstructionException
from Bio.SeqUtils import seq1


PANEL = ("1ubq", "1csp", "2lzm", "1ten", "1ake", "1pgb")
AMINO = set("ACDEFGHIKLMNPQRSTVWY")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def structure(path, single_model=False):
    path = Path(path)
    parser = (MMCIFParser(QUIET=True, auth_chains=False, auth_residues=False)
              if path.suffix.lower() == ".cif" else PDBParser(QUIET=True, PERMISSIVE=False))
    source = str(path)
    if single_model and path.suffix.lower() == ".cif":
        data = MMCIF2Dict(str(path))
        if "_atom_site.occupancy" not in data:
            # Native OF3/Biotite predictions omit this optional CIF column.
            # CA scoring does not use occupancy; supply it only to the parser
            # and only when there is no alternate-conformer selection to infer.
            require(all(value in {".", "?", ""} for value in data["_atom_site.label_alt_id"]),
                    "Missing occupancy with alternate conformers is ambiguous")
            data["_atom_site.occupancy"] = ["1.0"] * len(data["_atom_site.id"])
            source = io.StringIO()
            writer = MMCIFIO()
            writer.set_dict(data)
            writer.save(source)
            source.seek(0)
    parsed = parser.get_structure("structure", source)
    require(not single_model or len(parsed) == 1,
            "Multi-model prediction files must be separated and every sample scored explicitly")
    return parsed[0]


def residues(chain):
    return [r for r in chain if seq1(r.resname) in AMINO and r.id[0] == " "]


def reference(cif, chain_id):
    data = MMCIF2Dict(str(cif))
    entities = dict(zip(data["_struct_asym.id"], data["_struct_asym.entity_id"]))
    sequences = dict(zip(data["_entity_poly.entity_id"], data["_entity_poly.pdbx_seq_one_letter_code_can"]))
    sequence = "".join(sequences[entities[chain_id]].split()).upper()
    require(sequence and set(sequence) <= AMINO, "Reference has nonstandard or ambiguous amino acids")
    observed = residues(structure(cif)[chain_id])
    coords, positions = [], []
    for residue in observed:
        position = residue.id[1] - 1  # mmCIF label_seq_id, not author numbering.
        require(0 <= position < len(sequence), "Invalid reference label_seq_id")
        require(seq1(residue.resname) == sequence[position], "Reference sequence/coordinate mismatch")
        if "CA" in residue:
            positions.append(position)
            coords.append(residue["CA"].coord.tolist())
    require(len(positions) == len(set(positions)), "Duplicate reference residue positions")
    require(len(positions) >= 3 and np.isfinite(coords).all(), "Insufficient or invalid reference coordinates")
    return sequence, positions, coords


def retain_case(root, pdb, chain="A"):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    require(len(pdb) == 4 and pdb.isalnum(), "Expected a four-character PDB accession")
    cif = root / f"{pdb.lower()}.cif"
    url = f"https://files.rcsb.org/download/{pdb.upper()}.cif"
    if not cif.exists():
        with urllib.request.urlopen(url, timeout=60) as response:
            raw = response.read(32 * 1024 * 1024 + 1)
        require(len(raw) <= 32 * 1024 * 1024, "Reference exceeds the bounded download size")
        temp = cif.with_suffix(".part")
        temp.write_bytes(raw)
        temp.replace(cif)
    sequence, positions, coords = reference(cif, chain)
    name = f"{pdb.lower()}_{chain}"
    fasta = root / f"{name}.fasta"
    fasta.write_text(f">{name}\n{sequence}\n")
    value = dict(version=1, name=name, source=url, reference_file=cif.name,
                 reference_sha256=sha(cif), label_chain=chain, sequence=sequence,
                 fasta_file=fasta.name, fasta_sha256=sha(fasta),
                 observed_positions=positions, reference_ca=coords,
                 observed_fraction=len(positions) / len(sequence))
    write(root / f"{name}.json", value)
    return value


def prediction_ca(path, sequence, chain_id=None):
    model = structure(path, single_model=True)
    chains = [c for c in model if residues(c)]
    if chain_id is None:
        require(len(chains) == 1, "Prediction requires an explicit chain when multiple protein chains exist")
        chain = chains[0]
    else:
        chain = model[chain_id]
    selected = residues(chain)
    require("".join(seq1(r.resname) for r in selected) == sequence,
            "Prediction residue sequence/order must match the complete submitted sequence")
    require(all("CA" in r for r in selected), "Prediction is missing C-alpha atoms")
    result = np.asarray([r["CA"].coord for r in selected], dtype=np.float64)
    require(result.shape == (len(sequence), 3) and np.isfinite(result).all(), "Invalid prediction coordinates")
    return result


def ca_metrics(reference_ca, predicted_ca):
    ref = np.asarray(reference_ca, dtype=np.float64)
    pred = np.asarray(predicted_ca, dtype=np.float64)
    require(ref.shape == pred.shape and ref.ndim == 2 and ref.shape[1] == 3 and len(ref) >= 3,
            "Corresponding coordinate arrays required")
    require(np.isfinite(ref).all() and np.isfinite(pred).all(), "Nonfinite coordinates")
    x, y = pred - pred.mean(axis=0), ref - ref.mean(axis=0)
    u, _, vt = np.linalg.svd(x.T @ y)
    correction = np.eye(3)
    correction[-1, -1] = np.linalg.det(u @ vt)
    fitted = x @ (u @ correction @ vt)
    rmsd = float(np.sqrt(np.mean(np.sum((fitted - y) ** 2, axis=1))))
    ref_dist = np.linalg.norm(ref[:, None] - ref[None, :], axis=-1)
    pred_dist = np.linalg.norm(pred[:, None] - pred[None, :], axis=-1)
    contacts = (ref_dist < 15.0) & ~np.eye(len(ref), dtype=bool)
    counts = contacts.sum(axis=1)
    require(np.all(counts > 0), "Reference contains a residue without C-alpha contacts within 15 A")
    errors = np.abs(ref_dist - pred_dist)
    preserved = sum((errors < threshold) & contacts for threshold in (0.5, 1.0, 2.0, 4.0)) / 4.0
    per_residue = preserved.sum(axis=1) / counts
    return dict(ca_rmsd_angstrom=rmsd, lddt_ca_mean=float(per_residue.mean()),
                lddt_ca_contact_weighted=float(preserved.sum() / contacts.sum()),
                lddt_ca_per_residue=per_residue.tolist(),
                reference_contact_pairs=int(contacts.sum() // 2), residues=len(ref))


def score(case_path, predictions, chain=None):
    case_path = Path(case_path)
    case = json.loads(case_path.read_text())
    require(case["version"] == 1, "Unsupported reference case")
    cif = case_path.parent / case["reference_file"]
    require(sha(cif) == case["reference_sha256"], "Reference file hash mismatch")
    seq, positions, coordinates = reference(cif, case["label_chain"])
    require(seq == case["sequence"] and positions == case["observed_positions"]
            and coordinates == case["reference_ca"], "Reference mapping changed")
    records = []
    for path in predictions:
        predicted = prediction_ca(path, seq, chain)
        records.append(dict(path=str(Path(path).resolve()), sha256=sha(path),
                            **ca_metrics(coordinates, predicted[positions])))
    require(records, "No predictions supplied")
    aggregate = {key: dict(mean=float(np.mean([r[key] for r in records])),
                           median=float(np.median([r[key] for r in records])),
                           minimum=float(min(r[key] for r in records)),
                           maximum=float(max(r[key] for r in records)))
                 for key in ("ca_rmsd_angstrom", "lddt_ca_mean")}
    return dict(version=1, scorer_sha256=sha(__file__), case=case["name"], case_sha256=sha(case_path),
                reference_sha256=case["reference_sha256"], observed_fraction=len(positions) / len(seq),
                parser_policy="Missing prediction occupancy is 1.0 in memory only when alternate conformers are absent; source bytes are unchanged",
                metrics_scope="C-alpha of experimentally observed residues; all supplied samples",
                samples=records, aggregate=aggregate, scientific_parity="not_established")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("references")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--pdb", nargs="+", default=list(PANEL))
    p.add_argument("--chain", default="A", help="mmCIF label_asym_id")
    p = commands.add_parser("score")
    p.add_argument("--case", type=Path, required=True)
    p.add_argument("--predictions", type=Path, nargs="+", required=True)
    p.add_argument("--chain")
    p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "references":
        values = [retain_case(args.out, pdb, args.chain) for pdb in args.pdb]
        print(json.dumps([dict(name=v["name"], length=len(v["sequence"]),
                              observed_fraction=v["observed_fraction"]) for v in values], indent=2))
    else:
        result = score(args.case, args.predictions, args.chain)
        write(args.out, result)
        print(json.dumps(result["aggregate"], indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, PDBException, PDBConstructionException) as exc:
        print(f"quality: {exc}", file=sys.stderr)
        sys.exit(1)
