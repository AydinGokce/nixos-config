#!/usr/bin/env python3
"""Compile pinned library snapshots to verified, model-specific input bundles."""
import argparse
import contextlib
import datetime
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from registry import Registry, digest_json, load_json, no_symlinks, reference, reference_values, safe_relative, verify_document
from chemistry import components, polymer, bonds, require, write_json

MODELS = {'boltz2': 'boltz_adapter', 'openfold3': 'openfold3_adapter',
          'protenix': 'protenix_adapter', 'rfaa': 'rfaa_adapter'}


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_snapshot(snapshot):
    verify_document(snapshot)
    require(type(snapshot.get('schema')) is int and snapshot['schema'] == 1 and snapshot.get('kind') == 'resolved-assembly', 'Expected a resolved library snapshot')
    for component in components(snapshot):
        record = component['record']
        verify_document(record)
        require(record['kind'] == 'construct' and reference(record) == component['construct_ref'], 'Construct reference/hash mismatch')
    for ref, record in snapshot.get('monomers', {}).items():
        verify_document(record)
        require(record['kind'] == 'monomer' and reference(record) == ref, 'Monomer reference/hash mismatch')
    if 'assembly_record' in snapshot:
        source = snapshot['assembly_record']
        verify_document(source)
        require(source['kind'] == 'assembly' and reference(source) == snapshot['source_ref'], 'Assembly snapshot source mismatch')
        expected = [{k: c[k] for k in ('chain_id', 'construct_ref')} for c in snapshot['components']]
        require(source['identity']['components'] == expected and source['identity'].get('bonds', []) == snapshot['bonds'],
                'Resolved assembly differs from its pinned source components/bonds')
    else:
        require(len(snapshot['components']) == 1 and snapshot['components'][0]['chain_id'] == 'A' and not snapshot['bonds'],
                'A construct snapshot must contain its single implicit chain A')
        source = snapshot['components'][0]['record']
        require(reference(source) == snapshot['source_ref'], 'Construct snapshot source mismatch')
    require(snapshot['provenance'] == {'registry_source_ref': snapshot['source_ref'], 'source_record_sha256': source['sha256']},
            'Snapshot provenance differs from its pinned source')
    needed, visited = set(), set()
    def visit(record):
        if reference(record) in visited:
            return
        visited.add(reference(record))
        for kind, ref in reference_values(record['identity']):
            if kind == 'monomer_ref':
                needed.add(ref)
                require(ref in snapshot['monomers'], 'Snapshot is missing a referenced monomer')
                visit(snapshot['monomers'][ref])
    visit(source)
    for component in snapshot['components']:
        visit(component['record'])
    require(needed == set(snapshot['monomers']), 'Snapshot monomer closure contains unrelated records')


