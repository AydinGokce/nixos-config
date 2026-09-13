#!/usr/bin/env python3
"""Bounded, read-only Verda capacity evidence for the native Console.

The credential-loading wrapper supplies the same API credentials as dc. Only
the four GETs below are made through its API client (plus OAuth authentication).
This helper never opens the budget ledger or invokes a launch/ensure operation.
The calling bridge kills this isolated process after 20 seconds, including any
provider request threads. Provider response bodies/exceptions are never logged.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import runpy
import sys
import time

ENDPOINTS = {
    'catalog': '/instance-types?currency=usd',
    'locations': '/locations',
    'regular': '/instance-availability?is_spot=false',
    'spot': '/instance-availability?is_spot=true',
}
GB_TO_GIB = 10**9 / 1024**3
LIMIT = 1024


def require(value):
    if not value:
        raise ValueError('Invalid provider capacity evidence')


def label(value, limit=200):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and all(character.isprintable() for character in value))


def location(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Z0-9][A-Z0-9-]{0,31}', value)


def locations(value):
    require(isinstance(value, list) and 0 < len(value) <= LIMIT)
    result = set()
    for row in value:
        require(isinstance(row, dict) and location(row.get('code')))
        require(row['code'] not in result)
        result.add(row['code'])
    return result


def availability(value, names):
    """Retain valid regions, but never interpret absent/broken regions as empty."""
    require(isinstance(value, list) and len(value) <= LIMIT)
    result, invalid, broken = {}, set(), False
    for row in value:
        if not isinstance(row, dict) or not location(row.get('location_code')):
            broken = True
            continue
        region = row['location_code']
        kinds = row.get('availabilities')
        if (region in result or region in invalid or not isinstance(kinds, list)
                or len(kinds) > LIMIT or not all(isinstance(item, str) for item in kinds)
                or len(kinds) != len(set(kinds)) or not set(kinds) <= names):
            result.pop(region, None)
            invalid.add(region)
            broken = True
            continue
        result[region] = list(kinds)
    return result, broken


def snapshot(responses, selector, policy, *, current=None):
    current = time.time() if current is None else current
    result = {'schema': 1, 'state': 'error', 'observed_epoch': current,
              'msa_available': None, 'msa_message': 'MSA availability is unknown; provider evidence is incomplete',
              'gpus': []}
    errors = []
    catalog = responses.get('catalog')
    empty = [{'location_code': selector['LOCATION'], 'availabilities': []}]
    try:
        require(isinstance(catalog, list) and len(catalog) <= LIMIT)
        selector['evidence'](catalog, empty, empty)
        require(policy['MAX_PRICE'] == selector['MAX_HOURLY'])
    except Exception:
        result['error'] = 'Verda instance catalog is unavailable or invalid'
        return result
    names = {row['instance_type']: row for row in catalog}
    try:
        expected = locations(responses.get('locations'))
    except Exception:
        expected = set()
        errors.append('locations')
    offers = {}
    for contract in ('regular', 'spot'):
        try:
            offers[contract], broken = availability(responses.get(contract), set(names))
            if broken or not expected or set(offers[contract]) != expected:
                errors.append(contract)
        except Exception:
            offers[contract] = {}
            errors.append(contract)
    region = selector['LOCATION']
    if all(region in offers[contract] for contract in ('regular', 'spot')):
        try:
            chosen = selector['choose'](catalog,
                [{'location_code': region, 'availabilities': offers['regular'][region]}],
                [{'location_code': region, 'availabilities': offers['spot'][region]}])
            result['msa_available'] = True
            result['msa_message'] = ('Compatible private MSA capacity is available in ' + region
                + (' (CPU worker)' if chosen['cpu'] else ''))
        except selector['Unavailable']:
            result['msa_available'] = False
            result['msa_message'] = (f'No {region} worker meets the private MSA policy: '
                f'{selector["MIN_GIB"]} GiB RAM, supported image and ${selector["MAX_HOURLY"]}/hour ceiling')
        except Exception:
            errors.append('MSA selection')
    else:
        errors.append('MSA region')

    for contract, regions in offers.items():
        for region, kinds in regions.items():
            for kind in kinds:
                row = names[kind]
                count = (row.get('gpu') or {}).get('number_of_gpus')
                if type(count) is not int or count < 0 or count > 64:
                    errors.append('GPU metadata')
                    continue
                if count == 0:
                    continue
                try:
                    ram = selector['numeric'](row['memory']['size_in_gigabytes'], 'host RAM')
                    price = selector['numeric'](row['spot_price' if contract == 'spot' else 'price_per_hour'],
                                                'hourly price', positive=True)
                    gpu_memory = (row.get('gpu_memory') or {}).get('size_in_gigabytes')
                    gpu_memory = (None if gpu_memory is None else
                        selector['numeric'](gpu_memory, 'GPU memory') * GB_TO_GIB)
                    one = [{'location_code': selector['LOCATION'], 'availabilities': [kind]}]
                    selected = policy['choose']([row], one if contract == 'regular' else empty,
                                                one if contract == 'spot' else empty)
                    eligible = region == selector['LOCATION'] and selected is not None
                    if eligible:
                        reason = 'Eligible for private MSA'
                    elif region != selector['LOCATION']:
                        reason = 'Private MSA databases are in ' + selector['LOCATION']
                    elif ram * GB_TO_GIB < selector['MIN_GIB']:
                        reason = f'Requires at least {selector["MIN_GIB"]} GiB host RAM'
                    elif price > selector['MAX_HOURLY']:
                        reason = f'Exceeds ${selector["MAX_HOURLY"]}/hour MSA ceiling'
                    else:
                        reason = 'Worker family or required image is unsupported for private MSA'
                    result['gpus'].append({'instance_type': kind,
                        'name': row['name'] if label(row.get('name')) else kind,
                        'location': region, 'contract': contract, 'gpu_count': count,
                        'gpu_memory_gib': gpu_memory, 'ram_gib': ram * GB_TO_GIB,
                        'price_hourly': price, 'msa_eligible': eligible, 'reason': reason})
                except Exception:
                    errors.append('GPU metadata')
    result['gpus'].sort(key=lambda row: (row['location'], not row['msa_eligible'],
        row['price_hourly'], row['instance_type'], row['contract']))
    if not errors:
        result['state'] = 'ready'
    else:
        result['state'] = 'partial' if result['gpus'] or result['msa_available'] is not None else 'error'
        result['error'] = 'Incomplete Verda capacity check: ' + ', '.join(sorted(set(errors)))
    return result


def collect(api, selector, policy, *, current=None):
    """One shared API client deduplicates OAuth using its existing auth lock."""
    def request(endpoint):
        try:
            return api.request('GET', endpoint)
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as executor:
        futures = {key: executor.submit(request, endpoint) for key, endpoint in ENDPOINTS.items()}
        responses = {key: future.result() for key, future in futures.items()}
    return snapshot(responses, selector, policy, current=current)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools-root', type=Path, default=Path('/etc/bio-tools'))
    args = parser.parse_args(argv)
    try:
        require(bool(os.environ.get('DATACRUNCH_CLIENT_ID')) and bool(os.environ.get('DATACRUNCH_CLIENT_SECRET')))
        selector = runpy.run_path(str(args.tools_root / 'msa/worker.py'))
        policy = runpy.run_path(str(args.tools_root / 'msa/build-queue.py'))
        client = runpy.run_path(str(args.tools_root / 'dc-budget.py'))['API']()
        value = collect(client, selector, policy)
    except Exception:
        value = {'schema': 1, 'state': 'error', 'observed_epoch': time.time(),
                 'msa_available': None, 'msa_message': 'MSA availability is unknown; provider check failed',
                 'gpus': [], 'error': 'Verda capacity check is unavailable'}
    print(json.dumps(value, allow_nan=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
