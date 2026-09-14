#!/usr/bin/env python3
"""Install the complete, dated ColabFold CPU reference on dedicated storage."""
import argparse
import concurrent.futures
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import time

VERSION = "colabfold-cpu-r18-uniref2302-env202108-pdb230517-tax20250804-v1"
MMSEQS_COMMIT = "8cc5ce367b5638c4306c2d7cfc652dd099a4643f"
BACKEND_COMMIT = "01365aa4735539ba95b417f73fb5326c77410394"
MMSEQS_SHA256 = "b7ef6e0e33df5dd4fa9cf988cbd8b4988c11a3a1255d2c377f13e1bb40c157fb"
BACKEND_SHA256 = "a0ae89d4a38ef1d0aec3842f692a38835fa21fc048cda3ed46f96be28c928b67"
DEFAULT_ROOT = "/mnt/bio-msa-databases/colabfold"
DEFAULT_TOOLS = "/mnt/bio-shared/envs/msa-tools-v1"
GIB = 2**30
SOURCES = {
    "uniref30": dict(archive="uniref30_2302.tar.gz", bytes=102918187842,
                      md5="7c710858a3dcadd750b50e77875bc676"),
    "environmental": dict(archive="colabfold_envdb_202108.tar.gz", bytes=117965643010,
                           md5="fb4976e65837fb8167d46ad0e3653c9c"),
    "taxonomy": dict(archive="uniref30_2302_newtaxonomy.tar.gz", bytes=1975608472,
                     md5=None),
    "pdb100": dict(archive="pdb100_230517.fasta.gz", bytes=28432889,
                   md5="9ed5d01c1f8ba8c281c5d05a2e6d1466"),
    "templates": dict(archive="pdb100_foldseek_230517.tar.gz", bytes=19189110724,
                      md5="ec5f0c493532417478f01b3ac8a30c8e"),
}
for _source in SOURCES.values():
    _source["url"] = "https://opendata.mmseqs.org/colabfold/" + _source["archive"]
COMPONENTS = ("uniref30", "environmental", "pdb100", "templates", "mmcif")
PREFIXES = {"uniref30": "uniref30_2302_db", "environmental": "colabfold_envdb_202108_db",
            "pdb100": "pdb100_230517"}
MANIFEST = dict(schema=1, version=VERSION, profile_gb=3000, cpu=True,
                colabfold_commit="c35de0221f4d297a39edf4cf292ba2832e321edc",
                mmseqs_commit=MMSEQS_COMMIT, backend_commit=BACKEND_COMMIT,
                fast_prebuilt_databases=False, sources=SOURCES,
                taxonomy="representative-sequence taxon IDs, 2025-08-04",
                indexes="complete CPU indexes; no index-subset or GPU padding",
                components=list(COMPONENTS))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


MANIFEST_SHA256 = hashlib.sha256(canonical(MANIFEST)).hexdigest()


def fail(message):
    raise RuntimeError(message)


def log(message):
    print("msa-databases: " + message, file=sys.stderr, flush=True)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def load(path):
    return json.loads(path.read_text())


def digest(path):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(16 * 1024**2), b""):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


def fingerprint(path):
    """Bounded cross-client check; archive checksums establish source integrity."""
    size = path.stat().st_size
    if size <= 0:
        fail(f"Empty installed file: {path}")
    with path.open("rb") as handle:
        first = handle.read(65536)
        handle.seek(max(0, size - 65536))
        last = handle.read(65536)
    return dict(bytes=size, edge_sha256=hashlib.sha256(first + last).hexdigest())


def inventory(directory, exclude_prefix=None):
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.name.startswith(".") or path.is_dir() or (exclude_prefix and path.name.startswith(exclude_prefix)):
            continue
        if not path.resolve().is_relative_to(directory.resolve()):
            fail(f"Database symlink escapes its component: {path}")
        if path.is_file():
            result[str(path.relative_to(directory))] = fingerprint(path)
    return result


