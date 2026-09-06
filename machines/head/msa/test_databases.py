"""Offline failure/resume tests; synthetic fixtures never qualify a real snapshot."""
import gzip
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import databases as db
import server


def archive(path, entries):
    with tarfile.open(path, 'w:gz') as tar:
        for name, content, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            member.linkname = '../outside' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
            tar.addfile(member, io.BytesIO(content) if member.isfile() else None)


def fake_database(prefix, expanded=False, index=True):
    suffixes = ['', '_h'] + (['_seq', '_seq_h', '_aln'] if expanded else [])
    if index:
        suffixes += ['.idx']
    for suffix in suffixes:
        p = Path(str(prefix) + suffix)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'ACDE\n\0')
        Path(str(p)+'.index').write_text('0\t0\t6\n')
        Path(str(p)+'.dbtype').write_bytes(b'\x00\x00\x00\x00')


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.space = patch.object(db, 'free_space')
        self.space.start()

    def tearDown(self):
        self.space.stop()
        self.tmp.cleanup()

    def source(self, content=b'archive'):
        return dict(archive='test.tar.gz', bytes=len(content), md5=hashlib.md5(content).hexdigest(), url='https://invalid.test/test.tar.gz')

    def test_complete_partial_is_verified_without_redownload(self):
        content = b'completed partial'
        source = self.source(content)
        (self.root/'.archives').mkdir()
        partial = self.root/'.archives/test.tar.gz.part'
        partial.write_bytes(content)
        with patch.object(db.subprocess, 'run') as run:
            path, receipt = db.download(self.root, source)
        run.assert_not_called()
        self.assertFalse(partial.exists())
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(receipt['sha256'], hashlib.sha256(content).hexdigest())

    def test_partial_download_resumes_and_verifies(self):
        content = b'complete source'
        (self.root/'.archives').mkdir()
        partial = self.root/'.archives/test.tar.gz.part'
        partial.write_bytes(content[:3])
        def complete(command, **kwargs):
            self.assertIn('--continue-at', command)
            self.assertEqual(partial.read_bytes(), content[:3])
            partial.write_bytes(content)
        with patch.object(db.subprocess, 'run', side_effect=complete) as run:
            db.download(self.root, self.source(content))
        self.assertEqual(run.call_count, 1)

    def test_wrong_published_hash_never_writes_receipt(self):
        (self.root/'.archives').mkdir()
        path = self.root/'.archives/test.tar.gz'
        path.write_bytes(b'wrong!!')
        with self.assertRaisesRegex(RuntimeError, 'MD5 mismatch'):
            db.download(self.root, self.source(b'archive'))
        self.assertTrue(path.exists())
        self.assertFalse(path.with_suffix('.gz.json').exists())

    def test_unpublished_source_cannot_change_after_first_verified_download(self):
        (self.root/'.archives').mkdir()
        path = self.root/'.archives/test.tar.gz'
        path.write_bytes(b'archive')
        source = dict(self.source(), md5=None)
        db.download(self.root, source)
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'changed after validation'):
            db.download(self.root, source)

    def test_archive_rejects_traversal_and_links(self):
        for name, kind in [('../outside', tarfile.REGTYPE), ('/outside', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('link', tarfile.LNKTYPE)]:
            with self.subTest(name=name, kind=kind):
                path = self.root/'unsafe.tar.gz'
                archive(path, [(name, b'x', kind)])
                with self.assertRaisesRegex(RuntimeError, 'Unsafe archive entry'):
                    db.extract(path, self.root/'out')

    def test_archive_crc_failure_and_missing_selected_file(self):
        path = self.root/'data.tar.gz'
        archive(path, [('valid', b'abc', tarfile.REGTYPE)])
        with self.assertRaisesRegex(RuntimeError, 'Missing selected'):
            db.extract(path, self.root/'out', {'missing'})
        content = bytearray(path.read_bytes())
        content[-8] ^= 255
        path.write_bytes(content)
        with self.assertRaises((gzip.BadGzipFile, EOFError)):
            db.extract(path, self.root/'crc')

    def test_existing_destination_symlink_is_not_followed(self):
        path = self.root/'data.tar.gz'
        archive(path, [('valid', b'abc', tarfile.REGTYPE)])
        target = self.root/'out'; target.mkdir()
        (target/'valid').symlink_to(self.root/'outside')
        with self.assertRaisesRegex(RuntimeError, 'Unsafe extraction destination'):
            db.extract(path, target)
        self.assertFalse((self.root/'outside').exists())

    def test_index_and_expansion_are_required(self):
        prefix = self.root/'db'
        fake_database(prefix, expanded=True)
        db.validate_db(prefix, True)
        Path(str(prefix)+'_aln').unlink()
        with self.assertRaisesRegex(RuntimeError, 'Missing complete'):
            db.validate_db(prefix, True)
        fake_database(prefix, expanded=True)
        Path(str(prefix)+'.idx.index').write_text('0\t100\t6\n')
        with self.assertRaisesRegex(RuntimeError, 'Truncated'):
            db.validate_db(prefix, True)

    def test_aliases_survive_atomic_promotion(self):
        stage = self.root/'stage'; stage.mkdir()
        (stage/'actual').write_text('data')
        (stage/'alias').symlink_to(stage/'actual')
        db.relocate_links(stage)
        stage.rename(self.root/'final')
        self.assertEqual((self.root/'final/alias').read_text(), 'data')
        (self.root/'final/bad').symlink_to(self.root/'outside')
        with self.assertRaises((RuntimeError, FileNotFoundError)):
            db.relocate_links(self.root/'final')

    def test_template_offsets_checked(self):
        (self.root/'pdb100_a3m.ffdata').write_bytes(b'abc\0')
        (self.root/'pdb100_a3m.ffindex').write_text('1abc_A\t0\t4\n')
        db.validate_component(self.root, 'templates')
        (self.root/'pdb100_a3m.ffindex').write_text('1abc_A\t0\t5\n')
        with self.assertRaisesRegex(RuntimeError, 'Truncated template'):
            db.validate_component(self.root, 'templates')

    def test_small_coordinate_mirror_never_qualifies(self):
        (self.root/'divided/ab').mkdir(parents=True)
        (self.root/'divided/ab/1abc.cif.gz').write_bytes(gzip.compress(b'data_1abc'))
        with self.assertRaisesRegex(RuntimeError, 'Incomplete full mmCIF'):
            db.validate_component(self.root, 'mmcif')

    def test_failed_index_retains_sources_and_reuses_completed_conversion(self):
        path = self.root/'.archives'/db.SOURCES['pdb100']['archive']
        path.parent.mkdir()
        path.write_bytes(gzip.compress(b'>x\nACDE\n'))
        receipt = {'sha256': db.digest(path)[0]}
        db.write_json(path.with_suffix('.gz.json'), receipt)
        calls = []
        fail_index = True
        def run(tool, args, stage, commands):
            calls.append(args[0])
            if args[0] == 'createdb':
                fake_database(Path(args[2]), index=False)
            else:
                self.assertEqual(args[0], 'createindex')
                if fail_index:
                    Path(str(args[1])+'.idx').write_bytes(b'partial')
                    raise subprocess.CalledProcessError(1, args)
                fake_database(Path(args[1]))
        with patch.object(db, 'download', return_value=(path, receipt)), patch.object(db, 'run_mmseqs', side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                db.install_component(self.root, 'pdb100', 'mmseqs', 2, 'unused::mirror', 873)
            self.assertTrue(path.exists())
            self.assertFalse((self.root/'pdb100').exists())
            self.assertFalse((self.root/'.msa-databases.json').exists())
            fail_index = False
            db.install_component(self.root, 'pdb100', 'mmseqs', 2, 'unused::mirror', 873)
        self.assertEqual(calls, ['createdb', 'createindex', 'createindex'])
        self.assertFalse(path.exists())
        self.assertFalse((self.root/'.staging/pdb100').exists())
        db.validate_db(self.root/'pdb100'/db.PREFIXES['pdb100'])
        (self.root/'.components/pdb100.json').unlink()
        db.install_component(self.root, 'pdb100', 'unused', 2, 'unused::mirror', 873)
        self.assertTrue((self.root/'.components/pdb100.json').exists())

    def test_unfinished_conversion_is_rebuilt(self):
        stage = self.root/'.staging/pdb100/built'; stage.mkdir(parents=True)
        (stage/'garbage').write_text('interrupted')
        path = self.root/'source.gz'; path.write_bytes(b'fake')
        def run(tool, args, stage, commands):
            self.assertFalse((stage/'built/garbage').exists())
            fake_database(Path(args[2] if args[0] == 'createdb' else args[1]))
        with patch.object(db, 'download', return_value=(path, {})), patch.object(db, 'run_mmseqs', side_effect=run):
            db.install_component(self.root, 'pdb100', 'mmseqs', 2, 'unused::mirror', 873)

    def test_promoted_component_finishes_interrupted_source_cleanup(self):
        final = self.root/'pdb100'
        fake_database(final/db.PREFIXES['pdb100'])
        source = self.root/'.archives'/db.SOURCES['pdb100']['archive']
        source.parent.mkdir()
        source.write_bytes(b'verified source')
        receipt = dict(manifest_sha256=db.MANIFEST_SHA256,
                       sources={'pdb100': {'sha256': db.digest(source)[0]}},
                       files=db.validate_component(final,'pdb100'))
        db.write_json(source.with_suffix('.gz.json'),receipt['sources']['pdb100'])
        db.write_json(final/'.component.json',receipt)
        stage = self.root/'.staging/pdb100'; stage.mkdir(parents=True)
        (stage/'source.tsv').write_text('large intermediate')
        db.install_component(self.root,'pdb100','unused',2,'unused::mirror',873)
        self.assertFalse(stage.exists())
        self.assertFalse(source.exists())
        self.assertTrue(source.with_suffix('.gz.json').exists())

    def test_executable_digest_must_match_pinned_build_before_execution(self):
        binary = self.root/'bin/mmseqs'; binary.parent.mkdir()
        binary.write_text('different build')
        with patch.object(db.subprocess,'check_output') as run:
            with self.assertRaisesRegex(RuntimeError,'differs from the pinned'):
                db.tools(self.root)
        run.assert_not_called()

    def test_complete_receipt_requires_all_components_and_manifest(self):
        db.write_json(self.root/'manifest.json', db.MANIFEST)
        with self.assertRaises(FileNotFoundError):
            db.validate(self.root)
        db.write_json(self.root/'manifest.json', dict(db.MANIFEST, fast_prebuilt_databases=True))
        with self.assertRaisesRegex(RuntimeError, 'manifest does not match'):
            db.validate(self.root)

    def test_gpu_environment_cannot_change_install(self):
        flags = {'GPU':'1','MMSEQS_FORCE_GPU':'1','MMSEQS_FORCE_GPUSERVER':'1',
                 'MMSEQS_NO_INDEX':'1','MMSEQS_IGNORE_INDEX':'1'}
        with patch.dict(db.os.environ, flags), \
                patch.object(db.subprocess, 'run') as run:
            db.run_mmseqs('mmseqs', ['createdb','input','out'], self.root, [])
        env = run.call_args.kwargs['env']
        for name in flags:
            self.assertNotIn(name, env)
        self.assertEqual(env['MMSEQS_FORCE_MERGE'], '1')

    def test_low_space_fails_clearly(self):
        self.space.stop()
        with patch.object(db.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100,99,1)):
            with self.assertRaisesRegex(RuntimeError, 'never reduce the databases'):
                db.free_space(self.root, 2)
        self.space.start()

    def test_head_download_resumes_after_failure_without_tools_or_installation_receipt(self):
        first, second = b'first source', b'second source'
        sources = {'one': dict(self.source(first),archive='one.tar.gz'),
                   'two': dict(self.source(second),archive='two.tar.gz')}
        fail_once = True
        def curl(command, **kwargs):
            nonlocal fail_once
            target = Path(command[command.index('--output')+1])
            if target.name == 'one.tar.gz.part':
                target.write_bytes(first)
            else:
                if fail_once:
                    target.write_bytes(second[:3]); fail_once=False
                    raise subprocess.CalledProcessError(1,command)
                self.assertEqual(target.read_bytes(),second[:3])
                self.assertIn('--continue-at',command)
                target.write_bytes(second)
        with patch.object(db,'SOURCES',sources), patch.object(db.subprocess,'run',side_effect=curl) as run, \
                patch.object(db,'tools',side_effect=AssertionError('download must not bootstrap tools')), \
                patch.object(db,'install_component',side_effect=AssertionError('download must not install')):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(subprocess.CalledProcessError):
                db.main(['download','--root',str(self.root)])
            self.assertTrue((self.root/'manifest.json').exists())
            self.assertTrue((self.root/'.archives/one.tar.gz.json').exists())
            self.assertFalse((self.root/'.downloads.json').exists())
            with contextlib.redirect_stdout(io.StringIO()) as output:
                db.main(['download','--root',str(self.root)])
            self.assertFalse(json.loads(output.getvalue())['production_ready'])
            self.assertEqual(run.call_count,3)
            with contextlib.redirect_stdout(io.StringIO()):
                db.main(['download','--root',str(self.root)])
            self.assertEqual(run.call_count,3)
        self.assertFalse((self.root/'.msa-databases.json').exists())
        self.assertFalse((self.root/'.components').exists())
        self.assertEqual(set(db.load(self.root/'.downloads.json')['sources']),{'one','two'})

    def test_download_preflight_accounts_for_all_remaining_archives(self):
        content = b'first source'
        sources = {'one':dict(self.source(content),archive='one.tar.gz'),
                   'two':dict(self.source(content),archive='two.tar.gz')}
        (self.root/'.archives').mkdir()
        (self.root/'.archives/one.tar.gz.part').write_bytes(content[:3])
        with patch.object(db,'SOURCES',sources), patch.object(db,'download',return_value=(None,{})), \
                patch.object(db,'free_space') as space:
            db.download_sources(self.root)
        space.assert_called_once_with(self.root, 2*len(content)-3+16*db.GIB)

    def test_conversion_stops_before_index_and_full_install_reuses_it(self):
        path = self.root/'.archives'/db.SOURCES['pdb100']['archive']
        path.parent.mkdir()
        path.write_bytes(gzip.compress(b'>x\nACDE\n'))
        source = {'sha256': db.digest(path)[0]}
        db.write_json(path.with_suffix('.gz.json'), source)
        calls = []
        def run(tool, args, stage, commands):
            calls.append(args[0])
            commands.append([tool, *map(str, args)])
            db.write_json(stage/'commands.json', commands)
            fake_database(Path(args[2] if args[0] == 'createdb' else args[1]),
                          index=args[0] == 'createindex')
        with patch.object(db, 'download', return_value=(path, source)), \
                patch.object(db, 'run_mmseqs', side_effect=run):
            first = db.install_component(self.root, 'pdb100', 'mmseqs', 2, 'unused::mirror', 873, convert_only=True)
            prefix = self.root/'.staging/pdb100/built'/db.PREFIXES['pdb100']
            self.assertEqual(calls, ['createdb'])
            self.assertFalse(Path(str(prefix)+'.idx').exists())
            self.assertFalse((self.root/'.components').exists())
            self.assertFalse((self.root/'.msa-databases.json').exists())
            self.assertTrue(path.exists())
            self.assertFalse(first['production_ready'])
            self.assertEqual(first['statistics']['representatives']['residues'], 4)
            repeated = db.install_component(self.root, 'pdb100', 'mmseqs', 2, 'unused::mirror', 873, convert_only=True)
            self.assertEqual(first, repeated)
            self.assertEqual(calls, ['createdb'])
            # An interrupted index must not poison the reusable conversion.
            Path(str(prefix)+'.idx').touch()
            db.install_component(self.root, 'pdb100', 'mmseqs', 2, 'unused::mirror', 873)
        self.assertEqual(calls, ['createdb', 'createindex'])
        self.assertFalse(path.exists())
        self.assertFalse((self.root/'.staging/pdb100').exists())
        installed = db.load(self.root/'.components/pdb100.json')
        self.assertEqual(first['statistics'], installed['statistics'])
        self.assertEqual([command[1] for command in installed['commands']], ['createdb', 'createindex'])
        self.assertEqual(first, db.load(self.root/'.conversions/pdb100.json'))

    def test_changed_converted_output_fails_before_indexing(self):
        path = self.root/'source.gz'; path.write_bytes(b'fake')
        def run(tool, args, stage, commands):
            fake_database(Path(args[2]), index=False)
        with patch.object(db, 'download', return_value=(path, {})), \
                patch.object(db, 'run_mmseqs', side_effect=run) as mmseqs:
            db.install_component(self.root, 'pdb100', 'mmseqs', 2, 'unused::mirror', 873, convert_only=True)
            prefix = self.root/'.staging/pdb100/built'/db.PREFIXES['pdb100']
            prefix.write_bytes(b'AAAA\n\0')
            with self.assertRaisesRegex(RuntimeError, 'differs from its receipt'):
                db.install_component(self.root, 'pdb100', 'mmseqs', 2, 'unused::mirror', 873)
            self.assertEqual(mmseqs.call_count, 1)
        self.assertFalse((self.root/'.components').exists())

    def test_conversion_has_latest_taxonomy_before_any_index(self):
        path = self.root/'source.tar.gz'; path.write_bytes(b'fake')
        def extract(source, target):
            target.mkdir(parents=True, exist_ok=True)
            if target.name == 'taxonomy':
                (target/(db.PREFIXES['uniref30']+'_mapping')).write_bytes(bytes.fromhex('1300170c')+b'mapping')
                (target/(db.PREFIXES['uniref30']+'_taxonomy')).write_bytes(b'latest taxonomy')
        def run(tool, args, stage, commands):
            self.assertEqual(args[0], 'tsv2exprofiledb')
            fake_database(Path(args[2]), expanded=True, index=False)
        with patch.object(db, 'download', return_value=(path, {})), \
                patch.object(db, 'extract', side_effect=extract), \
                patch.object(db, 'run_mmseqs', side_effect=run) as mmseqs:
            receipt = db.install_component(self.root, 'uniref30', 'mmseqs', 2, 'unused::mirror', 873, convert_only=True)
        prefix = self.root/'.staging/uniref30/built'/db.PREFIXES['uniref30']
        self.assertEqual(Path(str(prefix)+'.idx_taxonomy').read_bytes(), b'latest taxonomy')
        self.assertFalse(Path(str(prefix)+'.idx').exists())
        self.assertEqual(mmseqs.call_count, 1)
        self.assertEqual(set(receipt['sources']), {'uniref30', 'taxonomy'})
        self.assertEqual(set(receipt['statistics']), {'representatives', 'members'})

    def test_statistics_use_decoded_lengths_and_reject_invalid_or_padded_rows(self):
        prefix = self.root/'compressed'
        prefix.write_bytes(b'compressed data')
        Path(str(prefix)+'.dbtype').write_bytes((0x80000000).to_bytes(4, 'little'))
        index = Path(str(prefix)+'.index')
        index.write_text('0\t0\t78\n1\t8\t80\n')
        stats = db.sequence_statistics(prefix)
        self.assertEqual((stats['entries'], stats['residues'], stats['max_length']), (2, 154, 78))
        index.write_text('0\t0\t78\n1\t8\tbroken\n')
        with self.assertRaises(subprocess.CalledProcessError):
            db.sequence_statistics(prefix)
        Path(str(prefix)+'.dbtype').write_bytes((8 << 16).to_bytes(4, 'little'))
        with self.assertRaisesRegex(RuntimeError, 'unpadded amino-acid'):
            db.sequence_statistics(prefix)

    def test_conversion_rejects_head_ram_before_loading_tools_or_creating_root(self):
        root = self.root/'not-created'
        with patch.object(Path, 'read_text', return_value='MemAvailable: 16000000 kB\n'), \
                patch.object(db, 'tools') as tools:
            with self.assertRaisesRegex(RuntimeError, 'at least 56 GiB available RAM'):
                db.main(['convert', '--root', str(root)])
        tools.assert_not_called()
        self.assertFalse(root.exists())

    def test_convert_main_writes_only_partial_receipt(self):
        converted = dict(stage='component-converted', production_ready=False)
        with patch.object(Path, 'read_text', return_value='MemAvailable: 64000000 kB\n'), \
                patch.object(db, 'tools', return_value={'mmseqs':'pinned-mmseqs'}), \
                patch.object(db, 'install_component', return_value=converted) as install, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            db.main(['convert', '--root', str(self.root)])
        self.assertEqual([call.args[1] for call in install.call_args_list], list(db.PREFIXES))
        self.assertTrue(all(call.kwargs['convert_only'] for call in install.call_args_list))
        result = db.load(self.root/'.conversions.json')
        self.assertEqual(result, json.loads(output.getvalue()))
        self.assertFalse(result['production_ready'])
        self.assertFalse((self.root/'.msa-databases.json').exists())
        self.assertFalse((self.root/'.components').exists())


class ServerTests(unittest.TestCase):
    def test_effective_mmseqs_thread_limit_is_bound_to_provenance_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready = dict(prefixes={k:'/db/'+k for k in db.PREFIXES},pdb70='/db/pdb100',pdbdivided='/db/divided',pdbobsolete='/db/obsolete')
            tools = dict(mmseqs='/tools/mmseqs',server='/tools/server')
            with patch.object(db,'validate',return_value=ready), patch.object(db,'tools',return_value=tools):
                with patch.dict(db.os.environ,{'MMSEQS_NUM_THREADS':'16'}):
                    _, first = server.configuration(root,root/'results',root/'tools')
                with patch.dict(db.os.environ,{'MMSEQS_NUM_THREADS':'8'}):
                    _, second = server.configuration(root,root/'results',root/'tools')
                with patch.dict(db.os.environ,{'MMSEQS_NUM_THREADS':'0'}):
                    with self.assertRaisesRegex(RuntimeError,'must be positive'):
                        server.configuration(root,root/'results',root/'tools')
            self.assertEqual(first['runtime']['mmseqs_threads'],16)
            self.assertEqual(second['runtime']['mmseqs_threads'],8)
            self.assertNotEqual(first['namespace'],second['namespace'])

    def test_namespace_pins_database_and_tools_and_only_binds_localhost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready = dict(prefixes={k:'/db/'+k for k in db.PREFIXES},pdb70='/db/pdb100',pdbdivided='/db/divided',pdbobsolete='/db/obsolete',manifest_sha256='first')
            tools = dict(mmseqs='/tools/mmseqs',server='/tools/server',mmseqs_sha256='binary1')
            with patch.object(db, 'validate', return_value=ready), patch.object(db, 'tools', return_value=tools):
                first, provenance = server.configuration(root, root/'results', root/'tools')
                ready['manifest_sha256'] = 'second'
                second, other = server.configuration(root, root/'results', root/'tools')
                tools['mmseqs_sha256'] = 'binary2'
                third, changed = server.configuration(root, root/'results', root/'tools')
            self.assertEqual(first['server']['address'], '127.0.0.1:8080')
            self.assertFalse(first['server']['dbmanagment'])
            self.assertNotIn('gpu', first['paths']['colabfold'])
            self.assertNotIn('environmentalpair', first['paths']['colabfold'])
            self.assertFalse(first['paths']['colabfold']['parallelstages'])
            self.assertNotEqual(provenance['namespace'], other['namespace'])
            self.assertNotEqual(other['namespace'], changed['namespace'])
            self.assertNotEqual(first['paths']['results'], second['paths']['results'])


if __name__ == '__main__':
    unittest.main()
