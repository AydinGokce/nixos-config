"""Resident public queries retain the native parser behind the shared guard."""
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

from inference import frontend, pool
from inference.common import atomic_json, digest, now, read, sha256


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == '/health' else 404)
        self.end_headers()
        self.wfile.write(b'{"service":"bio-public-msa-proxy","schema":1}')

    def log_message(self, *args):
        pass


class PublicPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = Path(__file__).absolute().parent.parent
        self.toolkit = self.root / 'head-tools'
        self.remote_tools = self.root / 'worker-tools'
        files = {'msa/prepared.py': source / 'msa/prepared.py',
                 'py/public_msa_client.py': source.parent.parent / 'modules/bio/py/public_msa_client.py',
                 'inference/adapters/protenix.py': source / 'inference/adapters/protenix.py'}
        for relative, path in files.items():
            for root in (self.toolkit, self.remote_tools):
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
        self.patch_file = patch.object(frontend, '__file__', str(self.toolkit / 'inference/frontend.py'))
        self.patch_file.start()
        self.addCleanup(self.patch_file.stop)
        self.state = self.root / 'state'
        self.target = {'instance_id': 'vm-one', 'boot_id': 'boot-one', 'ip': '192.0.2.1',
                       'key': '/fixture/key', 'known_hosts': '/fixture/known_hosts'}
        self.worker = {'worker_id': 'protenix-one', 'tools_root': str(self.remote_tools),
                       'deadline_epoch': now() + 3600, 'python': sys.executable,
                       'runtime_image': {'sha256': 'a' * 64},
                       'environment': {'PATH': '/fixture/venv/bin:/usr/bin', 'LD_LIBRARY_PATH': '/fixture/lib'},
                       'source_files': {str(self.remote_tools / relative): sha256(self.toolkit / relative)
                                        for relative in files}}
        atomic_json(self.state / 'launches/protenix-one/intent.json', {'target': self.target})
        self.fasta = self.root / 'input.fasta'
        self.fasta.write_text('>chain A\nACDE\n')
        self.destination = self.root / 'request/public-prepared'
        self.args = Namespace(state=self.state, model='protenix', native_seed=None, probe=True,
                              backend='public', bundle=None, refresh_preparation=False,
                              shared=self.root / 'shared', fasta=self.fasta,
                              endpoint='https://api.colabfold.com', chemistry_sha=None)
        self.calls = []

    def proxy(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Health)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        environment = patch.dict(os.environ, {'BIO_PUBLIC_MSA_HEAD_PORT': str(server.server_port)})
        environment.start()
        self.addCleanup(environment.stop)
        return server.server_port

    def remote(self, target, argv, **kwargs):
        self.assertEqual(target, self.target)
        self.calls.append((argv, kwargs))
        if argv[0] == '/usr/bin/cat':
            return (self.target['boot_id'] + '\n').encode()
        return b'fixture output\n'

    def prepare(self):
        with patch.object(frontend, 'remote', side_effect=self.remote):
            return frontend.prepare_public(self.state, self.worker, 'protenix', self.fasta,
                                           self.destination, 1800, self.args.endpoint)

    def test_guarded_native_argv_pins_and_tunnel_cover_the_waited_process(self):
        port = self.proxy()
        self.assertEqual(self.prepare(), self.destination)
        self.assertEqual(len(self.calls), 3)
        boot, (command, options), journal = self.calls
        self.assertNotIn('ssh_options', boot[1])
        self.assertNotIn('ssh_options', journal[1])
        self.assertEqual(command[:4], ['systemd-run', '--quiet', '--wait', '--collect'])
        expected_options = ['-o', 'ExitOnForwardFailure=yes', '-R',
                            '127.0.0.1:18763:127.0.0.1:' + str(port)]
        self.assertEqual(options['ssh_options'], expected_options)
        self.assertIn('Environment=CUDA_VISIBLE_DEVICES=', command)
        self.assertIn('Environment=BIO_PUBLIC_MSA_PROXY=http://127.0.0.1:18763', command)
        self.assertIn('Environment=MMSEQS_SERVICE_HOST_URL=' + self.args.endpoint, command)
        self.assertTrue(any(v.startswith('Environment=BIO_JOB_DEADLINE_EPOCH=') for v in command))
        offset = command.index(self.worker['python'])
        sources = json.loads(command[offset + 3])
        self.assertEqual(set(sources), {str(self.remote_tools / relative)
                                      for relative in ('msa/prepared.py', 'py/public_msa_client.py')})
        self.assertEqual(command[offset + 4:], [str(self.remote_tools / 'py/public_msa_client.py'),
            '--model', 'protenix', '--entrypoint', str(self.remote_tools / 'msa/prepared.py'), '--',
            'prepare', '--model', 'protenix', '--fasta', str(self.fasta), '--out', str(self.destination),
            '--source', 'public', '--server-url', self.args.endpoint])
        receipt = read(self.destination.parent / 'cpu-preparation.json')
        self.assertEqual(receipt['sources'], sources)
        self.assertEqual(receipt['ssh_options'], expected_options)
        self.assertLessEqual(receipt['deadline_epoch'], self.worker['deadline_epoch'] - 60)
        self.assertEqual((self.destination.parent / 'cpu-preparation-journal.log').read_bytes(), b'fixture output\n')

    def test_missing_or_mismatched_pins_fail_before_any_remote_execution(self):
        for relative in ('msa/prepared.py', 'py/public_msa_client.py'):
            path = str(self.remote_tools / relative)
            original = self.worker['source_files'][path]
            for expected in (None, '0' * 64):
                with self.subTest(relative=relative, expected=expected):
                    if expected is None:
                        self.worker['source_files'].pop(path, None)
                    else:
                        self.worker['source_files'][path] = expected
                    with self.assertRaises(frontend.Unavailable):
                        self.prepare()
                    self.assertEqual(self.calls, [])
                    self.assertFalse(self.destination.parent.exists())
            self.worker['source_files'][path] = original

    def test_worker_source_tampering_is_checked_before_helper_or_native_code(self):
        self.proxy()
        self.prepare()
        command = self.calls[1][0]
        command = command[command.index(self.worker['python']):]
        marker = self.root / 'query-started'
        for relative in ('msa/prepared.py', 'py/public_msa_client.py'):
            with self.subTest(relative=relative):
                source = self.remote_tools / relative
                original = source.read_bytes()
                source.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
                result = subprocess.run(command, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'AssertionError', result.stderr)
                self.assertFalse(marker.exists())
                source.write_bytes(original)

    def test_expired_worker_or_changed_boot_never_starts_preparation(self):
        self.worker['deadline_epoch'] = now()
        with self.assertRaises(frontend.Unavailable):
            self.prepare()
        self.assertEqual(self.calls, [])
        self.worker['deadline_epoch'] = now() + 3600
        self.proxy()
        with patch.object(frontend, 'remote', return_value=b'another-boot\n') as remote:
            with self.assertRaisesRegex(ValueError, 'boot identity'):
                frontend.prepare_public(self.state, self.worker, 'protenix', self.fasta,
                                        self.destination, 1800, self.args.endpoint)
        self.assertEqual(remote.call_count, 1)
        self.assertFalse(self.destination.parent.exists())

    def probe(self, cached):
        profile = ({'config_id': 'profile-id'}, {}, self.worker)
        with patch.object(frontend, 'profile', return_value=profile), \
                patch.object(frontend.ArtifactCache, 'lookup', return_value=cached) as lookup, \
                patch.object(frontend, 'remote') as remote, patch.object(frontend, 'prepare_public') as prepare:
            try:
                return frontend.submit(self.args), lookup
            finally:
                remote.assert_not_called()
                prepare.assert_not_called()

    def test_legacy_exact_cached_input_remains_available(self):
        self.worker['source_files'].pop(str(self.remote_tools / 'py/public_msa_client.py'))
        identity = frontend.public_preparation_identity(self.args, self.worker, self.fasta)
        result, lookup = self.probe({'identity': identity})
        self.assertTrue(result['available'])
        lookup.assert_called_once_with(identity)
        self.assertEqual(identity['preparation_toolchain_sha256'], digest(self.worker['source_files']))

    def test_legacy_cache_miss_or_refresh_is_unavailable_before_preparation(self):
        self.worker['source_files'].pop(str(self.remote_tools / 'py/public_msa_client.py'))
        with self.assertRaises(frontend.Unavailable):
            self.probe(None)
        self.args.refresh_preparation = True
        with patch.object(frontend.ArtifactCache, 'lookup') as lookup, \
                patch.object(frontend, 'profile', return_value=({'config_id': 'profile-id'}, {}, self.worker)):
            with self.assertRaises(frontend.Unavailable):
                frontend.submit(self.args)
            lookup.assert_not_called()
        with patch.object(frontend, 'profile', return_value=({'config_id': 'profile-id'}, {}, self.worker)), \
                patch.object(sys, 'argv', ['frontend', '--model', 'protenix', '--fasta', str(self.fasta),
                                          '--probe', '--refresh-preparation']):
            with self.assertRaises(SystemExit) as exit:
                frontend.main()
        self.assertEqual(exit.exception.code, 78)

    def test_legacy_prepared_and_private_probes_need_no_new_helper(self):
        self.worker['source_files'].pop(str(self.remote_tools / 'py/public_msa_client.py'))
        for backend, bundle in (('public', self.root / 'bundle'), ('private', self.root / 'bundle')):
            with self.subTest(backend=backend):
                self.args.backend, self.args.bundle = backend, bundle
                result, lookup = self.probe(None)
                self.assertTrue(result['available'])
                lookup.assert_not_called()

    def test_new_worker_probe_does_not_read_or_create_preparation_cache(self):
        result, lookup = self.probe(None)
        self.assertTrue(result['available'])
        lookup.assert_not_called()

    def test_tunnel_slot_serializes_physical_vm_and_has_bounded_wait(self):
        started, acquired = threading.Event(), threading.Event()
        def waiter():
            started.set()
            with frontend.public_preparation_slot(self.state, dict(self.target), now() + 5):
                acquired.set()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with frontend.public_preparation_slot(self.state, self.target, now() + 5):
                future = executor.submit(waiter)
                self.assertTrue(started.wait(1))
                self.assertFalse(acquired.wait(.15))
                with frontend.public_preparation_slot(self.state, dict(self.target, instance_id='another-vm'), now() + 1):
                    pass
                with self.assertRaises(frontend.Unavailable):
                    with frontend.public_preparation_slot(self.state, self.target, now() + .05):
                        self.fail('A second preparation acquired the occupied worker port')
            future.result(timeout=2)
        self.assertTrue(acquired.is_set())


