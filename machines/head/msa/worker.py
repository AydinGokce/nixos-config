#!/usr/bin/env python3
"""Read-only private-MSA worker selection with an explicit search profile.

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
PROFILES = runpy.run_path(str(HERE/'search_profile.py'))
DEFAULT_PROFILE = PROFILES['DEFAULT_PROFILE']
MAPPED_PROFILE = PROFILES['MAPPED_PROFILE']
MAPPED_PROFILES = PROFILES['MAPPED_PROFILES']
LEGACY_PROFILE = PROFILES['LEGACY_PROFILE']
LOCATION = 'FIN-02'
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


def search_profile(value=DEFAULT_PROFILE):
    try:
        return PROFILES['resolve'](value)
    except (TypeError, ValueError):
        raise Error('Invalid private MSA search profile') from None


def memory_requirement(profile=DEFAULT_PROFILE):
    value = search_profile(profile)['minimum_advertised_bytes']
    if value % 1024**3 == 0:
        return f'{value // 1024**3} GiB advertised host RAM'
    return f'{value / 10**9:g} GB advertised host RAM ({value / 1024**3:.2f} GiB)'


def assess_offer(row, *, spot=False, location=LOCATION, profile=DEFAULT_PROFILE):
    """The same complete offer policy serves launch selection and the Console.

    Provider GB is decimal. Guest memory is checked separately by the recipe;
    admission does not claim that full indexes fit or are resident in RAM.
    Build/install selection intentionally retains its separate 768 GiB policy.
    """
    empty = [{'location_code': LOCATION, 'availabilities': []}]
    evidence([row], empty, empty)
    require(type(spot) is bool and isinstance(location, str), 'Invalid offer contract or location')
    profile = search_profile(profile)
    kind = row['instance_type']
    memory = numeric(row['memory']['size_in_gigabytes'], 'catalog memory')
    price = numeric(row['spot_price' if spot else 'price_per_hour'], 'worker price', positive=True)
    # Keep the resident build policy exactly as before. CPU-only mapped search
    # can also use the reviewed PCIe GPU VM families; CUDA wheel compatibility
    # is irrelevant. The actual guest must still pass tools.sh's x86_64/AVX2
    # checks. Anchored mapped names exclude Grace/GB, CC and unknown suffixes.
    if profile['profile_id'] == LEGACY_PROFILE:
        supported = bool(re.fullmatch(r'CPU\.[0-9]+V\.[0-9]+G|[1248](?:A100|H100|H200|B200|B300)\.[A-Za-z0-9.]+', kind))
    else:
        supported = bool(re.fullmatch(r'CPU\.[0-9]+V\.[0-9]+G|'
            r'[1248](?:A100|H100|H200|B200|B300)(?:\.[0-9]+S)?\.[0-9]+V|'
            r'[1248](?:L40S|A6000|RTX6000ADA|RTXPRO6000|V100)\.[0-9]+V', kind))
    cpu = kind.startswith('CPU.')
    image = 'ubuntu-24.04' if cpu else 'ubuntu-24.04-cuda-12.8-open-docker'
    if not cpu and profile['profile_id'] in MAPPED_PROFILES and 'ubuntu-24.04' in row['supported_os']:
        # Both images are already allowed by the launcher. Native CPU MSA
        # needs no driver/toolkit initialization; older V100/A6000 instances
        # also omit the CUDA-12.8-open image from their supported catalog.
        image = 'ubuntu-24.04'
    reason = None
    if location != LOCATION:
        reason = 'Private MSA databases are in ' + LOCATION
    elif memory * 10**9 < profile['minimum_advertised_bytes']:
        reason = 'Requires at least ' + memory_requirement(profile)
    elif not supported or kind.endswith('.CC'):
        reason = 'Worker family is unsupported for private MSA'
    elif image not in row['supported_os']:
        reason = 'Required worker image is unsupported for private MSA'
    elif price > MAX_HOURLY:
        reason = f'Exceeds ${MAX_HOURLY}/hour MSA ceiling'
    return dict(instance_type=kind, spot=spot, price_per_hour=price,
                advertised_gb=memory, conservative_gib=memory * 10**9 / 1024**3,
                image=image, supported_os=list(row['supported_os']), cpu=cpu,
                eligible=reason is None, reason=reason or 'Eligible for private MSA')


def choose(catalog, regular, spot, *, observed=None, spot_only=False, profile=DEFAULT_PROFILE):
    profile = search_profile(profile)
    require(type(spot_only) is bool, 'Invalid spot-only selection')
    evidence(catalog, regular, spot)
    available = {False: set(), True: set()}
    for is_spot, rows in ((False, regular), (True, spot)):
        if not (spot_only and not is_spot):
            available[is_spot] = set(next(row['availabilities'] for row in rows
                                          if row.get('location_code') == LOCATION))
    candidates = []
    for row in catalog:
        for is_spot, names in available.items():
            if row['instance_type'] in names:
                offer = assess_offer(row, spot=is_spot, profile=profile)
                if offer['eligible']:
                    candidates.append({key: value for key, value in offer.items()
                                       if key not in ('eligible', 'reason')})
    selected = min(candidates, key=lambda c: (c['price_per_hour'], not c['cpu'],
                                            c['spot'], c['instance_type']), default=None)
    if selected is None:
        raise Unavailable(f'No available {LOCATION} x86 worker satisfies {memory_requirement(profile)}, '
                          f'supported image and ${MAX_HOURLY}/hour ceiling')
    return dict(selected, schema=1, kind='msa-worker-choice', location=LOCATION,
                observed_at=observed or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                maximum_instance_hourly=MAX_HOURLY,
                minimum_advertised_bytes=profile['minimum_advertised_bytes'],
                minimum_total_gib=profile['minimum_total_gib'],
                minimum_available_gib=profile['minimum_available_gib'],
                profile_id=profile['profile_id'], profile_sha256=profile['profile_sha256'],
                search_profile=profile,
                spot_only=spot_only,
                selection_policy_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                reserved=False)


def preview(api, *, observed=None, spot_only=False, profile=DEFAULT_PROFILE):
    profile = search_profile(profile)
    try:
        rows = [api.request('GET', endpoint) for endpoint in ENDPOINTS]
    except Exception:
        # API implementations may include provider response text. Never echo
        # an exception containing a credential or token; the normal dc helper
        # already supplies detailed redacted diagnostics in its own commands.
        raise Error('Read-only provider capacity request failed; no worker was selected') from None
    return choose(*rows, observed=observed, spot_only=spot_only, profile=profile)


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
                      profile=DEFAULT_PROFILE, clock=time.monotonic, sleep=time.sleep, unavailable=None):
    """Retry trustworthy shortages only; expiry/cancellation cannot select a worker."""
    wait_seconds, poll_seconds = durations(wait_seconds, poll_seconds)
    profile = search_profile(profile)
    if not wait_seconds:
        return preview(api, spot_only=spot_only, profile=profile)
    deadline = clock() + wait_seconds
    timed_out = Unavailable(f'The {wait_seconds:g}-second capacity wait window expired; '
                            'no worker was selected')
    try:
        with deadline_alarm(wait_seconds):
            while clock() < deadline:
                try:
                    selected = preview(api, spot_only=spot_only, profile=profile)
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
    parser.add_argument('--profile', default=DEFAULT_PROFILE, choices=(LEGACY_PROFILE, *sorted(MAPPED_PROFILES)),
                        help='Pinned search profile; build/install callers must use the resident profile')
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
                spot_only=args.spot_only, profile=args.profile, unavailable=activity.unavailable if activity else None)
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
