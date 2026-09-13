"""Bounded, native-free target inspection and identity-preserving BindCraft input.

Coordinates are never repaired and residues are never substituted. Crops and
chain breaks become separate submitted chains rather than invented peptide bonds.
The mmCIF reader follows the wwPDB token/loop syntax, with a deliberately strict
single-coordinate-block/model subset:
https://mmcif.wwpdb.org/docs/tutorials/mechanics/pdbx-mmcif-syntax.html
"""
from __future__ import annotations

import math
from pathlib import Path
import re
import string

MAX_BYTES = 32 * 1024 * 1024
MAX_ATOMS = 250000
MAX_RESIDUES = 10000
PDB_CHAINS = string.ascii_uppercase + string.ascii_lowercase
AA = dict(zip('ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL'.split(),
              'ARNDCQEGHILKMFPSTWYV'))
SIDECHAINS = dict(zip(AA, [
    'CB', 'CB CG CD NE CZ NH1 NH2', 'CB CG OD1 ND2', 'CB CG OD1 OD2', 'CB SG',
    'CB CG CD OE1 NE2', 'CB CG CD OE1 OE2', '', 'CB CG ND1 CD2 CE1 NE2',
    'CB CG1 CG2 CD1', 'CB CG CD1 CD2', 'CB CG CD CE NZ', 'CB CG SD CE',
    'CB CG CD1 CD2 CE1 CE2 CZ', 'CB CG CD', 'CB OG', 'CB OG1 CG2',
    'CB CG CD1 CD2 NE1 CE2 CE3 CZ2 CZ3 CH2', 'CB CG CD1 CD2 CE1 CE2 CZ OH', 'CB CG1 CG2']))
BACKBONE = {'N', 'CA', 'C', 'O'}
MISSING = {'', '.', '?'}


def _require(value, message):
    if not value:
        raise ValueError(message)


def _integer(value, name):
    _require(isinstance(value, str) and re.fullmatch(r'[+-]?\d+', value) is not None,
             f'{name} must be an integer; preserve insertion codes separately')
    number = int(value)
    _require(-10000000 <= number <= 10000000, f'{name} is out of range')
    return number


def _float(value, name, default=None):
    if value in MISSING and default is not None:
        return default
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f'Invalid {name}') from exc
    _require(math.isfinite(number), f'Nonfinite {name}')
    return number


def _identity(chain, number, insertion):
    _require(isinstance(chain, str) and len(chain) <= 64
             and all(32 <= ord(c) < 127 for c in chain), 'Invalid original chain identifier')
    _require(type(number) is int and -10000000 <= number <= 10000000, 'Invalid residue number')
    _require(isinstance(insertion, str) and len(insertion) <= 8
             and all(c.isascii() and c.isalnum() for c in insertion), 'Invalid insertion code')
    return chain, number, insertion


def _ref(key):
    return dict(chain=key[0], number=key[1], insertion_code=key[2])


def _atom(*, chain, number, insertion, name, residue, position, record='ATOM',
          element='', alt='', occupancy=1.0, bfactor=0.0, segment='', label_number=None):
    key = _identity(chain, number, insertion)
    _require(isinstance(name, str) and 0 < len(name) <= 8
             and all(32 < ord(c) < 127 for c in name), 'Invalid atom name')
    _require(isinstance(residue, str) and 0 < len(residue) <= 16
             and all(32 < ord(c) < 127 for c in residue), 'Invalid residue name')
    _require(0 <= occupancy <= 1, 'Atom occupancy must be in 0..1')
    _require(-99.99 <= bfactor <= 999.99, 'Atom B factor cannot be represented in canonical PDB')
    _require(len(position) == 3 and all(math.isfinite(x) for x in position), 'Invalid coordinates')
    _require(all(len(f'{x:8.3f}') == 8 for x in position), 'Coordinates cannot be represented in canonical PDB')
    _require(len(alt) <= 8 and all(c.isascii() and c.isalnum() for c in alt), 'Invalid alternate-conformer identifier')
    _require(record in {'ATOM', 'HETATM'}, 'Unsupported coordinate record')
    return dict(key=key, name=name, residue=residue, position=position, record=record,
                element=element, alt=alt, occupancy=occupancy, bfactor=bfactor,
                segment=segment, label_number=label_number)


