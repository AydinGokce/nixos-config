"""Structure publication/association never invents identity or erases evidence."""
import base64
from copy import deepcopy
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from workbench.api import API
from workbench.common import Error, canonical, uid
from workbench import library_structures as structures
from workbench import test_library_edits as fixtures
from workbench.test_library_sequence import definition

PDB = b'ATOM      1  CA  ALA A   1       0.000   1.000   2.000  1.00 70.00           C  \nEND\n'
CIF = b'data_example\nloop_\n_atom_site.group_PDB\n_atom_site.Cartn_x\n_atom_site.Cartn_y\n_atom_site.Cartn_z\nATOM 0 1 2\n'


class ProteinStructuresTests(unittest.TestCase):
    setUp = fixtures.LibraryCurationTests.setUp
    protein = fixtures.LibraryCurationTests.protein
    project = fixtures.LibraryCurationTests.project
    request = fixtures.LibraryCurationTests.request
    reverse = fixtures.LibraryCurationTests.reverse

    def upload(self, data=PDB, name='input.pdb', api=None):
        api = api or self.api
        digest = hashlib.sha256(data).hexdigest()
        result = api.call('upload.begin', {'name': name, 'size': len(data), 'sha256': digest})
        api.call('upload.chunk', {'upload_id': result['upload_id'], 'offset': 0,
            'data_base64': base64.b64encode(data).decode()})
        api.call('upload.finish', {'upload_id': result['upload_id'], 'sha256': digest})
        return {'source': {'kind': 'upload', 'id': result['upload_id'], 'sha256': digest}}

    def attach(self, ref='editor', items=None, api=None, key=None):
        record = self.registry.show(ref); self.counter += 1
        params = {'ref': self.module.reference(record), 'expected_sha256': record['sha256'],
                  'structures': items if items is not None else [self.upload(api=api)],
                  'request_key': key or 'attach-' + str(self.counter)}
        return (api or self.api).call('library.structure_attach', params), params

    def listing(self, ref='editor', api=None, **kwargs):
        return (api or self.api).call('library.structures', {'ref': ref, **kwargs})

    def hide(self, ident, hidden=True, ref='editor', api=None):
        record = self.registry.show(ref); self.counter += 1
        return (api or self.api).call('library.structure_visibility', {
            'ref': self.module.reference(record), 'expected_sha256': record['sha256'],
            'entry_id': ident, 'hidden': hidden, 'request_key': 'visibility-' + str(self.counter)})

    def job(self, source='construct:editor@1', actor='harrison', extra=None, mode='batch',
            role='structure', raw=PDB, input_id='selected'):
        batch_id, job_id, artifact_id = uid(), uid(), uid()
        declaration = source if isinstance(source, dict) else {'kind': 'library', 'ref': source}
        batch = {'batch_id': batch_id, 'state': 'complete', '_request': {'mode': mode,
                 'inputs': [{'id': 'selected', 'source': declaration}, *(extra or [])]}}
        job = {'job_id': job_id, 'batch_id': batch_id, 'state': 'complete', 'model': 'rf3', 'input_id': input_id}
        artifact = {'artifact_id': artifact_id, 'job_id': job_id, 'name': 'model.pdb', 'format': 'pdb',
                    'role': role, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        folder = self.store.directory('artifacts', artifact_id); folder.mkdir(parents=True); (folder/'content').write_bytes(raw)
        with self.store.transaction() as db:
            for kind, value in [('batch', batch), ('job', job), ('artifact', artifact)]:
                self.store.put(db, kind, value, actor)
        return job_id, artifact_id, batch_id

    def test_manual_upload_bytes_are_shared_exact_and_do_not_change_molecular_identity(self):
        before = self.protein(); result, params = self.attach()
        current = self.registry.show(result['ref'])
        self.assertEqual(current['identity'], before['identity']); self.assertNotIn('structure_file', current['identity'])
        row = self.listing(api=self.bob)['entries'][0]
        self.assertEqual(row['source_ref'], self.module.reference(before))
        self.assertEqual(row['sequence_relation'], 'same_library_sequence')
        self.assertEqual(row['coordinate_sequence_match'], 'unverified')
        response = self.bob.call('library.structure_read', {'ref': result['ref'], 'entry_id': row['entry_id']})
        self.assertEqual(base64.b64decode(response['data_base64']), PDB)
        self.assertEqual(response['sha256'], hashlib.sha256(PDB).hexdigest())
        self.assertEqual(self.api.call('library.structure_attach', params), result)
        self.assertEqual(self.registry.show('editor'), current)

    def test_create_multiple_structures_is_atomic_and_replay_keeps_single_creation(self):
        self.protein(); project = self.project()
        params = {'project_ref': self.module.reference(project), 'expected_sha256': project['sha256'],
            'sequence': 'MAG', 'alt_name': 'New', 'request_key': 'create-structures',
            'structures': [self.upload(), self.upload(CIF, 'second.mmcif')]}
        result = self.api.call('library.create', params)
        self.assertEqual(len(self.listing(result['ref'])['entries']), 2)
        self.assertEqual(self.api.call('library.create', params), result)
        self.assertIn(result['ref'], [m['source_ref'] for m in self.registry.show('study')['identity']['members']])
        self.reverse(); self.assertTrue(self.registry.show(result['ref'].split('@')[0])['provenance']['workbench']['archived'])
        self.reverse('redo'); self.assertEqual(len(self.listing(result['ref'])['entries']), 2)

    def test_bad_second_upload_never_creates_partial_protein_or_project_revision(self):
        self.protein(); project = self.project(); before = self.registry.list()
        good = self.upload(); bad = self.upload(b'not a structure\n')
        with self.assertRaises(Error):
            self.api.call('library.create', {'project_ref': self.module.reference(project), 'expected_sha256': project['sha256'],
                'sequence': 'MAG', 'request_key': 'bad-create', 'structures': [good, bad]})
        self.assertEqual(self.registry.list(), before); self.assertEqual(self.registry.show('study'), project)

    def test_attach_hide_undo_redo_retains_all_original_bytes_and_cas(self):
        self.protein(); result, params = self.attach(); row = self.listing()['entries'][0]
        original = self.registry.show(result['ref']); receipt = next(a for a in original['attachments'] if a['path'].endswith('.pdb'))
        path = self.registry._path(result['ref']).parent/receipt['path']; original_bytes = path.read_bytes()
        self.hide(row['entry_id']); self.assertEqual(self.listing()['entries'], [])
        self.assertEqual(len(self.listing(include_hidden=True)['entries']), 1)
        self.reverse(); self.assertEqual(len(self.listing()['entries']), 1)
        self.reverse('redo'); self.assertEqual(self.listing()['entries'], [])
        self.assertEqual(path.read_bytes(), original_bytes)
        stale = {**params, 'request_key': 'stale'}
        with self.assertRaises(Error): self.api.call('library.structure_attach', stale)
        self.registry.verify()

    def test_undo_upload_keeps_assets_and_older_name_undo_redo_still_works(self):
        self.protein(); self.request({'alt_name': 'Named'})
        result, _ = self.attach(); row = self.listing()['entries'][0]
        self.reverse(); self.assertEqual(self.listing()['entries'], [])
        self.reverse(); self.assertEqual(self.registry.show('editor')['provenance']['inventory']['alt_orf_name'], 'Editor')
        self.reverse('redo'); self.reverse('redo')
        self.assertEqual(self.listing()['entries'][0]['entry_id'], row['entry_id'])
        original = self.registry.show(result['ref'])
        current = self.registry.show('editor')
        self.assertEqual(original['attachments'], current['attachments'])

    def test_other_actor_name_undo_preserves_structure_curation(self):
        self.protein(); self.request({'alt_name': 'Named'})
        self.attach(api=self.bob); self.reverse()
        self.assertEqual(len(self.listing()['entries']), 1)
        self.reverse('redo'); self.assertEqual(len(self.listing()['entries']), 1)

    def test_sequence_revisions_label_history_without_changing_association(self):
        self.protein(); self.attach(); original = self.listing()['entries'][0]
        self.request({'sequence': 'ACDEFGHIL'})
        row = self.listing()['entries'][0]
        self.assertEqual(row['entry_id'], original['entry_id'])
        self.assertEqual(row['source_ref'], original['source_ref'])
        self.assertEqual(row['sequence_relation'], 'historical_library_sequence')
        self.reverse(); self.assertEqual(self.listing()['entries'][0]['sequence_relation'], 'same_library_sequence')

    def test_parent_undo_preserves_later_hidden_derived_protein_assets(self):
        parent = self.registry.import_record({'kind': 'construct', 'id': 'plasmid',
            'identity': {'molecule_type': 'dna', 'sequence': 'ATGGCTGGGTAA'}})
        self.registry.import_record({'kind': 'construct', 'id': 'editor', 'identity': {'molecule_type': 'protein',
            'encoded_by': {'construct_ref': self.module.reference(parent), 'sequence_sha256': hashlib.sha256(b'ATGGCTGGGTAA').hexdigest(),
                           'translation': definition([{'start': 0, 'end': 12}])}}})
        self.request({'sequence': 'ATGGGTGGGTAA'}, 'plasmid')
        self.attach(); self.reverse(); self.reverse()
        self.assertEqual(self.api.call('library.sequence', {'ref': 'editor'})['sequence'], 'MAG')
        self.assertEqual(len(self.listing(include_hidden=True)['entries']), 1)
        self.reverse('redo'); self.reverse('redo'); self.assertEqual(len(self.listing()['entries']), 1)

    def test_shared_harrison_result_does_not_broaden_generic_artifact_or_job_access(self):
        self.protein(); jid, aid, _ = self.job()
        row = self.listing()['entries'][0]
        self.assertEqual(row['job_id'], jid); self.assertEqual(row['access_scope'], 'shared_library')
        with self.assertRaises(Error): self.api.call('artifact.read', {'artifact_id': aid})
        with self.assertRaises(Error): self.api.call('job.get', {'job_id': jid})
        result = self.api.call('library.structure_read', {'ref': 'editor', 'entry_id': row['entry_id']})
        self.assertEqual(base64.b64decode(result['data_base64']), PDB)

    def test_private_complex_partner_is_visible_only_to_original_actor(self):
        self.protein(); self.job(actor='bob', mode='assembly', input_id='assembly', extra=[{
            'id': 'private', 'source': {'kind': 'text', 'format': 'sequence', 'text': 'MPRIVATE'}}])
        self.assertEqual(self.listing()['entries'], [])
        row = self.listing(api=self.bob)['entries'][0]
        self.assertEqual(row['access_scope'], 'current_actor')
        with self.assertRaises(Error):
            self.api.call('library.structure_read', {'ref': 'editor', 'entry_id': row['entry_id']})

    def test_shared_explicit_assembly_and_distinct_batch_pairs(self):
        self.protein(); self.protein('other')
        self.registry.import_record({'kind': 'assembly', 'id': 'complex', 'identity': {'components': [
            {'chain_id': 'A', 'construct_ref': 'construct:editor@1'},
            {'chain_id': 'B', 'construct_ref': 'construct:other@1'}], 'bonds': []}})
        self.job('assembly:complex@1')
        self.assertEqual(len(self.listing()['entries']), 1)
        self.job(extra=[{'id': 'other', 'source': {'kind': 'library', 'ref': 'construct:other@1'}}], input_id='other')
        self.assertEqual(len(self.listing()['entries']), 1)
        self.assertEqual(len(self.listing('other')['entries']), 2)

    def test_no_floating_name_sequence_or_cross_actor_forgery_associations(self):
        self.protein(); self.protein('other')
        self.job('editor'); self.job('construct:other@1')
        self.job({'kind': 'text', 'format': 'sequence', 'text': 'ACDEFGHIK'})
        self.job(role='data'); self.job(role='target_structure')
        _, _, bid = self.job()
        with self.store.transaction() as db: db.execute("UPDATE objects SET actor='different' WHERE id=?", (bid,))
        self.assertEqual(self.listing()['entries'], [])

    def test_wrong_protein_family_read_and_visibility_are_rejected(self):
        self.protein(); self.protein('other'); self.job()
        row = self.listing()['entries'][0]
        with self.assertRaises(Error): self.api.call('library.structure_read', {'ref': 'other', 'entry_id': row['entry_id']})
        with self.assertRaises(Error): self.hide(row['entry_id'], ref='other')

    def test_prediction_hide_is_shared_and_does_not_delete_artifact(self):
        self.protein(); _, aid, _ = self.job(); row = self.listing()['entries'][0]
        path = self.store.directory('artifacts', aid)/'content'; before = path.read_bytes()
        self.hide(row['entry_id']); self.assertEqual(self.listing(api=self.bob)['entries'], [])
        self.reverse(); self.assertEqual(len(self.listing(api=self.bob)['entries']), 1)
        self.reverse('redo'); self.assertEqual(self.listing()['entries'], [])
        self.assertEqual(path.read_bytes(), before)

    def test_download_pagination_integrity_tamper_and_no_internal_paths(self):
        self.protein(); _, aid, _ = self.job(); row = self.listing()['entries'][0]
        self.assertNotIn('_path', row)
        parts=[]; offset=0
        while True:
            result=self.api.call('library.structure_read',{'ref':'editor','entry_id':row['entry_id'],'offset':offset,'length':11})
            parts.append(base64.b64decode(result['data_base64'])); offset=result['next_offset']
            if result['eof']:break
        self.assertEqual(b''.join(parts),PDB)
        (self.store.directory('artifacts',aid)/'content').write_bytes(b'x'*len(PDB))
        with self.assertRaises(Error):self.api.call('library.structure_read',{'ref':'editor','entry_id':row['entry_id']})

    def test_attachment_ownership_hash_format_limits_and_idempotent_parameter_binding(self):
        self.protein(); foreign=self.upload(api=self.bob)
        with self.assertRaises(Error):self.attach(items=[foreign])
        item=self.upload(); wrong=deepcopy(item);wrong['source']['sha256']='0'*64
        with self.assertRaises(Error):self.attach(items=[wrong])
        with patch.object(structures,'MAX_FILE',len(PDB)-1),self.assertRaises(Error):self.attach(items=[item])
        with self.assertRaises(Error):self.attach(items=[item]*17)
        result,params=self.attach(items=[item]); self.assertEqual(self.api.call('library.structure_attach',params),result)
        with self.assertRaises(Error):self.api.call('library.structure_attach',{**params,'structures':[{**item,'label':'changed'}]})

    def test_duplicate_assets_are_reused_and_saved_library_backup_is_sufficient(self):
        self.protein(); self.attach(); before=self.registry.show('editor'); self.attach()
        self.assertEqual(len(self.listing()['entries']),1)
        self.assertEqual(before['attachments'],self.registry.show('editor')['attachments'])
        archive=self.base/'backup.tar.gz'; self.registry.export_snapshot(archive)
        restored=self.base/'restored';self.module.restore_backup(archive,restored)
        api=API(self.store,'bob',library_config={**self.config,'library_root':str(restored)})
        row=self.listing(api=api)['entries'][0]
        self.assertEqual(base64.b64decode(api.call('library.structure_read',{'ref':'editor','entry_id':row['entry_id']})['data_base64']),PDB)

    def test_stable_order_cursor_and_same_job_duplicate_alias_deduplication(self):
        self.protein()
        for _ in range(4):self.job()
        first=self.listing(limit=2); second=self.listing(limit=2,cursor=first['next_cursor'])
        self.assertEqual(len({r['entry_id'] for r in first['entries']+second['entries']}),4)
        self.assertIsNone(second['next_cursor'])
        jid,aid,_=self.job();original=self.store.read('artifact',aid)
        alias={**original,'artifact_id':uid(),'name':'canonical.pdb'}
        with self.store.transaction() as db:self.store.put(db,'artifact',alias,'harrison')
        self.assertEqual(sum(r['job_id']==jid for r in self.listing()['entries']),1)

    def test_backlinks_bind_original_protein_and_parent_after_metadata_and_sequence_edits(self):
        parent=self.registry.import_record({'kind':'construct','id':'plasmid','identity':{
            'molecule_type':'dna','sequence':'ATGGCTGGGTAA','circular':True,'strand_count':2,'molecular_form':'plasmid'}})
        protein=self.registry.import_record({'kind':'construct','id':'editor','identity':{'molecule_type':'protein',
            'encoded_by':{'construct_ref':self.module.reference(parent),'sequence_sha256':hashlib.sha256(b'ATGGCTGGGTAA').hexdigest(),
                'translation':definition([{'start':0,'end':12}])}}})
        self.attach(); self.job('construct:editor@1')
        self.request({'sequence':'ATGGGTGGGTAA'},'plasmid')
        for row in self.listing()['entries']:
            self.assertEqual(row['protein']['ref'],'construct:editor@1')
            self.assertEqual(row['protein']['sha256'],protein['sha256'])
            self.assertEqual(row['protein']['sequence_sha256'],hashlib.sha256(b'MAG').hexdigest())
            self.assertEqual(row['protein']['derivation_kind'],'derived')
            self.assertEqual(row['protein']['parent'],{'ref':'construct:plasmid@1','sha256':parent['sha256'],
                'molecule_type':'dna','molecular_form':'plasmid'})
            self.assertEqual(row['sequence_relation'],'historical_library_sequence')
            read=self.api.call('library.structure_read',{'ref':self.listing()['ref'],'entry_id':row['entry_id']})
            self.assertEqual(read['protein'],row['protein'])

    def test_thumbnail_status_reauthorizes_real_association_before_renderer_lookup(self):
        from workbench import library_structure_thumbnails as thumbnails
        self.protein();self.protein('other');self.job()
        result=self.listing();row=result['entries'][0]
        with patch.object(thumbnails,'renderer_identity',side_effect=Error('unavailable','not installed')) as renderer:
            status=self.api.call('library.structure_thumbnail',{'ref':result['ref'],'entry_id':row['entry_id']})
            self.assertEqual(status['state'],'unavailable')
            self.assertEqual(status['source_sha256'],row['sha256'])
            self.assertEqual(renderer.call_count,1)
            with self.assertRaises(Error):
                self.api.call('library.structure_thumbnail',{'ref':'construct:other@1','entry_id':row['entry_id']})
            self.assertEqual(renderer.call_count,1)

    def test_independent_visibility_conflict_cannot_overwrite_other_actor(self):
        self.protein();self.attach();row=self.listing()['entries'][0]
        self.hide(row['entry_id']);self.hide(row['entry_id'],False,api=self.bob)
        with self.assertRaises(Error):self.reverse()
        self.assertEqual(len(self.listing()['entries']),1)

    def test_manual_asset_total_limit_and_unknown_fields_fail_before_publication(self):
        before=self.protein();item=self.upload()
        with patch.object(structures,'MAX_ASSETS',len(PDB)-1),self.assertRaises(Error):self.attach(items=[item])
        self.assertEqual(self.registry.show('editor'),before)
        with self.assertRaises(Error):self.attach(items=[{**item,'path':'/etc/passwd'}])
        self.assertEqual(self.registry.show('editor'),before)

    def test_run_opened_links_are_exact_owned_and_do_not_infer_sequence_matches(self):
        protein=self.protein();self.protein('other')
        _,aid,_=self.job(actor='alice')
        result=self.api.call('library.structure_links',{'artifact_id':aid})
        self.assertEqual(result['artifact_id'],aid)
        self.assertEqual(result['proteins'],[{'ref':'construct:editor@1','sha256':protein['sha256'],
            'sequence_sha256':hashlib.sha256(b'ACDEFGHIK').hexdigest(),'derivation_kind':'explicit','parent':None}])
        with self.assertRaises(Error):self.bob.call('library.structure_links',{'artifact_id':aid})
        _,unlinked,_=self.job({'kind':'text','format':'sequence','text':'ACDEFGHIK'},actor='alice')
        self.assertEqual(self.api.call('library.structure_links',{'artifact_id':unlinked})['proteins'],[])
        _,template,_=self.job(actor='alice',role='data')
        with self.assertRaises(Error):self.api.call('library.structure_links',{'artifact_id':template})

    def test_unicode_labels_and_automatic_filename_controls(self):
        self.protein();item=self.upload();label='α'*200
        self.attach(items=[{**item,'label':label}]);row=self.listing()['entries'][0]
        self.assertEqual(row['label'],label)
        self.assertEqual(structures._display_label('a\n\x7fb\u0085c.pdb'),'abc.pdb')
        with self.assertRaises(Error):self.attach(items=[{**item,'label':'a\u0085b'}])


class SavedBinderStructuresTests(unittest.TestCase):
    def saved(self):
        from workbench.test_binder_api import BinderTests
        fixture = BinderTests('runTest'); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        job = fixture.seal_fixture()
        module, registry, project = fixture.project()
        candidate = next(row for row in fixture.api.call('binder.candidates', {'job_id': job['job_id']})['candidates']
                         if row['status'] == 'accepted')
        saved = fixture.api.call('binder.save', {'job_id': job['job_id'], 'candidate_id': candidate['candidate_id'],
            'project_ref': module.reference(project), 'expected_sha256': project['sha256'], 'request_key': uid()})
        return fixture, module, registry, saved, candidate

    def test_native_save_already_associates_candidate_and_survives_restore_sequence_edits_and_visibility_undo(self):
        fixture, module, registry, saved, candidate = self.saved()
        original = registry.show(saved['ref']); params = {'ref': saved['ref'].split('@')[0]}
        row = fixture.bob.call('library.structures', params)['entries'][0]
        self.assertEqual(row['association'], 'saved_bindcraft_candidate')
        self.assertEqual(row['source_ref'], saved['ref']); self.assertEqual(row['protein']['sha256'], original['sha256'])
        self.assertEqual(row['sha256'], candidate['structure_artifacts'][0]['sha256'])
        self.assertEqual(row['label'], 'predicted-complex.pdb'); self.assertEqual(row['candidate_status'], 'accepted')
        with self.assertRaises(Error):fixture.bob.call('job.get', {'job_id': row['job_id']})
        before = fixture.bob.call('library.structure_read', {**params, 'entry_id': row['entry_id']})
        fixture.api.call('library.edit', {'ref': saved['ref'], 'expected_sha256': original['sha256'],
            'patch': {'sequence': 'AAAAG'}, 'request_key': uid()})
        newer = fixture.bob.call('library.structures', params)
        self.assertEqual(len(newer['entries']), 1)
        self.assertEqual(newer['entries'][0]['entry_id'], row['entry_id'])
        self.assertEqual(newer['entries'][0]['source_ref'], saved['ref'])
        self.assertEqual(newer['entries'][0]['sequence_relation'], 'historical_library_sequence')
        self.assertEqual(fixture.bob.call('library.structures', {**params, 'include_revisions': False})['entries'], [])
        fixture.bob.call('library.structure_visibility', {'ref': newer['ref'], 'expected_sha256': newer['sha256'],
            'entry_id': row['entry_id'], 'hidden': True, 'request_key': uid()})
        self.assertEqual(fixture.api.call('library.structures', params)['entries'], [])
        history = fixture.bob.call('library.history', {})
        fixture.bob.call('library.undo', {'operation_id': history['undo']['operation_id'], 'request_key': uid()})
        after = fixture.bob.call('library.structure_read', {**params, 'entry_id': row['entry_id']})
        self.assertEqual(after['data_base64'], before['data_base64'])
        archive = fixture.root/'backup.tar.gz'; registry.export_snapshot(archive)
        restored = fixture.root/'restored'; module.restore_backup(archive, restored)
        from workbench.store import Store
        fresh = API(Store(fixture.root/'empty-workbench'), 'new-team-member',
                    library_config={**fixture.config, 'library_root': str(restored)})
        cards = fresh.call('library.structures', params)['entries']
        self.assertEqual(len(cards), 1); self.assertEqual(cards[0]['entry_id'], row['entry_id'])
        self.assertEqual(fresh.call('library.structure_read', {**params, 'entry_id': row['entry_id']})['data_base64'], before['data_base64'])

    def test_saved_binder_requires_the_actual_candidate_provenance_not_just_a_named_attachment(self):
        fixture, module, registry, saved, _ = self.saved()
        record = registry.show(saved['ref'])
        document = {key: deepcopy(value) for key, value in record.items() if key in module.USER_FIELDS}
        document['id'] = 'copied-with-wrong-sequence'
        document['identity']['sequence'] = 'GGGGG'
        files = {Path(item['path']).name: registry._path(saved['ref']).parent/item['path'] for item in record['attachments']}
        unrelated = registry.import_record(document, files)
        self.assertEqual(fixture.api.call('library.structures', {'ref': module.reference(unrelated)})['entries'], [])
        row = fixture.api.call('library.structures', {'ref': saved['ref']})['entries'][0]
        path = registry._path(saved['ref']).parent/'attachments/bindcraft-provenance.json'
        raw = path.read_bytes(); path.chmod(0o600); path.write_bytes(raw.replace(b'"workflow":"bindcraft"', b'"workflow":"inventedx"'))
        with self.assertRaises(Error):fixture.api.call('library.structure_read', {'ref': saved['ref'], 'entry_id': row['entry_id']})


if __name__=='__main__':unittest.main()
