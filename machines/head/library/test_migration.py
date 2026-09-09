"""Lossless inventory migration, source evidence and guarded publication tests."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

import adapters
import context
import migration
import registry as r


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base/'source'
        self.source.mkdir()
        self.context = self.base/'context'
        self.context.mkdir()
        self.make_source()
        (self.context/'context.docx').write_bytes(b'original context source bytes')
        (self.context/'project.md').write_text('# Shared project\n\nExisting research objectives.\n')
        (self.context/'protein.md').write_text('# Intended protein purpose\n\nUnassessed source hypothesis.\n')
        (self.context/'match-manifest.json').write_text(json.dumps({
            'schema': 'bio-library-context-matches.v1', 'source_docx': {'sha256': sha(b'original context source bytes')},
            'project': {'id': migration.PROJECT, 'markdown_path': 'project.md'},
            'matches': [{'source_kind': 'protein_product', 'source_id': 'pGC001:primary',
                         'construct_identifier': 'pGC001', 'markdown_path': 'protein.md'}]}))
        self.frozen = self.base/'frozen'
        self.audit = migration.freeze(self.source/'constructs.sqlite', self.frozen)
        manifest_path = self.context/'match-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['source_snapshot'] = {'sha256': self.audit['snapshot']['sha256']}
        manifest['matches'][0]['markdown_sha256'] = sha((self.context/'protein.md').read_bytes())
        manifest_path.write_text(json.dumps(manifest))

    def tearDown(self):
        self.temp.cleanup()

    def make_source(self):
        for name in ['README.md', 'schema.sql', 'protein_schema.sql', 'protein_product_manifest.json',
                     'validation.json', 'add_proteins.py', 'build_database.py']:
            (self.source/name).write_text('source '+name+'\n')
        metadata = [{'identifier': ident, 'verbose_name': 'Source '+ident, 'alt_orf_name': None,
                     'description': 'Original purpose', 'notes': 'Clone 9 chosen' if ident == 'pGC016' else None,
                     'glycerol_stock': None, 'plasmid_received_or_produced': 'NO', 'spreadsheet_fill_color': 'green'}
                    for ident in ('pGC001', 'pGC016')]
        seqs, features, translations, products, files = [], [], [], [], []
        for i, ident, role, cell, sequence in [(1, 'pGC001', 'primary', 'G2', 'ATGGCC'),
                                             (2, 'pGC016', 'primary', 'G17', 'ATGGCA'),
                                             (3, 'pGC016', 'alternate', 'F17', 'ATGGCT')]:
            raw = ('ORIGINAL '+ident+' '+cell+'\r\n').encode()
            file = 'sources/'+ident+'-'+cell+'.gb'
            files.append({'path': file, 'media_type': 'text/plain', 'sha256': sha(raw), 'content': raw})
            seqs.append({'id': i, 'construct_identifier': ident, 'role': role, 'source_cell': cell,
                         'source_file': file, 'topology': 'circular', 'molecule_type': 'ds-DNA',
                         'sequence': sequence, 'sequence_sha256': sha(sequence.encode()), 'length_bp': len(sequence),
                         'raw_genbank': raw.decode()})
            features.append({'id': i, 'sequence_id': i, 'feature_index': 0, 'type': 'CDS',
                             'location_parts_json': '[{"start":4,"end":6},{"start":0,"end":4}]',
                             'qualifiers_json': '{"translation":["MA*"]}', 'source_location': 'join(5..6,1..4)'})
            translations.append({'feature_id': i, 'sequence_id': i, 'computed_translation': 'MA*',
                                 'internal_stop_positions_json': '[]', 'incomplete_codon_bases': ''})
            products.append({'product_id': ident+':'+role, 'construct_identifier': ident, 'sequence_id': i,
                             'sequence_role': role, 'anchor_feature_id': i, 'candidate_sequence': 'MA',
                             'protein_sha256': sha(b'MA'), 'length_aa': 2, 'method_version': 'protein-audit-1.0',
                             'validation_status': 'reference_matched' if i == 1 else 'review_required'})
        files.append({'path': 'sources/inventory.xlsx', 'media_type': 'application/zip', 'sha256': sha(b'original workbook'), 'content': b'original workbook'})
        provenance = [{'key': key, 'value': sha((self.source/name).read_bytes())} for name, key in
                      [('protein_product_manifest.json', 'protein_manifest_sha256'),
                       ('add_proteins.py', 'protein_import_script_sha256'), ('protein_schema.sql', 'protein_schema_sha256')]]
        rows = {'construct_metadata': metadata, 'sequences': seqs, 'sequence_features': features,
                'protein_translations': translations, 'protein_products': products, 'source_files': files,
                'provenance': provenance,
                'protein_product_evidence': [{'product_id': item['product_id'], 'feature_id': item['anchor_feature_id'], 'relationship': 'whole_reference'} for item in products],
                'import_warnings': [{'id': 1, 'sequence_id': 3, 'message': 'Preserve source warning'}],
                'protein_review_items': [{'id': 1, 'sequence_id': 3, 'detail': 'Candidate unresolved'}],
                'protein_variant_checks': [{'construct_identifier': 'pGC016', 'expected_substitution': 'source only'}],
                'spreadsheet_cells': [{'cell': 'J2', 'construct_identifier': 'pGC001', 'value': None,
                                       'formula': '=J1', 'hyperlink': 'https://source.invalid/x', 'fill_color': 'green'}],
                'spreadsheet_columns': [{'column_letter': 'J', 'original_header': 'Original status'}],
                'spreadsheet_legend': [{'fill_color': 'green', 'meaning': 'Received'}]}
        db = sqlite3.connect(self.source/'constructs.sqlite')
        for table, items in rows.items():
            columns = list(items[0])
            db.execute('CREATE TABLE '+table+' ('+', '.join('"'+key+'" '+('BLOB' if isinstance(items[0][key], bytes) else 'INTEGER' if type(items[0][key]) is int else 'TEXT') for key in columns)+')')
            db.executemany('INSERT INTO '+table+' VALUES('+','.join('?' for _ in columns)+')',
                           [[item[key] for key in columns] for item in items])
        db.execute('PRAGMA user_version=2')
        db.commit()
        db.close()
        self.source_rows = rows

    def stage(self, name='stage'):
        destination = self.base/name
        result = migration.stage(self.source, self.frozen, self.context, destination)
        return destination, result, r.Registry(destination/'registry')

    def test_stages_exact_sequences_clones_cells_and_review_without_source_mutation(self):
        before = (self.source/'constructs.sqlite').read_bytes()
        path, result, library = self.stage()
        self.assertEqual((self.frozen/'constructs-original.sqlite').read_bytes(), before)
        self.assertEqual((self.source/'constructs.sqlite').read_bytes(), before)
        self.assertEqual(len(result['records']), 7)
        self.assertEqual(result['context_matched_records'], 1)
        self.assertEqual(library.resolve('pGC001'), 'construct:pgc001-plasmid@1')
        with self.assertRaises(r.Error):
            library.resolve('pGC016')
        clone = library.show('pgc016-protein-f17')
        self.assertEqual(clone['identity']['encoded_by']['construct_ref'], 'construct:pgc016-plasmid-f17@1')
        self.assertEqual(clone['identity']['product_review']['status'], 'review_required')
        inventory = r.load_json(library.attachment_path('pGC001', 'attachments/inventory.json'))
        self.assertEqual(inventory['metadata'], self.source_rows['construct_metadata'][0])
        self.assertEqual(inventory['cells'], self.source_rows['spreadsheet_cells'])
        annotation = r.load_json(library.attachment_path('pGC001', 'attachments/annotations.json'))
        self.assertEqual(annotation['sequence_features'], [self.source_rows['sequence_features'][0]])
        self.assertEqual(annotation['protein_translations'], [self.source_rows['protein_translations'][0]])
        self.assertEqual(library.attachment_path('pgc001-protein', 'attachments/description.md').read_bytes(),
                         (self.context/'protein.md').read_bytes())
        self.assertEqual(r.projects.markdown_state(library.attachment_path('pGC001', 'attachments/description.md').read_bytes()),
                         'incomplete_scaffold')
        self.assertEqual(len(library.show(migration.PROJECT)['identity']['members']), 6)
        self.assertEqual(migration.verify_stage(path), result)

    def test_prediction_gate_and_encoded_source_validation(self):
        path, _, library = self.stage()
        adapters.compile_input(path/'registry', 'pgc001-protein', 'esm', self.base/'ready', plain_fasta=True)
        with self.assertRaisesRegex(ValueError, 'requires review'):
            adapters.compile_input(path/'registry', 'pgc016-protein-f17', 'esm', self.base/'unresolved', plain_fasta=True)
        with self.assertRaisesRegex(ValueError, 'double-stranded plasmids'):
            adapters.compile_input(path/'registry', 'pgc001-plasmid', 'esm', self.base/'plasmid', plain_fasta=True)
        with self.assertRaisesRegex(r.Error, 'exact source'):
            library.import_record({'kind': 'construct', 'id': 'wrong-source', 'identity': {
                'molecule_type': 'protein', 'sequence': 'MA', 'encoded_by': {
                    'construct_ref': 'pGC001', 'sequence_sha256': '0'*64}}})
        with self.assertRaisesRegex(r.Error, 'plasmid requires'):
            library.import_record({'kind': 'construct', 'id': 'invalid-plasmid',
                'identity': {'molecule_type': 'dna', 'sequence': 'ATGC', 'molecular_form': 'plasmid'}})

    def test_backup_restore_and_context_retain_source_dependency_archives(self):
        path, result, library = self.stage()
        destination = self.base/'restored'
        r.restore_backup(path/'library.tar.gz', destination)
        restored = r.Registry(destination)
        self.assertEqual(restored.verify()['records'], 7)
        self.assertEqual(restored.attachment_path(migration.PROJECT, 'attachments/constructs-original.sqlite').read_bytes(),
                         (self.source/'constructs.sqlite').read_bytes())
        # A project containing only a protein must still bring its encoded DNA.
        brief = self.base/'brief.md'
        brief.write_text('# Protein evaluation\n')
        library.import_record(r.projects.project_document('protein-only', members=['pgc001-protein']), {'project.md': brief})
        snapshot = library.project_snapshot('protein-only')
        closure = context._closure(library, snapshot)
        self.assertEqual(set(closure), {'project:protein-only@1', 'construct:pgc001-protein@1', 'construct:pgc001-plasmid@1'})
        with tarfile.open(restored.attachment_path(migration.PROJECT, 'attachments/source-files.tar.gz')) as tar:
            self.assertEqual(tar.extractfile('sources/inventory.xlsx').read(), b'original workbook')

    def test_publication_is_guarded_idempotent_and_refuses_unrelated_records(self):
        path, result, library = self.stage()
        root = self.base/'production'
        live = r.Registry(root)
        live.init()
        with self.assertRaisesRegex(r.Error, 'fingerprint mismatch'):
            migration.publish(path, root, '0'*64)
        first = migration.publish(path, root, result['migration_fingerprint'])
        self.assertFalse(first['already_published'])
        self.assertTrue(migration.publish(path, root, result['migration_fingerprint'])['already_published'])
        self.assertEqual(live.verify()['records'], 7)
        live.import_record({'kind': 'construct', 'id': 'unrelated', 'identity': {'molecule_type': 'protein', 'sequence': 'MA'}})
        with self.assertRaisesRegex(r.Error, 'existing or changed records'):
            migration.publish(path, root, result['migration_fingerprint'])

    def test_archive_tampering_and_frozen_source_changes_fail_closed(self):
        path, result, library = self.stage()
        target = library.attachment_path('pgc001-plasmid', 'attachments/source.gb')
        target.write_bytes(b'changed source')
        with self.assertRaisesRegex(r.Error, 'integrity failure'):
            migration.verify_stage(path)
        (self.frozen/'constructs-original.sqlite').write_bytes(b'changed original')
        with self.assertRaisesRegex(r.Error, 'Frozen source bytes changed'):
            self.stage('second')

    def test_same_frozen_inputs_produce_same_records_and_backup(self):
        _, first, _ = self.stage('one')
        _, second, _ = self.stage('two')
        self.assertEqual(first['records'], second['records'])
        self.assertEqual(first['migration_fingerprint'], second['migration_fingerprint'])
        # Core backup gzip has a wallclock header; compare bound manifest/files.
        self.assertEqual(first['backup']['manifest_sha256'], second['backup']['manifest_sha256'])

    def test_context_snapshot_and_markdown_hashes_must_match(self):
        manifest_path = self.context/'match-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['source_snapshot']['sha256'] = '0'*64
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(r.Error, 'different source database'):
            self.stage()
        manifest['source_snapshot']['sha256'] = self.audit['snapshot']['sha256']
        manifest_path.write_text(json.dumps(manifest))
        (self.context/'protein.md').write_text('# Unreviewed changed description')
        with self.assertRaisesRegex(r.Error, 'description SHA mismatch'):
            self.stage()


if __name__ == '__main__':
    unittest.main()