def _pdb_atoms(text):
    explicit = active = ended = False
    seen_atoms = False
    segments = {}
    last_chain = ''
    for line in text.splitlines():
        record = line[:6].strip()
        if record == 'MODEL':
            _require(not explicit and not seen_atoms, 'Multiple or ambiguous PDB models are unsupported')
            _require(_integer(line[10:14].strip(), 'PDB model number') > 0, 'PDB model number must be positive')
            explicit = active = True
        elif record == 'ENDMDL':
            _require(explicit and active, 'Unmatched PDB ENDMDL')
            active = False
        elif record == 'END':
            ended = True
        elif record == 'TER':
            chain = line[21:22] if len(line) > 21 and line[21] != ' ' else last_chain
            chain = '' if chain == ' ' else chain
            segments[chain] = segments.get(chain, 0) + 1
        elif record in {'ATOM', 'HETATM'}:
            _require(not ended and (not explicit or active), 'Coordinates outside the selected PDB model')
            _require(len(line) >= 54, 'Truncated PDB atom record')
            _integer(line[6:11].strip(), 'PDB atom serial')
            chain = line[21] if line[21] != ' ' else ''
            last_chain = chain
            seen_atoms = True
            yield _atom(chain=chain, number=_integer(line[22:26].strip(), 'PDB residue number'),
                insertion=line[26].strip(), name=line[12:16].strip(), residue=line[17:20].strip(),
                position=[_float(line[a:a + 8].strip(), 'PDB coordinate') for a in (30, 38, 46)],
                record=record, element=line[76:78].strip() if len(line) >= 78 else '',
                alt=line[16].strip(), occupancy=_float(line[54:60].strip(), 'occupancy', 1.0),
                bfactor=_float(line[60:66].strip(), 'B factor', 0.0), segment=segments.get(chain, 0))
    _require(not explicit or not active, 'Unterminated PDB model')


def _tokens(text):
    """Streaming CIF 1.x tokens, preserving quoted control-looking values."""
    i, size = 0, len(text)
    while i < size:
        if text[i].isspace():
            i += 1
            continue
        if text[i] == '#':
            end = text.find('\n', i)
            i = size if end < 0 else end + 1
            continue
        if text[i] == ';' and (i == 0 or text[i - 1] == '\n'):
            start = i + 1
            end = text.find('\n;', start)
            _require(end >= 0, 'Unterminated mmCIF multiline value')
            i = end + 2
            _require(i == size or text[i].isspace() or text[i] == '#', 'Malformed mmCIF multiline delimiter')
            token, quoted = text[start:end], True
        elif text[i] in "'\"":
            quote, start = text[i], i + 1
            i = start
            while i < size and not (text[i] == quote and (i + 1 == size or text[i + 1].isspace() or text[i + 1] == '#')):
                _require(text[i] not in '\r\n', 'Use semicolon text for multiline mmCIF values')
                i += 1
            _require(i < size, 'Unterminated mmCIF quoted value')
            token, quoted = text[start:i], True
            i += 1
        else:
            start = i
            while i < size and not text[i].isspace():
                i += 1
            token, quoted = text[start:i], False
        _require(len(token) <= 1024 * 1024, 'Oversized mmCIF token')
        yield token, quoted


def _control(token):
    value, quoted = token
    value = value.lower()
    return not quoted and (value.startswith(('_', 'data_', 'save_')) or value in {'loop_', 'stop_', 'global_'})


