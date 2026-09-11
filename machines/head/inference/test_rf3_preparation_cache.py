"""Exercise RF3 pre-search replay with real parsers/cache and an offline API fixture."""
from copy import deepcopy
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import importlib.util
import json
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from inference import frontend
from inference.cache import CacheError
from inference.common import atomic_json, read, sha256


class RF3PreparationCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.root / 'original.json'
        self.document = [{'name': 'mixed', 'components': [
            {'chain_id': 'Z', 'seq': 'ACDE', 'chain_type': 'polypeptide(L)', 'is_polymer': True},
            {'chain_id': 'A', 'seq': 'GHIK', 'chain_type': 'polypeptide(L)', 'is_polymer': True},
            {'chain_id': 'L', 'smiles': 'C[C@H](O)F', 'res_name': 'ligand'}]}]
        atomic_json(self.original, self.document)
        self.prepare, self.msa = frontend.rf3_modules()
        self.modules = patch.object(frontend, 'rf3_modules', return_value=(self.prepare, self.msa))
        self.modules.start(); self.addCleanup(self.modules.stop)
        self.calls = []
        self.search_calls = []
        self.private_wrong_generation = False
        self.client = patch.object(self.msa.Client, 'search', self.api_search)
        self.client.start(); self.addCleanup(self.client.stop)
        self.runner = patch.object(frontend.subprocess, 'run', side_effect=self.run_command)
        self.mock_run = self.runner.start(); self.addCleanup(self.runner.stop)
        self.args = SimpleNamespace(model='rf3', fasta=None, native_json=self.original,
            rf3_name='unused-for-native', rf3_out=self.root / 'out-1', bundle=None, probe=False,
            shared=self.root / 'shared', backend='public', endpoint='https://api.example/', timeout=60,
            chemistry_sha='a'*64, library_reference='assembly:mixed@3', refresh_preparation=False,
            rf3_database_root=self.root / 'databases')

    def api_search(self, sequences, kind, mode, out):
        self.search_calls.append((list(sequences), kind, mode))
        out.mkdir(parents=True)
        (out / 'out.tar.gz').write_bytes(b'retained-api-response')
        atomic_json(out / 'request.json', {'query': sequences, 'kind': kind, 'mode': mode,
                                          'ticket': 'fixture-' + str(len(self.search_calls))})
        result = {101+i: f'>{101+i}\n{seq}\n>hit-{i}\n{seq}\n' for i, seq in enumerate(sequences)}
        return [result] if kind == 'pair' else [result, result]

    def run_command(self, argv, **kwargs):
        self.calls.append(list(argv))
        def value(flag, default=None):
            return argv[argv.index(flag)+1] if flag in argv else default
        if argv[0] == 'bio-msa':
            queries = self.prepare.read_json(value('--json'))
            self.assertEqual(list(queries), ['Z', 'A'])
            root = Path(value('--bundle-result')).parent
            provenance = frontend.rf3_search_database(self.args.rf3_database_root)
            if self.private_wrong_generation:
                provenance['database']['components']['mmcif'] = '0'*64
            atomic_json(root / 'provenance.json', dict(provenance, runtime={'mmseqs_threads': 16},
                search_settings='unmodified pipelines', namespace='retained-origin'))
            search = self.msa.search(queries, root / 'private-search', 'http://localhost:8082',
                'private', time.time()+60, root / 'provenance.json')
            atomic_json(value('--bundle-result'), {'bundle': str(search)})
        else:
            self.msa.prepare_input(SimpleNamespace(fasta=value('--fasta'), native_json=value('--native-json'),
                search_bundle=value('--search-bundle'), server_url=value('--server-url'), source=value('--source'),
                deadline=float(value('--deadline', time.time()+60)), database_provenance=None,
                out=value('--out'), name=value('--name')))
        return subprocess.CompletedProcess(argv, 0)

    def next(self):
        self.args.rf3_out = self.root / ('out-' + str(len(list(self.root.glob('out-*.preparation-cache.json')))+1))
        return frontend.rf3_prepare_cached(self.args)

    def database(self):
        source = Path(frontend.__file__).parent.parent / 'msa/databases.py'
        spec = importlib.util.spec_from_file_location('test_rf3_db_pins', source)
        pins = importlib.util.module_from_spec(spec); spec.loader.exec_module(pins)
        atomic_json(self.args.rf3_database_root / 'manifest.json', pins.MANIFEST)
        receipt = dict(version=pins.VERSION, manifest_sha256=pins.MANIFEST_SHA256,
            components={name: str(index+1)*64 for index, name in enumerate(pins.COMPONENTS)},
            tools={'mmseqs_sha256': pins.MMSEQS_SHA256, 'server_sha256': pins.BACKEND_SHA256}, installed_utc=1)
        atomic_json(self.args.rf3_database_root / '.msa-databases.json', receipt)
        return receipt

    def test_replay_skips_search_and_preserves_original_raw_receipt_and_separate_request(self):
        first = self.next(); original_calls = len(self.calls)
        second = self.next()
        self.assertFalse(first['reused_preparation']); self.assertTrue(second['reused_preparation'])
        self.assertEqual(len(self.calls), original_calls)
        self.assertEqual(first['cache_receipt'], second['cache_receipt'])
        self.assertEqual(first['original_search_completed_at_epoch'], second['original_search_completed_at_epoch'])
        self.assertNotEqual(first['request_directory'], second['request_directory'])
        self.assertEqual(second['evidence_semantics'], 'replayed original captured search')
        self.assertEqual(first['cache_receipt']['identity']['ordered_queries'][0]['chain_id'], 'Z')
        self.assertEqual(first['cache_receipt']['identity']['search']['database']['status'], 'provider-unreported')
        a, b = Path(first['bundle']), Path(second['bundle'])
        self.assertEqual((a/'msa-search/search.json').read_bytes(), (b/'msa-search/search.json').read_bytes())
        (b/'msas/Z.a3m').write_text('changed fresh writable job copy')
        self.assertNotEqual((a/'msas/Z.a3m').read_text(), (b/'msas/Z.a3m').read_text())
        self.assertTrue(self.next()['reused_preparation'])

    def test_refresh_has_distinct_entry_and_preserves_prior_default(self):
        first = self.next(); self.args.refresh_preparation = True; second = self.next()
        self.assertFalse(second['reused_preparation']); self.assertTrue(second['refreshed'])
        self.assertNotEqual(first['cache_receipt']['key'], second['cache_receipt']['key'])
        self.args.refresh_preparation = False
        third = self.next(); self.assertEqual(third['cache_receipt'], first['cache_receipt'])

    def test_chemistry_query_order_assets_endpoint_and_name_are_cache_identity(self):
        baseline = frontend.rf3_search_identity(self.args)[-1]
        altered = deepcopy(self.document); altered[0]['components'][2]['smiles'] = 'C[C@@H](O)F'
        atomic_json(self.original, altered)
        self.assertNotEqual(baseline['input']['chemistry_sha256'], frontend.rf3_search_identity(self.args)[-1]['input']['chemistry_sha256'])
        altered[0]['components'][0:2] = list(reversed(altered[0]['components'][0:2])); atomic_json(self.original, altered)
        self.assertNotEqual(baseline['input']['ordered_queries_sha256'], frontend.rf3_search_identity(self.args)[-1]['input']['ordered_queries_sha256'])
        self.original.write_text('>one\nACDE\n'); self.args.fasta=self.original; self.args.native_json=None
        one=frontend.rf3_search_identity(self.args)[-1]; self.args.rf3_name='new-name'
        self.assertNotEqual(one, frontend.rf3_search_identity(self.args)[-1])
        self.args.endpoint='https://another.example'
        self.assertNotEqual(one['search'], frontend.rf3_search_identity(self.args)[-1]['search'])
        self.args.fasta=None; self.args.native_json=self.original
        asset=self.root/'compound.sdf'; asset.write_text('exact-SDF-1')
        atomic_json(self.original, [{'name':'sdf','components':[{'chain_id':'L','path':asset.name,'res_name':'LIG'}]}])
        one=frontend.rf3_search_identity(self.args)[-1]; asset.write_text('exact-SDF-2')
        self.assertNotEqual(one['input']['chemistry_sha256'],frontend.rf3_search_identity(self.args)[-1]['input']['chemistry_sha256'])
        asset.unlink(); asset.symlink_to(self.original)
        with self.assertRaises(CacheError): frontend.rf3_search_identity(self.args)

    def test_private_full_generation_changes_miss_but_installation_timestamp_does_not(self):
        receipt=self.database(); self.args.backend='private'
        first=self.next(); self.assertEqual(first['cache_receipt']['identity']['search']['database']['status'],'verified')
        receipt['installed_utc']=999999
        atomic_json(self.args.rf3_database_root/'.msa-databases.json',receipt)
        self.assertTrue(self.next()['reused_preparation'])
        receipt['components']['mmcif']='9'*64
        atomic_json(self.args.rf3_database_root/'.msa-databases.json',receipt)
        third=self.next(); self.assertFalse(third['reused_preparation'])
        self.assertNotEqual(first['cache_receipt']['key'],third['cache_receipt']['key'])

    def test_incomplete_private_receipt_or_actual_wrong_generation_fail_without_public_fallback(self):
        receipt=self.database(); self.args.backend='private'
        del receipt['components']['mmcif']; atomic_json(self.args.rf3_database_root/'.msa-databases.json',receipt)
        with self.assertRaisesRegex(ValueError,'complete database'): self.next()
        self.assertEqual(self.calls,[])
        self.database(); self.private_wrong_generation=True
        with self.assertRaisesRegex(ValueError,'different database'): self.next()
        self.assertEqual(self.calls[0][0],'bio-msa')
        self.assertFalse(list((self.args.shared/'inference/rf3-preparation-cache/entries').glob('*.json')))
        self.assertFalse(self.args.rf3_out.exists())

    def test_private_capacity_allowance_extends_supervision_without_extending_search_timeout(self):
        self.database(); self.args.backend = 'private'
        clock = [1000.]
        original = self.run_command
        observations = []
        def delayed_capacity(argv, **kwargs):
            observations.append((list(argv), kwargs['timeout']))
            result = original(argv, **kwargs)
            if argv[0] == 'bio-msa':
                clock[0] += 7200
            return result
        self.mock_run.side_effect = delayed_capacity
        with patch.object(frontend, 'now', side_effect=lambda: clock[0]), \
             patch.dict('os.environ', {'BIO_MSA_CAPACITY_WAIT_SECONDS': '7200'}):
            first = self.next()
        self.assertFalse(first['reused_preparation'])
        command, outer_timeout = observations[0]
        self.assertEqual(outer_timeout, 7260)
        self.assertEqual(command[command.index('--timeout') + 1], '60')
        self.assertEqual(command[command.index('--capacity-wait-seconds') + 1], '7200')
        self.assertEqual(observations[1][1], 60)
        self.mock_run.side_effect = AssertionError('A cached search must not wait for or start a session')
        self.assertTrue(self.next()['reused_preparation'])

    def test_private_cache_lock_wait_uses_capacity_allowance_before_native_work_budget(self):
        self.database(); self.args.backend = 'private'
        clock = [1000.]
        @contextmanager
        def busy_cache(*args):
            clock[0] += 120
            yield
        with patch.object(frontend, 'rf3_search_lock', busy_cache), \
             patch.object(frontend, 'now', side_effect=lambda: clock[0]), \
             patch.dict('os.environ', {'BIO_MSA_CAPACITY_WAIT_SECONDS': '7200'}):
            self.assertFalse(self.next()['reused_preparation'])
        command = self.calls[0]
        self.assertEqual(command[command.index('--timeout') + 1], '60')
        self.assertEqual(command[command.index('--capacity-wait-seconds') + 1], '7080')

    def test_public_and_nonprotein_preparation_keep_original_supervision_bound(self):
        with patch.object(frontend, 'now', return_value=1000):
            first = self.next()
        request = read(Path(first['request_directory']) / 'request.json')
        self.assertEqual(request['deadline_epoch'], 1060)
        atomic_json(self.original, [{'name': 'ligand', 'components': [{'chain_id': 'L', 'smiles': 'CCO'}]}])
        self.args.backend = 'private'
        with patch.object(frontend, 'now', return_value=2000):
            second = self.next()
        request = read(Path(second['request_directory']) / 'request.json')
        self.assertEqual(request['deadline_epoch'], 2060)

    def test_original_mutation_and_failed_search_never_publish(self):
        original=self.run_command
        def mutate(argv,**kwargs):
            result=original(argv,**kwargs); self.original.write_text('changed'); return result
        self.mock_run.side_effect=mutate
        with self.assertRaises(ValueError): self.next()
        self.assertFalse(self.args.rf3_out.exists())
        self.assertFalse(list((self.args.shared/'inference/rf3-preparation-cache/entries').glob('*.json')))
        atomic_json(self.original,self.document)
        self.mock_run.side_effect=subprocess.CalledProcessError(7,['offline-fixture'])
        with self.assertRaises(subprocess.CalledProcessError): self.next()
        self.assertFalse(self.args.rf3_out.exists())
        self.assertEqual(len(list(self.root.glob('.rf3-search-request-*/failure.json'))),2)

    def test_no_protein_query_needs_no_database_or_network_and_existing_output_refused(self):
        atomic_json(self.original,[{'name':'ligand','components':[{'chain_id':'L','smiles':'C[C@H](O)F'}]}])
        self.args.backend='private'; first=self.next()
        self.assertEqual(self.search_calls,[])
        self.assertEqual(first['cache_receipt']['identity']['search']['backend'],'none')
        with self.assertRaises(FileExistsError): frontend.rf3_prepare_cached(self.args)
        doc=deepcopy(self.document);doc[0]['components'][0]['msa_path']='do-not-replace.a3m';atomic_json(self.original,doc)
        with self.assertRaisesRegex(ValueError,'will not replace'): frontend.rf3_search_identity(self.args)

    def test_validly_resealed_wrong_pair_order_rejected(self):
        first=self.next(); bundle=Path(first['bundle']); search=read(bundle/'msa-search/search.json')
        search['queries']=dict(reversed(list(search['queries'].items())))
        self.msa.seal(bundle/'msa-search','search.json',search)
        manifest=read(bundle/'msa-manifest.json');manifest['search_sha256']=search['sha256']
        self.msa.seal(bundle,'msa-manifest.json',manifest)
        original,_,queries,identity=frontend.rf3_search_identity(self.args)
        with self.assertRaisesRegex(ValueError,'ordered query'):
            frontend.verify_rf3_search_preparation(bundle,original,queries,identity)

    def test_concurrent_identical_requests_capture_search_once(self):
        first, second = deepcopy(self.args), deepcopy(self.args)
        second.rf3_out = self.root / 'concurrent-2'
        original = self.run_command
        def slow(argv, **kwargs):
            time.sleep(.1)
            return original(argv, **kwargs)
        self.mock_run.side_effect = slow
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(frontend.rf3_prepare_cached, args) for args in (first, second)]
            values = [future.result(timeout=15) for future in futures]
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sorted(value['reused_preparation'] for value in values), [False, True])
        self.assertEqual(values[0]['cache_receipt'], values[1]['cache_receipt'])

    def test_corrupted_cache_fails_without_search_or_output(self):
        first = self.next(); calls = len(self.calls)
        cached = first['cache_receipt']
        path = self.args.shared/'inference/rf3-preparation-cache/objects'/cached['content_sha256']/'data/msas/Z.a3m'
        path.chmod(0o600); path.write_text('tampered')
        with self.assertRaises(CacheError): self.next()
        self.assertEqual(len(self.calls), calls)
        self.assertFalse(self.args.rf3_out.exists())


if __name__=='__main__': unittest.main()
