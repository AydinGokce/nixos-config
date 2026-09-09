#!/usr/bin/env python3
"""Lossless, inspectable migration of the existing constructs SQLite inventory.

No sequence derivation, native parsers, network requests or prediction launches.
Stage into a private registry, verify it, then explicitly publish to an empty
head library. A stable publication guard and atomic exchange prevent partial
libraries from becoming visible. Nonempty unrelated destinations are refused.
"""
from __future__ import annotations

import argparse
import copy
import ctypes
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile

import registry as r

SCHEMA = 'bio-library-construct-migration.v1'
PROJECT = 'gcc-germline-engineering'
TABLES = ('construct_metadata', 'sequences', 'sequence_features', 'import_warnings',
          'protein_translations', 'protein_products', 'protein_product_evidence',
          'protein_review_items', 'protein_variant_checks', 'spreadsheet_cells',
          'spreadsheet_columns', 'spreadsheet_legend', 'source_files', 'provenance')


def _rows(db, table, where='', values=()):
    r.require(table in TABLES, 'Unrecognized source table')
    return [dict(row) for row in db.execute('SELECT * FROM '+table+where, values)]


def _raw(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)+'\n').encode()


def _put(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as handle:
        os.chmod(path, 0o600)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _receipt(path):
    return {'bytes': Path(path).stat().st_size, 'sha256': r.file_digest(path)}


def _logical_digest(db):
    digest = hashlib.sha256()
    for table in sorted(TABLES):
        rows = []
        for row in _rows(db, table):
            rows.append({key: {'blob_bytes': len(value), 'blob_sha256': hashlib.sha256(value).hexdigest()}
                         if isinstance(value, bytes) else value for key, value in row.items()})
        digest.update(r.json_bytes({'table': table, 'rows': sorted(rows, key=r.json_bytes)}))
    return digest.hexdigest()


def _database(path):
    r.no_symlinks(path, regular=True)
    db = sqlite3.connect(Path(path).absolute().as_uri()+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    r.require(db.execute('PRAGMA user_version').fetchone()[0] == 2, 'Expected source schema version 2')
    r.require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'Source SQLite integrity failure')
    r.require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'Source foreign-key failure')
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    r.require(names == set(TABLES), 'Source schema changed; explicit mapping review required')
    return db


def freeze(source, destination):
    """Retain stable exact main-file bytes and a transactionally consistent backup."""
    source, destination = Path(source).absolute(), Path(destination).absolute()
    r.no_symlinks(source, regular=True)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    db = _database(source)
    try:
        before = _receipt(source)
        _put(destination/'constructs-original.sqlite', source.read_bytes())
        r.require(before == _receipt(source) == _receipt(destination/'constructs-original.sqlite'),
                  'Source SQLite main file changed during capture; retry with a new staging directory')
        target = sqlite3.connect(destination/'constructs-snapshot.sqlite')
        try:
            db.backup(target)
        finally:
            target.close()
        snapshot = _database(destination/'constructs-snapshot.sqlite')
        try:
            logical = _logical_digest(snapshot)
            wal_present = Path(str(source)+'-wal').exists()
            if not wal_present:
                original = _database(destination/'constructs-original.sqlite')
                try:
                    r.require(_logical_digest(original) == logical, 'Original database and snapshot differ logically')
                finally:
                    original.close()
            r.require(before == _receipt(source), 'Source SQLite main file changed before capture completed')
            result = {'schema': SCHEMA, 'source_path': str(source), 'captured_at': r.now(),
                      'original_main_file': before, 'snapshot': _receipt(destination/'constructs-snapshot.sqlite'),
                      'snapshot_logical_sha256': logical,
                      'source_wal_present': wal_present,
                      'counts': {table: snapshot.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in TABLES},
                      'note': 'Original main-file bytes are provenance; use the consistent SQLite backup for queries. No source file was modified.'}
        finally:
            snapshot.close()
        result['sha256'] = r.digest_json(result)
        _put(destination/'source-audit.json', _raw(result))
        return result
    finally:
        db.close()


