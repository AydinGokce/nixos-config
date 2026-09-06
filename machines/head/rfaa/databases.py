#!/usr/bin/env python3
"""Provision the dated databases required by the pinned RFAA pipeline.

Downloads are explicit (never triggered by inference), resumable, serial, and
promoted from staging only after archive and FFindex validation. BFD's published
MD5 verifies its upstream mirror; HTTPS, fixed byte lengths, gzip CRCs and local
SHA256 receipts cover the two upstream archives without published checksums.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

VERSION = "rfaa-2020_06-2021Mar03-v1"
DATASETS = {
    "uniref30": {
        "directory": "UniRef30_2020_06",
        "prefix": "UniRef30_2020_06",
        "archive": "UniRef30_2020_06_hhsuite.tar.gz",
        "url": "https://wwwuser.gwdguser.de/~compbiol/uniclust/2020_06/UniRef30_2020_06_hhsuite.tar.gz",
        "bytes": 49899998613,
        "space_gib": 300,
        "components": ["a3m", "hhm", "cs219"],
    },
    "bfd": {
        "directory": "bfd",
        "prefix": "bfd_metaclust_clu_complete_id30_c90_final_seq.sorted_opt",
        "archive": "bfd_metaclust_clu_complete_id30_c90_final_seq.sorted_opt.tar.gz",
        "url": "https://storage.googleapis.com/alphafold-databases/casp14_versions/bfd_metaclust_clu_complete_id30_c90_final_seq.sorted_opt.tar.gz",
        "bytes": 291649557441,
        "md5": "6a634dc6eb105c2e9b4cba7bbae93412",
        "space_gib": 2200,
        "components": ["a3m", "hhm", "cs219"],
    },
    "pdb100": {
        "directory": "pdb100_2021Mar03",
        "prefix": "pdb100_2021Mar03",
        "archive": "pdb100_2021Mar03.tar.gz",
        "url": "https://files.ipd.uw.edu/pub/RoseTTAFold/pdb100_2021Mar03.tar.gz",
        "bytes": 87225655731,
        "space_gib": 600,
        "components": ["a3m", "hhm", "cs219", "pdb"],
    },
}


def fail(message):
    raise RuntimeError(message)


def log(message):
    print(f"rfaa-databases: {message}", flush=True)


def index_edges(path):
    """Read the first and last entry without loading a multi-GB index into RAM."""
    with path.open("rb") as handle:
        first = handle.readline()
        handle.seek(max(0, path.stat().st_size - 65536))
        rows = handle.read().splitlines()
        if not rows:
            fail(f"Empty FFindex: {path}")
        last = rows[-1]
    for row in (first, last):
        parts = row.split()
        if len(parts) != 3:
            fail(f"Malformed FFindex row in {path}")
        try:
            offset, length = map(int, parts[1:])
        except ValueError:
            fail(f"Invalid FFindex offset/length in {path}")
        if offset < 0 or length < 1:
            fail(f"Invalid FFindex range in {path}")
        yield offset, length


def validate_directory(directory, dataset):
    result = {}
    for component in dataset["components"]:
        stem = directory / f'{dataset["prefix"]}_{component}'
        data = Path(f"{stem}.ffdata")
        index = Path(f"{stem}.ffindex")
        if not data.is_file() or not index.is_file():
            fail(f"Missing database pair: {stem}.ffdata/.ffindex")
        if data.stat().st_size < 1024 or index.stat().st_size < 16:
            fail(f"Empty or placeholder database: {stem}")
        for offset, length in index_edges(index):
            if offset + length > data.stat().st_size:
                fail(f"Truncated data file for {index}")
        with data.open("rb") as handle:
            if not handle.read(4096).strip(b"\0"):
                fail(f"Zero-filled database: {data}; validate on the downloading node")
        result[data.name] = data.stat().st_size
        result[index.name] = index.stat().st_size
    return result


def validate(root, selected, require_receipt=True):
    for name in selected:
        dataset = DATASETS[name]
        directory = root / dataset["directory"]
        files = validate_directory(directory, dataset)
        receipt = directory / ".rfaa-database.json"
        if require_receipt:
            if not receipt.is_file():
                fail(f"Missing installation receipt: {receipt}; run adopt for existing data")
            saved = json.loads(receipt.read_text())
            if saved.get("version") != VERSION or saved.get("files") != files:
                fail(f"Database receipt does not match {directory}")
        log(f"{name}: ready ({sum(files.values()) / 2**30:.1f} GiB indexed files)")


def checksums(path):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(16 * 1024 * 1024), b""):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


def receipt(directory, dataset, files, sha256=None):
    value = {"version": VERSION, "url": dataset["url"], "archive_bytes": dataset["bytes"],
             "archive_sha256": sha256, "files": files, "installed_at": int(time.time())}
    temporary = directory / ".rfaa-database.json.tmp"
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(directory / ".rfaa-database.json")


def install(root, name, keep_archives):
    dataset = DATASETS[name]
    final = root / dataset["directory"]
    if (final / ".rfaa-database.json").exists():
        validate(root, [name])
        return
    if final.exists():
        fail(f"Unmanaged database directory {final}; validate/adopt it explicitly first")
    archives = root / "archives"
    archives.mkdir(exist_ok=True)
    archive = archives / dataset["archive"]
    partial = Path(f"{archive}.part")
    # A complete download or an interrupted extraction already occupies space.
    # du accounts for allocated blocks, not the apparent size of sparse files.
    staging = root / f".{dataset['directory']}.staging"
    occupied = 0
    for path in (archive, partial, staging):
        if path.exists():
            occupied += int(subprocess.check_output(["du", "-s", "-B1", str(path)]).split()[0])
    needed = max(0, dataset["space_gib"] * 2**30 - occupied)
    if shutil.disk_usage(root).free < needed:
        fail(f"{name} needs roughly {needed / 2**30:.0f} GiB more free space for download/extraction")
    if not archive.exists():
        log(f"Downloading {name}: {dataset['bytes']} bytes (resume supported)")
        # An interruption between the final write and rename leaves a complete
        # .part file. Sending Range: bytes=<length>- would return HTTP 416.
        if not partial.exists() or partial.stat().st_size < dataset["bytes"]:
            subprocess.run(["curl", "--fail", "--location", "--retry", "12", "--retry-delay", "10",
                            "--retry-all-errors", "--connect-timeout", "30", "--speed-time", "120",
                            "--speed-limit", "1024", "--continue-at", "-", "--output", str(partial),
                            dataset["url"]], check=True)
        if partial.stat().st_size != dataset["bytes"]:
            fail(f"Unexpected archive size for {partial}; expected {dataset['bytes']} bytes")
        partial.replace(archive)
    if archive.stat().st_size != dataset["bytes"]:
        fail(f"Unexpected archive size: {archive}")
    log(f"Hashing {name} archive before extraction")
    sha256, md5 = checksums(archive)
    if dataset.get("md5") and dataset["md5"] != md5:
        fail(f"BFD published MD5 mismatch for {archive}; remove the damaged archive and retry")
    staging.mkdir(exist_ok=True)
    log(f"Extracting {name}; interrupted staging is overwritten on retry")
    subprocess.run(["tar", "--extract", "--gzip", "--file", str(archive),
                    "--directory", str(staging), "--no-same-owner", "--no-same-permissions"], check=True)
    # UniRef30/BFD archives use flat paths; the PDB archive has a top-level directory.
    extracted = staging
    if (staging / dataset["directory"]).is_dir():
        extracted = staging / dataset["directory"]
    files = validate_directory(extracted, dataset)
    receipt(extracted, dataset, files, sha256)
    extracted.rename(final)
    if staging.exists():
        staging.rmdir()
    if not keep_archives:
        archive.unlink()
    log(f"Installed {name}; archive {'retained' if keep_archives else 'removed'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "install", "validate", "adopt"])
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("RFAA_DB_DIR", "/mnt/bio-databases/rfaa")))
    parser.add_argument("--only", choices=list(DATASETS))
    parser.add_argument("--keep-archives", action="store_true")
    args = parser.parse_args()
    selected = [args.only] if args.only else list(DATASETS)
    if args.action == "plan":
        print(json.dumps({"version": VERSION, "root": str(args.root), "datasets": {k: DATASETS[k] for k in selected},
                          "capacity_note": "Full deployment: plan at least 3 TiB including temporary archives and headroom."}, indent=2))
        return
    if args.action == "validate":
        validate(args.root, selected)
        return
    args.root.mkdir(parents=True, exist_ok=True)
    with (args.root / ".install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for name in selected:
            if args.action == "adopt":
                directory = args.root / DATASETS[name]["directory"]
                files = validate_directory(directory, DATASETS[name])
                receipt(directory, DATASETS[name], files)
                log(f"Adopted existing {name}; archive provenance not verified")
            else:
                install(args.root, name, args.keep_archives)
        validate(args.root, selected)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"rfaa-databases: ERROR: {error}", file=sys.stderr)
        sys.exit(1)
