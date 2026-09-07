"""Pre-rental MD gates, immutable cached evidence, and pinned runtime selection."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from md.admission import check
from md.bundle import pack
from md.launch import select
from md.runtime.restore import restore, expected_fingerprint
from md.test_protocols import plumed_request, TOP
from md.protocols import prepare as prepare_plan
from workbench.common import Error, digest


HEAD = Path(__file__).resolve().parent.parent


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tools = self.root / 'tools'
        (self.tools / 'md').mkdir(parents=True)
        for source in (HEAD / 'md').glob('*.py'):
            if not source.name.startswith('test_'):
                shutil.copyfile(source, self.tools / 'md' / source.name)
        (self.tools / 'workbench').symlink_to(HEAD / 'workbench', target_is_directory=True)
        self.runtime = self.root / 'runtime'
        (self.runtime / 'bin').mkdir(parents=True)
        (self.runtime / 'manifest.json').write_text('{"schema":"test-runtime","fingerprint":"fixture"}\n')
        (self.runtime / 'activate.sh').write_text(': # Isolated admission CLI fixture; no environment changes.\n')
        (self.runtime / 'bin/python').symlink_to(sys.executable)
        self.state = self.root / 'admissions'
        self.bundle = self.root / 'input.tar.gz'
        self.assets = self.root / 'assets'
        self.assets.mkdir()
        (self.assets / 'prepared.top').write_text(TOP)
        (self.assets / 'prepared.gro').write_text('Deliberately invalid native-coordinate fixture\n0\n10 10 10\n')
        self.request = plumed_request()
        self.write_bundle()

    def write_bundle(self):
        pack(self.request, {p.name: p for p in self.assets.iterdir()}, self.bundle)

    def admit(self):
        return check(self.bundle, self.tools, self.runtime, self.state)

    def cached_root(self, receipt):
        return self.state / digest(receipt['identity'])

    def test_successful_native_preflight_is_reused_without_engine_execution(self):
        with patch('md.admission.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as native:
            original = self.admit()
            self.assertEqual(self.admit(), original)
            self.assertEqual(native.call_count, 1)
        self.assertFalse(original['paid_compute_requested'])
        self.assertFalse(original['molecular_dynamics_executed'])
        self.assertEqual(original['state'], 'complete')

    def test_changed_cached_engine_evidence_is_rejected_without_rerunning(self):
        with patch('md.admission.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as native:
            receipt = self.admit()
            (self.cached_root(receipt) / 'native-preflight.log').write_bytes(b'changed engine evidence')
            with self.assertRaisesRegex(Error, 'evidence changed'):
                self.admit()
            self.assertEqual(native.call_count, 1)

    def test_changed_cached_receipt_identity_or_inventory_is_rejected(self):
        with patch('md.admission.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as native:
            receipt = self.admit()
            path = self.cached_root(receipt) / 'receipt.json'
            for change in ('identity', 'files', 'state'):
                altered = copy.deepcopy(receipt)
                if change == 'identity':
                    altered['identity']['bundle_sha256'] = '0' * 64
                elif change == 'files':
                    altered['files'] = {}
                else:
                    altered['state'] = 'failed'
                path.write_text(json.dumps(altered))
                with self.subTest(change=change), self.assertRaises(Error):
                    self.admit()
            self.assertEqual(native.call_count, 1)

    def test_source_runtime_and_bundle_changes_require_new_native_admission(self):
        with patch('md.admission.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as native:
            identities = [self.admit()['identity']]
            with (self.tools / 'md/protocols.py').open('a') as stream:
                stream.write('\n# Changed trusted protocol source.\n')
            identities.append(self.admit()['identity'])
            (self.runtime / 'manifest.json').write_text('{"fingerprint":"another-pinned-runtime"}\n')
            identities.append(self.admit()['identity'])
            self.request['simulation']['seed'] += 1
            self.write_bundle()
            identities.append(self.admit()['identity'])
            self.assertEqual(native.call_count, 4)
            self.assertEqual(len({digest(identity) for identity in identities}), 4)

    def test_failed_native_preflight_remains_failed_on_retry(self):
        with patch('md.admission.subprocess.run', return_value=subprocess.CompletedProcess([], 7)) as native:
            with self.assertRaisesRegex(ValueError, 'Native MD topology preflight failed'):
                self.admit()
            with self.assertRaisesRegex(Error, 'unsuccessful'):
                self.admit()
            self.assertEqual(native.call_count, 1)

    def test_incomplete_cached_attempt_cannot_be_reinterpreted_as_success(self):
        with patch('md.admission.subprocess.run', side_effect=subprocess.TimeoutExpired('native fixture', 500)) as native:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.admit()
            with self.assertRaisesRegex(Error, 'without a receipt'):
                self.admit()
            self.assertEqual(native.call_count, 1)

    def test_invalid_request_rejects_before_native_preflight(self):
        self.request['protocol'] = 'unsupported-protocol'
        self.write_bundle()
        with patch('md.admission.subprocess.run') as native, self.assertRaises(ValueError):
            self.admit()
        native.assert_not_called()

    def test_unsealed_packed_resume_rejects_before_native_preflight_or_rental(self):
        retained = self.root / 'retained-simulation'
        plan = prepare_plan(self.request, self.assets, retained)
        (retained / 'execution-plan.json').write_text(json.dumps(plan))
        (retained / '.stages').mkdir()
        (retained / '_runtime.json').write_text(json.dumps({'schema': 'bio-md-runtime.v1',
            'variant': 'cpu', 'fingerprint': expected_fingerprint('cpu')}))
        # A sealed parser stage may precede the unsealed dynamics checkpoint.
        # These are inert receipt bytes, not a scientific engine fixture.
        from md.worker import fingerprint
        from md.bundle import canonical, digest as bytes_digest
        first = plan['stages'][0]
        for name in first['outputs']:
            path = retained / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'completed preparation fixture')
        (retained / '.stages' / (first['id'] + '.json')).write_text(json.dumps({
            'state': 'complete', 'binding': {'stage': first,
                'inputs': {name: fingerprint(retained / name) for name in first['inputs']},
                'plan_sha256': bytes_digest(canonical(plan))},
            'outputs': {name: fingerprint(retained / name) for name in first['outputs']}}))
        sampling = next(stage for stage in plan['stages'] if stage.get('checkpoint'))
        checkpoint = retained / sampling['checkpoint']
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_bytes(b'native checkpoint exists, but the worker never sealed its receipt')
        pack(self.request, {p.name: p for p in self.assets.iterdir()}, self.bundle,
             resume={p.relative_to(retained).as_posix(): p for p in retained.rglob('*') if p.is_file()})
        with patch('md.admission.subprocess.run') as native, self.assertRaisesRegex(ValueError, 'no sealed stage receipts|no receipt'):
            self.admit()
        native.assert_not_called()

    def test_low_level_submit_rejects_bad_native_bundle_before_provider_calls(self):
        """Run the production shell + admission + worker with isolated native/provider stubs.

        The controlled GROMACS parser failure makes this portable in head CI;
        this is an execution-boundary regression, not engine qualification.
        """
        shell = shutil.which('bash')
        self.assertIsNotNone(shell)
        (self.tools / 'py').mkdir()
        (self.tools / 'py/head_preparation_gate.py').write_text(
            'import sys\nprint("ready", flush=True)\nsys.stdin.read()\n')
        native = self.runtime / 'bin/gmx'
        native.write_text('#!' + sys.executable + '\n' + '''
import os,sys
from pathlib import Path
Path(os.environ['MD_TEST_NATIVE_MARKER']).write_text(' '.join(sys.argv[1:]))
print('Fixture native parser rejected malformed molecular coordinates', file=sys.stderr)
raise SystemExit(7)
''')
        native.chmod(0o700)
        commands = self.root / 'commands'
        commands.mkdir()
        (commands / 'python3').symlink_to(sys.executable)
        for name in ('dc', 'ssh', 'scp', 'rsync', 'curl', 'wget'):
            executable = commands / name
            executable.write_text('#!' + sys.executable + '\n' + '''
import os,sys
from pathlib import Path
Path(os.environ['MD_TEST_PROVIDER_MARKER']).write_text('FORBIDDEN '+sys.argv[0])
raise SystemExit(99)
''')
            executable.chmod(0o700)
        native_marker, provider_marker = self.root / 'native-called', self.root / 'provider-called'
        env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ.get('PATH', ''),
                   BIO_TOOLS_SRC=str(self.tools), BIO_STATE_DIR=str(self.root / 'state'),
                   BIO_CLUSTER_CONFIG=str(self.root / 'absent-cluster.sh'),
                   BIO_MD_CPU_RUNTIME=str(self.runtime), BIO_MD_ADMISSIONS=str(self.state),
                   BIO_RESULTS_DIR=str(self.root / 'results'), BIO_SHARED_MNT=str(self.root / 'shared'),
                   MD_TEST_NATIVE_MARKER=str(native_marker), MD_TEST_PROVIDER_MARKER=str(provider_marker))
        result = subprocess.run([shell, str(HEAD / 'bio-submit.sh'), 'md', '--in', str(self.bundle),
                                 '--timeout', '600', '--execution', 'ephemeral'],
                                env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Native MD topology preflight failed', result.stderr)
        self.assertIn('grompp', native_marker.read_text())
        self.assertFalse(provider_marker.exists(), result.stdout + result.stderr)
        receipts = list(self.state.glob('*/receipt.json'))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())['state'], 'failed')


class LaunchRegistryTests(unittest.TestCase):
    def test_registry_rejects_archive_traversal_and_unpinned_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'runtime.tar.gz').write_bytes(b'pinned archive placeholder')
            for archive, fingerprint in (('../outside.tar.gz', 'known'), ('/absolute.tar.gz', 'known'),
                                         ('runtime.tar.gz', 'wrong')):
                registry = {'schema': 'bio-md-runtime-deployment.v1',
                            'cpu': {'archive': archive, 'fingerprint': fingerprint, 'sha256': '0' * 64}}
                (root / 'current.json').write_text(json.dumps(registry))
                with self.subTest(archive=archive, fingerprint=fingerprint), \
                     patch('md.launch.expected_fingerprint', return_value='known'), self.assertRaises(ValueError):
                    select(root, 'cpu')

    def test_selected_archive_hash_is_verified_before_runtime_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'runtime.tar.gz'
            archive.write_bytes(b'corrupt bytes replacing the published archive')
            expected_sha = hashlib.sha256(b'the immutable published archive').hexdigest()
            registry = {'schema': 'bio-md-runtime-deployment.v1',
                        'cpu': {'archive': archive.name, 'fingerprint': 'known', 'sha256': expected_sha}}
            (root / 'current.json').write_text(json.dumps(registry))
            with patch('md.launch.expected_fingerprint', return_value='known'):
                selected, entry = select(root, 'cpu')
            prefix = root / 'restored'
            with patch('md.runtime.restore.expected_fingerprint', return_value='known'), \
                 self.assertRaisesRegex(ValueError, 'archive SHA-256 differs'):
                restore(selected, entry['sha256'], prefix, fingerprint=entry['fingerprint'], variant='cpu')
            self.assertFalse(prefix.exists())


if __name__ == '__main__':
    unittest.main()
