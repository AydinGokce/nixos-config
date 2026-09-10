#!/usr/bin/env python3
"""Durable evidence separating MSA capacity waiting from cloud submission.

An allocation marker is fsynced before the first cloud launch. Once published,
neither a failed command nor a missing response can prove that nothing was rented.
"""
import argparse
import os
from pathlib import Path
import re
import sys
import time

import lifecycle
import session


def _context(state, intent):
    state = lifecycle.registry_root(state)
    session.require(state.name == intent['session_id']
                    and re.fullmatch(r'[a-f0-9]{32}', intent['session_id'])
                    and intent.get('kind') == 'managed-private-msa-session'
                    and intent.get('lifecycle') != 'borrowed-api', 'Wrong managed startup intent')
    session.require(lifecycle.document(state / 'intent.json') == intent, 'Startup intent changed')
    pointer = lifecycle.document(state.parent / 'active.json')
    session.require(pointer == {'session_id': intent['session_id'],
                                'intent_sha256': session.sha(state / 'intent.json')},
                    'Startup is not the active registered session')
    tools = Path(intent['tools'])
    session.require(tools == state / 'tools' and session.sources(tools) == intent['sources']
                    and intent['sources'].get('msa/startup.py') == session.sha(tools / 'msa/startup.py'),
                    'Startup source snapshot changed or lacks the allocation protocol')
    session.require(session.sha(Path(intent['argv'][0])) == intent['submit_sha256'],
                    'Pinned startup launcher changed')
    return state


def _binding(state, intent):
    observed = lifecycle.document(state / 'observed-start.json')
    session.require(observed.get('session_id') == intent['session_id']
                    and observed.get('intent_sha256') == session.sha(state / 'intent.json')
                    and observed.get('unit') == intent['unit']
                    and re.fullmatch(r'[a-f0-9]{32}', observed.get('invocation_id', '')),
                    'Startup invocation binding changed')
    return observed


def _running_context(state):
    # Lazy import avoids a cycle when session_client reads these receipts.
    import session_client as client
    intent = lifecycle.document(Path(state) / 'intent.json')
    state = _context(state, intent)
    session.require(Path(__file__).resolve() == Path(intent['tools']) / 'msa/startup.py'
                    and os.environ.get('BIO_MSA_SESSION_ID') == intent['session_id'],
                    'Startup helper must run from its pinned managed session')
    live = client.unit_state(intent['unit'])
    session.require(live.get('LoadState') == 'loaded'
                    and live.get('InvocationID') == os.environ.get('INVOCATION_ID')
                    and live.get('ActiveState') in {'active', 'activating', 'deactivating'},
                    'Startup helper is not running under its exact managed unit')
    client._starting_binding(state, intent, live)
    return state, intent, live, _binding(state, intent)


def _attempt(state, intent):
    attempt = lifecycle.document(state / 'attempt.json')
    binding = _binding(state, intent)
    session.require(attempt.get('schema') == 1 and attempt.get('kind') == 'msa-startup-attempt'
                    and all(attempt.get(key) == value for key, value in binding.items())
                    and attempt.get('submit_sha256') == intent['submit_sha256']
                    and attempt.get('startup_sha256') == intent['sources']['msa/startup.py'],
                    'Startup attempt binding changed')
    run = lifecycle.registry_root(attempt['run_dir'])
    session.require(run.is_dir() and re.fullmatch(r'msa-[0-9]{8}-[0-9]{6}-[0-9]+', run.name),
                    'Startup run directory changed')
    return attempt, run


def _require_no_allocation(state, run):
    session.require(not any(os.path.lexists(path) for path in
                            (state / 'allocation-started.json', state / 'launch.json', run / 'job.json')),
                    'Allocation may have started; absence cannot be certified')


def begin(state, run_dir):
    with lifecycle.registration_lock(state, time.monotonic() + 30):
        state, intent, live, binding = _running_context(state)
        session.require(live['ActiveState'] in {'active', 'activating'}, 'Startup is already stopping')
        run = lifecycle.registry_root(run_dir)
        session.require(run.is_dir() and re.fullmatch(r'msa-[0-9]{8}-[0-9]{6}-[0-9]+', run.name),
                        'Invalid startup run directory')
        _require_no_allocation(state, run)
        session.require(not os.path.lexists(state / 'no-allocation.json'), 'Startup is already finished')
        attempt = dict(schema=1, kind='msa-startup-attempt', **binding, run_dir=str(run),
                       submit_sha256=intent['submit_sha256'],
                       startup_sha256=intent['sources']['msa/startup.py'], created_epoch=time.time())
        session.atomic(state / 'attempt.json', attempt, exclusive=True)
        return attempt


