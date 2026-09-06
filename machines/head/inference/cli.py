#!/usr/bin/env python3
"""Operator interface for durable requests and explicitly bounded warm workers."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).absolute().parent.parent))
from inference.common import read
from inference.dispatcher import Dispatcher
from inference.job_queue import Queue
from inference.postprocess import process


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=Path(os.environ.get('BIO_INFERENCE_STATE', '/var/lib/bio-inference')))
    commands = parser.add_subparsers(dest='command', required=True)
    submit = commands.add_parser('enqueue')
    submit.add_argument('--job', type=Path, required=True)
    submit.add_argument('--request-key')
    submit.add_argument('--wait', action='store_true')
    submit.add_argument('--timeout', type=int, default=7200)
    for name in ('status', 'retry', 'wait'):
        command = commands.add_parser(name)
        command.add_argument('id')
        if name == 'wait':
            command.add_argument('--timeout', type=int, default=7200)
    listing = commands.add_parser('list')
    listing.add_argument('--status')
    commands.add_parser('workers')
    commands.add_parser('tick')
    commands.add_parser('serve')
    launch = commands.add_parser('worker-start')
    launch.add_argument('--target', type=Path, required=True)
    launch.add_argument('--config', type=Path, required=True)
    launch.add_argument('--policy', type=Path, required=True)
    args = parser.parse_args(argv)
    queue = Queue(args.state / 'jobs.sqlite')
    if args.command == 'worker-start':
        from inference.pool import start
        result = start(read(args.target), read(args.config), read(args.policy), args.state)
    elif args.command in ('tick', 'serve'):
        dispatcher = Dispatcher(queue, args.state / 'workers', postprocessor=process)
        try:
            while True:
                result = dispatcher.tick()
                if args.command == 'tick':
                    break
                if result['errors']:
                    print(json.dumps(result), flush=True)
                time.sleep(1)
        finally:
            dispatcher.close()
    elif args.command == 'enqueue':
        result = queue.enqueue(read(args.job), request_key=args.request_key)
        if args.wait:
            result = wait(queue, result['id'], args.timeout)
    elif args.command == 'status':
        result = queue.get(args.id)
        if result is None:
            raise ValueError('Unknown inference request')
    elif args.command == 'wait':
        result = wait(queue, args.id, args.timeout)
    elif args.command == 'retry':
        queue.retry(args.id)
        result = queue.get(args.id)
    elif args.command == 'list':
        result = queue.list(args.status)
    else:
        result = []
        for path in sorted((args.state / 'workers').glob('*.json')):
            config = read(path)
            status = Path(config['spool_root']) / config['worker_id'] / 'status.json'
            result.append({'worker_id': config['worker_id'], 'config_id': config['config_id'],
                           'model': config['config']['model'], 'status': read(status) if status.exists() else None})
    print(json.dumps(result, indent=2, allow_nan=False))


def wait(queue, job_id, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = queue.get(job_id)
        if result is None:
            raise ValueError('Unknown inference request')
        if result['state'] in ('complete', 'failed', 'interrupted'):
            if result['state'] != 'complete':
                print(json.dumps(result, indent=2), file=sys.stderr)
                raise SystemExit(1)
            return result
        time.sleep(1)
    raise TimeoutError('Client wait timed out; durable request was not cancelled or retried')


if __name__ == '__main__':
    main()
