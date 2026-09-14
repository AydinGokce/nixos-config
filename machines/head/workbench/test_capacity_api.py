"""Read-only capacity policy, failure visibility, and shared process cache proofs."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import io
import json
import os
import runpy
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from workbench.api import API
from workbench.common import Error, canonical
from workbench.store import Store
from workbench import capacity_api as cache
from workbench import capacity_provider as provider

HEAD = Path(__file__).resolve().parent.parent
SELECTOR = runpy.run_path(str(HEAD / 'msa/worker.py'))


def machine(kind='8A100.176V', *, ram=960, regular=12, spot=6, count=8, images=None, **changes):
    return {'instance_type': kind, 'name': 'A100 SXM4 80GB', 'memory': {'size_in_gigabytes': ram},
            'gpu': {'number_of_gpus': count}, 'gpu_memory': {'size_in_gigabytes': count * 80},
            'currency': 'usd', 'price_per_hour': str(regular), 'spot_price': str(spot),
            'supported_os': ['ubuntu-24.04', 'ubuntu-24.04-cuda-12.8-open-docker'] if images is None else images,
            **changes}


def evidence(*, catalog=None, regular=None, spot=None):
    catalog = [machine()] if catalog is None else catalog
    return {'catalog': catalog, 'locations': [{'code': 'FIN-01'}, {'code': 'FIN-02'}],
            'regular': [{'location_code': 'FIN-01', 'availabilities': []},
                        {'location_code': 'FIN-02', 'availabilities': [catalog[0]['instance_type']] if regular is None else regular}],
            'spot': [{'location_code': 'FIN-01', 'availabilities': []},
                     {'location_code': 'FIN-02', 'availabilities': [] if spot is None else spot}]}


def observed(current=1000, **changes):
    return {**provider.snapshot(evidence(), SELECTOR, current=current), **changes}


def aws_observed(current=1000, **changes):
    return dict(schema=1, state='ready', observed_epoch=current, provider='aws', msa_provider='aws',
        compute_kind='cpu', region='us-east-1', msa_available=None,
        msa_message='Eligible AWS CPU offering; live capacity is unproven while stopped', gpus=[],
        cpus=[dict(instance_type='r6a.32xlarge', name='AMD EPYC CPU', location='us-east-1a',
            contract='on-demand', vcpus=128, ram_gib=1024, price_hourly=7.2576, msa_eligible=True,
            availability='eligible', reason='Standard quota and offering verified; live capacity unproven')], **changes)


class AwsCapacityTests(unittest.TestCase):
    def test_stopped_eligible_cpu_is_not_claimed_available(self):
        result = cache.normalize(aws_observed())
        self.assertIsNone(result['msa_available']); self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['msa_provider'], 'aws'); self.assertEqual(result['cpus'][0]['vcpus'], 128)
        self.assertEqual(result['cpus'][0]['availability'], 'eligible')
        self.assertEqual(result['cpus'][0]['ram_gib'], 1024)
        with self.assertRaises(Error):
            cache.normalize(observed(msa_available=None))

    def test_gpu_availability_never_overrides_aws_msa_readiness(self):
        verda = observed()
        for available in (True, False, None):
            aws = aws_observed(); aws['msa_available'] = available
            result = cache.normalize(provider.combined(aws, verda))
            self.assertEqual(result['msa_available'], available)
            self.assertEqual(result['gpus'], verda['gpus'])
        failed = cache.normalize(provider.combined(None, verda))
        self.assertEqual(failed['state'], 'error'); self.assertIsNone(failed['msa_available'])
        self.assertTrue(failed['gpus'])
        healthy = cache.normalize(provider.combined(aws_observed(), None))
        self.assertEqual(healthy['state'], 'ready'); self.assertEqual(healthy['gpus'], [])
        self.assertIn('gpu_error', healthy)

    def test_cpu_receipts_are_bounded_and_only_supported_scope_is_displayed(self):
        for change in ({'vcpus': True}, {'vcpus': 0}, {'ram_gib': float('nan')},
                       {'price_hourly': -1}, {'availability': 'available'},
                       {'reason': 'bad\nlabel'}, {'contract': 'spot'}):
            value = aws_observed(); value['cpus'][0].update(change)
            with self.subTest(change=change), self.assertRaises(Error):
                cache.normalize(value)
        value = aws_observed(); value['region'] = 'us-west-2'
        with self.assertRaises(Error): cache.normalize(value)
        value = aws_observed(); value['cpus'][0]['price_hourly'] = None
        self.assertIsNone(cache.normalize(value)['cpus'][0]['price_hourly'])

    def test_read_only_aws_command_and_redacted_independent_failure(self):
        def run(command, **kwargs):
            self.assertEqual(command, ['/trusted/bio-aws-msa', 'capacity'])
            kwargs['stdout'].write(canonical(aws_observed()))
            return subprocess.CompletedProcess(command, 0)
        with patch.object(provider.shutil, 'which', return_value='/trusted/bio-aws-msa'), \
             patch.object(provider.subprocess, 'run', side_effect=run):
            self.assertEqual(provider.aws_snapshot()['provider'], 'aws')
        with patch.dict(os.environ, BIO_MSA_PROVIDER='aws'), \
             patch.object(provider, 'aws_snapshot', return_value=aws_observed()), \
             patch.object(provider, 'verda_snapshot', side_effect=RuntimeError('private-secret')), \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(provider.main([]), 0)
        self.assertNotIn('private-secret', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['msa_provider'], 'aws')

    def test_aws_bridge_failure_keeps_selected_provider_visible(self):
        with patch.dict(os.environ, BIO_MSA_PROVIDER='aws'):
            value = cache.normalize(cache.failed(1000))
        self.assertEqual(value['msa_provider'], 'aws')
        self.assertEqual(value['cpus'], [])
        self.assertIsNone(value['msa_available'])


class ProviderTests(unittest.TestCase):
    def test_other_gpu_families_share_mapped_admission_and_base_image_support(self):
        for kind, ram in [('4A6000.40V', 240), ('4L40S.80V', 240),
                          ('4RTX6000ADA.40V', 240), ('2RTXPRO6000.60V', 180),
                          ('8V100.48V', 180)]:
            candidate = machine(kind, ram=ram, images=['ubuntu-24.04'])
            data = evidence(catalog=[candidate])
            with self.subTest(kind=kind):
                value = provider.snapshot(data, SELECTOR, current=1000, profile=SELECTOR['MAPPED_PROFILE'])
                self.assertEqual(value['state'], 'ready')
                self.assertTrue(value['msa_available'])
                self.assertTrue(value['gpus'][0]['msa_eligible'])
                choice = SELECTOR['choose'](data['catalog'], data['regular'], data['spot'], profile=SELECTOR['MAPPED_PROFILE'])
                self.assertEqual(choice['image'], 'ubuntu-24.04')
                value = provider.snapshot(data, SELECTOR, current=1000, profile=SELECTOR['LEGACY_PROFILE'])
                self.assertFalse(value['msa_available'])
                self.assertFalse(value['gpus'][0]['msa_eligible'])
        candidate = machine('2RTXPRO6000.60V.CC', ram=180, images=['ubuntu-24.04'])
        value = provider.snapshot(evidence(catalog=[candidate]), SELECTOR, current=1000, profile=SELECTOR['MAPPED_PROFILE'])
        self.assertFalse(value['msa_available'])
        self.assertFalse(value['gpus'][0]['msa_eligible'])

    def test_128_decimal_gb_profiles_and_offer_metadata_match_actual_selector(self):
        for profile in (SELECTOR['MAPPED_PROFILE'], SELECTOR['LEGACY_PROFILE']):
            for candidate in (machine('1H100.80S.32V', ram=128, count=1),
                              machine('1H100.80S.32V', ram=127.999, count=1),
                              machine('8H100.80S.176V', ram=1360),
                              machine('2GB200.186V', ram=1800),
                              machine(images=['ubuntu-22.04']), machine(regular=13.01, spot=13)):
                data = evidence(catalog=[candidate], spot=[candidate['instance_type']])
                with self.subTest(profile=profile, candidate=candidate):
                    value = provider.snapshot(data, SELECTOR, current=1000, profile=profile)
                    self.assertEqual(value['state'], 'ready')
                    expected_available = False
                    for row in value['gpus']:
                        offer = SELECTOR['assess_offer'](candidate, profile=profile,
                            spot=row['contract'] == 'spot', location=row['location'])
                        self.assertEqual(row['msa_eligible'], offer['eligible'])
                        self.assertEqual(row['reason'], offer['reason'])
                        self.assertEqual(row['ram_gib'], offer['conservative_gib'])
                        expected_available |= offer['eligible']
                    self.assertEqual(value['msa_available'], expected_available)
                    try:
                        choice = SELECTOR['choose'](data['catalog'], data['regular'], data['spot'], profile=profile)
                    except SELECTOR['Unavailable']:
                        self.assertFalse(value['msa_available'])
                    else:
                        self.assertTrue(value['msa_available'])
                        matching = next(row for row in value['gpus'] if
                            row['instance_type'] == choice['instance_type'] and
                            (row['contract'] == 'spot') == choice['spot'])
                        self.assertTrue(matching['msa_eligible'])
        value = provider.snapshot(evidence(catalog=[machine(ram=127)]), SELECTOR, current=1000, profile=SELECTOR['MAPPED_PROFILE'])
        self.assertIn('128 GB advertised host RAM (119.21 GiB)', value['msa_message'])

    def test_128gb_cpu_capacity_counts_without_a_gpu_row(self):
        cpu = machine('CPU.32V.128G', ram=128, regular=1, spot=.5, count=0)
        value = provider.snapshot(evidence(catalog=[cpu]), SELECTOR, current=1000, profile=SELECTOR['MAPPED_PROFILE'])
        self.assertEqual(value['state'], 'ready')
        self.assertTrue(value['msa_available'])
        self.assertEqual(value['gpus'], [])
        self.assertIn('CPU worker', value['msa_message'])

    def test_malformed_catalog_is_unknown_for_both_selector_and_console(self):
        good = machine('1H100.80S.32V', ram=128, count=1)
        for changes in ({'memory': {'size_in_gigabytes': True}},
                        {'memory': {'size_in_gigabytes': 'nan'}},
                        {'memory': {'size_in_gigabytes': float('inf')}},
                        {'price_per_hour': -1}, {'spot_price': True}, {'currency': 'eur'},
                        {'supported_os': 'ubuntu-24.04'}):
            broken = machine('8H100.80S.176V', **changes)
            data = evidence(catalog=[good, broken])
            with self.subTest(changes=changes):
                value = provider.snapshot(data, SELECTOR, current=1000)
                self.assertIsNone(value['msa_available'])
                self.assertEqual(value['state'], 'error')
                self.assertEqual(value['gpus'], [])
                with self.assertRaises(SELECTOR['Error']):
                    SELECTOR['choose'](data['catalog'], data['regular'], data['spot'])

    def test_policy_and_units_keep_cpu_offers_in_msa_decision(self):
        value = observed()
        self.assertEqual(value['state'], 'ready')
        self.assertTrue(value['msa_available'])
        row = value['gpus'][0]
        self.assertTrue(row['msa_eligible'])
        self.assertAlmostEqual(row['ram_gib'], 960 * 10**9 / 1024**3)
        self.assertAlmostEqual(row['gpu_memory_gib'], 640 * 10**9 / 1024**3)
        cpu = machine('CPU.360V.1440G', ram=1440, regular=4.32, spot=1.728, count=0)
        value = provider.snapshot(evidence(catalog=[cpu]), SELECTOR, current=1000)
        self.assertTrue(value['msa_available']); self.assertEqual(value['gpus'], [])
        self.assertIn('CPU', value['msa_message'])

    def test_available_gpu_is_not_necessarily_eligible_for_msa(self):
        cases = [machine(ram=127), machine(regular=13.01), machine(images=['ubuntu-22.04']),
                 machine('8GB300.256V', ram=1800), machine('8H100.176V.CC')]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                value = provider.snapshot(evidence(catalog=[candidate]), SELECTOR, current=1000)
                self.assertEqual(value['state'], 'ready'); self.assertFalse(value['msa_available'])
                self.assertEqual(len(value['gpus']), 1); self.assertFalse(value['gpus'][0]['msa_eligible'])
        data = evidence(regular=[])
        data['regular'][0]['availabilities'] = [data['catalog'][0]['instance_type']]
        value = provider.snapshot(data, SELECTOR, current=1000)
        self.assertFalse(value['msa_available']); self.assertEqual(value['gpus'][0]['location'], 'FIN-01')
        self.assertFalse(value['gpus'][0]['msa_eligible'])

    def test_regular_and_spot_policy_are_distinct_and_ceiling_inclusive(self):
        candidate = machine(regular=14, spot=13)
        data = evidence(catalog=[candidate], spot=[candidate['instance_type']])
        value = provider.snapshot(data, SELECTOR, current=1000)
        self.assertTrue(value['msa_available'])
        rows = {row['contract']: row for row in value['gpus']}
        self.assertFalse(rows['regular']['msa_eligible']); self.assertTrue(rows['spot']['msa_eligible'])
        self.assertEqual(rows['spot']['price_hourly'], 13)

    def test_failed_essential_lookups_never_become_unavailable(self):
        for key in ('catalog', 'regular', 'spot'):
            with self.subTest(key=key):
                data = evidence(); data[key] = None
                value = provider.snapshot(data, SELECTOR, current=1000)
                self.assertIsNone(value['msa_available']); self.assertIn(value['state'], ('partial', 'error'))
        for change in (lambda data: data['regular'].pop(),
                       lambda data: data['regular'].append(deepcopy(data['regular'][-1])),
                       lambda data: data['regular'][-1]['availabilities'].append('unknown'),
                       lambda data: data['catalog'][0].update(currency='eur')):
            data = evidence(); change(data)
            value = provider.snapshot(data, SELECTOR, current=1000)
            self.assertIsNone(value['msa_available']); self.assertNotEqual(value['state'], 'ready')

    def test_nonessential_region_or_locations_failure_is_explicit_partial(self):
        for change in (lambda data: data.update(locations=None),
                       lambda data: data['regular'].pop(0)):
            data = evidence(); change(data)
            value = provider.snapshot(data, SELECTOR, current=1000)
            self.assertEqual(value['state'], 'partial'); self.assertTrue(value['msa_available'])
            self.assertIn('error', value)

    def test_get_only_fixed_endpoints_and_exception_redaction(self):
        data = evidence(); requests = []
        class FakeAPI:
            def request(self, method, endpoint):
                requests.append((method, endpoint))
                return deepcopy(data[next(key for key, path in provider.ENDPOINTS.items() if path == endpoint)])
        result = provider.collect(FakeAPI(), SELECTOR, current=1000)
        self.assertTrue(result['msa_available'])
        self.assertEqual(sorted(requests), sorted(('GET', path) for path in provider.ENDPOINTS.values()))
        class FailedAPI:
            def request(self, method, endpoint):
                raise RuntimeError('Bearer private-access-token and client_secret=private-password')
        result = provider.collect(FailedAPI(), SELECTOR, current=1000)
        self.assertIsNone(result['msa_available']); self.assertEqual(result['state'], 'error')
        self.assertNotIn('private-', canonical(result).decode())


class CacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'state')
        self.config = {'tools_dir': str(HEAD), 'capacity_helper': '/trusted/bio-msa-capacity'}
        self.api = API(self.store, 'harrison', worker_config=self.config)
        self.current = 1000
        self.calls = []

    def bridge(self, config):
        self.calls.append(deepcopy(config))
        return observed(self.current)

    def capacity(self, params=None, api=None, bridge=None):
        return cache.capacity(api or self.api, {} if params is None else params,
            bridge=bridge or self.bridge, clock=lambda: self.current)

    def test_cache_survives_rpc_restarts_and_refreshes_at_five_seconds(self):
        first = self.capacity()
        self.assertEqual(first['checked_epoch'], 1000)
        self.current = 1004
        restarted = API(Store(self.root / 'state'), 'another', worker_config=self.config)
        second = self.capacity(api=restarted)
        self.assertEqual(len(self.calls), 1); self.assertEqual(second['checked_epoch'], 1000)
        self.assertEqual(second['server_epoch'], 1004)
        self.current = 1005
        self.assertEqual(self.capacity()['checked_epoch'], 1005); self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.store.listing('worker_control'), []); self.assertEqual(self.store.listing('job'), [])
        self.assertFalse((self.root / 'state/budget.json').exists())

    def test_manual_refresh_bypasses_cache_and_configuration_changes_invalidate_it(self):
        self.capacity(); self.current += 1
        self.assertEqual(self.capacity({'refresh': True})['checked_epoch'], 1001)
        self.assertEqual(len(self.calls), 2)
        self.api.worker_config = {**self.config, 'tools_dir': '/new/immutable/deployment'}
        self.capacity(); self.assertEqual(len(self.calls), 3)

    def test_provider_change_invalidates_cached_verda_availability(self):
        with patch.dict(os.environ, BIO_MSA_PROVIDER='verda'):
            self.assertTrue(self.capacity()['msa_available'])
        self.current += 1
        with patch.dict(os.environ, BIO_MSA_PROVIDER='aws'):
            value = self.capacity(bridge=lambda config: aws_observed(self.current))
        self.assertEqual(value['msa_provider'], 'aws')
        self.assertIsNone(value['msa_available'])
        self.assertEqual(value['checked_epoch'], self.current)

    def test_error_preserves_last_success_time_is_unknown_and_backs_off(self):
        self.capacity(); self.current = 1005
        def broken(config):
            self.calls.append(config)
            raise subprocess.TimeoutExpired('secret-command', 20, output=b'private-key', stderr=b'access-token')
        result = self.capacity(bridge=broken)
        self.assertEqual(result['state'], 'error'); self.assertIsNone(result['msa_available'])
        self.assertEqual(result['checked_epoch'], 1000); self.assertEqual(result['observed_epoch'], 1005)
        self.assertEqual(result['refresh_after_seconds'], 30)
        self.assertNotIn('secret', canonical(result).decode()); self.assertNotIn('token', canonical(result).decode())
        self.current = 1034
        self.capacity(bridge=broken); self.assertEqual(len(self.calls), 2)
        self.current = 1035
        self.capacity(bridge=broken); self.assertEqual(len(self.calls), 3)
        self.current = 1036
        self.capacity({'refresh': True}, bridge=broken); self.assertEqual(len(self.calls), 4)

    def test_partial_check_does_not_advance_complete_timestamp(self):
        self.capacity(); self.current = 1005
        result = self.capacity(bridge=lambda config: observed(self.current, state='partial', error='Spot region list is incomplete'))
        self.assertEqual(result['checked_epoch'], 1000); self.assertEqual(result['observed_epoch'], 1005)
        self.assertEqual(result['state'], 'partial')

    def test_concurrent_manual_refreshes_share_one_provider_request(self):
        entered, release = threading.Event(), threading.Event()
        def bridge(config):
            self.calls.append(config); entered.set()
            self.assertTrue(release.wait(5))
            return observed(self.current)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.capacity, {'refresh': True}, bridge=bridge)
            self.assertTrue(entered.wait(5))
            second = self.capacity({'refresh': True}, bridge=bridge)
            self.assertTrue(second['refreshing']); self.assertIsNone(second['msa_available'])
            self.assertIsNone(second['checked_epoch']); self.assertEqual(len(self.calls), 1)
            release.set()
            self.assertTrue(first.result(timeout=5)['msa_available'])
        self.assertFalse(self.capacity()['refreshing']); self.assertEqual(len(self.calls), 1)

    def test_stale_snapshot_during_refresh_does_not_claim_availability(self):
        first = self.capacity()
        saved = {'snapshot': first, 'checked_epoch': first['checked_epoch']}
        result = cache.envelope(saved, 1121, refreshing=True)
        self.assertTrue(result['stale']); self.assertIsNone(result['msa_available'])
        self.assertEqual(result['checked_epoch'], 1000)

    def test_untrusted_params_and_helper_fields_cannot_choose_credentials_or_resources(self):
        for params in ({'refresh': 1}, {'refresh': 'true'}, {'endpoint': '/instances'}, {'tools_dir': '/other'}, {'credential': 'private'}):
            with self.subTest(params=params), self.assertRaises(Error):
                self.capacity(params)
        self.assertEqual(self.calls, [])
        result = self.capacity(bridge=lambda config: observed(self.current, credential='private-key', argv=['token']))
        self.assertNotIn('private-key', canonical(result).decode()); self.assertNotIn('argv', result)

    def test_bridge_timeout_and_fixed_cli_arguments(self):
        def execute(command, **kwargs):
            self.assertEqual(command, ['/trusted/bio-msa-capacity', '--tools-root', str(HEAD)])
            self.assertEqual(kwargs['timeout'], 20)
            self.assertFalse(kwargs['check'])
            kwargs['stdout'].write(canonical(observed()))
            kwargs['stderr'].write(b'never show a credential from stderr')
            return subprocess.CompletedProcess(command, 0)
        with patch('workbench.capacity_api.subprocess.run', side_effect=execute):
            result = cache.invoke(self.config)
        self.assertTrue(result['msa_available'])
        self.assertNotIn('credential', canonical(result).decode())

    def test_rpc_route_and_worker_status_stay_independent(self):
        from workbench.cli import rpc
        output = io.BytesIO()
        request = canonical({'id': 'capacity', 'method': 'worker.capacity', 'params': {'refresh': True}}) + b'\n'
        with patch('workbench.capacity_api.invoke', return_value=observed()), \
                patch('workbench.capacity_api.time.time', return_value=1000), \
                patch('workbench.worker_api.invoke', side_effect=AssertionError('Capacity must not inspect/control a session')):
            rpc(self.store, 'harrison', io.BytesIO(request), output, worker_config=self.config)
        result = json.loads(output.getvalue())['result']
        self.assertTrue(result['msa_available']); self.assertEqual(result['checked_epoch'], 1000)


if __name__ == '__main__':
    unittest.main()
