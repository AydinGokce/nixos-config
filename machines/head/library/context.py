"""Export hash-verified research inputs and a separate editable assessment area.

This module uses the standard library only. Exporting a context runs no models,
creates no research conclusions, and does not authorize resource launches.
"""
from __future__ import annotations

import copy
import contextvars
import ctypes
import errno
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
from contextlib import contextmanager

_spec = importlib.util.spec_from_file_location('_bio_context_registry', Path(__file__).with_name('registry.py'))
r = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r)
Error = r.Error
require = r.require
MANIFEST = 'context-manifest.json'
MAX_ARCHIVE_BYTES = 512 * 1024**2
MAX_TAR_BYTES = 1024 * 1024**2
MAX_FILE_BYTES = 256 * 1024**2
MAX_JSON_BYTES = 16 * 1024**2
MAX_FILES = 10000
MAX_METADATA_BYTES = 64 * 1024**2
_write_budget = contextvars.ContextVar('context_export_bytes', default=None)

WORKFLOW = '''# Research assessment workflow

Read project.md, members.md, and the descriptions of every relevant exact
construct/assembly revision. Treat the purpose documents as research intent and
hypotheses, not established function or new resource authorization. Missing or
scaffold descriptions are listed in context-manifest.json. State the missing
information explicitly; do not invent objectives, acceptance thresholds or goals.

1. Verify this workspace with bio-library context-verify PATH before using it.
   Keep inputs/, descriptions/, project.md and the supplied templates unchanged.
   Put plans, notes, proposed revisions, results and assessments under analysis/.
2. Map each stated success criterion to the actual capabilities and limitations
   of the chosen model and input adapter. Check supported chemistry, necessary
   MSAs, model/version availability and applicable output validation. A criterion
   outside those capabilities needs an appropriate experiment or other evidence.
3. Plan suitable positive/negative controls, comparisons, replicates or seeds,
   and assays before drawing conclusions. Record why each test can address its
   criterion, with metrics, thresholds and conditions supplied by the research
   brief. Export itself performs no inference. Continue only within the user's
   active task and existing authorization for resources and external services.
4. Preserve the exact project, construct, assembly and monomer revisions, source
   hashes, model/checkpoint, input adapter, MSA source/database version and files,
   settings, seeds, software, raw outputs, output validation and evidence hashes.
   The input registry includes all dependency/parent revisions. For later
   compilation, make a writable copy of inputs/registry under analysis/ and
   initialize/reindex that copy with the current library tooling; never reindex
   or edit the frozen input registry. Document any newly published revisions.
5. Assess each criterion separately as prediction-supported, contradicted,
   inconclusive, or needs-experiment. Link every finding to exact evidence and
   identify uncertainty and conflicting results. Confidence scores, plausible
   structures and passed chemical checks do not demonstrate intended biological
   function, binding, activity or experimental success.
6. Use assessment-template.md/json as scaffolds. They contain no conclusions.
   Do not replace missing measurements with expected outcomes or turn an
   untested hypothesis into a completed assessment.
'''

ASSESSMENT_MD = '''# Assessment — not yet performed

## Scope and exact input revisions

Record the project and member references, context manifest hash, criteria and
conditions assessed. List missing or incomplete purpose descriptions.

## Criteria and capability mapping

For each supplied criterion: intended function, metric/threshold/conditions,
model or assay that can test it, capabilities/limits, and required evidence.

## Planned controls and tests

Record positive/negative controls, comparisons, replicates/seeds and assays.
No experiment or prediction has been run by this template.

## Evidence and provenance

Record exact input revisions, model/checkpoint/adapter, MSA source/database and
files, settings/seeds, software, raw result paths and hashes, output validation,
observations and uncertainties.

## Findings by criterion

Use prediction-supported, contradicted, inconclusive or needs-experiment, with
specific evidence. Leave unassessed criteria unassessed. Prediction confidence
is not demonstrated function; distinguish hypotheses from observations.

## Limitations and next tests

List remaining uncertainty and experiments needed. Do not invent success claims.
'''


