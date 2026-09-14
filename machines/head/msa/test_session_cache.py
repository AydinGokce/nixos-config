"""Offline exact-route, transport and closure checks; no cloud or device writes."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import session_cache as cache


def route_fixture():
    generation = dict(schema=1, cache_id='a'*32,
        source_manifest_sha256=cache.content.databases.MANIFEST_SHA256, source_receipt_sha256='b'*64)
    binding = dict(generation, volume_id='11111111-1111-4111-8111-111111111111',
        filesystem_uuid='22222222-2222-4222-8222-222222222222',
        cache_generation=cache.content.digest(generation), ready_receipt_sha256=None)
    ready = dict(**cache.content.identity(binding), kind=cache.content.KIND, status='ready',
        rootrel='colabfold', filesystem_type='ext4', size_bytes=cache.content.SIZE_BYTES,
        completion=dict(full_readback=True, files=10, payload_bytes=1234,
                        source_bytes_hashed=1234, destination_bytes_readback=1234))
    binding['ready_receipt_sha256'] = cache.digest(ready)
    return dict(schema=1, kind=cache.KIND, lease_id='c'*32,
                binding=cache.content.binding(binding), ready_receipt=ready)


class CacheRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.route = route_fixture()
        self.path = self.root/'cache-route.json'; cache.save(self.path, self.route)

    def test_exact_ready_bytes_and_complete_readback_are_required(self):
        self.assertEqual(cache.validate(self.route), self.route)
        for mutate in (
            lambda x: x['binding'].update(volume_id='33333333-3333-4333-8333-333333333333'),
            lambda x: x['binding'].update(source_receipt_sha256='d'*64),
            lambda x: x['ready_receipt']['completion'].update(destination_bytes_readback=1233),
            lambda x: x.update(schema=True),
        ):
            changed = copy.deepcopy(self.route); mutate(changed)
            with self.assertRaises((ValueError, RuntimeError)): cache.validate(changed)
        # A legitimately sealed but incomplete ready document also fails.
        changed = copy.deepcopy(self.route); changed['ready_receipt']['completion']['full_readback'] = False
        changed['binding']['ready_receipt_sha256'] = cache.digest(changed['ready_receipt'])
        with self.assertRaisesRegex(ValueError, 'readback'): cache.validate(changed)

    def test_existing_standalone_cache_helpers_do_not_expand_old_session_source_receipts(self):
        import session
        required=['msa/session.py','msa/session_client.py','msa/panel.py','msa/prepared.py',
                  'msa/server.py','msa/databases.py','recipes/_common.sh','rf3/msa.py']
        helpers=['msa/block_cache.py','msa/block_cache_control.py','msa/block_cache_device.py']
        for name in required+helpers:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(name)
        old=session.sources(self.root)
        self.assertEqual(set(old),set(required))
        (self.root/'msa/session_cache.py').write_text('new explicit cache route')
        new=session.sources(self.root)
        self.assertEqual(set(new)-set(old),set(helpers+['msa/session_cache.py']))
        self.assertTrue(all(new[name]==value for name,value in old.items()))

    def test_selection_requires_published_unleased_current_generation(self):
        envelope = dict(status='allocated', lease=None, **self.route['binding'])
        control = SimpleNamespace(execute=lambda _: envelope,
            read=lambda: dict(**self.route['binding'], ready_receipt=self.route['ready_receipt']))
        selected = cache.select(control=control)
        self.assertEqual(selected['binding'], self.route['binding'])
        self.assertNotEqual(selected['lease_id'], self.route['lease_id'])
        envelope['lease'] = {'status':'bound'}
        with self.assertRaisesRegex(ValueError, 'already leased'): cache.select(control=control)
        envelope['lease'] = None; envelope['ready_receipt_sha256'] = None
        with self.assertRaisesRegex(ValueError, 'published full SSD'): cache.select(control=control)

    def test_prefetch_session_uses_its_bound_cache_but_other_profiles_do_not(self):
        state=self.root/('e'*32);state.mkdir()
        intent=dict(session_id=state.name,provider_name='verda',
                    search_profile=dict(profile_id='mapped-prefetch-128gb-v1'),database_cache=self.route)
        cache.save(state/'intent.json',intent)
        self.assertEqual(cache.selected_session(state),self.route)
        for profile in ('resident-768gib-v1','unknown'):
            intent['search_profile']['profile_id']=profile
            (state/'intent.json').unlink()
            cache.save(state/'intent.json',intent)
            with self.assertRaisesRegex(ValueError,'mapped Verda session'):
                cache.selected_session(state)

    def test_head_cli_uses_standard_credential_override_without_exposing_it(self):
        state=self.root/('d'*32);state.mkdir()
        cache.save(state/'intent.json',dict(session_id=state.name,provider_name='verda',
            search_profile=dict(profile_id='mapped-128gb-v1'),database_cache=self.route))
        credentials=self.root/'head-credentials.env'
        credentials.write_text('DATACRUNCH_CLIENT_ID=fixture-client\nDATACRUNCH_CLIENT_SECRET=fixture-secret\n')
        env=dict(os.environ,DC_CREDENTIALS_FILE=str(credentials),DATACRUNCH_ENV_FILE='/does/not/exist')
        env.pop('BIO_MSA_CACHE_AUTHENTICATED',None)
        result=subprocess.run([sys.executable,cache.__file__,'select','--session-state',str(state)],
            env=env,text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),self.route)
        self.assertNotIn('fixture-secret',result.stdout+result.stderr)

    def test_controller_uses_selected_full_tools_root_for_nix_directory_symlinks(self):
        import block_cache_control
        selected=self.root/'installed-tools';selected.mkdir()
        store=self.root/'nix-source-msa';store.mkdir()
        (selected/'msa').symlink_to(store,target_is_directory=True)
        def modules(path):
            if path==str(selected/'dc-budget.py'):return {'API':lambda:'api'}
            if path==str(selected/'rfaa/storage.py'):return {'storage':'exact'}
            raise AssertionError('Resolved single-directory Nix parent was used: '+path)
        with patch.dict(os.environ,BIO_TOOLS_SRC=str(selected)), \
             patch.object(cache.runpy,'run_path',side_effect=modules), \
             patch.object(block_cache_control,'Controller',return_value='controller') as construct:
            self.assertEqual(cache.controller(),'controller')
            self.assertEqual(construct.call_args.args[0],'api')
        with patch.object(cache.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{}','')) as run:
            cache.invoke_head(selected,'select')
        self.assertEqual(run.call_args.kwargs['env']['BIO_TOOLS_SRC'],str(selected))

    def test_lost_acquire_reply_is_released_by_exact_retained_lease(self):
        lease = dict(lease_id=self.route['lease_id'], mode='serve', status='preparing',
                     **{k:v for k,v in self.route['binding'].items() if k!='ready_receipt_sha256'})
        calls = []
        def release(ident):
            calls.append(ident); lease.update(status='released', release_checks=dict(
                exact_workers_absent=True, exact_os_disks_absent=True,
                volume_detached=True, managed_jobs_closed=True))
            return dict(**self.route['binding'], lease=lease)
        control = SimpleNamespace(lease_store=lambda _: SimpleNamespace(read=lambda: lease), release=release)
        result = cache.release(self.path, control=control)
        self.assertEqual(calls, [self.route['lease_id']]); self.assertEqual(result['status'], 'released')
        self.assertEqual(cache.release(self.path, control=control), result)
        self.assertEqual(len(calls), 1)
        lease['cache_generation'] = 'e'*64
        with self.assertRaisesRegex(ValueError, 'another route'): cache.release(self.path, control=control)

    def test_uncertain_launch_and_failed_cleanup_cannot_be_called_released(self):
        absent = SimpleNamespace(lease_store=lambda _: SimpleNamespace(read=lambda: None))
        self.assertEqual(cache.release(self.path, control=absent)['status'], 'not-acquired')
        cache.save(self.root/'cache-launching.json', {})
        with self.assertRaisesRegex(ValueError, 'may have started'): cache.release(self.path, control=absent)
        lease = dict(lease_id=self.route['lease_id'], mode='serve', status='launching',
                     **{k:v for k,v in self.route['binding'].items() if k!='ready_receipt_sha256'})
        def refused(_): raise ValueError('VM/OS absence is not proven')
        held = SimpleNamespace(lease_store=lambda _: SimpleNamespace(read=lambda: lease), release=refused)
        with self.assertRaisesRegex(ValueError, 'not proven'): cache.release(self.path, control=held)
        self.assertFalse((self.root/'cache-released.json').exists())

    def test_live_status_rejects_changed_mount_receipts_and_ready_bytes(self):
        folder=self.root/'proof'; folder.mkdir(); target=self.root/'mounted'; (target/'colabfold').mkdir(parents=True)
        (target/'ready.json').write_bytes(cache.raw(self.route['ready_receipt']))
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        cache.save(folder/'route.json',self.route); cache.save(folder/'mount.json',{'mounted':True})
        cache.save(folder/'content.json',{'verified':True})
        receipt=dict(**cache.identity(self.route),boot_id=boot,device='/dev/owned',root=str(target/'colabfold'),
            mount_sha256=cache.digest({'mounted':True}),content_sha256=cache.digest({'verified':True}))
        cache.save(folder/'verified.json',receipt)
        with patch.object(cache.device,'MOUNTPOINT',str(target)), patch.object(cache.content,'cache_mount') as mounted:
            good=cache.worker_receipt(folder/'verified.json',target/'colabfold')
            self.assertEqual(good['lease_id'],self.route['lease_id']);self.assertTrue(mounted.call_args.kwargs['readonly'])
            (target/'ready.json').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'ready receipt changed'):
                cache.worker_receipt(folder/'verified.json',target/'colabfold')
            (target/'ready.json').write_bytes(cache.raw(self.route['ready_receipt']))
            (folder/'mount.json').write_text('{"mounted":false}')
            with self.assertRaisesRegex(ValueError,'verification receipt changed'):
                cache.worker_receipt(folder/'verified.json',target/'colabfold')

    def test_readiness_cannot_cross_lease_generation_or_boot(self):
        receipt=dict(**cache.identity(self.route),boot_id='actual')
        cache.check_head(self.route,receipt,boot_id='actual')
        for field,value in [('lease_id','d'*32),('source_receipt_sha256','e'*64),('boot_id','old')]:
            changed=dict(receipt);changed[field]=value
            with self.assertRaises(ValueError):cache.check_head(self.route,changed,boot_id='actual')

    def test_boot_handoff_requires_exact_managed_instance_and_authenticated_address(self):
        boot='44444444-4444-4444-8444-444444444444'
        job=dict(instance='worker',ip='192.0.2.1',database_cache=cache.identity(self.route))
        envelope=dict(**self.route['binding'],provider=dict(
            managed=dict(instance_id='worker',boot_id=boot),instances=[dict(id='worker',ip=job['ip'])]))
        control=SimpleNamespace(bind=lambda *args:envelope)
        self.assertEqual(cache.head_binding(self.route,job,boot,control=control),envelope)
        for key,value in [('ip','192.0.2.2'),('instance','other')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                cache.head_binding(self.route,dict(job,**{key:value}),boot,control=control)


class CacheTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.route=route_fixture()
        self.route_path=self.root/'route.json';cache.save(self.route_path,self.route)
        self.job=self.root/'job.json';cache.save(self.job,dict(database_cache=cache.identity(self.route)))
        self.handoff=self.root/'handoff.json';self.script=self.root/'script';self.script.write_bytes(b'payload')
        self.boot='44444444-4444-4444-8444-444444444444'
        self.calls=[];self.output=io.BytesIO()

    def bind(self,route,job,boot):
        self.calls.append(boot);self.assertEqual(boot,self.boot)
        return {'proof':'exact attachment'}

    def run_child(self,source):
        return cache.run_worker(self.route_path,self.job,self.handoff,self.script,
            [sys.executable,'-c',source],bind=self.bind,output=self.output)

    def test_fragmented_marker_delivers_one_fresh_handoff_and_forwards_output(self):
        prefix=cache.marker(self.route)+' '
        source=("import sys,time,pathlib;assert sys.stdin.buffer.read()==b'payload';"
                f"sys.stdout.write({prefix[:15]!r});sys.stdout.flush();time.sleep(.05);"
                f"print({prefix[15:]+self.boot!r},flush=True);"
                f"p=pathlib.Path({str(self.handoff)!r});until=time.monotonic()+3\n"
                "while not p.exists() and time.monotonic()<until:time.sleep(.02)\n"
                "assert p.exists();print('native proceeds',flush=True)")
        self.assertEqual(self.run_child(source),0)
        self.assertEqual(self.calls,[self.boot]);self.assertIn(b'native proceeds',self.output.getvalue())
        self.assertEqual(cache.load(self.handoff),{'proof':'exact attachment'})

    def test_duplicate_marker_fails_and_reaps_live_child(self):
        line=cache.marker(self.route)+' '+self.boot
        source=f"import time;print({line!r},flush=True);print({line!r},flush=True);time.sleep(60)"
        with self.assertRaisesRegex(ValueError,'duplicate'):self.run_child(source)
        self.assertEqual(self.calls,[self.boot])

    def test_zero_exit_without_cache_proof_is_failure(self):
        with self.assertRaisesRegex(ValueError,'without mounting'):self.run_child("print('skipped')")
        self.assertFalse(self.handoff.exists())


if __name__=='__main__':unittest.main()
