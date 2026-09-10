#!/usr/bin/env python3
"""SSH JSON RPC and durable workbench dispatcher. No network listener."""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).absolute().parent.parent))
from workbench.api import API
from workbench.common import WIRE, Error, canonical, keys, parse, require
from workbench.service import Daemon, configuration
from workbench.store import Store


def rpc(store, actor, incoming, outgoing, *, worker_config=None):
    api = API(store, actor, worker_config=worker_config)
    while True:
        line = incoming.readline(WIRE + 1)
        if not line:
            return
        ident = None
        try:
            require(len(line) <= WIRE and line.endswith(b'\n'), 'RPC envelope exceeds limit or lacks newline', 'limit')
            request = parse(line)
            keys(request, ('id', 'method', 'params'))
            ident = request['id']
            require(type(ident) in (str, int) and len(str(ident)) <= 128, 'Invalid RPC request ID')
            result = api.call(request['method'], request['params'])
            response = {'id': ident, 'result': result}
            require(len(canonical(response)) + 1 <= WIRE, 'Response exceeds envelope limit; use pagination', 'limit')
        except Error as exc:
            response = {'id': ident, 'error': {'code': exc.code, 'message': str(exc)}}
        except (ValueError, TypeError, KeyError):
            response = {'id': ident, 'error': {'code': 'invalid', 'message': 'Invalid request field type or value'}}
        except Exception:
            response = {'id': ident, 'error': {'code': 'internal', 'message': 'Internal head service error; operation state was retained'}}
        outgoing.write(canonical(response) + b'\n'); outgoing.flush()
        if len(line) > WIRE or not line.endswith(b'\n'):
            return


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default=os.environ.get('BIO_WORKBENCH_STATE', '/var/lib/bio-workbench'))
    parser.add_argument('--config', default=os.environ.get('BIO_WORKBENCH_CONFIG'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('rpc'); sub.add_parser('daemon'); sub.add_parser('tick')
    execute = sub.add_parser('execute')
    execute.add_argument('--kind', choices=['validation', 'job'], required=True)
    execute.add_argument('--id', required=True); execute.add_argument('--intent-sha256', required=True)
    retained = sub.add_parser('import-retained', help='Operator-only import; never exposed through RPC')
    retained.add_argument('--source', required=True); retained.add_argument('--model', required=True)
    retained.add_argument('--actor', required=True); retained.add_argument('--name', required=True)
    args = parser.parse_args(argv)
    store = Store(args.state)
    if args.command == 'rpc':
        require(bool(os.environ.get('BIO_WORKBENCH_ACTOR')), 'Trusted BIO_WORKBENCH_ACTOR is required')
        rpc(store, os.environ['BIO_WORKBENCH_ACTOR'], sys.stdin.buffer, sys.stdout.buffer,
            worker_config=configuration(args.config) if args.config else None)
        return
    if args.command == 'import-retained':
        require(os.geteuid() == 0, 'Retained result import is an operator-only command')
        from workbench.import_retained import import_retained
        print(canonical(import_retained(store, args.source, args.model, args.actor, args.name)).decode(), flush=True)
        return
    config = configuration(args.config)
    if args.command == 'execute':
        from workbench.runner import execute
        execute(store, args.kind, args.id, args.intent_sha256, config)
        return
    lock = os.open(store.root / 'daemon.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        daemon = Daemon(store, config)
        while True:
            result = daemon.tick()
            if result['errors'] or args.command == 'tick':
                print(canonical(result).decode(), flush=True)
            if args.command == 'tick':
                break
            time.sleep(2)
    finally:
        os.close(lock)


if __name__ == '__main__':
    try:
        main()
    except (Error, OSError) as exc:
        print('bio-workbench: ' + str(exc), file=sys.stderr)
        sys.exit(2)
