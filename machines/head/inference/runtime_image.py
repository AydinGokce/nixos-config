#!/usr/bin/env python3
"""Stage an immutable same-host runtime from an already verified model venv.

No package installation is performed. The caller supplies the complete package
pin and base interpreter hash. External editable roots require an explicit name,
absolute path, and source inventory digest. Unsupported external links or .pth
execution fail before publication. The original venv is never modified.
"""
import argparse
import ast
import email.parser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


MODELS = {"protenix": ("protenix", "2.0.0"), "openfold3": ("openfold3", "0.5.0"),
          "boltz2": ("boltz", "2.2.1"), "rf3": ("rc-foundry", "0.2.1.dev16+gb02eed6a6")}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(8 << 20), b""):
            value.update(block)
    return value.hexdigest()


def inventory(root):
    """Inventory regular files and symlinks without traversing linked dirs."""
    root = Path(root)
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs.sort(); files.sort()
        for name in list(dirs) + files:
            path = Path(directory) / name
            key = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                result[key] = {"kind": "symlink", "target": os.readlink(path)}
            elif stat.S_ISREG(info.st_mode):
                result[key] = {"kind": "file", "bytes": info.st_size,
                               "executable": bool(info.st_mode & 0o111), "sha256": file_hash(path)}
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError("Unsupported runtime filesystem object: " + key)
    return result


def package_inventory(root):
    values = {}
    for path in Path(root).glob("lib/python*/site-packages/*.dist-info/METADATA"):
        message = email.parser.Parser().parsestr(path.read_text())
        name, version = message.get("Name"), message.get("Version")
        if not name or not version or name in values:
            raise ValueError("Missing or duplicate package metadata")
        values[name] = version
    return values


def normalized_packages(packages):
    result = {name.lower().replace("_", "-"): version for name, version in packages.items()}
    if len(result) != len(packages):
        raise ValueError("Ambiguous package names")
    return result


def relocate(path, mappings):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Expected an absolute runtime reference")
    for old, new in sorted(mappings, key=lambda item: len(str(item[0])), reverse=True):
        if path.is_relative_to(old):
            return new / path.relative_to(old)
    raise ValueError("Undeclared external runtime reference: " + str(path))


def rewrite_pth(text, mappings, allowed_imports):
    result = []
    editable = re.compile(r"import (__editable___[A-Za-z0-9_]+_finder); \1\.install\(\)")
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            result.append(line)
        elif value.startswith(("import ", "import\t")):
            if value not in allowed_imports and not editable.fullmatch(value):
                raise ValueError("Unapproved executable .pth line: " + value)
            result.append(line)
        elif Path(value).is_absolute():
            result.append(str(relocate(value, mappings)))
        else:
            if ".." in Path(value).parts:
                raise ValueError("Escaping relative .pth path")
            result.append(line)
    return "\n".join(result) + "\n"


def rewrite_finder(text, mappings):
    """Only rewrite declared absolute path literals in setuptools finders."""
    tree = ast.parse(text)
    class Relocator(ast.NodeTransformer):
        def visit_Constant(self, node):
            if isinstance(node.value, str) and node.value.startswith("/"):
                return ast.copy_location(ast.Constant(str(relocate(node.value, mappings))), node)
            return node
    return ast.unparse(Relocator().visit(tree)) + "\n"


def copy_tree(source, target, expected, mappings, system_python, allowed_imports, reusable=None):
    changed = {}
    for name, entry in expected.items():
        source_path, target_path = source / name, target / name
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if entry["kind"] == "symlink":
            link = entry["target"]
            resolved = source_path.resolve(strict=True)
            if resolved == system_python and re.fullmatch(r"bin/python(?:[0-9]+(?:\.[0-9]+)?)?", name):
                replacement = str(system_python)
            else:
                final = relocate(resolved, mappings)
                replacement = str(final) if Path(link).is_absolute() else os.path.relpath(final, relocate(source_path, mappings).parent)
            os.symlink(replacement, target_path)
            if replacement != link:
                changed[name] = {"kind": "symlink", "before": link, "after": replacement}
            continue
        if file_hash(source_path) != entry["sha256"]:
            raise ValueError("Source changed while staging: " + name)
        # Reuse only separately verified, read-only local image files. Never
        # hardlink a mutable source venv into its supposedly isolated image.
        reusable_path = (reusable or {}).get((entry["sha256"], entry["executable"]))
        # Python processes startup hooks only directly under site-packages.
        # Native packages also ship binary model checkpoints named *.pth.
        site_hook = bool(re.fullmatch(r"lib/python[0-9]+\.[0-9]+/site-packages/[^/]+", name))
        rewrite_candidate = name.startswith("bin/") or (site_hook and (
            name.endswith((".pth", ".egg-link")) or Path(name).name.startswith("__editable___")))
        if reusable_path is not None and not rewrite_candidate:
            os.link(reusable_path, target_path)
            continue
        shutil.copyfile(source_path, target_path)
        if file_hash(target_path) != entry["sha256"]:
            raise ValueError("Runtime copy failed its content hash: " + name)
        target_path.chmod(0o755 if entry["executable"] else 0o644)
        replacement = None
        if site_hook and name.endswith(".pth"):
            replacement = rewrite_pth(target_path.read_text(), mappings, allowed_imports)
        elif site_hook and Path(name).name.startswith("__editable___") and name.endswith("_finder.py"):
            replacement = rewrite_finder(target_path.read_text(), mappings)
        elif site_hook and name.endswith(".egg-link"):
            replacement = rewrite_pth(target_path.read_text(), mappings, set())
        elif name.startswith("bin/"):
            with target_path.open("rb") as handle:
                first = handle.readline(1024)
            if first.startswith(b"#!") and str(source).encode() in first:
                text = target_path.read_text()
                replacement = text.replace(str(source) + "/bin/", str(relocate(source, mappings)) + "/bin/", 1)
        if replacement is not None and replacement.encode() != target_path.read_bytes():
            target_path.write_text(replacement)
            changed[name] = {"kind": "file", "before_sha256": entry["sha256"], "after_sha256": file_hash(target_path)}
    if inventory(source) != expected:
        raise ValueError("Source runtime changed during staging")
    return changed