class ResidentSourceArchiveTests(unittest.TestCase):
    def test_guard_is_archived_and_bound_to_new_worker_source_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            toolkit = root / 'tools'
            for name in ('inference/worker.py', 'msa/prepared.py', 'rf3/requirements.lock',
                         'py/public_msa_client.py', 'inference/test_excluded.py'):
                path = toolkit / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# pinned source: ' + name + '\n')
            hosts = root / 'known_hosts'
            hosts.write_text('fixture pinned host\n')
            target = {'known_hosts': str(hosts), 'known_hosts_sha256': sha256(hosts),
                      'boot_id': 'boot-one', 'hostname': 'fixture-worker', 'instance_id': 'vm-one',
                      'os_id': 'os-one', 'original_deadline_epoch': now() + 3600}
            config = {'model': 'protenix', 'work_dir': str(root / 'work'), 'native_config': {}}
            image = {'path': '/fixture/image', 'sha256': 'a' * 64}
            policy = {'worker_id': 'worker-one', 'deadline_epoch': now() + 1800, 'gpu_uuid': 'GPU-fixture',
                      'runtime_image': image, 'control_storage': {'source': 'fixture', 'mountpoint': str(root / 'control')}}
            archives = []
            def remote(target, argv, **kwargs):
                if argv[0] == 'tar':
                    archives.append(kwargs['input'])
                if 'socket.gethostname' in ' '.join(argv):
                    return json.dumps({'hostname': 'fixture-worker', 'boot_id': 'boot-one',
                                       'gpus': 'GPU-fixture, fixture GPU', 'active': ''}).encode()
                if any(value.endswith('/control_storage.py') for value in argv):
                    return b'{}'
                if any(value.endswith('/runtime_image.py') for value in argv):
                    return json.dumps(dict(image, model='protenix', path_aliases={},
                        specification_sha256='b' * 64, python='/fixture/python',
                        environment={'PATH': '/fixture/bin', 'LD_LIBRARY_PATH': '/fixture/lib'})).encode()
                return b''
            with patch.object(pool, '__file__', str(toolkit / 'inference/pool.py')), \
                    patch.object(pool, 'admission', return_value={}), \
                    patch.object(pool, 'ensure_control_storage', return_value={}), \
                    patch.object(pool, 'remote', side_effect=remote):
                pool.start(target, config, policy, root / 'state')
            session = read(root / 'state/workers/worker-one.json')
            source = session['tools_root'] + '/py/public_msa_client.py'
            self.assertEqual(session['source_files'][source], sha256(toolkit / 'py/public_msa_client.py'))
            self.assertEqual(len(archives), 1)
            with tarfile.open(fileobj=io.BytesIO(archives[0]), mode='r:gz') as archive:
                self.assertEqual(archive.extractfile('py/public_msa_client.py').read(),
                                 (toolkit / 'py/public_msa_client.py').read_bytes())
                self.assertNotIn('inference/test_excluded.py', archive.getnames())
            before = digest({name: sha256(path) for name, path in pool.source_assets(toolkit).items()})
            (toolkit / 'py/public_msa_client.py').write_text('# updated guard\n')
            after = digest({name: sha256(path) for name, path in pool.source_assets(toolkit).items()})
            self.assertNotEqual(before, after)
            (toolkit / 'py/public_msa_client.py').unlink()
            with self.assertRaisesRegex(ValueError, 'required source'):
                pool.source_assets(toolkit)

    def test_remote_forwarding_options_precede_host_and_keep_native_argv_quoted(self):
        target = {'key': '/key', 'known_hosts': '/hosts', 'ip': '192.0.2.1'}
        argv = ['systemd-run', '--wait', '/python', '/input with spaces/parser.py', '--fasta', 'two chains.fasta']
        options = ['-o', 'ExitOnForwardFailure=yes', '-R', '127.0.0.1:18763:127.0.0.1:18763']
        with patch.object(pool.subprocess, 'run') as run:
            run.return_value.stdout = b'done'
            self.assertEqual(pool.remote(target, argv, timeout=900, ssh_options=options), b'done')
        command = run.call_args.args[0]
        self.assertEqual(command[-6:-2], options)
        self.assertEqual(command[-2], 'root@192.0.2.1')
        self.assertEqual(shlex.split(command[-1]), argv)
        self.assertEqual(run.call_args.kwargs['timeout'], 900)


if __name__ == '__main__':
    unittest.main()
