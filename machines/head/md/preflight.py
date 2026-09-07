"""Run native topology preparation/checks on the head before any paid launch."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .worker import execute, save


def preflight_plan(plan, work):
    stages = {s['id']: s for s in plan['stages']}

    def ancestry(ident, visiting=None):
        visiting = set() if visiting is None else visiting
        if ident in visiting:
            raise ValueError('Cyclic stage dependency')
        visiting.add(ident)
        found = {ident}
        for parent in stages[ident].get('dependencies', []):
            found |= ancestry(parent, visiting.copy())
        return found

    chosen, systems = set(), set()
    for stage in plan['stages']:
        argv = stage['argv']
        if argv[:2] != ['gmx', 'grompp']:
            continue
        parents = ancestry(stage['id'])
        if any(stages[p]['argv'][:2] == ['gmx', 'mdrun'] for p in parents):
            continue  # A later state depends on dynamics: never run it on head.
        def argument(flag):
            value = argv[argv.index(flag) + 1]
            return str((work / stage['cwd'] / value).resolve())
        checks = [check for check in plan['stages'] if
                  check['argv'][:4] == ['python', '-m', 'md.protocols', 'check-topology'] and
                  stage['id'] in check.get('dependencies', [])]
        manifests = []
        for check in checks:
            args = check['argv']
            for i, flag in enumerate(args[:-1]):
                if flag == '--manifest':
                    manifests.append(str((work / check['cwd'] / args[i + 1]).resolve()))
                elif flag in {'--force-field', '--water-model', '--neutral-transformation'}:
                    manifests.append(flag + ':' + args[i + 1])
        pair = argument('-c'), argument('-p'), tuple(manifests)
        if pair in systems:
            continue
        systems.add(pair)
        chosen |= parents
        for check in checks:
            chosen |= ancestry(check['id'])
    if not chosen:
        raise ValueError('Protocol has no native topology preflight before dynamics')
    for stage in plan['stages']:
        if stage.get('head_preflight') is True:
            chosen |= ancestry(stage['id'])
    result = {**plan, 'schema': 'bio-md-native-preflight.v1',
              'stages': [s for s in plan['stages'] if s['id'] in chosen],
              'claims': ['GROMACS parsed initial coordinates and topology; no molecular dynamics was run.']}
    if any(s['argv'][:2] == ['gmx', 'mdrun'] for s in result['stages']):
        raise ValueError('Head preflight cannot execute molecular dynamics')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    args = parser.parse_args()
    selected = preflight_plan(json.loads(args.plan.read_text()), args.work)
    status = execute(selected, args.work, args.runtime)
    save(args.work / 'native-preflight.json', {'schema': 'bio-md-native-preflight-receipt.v1',
             'state': 'complete' if status == 0 else 'failed', 'stages': [s['id'] for s in selected['stages']],
             'molecular_dynamics_executed': False, 'paid_compute_requested': False})
    return status


if __name__ == '__main__':
    raise SystemExit(main())
