import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest import mock

import session
import session_client as client
import startup


class StartupProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ident = 'a' * 32
        self.invocation = 'b' * 32
        self.state = self.root / 'sessions' / self.ident
        self.state.mkdir(parents=True)
        self.run = self.root / 'results' / 'msa-20260910-230000-1234'
        self.run.mkdir(parents=True)
        self.submit = self.root / 'bio-submit'
        self.submit.write_text('pinned launcher fixture')
        tools = self.state / 'tools'
        for name in ['msa/session.py', 'msa/session_client.py', 'msa/panel.py', 'msa/prepared.py',
                     'msa/server.py', 'msa/databases.py', 'recipes/_common.sh', 'rf3/msa.py']:
            path = tools / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        helper = tools / 'msa/startup.py'
        helper.write_bytes(Path(startup.__file__).read_bytes())
        self.intent = dict(schema=1, kind='managed-private-msa-session', session_id=self.ident,
                           unit='bio-msa-session-' + self.ident + '.service', tools=str(tools),
                           sources=session.sources(tools), submit_sha256=session.sha(self.submit),
                           argv=[str(self.submit), 'msa', '--sub', 'session', '--timeout', '300'])
        session.atomic(self.state / 'intent.json', self.intent)
        session.atomic(self.state.parent / 'active.json', dict(session_id=self.ident,
                       intent_sha256=session.sha(self.state / 'intent.json')))
        session.atomic(self.state / 'start-intent.json', dict(command=['systemd-run', *self.intent['argv']]))
        self.live = dict(LoadState='loaded', ActiveState='active', MainPID='1234', ControlPID='0',
                         InvocationID=self.invocation,
                         Description='Managed private MSA session ' + self.ident,
                         ExecStart='{ argv[]=' + shlex.join(self.intent['argv']) + ' ; }')
        self.stopped = dict(self.live, ActiveState='failed', MainPID='0')
        patches = [mock.patch.dict(os.environ, BIO_MSA_SESSION_ID=self.ident, INVOCATION_ID=self.invocation),
                   mock.patch.object(startup, '__file__', str(helper)),
                   mock.patch.object(client, 'unit_state', return_value=self.live)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def finished(self, status=4):
        startup.begin(self.state, self.run)
        return startup.finish(self.state, status)

    def test_capacity_failure_has_bound_no_allocation_proof_only_after_exit(self):
        attempt = startup.begin(self.state, self.run)
        self.assertEqual(attempt['submit_sha256'], self.intent['submit_sha256'])
        self.assertEqual(attempt['invocation_id'], self.invocation)
        self.assertFalse((self.state / 'no-allocation.json').exists())
        proof = startup.finish(self.state, 4)
        self.assertEqual(proof['reason'], 'capacity_timeout')
        self.assertEqual(proof['attempt_sha256'], session.sha(self.state / 'attempt.json'))
        self.assertEqual(startup.validate_no_allocation(self.state, self.intent, self.stopped), proof)
        self.assertEqual(startup.validate_no_allocation(self.state, self.intent, {'LoadState': 'not-found'}), proof)

    def test_early_failure_and_cancellation_are_distinct_from_capacity_timeout(self):
        startup.begin(self.state, self.run)
        for status, reason in [(2, 'preallocation_failed'), (143, 'cancelled'), (130, 'cancelled')]:
            with self.subTest(status=status):
                proof = startup.finish(self.state, status)
                self.assertEqual(proof['reason'], reason)
                self.assertEqual(startup.validate_no_allocation(self.state, self.intent, self.stopped), proof)
                (self.state / 'no-allocation.json').unlink()

    def test_mark_allocation_is_durable_and_prevents_absence_receipt_for_lost_reply(self):
        startup.begin(self.state, self.run)
        marker = startup.mark_allocation(self.state)
        self.assertEqual(marker, session.load(self.state / 'allocation-started.json'))
        self.assertEqual(startup.mark_allocation(self.state), marker)
        self.assertIsNone(startup.finish(self.state, 4))
        self.assertFalse((self.state / 'no-allocation.json').exists())
        with self.assertRaises(ValueError):
            startup.validate_no_allocation(self.state, self.intent, self.stopped)

    def test_terminal_receipt_cannot_be_followed_by_allocation(self):
        proof = self.finished()
        self.assertEqual(startup.finish(self.state, 4), proof)
        with self.assertRaisesRegex(ValueError, 'already finished'):
            startup.mark_allocation(self.state)
        self.assertFalse((self.state / 'allocation-started.json').exists())

    def test_finished_receipt_does_not_prove_safety_while_unit_lives(self):
        self.finished()
        with self.assertRaisesRegex(ValueError, 'still live'):
            startup.validate_no_allocation(self.state, self.intent, self.live)

    def test_changed_invocation_cannot_begin_or_finish_or_retire(self):
        self.finished()
        with mock.patch.dict(os.environ, INVOCATION_ID='c' * 32):
            with self.assertRaises(ValueError):
                startup.finish(self.state, 4)
        with self.assertRaisesRegex(ValueError, 'invocation changed'):
            startup.validate_no_allocation(self.state, self.intent,
                                           dict(self.stopped, InvocationID='c' * 32))
        with self.assertRaisesRegex(ValueError, 'invocation changed'):
            startup.validate_no_allocation(self.state, self.intent,
                                           dict(self.stopped, InvocationID=''))

    def test_stopping_unit_cannot_start_allocation(self):
        startup.begin(self.state, self.run)
        self.live['ActiveState'] = 'deactivating'
        with self.assertRaisesRegex(ValueError, 'already stopping'):
            startup.mark_allocation(self.state)
        self.assertEqual(startup.finish(self.state, 143)['reason'], 'cancelled')

    def test_missing_legacy_attempt_cannot_produce_or_validate_absence(self):
        with self.assertRaises(ValueError):
            startup.finish(self.state, 4)
        with self.assertRaises(ValueError):
            startup.validate_no_allocation(self.state, self.intent, self.stopped)

    def test_allocation_evidence_including_dangling_symlink_blocks_retirement(self):
        self.finished()
        for path in (self.state / 'allocation-started.json', self.state / 'launch.json', self.run / 'job.json'):
            with self.subTest(path=path.name):
                path.symlink_to(self.root / 'missing')
                with self.assertRaisesRegex(ValueError, 'Allocation may have started'):
                    startup.validate_no_allocation(self.state, self.intent, self.stopped)
                path.unlink()

    def test_receipt_tampering_never_unlocks_recovery(self):
        proof = self.finished()
        for key, value in [('invocation_id', 'c' * 32), ('attempt_sha256', '0' * 64),
                           ('session_id', 'c' * 32), ('run_dir', '/tmp/elsewhere'),
                           ('reason', 'preallocation_failed'), ('no_allocation_attempted', False)]:
            with self.subTest(key=key):
                session.atomic(self.state / 'no-allocation.json', dict(proof, **{key: value}))
                with self.assertRaises(ValueError):
                    startup.validate_no_allocation(self.state, self.intent, self.stopped)

    def test_attempt_source_and_submit_are_pinned(self):
        self.finished()
        self.submit.write_text('changed launcher')
        with self.assertRaisesRegex(ValueError, 'launcher changed'):
            startup.validate_no_allocation(self.state, self.intent, self.stopped)
        self.submit.write_text('pinned launcher fixture')
        (self.state / 'tools/msa/startup.py').write_text('changed helper')
        with self.assertRaisesRegex(ValueError, 'source snapshot changed'):
            startup.validate_no_allocation(self.state, self.intent, self.stopped)

    def test_active_pointer_and_saved_observation_are_required(self):
        self.finished()
        (self.state / 'observed-start.json').unlink()
        with self.assertRaises(ValueError):
            startup.validate_no_allocation(self.state, self.intent, self.stopped)
        with self.assertRaises(ValueError):
            startup.finish(self.state, 0)

    def test_unsafe_run_directory_and_unpinned_helper_cannot_begin(self):
        with self.assertRaisesRegex(ValueError, 'run directory'):
            startup.begin(self.state, self.root)
        with mock.patch.object(startup, '__file__', str(self.root / 'untrusted.py')):
            with self.assertRaisesRegex(ValueError, 'pinned managed session'):
                startup.begin(self.state, self.run)
        self.assertFalse((self.state / 'attempt.json').exists())


if __name__ == '__main__':
    unittest.main()
