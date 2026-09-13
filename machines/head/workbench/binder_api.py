"""Structure-bound BindCraft requests through the existing durable dispatcher."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .common import (WIRE, canonical, file_sha, identifier, inventory, keys, number,
                     parse, read_json, require, safe_file, sha, string, uid, write_json)

DEFAULTS = {'lengths': [65, 150], 'designs': 100, 'timeout_seconds': 7200,
            'max_cost_usd': 10.0, 'seed': None}
MAX_TARGET = 32 * 1024 * 1024


def configuration(api):
    from .service import configuration as configured
    return api.worker_config or api.library_config or configured(os.environ.get('BIO_WORKBENCH_CONFIG'))


def bundle_module(tools):
    path = Path(tools) / 'bindcraft/bundle.py'
    spec = importlib.util.spec_from_file_location('_workbench_bindcraft_bundle', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def catalog(api, params):
    keys(params)
    config = configuration(api)
    manifest = Path(config.get('bindcraft_shared', '/mnt/bio-shared')) / 'bindcraft/install-manifest.json'
    ready, status, license_scope = False, 'not_installed', None
    try:
        value = read_json(manifest)
        ready = (value.get('schema') == 'bio-bindcraft-install.v1' and value.get('readiness') == 'ready'
                 and all(value.get('components', {}).get(name, {}).get('ready') is True
                         for name in ('environment', 'sources', 'af2', 'pyrosetta')))
        status = 'ready' if ready else 'incomplete'
        license_scope = value.get('components', {}).get('pyrosetta', {}).get('use_scope')
    except (ValueError, OSError):
        pass
    return {'schema': 1, 'enabled': True, 'runtime_ready': ready, 'runtime_status': status,
            'runtime_check': 'Installation receipt; complete integrity checks run before any allocation',
            'defaults': deepcopy(DEFAULTS), 'formats': ['pdb', 'mmcif'], 'msa_required': False,
            'limits': {'target_bytes': MAX_TARGET, 'length_min': 5, 'length_max': 1000,
                       'designs_min': 1, 'designs_max': 10000, 'timeout_min': 60,
                       'timeout_max': 85500, 'cost_min_usd': 0.01, 'cost_max_usd': 750,
                       'seed_min': 0, 'seed_max': 2147483647},
            'patch_scope': 'frontend_session_exact_target_sha256',
            'designs_meaning': 'Accepted designs requested; rejected attempts can continue until the runtime bound',
            'cost_scope': 'Maximum freshly quoted GPU plus disposable OS reservation, including startup/cleanup allowance; shared head/storage are accounted by the separate project budget',
            'license_scope': license_scope}


def target_source(api, target):
    keys(target, ('kind', 'id', 'sha256'), ('source_ref', 'project_ref'))
    require(target['kind'] in {'upload', 'artifact'}, 'Select an uploaded or retained target structure')
    identifier(target['id']); sha(target['sha256'])
    kind = target['kind']
    item = api.store.read(kind, target['id'], api.actor)
    if kind == 'upload':
        require(item['state'] == 'complete', 'Target upload is incomplete', 'conflict')
    else:
        require(item.get('format') in {'pdb', 'cif', 'mmcif'}, 'Target artifact must be a PDB or mmCIF structure')
    require(item['sha256'] == target['sha256'], 'Target structure SHA-256 differs', 'integrity')
    require(0 < item['size'] <= MAX_TARGET, 'Target structure exceeds 32 MiB', 'limit')
    path = safe_file(api.store.directory(kind + 's', target['id']) / 'content')
    require(path.stat().st_size == item['size'] and file_sha(path) == target['sha256'],
            'Retained target bytes differ from their exact hash', 'integrity')
    for key in ('source_ref', 'project_ref'):
        if key in target:
            string(target[key], key, 256)
    return path, item


def target_context(api, target):
    refs = {key: target[key] for key in ('source_ref', 'project_ref') if key in target}
    if not refs:
        return {}
    from .library_api import opened
    result = {}
    with opened(api) as (module, registry, records):
        for key, ref in refs.items():
            module.pinned_parts(ref)
            resolved = registry._resolve(ref, records, 'project' if key == 'project_ref' else 'construct')
            require(resolved == ref, 'Target context must use an exact library revision')
            record = records[ref]
            if key == 'source_ref':
                require(record['identity'].get('molecule_type') == 'protein', 'Target source must be a protein construct')
            result[key] = {'ref': ref, 'sha256': record['sha256'],
                           'relationship': 'User-selected context; record existence and hash verified, structural derivation not inferred'}
    return result


def inspect_target(api, params):
    keys(params, ('target',))
    from .binder_structure import inspect_structure
    path, item = target_source(api, params['target'])
    inspected = inspect_structure(path.read_bytes(), filename=item['name'])
    result = {'schema': 1, 'target': deepcopy(params['target']), 'target_name': item['name'],
              **inspected, 'context': target_context(api, params['target'])}
    require(len(canonical(result)) < WIRE - 4096,
            'Target inspection exceeds the response limit; upload a smaller target structure', 'limit')
    return result


def _residues(value, label):
    require(isinstance(value, list) and len(value) <= 10000, label + ' must be a bounded residue list')
    normalized = []
    for item in value:
        keys(item, ('chain', 'number'), ('insertion_code',))
        chain = _chain(item['chain'])
        position = number(item['number'], label + ' number', -999999, 9999999)
        insertion = item.get('insertion_code', '')
        require(isinstance(insertion, str) and len(insertion) <= 1 and
                (not insertion or insertion.isascii() and insertion.isalnum()), 'Invalid insertion code')
        normalized.append({'chain': chain, 'number': position, 'insertion_code': insertion})
    require(len({canonical(item) for item in normalized}) == len(normalized), 'Duplicate ' + label + ' residue')
    return normalized


def _chain(value):
    require(isinstance(value, str) and len(value) <= 128 and not any(ord(c) < 32 for c in value),
            'Invalid original target chain')
    return value


def request(api, params):
    keys(params, ('request_key', 'name', 'target', 'chains'),
         ('hotspots', 'crop', 'lengths', 'designs', 'timeout_seconds', 'max_cost_usd', 'seed'))
    string(params['request_key'], 'request_key', 200); string(params['name'], 'name', 200)
    target_source(api, params['target'])
    require(isinstance(params['chains'], list) and 1 <= len(params['chains']) <= 52,
            'Select 1..52 target protein chains')
    for chain in params['chains']:
        _chain(chain)
    require(len(set(params['chains'])) == len(params['chains']), 'Duplicate target chain')
    document = {**deepcopy(DEFAULTS), **deepcopy(params)}
    document['hotspots'] = _residues(document.get('hotspots', []), 'hotspot')
    if 'crop' in document and document['crop'] is not None:
        document['crop'] = _residues(document['crop'], 'crop')
        require(document['crop'], 'An explicit crop cannot be empty')
    else:
        document.pop('crop', None)
    lengths = document['lengths']
    require(isinstance(lengths, list) and len(lengths) == 2, 'Specify minimum and maximum binder lengths')
    for value in lengths:
        number(value, 'binder length', 5, 1000)
    require(lengths[0] <= lengths[1], 'Minimum binder length exceeds maximum')
    number(document['designs'], 'accepted designs', 1, 10000)
    number(document['timeout_seconds'], 'timeout_seconds', 60, 85500)
    cost = document['max_cost_usd']
    require(type(cost) in (float, int) and math.isfinite(cost) and 0.01 <= cost <= 750,
            'Maximum run reservation must be between $0.01 and $750')
    if document['seed'] is not None:
        number(document['seed'], 'seed', 0, 2147483647)
    context = target_context(api, params['target'])
    with api.store.transaction() as db:
        old = api.store.idem(db, api.actor, 'binder.run', document['request_key'], document)
        if old:
            return api._batch(old, db)
        ident, pair_id = uid(), uid()
        batch = {'batch_id': ident, 'name': document['name'], 'mode': 'batch', 'workflow': 'bindcraft',
                 'state': 'validating', 'msa_backend': 'private', 'execution': 'ephemeral',
                 'inputs': [{'id': 'target', 'name': document['name'], 'molecule_type': 'structure'}],
                 'models': ['bindcraft'], 'pairs': [{'pair_id': pair_id, 'input_id': 'target',
                     'input_name': document['name'], 'model': 'bindcraft', 'state': 'pending',
                     'reasons': [], 'job_id': None}], 'errors': [], 'auto_run': True,
                 '_workflow': 'bindcraft', '_committed': False, '_request': document, '_target_context': context}
        api.store.put(db, 'batch', batch, api.actor)
        api.store.idem(db, api.actor, 'binder.run', document['request_key'], document, ident)
        api.store.event(db, ident, 'run_requested', {'workflow': 'bindcraft', 'target_sha256': params['target']['sha256']})
        return api._batch(ident, db)


def native_preflight(bundle, config):
    result = subprocess.run([sys.executable, '-B', str(Path(config['tools_dir']) / 'bindcraft/runtime.py'),
                             'preflight', '--bundle', str(bundle),
                             '--shared', config.get('bindcraft_shared', '/mnt/bio-shared')],
                            capture_output=True, text=True, timeout=120, check=False)
    require(result.returncode == 0, 'BindCraft installation preflight failed: ' +
            (result.stderr or result.stdout)[-3000:], 'unavailable')
    value = parse(result.stdout)
    require(value.get('status') == 'ready', 'BindCraft runtime is not ready', 'unavailable')
    return value


def validate_batch(store, batch, config):
    from .api import API
    from .binder_structure import normalize_structure, inspect_structure
    api = API(store, store.actor(batch['batch_id']), library_config=config, worker_config=config)
    document = batch['_request']
    source, item = target_source(api, document['target'])
    require(target_context(api, document['target']) == batch['_target_context'],
            'Pinned target library context changed', 'integrity')
    root = store.directory('batches', batch['batch_id']) / 'pairs' / batch['pairs'][0]['pair_id']
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    raw = source.read_bytes()
    normalized = normalize_structure(raw, filename=item['name'], chains=document['chains'],
                                     crop=document.get('crop'), hotspots=document['hotspots'])
    (root / 'original-structure').write_bytes(raw)
    (root / 'target.pdb').write_bytes(normalized['pdb'])
    submitted = inspect_structure(normalized['pdb'], filename='target.pdb')
    by_chain = {chain['chain']: chain for chain in submitted['chains']}
    submitted_chains = [by_chain[chain] for chain in normalized['chains'].split(',')]
    require(sum(chain['residue_count'] for chain in submitted_chains) +
            50 * len(submitted_chains) + document['lengths'][1] <= 9999,
            'Selected target fragments and binder would exceed native PDB residue numbering limits', 'limit')
    bundle = bundle_module(config['tools_dir'])
    settings = {'binder_name': 'binder_' + batch['batch_id'][:12], 'chains': normalized['chains'],
                'target_hotspot_residues': normalized['hotspots'], 'lengths': document['lengths'],
                'number_of_final_designs': document['designs']}
    execution = {'seed': document['seed']} if document['seed'] is not None else None
    manifest = bundle.create(root / 'target.pdb', settings, bundle.defaults('advanced'),
                             bundle.defaults('filters'), root / 'input.tar.gz', execution=execution)
    provenance = {'schema': 1, 'workflow': 'bindcraft', 'target': document['target'],
                  'target_context': batch['_target_context'], 'target_name': item['name'],
                  'target_format': normalized['inspection']['format'],
                  'submitted_target_sha256': file_sha(root / 'target.pdb'),
                  'residue_map': normalized['residue_map'], 'chains': document['chains'],
                  'submitted_chains': [{key: chain[key] for key in ('chain', 'sequence', 'residue_count')}
                                       for chain in submitted_chains],
                  'crop': document.get('crop'), 'hotspots': document['hotspots'],
                  'submitted_hotspots': normalized['hotspots'], 'settings': {
                      key: document[key] for key in DEFAULTS}, 'input_manifest': manifest,
                  'scientific_settings': 'Pinned upstream four-stage defaults and filters, unchanged'}
    write_json(root / 'binder-provenance.json', provenance)
    checked = native_preflight(root / 'input.tar.gz', config)
    write_json(root / 'native-preflight.json', checked)
    tools = Path(config['tools_dir'])
    sources = [Path(config['bio_submit']).resolve(), tools / 'recipes/bindcraft.sh', tools / 'dc-budget.py']
    sources += [p for directory in ('bindcraft', 'workbench') for p in (tools / directory).rglob('*')
                if p.is_file() and p.suffix in {'.py', '.json', '.sh'}
                and '__pycache__' not in p.parts and not p.name.startswith('test_')]
    return {'argv': [config['bio_submit'], 'bindcraft', '--in', str(root / 'input.tar.gz'),
                     '--timeout', str(document['timeout_seconds']), '--execution', 'ephemeral',
                     '--name', document['name']],
            'tools_dir': config['tools_dir'], 'environment': {'DC_MAX_JOB_COST_USD': str(document['max_cost_usd']),
                'DC_JOB_COST_SCOPE': 'binder-' + batch['batch_id']},
            'input_root': str(root), 'input_files': inventory(root),
            'source_pins': {str(p.resolve()): file_sha(p.resolve()) for p in sources},
            'timeout': document['timeout_seconds'], 'settings': provenance,
            'msa_backend': 'none', 'msa_applicable': False, 'native_validation': checked}
