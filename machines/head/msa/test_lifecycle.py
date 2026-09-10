"""No-cloud on-demand session lifecycle and real filesystem coordination tests."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import lifecycle
import session
import session_client as client


def silent(*args):
    pass


def fake_root_observer(root):
    pointer = lifecycle.document(root / 'active.json', optional=True)
    if pointer is None:
        return {'state': 'missing'}
    if not (root / 'published.json').exists():
        raise lifecycle.SessionError('session_uncertain', 'publication not complete')
    return {'state': 'ready', 'session_id': pointer['session_id'], 'ready': pointer['session_id']}


def publication_process(root, entered, release, queue):
    def start():
        ident = 'a' * 32
        session.atomic(root / 'active.json', {'session_id': ident}, exclusive=True)
        entered.set()
        if not release.wait(10):
            raise RuntimeError('test barrier timeout')
        session.atomic(root / 'published.json', {'ready': True}, exclusive=True)
        return {'session_id': ident}
    try:
        value = lifecycle.ensure(root, time.monotonic()+15, observe=lambda: fake_root_observer(root),
            start=start, retire=lambda _: None, progress=silent, interval=.01)
        queue.put(('ready', value))
    except Exception as exc:
        queue.put(('error', str(exc)))


def follower_process(root, queue):
    def forbidden():
        raise AssertionError('follower attempted duplicate allocation')
    try:
        value = lifecycle.ensure(root, time.monotonic()+15, observe=lambda: fake_root_observer(root),
            start=forbidden, retire=lambda _: None, progress=silent, interval=.01)
        queue.put(('ready', value))
    except Exception as exc:
        queue.put(('error', str(exc)))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sessions = self.root / 'sessions'
        self.tools = self.root / 'tools'
        names = ['msa/session.py', 'msa/session_client.py', 'msa/lifecycle.py', 'msa/panel.py',
                 'msa/prepared.py', 'msa/server.py', 'msa/databases.py', 'recipes/_common.sh', 'rf3/msa.py']
        for name in names:
            path = self.tools / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        self.submit = self.root / 'bio-submit'
        self.submit.write_text('fixture only')
        self.args = argparse.Namespace(root=self.sessions, tools=self.tools, timeout=300,
            session_timeout=7200, idle_seconds=900, warm='prefetch', worker=None, spot=False,
            model='rf3', name=None, fasta=None, json=self.root/'queries.json', bundle_result=None,
            require_session=False)
        self.args.json.write_text(json.dumps({'A': 'MAG'}))
        self.run_calls = []
        self.units = {}

    def launch(self, command, **kwargs):
        self.run_calls.append(command)
        state, intent = client.active(self.sessions)
        self.units[intent['unit']] = dict(LoadState='loaded', ActiveState='active', SubState='running',
            InvocationID='b'*32, MainPID='100', ControlPID='0',
            Description='Managed private MSA session '+intent['session_id'],
            ExecStart='{ path='+str(self.submit)+' ; argv[]='+' '.join(intent['argv'])+' ; ignore_errors=no ; }')
        return subprocess.CompletedProcess(command, 0, '', '')

    def start(self):
        with patch.object(client.shutil, 'which', return_value=str(self.submit)), \
             patch.object(client.subprocess, 'run', side_effect=self.launch):
            return client.start(self.args)

    def unit(self, name, **kwargs):
        return self.units.get(name, {'LoadState': 'not-found'})

    def registered(self):
        value = self.start()
        state, intent = client.active(self.sessions)
        launch = {'session_id': intent['session_id'], 'unit': intent['unit'], 'invocation_id': 'b'*32,
                  'instance': 'exact-worker', 'ip': '192.0.2.1', 'provider': {'os_id': 'exact-os'},
                  'remote_out': str(self.root/'out')}
        session.atomic(state/'launch.json', launch)
        return state, intent, launch

    def test_concurrent_callers_issue_one_shared_budgeted_launch(self):
        def observe():
            pointer = lifecycle.document(self.sessions/'active.json', optional=True)
            return ({'state':'ready','session_id':pointer['session_id'],'ready':pointer['session_id']}
                    if pointer else {'state':'missing'})
        def ensure():
            return lifecycle.ensure(self.sessions,time.monotonic()+10,observe=observe,
                start=lambda:client._start_locked(self.args),retire=lambda _:None,progress=silent,interval=.01)
        with patch.object(client.shutil,'which',return_value=str(self.submit)), \
             patch.object(client.subprocess,'run',side_effect=self.launch), ThreadPoolExecutor(8) as pool:
            values = list(pool.map(lambda _:ensure(),range(8)))
        self.assertEqual(len(set(values)),1)
        self.assertEqual(len(self.run_calls),1)
        command=self.run_calls[0]
        self.assertIn('--property=Restart=no',command)
        self.assertIn('--setenv=DC_MAX_INSTANCE_HOURLY=13.0',command)
        self.assertIn('--setenv=BIO_MSA_SESSION_IDLE_SECONDS=900',command)
        self.assertIn('--setenv=BIO_MSA_SESSION_WARM=prefetch',command)
        self.assertFalse(any('BIO_MSA_PROGRESS_LOG' in v for v in command))

    def test_process_follower_waits_for_inflight_active_pointer_publication(self):
        context=multiprocessing.get_context('fork')
        entered,release=context.Event(),context.Event()
        queue=context.Queue()
        leader=context.Process(target=publication_process,args=(self.sessions,entered,release,queue))
        follower=context.Process(target=follower_process,args=(self.sessions,queue))
        leader.start()
        try:
            self.assertTrue(entered.wait(5))
            follower.start()
            time.sleep(.25)
            self.assertTrue(follower.is_alive(),'follower prematurely classified partial startup as uncertain')
            release.set()
            results=[queue.get(timeout=10),queue.get(timeout=10)]
            self.assertEqual(results,[('ready','a'*32),('ready','a'*32)])
        finally:
            release.set()
            for child in [leader,follower]:
                if child.pid:
                    child.join(5)
                    if child.is_alive():child.terminate();child.join(5)
            queue.close()
        self.assertEqual(leader.exitcode,0)
        self.assertEqual(follower.exitcode,0)

    def test_starting_observation_pins_exact_invocation_and_rejects_replacement(self):
        self.start()
        with patch.object(client,'unit_state',side_effect=self.unit):
            result=client.observe_session(self.sessions)
            self.assertEqual(result['state'],'starting')
            state,intent=client.active(self.sessions)
            self.assertEqual(session.load(state/'observed-start.json')['invocation_id'],'b'*32)
            self.units[intent['unit']]['InvocationID']='c'*32
            with self.assertRaisesRegex(lifecycle.SessionError,'replaced'):
                client.observe_session(self.sessions)
        self.assertEqual(len(self.run_calls),1)

    def test_replaced_start_command_is_uncertain_not_joined(self):
        self.start();state,intent=client.active(self.sessions)
        self.units[intent['unit']]['ExecStart']='{ argv[]=/bin/false ; }'
        with patch.object(client,'unit_state',side_effect=self.unit),self.assertRaisesRegex(lifecycle.SessionError,'exact saved command'):
            client.observe_session(self.sessions)
        self.assertFalse((state/'observed-start.json').exists())

    def test_uncertain_start_reply_and_absent_unit_never_launch_again(self):
        def lost(command,**kwargs):
            self.run_calls.append(command)
            raise subprocess.TimeoutExpired(command,30)
        with patch.object(client.shutil,'which',return_value=str(self.submit)), \
             patch.object(client.subprocess,'run',side_effect=lost), \
             patch.object(client,'unit_state',return_value={'LoadState':'not-found'}):
            for _ in range(2):
                with self.assertRaises(lifecycle.SessionError) as caught:
                    client.ensure_session(self.args,time.monotonic()+5,progress=silent)
                self.assertEqual(caught.exception.code,'session_uncertain')
        self.assertEqual(len(self.run_calls),1)
        self.assertTrue((self.sessions/'active.json').exists())

    def test_known_ended_session_requires_exact_fresh_worker_and_os_absence(self):
        state,intent,launch=self.registered()
        with patch.object(client,'unit_state',return_value={'LoadState':'not-found'}):
            observed=client.observe_session(self.sessions)
            self.assertEqual(observed['state'],'terminal')
            bad={'status':'closed','instance':'different-worker','os_id':'exact-os',
                 'exact_worker_absent':True,'exact_os_absent_active_and_trash':True}
            with patch.object(client,'provider_check',return_value=bad),self.assertRaisesRegex(ValueError,'not confirmed'):
                client._retire_locked(self.sessions,observed)
            self.assertTrue((self.sessions/'active.json').exists())
            good={**bad,'instance':'exact-worker'}
            with patch.object(client,'provider_check',return_value=good) as provider:
                client._retire_locked(self.sessions,observed)
            self.assertEqual(provider.call_count,1)
        self.assertFalse((self.sessions/'active.json').exists())
        self.assertEqual(session.load(state/'closed.json')['proof'],good)

    def test_closed_receipt_alone_cannot_bypass_new_provider_check(self):
        state,_,_=self.registered()
        session.atomic(state/'closed.json',{'old_receipt':True})
        with patch.object(client,'unit_state',return_value={'LoadState':'not-found'}):
            observed=client.observe_session(self.sessions)
            with patch.object(client,'provider_check',side_effect=ValueError('still present')),self.assertRaisesRegex(ValueError,'still present'):
                client._retire_locked(self.sessions,observed)
        self.assertTrue((self.sessions/'active.json').exists())

    def test_live_or_replaced_unit_never_retired(self):
        _,intent,_=self.registered()
        with patch.object(client,'unit_state',return_value={'LoadState':'not-found'}):
            observed=client.observe_session(self.sessions)
        with patch.object(client,'unit_state',side_effect=self.unit),patch.object(client,'provider_check') as provider, \
             self.assertRaisesRegex(ValueError,'still live'):
            client._retire_locked(self.sessions,observed)
        provider.assert_not_called()

    def test_wait_has_bounded_deadline_without_replacement(self):
        clock=[0.]
        def sleep(seconds):clock[0]+=seconds
        with patch.object(client,'_start_locked') as start,self.assertRaises(lifecycle.SessionError) as caught:
            lifecycle.ensure(self.sessions,10,observe=lambda:{'state':'warming','session_id':'a'*32,'message':'warming'},
                start=start,retire=lambda _:None,progress=silent,clock=lambda:clock[0],sleep=sleep)
        self.assertEqual(caught.exception.code,'session_starting')
        self.assertEqual(clock[0],10)
        start.assert_not_called()

    def test_joined_generation_failure_is_not_restarted_by_same_call(self):
        values=iter([{'state':'warming','session_id':'a'*32,'message':'warming'},
                     {'state':'terminal','session_id':'a'*32}])
        with patch.object(client,'_start_locked') as start,self.assertRaises(lifecycle.SessionError) as caught:
            lifecycle.ensure(self.sessions,time.monotonic()+10,observe=lambda:next(values),
                start=start,retire=lambda _:None,progress=silent,sleep=lambda _:None)
        self.assertEqual(caught.exception.code,'session_failed')
        start.assert_not_called()

    def test_previous_terminal_generation_can_be_retired_then_replaced_once(self):
        value={'state':'terminal','session_id':'a'*32}
        starts=[];retired=[]
        def retire(old):retired.append(old['session_id']);value.clear();value['state']='missing'
        def start():
            starts.append('b'*32);value.update(state='ready',session_id='b'*32,ready='b'*32)
            return {'session_id':'b'*32}
        found=lifecycle.ensure(self.sessions,time.monotonic()+10,observe=lambda:dict(value),
            start=start,retire=retire,progress=silent)
        self.assertEqual(found,'b'*32);self.assertEqual(starts,['b'*32]);self.assertEqual(retired,['a'*32])

    def test_caller_cancellation_does_not_stop_shared_service(self):
        def cancelled(_):raise KeyboardInterrupt()
        with patch.object(client,'stop') as stop,self.assertRaises(KeyboardInterrupt):
            lifecycle.ensure(self.sessions,time.monotonic()+10,
                observe=lambda:{'state':'starting','session_id':'a'*32,'message':'waiting'},
                start=lambda:None,retire=lambda _:None,progress=silent,sleep=cancelled)
        stop.assert_not_called()

    def test_malformed_molecular_input_does_not_ensure_or_allocate(self):
        self.args.json.write_text(json.dumps({'A':'PEP*INVALID'}))
        with patch.object(client,'ensure_session') as ensure,patch.object(client.subprocess,'run') as run,self.assertRaisesRegex(ValueError,'per-chain'):
            client.prepare(self.args)
        ensure.assert_not_called();run.assert_not_called()

    def test_require_session_retains_explicit_no_start_behavior(self):
        self.args.require_session=True
        with patch.object(client,'ensure_session') as ensure,patch.object(client.subprocess,'run') as run,self.assertRaises(lifecycle.SessionError):
            client.prepare(self.args)
        ensure.assert_not_called();run.assert_not_called()

    def test_symlinked_registry_cannot_hide_another_registration(self):
        actual=self.root/'actual';actual.mkdir()
        link=self.root/'link';link.symlink_to(actual,target_is_directory=True)
        with self.assertRaises(lifecycle.SessionError):client.observe_session(link/'nested')
        self.assertFalse((actual/'nested').exists())

    def test_progress_is_private_bounded_identical_json_and_timestamped(self):
        path=self.root/'progress.log';path.touch(mode=0o600)
        stderr=io.StringIO()
        with patch.dict(os.environ,{'BIO_MSA_PROGRESS_LOG':str(path)}),redirect_stderr(stderr):
            lifecycle.validate_progress_log();lifecycle.emit('warming','Full index warm-up\nready soon','a'*32)
            lifecycle.emit('failed','\U0001f6a7'*1600,'a'*32,'session_uncertain')
        self.assertEqual(path.read_text(),stderr.getvalue())
        for line in path.read_text().splitlines():
            self.assertLessEqual(len((line+'\n').encode()),4096)
            body=json.loads(line.split(' ',2)[2])
            self.assertGreater(body['timestamp_ns'],0)
            self.assertNotIn('\n',body['message'])

    def test_progress_rejects_symlink_and_public_mode_before_startup(self):
        real=self.root/'real.log';real.touch(mode=0o600)
        link=self.root/'link.log';link.symlink_to(real)
        for path in [link,real]:
            if path==real:real.chmod(0o644)
            with patch.dict(os.environ,{'BIO_MSA_PROGRESS_LOG':str(path)}),patch.object(client,'ensure_session') as ensure,self.assertRaises(lifecycle.SessionError):
                client.prepare(self.args)
            ensure.assert_not_called()

    def test_progress_never_exceeds_bound(self):
        path=self.root/'progress.log';path.write_bytes(b'x'*(lifecycle.MAX_PROGRESS-20));path.chmod(0o600)
        with patch.dict(os.environ,{'BIO_MSA_PROGRESS_LOG':str(path)}),redirect_stderr(io.StringIO()):
            lifecycle.emit('waiting','Queued private search')
        self.assertLessEqual(path.stat().st_size,lifecycle.MAX_PROGRESS)

    def test_new_source_pin_includes_lifecycle_while_legacy_snapshot_stays_valid(self):
        before=session.sources(self.tools)
        self.assertIn('msa/lifecycle.py',before)
        (self.tools/'msa/lifecycle.py').unlink()
        legacy=session.sources(self.tools)
        self.assertEqual(legacy,{k:v for k,v in before.items() if k!='msa/lifecycle.py'})

    def test_provider_wait_uses_remaining_request_deadline(self):
        with patch.object(client.subprocess,'check_output',return_value='{}') as call:
            client.provider_check(self.tools,'worker','192.0.2.1',deadline=time.monotonic()+.5)
        self.assertGreater(call.call_args.kwargs['timeout'],0)
        self.assertLessEqual(call.call_args.kwargs['timeout'],.5)

    def test_existing_ready_session_is_reused_without_another_start(self):
        state,intent,launch=self.registered()
        session.atomic(Path(launch['remote_out'])/'session-ready.json',{'placeholder':True})
        ready=(state,intent,launch,{'session_id':intent['session_id']},'d'*64)
        with patch.object(client,'unit_state',side_effect=self.unit), \
             patch.object(client,'ready_session',return_value=ready),patch.object(client,'_start_locked') as start:
            self.assertEqual(client.ensure_session(self.args,time.monotonic()+5,progress=silent),ready)
        start.assert_not_called()

    def test_startup_time_is_removed_from_search_timeout_and_success_emits_ready(self):
        state,intent,launch=self.registered()
        clock=[0.];sent=[]
        ready={'session_id':intent['session_id'],'created_epoch':1000,'deadline_epoch':10000,
               'tools':str(self.tools),'state':'/tmp/fixture-session'}
        def ensure(args,deadline):
            self.assertEqual(deadline,300)
            clock[0]=120
            return state,intent,launch,ready,'d'*64
        def run(command,**kwargs):
            if 'input' not in kwargs:return subprocess.CompletedProcess(command,0,b'',b'')
            request=json.loads(kwargs['input']);sent.append(request)
            bundle=Path(launch['remote_out'])/'requests'/request['request_id']/'prepared'
            session.atomic(bundle/'search.json',{'fixture':True})
            response={'status':'complete','request_id':request['request_id'],
                      'request_sha256':session.request_document(request,ready,'d'*64),'ready_sha256':'d'*64,
                      'bundle':str(bundle),'bundle_manifest_sha256':session.sha(bundle/'search.json')}
            return subprocess.CompletedProcess(command,0,session.canonical(response),b'')
        with patch.object(client.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(client.time,'time',side_effect=lambda:1000+clock[0]), \
             patch.object(client,'ensure_session',side_effect=ensure),patch.object(client,'ssh',return_value=['fixture-ssh']), \
             patch.object(client.subprocess,'run',side_effect=run),patch.object(lifecycle,'emit') as emit:
            receipt=client.prepare(self.args)
        self.assertEqual(sent[0]['timeout_seconds'],180)
        self.assertEqual(sent[0]['deadline_epoch'],1300)
        self.assertEqual([c.args[0] for c in emit.call_args_list],['waiting','ready'])
        self.assertEqual(receipt['request_id'],sent[0]['request_id'])

    def test_require_session_readiness_gets_the_same_absolute_deadline(self):
        self.args.require_session=True
        with patch.object(client.time,'monotonic',return_value=100), \
             patch.object(client,'ready_session',side_effect=lifecycle.SessionError('session_missing','absent')) as ready, \
             self.assertRaises(lifecycle.SessionError):
            client.prepare(self.args)
        self.assertEqual(ready.call_args.kwargs['deadline'],400)


if __name__=='__main__':unittest.main()
