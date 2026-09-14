"""Offline shared-ledger tests; no credentials or cloud API calls."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import aws_budget as module


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 100000.0
        self.token = 'a' * 32
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.budget = module.Budget(self.root, clock=lambda: self.now)
        state = module.DC['new_state'](self.root/'ledger.tsv', self.now, '606')
        state['last_watchdog'] = self.now
        self.budget.store.save(state)
        self.budget.heartbeat()

    def read(self):
        return json.loads(self.budget.store.path.read_text())

    def refresh(self):
        with self.budget.store.locked(self.now) as state:
            state['last_watchdog'] = self.now
        self.budget.heartbeat()

    def reserve(self, **extra):
        values = dict(token=self.token, compute_hourly=7.2626,
                      additional_storage_hourly=212/730, hours=6)
        values.update(extra)
        return self.budget.reserve(**values)

    def start(self):
        self.reserve()
        self.budget.storage(self.token, 'vol-0123456789abcdef0', 204/730, self.now)
        self.budget.storage(self.token, 'vol-0123456789abcdef1', 8/730, self.now)
        self.budget.running(self.token, 'i-0123456789abcdef0', 7.2626)

    def test_authorized_ceiling_preserves_prior_history(self):
        before = self.read()
        self.assertEqual(self.reserve()['ceiling'], 1000)
        after = self.read()
        for key in before:
            if key != 'aws':
                self.assertEqual(before[key], after[key])
        with patch.dict(os.environ, DC_BUDGET_CEILING='5000'):
            self.assertEqual(module.Budget(self.root).ceiling, 1000)

    def test_shared_ceiling_counts_verda_history_and_aws_commitments(self):
        state = self.read()
        state['historical_correction'] = 955
        self.budget.store.save(state)
        with self.assertRaisesRegex(module.Error, 'BUDGET HALT'):
            self.reserve()
        self.assertFalse(self.read()['aws']['reservations'])

    def test_stop_retains_storage_and_start_preserves_compute_history(self):
        self.start()
        self.now += 3600
        expected = 606 + 7.2626 + 212/730
        self.assertAlmostEqual(self.budget.snapshot()['spent'], expected)
        self.budget.observe_instance('i-0123456789abcdef0', 'stopped')
        self.budget.release(self.token, 'i-0123456789abcdef0', 'stopped')
        self.now += 3600
        self.refresh()
        self.assertAlmostEqual(self.budget.snapshot()['spent'], expected + 212/730)
        self.assertAlmostEqual(self.budget.snapshot()['hourly'], 212/730)
        self.reserve(token='b'*32, additional_storage_hourly=0, hours=2)
        self.budget.storage('b'*32, 'vol-0123456789abcdef0', 204/730, 100000.0)
        self.budget.storage('b'*32, 'vol-0123456789abcdef1', 8/730, 100000.0)
        self.budget.running('b'*32, 'i-0123456789abcdef0', 7.2626)
        self.now += 3600
        self.assertAlmostEqual(self.budget.snapshot()['spent'], expected + 2*212/730 + 7.2626)

    def test_lost_reservation_response_retry_does_not_extend_or_duplicate(self):
        first = self.reserve()
        self.now += 30
        retry = self.reserve()
        self.assertEqual(first['deadline'], retry['deadline'])
        self.assertEqual(len(self.read()['aws']['reservations']), 1)
        with self.assertRaisesRegex(module.Error, 'differs'):
            self.reserve(hours=2)

    def test_own_pending_cost_is_counted_but_another_pending_blocks(self):
        self.reserve()
        self.now += 60
        self.assertGreater(self.budget.check(self.token)['budget']['spent'], 606)
        with self.assertRaisesRegex(module.Error, 'Unresolved'):
            self.reserve(token='b'*32)

    def test_stale_either_watchdog_blocks_spending(self):
        self.now += 181
        with self.assertRaisesRegex(module.Error, 'watchdog is stale'):
            self.reserve()
        self.refresh()
        self.reserve()
        self.now += 181
        with self.assertRaisesRegex(module.Error, 'watchdog is stale'):
            self.budget.check(self.token)

    def test_watchdog_update_while_waiting_for_lock_remains_fresh(self):
        self.reserve()
        original = self.budget.store.locked

        @contextmanager
        def contended(now):
            self.now += 2
            with original(self.now) as state:
                state['last_watchdog'] = self.now
                state['aws']['last_watchdog'] = self.now
                yield state

        with patch.object(self.budget.store, 'locked', contended):
            self.assertEqual(self.budget.check(self.token)['status'], 'pending')
        # Check did not grant an extension while accepting the fresh heartbeat.
        self.assertEqual(self.read()['aws']['reservations'][self.token]['deadline'], 121600)

    def test_reservation_clock_begins_after_admission_wait(self):
        original = self.budget.store.locked

        @contextmanager
        def contended(now):
            self.now += 2
            with original(self.now) as state:
                state['last_watchdog'] = self.now
                state['aws']['last_watchdog'] = self.now
                yield state

        with patch.object(self.budget.store, 'locked', contended):
            result = self.reserve()
        self.assertEqual(result['deadline'], 121602)

    def test_lock_wait_does_not_hide_expiration_or_a_future_heartbeat(self):
        self.reserve()
        original = self.budget.store.locked

        @contextmanager
        def expired_while_waiting(now):
            self.now = 121601
            with original(self.now) as state:
                state['last_watchdog'] = self.now
                state['aws']['last_watchdog'] = self.now
                yield state

        with patch.object(self.budget.store, 'locked', expired_while_waiting):
            with self.assertRaisesRegex(module.Error, 'closed or expired'):
                self.budget.check(self.token)
        self.now = 100010
        with original(self.now) as state:
            state['aws']['last_watchdog'] = self.now + 1
        with self.assertRaisesRegex(module.Error, 'watchdog is stale'):
            self.budget.check(self.token)

    def test_unconfirmed_stop_or_unbound_storage_cannot_release(self):
        self.reserve()
        self.budget.running(self.token, 'i-0123456789abcdef0', 7.2626)
        with self.assertRaisesRegex(module.Error, 'does not match'):
            self.budget.release(self.token, 'i-0123456789abcdef0', 'stopped')
        self.budget.observe_instance('i-0123456789abcdef0', 'stopped')
        with self.assertRaisesRegex(module.Error, 'Unresolved EBS'):
            self.budget.release(self.token, 'i-0123456789abcdef0', 'stopped')

    def test_definite_rejection_retains_allocated_database_cost(self):
        self.reserve()
        self.budget.storage(self.token, 'vol-0123456789abcdef0', 204/730, self.now)
        with self.assertRaisesRegex(module.Error, 'Uncertain'):
            self.budget.cancel_unallocated(self.token, no_instance_confirmed=False,
                                          no_pending_storage_confirmed=True)
        self.budget.cancel_unallocated(self.token, no_instance_confirmed=True,
                                      no_pending_storage_confirmed=True)
        self.now += 3600
        self.assertAlmostEqual(self.budget.snapshot()['spent'], 606 + 204/730)
        self.assertEqual(self.budget.snapshot()['reserved'], 0)

    def test_verda_inventory_absence_cannot_retire_aws_cost(self):
        self.start()
        with self.budget.store.locked(self.now) as state:
            module.DC['reconcile'](state, ([], [], []), self.now)
        self.assertAlmostEqual(self.budget.snapshot()['hourly'], 7.2626 + 212/730)

    def test_external_restart_is_uncertain_and_charged_since_last_observation(self):
        self.start()
        self.budget.observe_instance('i-0123456789abcdef0', 'stopped')
        self.budget.release(self.token, 'i-0123456789abcdef0', 'stopped')
        self.now += 3600
        report = self.budget.observe_instance('i-0123456789abcdef0', 'running')
        self.assertTrue(report['uncertain'])
        self.assertAlmostEqual(report['spent'], 606 + 7.2626 + 212/730)

    def test_wrong_account_and_missing_ledger_fail_closed(self):
        with self.assertRaisesRegex(module.Error, 'account or region changed'):
            module.Budget(self.root, account_id='000000000000').heartbeat()
        self.budget.store.path.unlink()
        with self.assertRaisesRegex(module.Error, 'refusing to reset'):
            self.reserve()
        with self.assertRaisesRegex(module.Error, 'refusing to reset'):
            self.budget.running(self.token, 'i-0123456789abcdef0', 7.2626)
        self.assertFalse(self.budget.store.path.exists())

    def test_corrupt_aws_cost_cannot_be_silently_omitted(self):
        self.start()
        state = self.read()
        state['aws']['resources']['instance:i-0123456789abcdef0']['cost'] = -1
        self.budget.store.save(state)
        with self.assertRaises(module.Error):
            self.budget.snapshot()

    def test_ec2_creation_precision_reconciliation_never_loses_or_duplicates_cost(self):
        self.reserve()
        self.now += 1
        rate = 204/730
        ident = 'vol-0123456789abcdef0'
        self.budget.storage(self.token, ident, rate, 100000.003)
        before = self.read()['aws']['resources']['volume:'+ident]
        self.budget.storage(self.token, ident, rate, 100000.0)
        first = copy.deepcopy(self.read()['aws'])
        self.assertEqual(first['resources']['volume:'+ident]['created'], 100000.0)
        self.assertAlmostEqual(first['resources']['volume:'+ident]['cost'], before['cost'] + .003*rate/3600)
        self.budget.storage(self.token, ident, rate, 100000.003)
        self.budget.storage(self.token, ident, rate, 100000.0)
        self.assertEqual(first, self.read()['aws'])
        with self.assertRaisesRegex(module.Error, 'differs'):
            self.budget.storage(self.token, ident, rate, 99999.999)
        with self.assertRaisesRegex(module.Error, 'differs'):
            self.budget.storage(self.token, ident, rate+1, 100000.0)

    def test_different_precise_creation_timestamps_still_fail_closed(self):
        self.reserve()
        self.now += 1
        self.budget.storage(self.token, 'vol-0123456789abcdef0', 204/730, 100000.003)
        with self.assertRaisesRegex(module.Error, 'differs'):
            self.budget.storage(self.token, 'vol-0123456789abcdef0', 204/730, 100000.004)


if __name__ == '__main__':
    unittest.main()