def _relative(value):
    path = r.safe_relative(value)
    require(path.parts and len(value.encode('utf-8')) <= 4096 and len(path.parts) <= 20 and
            not any(ord(character) < 32 for character in value), 'Unsafe context path')
    return path


def _write(path, data):
    require(len(data) <= MAX_FILE_BYTES, 'Context file exceeds its bounded file size')
    budget = _write_budget.get()
    if budget is not None:
        budget[0] += len(data)
        require(budget[0] <= MAX_TAR_BYTES-MAX_FILES*8192-10240, 'Context export exceeds its total size bound')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as handle:
        os.chmod(path, 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _json(path, value):
    data = (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)+'\n').encode('utf-8')
    require(len(data) <= MAX_JSON_BYTES, 'Context JSON exceeds its size bound')
    _write(path, data)


def _receipt(path):
    r.no_symlinks(path, regular=True)
    size = Path(path).stat().st_size
    require(size <= MAX_FILE_BYTES, 'Context file exceeds the bounded file size')
    return {'bytes': size, 'sha256': r.file_digest(path)}


def _destination(path):
    path = Path(path).absolute()
    r.no_symlinks(path)
    require(not path.exists(), 'Context destination already exists: '+str(path))
    require(path.parent.is_dir(), 'Context destination parent must exist')
    return path


def _publish_directory(source, destination):
    # Linux is used by both the head and Nix workstation. RENAME_NOREPLACE
    # publishes atomically without replacing even a concurrently created empty
    # directory. A plain rename would silently replace such a destination.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    require(rename is not None, 'Atomic context publication requires renameat2')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise Error('Context destination already exists: '+str(destination))
        raise OSError(code, os.strerror(code), str(destination))
    r.fsync_directory(Path(destination).parent)


def _dependencies(record):
    result = set(record['parents'])
    result.update(ref for _, ref in r.reference_values(record['identity']))
    if record['kind'] == 'project':
        result.update(member['source_ref'] for member in record['identity']['members'])
    return result


def _record_path(ref):
    kind, identifier, revision = r.pinned_parts(ref)
    return f'inputs/registry/{r.COLLECTIONS[kind]}/{identifier}/{revision}/record.json'


def _closure(registry, snapshot):
    records = {}
    pending = [snapshot['project_ref']]
    while pending:
        ref = pending.pop()
        if ref in records:
            continue
        require(len(records) < MAX_FILES, 'Context dependency closure is too large')
        record = registry.show(ref)
        r.verify_document(record)
        require(r.reference(record) == ref, 'Context dependency changed its pinned reference')
        records[ref] = record
        pending.extend(_dependencies(record)-records.keys())
    return records


def _assessment(snapshot):
    return {'schema': 1, 'status': 'unassessed', 'project_ref': snapshot['project_ref'],
            'project_snapshot_sha256': snapshot['sha256'],
            'member_refs': [member['source_ref'] for member in snapshot['members']],
            'criteria': [], 'capability_mapping': [], 'controls_and_assays': [],
            'runs_and_evidence': [], 'findings': [], 'limitations': [],
            'allowed_criterion_verdicts': ['prediction-supported', 'contradicted', 'inconclusive', 'needs-experiment'],
            'interpretation': 'No conclusions are supplied. Prediction confidence and chemical validity do not demonstrate intended function.'}


def _description(record, paths, stage, warnings):
    ref = r.reference(record)
    receipt = r.projects.description_receipt(record)
    relative = f'descriptions/{r.COLLECTIONS[record["kind"]]}/{record["id"]}/{record["revision"]}.md'
    result = {'ref': ref, 'path': relative, 'present': receipt['present']}
    if receipt['present']:
        source = paths[ref]['attachments'][receipt['path']]['path']
        raw = (stage/source).read_bytes()
        state = r.projects.markdown_state(raw)
        result.update(state=state, source=source, sha256=receipt['sha256'])
        if state == 'incomplete_scaffold':
            warnings.append({'ref': ref, 'code': 'incomplete_description', 'path': relative})
    else:
        raw = f'# Missing purpose description\n\nNo purpose description was supplied for {ref}. Do not infer intended function or success criteria.\n'.encode()
        result['state'] = 'missing'
        warnings.append({'ref': ref, 'code': 'missing_description', 'path': relative})
    _write(stage/relative, raw)
    return result


