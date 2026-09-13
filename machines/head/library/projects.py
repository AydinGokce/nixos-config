"""Versioned research intent, separate from molecular identity and evidence.

Markdown is retained as an immutable attachment. It describes goals and
hypotheses, not verified biological function or instructions to run resources.
"""
import copy
import re

DESCRIPTION = 'description.md'
OBJECTIVES = 'project.md'
MAX_MARKDOWN_BYTES = 1024 * 1024
INCOMPLETE_MARKER = '<!-- bio-library:purpose-scaffold:v1 incomplete -->'
MEMBER_REF = re.compile(r'(construct|assembly):[a-z][a-z0-9_-]{0,63}@[1-9][0-9]*\Z')


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def markdown_bytes(raw, *, require=_require, allow_empty=False):
    require(isinstance(raw, bytes) and (allow_empty or len(raw) > 0) and len(raw) <= MAX_MARKDOWN_BYTES,
            'Purpose Markdown must be no larger than 1 MiB' if allow_empty else
            'Purpose Markdown must be nonempty and no larger than 1 MiB')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        require(False, 'Purpose Markdown must be UTF-8')
    require((allow_empty or text.strip()) and '\x00' not in text,
            'Purpose Markdown must contain no NUL bytes' if allow_empty else
            'Purpose Markdown must contain text and no NUL bytes')
    return raw  # Preserve original newlines and bytes, including CRLF.


def filename(kind):
    return OBJECTIVES if kind == 'project' else DESCRIPTION if kind in {'construct', 'assembly'} else None


def scaffold(kind, name):
    if kind == 'project':
        text = f'''# {name}: project brief

## Objectives

Not yet specified. State the scientific or engineering problem and intended use.

## Scope and constraints

Not yet specified. Record required conditions, exclusions and practical limits.

## Hypotheses

Not yet specified. Distinguish proposed mechanisms from established evidence.

## Testable success criteria

Not yet specified. For each criterion, give a metric or observation, acceptance
threshold, test conditions, method and required evidence.

## Evidence and limitations

No functional claim is established by this scaffold. Prediction confidence and
chemical output checks do not demonstrate biological function or experimental
success. Link evidence to the exact project and molecular revisions assessed.
'''
    else:
        text = f'''# {name}: intended purpose

## Intended function

Not yet specified. Describe the desired function and intended operating context.

## Design rationale and hypotheses

Not yet specified. Explain the proposed mechanism and identify untested assumptions.

## Testable success criteria

Not yet specified. For each criterion, give a metric or observation, acceptance
threshold, test conditions, method and required evidence.

## Evidence and limitations

No function has been demonstrated by this scaffold. A plausible predicted
structure, model confidence score or passed chemistry check does not establish
function. Record evidence against the exact construct/assembly revision.
'''
    return (INCOMPLETE_MARKER+'\n\n'+text).encode('utf-8')


def markdown_state(raw, *, allow_empty=False):
    """A non-scaffold document is unassessed, never automatically complete."""
    markdown_bytes(raw, allow_empty=allow_empty)
    if not raw.decode('utf-8').strip():
        return 'incomplete_empty'
    return 'incomplete_scaffold' if INCOMPLETE_MARKER.encode() in raw else 'unassessed'


def validate_identity(identity, *, require=_require, pinned=True):
    require(isinstance(identity, dict) and set(identity) == {'objectives_file', 'members'},
            'Project identity requires only objectives_file and members')
    require(identity['objectives_file'] == 'attachments/'+OBJECTIVES,
            'Project objectives_file must be attachments/project.md')
    members = identity['members']
    require(isinstance(members, list), 'Project members must be a list')
    references = []
    for member in members:
        require(isinstance(member, dict) and set(member) == {'source_ref', 'role'},
                'Each project member requires source_ref and role')
        ref = member['source_ref']
        require(isinstance(ref, str) and ref and (not pinned or MEMBER_REF.fullmatch(ref)),
                'Project members must pin construct or assembly revisions')
        require(isinstance(member['role'], str) and '\x00' not in member['role'],
                'Project member role must be Markdown text without NUL bytes')
        require(len(member['role'].encode('utf-8')) <= MAX_MARKDOWN_BYTES,
                'Project member role is too large')
        references.append(ref)
    require(len(references) == len(set(references)), 'Project member revision is duplicated')


def pin_identity(identity, resolve, *, require=_require):
    validate_identity(identity, require=require, pinned=False)
    result = copy.deepcopy(identity)
    for member in result['members']:
        member['source_ref'] = resolve(member['source_ref'])
    validate_identity(result, require=require)
    return result


def project_document(identifier, name=None, members=None, **metadata):
    """Build an import document; the registry pins member aliases atomically."""
    _require(not set(metadata) - {'aliases', 'tags', 'notes', 'parents', 'status', 'provenance'},
             'Unknown project metadata')
    members = [] if members is None else copy.deepcopy(members)
    _require(isinstance(members, list), 'Project members must be a list')
    normalized = []
    for member in members:
        if isinstance(member, str):
            member = {'source_ref': member, 'role': ''}
        elif isinstance(member, dict):
            member.setdefault('role', '')
        normalized.append(member)
    identity = {'objectives_file': 'attachments/'+OBJECTIVES, 'members': normalized}
    validate_identity(identity, pinned=False)
    return {'kind': 'project', 'id': identifier, 'name': name or identifier, 'identity': identity, **metadata}


def description_receipt(record):
    """No mutable latest-document lookup: use only this exact record revision."""
    name = filename(record['kind'])
    if name is None:
        return {'present': False}
    for item in record['attachments']:
        if item['path'] == 'attachments/'+name:
            return {'present': True, **copy.deepcopy(item)}
    return {'present': False}
