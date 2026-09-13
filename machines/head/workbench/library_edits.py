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
from pathlib import Path
import tempfile

from .common import Error, digest, identifier, keys, require, sha, string, uid

MAX_SEQUENCE = 1_000_000
MAX_EVENTS = 10_000
MAX_RECORDS = 100_000
MAX_CHANGED_RECORDS = 1000
MAX_DESCRIPTION_BYTES = 1024 * 1024
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
                event.get('action') in {'create', 'edit', 'noop', 'undo', 'redo'}, 'Malformed library operation', 'integrity')
        identifier(event.get('operation_id')); string(event.get('actor'), 'operation actor', 128)
        require(event.get('before_ref') in records or
                event['action'] == 'create' and event.get('before_ref') is None,
                'Library operation predecessor is missing', 'integrity')
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
        if event['action'] in {'create', 'edit'}:
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


def _description_bytes(value):
    require(isinstance(value, str), 'Project description must be Markdown text')
    try:
        raw = value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise Error('invalid', 'Project description must be UTF-8') from exc
    require(len(raw) <= MAX_DESCRIPTION_BYTES, 'Project description must be no larger than 1 MiB', 'limit')
    require('\x00' not in value, 'Project description must contain no NUL bytes')
    return raw


def _description_receipt(record):
    return next((item for item in record['attachments'] if item['path'] == 'attachments/project.md'), None)


@contextmanager
def _description_attachment(registry, raw):
    if raw is None:
        yield {}
        return
    # The registry transaction copies/fsyncs replacement bytes before publishing
    # its durable journal. The disposable input never becomes a library revision.
    with tempfile.TemporaryDirectory(prefix='project-description-', dir=registry.root / '.staging') as temporary:
        path = Path(temporary) / 'project.md'
        path.write_bytes(raw)
        yield {'project.md': path}


