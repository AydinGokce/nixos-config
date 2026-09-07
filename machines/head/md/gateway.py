"""MD inputs use the existing actor-scoped uploads, durable queue and artifacts."""
from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

from workbench.common import (file_sha, inventory, keys, number, require, safe_file,
                              string, uid, write_json)
from .bundle import pack, relative_name

PROTOCOLS = ['pmx_binding_ddg', 'plumed_metadynamics', 'bfee3_geometric']


def catalog():
    import json
    templates = {path.stem: json.loads(path.read_text())
                 for path in sorted((Path(__file__).parent / 'examples').glob('*.json'))}
    return {'schema': 'bio-md-catalog.v1', 'protocols': PROTOCOLS,
            'request_schema': 'bio-md-request.v1', 'validate_method': 'md.validate', 'plan_method': 'md.plan',
            'submit_method': 'batch.create', 'status_method': 'batch.get', 'resume_method': 'md.resume',
            'source': 'prepared structures and force-field topologies',
            'budget_ceiling_usd': 750, 'automatic_retry': False,
            'default_worker': 'auto',
            'worker_choices': ['auto', '1A100.22V', '1A100.40S.22V', '1L40S.20V', '1H100.80S.32V', 'CPU.16V.64G'],
            'request_templates': templates,
            'template_note': 'Replace paths, selections, chemical decisions and sampling settings for the actual system; templates are not ready-to-run scientific protocols.',
            'notes': ['Sampling uncertainty and physical-model disagreement are reported separately.',
                      'Modified nucleotides require explicit chemical identity and compatible validated parameters.',
                      'BFEE3 GROMACS supports the geometric route; no NAMD alchemical route is claimed.',
                      'Completion of a short simulation does not establish affinity or convergence.']}


def inspect_plan(api, params):
    keys(params, ('batch_id',))
    batch = api.store.read('batch', params['batch_id'], api.actor)
    require(batch.get('_workflow') == 'md', 'This is not an MD batch')
    pair = batch['pairs'][0]
    require(pair['state'] == 'compatible', 'MD preflight is not compatible yet', 'conflict')
    from workbench.common import read_json
    from workbench.runner import verify_prepared
    verify_prepared(pair['_prepared'])
    return {'batch_id': batch['batch_id'], 'plan': read_json(Path(pair['_prepared']['input_root']) / 'plan.json'),
            'preflight': pair['_prepared']['native_validation'], 'automatic_retry': False}


def compare_results(api, params):
    keys(params, ('artifact_ids',))
    require(isinstance(params['artifact_ids'], list) and 1 <= len(params['artifact_ids']) <= 32,
            'Select 1..32 retained cycle or binding-delta-delta-G reports')
    require(len(set(params['artifact_ids'])) == len(params['artifact_ids']), 'Duplicate comparison artifact')
    from workbench.common import read_json
    from .analysis import summarize_models
    reports, sources, unavailable = [], [], []
    total = 0
    for ident in params['artifact_ids']:
        artifact = api.store.read('artifact', ident, api.actor)
        require(artifact['model'] == 'md' and artifact['format'] == 'json', 'Select an MD JSON analysis artifact')
        total += artifact['size']
        require(artifact['size'] <= 16 * 1024**2 and total <= 64 * 1024**2, 'Comparison reports exceed size limit', 'limit')
        path = api.store.directory('artifacts', ident) / 'content'
        require(file_sha(path) == artifact['sha256'], 'MD report checksum differs', 'integrity')
        value = read_json(path)
        if value.get('schema') == 'bio-md-cycle-report.v1':
            require(isinstance(value.get('binding_ddg'), list), 'Invalid MD cycle report')
            reports.extend(value['binding_ddg'])
            unavailable.extend({'artifact_id': ident, 'diagnostic': item}
                               for item in value.get('unavailable_binding_ddg', []))
        else:
            reports.append(value)
        sources.append({'artifact_id': ident, 'sha256': artifact['sha256']})
    comparison = summarize_models(reports) if reports else {
        'schema': 'bio-md-model-comparison.v1', 'status': 'insufficient_sampling',
        'quantity': 'binding_ddg', 'models': [], 'pooled_estimate': None,
        'between_model_disagreement': None,
        'reason': 'No supported binding estimates are available; absence of an estimate is not evidence of agreement.'}
    return {'comparison': comparison, 'unavailable_binding_ddg': unavailable,
            'sources': sources, 'paid_compute_requested': False}