def mark_allocation(state):
    with lifecycle.registration_lock(state, time.monotonic() + 30):
        state, intent, live, binding = _running_context(state)
        session.require(live['ActiveState'] in {'active', 'activating'}, 'Startup is already stopping')
        _attempt(state, intent)
        session.require(not os.path.lexists(state / 'no-allocation.json'), 'Startup is already finished')
        marker = dict(schema=1, kind='msa-allocation-started', **binding,
                      attempt_sha256=session.sha(state / 'attempt.json'))
        path = state / 'allocation-started.json'
        try:
            session.atomic(path, marker, exclusive=True)
        except FileExistsError:
            session.require(lifecycle.document(path) == marker, 'Allocation marker changed')
        return marker


def finish(state, exit_status):
    session.require(type(exit_status) is int and 0 <= exit_status <= 255, 'Invalid startup exit status')
    with lifecycle.registration_lock(state, time.monotonic() + 30):
        state, intent, _, binding = _running_context(state)
        attempt, run = _attempt(state, intent)
        # Even an unsuccessful dc launch may have submitted a cloud request.
        # Retain the uncertainty fence until exact resource cleanup is proven.
        if any(os.path.lexists(path) for path in
               (state / 'allocation-started.json', state / 'launch.json', run / 'job.json')):
            return None
        session.require(exit_status != 0, 'Successful startup cannot certify a pre-allocation failure')
        reason = ('capacity_timeout' if exit_status == 4 else
                  'cancelled' if exit_status in {129, 130, 143} else 'preallocation_failed')
        receipt = dict(schema=1, kind='msa-no-allocation', **binding,
                       attempt_sha256=session.sha(state / 'attempt.json'),
                       run_dir=attempt['run_dir'], exit_status=exit_status, reason=reason,
                       no_allocation_attempted=True)
        path = state / 'no-allocation.json'
        try:
            session.atomic(path, receipt, exclusive=True)
        except FileExistsError:
            session.require(lifecycle.document(path) == receipt, 'Startup outcome changed')
        return receipt


def validate_no_allocation(state, intent, live):
    """Return bound terminal proof, or reject uncertainty without modifying state."""
    import session_client as client
    state = _context(state, intent)
    session.require(client._terminal_unit(live), 'Startup unit is still live')
    attempt, run = _attempt(state, intent)
    binding = _binding(state, intent)
    session.require((live.get('LoadState') == 'not-found' and not live.get('InvocationID'))
                    or live.get('InvocationID') == binding['invocation_id'],
                    'Stopped startup invocation changed')
    _require_no_allocation(state, run)
    receipt = lifecycle.document(state / 'no-allocation.json')
    status = receipt.get('exit_status')
    reason = ('capacity_timeout' if status == 4 else
              'cancelled' if status in {129, 130, 143} else 'preallocation_failed')
    session.require(receipt.get('schema') == 1 and receipt.get('kind') == 'msa-no-allocation'
                    and all(receipt.get(key) == value for key, value in binding.items())
                    and receipt.get('attempt_sha256') == session.sha(state / 'attempt.json')
                    and receipt.get('run_dir') == attempt['run_dir']
                    and type(status) is int and 1 <= status <= 255
                    and receipt.get('reason') == reason and receipt.get('no_allocation_attempted') is True,
                    'Terminal startup outcome binding changed')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('begin', 'mark-allocation', 'finish'))
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--exit-status', type=int)
    args = parser.parse_args()
    if args.action == 'begin':
        if args.run_dir is None:
            parser.error('--run-dir is required for begin')
        begin(args.state, args.run_dir)
    elif args.action == 'mark-allocation':
        mark_allocation(args.state)
    else:
        if args.exit_status is None:
            parser.error('--exit-status is required for finish')
        finish(args.state, args.exit_status)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print('msa-startup: ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