def _patch(module, record, patch, records=None, registry=None):
    keys(patch, optional=('alt_name', 'name', 'description', 'sequence', 'sequence_edit', 'archived', 'translation', 'parent_ref', 'frame_offset'))
    require(patch, 'An edit must contain at least one field')
    require(record['kind'] in {'construct', 'assembly', 'project'}, 'This library entry is not editable')
    document = {key: deepcopy(value) for key, value in record.items() if key in module.USER_FIELDS}
    provenance = document['provenance']
    require('workbench' not in provenance or isinstance(provenance['workbench'], dict),
            'Reserved workbench provenance has an incompatible format', 'conflict')
    curated = provenance.setdefault('workbench', {})
    labels = []
    frame_edit = 'frame_offset' in patch
    if frame_edit:
        require(not ({'translation', 'parent_ref', 'sequence', 'sequence_edit'} & set(patch)),
                'Change a reading frame or its source/definition in one operation')
        require(record['kind'] == 'construct' and record['identity'].get('molecule_type') == 'protein'
                and module.translation.is_derived(record),
                'A reading frame requires a protein derived from a nucleotide source')
        require(type(patch['frame_offset']) is int and 0 <= patch['frame_offset'] <= 2,
                'frame_offset must be an integer from 0 to 2')
        patch = {**patch, 'translation': module.translation.frame_definition(
            record['identity']['encoded_by']['translation'], patch['frame_offset'])}
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
    if 'description' in patch:
        require(record['kind'] == 'project', 'Description editing is available for projects only')
        raw = _description_bytes(patch['description'])
        receipt = _description_receipt(record)
        if receipt is None or receipt['sha256'] != hashlib.sha256(raw).hexdigest():
            labels.append('Change project description')
    if 'archived' in patch:
        require(type(patch['archived']) is bool, 'archived must be boolean')
        if patch['archived'] != presentation(record)['archived']:
            curated['archived'] = patch['archived']
            labels.append('Archive' if patch['archived'] else 'Restore')
    if 'translation' in patch or 'parent_ref' in patch:
        identity = document['identity']
        require(record['kind'] == 'construct' and identity.get('molecule_type') == 'protein',
                'Translation definitions belong to protein constructs')
        require(not any(identity.get(field) for field in ('modifications', 'residues', 'linkages', 'termini', 'bonds', 'crosslinks')),
                'Explicit protein chemistry requires residue remapping before changing its derivation')
        encoded = identity.get('encoded_by', {})
        parent_ref = patch.get('parent_ref', encoded.get('construct_ref'))
        require(isinstance(parent_ref, str) and parent_ref, 'Select a source plasmid for this protein')
        parent = _current(module, registry, records, parent_ref)
        require(parent['kind'] == 'construct' and parent['identity'].get('molecule_type') in {'dna', 'rna'},
                'Protein products require a DNA or RNA parent')
        definition = deepcopy(patch.get('translation', encoded.get('translation')))
        module.translation.validate_definition(definition, require=module.require)
        before = module.effective_sequence(record, records)
        identity.pop('sequence', None)
        identity['encoded_by'] = {'construct_ref': module.reference(parent),
            'sequence_sha256': hashlib.sha256(parent['identity']['sequence'].encode()).hexdigest(),
            'translation': definition}
        after = module.effective_sequence(document, records)
        if identity != record['identity']:
            curated['translation_edit'] = {'source_ref': module.reference(record),
                'definition': ('User changed the coding frame; translate complete codons through the first in-frame stop.'
                               if frame_edit else 'User selected a nucleotide coding footprint and protein residue range.'),
                'annotation_applicability': 'Inherited protein annotations and purpose refer to the predecessor; reassess against the translated product.'}
            if before['sequence_sha256'] != after['sequence_sha256'] and identity.get('product_review', {}).get('status') == 'reference_matched':
                curated['translation_edit']['historical_product_review'] = identity.pop('product_review')
                document['tags'] = [tag for tag in document['tags'] if tag != 'reference_matched']
            document['status'] = 'defined' if after['available'] else 'draft'
            labels.append('Change translation frame' if frame_edit else 'Change protein definition')
    require(not ('sequence_edit' in patch and 'sequence' in patch), 'Choose a sequence splice or a complete sequence')
    require(not ({'sequence', 'sequence_edit'} & set(patch) and {'translation', 'parent_ref'} & set(patch)),
            'Edit a direct sequence or its translation definition in one operation')
    if 'sequence_edit' in patch:
        splice = patch['sequence_edit']
        keys(splice, ('start', 'end', 'replacement'))
        sequence = document['identity'].get('sequence')
        require(isinstance(sequence, str), 'Derived proteins are edited through their source or coordinates')
        require(all(type(splice[key]) is int for key in ('start', 'end')) and
                0 <= splice['start'] <= splice['end'] <= len(sequence), 'Sequence splice is outside the current sequence')
        require(isinstance(splice['replacement'], str), 'Splice replacement must be a sequence string')
        patch = {**patch, 'sequence': sequence[:splice['start']] + splice['replacement'] + sequence[splice['end']:]}
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
            if 'sequence_edit' in patch:
                splice = patch['sequence_edit']
                curated['sequence_edit']['splice'] = {'start': splice['start'], 'end': splice['end'],
                                                     'replacement_length': len(splice['replacement'])}
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


def _change_ref(module, change, records):
    if 'create' in change:
        document = change['create']
        return {'before_ref': None, 'after_ref': f"{document['kind']}:{document['id']}@1"}
    old = records[change['ref']]
    return {'before_ref': change['ref'], 'after_ref': f"{old['kind']}:{old['id']}@{old['revision'] + 1}"}


def _current_projects(records):
    latest = {}
    for record in records.values():
        if record['kind'] == 'project' and (record['id'] not in latest or
                                           record['revision'] > latest[record['id']]['revision']):
            latest[record['id']] = record
    return sorted(latest.values(), key=lambda record: record['id'])


def _alt_name(params):
    value = params.get('alt_name', '')
    require(isinstance(value, str) and len(value) <= 512 and not any(ord(char) < 32 for char in value)
            and (not value or value.strip()), 'Alt name must be empty or nonblank text up to 512 characters')
    return value


