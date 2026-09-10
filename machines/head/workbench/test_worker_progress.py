"""Observed stage counters and ETA freshness, using local fixture logs only."""
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tempfile
import time
import unittest

from workbench import progress
from workbench.common import canonical
from workbench.runner import JobProgress, run_job, submission_error
from workbench.test_msa_progress import marker


def event(seconds=1000, **changes):
    return {'schema': 1, 'stage': 'index_warm', 'scope': 'msa', 'state': 'running',
            'stage_id': 'fixture-prefetch', 'message': 'Loading real index pages',
            'timestamp_ns': int(seconds * 10**9), **changes}


def line(value):
    return progress.PREFIX + canonical(value).decode() + '\n'


class WorkerProgressTests(unittest.TestCase):
    def test_every_actual_stage_parses_without_inventing_percent_or_job_eta(self):
        for stage in progress.STAGES:
            value = event(stage=stage)
            parsed = progress.events(line(value))
            self.assertEqual(parsed, [value])
            view = progress.view(parsed[0], epoch=1000)
            self.assertEqual(view['stage'], stage)
            self.assertEqual(view['eta']['state'], 'unknown')
            self.assertNotIn('percent', view)

    def test_measured_counter_eta_and_range_are_for_this_stage_only(self):
        samples = [event(1000, completed=0, total=1000, unit='bytes'),
                   event(1010, completed=100, total=1000, unit='bytes')]
        result = progress.view(samples[-1], samples, epoch=1010)
        self.assertEqual(result['eta']['state'], 'estimate')
        self.assertEqual(result['eta']['seconds'], 90)
        self.assertEqual(result['eta']['scope'], 'stage')
        samples.append(event(1020, completed=300, total=1000, unit='bytes'))
        result = progress.view(samples[-1], samples, epoch=1020)
        self.assertEqual(result['eta']['state'], 'range')
        self.assertEqual((result['eta']['lower_seconds'], result['eta']['upper_seconds']), (35, 70))

    def test_eta_is_unknown_for_one_measurement_reset_different_stage_or_no_rate(self):
        first = event(1000, completed=100, total=1000, unit='bytes')
        for second in [event(1010, completed=100, total=1000, unit='bytes'),
                       event(1010, completed=50, total=1000, unit='bytes'),
                       event(1010, completed=200, total=1000, unit='bytes', stage_id='different'),
                       event(1000.5, completed=200, total=1000, unit='bytes')]:
            self.assertEqual(progress.view(second, [first, second], epoch=1010)['eta']['state'], 'unknown')
        self.assertEqual(progress.view(first, [first], epoch=1000)['eta']['state'], 'unknown')
        unknown_total = event(1000, completed=100, unit='bytes')
        self.assertEqual(progress.view(unknown_total, epoch=1000)['completed'], 100)
        self.assertEqual(progress.view(unknown_total, epoch=1000)['eta']['state'], 'unknown')

    def test_stale_and_future_observations_suppress_durations(self):
        value = event(eta={'state': 'range', 'scope': 'startup', 'lower_seconds': 100,
                           'upper_seconds': 200, 'basis': 'Measured prior startup stages'})
        for epoch in (1031, 990):
            view = progress.view(value, epoch=epoch)
            self.assertTrue(view['stale']); self.assertEqual(view['eta']['state'], 'stale')
            self.assertNotIn('lower_seconds', view['eta'])
        fresh = progress.view(value, epoch=1000)
        self.assertFalse(fresh['stale']); self.assertEqual(fresh['eta']['scope'], 'startup')
        self.assertEqual(progress.freshness(fresh, epoch=1040)['eta']['state'], 'stale')

    def test_eta_age_is_applied_once_per_elapsed_interval(self):
        value = event(eta={'state': 'estimate', 'scope': 'stage', 'seconds': 100, 'basis': 'Observed transfer rate'})
        first = progress.view(value, epoch=1010)
        self.assertEqual(first['eta']['seconds'], 90)
        self.assertEqual(progress.freshness(first, epoch=1010)['eta']['seconds'], 90)
        self.assertEqual(progress.freshness(first, epoch=1020)['eta']['seconds'], 80)

    def test_malformed_unknown_secret_and_partial_marker_fields_are_ignored(self):
        bad = [event(schema=True), event(stage=[]), event(scope={}), event(state=[]),
               event(timestamp_ns=True), event(completed=10, total=1, unit='bytes'),
               event(completed=True, total=100, unit='bytes'), event(argv=['secret']),
               event(message='unsafe\nline'), event(message='x' * 2049),
               event(eta={'state': 'unknown', 'scope': 'job', 'basis': 'Unknown', 'seconds': 2})]
        for value in bad:
            self.assertEqual(progress.events(line(value)), [])
        self.assertEqual(progress.events(line(event()).rstrip('\n')), [])
        self.assertEqual(progress.events(progress.PREFIX + '{"schema":1,"schema":1}\n'), [])
        self.assertEqual(progress.events('prefix ' + line(event())), [])

    def test_new_stage_sidechannel_mirror_and_real_downstream_phase_order(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        log, msa, worker = root / 'run.log', root / 'msa.log', root / 'worker.log'
        epoch = time.time_ns()
        log.write_text('checkpoint inventory before startup\n'); os.utime(log, ns=(epoch, epoch))
        msa.write_text(marker('warming', 'Broad legacy warm-up', epoch + 1))
        value = event((epoch + 2) / 10**9, timestamp_ns=epoch + 2)
        worker.write_text(line(value))
        observer = JobProgress(log, msa, worker)
        phase, detail = observer.details()
        self.assertEqual(phase, 'private MSA index warm')
        self.assertEqual(detail['stage'], 'index_warm')
        observer.forward(); once = log.read_text(); observer.forward()
        self.assertEqual(log.read_text(), once)
        self.assertEqual(observer.details()[0], 'private MSA index warm')
        self.assertIn('[worker progress] BIO_WORKER_STAGE', once)
        msa.write_text(marker('warming', 'Later broad heartbeat', epoch + 3))
        self.assertEqual(observer.details()[0], 'private MSA index warm')
        with log.open('a') as stream: stream.write('Inference started\n')
        self.assertEqual(observer.details()[0], 'inference')
        self.assertEqual(observer.details()[1]['eta']['state'], 'unknown')

    def test_final_failed_generic_marker_preserves_specific_cause(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        log, worker = root / 'run.log', root / 'worker.log'
        log.write_text('bio-submit: nested preparation exited 2\n')
        worker.write_text(line(event(state='failed', message='Runtime archive checksum differs', stage='runtime_download')))
        error = submission_error(log, 2, worker_progress=worker)
        self.assertEqual(error['message'], 'Runtime archive checksum differs')
        self.assertEqual(error['stage'], 'runtime_download')
        self.assertFalse(error['automatic_retry'])

    def test_direct_marker_cannot_reinterpret_old_native_words_as_new_work(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        log, msa = root / 'run.log', root / 'msa.log'
        msa.write_text('')
        log.write_text('checkpoint inventory from before startup\n' + line(event(time.time(), scope='gpu', stage='runtime_download')))
        observer = JobProgress(log, msa)
        self.assertEqual(observer.details()[0], 'runtime download')
        with log.open('a') as stream: stream.write('Inference started\n')
        self.assertEqual(observer.details()[0], 'inference')

    def test_real_local_runner_exposes_stage_eta_and_retains_owned_evidence(self):
        from workbench.test_workbench import WorkbenchTests
        fixture = WorkbenchTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        stages = ['allocating', 'runtime_download', 'index_warm', 'model_setup', 'inference']
        script = '''import json,os,pathlib,time
side=pathlib.Path(os.environ['BIO_WORKER_PROGRESS_LOG'])
assert side.stat().st_mode & 0o777 == 0o600
root=pathlib.Path(ROOT)
for stage in STAGES:
 value={'schema':1,'stage':stage,'scope':'msa' if stage=='index_warm' else 'gpu',
  'state':'running','message':'Fixture '+stage,'timestamp_ns':time.time_ns(),
  'eta':{'state':'range','scope':'stage','lower_seconds':30,'upper_seconds':60,'basis':'Synthetic measured fixture'}}
 with side.open('a') as stream:stream.write('BIO_WORKER_STAGE '+json.dumps(value)+'\\n')
 limit=time.monotonic()+12
 while not(root/stage).exists():
  if time.monotonic()>limit:raise RuntimeError('Progress was not observed')
  time.sleep(.02)
'''.replace('ROOT', repr(str(fixture.root))).replace('STAGES', repr(stages))
        ident = fixture.running_fixture(script)
        with fixture.store.transaction() as db:
            job = fixture.store.get(db, 'job', ident)
            job['_prepared']['environment']['BIO_WORKER_PROGRESS_LOG'] = '/must-not-be-used'
            fixture.store.put(db, 'job', job)
        def follow():
            for stage in stages:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    job = fixture.api.call('job.get', {'job_id': ident})
                    detail = job['progress']
                    if detail.get('stage') == stage:
                        self.assertEqual(job['state'], 'running')
                        self.assertEqual(detail['eta']['scope'], 'stage')
                        self.assertEqual(detail['eta']['state'], 'range')
                        self.assertLessEqual(detail['eta']['upper_seconds'], 60)
                        self.assertNotIn('percent', detail)
                        batch = fixture.api.call('batch.get', {'batch_id': job['batch_id']})
                        self.assertEqual(batch['jobs'][0]['progress']['stage'], stage)
                        self.assertIn('BIO_WORKER_STAGE', fixture.api.call('job.logs', {'job_id': ident})['text'])
                        (fixture.root / stage).touch(); break
                    time.sleep(.02)
                else:
                    raise AssertionError('Did not see stage ' + stage)
        with ThreadPoolExecutor(1) as pool:
            follower = pool.submit(follow)
            run_job(fixture.store, ident, fixture.config); follower.result()
        job = fixture.api.call('job.get', {'job_id': ident})
        self.assertEqual(job['state'], 'complete')
        self.assertEqual(job['progress']['eta']['state'], 'unknown')
        self.assertIn('worker-progress.log', [value['name'] for value in job['artifacts']])
        self.assertEqual(len(fixture.store.listing('job')), 1)
