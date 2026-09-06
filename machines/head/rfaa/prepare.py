#!/usr/bin/env python3
"""Prepare RFAA protein inputs with the pinned upstream MSA/template search.

The search settings follow RoseTTAFold-All-Atom d69ab3a's make_msa.sh.
Commands use argument lists, checked exit codes, and atomic output promotion;
failed searches never degrade into an apparently successful single-sequence run.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from databases import DATASETS, VERSION, validate


def fasta_sequence(path):
    lines = path.read_text().splitlines()
    headers = [i for i, row in enumerate(lines) if row.startswith(">")]
    if len(headers) != 1 or any(row.strip() for row in lines[:headers[0]]):
        raise ValueError("RFAA cloud input must contain exactly one FASTA record")
    sequence = "".join("".join(row.split()) for row in lines[headers[0] + 1:]).upper()
    if not sequence or re.search(r"[^ACDEFGHIKLMNPQRSTVWYX]", sequence):
        raise ValueError("RFAA requires a nonempty amino-acid sequence (20 standard residues or X)")
    return sequence


def nonempty(path):
    return path.is_file() and path.stat().st_size > 0


def a3m_count(path):
    if not nonempty(path):
        raise ValueError(f"Empty MSA: {path}")
    with path.open() as handle:
        count = sum(row.startswith(">") for row in handle)
    if not count:
        raise ValueError(f"Invalid A3M: {path}")
    return count


def run(argv, logfile, cwd=None, stdout=None):
    print("rfaa-prepare:", " ".join(map(str, argv)), flush=True)
    with logfile.open("ab") as log:
        subprocess.run(list(map(str, argv)), cwd=cwd, stdout=stdout or log,
                       stderr=log, check=True)


def generated(argv, output, flag, logfile):
    """Only reuse outputs whose producing process previously finished."""
    if nonempty(output):
        a3m_count(output)
        return
    temporary = Path(f"{output}.part")
    run([*argv, flag, temporary], logfile)
    a3m_count(temporary)
    temporary.replace(output)


def msa_search(query, directory, root, cpu, mem):
    output = directory / "t000_.msa0.a3m"
    if nonempty(output):
        a3m_count(output)
        return output
    work = directory / "hhblits"
    work.mkdir(exist_ok=True)
    logfile = directory / "log" / "hhblits.log"
    common = ["hhblits", "-o", "/dev/null", "-mact", "0.35", "-maxfilt", "100000000",
              "-neffmax", "20", "-cov", "25", "-cpu", cpu, "-nodiff", "-realign_max",
              "100000000", "-maxseq", "1000000", "-maxmem", mem, "-n", "4", "-v", "0"]
    searches = [("uniref30", value) for value in ("1e-10", "1e-6", "1e-3")]
    searches.append(("bfd", "1e-3"))
    previous = query
    for name, evalue in searches:
        dataset = DATASETS[name]
        prefix = root / dataset["directory"] / dataset["prefix"]
        raw = work / f"{name}.{evalue}.a3m"
        generated([*common, "-d", prefix, "-i", previous, "-e", evalue], raw, "-oa3m", logfile)
        filtered = {}
        for coverage in (75, 50):
            candidate = work / f"{name}.{evalue}.id90cov{coverage}.a3m"
            generated(["hhfilter", "-maxseq", "100000", "-id", "90", "-cov", coverage,
                       "-i", raw], candidate, "-o", logfile)
            filtered[coverage] = candidate
        previous = filtered[50]
        chosen = filtered[75] if a3m_count(filtered[75]) > 2000 else (
            filtered[50] if a3m_count(filtered[50]) > 4000 else None)
        if chosen:
            previous = chosen
            break
    temporary = Path(f"{output}.part")
    shutil.copyfile(previous, temporary)
    temporary.replace(output)
    return output


def secondary_structure(msa, directory):
    output = directory / "t000_.ss2"
    if nonempty(output):
        return output
    work = directory / "psipred"
    work.mkdir(exist_ok=True)
    logfile = directory / "log" / "psipred.log"
    psidata = Path(os.environ["PSIPRED_DATA"])
    csdata = Path(os.environ["CSBLAST_DATA"])
    # Local filenames avoid legacy makemat's path/name limitations.
    shutil.copyfile(msa, work / "input.a3m")
    run(["csbuild", "-i", "input.a3m", "-I", "a3m", "-D", csdata / "K4000.crf",
         "-o", "query.chk", "-O", "chk"], logfile, cwd=work)
    with msa.open() as handle:
        first = handle.readline()
        sequence = []
        for line in handle:
            if line.startswith(">"):
                break
            sequence.append(line.strip())
    (work / "query.fasta").write_text(first + "".join(sequence) + "\n")
    (work / "query.pn").write_text("query.chk\n")
    (work / "query.sn").write_text("query.fasta\n")
    run(["makemat", "-P", "query"], logfile, cwd=work)
    with (work / "query.ss").open("wb") as stdout:
        run(["psipred", "query.mtx", psidata / "weights.dat", psidata / "weights.dat2",
             psidata / "weights.dat3"], logfile, cwd=work, stdout=stdout)
    with (work / "query.horiz").open("wb") as stdout:
        run(["psipass2", psidata / "weights_p2.dat", "1", "1.0", "1.0", "query.ss2",
             "query.ss"], logfile, cwd=work, stdout=stdout)
    horizontal = (work / "query.horiz").read_text().splitlines()
    pred = "".join(row.split()[1] for row in horizontal if row.startswith("Pred:"))
    conf = "".join(row.split()[1] for row in horizontal if row.startswith("Conf:"))
    if not pred or len(pred) != len(conf) or len(pred) != len("".join(sequence)):
        raise ValueError("PSIPRED produced an incomplete secondary-structure prediction")
    temporary = Path(f"{output}.part")
    temporary.write_text(f">ss_pred\n{pred}\n>ss_conf\n{conf}\n")
    temporary.replace(output)
    return output


def template_search(msa, secondary, directory, root, cpu, mem):
    hhr = directory / "t000_.hhr"
    atab = directory / "t000_.atab"
    if nonempty(hhr) and atab.exists() and (directory / ".templates-complete").exists():
        return
    combined = directory / "t000_.msa0.ss2.a3m"
    with combined.open("wb") as handle:
        for source in (secondary, msa):
            with source.open("rb") as part:
                shutil.copyfileobj(part, handle)
    dataset = DATASETS["pdb100"]
    database = root / dataset["directory"] / dataset["prefix"]
    hhr_part, atab_part = Path(f"{hhr}.part"), Path(f"{atab}.part")
    run(["hhsearch", "-b", "50", "-B", "500", "-z", "50", "-Z", "500", "-mact",
         "0.05", "-cpu", cpu, "-maxmem", mem, "-aliw", "100000", "-e", "100", "-p",
         "5.0", "-d", database, "-i", combined, "-o", hhr_part, "-atab", atab_part,
         "-v", "0"], directory / "log" / "hhsearch.log")
    # No significant template hits is a legitimate search result; HHR still
    # contains its search summary. An empty ATAB then means zero templates.
    if not nonempty(hhr_part) or not atab_part.exists():
        raise ValueError("HHsearch did not produce its template search outputs")
    hhr_part.replace(hhr)
    atab_part.replace(atab)
    (directory / ".templates-complete").touch()


def prepare(fasta, directory, mode, root, cpu, mem):
    sequence = fasta_sequence(fasta)
    request = {"version": VERSION, "mode": mode, "sequence_sha256":
               hashlib.sha256(sequence.encode()).hexdigest()}
    directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve()
    manifest = directory / ".prepare-request.json"
    if manifest.exists():
        if json.loads(manifest.read_text()) != request:
            raise ValueError("Existing preparation has a different query or mode; use a fresh output directory")
    elif list(directory.glob("t000_*")):
        raise ValueError("Existing MSA/template files have no provenance; use a fresh output directory")
    else:
        manifest.write_text(json.dumps(request, indent=2) + "\n")
    query = directory / "query.fasta"
    query.write_text(f">query\n{sequence}\n")
    if mode == "single-seq":
        shutil.copyfile(query, directory / "t000_.msa0.a3m")
        (directory / "t000_.hhr").write_text("")
        (directory / "t000_.atab").write_text("")
        return
    root = root.resolve()
    validate(root, list(DATASETS))
    (directory / "log").mkdir(exist_ok=True)
    msa = msa_search(query, directory, root, cpu, mem)
    secondary = secondary_structure(msa, directory)
    template_search(msa, secondary, directory, root, cpu, mem)
    result = dict(request, msa_sequences=a3m_count(msa), templates_searched=True,
                  signalp=False, database_root=str(root), cpu=cpu, memory_gib=mem)
    (directory / "preparation.json").write_text(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fasta", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--mode", choices=["full", "single-seq"], default="full")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("RFAA_DB_DIR", "/mnt/bio-databases/rfaa")))
    parser.add_argument("--cpu", type=int, default=4)
    parser.add_argument("--mem", type=int, default=64)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    fasta_sequence(args.fasta)
    if args.validate_only:
        return
    if args.out is None or args.cpu < 1 or args.mem < 1:
        parser.error("--out is required, and --cpu/--mem must be positive")
    prepare(args.fasta, args.out, args.mode, args.root, args.cpu, args.mem)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.CalledProcessError) as error:
        print(f"rfaa-prepare: ERROR: {error}", file=sys.stderr)
        sys.exit(1)