def free_space(root, needed):
    free = shutil.disk_usage(root).free
    if free < needed:
        fail(f"Need {needed/GIB:.1f} GiB free, have {free/GIB:.1f} GiB at {root}; "
             "extend dedicated storage, never reduce the databases")


def allocated(path):
    if not path.exists():
        return 0
    return int(subprocess.check_output(["du", "-s", "-B1", str(path)]).split()[0])


def download(root, source):
    archives = root / ".archives"
    archives.mkdir(exist_ok=True)
    archive = archives / source["archive"]
    partial = archive.with_suffix(archive.suffix + ".part")
    saved = archive.with_suffix(archive.suffix + ".json")
    if not archive.exists():
        size = partial.stat().st_size if partial.exists() else 0
        if size > source["bytes"]:
            fail(f"Oversized partial archive: {partial}")
        free_space(root, source["bytes"] - size + 16*GIB)
        if size < source["bytes"]:
            subprocess.run(["curl", "--fail", "--location", "--retry", "12", "--retry-all-errors",
                            "--retry-delay", "10", "--connect-timeout", "30", "--speed-time", "120",
                            "--speed-limit", "1024", "--continue-at", "-", "--output", str(partial),
                            source["url"]], check=True)
        if partial.stat().st_size != source["bytes"]:
            fail(f"Unexpected archive length: {partial}")
        partial.replace(archive)
    if archive.stat().st_size != source["bytes"]:
        fail(f"Unexpected archive length: {archive}")
    sha, md5 = digest(archive)
    if source.get("md5") and source["md5"] != md5:
        fail(f"Published MD5 mismatch for {archive}; damaged data retained for inspection")
    receipt = dict(source, sha256=sha, actual_md5=md5)
    if saved.exists() and load(saved) != receipt:
        fail(f"Archive changed after validation: {archive}")
    write_json(saved, receipt)
    return archive, receipt


def extract(archive, target, selected=None, reserve=16*GIB):
    """Stream regular files only; reject traversal/links instead of trusting tar."""
    target.mkdir(parents=True, exist_ok=True)
    extracted = []
    with gzip.open(archive, "rb") as decoded, tarfile.open(fileobj=decoded, mode="r|", bufsize=4*1024**2) as stream:
        for member in stream:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or member.issym() or member.islnk():
                fail(f"Unsafe archive entry: {member.name}")
            if member.isdir():
                continue
            if not member.isfile():
                fail(f"Unsupported archive entry: {member.name}")
            if selected is not None and str(name) not in selected:
                continue
            output = target.joinpath(*name.parts)
            if output.is_symlink() or not output.resolve().is_relative_to(target.resolve()):
                fail(f"Unsafe extraction destination: {output}")
            output.parent.mkdir(parents=True, exist_ok=True)
            # Account for a resumable overwritten file already occupying space.
            free_space(target, max(0, member.size - allocated(output)) + reserve)
            with stream.extractfile(member) as source, output.open("wb") as dest:
                shutil.copyfileobj(source, dest, 4*1024**2)
            if output.stat().st_size != member.size:
                fail(f"Incomplete extracted file: {output}")
            extracted.append(str(name))
        # tar EOF precedes gzip EOF; finish decoding to verify the gzip CRC.
        while decoded.read(4*1024**2):
            pass
    if selected is not None and set(extracted) != set(selected):
        fail(f"Missing selected archive entries: {set(selected) - set(extracted)}")
    return extracted


def relocate_links(directory):
    """MMseqs aliasdb can write absolute links; make promotion relocatable."""
    root = directory.resolve()
    for path in directory.rglob("*"):
        if path.is_symlink():
            target = path.resolve(strict=True)
            if not target.is_relative_to(root):
                fail(f"Generated link escapes component: {path}")
            relative = os.path.relpath(target, path.parent)
            path.unlink()
            path.symlink_to(relative)