def _archive(path, files):
    """Deterministic, regular-files-only provenance archive."""
    with Path(path).open('xb') as output:
        os.chmod(path, 0o600)
        with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode='w', format=tarfile.PAX_FORMAT) as tar:
                for name, raw in sorted(files.items()):
                    r.safe_relative(name)
                    entry = tarfile.TarInfo(name)
                    entry.mode, entry.mtime, entry.size = 0o600, 0, len(raw)
                    tar.addfile(entry, io.BytesIO(raw))
        output.flush()
        os.fsync(output.fileno())


def _verify_archive(path, expected):
    observed = {}
    with tarfile.open(path, 'r:gz') as archive:
        for entry in archive:
            r.safe_relative(entry.name)
            r.require(entry.isfile() and entry.name not in observed and entry.name in expected,
                      'Unexpected or duplicate provenance archive entry')
            with archive.extractfile(entry) as source:
                digest, size = hashlib.sha256(), 0
                for chunk in iter(lambda: source.read(1024*1024), b''):
                    size += len(chunk)
                    digest.update(chunk)
            r.require(size == entry.size, 'Truncated provenance archive entry')
            observed[entry.name] = digest.hexdigest()
    r.require(observed == expected, 'Original source/context archive differs from its fingerprint')


def _source_rows(snapshot):
    db = _database(snapshot)
    try:
        tables = {table: _rows(db, table) for table in TABLES}
        tables['logical_sha256'] = _logical_digest(db)
        tables['snapshot_sha256'] = r.file_digest(snapshot)
    finally:
        db.close()
    for item in tables['source_files']:
        r.safe_relative(item['path'])
        r.require(hashlib.sha256(item['content']).hexdigest() == item['sha256'], 'Stored source-file hash mismatch')
    for table, sequence, digest, length in [('sequences', 'sequence', 'sequence_sha256', 'length_bp'),
                                            ('protein_products', 'candidate_sequence', 'protein_sha256', 'length_aa')]:
        for row in tables[table]:
            r.require(hashlib.sha256(row[sequence].encode()).hexdigest() == row[digest] and
                      len(row[sequence]) == row[length], f'{table} exact sequence/length/hash mismatch')
    for row in tables['sequences']:
        r.require(row['topology'] == 'circular' and row['molecule_type'] == 'ds-DNA',
                  'Source plasmid topology changed; explicit mapping review required')
    return tables


def _context(path, tables):
    path = Path(path).absolute()
    r.no_symlinks(path)
    manifest = r.load_json(path/'match-manifest.json')
    r.require(manifest.get('schema') == 'bio-library-context-matches.v1' and
              manifest.get('project', {}).get('id') == PROJECT, 'Unexpected context mapping schema/project')
    r.require(manifest.get('source_snapshot', {}).get('sha256') == tables['snapshot_sha256'],
              'Context mappings refer to a different source database snapshot')
    known = {'sequence': {row['id']: row for row in tables['sequences']},
             'protein_product': {row['product_id']: row for row in tables['protein_products']}}
    descriptions, files = {}, {}
    for source in sorted(path.rglob('*')):
        r.no_symlinks(source)
        if source.is_dir():
            continue
        r.no_symlinks(source, regular=True)
        r.require(source.stat().st_size <= 64*1024**2, 'Context source exceeds archive file limit')
        files[source.relative_to(path).as_posix()] = source.read_bytes()
    for match in manifest['matches']:
        key = (match['source_kind'], match['source_id'])
        r.require(key[0] in known and key[1] in known[key[0]] and key not in descriptions,
                  'Unknown or duplicate context source mapping')
        row = known[key[0]][key[1]]
        r.require(match['construct_identifier'] == row['construct_identifier'], 'Context source inventory identity mismatch')
        name = str(r.safe_relative(match['markdown_path']))
        r.require(name in files, 'Context description file missing')
        if 'markdown_sha256' in match:
            r.require(hashlib.sha256(files[name]).hexdigest() == match['markdown_sha256'], 'Context description SHA mismatch')
        r.projects.markdown_bytes(files[name], require=r.require)
        if 'source_review_status' in match:
            product = row if key[0] == 'protein_product' else next((item for item in tables['protein_products'] if item['sequence_id'] == key[1]), None)
            r.require(product is not None and product['validation_status'] == match['source_review_status'],
                      'Context source review status differs from the audited database')
        descriptions[key] = files[name]
    project_name = str(r.safe_relative(manifest['project']['markdown_path']))
    r.require(project_name in files, 'Context project Markdown missing')
    r.projects.markdown_bytes(files[project_name], require=r.require)
    expected_docx = manifest.get('source_docx', {}).get('sha256')
    r.require(expected_docx and any(hashlib.sha256(raw).hexdigest() == expected_docx for raw in files.values()),
              'Original context DOCX with declared SHA is missing')
    return manifest, descriptions, files, files[project_name]


