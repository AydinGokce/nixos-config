"""Scientific-identity and malformed-input regressions for BindCraft targets."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench import binder_structure as structure


def ref(chain, number, insertion=''):
    return dict(chain=chain, number=number, insertion_code=insertion)


def pdb(chains, *, alt='', residue='ALA', record='ATOM', remove=(), ter_after=()):
    """A peptide fixture with 1.3 Å inter-residue C–N distances."""
    lines, serial = [], 1
    for chain, ids in chains:
        for index, value in enumerate(ids):
            number, insertion = value if isinstance(value, tuple) else (value, '')
            for name, dx, y, element in [('N', 0, 0, 'N'), ('CA', 1.3, .3, 'C'),
                                         ('C', 2.5, 0, 'C'), ('O', 2.5, 1.2, 'O'), ('CB', 1.3, 1.8, 'C')]:
                if name in remove:
                    continue
                atom_field = f' {name:<3}'
                lines.append(f'{record:<6}{serial:5d} {atom_field}{alt:1}{residue:>3} {chain or " "}{number:4d}{insertion or " "}   '
                             f'{index * 3.8 + dx:8.3f}{y:8.3f}{0:8.3f}{1:6.2f}{87.5:6.2f}          {element:>2}  ')
                serial += 1
            if index in ter_after:
                lines.append(f'TER   {serial:5d}      {residue:>3} {chain or " "}{number:4d}')
                serial += 1
        lines.append(f'TER   {serial:5d}      {residue:>3} {chain or " "}{number:4d}')
        serial += 1
    return ('\n'.join(lines) + '\nEND\n').encode()


COLUMNS = ['group_PDB', 'id', 'type_symbol', 'label_atom_id', 'label_alt_id',
           'label_comp_id', 'label_asym_id', 'label_seq_id', 'pdbx_PDB_ins_code',
           'Cartn_x', 'Cartn_y', 'Cartn_z', 'occupancy', 'B_iso_or_equiv',
           'auth_seq_id', 'auth_comp_id', 'auth_asym_id', 'auth_atom_id', 'pdbx_PDB_model_num']


def cif(chains, *, model='1', alt='.', prelude='', use_auth=True):
    rows = []
    serial = 1
    for chain, ids in chains:
        for index, value in enumerate(ids):
            number, insertion = value if isinstance(value, tuple) else (value, '')
            for name, dx, y, element in [('N', 0, 0, 'N'), ('CA', 1.3, .3, 'C'),
                                         ('C', 2.5, 0, 'C'), ('O', 2.5, 1.2, 'O'), ('CB', 1.3, 1.8, 'C')]:
                values = ['ATOM', str(serial), element, name, alt, 'ALA', 'label_' + chain,
                          str(index + 1), insertion or '?', str(index * 3.8 + dx), str(y), '0',
                          '1', '87.5', str(number), 'ALA', chain, name, model]
                rows.append(values)
                serial += 1
    columns = COLUMNS if use_auth else [c for c in COLUMNS if not c.startswith('auth_')]
    selected = [COLUMNS.index(c) for c in columns]
    return ('data_target\n' + prelude + '\nloop_\n' + '\n'.join('_atom_site.' + c for c in columns) + '\n'
            + '\n'.join(' '.join(row[i] for i in selected) for row in rows) + '\n#\n').encode()


class BinderStructureTests(unittest.TestCase):
    def test_pdb_author_numbers_insertions_and_blank_chain_are_exact(self):
        data = pdb([('', [-2, -1, (42, ''), (42, 'A')])])
        inspected = structure.inspect_structure(data)
        self.assertEqual(inspected['chains'][0]['chain'], '')
        self.assertEqual([ref('', r['number'], r['insertion_code']) for r in inspected['chains'][0]['residues']],
                         [ref('', -2), ref('', -1), ref('', 42), ref('', 42, 'A')])
        normalized = structure.normalize_structure(data, chains=[''], hotspots=[ref('', 42, 'A')])
        self.assertEqual(normalized['chains'], 'A')
        self.assertEqual(normalized['hotspots'], 'A4')
        self.assertEqual(normalized['residue_map'][-1]['original'], ref('', 42, 'A'))
        self.assertEqual(normalized['residue_map'][-1]['submitted'], ref('A', 4))
        output = structure.inspect_structure(normalized['pdb'])
        self.assertEqual(output['chains'][0]['sequence'], 'AAAA')
        self.assertEqual([r['position'] for r in output['chains'][0]['residues']],
                         [r['position'] for r in inspected['chains'][0]['residues']])

    def test_mmcif_multi_character_chain_author_label_mapping_and_insertion(self):
        data = cif([('heavy_chain', [42, (42, 'A'), 43]), ('light_chain', [900, 901])])
        inspected = structure.inspect_structure(data, 'target.mmcif')
        self.assertEqual([chain['chain'] for chain in inspected['chains']], ['heavy_chain', 'light_chain'])
        result = structure.normalize_structure(data, filename='target.mmcif', chains=['light_chain', 'heavy_chain'],
            hotspots=[ref('heavy_chain', 42, 'A'), ref('light_chain', 901)])
        self.assertEqual(result['chains'], 'A,B')
        self.assertEqual(result['hotspots'], 'A2,B2')
        self.assertEqual(result['residue_map'][1]['original'], ref('heavy_chain', 42, 'A'))
        self.assertEqual(result['residue_map'][-1]['submitted'], ref('B', 2))

    def test_disjoint_crop_is_separate_peptides_with_exact_hotspot_remapping(self):
        data = pdb([('Z', [101, 102, 103, 104, 105, 106])])
        result = structure.normalize_structure(data, chains=['Z'],
            crop=[ref('Z', i) for i in [106, 102, 105, 101]], hotspots=[ref('Z', 105)])
        self.assertEqual(result['chains'], 'A,B')
        self.assertEqual(result['hotspots'], 'B1')
        output = structure.inspect_structure(result['pdb'])
        self.assertEqual([c['residue_count'] for c in output['chains']], [2, 2])
        self.assertEqual([r['original']['number'] for r in result['residue_map']], [101, 102, 105, 106])
        self.assertIn('separate submitted chain fragments', result['warnings'][-1])

    def test_explicit_ter_and_coordinate_gaps_never_create_peptide_bonds(self):
        original = pdb([('A', [1, 2, 3, 4])], ter_after=[1])
        result = structure.normalize_structure(original, chains=['A'])
        self.assertEqual(result['chains'], 'A,B')
        lines = pdb([('A', [1, 2, 3, 4])]).decode().splitlines()
        for index, line in enumerate(lines):
            if line.startswith('ATOM') and int(line[22:26]) >= 3:
                lines[index] = line[:30] + f'{float(line[30:38]) + 100:8.3f}' + line[38:]
        result = structure.normalize_structure(('\n'.join(lines) + '\n').encode(), chains=['A'])
        self.assertEqual(result['chains'], 'A,B')

    def test_mmcif_label_sequence_gap_is_preserved_even_when_coordinates_touch(self):
        text = cif([('long', [10, 11, 12, 13])]).decode()
        rows = text.splitlines()
        for i, row in enumerate(rows):
            if row.startswith('ATOM'):
                values = row.split()
                if int(values[7]) >= 3:
                    values[7] = str(int(values[7]) + 5)
                rows[i] = ' '.join(values)
        result = structure.normalize_structure(('\n'.join(rows) + '\n').encode(), chains=['long'])
        self.assertEqual(result['chains'], 'A,B')

    def test_missing_crop_hotspot_or_chain_is_rejected_without_guessing(self):
        data = pdb([('A', [1, 2, (3, 'A')])])
        for arguments in [dict(chains=['B']), dict(chains=['A'], crop=[ref('A', 3)]),
                          dict(chains=['A'], hotspots=[ref('A', 3)]),
                          dict(chains=['A'], crop=[ref('A', 1), ref('A', 2)], hotspots=[ref('A', 3, 'A')])]:
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, 'absent'):
                structure.normalize_structure(data, **arguments)

    def test_reference_types_duplicates_empty_crop_and_single_residue_fragments(self):
        data = pdb([('A', [1, 2, 3])])
        for number in [True, '1', 1.0]:
            with self.subTest(number=number), self.assertRaises(ValueError):
                structure.normalize_structure(data, chains=['A'], hotspots=[ref('A', number)])
        for arguments in [dict(chains=['A', 'A']), dict(chains=['A'], crop=[]),
                          dict(chains=['A'], hotspots=[ref('A', 1), ref('A', 1)]),
                          dict(chains=['A'], crop=[ref('A', 1)]),
                          dict(chains=['A'], crop=[ref('A', 1), ref('A', 3)])]:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                structure.normalize_structure(data, **arguments)

    def test_missing_backbone_is_visible_and_cannot_be_submitted(self):
        data = pdb([('A', [1, 2])], remove=['O'])
        inspected = structure.inspect_structure(data)
        self.assertFalse(inspected['chains'][0]['supported'])
        self.assertIn('Missing backbone atoms: O', inspected['warnings'][0])
        with self.assertRaisesRegex(ValueError, 'Missing backbone'):
            structure.normalize_structure(data, chains=['A'])

    def test_unsupported_chemistry_can_be_inspected_but_never_selected_or_coerced(self):
        canonical = pdb([('A', [1, 2])]).decode().removesuffix('END\n')
        modified = pdb([('M', [1, 2])], residue='MSE', record='HETATM').decode()
        data = (canonical + modified).encode()
        inspected = structure.inspect_structure(data)
        self.assertTrue(inspected['chains'][0]['supported'])
        self.assertFalse(inspected['chains'][1]['supported'])
        good = structure.normalize_structure(data, chains=['A'])
        self.assertEqual(good['chains'], 'A')
        self.assertNotIn(b'MSE', good['pdb'])
        with self.assertRaisesRegex(ValueError, 'canonical polymer amino acids'):
            structure.normalize_structure(data, chains=['M'])

    def test_noncanonical_element_atom_name_and_zero_occupancy_are_rejected(self):
        source = pdb([('A', [1, 2])]).decode()
        first, rest = source.split('\n', 1)
        variants = [first[:76] + 'FE' + first[78:], first[:12] + ' XX ' + first[16:],
                    first[:54] + '  0.00' + first[60:]]
        for line in variants:
            with self.subTest(line=line), self.assertRaisesRegex(ValueError, 'Unsupported selected residue'):
                structure.normalize_structure((line + '\n' + rest).encode(), chains=['A'])

    def test_unambiguous_single_conformer_retains_coordinates_and_records_warning(self):
        data = pdb([('A', [1, 2])], alt='B')
        result = structure.normalize_structure(data, chains=['A'])
        self.assertTrue(any('one explicit conformer B' in note for note in result['warnings']))
        self.assertTrue(all(line[16] == ' ' for line in result['pdb'].decode().splitlines() if line.startswith('ATOM')))
        self.assertEqual(structure.inspect_structure(data)['chains'][0]['residues'][0]['position'],
                         structure.inspect_structure(result['pdb'])['chains'][0]['residues'][0]['position'])

    def test_ambiguous_altloc_or_duplicate_atom_identity_is_rejected(self):
        data = pdb([('A', [1, 2])], alt='A').decode()
        first = data.splitlines()[0]
        other = first[:16] + 'B' + first[17:]
        for added in [first, other]:
            bad = (added + '\n' + data).encode()
            with self.subTest(added=added), self.assertRaisesRegex(ValueError, 'Duplicate atom|alternate conformer'):
                structure.normalize_structure(bad, chains=['A'])

    def test_multiple_models_and_atoms_outside_models_are_rejected(self):
        body = pdb([('A', [1, 2])]).decode().removesuffix('END\n')
        valid = ('MODEL        1\n' + body + 'ENDMDL\nEND\n').encode()
        self.assertEqual(structure.inspect_structure(valid)['chains'][0]['residue_count'], 2)
        for bad in [('MODEL        1\n' + body + 'ENDMDL\nMODEL        2\n' + body + 'ENDMDL\n').encode(),
                    ('MODEL        1\n' + body + 'ENDMDL\n' + body).encode(),
                    ('MODEL        1\n' + body).encode(),
                    (body + 'MODEL        1\n' + body).encode()]:
            with self.assertRaisesRegex(ValueError, 'model'):
                structure.inspect_structure(bad)
        bad = cif([('long', [1, 2])]).decode().splitlines()
        for i, line in enumerate(bad):
            if line.startswith('ATOM') and line.split()[7] == '2':
                bad[i] = line.rsplit(' ', 1)[0] + ' 2'
        with self.assertRaisesRegex(ValueError, 'models'):
            structure.inspect_structure(('\n'.join(bad) + '\n').encode())

    def test_mmcif_quoted_controls_multiline_metadata_and_label_fallback(self):
        metadata = "_entry.id 'loop_'\n_struct.title\n;This title has data_other and _atom_site.id\ninside text.\n;\n"
        data = cif([('long', [42, 43])], prelude=metadata, use_auth=False)
        inspected = structure.inspect_structure(data)
        self.assertEqual(inspected['format'], 'mmcif')
        self.assertEqual(inspected['chains'][0]['chain'], 'label_long')
        self.assertEqual([r['number'] for r in inspected['chains'][0]['residues']], [1, 2])

    def test_malformed_cif_does_not_silently_truncate_or_reinterpret_coordinates(self):
        valid = cif([('long', [1, 2])])
        variants = [valid + b'data_second\n', valid.replace(b'_atom_site.id\n', b'_atom_site.id\n_atom_site.id\n'),
                    valid.replace(b'ATOM 1 N N', b'ATOM 1 N'),
                    valid.replace(b'data_target\n', b'data_target\n_bad.value "unterminated\n'),
                    valid.replace(b'data_target\n', b'data_target\n_bad.value\n;unterminated\n'),
                    valid.replace(b'_atom_site.group_PDB', b'_other.group_PDB')]
        for data in variants:
            with self.subTest(data=data[:90]), self.assertRaises(ValueError):
                structure.inspect_structure(data)

    def test_conflicting_author_residue_chemistry_and_reused_identity_are_rejected(self):
        data = cif([('long', [1, 2])]).replace(b'1 ALA long N', b'1 GLY long N', 1)
        with self.assertRaisesRegex(ValueError, 'chemistry'):
            structure.inspect_structure(data)
        data = pdb([('A', [1, 2]), ('A', [1, 2])])
        with self.assertRaisesRegex(ValueError, 'Ambiguous original residue identity'):
            structure.inspect_structure(data)

    def test_numeric_coordinate_bounds_and_input_limits_fail_before_submission(self):
        source = pdb([('A', [1, 2])]).decode()
        for coordinate in ['     nan', '     inf', '-9999999']:
            bad = (source[:30] + coordinate + source[38:]).encode()
            with self.subTest(coordinate=coordinate), self.assertRaises(ValueError):
                structure.inspect_structure(bad)
        for data in [b'', b'\x00', b'\xff\xfe', 'not bytes']:
            with self.subTest(data=data), self.assertRaises(ValueError):
                structure.inspect_structure(data)
        with patch.object(structure, 'MAX_BYTES', 20), self.assertRaisesRegex(ValueError, '32 MiB'):
            structure.inspect_structure(source.encode())
        with patch.object(structure, 'MAX_ATOMS', 2), self.assertRaisesRegex(ValueError, 'atom limit'):
            structure.inspect_structure(source.encode())
        with patch.object(structure, 'MAX_RESIDUES', 1), self.assertRaisesRegex(ValueError, 'coordinate residues'):
            structure.inspect_structure(source.encode())

    def test_output_is_deterministic_and_passes_native_bundle_validation(self):
        data = pdb([('Z', [10, 11]), ('C', [51, 52])])
        one = structure.normalize_structure(data, chains=['Z', 'C'], hotspots=[ref('C', 52)])
        two = structure.normalize_structure(data, chains=['C', 'Z'], hotspots=[ref('C', 52)])
        self.assertEqual(one['pdb'], two['pdb'])
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bindcraft'))
        from bundle import validate_settings
        validate_settings(dict(binder_name='fixture', chains=one['chains'],
            target_hotspot_residues=one['hotspots'], lengths=[65, 65], number_of_final_designs=1), one['pdb'])


if __name__ == '__main__':
    unittest.main()