def _new_document(ident, name, identity, alt_name, provenance=None):
    provenance = deepcopy(provenance or {})
    provenance['workbench'] = {'alt_name': alt_name, 'created_in': 'Bio Workbench'}
    return {'schema': 1, 'kind': 'construct', 'id': ident, 'name': name, 'status': 'defined',
            'aliases': [], 'tags': [], 'notes': '', 'parents': [], 'identity': identity,
            'provenance': provenance}


def create_product(api, params):
    keys(params, ('parent_ref', 'expected_sha256', 'translation', 'request_key'), ('alt_name',))
    string(params['parent_ref'], 'parent_ref', 256); sha(params['expected_sha256'])
    string(params['request_key'], 'request_key', 200)
    alt_name = _alt_name(params)
    with _opened(api, write=True) as (module, registry, records):
        events = _events(module, records)
        replay = _replay(events, api.actor, 'library.product_create', params['request_key'], params)
        if replay is not None:
            return replay
        parent = _current(module, registry, records, params['parent_ref'], params['expected_sha256'])
        require(parent['kind'] == 'construct' and parent['identity'].get('molecule_type') in {'dna', 'rna'},
                'Select a DNA or RNA parent for a protein product')
        require(not presentation(parent)['archived'], 'Restore the parent before adding a product', 'conflict')
        definition = deepcopy(params['translation'])
        module.translation.validate_definition(definition, require=module.require)
        identity = {'molecule_type': 'protein', 'encoded_by': {
            'construct_ref': module.reference(parent),
            'sequence_sha256': hashlib.sha256(parent['identity']['sequence'].encode()).hexdigest(),
            'translation': definition}}
        token = digest({'actor': api.actor, 'method': 'library.product_create', 'request_key': params['request_key']})[:12]
        ident = parent['id'][:40] + '-protein-' + token
        provenance = {key: deepcopy(parent['provenance'][key]) for key in
                      ('source_inventory_identifier', 'inventory', 'source_archive_project_id') if key in parent['provenance']}
        document = _new_document(ident, alt_name or parent['name'], identity, alt_name, provenance)
        resolved = module.effective_sequence(document, records)
        require(resolved['available'], 'Protein definition is unavailable: ' +
                '; '.join(item['message'] for item in resolved['issues']))
        new_ref = f'construct:{ident}@1'
        additions = {module.reference(project): [new_ref] for project in _current_projects(records)
                     if any(member['source_ref'].split('@')[0] == params['parent_ref'].split('@')[0]
                            for member in project['identity']['members'])}
        return _commit_operation(api, module, registry, records, events, [{'create': document}],
            before_ref=None, after_ref=new_ref, method='library.product_create', params=params,
            action='create', label='Create protein product', additions=additions)


def create_protein(api, params):
    keys(params, ('project_ref', 'expected_sha256', 'sequence', 'request_key'), ('alt_name', 'structures'))
    string(params['project_ref'], 'project_ref', 256); sha(params['expected_sha256'])
    string(params['request_key'], 'request_key', 200)
    alt_name = _alt_name(params)
    from .library_structures import prepared_sources, add_to_document
    with prepared_sources(api, params.get('structures', [])) as prepared, _opened(api, write=True) as (module, registry, records):
        events = _events(module, records)
        replay = _replay(events, api.actor, 'library.create', params['request_key'], params)
        if replay is not None:
            return replay
        project = _current(module, registry, records, params['project_ref'], params['expected_sha256'])
        require(project['kind'] == 'project' and not presentation(project)['archived'],
                'Select an active project for this protein')
        sequence = params['sequence']
        require(isinstance(sequence, str) and 0 < len(sequence) <= MAX_SEQUENCE and
                set(sequence) <= module.ALPHABETS['protein'],
                'Protein sequence must contain canonical uppercase symbols without spaces or FASTA headers')
        ident = 'protein-' + digest({'actor': api.actor, 'method': 'library.create',
                                     'request_key': params['request_key']})[:20]
        document = _new_document(ident, alt_name or 'Protein', {'molecule_type': 'protein', 'sequence': sequence}, alt_name)
        if set(sequence) - set('ACDEFGHIKLMNPQRSTVWY'):
            document['status'] = 'draft'
        new_ref = f'construct:{ident}@1'
        attachments = {}
        if prepared:
            attachments, _ = add_to_document(None, document, prepared, new_ref, hashlib.sha256(sequence.encode()).hexdigest())
        return _commit_operation(api, module, registry, records, events, [{'create': document, 'attachments': attachments}],
            before_ref=None, after_ref=new_ref, method='library.create', params=params,
            action='create', label='Create standalone protein', additions={module.reference(project): [new_ref]})