def _identifier(row, molecule, counts):
    identifier = row['construct_identifier'].lower()+'-'+molecule
    if counts[row['construct_identifier']] > 1:
        identifier += '-'+row['source_cell'].lower()
    r.require(r.ID.fullmatch(identifier), 'Source requires a reviewed stable ID mapping')
    return identifier


def _inventory_description(metadata, kind, row, encoded=None):
    role = 'circular double-stranded DNA plasmid' if kind == 'sequence' else 'existing protein product candidate'
    lines = [r.projects.INCOMPLETE_MARKER, '', f'# {metadata["identifier"]}: {role}', '',
             '## Inventory description', '', metadata['description'] or 'Not specified in the source inventory.', '',
             '## Source notes', '', metadata['notes'] or 'No source notes.', '',
             '## Identity and evidence', '',
             'Imported without sequence alteration from the existing construct inventory. Inventory descriptions express intended use, not demonstrated function.']
    if encoded:
        lines.extend(['', f'Encoded by `{encoded}`. Source product review status: `{row["validation_status"]}`.',
                      'Reference matching is annotation evidence; it is not experimental confirmation. Review-required candidates remain excluded from prediction.'])
    if metadata['identifier'] == 'pGC016':
        lines.extend(['', 'The inventory links two sibling sequences. G17 is the primary spreadsheet sequence link; F17 is the alternate Clone 9 link. The note that Clone 9 was chosen is preserved without equating link role with physical-stock identity.'])
    lines.extend(['', '## Research objectives and acceptance criteria', '',
                  'Not yet populated from a matching project-context description. Complete these manually; no thresholds or functional conclusions were invented.', ''])
    return '\n'.join(lines).encode()


def _construct_doc(identifier, metadata, identity, provenance, aliases, tags):
    display = metadata['alt_orf_name'] or metadata['verbose_name'] or metadata['identifier']
    form = 'plasmid' if identity['molecule_type'] == 'dna' else 'protein product'
    return {'kind': 'construct', 'id': identifier, 'name': f'{metadata["identifier"]} — {display} ({form})',
            'aliases': aliases, 'tags': sorted(set(['construct-inventory', PROJECT, form.replace(' ', '-'), *tags])),
            'status': 'defined', 'notes': metadata['notes'] or '', 'identity': identity, 'provenance': provenance}


def _write_record(registry, scratch, document, attachments):
    sources = {name: _put(scratch/document['id']/name, raw) for name, raw in attachments.items()}
    return registry.import_record(document, sources)


