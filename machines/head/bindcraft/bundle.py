"""Self-contained, hashed BindCraft inputs; no native imports or cloud access."""
import hashlib
import io
import json
import math
from pathlib import Path
import re
import tarfile

HERE = Path(__file__).resolve().parent
PIN = "efb5bfeb8b4b1a5944256f979c34e0c8e6a82d9d"
ASSETS = {"target.pdb", "settings.json", "advanced.json", "filters.json"}
OPTIONAL_ASSETS = {"execution.json"}
MAX_BYTES = 32 << 20
AMINO_ACIDS = set("ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL".split())


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON number")))


def defaults(kind):
    return read_json((HERE / "defaults" / (kind + ".json")).read_bytes())


def validate_pdb(data, chains):
    text = data.decode("ascii")
    seen = {}
    models = 0
    for line in text.splitlines():
        if line.startswith("MODEL "):
            models += 1
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        if len(line) < 54:
            raise ValueError("Truncated PDB atom record")
        chain = line[21]
        if chain not in chains:
            continue
        if line[:6] != "ATOM  " or line[17:20] not in AMINO_ACIDS:
            raise ValueError("BindCraft target chains must contain canonical protein residues only")
        if line[26] != " ":
            raise ValueError("Renumber target insertion codes before BindCraft submission")
        for start in (30, 38, 46):
            if not math.isfinite(float(line[start:start + 8])):
                raise ValueError("Nonfinite PDB coordinate")
        number = int(line[22:26])
        if line[12:16].strip() == "CA":
            seen.setdefault(chain, set()).add(number)
    if models > 1:
        raise ValueError("Use one PDB model for BindCraft")
    if set(chains) != set(seen) or any(len(residues) < 2 for residues in seen.values()):
        raise ValueError("Every selected target chain needs at least two protein residues")
    return seen


