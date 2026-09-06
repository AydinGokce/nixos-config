"""Offline queue admission/reconciliation tests; no cloud resources created."""
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import Mock
import databases

HERE = Path(__file__).resolve().parent
q = runpy.run_path(str(HERE / 'build-queue.py'))
storage = runpy.run_path(str(HERE.parent / 'rfaa/storage.py'))
VOLUME = '3ccef50a-59fe-4a5f-b7d3-ec669fe7ccef'
BOOT = 'da261a25-3217-44e6-a019-4bfc2e7771c0'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def row(kind='CPU.360V.1440G', gb=1440, rate=4.32, spot=1.728):
    return dict(instance_type=kind, memory={'size_in_gigabytes': gb}, price_per_hour=rate,
                spot_price=spot, currency='usd', supported_os=['ubuntu-24.04', 'ubuntu-24.04-cuda-12.8-open-docker'])


def avail(*names):
    return [{'location_code': 'FIN-02', 'availabilities': list(names)}]


class SelectionTests(unittest.TestCase):
    def test_price_first_cpu_only_on_tie_and_spot_allowed(self):
        cpu, gpu = row(), row('8H100.80S.176V', gb=1360, rate=26, spot=13)
        choose = lambda: q['choose']([cpu, gpu], avail(cpu['instance_type']), avail(gpu['instance_type']))
        self.assertEqual(choose()['instance_type'], cpu['instance_type'])
        cpu['price_per_hour'] = 13
        self.assertTrue(choose()['cpu'])
        cpu['price_per_hour'] = 13.01
        self.assertEqual(choose()['instance_type'], gpu['instance_type'])
        self.assertTrue(choose()['spot'])

    def test_decimal_ram_conversion_and_exact_supported_image(self):
        types = [row('CPU.192V.768G', gb=768), row('8H100.80S.176V', gb=1360), row('2GB200.186V', gb=1600)]
        types[1]['supported_os'] = ['ubuntu-22.04']
        self.assertIsNone(q['choose'](types, avail(*(r['instance_type'] for r in types)), avail()))
        enough = row('CPU.256V.1024G', gb=1024)
        self.assertGreater(q['choose']([enough], avail(enough['instance_type']), avail())['conservative_gib'], 768)

    def test_unavailable_expensive_or_ambiguous_catalog_never_selected(self):
        item = row(rate=13.01, spot=13.01)
        self.assertIsNone(q['choose']([item], avail(item['instance_type']), avail(item['instance_type'])))
        self.assertIsNone(q['choose']([row()], avail(), avail()))
        for catalog, regular in [([row(), row()], avail()), ([row()], []), ([row(gb=True)], avail()),
                                 ([row(spot=float('nan'))], avail())]:
            with self.subTest(catalog=catalog), self.assertRaises(q['Error']):
                q['choose'](catalog, regular, avail())


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.source = dict(archive='templates.tar.gz', bytes=7, md5=hashlib.md5(b'archive').hexdigest(), url='https://example.invalid/pinned')
        self.saved = dict(self.source, sha256=hashlib.sha256(b'archive').hexdigest(), actual_md5=self.source['md5'])
        self.db = dict(MANIFEST={'version': 'miniature-only'}, MANIFEST_SHA256='a'*64, SOURCES={'templates': self.source},
                       COMPONENTS=['templates'], validate=Mock(return_value={'complete': True}),
                       validate_component=databases.validate_component)
        write(self.root/'manifest.json', self.db['MANIFEST'])
        write(self.root/'.downloads.json', dict(stage='sources-downloaded', production_ready=False,
              manifest_sha256='a'*64, sources={'templates': self.saved}))
        write(self.root/'.archives/templates.tar.gz.json', self.saved)
        (self.root/'.archives/templates.tar.gz').write_bytes(b'archive')

    def tearDown(self):
        self.temp.cleanup()

    def verify(self):
        return q['verified_inputs'](self.root, self.db)

    def promote(self):
        folder = self.root/'templates'; folder.mkdir()
        (folder/'pdb100_a3m.ffdata').write_bytes(b'abc\0')
        (folder/'pdb100_a3m.ffindex').write_text('1abc_A\t0\t4\n')
        receipt = dict(component='templates', manifest_sha256='a'*64, sources={'templates': copy.deepcopy(self.saved)},
                       files=databases.validate_component(folder, 'templates'))
        write(folder/'.component.json', receipt); write(self.root/'.components/templates.json', receipt)
        (self.root/'.archives/templates.tar.gz').unlink()
        return receipt

    def test_download_metadata_and_actual_pinned_size(self):
        self.assertEqual(self.verify()['sources']['templates']['status'], 'archive_verified')
        # Scheduler checks exact size; installer rehashes complete large archives.
        (self.root/'.archives/templates.tar.gz').write_bytes(b'short')
        with self.assertRaisesRegex(q['Error'], 'size'):
            self.verify()

    def test_missing_initial_receipt_waits_but_inconsistent_receipt_blocks(self):
        (self.root/'.downloads.json').unlink()
        self.assertEqual(self.verify()['stage'], 'waiting_downloads')
        write(self.root/'.downloads.json', {'stage': 'sources-downloaded', 'sources': {}})
        with self.assertRaisesRegex(q['Error'], 'downloads receipt'):
            self.verify()

    def test_source_receipt_disagreement_and_symlink_block(self):
        write(self.root/'.archives/templates.tar.gz.json', dict(self.saved, sha256='b'*64))
        with self.assertRaisesRegex(q['Error'], 'disagree'):
            self.verify()
        write(self.root/'.archives/templates.tar.gz.json', self.saved)
        path = self.root/'.archives/templates.tar.gz'; path.rename(self.root/'other'); path.symlink_to(self.root/'other')
        with self.assertRaises(q['Error']):
            self.verify()

    def test_missing_archive_requires_real_valid_promoted_owner_not_receipt_only(self):
        self.promote()
        result = self.verify()['sources']['templates']
        self.assertEqual(result['status'], 'validated_promoted_component')
        self.assertEqual(result['component'], 'templates')
        (self.root/'templates/pdb100_a3m.ffdata').write_bytes(b'a\0')
        with self.assertRaisesRegex(RuntimeError, 'Truncated template'):
            self.verify()

    def test_incomplete_staging_or_source_history_never_substitutes_for_archive(self):
        receipt = self.promote(); receipt['sources']['templates']['sha256'] = 'b'*64
        write(self.root/'.components/templates.json', receipt); write(self.root/'templates/.component.json', receipt)
        with self.assertRaisesRegex(q['Error'], 'original downloaded source'):
            self.verify()
        (self.root/'templates').rename(self.root/'staging')
        with self.assertRaises(FileNotFoundError):
            self.verify()

    def test_taxonomy_binds_to_uniref_promoted_component(self):
        self.db['SOURCES'] = {'taxonomy': self.source}; self.db['COMPONENTS'] = ['uniref30']
        self.db['validate_component'] = Mock(return_value={'verified-real-files': True})
        write(self.root/'.downloads.json', dict(stage='sources-downloaded', production_ready=False,
              manifest_sha256='a'*64, sources={'taxonomy': self.saved}))
        receipt = dict(component='uniref30', manifest_sha256='a'*64, sources={'taxonomy': self.saved}, files={'verified-real-files': True})
        write(self.root/'.components/uniref30.json', receipt); write(self.root/'uniref30/.component.json', receipt)
        (self.root/'.archives/templates.tar.gz').unlink()
        self.assertEqual(self.verify()['sources']['taxonomy']['component'], 'uniref30')
        self.db['validate_component'].assert_called_once_with(self.root/'uniref30', 'uniref30')

    def test_valid_final_database_needs_no_deleted_source_archives(self):
        write(self.root/'.msa-databases.json', {'complete': True})
        (self.root/'.downloads.json').unlink(); (self.root/'.archives/templates.tar.gz').unlink()
        self.assertEqual(self.verify()['stage'], 'database_ready')
        self.db['validate'].assert_called_once_with(self.root)
        self.db['validate'].side_effect = RuntimeError('corrupt final component')
        with self.assertRaisesRegex(RuntimeError, 'corrupt final'):
            self.verify()


