#!/usr/bin/env python3
"""Pack the qualified installed environment without ambiguous archive entries.

Some CUDA development packages own overlapping paths. conda-pack lists every
owner's cached file, which can include a different version from the installed
environment. Select the owner that exactly reproduces the installed bytes after
normal conda prefix replacement; never silently pick the last archive member.
"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import stat


def unique_installed_files(prefix, files, replace_prefix):
    groups = defaultdict(list)
    for item in files:
        groups[item.target].append(item)
    selected, overlaps = [], []
    for target, candidates in groups.items():
        if len(candidates) == 1:
            selected.append(candidates[0])
            continue
        installed = prefix / target
        if installed.is_symlink() or not installed.is_file():
            raise ValueError("Overlapping package entry is not a regular file: " + target)
        content = installed.read_bytes()
        executable = stat.S_IMODE(installed.stat().st_mode) & 0o111
        matches = []
        for item in candidates:
            source = Path(item.source)
            if source.is_symlink() or not source.is_file():
                raise ValueError("Overlapping package source is not a regular file: " + target)
            data = source.read_bytes()
            if item.prefix_placeholder and item.file_mode in ("binary", "text"):
                data = replace_prefix(data, item.file_mode, item.prefix_placeholder, str(prefix))
            if data == content and stat.S_IMODE(source.stat().st_mode) & 0o111 == executable:
                matches.append(item)
        if not matches:
            raise ValueError("No cached owner reproduces the installed file: " + target)
        selected.append(matches[0])
        overlaps.append({"path": target, "owners": len(candidates),
                         "installed_sha256": hashlib.sha256(content).hexdigest()})
    return selected, overlaps


def main():
    from conda_pack import CondaEnv
    from conda_pack.prefixes import replace_prefix

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prefix = args.prefix.resolve(strict=True)
    environment = CondaEnv.from_prefix(str(prefix))
    files, overlaps = unique_installed_files(prefix, environment.files, replace_prefix)
    CondaEnv(str(prefix), files).pack(output=str(args.output), n_threads=4, verbose=True)
    receipt = {"schema": "bio-md-pack.v1", "method": "match installed bytes after conda prefix relocation",
               "unique_members": len(files), "overlapping_package_paths": overlaps}
    args.output.with_suffix(args.output.suffix + ".packing.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