def _project_changes(module, records, changes, operation_id, additions=None, removals=None):
    """Advance each affected current project once, after all molecular changes."""
    additions = additions or {}
    removals = removals or {}
    refs = [_change_ref(module, change, records) for change in changes]
    replacements = {item['before_ref']: item['after_ref'] for item in refs if item['before_ref']}
    planned = {change['ref']: change for change in changes if 'ref' in change}
    current_projects = {}
    for record in records.values():
        if record['kind'] == 'project' and (record['id'] not in current_projects or
                                          record['revision'] > current_projects[record['id']]['revision']):
            current_projects[record['id']] = record
    for project in sorted(current_projects.values(), key=lambda value: value['id']):
        project_ref = module.reference(project)
        previous = planned.get(project_ref)
        patch = deepcopy(previous['patch']) if previous else {}
        identity = deepcopy(patch.get('identity', project['identity']))
        original = deepcopy(identity)
        identity['members'] = [member for member in identity['members']
                               if member['source_ref'] not in removals.get(project_ref, [])]
        for member in identity['members']:
            member['source_ref'] = replacements.get(member['source_ref'], member['source_ref'])
        for member_ref in additions.get(project_ref, []):
            member_ref = replacements.get(member_ref, member_ref)
            if not any(member['source_ref'] == member_ref for member in identity['members']):
                identity['members'].append({'source_ref': member_ref, 'role': ''})
        if identity == original:
            continue
        provenance = deepcopy(patch.get('provenance', project['provenance']))
        require('workbench' not in provenance or isinstance(provenance['workbench'], dict),
                'Project workbench provenance has an incompatible format', 'conflict')
        provenance.setdefault('workbench', {})['project_update'] = {
            'operation_id': operation_id, 'changed_refs': refs}
        patch.update(identity=identity, provenance=provenance)
        if previous:
            previous['patch'] = patch
        else:
            changes.append({'ref': project_ref, 'expected_sha256': project['sha256'], 'patch': patch})
    return changes


def _commit_operation(api, module, registry, records, events, changes, *, before_ref, after_ref,
                      method, params, action, label, original_operation_id=None, splices=None, additions=None, removals=None):
    require(len(events) < MAX_EVENTS, 'Library operation-history limit exceeded', 'limit')
    operation_id = uid()
    changes = registry.plan_derivations(changes, records, splices=splices)
    changes = _project_changes(module, records, changes, operation_id, additions, removals)
    require(len(changes) <= MAX_CHANGED_RECORDS, 'Too many project memberships for one edit', 'limit')
    changed_refs = [_change_ref(module, change, records) for change in changes]
    dependents = [item for item in changed_refs if item['before_ref'] and item['before_ref'] != before_ref and
                  module.translation.is_derived(records[item['before_ref']])]
    event = {
        'schema': 1, 'sequence': events[-1]['sequence'] + 1 if events else 1,
        'operation_id': operation_id, 'actor': api.actor, 'method': method,
        'request_key': params['request_key'], 'request_sha256': digest(params),
        'action': action, 'label': label, 'before_ref': before_ref, 'after_ref': after_ref,
        'changed_refs': changed_refs,
    }
    if dependents:
        event['dependent_refs'] = dependents
    if original_operation_id is not None:
        event['original_operation_id'] = original_operation_id
    primary = next(change for change in changes if _change_ref(module, change, records)['after_ref'] == after_ref)
    target = primary['create'] if 'create' in primary else primary['patch']
    target.setdefault('provenance', {}).setdefault('workbench', {})['operation'] = event
    registry._revise_many_locked(changes, records)
    events.append(event)
    return _response(event, events, api.actor)


