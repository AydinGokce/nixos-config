"""Shared protein structure associations; immutable assets and reversible visibility.

Predictions are discovered from exact retained library inputs, never names or
sequence guesses. Generic job/artifact ownership is not expanded by this API.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import math
from pathlib import Path
import re
import shutil
import tempfile

from .common import (Error, canonical, digest, file_sha, identifier, keys, now, number,
                     parse, require, safe_file, sha, string)
from .library_api import opened, pin

FIELD = 'protein_structures'
MAX_FILE = 32 * 1024 * 1024
MAX_FILES = 16
MAX_ASSETS = 128 * 1024 * 1024
MAX_ENTRIES = 256
MAX_CANDIDATES = 10000
FORMATS = {'pdb', 'cif', 'mmcif'}


def entry_id(value):
    require(isinstance(value, str) and re.fullmatch(r'[mp]-[a-f0-9]{32}', value), 'Invalid structure entry ID')
    return value


def catalog(record):
    curated = record.get('provenance', {}).get('workbench', {})
    require(isinstance(curated, dict), 'Reserved workbench provenance has an incompatible format', 'integrity')
    value = curated.get(FIELD)
    if value is None:
        return {'schema': 1, 'entries': {}, 'hidden': {}}
    require(isinstance(value, dict) and set(value) == {'schema', 'entries', 'hidden'} and
            value['schema'] == 1 and isinstance(value['entries'], dict) and isinstance(value['hidden'], dict),
            'Malformed protein structure catalog', 'integrity')
    require(len(value['entries']) <= MAX_ENTRIES and len(value['hidden']) <= 4096 and
            len(canonical(value)) <= 256 * 1024, 'Protein structure catalog exceeds limits', 'limit')
    receipts = {item['path']: item for item in record.get('attachments', [])}
    for ident, row in value['entries'].items():
        entry_id(ident)
        require(ident.startswith('m-') and isinstance(row, dict), 'Invalid manual structure catalog entry', 'integrity')
        keys(row, ('entry_id', 'label', 'format', 'size', 'sha256', 'attachment',
                   'source_ref', 'source_sequence_sha256', 'created_at', 'source'))
        require(row['entry_id'] == ident and row['format'] in FORMATS, 'Structure identity differs', 'integrity')
        sha(row['sha256']); sha(row['source_sequence_sha256'])
        string(row['label'], 'structure label', 200); string(row['source_ref'], 'structure source ref', 256)
        number(row['size'], 'structure size', 1, MAX_FILE)
        require(row['attachment'] == 'attachments/structure-' + row['sha256'] +
                ('.pdb' if row['format'] == 'pdb' else '.cif'), 'Invalid structure attachment name', 'integrity')
        receipt = receipts.get(row['attachment'])
        require(receipt is not None and receipt['sha256'] == row['sha256'] and receipt['bytes'] == row['size'],
                'Structure catalog does not match its immutable attachment', 'integrity')
        expected = 'm-' + digest({'family': row['source_ref'].split('@')[0], 'sha256': row['sha256'],
                                  'sequence_sha256': row['source_sequence_sha256']})[:32]
        require(ident == expected and row['source_ref'].split('@')[0] == pin(record).split('@')[0],
                'Structure source belongs to another protein', 'integrity')
    for ident, hidden in value['hidden'].items():
        entry_id(ident); require(type(hidden) is bool, 'Structure visibility must be boolean', 'integrity')
        require(not ident.startswith('m-') or ident in value['entries'], 'Hidden manual structure is missing', 'integrity')
    assets = {row['attachment']: row['size'] for row in value['entries'].values()}
    require(sum(assets.values()) <= MAX_ASSETS, 'Protein manual structure assets exceed 128 MiB', 'limit')
    return deepcopy(value)


def managed_attachments(record):
    return {row['attachment'] for row in catalog(record)['entries'].values()}


def ordinary_attachments(record):
    managed = managed_attachments(record)
    return [row for row in record.get('attachments', []) if row['path'] not in managed]


def preserve_catalog(old, document):
    value = catalog(old)
    curated = document.setdefault('provenance', {}).setdefault('workbench', {})
    if value['entries'] or value['hidden']:
        curated[FIELD] = value
    else:
        curated.pop(FIELD, None)


def _protein(module, registry, records, ref):
    selected = records[registry._resolve(ref, records, 'construct')]
    require(selected['identity'].get('molecule_type') == 'protein', 'Select a protein construct')
    resolved = module.effective_sequence(selected, records)
    return selected, resolved


def _sequence_sha(module, records, ref):
    record = records.get(ref)
    if not record or record['kind'] != 'construct' or record['identity'].get('molecule_type') != 'protein':
        return None
    return module.effective_sequence(record, records)['sequence_sha256']


def _protein_context(module, records, ref):
    record = records.get(ref)
    require(record is not None and record['kind'] == 'construct' and
            record['identity'].get('molecule_type') == 'protein', 'Associated protein source is missing', 'integrity')
    resolved = module.effective_sequence(record, records)
    parent = None
    if module.translation.is_derived(record):
        source = records[record['identity']['encoded_by']['construct_ref']]
        parent = {'ref': pin(source), 'sha256': source['sha256'],
                  'molecule_type': source['identity']['molecule_type'],
                  'molecular_form': source['identity'].get('molecular_form')}
    return {'ref': ref, 'sha256': record['sha256'], 'sequence_sha256': resolved['sequence_sha256'],
            'derivation_kind': resolved['kind'], 'parent': parent}


def _association(job, batch, records):
    document = batch.get('_request', {})
    inputs = [item for item in document.get('inputs', [])
              if document.get('mode') == 'assembly' or item.get('id') == job.get('input_id')]
    refs, shared = set(), bool(inputs)
    for item in inputs:
        source = item.get('source', {})
        ref = source.get('ref')
        record = records.get(ref) if isinstance(ref, str) else None
        exact = source.get('kind') == 'library' and record is not None and ref == pin(record)
        shared &= exact
        if not exact:
            continue
        if record['kind'] == 'construct':
            refs.add(ref)
        elif record['kind'] == 'assembly':
            refs.update(part['construct_ref'] for part in record['identity']['components'])
        else:
            shared = False
    return refs, shared


def _relation(binding, selected):
    return ('unresolved' if binding is None or selected is None else
            'same_library_sequence' if binding == selected else 'historical_library_sequence')


def _display_label(value):
    return ''.join(character for character in value if character.isprintable())[:200] or 'Structure'


def _saved_binders(module, registry, records, selected, resolved, curated, include_revisions):
    """Discover the candidate already published by binder.save, never its target.

    These records predate the gallery catalog. The original candidate receipt is
    sufficient without exposing its former actor's job or requiring that job to
    survive a library backup/restore. Subsequent sequence edits are not new saves.
    """
    family = pin(selected).split('@')[0]
    seen, rows = set(), []
    revisions = sorted((record for ref, record in records.items() if ref.split('@')[0] == family),
                       key=lambda record: record['revision'])
    for record in revisions:
        metadata = record.get('provenance', {}).get('bindcraft')
        if not isinstance(metadata, dict) or 'predicted_structure_sha256' not in metadata:
            continue
        structure_sha = sha(metadata.get('predicted_structure_sha256'))
        binding = sha(metadata.get('candidate_sequence_sha256'))
        identifier(metadata.get('job_id')); identifier(metadata.get('candidate_id'))
        ident = 'p-' + digest({'family': family, 'binding': 'bindcraft_saved',
                              'sha256': structure_sha, 'sequence_sha256': binding})[:32]
        if ident in seen or _sequence_sha(module, records, pin(record)) != binding:
            continue
        # Pin the earliest matching saved revision, even when viewing a later one.
        seen.add(ident)
        if not include_revisions and pin(record) != pin(selected):
            continue
        receipts = {item['path']: item for item in record.get('attachments', [])}
        structure = receipts.get('attachments/predicted-complex.pdb')
        proof = receipts.get('attachments/bindcraft-provenance.json')
        require(structure is not None and structure['sha256'] == structure_sha and proof is not None and
                metadata.get('provenance_attachment') == proof['path'] and
                metadata.get('provenance_sha256') == proof['sha256'],
                'Saved BindCraft candidate attachment binding differs', 'integrity')
        path = safe_file(registry._path(pin(record)).parent / proof['path'])
        require(path.stat().st_size == proof['bytes'] and 0 < proof['bytes'] <= 4 * 1024 * 1024 and
                file_sha(path) == proof['sha256'], 'Saved BindCraft provenance bytes differ or exceed limits', 'integrity')
        provenance = parse(path.read_bytes())
        candidate = provenance.get('candidate', {}) if isinstance(provenance, dict) else {}
        require(isinstance(provenance, dict) and provenance.get('workflow') == 'bindcraft' and isinstance(candidate, dict) and
                candidate.get('candidate_id') == metadata.get('candidate_id') and
                candidate.get('sequence_sha256') == binding and
                isinstance(candidate.get('sequence'), str) and
                hashlib.sha256(candidate['sequence'].encode()).hexdigest() == binding and
                isinstance(candidate.get('structure_artifacts'), list) and
                any(isinstance(item, dict) and item.get('sha256') == structure_sha and
                    item.get('size') == structure['bytes'] and item.get('role') == 'structure' and
                    item.get('format') == 'pdb' and item.get('job_id') == metadata.get('job_id')
                    for item in candidate.get('structure_artifacts', [])),
                'Saved BindCraft candidate provenance differs', 'integrity')
        rows.append({'entry_id': ident, 'origin': 'prediction', 'label': 'predicted-complex.pdb',
            'format': 'pdb', 'size': structure['bytes'], 'sha256': structure_sha,
            'source_ref': pin(record), 'source_refs': [pin(record)], 'source_sequence_sha256': binding,
            'protein': _protein_context(module, records, pin(record)),
            'sequence_relation': _relation(binding, resolved['sequence_sha256']),
            'coordinate_sequence_match': 'unverified', 'hidden': curated['hidden'].get(ident, False),
            'created_at': record['created_at'], 'model': 'bindcraft', 'job_id': metadata.get('job_id'),
            'job_state': None, 'confidence': None, 'qa': None,
            'candidate_status': candidate.get('status'), 'association': 'saved_bindcraft_candidate',
            'access_scope': 'shared_library',
            '_path': registry._path(pin(record)).parent / structure['path']})
    return rows


def _entries(api, module, registry, records, selected, resolved, include_revisions=True):
    family = pin(selected).split('@')[0]
    current = records[registry._resolve(family, records)]
    # Family curation survives metadata-only revisions; source pins remain exact.
    curated = catalog(current)
    rows = _saved_binders(module, registry, records, selected, resolved, curated, include_revisions)
    for ident, original in curated['entries'].items():
        if not include_revisions and original['source_ref'] != pin(selected):
            continue
        require(original['source_ref'] in records and
                _sequence_sha(module, records, original['source_ref']) == original['source_sequence_sha256'],
                'Manual structure source revision/sequence differs', 'integrity')
        row = {key: deepcopy(value) for key, value in original.items() if key not in {'attachment', 'source'}}
        row.update(origin='manual', hidden=curated['hidden'].get(ident, False),
                   sequence_relation=_relation(row['source_sequence_sha256'], resolved['sequence_sha256']),
                   protein=_protein_context(module, records, row['source_ref']),
                   coordinate_sequence_match='unverified', access_scope='shared_library',
                   _path=registry._path(pin(current)).parent / original['attachment'])
        rows.append(row)
    with api.store.connection() as db:
        candidates = db.execute("""SELECT a.data AS artifact,j.data AS job,b.data AS batch,j.actor AS actor
            FROM objects a JOIN objects j ON j.id=json_extract(a.data,'$.job_id')
            JOIN objects b ON b.id=json_extract(j.data,'$.batch_id')
            WHERE a.kind='artifact' AND j.kind='job' AND b.kind='batch'
              AND a.actor=j.actor AND j.actor=b.actor
              AND json_extract(a.data,'$.role')='structure'
              AND LOWER(json_extract(a.data,'$.format')) IN ('pdb','cif','mmcif')
              AND j.state IN ('complete','failed','cancelled','interrupted')
            ORDER BY a.created,a.id LIMIT ?""", (MAX_CANDIDATES + 1,)).fetchall()
    require(len(candidates) <= MAX_CANDIDATES, 'Structure association scan exceeds its bounded result limit', 'limit')
    seen = set()
    for candidate in candidates:
        artifact, job, batch = (parse(candidate[key]) for key in ('artifact', 'job', 'batch'))
        require(artifact['job_id'] == job['job_id'] and job['batch_id'] == batch['batch_id'],
                'Prediction association identity differs', 'integrity')
        refs, shared = _association(job, batch, records)
        if not shared and candidate['actor'] != api.actor:
            continue  # A private complex partner does not become a shared library output.
        matches = sorted(ref for ref in refs if ref.split('@')[0] == family and
                         (include_revisions or ref == pin(selected)))
        if not matches:
            continue
        # Same bytes can occur under raw/canonical output aliases in one job.
        ident = 'p-' + digest({'family': family, 'job_id': job['job_id'], 'sha256': artifact['sha256']})[:32]
        if ident in seen:
            continue
        seen.add(ident)
        source_ref = pin(selected) if pin(selected) in matches else matches[0]
        binding = _sequence_sha(module, records, source_ref)
        rows.append({'entry_id': ident, 'origin': 'prediction', 'label': _display_label(Path(artifact['name']).name),
            'format': artifact['format'], 'size': artifact['size'], 'sha256': artifact['sha256'],
            'source_ref': source_ref, 'source_refs': matches, 'source_sequence_sha256': binding,
            'protein': _protein_context(module, records, source_ref),
            'sequence_relation': _relation(binding, resolved['sequence_sha256']),
            'coordinate_sequence_match': 'unverified', 'hidden': curated['hidden'].get(ident, False),
            'created_at': job['created_at'], 'model': job['model'], 'job_id': job['job_id'],
            'job_state': job['state'], 'confidence': artifact.get('confidence'), 'qa': artifact.get('qa'),
            'access_scope': 'shared_library' if shared else 'current_actor',
            '_path': api.store.directory('artifacts', artifact['artifact_id']) / 'content'})
    rows.sort(key=lambda row: (row['created_at'], row['entry_id']), reverse=True)
    return rows


def _public(row):
    return {key: value for key, value in row.items() if not key.startswith('_')}


def list_structures(api, params):
    keys(params, ('ref',), ('include_revisions', 'include_hidden', 'limit', 'cursor'))
    string(params['ref'], 'ref', 256)
    revisions, hidden = params.get('include_revisions', True), params.get('include_hidden', False)
    require(type(revisions) is bool and type(hidden) is bool, 'Structure filters must be boolean')
    limit = number(params.get('limit', 50), 'limit', 1, 100)
    with opened(api) as (module, registry, records):
        selected, resolved = _protein(module, registry, records, params['ref'])
        rows = [row for row in _entries(api, module, registry, records, selected, resolved, revisions)
                if hidden or not row['hidden']]
        if params.get('cursor') is not None:
            cursor = entry_id(params['cursor']); found = [i for i, row in enumerate(rows) if row['entry_id'] == cursor]
            require(found, 'Structure list changed; refresh its first page', 'conflict')
            rows = rows[found[0] + 1:]
        page = rows[:limit]
        return {'schema': 1, 'ref': pin(selected), 'sha256': selected['sha256'],
            'sequence_sha256': resolved['sequence_sha256'], 'entries': [_public(row) for row in page],
            'next_cursor': page[-1]['entry_id'] if len(rows) > limit else None,
            'scope': 'shared_library_and_own_private_complexes'}


def resolve_structure(api, ref, ident):
    string(ref, 'ref', 256); entry_id(ident)
    with opened(api) as (module, registry, records):
        selected, resolved = _protein(module, registry, records, ref)
        rows = [row for row in _entries(api, module, registry, records, selected, resolved)
                if row['entry_id'] == ident]
        require(len(rows) == 1, 'Structure is not associated with this protein', 'not_found')
        row = rows[0]
        # Hidden entries remain downloadable by their exact identity; hiding is
        # curation, not revocation or destruction of historical evidence.
        path = safe_file(row['_path'])
        require(0 < row['size'] <= MAX_FILE and path.stat().st_size == row['size'] and
                file_sha(path) == row['sha256'], 'Associated structure bytes changed or exceed 32 MiB', 'integrity')
        return {'ref': pin(selected), 'entry_id': ident, 'source_sha256': row['sha256'],
                'source_size': row['size'], 'source_format': row['format'],
                'source_path': path, 'entry': _public(row)}


def read_structure(api, params):
    keys(params, ('ref', 'entry_id'), ('offset', 'length'))
    source = resolve_structure(api, params['ref'], params['entry_id'])
    offset = number(params.get('offset', 0), 'offset', 0, source['source_size'])
    length = number(params.get('length', 131072), 'length', 1, 262144)
    with source['source_path'].open('rb') as stream:
        stream.seek(offset); raw = stream.read(length)
    return {'ref': source['ref'], 'entry_id': source['entry_id'],
        'source_ref': source['entry']['source_ref'],
        'source_sequence_sha256': source['entry']['source_sequence_sha256'],
        'protein': source['entry']['protein'],
        'sha256': source['source_sha256'], 'size': source['source_size'], 'format': source['source_format'],
        'name': source['entry']['label'], 'offset': offset, 'next_offset': offset + len(raw),
        'eof': offset + len(raw) == source['source_size'], 'data_base64': base64.b64encode(raw).decode()}


def structure_links(api, params):
    keys(params, ('artifact_id',))
    identifier(params['artifact_id'])
    artifact = api.store.read('artifact', params['artifact_id'], api.actor)
    require(artifact.get('role') == 'structure' and artifact.get('format') in FORMATS,
            'Select a retained predicted structure')
    path = safe_file(api.store.directory('artifacts', artifact['artifact_id']) / 'content')
    require(path.stat().st_size == artifact['size'] and file_sha(path) == artifact['sha256'],
            'Prediction structure bytes changed', 'integrity')
    job = api.store.read('job', artifact['job_id'], api.actor)
    batch = api.store.read('batch', job['batch_id'], api.actor)
    with opened(api) as (module, _, records):
        refs, _ = _association(job, batch, records)
        proteins = [_protein_context(module, records, ref) for ref in sorted(refs)
                    if records[ref]['kind'] == 'construct' and
                       records[ref]['identity'].get('molecule_type') == 'protein']
    return {'schema': 1, 'artifact_id': artifact['artifact_id'], 'sha256': artifact['sha256'], 'proteins': proteins}


def _format(raw, filename):
    fmt = Path(filename).suffix.lower().lstrip('.')
    require(fmt in FORMATS, 'Structures must be PDB or mmCIF files')
    try:
        text = raw.decode('utf-8')
    except UnicodeError as exc:
        raise Error('invalid', 'Structure must be UTF-8 text') from exc
    require('\x00' not in text, 'Structure contains binary data')
    if fmt == 'pdb':
        atoms = [line for line in text.splitlines() if line.startswith(('ATOM  ', 'HETATM'))]
        require(atoms, 'PDB contains no coordinate records')
        try:
            require(all(len(line) >= 54 and all(math.isfinite(float(line[start:start + 8]))
                        for start in (30, 38, 46)) for line in atoms), 'PDB coordinates are invalid')
        except ValueError as exc:
            raise Error('invalid', 'PDB coordinates are invalid') from exc
    else:
        require(re.search(r'(?m)^data_\S*', text) and '_atom_site.' in text,
                'mmCIF contains no atom-site data')
    return fmt


@contextmanager
def prepared_sources(api, structures):
    require(isinstance(structures, list) and len(structures) <= MAX_FILES, 'Attach at most 16 structures per operation', 'limit')
    with tempfile.TemporaryDirectory(prefix='library-structures-') as directory:
        prepared = []
        for index, item in enumerate(structures):
            keys(item, ('source',), ('label',))
            source = item['source']; keys(source, ('kind', 'id', 'sha256'))
            require(source['kind'] in {'upload', 'artifact'}, 'Structure source must be an upload or owned artifact')
            identifier(source['id']); sha(source['sha256'])
            receipt = api.store.read(source['kind'], source['id'], api.actor)
            require(source['kind'] != 'upload' or receipt['state'] == 'complete', 'Structure upload is incomplete', 'conflict')
            require(receipt['sha256'] == source['sha256'], 'Structure source hash differs', 'integrity')
            number(receipt['size'], 'structure bytes', 1, MAX_FILE)
            path = safe_file(api.store.directory(source['kind'] + 's', source['id']) / 'content')
            require(path.stat().st_size == receipt['size'], 'Structure source size differs', 'integrity')
            copied = Path(directory) / str(index)
            shutil.copyfile(path, copied)
            raw = copied.read_bytes()
            require(len(raw) == receipt['size'] and hashlib.sha256(raw).hexdigest() == source['sha256'],
                    'Structure changed while copying', 'integrity')
            fmt = _format(raw, receipt['name'])
            label = item.get('label', Path(receipt['name']).name)
            string(label, 'structure label', 200)
            require(label.isprintable(), 'Structure label contains control characters')
            prepared.append({'path': copied, 'label': label, 'format': fmt, 'size': len(raw),
                             'sha256': source['sha256'], 'source': deepcopy(source)})
        yield prepared


def add_to_document(record, document, prepared, source_ref, sequence_sha256):
    require(sequence_sha256 is not None, 'Protein must resolve before a structure can be associated', 'conflict')
    value = catalog(record) if record else {'schema': 1, 'entries': {}, 'hidden': {}}
    attachments = {}; changed = []
    for item in prepared:
        ident = 'm-' + digest({'family': source_ref.split('@')[0], 'sha256': item['sha256'],
                              'sequence_sha256': sequence_sha256})[:32]
        filename = 'structure-' + item['sha256'] + ('.pdb' if item['format'] == 'pdb' else '.cif')
        if ident not in value['entries']:
            value['entries'][ident] = {'entry_id': ident, **{key: item[key] for key in ('label', 'format', 'size', 'sha256', 'source')},
                'attachment': 'attachments/' + filename, 'source_ref': source_ref,
                'source_sequence_sha256': sequence_sha256, 'created_at': now()}
            if not record or not any(a['path'] == 'attachments/' + filename for a in record['attachments']):
                attachments[filename] = item['path']
            changed.append(ident)
        elif value['hidden'].get(ident, False):
            changed.append(ident)
        value['hidden'][ident] = False
    document.setdefault('provenance', {}).setdefault('workbench', {})[FIELD] = value
    # Validate the prospective catalog against both old and newly staged receipts.
    future = {**document, 'revision': int(source_ref.rsplit('@', 1)[1]),
              'attachments': [*(record.get('attachments', []) if record else []),
                *[{'path': 'attachments/' + name, 'sha256': file_sha(path), 'bytes': path.stat().st_size}
                  for name, path in attachments.items()]]}
    catalog(future)
    return attachments, changed


def attach(api, params):
    from . import library_edits as edits
    keys(params, ('ref', 'expected_sha256', 'structures', 'request_key'))
    string(params['ref'], 'ref', 256); sha(params['expected_sha256']); string(params['request_key'], 'request_key', 200)
    require(params['structures'], 'Select at least one structure')
    with prepared_sources(api, params['structures']) as prepared, edits._opened(api, write=True) as (module, registry, records):
        events = edits._events(module, records)
        replay = edits._replay(events, api.actor, 'library.structure_attach', params['request_key'], params)
        if replay is not None:
            return replay
        old = edits._current(module, registry, records, params['ref'], params['expected_sha256'])
        require(old['identity'].get('molecule_type') == 'protein', 'Select a protein construct')
        resolved = module.effective_sequence(old, records)
        document = edits._effective(module, old)
        attachments, changed = add_to_document(old, document, prepared, pin(old), resolved['sequence_sha256'])
        return edits._publish(api, module, registry, records, events, old, document,
            method='library.structure_attach', params=params, action='edit' if changed else 'noop',
            label='Attach protein structures', attachments=attachments)


def visibility(api, params):
    from . import library_edits as edits
    keys(params, ('ref', 'expected_sha256', 'entry_id', 'hidden', 'request_key'))
    string(params['ref'], 'ref', 256); sha(params['expected_sha256']); entry_id(params['entry_id'])
    string(params['request_key'], 'request_key', 200); require(type(params['hidden']) is bool, 'hidden must be boolean')
    with edits._opened(api, write=True) as (module, registry, records):
        events = edits._events(module, records)
        replay = edits._replay(events, api.actor, 'library.structure_visibility', params['request_key'], params)
        if replay is not None:
            return replay
        old = edits._current(module, registry, records, params['ref'], params['expected_sha256'])
        require(old['identity'].get('molecule_type') == 'protein', 'Select a protein construct')
        resolved = module.effective_sequence(old, records)
        rows = _entries(api, module, registry, records, old, resolved)
        require(any(row['entry_id'] == params['entry_id'] for row in rows), 'Structure is not associated with this protein', 'not_found')
        document = edits._effective(module, old); value = catalog(old)
        changed = value['hidden'].get(params['entry_id'], False) != params['hidden']
        value['hidden'][params['entry_id']] = params['hidden']
        document.setdefault('provenance', {}).setdefault('workbench', {})[FIELD] = value
        catalog({**document, 'revision': old['revision'], 'attachments': old['attachments']})
        return edits._publish(api, module, registry, records, events, old, document,
            method='library.structure_visibility', params=params, action='edit' if changed else 'noop',
            label='Hide protein structure' if params['hidden'] else 'Restore protein structure')


def reverse_structure(api, module, registry, records, events, original, action, params):
    from . import library_edits as edits
    before, after = records[original['before_ref']], records[original['after_ref']]
    old = records[registry._resolve(pin(after).split('@')[0], records)]
    prior, added, current = catalog(before), catalog(after), catalog(old)
    affected = set(prior['entries']) ^ set(added['entries'])
    affected.update(ident for ident in prior['hidden'].keys() | added['hidden'].keys()
                    if prior['hidden'].get(ident, False) != added['hidden'].get(ident, False))
    for ident in affected:
        # An undone newly attached association becomes hidden, but its catalog
        # and immutable bytes remain available for redo and retained references.
        previous_hidden = ident not in prior['entries'] if ident.startswith('m-') else False
        previous_hidden = prior['hidden'].get(ident, previous_hidden)
        added_hidden = added['hidden'].get(ident, False)
        expected, restored = ((added_hidden, previous_hidden) if action == 'undo' else (previous_hidden, added_hidden))
        require(current['hidden'].get(ident, False) == expected, 'This structure visibility has another edit', 'conflict')
        if ident.startswith('m-'):
            require(current['entries'].get(ident) == added['entries'].get(ident), 'Structure association changed', 'conflict')
        current['hidden'][ident] = restored
    document = edits._effective(module, old)
    document.setdefault('provenance', {}).setdefault('workbench', {})[FIELD] = current
    return edits._publish(api, module, registry, records, events, old, document,
        method='library.' + action, params=params, action=action,
        label=original['label'], original_operation_id=original['operation_id'])
