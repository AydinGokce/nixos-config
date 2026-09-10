"""Private MSA progress/cancellation proofs using temporary jobs and local children."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from workbench import test_workbench as fixtures
from workbench.runner import (JobProgress, MSA_PHASES, observe_phase, run_job,
                              session_stages, submission_error)


def marker(stage, message, timestamp=None, **fields):
    return 'BIO_MSA_SESSION_STAGE ' + stage + ' ' + json.dumps({
        'message': message, 'timestamp_ns': time.time_ns() if timestamp is None else timestamp, **fields}) + '\n'


class ProgressParsingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / 'run.log'; self.log.write_text('')
        self.side = self.root / 'msa-session-progress.log'; self.side.write_text('')

    def test_every_structured_stage_exposes_its_actual_message_without_percentages(self):
        for stage, phase in MSA_PHASES.items():
            with self.subTest(stage=stage):
                self.side.write_text(marker(stage, 'Specific ' + stage, session_id='a' * 32, code='fixture'))
                self.assertEqual(observe_phase(self.log, self.side), (phase, 'Specific ' + stage))
        self.side.write_text('')
        self.log.write_text(marker('warming', 'Direct stderr works too'))
        self.assertEqual(observe_phase(self.log), ('private MSA warm-up', 'Direct stderr works too'))

    def test_malformed_oversize_and_unfinished_markers_do_not_break_the_run(self):
        good = marker('starting', 'Starting safely')
        invalid = [
            'BIO_MSA_SESSION_STAGE unknown {"message":"wrong"}\n',
            'BIO_MSA_SESSION_STAGE failed {"message":"one","message":"two"}\n',
            marker('failed', 'x' * 2049), marker('failed', 'bad\nline'),
            marker('failed', '\ud800'),
            marker('failed', 'bad clock', timestamp=True), marker('failed', 'negative', timestamp=-1),
            marker('failed', 'bad ID', session_id='../other'),
            marker('failed', 'bad code', code=['oops']),
            marker('failed', 'unapproved field', argv=['secret']),
            'BIO_MSA_SESSION_STAGE failed {"message":',
            'BIO_MSA_SESSION_STAGE failed {"message":"x","timestamp_ns":NaN}\n',
        ]
        for malformed in invalid:
            with self.subTest(malformed=malformed[:90]):
                self.side.write_text(good + malformed)
                self.assertEqual(observe_phase(self.log, self.side), ('private MSA startup', 'Starting safely'))
        self.side.write_text('x' * 40000 + '\n' + marker('ready', 'Bounded tail retained'))
        self.assertEqual(observe_phase(self.log, self.side), ('private MSA ready', 'Bounded tail retained'))
        self.assertEqual(session_stages('prefix ' + good, time.time_ns()), [])

    def test_newest_stage_and_later_native_work_supersede_earlier_waiting(self):
        epoch = time.time_ns()
        self.log.write_text('checkpoint mentioned before preparation\n')
        os.utime(self.log, ns=(epoch, epoch))
        self.side.write_text(marker('warming', 'Index pages warming', epoch + 10))
        self.assertEqual(observe_phase(self.log, self.side)[0], 'private MSA warm-up')
        self.side.write_text(marker('waiting', 'Waiting for search', epoch + 20) + marker('ready', 'Search completed', epoch + 30))
        self.assertEqual(observe_phase(self.log, self.side), ('private MSA ready', 'Search completed'))
        self.log.write_text('Inference started\n')
        os.utime(self.log, ns=(epoch + 40, epoch + 40))
        self.assertEqual(observe_phase(self.log, self.side)[0], 'inference')
        self.log.write_text(marker('waiting', 'Old direct marker', epoch + 5))
        self.assertEqual(observe_phase(self.log, self.side), ('private MSA ready', 'Search completed'))

    def test_log_forwarding_does_not_turn_old_checkpoint_text_into_new_native_progress(self):
        self.log.write_text('checkpoint inventory from before private search\n')
        self.side.write_text(marker('warming', 'Index warming'))
        observer = JobProgress(self.log, self.side)
        self.assertEqual(observer.observe()[0], 'private MSA warm-up')
        observer.forward()
        once = self.log.read_text()
        self.assertIn('[private MSA progress] BIO_MSA_SESSION_STAGE warming', once)
        self.assertEqual(observer.observe()[0], 'private MSA warm-up')
        observer.forward(); self.assertEqual(self.log.read_text(), once)
        self.side.write_text(marker('ready', 'Search complete'))
        self.assertEqual(observer.observe()[0], 'private MSA ready')
        observer.forward(); self.assertEqual(observer.observe()[0], 'private MSA ready')
        with self.log.open('a') as stream: stream.write('Inference started\n')
        self.assertEqual(observer.observe()[0], 'inference')

    def test_failed_cause_is_preserved_and_old_failure_does_not_override_later_ready(self):
        self.side.write_text(marker('failed', 'Worker start was denied by the budget guard', code='budget'))
        self.log.write_text("bio-submit: RF3 preparation failed: Command ['bio-msa', 'prepare'] returned non-zero exit status 2.\n")
        error = submission_error(self.log, 2, self.side)
        self.assertEqual(error['message'], 'Private MSA prerequisite failed: Worker start was denied by the budget guard')
        self.assertEqual(error['code'], 'budget'); self.assertFalse(error['automatic_retry'])
        self.side.write_text(marker('failed', 'Old failure') + marker('ready', 'Current session ready'))
        self.log.write_text('rf3-msa: Native alignment checksum differs\n')
        self.assertEqual(submission_error(self.log, 2, self.side)['message'], 'Native alignment checksum differs')
        self.log.write_text('no structured cause\n')
        self.assertEqual(submission_error(self.log, 17, self.side)['message'], 'Submission exited with status 17')

    def test_symlink_sidechannel_is_not_followed(self):
        foreign = self.root / 'foreign'; foreign.write_text(marker('failed', 'foreign'))
        self.side.unlink(); self.side.symlink_to(foreign)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            observe_phase(self.log, self.side)


class MsaRunnerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkbenchTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.root, self.store, self.api, self.config = (self.fixture.root, self.fixture.store,
                                                     self.fixture.api, self.fixture.config)

    def test_runtime_startup_warm_wait_ready_and_downstream_phase_reach_job_get_and_logs(self):
        steps = [('starting', 'Starting the shared private worker'), ('warming', 'Loading index pages'),
                 ('waiting', 'Waiting for this private search'), ('ready', 'Private search completed')]
        script = '''import json,os,pathlib,time
side=pathlib.Path(os.environ['BIO_MSA_PROGRESS_LOG'])
assert side.stat().st_mode & 0o777 == 0o600
root=pathlib.Path(ROOT)
for index,(stage,message) in enumerate(STEPS):
 with side.open('a') as stream:
  stream.write('BIO_MSA_SESSION_STAGE '+stage+' '+json.dumps({'message':message,'timestamp_ns':time.time_ns()})+'\\n')
 limit=time.monotonic()+12
 while not (root/('ack-'+str(index))).exists():
  if time.monotonic()>limit:raise RuntimeError('Fixture progress was not observed')
  time.sleep(.02)
print('Inference started',flush=True)
limit=time.monotonic()+12
while not (root/'ack-inference').exists():
 if time.monotonic()>limit:raise RuntimeError('Fixture inference phase was not observed')
 time.sleep(.02)
'''.replace('ROOT', repr(str(self.root))).replace('STEPS', repr(steps))
        ident = self.fixture.running_fixture(script)
        with self.store.transaction() as db:
            job = self.store.get(db, 'job', ident)
            job['_prepared']['environment']['BIO_MSA_PROGRESS_LOG'] = '/must-not-be-used'
            self.store.put(db, 'job', job)
        observed = []
        def follow():
            for index, (stage, message) in enumerate(steps):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    job = self.api.call('job.get', {'job_id': ident})
                    if job.get('phase') == MSA_PHASES[stage]:
                        self.assertEqual(job['state'], 'running')
                        self.assertEqual(job['progress']['message'], message)
                        self.assertNotIn('percent', job['progress'])
                        log = self.api.call('job.logs', {'job_id': ident})['text']
                        self.assertIn('BIO_MSA_SESSION_STAGE ' + stage, log)
                        observed.append(stage); (self.root / ('ack-' + str(index))).touch(); break
                    time.sleep(.02)
                else: raise AssertionError('Did not see private MSA stage ' + stage)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.api.call('job.get', {'job_id': ident}).get('phase') == 'inference':
                    (self.root / 'ack-inference').touch(); return
                time.sleep(.02)
            raise AssertionError('Did not clear prerequisite wait for inference')
        with ThreadPoolExecutor(1) as pool:
            follower = pool.submit(follow)
            run_job(self.store, ident, self.config); follower.result()
        result = self.api.call('job.get', {'job_id': ident})
        self.assertEqual(observed, [stage for stage, _ in steps])
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['progress']['message'], 'Run completed; outputs retained')
        self.assertIn('msa-session-progress.log', [item['name'] for item in result['artifacts']])
        self.assertEqual(len(self.store.listing('job')), 1)

    def test_nested_rf3_exit_between_polls_retains_human_failure_without_another_job(self):
        cause = 'MSA worker reservation denied: available budget is insufficient'
        script = '''import json,os,pathlib,sys,time
side=pathlib.Path(os.environ['BIO_MSA_PROGRESS_LOG'])
line='BIO_MSA_SESSION_STAGE failed '+json.dumps({'message':CAUSE,'timestamp_ns':time.time_ns(),'code':'budget'})+'\\n'
# Simulate RF3's capture of child stderr; the runner must never discover/glob this file.
nested=pathlib.Path(os.environ['BIO_RESULTS_DIR'])/'.rf3-search-request-fixture';nested.mkdir()
(nested/'private-search.log').write_text(line)
with side.open('a') as stream:stream.write(line)
print("bio-submit: RF3 preparation failed: Command ['bio-msa', 'prepare'] returned non-zero exit status 2.",flush=True)
sys.exit(2)
'''.replace('CAUSE', repr(cause))
        ident = self.fixture.running_fixture(script)
        run_job(self.store, ident, self.config)
        result = self.api.call('job.get', {'job_id': ident})
        self.assertEqual(result['state'], 'failed'); self.assertEqual(result['exit_code'], 2)
        self.assertIn(cause, result['error']['message'])
        self.assertIn(cause, result['progress']['message'])
        self.assertEqual(result['error']['prerequisite'], 'private_msa')
        self.assertFalse(result['error']['automatic_retry'])
        self.assertIn(cause, self.api.call('job.logs', {'job_id': ident})['text'])
        self.assertEqual(len(self.store.listing('job')), 1)
        self.assertEqual(len(self.store.listing('batch')), 1)

    def test_cancellation_stops_owned_waiter_group_but_preserves_independent_shared_process(self):
        shared = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'], start_new_session=True)
        self.addCleanup(lambda: self.stop_process(shared))
        script = '''import json,os,pathlib,signal,subprocess,sys,time
out=pathlib.Path(os.environ['BIO_RESULTS_DIR'])
child=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(60)'])
(out/'child.json').write_text(json.dumps({'pid':child.pid}))
def cancel(sig,frame):
 child.wait(timeout=5)
 (out/'cancelled.json').write_text(json.dumps({'owned_child_exit':child.returncode}))
 sys.exit(143)
signal.signal(signal.SIGTERM,cancel)
with pathlib.Path(os.environ['BIO_MSA_PROGRESS_LOG']).open('a') as stream:
 stream.write('BIO_MSA_SESSION_STAGE waiting '+json.dumps({'message':'Waiting for shared private MSA readiness','timestamp_ns':time.time_ns()})+'\\n')
while True:time.sleep(.02)
'''
        ident = self.fixture.running_fixture(script)
        def cancel_when_waiting():
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                job = self.api.call('job.get', {'job_id': ident})
                if job.get('phase') == 'private MSA waiting':
                    self.api.call('job.cancel', {'job_id': ident}); return
                time.sleep(.02)
            raise AssertionError('Owned waiter never reached its prerequisite wait')
        with ThreadPoolExecutor(1) as pool:
            cancellation = pool.submit(cancel_when_waiting)
            run_job(self.store, ident, self.config); cancellation.result()
        result = self.api.call('job.get', {'job_id': ident})
        self.assertEqual(result['state'], 'cancelled'); self.assertEqual(result['exit_code'], 143)
        self.assertEqual(result['progress']['message'], 'Submission cancelled')
        self.assertIsNone(shared.poll(), 'Cancelling a caller must not signal the independent shared process')
        cleanup = json.loads((self.store.directory('jobs', ident) / 'results/cancelled.json').read_text())
        self.assertEqual(cleanup['owned_child_exit'], -signal.SIGTERM)
        self.assertEqual(len(self.store.listing('job')), 1)

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
