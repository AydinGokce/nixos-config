"""Typed input preservation and bounded, native CPU compatibility checks."""
from __future__ import annotations

from copy import deepcopy
import fcntl
import importlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from .catalog import FOLDING, FORMATS, MOLECULES, check_settings, specification
from .common import (Error, atomic, canonical, digest, file_sha, identifier, inventory, keys,
                     no_links, parse, read_json, require, safe_file, string, uid, write_json)

CANONICAL = set('ACDEFGHIKLMNPQRSTVWY')
AA3 = set('ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL'.split())


def request(params):
    keys(params, ('request_key', 'inputs', 'models'), ('name', 'mode', 'msa_backend', 'execution', 'settings'))
    string(params['request_key'], 'request_key', 200)
    result = deepcopy(params)
    result.setdefault('name', 'Untitled batch'); string(result['name'], 'name', 200)
    result.setdefault('mode', 'batch'); require(isinstance(result['mode'], str) and result['mode'] in {'batch', 'assembly'}, 'Invalid mode')
    result.setdefault('msa_backend', 'public'); require(isinstance(result['msa_backend'], str) and result['msa_backend'] in {'public', 'private'}, 'Invalid MSA backend')
    result.setdefault('execution', 'auto'); require(isinstance(result['execution'], str) and result['execution'] in {'auto', 'resident', 'ephemeral'}, 'Invalid execution')
    require(isinstance(result['inputs'], list) and 0 < len(result['inputs']) <= 128, 'Expected 1..128 inputs', 'limit')
    require(isinstance(result['models'], list) and 0 < len(result['models']) <= 9, 'Expected a nonempty model list')
    require(all(isinstance(model, str) for model in result['models']), 'Model IDs must be strings')
    require(len(set(result['models'])) == len(result['models']), 'Repeated model')
    result.setdefault('settings', {})
    require(isinstance(result['settings'], dict) and set(result['settings']) <= set(result['models']), 'Settings must match selected models')
    for model in result['models']:
        specification(model)
        check_settings(model, result['settings'].get(model, {}))
    ids = set()
    for item in result['inputs']:
        keys(item, ('id', 'name', 'molecule_type', 'source'), ('chain_id',))
        string(item['id'], 'input id', 128); string(item['name'], 'input name', 200)
        require(item['id'] not in ids, 'Repeated input id'); ids.add(item['id'])
        require(item['molecule_type'] in MOLECULES, 'Invalid molecule type')
        if 'chain_id' in item:
            require(isinstance(item['chain_id'], str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_]{0,31}', item['chain_id']), 'Invalid chain ID')
        source = item['source']
        require(isinstance(source, dict), 'Source must be an object')
        kind = source.get('kind'); require(isinstance(kind, str), 'Source kind must be a string')
        if kind == 'library':
            keys(source, ('kind', 'ref')); string(source['ref'], 'library ref')
        elif kind in {'text', 'upload'}:
            keys(source, ('kind', 'format', 'text' if kind == 'text' else 'upload_id'), ('attachments',))
            require(source['format'] in FORMATS, 'Unknown input format')
            if kind == 'text':
                require(isinstance(source['text'], str) and 0 < len(source['text'].encode()) <= 1500000, 'Text input is empty or too large', 'limit')
            else:
                identifier(source['upload_id'])
            if 'attachments' in source:
                require(source['format'] == 'library-json' and isinstance(source['attachments'], dict), 'Attachments require library-json')
                for name, upload in source['attachments'].items():
                    require(isinstance(name, str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name) and name != 'record.json', 'Attachment key must be a plain filename')
                    identifier(upload)
        else:
            raise Error('invalid', 'Unknown source kind')
    return result


def upload_path(store, actor, ident):
    data = store.read('upload', ident, actor)
    require(data['state'] == 'complete', 'Upload is incomplete', 'conflict')
    path = store.directory('uploads', ident) / 'content'
    require(path.stat().st_size == data['size'] and file_sha(path) == data['sha256'], 'Uploaded bytes changed', 'integrity')
    return path


def source_bytes(store, actor, source):
    if source['kind'] == 'text':
        return source['text'].encode()
    return upload_path(store, actor, source['upload_id']).read_bytes()


def fasta(raw):
    require(isinstance(raw, str), 'FASTA must be UTF-8 text')
    records, current = [], None
    seen = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('>'):
            require(len(line) > 1 and line[1:].split(), 'FASTA header is empty')
            name = line[1:].split()[0]; string(name, 'FASTA identifier', 200)
            require(name not in seen, 'Repeated FASTA identifier: ' + name); seen.add(name)
            current = [name, '']; records.append(current)
        else:
            require(current is not None, 'FASTA sequence precedes its header')
            require(re.fullmatch('[A-Za-z]+', line) is not None, 'FASTA sequence contains non-letter symbols')
            current[1] += line.upper()
    require(records and all(seq for _, seq in records), 'FASTA contains an empty sequence')
    return records


def expand(store, actor, document):
    if document['mode'] == 'assembly':
        return [{'id': 'assembly', 'name': document['name'], 'components': document['inputs']}]
    out = []
    for item in document['inputs']:
        try:
            records = fasta(source_bytes(store, actor, item['source']).decode()) if item['source'].get('format') == 'fasta' else None
            if records:
                for index, (name, sequence) in enumerate(records):
                    component = deepcopy(item)
                    component['source'] = {'kind': 'text', 'format': 'sequence', 'text': sequence}
                    out.append({'id': item['id'] + ':' + str(index + 1), 'name': name,
                                'components': [component], 'fasta_original': item, 'record_count': len(records)})
            else:
                out.append({'id': item['id'], 'name': item['name'], 'components': [item]})
        except (ValueError, UnicodeError, OSError) as exc:
            out.append({'id': item['id'], 'name': item['name'], 'components': [item], 'error': str(exc)})
    return out


def pairs(store, actor, document):
    out, sets = [], set()
    for item in expand(store, actor, document):
        for model in document['models']:
            selected = item
            if model == 'evolvepro' and 'fasta_original' in item:
                ident = item['fasta_original']['id']
                if ident in sets:
                    continue
                sets.add(ident)
                selected = {'id': ident, 'name': item['fasta_original']['name'], 'components': [item['fasta_original']]}
            out.append({'pair_id': uid(), 'input_id': selected['id'], 'input_name': selected['name'],
                        'model': model, 'state': 'pending', 'reasons': [], 'job_id': None, '_input': selected})
            require(len(out) <= 512, 'Expanded input/model combinations exceed 512', 'limit')
    return out


def library(tools):
    path = str(Path(tools) / 'library')
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module('registry')


def clone_record(source, target, ref, module, visited=None):
    visited = set() if visited is None else visited
    record = source.show(ref); pin = module.reference(record)
    if pin in visited:
        return pin
    visited.add(pin)
    dependencies = list(record.get('parents', []))
    for _, value in module.reference_values(record['identity']):
        dependencies.extend(value if isinstance(value, list) else [value])
    for child in dependencies:
        clone_record(source, target, child, module, visited)
    destination = target._path(pin).parent
    if destination.exists():
        require(target.show(pin)['sha256'] == record['sha256'], 'Conflicting library revisions')
    else:
        original = source.record_path(pin).parent
        before = inventory(original)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(original, destination)
        require(inventory(destination) == before, 'Library copy changed', 'integrity')
    return pin


def add_component(store, actor, item, target, module, config, index):
    source = item['source']
    if source['kind'] == 'library':
        original = module.Registry(config['library_root'])
        return clone_record(original, target, source['ref'], module)
    raw = source_bytes(store, actor, source)
    fmt = source['format']
    ident = 'input-' + str(index)
    attachments = {}
    if fmt == 'library-json':
        document = parse(raw)
        if 'records' in document:
            keys(document, ('records', 'entrypoint'))
            require(isinstance(document['records'], list) and 0 < len(document['records']) <= 256, 'Expected 1..256 library records')
            records, entrypoint = document['records'], document['entrypoint']
        else:
            records, entrypoint = [document], None
        assets = {name: upload_path(store, actor, upload) for name, upload in source.get('attachments', {}).items()}
        last = None
        for record in records:
            require(isinstance(record, dict) and record.get('kind') in {'construct', 'assembly', 'monomer'}, 'Only molecular library records are accepted')
            needed = record.get('identity', {}).get('structure_file')
            selected = {}
            if needed is not None:
                require(isinstance(needed, str) and needed.startswith('attachments/') and needed.count('/') == 1, 'Invalid structure attachment path')
                name = needed.split('/')[1]
                require(name in assets, 'Missing attachment upload for ' + name)
                selected[name] = assets[name]
            last = target.import_record(record, selected)
        return target.resolve(entrypoint or module.reference(last))
    if fmt in {'sequence', 'fasta'}:
        seqs = fasta(raw.decode()) if fmt == 'fasta' else [(item['name'], ''.join(raw.decode().split()).upper())]
        require(len(seqs) == 1, 'An assembly component must have one FASTA record')
        require(item['molecule_type'] in {'protein', 'dna', 'rna'}, 'Sequence input needs an explicit polymer type')
        identity = {'molecule_type': item['molecule_type'], 'sequence': seqs[0][1]}
    elif fmt in {'smiles', 'ccd', 'sdf'}:
        require(item['molecule_type'] == 'ligand', 'Chemical input requires molecule_type ligand')
        identity = {'molecule_type': 'small_molecule'}
        if fmt == 'sdf':
            identity.update(structure_file='attachments/source.sdf', structure_format='sdf')
            saved = target.root / '.staging' / ('input-' + str(index) + '.sdf')
            atomic(saved, raw, exclusive=True)
            attachments['source.sdf'] = saved
        else:
            identity['smiles' if fmt == 'smiles' else 'ccd'] = raw.decode().strip()
    else:
        raise Error('invalid', 'This format is not a typed molecular construct')
    record = target.import_record({'kind': 'construct', 'id': ident, 'name': item['name'], 'identity': identity}, attachments)
    return module.reference(record)


def native_compile(config, registry, ref, model, destination, backend, log):
    module = library(config['tools_dir'])
    runtime = importlib.import_module('runtime')
    runtime_config = read_json(Path(config['runtime_config']).resolve())
    arguments = ['--root', str(registry.root), '--ref', ref, '--model', model,
                 '--out', str(destination), '--msa-backend', backend]
    if backend == 'private' and model != 'rf3':
        arguments.append('--plain-fasta')
    state = no_links(Path(runtime_config.get('state', '/var/lib/bio-library-runtime')))
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = no_links(state / 'preflight.lock')
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix='workbench-', dir=state) as scratch:
            argv, env = runtime.command(arguments, runtime_config, Path(scratch))
            with log.open('ab') as output:
                output.write(canonical({'native_cpu_argv': argv, 'cuda_visible_devices': ''}) + b'\n'); output.flush()
                result = subprocess.run(argv, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=600, check=False)
            if result.returncode != 0:
                lines = log.read_text(errors='replace').splitlines()
                detail = next((line.strip() for line in reversed(lines) if line.strip()), 'native parser exited unsuccessfully')
                raise Error('invalid', 'Native CPU parser: ' + detail[-2000:])
    return read_json(destination / 'bundle.json')


