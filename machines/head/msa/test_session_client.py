import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import session
import session_client as client


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.tools = self.root/"source-tools"; self.tools.mkdir()
        for name in ["msa/session.py", "msa/session_client.py", "msa/panel.py", "msa/prepared.py", "msa/server.py",
                     "msa/databases.py", "recipes/_common.sh", "rf3/msa.py"]:
            path = self.tools/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(name)
        self.submit = self.root/"bio-submit"; self.submit.write_text("fixture")
        self.args = argparse.Namespace(root=self.root/"sessions", tools=self.tools, timeout=300,
            idle_seconds=60, warm="report", worker=None, spot=False)

    def tearDown(self): self.tmp.cleanup()

    def test_start_routes_only_through_budgeted_submit_and_freezes_sources(self):
        with mock.patch.object(client.shutil, "which", return_value=str(self.submit)), \
             mock.patch.object(client.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as run:
            result = client.start(self.args)
        state = Path(result["state"]); intent = session.load(state/"intent.json")
        self.assertEqual(intent["sources"], session.sources(state/"tools"))
        command = run.call_args.args[0]
        self.assertIn("--property=RuntimeMaxSec=3300", command)
        self.assertIn("--setenv=BIO_MSA_CAPACITY_WAIT_SECONDS=1800", command)
        self.assertEqual(intent['timeout_seconds'], 300)
        self.assertEqual(intent['capacity_wait_seconds'], 1800)
        self.assertIn("--setenv=DC_MAX_INSTANCE_HOURLY=13.0", command)
        self.assertEqual(command[-6:], [str(self.submit), "msa", "--sub", "session", "--timeout", "300"])
        self.tools.joinpath("msa/session.py").write_text("changed later")
        self.assertNotEqual(session.sources(self.tools), intent["sources"])
        self.assertEqual(session.sources(state/"tools"), intent["sources"])

    def test_uncertain_start_is_not_repeated(self):
        with mock.patch.object(client.shutil, "which", return_value=str(self.submit)), \
             mock.patch.object(client.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 30)) as run:
            with self.assertRaises(subprocess.TimeoutExpired): client.start(self.args)
            with self.assertRaisesRegex(ValueError, "registration exists"): client.start(self.args)
        self.assertEqual(run.call_count, 1)
        state, _ = client.active(self.args.root)
        self.assertTrue((state/"start-intent.json").exists())
        self.assertFalse((state/"start-result.json").exists())

    def test_invalid_hourly_limit_cannot_launch(self):
        with mock.patch.dict(os.environ, {"DC_MAX_INSTANCE_HOURLY":"nan"}), \
             mock.patch.object(client.shutil, "which", return_value=str(self.submit)), \
             mock.patch.object(client.subprocess, "run") as run, self.assertRaises(ValueError):
            client.start(self.args)
        run.assert_not_called()

    def test_capacity_wait_setting_does_not_extend_paid_worker_time(self):
        self.args.capacity_wait_seconds = 120
        with mock.patch.object(client.shutil, 'which', return_value=str(self.submit)), \
             mock.patch.object(client.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            client.start(self.args)
        command = run.call_args.args[0]
        self.assertIn('--property=RuntimeMaxSec=1620', command)
        self.assertIn('--setenv=BIO_MSA_CAPACITY_WAIT_SECONDS=120', command)
        self.assertEqual(command[-2:], ['--timeout', '300'])

    def test_invalid_capacity_window_fails_before_registering_or_starting(self):
        for value in (-1, 7201, True, float('nan'), 'not-a-number'):
            with self.subTest(value=value):
                self.args.capacity_wait_seconds = value
                with mock.patch.object(client.subprocess, 'run') as run, self.assertRaises(ValueError):
                    client.start(self.args)
                run.assert_not_called()
                self.assertFalse((self.args.root/'active.json').exists())

    def test_prepare_without_active_session_never_launches_compute(self):
        with mock.patch.object(client.subprocess, "run") as run, self.assertRaises(ValueError):
            client.ready_session(self.args.root)
        run.assert_not_called()

    def test_active_registration_hash_change_is_rejected(self):
        with mock.patch.object(client.shutil, "which", return_value=str(self.submit)), \
             mock.patch.object(client.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            result = client.start(self.args)
        state = Path(result["state"]); value = session.load(state/"intent.json"); value["timeout_seconds"] += 1
        session.atomic(state/"intent.json", value)
        with self.assertRaisesRegex(ValueError, "intent changed"): client.active(self.args.root)

    def registered(self,borrowed=False):
        with mock.patch.object(client.shutil,"which",return_value=str(self.submit)),mock.patch.object(client.subprocess,"run",return_value=subprocess.CompletedProcess([],0,"","")):
            result=client.start(self.args)
        state=Path(result['state']);intent=session.load(state/'intent.json')
        launch=dict(instance='worker-fixture',ip='192.0.2.1',invocation_id='b'*32,provider=dict(os_id='os-fixture'),remote_out=str(self.root/'out'))
        if borrowed:
            intent['lifecycle']='borrowed-api';launch['worker_session']=dict(unit='bio-msa-session-fixture.service',invocation_id='c'*32)
            session.atomic(state/'intent.json',intent);session.atomic(self.args.root/'active.json',dict(session_id=intent['session_id'],intent_sha256=session.sha(state/'intent.json')))
        session.atomic(state/'launch.json',launch)
        return state,intent,launch

    def test_expired_collected_unit_closes_only_after_fresh_exact_absence(self):
        state,intent,launch=self.registered()
        with mock.patch.object(client,'unit_state',return_value=dict(LoadState='not-found')), \
             mock.patch.object(client,'provider_check',return_value=dict(status='closed',exact_worker_absent=True,exact_os_absent_active_and_trash=True))as provider, \
             mock.patch.object(client.subprocess,'run')as run:
            result=client.stop(self.args.root)
        run.assert_not_called();self.assertEqual(result['status'],'closed')
        self.assertEqual(provider.call_args.args[-1],'os-fixture');self.assertFalse((self.args.root/'active.json').exists())
        self.assertTrue((state/'stop-intent.json').exists());self.assertTrue((state/'closed.json').exists())

    def test_uncertain_resource_cleanup_keeps_registration(self):
        state,_,_=self.registered()
        with mock.patch.object(client,'unit_state',return_value=dict(LoadState='not-found')), \
             mock.patch.object(client,'provider_check',side_effect=ValueError('OS still present')),self.assertRaisesRegex(ValueError,'OS still present'):
            client.stop(self.args.root)
        self.assertTrue((self.args.root/'active.json').exists());self.assertFalse((state/'closed.json').exists())

    def test_borrowed_stop_targets_only_spool_unit_and_preserves_original_owner(self):
        state,intent,launch=self.registered(borrowed=True)
        session.atomic(Path(launch['remote_out'])/'session-closed.json',dict(session_id=intent['session_id'],borrowed_api_preserved=True))
        observed='LoadState=loaded\nInvocationID='+('c'*32)+'\nMainPID=444\nControlPID=0\n'
        with mock.patch.object(client,'ssh',return_value=['ssh','fixture']), \
             mock.patch.object(client.subprocess,'check_output',return_value=observed), \
             mock.patch.object(client.subprocess,'run')as run, mock.patch.object(client,'provider_check')as provider:
            result=client.stop(self.args.root)
        self.assertTrue(result['borrowed_api_preserved']);provider.assert_not_called()
        self.assertEqual(run.call_args.args[0],['ssh','fixture','systemctl stop bio-msa-session-fixture.service'])
        self.assertNotIn(intent['unit'],run.call_args.args[0][-1]);self.assertFalse((self.args.root/'active.json').exists())

    def test_removed_borrowed_worker_reconciles_only_with_exact_permanent_absence(self):
        state,intent,launch=self.registered(borrowed=True)
        proof=dict(status='closed',instance=launch['instance'],os_id=launch['provider']['os_id'],
                   exact_worker_absent=True,exact_os_absent_active_and_trash=True)
        with mock.patch.object(client,'ssh',return_value=['ssh','fixture']), \
             mock.patch.object(client.subprocess,'check_output',side_effect=subprocess.CalledProcessError(255,'ssh')), \
             mock.patch.object(client,'provider_check',return_value=proof)as provider, \
             mock.patch.object(client.subprocess,'run')as run:
            result=client.stop(self.args.root)
        run.assert_not_called();self.assertEqual(provider.call_args.args[-1],'os-fixture')
        self.assertTrue(result['original_worker_already_removed']);self.assertFalse(result['borrowed_api_preserved'])
        self.assertFalse((self.args.root/'active.json').exists())
        self.assertEqual(session.load(state/'closed.json')['proof']['provider'],proof)

    def test_unreachable_borrowed_worker_keeps_registration_if_cleanup_uncertain(self):
        state,_,_=self.registered(borrowed=True)
        with mock.patch.object(client,'ssh',return_value=['ssh','fixture']), \
             mock.patch.object(client.subprocess,'check_output',side_effect=subprocess.TimeoutExpired('ssh',20)), \
             mock.patch.object(client,'provider_check',side_effect=ValueError('OS still present')), \
             mock.patch.object(client.subprocess,'run')as run, self.assertRaisesRegex(ValueError,'OS still present'):
            client.stop(self.args.root)
        run.assert_not_called();self.assertTrue((self.args.root/'active.json').exists())
        self.assertFalse((state/'closed.json').exists());self.assertFalse((state/'stop-intent.json').exists())


if __name__ == "__main__": unittest.main()
