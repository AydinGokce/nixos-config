"""Worker selection uses read-only evidence and fails closed without renting."""
import contextlib
import copy
import io
import json
import os
import runpy
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import worker


def row(kind='CPU.360V.1440G', gb=1440, price=4.32, spot=1.728):
    return {'instance_type': kind, 'memory': {'size_in_gigabytes': gb}, 'price_per_hour': price,
            'spot_price': spot, 'currency': 'usd',
            'supported_os': ['ubuntu-24.04', 'ubuntu-24.04-cuda-12.8-open-docker']}


def avail(*names):
    return [{'location_code': 'FIN-02', 'availabilities': list(names)}]


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class RoundsAPI:
    def __init__(self, rounds):
        self.rounds = rounds
        self.calls = []

    def request(self, method, endpoint):
        index = len(self.calls)
        self.calls.append((method, endpoint))
        if method != 'GET':
            raise AssertionError('Mutation attempted')
        return self.rounds[min(index // len(worker.ENDPOINTS), len(self.rounds)-1)][index % len(worker.ENDPOINTS)]


class WaitingTests(unittest.TestCase):
    def test_shortage_then_capacity_returns_one_choice_after_read_only_retry(self):
        item = row()
        api = RoundsAPI([([item], avail(), avail()), ([item], avail(), avail(item['instance_type']))])
        clock = Clock()
        shortages = []
        selected = worker.wait_for_capacity(api, wait_seconds=90, poll_seconds=30,
            clock=clock, sleep=clock.sleep, unavailable=lambda: shortages.append(clock()))
        self.assertEqual(selected['instance_type'], item['instance_type'])
        self.assertTrue(selected['spot'])
        self.assertEqual(api.calls, [('GET', endpoint) for endpoint in worker.ENDPOINTS] * 2)
        self.assertEqual(clock.sleeps, [30])
        self.assertEqual(shortages, [0])

    def test_deadline_does_not_start_an_attempt_at_or_after_expiry(self):
        api = RoundsAPI([([row()], avail(), avail())])
        clock = Clock()
        with self.assertRaisesRegex(worker.Unavailable, '65-second capacity wait window'):
            worker.wait_for_capacity(api, wait_seconds=65, poll_seconds=30, clock=clock, sleep=clock.sleep)
        self.assertEqual(clock.sleeps, [30, 30, 5])
        self.assertEqual(len(api.calls), 9)
        self.assertEqual(clock(), 65)

    def test_result_returning_after_deadline_is_not_accepted(self):
        clock = Clock()
        item = row()
        class SlowAPI(RoundsAPI):
            def request(self, method, endpoint):
                clock.now += 11
                return super().request(method, endpoint)
        api = SlowAPI([([item], avail(item['instance_type']), avail())])
        with self.assertRaises(worker.Unavailable):
            worker.wait_for_capacity(api, wait_seconds=30, clock=clock, sleep=clock.sleep)
        self.assertEqual(len(api.calls), 3)
        self.assertEqual(clock.sleeps, [])

    def test_malformed_evidence_or_api_error_is_never_retried(self):
        broken = RoundsAPI([([row()], [], avail())])
        class APIError:
            def __init__(self): self.calls = []
            def request(self, method, endpoint):
                self.calls.append((method, endpoint))
                raise RuntimeError('PRIVATE_CREDENTIAL')
        for api in (broken, APIError()):
            clock = Clock()
            with self.subTest(api=type(api).__name__), self.assertRaises(worker.Error) as context:
                worker.wait_for_capacity(api, wait_seconds=90, clock=clock, sleep=clock.sleep)
            self.assertNotIsInstance(context.exception, worker.Unavailable)
            self.assertNotIn('PRIVATE_CREDENTIAL', str(context.exception))
            self.assertLessEqual(len(api.calls), 3)
            self.assertEqual(clock.sleeps, [])

    def test_cancellation_stops_without_another_capacity_query(self):
        api = RoundsAPI([([row()], avail(), avail())])
        def cancel(seconds):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            worker.wait_for_capacity(api, wait_seconds=90, sleep=cancel)
        self.assertEqual(len(api.calls), 3)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_deadline_interrupts_inflight_io_and_restores_previous_handler(self):
        previous = signal.getsignal(signal.SIGALRM)
        class API:
            calls = []
            def request(self, method, endpoint):
                self.calls.append((method, endpoint))
                time.sleep(10)
                raise AssertionError('Deadline failed to interrupt the provider request')
        api = API()
        started = time.monotonic()
        with self.assertRaises(worker.Unavailable):
            worker.wait_for_capacity(api, wait_seconds=.05)
        self.assertLess(time.monotonic()-started, 2)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_durations_require_finite_bounded_numbers_before_provider_access(self):
        for wait, poll in [(-1, 30), (7201, 30), ('nan', 30), ('inf', 30), (True, 30),
                           (30, 0), (30, .5), (30, 301), (30, 'nan'), (30, 'inf'), (30, True)]:
            api = RoundsAPI([([row()], avail(), avail())])
            with self.subTest(wait=wait, poll=poll), self.assertRaises(worker.Error):
                worker.wait_for_capacity(api, wait_seconds=wait, poll_seconds=poll)
            self.assertEqual(api.calls, [])

    def test_zero_wait_preserves_one_check_without_retry_or_progress(self):
        api = RoundsAPI([([row()], avail(), avail())])
        clock = Clock()
        with self.assertRaisesRegex(worker.Unavailable, 'No available FIN-02'):
            worker.wait_for_capacity(api, clock=clock, sleep=clock.sleep,
                unavailable=lambda: self.fail('single check must not emit waiting progress'))
        self.assertEqual(len(api.calls), 3)
        self.assertEqual(clock.sleeps, [])


class SelectionTests(unittest.TestCase):
    def choose_mapped(self, *args, **kwargs):
        kwargs.setdefault("profile", worker.MAPPED_PROFILE)
        return worker.choose(*args, **kwargs)

    def test_prefetch_profile_preserves_128gb_admission_and_ubuntu_selection(self):
        profile='mapped-prefetch-128gb-v1'
        item=row('1H100.80S.32V',128,3.25,1.625)
        selected=worker.choose([item],avail(item['instance_type']),avail(),profile=profile)
        self.assertEqual(selected['profile_id'],profile)
        self.assertEqual(selected['image'],'ubuntu-24.04')
        self.assertEqual(selected['search_profile']['memory_max_gib'],96)
        item['memory']['size_in_gigabytes']=127.999
        with self.assertRaises(worker.Unavailable):
            worker.choose([item],avail(item['instance_type']),avail(),profile=profile)

    def test_mapped_accepts_reviewed_cpu_hosts_of_other_gpu_families(self):
        for kind, gb in [('4L40S.80V', 240), ('4A6000.40V', 240),
                         ('4RTX6000ADA.40V', 240), ('2RTXPRO6000.60V', 180),
                         ('4RTXPRO6000.120V', 360), ('8V100.48V', 180)]:
            item = row(kind, gb, 4, 2)
            item['supported_os'] = ['ubuntu-24.04']
            with self.subTest(kind=kind):
                result = self.choose_mapped([item], avail(kind), avail())
                self.assertEqual(result['instance_type'], kind)
                self.assertFalse(result['cpu'])
                self.assertEqual(result['image'], 'ubuntu-24.04')
                # More RAM and the old image do not expand resident/build's
                # independently reviewed family policy.
                resident = row(kind, 1440)
                with self.assertRaises(worker.Unavailable):
                    self.choose_mapped([resident], avail(kind), avail(), profile=worker.LEGACY_PROFILE)

    def test_mapped_prefers_base_ubuntu_and_only_uses_reviewed_cuda_fallback(self):
        item = row('1H100.80S.32V', 170)
        result = self.choose_mapped([item], avail(item['instance_type']), avail())
        self.assertEqual(result['image'], 'ubuntu-24.04')
        item['supported_os'] = ['ubuntu-24.04-cuda-12.8-open-docker']
        result = self.choose_mapped([item], avail(item['instance_type']), avail())
        self.assertEqual(result['image'], 'ubuntu-24.04-cuda-12.8-open-docker')
        for images in (['ubuntu-24.04-cuda-13.0-open-docker'], ['ubuntu-22.04'], []):
            item['supported_os'] = images
            with self.subTest(images=images), self.assertRaises(worker.Unavailable):
                self.choose_mapped([item], avail(item['instance_type']), avail())
        resident = row('8H100.80S.176V', 1360)
        self.assertEqual(self.choose_mapped([resident], avail(resident['instance_type']), avail(),
                         profile=worker.LEGACY_PROFILE)['image'], 'ubuntu-24.04-cuda-12.8-open-docker')
        resident['supported_os'] = ['ubuntu-24.04']
        with self.assertRaises(worker.Unavailable):
            self.choose_mapped([resident], avail(resident['instance_type']), avail(), profile=worker.LEGACY_PROFILE)

    def test_mapped_does_not_infer_support_from_unknown_suffix_or_grace_name(self):
        for kind in ('2GB200.186V', '1GB300.32V', '1GH200.72V', '2GRACE.60V',
                     '2RTXPRO6000.60V.CC', '2RTXPRO6000.60V.UNKNOWN', '2L40S.ARM.80V',
                     '1H100.80S.32V.CC.UNKNOWN', '1H100.80S.32V.ARM', '1H100.UNKNOWN',
                     '4UNKNOWN.80V', '16RTXPRO6000.480V'):
            item = row(kind, 1800)
            with self.subTest(kind=kind), self.assertRaises(worker.Unavailable):
                self.choose_mapped([item], avail(kind), avail())

    def test_mapped_profile_uses_exact_decimal_128gb_and_distinct_guest_floors(self):
        for gb in (128, '128', 128.0, 128.01, 170):
            with self.subTest(gb=gb):
                item = row('1H100.80S.32V', gb, 3.25, 1.625)
                result = self.choose_mapped([item], avail(item['instance_type']), avail())
                self.assertEqual(result['profile_id'], 'mapped-128gb-v1')
                self.assertEqual(result['minimum_advertised_bytes'], 128_000_000_000)
                self.assertEqual(result['minimum_total_gib'], 110)
                self.assertEqual(result['minimum_available_gib'], 100)
                self.assertEqual(result['search_profile'], worker.search_profile(worker.MAPPED_PROFILE))
                self.assertEqual(result['profile_sha256'], result['search_profile']['profile_sha256'])
                self.assertAlmostEqual(result['conservative_gib'], float(gb) * 10**9 / 1024**3)
                self.assertFalse(result['reserved'])
        for gb in (0, 127, 127.99999, '127.99999'):
            item = row('CPU.32V.128G', gb)
            with self.subTest(gb=gb), self.assertRaisesRegex(worker.Unavailable, '128 GB advertised'):
                self.choose_mapped([item], avail(item['instance_type']), avail())

    def test_legacy_profile_keeps_exact_build_admission_and_tie_breaks(self):
        policy = runpy.run_path(str(Path(worker.__file__).with_name('build-queue.py')))
        catalog = [row('CPU.32V.128G', 128, 1, .5), row('CPU.192V.768G', 768, 1, .5),
                   row('CPU.256V.1024G', 1024, 6, 3), row('1H100.80S.32V', 170, 3.25, 1.625),
                   row('8H100.80S.176V', 1360, 13, 6), row('2GB200.186V', 1800, 1, .5),
                   row('8H100.80S.176V.CC', 1440, 1, .5)]
        for regular in ([], [item['instance_type'] for item in catalog]):
            for spot in ([], [item['instance_type'] for item in catalog]):
                for spot_only in (False, True):
                    with self.subTest(regular=bool(regular), spot=bool(spot), spot_only=spot_only):
                        expected = policy['choose'](catalog, avail(*([] if spot_only else regular)), avail(*spot))
                        if expected is None:
                            with self.assertRaises(worker.Unavailable):
                                worker.choose(catalog, avail(*regular), avail(*spot),
                                              profile=worker.LEGACY_PROFILE, spot_only=spot_only)
                        else:
                            result = worker.choose(catalog, avail(*regular), avail(*spot),
                                                   profile=worker.LEGACY_PROFILE, spot_only=spot_only)
                            self.assertEqual({key: result[key] for key in expected}, expected)
                            self.assertEqual(result['minimum_advertised_bytes'], 768 * 1024**3)
                            self.assertEqual(result['search_profile']['warm_mode'], 'prefetch')
        edge = row('CPU.192V.768G', 768 * 1024**3 / 10**9)
        self.assertEqual(worker.choose([edge], avail(edge['instance_type']), avail(),
                         profile=worker.LEGACY_PROFILE)['conservative_gib'], 768)
        # The actual database build/install selector stays high-memory.
        low = row('CPU.32V.128G', 128)
        self.assertIsNone(policy['choose']([low], avail(low['instance_type']), avail()))

    def test_invalid_or_tampered_profile_is_rejected_before_provider_access(self):
        modified = worker.search_profile(); modified['minimum_advertised_bytes'] = 1
        boolean = worker.search_profile(); boolean['api_workers'] = True
        for profile in ('unknown', modified, boolean, True):
            api = RoundsAPI([([row()], avail(), avail())])
            with self.subTest(profile=profile), self.assertRaises(worker.Error):
                worker.wait_for_capacity(api, profile=profile, wait_seconds=7200)
            self.assertEqual(api.calls, [])

    def test_profile_is_preserved_across_capacity_retries(self):
        large = row()
        small = row('CPU.32V.128G', 128)
        api = RoundsAPI([([small, large], avail(small['instance_type']), avail()),
                         ([small, large], avail(large['instance_type']), avail())])
        clock = Clock()
        result = worker.wait_for_capacity(api, profile=worker.LEGACY_PROFILE, wait_seconds=7200,
                                          clock=clock, sleep=clock.sleep)
        self.assertEqual(result['instance_type'], large['instance_type'])
        self.assertEqual(result['profile_id'], worker.LEGACY_PROFILE)
        self.assertEqual(clock.sleeps, [30])

    def test_unavailable_cpu_selects_cheapest_eligible_regular_or_spot(self):
        cpu, h100, b200 = row(), row('8H100.80S.176V', 1360, 26, 13), row('8B200.240V', 1440, 24, 12)
        result = worker.choose([cpu, h100, b200], avail(), avail(h100['instance_type'], b200['instance_type']))
        self.assertEqual(result['instance_type'], b200['instance_type'])
        self.assertTrue(result['spot'])
        self.assertEqual(result['price_per_hour'], 12)
        self.assertEqual(result['image'], 'ubuntu-24.04-cuda-12.8-open-docker')
        self.assertFalse(result['reserved'])

    def test_price_boundary_decimal_memory_and_architecture_are_enforced(self):
        catalog = [row('CPU.192V.768G', 768, 1, .5), row('2GB200.186V', 1800, 1, .5),
                   row('8H100.80S.176V.CC', 1440, 1, .5), row('8H100.80S.176V', 1360, 13, 13.01)]
        names = [item['instance_type'] for item in catalog]
        result = worker.choose(catalog, avail(*names), avail(*names), profile=worker.LEGACY_PROFILE)
        self.assertEqual(result['instance_type'], '8H100.80S.176V')
        self.assertFalse(result['spot'])
        catalog[-1]['price_per_hour'] = 13.00001
        with self.assertRaises(worker.Unavailable):
            worker.choose(catalog, avail(*names), avail(*names), profile=worker.LEGACY_PROFILE)

    def test_tie_prefers_cpu_then_regular_without_changing_queue_policy(self):
        cpu, gpu = row(price=13, spot=13), row('8H100.80S.176V', 1360, 13, 13)
        result = worker.choose([gpu, cpu], avail(gpu['instance_type'], cpu['instance_type']),
                               avail(gpu['instance_type'], cpu['instance_type']))
        self.assertTrue(result['cpu'])
        self.assertFalse(result['spot'])
        self.assertEqual(result['image'], 'ubuntu-24.04')

    def test_spot_only_never_uses_cheaper_regular_capacity(self):
        cpu, gpu = row(price=4, spot=20), row('8H100.80S.176V', 1360, 26, 13)
        result = worker.choose([cpu, gpu], avail(cpu['instance_type']), avail(gpu['instance_type']), spot_only=True)
        self.assertEqual(result['instance_type'], gpu['instance_type'])
        self.assertTrue(result['spot'])
        self.assertTrue(result['spot_only'])
        with self.assertRaises(worker.Unavailable):
            worker.choose([cpu], avail(cpu['instance_type']), avail(), spot_only=True)

    def test_wrong_image_and_no_capacity_fail_closed(self):
        item = row(); item['supported_os'] = ['ubuntu-22.04']
        with self.assertRaises(worker.Unavailable):
            worker.choose([item], avail(item['instance_type']), avail())
        with self.assertRaises(worker.Unavailable):
            worker.choose([row()], avail(), avail())

    def test_malformed_catalog_never_yields_partial_valid_choice(self):
        good = row()
        for field, value in [('instance_type', None), ('memory', {}), ('memory', {'size_in_gigabytes': True}),
                             ('memory', {'size_in_gigabytes': 'nan'}), ('price_per_hour', -1),
                             ('spot_price', float('inf')), ('spot_price', True), ('currency', 'eur'),
                             ('supported_os', 'ubuntu-24.04')]:
            bad = row('CPU.256V.1024G', 1024)
            bad[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(worker.Error):
                worker.choose([good, bad], avail(good['instance_type']), avail())
        with self.assertRaises(worker.Error):
            worker.choose([good, copy.deepcopy(good)], avail(good['instance_type']), avail())

    def test_missing_duplicate_unknown_and_wrong_location_availability_are_refused(self):
        item = row()
        invalid = [[], avail()+avail(), avail('unknown'), avail(item['instance_type'], item['instance_type']),
                   [{'location_code': 'ICE-01', 'availabilities': [item['instance_type']]}],
                   [{'location_code': 'FIN-02', 'availabilities': None}]]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(worker.Error):
                worker.choose([item], value, avail())

    def test_preview_only_uses_three_gets_without_account_or_resource_mutation(self):
        item = row()
        class API:
            def __init__(self): self.calls = []
            def request(self, method, endpoint):
                self.calls.append((method, endpoint))
                if method != 'GET': raise AssertionError('mutation attempted')
                return {'/instance-types?currency=usd': [item],
                        '/instance-availability?location_code=FIN-02&is_spot=false': avail(item['instance_type']),
                        '/instance-availability?location_code=FIN-02&is_spot=true': avail()}[endpoint]
        api = API(); result = worker.preview(api)
        self.assertEqual(api.calls, [('GET', endpoint) for endpoint in worker.ENDPOINTS])
        self.assertEqual(result['instance_type'], item['instance_type'])

    def test_provider_exceptions_never_expose_response_credentials(self):
        class API:
            def request(self, method, path): raise RuntimeError('token=PRIVATE_CREDENTIAL')
        with self.assertRaises(worker.Error) as context:
            worker.preview(API())
        self.assertNotIn('PRIVATE_CREDENTIAL', str(context.exception))


class CLITests(unittest.TestCase):
    def invoke(self, body, credentials=True, extra=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'dc-budget.py').write_text(body)
            (root/'py').mkdir()
            (root/'py/worker_progress.py').write_bytes(
                (Path(__file__).resolve().parents[3]/'modules/bio/py/worker_progress.py').read_bytes())
            stdout, stderr = io.StringIO(), io.StringIO()
            env = {'DATACRUNCH_CLIENT_ID': 'fixture', 'DATACRUNCH_CLIENT_SECRET': 'fixture'} if credentials else {}
            with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = worker.main(['select', '--tools-root', str(root), *extra])
            return code, stdout.getvalue(), stderr.getvalue()

    def test_no_capacity_exit4_with_no_json_choice(self):
        body = 'class API:\n def request(self,method,path):\n  assert method=="GET"\n  return '+repr([row()])+' if path.startswith("/instance-types") else '+repr(avail())+'\n'
        status, out, err = self.invoke(body)
        self.assertEqual(status, 4)
        self.assertEqual(out, '')
        self.assertIn('No available FIN-02', err)

    def test_cli_default_rejects_128gb_until_experimental_profile_is_explicit(self):
        item = row('CPU.32V.128G', 128)
        body = ('class API:\n def request(self,method,path):\n  assert method=="GET"\n'
                +'  return '+repr([item])+' if path.startswith("/instance-types") else '
                +repr(avail(item['instance_type']))+'\n')
        status, out, err = self.invoke(body, extra=['--profile', worker.MAPPED_PROFILE])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out)['profile_id'], worker.MAPPED_PROFILE)
        self.assertEqual(err, '')
        status, out, err = self.invoke(body)
        self.assertEqual(status, 4)
        self.assertEqual(out, '')
        self.assertIn('768 GiB advertised', err)

    def test_cli_success_is_exact_json_selection_without_catalog_extra_fields(self):
        item = row('8H100.80S.176V', 1360, 26, 13)
        item['unrelated_provider_field'] = 'PRIVATE_CREDENTIAL'
        body = ('class API:\n def request(self,method,path):\n  assert method=="GET"\n'
                +'  return '+repr([item])+' if path.startswith("/instance-types") else '
                +repr(avail(item['instance_type']))+'\n')
        status, out, err = self.invoke(body, extra=['--spot-only'])
        self.assertEqual(status, 0)
        self.assertEqual(err, '')
        result = json.loads(out)
        self.assertEqual(result['instance_type'], item['instance_type'])
        self.assertEqual(result['price_per_hour'], 13)
        self.assertTrue(result['spot'])
        self.assertEqual(result['image'], 'ubuntu-24.04-cuda-12.8-open-docker')
        self.assertEqual(result['location'], 'FIN-02')
        self.assertIn('observed_at', result)
        self.assertNotIn('PRIVATE_CREDENTIAL', out)

    def test_missing_credentials_or_malformed_helper_fails_before_provider_access(self):
        for credentials in (False, True):
            status, out, err = self.invoke('raise RuntimeError("PRIVATE_CREDENTIAL")\n' if credentials else
                                           'raise AssertionError("helper must not load")\n', credentials)
            self.assertEqual(status, 2)
            self.assertEqual(out, '')
            self.assertNotIn('PRIVATE_CREDENTIAL', err)

    def test_waiting_cli_progress_has_unknown_availability_eta_and_no_extra_stdout(self):
        body = ('class API:\n def request(self,method,path):\n  assert method=="GET"\n'
                +'  return '+repr([row()])+' if path.startswith("/instance-types") else '+repr(avail())+'\n')
        status, out, err = self.invoke(body, extra=['--wait-seconds', '.05'])
        self.assertEqual(status, 4)
        self.assertEqual(out, '')
        events = [json.loads(line.removeprefix('BIO_WORKER_STAGE ')) for line in err.splitlines()
                  if line.startswith('BIO_WORKER_STAGE ')]
        self.assertGreaterEqual(len(events), 3)
        self.assertEqual({event['stage'] for event in events}, {'waiting_capacity'})
        self.assertEqual(events[0]['state'], 'running')
        self.assertEqual(events[-1]['state'], 'failed')
        self.assertEqual(len({event['stage_id'] for event in events}), 1)
        self.assertTrue(all(event['eta']['state'] == 'unknown' for event in events))
        self.assertTrue(all('seconds' not in event['eta'] and 'completed' not in event for event in events))
        self.assertIn('left in the retry window', events[0]['message'])
        self.assertIn('0.05-second capacity wait window', err)

    def test_invalid_wait_configuration_does_not_import_provider_helper(self):
        for extra in [['--wait-seconds', 'nan'], ['--wait-seconds', '7201'], ['--poll-seconds', '0']]:
            status, out, err = self.invoke('raise AssertionError("PRIVATE_CREDENTIAL")\n', extra=extra)
            self.assertEqual(status, 2)
            self.assertEqual(out, '')
            self.assertNotIn('PRIVATE_CREDENTIAL', err)

    def test_sigterm_during_wait_exits_without_another_query_or_json_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'py').mkdir()
            (root/'py/worker_progress.py').write_bytes(
                (Path(__file__).resolve().parents[3]/'modules/bio/py/worker_progress.py').read_bytes())
            (root/'dc-budget.py').write_text('from pathlib import Path\nclass API:\n'
                +' def request(self,method,path):\n  assert method=="GET"\n'
                +'  with Path('+repr(str(root/'calls'))+').open("a") as stream: stream.write(path+"\\n")\n'
                +'  return '+repr([row()])+' if path.startswith("/instance-types") else '+repr(avail())+'\n')
            child = subprocess.Popen([sys.executable, str(Path(worker.__file__).resolve()),
                'select', '--tools-root', str(root), '--wait-seconds', '90'],
                env={**os.environ, 'DATACRUNCH_CLIENT_ID': 'fixture', 'DATACRUNCH_CLIENT_SECRET': 'fixture'},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic()+5
                while time.monotonic() < deadline:
                    if (root/'calls').exists() and len((root/'calls').read_text().splitlines()) == 3:
                        break
                    if child.poll() is not None:
                        self.fail('Selector exited before waiting for capacity')
                    time.sleep(.01)
                else:
                    self.fail('Selector did not reach the first capacity check')
                child.terminate()
                out, err = child.communicate(timeout=3)
                self.assertEqual(child.returncode, -signal.SIGTERM)
                self.assertEqual(out, '')
                self.assertEqual(len((root/'calls').read_text().splitlines()), 3)
                self.assertNotIn('PRIVATE_CREDENTIAL', err)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()


if __name__ == '__main__':
    unittest.main()