def _publish(api, module, registry, records, events, old, document, *, method, params, action,
             label, original_operation_id=None, dependent_changes=None, splices=None, additions=None, removals=None,
             attachments=None):
    before_ref = module.reference(old)
    after_ref = f"{old['kind']}:{old['id']}@{old['revision'] + 1}"
    changes = [{'ref': before_ref, 'expected_sha256': old['sha256'], 'patch': document},
               *(dependent_changes or [])]
    if attachments:
        changes[0]['attachments'] = attachments
    return _commit_operation(api, module, registry, records, events, changes,
        before_ref=before_ref, after_ref=after_ref, method=method, params=params, action=action,
        label=label, original_operation_id=original_operation_id, splices=splices, additions=additions, removals=removals)


def edit(api, params):
    keys(params, ('ref', 'expected_sha256', 'request_key', 'patch'))
    string(params['ref'], 'ref', 256); sha(params['expected_sha256'])
    string(params['request_key'], 'request_key', 200)
    # Cap before canonical hashing: RPC already has a 2 MiB wire limit.
    require(isinstance(params['patch'], dict), 'patch must be an object')
    raw_description = _description_bytes(params['patch']['description']) if 'description' in params['patch'] else None
    with _opened(api, write=True) as (module, registry, records):
        events = _events(module, records)
        replay = _replay(events, api.actor, 'library.edit', params['request_key'], params)
        if replay is not None:
            return replay
        old = _current(module, registry, records, params['ref'], params['expected_sha256'])
        document, label = _patch(module, old, params['patch'], records, registry)
        description_changed = (raw_description is not None and
                               _description_receipt(old)['sha256'] != hashlib.sha256(raw_description).hexdigest())
        changed = _effective(module, old) != _effective(module, document) or description_changed
        splice = document['provenance'].get('workbench', {}).get('sequence_edit', {}).get('splice')
        splices = {module.reference(old): splice} if 'sequence_edit' in params['patch'] and changed and splice else None
        additions = {}
        if 'parent_ref' in params['patch']:
            parent_ref = document['identity']['encoded_by']['construct_ref']
            for project in _current_projects(records):
                if any(member['source_ref'] == module.reference(old) for member in project['identity']['members']):
                    additions[module.reference(project)] = [parent_ref]
        with _description_attachment(registry, raw_description if description_changed else None) as attachments:
            return _publish(api, module, registry, records, events, old, document,
                            method='library.edit', params=params, action='edit' if changed else 'noop', label=label,
                            splices=splices, additions=additions, attachments=attachments)


def _comparable(module, record):
    """Metadata-only parent revisions do not change a derived molecular definition."""
    value = _effective(module, record)
    # Structure curation is independent of names and molecular definitions.
    # Undo keeps its append-only assets, including uploads subsequently hidden.
    from .library_structures import FIELD, catalog
    catalog(record)
    curated = value.get('provenance', {}).get('workbench', {})
    curated.pop(FIELD, None)
    if not curated:
        value.get('provenance', {}).pop('workbench', None)
    encoded = value.get('identity', {}).get('encoded_by', {})
    if encoded.get('translation'):
        encoded['construct_ref'] = encoded['construct_ref'].split('@')[0]
    return value