class FakeOps:
    def __init__(self):
        self.receipt = dict(volume_id=VOLUME, retention='persistent', jobs={})
        self.job_rows = {}; self.units = {}; self.lock_free = True
        self.validation = {'stage': 'sources_ready'}
        self.candidate = q['choose']([row()], avail('CPU.360V.1440G'), avail())
        self.launches = []; self.cleaned = True; self.result_records = []; self.panel_checks = []
        self.boot_id = BOOT; self.before_launch = None; self.launch_error = None

    def storage_receipt(self): return copy.deepcopy(self.receipt)
    def boot(self): return self.boot_id
    def jobs(self): return copy.deepcopy(self.job_rows)
    def unit(self, name): return copy.deepcopy(self.units.get(name, {'LoadState': 'not-found', 'ActiveState': 'inactive'}))
    @contextlib.contextmanager
    def submit_lock(self): yield self.lock_free
    def mounted(self, receipt): pass
    def inputs(self):
        if isinstance(self.validation, Exception): raise self.validation
        return copy.deepcopy(self.validation)
    def choice(self):
        if isinstance(self.candidate, Exception): raise self.candidate
        return self.candidate
    def launch(self, attempt, panel):
        if self.before_launch: self.before_launch(attempt)
        self.launches.append(copy.deepcopy(attempt))
        if self.launch_error: raise self.launch_error
        return subprocess.CompletedProcess([], 0)
    def cleanup(self, attempt, *args):
        attempt['results'] = self.result_records
        return self.cleaned
    def verify_panel(self, panel, result):
        self.panel_checks.append((panel, result)); return {'targets': 1}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = q['private_store'](storage, self.root/'msa-build-queue.json', owner=os.getuid())
        self.ops = FakeOps(); self.queue = q['Queue'](self.store, self.ops, clock=lambda: 123)
        self.queue.init()
    def tearDown(self): self.temp.cleanup()
    def state(self): return self.store.read()
    def finish(self, code=1):
        attempt = self.state()['attempts'][-1]
        self.ops.units[attempt['unit']] = dict(LoadState='loaded', ActiveState='active' if code == 0 else 'failed',
              SubState='exited' if code == 0 else 'failed', ExecMainCode='1', ExecMainStatus=str(code),
              ExecMainPID='1234', InvocationID='invocation'+str(attempt['number']), Description=attempt['description'])
        return attempt
    def panel(self):
        target = self.root/('msa-build-panel-'+'a'*32+'.json')
        target.write_text('{"version":1,"targets":[{"name":"mini","model":"boltz2","sequence":"ACDE"}]}'); target.chmod(0o600)
        state = self.state(); state['panel'] = dict(path=str(target), sha256=q['sha'](target), canonical_sha256='c'*64)
        self.store.save(state); return target

    def test_durable_dispatch_before_ambiguous_start_never_duplicates(self):
        def check_saved(attempt):
            self.assertEqual(self.state()['status'], 'dispatching')
            self.assertEqual(self.state()['attempts'][-1]['unit'], attempt['unit'])
        self.ops.before_launch = check_saved; self.ops.launch_error = subprocess.TimeoutExpired(['systemd-run'], 30)
        for _ in range(4): self.assertEqual(self.queue.tick()['status'], 'uncertain')
        self.assertEqual(len(self.ops.launches), 1); self.assertEqual(len(self.state()['attempts']), 1)

    def test_running_unit_requires_exact_description_and_invocation(self):
        self.queue.tick(); attempt = self.finish()
        self.ops.units[attempt['unit']].update(SubState='running', ExecMainCode='0')
        self.assertEqual(self.queue.tick()['status'], 'running')
        self.ops.units[attempt['unit']]['InvocationID'] = 'another'
        self.assertEqual(self.queue.tick()['status'], 'blocked'); self.assertEqual(len(self.ops.launches), 1)

    def test_reboot_never_reuses_same_pid_or_redispatches(self):
        self.queue.tick(); self.finish(); self.ops.boot_id = 'abababab-3217-44e6-a019-4bfc2e7771c0'
        self.assertEqual(self.queue.tick()['status'], 'uncertain'); self.assertEqual(len(self.ops.launches), 1)

    def test_active_downloader_lock_and_no_id_reservation_prevent_rental(self):
        self.ops.units['msa-database-install.service'] = {'LoadState': 'loaded', 'ActiveState': 'active'}
        self.assertEqual(self.queue.tick()['status'], 'waiting_downloads')
        self.ops.units.clear(); self.ops.lock_free = False
        self.assertEqual(self.queue.tick()['status'], 'waiting_manual')
        self.ops.lock_free = True; self.ops.job_rows['no-id'] = {'status': 'uncertain', 'volumes': [VOLUME]}
        self.assertEqual(self.queue.tick()['status'], 'waiting_cleanup'); self.assertFalse(self.ops.launches)

    def test_incomplete_metadata_waits_corrupt_metadata_blocks(self):
        self.ops.validation = {'stage': 'waiting_downloads', 'reason': 'not downloaded'}
        self.assertEqual(self.queue.tick()['status'], 'waiting_downloads')
        self.ops.validation = q['Error']('pinned archive truncated')
        self.assertEqual(self.queue.tick()['status'], 'blocked'); self.assertFalse(self.ops.launches)

    def test_capacity_retry_does_not_consume_attempt(self):
        for selection in (None, q['Unavailable']('network timeout')):
            self.ops.candidate = selection
            self.assertEqual(self.queue.tick()['status'], 'waiting_capacity'); self.assertEqual(self.state()['attempts'], [])
        self.ops.candidate = q['Error']('invalid price')
        self.assertEqual(self.queue.tick()['status'], 'blocked')

    def test_guard_exit_four_terminal_until_operator_action(self):
        self.queue.tick(); self.finish(4)
        for _ in range(2): self.assertEqual(self.queue.tick()['status'], 'blocked')
        self.assertTrue(self.state()['attempts'][0]['closed']); self.assertEqual(len(self.ops.launches), 1)

    def test_unresolved_cleanup_blocks_even_with_final_receipt(self):
        self.queue.tick(); self.finish(1); self.ops.cleaned = False; self.ops.validation = {'stage': 'database_ready'}
        self.assertEqual(self.queue.tick()['status'], 'waiting_cleanup')
        self.assertFalse(self.state()['attempts'][0].get('closed')); self.assertEqual(len(self.ops.launches), 1)

    def test_failed_install_with_promoted_database_retries_panel_only(self):
        self.panel(); self.queue.tick(); self.finish(1); self.ops.validation = {'stage': 'database_ready'}
        self.assertEqual(self.queue.tick()['status'], 'uncertain')
        self.assertEqual([a['stage'] for a in self.ops.launches], ['install', 'panel'])
        self.assertTrue(self.state()['attempts'][0]['closed'])

    def test_already_ready_database_still_schedules_requested_panel(self):
        self.panel(); self.ops.validation = {'stage': 'database_ready'}; self.queue.tick()
        self.assertEqual(self.ops.launches[0]['stage'], 'panel')

    def test_ready_requires_verified_panel_and_exact_clean_result(self):
        self.panel(); self.queue.tick(); self.finish(0); self.ops.validation = {'stage': 'database_ready'}
        self.ops.result_records = [{'path': '/exact/msa-job', 'exit_status': 0}]
        self.assertEqual(self.queue.tick()['status'], 'ready'); self.assertEqual(len(self.ops.panel_checks), 1)
        self.assertEqual(self.queue.tick()['status'], 'ready'); self.assertEqual(len(self.ops.launches), 1)

    def test_successful_service_missing_exact_results_does_not_claim_ready(self):
        self.queue.tick(); self.finish(0); self.ops.validation = {'stage': 'database_ready'}
        self.assertEqual(self.queue.tick()['status'], 'blocked')

    def test_incomplete_panel_never_claims_ready_or_drops_targets(self):
        path = self.panel(); original = path.read_bytes()
        self.queue.tick(); self.finish(0); self.ops.validation = {'stage': 'database_ready'}
        self.ops.result_records = [{'path': '/exact/msa-job', 'exit_status': 0}]
        self.ops.verify_panel = Mock(side_effect=q['Error']('Panel target is incomplete'))
        self.assertEqual(self.queue.tick()['status'], 'blocked')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.ops.launches), 1)

    def test_volume_identity_change_blocks_before_dispatch(self):
        self.ops.receipt['volume_id'] = 'abababab-3217-44e6-a019-4bfc2e7771c0'
        self.assertEqual(self.queue.tick()['status'], 'blocked')
        self.assertFalse(self.ops.launches)

    def test_three_failures_exhaust_without_fourth_launch(self):
        self.queue.tick()
        for number in range(1, 4):
            self.finish(1)
            self.assertEqual(self.queue.tick()['status'], 'exhausted' if number == 3 else 'uncertain')
        self.assertEqual(len(self.ops.launches), 3); self.assertTrue(all(a['closed'] for a in self.state()['attempts']))

    def test_database_only_ready_never_launches(self):
        self.ops.validation = {'stage': 'database_ready'}
        self.assertEqual(self.queue.tick()['status'], 'ready'); self.assertFalse(self.ops.launches)

    def test_panel_mutation_and_unsafe_queue_permissions_fail_closed(self):
        self.panel().write_text('{}')
        with self.assertRaisesRegex(q['Error'], 'snapshot changed'): self.queue.tick()
        self.store.path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'owned by root'): self.queue.tick()
        self.assertFalse(self.ops.launches)

    def test_duplicate_json_keys_rejected_before_dispatch(self):
        self.store.path.write_text('{"version":1,"version":1}')
        with self.assertRaisesRegex(q['Error'], 'Duplicate JSON'): self.queue.tick()
        self.assertFalse(self.ops.launches)

    def test_concurrent_tick_does_not_wait_or_dispatch(self):
        with self.store.expiry_operation() as acquired:
            self.assertTrue(acquired); self.assertEqual(self.queue.tick()['status'], 'busy')
        self.assertFalse(self.ops.launches)


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.api = Mock(); self.api.inventory.return_value = ([], [], [])
        self.ops = q['Operations'](self.root, self.root/'mount/colabfold', storage, {'API': lambda: self.api}, owner=os.getuid())
        self.ops.results = self.root/'results'
        self.receipt = dict(volume_id=VOLUME, nfs='nfs.fin-02.datacrunch.io:/exact', jobs={})
        self.attempt = dict(jobs_before=[], tracked_before=[], job_tokens=[], boot_id=BOOT)
        self.jobs = {'job-token': {'status': 'closed', 'id': 'worker', 'os_id': 'os', 'volumes': [VOLUME]}}
        self.unit = {'ExecMainPID': '1234'}
    def tearDown(self): self.temp.cleanup()

    def test_exact_worker_and_active_or_trashed_os_block_retry_unrelated_preserved(self):
        for inventory in [([{'id': 'worker'}], [], []), ([], [{'id': 'os'}], []), ([], [], [{'id': 'os'}])]:
            self.api.inventory.return_value = inventory
            self.assertFalse(self.ops.cleanup(self.attempt, self.receipt, self.jobs, self.unit))
        self.api.inventory.return_value = ([{'id': 'head'}], [{'id': VOLUME}], [{'id': 'unmanaged-old-os'}])
        self.assertTrue(self.ops.cleanup(self.attempt, self.receipt, self.jobs, self.unit))
        self.assertEqual(self.attempt['job_tokens'], ['job-token']); self.api.request.assert_not_called()

    def test_no_id_uncertain_blocks_before_api_missing_ledger_entry_blocks(self):
        self.jobs['unknown'] = {'status': 'uncertain', 'volumes': [VOLUME]}
        self.assertFalse(self.ops.cleanup(self.attempt, self.receipt, self.jobs, self.unit)); self.api.inventory.assert_not_called()
        del self.jobs['unknown']
        with self.assertRaisesRegex(q['Error'], 'disappeared'):
            self.ops.cleanup(self.attempt, self.receipt, self.jobs, self.unit)

    def test_results_require_tracked_pid_boot_volume_and_instance(self):
        name = 'msa-exact-1234'
        self.receipt['jobs'][name] = {'pid': 1234, 'boot_id': BOOT}
        self.receipt['jobs']['msa-old-1234'] = {'pid': 1234, 'boot_id': 'other-boot'}
        path = self.ops.results/name/'job.json'
        write(path, dict(job=name, model='msa', instance='worker', database_volume=VOLUME, exit_status=0))
        self.assertTrue(self.ops.cleanup(self.attempt, self.receipt, self.jobs, self.unit))
        self.assertEqual(len(self.attempt['results']), 1); self.assertEqual(self.attempt['results'][0]['job'], name)
        write(path, dict(job=name, model='msa', instance='other', database_volume=VOLUME, exit_status=0))
        with self.assertRaisesRegex(q['Error'], 'identity is unresolved'):
            self.ops.cleanup(self.attempt, self.receipt, self.jobs, self.unit)

    def test_launch_fixed_guard_timeout_retained_unit_and_frozen_panel(self):
        self.ops.run = Mock(return_value=subprocess.CompletedProcess([], 0))
        attempt = dict(stage='panel', worker=dict(instance_type='8H100.80S.176V', spot=True), unit='exact.service', description='exact', log='/private/log')
        self.ops.launch(attempt, {'path': '/private/panel.json'})
        args = self.ops.run.call_args.args[0]
        self.assertIn('--setenv=DC_MAX_INSTANCE_HOURLY=13', args); self.assertIn('--property=RemainAfterExit=yes', args)
        self.assertEqual(args[args.index('--')+1:], ['bio-msa', 'panel', '--worker', '8H100.80S.176V', '--timeout', '21600', '--spot', '--json', '/private/panel.json'])

    def test_mount_requires_exact_registered_export_and_nfs41(self):
        row = {'source': self.receipt['nfs'], 'fstype': 'nfs4', 'options': 'rw,vers=4.1'}
        self.ops.run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({'filesystems': [row]}), ''))
        self.ops.mounted(self.receipt)
        row['options'] = 'rw,vers=4.2'; self.ops.run.return_value.stdout = json.dumps({'filesystems': [row]})
        with self.assertRaisesRegex(q['Error'], 'export or protocol'): self.ops.mounted(self.receipt)

    def test_only_active_persistent_colabfold_receipt_can_admit_work(self):
        receipt = dict(version=1, profile='colabfold', volume_id=VOLUME, retention='persistent', expires_at=None,
                       status='active', name='bio-colabfold-db-123456789', location='FIN-02', size_gb=3000,
                       permanent=False, jobs={}, nfs=self.receipt['nfs'], created_at='2026-09-06T00:00:00Z')
        store = storage['Store'](self.root/'msa-storage.json', owner=os.getuid())
        self.ops.run = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        store.save(receipt)
        self.assertEqual(self.ops.storage_receipt()['volume_id'], VOLUME)
        for values in ({'status': 'retiring'}, {'profile': 'rfaa'},
                       {'retention': 'timed', 'expires_at': '2099-01-01T00:00:00Z'}):
            with self.subTest(values=values):
                store.save(dict(receipt, **values))
                with self.assertRaises(RuntimeError):
                    self.ops.storage_receipt()
        self.api.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