def copy_assets(registry, snapshot, destination):
    records = {c['construct_ref']: c['record'] for c in snapshot['components']}
    records.update(snapshot.get('monomers', {}))
    if 'assembly_record' in snapshot:
        records[snapshot['source_ref']] = snapshot['assembly_record']
    assets = {}
    for ref, record in records.items():
        assets[ref] = {}
        for attachment in record['attachments']:
            relative = attachment['path']
            source = registry.attachment_path(ref, relative)
            target = destination/'assets'/record['kind']/record['id']/str(record['revision'])/safe_relative(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            require(target.stat().st_size == attachment['bytes'] and file_digest(target) == attachment['sha256'], 'Attachment changed while creating the input snapshot')
            assets[ref][relative] = target
    return assets


def fasta(snapshot, destination):
    entries = components(snapshot)
    require(len(entries) == 1 and not bonds(snapshot), 'This input path requires exactly one unlinked canonical protein')
    kind, sequence, modifications, circular = polymer(entries[0], snapshot)
    require(kind == 'protein' and not modifications and not circular,
            'This input path requires one canonical linear protein without chemical modifications')
    path = destination/'input.fasta'
    path.write_text('>construct\n'+sequence+'\n')
    return dict(entrypoint=path.name, format='protein-fasta', has_protein=True,
                expected_chains=['A'], chain_mapping={'A': entries[0]['chain_id']}, model_version=None)


def manifest_files(directory):
    result = {}
    for path in sorted(directory.rglob('*')):
        no_symlinks(path)
        if path.is_dir():
            continue
        require(path.is_file(), 'Input bundles may contain only ordinary files and directories')
        relative = path.relative_to(directory).as_posix()
        if relative == 'bundle.json':
            continue
        result[relative] = dict(bytes=path.stat().st_size, sha256=file_digest(path))
    return result


def compile_input(root, ref, model, destination, *, plain_fasta=False, msa_backend='public'):
    require(model in MODELS or model in {'esm', 'evolvepro'}, 'This tool requires a folding/sequence model; backbone design tools still need a structure input')
    require(msa_backend in {'public', 'private'}, 'Unknown MSA backend')
    registry = Registry(root)
    snapshot = registry.snapshot(ref)
    verify_snapshot(snapshot)
    destination = Path(destination).absolute()
    no_symlinks(destination)
    require(not destination.exists() or destination.is_dir() and not any(destination.iterdir()), 'Input destination must be absent or empty')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.native-input-', dir=destination.parent))
    try:
        assets = copy_assets(registry, snapshot, staging)
        write_json(staging/'source.json', snapshot)
        if plain_fasta or model in {'esm', 'evolvepro'}:
            metadata = fasta(snapshot, staging)
            preflight = dict(native_parser=False, canonical_single_protein=True, model_inference=False, msa_queries=False)
        else:
            require(msa_backend == 'public', 'Mixed/native construct input currently requires the public MSA path; private preparation supports a single canonical protein. No fallback was performed.')
            module = importlib.import_module(MODELS[model])
            # Native parsers can print informational messages. Keep stdout
            # reserved for the machine-readable result returned by this CLI.
            with contextlib.redirect_stdout(sys.stderr):
                metadata = module.build(snapshot, staging, assets, {'msa_backend': msa_backend})
                preflight = module.preflight(staging, metadata)
            require(preflight.get('native_parser') is True, 'Native CPU parser validation did not complete')
        write_json(staging/'preflight.json', preflight)
        entrypoint = str(safe_relative(metadata['entrypoint']))
        require((staging/entrypoint).is_file(), 'Adapter did not create its native entrypoint')
        sources = {path.name: file_digest(path) for path in Path(__file__).parent.glob('*.py')
                   if not path.name.startswith('test_')}
        document = dict(metadata, schema=1, kind='native-input-bundle', model=model,
                        entrypoint=entrypoint, source_ref=snapshot['source_ref'],
                        source_snapshot_sha256=snapshot['sha256'],
                        msa_backend='local-hhsuite' if model == 'rfaa' else msa_backend,
                        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        adapter_sources=sources, files=manifest_files(staging))
        document['sha256'] = digest_json(document)
        write_json(staging/'bundle.json', document)
        validate_bundle(staging, model)
        if destination.exists():
            destination.rmdir()
        staging.rename(destination)
        return dict(bundle=str(destination), sha256=document['sha256'], model=model,
                    format=document['format'], entrypoint=str(destination/entrypoint),
                    source_ref=document['source_ref'], preflight=preflight)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def validate_bundle(bundle, model, expected=None):
    bundle = Path(bundle).absolute()
    no_symlinks(bundle)
    document = load_json(bundle/'bundle.json')
    verify_document(document)
    require(type(document.get('schema')) is int and document['schema'] == 1 and document.get('kind') == 'native-input-bundle', 'Invalid native input bundle schema')
    require(document.get('model') == model, 'Native input bundle belongs to a different model')
    require(expected is None or document['sha256'] == expected, 'Native input bundle differs from the submitted snapshot')
    require(document.get('files') == manifest_files(bundle), 'Native input file inventory/checksums changed')
    require(document['entrypoint'] in document['files'], 'Native entrypoint is not bound to the bundle manifest')
    snapshot = load_json(bundle/'source.json')
    verify_snapshot(snapshot)
    require(snapshot['sha256'] == document['source_snapshot_sha256'] and snapshot['source_ref'] == document['source_ref'], 'Native bundle source binding changed')
    return document


def materialize(bundle, model, destination, expected=None):
    document = validate_bundle(bundle, model, expected)
    sources = {path.name: file_digest(path) for path in Path(__file__).parent.glob('*.py')
               if not path.name.startswith('test_')}
    require(document['adapter_sources'] == sources, 'Runtime adapter source differs from the checked compiler; recheck the construct with the deployed version')
    destination = Path(destination).absolute()
    no_symlinks(destination)
    require(not destination.exists(), 'Native runtime output already exists; use a fresh job directory')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.native-copy-', dir=destination.parent))
    try:
        shutil.copytree(bundle, temporary, dirs_exist_ok=True)
        validate_bundle(temporary, model, document['sha256'])
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination/document['entrypoint']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    compile_parser = subparsers.add_parser('compile')
    compile_parser.add_argument('--root', default=os.environ.get('BIO_LIBRARY_ROOT', '/var/lib/bio-library'))
    compile_parser.add_argument('--ref', required=True)
    compile_parser.add_argument('--model', required=True)
    compile_parser.add_argument('--out', required=True, type=Path)
    compile_parser.add_argument('--plain-fasta', action='store_true')
    compile_parser.add_argument('--msa-backend', default='public', choices=['public', 'private'])
    for name in ('validate', 'materialize'):
        child = subparsers.add_parser(name)
        child.add_argument('--bundle', required=True, type=Path)
        child.add_argument('--model', required=True)
        child.add_argument('--expected-sha256', default=os.environ.get('BIO_NATIVE_SHA256') or None)
        if name == 'materialize':
            child.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    if args.command == 'compile':
        print(json.dumps(compile_input(args.root, args.ref, args.model, args.out,
                                      plain_fasta=args.plain_fasta, msa_backend=args.msa_backend), indent=2))
    elif args.command == 'validate':
        print(json.dumps(validate_bundle(args.bundle, args.model, args.expected_sha256), indent=2))
    else:
        print(materialize(args.bundle, args.model, args.out, args.expected_sha256))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, RuntimeError) as error:
        print('bio-input-adapter: ' + str(error), file=sys.stderr)
        sys.exit(2)
