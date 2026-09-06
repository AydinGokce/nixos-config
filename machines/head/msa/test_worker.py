"""Worker selection uses read-only evidence and fails closed without renting."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import worker


def row(kind='CPU.360V.1440G', gb=1440, price=4.32, spot=1.728):
    return {'instance_type': kind, 'memory': {'size_in_gigabytes': gb}, 'price_per_hour': price,
            'spot_price': spot, 'currency': 'usd',
            'supported_os': ['ubuntu-24.04', 'ubuntu-24.04-cuda-12.8-open-docker']}


def avail(*names):
    return [{'location_code': 'FIN-02', 'availabilities': list(names)}]


class SelectionTests(unittest.TestCase):
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
        result = worker.choose(catalog, avail(*names), avail(*names))
        self.assertEqual(result['instance_type'], '8H100.80S.176V')
        self.assertFalse(result['spot'])
        catalog[-1]['price_per_hour'] = 13.00001
        with self.assertRaises(worker.Unavailable):
            worker.choose(catalog, avail(*names), avail(*names))

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


if __name__ == '__main__':
    unittest.main()
