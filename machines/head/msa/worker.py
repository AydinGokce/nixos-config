#!/usr/bin/env python3
"""Read-only private-MSA worker selection under the existing build-queue policy.

The caller exports the same private credentials used by dc, then runs:
  worker.py select --tools-root /etc/bio-tools

Use --wait-seconds to retry confirmed capacity shortages for a bounded period;
the default is a single check. Only valid empty capacity results are retried,
including regular and spot capacity on every attempt. A deadline also bounds
in-flight provider requests. Progress reports the retry window, never an ETA
for hardware availability. The wait happens before any paid worker is rented.

Stdout is one JSON choice (type/spot/image/price), never a reservation. Exit 4
means no qualifying capacity before the deadline; exit 2 means invalid evidence. The
caller must still use dc's fresh launch quote, $13/hour ceiling, lifetime budget,
and normal owned-resource cleanup. An explicit --worker bypasses this selector
at the caller; this helper never changes an explicit worker preference.
"""
import argparse
import contextlib
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import runpy
import signal
import sys
import time

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


class CapacityDeadline(BaseException):
    """Bypass API exception handlers when the selection deadline interrupts I/O."""


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


def durations(wait_seconds, poll_seconds):
    wait_seconds = numeric(wait_seconds, 'capacity wait duration')
    poll_seconds = numeric(poll_seconds, 'capacity poll interval', positive=True)
    require(wait_seconds <= 7200, 'Capacity wait duration must be between 0 and 7200 seconds')
    require(1 <= poll_seconds <= 300, 'Capacity poll interval must be between 1 and 300 seconds')
    return wait_seconds, poll_seconds


@contextlib.contextmanager
def deadline_alarm(seconds):
    """Linux head helper: cancel even a provider response stuck in a read.

    The dc API has per-request timeouts, but its sequential token/catalog/
    availability reads otherwise could outlive the selection deadline. The
    signal uses BaseException so transport error fences cannot swallow it.
    This standalone CLI must not borrow an alarm belonging to another caller.
    """
    require(not any(signal.getitimer(signal.ITIMER_REAL)), 'Capacity selector already has an active deadline')
    previous = signal.getsignal(signal.SIGALRM)

    def expired(signum, frame):
        raise CapacityDeadline()

    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def wait_for_capacity(api, *, wait_seconds=0, poll_seconds=30, spot_only=False,
                      clock=time.monotonic, sleep=time.sleep, unavailable=None):
    """Retry trustworthy shortages only; expiry/cancellation cannot select a worker."""
    wait_seconds, poll_seconds = durations(wait_seconds, poll_seconds)
    if not wait_seconds:
        return preview(api, spot_only=spot_only)
    deadline = clock() + wait_seconds
    timed_out = Unavailable(f'The {wait_seconds:g}-second capacity wait window expired; '
                            'no worker was selected')
    try:
        with deadline_alarm(wait_seconds):
            while clock() < deadline:
                try:
                    selected = preview(api, spot_only=spot_only)
                except Unavailable:
                    remaining = deadline - clock()
                    if remaining <= 0:
                        break
                    if unavailable:
                        unavailable()
                    sleep(min(poll_seconds, remaining))
                    continue
                # An in-flight result completing after expiry cannot authorize
                # a subsequent allocation even if it reports available hardware.
                if clock() >= deadline:
                    break
                return selected
    except CapacityDeadline:
        pass
    raise timed_out


def capacity_activity(tools_root, wait_seconds):
    """Use the normal bounded progress sink and a fresh ten-second heartbeat."""
    progress = runpy.run_path(str(tools_root/'py/worker_progress.py'))

    class CapacityActivity(progress['Activity']):
        def __init__(self):
            self.deadline = time.monotonic() + wait_seconds
            self.waiting = False
            super().__init__('waiting_capacity', interval=10, scope='msa',
                eta=dict(state='unknown', scope='stage', basis='Worker availability has no reliable estimate'))

        def unavailable(self):
            with self.lock:
                self.waiting = True
                self._emit()

        def _emit(self, state='running'):
            if state == 'running':
                remaining = max(0, math.ceil(self.deadline - time.monotonic()))
                action = 'Waiting for' if self.waiting else 'Checking'
                self.fields['message'] = (f'{action} cloud capacity; {remaining // 60}m {remaining % 60:02d}s '
                                          'left in the retry window. No worker has been rented.')
            elif state == 'complete':
                self.fields['message'] = 'Compatible cloud capacity found; worker allocation is next.'
            else:
                self.fields['message'] = 'Capacity selection ended; no worker was selected.'
            return super()._emit(state)

    return CapacityActivity()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['select'])
    parser.add_argument('--tools-root', type=Path,
                        default=Path(os.environ.get('BIO_TOOLS_SRC', '/etc/bio-tools')))
    parser.add_argument('--spot-only', action='store_true', help='Honor an explicit spot-only request; never choose regular capacity')
    parser.add_argument('--wait-seconds', default=0,
                        help='Retry confirmed capacity shortages for up to this many seconds (0..7200; default: one check)')
    parser.add_argument('--poll-seconds', default=30,
                        help='Delay between confirmed capacity shortages (1..300 seconds; default: 30)')
    args = parser.parse_args(argv)
    try:
        wait_seconds, poll_seconds = durations(args.wait_seconds, args.poll_seconds)
        require(bool(os.environ.get('DATACRUNCH_CLIENT_ID')) and bool(os.environ.get('DATACRUNCH_CLIENT_SECRET')),
                'Provider credentials must be supplied by the existing private dc wrapper')
        budget = runpy.run_path(str(args.tools_root/'dc-budget.py'))
        api = budget['API']()
        with capacity_activity(args.tools_root, wait_seconds) if wait_seconds else contextlib.nullcontext() as activity:
            selected = wait_for_capacity(api, wait_seconds=wait_seconds, poll_seconds=poll_seconds,
                spot_only=args.spot_only, unavailable=activity.unavailable if activity else None)
        print(json.dumps(selected, sort_keys=True, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        print('msa-worker: capacity selection cancelled; no worker was selected', file=sys.stderr)
        return 130
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
