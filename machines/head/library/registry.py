#!/usr/bin/env python3
"""Head-local, immutable molecular records with a rebuildable SQLite index.

The registry preserves chemical descriptions; it does not infer chemistry or
promise that a prediction model supports a stored description.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import datetime
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile

SCHEMA = 1
DEFAULT_ROOT = "/var/lib/bio-library"
COLLECTIONS = {"construct": "constructs", "monomer": "monomers", "assembly": "assemblies"}
ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
CHAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{0,31}\Z")
REF = re.compile(r"(construct|monomer|assembly):([a-z][a-z0-9_-]{0,63})@([1-9][0-9]*)\Z")
MOLECULE_TYPES = {"protein", "dna", "rna", "small_molecule", "mixed_polymer"}
ALPHABETS = {"protein": set("ACDEFGHIKLMNPQRSTVWYXBZUOJ"),
             "dna": set("ACGTRYSWKMBDHVN"), "rna": set("ACGURYSWKMBDHVN")}
USER_FIELDS = {"schema", "kind", "id", "name", "aliases", "tags", "notes", "parents",
               "status", "identity", "provenance"}
MANAGED_FIELDS = {"revision", "attachments", "created_at", "sha256"}


class Error(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise Error(message)


def json_bytes(document):
    try:
        return json.dumps(document, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Error(f"Not a finite JSON document: {exc}") from exc


def digest_json(document):
    """Digest an object excluding only its top-level sha256 receipt field."""
    require(isinstance(document, dict), "A digested document must be an object")
    return hashlib.sha256(json_bytes({k: v for k, v in document.items() if k != "sha256"})).hexdigest()


def verify_document(document):
    require(isinstance(document, dict) and document.get("sha256") == digest_json(document),
            "Document SHA-256 mismatch")
    return document


def load_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        return parse_json(handle.read())


def parse_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    def constant(value):
        raise Error(f"Nonfinite JSON number: {value}")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def reference(record):
    return f"{record['kind']}:{record['id']}@{record['revision']}"


def pinned_parts(ref):
    require(isinstance(ref, str), "Reference must be a string")
    match = REF.fullmatch(ref)
    require(match is not None, f"Expected a pinned kind:id@revision reference: {ref!r}")
    return match[1], match[2], int(match[3])


def safe_relative(value):
    require(isinstance(value, str) and value, "Attachment path must be nonempty")
    path = PurePosixPath(value)
    require(not path.is_absolute() and str(path) == value and "\\" not in value and "\x00" not in value and
            all(part not in {".", ".."} for part in path.parts), f"Unsafe path: {value!r}")
    return path


def no_symlinks(path, *, regular=False):
    path = Path(path).absolute()
    for item in [*reversed(path.parents), path]:
        require(not item.is_symlink(), f"Symlinks are not allowed: {item}")
    if regular:
        require(path.is_file() and stat.S_ISREG(path.stat().st_mode), f"Expected regular file: {path}")
    return path


def file_digest(path):
    no_symlinks(path, regular=True)
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path, document):
    data = (json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    with open(path, "xb") as handle:
        os.chmod(path, 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def atomic_json(path, document):
    path = Path(path).absolute()
    no_symlinks(path)
    require(path.parent.is_dir(), f"Output parent does not exist: {path.parent}")
    fd, temp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def strings(value, field):
    require(isinstance(value, list) and all(isinstance(x, str) and x for x in value),
            f"{field} must be a list of nonempty strings")
    require(len(value) == len(set(value)), f"Duplicate {field}")


def position(value, length, field):
    require(type(value) is int and value >= 1, f"{field} must be a positive 1-based integer")
    if length is not None:
        require(value <= length, f"{field} {value} exceeds length {length}")


def identity_length(record):
    identity = record["identity"]
    return len(identity.get("sequence", identity.get("residues", []))) or None


def reference_values(value):
    """Recognized chemical references, including nested terminal modifications."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"monomer_ref", "construct_ref"}:
                yield key, child
            elif key in {"monomer_refs", "construct_refs"}:
                require(isinstance(child, list), f"{key} must be a list")
                for item in child:
                    yield key[:-1], item
            else:
                yield from reference_values(child)
    elif isinstance(value, list):
        for item in value:
            yield from reference_values(item)


