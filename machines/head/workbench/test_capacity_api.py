"""Read-only capacity policy, failure visibility, and shared process cache proofs."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import io
import json
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
POLICY = runpy.run_path(str(HEAD / 'msa/build-queue.py'))


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
    return {**provider.snapshot(evidence(), SELECTOR, POLICY, current=current), **changes}


class ProviderTests(unittest.TestCase):
    def test_policy_and_units_keep_cpu_offers_in_msa_decision(self):
        value = observed()
        self.assertEqual(value['state'], 'ready')
        self.assertTrue(value['msa_available'])
        row = value['gpus'][0]
        self.assertTrue(row['msa_eligible'])
        self.assertAlmostEqual(row['ram_gib'], 960 * 10**9 / 1024**3)
        self.assertAlmostEqual(row['gpu_memory_gib'], 640 * 10**9 / 1024**3)
        cpu = machine('CPU.360V.1440G', ram=1440, regular=4.32, spot=1.728, count=0)
        value = provider.snapshot(evidence(catalog=[cpu]), SELECTOR, POLICY, current=1000)
        self.assertTrue(value['msa_available']); self.assertEqual(value['gpus'], [])
        self.assertIn('CPU', value['msa_message'])

    def test_available_gpu_is_not_necessarily_eligible_for_msa(self):
        cases = [machine(ram=768), machine(regular=13.01), machine(images=['ubuntu-22.04']),
                 machine('8GB300.256V', ram=1800), machine('8H100.176V.CC')]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                value = provider.snapshot(evidence(catalog=[candidate]), SELECTOR, POLICY, current=1000)
                self.assertEqual(value['state'], 'ready'); self.assertFalse(value['msa_available'])
                self.assertEqual(len(value['gpus']), 1); self.assertFalse(value['gpus'][0]['msa_eligible'])
        data = evidence(regular=[])
        data['regular'][0]['availabilities'] = [data['catalog'][0]['instance_type']]
        value = provider.snapshot(data, SELECTOR, POLICY, current=1000)
        self.assertFalse(value['msa_available']); self.assertEqual(value['gpus'][0]['location'], 'FIN-01')
        self.assertFalse(value['gpus'][0]['msa_eligible'])

    def test_regular_and_spot_policy_are_distinct_and_ceiling_inclusive(self):
        candidate = machine(regular=14, spot=13)
        data = evidence(catalog=[candidate], spot=[candidate['instance_type']])
        value = provider.snapshot(data, SELECTOR, POLICY, current=1000)
        self.assertTrue(value['msa_available'])
        rows = {row['contract']: row for row in value['gpus']}
        self.assertFalse(rows['regular']['msa_eligible']); self.assertTrue(rows['spot']['msa_eligible'])
        self.assertEqual(rows['spot']['price_hourly'], 13)

    def test_failed_essential_lookups_never_become_unavailable(self):
        for key in ('catalog', 'regular', 'spot'):
            with self.subTest(key=key):
                data = evidence(); data[key] = None
                value = provider.snapshot(data, SELECTOR, POLICY, current=1000)
                self.assertIsNone(value['msa_available']); self.assertIn(value['state'], ('partial', 'error'))
        for change in (lambda data: data['regular'].pop(),
                       lambda data: data['regular'].append(deepcopy(data['regular'][-1])),
                       lambda data: data['regular'][-1]['availabilities'].append('unknown'),
                       lambda data: data['catalog'][0].update(currency='eur')):
            data = evidence(); change(data)
            value = provider.snapshot(data, SELECTOR, POLICY, current=1000)
            self.assertIsNone(value['msa_available']); self.assertNotEqual(value['state'], 'ready')

    def test_nonessential_region_or_locations_failure_is_explicit_partial(self):
        for change in (lambda data: data.update(locations=None),
                       lambda data: data['regular'].pop(0)):
            data = evidence(); change(data)
            value = provider.snapshot(data, SELECTOR, POLICY, current=1000)
            self.assertEqual(value['state'], 'partial'); self.assertTrue(value['msa_available'])
            self.assertIn('error', value)

    def test_get_only_fixed_endpoints_and_exception_redaction(self):
        data = evidence(); requests = []
        class FakeAPI:
            def request(self, method, endpoint):
                requests.append((method, endpoint))
                return deepcopy(data[next(key for key, path in provider.ENDPOINTS.items() if path == endpoint)])
        result = provider.collect(FakeAPI(), SELECTOR, POLICY, current=1000)
        self.assertTrue(result['msa_available'])
        self.assertEqual(sorted(requests), sorted(('GET', path) for path in provider.ENDPOINTS.values()))
        class FailedAPI:
            def request(self, method, endpoint):
                raise RuntimeError('Bearer private-access-token and client_secret=private-password')
        result = provider.collect(FailedAPI(), SELECTOR, POLICY, current=1000)
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