def stage(source_directory, frozen, context_directory, destination):
    source_directory, frozen, destination = map(lambda value: Path(value).absolute(),
                                                  (source_directory, frozen, destination))
    r.no_symlinks(destination)
    r.require(not destination.exists(), 'Stage destination exists; use verify-stage on the existing immutable result')
    audit = r.verify_document(r.load_json(frozen/'source-audit.json'))
    r.require(_receipt(frozen/'constructs-snapshot.sqlite') == audit['snapshot'] and
              _receipt(frozen/'constructs-original.sqlite') == audit['original_main_file'], 'Frozen source bytes changed')
    tables = _source_rows(frozen/'constructs-snapshot.sqlite')
    r.require(tables['logical_sha256'] == audit['snapshot_logical_sha256'], 'Frozen logical source digest changed')
    if not audit['source_wal_present']:
        original = _database(frozen/'constructs-original.sqlite')
        try:
            r.require(_logical_digest(original) == tables['logical_sha256'], 'Frozen original and snapshot differ logically')
        finally:
            original.close()
    context_manifest, descriptions, context_files, project_markdown = _context(context_directory, tables)
    source_files = {row['path']: row['content'] for row in tables['source_files']}
    supplemental_names = ['README.md', 'schema.sql', 'protein_schema.sql', 'protein_product_manifest.json',
                          'validation.json', 'add_proteins.py', 'build_database.py']
    supplemental_names += [p.relative_to(source_directory).as_posix()
                           for p in sorted((source_directory/'protein_reports').glob('*')) if p.is_file()]
    for name in supplemental_names:
        r.no_symlinks(source_directory/name, regular=True)
        source_files[name] = (source_directory/name).read_bytes()
    provenance = {row['key']: row['value'] for row in tables['provenance']}
    for file, key in [('protein_product_manifest.json', 'protein_manifest_sha256'),
                      ('add_proteins.py', 'protein_import_script_sha256'), ('protein_schema.sql', 'protein_schema_sha256')]:
        r.require(hashlib.sha256(source_files[file]).hexdigest() == provenance[key], 'Supplemental source audit hash changed: '+file)
    fingerprint_data = {'schema': SCHEMA, 'project_id': PROJECT, 'source_audit_sha256': audit['sha256'],
                        'context_files': {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(context_files.items())},
                        'source_files': {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(source_files.items())},
                        'engine_sha256': r.file_digest(__file__)}
    fingerprint = r.digest_json(fingerprint_data)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.'+destination.name+'-', dir=destination.parent))
    original_now = r.now
    # One captured transaction timestamp makes record bytes and their digests
    # deterministic when staging the same frozen source and context again.
    r.now = lambda: audit['captured_at']
    try:
        library = r.Registry(temporary/'registry')
        library.init()
        scratch = temporary/'scratch'
        metadata = {row['identifier']: row for row in tables['construct_metadata']}
        sequences = {row['id']: row for row in tables['sequences']}
        counts = {identifier: sum(row['construct_identifier'] == identifier for row in sequences.values()) for identifier in metadata}
        r.require(all(counts.values()), 'Inventory entry without a sequence needs an explicit mapping')
        mapping, source_refs, records = [], {}, []
        for row in sorted(sequences.values(), key=lambda item: (item['construct_identifier'], item['source_cell'])):
            inventory = metadata[row['construct_identifier']]
            identifier = _identifier(row, 'plasmid', counts)
            seq_id = row['id']
            originals = {item['path']: item for item in tables['source_files']}
            original = originals[row['source_file']]['content']
            r.require(original.decode('utf-8') == row['raw_genbank'], 'Original GenBank bytes/text differ')
            inventory_evidence = {'metadata': inventory, 'cells': [item for item in tables['spreadsheet_cells'] if item['construct_identifier'] == inventory['identifier']],
                                  'columns': tables['spreadsheet_columns'], 'legend': tables['spreadsheet_legend']}
            annotation_evidence = {table: [item for item in tables[table] if item['sequence_id'] == seq_id]
                                   for table in ['sequence_features', 'import_warnings', 'protein_translations', 'protein_review_items']}
            annotation_evidence.update(coordinate_convention='Source zero-based end-exclusive parsed coordinates and ordered parts retained without flattening',
                                       protein_variant_checks=[item for item in tables['protein_variant_checks'] if item['construct_identifier'] == inventory['identifier']])
            source_provenance = {'migration_fingerprint': fingerprint, 'source_snapshot_sha256': audit['snapshot']['sha256'],
                                 'source_inventory_identifier': inventory['identifier'], 'source_sequence_id': seq_id,
                                 'source_cell': row['source_cell'], 'source_sequence_role': row['role'],
                                 'inventory': inventory, 'source_archive_project_id': PROJECT,
                                 'physical_clone_identity': 'unresolved link-role versus physical-stock assignment' if counts[inventory['identifier']] > 1 else 'not inferred from inventory status'}
            aliases = [inventory['identifier']] if counts[inventory['identifier']] == 1 else [inventory['identifier']+'-'+row['source_cell']]
            document = _construct_doc(identifier, inventory, {'molecule_type': 'dna', 'sequence': row['sequence'],
                                       'circular': True, 'strand_count': 2, 'molecular_form': 'plasmid'},
                                      source_provenance, aliases, [row['role']])
            description = descriptions.get(('sequence', seq_id), _inventory_description(inventory, 'sequence', row))
            record = _write_record(library, scratch, document,
                      {'source.gb': original, 'inventory.json': _raw(inventory_evidence), 'sequence.json': _raw(row),
                       'annotations.json': _raw(annotation_evidence), 'description.md': description})
            ref = r.reference(record)
            records.append(record)
            source_refs[seq_id] = ref
            mapping.append({'source_kind': 'sequence', 'source_id': seq_id, 'construct_identifier': inventory['identifier'],
                            'ref': ref, 'sequence_sha256': row['sequence_sha256'], 'source_role': row['role'],
                            'source_cell': row['source_cell'], 'context_matched': ('sequence', seq_id) in descriptions})
        for row in sorted(tables['protein_products'], key=lambda item: item['product_id']):
            inventory, sequence = metadata[row['construct_identifier']], sequences[row['sequence_id']]
            r.require(sequence['construct_identifier'] == row['construct_identifier'] and sequence['role'] == row['sequence_role'],
                      'Source protein-to-plasmid relationship mismatch')
            identifier = _identifier(sequence, 'protein', counts)
            ref = source_refs[row['sequence_id']]
            identity = {'molecule_type': 'protein', 'sequence': row['candidate_sequence'],
                        'encoded_by': {'construct_ref': ref, 'sequence_sha256': sequence['sequence_sha256']},
                        'product_review': {'status': row['validation_status'], 'source': 'constructs.sqlite/protein_products',
                                           'source_product_id': row['product_id'], 'method_version': row['method_version']}}
            evidence = [item for item in tables['protein_product_evidence'] if item['product_id'] == row['product_id']]
            feature_ids = {row['anchor_feature_id'], *[item['feature_id'] for item in evidence]}
            derivation = {'source_product': row, 'encoded_by': identity['encoded_by'], 'evidence': evidence,
                          'features': [item for item in tables['sequence_features'] if item['id'] in feature_ids],
                          'translations': [item for item in tables['protein_translations'] if item['feature_id'] in feature_ids],
                          'review_items': [item for item in tables['protein_review_items'] if item['sequence_id'] == row['sequence_id']],
                          'migration_policy': 'Existing candidate sequence copied exactly; no translation, tag removal, repair or new protein derivation.'}
            provenance_record = {'migration_fingerprint': fingerprint, 'source_snapshot_sha256': audit['snapshot']['sha256'],
                                 'source_inventory_identifier': inventory['identifier'], 'source_product_id': row['product_id'],
                                 'source_sequence_id': row['sequence_id'], 'source_sequence_role': row['sequence_role'],
                                 'inventory': inventory, 'source_archive_project_id': PROJECT}
            document = _construct_doc(identifier, inventory, identity, provenance_record, [], [row['validation_status'], row['sequence_role']])
            description = descriptions.get(('protein_product', row['product_id']), _inventory_description(inventory, 'protein_product', row, ref))
            record = _write_record(library, scratch, document,
                                  {'derivation.json': _raw(derivation), 'description.md': description,
                                   'candidate.fasta': ('>'+row['product_id']+'\n'+row['candidate_sequence']+'\n').encode()})
            records.append(record)
            mapping.append({'source_kind': 'protein_product', 'source_id': row['product_id'], 'construct_identifier': inventory['identifier'],
                            'ref': r.reference(record), 'sequence_sha256': row['protein_sha256'], 'source_role': row['sequence_role'],
                            'encoded_by': ref, 'product_review': row['validation_status'],
                            'context_matched': ('protein_product', row['product_id']) in descriptions})
        sources_path = scratch/'sources.tar.gz'
        _archive(sources_path, source_files)
        context_path = scratch/'context.tar.gz'
        _archive(context_path, context_files)
        archive_manifest = {'schema': SCHEMA, 'migration_fingerprint': fingerprint, 'fingerprint_inputs': fingerprint_data,
                            'source_audit': audit, 'context_mapping': context_manifest, 'mapping': mapping,
                            'policy': 'All existing candidate sequences and review statuses retained; no scientific conclusions or new sequence derivation.',
                            'source_archive': _receipt(sources_path), 'context_archive': _receipt(context_path)}
        archive_manifest['sha256'] = r.digest_json(archive_manifest)
        attachments = {'project.md': _put(scratch/'project.md', project_markdown),
                       'constructs-original.sqlite': frozen/'constructs-original.sqlite',
                       'constructs-snapshot.sqlite': frozen/'constructs-snapshot.sqlite',
                       'source-audit.json': frozen/'source-audit.json',
                       'migration-manifest.json': _put(scratch/'migration-manifest.json', _raw(archive_manifest)),
                       'source-files.tar.gz': sources_path, 'research-context.tar.gz': context_path,
                       'inventory.xlsx': _put(scratch/'inventory.xlsx', source_files['sources/inventory.xlsx']),
                       'context-matches.json': _put(scratch/'context-matches.json', _raw(context_manifest))}
        direct_context = {'MATCH-REVIEW.md': 'context-review.md', 'VALIDATION.json': 'context-validation.json',
                          'original-text.json': 'context-original-text.json', 'original-text.txt': 'context-original-text.txt'}
        docx_name = context_manifest['source_docx'].get('attachment_path')
        if docx_name:
            r.require(len(r.safe_relative(docx_name).parts) == 1, 'Context DOCX attachment requires a plain filename')
            direct_context[docx_name] = docx_name
        for source_name, attachment_name in direct_context.items():
            if source_name in context_files:
                attachments[attachment_name] = _put(scratch/'project-context'/attachment_name, context_files[source_name])
        project = r.projects.project_document(PROJECT, 'General Cybernetics Germline Engineering',
                       members=[{'source_ref': r.reference(record), 'role': 'Inventory plasmid; select a separately defined molecular product for prediction.'
                                 if record['identity']['molecule_type'] == 'dna' else
                                 'Existing protein product; source review status: '+record['identity']['product_review']['status']+'.'}
                                for record in records], status='defined', tags=['construct-inventory'],
                       provenance={'migration_fingerprint': fingerprint, 'source_snapshot_sha256': audit['snapshot']['sha256'],
                                   'source_original_main_file_sha256': audit['original_main_file']['sha256'],
                                   'context_docx_sha256': context_manifest['source_docx']['sha256']})
        project_record = library.import_record(project, attachments)
        records.append(project_record)
        verification = library.verify()
        backup = library.export_snapshot(temporary/'library.tar.gz')
        shutil.rmtree(scratch)
        result = {'schema': SCHEMA, 'migration_fingerprint': fingerprint, 'created_at': audit['captured_at'],
                  'project_ref': r.reference(project_record), 'project_sha256': project_record['sha256'],
                  'source_audit': audit, 'mapping': mapping, 'context_matched_records': len(descriptions),
                  'records': {r.reference(record): record['sha256'] for record in records},
                  'registry_verification': verification, 'backup': {key: value for key, value in backup.items() if key != 'path'}}
        result['sha256'] = r.digest_json(result)
        _put(temporary/'migration.json', _raw(result))
        verify_stage(temporary)
        _rename(temporary, destination, 1)
        return result
    finally:
        r.now = original_now
        if temporary.exists():
            shutil.rmtree(temporary)


