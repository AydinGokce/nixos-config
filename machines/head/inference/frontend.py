"""Prepare ordinary folding inputs on CPUs and enqueue compatible warm jobs.

An operator profile declares an audited native default configuration. No model
parameters are inferred from benchmark output or silently overridden here.
Exit 78 means no compatible active profile; bio-submit auto may use its existing
ephemeral route. Once preparation/submission starts, failure never falls back.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).absolute().parent.parent))
from inference.cache import ArtifactCache, _no_symlinks, _rename_new, identity_key
from inference.cli import wait
from inference.common import atomic_json, configuration_id, digest, inventory, now, read, sha256, verify_inventory
from inference.job_queue import Queue
from inference.pool import remote


class Unavailable(Exception):
    pass


def profile(state, model, native_seed=None):
    path = state / 'profiles' / (model + '.json')
    if not path.is_file():
        raise Unavailable('No resident default profile for ' + model)
    value = read(path)
    if value.get('model') != model:
        raise ValueError('Resident profile declares another model')
    if model == 'boltz2':
        if (value.get('interface') != 'bio-submit-explicit-seed-v1' or native_seed is None):
            raise Unavailable('The audited Boltz resident profile requires an explicit native seed')
        if type(native_seed) is not int or native_seed != 42 or value.get('native_seed') != native_seed:
            raise Unavailable('No audited Boltz resident profile for the requested native seed')
        if value.get('seeds') != [native_seed] or type(value.get('native_seed')) is not int:
            raise ValueError('Boltz profile seed declaration is inconsistent')
    elif value.get('interface') != 'bio-submit-native-defaults-v1' or native_seed is not None:
        raise ValueError('Resident profile must explicitly declare the native default interface')
    verify_inventory(value['validation_root'], value['validation_files'])
    config = read(state / 'configs' / (value['config_id'] + '.json'))
    if configuration_id(config) != value['config_id'] or config['model'] != model:
        raise ValueError('Resident default configuration changed')
    if model == 'boltz2' and (type(config['native_config'].get('seed')) is not int
                             or config['native_config']['seed'] != native_seed):
        raise ValueError('Boltz profile seed differs from its loaded native configuration')
    for worker_path in sorted((state / 'workers').glob('*.json')):
        worker = read(worker_path)
        if worker['config_id'] != value['config_id']:
            continue
        toolkit = Path(__file__).absolute().parent.parent
        parser_folder = 'rf3' if model == 'rf3' else 'msa'
        for relative in ('inference/adapters/' + model + '.py', parser_folder + '/prepare.py' if model == 'rf3' else 'msa/prepared.py'):
            expected = worker.get('source_files', {}).get(worker.get('tools_root', '') + '/' + relative)
            if expected != sha256(toolkit / relative):
                raise Unavailable('Resident worker uses another adapter/preparation generation')
        status_path = Path(worker['spool_root']) / worker['worker_id'] / 'status.json'
        if not status_path.is_file():
            continue
        status = read(status_path)
        if (status.get('state') in ('ready', 'running') and status.get('config_id') == value['config_id']
                and -5 <= now() - status['heartbeat_epoch'] <= 30
                and min(worker['deadline_epoch'], status['deadline_epoch']) > now() + 180):
            return value, config, worker
    raise Unavailable('No active resident worker with the audited default configuration')


def prepared_module():
    sys.path.insert(0, str(Path(__file__).absolute().parent.parent / 'msa'))
    import prepared
    return prepared


def preparation_identity(model, input_path, parser, backend, source, *, chemistry_sha=None, runtime_id=None):
    return {'schema': 1, 'artifact_kind': 'prepared',
        'input': {'sha256': sha256(input_path), 'chemistry_sha256': chemistry_sha or sha256(input_path)},
        'model': {'name': model, 'adapter_sha256': sha256(Path(__file__).parent / 'adapters' / (model + '.py')),
                  'parser_sha256': sha256(parser)},
        'search': {'backend': backend, 'provenance_sha256': digest(source),
            'database': source['database'], 'settings_sha256': digest({'parser': sha256(parser), 'runtime': runtime_id}),
            'template_mode': 'native-default', 'pairing_mode': 'native-default'}}


def prepare_public(state, worker, model, fasta, destination, timeout, endpoint):
    parser = worker['tools_root'] + '/msa/prepared.py'
    expected = worker['source_files'][parser]
    if expected != sha256(Path(__file__).absolute().parent.parent / 'msa/prepared.py'):
        raise ValueError('CPU preparation parser differs from the cached head parser identity')
    launch = read(state / 'launches' / worker['worker_id'] / 'intent.json')
    target = launch['target']
    actual = remote(target, ['/usr/bin/cat', '/proc/sys/kernel/random/boot_id']).decode().strip()
    if actual != target['boot_id']:
        raise ValueError('CPU preparation allocation boot identity changed')
    remaining = min(timeout, int(worker['deadline_epoch'] - now()) - 60)
    if remaining < 120:
        raise Unavailable('Insufficient remaining allocation time for preparation')
    unit = 'bio-prepare-' + uuid.uuid4().hex
    properties = ['Type=exec', 'CPUQuota=800%', 'TasksMax=4096', 'MemoryMax=32G',
        'RuntimeMaxSec=' + str(remaining), 'TimeoutStopSec=20', 'KillMode=control-group',
        'Environment=CUDA_VISIBLE_DEVICES=', 'Environment=OMP_NUM_THREADS=1',
        'Environment=OPENBLAS_NUM_THREADS=1', 'Environment=MKL_NUM_THREADS=1',
        'Environment=NUMEXPR_NUM_THREADS=1', 'Environment=PYTHONDONTWRITEBYTECODE=1',
        'Environment=BOLTZ_CACHE=/mnt/bio-shared/cache/boltz',
        'Environment=OPENFOLD_CACHE=/mnt/bio-shared/openfold3/home/.openfold3',
        'Environment=PROTENIX_ROOT_DIR=/mnt/bio-shared/protenix/release_data']
    for key in ('PATH', 'LD_LIBRARY_PATH'):
        properties.append('Environment=' + key + '=' + worker['environment'][key])
    command = ['systemd-run', '--quiet', '--wait', '--collect', '--unit', unit]
    for prop in properties:
        command += ['--property', prop]
    checked_exec = 'import hashlib,os,pathlib,sys; p=pathlib.Path(sys.argv[1]); assert hashlib.sha256(p.read_bytes()).hexdigest()==sys.argv[2]; os.execv(sys.executable,[sys.executable,str(p),*sys.argv[3:]])'
    command += [worker['python'], '-c', checked_exec, parser, expected, 'prepare', '--model', model,
        '--fasta', str(fasta), '--out', str(destination), '--source', 'public', '--server-url', endpoint]
    atomic_json(destination.parent / 'cpu-preparation.json', {'unit': unit, 'command': command,
        'worker_id': worker['worker_id'], 'boot_id': actual, 'deadline_epoch': now() + remaining}, exclusive=True)
    try:
        output = remote(target, command, timeout=remaining + 45)
        (destination.parent / 'cpu-preparation.log').write_bytes(output)
    finally:
        journal = remote(target, ['journalctl', '-u', unit, '--no-pager', '-o', 'short-iso'], timeout=30)
        (destination.parent / 'cpu-preparation-journal.log').write_bytes(journal)
    return destination


def materialize_job(model, bundle, native, config, job_id, seeds, fasta=None):
    prepared = prepared_module()
    entry = prepared.materialize(bundle, model, native, fasta)
    job = {'id': job_id, 'model': model, 'config_id': configuration_id(config),
           'native_input': str(entry), 'seeds': seeds, 'native_config': deepcopy(config['native_config'])}
    if model == 'openfold3':
        runtime = read(native / 'runtime.json')['template_preprocessor_settings']
        settings = job['native_config']['template_preprocessor_settings']
        for key in ('output_directory', 'structure_directory', 'cache_directory', 'log_directory'):
            if key in runtime:
                settings[key] = runtime[key]
    return job


def rf3_modules():
    """Load the pinned preparation helpers without importing a model package."""
    root = Path(__file__).absolute().parent.parent / 'rf3'
    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    prepare = load('resident_frontend_rf3_prepare', root / 'prepare.py')
    previous = sys.modules.get('prepare')
    try:
        sys.modules['prepare'] = prepare
        search = load('resident_frontend_rf3_msa', root / 'msa.py')
    finally:
        if previous is None:
            sys.modules.pop('prepare', None)
        else:
            sys.modules['prepare'] = previous
    return prepare, search


def rf3_search_database(root):
    """Read the certified full generation, without re-scanning terabytes on a hit.

    A miss still uses bio-msa's full server admission. This receipt identifies
    the existing certified generation; it does not certify a fresh search.
    """
    source = Path(__file__).absolute().parent.parent / 'msa/databases.py'
    spec = importlib.util.spec_from_file_location('rf3_cache_database_pins', source)
    pins = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pins)
    root = _no_symlinks(root)
    prepare, _ = rf3_modules()
    manifest = prepare.read_json(_no_symlinks(root / 'manifest.json'))
    value = prepare.read_json(_no_symlinks(root / '.msa-databases.json'))
    components = value.get('components', {})
    if (manifest != pins.MANIFEST or value.get('version') != pins.VERSION
            or value.get('manifest_sha256') != pins.MANIFEST_SHA256
            or set(components) != set(pins.COMPONENTS)
            or any(not isinstance(v, str) or len(v) != 64 or any(c not in '0123456789abcdef' for c in v)
                   for v in components.values())):
        raise ValueError('RF3 private preparation requires the pinned complete database receipt')
    tools = {key: value.get('tools', {}).get(key) for key in ('mmseqs_sha256', 'server_sha256')}
    if tools != {'mmseqs_sha256': pins.MMSEQS_SHA256, 'server_sha256': pins.BACKEND_SHA256}:
        raise ValueError('RF3 private database tool provenance differs from the pinned search backend')
    return {'database': {key: deepcopy(value[key]) for key in ('version', 'manifest_sha256', 'components')},
            'tools': tools}


def rf3_molecular_identity(document, source):
    """Bind chemistry verbatim; only asset locations become their content hashes."""
    value = deepcopy(document)
    for component in value['components']:
        component.pop('msa_path', None)
        if component.get('path'):
            asset = Path(component['path'])
            asset = _no_symlinks(asset if asset.is_absolute() else Path(source).parent / asset)
            if asset.suffix.lower() not in ('.sdf', '.cif') or not asset.is_file():
                raise ValueError('RF3 component asset must be a regular SDF or CIF file')
            component['path'] = {'format': asset.suffix.lower(), 'sha256': sha256(asset)}
    return value


def rf3_search_identity(args):
    prepare, msa = rf3_modules()
    if bool(args.fasta) == bool(args.native_json):
        raise ValueError('RF3 cached preparation requires exactly one FASTA or native JSON input')
    original = _no_symlinks(args.fasta or args.native_json)
    document, headers = (prepare.fasta_document(original, args.rf3_name) if args.fasta
                         else (prepare.document(prepare.read_json(original)), {}))
    if any(c.get('msa_path') for c in document['components']):
        raise ValueError('Explicit RF3 alignments require --msa-bundle; preparation will not replace them')
    queries = prepare.protein_queries(document)
    ordered = [{'chain_id': key, 'sequence': value} for key, value in queries.items()]
    chemistry = rf3_molecular_identity(document, original)
    endpoint = None
    known = None
    if queries and args.backend == 'public':
        # Constructor validates the endpoint and performs no network request.
        endpoint = msa.Client(args.endpoint, 'public', now() + args.timeout).endpoint
    if queries and args.backend == 'private':
        known = rf3_search_database(args.rf3_database_root)
    database = ({'status': 'verified', 'sha256': digest(known)} if known else
                {'status': 'provider-unreported', 'provider': endpoint or 'not-used:no-protein-query'})
    toolkit = Path(__file__).absolute().parent.parent
    sources = {name: sha256(toolkit / name) for name in
        ('rf3/msa.py', 'rf3/prepare.py', 'rf3/runtime.py', 'rf3/requirements.lock',
         'library/rf3_adapter.py', 'library/rf3_compat.py', 'inference/frontend.py')}
    if known:
        sources.update({name: sha256(toolkit / name) for name in
                        ('msa/databases.py', 'msa/server.py', 'msa/settings.py')})
    settings = {'mode': 'env' if queries else None,
                'pairing_mode': 'pairgreedy' if len(set(queries.values())) > 1 else None,
                'template_mode': 'disabled', 'sources': sources}
    origin = {'backend': args.backend if queries else 'none', 'endpoint': endpoint,
              'known_database': known, 'public_database_version': 'provider-unreported' if endpoint else None}
    identity = {'schema': 1, 'artifact_kind': 'prepared', 'protocol': 'rf3-before-search-cache-v1',
        'input': {'sha256': sha256(original), 'chemistry_sha256': digest(chemistry),
            'ordered_queries_sha256': digest(ordered), 'original_headers': headers,
            'source_format': 'fasta' if args.fasta else 'rf3-json',
            'library_bundle_sha256': args.chemistry_sha},
        'model': {'name': 'rf3', 'adapter_sha256': sources['library/rf3_adapter.py'],
                  'parser_sha256': sources['rf3/prepare.py']},
        'search': {'backend': origin['backend'], 'provenance_sha256': digest(origin), 'database': database,
            'known_origin': origin, 'settings_sha256': digest(settings), 'settings': settings,
            'template_mode': 'disabled', 'pairing_mode': settings['pairing_mode'] or 'unpaired-only'},
        'molecular_input': chemistry, 'ordered_queries': ordered}
    return original, document, queries, identity


def verify_rf3_search_preparation(bundle, original, queries, identity):
    prepare, msa = rf3_modules()
    entry = Path(bundle) / 'input.json'
    manifest = prepare.validate(entry)
    document = prepare.document(prepare.read_json(entry))
    if (manifest['source_sha256'] != identity['input']['sha256'] or sha256(original) != manifest['source_sha256']
            or manifest['source_format'] != identity['input']['source_format']
            or manifest['original_fasta_headers'] != identity['input']['original_headers']
            or rf3_molecular_identity(document, entry) != identity['molecular_input']):
        raise ValueError('RF3 cached preparation differs from the original molecular input/assets')
    search = msa.validate_search(entry.parent / 'msa-search', queries)
    if (list(search['queries'].items()) != list(queries.items())
            or list(prepare.protein_queries(document).items()) != list(queries.items())
            or search['source'] != identity['search']['backend']
            or search['mode'] != identity['search']['settings']['mode']
            or search['pairing_mode'] != identity['search']['settings']['pairing_mode']
            or manifest.get('search_sha256') != search['sha256']):
        raise ValueError('RF3 cached search changed its ordered query/backend/settings binding')
    for chain, row in manifest['chain_msas'].items():
        if row['sha256'] != search['chain_msas'][chain]['sha256']:
            raise ValueError('RF3 prepared alignment differs from its retained search')
    origin = identity['search']['known_origin']
    if origin['endpoint'] and search['endpoint'].rstrip('/') != origin['endpoint']:
        raise ValueError('RF3 cached search endpoint differs')
    if origin['known_database']:
        value = prepare.read_json(entry.parent / 'msa-search/database-provenance.json')
        known = origin['known_database']
        if (any(value.get('database', {}).get(k) != v for k, v in known['database'].items())
                or any(value.get('tools', {}).get(k) != v for k, v in known['tools'].items())):
            raise ValueError('RF3 search used a different database/tool generation')
    return manifest, search


@contextmanager
def rf3_search_lock(cache, identity, deadline):
    """Serialize only identical preparations, without holding a cache/accounting lock."""
    folder = _no_symlinks(cache.root / 'search-locks')
    folder.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(folder / (identity_key(identity) + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('RF3 preparation lock is not a regular file')
        while True:
            if now() >= deadline:
                raise TimeoutError('RF3 preparation deadline exceeded while waiting for an identical request')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(.25, deadline - now()))
        yield
    finally:
        os.close(fd)


def rf3_prepare_cached(args):
    """Replay a verified prepared search or capture one before any inference rental."""
    if args.model != 'rf3' or args.bundle or args.probe or not args.rf3_out:
        raise ValueError('RF3 preparation-only mode requires --rf3-out and no bundle/probe')
    started, deadline = now(), now() + args.timeout
    original, document, queries, identity = rf3_search_identity(args)
    output = _no_symlinks(args.rf3_out)
    receipt_path = output.with_name(output.name + '.preparation-cache.json')
    if output.exists() or receipt_path.exists() or receipt_path.is_symlink():
        raise FileExistsError('RF3 preparation destination or request receipt already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='.rf3-search-request-', dir=output.parent))
    cache = ArtifactCache(args.shared / 'inference/rf3-preparation-cache')
    if args.refresh_preparation:
        identity['refresh_generation'] = uuid.uuid4().hex
    atomic_json(work / 'request.json', {'identity': identity, 'started_epoch': started,
        'deadline_epoch': deadline, 'original': str(original), 'library_reference': args.library_reference}, exclusive=True)
    try:
        with rf3_search_lock(cache, identity, deadline):
            cached = cache.lookup(identity)
            reused = cached is not None
            if cached is None:
                def command(argv, label):
                    remaining = deadline - now()
                    if remaining <= 0:
                        raise TimeoutError('RF3 preparation deadline exceeded')
                    atomic_json(work / (label + '-command.json'), {'argv': argv}, exclusive=True)
                    with (work / (label + '.log')).open('wb') as log:
                        subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=remaining, check=True)
                tools = Path(__file__).absolute().parent.parent
                input_args = ['--fasta' if args.fasta else '--native-json', str(original)]
                search_args = ['--server-url', args.endpoint, '--source', 'public', '--deadline', str(deadline)]
                if queries and args.backend == 'private':
                    # Preserve component/query order; canonical JSON sort_keys would change pairing order.
                    (work / 'queries.json').write_text(json.dumps(queries) + '\n')
                    command(['bio-msa', 'prepare', '--model', 'rf3', '--json', str(work / 'queries.json'),
                        '--timeout', str(max(1, math.ceil(deadline - now()))), '--bundle-result', str(work / 'search-result.json')], 'private-search')
                    search_args = ['--search-bundle', rf3_modules()[0].read_json(work / 'search-result.json')['bundle']]
                command([sys.executable, str(tools / 'rf3/msa.py'), 'prepare', *input_args, *search_args,
                         '--out', str(work / 'prepared'), '--name', args.rf3_name], 'prepare')
                verify_rf3_search_preparation(work / 'prepared', original, queries, identity)
                refreshed_identity = rf3_search_identity(args)[-1]
                if refreshed_identity != {k: v for k, v in identity.items() if k != 'refresh_generation'}:
                    raise ValueError('RF3 original input, assets, parser or database changed during preparation')
                cached = cache.publish(work / 'prepared', identity)
            cache.materialize(cached, work / 'materialized')
            manifest, search = verify_rf3_search_preparation(work / 'materialized', original, queries, identity)
            if rf3_search_identity(args)[-1] != {k: v for k, v in identity.items() if k != 'refresh_generation'}:
                raise ValueError('RF3 preparation request identity changed before publication')
            _rename_new(work / 'materialized', output)
        result = {'schema': 1, 'kind': 'rf3-preparation-cache-request', 'bundle': str(output),
            'cache_receipt': cached, 'reused_preparation': reused, 'refreshed': args.refresh_preparation,
            'input_sha256': identity['input']['sha256'], 'library_reference': args.library_reference,
            'prepared_sha256': manifest['sha256'], 'search_sha256': search['sha256'],
            'original_search_completed_at_epoch': search.get('completed_at_epoch'),
            'evidence_semantics': 'replayed original captured search' if reused else 'new captured search',
            'request_directory': str(work), 'started_epoch': started, 'completed_epoch': now()}
        atomic_json(receipt_path, result, exclusive=True)
        return result
    except Exception as exc:
        atomic_json(work / 'failure.json', {'error': str(exc), 'type': type(exc).__name__, 'failed_epoch': now()}, exclusive=True)
        raise


def rf3_preparation(bundle, original, backend, config, runtime_id, chemistry_sha=None):
    """RF3 comes fully prepared; bind exact chemistry and the captured search."""
    prepare, msa = rf3_modules()
    entry = Path(bundle) / 'input.json' if Path(bundle).is_dir() else Path(bundle)
    if entry.name != 'input.json':
        raise ValueError('RF3 resident input must be a complete prepared input.json')
    manifest = prepare.validate(entry)
    from inference.adapters.rf3 import EXPECTED_FILE, EXPECTED_RECEIPT
    if any(name in manifest['files'] for name in (EXPECTED_FILE, EXPECTED_RECEIPT)):
        raise ValueError('RF3 caller preparation must not supply head-owned expected chemistry cache artifacts')
    if manifest['source_sha256'] != sha256(original):
        raise ValueError('RF3 prepared input belongs to a different original molecular input')
    document = prepare.document(prepare.read_json(entry))
    queries = prepare.protein_queries(document)
    search = msa.validate_search(entry.parent / 'msa-search', queries)
    if manifest.get('search_sha256') != search['sha256']:
        raise ValueError('RF3 preparation does not bind its captured MSA search')
    if search['source'] != (backend if queries else 'none'):
        raise ValueError('RF3 captured search backend differs from the requested backend')
    for chain, row in manifest['chain_msas'].items():
        if row['sha256'] != search['chain_msas'][chain]['sha256']:
            raise ValueError('RF3 prepared A3M differs from the captured search result')
    database = {'status': 'provider-unreported', 'provider': search['endpoint'] or 'not-used:no-protein-query'}
    if queries and backend == 'private':
        provenance_path = entry.parent / 'msa-search/database-provenance.json'
        provenance = read(provenance_path)
        expected_components = {'uniref30', 'environmental', 'pdb100', 'templates', 'mmcif'}
        components = provenance.get('database', {}).get('components', {})
        if (set(components) != expected_components or any(not isinstance(value, str) or len(value) != 64
                or any(c not in '0123456789abcdef' for c in value) for value in components.values())):
            raise ValueError('RF3 private search lacks its full database component provenance')
        database = {'status': 'verified', 'sha256': sha256(provenance_path)}
    molecular = deepcopy(document)
    for component in molecular['components']:
        component.pop('msa_path', None)
    chemistry = {'document': molecular, 'assets': {component['path']: manifest['files'][component['path']]
        for component in document['components'] if 'path' in component}, 'library_bundle_sha256': chemistry_sha}
    parser = Path(__file__).absolute().parent.parent / 'rf3/prepare.py'
    identity = {'schema': 1, 'artifact_kind': 'prepared',
        'input': {'sha256': sha256(original), 'chemistry_sha256': digest(chemistry),
                  'native_input_sha256': sha256(entry), 'prepared_sha256': manifest['sha256']},
        'model': {'name': 'rf3', 'adapter_sha256': sha256(Path(__file__).parent / 'adapters/rf3.py'),
                  'parser_sha256': sha256(parser), 'runtime_image_sha256': runtime_id},
        'search': {'backend': search['source'], 'provenance_sha256': search['sha256'], 'database': database,
            'settings_sha256': digest({'mode': search['mode'], 'pairing_mode': search['pairing_mode'],
                'pairing_encoding': search['pairing_encoding'], 'helper_sha256': sha256(parser.parent / 'msa.py')}),
            'template_mode': 'disabled', 'pairing_mode': search['pairing_mode'] or 'native-preserved-taxid'}}
    identity['expected_chemistry'] = {'protocol': 'rf3-coordinate-free-native-expected-v1',
        'sources': {name: sha256(parser.parent.parent / name) for name in
            ('rf3/prepare.py', 'rf3/runtime.py', 'rf3/requirements.lock', 'library/rf3_output.py', 'library/rf3_compat.py')}}
    if config['native_config'].get('template_selection') is not None:
        raise ValueError('RF3 default resident preparation does not introduce template conditioning')
    return entry, manifest, document, search, identity


def prepare_rf3_expected(state, worker, bundle, timeout):
    """Derive a bound chemical graph on CPUs before occupying the GPU queue."""
    from inference.adapters.rf3 import EXPECTED_FILE, EXPECTED_RECEIPT, prepared_expected
    toolkit = Path(__file__).absolute().parent.parent
    names = ('inference/adapters/rf3.py', 'inference/adapters/_common.py',
             'rf3/prepare.py', 'rf3/runtime.py', 'rf3/requirements.lock',
             'library/rf3_output.py', 'library/rf3_compat.py')
    sources = {worker['tools_root'] + '/' + name: sha256(toolkit / name) for name in names}
    if any(worker['source_files'].get(path) != value for path, value in sources.items()):
        raise ValueError('RF3 CPU preparation toolchain differs from the active worker')
    target = read(state / 'launches' / worker['worker_id'] / 'intent.json')['target']
    actual = remote(target, ['/usr/bin/cat', '/proc/sys/kernel/random/boot_id']).decode().strip()
    if actual != target['boot_id']:
        raise ValueError('RF3 CPU preparation allocation boot identity changed')
    remaining = min(300, timeout, int(worker['deadline_epoch'] - now()) - 60)
    if remaining < 120:
        raise Unavailable('Insufficient remaining allocation time for RF3 CPU preparation')
    unit = 'bio-rf3-expected-' + uuid.uuid4().hex
    properties = ['Type=exec', 'CPUQuota=800%', 'TasksMax=4096', 'MemoryMax=32G',
        'PrivateNetwork=yes', 'RuntimeMaxSec=' + str(remaining), 'TimeoutStopSec=20', 'KillMode=control-group',
        'Environment=CUDA_VISIBLE_DEVICES=', 'Environment=OMP_NUM_THREADS=1',
        'Environment=OPENBLAS_NUM_THREADS=1', 'Environment=MKL_NUM_THREADS=1',
        'Environment=NUMEXPR_NUM_THREADS=1', 'Environment=PYTHONDONTWRITEBYTECODE=1']
    for key in ('PATH', 'LD_LIBRARY_PATH'):
        properties.append('Environment=' + key + '=' + worker['environment'][key])
    command = ['systemd-run', '--quiet', '--wait', '--collect', '--unit', unit]
    for prop in properties:
        command += ['--property', prop]
    parser = worker['tools_root'] + '/inference/adapters/rf3.py'
    checked_exec = ('import hashlib,json,os,pathlib,sys; files=json.loads(sys.argv[1]); '
        'assert all(hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()==h for p,h in files.items()); '
        'os.execv(sys.executable,[sys.executable,"-B",*sys.argv[2:]])')
    expected = bundle.parent / 'expected-chemistry.json'
    receipt = bundle.parent / 'expected-chemistry-receipt.json'
    command += [worker['python'], '-B', '-c', checked_exec, json.dumps(sources), parser,
        '--expected-input', str(bundle / 'input.json'), '--expected-output', str(expected),
        '--expected-receipt', str(receipt), '--tools-dir', worker['tools_root']]
    intent = {'unit': unit, 'command': command, 'worker_id': worker['worker_id'], 'boot_id': actual,
              'deadline_epoch': now() + remaining, 'sources': sources, 'gpu_used': False}
    atomic_json(bundle.parent / 'cpu-expected-preparation.json', intent, exclusive=True)
    try:
        output = remote(target, command, timeout=remaining + 45)
        (bundle.parent / 'cpu-expected-preparation.log').write_bytes(output)
    finally:
        journal = remote(target, ['journalctl', '-u', unit, '--no-pager', '-o', 'short-iso'], timeout=30)
        (bundle.parent / 'cpu-expected-preparation-journal.log').write_bytes(journal)
    prepare, msa = rf3_modules()
    manifest = prepare.validate(bundle / 'input.json')
    shutil.copyfile(expected, bundle / EXPECTED_FILE)
    shutil.copyfile(receipt, bundle / EXPECTED_RECEIPT)
    msa.seal(bundle, 'msa-manifest.json', manifest)
    verified = prepared_expected(bundle / 'input.json', toolkit)
    return {'unit': unit, 'intent_sha256': sha256(bundle.parent / 'cpu-expected-preparation.json'),
            'expected_receipt_sha256': verified['sha256']}


def publish_rf3(result, job, destination):
    """Publish only the checksum-bound CPU selection, retaining every raw sample."""
    receipt = result['result']
    detail = receipt.get('postprocess', {})
    checked = detail.get('result', {})
    if (result.get('state') != 'complete' or checked.get('status') != 'complete'
            or checked.get('job_id') != job['id'] or checked.get('job_sha256') != digest(job)
            or detail.get('job_sha256') != digest(job) or detail.get('stage_sha256') != digest(job['postprocess'])):
        raise ValueError('RF3 completion lacks the exact successful CPU chemistry stage')
    verify_inventory(receipt['output_dir'], receipt['files'])
    verify_inventory(detail['output_dir'], detail['files'])
    if sha256(detail['result_file']) != detail['sha256'] or read(detail['result_file']) != checked:
        raise ValueError('RF3 CPU completion receipt changed')
    source = Path(checked['output_dir'])
    if not source.resolve().is_relative_to(Path(detail['output_dir']).resolve()):
        raise ValueError('RF3 validated output is outside its CPU execution')
    verify_inventory(source, checked['files'])
    if inventory(source) != checked['files']:
        raise ValueError('RF3 validated output has an unexpected file inventory')
    validation = checked.get('output_validation', {})
    if (validation.get('status') != 'passed' or read(source / 'rf3-output-validation.json') != validation
            or sha256(source / 'rf3-output-validation.json') != checked['output_validation_sha256']):
        raise ValueError('RF3 chemical validation report changed or did not pass')
    selected = validation['selected']['files']
    for name in ('model', 'summary', 'confidences'):
        item = selected[name]
        if checked['files'].get(item['path'], {}).get('sha256') != item['sha256']:
            raise ValueError('RF3 selected output is not in the validated inventory')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.rf3-publish-', dir=destination.parent))
    try:
        shutil.copytree(source, stage, dirs_exist_ok=True)
        if inventory(stage) != checked['files'] or inventory(source) != checked['files']:
            raise ValueError('RF3 validated output changed during publication')
        runtime = {'schema': 1, 'model': 'rf3', 'status': 'complete', 'execution': 'resident',
            'settings': checked['settings'], 'rng_policy': checked['rng_policy'],
            'prepared_sha256': checked['prepared_sha256'], 'load_receipt_sha256': checked['load_receipt_sha256'],
            'outputs': {'model': selected['model']['path'], 'model_sha256': selected['model']['sha256'],
                'summary': selected['summary']['path'], 'summary_sha256': selected['summary']['sha256'],
                'ranking_score': validation['selected']['ranking_score']},
            'output_validation': validation, 'output_validation_sha256': checked['output_validation_sha256'],
            'request_sha256': digest(job), 'resident_completion_sha256': digest(result)}
        atomic_json(stage / 'rf3-runtime.json', runtime, exclusive=True)
        atomic_json(stage / 'resident-result.json', result, exclusive=True)
        atomic_json(stage / 'job.json', job, exclusive=True)
        _rename_new(stage, destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return destination


def submit_rf3(args, policy, config, worker):
    if not args.bundle or not args.fasta:
        raise ValueError('RF3 resident routing requires the original input and fully prepared MSA bundle')
    if config.get('postprocess_mode') != 'deferred' or policy.get('postprocess', {}).get('kind') != 'pinned-command':
        raise ValueError('RF3 resident profile must declare its mandatory pinned CPU chemistry stage')
    from inference.adapters.rf3 import _normalize_native
    native = _normalize_native(config['native_config'], config['checkpoint']['path'])
    if policy.get('seeds') != [native['seed']]:
        raise ValueError('RF3 profile seed must match the cold-initialization configuration')
    started = now()
    entry, manifest, document, search, identity = rf3_preparation(args.bundle, args.fasta, args.backend,
        config, worker['runtime_image']['sha256'], args.chemistry_sha)
    search_cache_request = None
    if getattr(args, 'rf3_preparation_receipt', None):
        record = rf3_modules()[0].read_json(args.rf3_preparation_receipt)
        if (record.get('kind') != 'rf3-preparation-cache-request'
                or record.get('input_sha256') != sha256(args.fasta)
                or record.get('prepared_sha256') != manifest['sha256']
                or record.get('search_sha256') != search['sha256']
                or Path(record['bundle']).absolute() != entry.parent.absolute()
                or ArtifactCache(args.shared / 'inference/rf3-preparation-cache').lookup(
                    record['cache_receipt']['identity']) != record['cache_receipt']):
            raise ValueError('RF3 per-request search cache evidence differs from the prepared input')
        search_cache_request = {'path': str(args.rf3_preparation_receipt),
                                'sha256': sha256(args.rf3_preparation_receipt), 'record': record}
    cache = ArtifactCache(args.shared / 'inference/cache')
    cached = cache.lookup(identity)
    reused = cached is not None
    job_id = 'rf3-resident-' + time.strftime('%Y%m%d-%H%M%S', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
    run = Path(worker['input_root']) / job_id
    run.mkdir(parents=True, exist_ok=False)
    original = run / ('original.fasta' if manifest['source_format'] == 'fasta' else 'original.json')
    shutil.copyfile(args.fasta, original)
    if sha256(original) != identity['input']['sha256']:
        raise ValueError('Original RF3 molecular input changed during its request copy')
    bundle = run / 'prepared-bundle'
    preparation = None
    if cached is None:
        source = run / 'cpu-prepared'
        shutil.copytree(entry.parent, source)
        if rf3_modules()[0].validate(source / 'input.json') != manifest:
            raise ValueError('RF3 input changed before CPU chemistry preparation')
        preparation = prepare_rf3_expected(args.state, worker, source, args.timeout)
        cached = cache.publish(source, identity)
    cache.materialize(cached, bundle)
    from inference.adapters.rf3 import prepared_expected
    binding = prepared_expected(bundle / 'input.json', Path(__file__).absolute().parent.parent)
    if binding is None or binding['base_prepared_sha256'] != manifest['sha256']:
        raise ValueError('RF3 cached expected chemistry differs from its captured input')
    prepared_manifest = rf3_modules()[0].validate(bundle / 'input.json')
    native.update(inputs=str(bundle / 'input.json'), out_dir=None, cyclic_chains=document.get('cyclic_chains', []))
    job = {'id': job_id, 'model': 'rf3', 'config_id': configuration_id(config),
        'native_input': str(bundle / 'input.json'), 'seeds': list(policy['seeds']), 'native_config': native,
        'postprocess': deepcopy(policy['postprocess']),
        'provenance': {'cache_receipt': cached, 'reused_preparation': reused, 'input_sha256': sha256(original),
            'prepared_sha256': prepared_manifest['sha256'], 'base_prepared_sha256': manifest['sha256'],
            'expected_chemistry_receipt_sha256': binding['sha256'], 'cpu_preparation': preparation,
            'search_sha256': search['sha256'],
            'search_cache_request': search_cache_request,
            'library_reference': args.library_reference, 'chemistry_sha256': args.chemistry_sha,
            'profile': policy, 'preparation_seconds': now() - started},
        'input_files': {'root': str(bundle), 'files': inventory(bundle)}}
    atomic_json(run / 'job.json', job, exclusive=True)
    queue = Queue(args.state / 'jobs.sqlite')
    queue.enqueue(job)
    print('bio-submit: resident request ' + job_id + ' queued', flush=True)
    result = wait(queue, job_id, args.timeout)
    destination = publish_rf3(result, job, args.results / job_id)
    print('results at ' + str(destination), flush=True)
    return {'job_id': job_id, 'state': result['state'], 'output_dir': str(destination)}


def submit(args):
    if getattr(args, 'rf3_prepare_only', False):
        return rf3_prepare_cached(args)
    policy, config, worker = profile(args.state, args.model, getattr(args, 'native_seed', None))
    if args.probe:
        return {'available': True, 'config_id': policy['config_id']}
    if args.model == 'rf3':
        return submit_rf3(args, policy, config, worker)
    started = now()
    job_id = args.model + '-resident-' + time.strftime('%Y%m%d-%H%M%S', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
    run = Path(worker['input_root']) / job_id
    run.mkdir(parents=True)
    fasta = run / 'input.fasta'
    shutil.copyfile(args.fasta, fasta)
    parser = Path(__file__).absolute().parent.parent / 'msa/prepared.py'
    cache = ArtifactCache(args.shared / 'inference/cache')
    if args.bundle:
        manifest = prepared_module().validate(args.bundle, args.model, fasta)
        source = deepcopy(manifest['source'])
        db = source.get('database_provenance')
        if source['kind'] == 'private' and not db:
            raise ValueError('Private prepared input lacks verified database provenance')
        source['database'] = ({'status': 'verified', 'sha256': sha256(args.bundle / db)} if db
                              else {'status': 'provider-unreported', 'provider': source['endpoint']})
        # Explicit bundles name a specific captured search, never a fresh query.
        source['bundle_manifest_sha256'] = sha256(args.bundle / 'manifest.json')
        identity = preparation_identity(args.model, fasta, parser, source['kind'], source,
                                        chemistry_sha=args.chemistry_sha)
        cached = cache.publish(args.bundle, identity)
        reused = True
    else:
        if args.backend != 'public':
            raise ValueError('Private inference requires a validated private MSA preparation bundle')
        source = {'endpoint': args.endpoint,
                  'database': {'status': 'provider-unreported', 'provider': args.endpoint}}
        identity = preparation_identity(args.model, fasta, parser, 'public', source,
            chemistry_sha=args.chemistry_sha, runtime_id=worker['runtime_image']['sha256'])
        identity['preparation_toolchain_sha256'] = digest(worker['source_files'])
        cached = None if args.refresh_preparation else cache.lookup(identity)
        reused = cached is not None
        if cached is None:
            bundle = prepare_public(args.state, worker, args.model, fasta, run / 'public-prepared', args.timeout, args.endpoint)
            prepared_module().validate(bundle, args.model, fasta)
            if args.refresh_preparation:
                # A deliberate refresh creates another immutable search identity.
                identity['refresh_generation'] = job_id
            cached = cache.publish(bundle, identity)
    bundle = run / 'prepared-bundle'
    cache.materialize(cached, bundle)
    job = materialize_job(args.model, bundle, run / 'native-input', config, job_id, policy['seeds'], fasta)
    job['provenance'] = {'cache_receipt': cached, 'reused_preparation': reused, 'input_sha256': sha256(fasta),
                         'library_reference': args.library_reference, 'chemistry_sha256': args.chemistry_sha,
                         'profile': policy, 'preparation_seconds': now() - started}
    if getattr(args, 'native_seed', None) is not None:
        job['provenance']['requested_native_seed'] = args.native_seed
    job['input_files'] = {'root': str(run / 'native-input'), 'files': inventory(run / 'native-input')}
    atomic_json(run / 'job.json', job, exclusive=True)
    queue = Queue(args.state / 'jobs.sqlite')
    queue.enqueue(job)
    print('bio-submit: resident request ' + job_id + ' queued', flush=True)
    result = wait(queue, job_id, args.timeout)
    receipt = result['result']
    verify_inventory(receipt['output_dir'], receipt['files'])
    destination = args.results / job_id
    shutil.copytree(receipt['output_dir'], destination)
    atomic_json(destination / 'resident-result.json', result, exclusive=True)
    atomic_json(destination / 'job.json', job, exclusive=True)
    print('results at ' + str(destination), flush=True)
    return {'job_id': job_id, 'state': result['state'], 'output_dir': str(destination)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=Path('/var/lib/bio-inference'))
    parser.add_argument('--shared', type=Path, default=Path('/mnt/bio-shared'))
    parser.add_argument('--results', type=Path, default=Path('/var/lib/bio-runs'))
    parser.add_argument('--model', choices=('protenix', 'openfold3', 'boltz2', 'rf3'), required=True)
    parser.add_argument('--fasta', type=Path)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--backend', choices=('public', 'private'), default='public')
    parser.add_argument('--endpoint', default='https://api.colabfold.com')
    parser.add_argument('--timeout', type=int, default=7200)
    parser.add_argument('--chemistry-sha')
    parser.add_argument('--library-reference')
    parser.add_argument('--native-seed', type=int, help='Explicit audited native seed (Boltz: 42)')
    parser.add_argument('--refresh-preparation', action='store_true')
    parser.add_argument('--rf3-prepare-only', action='store_true', help='Verify/reuse RF3 preparation before search or rental')
    parser.add_argument('--native-json', type=Path)
    parser.add_argument('--rf3-out', type=Path)
    parser.add_argument('--rf3-name', default='rf3_job')
    parser.add_argument('--rf3-preparation-receipt', type=Path)
    parser.add_argument('--rf3-database-root', type=Path,
                        default=Path(os.environ.get('MSA_DB_ROOT', '/mnt/bio-msa-databases/colabfold')))
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(submit(args), allow_nan=False))
    except Unavailable as exc:
        print('bio-submit: ' + str(exc), file=sys.stderr)
        raise SystemExit(78)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        if not args.rf3_prepare_only:
            raise
        print('bio-submit: RF3 preparation failed: ' + str(exc), file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print('See the retained preparation request log beside --rf3-out.', file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
