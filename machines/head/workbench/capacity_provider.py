#!/usr/bin/env python3
"""Bounded, read-only MSA and separate Verda GPU capacity evidence.

For Verda, the credential-loading wrapper supplies the same API credentials as
dc. Only the four GETs below are made through its client (plus OAuth
authentication). The AWS branch is local-only and reports unknown availability.
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
AWS_OFFLINE_MESSAGE = ('AWS availability is unknown; automatic checks are disabled. '
                       'A controlled manual check is required; the Refresh button reads cached data only.')


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


def snapshot(responses, selector, *, current=None, profile=None):
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
    except Exception:
        result['error'] = 'Verda instance catalog is unavailable or invalid'
        return result
    try:
        profile = selector['search_profile'](selector['DEFAULT_PROFILE'] if profile is None else profile)
    except Exception:
        result['error'] = 'Configured private MSA search profile is invalid'
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
                [{'location_code': region, 'availabilities': offers['spot'][region]}], profile=profile)
            result['msa_available'] = True
            result['msa_message'] = ('Compatible private MSA capacity is available in ' + region
                + (' (CPU worker)' if chosen['cpu'] else ''))
        except selector['Unavailable']:
            result['msa_available'] = False
            result['msa_message'] = (f'No {region} worker meets the private MSA policy: '
                f'{selector["memory_requirement"](profile)}, supported image and '
                f'${selector["MAX_HOURLY"]}/hour ceiling')
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
                    offer = selector['assess_offer'](row, spot=contract == 'spot',
                                                     location=region, profile=profile)
                    gpu_memory = (row.get('gpu_memory') or {}).get('size_in_gigabytes')
                    gpu_memory = (None if gpu_memory is None else
                        selector['numeric'](gpu_memory, 'GPU memory') * GB_TO_GIB)
                    result['gpus'].append({'instance_type': kind,
                        'name': row['name'] if label(row.get('name')) else kind,
                        'location': region, 'contract': contract, 'gpu_count': count,
                        'gpu_memory_gib': gpu_memory, 'ram_gib': offer['conservative_gib'],
                        'price_hourly': offer['price_per_hour'], 'msa_eligible': offer['eligible'],
                        'reason': offer['reason']})
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


def collect(api, selector, *, current=None, profile=None):
    """One shared API client deduplicates OAuth using its existing auth lock."""
    def request(endpoint):
        try:
            return api.request('GET', endpoint)
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as executor:
        futures = {key: executor.submit(request, endpoint) for key, endpoint in ENDPOINTS.items()}
        responses = {key: future.result() for key, future in futures.items()}
    return snapshot(responses, selector, current=current, profile=profile)


def aws_snapshot(current=None):
    """UI capacity observation never authorizes an AWS account request."""
    current = time.time() if current is None else current
    return dict(schema=1, provider='aws', msa_provider='aws', compute_kind='cpu',
                region='us-east-1', state='error', observed_epoch=current,
                msa_available=None, msa_message=AWS_OFFLINE_MESSAGE, gpus=[], cpus=[],
                error=AWS_OFFLINE_MESSAGE)


def verda_snapshot(tools):
    require(bool(os.environ.get('DATACRUNCH_CLIENT_ID')) and bool(os.environ.get('DATACRUNCH_CLIENT_SECRET')))
    selector = runpy.run_path(str(tools / 'msa/worker.py'))
    client = runpy.run_path(str(tools / 'dc-budget.py'))['API']()
    return collect(client, selector, profile=os.environ.get('BIO_MSA_SEARCH_PROFILE'))


def combined(aws, verda):
    """Verda prediction inventory cannot override the selected AWS MSA status."""
    if aws is None:
        aws = {'schema': 1, 'state': 'error', 'observed_epoch': time.time(),
               'msa_available': None, 'msa_message': 'AWS CPU MSA availability is unknown; provider check failed',
               'msa_provider': 'aws', 'compute_kind': 'cpu', 'region': 'us-east-1', 'cpus': [],
               'error': 'AWS CPU capacity check is unavailable'}
    result = dict(aws)
    result['gpus'] = [] if verda is None else verda.get('gpus', [])
    if verda is None or verda.get('state') != 'ready':
        result['gpu_error'] = 'Verda GPU inventory is unavailable or incomplete; AWS MSA status is independent'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools-root', type=Path, default=Path('/etc/bio-tools'))
    args = parser.parse_args(argv)
    def observed(function, *arguments):
        try:
            return function(*arguments)
        except Exception:
            return None
    selected = os.environ.get('BIO_MSA_PROVIDER', 'verda')
    if selected == 'aws':
        # Even an uncached/direct helper invocation is offline. Ordinary UI
        # refresh requests are not authorization for cloud account queries.
        value = aws_snapshot()
    elif selected == 'verda':
        value = observed(verda_snapshot, args.tools_root)
    else:
        value = None
    if value is None:
        value = {'schema': 1, 'state': 'error', 'observed_epoch': time.time(),
                 'msa_available': None, 'msa_message': 'MSA availability is unknown; provider check failed',
                 'gpus': [], 'error': 'Verda capacity check is unavailable'}
    print(json.dumps(value, allow_nan=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