def _current_parent_pin(module, registry, records, document):
    if not module.translation.is_derived(document):
        return
    encoded = document['identity']['encoded_by']
    parent = records[registry._resolve(encoded['construct_ref'].split('@')[0], records)]
    require(hashlib.sha256(parent['identity']['sequence'].encode()).hexdigest() == encoded['sequence_sha256'],
            'The parent nucleotide sequence changed. Undo or redo cannot restore coordinates for a different sequence.', 'conflict')
    encoded['construct_ref'] = module.reference(parent)


def _reverse_creation(api, module, registry, records, events, original, action, params):
    created = records[original['after_ref']]
    old = records[registry._resolve(f"{created['kind']}:{created['id']}", records)]
    current, expected = _comparable(module, old), _comparable(module, created)
    for document in (current, expected):
        curated = document['provenance'].get('workbench', {})
        curated.pop('archived', None)
    require(current == expected and presentation(old)['archived'] == (action == 'redo'),
            'This protein has another edit. Undo or redo would overwrite it.', 'conflict')
    from .library_structures import ordinary_attachments
    require(ordinary_attachments(old) == ordinary_attachments(created), 'This protein has different source attachments.', 'conflict')
    document = {key: deepcopy(value) for key, value in old.items() if key in module.USER_FIELDS}
    document['provenance'].setdefault('workbench', {})['archived'] = action == 'undo'
    _current_parent_pin(module, registry, records, document)
    return _publish(api, module, registry, records, events, old, document,
        method='library.' + action, params=params, action=action,
        label=original['label'], original_operation_id=original['operation_id'])


def _dependent_reversal(module, registry, records, original, action, parent):
    changes = []
    expected_families = set()
    for dependency in original.get('dependent_refs', []):
        before, after = records[dependency['before_ref']], records[dependency['after_ref']]
        target, restored = (after, before) if action == 'undo' else (before, after)
        family = f"{target['kind']}:{target['id']}"
        expected_families.add(family)
        old = records[registry._resolve(family, records)]
        from .library_structures import ordinary_attachments, preserve_catalog
        require(_comparable(module, old) == _comparable(module, target) and ordinary_attachments(old) == ordinary_attachments(target),
                'A derived protein has another edit. Undo or redo would overwrite its definition.', 'conflict')
        document = {key: deepcopy(value) for key, value in restored.items() if key in module.USER_FIELDS}
        preserve_catalog(old, document)
        document['parents'] = deepcopy(old['parents'])
        changes.append({'ref': module.reference(old), 'expected_sha256': old['sha256'], 'patch': document})
    if parent['kind'] == 'construct' and parent['identity'].get('molecule_type') in {'dna', 'rna'}:
        family = f"construct:{parent['id']}"
        current = {}
        for record in records.values():
            key = (record['kind'], record['id'])
            if key not in current or record['revision'] > current[key]['revision']:
                current[key] = record
        for child in current.values():
            if (module.translation.is_derived(child) and
                child['identity']['encoded_by']['construct_ref'].split('@')[0] == family and
                not presentation(child)['archived']):
                require(f"construct:{child['id']}" in expected_families,
                        'A new protein product was added after this edit. Undo would change its source.', 'conflict')
    return changes


