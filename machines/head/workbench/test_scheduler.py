"""Durable burst admission tests; fake owned units never run predictions."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workbench.common import Error, now, read_json, uid, write_json
from workbench.service import configuration, Daemon
from workbench.store import Store


class OwnedUnits:
    def __init__(self):
        self.states = {}
        self.launched = []

    def state(self, config, unit):
        return self.states.get(unit, {'LoadState': 'not-found'})

    def launch(self, args, **kwargs):
        unit = next(arg.split('=', 1)[1] for arg in args if arg.startswith('--unit='))
        command = args[args.index('--') + 1:]
        self.states[unit] = {'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'running',
                             'InvocationID': uid(), 'ExecStart': '{ argv[]=' + ' '.join(command) + ' ; }'}
        self.launched.append(unit)
        return subprocess.CompletedProcess(args, 0, '', '')


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'state')
        self.config = {'tools_dir': str(Path(__file__).absolute().parents[1]), 'max_jobs': 10,
                       'systemctl': '/bin/false', 'systemd_run': '/bin/false'}
        self.units = OwnedUnits()
        self.daemon = self.restart()

    def restart(self):
        return Daemon(self.store, self.config, system=self.units.state, launch=self.units.launch)

    def jobs(self, count):
        result = []
        with self.store.transaction() as db:
            for index in range(count):
                job = {'job_id': uid(), 'state': 'queued', 'phase': 'queued', 'model': 'protenix',
                       '_prepared': {'timeout': 60}, 'provenance': {},
                       'created_at': f'2026-09-06T12:00:{index:02d}+00:00'}
                result.append(self.store.put(db, 'job', job, 'harrison'))
        return result

    def operation(self, job):
        with self.store.connection() as db:
            return dict(db.execute('SELECT * FROM operations WHERE object_id=?', (job['job_id'],)).fetchone())

    def receipt(self, job, pending=False, ended=True):
        row = self.operation(job)
        state = self.units.states[row['unit']]
        if ended:
            state['SubState'] = 'exited'
        receipt = {'kind': row['kind'], 'object_id': job['job_id'], 'unit': row['unit'],
                   'invocation_id': state['InvocationID'], 'intent_sha256': row['intent_sha256'], 'started_at': now()}
        folder = self.store.directory('operations', job['job_id'])
        write_json(folder / 'started.json', receipt)
        receipt = {**receipt, 'state': 'complete', 'finished_at': now()}
        write_json(folder / 'terminal.json', receipt)
        with self.store.transaction() as db:
            current = self.store.get(db, 'job', job['job_id'])
            current['state'] = 'running' if pending else 'complete'
            if pending:
                current['_resident_pending'] = {'test_owned_request': True}
            self.store.put(db, 'job', current)
            # Simulate the runner dying after durable terminal.json, before its
            # final operation transaction. The job itself was already updated.
            db.execute("UPDATE operations SET state='running',invocation_id=? WHERE object_id=?",
                       (state['InvocationID'], job['job_id']))
        return receipt

    def test_trusted_capacity_accepts_bursts_and_rejects_invalid_values(self):
        path = self.root / 'config.json'
        for capacity in (1, 10, 32):
            write_json(path, {'tools_dir': self.config['tools_dir'], 'max_jobs': capacity})
            self.assertEqual(configuration(path)['max_jobs'], capacity)
        for capacity in (0, 33, -1, True, 10.0, '10'):
            with self.subTest(capacity=capacity):
                write_json(path, {'tools_dir': self.config['tools_dir'], 'max_jobs': capacity})
                with self.assertRaisesRegex(Error, '1..32'):
                    configuration(path)

    def test_burst_admits_ten_fifo_and_reports_remaining_queue(self):
        jobs = self.jobs(13)
        self.assertEqual(self.daemon.tick()['errors'], [])
        self.assertEqual(self.units.launched, ['bio-workbench-job-' + job['job_id'] + '.service' for job in jobs[:10]])
        for position, job in enumerate(jobs[10:], 1):
            progress = self.store.read('job', job['job_id'])['progress']
            self.assertEqual((progress['queue_position'], progress['active_jobs'], progress['max_jobs']), (position, 10, 10))
            self.assertIn(f'Queue position {position}', progress['message'])
            self.assertIn('10 of 10 execution slots occupied', progress['message'])
        self.assertEqual(self.restart().tick()['errors'], [])
        self.assertEqual(len(self.units.launched), 10)

    def test_receipt_window_releases_slot_and_restarts_exactly_once(self):
        jobs = self.jobs(12)
        self.daemon.tick()
        self.receipt(jobs[0])
        self.assertEqual(self.restart().tick()['errors'], [])
        self.assertEqual(len(self.units.launched), 11)
        self.assertEqual(self.operation(jobs[0])['state'], 'complete')
        self.assertEqual(self.store.read('job', jobs[0]['job_id'])['state'], 'complete')
        self.assertEqual(self.store.read('job', jobs[11]['job_id'])['progress']['queue_position'], 1)
        self.restart().tick()
        self.assertEqual(len(self.units.launched), 11)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE object_id=? AND kind='terminal_receipt_recovered'",
                                       (jobs[0]['job_id'],)).fetchone()[0], 1)

    def test_cancelled_queued_job_is_skipped_when_slot_opens(self):
        jobs = self.jobs(12)
        self.daemon.tick()
        with self.store.transaction() as db:
            current = self.store.get(db, 'job', jobs[10]['job_id'])
            current['state'] = 'cancelled'
            self.store.put(db, 'job', current)
        self.receipt(jobs[0])
        self.daemon.tick()
        self.assertEqual(self.units.launched[-1], 'bio-workbench-job-' + jobs[11]['job_id'] + '.service')
        self.assertNotIn('bio-workbench-job-' + jobs[10]['job_id'] + '.service', self.units.launched)

    def test_invalid_receipt_keeps_slot_and_never_relaunches(self):
        self.config['max_jobs'] = 1
        jobs = self.jobs(2)
        self.daemon.tick()
        original = self.receipt(jobs[0])
        folder = self.store.directory('operations', jobs[0]['job_id'])
        for key, value in [('kind', 'validation'), ('object_id', uid()), ('unit', 'other.service'),
                           ('invocation_id', uid()), ('intent_sha256', '0' * 64), ('started_at', 'different'),
                           ('finished_at', None), ('state', 'running'), ('state', [])]:
            with self.subTest(key=key, value=value):
                write_json(folder / 'terminal.json', {**original, key: value})
                result = self.restart().tick()
                self.assertEqual(len(result['errors']), 1, result)
                self.assertEqual(self.operation(jobs[0])['state'], 'running')
                self.assertEqual(len(self.units.launched), 1)
        write_json(folder / 'terminal.json', original)
        started = read_json(folder / 'started.json')
        write_json(folder / 'started.json', {**started, 'invocation_id': uid()})
        self.assertEqual(len(self.restart().tick()['errors']), 1)
        self.assertEqual(len(self.units.launched), 1)
        write_json(folder / 'started.json', started)
        self.assertEqual(self.restart().tick()['errors'], [])
        self.assertEqual(len(self.units.launched), 2)

    def test_valid_receipt_does_not_release_a_still_running_unit(self):
        self.config['max_jobs'] = 1
        jobs = self.jobs(2)
        self.daemon.tick()
        self.receipt(jobs[0], ended=False)
        self.daemon.tick()
        self.assertEqual(len(self.units.launched), 1)
        self.units.states[self.operation(jobs[0])['unit']]['SubState'] = 'exited'
        self.daemon.tick()
        self.assertEqual(len(self.units.launched), 2)

    def test_bound_receipt_recovers_after_unit_was_forgotten(self):
        self.config['max_jobs'] = 1
        jobs = self.jobs(2)
        self.daemon.tick()
        self.receipt(jobs[0])
        self.units.states.pop(self.operation(jobs[0])['unit'])
        self.assertEqual(self.restart().tick()['errors'], [])
        self.assertEqual(self.operation(jobs[0])['state'], 'complete')
        self.assertEqual(len(self.units.launched), 2)

    def test_resident_client_and_request_share_one_slot(self):
        self.config['max_jobs'] = 2
        jobs = self.jobs(3)
        self.daemon.tick()
        # Only one existing job continues natively; the other has completed.
        self.receipt(jobs[0], pending=True, ended=False)
        self.receipt(jobs[1])
        with patch('workbench.runner.reconcile_resident', return_value=False):
            self.assertEqual(self.restart().tick()['errors'], [])
        self.assertEqual(len(self.units.launched), 3)
        self.assertEqual(len(self.daemon.snapshot()[2]), 2)
        self.assertTrue(self.store.read('job', jobs[0]['job_id'])['_resident_pending'])

    def test_resident_completion_releases_slot_during_same_tick(self):
        self.config['max_jobs'] = 1
        jobs = self.jobs(2)
        self.daemon.tick()
        self.receipt(jobs[0], pending=True)
        with patch('workbench.runner.reconcile_resident', return_value=False):
            self.daemon.tick()
        self.assertEqual(len(self.units.launched), 1)

        def completed(store, job):
            with store.transaction() as db:
                current = store.get(db, 'job', job['job_id'])
                current['state'] = 'complete'
                current.pop('_resident_pending')
                store.put(db, 'job', current)
            return True
        with patch('workbench.runner.reconcile_resident', side_effect=completed):
            self.assertEqual(self.restart().tick()['errors'], [])
        self.assertEqual(len(self.units.launched), 2)

    def test_unit_replacement_does_not_release_receipt_slot(self):
        self.config['max_jobs'] = 1
        jobs = self.jobs(2)
        self.daemon.tick()
        self.receipt(jobs[0])
        self.units.states[self.operation(jobs[0])['unit']]['InvocationID'] = uid()
        self.assertEqual(len(self.restart().tick()['errors']), 1)
        self.assertEqual(self.operation(jobs[0])['state'], 'running')
        self.assertEqual(len(self.units.launched), 1)

    def test_invalid_receipt_with_inconsistent_job_keeps_slot(self):
        self.config['max_jobs'] = 1
        jobs = self.jobs(2)
        self.daemon.tick()
        self.receipt(jobs[0])
        with self.store.transaction() as db:
            current = self.store.get(db, 'job', jobs[0]['job_id'])
            current['state'] = 'running'
            self.store.put(db, 'job', current)
        self.assertEqual(len(self.restart().tick()['errors']), 1)
        self.assertEqual(self.operation(jobs[0])['state'], 'running')
        self.assertEqual(len(self.units.launched), 1)


if __name__ == '__main__':
    unittest.main()
