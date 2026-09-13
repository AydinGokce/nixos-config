"""Read sealed native candidate evidence and save revision-bound library proteins."""
from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import math
from pathlib import Path
import re
import shutil
import tempfile

from .common import (WIRE, canonical, digest, file_sha, identifier, keys, number, parse,
                     require, safe_file, sha, string)

AA = set('ACDEFGHIKLMNPQRSTVWY')
METRICS = {'plddt': 'pLDDT', 'iptm': 'i_pTM', 'pae': 'pAE', 'interface_pae': 'i_pAE',
           'rosetta_dg': 'dG', 'interface_hbonds': 'n_InterfaceHbonds',
           'interface_unsatisfied_hbonds': 'n_InterfaceUnsatHbonds',
           'interface_sasa': 'dSASA', 'binder_rmsd': 'Binder_RMSD'}


def artifacts(api, job_id):
    job = api.store.read('job', job_id, api.actor)
    require(job['model'] == 'bindcraft', 'Select a BindCraft job')
    with api.store.connection() as db:
        rows = db.execute("SELECT data FROM objects WHERE kind='artifact' AND actor=? "
                          "AND json_extract(data,'$.job_id')=? ORDER BY created,id", (api.actor, job_id))
        return job, [parse(row['data']) for row in rows]


def verified_file(api, artifact, maximum=64 * 1024 * 1024):
    require(0 <= artifact['size'] <= maximum, 'Candidate evidence exceeds the supported size limit', 'limit')
    path = safe_file(api.store.directory('artifacts', artifact['artifact_id']) / 'content')
    require(path.stat().st_size == artifact['size'] and file_sha(path) == artifact['sha256'],
            'Candidate artifact SHA-256 differs', 'integrity')
    return path


def named(items, suffix):
    found = [a for a in items if a['name'] == suffix or a['name'].endswith('/' + suffix)]
    require(len(found) <= 1, 'Ambiguous retained BindCraft evidence: ' + suffix, 'integrity')
    return found[0] if found else None


def csv_rows(api, artifact):
    if artifact is None:
        return []
    with verified_file(api, artifact).open(newline='') as stream:
        reader = csv.DictReader(stream)
        require(reader.fieldnames and len(reader.fieldnames) == len(set(reader.fieldnames)),
                'Malformed native candidate CSV headers', 'integrity')
        rows = []
        for row in reader:
            require(len(rows) < 20000 and None not in row and all(isinstance(v, str) and len(v) <= 16384 for v in row.values()),
                    'Malformed or oversized native candidate CSV', 'integrity')
            rows.append(row)
        return rows


