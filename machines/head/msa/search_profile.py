#!/usr/bin/env python3
"""Versioned execution profiles for the complete CPU MSA search.

The limits below control admission, residency and aggregate concurrency. They
do not change a database, index split or scientific parameter. The explicit
mapped-prefetch profile selects a separately pinned scheduling-only native build.
The mapped guest thresholds require qualification on the complete databases;
they are not a source-derived worst-case native allocation guarantee.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re

GIB = 1024**3
MAPPED_PROFILE = "mapped-128gb-v1"
PREFETCH_PROFILE = "mapped-prefetch-128gb-v1"
MAPPED_PROFILES = frozenset((MAPPED_PROFILE, PREFETCH_PROFILE))
LEGACY_PROFILE = "resident-768gib-v1"
DEFAULT_PROFILE = LEGACY_PROFILE
ENVIRONMENT_KEY = "BIO_MSA_SEARCH_PROFILE"
_DEFINITIONS = {
    PREFETCH_PROFILE: dict(minimum_advertised_bytes=128_000_000_000,
        minimum_total_gib=110, minimum_available_gib=100, mmseqs_threads=4,
        warm_mode="report", memory_max_gib=96, memory_swap_max_bytes=0,
        native_variant="gc-mmseqs-posting-prefetch-v2",
        runtime_manifest_sha256="65e7a0e6f7184bc8c0f4aa35c9f28af960b7b4ba59d9f21fad518e9fe4289bca",
        posting_readers=32, posting_window_ids=128, posting_touch_bytes=64*1024**2),
    MAPPED_PROFILE: dict(minimum_advertised_bytes=128_000_000_000,
        minimum_total_gib=110, minimum_available_gib=100, mmseqs_threads=4,
        warm_mode="report", memory_max_gib=96, memory_swap_max_bytes=0),
    LEGACY_PROFILE: dict(minimum_advertised_bytes=768*GIB,
        minimum_total_gib=768, minimum_available_gib=768, mmseqs_threads=16,
        warm_mode="prefetch", memory_max_gib=None, memory_swap_max_bytes=None),
}
ENVIRONMENT_FIELDS = ("MMSEQS_NUM_THREADS", "OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "OMP_DYNAMIC")
PREFETCH_ENVIRONMENT = dict(GC_MMSEQS_POSTING_PREFETCH="1", GC_MMSEQS_POSTING_RANDOM="1",
                            GC_MMSEQS_POSTING_READERS="32")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def require(value, message):
    if not value:
        raise ValueError(message)


def resolve(value=None):
    """Return a fresh exact receipt, rejecting unknown or modified profiles."""
    name = DEFAULT_PROFILE if value is None else value.get("profile_id") if isinstance(value, dict) else value
    require(isinstance(name, str) and name in _DEFINITIONS, "Unknown private MSA search profile")
    definition = dict(schema=1, kind="private-msa-search-profile", profile_id=name,
        api_workers=1, parallel_databases=1, parallel_stages=False, db_load_mode=2,
        **_DEFINITIONS[name])
    result = dict(definition, profile_sha256=hashlib.sha256(canonical(definition)).hexdigest())
    if isinstance(value, dict):
        # Compare canonical bytes rather than Python equality (True == 1).
        try:
            same = canonical(value) == canonical(result)
        except (ValueError, TypeError, OverflowError):
            same = False
        require(same, "Private MSA search profile receipt changed")
    return result


def is_mapped(profile=None):
    return resolve(profile)["profile_id"] in MAPPED_PROFILES


def tools_directory(profile=None):
    return "msa-tools-prefetch-v1" if resolve(profile)["profile_id"] == PREFETCH_PROFILE else "msa-tools-v1"


def environment_fields(profile=None):
    return ENVIRONMENT_FIELDS + (tuple(PREFETCH_ENVIRONMENT) if resolve(profile)["profile_id"] == PREFETCH_PROFILE else ())


def warm_mode(profile=None, requested=None):
    profile = resolve(profile)
    mode = profile["warm_mode"] if requested is None else requested
    require(mode in ("report", "prefetch", "lock"), "Unknown index warm mode")
    require(not is_mapped(profile) or mode == "report",
            "The mapped 128 GB profile requires report-only residency; full prefetch/lock is forbidden")
    return mode


def _memory_bytes(meminfo, key):
    value = meminfo.get(key)
    require(isinstance(value, str), "Missing guest memory field: "+key)
    match = re.fullmatch(r"\s*([0-9]+)\s+kB\s*", value)
    require(match is not None, "Invalid guest memory field: "+key)
    size = int(match.group(1))*1024
    require(0 < size <= 2**60, "Invalid guest memory value: "+key)
    return size


def check_guest(profile=None, meminfo=None):
    """Check actual Linux guest RAM; all /proc/meminfo kB values are KiB."""
    profile = resolve(profile)
    if meminfo is None:
        meminfo = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            require(key not in meminfo, "Duplicate guest memory field")
            meminfo[key] = value
    require(isinstance(meminfo, dict), "Invalid guest memory observation")
    total = _memory_bytes(meminfo, "MemTotal")
    available = _memory_bytes(meminfo, "MemAvailable")
    require(available <= total, "Guest available memory exceeds total memory")
    require(total >= profile["minimum_total_gib"]*GIB,
            f"{profile['profile_id']} requires at least {profile['minimum_total_gib']} GiB guest total RAM")
    require(available >= profile["minimum_available_gib"]*GIB,
            f"{profile['profile_id']} requires at least {profile['minimum_available_gib']} GiB guest available RAM")
    return dict(schema=1, kind="private-msa-guest-memory", profile_sha256=profile["profile_sha256"],
                total_bytes=total, available_bytes=available,
                minimum_total_bytes=profile["minimum_total_gib"]*GIB,
                minimum_available_bytes=profile["minimum_available_gib"]*GIB)


def configure_environment(profile=None, environ=None):
    """Set all relevant native/OpenMP caps before starting any API child."""
    profile = resolve(profile)
    environ = os.environ if environ is None else environ
    threads = str(profile["mmseqs_threads"])
    values = dict(MMSEQS_NUM_THREADS=threads, OMP_NUM_THREADS=threads,
                  OMP_THREAD_LIMIT=threads, OMP_DYNAMIC="FALSE")
    for name in PREFETCH_ENVIRONMENT:
        environ.pop(name, None)
    if profile["profile_id"] == PREFETCH_PROFILE:
        values.update(PREFETCH_ENVIRONMENT)
    environ.update(values)
    environ[ENVIRONMENT_KEY] = profile["profile_id"]
    return values


def validate_guest_receipt(value, profile=None):
    profile = resolve(profile)
    require(isinstance(value, dict), "Missing private MSA guest memory receipt")
    for field in ("total_bytes", "available_bytes"):
        amount = value.get(field)
        require(type(amount) is int and amount > 0 and amount % 1024 == 0,
                "Invalid private MSA guest memory receipt")
    checked = check_guest(profile, dict(MemTotal=str(value["total_bytes"]//1024)+" kB",
                                      MemAvailable=str(value["available_bytes"]//1024)+" kB"))
    require(canonical(value) == canonical(checked), "Private MSA guest memory receipt changed")
    return checked


def validate_configuration(config, provenance, profile=None, *, allow_legacy=False):
    """Verify the aggregate execution policy, including adopted API receipts."""
    profile = resolve(profile)
    try:
        require(config["app"] == "colabfold"
                and type(config["local"]["workers"]) is int
                and config["local"]["workers"] == profile["api_workers"]
                and type(config["worker"]["paralleldatabases"]) is int
                and config["worker"]["paralleldatabases"] == profile["parallel_databases"]
                and config["paths"]["colabfold"]["parallelstages"] is False
                and config["paths"]["colabfold"].get("gpu") is None
                and not config["paths"]["colabfold"].get("environmentalpair"),
                "Private MSA configuration violates serialized search profile")
        runtime = provenance["runtime"]
        if profile["profile_id"] == PREFETCH_PROFILE:
            require(provenance["tools"]["runtime_manifest_sha256"] == profile["runtime_manifest_sha256"]
                    and provenance["tools"]["native_variant"] == profile["native_variant"],
                    "Native runtime differs from the selected search profile")
            import native_runtime
            native_runtime.validate_provenance(provenance["tools"])
            require(config["paths"]["mmseqs"] == provenance["tools"]["mmseqs"],
                    "Mapped-prefetch executable path differs from its provenance")
        else:
            require(not provenance.get("tools", {}).get("native_variant"),
                    "Legacy search profile cannot adopt a modified native runtime")
        recorded = provenance.get("search_profile")
        if recorded is None and allow_legacy and profile["profile_id"] == LEGACY_PROFILE:
            require(type(runtime["mmseqs_threads"]) is int
                    and 1 <= runtime["mmseqs_threads"] <= profile["mmseqs_threads"],
                    "Legacy private MSA thread count exceeds resident profile")
        else:
            require(isinstance(recorded, dict) and resolve(recorded) == profile,
                    "Private MSA API belongs to a different search profile")
            expected = configure_environment(profile, {})
            require(type(runtime["mmseqs_threads"]) is int
                    and runtime["mmseqs_threads"] == profile["mmseqs_threads"]
                    and runtime["environment"] == expected,
                    "Private MSA native thread environment violates search profile")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Incomplete private MSA execution profile configuration") from exc
    return profile


def check_process_environment(pid, profile=None):
    """Read only nonsecret execution limits, rejecting leaked prefetch flags."""
    profile = resolve(profile)
    require(type(pid) is int and pid > 0, "Invalid private MSA API PID")
    expected = configure_environment(profile, {})
    values = {}
    # Never retain or report unrelated process environment entries.
    for entry in (Path("/proc")/str(pid)/"environ").read_bytes().split(b"\0"):
        key, separator, value = entry.partition(b"=")
        if separator and key in {name.encode() for name in (*ENVIRONMENT_FIELDS, *PREFETCH_ENVIRONMENT)}:
            name = key.decode("ascii")
            require(name not in values, "Duplicate private MSA native thread limit")
            values[name] = value.decode("ascii")
    require(values == expected, "Live private MSA API thread environment violates search profile")
    return values


def check_cgroup(profile=None, pid=None, *, proc_root=Path("/proc"), cgroup_root=Path("/sys/fs/cgroup")):
    """Verify the effective physical limit, including stricter ancestors.

    No address-space limit is appropriate: the full index mappings exceed RAM.
    Legacy resident processes deliberately retain their original resource policy.
    """
    profile = resolve(profile)
    if profile["memory_max_gib"] is None:
        return None
    pid = os.getpid() if pid is None else pid
    require(type(pid) is int and pid > 0, "Invalid private MSA process PID")
    lines = (Path(proc_root)/str(pid)/"cgroup").read_text().splitlines()
    paths = [line[3:] for line in lines if line.startswith("0::")]
    require(len(paths) == 1 and paths[0].startswith("/")
            and all(part not in (".", "..") for part in paths[0].split("/")),
            "Mapped private MSA requires an identifiable cgroup-v2 scope")
    root = Path(cgroup_root).resolve()
    directory = root/paths[0].lstrip("/")
    require(directory.resolve().is_relative_to(root), "Private MSA cgroup escapes its mount")
    limits = {}
    for filename in ("memory.max", "memory.swap.max"):
        values = []
        parent = directory
        while True:
            path = parent/filename
            if path.is_file():
                value = path.read_text().strip()
                require(value == "max" or re.fullmatch(r"[0-9]+", value) is not None,
                        "Invalid private MSA cgroup memory limit")
                if value != "max":
                    values.append(int(value))
            else:
                require(parent == root, "Private MSA cgroup memory controller is missing")
            if parent == root:
                break
            parent = parent.parent
        limits[filename] = min(values) if values else None
    require(limits["memory.max"] == profile["memory_max_gib"]*GIB
            and limits["memory.swap.max"] == profile["memory_swap_max_bytes"],
            f"{profile['profile_id']} requires an effective {profile['memory_max_gib']} GiB cgroup memory cap and zero swap")
    return dict(schema=1, kind="private-msa-cgroup-memory", profile_sha256=profile["profile_sha256"],
                cgroup_path=paths[0], memory_max_bytes=limits["memory.max"],
                memory_swap_max_bytes=limits["memory.swap.max"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("show", "check-guest", "check-cgroup"))
    parser.add_argument("--profile", default=os.environ.get(ENVIRONMENT_KEY, DEFAULT_PROFILE))
    args = parser.parse_args(argv)
    profile = resolve(args.profile)
    result = (check_guest(profile) if args.action == "check-guest" else
              check_cgroup(profile) if args.action == "check-cgroup" else profile)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
