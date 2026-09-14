"""AWS routing and persistent-worker closure, without cloud or SSH operations."""
import argparse
from contextlib import nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import provider
import search_profile
import session
import session_client as client


def running():
    return dict(provider='aws', region='us-east-1', account='147997164104',
        instance='i-0123456789abcdef0', ip='192.0.2.1', os_id='vol-0123456789abcdef0',
        db_volume_id='vol-0123456789abcdef1', token='a'*32, hostname='bio-aws-fixture',
        checked_epoch=time.time(), reservation_deadline=time.time()+7200, budget={})


def stopped(proof):
    return dict(proof, status='closed', compute_stopped=True, instance_state='stopped',
                budget_released=True, checked_epoch=time.time())


class ProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.helper = self.root/'bio-aws-msa'; self.helper.write_text('fixture helper')
        self.intent = dict(provider_name='aws', provider_helper=str(self.helper),
            provider_helper_sha256=provider.sha(self.helper), tools=str(self.root/'tools'), session_id='b'*32)

    def test_existing_session_provider_never_changes_with_configuration(self):
        with mock.patch.dict(os.environ, BIO_MSA_PROVIDER='aws'):
            self.assertEqual(provider.selected(), 'aws')
            self.assertEqual(provider.name({}), 'verda')
            self.assertEqual(provider.name(self.intent), 'aws')
        with mock.patch.dict(os.environ, BIO_MSA_PROVIDER='invalid'), self.assertRaises(ValueError):
            provider.selected()

    def test_frozen_helper_change_blocks_before_subprocess(self):
        self.helper.write_text('replacement executable')
        with mock.patch.object(provider.subprocess, 'run') as run, self.assertRaises(ValueError):
            provider.invoke(self.intent, self.root, 'provider-close')
        run.assert_not_called()

    def test_exact_request_sync_has_no_launch_or_arbitrary_path_argument(self):
        reply = dict(provider='aws', status='synchronized', session_id='b'*32, request_id='c'*32)
        with mock.patch.object(provider.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(reply).encode(), b'')) as run:
            self.assertEqual(client.sync_output(self.root, self.intent, request_id='c'*32), None)
        self.assertEqual(run.call_args.args[0], [str(self.helper), 'sync-output', '--state', str(self.root),
                                               '--request-id', 'c'*32])
        for request_id in ('../another', 'd'*31, 'C'*32):
            with mock.patch.object(provider.subprocess, 'run') as run, self.assertRaises(ValueError):
                provider.invoke(self.intent, self.root, 'sync-output', request_id=request_id)
            run.assert_not_called()

    def test_sync_uncertainty_or_other_request_never_becomes_success(self):
        for reply in (dict(provider='aws', status='synchronized', session_id='x'*32, request_id=None),
                      dict(provider='aws', status='queued', session_id='b'*32, request_id=None)):
            with mock.patch.object(provider, 'invoke', return_value=reply), self.assertRaises(ValueError):
                client.sync_output(self.root, self.intent)
        with mock.patch.object(provider.subprocess, 'run', side_effect=subprocess.CalledProcessError(
                1, 'helper', stderr=b'private credential diagnostic')):
            with self.assertRaisesRegex(ValueError, 'registration is retained') as error:
                provider.invoke(self.intent, self.root, 'sync-output')
        self.assertNotIn('credential', str(error.exception))

    def test_running_proof_requires_exact_storage_reservation_scope_and_freshness(self):
        proof = running(); launch = dict(instance=proof['instance'], ip=proof['ip'], provider=deepcopy(proof))
        self.assertEqual(provider.check_proof(proof, launch), proof)
        for change in (dict(instance='i-1123456789abcdef0'), dict(os_id='vol-1123456789abcdef0'),
                       dict(db_volume_id='vol-1123456789abcdef1'), dict(token='b'*32),
                       dict(account='247997164104'), dict(region='us-west-2'),
                       dict(checked_epoch=time.time()-121), dict(reservation_deadline=time.time()+59)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                provider.check_proof(dict(proof, **change), launch)

    def test_stopped_receipt_requires_released_compute_and_retained_exact_disks(self):
        prior = running(); launch = dict(instance=prior['instance'], provider=prior)
        closed = stopped(prior)
        self.assertEqual(provider.closed(closed, launch), closed)
        self.assertNotIn('exact_worker_absent', closed)
        for change in (dict(compute_stopped=False), dict(budget_released=False), dict(instance_state='stopping'),
                       dict(os_id='vol-1123456789abcdef0'), dict(db_volume_id='vol-1123456789abcdef1'),
                       dict(token='b'*32), dict(checked_epoch=time.time()-121)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                provider.closed(dict(closed, **change), launch)
        for field in ('os_id', 'db_volume_id', 'token', 'account', 'region'):
            broken = deepcopy(launch); broken['provider'].pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                provider.closed(closed, broken)

    def test_verda_absence_is_not_an_aws_stop_receipt(self):
        prior = running(); launch = dict(instance=prior['instance'], provider=prior)
        with self.assertRaises(ValueError):
            client.closure_proof(self.intent, launch, dict(status='closed', exact_worker_absent=True,
                exact_os_absent_active_and_trash=True, instance=prior['instance'], os_id=prior['os_id']))
        with self.assertRaises(ValueError):
            client.closure_proof({}, launch, stopped(prior))


class AwsSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tools = self.root/'source-tools'; self.tools.mkdir()
        for name in ('msa/session.py', 'msa/session_client.py', 'msa/panel.py', 'msa/prepared.py',
                     'msa/server.py', 'msa/databases.py', 'recipes/_common.sh', 'rf3/msa.py'):
            path = self.tools/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(name)
        for module in (search_profile, provider):
            (self.tools/'msa'/Path(module.__file__).name).write_bytes(Path(module.__file__).read_bytes())
        self.helper = self.root/'bio-aws-msa'; self.helper.write_text('fixture')
        self.args = argparse.Namespace(root=self.root/'sessions', tools=self.tools, timeout=7200,
            idle_seconds=900, warm=None, worker=None, spot=False, provider='aws')

    def start(self):
        with mock.patch.object(provider.shutil, 'which', return_value=str(self.helper)), \
             mock.patch.object(client.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            result = client.start(self.args)
        state = Path(result['state'])
        return state, session.load(state/'intent.json'), run.call_args.args[0]

    def test_aws_start_freezes_resident_policy_and_calls_only_managed_provider(self):
        with mock.patch.dict(os.environ, BIO_MSA_SEARCH_PROFILE=search_profile.MAPPED_PROFILE):
            state, intent, command = self.start()
        self.assertEqual(intent['provider_name'], 'aws')
        self.assertEqual(intent['provider_helper_sha256'], provider.sha(self.helper))
        self.assertEqual(intent['search_profile'], search_profile.resolve(search_profile.LEGACY_PROFILE))
        self.assertEqual(intent['idle_seconds'], 900); self.assertEqual(intent['warm'], 'prefetch')
        self.assertIn('msa/provider.py', intent['sources'])
        self.assertEqual(command[-6:], [str(self.helper), 'launch-session', '--state', str(state),
                                       '--tools', str(state/'tools')])
        self.assertIn('--setenv=BIO_MSA_PROVIDER=aws', command)
        self.assertIn('--setenv=BIO_MSA_SEARCH_PROFILE=resident-768gib-v1', command)
        self.assertNotIn('--spot', command)

    def test_unsupported_profiles_policies_or_workers_fail_before_launch(self):
        original = vars(self.args).copy()
        for key, value in (('search_profile', search_profile.MAPPED_PROFILE), ('warm', 'report'),
                           ('idle_seconds', 60), ('spot', True), ('worker', '1H100.80S.32V'),
                           ('worker', 'x2idn.16xlarge')):
            self.args = argparse.Namespace(**{**original, key: value})
            with self.subTest(key=key), mock.patch.object(provider.shutil, 'which', return_value=str(self.helper)), \
                 mock.patch.object(client.subprocess, 'run') as run, self.assertRaises(ValueError):
                client.start(self.args)
            run.assert_not_called(); self.assertFalse((self.args.root/'active.json').exists())

    def test_terminal_aws_registration_retires_only_after_fresh_exact_stop(self):
        state, intent, _ = self.start(); prior = running()
        launch = dict(instance=prior['instance'], ip=prior['ip'], invocation_id='d'*32, provider=prior)
        session.atomic(state/'launch.json', launch)
        observed = dict(session_id=intent['session_id'], intent_sha256=session.sha(state/'intent.json'),
                        launch_sha256=session.sha(state/'launch.json'))
        live = dict(LoadState='not-found')
        with mock.patch.object(client, 'unit_state', return_value=live), \
             mock.patch.object(provider, 'invoke', return_value=stopped(prior)) as invoke:
            client._retire_locked(self.args.root, observed)
        self.assertEqual(invoke.call_args.args[2], 'provider-close')
        self.assertTrue(session.load(state/'closed.json')['proof']['compute_stopped'])
        self.assertFalse((self.args.root/'active.json').exists())

    def test_uncertain_or_changed_aws_storage_keeps_pointer_for_recovery(self):
        state, intent, _ = self.start(); prior = running()
        launch = dict(instance=prior['instance'], ip=prior['ip'], invocation_id='d'*32, provider=prior)
        session.atomic(state/'launch.json', launch)
        observed = dict(session_id=intent['session_id'], intent_sha256=session.sha(state/'intent.json'),
                        launch_sha256=session.sha(state/'launch.json'))
        with mock.patch.object(client, 'unit_state', return_value=dict(LoadState='not-found')), \
             mock.patch.object(provider, 'invoke', return_value=dict(stopped(prior), budget_released=False)), \
             self.assertRaises(ValueError):
            client._retire_locked(self.args.root, observed)
        self.assertTrue((self.args.root/'active.json').exists()); self.assertFalse((state/'closed.json').exists())

    def prepared_case(self, tamper=False):
        state, intent, _ = self.start(); proof = running()
        launch = dict(instance=proof['instance'], ip=proof['ip'], provider=proof, remote_out=str(self.root/'out'))
        session.atomic(state/'launch.json', launch)
        ready = dict(session_id=intent['session_id'], created_epoch=time.time()-10,
            deadline_epoch=time.time()+7200, tools='/frozen/tools', state='/private/session')
        fasta = self.root/'query.fasta'; fasta.write_text('>one\nACDEFG\n')
        args = argparse.Namespace(**{**vars(self.args), 'timeout':300, 'require_session':True,
            'name':'one', 'fasta':fasta, 'json':None, 'model':'boltz2', 'bundle_result':None})
        digest = 'd'*64; ident = 'c'*32; calls = []; manifest_bytes = b'{"schema":1}'
        bundle = Path(launch['remote_out'])/'requests'/ident/'prepared'
        def native(command, **kwargs):
            calls.append('search' if 'input' in kwargs else 'validate')
            if 'input' not in kwargs:
                self.assertTrue((bundle/'manifest.json').is_file())
                self.assertIn('validate', command)
                return subprocess.CompletedProcess(command, 0)
            self.assertFalse(bundle.exists())
            document = json.loads(kwargs['input'])
            response = dict(status='complete', request_id=ident, ready_sha256=digest,
                request_sha256=session.hashlib.sha256(kwargs['input']).hexdigest(), bundle=str(bundle),
                bundle_manifest_sha256=session.hashlib.sha256(manifest_bytes).hexdigest())
            self.assertEqual(document['sequence'], 'ACDEFG')
            return subprocess.CompletedProcess(command, 0, session.canonical(response), b'')
        def pull(*arguments, **kwargs):
            self.assertEqual(kwargs['request_id'], ident)
            self.assertEqual(arguments[2], 'sync-output')
            calls.append('pull'); bundle.mkdir(parents=True)
            (bundle/'manifest.json').write_bytes(b'tampered' if tamper else manifest_bytes)
            return dict(provider='aws', status='synchronized', session_id=intent['session_id'], request_id=ident)
        with mock.patch.object(client, 'ready_session', return_value=(state,intent,launch,ready,digest)), \
             mock.patch.object(client, 'ssh', return_value=['ssh-fixture']), \
             mock.patch.object(client.uuid, 'uuid4', return_value=argparse.Namespace(hex=ident)), \
             mock.patch.object(session, 'startup_activity', return_value=nullcontext()), \
             mock.patch.object(client.lifecycle, 'emit'), \
             mock.patch.object(client.subprocess, 'run', side_effect=native), \
             mock.patch.object(provider, 'invoke', side_effect=pull):
            if tamper:
                with self.assertRaisesRegex(ValueError, 'manifest differs'): client.prepare(args)
            else:
                self.assertEqual(client.prepare(args)['request_id'], ident)
        return state/'requests'/ident, calls

    def test_completed_request_is_pulled_before_original_native_validation(self):
        request, calls = self.prepared_case()
        self.assertEqual(calls, ['search', 'pull', 'validate'])
        self.assertTrue((request/'complete.json').is_file())
        self.assertFalse((request/'failure.json').exists())

    def test_changed_download_is_retained_without_repeating_native_search(self):
        request, calls = self.prepared_case(tamper=True)
        self.assertEqual(calls, ['search', 'pull'])
        self.assertTrue((request/'failure.json').is_file())
        self.assertFalse((request/'complete.json').exists())


if __name__ == '__main__':
    unittest.main()