def validate_db(prefix, expanded=False, require_index=True):
    suffixes = ["", "_h", "_seq", "_seq_h", "_aln"] if expanded else ["", "_h"]
    if require_index:
        suffixes.append(".idx")
    for suffix in suffixes:
        p = Path(str(prefix) + suffix)
        for tail in ("", ".index", ".dbtype"):
            file = Path(str(p) + tail)
            if not file.is_file() or file.stat().st_size == 0:
                fail(f"Missing complete MMseqs database member: {file}")
        if Path(str(p) + ".dbtype").stat().st_size != 4:
            fail(f"Invalid MMseqs database type: {p}")
        data_size = p.stat().st_size
        index = Path(str(p) + ".index")
        with index.open("rb") as handle:
            first = handle.readline()
            handle.seek(max(0, index.stat().st_size - 65536))
            last = handle.read().splitlines()[-1]
        for row in (first, last):
            fields = row.split()
            if len(fields) != 3:
                fail(f"Invalid MMseqs index: {index}")
            _, offset, length = map(int, fields)
            # Compressed entries store decoded lengths; only offsets bound data.
            if offset < 0 or offset >= data_size or length < 1:
                fail(f"Truncated MMseqs data/index: {p}")


def validate_component(directory, name):
    if name in PREFIXES:
        validate_db(directory / PREFIXES[name], name != "pdb100")
        if name == "uniref30":
            for suffix in ("_mapping", "_taxonomy", ".idx_mapping", ".idx_taxonomy"):
                if not (directory / (PREFIXES[name] + suffix)).is_file():
                    fail(f"Missing representative taxonomy/pairing data: {suffix}")
    elif name == "templates":
        data = directory / "pdb100_a3m.ffdata"
        index = directory / "pdb100_a3m.ffindex"
        if not data.is_file() or not index.is_file() or min(data.stat().st_size, index.stat().st_size) == 0:
            fail("Missing PDB100 template FFDB")
        with index.open("rb") as handle:
            first = handle.readline()
            handle.seek(max(0, index.stat().st_size - 65536))
            last = handle.read().splitlines()[-1]
        for row in (first, last):
            fields = row.split()
            if len(fields) != 3:
                fail(f"Malformed template FFindex: {index}")
            offset, length = map(int, fields[1:])
            if offset < 0 or length < 1 or offset + length > data.stat().st_size:
                fail(f"Truncated template FFdata: {data}")
    elif name == "mmcif":
        sizes = {}
        for folder, minimum in (("divided", 100000), ("obsolete", 1)):
            paths = list((directory / folder).glob("*/*.cif.gz"))
            count = len(paths)
            if count < minimum:
                fail(f"Incomplete full mmCIF mirror: {folder} contains {count} structures")
            for path in paths:
                if path.is_symlink() or path.stat().st_size <= 0:
                    fail(f"Invalid mmCIF mirror file: {path}")
                sizes[str(path.relative_to(directory))] = path.stat().st_size
        content_manifest = directory / "mmcif-content.jsonl.gz"
        if not content_manifest.is_file():
            fail("Missing full mmCIF content manifest")
        return dict(files=len(sizes), bytes=sum(sizes.values()),
                    path_size_sha256=hashlib.sha256(canonical(sizes)).hexdigest(),
                    content_manifest_sha256=digest(content_manifest)[0])
    return inventory(directory)


def tools(root, profile=None):
    # Database construction and historical callers keep the official runtime.
    # Only an explicit search profile can select the separately pinned wrapper.
    if profile is not None:
        import search_profile
        if search_profile.resolve(profile)["profile_id"] == search_profile.PREFETCH_PROFILE:
            import native_runtime
            return native_runtime.validate(root)
    mmseqs, server = root / "bin/mmseqs", root / "bin/mmseqs-server"
    for binary, flag, expected, sha in ((mmseqs, "version", MMSEQS_COMMIT, MMSEQS_SHA256),
                                       (server, "-version", BACKEND_COMMIT, BACKEND_SHA256)):
        if digest(binary)[0] != sha:
            fail(f"Executable differs from the pinned upstream build: {binary}")
        if subprocess.check_output([str(binary), flag], text=True).strip() != expected:
            fail(f"Unexpected executable version: {binary}")
    return dict(mmseqs=str(mmseqs), server=str(server),
                mmseqs_sha256=MMSEQS_SHA256, server_sha256=BACKEND_SHA256)


