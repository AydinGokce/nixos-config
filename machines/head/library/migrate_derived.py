#!/usr/bin/env python3
"""Review and atomically migrate audited explicit proteins to DNA coordinates.

The plan pins current curation and immutable source evidence. Applying it never
changes a sequence, deletes a revision, or starts a prediction. Reapplying the
same successful plan verifies its immutable per-product receipts and is a no-op.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import sys

MIGRATION = 'audited-coordinate-proteins-v1'
MAX_DOCUMENT = 16 * 1024 * 1024


def digest(value):
    return hashlib.sha256(value).hexdigest()


def load(module, path):
    path = Path(path)
    module.require(path.stat().st_size <= MAX_DOCUMENT, 'Migration document exceeds 16 MiB')
    raw = path.read_bytes()
    return module.parse_json(raw.decode('utf-8')), digest(raw)


def plan_digest(module, plan):
    return digest(module.json_bytes({key: value for key, value in plan.items() if key != 'plan_sha256'}))


def entity(module, reference):
    return module.pinned_parts(reference)[:2]


def newest(records):
    result = {}
    for record in records.values():
        key = (record['kind'], record['id'])
        if key not in result or record['revision'] > result[key]['revision']:
            result[key] = record
    return result


def translation(row):
    return {'schema': 1, 'segments': [
        {'start': part['start_0based'], 'end': part['end_exclusive']}
        for part in row['coding_parts_in_translation_order']],
        'strand': row['strand'], 'genetic_code': row['translation_table'],
        'codon_start': row['codon_start'], 'initiation': 'cds',
        'residue_start': 0, 'residue_end': None}


def attachment(record, name):
    return next((item for item in record['attachments'] if item['path'] == 'attachments/' + name), None)


def original_evidence(module, records, row):
    label = row['product_id']
    protein = records.get(row['migrated_ref'])
    parent = records.get(row['plasmid_ref'])
    module.require(protein is not None and protein['sha256'] == row['migrated_record_sha256'],
                   label + ': original protein revision or digest differs from the audit')
    module.require(parent is not None and parent['sha256'] == row['plasmid_record_sha256'],
                   label + ': original nucleotide revision or digest differs from the audit')
    module.require(row.get('exact_footprint_and_translation') is True,
                   label + ': source audit has not established an exact translation')
    module.require(digest(parent['identity']['sequence'].encode()) == row['source_sequence_sha256'],
                   label + ': audited nucleotide digest is inconsistent')
    receipt = attachment(protein, 'derivation.json')
    module.require(receipt is not None and receipt['sha256'] == row['derivation_attachment_sha256'],
                   label + ': original derivation attachment differs from the audit')
    binding(module, protein, row, records)
    value = translation(row)
    module.translation.validate_definition(value)
    module.require(all(part.get('strand') == value['strand'] for part in row['coding_parts_in_translation_order']),
                   label + ': mixed source-coordinate strands are not supported')
    candidate = {'kind': 'construct', 'identity': {'molecule_type': 'protein', 'encoded_by': {
        'construct_ref': row['plasmid_ref'], 'sequence_sha256': row['source_sequence_sha256'], 'translation': value}}}
    result = module.effective_sequence(candidate, records)
    module.require(result['available'] and result['sequence_sha256'] == row['protein_sha256'] and
                   result.get('coding_sequence_sha256') == row['coding_dna_sha256'],
                   label + ': shared translation engine disagrees with the source audit')
    return value


def binding(module, protein, row, records):
    label = row['product_id']
    identity = protein.get('identity', {})
    encoded = identity.get('encoded_by', {})
    module.require(protein['kind'] == 'construct' and identity.get('molecule_type') == 'protein',
                   label + ': expected an ordinary protein construct')
    module.require(encoded.get('construct_ref') and entity(module, encoded['construct_ref']) == entity(module, row['plasmid_ref']) and
                   encoded.get('sequence_sha256') == row['source_sequence_sha256'],
                   label + ': protein nucleotide-source binding has changed')
    result = module.effective_sequence(protein, records)
    module.require(result['available'] and result['sequence_sha256'] == row['protein_sha256'],
                   label + ': current or project-pinned peptide differs from the audited product')


def make_plan(module, registry, records, audit, audit_sha256, expected_products=87):
    rows = audit.get('product_rows')
    module.require(type(expected_products) is int and 1 <= expected_products <= 1000,
                   'Expected product count must be 1..1000')
    module.require(isinstance(rows, list) and len(rows) == expected_products,
                   f'Expected exactly {expected_products} audited products')
    module.require(len({row['product_id'] for row in rows}) == len(rows) and
                   len({row['migrated_ref'] for row in rows}) == len(rows), 'Duplicate audited products')
    latest = newest(records)
    entries, row_by_entity = [], {}
    for row in sorted(rows, key=lambda value: value['migrated_ref']):
        label = row['product_id']
        value = original_evidence(module, records, row)
        key = entity(module, row['migrated_ref'])
        module.require(key not in row_by_entity, 'Duplicate audited protein entity')
        row_by_entity[key] = row
        current = latest[key]
        parent = latest[entity(module, row['plasmid_ref'])]
        module.require(parent['identity'].get('molecule_type') in {'dna', 'rna'} and
                       isinstance(parent['identity'].get('sequence'), str) and
                       digest(parent['identity']['sequence'].encode()) == row['source_sequence_sha256'],
                       label + ': current parent sequence differs from the audited nucleotide source')
        binding(module, current, row, records)
        receipt = attachment(current, 'derivation.json')
        module.require(receipt is not None and receipt['sha256'] == row['derivation_attachment_sha256'],
                       label + ': current source derivation evidence changed')
        identity = deepcopy(current['identity'])
        identity.pop('sequence', None)
        identity['encoded_by'].update(construct_ref=module.reference(parent),
                                      sequence_sha256=row['source_sequence_sha256'], translation=value)
        result = module.effective_sequence({'kind': 'construct', 'identity': identity}, records)
        module.require(result['available'] and result['sequence_sha256'] == row['protein_sha256'] and
                       result.get('coding_sequence_sha256') == row['coding_dna_sha256'],
                       label + ': current parent cannot reproduce the audited coordinate product')
        derived = module.translation.is_derived(current)
        if derived:
            module.require(current['identity'] == identity,
                           label + ': an existing coordinate definition differs; migration will not overwrite it')
        else:
            module.require('sequence' in current['identity'] and 'coordinate_migration' not in current['provenance'],
                           label + ': an explicit protein carries an incompatible migration receipt')
        before_ref = module.reference(current)
        entries.append({'product_id': label, 'action': 'already_derived' if derived else 'derive',
            'before_ref': before_ref, 'before_sha256': current['sha256'],
            'after_ref': before_ref if derived else f"construct:{current['id']}@{current['revision'] + 1}",
            'parent_ref': module.reference(parent), 'parent_sha256': parent['sha256'],
            'source_sequence_sha256': row['source_sequence_sha256'], 'protein_sha256': row['protein_sha256'],
            'coding_dna_sha256': row['coding_dna_sha256'], 'translation': value,
            'audited_ref': row['migrated_ref'], 'audited_sha256': row['migrated_record_sha256'],
            'derivation_attachment_sha256': row['derivation_attachment_sha256']})
    targets = {entity(module, entry['before_ref']): entry['after_ref'] for entry in entries}
    projects = []
    for current in sorted(latest.values(), key=module.reference):
        if current['kind'] != 'project':
            continue
        members = deepcopy(current['identity']['members'])
        replaced = []
        for member in members:
            key = entity(module, member['source_ref'])
            if key not in targets or member['source_ref'] == targets[key]:
                continue
            binding(module, records[member['source_ref']], row_by_entity[key], records)
            replaced.append({'before_ref': member['source_ref'], 'after_ref': targets[key]})
            member['source_ref'] = targets[key]
        if replaced:
            projects.append({'before_ref': module.reference(current), 'before_sha256': current['sha256'],
                'after_ref': f"project:{current['id']}@{current['revision'] + 1}",
                'members': members, 'replaced_refs': replaced})
    plan = {'schema': 1, 'migration': MIGRATION, 'audit_sha256': audit_sha256,
            'expected_products': expected_products, 'products': entries, 'projects': projects}
    plan['plan_sha256'] = plan_digest(module, plan)
    return plan


def receipt(plan, entry, kind):
    return {'schema': 1, 'migration': MIGRATION, 'plan_sha256': plan['plan_sha256'],
            'audit_sha256': plan['audit_sha256'], 'kind': kind, 'before_ref': entry['before_ref'],
            'before_sha256': entry['before_sha256'], 'after_ref': entry['after_ref']}


def changes_for(module, records, plan):
    changes = []
    for entry in plan['products']:
        if entry['action'] == 'already_derived':
            continue
        current = records[entry['before_ref']]
        identity = deepcopy(current['identity']); identity.pop('sequence')
        identity['encoded_by'].update(construct_ref=entry['parent_ref'],
            sequence_sha256=entry['source_sequence_sha256'], translation=deepcopy(entry['translation']))
        provenance = deepcopy(current['provenance'])
        provenance['coordinate_migration'] = {**receipt(plan, entry, 'protein'),
            'audited_ref': entry['audited_ref'], 'audited_sha256': entry['audited_sha256'],
            'source_ref': entry['parent_ref'], 'source_sha256': entry['parent_sha256'],
            'protein_sha256': entry['protein_sha256'], 'derivation_attachment_sha256': entry['derivation_attachment_sha256']}
        changes.append({'ref': entry['before_ref'], 'expected_sha256': entry['before_sha256'],
                        'patch': {'identity': identity, 'provenance': provenance}})
    for entry in plan['projects']:
        current = records[entry['before_ref']]
        identity = deepcopy(current['identity']); identity['members'] = deepcopy(entry['members'])
        provenance = deepcopy(current['provenance'])
        provenance['coordinate_migration'] = {**receipt(plan, entry, 'project'),
                                             'replaced_refs': deepcopy(entry['replaced_refs'])}
        changes.append({'ref': entry['before_ref'], 'expected_sha256': entry['before_sha256'],
                        'patch': {'identity': identity, 'provenance': provenance}})
    return changes


def verify_applied(module, records, plan):
    """Verify immutable migration revisions even if later curation advanced them."""
    entries = [entry for entry in plan['products'] if entry['action'] == 'derive'] + plan['projects']
    if not entries:
        return False
    found = [records.get(entry['after_ref'], {}).get('provenance', {}).get('coordinate_migration', {}).get('plan_sha256') == plan['plan_sha256']
             for entry in entries]
    if not any(found):
        return False
    module.require(all(found), 'Only part of the migration is present; do not publish a second transaction')
    for entry, change in zip(entries, changes_for(module, records, plan)):
        before, after = records[entry['before_ref']], records[entry['after_ref']]
        module.require(before['sha256'] == change['expected_sha256'], 'Migration predecessor digest changed')
        expected = {key: deepcopy(value) for key, value in before.items() if key in module.USER_FIELDS}
        expected.update(deepcopy(change['patch']))
        expected['parents'] = list(dict.fromkeys([*expected.get('parents', []), entry['before_ref']]))
        module.require(all(after.get(key) == value for key, value in expected.items()) and
                       after['attachments'] == before['attachments'],
                       entry['after_ref'] + ': migrated revision differs from its reviewed change')
        if after['kind'] == 'construct':
            result = module.effective_sequence(after, records)
            module.require(result['available'] and result['sequence_sha256'] == entry['protein_sha256'],
                           entry['after_ref'] + ': migrated peptide does not match the audit')
    return True


def apply_plan(module, registry, audit, audit_sha256, plan):
    module.require(plan.get('schema') == 1 and plan.get('migration') == MIGRATION and
                   plan.get('plan_sha256') == plan_digest(module, plan), 'Invalid migration plan digest or schema')
    module.require(plan['audit_sha256'] == audit_sha256, 'Audit file differs from the reviewed plan')
    with registry._lock(exclusive=True):
        records = registry._records_locked()
        if verify_applied(module, records, plan):
            return {'status': 'already_applied', 'plan_sha256': plan['plan_sha256'], 'changed_refs': []}
        current = make_plan(module, registry, records, audit, audit_sha256, plan['expected_products'])
        module.require(current == plan, 'Library revisions changed after planning; review a fresh plan before applying')
        changes = changes_for(module, records, plan)
        if not changes:
            return {'status': 'already_derived', 'plan_sha256': plan['plan_sha256'], 'changed_refs': []}
        outputs = registry._revise_many_locked(changes, records)
        module.require(verify_applied(module, records, plan), 'Migration receipts could not be verified after publication')
        return {'status': 'applied', 'plan_sha256': plan['plan_sha256'],
                'changed_refs': [{'before_ref': change['ref'], 'after_ref': module.reference(output),
                                  'after_sha256': output['sha256']} for change, output in zip(changes, outputs)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools-dir', default=str(Path(__file__).resolve().parents[1]))
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'apply'):
        command = commands.add_parser(name)
        command.add_argument('--root', required=True, help='Existing library registry root')
        command.add_argument('--audit', required=True, help='Exact retained coordinate-audit.json')
        if name == 'plan':
            command.add_argument('--output', required=True, help='New plan file (never overwritten)')
            command.add_argument('--expected-products', type=int, default=87)
        else:
            command.add_argument('--plan', required=True)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(args.tools_dir).absolute() / 'library'))
    module = importlib.import_module('registry')
    try:
        audit, audit_sha256 = load(module, args.audit)
        registry = module.Registry(args.root)
        if args.command == 'plan':
            with registry._lock():
                plan = make_plan(module, registry, registry._records_locked(), audit, audit_sha256, args.expected_products)
            with Path(args.output).open('x', encoding='utf-8') as handle:
                json.dump(plan, handle, ensure_ascii=False, indent=2); handle.write('\n')
            result = {'status': 'planned', 'plan_sha256': plan['plan_sha256'],
                      'products_to_derive': sum(entry['action'] == 'derive' for entry in plan['products']),
                      'already_derived': sum(entry['action'] == 'already_derived' for entry in plan['products']),
                      'projects_to_update': len(plan['projects']), 'output': str(Path(args.output).absolute())}
        else:
            plan, _ = load(module, args.plan)
            result = apply_plan(module, registry, audit, audit_sha256, plan)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (module.Error, ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({'status': 'refused', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