def numeric(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def checked_result(api, job, items):
    artifact = named(items, 'bindcraft-result.json')
    value = parse(verified_file(api, artifact).read_bytes()) if artifact else None
    if value is not None:
        expected = job['_prepared']['settings']['input_manifest']
        require(isinstance(value, dict) and value.get('kind') == 'bindcraft-result' and
                type(value.get('schema')) is int and value.get('schema') == 1 and value.get('input_manifest') == expected,
                'Native BindCraft result belongs to a different input manifest', 'integrity')
    return artifact, value


def prepared_file(job, name):
    prepared = job['_prepared']
    expected = prepared['input_files'].get(name)
    require(expected is not None, 'Missing validated binder input: ' + name, 'integrity')
    path = safe_file(Path(prepared['input_root']) / name)
    require(path.stat().st_size == expected['size'] and file_sha(path) == expected['sha256'],
            'Validated binder input changed: ' + name, 'integrity')
    return path


def provenance(job):
    value = parse(prepared_file(job, 'binder-provenance.json').read_bytes())
    require(value == job['_prepared']['settings'] and value.get('workflow') == 'bindcraft',
            'Binder provenance binding differs', 'integrity')
    return value


def seal_inputs(store, job, root):
    """Archive exact original and submitted structures before paid launch."""
    from .runner import seal_results
    value = provenance(job)
    source_root = root / 'binder-input-evidence'
    destination = source_root / 'binder-context'
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    original_name = 'original-target.' + ('cif' if value['target_format'] == 'mmcif' else 'pdb')
    for name, source in ((original_name, prepared_file(job, 'original-structure')),
                         ('submitted-target.pdb', prepared_file(job, 'target.pdb')),
                         ('binder-provenance.json', prepared_file(job, 'binder-provenance.json'))):
        target = destination / name
        if target.exists():
            require(file_sha(target) == file_sha(source), 'Existing binder input artifact changed', 'integrity')
        else:
            shutil.copyfile(source, target)
    return seal_results(store, job, source_root, 'binder-input')


def context(api, params):
    keys(params, ('job_id',), ('artifact_id',))
    job, items = artifacts(api, params['job_id'])
    checked_result(api, job, items)
    value = provenance(job)
    original_name = 'binder-context/original-target.' + ('cif' if value['target_format'] == 'mmcif' else 'pdb')
    original = named(items, original_name)
    submitted_artifact = named(items, 'binder-context/submitted-target.pdb')
    if original is None and value['target']['kind'] == 'artifact':
        candidate = api.store.read('artifact', value['target']['id'], api.actor)
        require(candidate['sha256'] == value['target']['sha256'], 'Original artifact hash changed', 'integrity')
        original = candidate
    if original is not None:
        verified_file(api, original)
        require(original['sha256'] == value['target']['sha256'], 'Original structure provenance hash differs', 'integrity')
    if submitted_artifact is not None:
        verified_file(api, submitted_artifact)
        require(submitted_artifact['sha256'] == value['submitted_target_sha256'], 'Submitted structure hash differs', 'integrity')
    result = {'schema': 1, 'job_id': params['job_id'], 'artifact_id': params.get('artifact_id'),
              'target': value['target'], 'target_context': value.get('target_context', {}),
              'original_structure_artifact': original, 'submitted_structure_artifact': submitted_artifact,
              'output_structure_artifact': None,
              'residue_map': value['residue_map'], 'submitted_chains': value['submitted_chains'],
              'output_mapping': {'status': 'unavailable', 'pairs': [], 'reason': 'Select a retained candidate structure'}}
    if params.get('artifact_id'):
        output = api.store.read('artifact', params['artifact_id'], api.actor)
        require(output['job_id'] == job['job_id'] and output['model'] == 'bindcraft' and
                output['format'] == 'pdb' and 'designs' in Path(output['name']).parts,
                'Select a predicted structure from this BindCraft job')
        result['output_structure_artifact'] = output
        from .binder_structure import inspect_structure
        submitted = inspect_structure(prepared_file(job, 'target.pdb').read_bytes(), filename='submitted.pdb')
        predicted = inspect_structure(verified_file(api, output).read_bytes(), filename='candidate.pdb')
        ordered = []
        by_chain = {chain['chain']: chain for chain in submitted['chains']}
        for chain in value['submitted_chains']:
            actual = by_chain.get(chain['chain'])
            require(actual and actual['sequence'] == chain['sequence'] and actual['residue_count'] == chain['residue_count'],
                    'Submitted chain-order/sequence provenance differs', 'integrity')
            ordered.extend(actual['residues'])
        target_chains = [chain for chain in predicted['chains'] if chain['chain'] == 'A']
        expected_sequence = ''.join(chain['sequence'] for chain in value['submitted_chains'])
        mapping = {canonical(entry['submitted']): entry for entry in value['residue_map']}
        require(len(mapping) == len(value['residue_map']) == len(ordered), 'Submitted residue mapping is not one-to-one', 'integrity')
        if len(target_chains) != 1 or target_chains[0]['sequence'] != expected_sequence or len(target_chains[0]['residues']) != len(ordered):
            result['output_mapping']['reason'] = 'Output target chain A does not exactly match the submitted target sequence/order; alignment unavailable'
        else:
            pairs = []
            for residue, predicted_residue in zip(ordered, target_chains[0]['residues']):
                ref = {key: residue[key] for key in ('chain', 'number', 'insertion_code')}
                require(canonical(ref) in mapping, 'Submitted residue is absent from the original map', 'integrity')
                pairs.append({**mapping[canonical(ref)], 'output': {
                    key: predicted_residue[key] for key in ('chain', 'number', 'insertion_code')}})
            # Pinned ColabDesign prep_pdb iterates requested chains in order;
            # _prep_binder collapses them into length[0]; save_pdb renumbers
            # this target as chain A but can retain inter-fragment +50 gaps.
            # Read the actual output IDs above instead of inventing 1..N IDs.
            result['output_mapping'] = {'status': 'available', 'pairs': pairs,
                'basis': 'Pinned ColabDesign target concatenation order, exact submitted/output sequence equality, actual output residue identifiers',
                'artifact_id': output['artifact_id'], 'sha256': output['sha256']}
    require(len(canonical(result)) < WIRE - 4096,
            'Full target mapping exceeds RPC response size; download binder-provenance.json for the complete map', 'limit')
    return result


def _read(api, job_id):
    job, items = artifacts(api, job_id)
    result, result_value = checked_result(api, job, items)
    candidate_rows = []
    for kind, filename in (('trajectory', 'trajectory_stats.csv'), ('mpnn', 'mpnn_design_stats.csv')):
        artifact = named(items, 'designs/' + filename)
        for row in csv_rows(api, artifact):
            candidate_rows.append((kind, artifact, row))
    candidates, names = [], set()
    for kind, table, row in candidate_rows:
        name, sequence = row.get('Design'), row.get('Sequence')
        require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,200}', name)
                and name not in names, 'Invalid or duplicate native candidate identity', 'integrity')
        require(isinstance(sequence, str) and 5 <= len(sequence) <= 1000 and set(sequence) <= AA,
                'Native candidate lacks an unambiguous canonical protein sequence', 'integrity')
        names.add(name)
        structures = []
        for artifact in items:
            parts = Path(artifact['name']).parts
            if artifact['format'] != 'pdb' or 'designs' not in parts or 'Ranked' in parts:
                continue
            stem = Path(artifact['name']).stem
            if (kind == 'trajectory' and stem == name or
                    kind == 'mpnn' and re.fullmatch(re.escape(name) + r'_model\d+', stem)):
                structures.append(artifact)
        accepted = any('Accepted' in Path(a['name']).parts for a in structures)
        rejected = any('Rejected' in Path(a['name']).parts for a in structures)
        require(not (accepted and rejected), 'Candidate has conflicting native acceptance evidence', 'integrity')
        status = 'trajectory' if kind == 'trajectory' else 'accepted' if accepted else 'rejected' if rejected else 'unclassified'
        structures.sort(key=lambda a: (0 if 'Accepted' in Path(a['name']).parts else
                                       1 if 'Rejected' in Path(a['name']).parts else 2, a['name']))
        metrics = {key: numeric(row.get(('Average_' if kind == 'mpnn' else '') + column)) for key, column in METRICS.items()}
        candidate = {'candidate_id': digest({'job_id': job_id, 'name': name})[:32], 'name': name,
                     'status': status, 'sequence': sequence, 'sequence_sha256': hashlib.sha256(sequence.encode()).hexdigest(),
                     'length': len(sequence), 'seed': row.get('Seed'), 'metrics': metrics,
                     'native_metrics': {key: val for key, value in row.items() if (val := numeric(value)) is not None},
                     'structure_artifacts': [{key: value for key, value in a.items() if not key.startswith('_')} for a in structures],
                     'provenance': {'job_id': job_id, 'table_artifact_id': table['artifact_id'], 'table_sha256': table['sha256'],
                                    'native_design_name': name, 'binder_chain': 'B',
                                    'acceptance': 'Native computational filters; experimental binding has not been established'},
                     '_row': row, '_table': table}
        candidates.append(candidate)
    failures = csv_rows(api, named(items, 'designs/failure_csv.csv'))
    failure_counts = {key: val for row in failures for key, value in row.items()
                      if (val := numeric(value)) is not None and val > 0}
    return {'schema': 1, 'job_id': job_id, 'state': job['state'], 'candidates': candidates,
            'summary': {'total': len(candidates), **{state: sum(c['status'] == state for c in candidates)
                         for state in ('accepted', 'rejected', 'trajectory', 'unclassified')},
                        'elapsed_seconds': result_value.get('elapsed_seconds') if isinstance(result_value, dict) else None,
                        'native_status': result_value.get('status') if isinstance(result_value, dict) else None,
                        'filter_failures': failure_counts, 'artifacts_sealed': bool(result or candidate_rows)},
            'warnings': ['Candidates appear after managed output transfer and archival.'] if not (result or candidate_rows) else [],
            '_job': job}


