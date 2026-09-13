"""Research exports preserve pinned chemistry/intent and reject corrupt archives."""
import copy
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import context as c
import registry as r


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.library = r.Registry(self.base/'library');self.library.init()
        self.brief = self.base/'brief.md'
        self.brief.write_bytes(b'# Research question\r\n\r\nMeasure binding under specified conditions. No results yet.\r\n')
        self.original = self.base/'source.fa';self.original.write_bytes(b'>exact source\r\nACDE\r\n')
        self.asset = self.base/'original.sdf';self.asset.write_bytes(b'original unresolved chemistry\r\n\x00preserve bytes\n')
        self.library.import_record({'kind':'construct','id':'protein','identity':{'molecule_type':'protein','sequence':'ACDE'}}, {'source.fa':self.original})
        self.library.describe('protein', self.brief)
        self.library.import_record({'kind':'monomer','id':'custom','identity':{'structure_file':'attachments/original.sdf'}}, {'original.sdf':self.asset})
        self.library.import_record({'kind':'construct','id':'oligo','identity':{'molecule_type':'dna','sequence':'ACGT','modifications':[{'position':2,'monomer_ref':'custom'}]}})
        self.library.import_record({'kind':'assembly','id':'complex','identity':{'components':[{'chain_id':'P','construct_ref':'protein'},{'chain_id':'D','construct_ref':'oligo'}]}})
        self.library.import_record(r.projects.project_document('study',members=['construct:protein@1']), {'project.md':self.brief})
        self.library.revise('study', {'identity':r.projects.project_document('study',members=[{'source_ref':'complex','role':'Test the assembly hypothesis'}])['identity']})
        self.expected = self.library.project_snapshot('study')
        # A newer monomer is intentionally outside this project's exact closure.
        self.library.revise('custom', {'notes':'Later revision must not replace pinned chemistry'})

    def tearDown(self):
        self.temporary.cleanup()

    def export(self, name='workspace'):
        path=self.base/name
        return path,c.export_directory(self.library,'study',path)

    def archive(self, name='context.tar.gz'):
        path=self.base/name
        c.export_archive(self.library,'study',path)
        return path

    def test_cleared_project_brief_exports_and_verifies_as_incomplete(self):
        self.brief.write_bytes(b'')
        project = self.library.describe('study', self.brief)
        path, manifest = self.export('cleared-project')
        self.assertEqual((path / 'project.md').read_bytes(), b'')
        self.assertEqual(manifest['project_brief_state'], 'incomplete_empty')
        self.assertIn({'ref': r.reference(project), 'code': 'incomplete_project_brief', 'path': 'project.md'}, manifest['warnings'])
        c.verify_directory(path)

    def rewrite_archive(self, source, name, transform):
        entries=[]
        with tarfile.open(source,'r:gz') as archive:
            for entry in archive:
                entries.append((copy.copy(entry),archive.extractfile(entry).read()))
        entries=transform(entries)
        target=self.base/name
        with tarfile.open(target,'w:gz') as archive:
            for entry,raw in entries:
                if entry.isfile():entry.size=len(raw)
                archive.addfile(entry,io.BytesIO(raw) if entry.isfile() else None)
        return target

    def test_export_preserves_all_exact_assets_parents_and_compilable_registry(self):
        with patch.object(subprocess,'run',side_effect=AssertionError('Export must not run models or commands')):
            path,manifest=self.export()
        self.assertEqual(manifest['project_ref'],'project:study@2')
        self.assertEqual(manifest['project_snapshot_sha256'],self.expected['sha256'])
        self.assertEqual((path/'project.md').read_bytes(),self.brief.read_bytes())
        self.assertIn('project:study@1',manifest['records'])
        self.assertIn('construct:protein@1',manifest['records'])
        self.assertIn('monomer:custom@1',manifest['records'])
        self.assertNotIn('monomer:custom@2',manifest['records'])
        for ref,entry in manifest['records'].items():
            self.assertEqual((path/entry['path']).read_bytes(),self.library.record_path(ref).read_bytes())
            for relative,asset in entry['attachments'].items():
                self.assertEqual((path/asset['path']).read_bytes(),self.library.attachment_path(ref,relative).read_bytes())
        working=path/'analysis/registry'
        shutil.copytree(path/'inputs/registry',working)
        restored=r.Registry(working);restored.init()
        self.assertEqual(restored.project_snapshot('study'),self.expected)
        self.assertEqual(restored.snapshot('assembly:complex@1'),self.expected['members'][0]['snapshot'])
        self.assertEqual(c.verify_directory(path),manifest)

    def test_scaffolds_are_flagged_and_templates_have_no_findings(self):
        path,manifest=self.export()
        warnings={(row['ref'],row['code']) for row in manifest['warnings']}
        self.assertIn(('construct:oligo@1','incomplete_description'),warnings)
        self.assertIn(('assembly:complex@1','incomplete_description'),warnings)
        self.assertNotIn(('construct:protein@2','incomplete_description'),warnings)
        self.assertEqual(manifest['project_brief_state'],'unassessed')
        assessment=json.loads((path/'analysis/assessment.json').read_text())
        self.assertEqual(assessment['status'],'unassessed')
        self.assertEqual(assessment['findings'],[])
        self.assertEqual(assessment['criteria'],[])
        for text in ('prediction-supported','contradicted','inconclusive','needs-experiment','Confidence scores'):
            self.assertIn(text,(path/'AGENTS.md').read_text())

    def test_legacy_missing_descriptions_and_empty_scaffold_project_are_explicit(self):
        record=self.library.show('construct:oligo@1')
        location=self.library.record_path('construct:oligo@1')
        record['attachments']=[];record['sha256']=r.digest_json(record)
        location.write_text(json.dumps(record));(location.parent/'attachments/description.md').unlink()
        _,manifest=self.export()
        self.assertIn({'ref':'construct:oligo@1','code':'missing_description','path':'descriptions/constructs/oligo/1.md'},manifest['warnings'])
        self.brief.write_bytes(r.projects.scaffold('project','Unspecified'))
        self.library.import_record(r.projects.project_document('empty'),{'project.md':self.brief})
        target=self.base/'empty';result=c.export_directory(self.library,'empty',target)
        self.assertEqual({row['code'] for row in result['warnings']},{'incomplete_project_brief','no_project_members'})

    def test_archive_roundtrip_and_mutable_analysis_are_separate(self):
        archive=self.archive();manifest=c.verify_archive(archive)
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode),0o600)
        target=self.base/'restored';self.assertEqual(c.extract_archive(archive,target),manifest)
        self.assertEqual(stat.S_IMODE((target/'project.md').stat().st_mode),0o600)
        (target/'analysis/assessment.json').write_text('{"status":"work in progress"}')
        (target/'analysis/origin.json').write_text('{"head":"test"}')
        (target/'analysis/large-environment').symlink_to(self.library.root,target_is_directory=True)
        (target/'analysis/missing-target').symlink_to(self.base/'absent')
        self.assertEqual(c.verify_directory(target),manifest)
        self.assertTrue(os.access(target/'analysis',os.W_OK))
        (target/'project.md').write_text('changed objective')
        with self.assertRaisesRegex(c.Error,'checksum'):
            c.verify_directory(target)

    def test_unrelated_or_missing_frozen_files_and_symlinked_roots_fail(self):
        path,_=self.export()
        (path/'inputs/extra.py').write_text('unrelated')
        with self.assertRaisesRegex(c.Error,'inventory'):
            c.verify_directory(path)
        (path/'inputs/extra.py').unlink();original=(path/'project.md').read_bytes();(path/'project.md').unlink()
        with self.assertRaises(c.Error):c.verify_directory(path)
        (path/'project.md').symlink_to(self.brief)
        with self.assertRaisesRegex(c.Error,'Symlinks'):c.verify_directory(path)
        (path/'project.md').unlink();(path/'project.md').write_bytes(original)
        shutil.rmtree(path/'analysis');(path/'analysis').symlink_to(self.base,target_is_directory=True)
        with self.assertRaisesRegex(c.Error,'Symlinks'):c.verify_directory(path)

    def test_no_overwrite_and_concurrent_empty_directory_is_preserved(self):
        path,manifest=self.export()
        with self.assertRaisesRegex(c.Error,'already exists'):c.export_directory(self.library,'study',path)
        archive=self.archive()
        with self.assertRaisesRegex(c.Error,'already exists'):c.export_archive(self.library,'study',archive)
        with self.assertRaisesRegex(c.Error,'already exists'):c.extract_archive(archive,path)
        target=self.base/'raced';publish=c._publish_directory;inode=[]
        def race(stage,destination):
            destination.mkdir();inode.append(destination.stat().st_ino)
            publish(stage,destination)
        with patch.object(c,'_publish_directory',side_effect=race),self.assertRaisesRegex(c.Error,'already exists'):
            c.extract_archive(archive,target)
        self.assertEqual(target.stat().st_ino,inode[0]);self.assertEqual(list(target.iterdir()),[])
        self.assertEqual(c.verify_directory(path),manifest)

    def test_attachment_tamper_during_export_never_publishes(self):
        wrong=self.base/'wrong';wrong.write_bytes(b'wrong original')
        actual=self.library.attachment_path
        def changed(ref,relative):
            return wrong if relative=='attachments/original.sdf' else actual(ref,relative)
        target=self.base/'failed'
        with patch.object(self.library,'attachment_path',side_effect=changed),self.assertRaisesRegex(c.Error,'differs'):
            c.export_directory(self.library,'study',target)
        self.assertFalse(target.exists())
        self.assertFalse(list(self.base.glob('.research-context-*')))

    def test_archive_checksum_and_duplicate_members_fail(self):
        archive=self.archive()
        def corrupt(entries):
            for i,(entry,raw) in enumerate(entries):
                if entry.name=='project.md':entries[i]=(entry,raw+b'changed')
            return entries
        changed=self.rewrite_archive(archive,'changed.tar.gz',corrupt)
        with self.assertRaisesRegex(c.Error,'checksum'):c.verify_archive(changed)
        duplicate=self.rewrite_archive(archive,'duplicate.tar.gz',lambda entries:entries+[entries[0]])
        with self.assertRaisesRegex(c.Error,'Duplicate'):c.verify_archive(duplicate)
        target=self.base/'not-published'
        with self.assertRaises(c.Error):c.extract_archive(changed,target)
        self.assertFalse(target.exists())

    def test_archive_paths_links_and_special_files_are_rejected(self):
        archive=self.archive()
        cases=[('../escape',tarfile.REGTYPE),('/absolute',tarfile.REGTYPE),('.',tarfile.REGTYPE),
               ('analysis/../../escape',tarfile.REGTYPE),('analysis\\escape',tarfile.REGTYPE),
               ('analysis/link',tarfile.SYMTYPE),('analysis/hardlink',tarfile.LNKTYPE),
               ('analysis/fifo',tarfile.FIFOTYPE),('analysis/directory',tarfile.DIRTYPE)]
        for index,(name,kind) in enumerate(cases):
            with self.subTest(name=name,kind=kind):
                item=tarfile.TarInfo(name);item.type=kind;item.linkname='../escape'
                bad=self.rewrite_archive(archive,f'bad-{index}.tar.gz',lambda rows:rows+[(item,b'payload')])
                with self.assertRaises(c.Error):c.verify_archive(bad)
        self.assertFalse((self.base/'escape').exists())

    def test_archive_sizes_metadata_and_trailing_data_are_bounded(self):
        archive=self.archive()
        for field,value in [('MAX_ARCHIVE_BYTES',1),('MAX_TAR_BYTES',100),('MAX_METADATA_BYTES',100),('MAX_FILES',2)]:
            with self.subTest(field=field),patch.object(c,field,value),self.assertRaises(c.Error):c.verify_archive(archive)
        target=self.base/'trailing.tar.gz'
        target.write_bytes(gzip.compress(gzip.decompress(archive.read_bytes())+b'hidden non-tar data'))
        with self.assertRaisesRegex(c.Error,'Unexpected data'):c.verify_archive(target)

    def test_archive_cannot_use_a_file_as_another_files_parent(self):
        archive=self.archive()
        first=tarfile.TarInfo('analysis/clash');second=tarfile.TarInfo('analysis/clash/child')
        target=self.rewrite_archive(archive,'conflict.tar.gz',lambda rows:rows+[(first,b'file'),(second,b'child')])
        with self.assertRaisesRegex(c.Error,'parent directory'):c.verify_archive(target)

    def test_resealed_manifest_cannot_omit_required_parent_dependency(self):
        path,manifest=self.export()
        ref='project:study@1';entry=manifest['records'].pop(ref)
        for relative in [entry['path'],*[a['path'] for a in entry['attachments'].values()]]:
            manifest['files'].pop(relative);(path/relative).unlink()
        manifest['sha256']=r.digest_json(manifest);(path/c.MANIFEST).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(c.Error,'missing a pinned dependency'):c.verify_directory(path)

    def test_dynamic_import_uses_adjacent_modules_without_pythonpath(self):
        folder=self.base/'isolated';folder.mkdir()
        for name in ('context.py','registry.py','projects.py','translation.py'):
            shutil.copyfile(Path(c.__file__).with_name(name),folder/name)
        code="import importlib.util; s=importlib.util.spec_from_file_location('isolated_context',"+repr(str(folder/'context.py'))+"); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.r.COLLECTIONS['project'])"
        result=subprocess.check_output([sys.executable,'-I','-c',code],text=True)
        self.assertEqual(result.strip(),'projects')


if __name__=='__main__':unittest.main()
