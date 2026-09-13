"""Portable BindCraft target/settings bundles; no native software or cloud calls."""
import builtins
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('bindcraft_bundle_tested', Path(__file__).with_name('bundle.py'))
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


def pdb_fixture():
    records, serial = [], 1
    for chain in ('A', 'B'):
        for residue in (5, 6, 7):
            for atom in ('N', 'CA', 'C', 'O'):
                records.append(f'ATOM  {serial:5d} {atom:^4s} ALA {chain}{residue:4d}    '
                               f'{float(serial):8.3f}{1.:8.3f}{2.:8.3f}{1.:6.2f}{20.:6.2f}          {atom[0]:>2s}  ')
                serial += 1
    return ('\n'.join(records) + '\nEND\n').encode('ascii')


class BundleTests(unittest.TestCase):
    def test_optional_campaign_seed_is_hashed_validated_and_materialized(self):
        bundle.create(self.pdb, self.settings, self.advanced, self.filters, self.path, execution={'seed': 42})
        manifest, assets = bundle.inspect(self.path)
        self.assertEqual(json.loads(assets['execution.json']), {'seed': 42})
        self.assertEqual(manifest['files']['execution.json'], bundle.digest(assets['execution.json']))
        bundle.materialize(self.path, self.root / 'with-seed')
        self.assertEqual(json.loads((self.root / 'with-seed/execution.json').read_text()), {'seed': 42})
        for execution in ({'seed': True}, {'seed': -1}, {'seed': 2**31}, {'seed': 3, 'command': 'other'}):
            with self.subTest(execution=execution), self.assertRaisesRegex(ValueError, 'campaign seed'):
                bundle.create(self.pdb, self.settings, self.advanced, self.filters,
                              self.root / 'bad-seed.tar.gz', execution=execution)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pdb = self.root / "target with 'quote.pdb"
        self.pdb.write_bytes(pdb_fixture())
        self.settings = {'binder_name': 'fixture-binder', 'chains': 'A',
                         'target_hotspot_residues': 'A5-7', 'lengths': [20, 25],
                         'number_of_final_designs': 1}
        self.advanced = bundle.defaults('advanced')
        self.filters = bundle.defaults('filters')
        self.path = self.root / 'input.tar.gz'

    def create(self, *, settings=None, advanced=None, filters=None):
        return bundle.create(self.pdb, self.settings if settings is None else settings,
                             self.advanced if advanced is None else advanced,
                             self.filters if filters is None else filters, self.path)

    def entries(self):
        with tarfile.open(self.path, 'r:gz') as archive:
            return {member.name: archive.extractfile(member).read() for member in archive}

    def rewrite(self, entries, extra=()):
        with tarfile.open(self.path, 'w:gz') as archive:
            for name, data in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            for info, data in extra:
                archive.addfile(info, io.BytesIO(data) if data is not None else None)

    def rehash(self, entries):
        manifest = json.loads(entries['manifest.json'])
        manifest['files'] = {name: bundle.digest(data) for name, data in entries.items() if name != 'manifest.json'}
        entries['manifest.json'] = bundle.encoded(manifest)
        return entries

    def test_round_trip_retains_exact_target_and_independent_settings_and_is_native_free(self):
        original_import = builtins.__import__
        def no_native(name, *args, **kwargs):
            if name.split('.')[0] in {'jax', 'jaxlib', 'pyrosetta', 'colabdesign'}:
                raise AssertionError('Bundle validation must not import native software: ' + name)
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=no_native):
            manifest = self.create()
            observed, assets = bundle.inspect(self.path)
            materialized = bundle.materialize(self.path, self.root / 'restored')
        self.assertEqual(observed, manifest)
        self.assertEqual(materialized, manifest)
        self.assertEqual((self.root / 'restored/target.pdb').read_bytes(), self.pdb.read_bytes())
        self.assertEqual(json.loads((self.root / 'restored/settings.json').read_bytes()), self.settings)
        self.assertEqual(json.loads((self.root / 'restored/advanced.json').read_bytes()), self.advanced)
        self.assertEqual(json.loads((self.root / 'restored/filters.json').read_bytes()), self.filters)
        self.assertEqual(set(assets), {'target.pdb', 'settings.json', 'advanced.json', 'filters.json'})

    def test_tampered_target_or_settings_fail_before_materialization(self):
        self.create()
        original = self.entries()
        for name in ('target.pdb', 'settings.json', 'advanced.json', 'filters.json'):
            with self.subTest(asset=name):
                changed = dict(original)
                changed[name] += b' '
                self.rewrite(changed)
                with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                    bundle.materialize(self.path, self.root / 'restored')
                self.assertFalse((self.root / 'restored').exists())

    def test_archive_traversal_links_duplicates_and_extra_files_are_rejected(self):
        self.create()
        original = self.entries()
        cases = [('../outside', tarfile.REGTYPE, ''), ('/tmp/outside', tarfile.REGTYPE, ''),
                 ('settings.json', tarfile.REGTYPE, ''), ('unexpected.json', tarfile.REGTYPE, ''),
                 ('target.pdb', tarfile.SYMTYPE, '../outside'),
                 ('target.pdb', tarfile.LNKTYPE, 'settings.json')]
        for name, kind, link in cases:
            with self.subTest(name=name, kind=kind):
                info = tarfile.TarInfo(name)
                info.type, info.linkname = kind, link
                info.size = 1 if kind == tarfile.REGTYPE else 0
                self.rewrite(original, [(info, b'x' if info.size else None)])
                with self.assertRaisesRegex(ValueError, 'unsafe bundle member'):
                    bundle.materialize(self.path, self.root / 'restored')
                self.assertFalse((self.root / 'restored').exists())
                self.assertFalse((self.root.parent / 'outside').exists())

    def test_missing_asset_and_wrong_upstream_revision_are_rejected(self):
        self.create()
        original = self.entries()
        missing = dict(original)
        missing.pop('target.pdb')
        self.rewrite(self.rehash(missing))
        with self.assertRaisesRegex(ValueError, 'Unexpected input bundle assets'):
            bundle.inspect(self.path)
        changed = dict(original)
        manifest = json.loads(changed['manifest.json'])
        manifest['bindcraft_commit'] = '0' * 40
        changed['manifest.json'] = bundle.encoded(manifest)
        self.rewrite(changed)
        with self.assertRaisesRegex(ValueError, 'unsupported BindCraft bundle manifest'):
            bundle.inspect(self.path)

    def test_duplicate_json_keys_and_nonfinite_settings_are_rejected_even_with_valid_hashes(self):
        self.create()
        original = self.entries()
        for name, data in [('settings.json', b'{"binder_name":"one","binder_name":"two"}'),
                           ('advanced.json', b'{"sampling_temp":NaN}')]:
            with self.subTest(name=name):
                changed = dict(original)
                changed[name] = data
                self.rewrite(self.rehash(changed))
                with self.assertRaises(ValueError):
                    bundle.inspect(self.path)

    def test_boolean_or_nonobject_manifest_is_not_a_valid_schema(self):
        self.create()
        original = self.entries()
        boolean = json.loads(original['manifest.json'])
        boolean['schema'] = True
        for manifest in (boolean, [], None, 'not a manifest'):
            with self.subTest(manifest=manifest):
                changed = dict(original)
                changed['manifest.json'] = bundle.encoded(manifest)
                self.rewrite(changed)
                with self.assertRaises(ValueError):
                    bundle.inspect(self.path)

    def test_decompression_limit_is_enforced_before_any_output_is_written(self):
        self.create()
        expanded = sum(len(data) for data in self.entries().values())
        self.assertLess(self.path.stat().st_size, expanded - 1)
        with patch.object(bundle, 'MAX_BYTES', expanded - 1), self.assertRaisesRegex(ValueError, 'Expanded'):
            bundle.materialize(self.path, self.root / 'restored')
        self.assertFalse((self.root / 'restored').exists())

    def test_target_selection_and_hotspots_must_refer_to_real_canonical_residues(self):
        bad_settings = [dict(self.settings, chains='C'), dict(self.settings, chains='A,A'),
                        dict(self.settings, target_hotspot_residues='B5'),
                        dict(self.settings, target_hotspot_residues='A5-8'),
                        dict(self.settings, target_hotspot_residues='A7-5')]
        for settings in bad_settings:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.create(settings=settings)
            self.assertFalse(self.path.exists())
        selected = dict(self.settings, chains='A,B', target_hotspot_residues='A5,B7')
        self.create(settings=selected)
        self.assertEqual(bundle.inspect(self.path)[0]['kind'], 'bindcraft-input')

    def test_malformed_coordinates_nonprotein_selected_chains_and_multiple_models_are_rejected(self):
        original = self.pdb.read_bytes()
        lines = original.decode().splitlines()
        malformed = [b'ATOM  truncated\n', original.replace(b' ALA A', b' MSE A'),
                     ('MODEL        1\n' + original.decode() + 'ENDMDL\nMODEL        2\n' + original.decode()).encode(),
                     (lines[0][:30] + '     nan' + lines[0][38:] + '\n' + '\n'.join(lines[1:])).encode(),
                     (lines[0][:26] + 'A' + lines[0][27:] + '\n' + '\n'.join(lines[1:])).encode()]
        for data in malformed:
            with self.subTest(target=data[:80]):
                self.pdb.write_bytes(data)
                with self.assertRaises(ValueError):
                    self.create()
                self.assertFalse(self.path.exists())

    def test_runtime_asset_overrides_invalid_design_bounds_and_boolean_counts_are_rejected(self):
        for key in ('af_params_dir', 'dssp_path', 'dalphaball_path'):
            advanced = deepcopy(self.advanced)
            advanced[key] = '/tmp/untrusted-asset'
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'managed by the cluster'):
                self.create(advanced=advanced)
        for settings in (dict(self.settings, lengths=[25, 20]), dict(self.settings, lengths=[True, 20]),
                         dict(self.settings, number_of_final_designs=True),
                         dict(self.settings, starting_pdb='/tmp/elsewhere.pdb')):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.create(settings=settings)
        self.assertFalse(self.path.exists())

    def test_materialization_never_overwrites_an_existing_directory(self):
        self.create()
        destination = self.root / 'existing'
        destination.mkdir()
        marker = destination / 'keep'
        marker.write_bytes(b'existing run')
        with self.assertRaises(FileExistsError):
            bundle.materialize(self.path, destination)
        self.assertEqual(list(destination.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b'existing run')


if __name__ == '__main__':
    unittest.main()