def validate_identity(record):
    identity = record.get("identity")
    require(isinstance(identity, dict), "identity must be a JSON object")
    kind = record["kind"]
    if kind == "construct":
        molecule = identity.get("molecule_type")
        require(molecule in MOLECULE_TYPES, f"Unsupported molecule_type: {molecule!r}")
        if "sequence" in identity:
            sequence = identity["sequence"]
            require(isinstance(sequence, str) and sequence, "sequence must be a nonempty string")
            require(molecule in ALPHABETS, "Use residues for mixed polymers, not an untyped sequence")
            require(set(sequence) <= ALPHABETS[molecule],
                    f"Invalid {molecule} sequence symbols; use explicit JSON modifications/residues")
        if "residues" in identity:
            residues = identity["residues"]
            require(isinstance(residues, list) and residues and
                    all((isinstance(x, str) and x) or isinstance(x, dict) and x for x in residues),
                    "residues must be a nonempty list of symbols or monomer descriptions")
            if "sequence" in identity:
                require(len(residues) == len(identity["sequence"]), "sequence/residues length mismatch")
        for field in ("smiles", "ccd", "structure_format", "structure_file"):
            if field in identity:
                require(isinstance(identity[field], str) and identity[field], f"{field} must be a nonempty string")
        if "smiles" in identity:
            require(not any(x in identity["smiles"] for x in ("\n", "\r", "\x00")), "SMILES must be one line")
        if "structure_file" in identity:
            require(str(safe_relative(identity["structure_file"])).startswith("attachments/"),
                    "structure_file must refer to an original attachment")
        if "circular" in identity:
            require(type(identity["circular"]) is bool, "circular must be boolean")
        length = identity_length(record)
        for field in ("modifications", "linkages"):
            if field not in identity:
                continue
            require(isinstance(identity[field], list), f"{field} must be a list")
            for item in identity[field]:
                require(isinstance(item, dict), f"Every {field} entry must be an object")
                require("position" in item or field == "linkages" and
                        {"from_position", "to_position"} <= set(item), f"{field} entry needs a position")
                for key in ("position", "from_position", "to_position"):
                    if key in item:
                        bound = length
                        if field == "linkages" and key == "position" and bound and not identity.get("circular", False):
                            bound -= 1
                        position(item[key], bound, f"{field}.{key}")
        if "termini" in identity:
            require(isinstance(identity["termini"], dict), "termini must be an object")
        for field in ("bonds", "crosslinks"):
            if field in identity:
                require(isinstance(identity[field], list), f"{field} must be a list")
                for bond in identity[field]:
                    validate_bond(bond, {"A": record}, internal=True)
    elif kind == "assembly":
        components = identity.get("components")
        require(isinstance(components, list) and components, "Assembly needs nonempty components")
        ids = []
        for component in components:
            require(isinstance(component, dict), "Assembly components must be objects")
            chain_id = component.get("chain_id")
            require(isinstance(chain_id, str) and CHAIN.fullmatch(chain_id), "Invalid chain_id")
            require(isinstance(component.get("construct_ref"), str), "Component needs construct_ref")
            require("count" not in component, "Use an explicit component/chain_id for every copy")
            ids.append(chain_id)
        require(len(ids) == len(set(ids)), "Duplicate assembly chain_id")
        require(isinstance(identity.get("bonds", []), list), "Assembly bonds must be a list")
    elif kind == "monomer":
        # Custom chemical graphs/attachment sites are preserved, not inferred.
        for field in ("smiles", "ccd", "structure_file"):
            if field in identity:
                require(isinstance(identity[field], str) and identity[field], f"{field} must be a nonempty string")
        if "structure_file" in identity:
            require(str(safe_relative(identity["structure_file"])).startswith("attachments/"),
                    "structure_file must refer to an original attachment")
    for key, ref in reference_values(identity):
        ref_kind, _, _ = pinned_parts(ref)
        require(ref_kind == key.removesuffix("_ref"), f"Wrong reference kind for {key}: {ref}")
        require(key != "construct_ref" or kind == "assembly", "construct_ref is only supported in assemblies")


def validate_bond(bond, chains, *, internal=False):
    require(isinstance(bond, dict) and {"from", "to"} <= set(bond), "Bond needs from/to atom endpoints")
    for side in ("from", "to"):
        endpoint = bond[side]
        require(isinstance(endpoint, dict), f"Bond {side} must be an object")
        chain = endpoint.get("chain_id", "A" if internal else None)
        require(isinstance(chain, str) and chain in chains, f"Unknown bond chain: {chain!r}")
        atom = endpoint.get("atom")
        require((isinstance(atom, str) and atom.strip() and "\x00" not in atom) or
                (type(atom) is int and atom >= 0), "Bond atom must be an explicit name or index")
        if "position" in endpoint:
            position(endpoint["position"], identity_length(chains[chain]), "bond.position")
        elif chains[chain]["identity"]["molecule_type"] != "small_molecule":
            raise Error("Polymer bond endpoint requires a 1-based residue position")