def verify_stage(path):
    path = Path(path).absolute()
    result = r.verify_document(r.load_json(path/'migration.json'))
    r.require(result.get('schema') == SCHEMA and result['project_ref'] == f'project:{PROJECT}@1', 'Unexpected migration receipt')
    library = r.Registry(path/'registry')
    with library._lock():
        records = library._records_locked()
        r.require({ref: record['sha256'] for ref, record in records.items()} == result['records'], 'Staged record set/hash mismatch')
        project = records[result['project_ref']]
        r.require(project['provenance']['migration_fingerprint'] == result['migration_fingerprint'], 'Migration identity mismatch')
        archive_root = library._path(result['project_ref']).parent/'attachments'
        archive_manifest = r.verify_document(r.load_json(archive_root/'migration-manifest.json'))
        r.require(archive_manifest['migration_fingerprint'] == result['migration_fingerprint'] ==
                  r.digest_json(archive_manifest['fingerprint_inputs']) and
                  archive_manifest['mapping'] == result['mapping'] and
                  archive_manifest['source_audit'] == result['source_audit'] and
                  archive_manifest['fingerprint_inputs']['source_audit_sha256'] == result['source_audit']['sha256'],
                  'Stage differs from archived migration inputs')
        r.require(_receipt(archive_root/'constructs-snapshot.sqlite') == result['source_audit']['snapshot'] and
                  _receipt(archive_root/'constructs-original.sqlite') == result['source_audit']['original_main_file'],
                  'Archived source database differs from the frozen bytes')
        r.require(_receipt(archive_root/'source-files.tar.gz') == archive_manifest['source_archive'] and
                  _receipt(archive_root/'research-context.tar.gz') == archive_manifest['context_archive'],
                  'Archived original source/context bytes changed')
        _verify_archive(archive_root/'source-files.tar.gz', archive_manifest['fingerprint_inputs']['source_files'])
        _verify_archive(archive_root/'research-context.tar.gz', archive_manifest['fingerprint_inputs']['context_files'])
        source_rows = _source_rows(archive_root/'constructs-snapshot.sqlite')
        r.require(source_rows['logical_sha256'] == result['source_audit']['snapshot_logical_sha256'], 'Archived source logical identity changed')
        source_sequences = {('sequence', row['id']): row for row in source_rows['sequences']}
        source_sequences.update({('protein_product', row['product_id']): row for row in source_rows['protein_products']})
        r.require(len(result['mapping']) == len(source_sequences) and
                  {(item['source_kind'], item['source_id']) for item in result['mapping']} == set(source_sequences),
                  'Source sequence/product coverage is incomplete or duplicated')
        r.require({item['ref'] for item in result['mapping']} == set(records)-{result['project_ref']},
                  'Mapping does not cover exactly the published constructs')
        plasmid_refs = {item['source_id']: item['ref'] for item in result['mapping'] if item['source_kind'] == 'sequence'}
        members = {item['source_ref'] for item in project['identity']['members']}
        r.require(members == set(records)-{result['project_ref']}, 'Project membership does not cover every migrated construct')
        for item in result['mapping']:
            record = records[item['ref']]
            source_row = source_sequences[(item['source_kind'], item['source_id'])]
            original_sequence = source_row['sequence'] if item['source_kind'] == 'sequence' else source_row['candidate_sequence']
            r.require(hashlib.sha256(record['identity']['sequence'].encode()).hexdigest() == item['sequence_sha256'] and
                      record['identity']['sequence'] == original_sequence, 'Mapped sequence bytes changed')
            if item['source_kind'] == 'protein_product':
                r.require(record['identity']['encoded_by']['construct_ref'] == item['encoded_by'] and
                          item['encoded_by'] == plasmid_refs[source_row['sequence_id']] and
                          record['identity']['product_review']['status'] == item['product_review'] == source_row['validation_status'],
                          'Mapped source relationship/review changed')
    r.require(_receipt(path/'library.tar.gz') == {key: result['backup'][key] for key in ('bytes', 'sha256')}, 'Stage backup bytes changed')
    verified = r.verify_backup(path/'library.tar.gz')
    r.require(verified['manifest_sha256'] == result['backup']['manifest_sha256'] and verified['records'] == len(records), 'Stage backup contents changed')
    return result