def resume_request(api, params):
    keys(params, ('job_id', 'request_key'), ('timeout', 'worker'))
    source = api.store.read('job', params['job_id'], api.actor)
    require(source['model'] == 'md' and source['state'] in {'failed', 'interrupted', 'cancelled'},
            'Resume requires a stopped MD job; running or completed jobs cannot be duplicated implicitly')
    batch = api.store.read('batch', source['batch_id'], api.actor)
    document = {k: v for k, v in batch['_request'].items() if not k.startswith('_')}
    document.update(request_key=params['request_key'], name=batch['name'] + ' (resume)')
    for key in ('timeout', 'worker'):
        if key in params:
            document[key] = params[key]
    return request(api, document, resume_job=source['job_id'])


def request(api, params, *, resume_job=None):
    keys(params, ('request_key', 'name', 'request', 'assets'), ('timeout', 'worker'))
    string(params['request_key'], 'request_key', 128)
    string(params['name'], 'name', 200)
    require(isinstance(params['request'], dict) and params['request'].get('protocol') in PROTOCOLS,
            'Specify a supported MD protocol and its complete request document')
    timeout = number(params.get('timeout', 7200), 'timeout', 60, 85500)
    worker = params.get('worker', 'auto')
    require(worker in {'auto', '1A100.22V', '1A100.40S.22V', '1L40S.20V', '1H100.80S.32V', 'CPU.16V.64G'},
            'Unsupported MD worker')
    require(isinstance(params['assets'], dict) and 0 < len(params['assets']) <= 256,
            'Provide 1..256 named uploaded assets')
    total = 0
    for name, ident in params['assets'].items():
        relative_name(name)
        upload = api.store.read('upload', ident, api.actor)
        require(upload['state'] == 'complete', 'An MD asset upload is incomplete', 'conflict')
        total += upload['size']
    require(total <= 1024**3, 'MD input assets exceed 1 GiB', 'limit')
    document = {**params, 'timeout': timeout, 'worker': worker}
    if resume_job:
        document['_resume_job_id'] = resume_job
    with api.store.transaction() as db:
        old = api.store.idem(db, api.actor, 'md.validate', params['request_key'], document)
        if old:
            return api._batch(old, db)
        ident, pair_id = uid(), uid()
        data = {'batch_id': ident, 'name': params['name'], 'mode': 'batch', 'workflow': 'md',
                'state': 'validating', 'msa_backend': 'public', 'execution': 'ephemeral',
                'inputs': [{'id': 'complex', 'name': params['name'], 'molecule_type': 'structure'}],
                'models': ['md'], 'pairs': [{'pair_id': pair_id, 'input_id': 'complex',
                    'input_name': params['name'], 'model': 'md', 'state': 'pending',
                    'reasons': [], 'job_id': None}], 'errors': [], '_request': document,
                '_workflow': 'md', '_committed': False}
        api.store.put(db, 'batch', data, api.actor)
        api.store.idem(db, api.actor, 'md.validate', params['request_key'], document, ident)
        api.store.event(db, ident, 'md_validation_requested', {'protocol': params['request']['protocol']})
        return api._batch(ident, db)