def stage(spec, destination):
    model = spec["model"]
    if model not in MODELS:
        raise ValueError("Unsupported pinned model environment")
    source = Path(spec["venv"]).resolve(strict=True)
    destination = Path(destination).absolute()
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Runtime source and destination must be disjoint")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Runtime image destination already exists")
    if not (source / "pyvenv.cfg").is_file():
        raise ValueError("A same-host Python venv is required")
    base = Path(spec["system_python"]["path"]).resolve(strict=True)
    if file_hash(base) != spec["system_python"]["sha256"]:
        raise ValueError("Base interpreter differs from the supplied pin")
    verify_system_runtime(spec.get("system_runtime", {}))
    before = package_inventory(source)
    if normalized_packages(before) != normalized_packages(spec["packages"]):
        raise ValueError("Source package inventory differs from the verified environment")
    package, version = MODELS[model]
    if normalized_packages(before).get(package) != version:
        raise ValueError("Unexpected model package version")
    source_inventory = inventory(source)
    for relative, expected in spec.get("required_files", {}).items():
        if source_inventory.get(relative, {}).get("sha256") != expected:
            raise ValueError("Required native source pin differs: " + relative)
    mappings = [(source, destination / "venv")]
    roots = []
    for item in spec.get("source_roots", []):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", item["name"]):
            raise ValueError("Invalid editable source name")
        if any(name == item["name"] for _, name, _ in roots):
            raise ValueError("Duplicate editable source destination")
        path = Path(item["path"]).resolve(strict=True)
        inv = inventory(path)
        if digest(inv) != item["inventory_sha256"]:
            raise ValueError("Editable source inventory differs from its pin")
        if any(old == path or old.is_relative_to(path) or path.is_relative_to(old) for old, _ in mappings):
            raise ValueError("Overlapping source roots")
        mappings.append((path, destination / "sources" / item["name"]))
        roots.append((path, item["name"], inv))
    allowed = set(spec.get("allowed_pth_imports", []))
    # These standard venv/setuptools hooks remain source-inventoried. All other
    # executable .pth lines must be explicitly included in the input spec.
    allowed.update({"import _virtualenv", "import os; var = 'SETUPTOOLS_USE_DISTUTILS'; enabled = os.environ.get(var, 'local') == 'local'; enabled and __import__('_distutils_hack').add_shim();"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    reusable = {}
    for previous in spec.get("reuse_images", []):
        root = Path(previous["path"]).resolve(strict=True)
        receipt = validate(root)
        if receipt["sha256"] != previous["sha256"]:
            raise ValueError("Reusable runtime image differs from its pin")
        if root.stat().st_dev != destination.parent.stat().st_dev:
            raise ValueError("Reusable image must be on the same local filesystem")
        for name, entry in receipt["files"].items():
            path = root / name
            if entry["kind"] == "file":
                if path.stat().st_mode & 0o222:
                    raise ValueError("Reusable runtime file is writable")
                reusable[(entry["sha256"], entry["executable"])] = path
    required_bytes = sum(entry["bytes"] for _, _, inv in [(source, "venv", source_inventory)] + roots
                         for entry in inv.values() if entry["kind"] == "file"
                         and (entry["sha256"], entry["executable"]) not in reusable)
    reserve = spec.get("minimum_free_bytes", 4 << 30)
    if type(reserve) is not int or reserve < 0 or shutil.disk_usage(destination.parent).free < required_bytes + reserve:
        raise ValueError("Insufficient local runtime capacity with the required free-space reserve")
    temporary = Path(tempfile.mkdtemp(prefix=".runtime-pending-", dir=destination.parent))
    try:
        rewrites = {"venv": copy_tree(source, temporary / "venv", source_inventory, mappings, base, allowed, reusable)}
        for path, name, inv in roots:
            rewrites[name] = copy_tree(path, temporary / "sources" / name, inv, mappings, base, allowed, reusable)
        copied_packages = package_inventory(temporary / "venv")
        if normalized_packages(copied_packages) != normalized_packages(before):
            raise ValueError("Copied package inventory changed")
        for pth in (temporary / "venv").glob("lib/python*/site-packages/*.pth"):
            for name in re.findall(r"import (__editable___[A-Za-z0-9_]+_finder)", pth.read_text()):
                if not (pth.parent / (name + ".py")).is_file():
                    raise ValueError("Editable .pth finder is absent")
        image_inventory = inventory(temporary)
        libs = sorted(str(destination / p.relative_to(temporary)) for p in (temporary / "venv").glob("lib/python*/site-packages/nvidia/*/lib"))
        libs += sorted(str(destination / p.relative_to(temporary)) for p in (temporary / "venv").glob("lib/python*/site-packages/nvidia/*/lib64"))
        libs += sorted(str(destination / p.relative_to(temporary)) for p in (temporary / "venv").glob("lib/python*/site-packages/torch/lib"))
        receipt = dict(schema=1, model=model, source_venv=str(source), packages=before,
                       specification_sha256=digest(spec), system_python=spec["system_python"],
                       system_runtime=spec.get("system_runtime", {}), reuse_images=spec.get("reuse_images", []),
                       source_inventory_sha256=digest(source_inventory), files=image_inventory,
                       source_roots={name: {"path": str(path), "inventory_sha256": digest(inv)} for path, name, inv in roots},
                       rewrites=rewrites, python=str(destination / "venv/bin/python"),
                       environment={"PATH": str(destination / "venv/bin") + ":/usr/local/cuda/bin:/usr/bin:/bin",
                                    "LD_LIBRARY_PATH": ":".join(libs), "PYTHONDONTWRITEBYTECODE": "1"},
                       path_aliases={str(old): str(new) for old, new in mappings})
        receipt["sha256"] = digest(receipt)
        (temporary / "runtime-image.json").write_bytes(json.dumps(receipt, indent=2).encode() + b"\n")
        for path in temporary.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o555 if path.is_dir() or path.stat().st_mode & 0o111 else 0o444)
        temporary.chmod(0o555)
        os.rename(temporary, destination)
        # The interpreter is the pinned same-host base; final-prefix .pth and
        # finder aliases only become executable once atomic publication exists.
        probe = "import importlib.metadata,json; print(json.dumps({d.metadata['Name']:d.version for d in importlib.metadata.distributions() if d.metadata.get('Name')}))"
        env = dict(os.environ, **receipt["environment"])
        observed = json.loads(subprocess.check_output([receipt["python"], "-I", "-B", "-c", probe], env=env, text=True, timeout=60))
        if normalized_packages(observed) != normalized_packages(before):
            raise ValueError("Published interpreter package probe differs; image must not be launched")
        destination.chmod(0o700)
        ready = {"schema": 1, "runtime_image_sha256": receipt["sha256"],
                 "observed_packages_sha256": digest(normalized_packages(observed)), "status": "ready"}
        with (destination / "ready.json").open("x") as handle:
            json.dump(ready, handle, sort_keys=True)
            handle.write("\n")
        (destination / "ready.json").chmod(0o444)
        destination.chmod(0o555)
        return receipt
    except BaseException:
        if temporary.exists():
            for path in temporary.rglob("*"):
                if not path.is_symlink():
                    if path.is_dir() or path.stat().st_nlink == 1:
                        path.chmod(0o755 if path.is_dir() else 0o644)
            temporary.chmod(0o700)
            shutil.rmtree(temporary)
        raise


def validate(path):
    root = Path(path).resolve(strict=True)
    receipt = json.loads((root / "runtime-image.json").read_text())
    ready = json.loads((root / "ready.json").read_text())
    claimed = receipt.pop("sha256")
    if digest(receipt) != claimed:
        raise ValueError("Runtime image receipt changed")
    files = inventory(root)
    files.pop("runtime-image.json")
    files.pop("ready.json")
    if (ready.get("status") != "ready" or ready.get("runtime_image_sha256") != claimed
            or ready.get("observed_packages_sha256") != digest(normalized_packages(receipt["packages"]))):
        raise ValueError("Runtime interpreter did not pass its bound package probe")
    verify_system_runtime(receipt.get("system_runtime", {}))
    if files != receipt["files"] or file_hash(Path(receipt["system_python"]["path"]).resolve()) != receipt["system_python"]["sha256"]:
        raise ValueError("Runtime image or base interpreter changed")
    receipt["sha256"] = claimed
    return receipt


def verify_system_runtime(pins):
    for name, expected in pins.get("files", {}).items():
        if not Path(name).is_absolute() or file_hash(name) != expected:
            raise ValueError("Pinned system runtime file changed: " + name)
    for name, expected in pins.get("trees", {}).items():
        if not Path(name).is_absolute() or digest(inventory(name)) != expected:
            raise ValueError("Pinned system runtime tree changed: " + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("stage")
    create.add_argument("--spec", type=Path, required=True)
    create.add_argument("--out", type=Path, required=True)
    check = commands.add_parser("validate")
    check.add_argument("--image", type=Path, required=True)
    args = parser.parse_args()
    result = stage(json.loads(args.spec.read_text()), args.out) if args.command == "stage" else validate(args.image)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
