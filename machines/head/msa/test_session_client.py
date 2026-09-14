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
import search_profile
import session_cache
from test_session_cache import route_fixture


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.tools = self.root/"source-tools"; self.tools.mkdir()
        for name in ["msa/session.py", "msa/session_client.py", "msa/panel.py", "msa/prepared.py", "msa/server.py",
                     "msa/databases.py", "recipes/_common.sh", "rf3/msa.py"]:
            path = self.tools/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(name)
        self.tools.joinpath('msa/search_profile.py').write_bytes(Path(search_profile.__file__).read_bytes())
        self.tools.joinpath('msa/session_cache.py').write_bytes(Path(session_cache.__file__).read_bytes())
        self.route = route_fixture()
        self.cache_select = mock.patch.object(client.session_cache, 'invoke_head', return_value=self.route)
        self.cache_select.start(); self.addCleanup(self.cache_select.stop)
        self.submit = self.root/"bio-submit"; self.submit.write_text("fixture")
        self.args = argparse.Namespace(root=self.root/"sessions", tools=self.tools, timeout=300,
            idle_seconds=60, warm="report", worker=None, spot=False, search_profile=search_profile.MAPPED_PROFILE)

    def tearDown(self): self.tmp.cleanup()

    def test_start_routes_only_through_budgeted_submit_and_freezes_sources(self):
        with mock.patch.object(client.shutil, "which", return_value=str(self.submit)), \
             mock.patch.object(client.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as run:
            result = client.start(self.args)
        state = Path(result["state"]); intent = session.load(state/"intent.json")
        self.assertEqual(intent["sources"], session.sources(state/"tools"))
        command = run.call_args.args[0]
        self.assertIn("--property=RuntimeMaxSec=8700", command)
        self.assertIn("--setenv=BIO_MSA_CAPACITY_WAIT_SECONDS=7200", command)
        self.assertEqual(intent['timeout_seconds'], 300)
        self.assertEqual(intent['capacity_wait_seconds'], 7200)
        self.assertIn("--setenv=DC_MAX_INSTANCE_HOURLY=13.0", command)
        self.assertEqual(intent['search_profile'], search_profile.resolve(search_profile.MAPPED_PROFILE))
        self.assertEqual(intent['database_cache'], self.route)
        self.assertIn('--setenv=BIO_MSA_SEARCH_PROFILE=mapped-128gb-v1', command)
        self.assertIn('--setenv=BIO_MSA_SESSION_WARM=report', command)
        self.assertFalse(any('MemoryMax' in word or 'MemorySwapMax' in word for word in command))
        self.assertEqual(command[-6:], [str(self.submit), "msa", "--sub", "session", "--timeout", "300"])
        self.tools.joinpath("msa/session.py").write_text("changed later")
        self.assertNotEqual(session.sources(self.tools), intent["sources"])
        self.assertEqual(session.sources(state/"tools"), intent["sources"])

    def test_unconfigured_verda_session_uses_resident_prefetch(self):
        del self.args.search_profile
        self.args.warm = None
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(client.shutil, 'which', return_value=str(self.submit)), \
             mock.patch.object(client.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,'','')):
            result = client.start(self.args)
        intent = session.load(Path(result['state'])/'intent.json')
        self.assertEqual(intent['search_profile'], search_profile.resolve(search_profile.LEGACY_PROFILE))
        self.assertEqual(intent['warm'], 'prefetch')
        self.assertNotIn('database_cache', intent)

    def test_prefetch_profile_requires_native_sources_before_registration(self):
        self.args.search_profile = search_profile.PREFETCH_PROFILE
        with mock.patch.object(client.subprocess, 'run') as run, \
             self.assertRaisesRegex(ValueError, 'native installer'):
            client.start(self.args)
        run.assert_not_called()
        self.assertFalse((self.args.root/'active.json').exists())

    def test_prefetch_profile_freezes_native_setup_and_uses_full_ssd_route(self):
        self.args.search_profile = search_profile.PREFETCH_PROFILE
        source = Path(session.__file__).parent.parent
        for name in session.PREFETCH_SOURCES:
            (self.tools/name).write_bytes((source/name).read_bytes())
        with mock.patch.object(client.shutil, 'which', return_value=str(self.submit)), \
             mock.patch.object(client.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,'','')) as run:
            result = client.start(self.args)
        state = Path(result['state']); intent = session.load(state/'intent.json')
        self.assertEqual(intent['database_cache'], self.route)
        self.assertEqual(intent['search_profile'], search_profile.resolve(search_profile.PREFETCH_PROFILE))
        self.assertIn('--setenv=BIO_MSA_SEARCH_PROFILE='+search_profile.PREFETCH_PROFILE, run.call_args.args[0])
        for name in session.PREFETCH_SOURCES:
            self.assertEqual(intent['sources'][name], session.sha(state/'tools'/name))
        (self.tools/'msa/tools.sh').write_text('changed setup after registration')
        self.assertNotEqual(session.sources(self.tools), intent['sources'])
        self.assertEqual(session.sources(state/'tools'), intent['sources'])

    def test_prefetch_profile_rejects_a_different_manifest_before_registration(self):
        self.args.search_profile = search_profile.PREFETCH_PROFILE
        source = Path(session.__file__).parent.parent
        for name in session.PREFETCH_SOURCES:
            (self.tools/name).write_bytes((source/name).read_bytes())
        lock = self.tools/'msa/native-runtime.json'
        lock.write_bytes(lock.read_bytes()+b'\n')
        with mock.patch.object(client.subprocess, 'run') as run, \
             self.assertRaisesRegex(ValueError, 'manifest differs from the search profile'):
            client.start(self.args)
        run.assert_not_called()
        self.assertFalse((self.args.root/'active.json').exists())

    def test_missing_cache_fails_before_registration_or_unit_start(self):
        with mock.patch.object(client.session_cache, 'invoke_head', side_effect=ValueError('SSD cache unavailable')), \
             mock.patch.object(client.subprocess, 'run') as run, self.assertRaisesRegex(ValueError,'SSD cache unavailable'):
            client.start(self.args)
        run.assert_not_called()
        self.assertFalse((self.args.root/'active.json').exists())

    def enable_cache(self, state, intent, launch):
        intent['database_cache']=self.route
        launch['job_file']=str(self.root/'job/job.json')
        Path(launch['job_file']).parent.mkdir(exist_ok=True)
        session.atomic(Path(launch['job_file']).parent/'cache-route.json',self.route)
        session.atomic(state/'intent.json',intent);session.atomic(state/'launch.json',launch)
        session.atomic(self.args.root/'active.json',dict(session_id=intent['session_id'],intent_sha256=session.sha(state/'intent.json')))

    def test_cache_closure_requires_exact_route_release_after_actual_worker_and_os_absence(self):
        state,intent,launch=self.registered();self.enable_cache(state,intent,launch)
        provider=dict(status='closed',instance=launch['instance'],os_id=launch['provider']['os_id'],
                      exact_worker_absent=True,exact_os_absent_active_and_trash=True)
        released=dict(status='released',**session_cache.identity(self.route))
        with mock.patch.object(client,'unit_state',return_value=dict(LoadState='not-found')), \
             mock.patch.object(client,'provider_check',return_value=provider), \
             mock.patch.object(client.session_cache,'invoke_head',return_value=dict(released,lease_id='d'*32)), \
             self.assertRaisesRegex(ValueError,'frozen full SSD cache'):
            client.stop(self.args.root)
        self.assertTrue((self.args.root/'active.json').exists());self.assertFalse((state/'closed.json').exists())
        with mock.patch.object(client,'unit_state',return_value=dict(LoadState='not-found')), \
             mock.patch.object(client,'provider_check',return_value=provider), \
             mock.patch.object(client.session_cache,'invoke_head',return_value=released) as release:
            result=client.stop(self.args.root)
        self.assertEqual(result['status'],'closed');self.assertFalse((self.args.root/'active.json').exists())
        self.assertEqual(release.call_args.args[1:3],('release','--route'))
        self.assertEqual(session.load(state/'closed.json')['proof']['database_cache'],released)

    def test_preallocation_recovery_releases_only_the_frozen_preparing_cache_lease(self):
        import startup
        state,intent,launch=self.registered();self.enable_cache(state,intent,launch)
        (state/'launch.json').unlink()
        session.atomic(state/'attempt.json',dict(run_dir=str(Path(launch['job_file']).parent)))
        session.atomic(state/'no-allocation.json',dict(no_allocation_attempted=True))
        observed=dict(session_id=intent['session_id'],intent_sha256=session.sha(state/'intent.json'),
                      no_allocation_sha256=session.sha(state/'no-allocation.json'))
        with mock.patch.object(client,'unit_state',return_value=dict(LoadState='not-found')), \
             mock.patch.object(startup,'validate_no_allocation',return_value={'no_allocation_attempted':True}), \
             mock.patch.object(client.session_cache,'invoke_head',side_effect=ValueError('lease uncertain')), \
             self.assertRaisesRegex(ValueError,'lease uncertain'):
            client._retire_locked(self.args.root,observed)
        self.assertTrue((self.args.root/'active.json').exists());self.assertFalse((state/'closed.json').exists())
        released=dict(status='not-acquired',**session_cache.identity(self.route))
        with mock.patch.object(client,'unit_state',return_value=dict(LoadState='not-found')), \
             mock.patch.object(startup,'validate_no_allocation',return_value={'no_allocation_attempted':True}), \
             mock.patch.object(client.session_cache,'invoke_head',return_value=released):
            client._retire_locked(self.args.root,observed)
        self.assertFalse((self.args.root/'active.json').exists())
        self.assertEqual(session.load(state/'closed.json')['proof']['database_cache'],released)

    def test_ready_cache_must_match_lease_generation_and_current_boot_before_remote_submit(self):
        state,intent,launch=self.registered()
        launch.update(boot_id='current-boot');launch['provider']['reservation_deadline']=10000
        self.enable_cache(state,intent,launch)
        receipt=dict(**session_cache.identity(self.route),boot_id=launch['boot_id'])
        ready=dict(session_id=intent['session_id'],owner=dict(boot_id=launch['boot_id']),
            output=launch['remote_out'],endpoint='http://127.0.0.1:8080',deadline_epoch=9000,
            sources=intent['sources'],tools='/tmp/tools',state='/tmp/bio-msa-session-'+intent['session_id'],
            search_profile=intent['search_profile'],database_cache=receipt)
        live=dict(LoadState='loaded',ActiveState='active',InvocationID=launch['invocation_id'])
        for change in ({'lease_id':'d'*32},{'source_receipt_sha256':'e'*64},{'boot_id':'old'},{}):
            with self.subTest(change=change):
                ready['database_cache']=dict(receipt,**change)
                session.atomic(Path(launch['remote_out'])/'session-ready.json',ready)
                with mock.patch.object(client,'unit_state',return_value=live), \
                     mock.patch.object(client,'provider_check',return_value=dict(
                         cache_lease_id=self.route['lease_id'],cache_generation=self.route['binding']['cache_generation'])), \
                     mock.patch.object(client,'ssh',return_value=['ssh']), \
                     mock.patch.object(client.subprocess,'check_output',return_value=json.dumps(ready)) as remote:
                    if change:
                        with self.assertRaises(ValueError):client.ready_session(self.args.root)
                        remote.assert_not_called()
                    else:self.assertEqual(client.ready_session(self.args.root)[3],ready)

    def test_cache_worker_hostname_requires_exact_budget_lease_and_generation(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        import time
        now=time.time();token='a'*32
        job=dict(id='worker',os_id='os',status='running',deadline=now+1000,
                 cache_lease_id=self.route['lease_id'],cache_generation=self.route['binding']['cache_generation'])
        state=dict(jobs={token:job},last_watchdog=now)
        row=dict(id='worker',ip='192.0.2.1',hostname='bio-msa-cache-'+self.route['lease_id'])
        controller=SimpleNamespace(store=SimpleNamespace(locked=lambda _:nullcontext(state)),clock=lambda:now,
            refresh=lambda _:[ [row], [dict(id='os')], [] ],persistent_hours=1,margin=0,ceiling=1000)
        helpers=dict(API=lambda:None,Store=lambda _:None,Controller=lambda *_:controller,
                     summary=lambda *_:dict(uncertain=False,storage_uncertain=False,spent=0,reserved=0,background_reserve=0))
        args=argparse.Namespace(tools=self.tools,instance='worker',ip=row['ip'],action='provider-check')
        with mock.patch.object(client.runpy,'run_path',return_value=helpers):
            proof=client.provider(args)
            self.assertEqual(proof['hostname'],row['hostname']);self.assertEqual(proof['cache_lease_id'],self.route['lease_id'])
            row['hostname']='bio-'+token[:12]
            with self.assertRaisesRegex(ValueError,'provider identity'):client.provider(args)
            del job['cache_lease_id']
            with self.assertRaisesRegex(ValueError,'lease identity'):client.provider(args)
            del job['cache_generation']
            self.assertNotIn('cache_lease_id',client.provider(args))
        intent=dict(tools=str(self.tools),database_cache=self.route)
        launch=dict(instance='worker',ip=row['ip'])
        with mock.patch.object(client,'provider_check',return_value=dict(proof,cache_lease_id='d'*32)), \
             self.assertRaisesRegex(ValueError,'frozen full SSD cache lease'):
            client.check_session_provider(self.root,intent,launch)

    def test_mapped_prefetch_and_unbound_tools_fail_before_registration(self):
        for change in ('prefetch', 'missing', 'changed'):
            with self.subTest(change=change):
                self.args.warm = 'prefetch' if change == 'prefetch' else None
                path = self.tools/'msa/search_profile.py'
                if change == 'missing': path.unlink()
                if change == 'changed': path.write_text('different policy')
                with mock.patch.object(client.subprocess, 'run') as run, self.assertRaises(ValueError):
                    client.start(self.args)
                run.assert_not_called()
                self.assertFalse((self.args.root/'active.json').exists())

    def test_explicit_resident_profile_preserves_prefetch(self):
        self.args.search_profile = search_profile.LEGACY_PROFILE
        self.args.warm = 'prefetch'
        with mock.patch.object(client.shutil, 'which', return_value=str(self.submit)), \
             mock.patch.object(client.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            result = client.start(self.args)
        intent = session.load(Path(result['state'])/'intent.json')
        self.assertEqual(intent['search_profile'], search_profile.resolve(search_profile.LEGACY_PROFILE))
        self.assertIn('--setenv=BIO_MSA_SESSION_WARM=prefetch', run.call_args.args[0])

    def test_readiness_must_match_profile_but_old_frozen_intents_remain_valid(self):
        state, intent, launch = self.registered()
        launch['boot_id'] = 'boot-fixture'
        launch['provider']['reservation_deadline'] = 10000
        session.atomic(state/'launch.json', launch)
        ready = dict(session_id=intent['session_id'], owner=dict(boot_id=launch['boot_id']),
                     output=launch['remote_out'], endpoint='http://127.0.0.1:8080', deadline_epoch=9000,
                     sources=intent['sources'], tools='/tmp/tools', state='/tmp/bio-msa-session-'+intent['session_id'])
        live = dict(LoadState='loaded', ActiveState='active', InvocationID=launch['invocation_id'])
        for candidate in (None, search_profile.resolve(search_profile.LEGACY_PROFILE), search_profile.resolve(search_profile.MAPPED_PROFILE)):
            with self.subTest(candidate=candidate):
                if candidate is None: ready.pop('search_profile', None)
                else: ready['search_profile'] = candidate
                session.atomic(Path(launch['remote_out'])/'session-ready.json', ready)
                with mock.patch.object(client, 'unit_state', return_value=live), \
                     mock.patch.object(client, 'provider_check'), mock.patch.object(client, 'ssh', return_value=['ssh']), \
                     mock.patch.object(client.subprocess, 'check_output', return_value=json.dumps(ready)) as remote:
                    if candidate == search_profile.resolve(search_profile.MAPPED_PROFILE):
                        self.assertEqual(client.ready_session(self.args.root)[3], ready)
                    else:
                        with self.assertRaisesRegex(ValueError, 'search profile'): client.ready_session(self.args.root)
                        remote.assert_not_called()
        intent.pop('search_profile'); ready.pop('search_profile')
        session.atomic(state/'intent.json', intent)
        session.atomic(self.args.root/'active.json', dict(session_id=intent['session_id'], intent_sha256=session.sha(state/'intent.json')))
        session.atomic(Path(launch['remote_out'])/'session-ready.json', ready)
        with mock.patch.object(client, 'unit_state', return_value=live), mock.patch.object(client, 'provider_check'), \
             mock.patch.object(client, 'ssh', return_value=['ssh']), \
             mock.patch.object(client.subprocess, 'check_output', return_value=json.dumps(ready)):
            self.assertEqual(client.ready_session(self.args.root)[3], ready)

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

    def test_environment_capacity_override_preserves_paid_worker_timeout(self):
        with mock.patch.dict(os.environ, BIO_MSA_CAPACITY_WAIT_SECONDS='1800'), \
             mock.patch.object(client.shutil, 'which', return_value=str(self.submit)), \
             mock.patch.object(client.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            result = client.start(self.args)
        intent = session.load(Path(result['state']) / 'intent.json')
        self.assertEqual(intent['capacity_wait_seconds'], 1800)
        self.assertEqual(intent['timeout_seconds'], 300)
        self.assertIn('--property=RuntimeMaxSec=3300', run.call_args.args[0])
        self.assertEqual(run.call_args.args[0][-2:], ['--timeout', '300'])

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
        # Retained pre-cache sessions remain readable by the new client.
        intent.pop('database_cache', None)
        session.atomic(state/'intent.json',intent)
        session.atomic(self.args.root/'active.json',dict(session_id=intent['session_id'],intent_sha256=session.sha(state/'intent.json')))
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
