"""Revision-preserving library curation with durable, actor-owned undo/redo.

The immutable target revision carries each operation receipt. There is no
second database to lose when the library is backed up or restored. Registry
transactions publish its current project memberships as one recoverable unit.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import os

from .common import Error, digest, identifier, keys, require, sha, string, uid

MAX_SEQUENCE = 1_000_000
MAX_EVENTS = 10_000
MAX_RECORDS = 100_000
MAX_CHANGED_RECORDS = 1000
SIMPLE_SEQUENCE_FIELDS = {'molecule_type', 'sequence', 'circular', 'strand_count', 'molecular_form',
                          'encoded_by', 'product_review'}


def _workbench(record):
    value = record.get('provenance', {}).get('workbench', {})
    return value if isinstance(value, dict) else {}


def presentation(record):
    """Structured display metadata; empty curated Alt names override imports."""
    provenance = record.get('provenance', {})
    inventory = provenance.get('inventory', {})
    if not isinstance(inventory, dict):
        inventory = {}
    curated = _workbench(record)
    def text(value):
        return value if isinstance(value, str) else ''
    alt = curated['alt_name'] if 'alt_name' in curated else inventory.get('alt_orf_name')
    identity = record.get('identity', {})
    modality = identity.get('molecular_form') or {
        'dna': 'DNA', 'rna': 'RNA', 'small_molecule': 'small molecule',
        'mixed_polymer': 'mixed polymer',
    }.get(identity.get('molecule_type'), identity.get('molecule_type', record['kind']))
    return {
        'inventory_id': text(provenance.get('source_inventory_identifier') or inventory.get('identifier')),
        'alt_name': text(alt),
        'verbose_name': text(inventory.get('verbose_name') or (record['name'] if record['kind'] != 'project' else '')),
        'modality': modality,
        'archived': curated.get('archived') is True,
    }


@contextmanager
def _opened(api, *, write=False):
    from .inputs import library
    from .service import configuration
    config = api.library_config or configuration(os.environ.get('BIO_WORKBENCH_CONFIG'))
    module = library(config['tools_dir'])
    registry = module.Registry(config['library_root'])
    try:
        with registry._lock(exclusive=write):
            records = registry._records_locked()
            require(len(records) <= MAX_RECORDS, 'Library curation record limit exceeded', 'limit')
            yield module, registry, records
    except module.Error as exc:
        raise Error('library', str(exc)) from exc
    except OSError as exc:
        raise Error('unavailable', 'Library curation is unavailable; retry the same request key') from exc


def _events(module, records):
    events = []
    for record in records.values():
        event = _workbench(record).get('operation')
        if not isinstance(event, dict) or event.get('after_ref') != module.reference(record):
            continue  # Project propagation may inherit an earlier receipt.
        require(event.get('schema') == 1 and type(event.get('sequence')) is int and event['sequence'] > 0 and
                event.get('action') in {'edit', 'noop', 'undo', 'redo'}, 'Malformed library operation', 'integrity')
        identifier(event.get('operation_id')); string(event.get('actor'), 'operation actor', 128)
        require(event.get('before_ref') in records, 'Library operation predecessor is missing', 'integrity')
        events.append(event)
    require(len(events) <= MAX_EVENTS, 'Library operation-history limit exceeded', 'limit')
    events.sort(key=lambda event: event['sequence'])
    require(len({event['sequence'] for event in events}) == len(events) and
            len({event['operation_id'] for event in events}) == len(events),
            'Duplicate library operation receipt', 'integrity')
    return events


def _stacks(events, actor):
    undo, redo = [], []
    for event in events:
        if event['actor'] != actor:
            continue
        if event['action'] == 'edit':
            undo.append(event['operation_id']); redo.clear()
        elif event['action'] == 'undo':
            require(undo and undo[-1] == event.get('original_operation_id'),
                    'Library undo history is inconsistent', 'integrity')
            redo.append(undo.pop())
        elif event['action'] == 'redo':
            require(redo and redo[-1] == event.get('original_operation_id'),
                    'Library redo history is inconsistent', 'integrity')
            undo.append(redo.pop())
    return undo, redo


def _history(events, actor):
    undo, redo = _stacks(events, actor)
    indexed = {event['operation_id']: event for event in events}
    def item(stack):
        if not stack:
            return None
        event = indexed[stack[-1]]
        return {key: event[key] for key in ('operation_id', 'label', 'before_ref', 'after_ref')}
    return {'undo': item(undo), 'redo': item(redo), 'undo_count': len(undo), 'redo_count': len(redo)}


def history(api, params):
    keys(params)
    with _opened(api) as (module, _, records):
        return _history(_events(module, records), api.actor)


def _effective(module, record):
    document = {key: deepcopy(value) for key, value in record.items() if key in module.USER_FIELDS}
    document.pop('parents', None)
    curated = document['provenance'].get('workbench')
    if isinstance(curated, dict):
        curated.pop('operation', None)
        curated.pop('project_update', None)
        if not curated:
            document['provenance'].pop('workbench')
    return document


def _current(module, registry, records, ref, expected_sha256=None):
    module.pinned_parts(ref)
    resolved = registry._resolve(ref, records)
    old = records[resolved]
    latest_ref = registry._resolve(f"{old['kind']}:{old['id']}", records)
    require(latest_ref == resolved, 'This library entry changed. Refresh before editing.', 'conflict')
    if expected_sha256 is not None:
        require(old['sha256'] == expected_sha256, 'This library entry changed. Refresh before editing.', 'conflict')
    return old


def _patch(module, record, patch):
    keys(patch, optional=('alt_name', 'name', 'sequence', 'archived'))
    require(patch, 'An edit must contain at least one field')
    require(record['kind'] in {'construct', 'assembly', 'project'}, 'This library entry is not editable')
    document = {key: deepcopy(value) for key, value in record.items() if key in module.USER_FIELDS}
    provenance = document['provenance']
    require('workbench' not in provenance or isinstance(provenance['workbench'], dict),
            'Reserved workbench provenance has an incompatible format', 'conflict')
    curated = provenance.setdefault('workbench', {})
    labels = []
    if 'alt_name' in patch:
        require(record['kind'] != 'project', 'Projects use name instead of Alt name')
        value = patch['alt_name']
        require(isinstance(value, str) and len(value) <= 512 and not any(ord(c) < 32 for c in value),
                'Alt name must be at most 512 characters without control characters')
        require(not value or value.strip(), 'Use an empty Alt name instead of whitespace')
        # Keep a genuine no-op a no-op; an empty override is necessary when an
        # imported nonempty Alt name is deliberately cleared.
        if value != presentation(record)['alt_name']:
            curated['alt_name'] = value
            labels.append('Change Alt name')
    if 'name' in patch:
        require(record['kind'] == 'project', 'Constructs use Alt name; the original descriptive name is provenance')
        value = string(patch['name'], 'project name', 512)
        require(value.strip(), 'Project name cannot be blank')
        if value != record['name']:
            document['name'] = value
            labels.append('Change project name')
    if 'archived' in patch:
        require(type(patch['archived']) is bool, 'archived must be boolean')
        if patch['archived'] != presentation(record)['archived']:
            curated['archived'] = patch['archived']
            labels.append('Archive' if patch['archived'] else 'Restore')
    if 'sequence' in patch:
        identity = document['identity']
        molecule = identity.get('molecule_type')
        require(record['kind'] == 'construct' and molecule in module.ALPHABETS and 'sequence' in identity,
                'Simple sequence editing requires an existing protein, DNA or RNA sequence')
        value = patch['sequence']
        require(isinstance(value, str) and 0 < len(value) <= MAX_SEQUENCE and set(value) <= module.ALPHABETS[molecule],
                'Sequence must contain 1..1000000 canonical uppercase symbols without spaces or FASTA headers')
        if value != identity['sequence']:
            require(not any(value for field, value in identity.items() if field not in SIMPLE_SEQUENCE_FIELDS),
                    'This sequence has explicit chemistry or structural annotations. Edit its molecular definition with explicit remapping; the simple sequence editor cannot discard them.')
            original = {key: deepcopy(identity[key]) for key in ('encoded_by', 'product_review') if key in identity}
            curated['sequence_edit'] = {
                'source_ref': module.reference(record),
                'original_sequence_sha256': hashlib.sha256(identity['sequence'].encode()).hexdigest(),
                'sequence_sha256': hashlib.sha256(value.encode()).hexdigest(),
                'definition': 'User supplied exact canonical sequence; no translation or reference match inferred.',
                'historical_identity_claims': original,
                'historical_attachment_paths': [item['path'] for item in record['attachments']],
                'annotation_applicability': 'Inherited source sequences, positional annotations, derivation evidence and purpose text describe the predecessor. Reassess applicability to this edited sequence.',
            }
            identity['sequence'] = value
            identity.pop('encoded_by', None)
            identity.pop('product_review', None)
            document['status'] = 'defined'
            document['tags'] = [tag for tag in document['tags'] if tag not in {'reference_matched', 'review_required'}]
            labels.append('Change sequence')
    if not curated:
        provenance.pop('workbench', None)
    label = ', '.join(labels) or 'No change'
    return document, label


def _response(event, events, actor):
    return {'operation_id': event['operation_id'], 'ref': event['after_ref'],
            'changed_refs': deepcopy(event['changed_refs']), 'changed': event['action'] != 'noop',
            'history': _history(events, actor)}


def _replay(events, actor, method, request_key, payload):
    fingerprint = digest(payload)
    for event in events:
        if event['actor'] == actor and event['method'] == method and event['request_key'] == request_key:
            require(event['request_sha256'] == fingerprint, 'Request key belongs to different parameters', 'conflict')
            return _response(event, events, actor)
    return None


def _publish(api, module, registry, records, events, old, document, *, method, params, action,
             label, original_operation_id=None):
    require(len(events) < MAX_EVENTS, 'Library operation-history limit exceeded', 'limit')
    before_ref = module.reference(old)
    after_ref = f"{old['kind']}:{old['id']}@{old['revision'] + 1}"
    operation_id = uid()
    changes = [{'ref': before_ref, 'expected_sha256': old['sha256'], 'patch': document}]
    changed_refs = [{'before_ref': before_ref, 'after_ref': after_ref}]
    # Preserve all old project snapshots. Only their current revisions advance,
    # and only memberships pinning this exact predecessor are changed.
    current_projects = {}
    for record in records.values():
        if record['kind'] == 'project' and (record['id'] not in current_projects or
                                          record['revision'] > current_projects[record['id']]['revision']):
            current_projects[record['id']] = record
    if old['kind'] in {'construct', 'assembly'}:
        for project in sorted(current_projects.values(), key=lambda value: value['id']):
            if not any(member['source_ref'] == before_ref for member in project['identity']['members']):
                continue
            identity = deepcopy(project['identity'])
            for member in identity['members']:
                if member['source_ref'] == before_ref:
                    member['source_ref'] = after_ref
            provenance = deepcopy(project['provenance'])
            require('workbench' not in provenance or isinstance(provenance['workbench'], dict),
                    'Project workbench provenance has an incompatible format', 'conflict')
            provenance.setdefault('workbench', {})['project_update'] = {
                'operation_id': operation_id, 'before_ref': before_ref, 'after_ref': after_ref}
            project_ref = module.reference(project)
            changes.append({'ref': project_ref, 'expected_sha256': project['sha256'],
                            'patch': {'identity': identity, 'provenance': provenance}})
            changed_refs.append({'before_ref': project_ref,
                                 'after_ref': f"project:{project['id']}@{project['revision'] + 1}"})
    require(len(changes) <= MAX_CHANGED_RECORDS, 'Too many project memberships for one edit', 'limit')
    event = {
        'schema': 1, 'sequence': events[-1]['sequence'] + 1 if events else 1,
        'operation_id': operation_id, 'actor': api.actor, 'method': method,
        'request_key': params['request_key'], 'request_sha256': digest(params),
        'action': action, 'label': label, 'before_ref': before_ref, 'after_ref': after_ref,
        'changed_refs': changed_refs,
    }
    if original_operation_id is not None:
        event['original_operation_id'] = original_operation_id
    document['provenance'].setdefault('workbench', {})['operation'] = event
    registry._revise_many_locked(changes, records)
    events.append(event)
    return _response(event, events, api.actor)


def edit(api, params):
    keys(params, ('ref', 'expected_sha256', 'request_key', 'patch'))
    string(params['ref'], 'ref', 256); sha(params['expected_sha256'])
    string(params['request_key'], 'request_key', 200)
    # Cap before canonical hashing: RPC already has a 2 MiB wire limit.
    require(isinstance(params['patch'], dict), 'patch must be an object')
    with _opened(api, write=True) as (module, registry, records):
        events = _events(module, records)
        replay = _replay(events, api.actor, 'library.edit', params['request_key'], params)
        if replay is not None:
            return replay
        old = _current(module, registry, records, params['ref'], params['expected_sha256'])
        document, label = _patch(module, old, params['patch'])
        changed = _effective(module, old) != _effective(module, document)
        return _publish(api, module, registry, records, events, old, document,
                        method='library.edit', params=params, action='edit' if changed else 'noop', label=label)


def _reverse(api, params, action):
    keys(params, ('operation_id', 'request_key'))
    identifier(params['operation_id']); string(params['request_key'], 'request_key', 200)
    method = 'library.' + action
    with _opened(api, write=True) as (module, registry, records):
        events = _events(module, records)
        replay = _replay(events, api.actor, method, params['request_key'], params)
        if replay is not None:
            return replay
        undo_stack, redo_stack = _stacks(events, api.actor)
        stack = undo_stack if action == 'undo' else redo_stack
        require(stack and stack[-1] == params['operation_id'],
                'Library history changed. Refresh before undo or redo.', 'conflict')
        original = next(event for event in events if event['operation_id'] == params['operation_id'])
        before, after = records[original['before_ref']], records[original['after_ref']]
        target = after if action == 'undo' else before
        restored = before if action == 'undo' else after
        current_ref = registry._resolve(f"{target['kind']}:{target['id']}", records)
        old = records[current_ref]
        if old['kind'] == 'project':
            # Member edits advance project snapshots automatically. Reversing
            # a project label/archive action must preserve those memberships,
            # including the new pins created by an earlier member undo.
            document = {key: deepcopy(value) for key, value in old.items() if key in module.USER_FIELDS}
            for field in ('name', 'archived'):
                def value(record):
                    return record['name'] if field == 'name' else presentation(record)['archived']
                if value(before) == value(after):
                    continue
                require(value(old) == value(target),
                        'This project field has another edit. Undo or redo would overwrite it; refresh the library.', 'conflict')
                if field == 'name':
                    document['name'] = restored['name']
                else:
                    curated = document['provenance'].setdefault('workbench', {})
                    if 'archived' in _workbench(restored):
                        curated['archived'] = _workbench(restored)['archived']
                    else:
                        curated.pop('archived', None)
        else:
            require(_effective(module, old) == _effective(module, target),
                    'This entry has another edit. Undo or redo would overwrite it; refresh the library.', 'conflict')
            document = {key: deepcopy(value) for key, value in restored.items() if key in module.USER_FIELDS}
        require(old['attachments'] == target['attachments'],
                'This entry has different source attachments; undo or redo cannot overwrite them.', 'conflict')
        document['parents'] = deepcopy(old['parents'])
        return _publish(api, module, registry, records, events, old, document,
                        method=method, params=params, action=action, label=original['label'],
                        original_operation_id=original['operation_id'])


def undo(api, params):
    return _reverse(api, params, 'undo')


def redo(api, params):
    return _reverse(api, params, 'redo')