def _cif_atoms(text):
    iterator = iter(_tokens(text))
    token = next(iterator, None)
    blocks = 0
    seen_tags = set()
    atom_category = False
    scalar_atoms = {}
    model = None

    def atom(row):
        nonlocal model
        def field(name, fallback=None, default=None):
            value = row.get(name, '')
            if value in MISSING and fallback:
                value = row.get(fallback, '')
            if value in MISSING and default is not None:
                return default
            _require(value not in MISSING, f'Missing mmCIF atom_site.{name}')
            return value
        if row.get('auth_comp_id', '') not in MISSING and row.get('label_comp_id', '') not in MISSING:
            _require(row['auth_comp_id'] == row['label_comp_id'], 'Conflicting mmCIF author/label residue chemistry')
        current = _integer(field('pdbx_pdb_model_num', default='1'), 'mmCIF model number')
        _require(current > 0 and (model is None or current == model), 'Multiple or ambiguous mmCIF models are unsupported')
        model = current
        label_number = row.get('label_seq_id', '')
        if label_number in MISSING:
            label_number = None
        else:
            label_number = _integer(label_number, 'mmCIF label residue number')
        return _atom(chain=field('auth_asym_id', 'label_asym_id'),
            number=_integer(field('auth_seq_id', 'label_seq_id'), 'mmCIF author residue number'),
            insertion=field('pdbx_pdb_ins_code', default=''),
            name=field('auth_atom_id', 'label_atom_id'), residue=field('auth_comp_id', 'label_comp_id'),
            position=[_float(field('cartn_' + axis), 'mmCIF coordinate') for axis in 'xyz'],
            record=field('group_pdb'), element=field('type_symbol'),
            alt=field('label_alt_id', default=''),
            occupancy=_float(field('occupancy', default='1'), 'occupancy'),
            bfactor=_float(field('b_iso_or_equiv', default='0'), 'B factor'),
            segment=field('label_asym_id', 'auth_asym_id'), label_number=label_number)

    while token is not None:
        value, quoted = token
        lower = value.lower()
        if not quoted and lower.startswith('data_'):
            blocks += 1
            _require(blocks == 1 and len(value) > 5, 'Use a single named mmCIF data block')
            token = next(iterator, None)
        elif not quoted and lower == 'loop_':
            _require(blocks == 1, 'mmCIF coordinates require a named data block')
            columns = []
            token = next(iterator, None)
            while token and not token[1] and token[0].startswith('_'):
                tag = token[0].lower()
                _require(tag not in seen_tags and '.' in tag, 'Duplicate or invalid mmCIF loop column')
                seen_tags.add(tag)
                columns.append(tag)
                _require(len(columns) <= 512, 'Too many mmCIF loop columns')
                token = next(iterator, None)
            _require(columns, 'mmCIF loop has no columns')
            category = columns[0].split('.')[0]
            _require(all(tag.split('.')[0] == category for tag in columns), 'Mixed categories in mmCIF loop')
            is_atoms = category == '_atom_site'
            if is_atoms:
                _require(not atom_category and not scalar_atoms, 'Repeated mmCIF atom_site category')
                atom_category = True
            count = 0
            while token is not None and not _control(token):
                values = []
                for _ in columns:
                    _require(token is not None and not _control(token), 'Incomplete mmCIF loop row')
                    values.append(token[0])
                    token = next(iterator, None)
                count += 1
                if is_atoms:
                    yield atom({key.split('.', 1)[1]: value for key, value in zip(columns, values)})
            _require(count > 0, 'Empty mmCIF loop')
        elif not quoted and lower.startswith('_'):
            _require(blocks == 1 and '.' in lower and lower not in seen_tags, 'Duplicate or invalid mmCIF scalar tag')
            seen_tags.add(lower)
            token = next(iterator, None)
            _require(token is not None and not _control(token), 'mmCIF tag has no value')
            if lower.startswith('_atom_site.'):
                _require(not atom_category, 'Repeated mmCIF atom_site category')
                scalar_atoms[lower.split('.', 1)[1]] = token[0]
            token = next(iterator, None)
        else:
            raise ValueError('Unsupported or misplaced mmCIF control/value token')
    if scalar_atoms:
        yield atom(scalar_atoms)


