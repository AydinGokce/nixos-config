"""Installer archive and immutable dependency checks; no network or GPU required."""
import hashlib
import io
import json
from pathlib import Path
import re
import runpy
import tarfile
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
MODULE = runpy.run_path(str(HERE / 'install.py'))


class InstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def archive(self, names, *, symlink=False):
        path = self.root / 'input.tar'
        with tarfile.open(path, 'w') as output:
            for name in names:
                member = tarfile.TarInfo(name)
                if symlink:
                    member.type = tarfile.SYMTYPE; member.linkname = '/tmp/other'
                    output.addfile(member)
                else:
                    member.size = 4; output.addfile(member, io.BytesIO(b'data'))
        return path

    def test_lock_has_complete_hashes_unique_packages_and_cuda_python_pins(self):
        pins = json.loads((HERE / 'pins.json').read_text())
        lock = json.loads((HERE / 'linux-64-cuda.lock.json').read_text())
        packages = {row['name']: row for row in lock['packages']}
        self.assertEqual(len(packages), len(lock['packages']))
        self.assertTrue(packages['python']['version'].startswith('3.10.'))
        self.assertEqual(packages['jax']['version'], '0.6.0')
        self.assertEqual(packages['jaxlib']['version'], '0.6.0')
        self.assertIn('cuda126', packages['jaxlib']['build'])
        self.assertEqual(packages['numpy']['version'], '1.26.4')
        self.assertNotIn('pyrosetta', packages)
        for item in [pins['micromamba'], pins['af2'], pins['pyrosetta'], *pins['sources'].values(), *lock['packages']]:
            with self.subTest(item=item['filename']):
                self.assertRegex(item['sha256'], '^[a-f0-9]{64}$')
                self.assertTrue(item['url'].startswith('https://'))
                self.assertEqual(Path(item['filename']).name, item['filename'])
                self.assertGreater(item['size'], 0)
                # Conda metadata sometimes leaves this blank. Preserve that
                # uncertainty instead of inventing a license for the package.
                self.assertIsInstance(item['license'], str)
        self.assertTrue(pins['pyrosetta']['requires_explicit_evaluation'])

    def test_fetch_reuses_only_matching_cached_bytes_and_checks_download(self):
        sha = hashlib.sha256(b'data').hexdigest()
        pin = {'sha256': sha, 'filename': 'asset', 'url': 'https://example.test/asset', 'size': 4}
        target = self.root / sha / 'asset'; target.parent.mkdir(); target.write_bytes(b'data')
        with patch('urllib.request.urlopen', side_effect=AssertionError('Must not redownload verified bytes')):
            self.assertEqual(MODULE['fetch'](pin, self.root), target)
        target.write_bytes(b'evil')
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'wrong')):
            with self.assertRaisesRegex(ValueError, 'pin verification'):
                MODULE['fetch'](pin, self.root)
        self.assertEqual(target.read_bytes(), b'evil')
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'data')):
            MODULE['fetch'](pin, self.root)
        self.assertEqual(target.read_bytes(), b'data')

    def test_source_archive_cannot_escape_or_replace_destination_on_validation_error(self):
        destination = self.root / 'source'; destination.mkdir(); (destination / 'retained').write_text('old')
        for names, symlink in [(['../outside'], False), (['root/link'], True)]:
            with self.subTest(names=names), self.assertRaises(ValueError):
                MODULE['extract_source'](self.archive(names, symlink=symlink), destination)
            self.assertEqual((destination / 'retained').read_text(), 'old')

    def test_params_inventory_rejects_incomplete_unexpected_and_duplicate_files(self):
        valid = ['params_model_' + str(i) + suffix + '.npz' for i in range(1, 6)
                 for suffix in ('', '_ptm', '_multimer_v3')] + ['LICENSE']
        for names in [valid[:-1], [*valid, '../escape'], [*valid, valid[0]]]:
            with self.subTest(names=names), self.assertRaises(ValueError):
                MODULE['extract_params'](self.archive(names), self.root / 'params')
        records = MODULE['extract_params'](self.archive(valid), self.root / 'params')
        self.assertEqual(len(records), 16)
        self.assertTrue(all(row['sha256'] == hashlib.sha256(b'data').hexdigest() and row['size'] == 4 for row in records))


if __name__ == '__main__':
    unittest.main()