def run_mmseqs(tool, args, stage, commands):
    free_space(stage, 16*GIB)
    command = [tool, *map(str, args)]
    commands.append(command)
    write_json(stage / "commands.json", commands)
    env = dict(os.environ, MMSEQS_FORCE_MERGE="1")
    # Runtime GPU flags or shortened indexes cannot leak in from the caller.
    for name in ("GPU", "MMSEQS_FORCE_GPU", "MMSEQS_FORCE_GPUSERVER", "MMSEQS_NO_INDEX", "MMSEQS_IGNORE_INDEX"):
        env.pop(name, None)
    subprocess.run(command, check=True, env=env, stdout=sys.stderr)


def cleanup_sources(root, name, receipt):
    stage = root / ".staging" / name
    if stage.exists():
        shutil.rmtree(stage)
    for source_name, source_receipt in receipt["sources"].items():
        if source_name in SOURCES:
            path = root / ".archives" / SOURCES[source_name]["archive"]
            if path.is_file() and load(path.with_suffix(path.suffix + ".json")) == source_receipt:
                path.unlink()


def sequence_statistics(prefix):
    """Match release18 DBReader::getAminoAcidDBSize without loading sequence data."""
    dtype = int.from_bytes(Path(str(prefix) + ".dbtype").read_bytes(), "little")
    if dtype & 0xFFFF != 0 or (dtype >> 16) & 8:  # DBTYPE_EXTENDED_GPU
        fail(f"Statistics require the unpadded amino-acid database: {prefix}")
    index = Path(str(prefix) + ".index")
    # awk streams the multi-GB numeric index in C. Double precision exactly
    # represents these integer counts/totals (<2**53); no Python list of rows.
    script = '''NF != 3 || $1 !~ /^[0-9]+$/ || $2 !~ /^[0-9]+$/ || $3 !~ /^[0-9]+$/ || $3 < 2 { exit 1 }
{ n++; residues += $3 - 2; if ($3 - 2 > longest) longest = $3 - 2 }
END { if (n == 0) exit 1; printf "%.0f %.0f %.0f\\n", n, residues, longest }'''
    counts = subprocess.check_output(["awk", script, str(index)], text=True,
                                     env=dict(os.environ, LC_ALL="C")).split()
    entries, residues, longest = map(int, counts)
    if residues >= 2**53 or residues <= 0:
        fail(f"Invalid or inexact sequence residue total: {prefix}")
    return dict(entries=entries, residues=residues, max_length=longest,
                encoded_bytes=prefix.stat().st_size, index_bytes=index.stat().st_size)


def conversion_inventory(built, name):
    return inventory(built, exclude_prefix=PREFIXES[name] + ".idx")


def conversion_receipt(root, name, built, source_receipts, commands):
    prefix = built / PREFIXES[name]
    validate_db(prefix, name != "pdb100", require_index=False)
    if name == "uniref30":
        for suffix in ("_mapping", "_taxonomy"):
            if not Path(str(prefix) + suffix).is_file():
                fail(f"Missing converted representative taxonomy: {prefix}{suffix}")
    files = conversion_inventory(built, name)
    marker = built / ".conversion.json"
    if marker.exists():
        receipt = load(marker)
        if receipt.get("manifest_sha256") != MANIFEST_SHA256 or receipt.get("sources") != source_receipts or receipt.get("files") != files:
            fail(f"Converted component differs from its receipt: {name}")
    else:
        statistics = dict(representatives=sequence_statistics(prefix))
        if name != "pdb100":
            statistics["members"] = sequence_statistics(Path(str(prefix) + "_seq"))
        receipt = dict(stage="component-converted", production_ready=False,
                       manifest_sha256=MANIFEST_SHA256, component=name,
                       sources=source_receipts, files=files, statistics=statistics,
                       commands=list(commands), completed_utc=time.time())
        write_json(marker, receipt)
    write_json(root / ".conversions" / (name + ".json"), receipt)
    return receipt


