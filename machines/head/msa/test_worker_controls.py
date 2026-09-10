import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import head_controls as head
import session
import session_client as client
import worker_controls as controls


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        (self.root/'state').mkdir();(self.root/'out').mkdir()
        self.now=time.time()
        self.ready={'session_id':'a'*32,'state':str(self.root/'state'),'output':str(self.root/'out'),
            'owner':{'boot_id':'fixture','pid':123},'created_epoch':self.now,'deadline_epoch':self.now+7200,
            'idle_seconds':900,'lifecycle':'owned-api','controls_version':1}
        self.digest='b'*64
        controls.initialize(self.ready,self.digest)

    def request(self,action='extend',ident='c'*32):
        return dict(schema=1,command_id=ident,action=action,session_id='a'*32,invocation_id='d'*32,
                    intent_sha256='e'*64,launch_sha256='f'*64)

    def update(self,**kwargs):
        with controls.locked(self.ready['state']):return controls.update_locked(self.ready,self.digest,**kwargs)

    def test_concurrent_exact_replay_applies_one_extension(self):
        request=self.request()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:controls.apply(self.ready,self.digest,request,self.now),range(8)))
        self.assertTrue(all(value==results[0] for value in results))
        self.assertEqual(results[0]['applied_seconds'],900)
        self.assertEqual(results[0]['idle_deadline_epoch'],self.now+1800)
        self.assertEqual(controls.checked(self.ready,self.digest)['revision'],1)
        with self.assertRaisesRegex(ValueError,'different payload'):
            controls.apply(self.ready,self.digest,self.request('shutdown'))

    def test_busy_credit_applies_after_work_without_extending_hard_lifetime(self):
        self.update(busy=True,queued=1,active='job')
        result=controls.apply(self.ready,self.digest,self.request(),self.now)
        self.assertEqual(result['idle_credit_seconds'],900)
        self.assertIsNone(result['idle_deadline_epoch'])
        value,close=self.update(busy=False,queued=0,active=None,now=self.now+1200)
        self.assertFalse(close);self.assertEqual(value['idle_deadline_epoch'],self.now+3000)
        self.assertEqual(value['hard_deadline_epoch'],self.ready['deadline_epoch'])

    def test_partial_extension_is_rejected_and_idempotent(self):
        value=controls.checked(self.ready,self.digest)
        value['idle_deadline_epoch']=value['hard_deadline_epoch']-60-100
        controls.atomic(controls.path(self.ready),value)
        result=controls.apply(self.ready,self.digest,self.request(),self.now)
        self.assertEqual(result['status'],'rejected');self.assertEqual(result['applied_seconds'],0)
        self.assertFalse(result['can_extend'])
        self.assertEqual(result,controls.apply(self.ready,self.digest,self.request(),self.now+100))

    def test_drain_preserves_accepted_work_and_closes_when_empty(self):
        self.update(busy=True,queued=1,active='job')
        result=controls.apply(self.ready,self.digest,self.request('shutdown'),self.now)
        self.assertEqual(result['state'],'closing');self.assertIsNone(result['shutdown_epoch'])
        _,close=self.update(busy=False,queued=1,active=None);self.assertFalse(close)
        value,close=self.update(busy=False,queued=0,active=None);self.assertTrue(close)
        request=dict(schema=1,request_id='1'*32,session_id='a'*32,ready_sha256=self.digest,model='protenix',
            name='target',sequence='ACDE',timeout_seconds=120,deadline_epoch=self.now+120)
        with mock.patch.object(session,'check_ready',return_value=self.ready),self.assertRaisesRegex(ValueError,'draining'):
            session.submit(Path(self.ready['state']),self.digest,request,0)
        self.assertFalse((Path(self.ready['state'])/'requests'/('1'*32+'.json')).exists())

    def test_idle_shutdown_is_immediate_and_borrowed_control_rejected(self):
        result=controls.apply(self.ready,self.digest,self.request('shutdown'),self.now)
        self.assertEqual(result['shutdown_epoch'],self.now)
        self.ready['lifecycle']='borrowed-api'
        result=controls.apply(self.ready,self.digest,self.request('extend','2'*32),self.now)
        self.assertEqual(result['status'],'rejected');self.assertIn('Borrowed',result['reason'])

    def test_committed_write_with_lost_reply_replays_without_double_extension(self):
        original=controls.atomic
        def lost(path,value):original(path,value);raise OSError('lost reply after rename')
        with mock.patch.object(controls,'atomic',side_effect=lost),self.assertRaises(OSError):
            controls.apply(self.ready,self.digest,self.request(),self.now)
        replay=controls.apply(self.ready,self.digest,self.request(),self.now+1)
        self.assertEqual(replay['idle_deadline_epoch'],self.now+1800)
        self.assertEqual(replay['control_revision'],1)

    def test_new_request_racing_drain_is_either_retained_or_rejected(self):
        import threading
        barrier=threading.Barrier(2)
        request=dict(schema=1,request_id='1'*32,session_id='a'*32,ready_sha256=self.digest,model='protenix',
            name='target',sequence='ACDE',timeout_seconds=120,deadline_epoch=self.now+120)
        def submit():
            barrier.wait(timeout=2)
            try:session.submit(Path(self.ready['state']),self.digest,request,0)
            except ValueError as error:return str(error)
        def shutdown():
            barrier.wait(timeout=2)
            return controls.apply(self.ready,self.digest,self.request('shutdown'))
        with mock.patch.object(session,'check_ready',return_value=self.ready),concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(submit);b=pool.submit(shutdown);error=a.result();receipt=b.result()
        retained=(Path(self.ready['state'])/'requests'/('1'*32+'.json')).exists()
        self.assertEqual(receipt['status'],'applied')
        self.assertIn('pending' if retained else 'draining',error)
        self.assertEqual(controls.checked(self.ready,self.digest)['queued_requests'],int(retained))

    def test_expired_idle_lease_cannot_be_revived(self):
        result=controls.apply(self.ready,self.digest,self.request(),self.now+901)
        self.assertEqual(result['status'],'rejected');self.assertEqual(result['applied_seconds'],0)
        self.assertIn('expired',result['reason'])


class HeadControlTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ControlTests(methodName='runTest');self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root/'registered';self.root.mkdir()
        self.ready=self.fixture.ready;self.digest=self.fixture.digest
        self.ready['tools']=str(self.fixture.root/'tools')
        self.state=self.root/('a'*32);self.state.mkdir()
        self.intent={'session_id':'a'*32,'unit':'bio-msa-session-'+('a'*32)+'.service','tools':str(self.state/'tools')}
        session.atomic(self.state/'intent.json',self.intent)
        self.launch={'session_id':'a'*32,'invocation_id':'d'*32,'unit':self.intent['unit'],
            'remote_out':self.ready['output'],'instance':'fixture-worker','ip':'192.0.2.1','boot_id':'fixture',
            'provider':{'reservation_deadline':self.ready['deadline_epoch']},'known_hosts':str(self.state/'known_hosts')}
        (self.state/'known_hosts').write_text('fixture key');self.launch['known_hosts_sha256']=session.sha(self.state/'known_hosts')
        session.atomic(self.state/'launch.json',self.launch)
        session.atomic(self.root/'active.json',{'session_id':'a'*32,'intent_sha256':session.sha(self.state/'intent.json')})
        self.params={'command_id':'c'*32,**head.target(self.state,self.intent,self.launch)}
        session.atomic(Path(self.ready['output'])/'session-ready.json',self.ready)
        status=controls.snapshot(controls.checked(self.ready,self.digest))
        session.atomic(Path(self.ready['output'])/'worker-status.json',dict(schema=1,session_id='a'*32,
            ready_sha256=self.digest,owner=self.ready['owner'],**status))
        self.patches=[mock.patch.object(head,'ready_binding',return_value=(self.ready,self.digest)),
            mock.patch.object(client,'unit_state',return_value={'LoadState':'loaded','ActiveState':'active','InvocationID':'d'*32})]
        for patcher in self.patches:patcher.start();self.addCleanup(patcher.stop)

    def test_actual_idle_status_has_bound_shutdown_epoch_and_legacy_has_no_invented_timer(self):
        status=head.worker_status(self.root)
        self.assertEqual(status['state'],'idle');self.assertTrue(status['can_extend'])
        self.assertEqual(status['idle_deadline_epoch'],self.ready['created_epoch']+900)
        self.ready['controls_version']=0
        status=head.worker_status(self.root)
        self.assertEqual(status['state'],'ready');self.assertFalse(status['can_extend'])
        self.assertIsNone(status['idle_deadline_epoch'])

    def test_stale_target_returns_bound_rejection_without_remote_action(self):
        self.params['launch_sha256']='0'*64
        with mock.patch.object(client.subprocess,'run') as remote:
            value=head.worker_control(self.root,'extend',self.params)
        self.assertEqual(value['status'],'rejected');remote.assert_not_called()
        self.assertEqual(value['launch_sha256'],'0'*64)
        self.assertEqual(value,head.worker_control(self.root,'extend',self.params))

    def test_lost_remote_reply_recovers_from_retained_ledger_after_pointer_changes(self):
        calls=[]
        def remote(argv,**kwargs):
            calls.append(argv)
            controls.apply(self.ready,self.digest,json.loads(kwargs['input']))
            raise subprocess.TimeoutExpired(argv,1)
        with mock.patch.object(client.subprocess,'run',side_effect=remote),self.assertRaises(subprocess.TimeoutExpired):
            head.worker_control(self.root,'extend',self.params)
        (self.root/'active.json').unlink()
        with mock.patch.object(client.subprocess,'run') as remote:
            receipt=head.worker_control(self.root,'extend',self.params)
        remote.assert_not_called();self.assertEqual(len(calls),1)
        self.assertEqual(receipt['applied_seconds'],900)
        self.assertEqual(receipt['idle_deadline_epoch'],self.ready['created_epoch']+1800)

    def test_wrong_reused_command_payload_fails_instead_of_running_again(self):
        self.params['launch_sha256']='0'*64
        head.worker_control(self.root,'extend',self.params)
        with self.assertRaisesRegex(ValueError,'different payload'):
            head.worker_control(self.root,'shutdown',self.params)

    def test_stale_heartbeat_disables_controls_without_remote_probe(self):
        path=Path(self.ready['output'])/'worker-status.json'
        value=session.load(path);value['checked_epoch']=time.time()-60;session.atomic(path,value)
        with mock.patch.object(client.subprocess,'run') as remote:
            status=head.worker_status(self.root)
        self.assertEqual(status['state'],'uncertain');self.assertFalse(status['can_shutdown']);remote.assert_not_called()

    def test_cli_routes_exact_control_flags(self):
        args=['worker-control','--root',str(self.root),'--action','shutdown']
        for key,value in self.params.items():args += ['--'+key.replace('_','-'),value]
        with mock.patch.object(client,'worker_control',return_value={'status':'rejected'}) as method:
            client.main(args)
        self.assertEqual(method.call_args.args[:3],(self.root,'shutdown',self.params))

    def test_bound_job_log_bridges_pre_mount_stage_and_private_progress(self):
        now=time.time_ns()
        old=dict(schema=1,stage='allocating',scope='msa',state='running',message='Allocating',timestamp_ns=now-10)
        latest=dict(schema=1,stage='base_setup',scope='msa',state='running',message='Base setup',timestamp_ns=now)
        session.atomic(self.state/'startup-progress.json',old)
        job=self.fixture.root/'job';job.mkdir();(job/'run.log').write_text('BIO_WORKER_STAGE '+json.dumps(latest)+'\n')
        self.launch['job_file']=str(job/'job.json')
        self.assertEqual(head.startup_snapshot(self.state,self.launch),latest)
        log=self.fixture.root/'progress.log';log.touch(mode=0o600)
        with mock.patch.dict(os.environ,{'BIO_WORKER_PROGRESS_LOG':str(log)}),mock.patch.object(head,'LAST_PROGRESS',None):
            head.forward_progress(latest);head.forward_progress(latest)
        self.assertEqual(len(log.read_text().splitlines()),1)
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
        try:
            from workbench import progress
            self.assertEqual(progress.events(log.read_text()),[latest])
        finally:sys.path.pop(0)


if __name__=='__main__':unittest.main()
