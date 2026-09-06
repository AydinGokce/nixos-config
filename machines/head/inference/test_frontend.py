from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

from inference.common import atomic_json, configuration_id, inventory, now, read, sha256
from inference.frontend import Unavailable, materialize_job, preparation_identity, prepared_module, profile
from inference.pool import relocate_config


class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {'model': 'protenix', 'native_config': {'sample_diffusion': {'N_sample': 5}},
                       'work_dir': '/private/worker'}
        self.config_id = configuration_id(self.config)
        proof = self.root / 'proof'; proof.mkdir()
        atomic_json(proof / 'validation.json', {'status': 'passed'})
        atomic_json(self.root / 'profiles/protenix.json', {'model': 'protenix',
            'interface': 'bio-submit-native-defaults-v1', 'config_id': self.config_id,
            'validation_root': str(proof), 'validation_files': inventory(proof), 'seeds': [101]})
        atomic_json(self.root / 'configs' / (self.config_id + '.json'), self.config)
        self.worker = {'worker_id': 'one', 'config_id': self.config_id,
                       'spool_root': str(self.root / 'spool'), 'deadline_epoch': now()+3600,
                       'tools_root': '/frozen/tools', 'source_files': {
                           '/frozen/tools/' + relative: sha256(Path(__file__).parent.parent / relative)
                           for relative in ('inference/adapters/protenix.py', 'msa/prepared.py')}}
        atomic_json(self.root / 'workers/one.json', self.worker)
        self.status = {'state': 'ready', 'config_id': self.config_id, 'heartbeat_epoch': now(), 'deadline_epoch': now()+3600}
        self.status_path = self.root / 'spool/one/status.json'
        atomic_json(self.status_path, self.status)

    def test_only_fresh_matching_audited_profile_is_selected(self):
        self.assertEqual(profile(self.root, 'protenix')[2], self.worker)
        atomic_json(self.status_path, dict(self.status, heartbeat_epoch=now()-100))
        with self.assertRaises(Unavailable):
            profile(self.root, 'protenix')
        atomic_json(self.status_path, self.status)
        (self.root / 'proof/validation.json').write_text('{}')
        with self.assertRaises(ValueError):
            profile(self.root, 'protenix')

    def test_native_materialization_retains_pairing_and_all_sample_settings(self):
        prepared = prepared_module()
        source = self.root / 'source'; source.mkdir()
        msa = '>query\nACDE\n>hit species=42\nAcC-E\n'
        (source / 'paired.a3m').write_text(msa)
        (source / 'unpaired.a3m').write_text(msa)
        atomic_json(source / 'input-update-msa.json', [{'name': 'query', 'sequences': [{'proteinChain': {
            'sequence': 'ACDE', 'count': 1, 'pairedMsaPath': str(source / 'paired.a3m'),
            'unpairedMsaPath': str(source / 'unpaired.a3m')}}]}])
        bundle = self.root / 'bundle'
        prepared.capture('protenix', source, bundle, endpoint='https://api.colabfold.com')
        original = deepcopy(self.config)
        job = materialize_job('protenix', bundle, self.root / 'native', self.config, 'job1', [101])
        chain = read(job['native_input'])[0]['sequences'][0]['proteinChain']
        self.assertEqual(Path(chain['pairedMsaPath']).read_text(), msa)
        self.assertEqual(Path(chain['unpairedMsaPath']).read_text(), msa)
        self.assertEqual(job['native_config']['sample_diffusion']['N_sample'], 5)
        self.assertEqual(job['seeds'], [101])
        self.assertEqual(self.config, original)

    def test_old_worker_parser_cannot_be_labeled_as_current_head_parser(self):
        changed = deepcopy(self.worker)
        changed['source_files']['/frozen/tools/msa/prepared.py'] = 'a'*64
        atomic_json(self.root / 'workers/one.json', changed)
        with self.assertRaisesRegex(Unavailable, 'another adapter/preparation generation'):
            profile(self.root, 'protenix')

    def test_cache_identity_changes_for_database_and_molecular_changes(self):
        fasta = self.root / 'input.fasta'; fasta.write_text('>query\nACDE\n')
        parser = Path(__file__).resolve().parent.parent / 'msa/prepared.py'
        source = {'database': {'status': 'verified', 'sha256': 'a'*64}}
        first = preparation_identity('protenix', fasta, parser, 'private', source)
        second = preparation_identity('protenix', fasta, parser, 'private',
            {'database': {'status': 'verified', 'sha256': 'b'*64}})
        self.assertNotEqual(first, second)
        third = preparation_identity('protenix', fasta, parser, 'private', source, chemistry_sha='c'*64)
        self.assertNotEqual(first, third)

    def test_runtime_relocation_only_rewrites_declared_path_prefixes(self):
        value = {'path': '/old/env/site/file', 'similar': '/old/env-other/file', 'nested': ['/old/env', 'C=C']}
        self.assertEqual(relocate_config(value, {'/old/env': '/new/image'}),
            {'path': '/new/image/site/file', 'similar': '/old/env-other/file', 'nested': ['/new/image', 'C=C']})

    def test_head_nix_directory_symlinks_preserve_package_and_sibling_imports(self):
        toolkit = self.root / 'etc/bio-tools'; toolkit.mkdir(parents=True)
        source = Path(__file__).resolve().parent.parent
        for folder in ('inference', 'msa', 'rf3', 'library'):
            (toolkit / folder).symlink_to(source / folder, target_is_directory=True)
        output = subprocess.check_output([sys.executable, str(toolkit / 'inference/cli.py'),
            '--state', str(self.root / 'new-state'), 'list'], text=True)
        self.assertEqual(json.loads(output), [])
        probe = 'from inference.frontend import prepared_module,rf3_modules; assert prepared_module().VERSIONS; assert rf3_modules()[0].AA'
        subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0,sys.argv[1]); ' + probe,
                        str(toolkit)], check=True)


if __name__ == '__main__':
    unittest.main()