def install_component(root, name, tool, threads, mirror, port, convert_only=False):
    if convert_only and name not in PREFIXES:
        fail(f"Conversion-only is not applicable to {name}")
    final = root / name
    external_receipt = root / ".components" / (name + ".json")
    if final.exists():
        receipt = load(final / ".component.json")
        if receipt.get("manifest_sha256") != MANIFEST_SHA256 or receipt["files"] != validate_component(final, name):
            fail(f"Installed component differs from its receipt: {name}")
        write_json(external_receipt, receipt)
        cleanup_sources(root, name, receipt)
        if convert_only:
            return conversion_receipt(root, name, final, receipt["sources"], receipt.get("commands", []))
        return
    stage = root / ".staging" / name
    stage.mkdir(parents=True, exist_ok=True)
    # Conservative per-stage working allowances; actual extraction checks each
    # member too. They include sources and index/conversion scratch, not just RAM.
    allowance = {"uniref30": 650, "environmental": 1900, "pdb100": 16, "templates": 30, "mmcif": 150}[name]
    free_space(root, max(16*GIB, allowance*GIB - allocated(stage) - allocated(root / ".archives")))
    built = stage / "built"
    commands = load(stage / "commands.json") if (stage / "commands.json").exists() else []
    source_receipts = {}
    if name in ("uniref30", "environmental", "pdb100", "templates"):
        archive, source_receipts[name] = download(root, SOURCES[name])
    if name in PREFIXES:
        prefix = built / PREFIXES[name]
        if not (built / ".converted.json").exists():
            # Only this installer-owned conversion output is discarded on retry;
            # MMseqs' dbtype markers alone cannot prove an interrupted conversion.
            if built.exists():
                shutil.rmtree(built)
            built.mkdir()
            if name == "pdb100":
                run_mmseqs(tool, ["createdb", archive, prefix], stage, commands)
            else:
                sources = stage / "sources"
                extract(archive, sources)
                source_prefix = sources / SOURCES[name]["archive"].removesuffix(".tar.gz")
                run_mmseqs(tool, ["tsv2exprofiledb", source_prefix, prefix, "--threads", threads], stage, commands)
            write_json(built / ".converted.json", dict(source=source_receipts[name]))
        elif load(built / ".converted.json").get("source") != source_receipts[name]:
            fail(f"Converted source changed: {name}")
        if name == "uniref30":
            # Release18 indexdb packs sequences/headers/alignments, not taxonomy;
            # pairaln opens the base _mapping separately. Apply it before the
            # conversion checkpoint without changing index or pairing inputs.
            tax_archive, source_receipts["taxonomy"] = download(root, SOURCES["taxonomy"])
            taxdir = stage / "taxonomy"
            extract(tax_archive, taxdir)
            for suffix in ("_mapping", "_taxonomy"):
                shutil.copyfile(taxdir / (PREFIXES[name] + suffix), Path(str(prefix) + suffix))
            mapping = Path(str(prefix) + "_mapping")
            with mapping.open("rb") as handle:
                mapping_header = handle.read(4)
            if mapping_header != bytes.fromhex("1300170c"):
                temporary = Path(str(mapping) + ".bin")
                temporary.unlink(missing_ok=True)
                run_mmseqs(tool, ["createbintaxmapping", mapping, temporary], stage, commands)
                temporary.replace(mapping)
            for suffix in ("mapping", "taxonomy"):
                link = Path(str(prefix) + ".idx_" + suffix)
                link.unlink(missing_ok=True)
                link.symlink_to(prefix.name + "_" + suffix)
        converted = conversion_receipt(root, name, built, source_receipts, commands)
        if convert_only:
            log(f"Converted complete {name}; indexing remains pending")
            return converted
        if not (built / ".indexed.json").exists():
            for file in built.glob(PREFIXES[name] + ".idx*"):
                # Taxonomy aliases are part of the completed conversion.
                if file.name in (prefix.name + ".idx_mapping", prefix.name + ".idx_taxonomy"):
                    continue
                if file.is_file() or file.is_symlink():
                    file.unlink()
            scratch = stage / "index-tmp"
            if scratch.exists():
                shutil.rmtree(scratch)
            run_mmseqs(tool, ["createindex", prefix, scratch, "--remove-tmp-files", "1", "--threads", threads], stage, commands)
            validate_db(prefix, name != "pdb100")
            write_json(built / ".indexed.json", dict(mmseqs=MMSEQS_COMMIT))
    elif name == "templates":
        extract(archive, built, {"pdb100_a3m.ffdata", "pdb100_a3m.ffindex"})
    else:
        built.mkdir(exist_ok=True)
        for folder in ("divided", "obsolete"):
            target = built / folder
            target.mkdir(exist_ok=True)
            command = ["rsync", "-rlpt", "--partial", "--delete", "--port=" + str(port),
                       mirror.rstrip("/") + "/data/structures/" + folder + "/mmCIF/", str(target) + "/"]
            commands.append(command)
            write_json(stage / "commands.json", commands)
            subprocess.run(command, check=True, stdout=sys.stderr)
        def check_gzip(path):
            sha = hashlib.sha256()
            with gzip.open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(4*1024**2), b""):
                    sha.update(chunk)
            return dict(path=str(path.relative_to(built)), compressed_bytes=path.stat().st_size,
                        mmcif_sha256=sha.hexdigest())
        content_manifest = built / "mmcif-content.jsonl.gz"
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(threads, 8)) as pool, \
                content_manifest.open("wb") as output, \
                gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
            for entry in pool.map(check_gzip, sorted(built.glob("*/*/*.cif.gz"))):
                compressed.write(canonical(entry) + b"\n")
        source_receipts["mmcif"] = dict(mirror=mirror, port=port, completed_utc=time.time(),
                                        gzip_crc_checked=True, decoded_content_sha256_recorded=True)
    relocate_links(built)
    files = validate_component(built, name)
    receipt = dict(manifest_sha256=MANIFEST_SHA256, component=name, sources=source_receipts,
                   commands=commands, files=files, completed_utc=time.time())
    if name in PREFIXES:
        receipt["statistics"] = converted["statistics"]
    write_json(built / ".component.json", receipt)
    built.rename(final)
    write_json(external_receipt, receipt)
    # Promotion proves the generated data complete. Only validated source
    # archives and this component's private staging/intermediates are removed.
    cleanup_sources(root, name, receipt)
    log(f"Installed complete {name}")


