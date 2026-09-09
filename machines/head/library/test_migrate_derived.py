"""Audit-bound coordinate migration, atomic publication and durable retries."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import migrate_derived as migration
import registry as r


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class CoordinateMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.registry = r.Registry(self.base / 'registry'); self.registry.init()
        self.doc = self.base / 'description.md'; self.doc.write_text('# Retained user purpose\n')
        self.sequence = 'ATGGCTTAA'
        self.parent = self.registry.import_record({'kind': 'construct', 'id': 'plasmid',
            'identity': {'molecule_type': 'dna', 'sequence': self.sequence, 'circular': True,
                         'molecular_form': 'plasmid', 'strand_count': 2}})
        self.rows = []
        self.products = []
        for index, status in enumerate(('reference_matched', 'review_required')):
            self.source = self.base / ('derivation-' + str(index) + '.json')
            self.source.write_bytes(r.json_bytes({'source_product': {'id': index}, 'evidence': []}))
            product = self.registry.import_record({'kind': 'construct', 'id': 'protein-' + str(index),
                'name': 'Original source name ' + str(index), 'status': 'defined',
                'identity': {'molecule_type': 'protein', 'sequence': 'MA', 'encoded_by': {
                    'construct_ref': r.reference(self.parent), 'sequence_sha256': sha(self.sequence)},
                    'product_review': {'status': status, 'source': 'fixture', 'source_product_id': str(index), 'method_version': 'v1'}},
                'provenance': {'workbench': {'alt_name': 'Current Alt ' + str(index)}, 'inventory': {'verbose_name': 'Verbose original'}}},
                {'derivation.json': self.source, 'description.md': self.doc})
            self.products.append(product)
            self.rows.append({'product_id': 'test:' + str(index), 'migrated_ref': r.reference(product),
                'migrated_record_sha256': product['sha256'], 'plasmid_ref': r.reference(self.parent),
                'plasmid_record_sha256': self.parent['sha256'], 'source_sequence_sha256': sha(self.sequence),
                'protein_sha256': sha('MA'), 'coding_dna_sha256': sha(self.sequence),
                'derivation_attachment_sha256': migration.attachment(product, 'derivation.json')['sha256'],
                'exact_footprint_and_translation': True, 'coding_parts_in_translation_order': [
                    {'start_0based': 0, 'end_exclusive': 9, 'strand': 1}],
                'strand': 1, 'translation_table': 1, 'codon_start': 1})
        self.project = self.registry.import_record({'kind': 'project', 'id': 'study', 'name': 'Current project title',
            'identity': {'objectives_file': 'attachments/project.md', 'members': [
                {'source_ref': r.reference(product), 'role': 'Purpose ' + product['id']} for product in self.products]},
            'provenance': {'workbench': {'alt_name': 'Research project'}, 'keep': ['all', 'metadata']}},
            {'project.md': self.doc})
        self.audit = {'product_rows': self.rows}
        self.audit_sha = sha(r.json_bytes(self.audit).decode())

    def records(self):
        with self.registry._lock():
            return self.registry._records_locked()

    def plan(self):
        with self.registry._lock():
            return migration.make_plan(r, self.registry, self.registry._records_locked(), self.audit, self.audit_sha, 2)

    def apply(self, plan):
        return migration.apply_plan(r, self.registry, self.audit, self.audit_sha, plan)

    def test_plan_is_read_only_deterministic_and_binds_all_current_preconditions(self):
        before = self.records()
        plan = self.plan()
        self.assertEqual(plan, self.plan())
        self.assertEqual(before, self.records())
        self.assertEqual(len(plan['products']), 2)
        self.assertEqual(len(plan['projects']), 1)
        self.assertEqual(plan['products'][0]['parent_sha256'], self.parent['sha256'])
        self.assertEqual(plan['projects'][0]['before_sha256'], self.project['sha256'])

    def test_atomic_conversion_preserves_sequences_metadata_documents_and_review_states(self):
        before = self.records()
        plan = self.plan()
        result = self.apply(plan)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(len(result['changed_refs']), 3)
        records = self.records()
        self.assertEqual(len(records), len(before) + 3)
        for reference, record in before.items():
            self.assertEqual(records[reference], record)
        for original in self.products:
            current = self.registry.show(original['id'])
            self.assertNotIn('sequence', current['identity'])
            self.assertEqual(r.effective_sequence(current, records)['sequence'], original['identity']['sequence'])
            self.assertEqual(current['identity']['product_review'], original['identity']['product_review'])
            self.assertEqual(current['attachments'], original['attachments'])
            for key in ('name', 'aliases', 'tags', 'notes', 'status'):
                self.assertEqual(current[key], original[key])
            for key, value in original['provenance'].items():
                self.assertEqual(current['provenance'][key], value)
        project = self.registry.show('study')
        self.assertEqual(project['identity']['members'], plan['projects'][0]['members'])
        self.assertEqual(project['attachments'], self.project['attachments'])
        self.assertEqual(project['provenance']['keep'], ['all', 'metadata'])

    def test_metadata_curated_products_and_older_project_pins_advance_to_current_definition(self):
        old = self.products[0]
        provenance = deepcopy(old['provenance']); provenance['workbench'].update(alt_name='User renamed it', archived=True)
        doc = self.base / 'updated.md'; doc.write_text('# Latest user purpose\n')
        current = self.registry.revise(old['id'], {'name': 'Edited source title', 'provenance': provenance}, {'description.md': doc})
        parent = self.registry.revise('plasmid', {'notes': 'Parent metadata only'})
        plan = self.plan()
        self.assertEqual(plan['products'][0]['before_ref'], r.reference(current))
        self.assertEqual(plan['products'][0]['parent_ref'], r.reference(parent))
        self.assertEqual(plan['projects'][0]['replaced_refs'][0]['before_ref'], r.reference(old))
        self.apply(plan)
        current = self.registry.show(old['id'])
        self.assertEqual(current['name'], 'Edited source title')
        self.assertEqual(current['provenance']['workbench'], provenance['workbench'])
        self.assertEqual(migration.attachment(current, 'description.md')['sha256'], hashlib.sha256(doc.read_bytes()).hexdigest())
        self.assertEqual(self.registry.show('study')['identity']['members'][0]['source_ref'], r.reference(current))

    def test_retry_verifies_historical_receipts_after_unrelated_project_and_product_edits(self):
        plan = self.plan(); self.apply(plan)
        self.registry.revise('study', {'notes': 'Later objectives edit'})
        self.registry.revise('protein-0', {'notes': 'Later product curation'})
        before = self.records()
        result = self.apply(plan)
        self.assertEqual(result['status'], 'already_applied')
        self.assertEqual(result['changed_refs'], [])
        self.assertEqual(self.records(), before)
        fresh = self.plan()
        self.assertTrue(all(entry['action'] == 'already_derived' for entry in fresh['products']))

    def test_stale_review_plan_rejects_current_project_product_and_parent_metadata_changes(self):
        for ident in ('study', 'protein-0', 'plasmid'):
            with self.subTest(ident=ident):
                plan = self.plan()
                self.registry.revise(ident, {'notes': 'Concurrent edit ' + ident})
                before = self.records()
                with self.assertRaisesRegex(r.Error, 'changed after planning'):
                    self.apply(plan)
                self.assertEqual(self.records(), before)

    def test_current_peptide_or_parent_mismatch_refuses_entire_migration(self):
        current = self.products[0]
        self.registry.revise(current['id'], {'identity': {**current['identity'], 'sequence': 'MG'}})
        with self.assertRaisesRegex(r.Error, 'peptide differs'):
            self.plan()
        self.registry.revise(current['id'], {'identity': current['identity']})
        self.registry.revise('plasmid', {'identity': {**self.parent['identity'], 'sequence': 'ATGGGTTAA'}})
        with self.assertRaisesRegex(r.Error, 'current parent sequence differs'):
            self.plan()
        self.assertTrue(all('sequence' in self.registry.show(p['id'])['identity'] for p in self.products))

    def test_current_project_pinned_different_peptide_is_never_silently_replaced(self):
        original = self.products[0]
        different = self.registry.revise(original['id'], {'identity': {**original['identity'], 'sequence': 'MG'}})
        self.registry.revise(original['id'], {'identity': original['identity']})
        identity = deepcopy(self.project['identity']); identity['members'][0]['source_ref'] = r.reference(different)
        self.registry.revise('study', {'identity': identity})
        with self.assertRaisesRegex(r.Error, 'project-pinned peptide differs'):
            self.plan()

    def test_missing_or_changed_original_evidence_and_source_attachments_refuse(self):
        for field in ('migrated_record_sha256', 'plasmid_record_sha256', 'derivation_attachment_sha256'):
            with self.subTest(field=field):
                original = self.rows[0][field]; self.rows[0][field] = '0' * 64
                with self.assertRaisesRegex(r.Error, 'differs from the audit'):
                    self.plan()
                self.rows[0][field] = original
        changed = self.base / 'new-derivation.json'; changed.write_text('{"different":true}')
        self.registry.revise('protein-0', {}, {'derivation.json': changed})
        with self.assertRaisesRegex(r.Error, 'current source derivation evidence changed'):
            self.plan()

    def test_plan_or_audit_tampering_refuses_before_any_write(self):
        plan = self.plan(); before = self.records()
        changed = deepcopy(plan); changed['products'][0]['translation']['segments'][0]['start'] = 3
        with self.assertRaisesRegex(r.Error, 'plan digest'):
            self.apply(changed)
        with self.assertRaisesRegex(r.Error, 'Audit file differs'):
            migration.apply_plan(r, self.registry, self.audit, '0' * 64, plan)
        self.assertEqual(self.records(), before)

    def test_failure_before_journal_publishes_nothing_and_can_retry(self):
        plan = self.plan(); before = self.records()
        with patch.object(r, 'atomic_json', side_effect=OSError('before journal')):
            with self.assertRaisesRegex(OSError, 'before journal'):
                self.apply(plan)
        self.assertEqual(before, self.records())
        self.assertEqual(self.apply(plan)['status'], 'applied')

    def test_crash_after_journal_recovers_one_complete_migration_and_retry_is_noop(self):
        plan = self.plan()
        original = r.atomic_json

        def durable_then_error(path, value):
            original(path, value)
            raise OSError('after journal rename')

        with patch.object(r, 'atomic_json', side_effect=durable_then_error):
            with self.assertRaisesRegex(OSError, 'after journal rename'):
                self.apply(plan)
        result = self.apply(plan)
        self.assertEqual(result['status'], 'already_applied')
        self.assertEqual(len(self.records()), 7)
        self.assertEqual(len(list((self.registry.root / '.transactions').glob('*.json'))), 0)


if __name__ == '__main__':
    unittest.main()