def export_directory(registry, ref, destination):
    destination = _destination(destination)
    require(not destination.is_relative_to(Path(registry.root).absolute()), 'Export context outside the live registry')
    snapshot = registry.project_snapshot(ref)
    r.verify_document(snapshot)
    records = _closure(registry, snapshot)
    stage = Path(tempfile.mkdtemp(prefix='.research-context-', dir=destination.parent))
    budget_token = _write_budget.set([0])
    try:
        paths = {}
        for record_ref, record in sorted(records.items()):
            relative = _record_path(record_ref)
            original = r.no_symlinks(registry.record_path(record_ref), regular=True)
            require(original.stat().st_size <= MAX_JSON_BYTES and r.load_json(original) == record, 'Original record changed during context export')
            _write(stage/relative, original.read_bytes())
            paths[record_ref] = {'path': relative, 'sha256': record['sha256'], 'attachments': {}}
            for attachment in record['attachments']:
                name = str(_relative(attachment['path']))
                target = str(Path(relative).parent/name)
                source = registry.attachment_path(record_ref, name)
                require(_receipt(source) == {key: attachment[key] for key in ('bytes', 'sha256')},
                        'Original attachment differs from its pinned record')
                _write(stage/target, Path(source).read_bytes())
                require(_receipt(stage/target) == _receipt(source), 'Original attachment changed during context export')
                paths[record_ref]['attachments'][name] = {'path': target, **{key: attachment[key] for key in ('bytes', 'sha256')}}
        _json(stage/'inputs/project-snapshot.json', snapshot)
        members, descriptions, warnings = [], {}, []
        for index, member in enumerate(snapshot['members']):
            relative = f'inputs/members/{index:04d}/snapshot.json'
            _json(stage/relative, member['snapshot'])
            members.append({'source_ref': member['source_ref'], 'role': member['role'],
                            'snapshot': relative, 'snapshot_sha256': member['snapshot']['sha256']})
            relevant = [records[member['source_ref']], *[part['record'] for part in member['snapshot']['components']]]
            for record in relevant:
                record_ref = r.reference(record)
                if record_ref not in descriptions:
                    descriptions[record_ref] = _description(record, paths, stage, warnings)
        project = snapshot['project_record']
        brief_path = paths[snapshot['project_ref']]['attachments'][project['identity']['objectives_file']]['path']
        brief = (stage/brief_path).read_bytes()
        state = r.projects.markdown_state(brief)
        if state == 'incomplete_scaffold':
            warnings.append({'ref': snapshot['project_ref'], 'code': 'incomplete_project_brief', 'path': 'project.md'})
        if not members:
            warnings.append({'ref': snapshot['project_ref'], 'code': 'no_project_members'})
        _write(stage/'project.md', brief)
        index = '# Exact project members\n\nProject: '+snapshot['project_ref']+'\n\n'
        for member in members:
            index += f'- `{member["source_ref"]}` — role: {member["role"] or "Not specified"}\n'
            index += f'  Description: [{member["source_ref"]}]({descriptions[member["source_ref"]]["path"]})\n'
        index += '\nDescriptions of member assemblies and their component constructs:\n\n'
        index += ''.join(f'- [{key}]({row["path"]}) — {row["state"]}\n' for key, row in sorted(descriptions.items()))
        _write(stage/'members.md', index.encode('utf-8'))
        _write(stage/'AGENTS.md', WORKFLOW.encode())
        _write(stage/'assessment-template.md', ASSESSMENT_MD.encode())
        _json(stage/'assessment-template.json', _assessment(snapshot))
        _write(stage/'analysis/README.md', b'Editable working area. Preserve exact evidence and provenance here. The initial assessment is unassessed; templates contain no findings.\n')
        _write(stage/'analysis/assessment.md', ASSESSMENT_MD.encode())
        _json(stage/'analysis/assessment.json', _assessment(snapshot))
        inventory = {str(path.relative_to(stage)): _receipt(path) for path in sorted(stage.rglob('*')) if path.is_file()}
        manifest = {'schema': 1, 'kind': 'research-context', 'created_at': r.now(),
                    'project_ref': snapshot['project_ref'], 'project_snapshot': 'inputs/project-snapshot.json',
                    'project_snapshot_sha256': snapshot['sha256'], 'project_brief_state': state,
                    'members': members, 'records': paths, 'descriptions': descriptions, 'warnings': warnings,
                    'files': {key: value for key, value in inventory.items() if not key.startswith('analysis/')},
                    'initial_analysis_files': {key: value for key, value in inventory.items() if key.startswith('analysis/')},
                    'mutable_roots': ['analysis/']}
        manifest['sha256'] = r.digest_json(manifest)
        _json(stage/MANIFEST, manifest)
        verify_directory(stage)
        _publish_directory(stage, destination)
        return manifest
    finally:
        _write_budget.reset(budget_token)
        if stage.exists():
            shutil.rmtree(stage)


