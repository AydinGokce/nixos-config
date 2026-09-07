"""Plan and copy only a temporary worker's selected scientific runtime assets."""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import stat


ROOTS = ("envs", "src", "python", "weights", "protenix", "openfold3", "rf3",
         "cache/hf", "cache/torch", "cache/uv", "cache/boltz", "cache/rfaa")
FOUNDRY = "b02eed6a6bdf8f44d14a80cc36e3da13c9f2291c"
GIB = 1024 ** 3


def paths_for(recipe, model="", sub=""):
    paths = {
        "boltz2": ["envs/boltz", "cache/boltz"],
        "openfold3": ["envs/openfold3", "openfold3/home"],
        "protenix": ["envs/protenix", "protenix/release_data"],
        "rf3": ["envs/rf3", f"src/foundry-rf3-{FOUNDRY}", "rf3/checkpoints",
                "rf3/installed.txt", "rf3/versions.json"],
        "mpnn": ["envs/proteinmpnn", "src/proteinmpnn"],
        "rfdiffusion": ["envs/rfdiffusion", "src/rfdiffusion", "python/py310", "weights/rfdiffusion"],
        "rfaa": ["envs/rfaa", "src/rfaa", "python/py310"],
        "af3": ["envs/alphafold3", "src/alphafold3", "weights/alphafold3"],
        "esm": ["envs/esm2"],
        "evolvepro": ["envs/evolvepro-plm", "envs/evolvepro-core"],
        "msa": ["envs/msa-tools-v1"],
        "md": [],  # Pinned packed MD runtime is selected by its separate lock.
    }[recipe].copy()
    if recipe in {"esm", "evolvepro"}:
        embedding = model or "esm2_t33_650M_UR50D"
        repository = "facebook/" + embedding if embedding.startswith("esm") and "/" not in embedding else embedding
        cache_name = "models--" + repository.replace("/", "--")
        if any(part in {".", "..", ""} for part in repository.split("/")) or "\\" in repository:
            raise ValueError("unsafe embedding model cache name")
        paths += ["cache/hf/hub/" + cache_name]
    if recipe == "msa" and sub != "convert":
        models = [model] if model in {"boltz2", "openfold3", "protenix"} else []
        if sub in {"panel", "session", "install"}:
            models = ["boltz2", "openfold3", "protenix"]
        for selected in models:
            paths.extend(paths_for(selected))
    return sorted(set(paths))


def source_size(source, *, shared=None, selected=()):
    total = files = 0
    if not source.exists() and not source.is_symlink():
        return total, files
    pending = [(source, source.lstat())]
    while pending:
        path, info = pending.pop()
        if stat.S_ISLNK(info.st_mode):
            if shared is not None:
                target = path.resolve(strict=False)
                if target.is_relative_to(shared):
                    relative = str(target.relative_to(shared))
                    if not any(relative == root or relative.startswith(root + "/") for root in selected):
                        raise ValueError(f"runtime symlink escapes selected assets: {path} -> {target}")
            files += 1
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
            files += 1
        elif stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                pending.extend((Path(entry.path), entry.stat(follow_symlinks=False)) for entry in entries)
        else:
            raise ValueError(f"special file in scientific runtime: {path}")
    return total, files


def plan(shared, recipe, model="", sub=""):
    paths = paths_for(recipe, model, sub)
    total = files = 0
    for relative in paths:
        source = shared / relative
        # Never allow a source root to redirect copying outside its named tree.
        if source.resolve() != source:
            raise ValueError(f"runtime source contains a symlink: {source}")
        size, count = source_size(source, shared=shared, selected=paths)
        total += size
        files += count
    # Local package setup/downloads need headroom beyond the existing assets.
    reserve = 25 * GIB
    if recipe in {"esm", "evolvepro"}:
        reserve += 65 * GIB if "15B" in model else (15 * GIB if "3B" in model else 0)
    os_size = max(50, math.ceil((total * 1.15 + reserve) / 10**9))
    if os_size > 200:
        raise ValueError(f"selected runtime needs a {os_size} GB worker disk, above the 200 GB cap; no worker launched")
    return dict(schema=1, isolation="private-local-copies", recipe=recipe, model=model, sub=sub,
                paths=paths, source_bytes=total, source_files=files, os_size_gb=os_size,
                setup_reserve_bytes=reserve)


def stage(shared, destination, value):
    if (value.get("schema") != 1 or value.get("isolation") != "private-local-copies"
            or value.get("paths") != paths_for(value["recipe"], value["model"], value["sub"])
            or not 50 <= value["os_size_gb"] <= 200):
        raise ValueError("invalid worker runtime plan")
    if shutil.disk_usage(destination.parent).free < value["source_bytes"] + 8 * GIB:
        raise ValueError("worker disk lacks space for its selected runtime and setup headroom")
    destination.mkdir()
    for relative in ROOTS:
        (destination / relative).mkdir(parents=True, exist_ok=True)
    for relative in value["paths"]:
        source = shared / relative
        if source.resolve() != source:
            raise ValueError(f"runtime source contains a symlink: {source}")
        if not source.exists():
            continue  # Existing recipes retain their original first-use setup.
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "stage", "roots"])
    parser.add_argument("--shared", type=Path)
    parser.add_argument("--recipe")
    parser.add_argument("--model", default="")
    parser.add_argument("--sub", default="")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.command == "roots":
        print("\n".join(ROOTS))
    elif args.command == "plan":
        print(json.dumps(plan(args.shared, args.recipe, args.model, args.sub), indent=2))
    else:
        stage(args.shared, args.destination, json.loads(args.plan.read_text()))
