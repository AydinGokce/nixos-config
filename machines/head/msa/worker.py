#!/usr/bin/env python3
"""Read-only private-MSA worker selection under the existing build-queue policy.

The caller exports the same private credentials used by dc, then runs:
  worker.py select --tools-root /etc/bio-tools

Stdout is one JSON choice (type/spot/image/price), never a reservation. Exit 4
means no qualifying capacity; exit 2 means malformed/unavailable evidence. The
caller must still use dc's fresh launch quote, $13/hour ceiling, lifetime budget,
and normal owned-resource cleanup. An explicit --worker bypasses this selector
at the caller; this helper never changes an explicit worker preference.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import runpy
import sys

HERE = Path(__file__).resolve().parent
LOCATION = 'FIN-02'
MIN_GIB = 768
MAX_HOURLY = 13
ENDPOINTS = ('/instance-types?currency=usd',
             '/instance-availability?location_code=FIN-02&is_spot=false',
             '/instance-availability?location_code=FIN-02&is_spot=true')


class Error(RuntimeError):
    pass


class Unavailable(Error):
    pass


def require(condition, message):
    if not condition:
        raise Error(message)


def numeric(value, context, *, positive=False):
    require(type(value) in (int, float, str), 'Invalid '+context)
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise Error('Invalid '+context) from None
    require(math.isfinite(number) and (number > 0 if positive else number >= 0), 'Invalid '+context)
    return number


def evidence(catalog, regular, spot):
    """Reject incomplete/ambiguous shapes before applying the fixed policy."""
    require(isinstance(catalog, list) and bool(catalog), 'Empty or invalid instance catalog')
    names = set()
    for row in catalog:
        require(isinstance(row, dict), 'Invalid catalog row')
        kind = row.get('instance_type')
        require(isinstance(kind, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', kind),
                'Invalid catalog instance type')
        require(kind not in names, 'Duplicate catalog instance type')
        names.add(kind)
        require(isinstance(row.get('memory'), dict), 'Missing catalog memory')
        numeric(row['memory'].get('size_in_gigabytes'), 'catalog memory')
        operating_systems = row.get('supported_os')
        require(isinstance(operating_systems, list)
                and all(isinstance(item, str) and bool(item) for item in operating_systems),
                'Invalid supported image catalog')
        # Currency/prices of all available catalog rows must be trustworthy,
        # including rows later excluded for RAM or CPU architecture.
        require(row.get('currency') == 'usd', 'USD worker pricing required')
        numeric(row.get('price_per_hour'), 'regular worker price', positive=True)
        numeric(row.get('spot_price'), 'spot worker price', positive=True)
    for value in (regular, spot):
        require(isinstance(value, list) and all(isinstance(row, dict) for row in value),
                'Invalid availability response')
        target = [row for row in value if row.get('location_code') == LOCATION]
        require(len(target) == 1, 'Missing or duplicate FIN-02 availability')
        kinds = target[0].get('availabilities')
        require(isinstance(kinds, list) and all(isinstance(kind, str) for kind in kinds),
                'Invalid FIN-02 availability types')
        require(len(kinds) == len(set(kinds)), 'Duplicate available instance type')
        require(set(kinds) <= names, 'Availability references an absent catalog instance type')


def choose(catalog, regular, spot, *, observed=None, spot_only=False):
    evidence(catalog, regular, spot)
    # Share the reviewed policy, rather than growing a second list of hardware
    # families, RAM conversions, image rules, tie breaks or price limits.
    policy_path = HERE/'build-queue.py'
    policy = runpy.run_path(str(policy_path))
    require(policy['MAX_PRICE'] == MAX_HOURLY, 'Build-queue price policy changed; review selector bounds')
    try:
        selected = policy['choose'](catalog, [{'location_code': LOCATION, 'availabilities': []}] if spot_only else regular, spot)
    except (policy['Error'], KeyError, TypeError, ValueError, OverflowError):
        raise Error('Malformed catalog or availability for the reviewed MSA worker policy') from None
    if selected is None:
        raise Unavailable('No available FIN-02 x86 worker satisfies 768 GiB RAM, supported image and $13/hour ceiling')
    require(selected['price_per_hour'] <= MAX_HOURLY and selected['conservative_gib'] >= MIN_GIB,
            'Build-queue worker bounds changed; refusing an unreviewed selection')
    return dict(selected, schema=1, kind='msa-worker-choice', location=LOCATION,
                observed_at=observed or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                maximum_instance_hourly=MAX_HOURLY, minimum_available_gib=MIN_GIB,
                spot_only=spot_only,
                selection_policy_sha256=hashlib.sha256(policy_path.read_bytes()).hexdigest(),
                reserved=False)


def preview(api, *, observed=None, spot_only=False):
    try:
        rows = [api.request('GET', endpoint) for endpoint in ENDPOINTS]
    except Exception:
        # API implementations may include provider response text. Never echo
        # an exception containing a credential or token; the normal dc helper
        # already supplies detailed redacted diagnostics in its own commands.
        raise Error('Read-only provider capacity request failed; no worker was selected') from None
    return choose(*rows, observed=observed, spot_only=spot_only)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['select'])
    parser.add_argument('--tools-root', type=Path,
                        default=Path(os.environ.get('BIO_TOOLS_SRC', '/etc/bio-tools')))
    parser.add_argument('--spot-only', action='store_true', help='Honor an explicit spot-only request; never choose regular capacity')
    args = parser.parse_args(argv)
    try:
        require(bool(os.environ.get('DATACRUNCH_CLIENT_ID')) and bool(os.environ.get('DATACRUNCH_CLIENT_SECRET')),
                'Provider credentials must be supplied by the existing private dc wrapper')
        budget = runpy.run_path(str(args.tools_root/'dc-budget.py'))
        selected = preview(budget['API'](), spot_only=args.spot_only)
        print(json.dumps(selected, sort_keys=True, allow_nan=False))
        return 0
    except Unavailable as error:
        print('msa-worker: '+str(error), file=sys.stderr)
        return 4
    except Exception:
        # Fixed diagnostic avoids accidentally displaying API/credential data
        # from a constructor/import failure before preview's exception fence.
        print('msa-worker: invalid or unavailable capacity evidence; no worker was selected', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
