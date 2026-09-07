import json
from pathlib import Path
import sys
import tempfile
import unittest

from md.worker import execute, verify_resume
from workbench.common import inventory
from md.runtime.restore import expected_fingerprint


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        (self.work / '_runtime.json').write_text(json.dumps({'schema': 'bio-md-runtime.v1',
                'variant': 'cpu', 'fingerprint': expected_fingerprint('cpu')}))
        self.runtime = self.root / 'runtime'
        (self.runtime / 'bin').mkdir(parents=True)
        executable = self.runtime / 'bin/gmx'
        executable.write_text('#!' + sys.executable + '\n' + '''
from pathlib import Path
import sys
if sys.argv[1] == 'grompp':
    p=Path('preparation-count')
    p.write_text(str(int(p.read_text())+1) if p.exists() else '1')
    Path('run.tpr').write_bytes(b'immutable native input')
elif sys.argv[1] == 'mdrun':
    Path('run.cpt').write_bytes(b'checkpoint with saved velocities and RNG')
    Path('native-argv.txt').write_text(' '.join(sys.argv[1:]))
    if '-cpi' not in sys.argv:
        sys.exit(75)
    Path('run.gro').write_bytes(b'continued final coordinates')
''')
        executable.chmod(0o700)
        self.plan = {'schema': 'bio-md-plan.v1', 'stages': [
            {'id': 'prepare', 'argv': ['gmx', 'grompp'], 'cwd': '.', 'dependencies': [], 'inputs': [], 'outputs': ['run.tpr']},
            {'id': 'production', 'argv': ['gmx', 'mdrun'], 'cwd': '.', 'dependencies': ['prepare'],
             'inputs': ['run.tpr'], 'outputs': ['run.gro', 'run.cpt'], 'checkpoint': 'run.cpt'}]}

    def test_explicit_resume_preserves_completed_stage_and_uses_native_checkpoint(self):
        self.assertEqual(execute(self.plan, self.work, self.runtime), 1)
        self.assertEqual(execute(self.plan, self.work, self.runtime), 0)
        self.assertEqual((self.work / 'preparation-count').read_text(), '1')
        self.assertIn('-cpi run.cpt -append', (self.work / 'native-argv.txt').read_text())
        self.assertEqual(json.loads((self.work / '.stages/production.json').read_text())['attempt'], 2)
        self.assertEqual(execute(self.plan, self.work, self.runtime), 0)
        self.assertEqual((self.work / 'preparation-count').read_text(), '1')

    def test_changed_native_input_cannot_continue_from_checkpoint(self):
        self.assertEqual(execute(self.plan, self.work, self.runtime), 1)
        (self.work / 'run.tpr').write_bytes(b'another Hamiltonian')
        with self.assertRaisesRegex(ValueError, 'output changed'):
            execute(self.plan, self.work, self.runtime)
        self.assertEqual((self.work / 'preparation-count').read_text(), '1')

    def test_changed_native_checkpoint_is_rejected(self):
        self.assertEqual(execute(self.plan, self.work, self.runtime), 1)
        (self.work / 'run.cpt').write_bytes(b'checkpoint from another trajectory')
        with self.assertRaisesRegex(ValueError, 'Checkpoint changed'):
            execute(self.plan, self.work, self.runtime)

    def test_interrupted_stage_without_checkpoint_cannot_restart_implicitly(self):
        self.assertEqual(execute(self.plan, self.work, self.runtime), 1)
        receipt_path = self.work / '.stages/production.json'
        receipt = json.loads(receipt_path.read_text())
        receipt['state'] = 'interrupted'
        receipt_path.write_text(json.dumps(receipt))
        (self.work / 'run.cpt').unlink()
        with self.assertRaisesRegex(ValueError, 'no checkpoint'):
            execute(self.plan, self.work, self.runtime)

    def test_checkpoint_requires_identical_plumed_history(self):
        self.plan['stages'][1]['restart_files'] = ['HILLS', 'COLVAR']
        for name in ('HILLS', 'COLVAR'):
            (self.work / name).write_text('native bias history\n')
        self.assertEqual(execute(self.plan, self.work, self.runtime), 1)
        (self.work / 'HILLS').write_text('history from another bias\n')
        with self.assertRaisesRegex(ValueError, 'Bias restart history'):
            execute(self.plan, self.work, self.runtime)

    def test_resume_verification_is_read_only_and_checks_completed_outputs(self):
        self.assertEqual(execute(self.plan, self.work, self.runtime), 1)
        before = inventory(self.work)
        result = verify_resume(self.plan, self.work)
        self.assertEqual(result['checkpoint_stages'], ['production'])
        self.assertEqual(inventory(self.work), before)
        self.assertFalse(result['paid_compute_requested'])
        (self.work / 'run.tpr').write_bytes(b'changed completed preparation')
        with self.assertRaisesRegex(ValueError, 'output changed'):
            verify_resume(self.plan, self.work)

    def test_running_and_orphaned_checkpoint_receipts_reject_before_continuation(self):
        self.assertEqual(execute(self.plan, self.work, self.runtime), 1)
        path = self.work / '.stages/production.json'
        receipt = json.loads(path.read_text()); receipt['state'] = 'running'
        path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'Unsealed running'):
            verify_resume(self.plan, self.work)
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'no receipt'):
            verify_resume(self.plan, self.work)

    def test_failed_dynamics_without_checkpoint_never_restarts_from_zero(self):
        self.assertEqual(execute(self.plan, self.work, self.runtime), 1)
        (self.work / 'run.cpt').unlink()
        with self.assertRaisesRegex(ValueError, 'no checkpoint'):
            verify_resume(self.plan, self.work)
        with self.assertRaisesRegex(ValueError, 'no checkpoint'):
            execute(self.plan, self.work, self.runtime)

    def test_changed_runtime_pins_reject_a_valid_native_checkpoint(self):
        self.assertEqual(execute(self.plan, self.work, self.runtime), 1)
        path = self.work / '_runtime.json'
        value = json.loads(path.read_text()); value['fingerprint'] = '0' * 64
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'runtime fingerprint differs'):
            verify_resume(self.plan, self.work)

    def test_head_resume_verification_rejects_changed_checkpoint_and_bias(self):
        self.plan['stages'][1]['restart_files'] = ['HILLS', 'COLVAR']
        for name in ('HILLS', 'COLVAR'):
            (self.work / name).write_bytes(b'retained native history')
        self.assertEqual(execute(self.plan, self.work, self.runtime), 1)
        for name in ('run.cpt', 'HILLS', 'COLVAR'):
            path = self.work / name; original = path.read_bytes()
            for replacement in (b'changed history or state', None):
                if replacement is None:
                    path.unlink()
                else:
                    path.write_bytes(replacement)
                with self.subTest(name=name, replacement=replacement), self.assertRaises(ValueError):
                    verify_resume(self.plan, self.work)
                path.write_bytes(original)

    def test_cancellation_flag_before_execution_starts_no_native_stage(self):
        flag = self.root / '.cancel-requested'; flag.write_text('cancel')
        self.assertEqual(execute(self.plan, self.work, self.runtime, cancel_file=flag), 75)
        self.assertFalse((self.work / 'preparation-count').exists())

    def test_cancellation_flag_during_dynamics_seals_native_checkpoint(self):
        flag = self.root / '.cancel-requested'
        executable = self.runtime / 'bin/gmx'
        prefix = executable.read_text().split("elif sys.argv[1] == 'mdrun':")[0]
        executable.write_text(prefix + "elif sys.argv[1] == 'mdrun':\n" + '''
    import signal,time
    def stopped(signum, frame):
        Path('run.cpt').write_bytes(b'checkpoint saved after managed termination')
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stopped)
    Path(''' + repr(str(flag)) + ''').write_text('cancel requested while sampling')
    while True:
        time.sleep(.01)
''')
        self.assertEqual(execute(self.plan, self.work, self.runtime, cancel_file=flag), 75)
        receipt = json.loads((self.work / '.stages/production.json').read_text())
        self.assertEqual(receipt['state'], 'interrupted')
        self.assertIn('checkpoint', receipt)
        self.assertEqual(verify_resume(self.plan, self.work)['checkpoint_stages'], ['production'])


if __name__ == '__main__':
    unittest.main()