def _validate_manifest(manifest):
    r.verify_document(manifest)
    require(type(manifest.get('schema')) is int and manifest['schema'] == 1 and manifest.get('kind') == 'research-context',
            'Invalid research context manifest')
    require(manifest.get('mutable_roots') == ['analysis/'], 'Unexpected mutable context area')
    require(manifest.get('project_snapshot') == 'inputs/project-snapshot.json', 'Unexpected project snapshot path')
    require(isinstance(manifest.get('files'), dict) and isinstance(manifest.get('initial_analysis_files'), dict), 'Context manifest needs file inventories')
    for field, mutable in (('files', False), ('initial_analysis_files', True)):
        for path, receipt in manifest[field].items():
            _relative(path)
            require(path != MANIFEST and path.startswith('analysis/') == mutable, 'Context inventory crosses its immutable/analysis boundary')
            require(isinstance(receipt, dict) and set(receipt) == {'bytes', 'sha256'} and
                    type(receipt['bytes']) is int and 0 <= receipt['bytes'] <= MAX_FILE_BYTES and
                    isinstance(receipt['sha256'], str) and len(receipt['sha256']) == 64 and
                    all(c in '0123456789abcdef' for c in receipt['sha256']), 'Invalid context file receipt')
    require(len(manifest['files'])+len(manifest['initial_analysis_files'])+1 <= MAX_FILES, 'Context contains too many files')
    require(sum(row['bytes'] for field in ('files','initial_analysis_files') for row in manifest[field].values()) <= MAX_TAR_BYTES,
            'Context exceeds its expanded size bound')