def pdb_check(raw):
    residues = {}
    models = 0
    for line in raw.decode().splitlines():
        if line.startswith('MODEL '):
            models += 1
        if line.startswith(('ATOM  ', 'HETATM')):
            require(line.startswith('ATOM  '), 'Structure design accepts canonical protein ATOM records only; ligand chemistry cannot be silently discarded')
            require(len(line) >= 54 and line[16:17] in {' ', 'A'}, 'PDB atom is incomplete or has unsupported alternate conformers')
            require(line[17:20] in AA3 and line[26:27] == ' ', 'PDB requires canonical residues without insertion codes')
            coordinates = [float(line[start:start + 8]) for start in (30, 38, 46)]
            require(all(math.isfinite(x) for x in coordinates), 'PDB coordinates must be finite')
            key = (line[21:22], int(line[22:26]))
            atom = line[12:16].strip()
            require(atom not in residues.setdefault(key, set()), 'Duplicate PDB atom/alternate conformer')
            residues[key].add(atom)
    require(models <= 1 and residues, 'Expected one nonempty PDB model')
    require(all({'N', 'CA', 'C', 'O'} <= atoms for atoms in residues.values()), 'Every residue needs N, CA, C, and O backbone atoms')
    return residues


def contig_check(value, residues=None):
    require(isinstance(value, str) and re.fullmatch(r'\[[A-Za-z0-9/ -]+\]', value), 'Invalid native contig syntax')
    tokens = re.split(r'[/ ]+', value[1:-1])
    for token in tokens:
        if token == '0':
            continue
        match = re.fullmatch(r'([A-Za-z]?)([1-9][0-9]*)(?:-([1-9][0-9]*))?', token)
        require(match is not None, 'Invalid contig range')
        chain, start, end = match.groups(); start = int(start); end = int(end or start)
        require(start <= end <= 10000, 'Contig range is reversed or too large')
        if chain:
            require(residues is not None and all((chain, n) in residues for n in range(start, end + 1)), 'Contig motif references absent PDB residues')
    require(any(token != '0' for token in tokens), 'Empty contig design')


