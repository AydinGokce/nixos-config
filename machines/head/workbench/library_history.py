"""Actor-scoped job history associated with exact, retained library revisions.

A name or sequence match is never sufficient. The job's original batch must
explicitly name a pinned library construct, or a pinned assembly containing it.
Floating names are intentionally excluded: resolving them today could assign an
old prediction to a different molecule.
"""
from __future__ import annotations

import json

from .common import identifier, keys, number, parse, require, string
from .library_api import opened


def _sources(records, target, include_revisions):
    selected = records[target]
    wanted = {ref for ref, record in records.items()
              if (record['kind'], record['id']) == (selected['kind'], selected['id'])
              and (include_revisions or ref == target)}
    sources = {ref: [ref] for ref in wanted}
    if selected['kind'] == 'construct':
        for ref, record in records.items():
            if record['kind'] != 'assembly':
                continue
            matches = sorted({part['construct_ref'] for part in record['identity']['components']}
                             & wanted)
            if matches:
                sources[ref] = matches
    return sources


def _associated_inputs(job, batch):
    document = batch.get('_request', {})
    for item in document.get('inputs', []):
        if document.get('mode') != 'assembly' and item.get('id') != job.get('input_id'):
            continue
        source = item.get('source', {})
        if source.get('kind') == 'library':
            yield source.get('ref')


def list_runs(api, params):
    keys(params, ('ref',), ('limit', 'cursor', 'include_revisions'))
    string(params['ref'], 'ref', 256)
    limit = number(params.get('limit', 30), 'limit', 1, 100)
    include_revisions = params.get('include_revisions', True)
    require(type(include_revisions) is bool, 'include_revisions must be a boolean')
    cursor = params.get('cursor')
    if cursor is not None:
        identifier(cursor)
    with opened(api) as (_, registry, records):
        ref = registry._resolve(params['ref'], records)
        require(records[ref]['kind'] in {'construct', 'assembly'},
                'Run history requires a molecular construct or assembly')
        sources = _sources(records, ref, include_revisions)
    # The original input declaration is the association. Both sides of the join
    # must belong to this actor; the shared library does not share job access.
    where = """
      j.kind='job' AND j.actor=? AND b.kind='batch' AND b.actor=?
      AND EXISTS (
        SELECT 1 FROM json_each(b.data, '$._request.inputs') i
        WHERE json_extract(i.value,'$.source.kind')='library'
          AND json_extract(i.value,'$.source.ref') IN (SELECT value FROM json_each(?))
          AND (json_extract(b.data,'$._request.mode')='assembly'
               OR json_extract(i.value,'$.id')=json_extract(j.data,'$.input_id'))
      )
    """
    join = "objects j JOIN objects b ON b.id=json_extract(j.data,'$.batch_id')"
    args = [api.actor, api.actor, json.dumps(sorted(sources))]
    with api.store.connection() as db:
        db.execute('BEGIN')
        if cursor is not None:
            old = db.execute('SELECT j.created,j.id FROM ' + join + ' WHERE ' + where + ' AND j.id=?',
                             [*args, cursor]).fetchone()
            require(old is not None, 'Unknown run history cursor', 'not_found')
            where += ' AND (j.created<? OR (j.created=? AND j.id<?))'
            args.extend([old['created'], old['created'], old['id']])
        page = db.execute('SELECT j.data AS job,b.data AS batch FROM ' + join + ' WHERE ' + where
                          + ' ORDER BY j.created DESC,j.id DESC LIMIT ?', [*args, limit + 1]).fetchall()
        result = []
        for row in page[:limit]:
            job, batch = parse(row['job']), parse(row['batch'])
            refs = sorted({source_ref for original in _associated_inputs(job, batch)
                           for source_ref in sources.get(original, [])})
            artifacts = db.execute("SELECT COUNT(*),COALESCE(SUM(CASE WHEN "
                "LOWER(json_extract(data,'$.format')) IN ('pdb','cif','mmcif') "
                "OR LOWER(json_extract(data,'$.name')) GLOB '*.pdb' "
                "OR LOWER(json_extract(data,'$.name')) GLOB '*.cif' "
                "OR LOWER(json_extract(data,'$.name')) GLOB '*.mmcif' "
                "THEN 1 ELSE 0 END),0) FROM objects WHERE kind='artifact' AND actor=? "
                "AND json_extract(data,'$.job_id')=?", (api.actor, job['job_id'])).fetchone()
            result.append({**{key: job.get(key) for key in (
                'job_id', 'batch_id', 'input_name', 'model', 'state',
                'created_at', 'updated_at', 'started_at', 'finished_at')},
                'batch_name': batch.get('name', ''), 'source_refs': refs,
                'selected_revision': ref in refs, 'artifact_count': artifacts[0],
                'structure_count': artifacts[1]})
        return {'ref': ref, 'records': result,
                'next_cursor': result[-1]['job_id'] if len(page) > limit else None,
                'include_revisions': include_revisions, 'scope': 'current_actor',
                'match': 'explicit_library_reference'}