def _validate_semantics(manifest, read_json, read_bytes):
    records = {}
    expected_files = {'project.md', 'members.md', 'AGENTS.md', 'assessment-template.md', 'assessment-template.json', manifest['project_snapshot']}
    for ref, entry in manifest['records'].items():
        require(entry['path'] == _record_path(ref), 'Record path does not match its exact revision')
        require(entry['path'] in manifest['files'], 'Context record is outside its frozen inventory')
        expected_files.add(entry['path'])
        record = r.verify_document(read_json(entry['path']))
        require(r.reference(record) == ref and record['sha256'] == entry['sha256'], 'Context record reference mismatch')
        r.Registry._validate_metadata(record)
        if record['kind'] == 'project':
            r.projects.validate_identity(record['identity'])
        else:
            r.validate_identity(record)
        require(set(entry['attachments']) == {a['path'] for a in record['attachments']}, 'Context attachment closure mismatch')
        for attachment in record['attachments']:
            parts = _relative(attachment['path']).parts
            require(len(parts) == 2 and parts[0] == 'attachments', 'Unexpected original attachment path')
            path = str(Path(entry['path']).parent/str(_relative(attachment['path'])))
            expected_files.add(path)
            require(entry['attachments'][attachment['path']] == {'path': path, **{key: attachment[key] for key in ('bytes','sha256')}}
                    and manifest['files'].get(path) == {key: attachment[key] for key in ('bytes','sha256')}, 'Context attachment is not bound to its record')
        records[ref] = record
    needed, pending = set(), [manifest['project_ref']]
    while pending:
        ref = pending.pop()
        if ref in needed:
            continue
        require(ref in records, 'Context is missing a pinned dependency/parent revision: '+ref)
        needed.add(ref); pending.extend(_dependencies(records[ref])-needed)
    require(needed == set(records), 'Context includes unrelated records')
    r.Registry._namespace(records)
    for record in records.values():
        r.Registry._validate_references(record, records)
    project = records[manifest['project_ref']]
    require(project['kind'] == 'project', 'Context root must be a project')
    pure = object.__new__(r.Registry)
    members = [{**copy.deepcopy(member), 'snapshot': pure._snapshot_locked(member['source_ref'], records),
                'description': r.projects.description_receipt(records[member['source_ref']])}
               for member in project['identity']['members']]
    expected = {'schema': 1, 'kind': 'resolved-project', 'project_ref': manifest['project_ref'],
                'project_record': project, 'members': members}
    expected['sha256'] = r.digest_json(expected)
    require(read_json(manifest['project_snapshot']) == expected and expected['sha256'] == manifest['project_snapshot_sha256'],
            'Project snapshot differs from its exact record/member closure')
    require(len(manifest['members']) == len(members), 'Context member count mismatch')
    for index, (entry, member) in enumerate(zip(manifest['members'], members)):
        require(entry == {'source_ref': member['source_ref'], 'role': member['role'],
                          'snapshot': f'inputs/members/{index:04d}/snapshot.json', 'snapshot_sha256': member['snapshot']['sha256']},
                'Context member mapping changed')
        require(entry['snapshot'] in manifest['files'] and read_json(entry['snapshot']) == member['snapshot'], 'Context molecular snapshot changed')
        expected_files.add(entry['snapshot'])
    brief = manifest['records'][manifest['project_ref']]['attachments'][project['identity']['objectives_file']]
    require(manifest['files'].get('project.md') == {key: brief[key] for key in ('bytes','sha256')}, 'Readable project brief differs from its original attachment')
    state = r.projects.markdown_state(read_bytes(brief['path']))
    require(manifest['project_brief_state'] == state, 'Project brief completeness classification changed')
    warnings = []
    if state == 'incomplete_scaffold':
        warnings.append({'ref': manifest['project_ref'], 'code': 'incomplete_project_brief', 'path': 'project.md'})
    if not members:
        warnings.append({'ref': manifest['project_ref'], 'code': 'no_project_members'})
    described = {member['source_ref'] for member in members}
    described.update(part['construct_ref'] for member in members for part in member['snapshot']['components'])
    require(set(manifest['descriptions']) == described, 'Context purpose-description coverage changed')
    for ref in described:
        record = records[ref]
        receipt = r.projects.description_receipt(record)
        path = f'descriptions/{r.COLLECTIONS[record["kind"]]}/{record["id"]}/{record["revision"]}.md'
        expected_files.add(path)
        entry = {'ref': ref, 'path': path, 'present': receipt['present']}
        if receipt['present']:
            source = manifest['records'][ref]['attachments'][receipt['path']]['path']
            state = r.projects.markdown_state(read_bytes(source))
            entry.update(state=state, source=source, sha256=receipt['sha256'])
            require(manifest['files'].get(path) == manifest['files'].get(source), 'Readable purpose description differs from its original attachment')
            if state == 'incomplete_scaffold':
                warnings.append({'ref': ref, 'code': 'incomplete_description', 'path': path})
        else:
            entry['state'] = 'missing'
            warnings.append({'ref': ref, 'code': 'missing_description', 'path': path})
        require(manifest['descriptions'][ref] == entry, 'Context purpose-description receipt changed')
    require(sorted(manifest['warnings'], key=lambda row: (row['ref'], row['code'])) ==
            sorted(warnings, key=lambda row: (row['ref'], row['code'])), 'Context missing/incomplete purpose warnings changed')
    for relative in ('AGENTS.md','members.md','assessment-template.md','assessment-template.json'):
        require(relative in manifest['files'], 'Missing research workflow/template file')
    require(set(manifest['files']) == expected_files, 'Context frozen inventory includes an unrelated file')