def _parse(data, filename):
    _require(type(data) is bytes and 0 < len(data) <= MAX_BYTES, 'Target must contain 1 byte to 32 MiB of PDB or mmCIF')
    _require(isinstance(filename, str), 'Target filename must be text')
    try:
        text = data.decode('utf-8')
    except UnicodeError as exc:
        raise ValueError('Target is not plain-text PDB or mmCIF') from exc
    _require('\x00' not in text, 'NUL in target structure')
    first = next((line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')), '')
    cif = Path(filename).suffix.lower() in {'.cif', '.mmcif'} or first.lower().startswith('data_')
    if not cif:
        _require(text.isascii(), 'PDB must be ASCII')
    chains = {}
    residues = {}
    atom_count = 0
    for item in (_cif_atoms(text) if cif else _pdb_atoms(text)):
        atom_count += 1
        _require(atom_count <= MAX_ATOMS, 'Target exceeds the atom limit')
        key = item['key']
        if key not in residues:
            _require(len(residues) < MAX_RESIDUES, 'Target exceeds 10000 coordinate residues')
            residue = dict(key=key, name=item['residue'], atoms=[], segment=item['segment'],
                           label_number=item['label_number'])
            residues[key] = residue
            chains.setdefault(key[0], []).append(residue)
        residue = residues[key]
        _require(residue['name'] == item['residue'] and residue['segment'] == item['segment']
                 and residue['label_number'] == item['label_number'], 'Ambiguous original residue identity or reused chain segment')
        residue['atoms'].append(item)
    _require(residues, 'Target contains no coordinate residues')
    return dict(format='mmcif' if cif else 'pdb', chains=chains, residues=residues, atom_count=atom_count)


def _issues(residue):
    atoms = residue['atoms']
    name = residue['name']
    issues = []
    if name not in AA or any(atom['record'] != 'ATOM' for atom in atoms):
        return ['Selected residues must be canonical polymer amino acids (ATOM records)']
    names = [atom['name'] for atom in atoms]
    if len(names) != len(set(names)):
        issues.append('Duplicate atom identities or alternate conformers')
    if len({atom['alt'] for atom in atoms if atom['alt']}) > 1:
        issues.append('Ambiguous alternate conformers')
    missing = BACKBONE - set(names)
    if missing:
        issues.append('Missing backbone atoms: ' + ', '.join(sorted(missing)))
    allowed = BACKBONE | {'OXT'} | set(SIDECHAINS[name].split())
    for atom in atoms:
        atom_name = atom['name']
        hydrogen = re.fullmatch(r'[123]?H[A-Z0-9]{0,3}', atom_name) is not None
        expected_element = 'H' if hydrogen else atom_name[0]
        if (atom_name not in allowed and not hydrogen) or len(atom_name) > 4:
            issues.append('Unsupported atom name: ' + atom_name)
        if atom['element'] and atom['element'] != expected_element:
            issues.append('Atom element disagrees with canonical protein chemistry: ' + atom_name)
        if atom['occupancy'] <= 0:
            issues.append('Zero-occupancy atoms are not usable target coordinates')
    return list(dict.fromkeys(issues))


def _inspection(parsed):
    chains = []
    warnings = []
    for chain, residues in parsed['chains'].items():
        values = []
        for residue in residues:
            issues = _issues(residue)
            atoms = residue['atoms']
            ca = next((atom for atom in atoms if atom['name'] == 'CA'), atoms[0])
            alternates = sorted({atom['alt'] for atom in atoms if atom['alt']})
            if issues:
                warnings.append(f'{chain or "(blank)"}:{residue["key"][1]}{residue["key"][2]}: ' + '; '.join(issues))
            elif alternates:
                warnings.append(f'{chain or "(blank)"}:{residue["key"][1]}{residue["key"][2]} has one explicit conformer {alternates[0]}; its coordinates are retained')
            values.append(dict(_ref(residue['key']), name=residue['name'],
                amino_acid=AA.get(residue['name'], 'X'), position=ca['position'], supported=not issues, issues=issues))
        chains.append(dict(chain=chain, residue_count=len(values),
            sequence=''.join(value['amino_acid'] for value in values), residues=values,
            supported=all(value['supported'] for value in values)))
    return dict(format=parsed['format'], chains=chains, warnings=warnings, atom_count=parsed['atom_count'])


def inspect_structure(data: bytes, filename: str = 'target.pdb'):
    return _inspection(_parse(data, filename))


def _references(values, name):
    _require(isinstance(values, list) and len(values) <= MAX_RESIDUES, f'{name} must be a bounded residue-reference list')
    result = []
    for value in values:
        _require(isinstance(value, dict) and set(value) == {'chain', 'number', 'insertion_code'}, f'Invalid {name} residue reference')
        result.append(_identity(value['chain'], value['number'], value['insertion_code']))
    _require(len(result) == len(set(result)), f'Duplicate {name} residue reference')
    return result


def _connected(previous, current):
    if previous['segment'] != current['segment']:
        return False
    left, right = previous['label_number'], current['label_number']
    if left is not None and right is not None and right != left + 1:
        return False
    carbon = next(atom['position'] for atom in previous['atoms'] if atom['name'] == 'C')
    nitrogen = next(atom['position'] for atom in current['atoms'] if atom['name'] == 'N')
    return 0.8 <= math.dist(carbon, nitrogen) <= 2.2


def normalize_structure(data: bytes, *, filename: str = 'target.pdb', chains: list[str],
                        crop: list[dict] | None = None, hotspots: list[dict] | None = None):
    parsed = _parse(data, filename)
    inspection = _inspection(parsed)
    _require(isinstance(chains, list) and 1 <= len(chains) <= len(PDB_CHAINS)
             and all(isinstance(chain, str) for chain in chains), 'Select 1 to 52 original protein chains')
    _require(len(chains) == len(set(chains)), 'Duplicate selected chain')
    _require(set(chains) <= set(parsed['chains']), 'Selected chain is absent from the target')
    selected = {residue['key'] for chain in chains for residue in parsed['chains'][chain]}
    if crop is not None:
        requested = _references(crop, 'crop')
        _require(requested, 'An explicit crop cannot be empty')
        _require(set(requested) <= selected, 'Crop residue is absent from its selected original chain')
        selected = set(requested)
    requested_hotspots = _references([] if hotspots is None else hotspots, 'hotspot')
    _require(set(requested_hotspots) <= selected, 'Hotspot residue is absent from the selected/cropped target')
    _require(selected, 'No target residues selected')
    for key in selected:
        issues = _issues(parsed['residues'][key])
        _require(not issues, f'Unsupported selected residue {key}: ' + '; '.join(issues))

    fragments = []
    for chain, residues in parsed['chains'].items():
        if chain not in chains:
            continue
        previous = None
        fragment = None
        for residue in residues:
            if residue['key'] not in selected:
                previous = None
                continue
            if previous is None or not _connected(previous, residue):
                fragment = []
                fragments.append(fragment)
            fragment.append(residue)
            previous = residue
    _require(len(fragments) <= len(PDB_CHAINS), 'Crop/chain breaks exceed 52 submitted chain fragments')
    _require(all(2 <= len(fragment) <= 9999 for fragment in fragments), 'Every submitted chain fragment needs 2 to 9999 complete residues')
    _require(sum(len(residue['atoms']) for fragment in fragments for residue in fragment) + len(fragments) <= 99999,
             'Selected atom serials exceed canonical PDB capacity')
    mapping, submitted, lines = [], {}, []
    serial = 1
    for chain_id, fragment in zip(PDB_CHAINS, fragments):
        for number, residue in enumerate(fragment, 1):
            new_ref = dict(chain=chain_id, number=number, insertion_code='')
            submitted[residue['key']] = new_ref
            mapping.append(dict(original=_ref(residue['key']), submitted=new_ref, name=residue['name'], amino_acid=AA[residue['name']]))
            for atom in residue['atoms']:
                name = atom['name']
                field = f'{name:<4}' if len(name) == 4 or name[0].isdigit() else f' {name:<3}'
                element = atom['element'] or ('H' if re.fullmatch(r'[123]?H[A-Z0-9]{0,3}', name) else name[0])
                x, y, z = atom['position']
                lines.append(f'ATOM  {serial:5d} {field} {residue["name"]:>3} {chain_id}{number:4d}    '
                    f'{x:8.3f}{y:8.3f}{z:8.3f}{atom["occupancy"]:6.2f}{atom["bfactor"]:6.2f}          {element:>2}  ')
                serial += 1
        lines.append(f'TER   {serial:5d}      {fragment[-1]["name"]:>3} {chain_id}{len(fragment):4d}')
        serial += 1
    lines.append('END')
    result = ('\n'.join(lines) + '\n').encode('ascii')
    _require(len(result) <= MAX_BYTES, 'Normalized target exceeds 32 MiB')
    native_hotspots = ','.join(f'{submitted[key]["chain"]}{submitted[key]["number"]}' for key in requested_hotspots) or None
    warnings = list(inspection['warnings'])
    if len(fragments) > len({key[0] for key in selected}):
        warnings.append('Disjoint crops or coordinate chain breaks are separate submitted chain fragments')
    return dict(pdb=result, residue_map=mapping, hotspots=native_hotspots,
                chains=','.join(PDB_CHAINS[:len(fragments)]), inspection=inspection, warnings=warnings)
