from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from inference import frontend
from inference.adapters.rf3 import default_native_config
from inference.adapters import rf3
from inference.common import atomic_json, configuration_id, digest, inventory, read, sha256


class RF3FrontendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original = self.root / 'original.json'
        atomic_json(self.original, [{'name': 'example', 'components': [
            {'chain_id': 'A', 'seq': 'ACDE', 'chain_type': 'polypeptide(L)', 'is_polymer': True},
            {'chain_id': 'L', 'smiles': 'C[C@H](O)F', 'res_name': 'LIG'}]}])
        self.config = {'model': 'rf3', 'checkpoint': {'path': '/weights/rf3.ckpt', 'sha256': 'a'*64},
            'native_config': default_native_config('/weights/rf3.ckpt'), 'postprocess_mode': 'deferred'}
        self.prepare, self.msa = frontend.rf3_modules()

    def bundle(self, name='bundle', backend='public', ligand='C[C@H](O)F'):
        source = self.root / (name + '-source.json')
        document = read(self.original)
        document[0]['components'][1]['smiles'] = ligand
        atomic_json(source, document)
        search = self.root / (name + '-search')
        (search / 'msas').mkdir(parents=True)
        a3m = search / 'msas/A.a3m'; a3m.write_text('>query\nACDE\n>hit\nAC-E\n')
        row = dict(self.prepare.validate_a3m(a3m, 'ACDE'), path='msas/A.a3m')
        if backend == 'private':
            atomic_json(search / 'database-provenance.json', {'database': {'components': {
                key: 'a'*64 for key in ('uniref30', 'environmental', 'pdb100', 'templates', 'mmcif')}}})
        self.msa.seal(search, 'search.json', {'schema': 1, 'kind': 'rf3-msa-search',
            'queries': {'A': 'ACDE'}, 'source': backend, 'endpoint': 'https://api.example' if backend == 'public' else 'http://localhost:8082',
            'mode': 'env', 'pairing_mode': None, 'pairing_encoding': 'native TaxID headers unchanged',
            'chain_msas': {'A': row}, 'completed_at_epoch': 1})
        entry = self.msa.prepare_input(SimpleNamespace(fasta=None, native_json=source, search_bundle=search,
            out=self.root / name, name='example'))
        return Path(entry), source

    def test_identity_binds_chemical_stereochemistry_backend_and_all_search_bytes(self):
        a, original = self.bundle()
        first = frontend.rf3_preparation(a, original, 'public', self.config, 'r'*64)[-1]
        b, source = self.bundle('other', ligand='C[C@@H](O)F')
        second = frontend.rf3_preparation(b.parent, source, 'public', self.config, 'r'*64)[-1]
        self.assertNotEqual(first['input']['chemistry_sha256'], second['input']['chemistry_sha256'])
        p, source = self.bundle('private', backend='private')
        third = frontend.rf3_preparation(p, source, 'private', self.config, 'r'*64)[-1]
        self.assertEqual(third['search']['database']['status'], 'verified')
        self.assertNotEqual(first['search'], third['search'])
        (a.parent / 'msa-search/msas/A.a3m').write_text('>query\nACDE\n')
        with self.assertRaisesRegex(Exception, 'SHA256 mismatch'):
            frontend.rf3_preparation(a, original, 'public', self.config, 'r'*64)

    def test_wrong_original_backend_or_search_binding_refused_before_cache(self):
        entry, original = self.bundle()
        with self.assertRaisesRegex(ValueError, 'backend'):
            frontend.rf3_preparation(entry, original, 'private', self.config, 'r'*64)
        with self.assertRaisesRegex(ValueError, 'original'):
            frontend.rf3_preparation(entry, entry, 'public', self.config, 'r'*64)
        manifest = read(entry.parent / 'msa-manifest.json'); manifest['search_sha256'] = '0'*64
        self.msa.seal(entry.parent, 'msa-manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'captured MSA'):
            frontend.rf3_preparation(entry, original, 'public', self.config, 'r'*64)

    def test_private_incomplete_database_provenance_refused(self):
        entry, original = self.bundle(backend='private')
        root = entry.parent / 'msa-search'
        provenance = read(root / 'database-provenance.json')
        del provenance['database']['components']['mmcif']
        atomic_json(root / 'database-provenance.json', provenance)
        self.msa.seal(root, 'search.json', read(root / 'search.json'))
        manifest = read(entry.parent / 'msa-manifest.json')
        manifest['search_sha256'] = read(root / 'search.json')['sha256']
        self.msa.seal(entry.parent, 'msa-manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'full database'):
            frontend.rf3_preparation(entry, original, 'private', self.config, 'r'*64)

    def expected_files(self, bundle):
        manifest = self.prepare.validate(bundle / 'input.json')
        document = self.prepare.document(self.prepare.read_json(bundle / 'input.json'))
        required = {'input.json'} | {c[k] for c in document['components'] for k in ('path', 'msa_path') if k in c}
        expected = {'source_commit': rf3.SOURCE_PIN, 'name': document['name'],
                    'prepared_files_sha256': {key: manifest['files'][key] for key in required}}
        target = bundle.parent / 'expected-chemistry.json'
        atomic_json(target, expected)
        receipt = {'schema': 1, 'kind': 'rf3-expected-chemistry', 'expected_sha256': sha256(target),
            'base_prepared_sha256': manifest['sha256'], 'runtime': {'native': 'verified'},
            'sources': {name: sha256(Path(frontend.__file__).parent.parent / name) for name in
                ('rf3/prepare.py', 'library/rf3_output.py', 'library/rf3_compat.py')}}
        receipt['sha256'] = digest(receipt)
        atomic_json(bundle.parent / 'expected-chemistry-receipt.json', receipt)
        return manifest, receipt

    def fake_expected(self, state, worker, bundle, timeout):
        manifest, receipt = self.expected_files(bundle)
        for source, target in (('expected-chemistry.json', rf3.EXPECTED_FILE),
                               ('expected-chemistry-receipt.json', rf3.EXPECTED_RECEIPT)):
            (bundle / target).write_bytes((bundle.parent / source).read_bytes())
        self.msa.seal(bundle, 'msa-manifest.json', manifest)
        return {'unit': 'cpu-only', 'expected_receipt_sha256': receipt['sha256']}

    def test_native_cpu_preparation_binds_toolchain_boot_and_hides_gpu(self):
        entry, original = self.bundle()
        tools = Path(frontend.__file__).parent.parent
        names = ('inference/adapters/rf3.py', 'inference/adapters/_common.py', 'rf3/prepare.py',
            'rf3/runtime.py', 'rf3/requirements.lock', 'library/rf3_output.py', 'library/rf3_compat.py')
        worker = {'worker_id': 'rf3-worker', 'tools_root': str(tools), 'python': '/native/python',
            'source_files': {str(tools / name): sha256(tools / name) for name in names},
            'environment': {'PATH': '/usr/bin', 'LD_LIBRARY_PATH': '/native/libs'},
            'deadline_epoch': frontend.now() + 600}
        state = self.root / 'state'
        atomic_json(state / 'launches/rf3-worker/intent.json', {'target': {'boot_id': 'boot'}})
        calls = []
        def remote(target, command, **kwargs):
            calls.append(command)
            if command[0] == '/usr/bin/cat': return b'boot\n'
            if command[0] == 'systemd-run': self.expected_files(entry.parent)
            return b'cpu completed'
        with patch.object(frontend, 'remote', side_effect=remote):
            result = frontend.prepare_rf3_expected(state, worker, entry.parent, 240)
        command = next(c for c in calls if c[0] == 'systemd-run')
        self.assertIn('PrivateNetwork=yes', command)
        self.assertIn('Environment=CUDA_VISIBLE_DEVICES=', command)
        self.assertIn('--expected-receipt', command)
        self.assertEqual(rf3.prepared_expected(entry, tools)['sha256'], result['expected_receipt_sha256'])
        with self.assertRaisesRegex(ValueError, 'head-owned'):
            frontend.rf3_preparation(entry, original, 'public', self.config, 'a'*64)

    def test_fresh_jobs_share_immutable_preparation_not_request_provenance(self):
        entry, original = self.bundle()
        stage = {'kind': 'pinned-command', 'argv': ['/pinned/python', '/pinned/rf3.py', '--prediction', '{prediction}', '--output', '{output}'],
                 'source_files': {'/pinned/python': 'a'*64}, 'output_dir': str(self.root / 'cpu')}
        policy = {'seeds': [101], 'postprocess': stage, 'config_id': configuration_id(self.config)}
        worker = {'input_root': str(self.root / 'inputs'), 'runtime_image': {'sha256': 'a'*64}}
        args = SimpleNamespace(bundle=entry, fasta=original, backend='public', chemistry_sha='b'*64,
            shared=self.root / 'shared', state=self.root / 'state', results=self.root / 'results', timeout=30,
            library_reference='assembly:mixed@3')
        queued = []
        fake_queue = SimpleNamespace(enqueue=lambda job: queued.append(deepcopy(job)))
        with patch.object(frontend, 'Queue', return_value=fake_queue), patch.object(frontend, 'wait', return_value={'state': 'complete'}), \
             patch.object(frontend, 'publish_rf3', side_effect=lambda result, job, dest: dest), \
             patch.object(frontend, 'prepare_rf3_expected', side_effect=self.fake_expected) as prepare_expected:
            frontend.submit_rf3(args, policy, self.config, worker)
            self.assertEqual(prepare_expected.call_count, 1)
            frontend.submit_rf3(args, policy, self.config, worker)
            self.assertEqual(prepare_expected.call_count, 1)
        first, second = queued
        self.assertNotEqual(first['id'], second['id'])
        self.assertNotEqual(first['native_input'], second['native_input'])
        self.assertEqual(first['provenance']['cache_receipt'], second['provenance']['cache_receipt'])
        self.assertFalse(first['provenance']['reused_preparation'])
        self.assertTrue(second['provenance']['reused_preparation'])
        self.assertEqual(first['postprocess'], stage)
        self.assertEqual(first['seeds'], [101])
        self.assertEqual(first['provenance']['library_reference'], 'assembly:mixed@3')
        self.assertEqual(self.config['native_config']['inputs'], None)
        self.assertNotEqual(first['provenance']['base_prepared_sha256'], first['provenance']['prepared_sha256'])
        self.assertEqual(first['provenance']['expected_chemistry_receipt_sha256'], second['provenance']['expected_chemistry_receipt_sha256'])
        self.assertIsNone(second['provenance']['cpu_preparation'])

    def complete(self):
        job = {'id': 'rf3-job', 'model': 'rf3', 'postprocess': {'kind': 'pinned-command'}}
        raw = self.root / 'raw'; raw.mkdir(); (raw / 'unchanged.cif').write_text('native raw')
        cpu = self.root / 'cpu'; output = cpu / 'validated-output'; output.mkdir(parents=True)
        selected = {}
        for name in ('model', 'summary', 'confidences'):
            path = output / (name + '.txt'); path.write_text('selected ' + name)
            selected[name] = {'path': path.name, 'sha256': sha256(path), 'raw_path': 'raw/' + path.name}
        (output / 'raw-alternative.cif').write_text('preserved other sample')
        audit = {'status': 'passed', 'selected': {'ranking_score': .8, 'files': selected}}
        atomic_json(output / 'rf3-output-validation.json', audit)
        checked = {'status': 'complete', 'job_id': job['id'], 'job_sha256': digest(job), 'output_dir': str(output),
            'output_validation': audit, 'output_validation_sha256': sha256(output / 'rf3-output-validation.json'),
            'settings': {}, 'rng_policy': 'cold-state', 'prepared_sha256': 'a'*64, 'load_receipt_sha256': 'b'*64,
            'files': inventory(output)}
        atomic_json(cpu / 'result.json', checked)
        result = {'state': 'complete', 'result': {'output_dir': str(raw), 'files': inventory(raw), 'postprocess': {
            'job_sha256': digest(job), 'stage_sha256': digest(job['postprocess']), 'output_dir': str(cpu),
            'files': inventory(cpu), 'result_file': str(cpu / 'result.json'), 'sha256': sha256(cpu / 'result.json'), 'result': checked}}}
        return job, result, raw

    def test_publishes_selected_qa_result_and_preserves_raw_candidates(self):
        job, result, raw = self.complete(); original = inventory(raw)
        out = frontend.publish_rf3(result, job, self.root / 'published')
        runtime = read(out / 'rf3-runtime.json')
        self.assertEqual(runtime['outputs']['model'], 'model.txt')
        self.assertEqual(runtime['output_validation']['status'], 'passed')
        self.assertTrue((out / 'raw-alternative.cif').is_file())
        self.assertEqual(inventory(raw), original)
        with self.assertRaises(FileExistsError):
            frontend.publish_rf3(result, job, out)

    def test_no_publication_for_pending_qa_or_changed_cpu_files(self):
        job, result, raw = self.complete()
        pending = deepcopy(result); del pending['result']['postprocess']
        with self.assertRaisesRegex(ValueError, 'CPU chemistry'):
            frontend.publish_rf3(pending, job, self.root / 'published')
        (self.root / 'cpu/validated-output/model.txt').write_text('tamper')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            frontend.publish_rf3(result, job, self.root / 'published')
        self.assertFalse((self.root / 'published').exists())


if __name__ == '__main__':
    unittest.main()