def validate_batch(store, batch, config):
    """Prepare and natively preflight on the head; no dynamics or paid rental."""
    from .protocols import validate_request, prepare
    pair = batch['pairs'][0]
    root = store.directory('batches', batch['batch_id']) / 'pairs' / pair['pair_id']
    assets = root / 'assets'
    assets.mkdir(parents=True, mode=0o700, exist_ok=False)
    document = batch['_request']
    actor = store.actor(batch['batch_id'])
    paths = {}
    for name, ident in document['assets'].items():
        name = relative_name(name)
        upload = store.read('upload', ident, actor)
        require(upload['state'] == 'complete', 'Validated upload is no longer complete', 'integrity')
        source = safe_file(store.directory('uploads', ident) / 'content')
        require(file_sha(source) == upload['sha256'], 'MD upload checksum differs', 'integrity')
        target = assets / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        paths[name] = target
    normalized = validate_request(document['request'], assets)
    # Generate the entire command plan on the head for inspection before any
    # paid launch; native preflight below and the worker both validate topology.
    plan = prepare(normalized, assets, root / 'preview')
    write_json(root / 'plan.json', plan)
    runtime = Path(config.get('md_runtime', '/var/lib/bio-md/runtime-cpu'))
    require((runtime / 'manifest.json').is_file() and (runtime / 'activate.sh').is_file(),
            'The pinned CPU MD runtime is not installed; no worker was launched', 'unavailable')
    resume = {}
    if document.get('_resume_job_id'):
        from workbench.common import parse
        previous = store.read('job', document['_resume_job_id'], actor)
        require(previous['model'] == 'md' and previous['state'] in {'failed', 'interrupted', 'cancelled'},
                'Resume source is no longer a stopped MD job', 'conflict')
        with store.connection() as db:
            artifacts = [parse(row['data']) for row in db.execute(
                "SELECT data FROM objects WHERE kind='artifact' AND actor=? AND json_extract(data,'$.job_id')=?",
                (actor, previous['job_id']))]
        plans = [a for a in artifacts if a['name'].endswith('/simulation/execution-plan.json')]
        require(len(plans) == 1, 'Resume source lacks one unambiguous executed MD plan', 'conflict')
        from workbench.common import read_json
        original_plan = store.directory('artifacts', plans[0]['artifact_id']) / 'content'
        require(file_sha(original_plan) == plans[0]['sha256'], 'Resume plan checksum differs', 'integrity')
        require(read_json(original_plan) == plan, 'Generated protocol changed; checkpoints cannot be resumed under another plan', 'integrity')
        prefix = plans[0]['name'].removesuffix('simulation/execution-plan.json')
        runtime_records = [a for a in artifacts if a['name'] == prefix + 'runtime.json']
        require(len(runtime_records) == 1, 'Resume source lacks its pinned runtime identity', 'conflict')
        original_runtime = store.directory('artifacts', runtime_records[0]['artifact_id']) / 'content'
        require(file_sha(original_runtime) == runtime_records[0]['sha256'], 'Resume runtime checksum differs', 'integrity')
        for artifact in artifacts:
            if artifact['name'].startswith(prefix + 'simulation/'):
                name = artifact['name'][len(prefix + 'simulation/'):]
                relative_name(name)
                if name == '_runtime.json':
                    # A prior resume's ancestor identity is replaced below by
                    # the authoritative runtime of this stopped execution.
                    continue
                source = store.directory('artifacts', artifact['artifact_id']) / 'content'
                require(file_sha(source) == artifact['sha256'], 'Resume artifact checksum differs', 'integrity')
                target = root / 'resume' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                resume[name] = target
        runtime_target = root / 'resume' / '_runtime.json'
        runtime_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original_runtime, runtime_target)
        resume['_runtime.json'] = runtime_target
        require(any(name.endswith('.cpt') for name in resume), 'Resume source contains no native checkpoint', 'conflict')
    pack(normalized, paths, root / 'input.tar.gz', resume=resume)
    from .admission import check as native_admission
    admission = native_admission(root / 'input.tar.gz', config['tools_dir'], runtime,
                                 config.get('md_admissions', '/var/lib/bio-md/admissions'))
    write_json(root / 'native-preflight.json', admission)
    from .launch import select as select_runtime
    _, runtime_deployment = select_runtime(config.get('md_runtime_archives', '/mnt/bio-shared/md-runtime'),
                                           'cpu' if document['worker'].startswith('CPU.') else 'cuda')
    write_json(root / 'runtime-deployment.json', runtime_deployment)
    tools = Path(config['tools_dir'])
    source_paths = [Path(config['bio_submit']), tools / 'recipes/md.sh', tools / 'py/worker_runtime.py']
    source_paths += [p for p in (tools / 'md').rglob('*') if p.is_file() and
                     '__pycache__' not in p.parts and not p.name.startswith('test_')]
    prepared = {'argv': [config['bio_submit'], 'md', '--in', str(root / 'input.tar.gz'),
                        '--timeout', str(document['timeout']), '--execution', 'ephemeral',
                        *(['--gpu', document['worker']] if document['worker'] != 'auto' else [])],
                'tools_dir': config['tools_dir'], 'environment': {'BIO_MD_CPU_RUNTIME': str(runtime),
                    'BIO_MD_ADMISSIONS': config.get('md_admissions', '/var/lib/bio-md/admissions'),
                    'BIO_MD_RUNTIME_ARCHIVES': config.get('md_runtime_archives', '/mnt/bio-shared/md-runtime')}, 'input_root': str(root),
                'input_files': inventory(root), 'source_pins': {str(p.resolve()): file_sha(p.resolve()) for p in source_paths},
                'timeout': document['timeout'], 'settings': {'protocol': normalized['protocol'],
                    'conditions': normalized['conditions'], 'worker': document['worker'], 'timeout': document['timeout']},
                'native_validation': {'kind': 'native_gromacs_topology_preflight', 'claims': plan.get('claims', []),
                                      'runtime_manifest_sha256': file_sha(runtime / 'manifest.json'),
                                      'engine_preflight': 'grompp and parameter checks passed on CPU head; no dynamics or paid worker'}}
    return prepared