def _reparent_memberships(module, registry, records, original, action, old, restored):
    """Reverse only memberships introduced to expose a newly selected source."""
    before, after = records[original['before_ref']], records[original['after_ref']]
    def source(record):
        return record['identity'].get('encoded_by', {}).get('construct_ref', '')
    if source(before).split('@')[0] == source(after).split('@')[0]:
        return {}, {}
    additions, removals = {}, {}
    if action == 'redo':
        parent_ref = source(restored)
        if parent_ref:
            for project in _current_projects(records):
                if any(member['source_ref'] == module.reference(old) for member in project['identity']['members']):
                    additions[module.reference(project)] = [parent_ref]
        return additions, removals
    family = source(after).split('@')[0]
    own_family = module.reference(old).split('@')[0]
    def uses_parent(ref, visited):
        if ref.split('@')[0] == family:
            return True
        if ref in visited:
            return False
        visited.add(ref)
        for _, value in module.reference_values(records[ref]['identity']):
            for child in value if isinstance(value, list) else [value]:
                if uses_parent(child, visited):
                    return True
        return False
    for changed in original['changed_refs']:
        prior = records.get(changed['before_ref'])
        if prior is None or prior['kind'] != 'project':
            continue
        if any(member['source_ref'].split('@')[0] == family for member in prior['identity']['members']):
            continue  # The parent already belonged to this project before reparenting.
        added = next((member for member in records[changed['after_ref']]['identity']['members']
                      if member['source_ref'].split('@')[0] == family), None)
        if added is None:
            continue
        project = records[registry._resolve('project:' + prior['id'], records)]
        selected = next((member for member in project['identity']['members']
                         if member['source_ref'].split('@')[0] == family), None)
        if selected is None:
            continue
        require(selected['role'] == added['role'],
                'The added parent membership has another edit. Undo would overwrite it.', 'conflict')
        for member in project['identity']['members']:
            if member['source_ref'].split('@')[0] in {family, own_family}:
                continue
            require(not uses_parent(member['source_ref'], set()),
                    'The added parent is now used by another project member. Undo would hide its source.', 'conflict')
        removals[module.reference(project)] = [selected['source_ref']]
    return additions, removals


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
        if original['action'] == 'create':
            return _reverse_creation(api, module, registry, records, events, original, action, params)
        if original['method'] in {'library.structure_attach', 'library.structure_visibility'}:
            from .library_structures import reverse_structure
            return reverse_structure(api, module, registry, records, events, original, action, params)
        before, after = records[original['before_ref']], records[original['after_ref']]
        target = after if action == 'undo' else before
        restored = before if action == 'undo' else after
        current_ref = registry._resolve(f"{target['kind']}:{target['id']}", records)
        old = records[current_ref]
        attachments = {}
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
            if _description_receipt(before) != _description_receipt(after):
                require(_description_receipt(old) == _description_receipt(target),
                        'This project description has another edit. Undo or redo would overwrite it; refresh the library.', 'conflict')
                attachments['project.md'] = registry._path(module.reference(restored)).parent / 'attachments/project.md'
            # Description is an independently editable project field. Reversing
            # a label/archive action must retain another actor's newer brief.
            def other_attachments(record):
                return [item for item in record['attachments'] if item['path'] != 'attachments/project.md']
            require(other_attachments(old) == other_attachments(target),
                    'This entry has different source attachments; undo or redo cannot overwrite them.', 'conflict')
        else:
            require(_comparable(module, old) == _comparable(module, target),
                    'This entry has another edit. Undo or redo would overwrite it; refresh the library.', 'conflict')
            document = {key: deepcopy(value) for key, value in restored.items() if key in module.USER_FIELDS}
            from .library_structures import ordinary_attachments, preserve_catalog
            require(ordinary_attachments(old) == ordinary_attachments(target),
                    'This entry has different source attachments; undo or redo cannot overwrite them.', 'conflict')
            preserve_catalog(old, document)
        document['parents'] = deepcopy(old['parents'])
        _current_parent_pin(module, registry, records, document)
        # Reverse the actual last transaction, which may have included products
        # created and then archived after the original edit. Restoring only the
        # original narrower dependency list would lose their coordinate ranges
        # across a complete Undo/Redo cycle.
        previous = next((event for event in reversed(events)
                         if event.get('original_operation_id') == original['operation_id'] and
                         event['action'] == ('undo' if action == 'redo' else 'redo')), None)
        dependents = _dependent_reversal(module, registry, records, previous or original, 'undo', old)
        additions, removals = _reparent_memberships(module, registry, records, original, action, old, document)
        return _publish(api, module, registry, records, events, old, document,
                        method=method, params=params, action=action, label=original['label'],
                        original_operation_id=original['operation_id'], dependent_changes=dependents,
                        additions=additions, removals=removals, attachments=attachments)


def undo(api, params):
    return _reverse(api, params, 'undo')


def redo(api, params):
    return _reverse(api, params, 'redo')
