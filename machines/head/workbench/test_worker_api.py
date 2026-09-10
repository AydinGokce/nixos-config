"""Local durable control journal proofs; no provider or worker is contacted."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import io
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from workbench.api import API
from workbench.common import Error, canonical, uid
from workbench.store import Store
from workbench import worker_api


TARGET = {'session_id': 'a' * 32, 'invocation_id': 'b' * 32,
          'intent_sha256': 'c' * 64, 'launch_sha256': 'd' * 64}


def observed(**changes):
    return {'schema': 1, 'state': 'idle', **TARGET, 'checked_epoch': 1000,
            'hard_deadline_epoch': 4000, 'idle_deadline_epoch': 1800,
            'shutdown_epoch': 1800, 'shutdown_reason': 'idle',
            'active_request_id': None, 'queued_requests': 0,
            'can_extend': True, 'can_shutdown': True, **changes}


def applied(action, params, **changes):
    return {'schema': 1, 'action': action, **params, 'status': 'applied',
            'applied_seconds': 900 if action == 'extend' else 0,
            'reason': 'Keep warm extended' if action == 'extend' else 'Accepted work will drain',
            'control_revision': 1, 'checked_epoch': 1000, 'hard_deadline_epoch': 4000,
            'idle_deadline_epoch': 2700, 'shutdown_epoch': 2700,
            'shutdown_requested': action == 'shutdown', **changes}


class WorkerApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'state')
        self.config = {'tools_dir': str(self.root / 'tools'), 'msa_sessions_root': str(self.root / 'sessions')}
        self.api = API(self.store, 'harrison', worker_config=self.config)
        self.params = {'request_key': uid(), 'target': deepcopy(TARGET)}

    def test_status_is_read_only_bounded_and_omits_internal_information(self):
        value = observed(provider_credentials='secret', argv=['private'], remote_out='/private')
        with patch('workbench.worker_api.invoke', return_value=value) as call, patch('workbench.worker_api.time.time', return_value=1000):
            status = self.api.call('worker.status', {})
        call.assert_called_once_with(self.config, 'status')
        self.assertEqual(status['target'], TARGET)
        self.assertTrue(status['controls']['extend']['enabled'])
        self.assertEqual(status['controls']['extend']['seconds'], 900)
        self.assertEqual(status['controls']['shutdown']['mode'], 'drain')
        self.assertNotIn('secret', canonical(status).decode())
        self.assertEqual(self.store.listing('worker_control'), [])
        self.assertFalse((self.root / 'sessions').exists())

    def test_rpc_uses_operator_configuration_without_client_path_overrides(self):
        from workbench.cli import rpc
        output = io.BytesIO()
        request = canonical({'id': 'status', 'method': 'worker.status', 'params': {}}) + b'\n'
        with patch('workbench.worker_api.invoke', return_value=observed()) as call:
            rpc(self.store, 'harrison', io.BytesIO(request), output, worker_config=self.config)
        call.assert_called_once_with(self.config, 'status')
        self.assertIn(b'"result"', output.getvalue())

    def test_absent_legacy_and_stale_observations_cannot_offer_controls(self):
        for value in [observed(checked_epoch=900), observed(checked_epoch=1100),
                      observed(launch_sha256=None), observed(can_extend=False, can_shutdown=False)]:
            result = worker_api.normalize_status(value, current=1000)
            self.assertFalse(result['controls']['extend']['enabled'])
            self.assertFalse(result['controls']['shutdown']['enabled'])
        result = worker_api.normalize_status({'schema': 1, 'state': 'absent', 'checked_epoch': 1000}, current=1000)
        self.assertIsNone(result['target']); self.assertIsNone(result['shutdown_epoch'])
        self.assertFalse(result['controls']['shutdown']['enabled'])

    def test_bound_raw_startup_snapshot_is_normalized_and_aged(self):
        from workbench.test_worker_progress import event
        value = observed(startup_progress=event(990, eta={'state': 'estimate', 'scope': 'stage',
                         'seconds': 100, 'basis': 'Observed transfer rate'}))
        result = worker_api.normalize_status(value, current=1000)
        self.assertEqual(result['progress']['stage'], 'index_warm')
        self.assertEqual(result['progress']['eta']['seconds'], 90)
        self.assertNotIn('startup_progress', result)

    def test_control_intent_returns_immediately_and_replays_across_restart(self):
        with patch('workbench.worker_api.invoke', side_effect=AssertionError('API must not call the worker')):
            first = self.api.call('worker.extend', self.params)
            restarted = API(Store(self.root / 'state'), 'harrison', worker_config=self.config)
            replay = restarted.call('worker.extend', self.params)
            self.assertEqual(first, replay)
            self.assertEqual(restarted.call('worker.control_get', {'control_id': first['control_id']}), first)
        self.assertEqual(first['state'], 'pending')
        self.assertNotIn('worker_control_id', first)
        self.assertEqual(len(self.store.listing('worker_control')), 1)
        self.assertEqual(self.store.listing('job'), [])
        with self.assertRaisesRegex(Error, 'different parameters'):
            self.api.call('worker.extend', {**self.params, 'target': {**TARGET, 'session_id': 'e' * 32}})
        with self.assertRaisesRegex(Error, 'not found'):
            API(self.store, 'another').call('worker.control_get', {'control_id': first['control_id']})

    def test_ambiguous_apply_retries_same_command_and_does_not_double_extend(self):
        intent = self.api.call('worker.extend', self.params)
        effects, calls = {}, []
        def bridge(config, action, params):
            calls.append(deepcopy(params))
            if params['command_id'] not in effects:
                effects[params['command_id']] = applied(action, params)
                raise subprocess.TimeoutExpired('fake transport lost response after apply', 1)
            return effects[params['command_id']]
        worker_api.reconcile(self.store, self.config, bridge=bridge, current=1000)
        pending = self.api.call('worker.control_get', {'control_id': intent['control_id']})
        self.assertEqual(pending['state'], 'pending'); self.assertEqual(pending['error']['code'], 'uncertain')
        # The dispatcher restarts; unrelated active generations cannot retarget
        # the retained command, and an early tick does not busy-poll it.
        store = Store(self.root / 'state')
        worker_api.reconcile(store, self.config, bridge=bridge, current=1001)
        self.assertEqual(len(calls), 1)
        worker_api.reconcile(store, self.config, bridge=bridge, current=1006)
        result = self.api.call('worker.control_get', {'control_id': intent['control_id']})
        self.assertEqual(result['state'], 'complete'); self.assertEqual(len(effects), 1)
        self.assertEqual(result['result']['applied_seconds'], 900)
        self.assertEqual(calls[0], calls[1]); self.assertEqual(result['target'], TARGET)
        self.assertEqual(self.api.call('worker.extend', self.params), result)
        worker_api.reconcile(store, self.config, bridge=bridge, current=1100)
        self.assertEqual(len(calls), 2)

    def test_shutdown_has_no_cancellation_side_effect_and_rejection_is_terminal(self):
        intent = self.api.call('worker.shutdown', self.params)
        with self.store.transaction() as db:
            self.store.put(db, 'job', {'job_id': uid(), 'state': 'running'}, 'another')
        worker_api.reconcile(self.store, self.config,
            bridge=lambda config, action, params: applied(action, params, status='rejected',
                applied_seconds=0, reason='This exact generation has already closed'), current=1000)
        result = self.api.call('worker.control_get', {'control_id': intent['control_id']})
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['result']['status'], 'rejected')
        self.assertEqual(self.store.listing('job')[0]['state'], 'running')

    def test_foreign_receipt_remains_uncertain_without_retargeting(self):
        intent = self.api.call('worker.extend', self.params)
        worker_api.reconcile(self.store, self.config,
            bridge=lambda config, action, params: applied(action, params, session_id='e' * 32), current=1000)
        result = self.api.call('worker.control_get', {'control_id': intent['control_id']})
        self.assertEqual(result['state'], 'pending'); self.assertIsNone(result['result'])
        self.assertEqual(result['target'], TARGET)

    def test_concurrent_reconcilers_share_the_exact_command_lock(self):
        self.api.call('worker.extend', self.params)
        entered, release = threading.Event(), threading.Event()
        calls = []
        def bridge(config, action, params):
            calls.append(params); entered.set()
            self.assertTrue(release.wait(5))
            return applied(action, params)
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(worker_api.reconcile, self.store, self.config, bridge=bridge, current=1000)
            self.assertTrue(entered.wait(5))
            second = pool.submit(worker_api.reconcile, self.store, self.config, bridge=bridge, current=1000)
            second.result(timeout=5); release.set(); first.result(timeout=5)
        self.assertEqual(len(calls), 1)

    def test_client_cannot_choose_paths_intervals_modes_or_partial_generation(self):
        for params in [dict(self.params, seconds=9000), dict(self.params, mode='kill'),
                       dict(self.params, root='/elsewhere'), {**self.params, 'target': {'session_id': 'a' * 32}},
                       {**self.params, 'target': {**TARGET, 'session_id': '../other'}}]:
            with self.subTest(params=params), self.assertRaises(Error):
                self.api.call('worker.extend', params)
        self.assertEqual(self.store.listing('worker_control'), [])

    def test_cli_bridge_passes_only_fixed_arguments_and_limits_response(self):
        tools = Path(self.config['tools_dir']); (tools / 'msa').mkdir(parents=True)
        helper = tools / 'msa/session_client.py'
        helper.write_text("import json,sys\nprint(json.dumps({'argv':sys.argv[1:]}))\n")
        value = worker_api.invoke(self.config, 'extend', {'command_id': 'f' * 32, **TARGET})
        self.assertEqual(value['argv'][:5], ['worker-control', '--root', self.config['msa_sessions_root'], '--command-id', 'f' * 32])
        self.assertIn('--launch-sha256', value['argv'])
        helper.write_text("print('x'*65537)\n")
        with self.assertRaisesRegex(Error, 'exceeds'):
            worker_api.invoke(self.config, 'status')

    def test_daemon_finishes_pending_control_after_client_disconnect(self):
        from workbench.service import Daemon
        tools = Path(self.config['tools_dir']); (tools / 'msa').mkdir(parents=True)
        (tools / 'msa/session_client.py').write_text('''import json,sys
args=sys.argv[2:]; fields=dict(zip(args[::2],args[1::2]))
value={key.replace('--','').replace('-','_'):value for key,value in fields.items() if key!='--root'}
value.update(schema=1,status='applied',applied_seconds=900,reason='Applied exactly once',checked_epoch=1000)
print(json.dumps(value))
''')
        intent = self.api.call('worker.extend', self.params)
        # No connected API/client participates in this transition.
        daemon = Daemon(Store(self.root / 'state'), {**self.config, 'max_jobs': 1})
        self.assertEqual(daemon.tick()['errors'], [])
        result = self.api.call('worker.control_get', {'control_id': intent['control_id']})
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['result']['applied_seconds'], 900)
        self.assertEqual(self.store.listing('job'), [])