class Registry:
    def __init__(self, root=DEFAULT_ROOT):
        self.root = Path(root).absolute()
        no_symlinks(self.root)

    def init(self):
        no_symlinks(self.root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in [*COLLECTIONS.values(), ".staging"]:
            path = self.root / name
            no_symlinks(path)
            path.mkdir(mode=0o700, exist_ok=True)
        with self._lock(exclusive=True):
            self._reindex_locked(self._records_locked())
        return {"schema": SCHEMA, "root": str(self.root)}

    @contextmanager
    def _lock(self, *, exclusive=False):
        no_symlinks(self.root)
        require(self.root.is_dir(), f"Registry not initialized: {self.root}")
        path = self.root / ".registry.lock"
        no_symlinks(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode), "Registry lock must be a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)

    def _path(self, ref):
        kind, ident, rev = pinned_parts(ref)
        return self.root / COLLECTIONS[kind] / ident / str(rev) / "record.json"

    def _read(self, path):
        no_symlinks(path, regular=True)
        document = verify_document(load_json(path))
        require(type(document.get("schema")) is int and document["schema"] == SCHEMA and document.get("kind") in COLLECTIONS,
                "Unsupported record schema/kind")
        require(set(document) == USER_FIELDS | MANAGED_FIELDS, "Unexpected or missing persisted record fields")
        require(isinstance(document.get("id"), str) and ID.fullmatch(document["id"]), "Invalid record id")
        require(type(document.get("revision")) is int and document["revision"] >= 1, "Invalid record revision")
        require(path == self._path(reference(document)), "Record location/identity mismatch")
        self._validate_metadata(document)
        validate_identity(document)
        attachments = document.get("attachments")
        require(isinstance(attachments, list), "Record attachments must be a list")
        names = []
        for item in attachments:
            require(isinstance(item, dict) and set(item) == {"path", "sha256", "bytes"}, "Invalid attachment receipt")
            relative = safe_relative(item["path"])
            require(relative.parts[0] == "attachments" and len(relative.parts) == 2, "Invalid attachment path")
            require(type(item["bytes"]) is int and item["bytes"] >= 0, "Invalid attachment size")
            target = path.parent / relative
            no_symlinks(target, regular=True)
            require(target.stat().st_size == item["bytes"] and file_digest(target) == item["sha256"],
                    f"Attachment integrity failure: {reference(document)}/{relative}")
            names.append(item["path"])
        require(len(names) == len(set(names)), "Duplicate attachment path")
        if document["identity"].get("structure_file"):
            require(document["identity"]["structure_file"] in names, "structure_file is missing its attachment")
        return document

    def _records_locked(self):
        result = {}
        for kind, collection in COLLECTIONS.items():
            folder = self.root / collection
            no_symlinks(folder)
            require(folder.is_dir(), f"Missing registry collection: {collection}")
            for entity in sorted(folder.iterdir()):
                no_symlinks(entity)
                require(entity.is_dir() and ID.fullmatch(entity.name), f"Invalid registry entity path: {entity}")
                for version in sorted(entity.iterdir()):
                    no_symlinks(version)
                    require(version.is_dir() and re.fullmatch(r"[1-9][0-9]*", version.name),
                            f"Invalid registry revision path: {version}")
                    document = self._read(version / "record.json")
                    result[reference(document)] = document
        self._namespace(result)
        for record in result.values():
            self._validate_references(record, result)
        return result

    @staticmethod
    def _namespace(records):
        names = {}
        for record in records.values():
            entity = (record["kind"], record["id"])
            for name in [record["id"], *record["aliases"]]:
                require(name not in names or names[name] == entity, f"ID/alias collision: {name}")
                names[name] = entity
        return names

    @staticmethod
    def _resolve(ref, records, expected=None):
        require(isinstance(ref, str) and ref, "Reference must be a nonempty string")
        if REF.fullmatch(ref):
            require(ref in records, f"Unknown revision: {ref}")
            resolved = ref
        else:
            kind = None
            if ":" in ref:
                kind, ref = ref.split(":", 1)
                require(kind in COLLECTIONS, "Unknown reference kind")
            require("@" not in ref and ALIAS.fullmatch(ref), "Invalid alias/reference")
            entities = Registry._namespace(records)
            require(ref in entities, f"Unknown ID/alias: {ref}")
            entity = entities[ref]
            require(kind is None or entity[0] == kind, f"Wrong reference kind for {ref}")
            matching = [r for r in records.values() if (r["kind"], r["id"]) == entity]
            resolved = reference(max(matching, key=lambda r: r["revision"]))
        require(expected is None or records[resolved]["kind"] == expected, f"Expected {expected}: {resolved}")
        return resolved

    def resolve(self, ref, expected=None):
        with self._lock():
            return self._resolve(ref, self._records_locked(), expected)

    def show(self, ref):
        with self._lock():
            records = self._records_locked()
            return copy.deepcopy(records[self._resolve(ref, records)])

    def record_path(self, ref):
        return self._path(self.resolve(ref))

    def attachment_path(self, ref, relative):
        document = self.show(ref)
        require(relative in [item["path"] for item in document["attachments"]], "Attachment not in record")
        return self._path(reference(document)).parent / safe_relative(relative)

    @staticmethod
    def _validate_metadata(record):
        require(record.get("status") in {"draft", "defined"}, "status must be draft or defined")
        require(isinstance(record.get("name"), str) and record["name"].strip(), "name must be nonempty")
        for field in ("aliases", "tags", "parents"):
            strings(record.get(field), field)
        require(all(ALIAS.fullmatch(alias) for alias in record["aliases"]), "Invalid alias")
        require(isinstance(record.get("notes"), str), "notes must be a string")
        require(isinstance(record.get("provenance"), dict), "provenance must be an object")
        for parent in record["parents"]:
            pinned_parts(parent)
        json_bytes(record)

    def _normalize(self, document, records, revision):
        require(isinstance(document, dict), "Record must be a JSON object")
        require(not set(document) - USER_FIELDS, f"Unknown or managed fields: {sorted(set(document) - USER_FIELDS)}")
        require(type(document.get("schema", SCHEMA)) is int and document.get("schema", SCHEMA) == SCHEMA,
                "Unsupported schema")
        kind, ident = document.get("kind"), document.get("id")
        require(kind in COLLECTIONS, "kind must be construct, monomer or assembly")
        require(isinstance(ident, str) and ID.fullmatch(ident), "id must be lowercase letters/digits/_/- and start with a letter")
        output = copy.deepcopy(document)
        output.update(schema=SCHEMA, kind=kind, id=ident, revision=revision, created_at=now())
        for key, default in {"name": ident, "aliases": [], "tags": [], "notes": "", "parents": [],
                             "status": "draft", "provenance": {}}.items():
            output.setdefault(key, default)
        output["parents"] = [self._resolve(parent, records) for parent in output["parents"]]
        def pin(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"monomer_ref", "construct_ref"}:
                        value[key] = self._resolve(child, records, key.removesuffix("_ref"))
                    elif key in {"monomer_refs", "construct_refs"}:
                        require(isinstance(child, list), f"{key} must be a list")
                        value[key] = [self._resolve(x, records, key.removesuffix("_refs")) for x in child]
                    else:
                        pin(child)
            elif isinstance(value, list):
                for item in value:
                    pin(item)
        pin(output.get("identity"))
        self._validate_metadata(output)
        validate_identity(output)
        self._validate_references(output, records)
        candidates = dict(records)
        candidates[reference(output)] = output
        self._namespace(candidates)
        return output

    @staticmethod
    def _validate_references(record, records):
        for parent in record["parents"]:
            require(parent in records, f"Missing parent revision: {parent}")
        for _, ref in reference_values(record["identity"]):
            require(ref in records, f"Missing referenced revision: {ref}")
        if record["kind"] == "assembly":
            chains = {item["chain_id"]: records[item["construct_ref"]]
                      for item in record["identity"]["components"]}
            for bond in record["identity"].get("bonds", []):
                validate_bond(bond, chains)

    @staticmethod
    def _copy_attachment(source, destination):
        no_symlinks(source, regular=True)
        with open(source, "rb") as incoming, open(destination, "xb") as outgoing:
            os.chmod(destination, 0o600)
            before = os.fstat(incoming.fileno())
            h, size = hashlib.sha256(), 0
            for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                outgoing.write(chunk)
                h.update(chunk)
                size += len(chunk)
            after = os.fstat(incoming.fileno())
            require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "Attachment changed during import")
            outgoing.flush()
            os.fsync(outgoing.fileno())
        return {"path": "attachments/" + destination.name, "sha256": h.hexdigest(), "bytes": size}

    def _publish(self, output, attachments, records):
        require(isinstance(attachments, dict), "attachments must map names to source paths")
        destination = self._path(reference(output)).parent
        no_symlinks(destination)
        require(not destination.exists(), "Revision already exists")
        staging = self.root / ".staging"
        no_symlinks(staging)
        temporary = Path(tempfile.mkdtemp(prefix="record-", dir=staging))
        try:
            (temporary / "attachments").mkdir(mode=0o700)
            output["attachments"] = []
            for name, source in sorted(attachments.items()):
                relative = safe_relative(name)
                require(len(relative.parts) == 1, "Attachment names must be plain filenames")
                require(name != "record.json", "Reserved attachment name")
                receipt = self._copy_attachment(Path(source), temporary / "attachments" / name)
                output["attachments"].append(receipt)
            if output["identity"].get("structure_file"):
                require(output["identity"]["structure_file"] in [x["path"] for x in output["attachments"]],
                        "structure_file is missing its attachment")
            output["sha256"] = digest_json(output)
            write_json(temporary / "record.json", output)
            fsync_directory(temporary / "attachments")
            fsync_directory(temporary)
            destination.parent.mkdir(mode=0o700, exist_ok=True)
            fsync_directory(destination.parent.parent)
            os.rename(temporary, destination)
            fsync_directory(destination.parent)
            records[reference(output)] = output
            self._reindex_locked(records)
            return copy.deepcopy(output)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def import_record(self, document, attachments=None):
        require(isinstance(document, dict), "Record must be a JSON object")
        with self._lock(exclusive=True):
            records = self._records_locked()
            kind, ident = document.get("kind"), document.get("id")
            require(not any(r["kind"] == kind and r["id"] == ident for r in records.values()),
                    "ID already exists; use revise to create an immutable revision")
            output = self._normalize(document, records, 1)
            return self._publish(output, attachments or {}, records)

    def revise(self, ref, patch, attachments=None):
        require(isinstance(patch, dict), "Revision patch must be an object")
        require(not set(patch) - USER_FIELDS, "Revision patch contains unknown/managed fields")
        with self._lock(exclusive=True):
            records = self._records_locked()
            old = records[self._resolve(ref, records)]
            latest = max(r["revision"] for r in records.values() if
                         (r["kind"], r["id"]) == (old["kind"], old["id"]))
            require(old["revision"] == latest, "Cannot revise a stale revision; start from the current revision")
            require(patch.get("kind", old["kind"]) == old["kind"] and patch.get("id", old["id"]) == old["id"],
                    "Revision cannot change kind/id; import a new record with a parent reference")
            document = {key: copy.deepcopy(value) for key, value in old.items() if key in USER_FIELDS}
            document.update(copy.deepcopy(patch))
            document["parents"] = list(dict.fromkeys([*document.get("parents", []), reference(old)]))
            output = self._normalize(document, records, latest + 1)
            sources = {Path(item["path"]).name: self._path(reference(old)).parent / item["path"]
                       for item in old["attachments"]}
            sources.update(attachments or {})
            return self._publish(output, sources, records)

    def list(self, *, kind=None, molecule_type=None, tag=None, all_revisions=False):
        require(kind is None or kind in COLLECTIONS, "Unknown kind filter")
        with self._lock():
            records = list(self._records_locked().values())
            if not all_revisions:
                latest = {}
                for record in records:
                    entity = (record["kind"], record["id"])
                    if entity not in latest or latest[entity]["revision"] < record["revision"]:
                        latest[entity] = record
                records = list(latest.values())
            return [{"ref": reference(r), "name": r["name"], "kind": r["kind"], "status": r["status"],
                     "molecule_type": r["identity"].get("molecule_type"), "aliases": r["aliases"],
                     "tags": r["tags"], "sha256": r["sha256"]}
                    for r in sorted(records, key=lambda r: (r["kind"], r["id"], r["revision"]))
                    if (kind is None or r["kind"] == kind) and
                    (molecule_type is None or r["identity"].get("molecule_type") == molecule_type) and
                    (tag is None or tag in r["tags"])]

    def snapshot(self, ref):
        with self._lock():
            records = self._records_locked()
            source_ref = self._resolve(ref, records)
            source = records[source_ref]
            require(source["kind"] in {"construct", "assembly"}, "Snapshot requires a construct or assembly")
            self._validate_references(source, records)
            if source["kind"] == "construct":
                components = [{"chain_id": "A", "construct_ref": source_ref}]
                bonds = []
            else:
                components = source["identity"]["components"]
                bonds = source["identity"].get("bonds", [])
            monomers, visiting = {}, set()
            def visit(record):
                ref = reference(record)
                if ref in visiting:
                    return
                visiting.add(ref)
                self._validate_references(record, records)
                for kind, child_ref in reference_values(record["identity"]):
                    if kind == "monomer_ref":
                        monomers[child_ref] = copy.deepcopy(records[child_ref])
                        visit(records[child_ref])
            resolved = []
            for component in components:
                record = records[component["construct_ref"]]
                visit(record)
                resolved.append({**copy.deepcopy(component), "record": copy.deepcopy(record)})
            visit(source)
            output = {"schema": SCHEMA, "kind": "resolved-assembly", "name": source["name"],
                      "source_ref": source_ref, "components": resolved, "bonds": copy.deepcopy(bonds),
                      "monomers": dict(sorted(monomers.items())),
                      "provenance": {"registry_source_ref": source_ref, "source_record_sha256": source["sha256"]}}
            if source["kind"] == "assembly":
                output["assembly_record"] = copy.deepcopy(source)
            output["sha256"] = digest_json(output)
            return output

    def verify(self, ref=None):
        with self._lock():
            records = self._records_locked()
            for record in records.values():
                self._validate_references(record, records)
            selected = [records[self._resolve(ref, records)]] if ref else list(records.values())
            return {"schema": SCHEMA, "verified": True, "records": len(selected),
                    "attachments": sum(len(r["attachments"]) for r in selected),
                    "references": [reference(r) for r in selected]}

    def _reindex_locked(self, records):
        path = self.root / "index.sqlite3"
        no_symlinks(path)
        fd, temporary = tempfile.mkstemp(prefix=".index-", dir=self.root)
        os.close(fd)
        try:
            db = sqlite3.connect(temporary)
            try:
                db.executescript("""
                    PRAGMA user_version=1;
                    CREATE TABLE records(ref TEXT PRIMARY KEY, kind TEXT, id TEXT, revision INTEGER,
                                         name TEXT, molecule_type TEXT, status TEXT, sha256 TEXT);
                    CREATE TABLE aliases(alias TEXT PRIMARY KEY, kind TEXT, id TEXT);
                    CREATE TABLE tags(ref TEXT, tag TEXT, PRIMARY KEY(ref,tag));
                """)
                for record in records.values():
                    ref = reference(record)
                    db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?)", (ref, record["kind"], record["id"],
                               record["revision"], record["name"], record["identity"].get("molecule_type"),
                               record["status"], record["sha256"]))
                    db.executemany("INSERT INTO tags VALUES(?,?)", [(ref, tag) for tag in record["tags"]])
                db.executemany("INSERT INTO aliases VALUES(?,?,?)",
                               [(alias, *entity) for alias, entity in self._namespace(records).items()])
                db.commit()
            finally:
                db.close()
            with open(temporary, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            fsync_directory(self.root)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def reindex(self):
        with self._lock(exclusive=True):
            records = self._records_locked()
            for record in records.values():
                self._validate_references(record, records)
            self._reindex_locked(records)
            return {"schema": SCHEMA, "indexed": len(records)}

    def export_snapshot(self, destination):
        destination = Path(destination).absolute()
        no_symlinks(destination)
        require(destination.parent.is_dir(), "Backup destination parent must exist")
        require(not destination.is_relative_to(self.root), "Write backups outside the live registry")
        with self._lock():
            records = self._records_locked()
            for record in records.values():
                self._validate_references(record, records)
            files = []
            for ref, record in sorted(records.items()):
                record_path = self._path(ref)
                for source in [record_path, *(record_path.parent / a["path"] for a in record["attachments"])]:
                    files.append({"path": str(source.relative_to(self.root)), "bytes": source.stat().st_size,
                                  "sha256": file_digest(source)})
            manifest = {"schema": SCHEMA, "kind": "registry-backup", "created_at": now(),
                        "records": len(records), "files": files}
            manifest["sha256"] = digest_json(manifest)
            fd, temporary = tempfile.mkstemp(prefix="." + destination.name + ".", dir=destination.parent)
            os.close(fd)
            try:
                with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                    for item in files:
                        source = self.root / item["path"]
                        entry = tarfile.TarInfo(item["path"])
                        entry.size, entry.mode, entry.mtime = item["bytes"], 0o600, 0
                        with source.open("rb") as incoming:
                            archive.addfile(entry, incoming)
                    data = json_bytes(manifest) + b"\n"
                    entry = tarfile.TarInfo("manifest.json")
                    entry.size, entry.mode = len(data), 0o600
                    archive.addfile(entry, io.BytesIO(data))
                verified = verify_backup(temporary)
                with open(temporary, "rb") as handle:
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                fsync_directory(destination.parent)
                return {"schema": SCHEMA, "path": str(destination), "sha256": file_digest(destination),
                        "bytes": destination.stat().st_size, "manifest_sha256": manifest["sha256"],
                        "records": verified["records"]}
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


def verify_backup(path):
    """Verify a bounded, flat-file archive without extracting or trusting paths."""
    no_symlinks(path, regular=True)
    with tarfile.open(path, "r:gz") as archive:
        contents = {}
        manifest = None
        for entry in archive:
            relative = safe_relative(entry.name)
            require(entry.isfile() and not entry.issym() and not entry.islnk(), "Backup may contain only regular files")
            require(entry.name not in contents, "Duplicate backup member")
            if entry.name != "manifest.json":
                require(relative.parts[0] in COLLECTIONS.values() and len(relative.parts) in {4, 5},
                        "Unexpected backup path")
                require(ID.fullmatch(relative.parts[1]) and re.fullmatch(r"[1-9][0-9]*", relative.parts[2]),
                        "Invalid backup revision path")
                require((len(relative.parts) == 4 and relative.parts[3] == "record.json") or
                        (len(relative.parts) == 5 and relative.parts[3] == "attachments"), "Unexpected backup file")
            h, size = hashlib.sha256(), 0
            with archive.extractfile(entry) as incoming:
                if entry.name == "manifest.json":
                    require(entry.size <= 64 * 1024 * 1024, "Backup manifest is unreasonably large")
                    raw = incoming.read()
                    manifest = parse_json(raw)
                    h.update(raw)
                    size = len(raw)
                else:
                    for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                        h.update(chunk)
                        size += len(chunk)
            require(size == entry.size, "Truncated backup member")
            contents[entry.name] = {"path": entry.name, "bytes": size, "sha256": h.hexdigest()}
        require(isinstance(manifest, dict), "Backup manifest missing")
        verify_document(manifest)
        require(manifest.get("schema") == SCHEMA and manifest.get("kind") == "registry-backup", "Invalid backup manifest")
        expected = manifest.get("files")
        require(isinstance(expected, list), "Invalid backup file manifest")
        require(all(isinstance(x, dict) and isinstance(x.get("path"), str) for x in expected), "Malformed backup file receipt")
        require(len(expected) == len({x["path"] for x in expected}), "Duplicate backup manifest receipt")
        contents.pop("manifest.json")
        require({x["path"]: x for x in expected} == contents, "Backup contents differ from manifest")
        count = sum(x.endswith("/record.json") for x in contents)
        require(type(manifest.get("records")) is int and manifest["records"] == count, "Backup record count mismatch")
        return {"schema": SCHEMA, "verified": True, "records": count, "files": len(contents),
                "manifest_sha256": manifest["sha256"]}


def restore_backup(path, destination):
    """Restore and validate in a sibling staging directory, then publish whole.

    destination must be absent or an empty directory. No archive extraction API
    is used: verified regular file contents are streamed into exclusive files.
    """
    path, destination = Path(path).absolute(), Path(destination).absolute()
    no_symlinks(path, regular=True)
    no_symlinks(destination)
    require(destination.parent.is_dir(), "Restore parent directory must exist")
    require(not destination.exists() or destination.is_dir() and not any(destination.iterdir()),
            "Restore destination must be absent or an empty directory")
    require(not path.is_relative_to(destination), "Backup cannot be inside its restore destination")
    archive_sha256 = file_digest(path)
    verified = verify_backup(path)
    temporary = Path(tempfile.mkdtemp(prefix="." + destination.name + ".restore-", dir=destination.parent))
    try:
        entries = set()
        with tarfile.open(path, "r:gz") as archive:
            for entry in archive:
                relative = safe_relative(entry.name)
                require(entry.isfile() and entry.name not in entries, "Invalid or duplicate restore member")
                entries.add(entry.name)
                if entry.name == "manifest.json":
                    continue
                require(relative.parts[0] in COLLECTIONS.values() and len(relative.parts) in {4, 5},
                        "Unexpected restore path")
                require(ID.fullmatch(relative.parts[1]) and re.fullmatch(r"[1-9][0-9]*", relative.parts[2]),
                        "Invalid restore revision path")
                require((len(relative.parts) == 4 and relative.parts[3] == "record.json") or
                        (len(relative.parts) == 5 and relative.parts[3] == "attachments"), "Unexpected restore file")
                target = temporary / relative
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.extractfile(entry) as incoming, target.open("xb") as outgoing:
                    os.chmod(target, 0o600)
                    shutil.copyfileobj(incoming, outgoing)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
        require(file_digest(path) == archive_sha256, "Backup changed during restoration")
        restored = Registry(temporary)
        restored.init()
        check = restored.verify()
        require(check["records"] == verified["records"], "Restored record count differs from backup")
        for parent, _, _ in os.walk(temporary, topdown=False):
            fsync_directory(parent)
        if destination.exists():
            # rmdir is intentional: it fails rather than deleting newly added data.
            destination.rmdir()
        os.rename(temporary, destination)
        fsync_directory(destination.parent)
        return {"schema": SCHEMA, "restored": True, "destination": str(destination),
                "records": check["records"], "archive_sha256": archive_sha256,
                "manifest_sha256": verified["manifest_sha256"]}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def fasta_document(path, molecule_type, ident):
    no_symlinks(path, regular=True)
    require(molecule_type in ALPHABETS, "FASTA import requires explicit protein, dna or rna type")
    lines = Path(path).read_text().splitlines()
    headers = [i for i, line in enumerate(lines) if line.startswith(">")]
    require(len(headers) == 1 and not any(x.strip() for x in lines[:headers[0]]),
            "Import one FASTA record at a time; use an assembly for multiple chains")
    sequence = "".join("".join(line.split()) for line in lines[headers[0] + 1:]).upper()
    canonical = {"protein": set("ACDEFGHIKLMNPQRSTVWY"), "dna": set("ACGT"), "rna": set("ACGU")}
    status = "defined" if sequence and set(sequence) <= canonical[molecule_type] else "draft"
    return {"kind": "construct", "id": ident, "name": lines[headers[0]][1:].strip() or ident,
            "status": status, "identity": {"molecule_type": molecule_type, "sequence": sequence},
            "provenance": {"import_format": "fasta", "original_filename": Path(path).name,
                           "sequence_formatting": "FASTA whitespace removed; letter case converted to uppercase",
                           "interpretation": "Standard linear polymer; unannotated residues and linkages use standard conventions; noncanonical or ambiguous symbols remain draft"}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("BIO_LIBRARY_ROOT", DEFAULT_ROOT))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    imp = commands.add_parser("import")
    source = imp.add_mutually_exclusive_group(required=True)
    source.add_argument("--json", type=Path)
    source.add_argument("--fasta", type=Path)
    source.add_argument("--smiles")
    source.add_argument("--sdf", type=Path)
    imp.add_argument("--type", choices=["protein", "dna", "rna"])
    imp.add_argument("--id")
    for command in [imp]:
        command.add_argument("--name")
        command.add_argument("--alias", action="append", default=[])
        command.add_argument("--tag", action="append", default=[])
        command.add_argument("--notes")
        command.add_argument("--status", choices=["draft", "defined"])
    imp.add_argument("--attachment", action="append", default=[], metavar="NAME=FILE")
    rev = commands.add_parser("revise")
    rev.add_argument("ref")
    rev.add_argument("--json", required=True, type=Path)
    rev.add_argument("--attachment", action="append", default=[], metavar="NAME=FILE")
    listing = commands.add_parser("list")
    listing.add_argument("--kind", choices=list(COLLECTIONS))
    listing.add_argument("--type", choices=sorted(MOLECULE_TYPES))
    listing.add_argument("--tag")
    listing.add_argument("--all-revisions", action="store_true")
    commands.add_parser("show").add_argument("ref")
    commands.add_parser("verify").add_argument("ref", nargs="?")
    commands.add_parser("reindex")
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("ref")
    snapshot.add_argument("--out", type=Path)
    commands.add_parser("export-snapshot").add_argument("--out", required=True, type=Path)
    commands.add_parser("verify-backup").add_argument("file", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("file", type=Path)
    restore.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args(argv)
    registry = Registry(args.root)
    if args.command == "init":
        result = registry.init()
    elif args.command in {"import", "revise"}:
        attachments = {}
        for item in args.attachment:
            require("=" in item, "--attachment must be NAME=FILE")
            name, path = item.split("=", 1)
            require(name not in attachments, "Duplicate attachment name")
            attachments[name] = Path(path)
        if args.command == "revise":
            no_symlinks(args.json, regular=True)
            attachments.setdefault("revision-source.json", args.json)
            result = registry.revise(args.ref, load_json(args.json), attachments)
        else:
            if args.json:
                no_symlinks(args.json, regular=True)
                document = load_json(args.json)
                require("source.json" not in attachments, "source.json is reserved for the imported original")
                attachments["source.json"] = args.json
            else:
                require(args.id is not None, "--id is required for FASTA/SMILES/SDF import")
                if args.fasta:
                    document = fasta_document(args.fasta, args.type, args.id)
                    require("source.fasta" not in attachments, "source.fasta is reserved for the imported original")
                    attachments["source.fasta"] = args.fasta
                elif args.sdf:
                    no_symlinks(args.sdf, regular=True)
                    require(args.sdf.stat().st_size > 0, "SDF attachment is empty")
                    document = {"kind": "construct", "id": args.id,
                                "identity": {"molecule_type": "small_molecule", "structure_format": "sdf",
                                             "structure_file": "attachments/source.sdf"},
                                "provenance": {"import_format": "sdf", "original_filename": args.sdf.name,
                                               "interpretation": "Original SDF retained; chemical graph not yet validated"}}
                    require("source.sdf" not in attachments, "source.sdf is reserved for the imported original")
                    attachments["source.sdf"] = args.sdf
                else:
                    document = {"kind": "construct", "id": args.id,
                                "identity": {"molecule_type": "small_molecule", "smiles": args.smiles},
                                "provenance": {"import_format": "smiles", "interpretation": "Original SMILES retained verbatim; chemical graph not yet validated"}}
            require(isinstance(document, dict), "Input JSON must be a record object")
            for key in ("id", "name", "notes", "status"):
                if getattr(args, key) is not None:
                    document[key] = getattr(args, key)
            if args.alias:
                document["aliases"] = args.alias
            if args.tag:
                document["tags"] = args.tag
            result = registry.import_record(document, attachments)
    elif args.command == "list":
        result = registry.list(kind=args.kind, molecule_type=args.type, tag=args.tag, all_revisions=args.all_revisions)
    elif args.command == "show":
        result = registry.show(args.ref)
    elif args.command == "verify":
        result = registry.verify(args.ref)
    elif args.command == "reindex":
        result = registry.reindex()
    elif args.command == "snapshot":
        result = registry.snapshot(args.ref)
        if args.out:
            atomic_json(args.out, result)
    elif args.command == "export-snapshot":
        result = registry.export_snapshot(args.out)
    elif args.command == "restore":
        result = restore_backup(args.file, args.destination)
    else:
        result = verify_backup(args.file)
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except (Error, OSError, EOFError, UnicodeError, json.JSONDecodeError, sqlite3.Error, tarfile.TarError) as exc:
        print(f"bio-library: {exc}", file=sys.stderr)
        sys.exit(2)