def source_pins(config, model):
    tools = Path(config['tools_dir'])
    paths = [Path(config['bio_submit']), tools / 'recipes' / (model + '.sh')]
    paths.extend(sorted((tools / 'library').glob('*.py')))
    if model in FOLDING:
        paths.extend(sorted((tools / 'inference').glob('*.py')))
    if model in {'esm', 'evolvepro'}:
        paths.append(tools / 'py' / ('esm_cli.py' if model == 'esm' else 'evolvepro_cloud.py'))
    return {str(path.resolve()): file_sha(path.resolve()) for path in paths if not path.name.startswith('test_')}


def prepare_pair(store, batch, pair, config, compiler=native_compile):
    model, document = pair['model'], batch['_request']
    spec = specification(model)
    require(spec['enabled'], spec['disabled_reason'])
    item = pair['_input']; require('error' not in item, item.get('error', 'Invalid input'))
    settings = document['settings'].get(model, {})
    backend, execution = document['msa_backend'], document['execution']
    require(backend == 'public' or model in FOLDING, 'This workflow does not use the shared private MSA backend')
    require(execution != 'resident' or model in FOLDING, 'This workflow does not support resident execution')
    actor = store.actor(batch['batch_id'])
    path = store.directory('batches', batch['batch_id']) / 'pairs' / pair['pair_id']
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    write_json(path / 'declared-input.json', item, exclusive=True)
    arguments, native, registry_path = [], None, None
    components = item['components']
    if model in {'mpnn', 'rfdiffusion'}:
        require(len(components) == 1 and components[0]['molecule_type'] == 'structure', 'This workflow requires one structure input')
        source = components[0]['source']; require(source.get('format') in spec['input_formats'], 'This workflow requires a PDB or declared contig input')
        raw = source_bytes(store, actor, source)
        residues = None
        if source['format'] == 'pdb':
            residues = pdb_check(raw)
            atomic(path / 'input.pdb', raw, exclusive=True); arguments += ['--pdb', str(path / 'input.pdb')]
        if model == 'rfdiffusion':
            contigs = settings.get('contigs') or (raw.decode().strip() if source['format'] == 'contigs' else None)
            contig_check(contigs, residues); arguments += ['--contigs', contigs]
        native = {'validation': 'canonical-backbone-and-native-contig-schema', 'residues': len(residues or {})}
    elif model == 'evolvepro' and len(components) == 1 and components[0]['source'].get('format') == 'fasta':
        require(components[0]['molecule_type'] == 'protein', 'EVOLVEpro requires protein variants')
        raw = source_bytes(store, actor, components[0]['source']); records = fasta(raw.decode())
        require(all(not set(seq) - CANONICAL for _, seq in records), 'EVOLVEpro requires canonical protein variants')
        atomic(path / 'input.fasta', raw, exclusive=True); arguments += ['--fasta', str(path / 'input.fasta')]
        native = {'validation': 'native-evolvepro-validate-only', 'variants': len(records)}
    else:
        module = library(config['tools_dir'])
        target = module.Registry(path / 'registry'); target.init(); registry_path = str(target.root)
        refs = [add_component(store, actor, component, target, module, config, n) for n, component in enumerate(components)]
        if len(refs) == 1 and document['mode'] == 'batch':
            ref = refs[0]
        else:
            chains = [component.get('chain_id', chr(65 + n) if n < 26 else 'C' + str(n + 1)) for n, component in enumerate(components)]
            require(len(chains) == len(set(chains)), 'Assembly chain IDs must be unique')
            require(all(target.show(ref)['kind'] == 'construct' for ref in refs), 'Nested assemblies must be submitted as one library assembly')
            record = target.import_record({'kind': 'assembly', 'id': 'workbench-assembly',
                'name': document['name'], 'identity': {'components': [{'chain_id': chain, 'construct_ref': ref} for chain, ref in zip(chains, refs)], 'bonds': []}})
            ref = module.reference(record)
        snapshot = target.snapshot(ref)
        write_json(path / 'source-snapshot.json', snapshot, exclusive=True)
        native = compiler(config, target, ref, model, path / 'native', backend, path / 'validation.log')
        # Single canonical proteins can use the ordinary FASTA path and the same
        # resident defaults. All typed chemistry keeps its immutable library ref.
        adapters = importlib.import_module('adapters')
        try:
            plain = path / 'plain'; plain.mkdir(mode=0o700)
            plain_metadata = adapters.fasta(snapshot, plain)
        except ValueError:
            plain_metadata = None
        if plain_metadata:
            arguments += ['--fasta', str(plain / plain_metadata['entrypoint'])]
        else:
            arguments += ['--' + target.show(ref)['kind'], ref]
    if model == 'evolvepro':
        if 'labels_upload_id' in settings:
            original = upload_path(store, actor, settings['labels_upload_id'])
            atomic(path / 'labels.csv', original.read_bytes(), exclusive=True)
            arguments += ['--labels', str(path / 'labels.csv')]
        script = Path(config['tools_dir']) / 'py' / 'evolvepro_cloud.py'
        fasta_input = arguments[arguments.index('--fasta') + 1] if '--fasta' in arguments else str(path / 'native' / native['entrypoint'])
        check = [sys.executable, str(script), '--input', fasta_input, '--sub', settings.get('sub', 'rank'), '--validate-only']
        if '--labels' in arguments:
            check += arguments[arguments.index('--labels'):arguments.index('--labels') + 2]
        result = subprocess.run(check, capture_output=True, timeout=30, text=True)
        atomic(path / 'workflow-validation.log', (result.stdout + result.stderr).encode(), exclusive=True)
        require(result.returncode == 0, (result.stderr or result.stdout)[-3000:])
    if model == 'esm' and settings.get('sub') == 'mutate':
        mutations = settings.get('mutations', '')
        require(re.fullmatch(r'[A-Z][1-9][0-9]*[A-Z](?:[,:][A-Z][1-9][0-9]*[A-Z])*', mutations), 'Mutate requires explicit substitutions such as A12G,V14L')
        sequence = fasta(Path(arguments[arguments.index('--fasta') + 1]).read_text())[0][1]
        for token in re.split('[,:]', mutations):
            position = int(token[1:-1]); require(position <= len(sequence) and sequence[position - 1] == token[0] and token[-1] in CANONICAL, 'Mutation differs from the declared wild-type sequence')
    elif 'mutations' in settings:
        require(False, 'mutations applies only to ESM mutate')
    for name in ('sub', 'model', 'num'):
        if name in settings:
            arguments += ['--' + name, str(settings[name])]
    if 'temperature' in settings:
        arguments += ['--temp', str(settings['temperature'])]
    arguments += ['--msa-backend', backend, '--execution', execution, '--timeout', str(settings.get('timeout', 7200))]
    extra = []
    for name in ('seed', 'regressor', 'mutations'):
        if name in settings:
            extra += ['--' + name, str(settings[name])]
    if extra:
        arguments += ['--', *extra]
    pins = source_pins(config, model)
    # Registry database/locks are derived mutable indexes; immutable records,
    # source/compiled JSON and all chemical attachments are bound explicitly.
    files = {name: value for name, value in inventory(path).items()
             if not (name.startswith('registry/') and (name.endswith(('.sqlite', '.sqlite-wal', '.sqlite-shm')) or '/.registry.lock' in name))}
    return {'argv': [config['bio_submit'], model, *arguments], 'tools_dir': config['tools_dir'],
            'environment': {'BIO_LIBRARY_ROOT': registry_path} if registry_path else {},
            'input_root': str(path), 'input_files': files, 'source_pins': pins,
            'native_validation': native, 'timeout': settings.get('timeout', 7200), 'settings': settings}