def candidates(api, params):
    keys(params, ('job_id',), ('limit', 'cursor'))
    limit = number(params.get('limit', 100), 'limit', 1, 100)
    result = _read(api, params['job_id'])
    values = result['candidates']
    if params.get('cursor'):
        cursor = identifier(params['cursor'])
        positions = [i for i, candidate in enumerate(values) if candidate['candidate_id'] == cursor]
        require(positions, 'Unknown candidate cursor')
        values = values[positions[0] + 1:]
    result['candidates'] = values[:limit]
    result['next_cursor'] = values[limit - 1]['candidate_id'] if len(values) > limit else None
    return result


def save(api, params):
    keys(params, ('job_id', 'candidate_id', 'project_ref', 'expected_sha256', 'request_key'), ('alt_name',))
    identifier(params['candidate_id']); string(params['request_key'], 'request_key', 200)
    string(params['project_ref'], 'project_ref', 256); sha(params['expected_sha256'])
    from . import library_edits as edits
    alt_name = edits._alt_name(params)
    # Actor ownership is required even for an idempotent library replay.
    job = api.store.read('job', params['job_id'], api.actor)
    require(job['model'] == 'bindcraft', 'Select a BindCraft job')
    with edits._opened(api, write=True) as (module, registry, records):
        events = edits._events(module, records)
        replay = edits._replay(events, api.actor, 'binder.save', params['request_key'], params)
        if replay is not None:
            return replay
        project = edits._current(module, registry, records, params['project_ref'], params['expected_sha256'])
        require(project['kind'] == 'project' and not edits.presentation(project)['archived'], 'Select an active project')
        data = _read(api, params['job_id'])
        matches = [c for c in data['candidates'] if c['candidate_id'] == params['candidate_id']]
        require(len(matches) == 1, 'Candidate not found', 'not_found')
        candidate = matches[0]
        require(candidate['structure_artifacts'], 'Candidate has no retained structure to verify', 'conflict')
        representative = candidate['structure_artifacts'][0]
        structure = verified_file(api, representative)
        from .binder_structure import inspect_structure
        inspection = inspect_structure(structure.read_bytes(), filename='candidate.pdb')
        binder_chains = [chain for chain in inspection['chains'] if chain['chain'] == 'B']
        require(len(binder_chains) == 1 and binder_chains[0]['sequence'] == candidate['sequence'],
                'Native candidate CSV sequence differs from its predicted binder chain', 'integrity')
        source_provenance = provenance(job)
        alignment = context(api, {'job_id': job['job_id'], 'artifact_id': representative['artifact_id']})
        require(alignment['output_mapping']['status'] == 'available',
                'Candidate output target cannot be matched to its submitted provenance', 'integrity')
        saved_provenance = deepcopy(source_provenance)
        prepared = job['_prepared']; prepared_root = Path(prepared['input_root'])
        target = safe_file(prepared_root / 'target.pdb')
        require(file_sha(target) == saved_provenance['submitted_target_sha256'] == prepared['input_files']['target.pdb']['sha256'],
                'Submitted target structure changed', 'integrity')
        saved = {key: value for key, value in candidate.items() if not key.startswith('_')}
        saved_provenance['candidate'] = saved
        saved_provenance['output_mapping'] = alignment['output_mapping']
        raw = canonical(saved_provenance)
        identity = 'binder-' + digest({'actor': api.actor, 'request_key': params['request_key']})[:20]
        metadata = {'job_id': params['job_id'], 'candidate_id': candidate['candidate_id'],
                    'status': candidate['status'], 'target': saved_provenance['target'],
                    'target_context': saved_provenance.get('target_context', {}), 'hotspots': saved_provenance['hotspots'],
                    'settings': saved_provenance['settings'], 'candidate_sequence_sha256': candidate['sequence_sha256'],
                    'predicted_structure_sha256': representative['sha256'],
                    'provenance_attachment': 'attachments/bindcraft-provenance.json',
                    'provenance_sha256': hashlib.sha256(raw).hexdigest()}
        document = edits._new_document(identity, alt_name or candidate['name'],
                    {'molecule_type': 'protein', 'sequence': candidate['sequence']}, alt_name, {'bindcraft': metadata})
        document['tags'] = ['bindcraft', 'binder-candidate']
        description = ('# ' + (alt_name or candidate['name']) + '\n\n'
                       'Computational BindCraft candidate for the selected target structure and hotspot patch.\n\n'
                       'Native filter status: ' + candidate['status'] + '. Experimental binding has not been established.\n\n'
                       'Target SHA-256: `' + saved_provenance['target']['sha256'] + '`.\n'
                       'Job: `' + params['job_id'] + '`. Full settings, residue mapping, target context and candidate evidence are in `bindcraft-provenance.json`.\n')
        with tempfile.TemporaryDirectory(prefix='binder-save-') as directory:
            folder = Path(directory)
            (folder / 'bindcraft-provenance.json').write_bytes(raw)
            (folder / 'description.md').write_text(description)
            attachments = {'bindcraft-provenance.json': folder / 'bindcraft-provenance.json',
                           'description.md': folder / 'description.md', 'predicted-complex.pdb': structure,
                           'design-target.pdb': target}
            new_ref = 'construct:' + identity + '@1'
            return edits._commit_operation(api, module, registry, records, events,
                [{'create': document, 'attachments': attachments}], before_ref=None, after_ref=new_ref,
                method='binder.save', params=params, action='create', label='Save BindCraft candidate',
                additions={module.reference(project): [new_ref]})
