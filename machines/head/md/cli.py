#!/usr/bin/env python3
"""Prepare, submit and inspect MD runs through the durable head workbench."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).absolute().parent.parent))
from workbench.api import API
from workbench.common import Error, CHUNK, file_sha, parse, safe_file
from workbench.store import Store
from md.bundle import relative_name


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default=os.environ.get('BIO_WORKBENCH_STATE', '/var/lib/bio-workbench'))
    parser.add_argument('--actor', default=os.environ.get('BIO_WORKBENCH_ACTOR', 'harrison'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('catalog')
    for helper in ('analysis', 'parameters', 'prepare'):
        helper_parser = sub.add_parser(helper, add_help=False)
        helper_parser.add_argument('arguments', nargs=argparse.REMAINDER)
    for command in ('plan', 'validate'):
        command_parser = sub.add_parser(command, help='Generate inspectable protocol; no paid execution')
        command_parser.add_argument('--request', required=True, type=Path)
        command_parser.add_argument('--assets', required=True, type=Path)
        if command == 'plan':
            command_parser.add_argument('--out', required=True, type=Path)
        else:
            command_parser.add_argument('--name', required=True)
            command_parser.add_argument('--receipt', required=True, type=Path,
                                        help='Retains uploaded IDs/idempotency key for exact retries')
            command_parser.add_argument('--timeout', type=int, default=7200)
            command_parser.add_argument('--worker', default='auto')
    submit = sub.add_parser('submit', help='Commit one existing validated preview; rents temporary compute')
    submit.add_argument('--batch', required=True)
    submit.add_argument('--request-key', required=True)
    status = sub.add_parser('status')
    status.add_argument('--batch', required=True)
    sub.add_parser('list')
    cancel = sub.add_parser('cancel')
    cancel.add_argument('--batch', required=True)
    resume = sub.add_parser('resume', help='Create a checkpoint-resume preview; submit it explicitly afterward')
    resume.add_argument('--job', required=True)
    resume.add_argument('--request-key', required=True)
    resume.add_argument('--timeout', type=int, default=7200)
    logs = sub.add_parser('logs')
    logs.add_argument('--job', required=True)
    logs.add_argument('--offset', type=int, default=0)
    artifacts = sub.add_parser('artifacts')
    artifacts.add_argument('--job', required=True)
    export = sub.add_parser('export')
    export.add_argument('--job', required=True)
    export.add_argument('--out', required=True, type=Path)
    raw = list(sys.argv[1:] if argv is None else argv)
    # Forward the helper's flags verbatim; argparse subparsers otherwise reject
    # a leading --help/--input before their REMAINDER positional starts.
    position = 0
    while position < len(raw):
        if raw[position] in {'--state', '--actor'}:
            position += 2
        elif raw[position].startswith(('--state=', '--actor=')):
            position += 1
        else:
            break
    helper_arguments = None
    if position < len(raw) and raw[position] in {'analysis', 'parameters', 'prepare'}:
        helper_arguments = raw[position + 1:]
        raw = raw[:position + 1]
    args = parser.parse_args(raw)
    if helper_arguments is not None:
        args.arguments = helper_arguments
    if args.command in {'analysis', 'parameters', 'prepare'}:
        runtime = Path('/var/lib/bio-md/runtime-cpu')
        module = {'analysis': 'md.analysis', 'parameters': 'md.chemistry', 'prepare': 'md.preparation'}[args.command]
        arguments = args.arguments
        if args.command == 'prepare' and '--runtime' not in arguments:
            arguments = ['--runtime', str(runtime), *arguments]
        os.execvp('bash', ['bash', '-c', 'source "$1/activate.sh" || exit; shift; exec "$@"',
                          'bio-md-analysis', str(runtime), 'env',
                          'PYTHONPATH=' + str(Path(__file__).absolute().parent.parent),
                          str(runtime / 'bin/python'), '-m', module, *arguments])
    if args.command == 'plan':
        from md.protocols import prepare, validate_request
        document = validate_request(parse(safe_file(args.request).read_bytes()), args.assets)
        result = prepare(document, args.assets, args.out)
        (args.out / 'plan.json').write_text(json.dumps(result, indent=2) + '\n')
        return result
    api = API(Store(args.state), args.actor)
    if args.command == 'catalog':
        return api.call('md.catalog', {})
    if args.command == 'validate':
        document = parse(safe_file(args.request).read_bytes())
        paths = {relative_name(str(p.relative_to(args.assets))): safe_file(p)
                 for p in sorted(args.assets.rglob('*')) if not p.is_dir()}
        if not paths or len(paths) > 256:
            raise ValueError('Provide 1..256 regular asset files')
        identities = {name: file_sha(path) for name, path in paths.items()}
        intent = {'request': document, 'name': args.name, 'timeout': args.timeout, 'worker': args.worker,
                  'source_sha256': identities}
        from md.worker import save
        if args.receipt.exists():
            receipt = parse(safe_file(args.receipt).read_bytes())
            if receipt['intent'] != intent:
                raise ValueError('Receipt belongs to different inputs/options; use a new receipt path')
        else:
            receipt = {'intent': intent, 'request_key': uuid.uuid4().hex, 'uploads': {}}
            save(args.receipt, receipt)
        for name, path in paths.items():
            if name in receipt['uploads']:
                continue
            upload = api.call('upload.begin', {'name': name, 'size': path.stat().st_size, 'sha256': identities[name]})
            with path.open('rb') as source:
                offset = 0
                while data := source.read(CHUNK):
                    api.call('upload.chunk', {'upload_id': upload['upload_id'], 'offset': offset,
                                             'data_base64': base64.b64encode(data).decode()})
                    offset += len(data)
            api.call('upload.finish', {'upload_id': upload['upload_id'], 'sha256': identities[name]})
            receipt['uploads'][name] = upload['upload_id']
            save(args.receipt, receipt)
        payload = {key: value for key, value in intent.items() if key != 'source_sha256'}
        payload.update(assets=receipt['uploads'], request_key=receipt['request_key'])
        result = api.call('md.validate', payload)
        receipt['batch_id'] = result['batch_id']
        save(args.receipt, receipt)
        return result
    if args.command == 'submit':
        batch = api.call('batch.get', {'batch_id': args.batch})
        if batch.get('workflow') != 'md':
            raise ValueError('This is not an MD batch')
        return api.call('batch.create', {'batch_id': args.batch, 'request_key': args.request_key,
                       'pair_ids': [p['pair_id'] for p in batch['pairs'] if p['state'] == 'compatible']})
    if args.command == 'resume':
        return api.call('md.resume', {'job_id': args.job, 'request_key': args.request_key, 'timeout': args.timeout})
    if args.command in {'status', 'cancel'}:
        return api.call('batch.get' if args.command == 'status' else 'batch.cancel', {'batch_id': args.batch})
    if args.command == 'list':
        return api.call('batch.list', {})
    if args.command == 'logs':
        return api.call('job.logs', {'job_id': args.job, 'offset': args.offset})
    if args.command in {'artifacts', 'export'}:
        artifacts, cursor = [], None
        while True:
            params = {'job_id': args.job, 'limit': 100}
            if cursor:
                params['cursor'] = cursor
            page = api.call('job.artifacts', params)
            artifacts.extend(page['artifacts'])
            cursor = page.get('next_cursor')
            if not cursor:
                break
        if args.command == 'artifacts':
            return {'artifacts': artifacts}
        args.out.mkdir(parents=True, exist_ok=False)
        for artifact in artifacts:
            path = args.out / relative_name(artifact['name'])
            path.parent.mkdir(parents=True, exist_ok=True)
            source = api.store.directory('artifacts', artifact['artifact_id']) / 'content'
            if file_sha(source) != artifact['sha256']:
                raise ValueError('Retained artifact checksum differs')
            import shutil
            shutil.copyfile(source, path)
        return {'out': str(args.out), 'artifacts': len(artifacts)}


if __name__ == '__main__':
    try:
        print(json.dumps(main(), indent=2, allow_nan=False))
    except (Error, ValueError, OSError) as exc:
        print('bio-md: ' + str(exc), file=sys.stderr)
        sys.exit(2)