def _verify_directory(path):
    root = r.no_symlinks(path)
    require(root.is_dir(), 'Context workspace is not a directory')
    manifest_path = r.no_symlinks(root/MANIFEST, regular=True)
    require(manifest_path.stat().st_size <= MAX_JSON_BYTES, 'Context manifest is too large')
    manifest = r.load_json(manifest_path)
    _validate_manifest(manifest)
    immutable = set()
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        if Path(directory) == root:
            analysis = r.no_symlinks(root/'analysis')
            require(analysis.is_dir(), 'Context analysis root must be a real directory')
            dirs.remove('analysis')  # Working data is outside the immutable verification scope.
        for name in [*dirs, *files]:
            item = Path(directory)/name
            r.no_symlinks(item)
            _relative(str(item.relative_to(root)))
            require(item.is_dir() or stat.S_ISREG(item.stat().st_mode), 'Context contains a nonregular file')
        for name in files:
            count += 1
            require(count <= MAX_FILES, 'Context contains too many files')
            item = Path(directory)/name
            relative = str(item.relative_to(root))
            if relative == MANIFEST or relative.startswith('analysis/'):
                continue
            immutable.add(relative)
            require(relative in manifest['files'] and _receipt(item) == manifest['files'][relative], 'Context frozen file checksum/inventory mismatch: '+relative)
    require(immutable == set(manifest['files']) and (root/'analysis').is_dir(), 'Context frozen input or analysis directory is missing')
    def read_json(relative):
        require(relative in manifest['files'] and manifest['files'][relative]['bytes'] <= MAX_JSON_BYTES, 'Context JSON exceeds its bound or inventory')
        return r.load_json(root/relative)
    def read_bytes(relative):
        require(relative in manifest['files'] and manifest['files'][relative]['bytes'] <= r.projects.MAX_MARKDOWN_BYTES, 'Purpose Markdown exceeds its bound or inventory')
        return (root/relative).read_bytes()
    _validate_semantics(manifest, read_json, read_bytes)
    return manifest


def verify_directory(path):
    try:
        return _verify_directory(path)
    except (KeyError, TypeError, AttributeError, UnicodeError) as exc:
        raise Error('Invalid context workspace: '+str(exc)) from exc


@contextmanager
def _open_archive(path):
    path = r.no_symlinks(path, regular=True)
    require(path.stat().st_size <= MAX_ARCHIVE_BYTES, 'Context archive exceeds its compressed size bound')
    # Bound decompression before tarfile interprets PAX/long-name metadata.
    with tempfile.TemporaryFile() as expanded:
        total = 0
        with gzip.open(path, 'rb') as source:
            while block := source.read(1024**2):
                total += len(block)
                require(total <= MAX_TAR_BYTES, 'Context archive exceeds its expanded size bound')
                expanded.write(block)
        expanded.seek(0)
        with tarfile.open(fileobj=expanded, mode='r:') as archive:
            yield archive


