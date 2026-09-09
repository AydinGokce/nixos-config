"""Exploration of the shared, revision-bound molecular library."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path

from .common import Error, keys, number, require, string


@contextmanager
def opened(api):
    from .inputs import library
    from .service import configuration
    config = api.library_config or configuration(os.environ.get('BIO_WORKBENCH_CONFIG'))
    module = library(config['tools_dir'])
    registry = module.Registry(config['library_root'])
    try:
        with registry._lock():
            records = registry._records_locked()
            yield module, registry, records
    except module.Error as exc:
        raise Error('library', str(exc)) from exc
    except OSError as exc:
        raise Error('unavailable', 'The construct library could not be read') from exc


def latest(records):
    selected = {}
    for record in records.values():
        key = (record['kind'], record['id'])
        if key not in selected or selected[key]['revision'] < record['revision']:
            selected[key] = record
    return list(selected.values())


def pin(record):
    return f"{record['kind']}:{record['id']}@{record['revision']}"


def submission(record, records):
    kind, identity = record['kind'], record['identity']
    if kind not in {'construct', 'assembly'}:
        return {'allowed': False, 'reason': 'Select a molecular construct or assembly to add to a run.'}
    if kind == 'assembly':
        for component in identity.get('components', []):
            child = records[component['construct_ref']]
            result = submission(child, records)
            if not result['allowed']:
                return {'allowed': False, 'reason': child['name'] + ': ' + result['reason']}
    else:
        if identity.get('molecular_form') == 'plasmid' or identity.get('strand_count') == 2:
            return {'allowed': False, 'reason': 'Whole double-stranded plasmids are inventory records. Select a defined encoded protein or an explicitly prepared molecular input.'}
        review = identity.get('product_review', {})
        if review and review.get('status') != 'reference_matched':
            return {'allowed': False, 'reason': 'This protein candidate has unresolved source product-review findings. Resolve its definition before prediction.'}
    return {'allowed': True, 'reason': 'Model compatibility is checked during run preview; this does not establish biological function.'}


def summary(record, records):
    from .library_edits import presentation
    identity = record['identity']
    review = identity.get('product_review', {})
    admission = submission(record, records)
    return {
        'ref': pin(record), 'kind': record['kind'], 'id': record['id'],
        'name': record['name'], 'revision': record['revision'],
        'sha256': record['sha256'], **presentation(record),
        'status': record['status'], 'molecule_type': identity.get('molecule_type'),
        'aliases': record['aliases'], 'tags': record['tags'],
        'sequence_length': len(identity.get('sequence', identity.get('residues', []))) or None,
        'molecular_form': identity.get('molecular_form'),
        'review_status': review.get('status'),
        'review_reason': admission['reason'] if review and not admission['allowed'] else '',
        'submission_allowed': admission['allowed'],
        'encoded_by_ref': identity.get('encoded_by', {}).get('construct_ref'),
        'member_count': len(identity.get('members', [])) if record['kind'] == 'project' else None,
    }


def list_records(api, params):
    from .library_edits import presentation
    keys(params, optional=('query', 'kind', 'molecule_type', 'project_ref', 'limit', 'offset', 'archived'))
    query = params.get('query', '')
    require(isinstance(query, str) and len(query) <= 512 and not any(ord(c) < 32 for c in query), 'Invalid library search text')
    kind = params.get('kind')
    require(kind is None or isinstance(kind, str) and kind in {'construct', 'monomer', 'assembly', 'project'}, 'Invalid library kind')
    molecule_type = params.get('molecule_type')
    require(molecule_type is None or isinstance(molecule_type, str) and molecule_type in {'protein', 'dna', 'rna', 'small_molecule', 'mixed_polymer'}, 'Invalid molecule type')
    limit = number(params.get('limit', 100), 'limit', 1, 500)
    offset = number(params.get('offset', 0), 'offset', 0, 1_000_000)
    archived = params.get('archived', False)
    require(type(archived) is bool, 'archived must be a boolean')
    if 'project_ref' in params:
        string(params['project_ref'], 'project_ref', 256)
    with opened(api) as (module, registry, records):
        current = latest(records)
        current_entities = {(r['kind'], r['id']): r for r in current}
        archived_entities = {key for key, record in current_entities.items() if presentation(record)['archived']}
        projects = sorted((r for r in current if r['kind'] == 'project' and
                           ((r['kind'], r['id']) in archived_entities) == archived),
                          key=lambda r: (r['name'].casefold(), r['id']))
        counts = {k: sum(r['kind'] == k for r in current) for k in module.COLLECTIONS}
        candidates = current
        project_ref = None
        if 'project_ref' in params:
            project_ref = registry._resolve(params['project_ref'], records, 'project')
            candidates = [records[member['source_ref']] for member in records[project_ref]['identity']['members']]
        terms = query.casefold().split()
        filtered = []
        for record in candidates:
            if ((record['kind'], record['id']) in archived_entities) != archived:
                continue
            if kind is not None and record['kind'] != kind:
                continue
            if molecule_type is not None and record['identity'].get('molecule_type') != molecule_type:
                continue
            display = presentation(record)
            haystack = ' '.join([pin(record), record['name'], display['alt_name'], display['verbose_name'],
                                 display['inventory_id'], *record['aliases'], *record['tags'], record['notes'],
                                 json.dumps(record['provenance'], ensure_ascii=False)]).casefold()
            if all(term in haystack for term in terms):
                filtered.append(record)
        filtered.sort(key=lambda r: (r['id'], r['kind'], r['revision']))
        page = filtered[offset:offset + limit]
        next_offset = offset + len(page) if offset + len(page) < len(filtered) else None
        return {'records': [summary(r, records) for r in page],
                'projects': [summary(r, records) for r in projects], 'counts': counts,
                'total_count': len(current), 'filtered_count': len(filtered),
                'next_offset': next_offset, 'truncated': next_offset is not None,
                'project_ref': project_ref, 'archived': archived,
                'archived_count': len(archived_entities)}


def get_record(api, params):
    from .library_edits import presentation
    keys(params, ('ref',)); string(params['ref'], 'ref', 256)
    with opened(api) as (module, registry, records):
        ref = registry._resolve(params['ref'], records)
        record = records[ref]
        path = 'attachments/project.md' if record['kind'] == 'project' else 'attachments/description.md'
        receipt = next((x for x in record['attachments'] if x['path'] == path), None)
        description = None
        if receipt:
            require(receipt['bytes'] <= 1024 * 1024, 'Purpose document exceeds display limit; export its attachment', 'limit')
            try:
                text = (registry._path(ref).parent / path).read_bytes().decode('utf-8')
            except UnicodeError as exc:
                raise Error('library', 'Purpose document is not UTF-8') from exc
            description = {'text': text, 'path': path, 'sha256': receipt['sha256'],
                           'incomplete': '<!-- bio-library:purpose-scaffold:v1 incomplete -->' in text}
        members = []
        if record['kind'] == 'project':
            members = [{**summary(records[m['source_ref']], records), **m} for m in record['identity']['members']]
        relations = []
        for parent in record.get('parents', []):
            relations.append({'relation': 'parent revision', 'ref': parent, 'label': records[parent]['name']})
        encoded_by = record['identity'].get('encoded_by', {}).get('construct_ref')
        if encoded_by:
            relations.append({'relation': 'encoded by', 'ref': encoded_by, 'label': records[encoded_by]['name']})
        for component in record['identity'].get('components', []) if record['kind'] == 'assembly' else []:
            child = component['construct_ref']
            relations.append({'relation': 'chain ' + component['chain_id'], 'ref': child, 'label': records[child]['name']})
        for other in sorted(records.values(), key=lambda r: (r['id'], -r['revision'])):
            if other['identity'].get('encoded_by', {}).get('construct_ref') == ref:
                relations.append({'relation': 'encodes', 'ref': pin(other), 'label': other['name']})
        projects = [summary(r, records) for r in sorted(records.values(), key=lambda r: (r['id'], -r['revision']))
                    if r['kind'] == 'project' and any(m['source_ref'] == ref for m in r['identity']['members'])]
        revisions = sorted((r for r in records.values() if (r['kind'], r['id']) == (record['kind'], record['id'])),
                           key=lambda r: r['revision'], reverse=True)
        latest_ref = pin(revisions[0])
        return {'ref': ref, 'record': record, 'sha256': record['sha256'], **presentation(record),
                'latest_ref': latest_ref, 'is_latest': ref == latest_ref,
                'description': description, 'members': members,
                'relations': relations, 'projects': projects,
                'revisions': [summary(r, records) for r in revisions], 'submission': submission(record, records)}


def attachment(api, params):
    keys(params, ('ref', 'name'), ('offset', 'length'))
    string(params['ref'], 'ref', 256); string(params['name'], 'name', 256)
    offset = number(params.get('offset', 0), 'offset', 0, 2**40)
    length = number(params.get('length', 131072), 'length', 1, 262144)
    name = params['name']
    if not name.startswith('attachments/'):
        name = 'attachments/' + name
    with opened(api) as (module, registry, records):
        relative = module.safe_relative(name)
        require(len(relative.parts) == 2 and relative.parts[0] == 'attachments', 'Attachment name must be a single filename')
        ref = registry._resolve(params['ref'], records)
        record = records[ref]
        receipt = next((x for x in record['attachments'] if x['path'] == name), None)
        require(receipt is not None, 'Attachment is not part of this library revision', 'not_found')
        require(offset <= receipt['bytes'], 'Offset exceeds attachment size')
        path = registry._path(ref).parent / relative
        with path.open('rb') as stream:
            stream.seek(offset)
            data = stream.read(length)
        return {'ref': ref, 'name': relative.name, 'offset': offset, 'next_offset': offset + len(data),
                'eof': offset + len(data) == receipt['bytes'], 'size': receipt['bytes'],
                'sha256': receipt['sha256'], 'data_b64': base64.b64encode(data).decode('ascii')}