def _rename(source, destination, flags):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    r.require(rename is not None, 'Atomic migration publication requires Linux renameat2')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), flags):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))
    r.fsync_directory(Path(destination).parent)


def publish(staged, destination, expected_fingerprint):
    """Publish only the verified exact stage to an empty initialized registry."""
    staged, destination = Path(staged).absolute(), Path(destination).absolute()
    result = verify_stage(staged)
    r.require(expected_fingerprint == result['migration_fingerprint'], 'Explicit expected migration fingerprint mismatch')
    r.no_symlinks(destination)
    library = r.Registry(destination)
    with r.publication_guard(destination, exclusive=True):
        r.require(destination.is_dir(), 'Initialize the destination registry before publication')
        with library._record_lock(exclusive=True):
            current = library._records_locked()
            if current:
                r.require({ref: record['sha256'] for ref, record in current.items()} == result['records'],
                          'Destination contains existing or changed records; migration will not merge or overwrite them')
                return {'published': True, 'already_published': True, 'root': str(destination),
                        'records': len(current), 'migration_fingerprint': expected_fingerprint}
            allowed = {*r.COLLECTIONS.values(), '.staging', '.registry.lock', 'index.sqlite3'}
            r.require({p.name for p in destination.iterdir()} <= allowed and
                      not any((destination/'.staging').iterdir()), 'Destination has unrelated files or unfinished staging')
            temporary = Path(tempfile.mkdtemp(prefix='.'+destination.name+'.migration-', dir=destination.parent))
            temporary.rmdir()
            try:
                r.restore_backup(staged/'library.tar.gz', temporary)
                replacement = r.Registry(temporary)
                with replacement._lock():
                    r.require({ref: record['sha256'] for ref, record in replacement._records_locked().items()} == result['records'],
                              'Restored publication candidate differs from staged records')
                _rename(temporary, destination, 2)  # RENAME_EXCHANGE; the old empty library remains at temporary.
                return {'published': True, 'already_published': False, 'root': str(destination),
                        'records': len(result['records']), 'migration_fingerprint': expected_fingerprint,
                        'previous_empty_registry': str(temporary)}
            except Exception:
                if temporary.exists():
                    shutil.rmtree(temporary)
                raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    freeze_parser = commands.add_parser('freeze')
    freeze_parser.add_argument('--source', required=True)
    freeze_parser.add_argument('--out', required=True)
    stage_parser = commands.add_parser('stage')
    stage_parser.add_argument('--source-directory', required=True)
    stage_parser.add_argument('--frozen', required=True)
    stage_parser.add_argument('--context', required=True)
    stage_parser.add_argument('--out', required=True)
    verify_parser = commands.add_parser('verify-stage')
    verify_parser.add_argument('stage')
    publish_parser = commands.add_parser('publish')
    publish_parser.add_argument('--stage', required=True)
    publish_parser.add_argument('--root', required=True)
    publish_parser.add_argument('--expected-fingerprint', required=True)
    args = parser.parse_args(argv)
    if args.command == 'freeze':
        result = freeze(args.source, args.out)
    elif args.command == 'stage':
        result = stage(args.source_directory, args.frozen, args.context, args.out)
    elif args.command == 'verify-stage':
        result = verify_stage(args.stage)
    else:
        result = publish(args.stage, args.root, args.expected_fingerprint)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (r.Error, OSError, sqlite3.Error) as exc:
        raise SystemExit('bio-library migration: '+str(exc))
