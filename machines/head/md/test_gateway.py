import base64
import copy
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from workbench.api import API
from workbench.common import Error
from workbench.store import Store
from workbench.runner import validation, verify_prepared
from md.bundle import pack, unpack
from md.gateway import compare_results


class BundleTests(unittest.TestCase):
    def test_original_asset_bytes_and_manifest_survive_transport(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'modified.itp').write_bytes(b'; stereochemistry retained\n[ atoms ]\n')
            request = {'schema': 'bio-md-request.v1', 'protocol': 'pmx_binding_ddg'}
            expected = pack(request, {'ff/modified.itp': root / 'modified.itp'}, root / 'bundle.tar.gz')
            observed, manifest = unpack(root / 'bundle.tar.gz', root / 'unpacked')
            self.assertEqual((observed, manifest), (request, expected))
            self.assertEqual((root / 'unpacked/assets/ff/modified.itp').read_bytes(), (root / 'modified.itp').read_bytes())

    def test_archive_traversal_links_and_duplicate_members_reject_before_writing(self):
        for bad in ('../outside', '/tmp/outside', 'assets/a/../../outside', 'assets/link', 'request.json'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                with tarfile.open(root / 'bad.tar.gz', 'w:gz') as archive:
                    info = tarfile.TarInfo(bad)
                    if bad == 'assets/link':
                        info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
                    else:
                        info.size = 2
                    archive.addfile(info, io.BytesIO(b'{}'))
                    if bad == 'request.json':
                        archive.addfile(info, io.BytesIO(b'{}'))
                with self.assertRaises(ValueError):
                    unpack(root / 'bad.tar.gz', root / 'result')
                self.assertFalse((root / 'result').exists())


class MDGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state')
        self.api = API(self.store, 'aydin')
        data = b'original chemical topology\n'
        import hashlib
        sha = hashlib.sha256(data).hexdigest()
        upload = self.api.call('upload.begin', {'name': 'system.top', 'size': len(data), 'sha256': sha})
        self.api.call('upload.chunk', {'upload_id': upload['upload_id'], 'offset': 0, 'data_base64': base64.b64encode(data).decode()})
        self.api.call('upload.finish', {'upload_id': upload['upload_id'], 'sha256': sha})
        self.document = {'request_key': 'preview', 'name': 'Affinity', 'request': {
            'schema': 'bio-md-request.v1', 'protocol': 'pmx_binding_ddg', 'conditions': {}},
            'assets': {'system.top': upload['upload_id']}, 'timeout': 600}

    def test_cross_actor_upload_and_path_injection_are_rejected(self):
        with self.assertRaises(Error):
            API(self.store, 'someone-else').call('md.validate', self.document)
        for name in ('../escape', '/absolute', 'sub//file'):
            bad = copy.deepcopy(self.document)
            bad['assets'] = {name: next(iter(bad['assets'].values()))}
            with self.assertRaises(ValueError):
                self.api.call('md.validate', bad)
        self.assertEqual(self.store.listing('batch'), [])

    def test_preview_retry_and_submit_retry_create_one_owned_job(self):
        first = self.api.call('md.validate', self.document)
        self.assertEqual(first, self.api.call('md.validate', self.document))
        self.assertEqual(self.store.listing('job'), [])
        changed = {**self.document, 'timeout': 700}
        with self.assertRaises(Error):
            self.api.call('md.validate', changed)
        prepared = {'argv': ['bio-submit', 'md'], 'tools_dir': '/trusted', 'settings': {}, 'timeout': 600}
        with patch('md.gateway.validate_batch', return_value=prepared):
            validation(self.store, first['batch_id'], {})
        batch = self.api.call('batch.get', {'batch_id': first['batch_id']})
        commit = {'request_key': 'commit', 'batch_id': batch['batch_id'], 'pair_ids': [batch['pairs'][0]['pair_id']]}
        result = self.api.call('batch.create', commit)
        self.assertEqual(result, self.api.call('batch.create', commit))
        self.assertEqual(len(self.store.listing('job')), 1)
        with self.assertRaises(Error):
            API(self.store, 'someone-else').call('batch.get', {'batch_id': batch['batch_id']})
        cancelled = self.api.call('batch.cancel', {'batch_id': batch['batch_id']})
        self.assertEqual(cancelled['state'], 'cancelled')

    def test_invalid_science_stays_rejected_and_cannot_be_submitted(self):
        batch = self.api.call('md.validate', self.document)
        with patch('md.gateway.validate_batch', side_effect=ValueError('Unsupported synthetic residue parameters')):
            validation(self.store, batch['batch_id'], {})
        current = self.api.call('batch.get', {'batch_id': batch['batch_id']})
        self.assertEqual(current['pairs'][0]['state'], 'rejected')
        with self.assertRaises(Error):
            self.api.call('batch.create', {'batch_id': batch['batch_id'], 'request_key': 'bad', 'pair_ids': [batch['pairs'][0]['pair_id']]})
        self.assertEqual(self.store.listing('job'), [])

    def test_unsampled_cycles_do_not_become_zero_disagreement(self):
        from workbench.common import file_sha
        ident = 'a' * 32
        content = self.store.directory('artifacts', ident) / 'content'
        content.parent.mkdir(parents=True)
        content.write_text(json.dumps({'schema': 'bio-md-cycle-report.v1', 'binding_ddg': [],
            'unavailable_binding_ddg': [{'reason': 'insufficient_overlap_or_numerical_support'}]}))
        with self.store.transaction() as db:
            self.store.put(db, 'artifact', {'artifact_id': ident, 'model': 'md',
                'format': 'json', 'size': content.stat().st_size, 'sha256': file_sha(content)}, self.api.actor)
        result = compare_results(self.api, {'artifact_ids': [ident]})
        self.assertEqual(result['comparison']['status'], 'insufficient_sampling')
        self.assertIsNone(result['comparison']['between_model_disagreement'])
        self.assertEqual(len(result['unavailable_binding_ddg']), 1)
        with self.assertRaises(Error):
            compare_results(API(self.store, 'someone-else'), {'artifact_ids': [ident]})


if __name__ == '__main__':
    unittest.main()
