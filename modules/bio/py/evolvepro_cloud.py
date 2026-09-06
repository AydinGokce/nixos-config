#!/usr/bin/env python3
"""Validate and run a cloud EVOLVEpro-style embedding / variant-ranking job.

Uses the same self-contained implementation as bio-evolvepro, with separate
PLM and regression interpreters. No GPU packages are needed for --validate-only.
"""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys


def read_variants(path):
    variants = {}
    name = None
    with Path(path).open() as source:
        for number, raw in enumerate(source, 1):
            line = raw.strip()
            if not line:
                continue
            if line.startswith(">"):
                parts = line[1:].split()
                if not parts:
                    raise ValueError(f"FASTA line {number}: missing variant identifier")
                name = parts[0]
                if name in variants:
                    raise ValueError(f"duplicate FASTA variant: {name}")
                variants[name] = ""
            else:
                if name is None:
                    raise ValueError(f"FASTA line {number}: sequence before first header")
                sequence = line.upper()
                if set(sequence) - set("ACDEFGHIKLMNPQRSTVWYBXZUO"):
                    raise ValueError(f"FASTA variant {name}: expected amino-acid letters")
                variants[name] += sequence
    if not variants:
        raise ValueError("FASTA contains no variants")
    for name, sequence in variants.items():
        if not sequence:
            raise ValueError(f"FASTA variant {name}: empty sequence")
    return variants


def read_labels(path, variants):
    measured = {}
    if path is None:
        return measured
    with Path(path).open(newline="") as source:
        rows = csv.DictReader(source)
        if not rows.fieldnames or not {"variant", "activity"} <= set(rows.fieldnames):
            raise ValueError("labels CSV must have columns variant,activity")
        for row in rows:
            name = row["variant"]
            if name not in variants:
                raise ValueError(f"measured variant absent from FASTA: {name}")
            if name in measured:
                raise ValueError(f"duplicate measured variant: {name}")
            try:
                activity = float(row["activity"])
            except (ValueError, TypeError):
                raise ValueError(f"variant {name}: activity must be a finite number") from None
            if not math.isfinite(activity):
                raise ValueError(f"variant {name}: activity must be a finite number")
            measured[name] = activity
    return measured


def positive_int(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--input", required=True, type=Path)
    result.add_argument("--labels", type=Path)
    result.add_argument("--out", type=Path)
    result.add_argument("--embedding-model", default="esm2_t33_650M_UR50D")
    result.add_argument("--num", type=positive_int, default=12)
    result.add_argument("--sub", choices=["embed", "rank"], default="rank")
    result.add_argument("--regressor", choices=["rf", "xgb"], default="rf")
    result.add_argument("--seed", type=int, default=0)
    result.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    result.add_argument("--plm-python")
    result.add_argument("--core-python")
    result.add_argument("--validate-only", action="store_true")
    return result


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(args):
    variants = read_variants(args.input)
    measured = read_labels(args.labels, variants)
    if args.sub == "embed" and args.labels is not None:
        raise ValueError("--labels applies to --sub rank; omit it for --sub embed")
    if args.seed < 0 or args.seed >= 2**32:
        raise ValueError("--seed must be between 0 and 4294967295")
    if args.sub == "rank" and len(measured) == len(variants):
        raise ValueError("all FASTA variants are measured; add unmeasured candidates to rank")
    if args.validate_only:
        print(f"bio-evolvepro: valid input ({len(variants)} variants, {len(measured)} measurements)")
        return
    if args.out is None or args.plm_python is None:
        raise ValueError("--out and --plm-python are required to run a job")
    if args.sub == "rank" and args.core_python is None:
        raise ValueError("--core-python is required for ranking")

    args.out.mkdir(parents=True, exist_ok=True)
    # Normalize case/line wrapping once; keep every identifier exactly as given.
    normalized_fasta = args.out / "variants.fasta"
    normalized_fasta.write_text("".join(f">{name}\n{sequence}\n" for name, sequence in variants.items()))
    normalized_labels = args.out / "labels.csv"
    if args.labels is not None:
        with normalized_labels.open("w", newline="") as target:
            rows = csv.writer(target)
            rows.writerow(["variant", "activity"])
            rows.writerows(measured.items())
    metadata = {
        "implementation": "bio-evolvepro (EVOLVEpro-style ESM-2 / top-layer regression)",
        "subcommand": args.sub,
        "embedding_model": args.embedding_model,
        "device": args.device,
        "regressor": args.regressor if measured else None,
        "seed": args.seed,
        "requested_variants": args.num,
        "variant_count": len(variants),
        "measurement_count": len(measured),
        "input_sha256": sha256(args.input),
        "labels_sha256": sha256(args.labels) if args.labels is not None else None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
    }
    manifest = args.out / "run.json"
    manifest.write_text(json.dumps(metadata, indent=2) + "\n")
    cli = Path(__file__).with_name("evolvepro_cli.py")
    embeddings = args.out / "embeddings.csv"
    subprocess.run([
        args.plm_python, str(cli), "embed", "--input", str(normalized_fasta),
        "--out", str(embeddings), "--model", args.embedding_model, "--device", args.device,
    ], check=True)
    if args.sub == "rank":
        selected = args.out / "next_round.csv"
        command = [
            args.core_python, str(cli), "evolve", "--embeddings", str(embeddings),
            "--out", str(selected), "--full", str(args.out / "ranking.csv"),
            "--n", str(args.num), "--model", args.regressor, "--seed", str(args.seed),
        ]
        if measured:
            command.extend(["--labels", str(normalized_labels)])
        subprocess.run(command, check=True)
        with selected.open(newline="") as source:
            selected_names = [row["variant"] for row in csv.DictReader(source)]
        (args.out / "selected.fasta").write_text("".join(
            f">{name}\n{variants[name]}\n" for name in selected_names
        ))
        metadata["selected_variants"] = selected_names
    metadata["status"] = "complete"
    metadata["finished_at"] = datetime.now(timezone.utc).isoformat()
    manifest.write_text(json.dumps(metadata, indent=2) + "\n")


def main():
    args = parser().parse_args()
    try:
        run(args)
    except (OSError, ValueError) as error:
        sys.exit(f"bio-evolvepro: {error}")
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)


if __name__ == "__main__":
    main()