def validate_settings(settings, pdb):
    expected = {"binder_name", "chains", "target_hotspot_residues", "lengths", "number_of_final_designs"}
    if not isinstance(settings, dict) or set(settings) != expected:
        raise ValueError("Unsupported or missing BindCraft target setting")
    if not isinstance(settings["binder_name"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", settings["binder_name"]):
        raise ValueError("Binder name must be a short filename-safe label")
    chains = settings["chains"]
    if not isinstance(chains, str) or not re.fullmatch(r"[A-Za-z0-9](,[A-Za-z0-9])*", chains):
        raise ValueError("Select comma-separated single-character PDB chains")
    chains = chains.split(",")
    if len(set(chains)) != len(chains):
        raise ValueError("Duplicate target chain")
    residues = validate_pdb(pdb, chains)
    lengths = settings["lengths"]
    if not isinstance(lengths, list) or len(lengths) != 2 or any(type(n) is not int or not 5 <= n <= 1000 for n in lengths) or lengths[0] > lengths[1]:
        raise ValueError("Binder lengths must be [minimum, maximum] between 5 and 1000")
    count = settings["number_of_final_designs"]
    if type(count) is not int or not 1 <= count <= 10000:
        raise ValueError("Request between 1 and 10000 final designs")
    hotspots = settings["target_hotspot_residues"]
    if hotspots is not None:
        if not isinstance(hotspots, str) or len(hotspots) > 4096:
            raise ValueError("Invalid hotspot expression")
        for token in hotspots.split(","):
            match = re.fullmatch(r"([A-Za-z])?(\d+)?(?:-(\d+))?", token)
            if not match or not token:
                raise ValueError("Use hotspot positions, ranges or chain letters")
            chain, start, stop = match.groups()
            chain = chain or chains[0]
            if chain not in residues or (stop and not start):
                raise ValueError("Hotspot chain is absent from the target")
            if start:
                first, last = int(start), int(stop or start)
                if first > last or last - first > 10000 or not set(range(first, last + 1)) <= residues[chain]:
                    raise ValueError("Hotspot residue is absent from its target chain")


def validate_advanced(value):
    original = defaults("advanced")
    if not isinstance(value, dict) or set(value) != set(original):
        raise ValueError("Advanced settings must match the pinned upstream schema")
    for key, default in original.items():
        item = value[key]
        if key in {"af_params_dir", "dssp_path", "dalphaball_path"}:
            if item != "":
                raise ValueError("Runtime asset paths are managed by the cluster")
        elif key == "max_trajectories":
            if item is not False and (type(item) is not int or item < 1):
                raise ValueError("max_trajectories must be false or a positive integer")
        elif isinstance(default, bool):
            if type(item) is not bool:
                raise ValueError("Expected boolean for " + key)
        elif isinstance(default, (int, float)):
            if type(item) not in (int, float) or not math.isfinite(item):
                raise ValueError("Expected finite number for " + key)
            if isinstance(default, int) and type(item) is not int:
                raise ValueError("Expected integer for " + key)
            if any(word in key for word in ("iterations", "recycles", "num_seqs", "max_mpnn", "start_monitoring")) and item < 0:
                raise ValueError("Negative iteration count for " + key)
        elif not isinstance(item, str):
            raise ValueError("Expected string for " + key)
    if value["design_algorithm"] not in {"2stage", "3stage", "4stage", "greedy", "mcmc"}:
        raise ValueError("Unsupported design algorithm")
    if value["model_path"] not in {"v_48_002", "v_48_010", "v_48_020", "v_48_030"} or value["mpnn_weights"] not in {"original", "soluble"}:
        raise ValueError("Unsupported MPNN weights")


def validate_filters(value):
    def check(actual, template):
        if isinstance(template, dict):
            if not isinstance(actual, dict) or set(actual) != set(template):
                raise ValueError("Filters must match the pinned upstream schema")
            for key in template:
                if key == "threshold":
                    item = actual[key]
                    if item is not None and (type(item) not in (int, float) or not math.isfinite(item)):
                        raise ValueError("Filter thresholds must be null or finite numbers")
                else:
                    check(actual[key], template[key])
        elif type(actual) is not type(template):
            raise ValueError("Invalid filter value")
    check(value, defaults("filters"))


def validate_assets(assets):
    if not ASSETS <= set(assets) or not set(assets) <= ASSETS | OPTIONAL_ASSETS:
        raise ValueError("Unexpected input bundle assets")
    if 'execution.json' in assets:
        execution = read_json(assets['execution.json'])
        if (not isinstance(execution, dict) or set(execution) != {'seed'} or
                type(execution['seed']) is not int or not 0 <= execution['seed'] <= 2147483647):
            raise ValueError('Execution settings require one integer campaign seed in 0..2147483647')
    settings = read_json(assets["settings.json"])
    validate_settings(settings, assets["target.pdb"])
    validate_advanced(read_json(assets["advanced.json"]))
    validate_filters(read_json(assets["filters.json"]))
    return settings


def create(pdb, settings, advanced, filters, out, *, execution=None):
    assets = {"target.pdb": Path(pdb).read_bytes(), "settings.json": encoded(settings),
              "advanced.json": encoded(advanced), "filters.json": encoded(filters)}
    if execution is not None:
        assets['execution.json'] = encoded(execution)
    validate_assets(assets)
    if sum(map(len, assets.values())) > MAX_BYTES:
        raise ValueError("BindCraft input exceeds 32 MiB")
    manifest = {"schema": 1, "kind": "bindcraft-input", "bindcraft_commit": PIN,
                "files": {name: digest(data) for name, data in assets.items()}}
    assets["manifest.json"] = encoded(manifest)
    with Path(out).open("xb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in sorted(assets.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
    return manifest


def inspect(path):
    if Path(path).stat().st_size > MAX_BYTES:
        raise ValueError("BindCraft archive exceeds 32 MiB")
    assets, total = {}, 0
    try:
        archive = tarfile.open(path, "r:gz")
    except tarfile.TarError as error:
        raise ValueError("Invalid BindCraft gzip/tar input bundle") from error
    with archive:
        for member in archive:
            if not member.isfile() or member.name not in ASSETS | OPTIONAL_ASSETS | {"manifest.json"} or member.name in assets:
                raise ValueError("Unexpected, duplicate or unsafe bundle member")
            total += member.size
            if total > MAX_BYTES:
                raise ValueError("Expanded BindCraft input exceeds 32 MiB")
            assets[member.name] = archive.extractfile(member).read()
    manifest = read_json(assets.pop("manifest.json", b"{}"))
    if not isinstance(manifest, dict) or type(manifest.get("schema")) is not int or manifest.get("schema") != 1 or manifest.get("kind") != "bindcraft-input" or manifest.get("bindcraft_commit") != PIN:
        raise ValueError("Invalid or unsupported BindCraft bundle manifest")
    if manifest.get("files") != {name: digest(data) for name, data in assets.items()}:
        raise ValueError("BindCraft input hash mismatch")
    validate_assets(assets)
    return manifest, assets


def materialize(path, destination):
    manifest, assets = inspect(path)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for name, data in assets.items():
        (destination / name).write_bytes(data)
    (destination / "manifest.json").write_bytes(encoded(manifest))
    return manifest