def validate(root, require_receipt=True):
    if load(root / "manifest.json") != MANIFEST:
        fail("Database manifest does not match the pinned full CPU reference")
    receipts = {}
    for name in COMPONENTS:
        saved = load(root / ".components" / (name + ".json"))
        if saved.get("manifest_sha256") != MANIFEST_SHA256 or saved["files"] != validate_component(root / name, name):
            fail(f"Component validation failed: {name}")
        receipts[name] = hashlib.sha256(canonical(saved)).hexdigest()
    if require_receipt:
        saved = load(root / ".msa-databases.json")
        if saved.get("manifest_sha256") != MANIFEST_SHA256 or saved.get("components") != receipts:
            fail("Missing or inconsistent complete-installation receipt")
    return dict(version=VERSION, manifest_sha256=MANIFEST_SHA256, components=receipts,
                prefixes={name: str(root / name / prefix) for name, prefix in PREFIXES.items()},
                pdb70=str(root / "templates/pdb100"), pdbdivided=str(root / "mmcif/divided"),
                pdbobsolete=str(root / "mmcif/obsolete"))


def download_sources(root):
    """Stage every pinned source using bounded RAM; never qualify installed DBs."""
    if (root / ".msa-databases.json").exists():
        return dict(stage="installation-already-complete", installation=validate(root))
    remaining = 0
    for source in SOURCES.values():
        path = root / ".archives" / source["archive"]
        if not path.exists():
            path = path.with_suffix(path.suffix + ".part")
        size = path.stat().st_size if path.exists() else 0
        if size > source["bytes"]:
            fail(f"Oversized archive/partial: {path}")
        remaining += source["bytes"] - size
    # All archives coexist in download-only mode, unlike serial conversion.
    free_space(root, remaining + 16*GIB)
    receipts = {}
    for name, source in SOURCES.items():
        log(f"Downloading/verifying {name}")
        _, receipts[name] = download(root, source)
    result = dict(stage="sources-downloaded", production_ready=False,
                  manifest_sha256=MANIFEST_SHA256, sources=receipts, completed_utc=time.time())
    write_json(root / ".downloads.json", result)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("plan", "download", "convert", "install", "validate"))
    p.add_argument("--root", type=Path, default=Path(os.environ.get("MSA_DB_ROOT", DEFAULT_ROOT)))
    p.add_argument("--tools-root", type=Path, default=Path(os.environ.get("MSA_TOOLS_ROOT", DEFAULT_TOOLS)))
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--pdb-mirror", default="rsync.ebi.ac.uk::pub/databases/rcsb/pdb-remediated")
    p.add_argument("--pdb-port", type=int, default=873)
    args = p.parse_args(argv)
    root = args.root.resolve()
    if args.action == "plan":
        print(json.dumps(dict(MANIFEST, manifest_sha256=MANIFEST_SHA256, root=str(root),
                              space_note="3000 decimal GB profile; serial working allowances and actual free-space checks; never reduce databases"), indent=2))
        return
    if args.action == "validate":
        print(json.dumps(validate(root), indent=2))
        return
    if args.threads < 1 or not 1 <= args.pdb_port <= 65535 or "::" not in args.pdb_mirror:
        fail("Invalid thread count or rsync mirror")
    if args.action in ("install", "convert"):
        available = next(int(line.split()[1])*1024 for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))
        minimum = 56 if args.action == "convert" else 120
        if available < minimum*GIB:
            fail(f"Full CPU database {args.action} needs at least {minimum} GiB available RAM; use a preparation worker, not the 16 GB head")
        provenance = tools(args.tools_root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = root / "manifest.json"
        if manifest.exists():
            if load(manifest) != MANIFEST:
                fail("Refusing to mix database versions in an existing root")
        elif any(path.name != ".install.lock" for path in root.iterdir()):
            fail("Refusing unmanaged/nonempty database root")
        else:
            write_json(manifest, MANIFEST)
        if args.action == "download":
            print(json.dumps(download_sources(root), indent=2))
            return
        if args.action == "convert":
            converted = {}
            for name in PREFIXES:
                converted[name] = install_component(root, name, provenance["mmseqs"], args.threads,
                                                    args.pdb_mirror, args.pdb_port, convert_only=True)
            result = dict(stage="databases-converted", production_ready=False, tools=provenance,
                          manifest_sha256=MANIFEST_SHA256, components=converted, completed_utc=time.time())
            write_json(root / ".conversions.json", result)
            print(json.dumps(result, indent=2))
            return
        for name in COMPONENTS:
            install_component(root, name, provenance["mmseqs"], args.threads, args.pdb_mirror, args.pdb_port)
        result = validate(root, require_receipt=False)
        write_json(root / ".msa-databases.json", dict(result, tools=provenance, installed_utc=time.time()))
        print(json.dumps(validate(root), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"msa-databases: ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