def _verify_archive(archive):
    inventory, metadata, markdown, entries = {}, {}, {}, []
    metadata_bytes, last_end = 0, 0
    for entry in archive:
        path = str(_relative(entry.name))
        require(entry.type in (tarfile.REGTYPE, tarfile.AREGTYPE) and not entry.sparse and
                not any(key.startswith('GNU.sparse') for key in entry.pax_headers), 'Context archive may contain only regular files')
        require(path not in inventory and len(entries) < MAX_FILES, 'Duplicate context archive member or file limit exceeded')
        require(0 <= entry.size <= MAX_FILE_BYTES, 'Context archive member exceeds its size bound')
        parts = _relative(path).parts
        capture_json = path in (MANIFEST, 'inputs/project-snapshot.json') or (
            len(parts) == 6 and parts[:2] == ('inputs', 'registry') and parts[-1] == 'record.json') or (
            len(parts) == 4 and parts[:2] == ('inputs', 'members') and parts[-1] == 'snapshot.json')
        capture_markdown = len(parts) == 7 and parts[:2] == ('inputs', 'registry') and parts[-2] == 'attachments' and parts[-1] in ('project.md', 'description.md')
        capture = capture_json or capture_markdown
        require(not capture or entry.size <= MAX_JSON_BYTES, 'Context JSON exceeds its size bound')
        if capture:
            metadata_bytes += entry.size
            require(metadata_bytes <= MAX_METADATA_BYTES, 'Context metadata exceeds its memory bound')
        digest, size, raw = hashlib.sha256(), 0, bytearray()
        with archive.extractfile(entry) as incoming:
            while block := incoming.read(1024**2):
                digest.update(block); size += len(block)
                if capture: raw.extend(block)
        require(size == entry.size, 'Truncated context archive member')
        inventory[path] = {'bytes': size, 'sha256': digest.hexdigest()}
        if capture_json: metadata[path] = r.parse_json(raw)
        if capture_markdown: markdown[path] = bytes(raw)
        entries.append(entry)
        last_end = max(last_end, entry.offset_data + ((entry.size+511)//512)*512)
    require(not any(str(parent) in inventory for path in inventory for parent in Path(path).parents),
            'Context archive file is also a parent directory')
    archive.fileobj.seek(last_end)
    while trailing := archive.fileobj.read(1024**2):
        require(not trailing.strip(b'\0'), 'Unexpected data after the context tar entries')
    require(MANIFEST in metadata, 'Context archive lacks its manifest')
    manifest = metadata[MANIFEST]
    _validate_manifest(manifest)
    require(set(inventory) == set(manifest['files']) | set(manifest['initial_analysis_files']) | {MANIFEST}, 'Context archive inventory mismatch')
    for field in ('files','initial_analysis_files'):
        require(all(inventory[path] == receipt for path, receipt in manifest[field].items()), 'Context archive checksum mismatch')
    _validate_semantics(manifest, lambda path: metadata[path], lambda path: markdown[path])
    return manifest, entries


def verify_archive(path):
    try:
        with _open_archive(path) as archive:
            return _verify_archive(archive)[0]
    except (tarfile.TarError, EOFError, UnicodeError, KeyError, TypeError) as exc:
        raise Error('Invalid context archive: '+str(exc)) from exc


def extract_archive(path, destination):
    destination = _destination(destination)
    stage = Path(tempfile.mkdtemp(prefix='.research-context-', dir=destination.parent))
    try:
        with _open_archive(path) as archive:
            manifest, entries = _verify_archive(archive)
            for entry in entries:
                target = stage/str(_relative(entry.name))
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.extractfile(entry) as source, target.open('xb') as output:
                    os.chmod(target, 0o600)
                    shutil.copyfileobj(source, output, 1024**2)
                    output.flush(); os.fsync(output.fileno())
        verify_directory(stage)
        _publish_directory(stage, destination)
        return manifest
    except (tarfile.TarError, EOFError, UnicodeError, KeyError, TypeError) as exc:
        raise Error('Invalid context archive: '+str(exc)) from exc
    finally:
        if stage.exists(): shutil.rmtree(stage)


def export_archive(registry, ref, path):
    path = _destination(path)
    require(not path.is_relative_to(Path(registry.root).absolute()), 'Export context outside the live registry')
    with tempfile.TemporaryDirectory(prefix='.research-context-export-', dir=path.parent) as temporary:
        root = Path(temporary)
        manifest = export_directory(registry, ref, root/'workspace')
        archive_path = root/'context.tar.gz'
        with tarfile.open(archive_path, 'w:gz', format=tarfile.PAX_FORMAT) as archive:
            for relative in sorted([*manifest['files'], *manifest['initial_analysis_files'], MANIFEST]):
                source = root/'workspace'/relative
                entry = tarfile.TarInfo(relative)
                entry.size, entry.mode, entry.mtime = source.stat().st_size, 0o600, 0
                with source.open('rb') as incoming:
                    archive.addfile(entry, incoming)
        os.chmod(archive_path, 0o600)
        verified = verify_archive(archive_path)
        require(verified == manifest, 'Published context archive differs from its verified workspace')
        with archive_path.open('rb') as handle: os.fsync(handle.fileno())
        os.link(archive_path, path)  # Atomic publication; never overwrites a destination.
        r.fsync_directory(path.parent)
        return manifest
