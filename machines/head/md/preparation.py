"""Canonical PDB preparation with explicit chemistry choices and no dynamics.

This helper does not parameterize modified residues, infer protonation from pH,
repair missing heavy atoms, or establish stability/binding. Native references:
https://manual.gromacs.org/current/onlinehelp/gmx-pdb2gmx.html
https://manual.gromacs.org/current/onlinehelp/gmx-grompp.html
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from .protocols import topology_charge_report

PROTEIN = set('ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL'.split())
HISTIDINES = {'HID', 'HIE', 'HIP'}
DECISIONS = {'protein_termini': 'charged', 'nucleic_termini': '5prime_OH_3prime_OH',
             'side_chains': 'standard_charged', 'hydrogens': 'rebuild', 'disulfides': 'none'}
LIMITATIONS = [
    'Preparation adds explicit hydrogens, selected terminal groups, solvent and ions; no dynamics or minimization was run.',
    'pH and the review are provenance: no pKa calculation or automatic protonation-state inference was performed.',
    'Only canonical residues with the explicitly selected supported terminal/protonation states are admitted.',
    'Modified residues, synthetic amidites, cofactors, alternative caps and disulfide-bonded systems need externally prepared, validated parameters.',
    'Preserving coordinates and passing grompp do not establish physical accuracy, stereochemical correctness, equilibration or affinity.',
]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024**2), b''):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n')


def atom_name(name):
    name = name.replace('*', "'")
    return {'O1P': 'OP1', 'O2P': 'OP2', 'OT1': 'O', 'OT2': 'OXT', 'OC1': 'O', 'OC2': 'OXT'}.get(name, name)


def element(name, declared=''):
    inferred = re.sub(r'^[0-9]+', '', name)[0].upper()
    require(inferred in 'HCNOSP', 'Noncanonical element/atom name requires external preparation: ' + name)
    require(not declared or declared.upper() == inferred, 'Atom name and declared element disagree: ' + name)
    return inferred


def canonical_residue(name):
    if name in PROTEIN or name in HISTIDINES:
        return ('HIS' if name in HISTIDINES else name), 'protein'
    if re.fullmatch(r'D[ACGT](?:[35N])?', name):
        return name[:2], 'DNA'
    if re.fullmatch(r'R?[ACGU](?:[35N])?', name):
        return name.removeprefix('R')[0], 'RNA'
    raise ValueError('Unsupported/modified residue ' + name + '; provide an externally prepared topology and validated parameter manifest')


def read_pdb(data):
    require(len(data) <= 50 * 1024**2, 'PDB input exceeds 50 MiB')
    try:
        lines = data.decode('ascii').splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError('A fixed-column ASCII PDB is required') from exc
    atoms, models = [], 0
    segment = 0
    for line_number, raw in enumerate(lines, 1):
        tag = raw[:6].strip()
        if tag == 'MODEL':
            models += 1
        elif tag == 'TER':
            segment += 1
        if tag not in {'ATOM', 'HETATM'}:
            continue
        line = raw.ljust(80)
        try:
            serial, resid = int(line[6:11]), int(line[22:26])
            xyz = [float(line[i:i + 8]) for i in (30, 38, 46)]
            occupancy = float(line[54:60]) if line[54:60].strip() else 1.0
        except ValueError as exc:
            raise ValueError(f'Malformed PDB atom at line {line_number}') from exc
        require(all(math.isfinite(x) for x in xyz + [occupancy]), 'Nonfinite PDB coordinates/occupancy')
        name = line[12:16].strip()
        require(name, 'Unnamed PDB atom')
        atoms.append({'serial': serial, 'name': name, 'resname': line[17:20].strip(),
                      'chain': line[21], 'resid': resid, 'icode': line[26].strip(),
                      'altloc': line[16].strip(), 'occupancy': occupancy, 'xyz_angstrom': xyz,
                      'element': line[76:78].strip(), 'segment': segment,
                      'record': tag, 'line': line, 'line_number': line_number})
    require(models <= 1, 'Multiple PDB models require an explicit external model selection')
    require(0 < len(atoms) <= 60000, 'PDB must contain 1..60000 selected/input atoms')
    return atoms, lines


def inspect_pdb(data, decisions, chains=None):
    """Validate fixed chain selection and molecular identity before native tools."""
    require(isinstance(decisions, dict), 'chemistry-decisions must be a JSON object')
    require(set(decisions) == set(DECISIONS) | {'histidines'}, 'Provide all explicit chemistry decisions and histidines (possibly {})')
    for key, supported in DECISIONS.items():
        require(decisions[key] == supported, f'{key} must explicitly be {supported!r}; other chemistry requires external preparation')
    require(isinstance(decisions['histidines'], dict), 'histidines must map exact chain:resid[icode] keys to HID/HIE/HIP')
    atoms, lines = read_pdb(data)
    available = list(dict.fromkeys(atom['chain'] for atom in atoms))
    if chains is None:
        chosen = available
    else:
        require(isinstance(chains, list) and chains and len(set(chains)) == len(chains), 'chains must be a nonempty unique fixed chain-ID list')
        require(all(isinstance(c, str) and len(c) == 1 and c in available for c in chains), 'Every selected chain ID must occur in the PDB')
        chosen = [c for c in available if c in chains]
    selected = [atom for atom in atoms if atom['chain'] in chosen]
    selected_serials = {atom['serial'] for atom in selected}
    for line in lines:
        tag = line[:6].strip()
        if tag == 'MODRES' and line[16:17] in chosen:
            raise ValueError('Selected MODRES chemistry requires external prepared parameters')
        if tag == 'SSBOND' and any(line[i:i + 1] in chosen for i in (15, 29)):
            raise ValueError('An explicit SSBOND requires externally prepared disulfide topology')
        if tag == 'LINK' and any(line[i:i + 1] in chosen for i in (21, 51)):
            raise ValueError('An explicit LINK requires external preparation and bond validation')
        if tag == 'CONECT':
            try:
                linked = {int(line[i:i + 5]) for i in range(6, len(line), 5) if line[i:i + 5].strip()}
            except ValueError as exc:
                raise ValueError('Malformed PDB connectivity record') from exc
            require(not linked & selected_serials,
                    'Explicit selected-atom CONECT bonds require external topology review; they are not silently discarded')
    residues, seen = [], set()
    current = None
    for atom in selected:
        require(not atom['altloc'], 'Alternate atom locations require explicit external conformer selection')
        require(atom['occupancy'] in {0.0, 1.0}, 'Partial occupancies require explicit external conformer review/selection')
        key = (atom['chain'], atom['resid'], atom['icode'])
        if key != current:
            require(key not in seen, 'Repeated/noncontiguous residue identity')
            seen.add(key); current = key
            canonical, polymer = canonical_residue(atom['resname'])
            residues.append({'chain': key[0], 'resid': key[1], 'icode': key[2], 'source_resname': atom['resname'],
                             'canonical_resname': canonical, 'polymer': polymer, 'segment': atom['segment'], 'atoms': []})
        residue = residues[-1]
        require(residue['source_resname'] == atom['resname'], 'Multiple chemical identities in one residue')
        normalized = atom_name(atom['name'])
        atom = {**atom, 'normalized_name': normalized, 'element': element(normalized, atom['element'])}
        require(normalized not in {a['normalized_name'] for a in residue['atoms']}, 'Duplicate atom name in selected residue')
        residue['atoms'].append(atom)
    histidine_keys = set()
    for chain in chosen:
        chain_residues = [r for r in residues if r['chain'] == chain]
        require(len({r['segment'] for r in chain_residues}) == 1, 'A chain ID split by TER needs distinct external chain IDs')
        require(len({r['polymer'] for r in chain_residues}) == 1, 'Mixed polymer types within one chain require external preparation')
        require(len(chain_residues) >= 2, 'Single-residue chains require explicit externally prepared termini')
        for previous, following in zip(chain_residues, chain_residues[1:]):
            a, b = previous['resid'], following['resid']
            same_with_insertion = a == b and bool(following['icode']) and following['icode'] > previous['icode']
            require(b == a + 1 or same_with_insertion, 'Residue numbering gap/reversal requires external completeness review')
            endpoints = ('C', 'N') if previous['polymer'] == 'protein' else ("O3'", 'P')
            left = {atom['normalized_name']: atom for atom in previous['atoms']}
            right = {atom['normalized_name']: atom for atom in following['atoms']}
            require(endpoints[0] in left and endpoints[1] in right, 'Missing polymer linkage atom; no reconstruction is performed')
            distance = math.dist(left[endpoints[0]]['xyz_angstrom'], right[endpoints[1]]['xyz_angstrom'])
            require(0.9 < distance < 2.1, 'Broken/unphysical inter-residue linkage; no missing segment is reconstructed')
        for index, residue in enumerate(chain_residues):
            if residue['canonical_resname'] == 'HIS':
                key = f"{chain.strip() or '_'}:{residue['resid']}{residue['icode']}"
                histidine_keys.add(key)
                state = decisions['histidines'].get(key)
                require(state in HISTIDINES, 'Explicit HID/HIE/HIP decision required for ' + key)
                if residue['source_resname'] in HISTIDINES:
                    require(residue['source_resname'] == state, 'Declared histidine state conflicts with input identity: ' + key)
                residue['native_resname'] = state
            else:
                residue['native_resname'] = residue['canonical_resname']
            residue['terminal_position'] = 'first' if index == 0 else 'last' if index == len(chain_residues) - 1 else 'internal'
            if residue['polymer'] != 'protein' and index == 0:
                require('P' not in {a['normalized_name'] for a in residue['atoms']}, '5-prime phosphate conflicts with chosen hydroxyl termini; external preparation required')
            suffix = residue['source_resname'][-1:]
            if residue['polymer'] != 'protein' and suffix in {'3', '5', 'N'}:
                require((suffix == '5' and index == 0) or (suffix == '3' and index == len(chain_residues) - 1),
                        'Native nucleotide terminal label conflicts with selected chain endpoint')
    require(set(decisions['histidines']) == histidine_keys, 'Histidine decisions must match all and only selected HIS residues')
    sulfurs = [a for r in residues if r['canonical_resname'] == 'CYS' for a in r['atoms'] if a['normalized_name'] == 'SG']
    require(not any(math.dist(a['xyz_angstrom'], b['xyz_angstrom']) < 3.0
                    for index, a in enumerate(sulfurs) for b in sulfurs[index + 1:]),
            'Nearby cysteine sulfurs could form an automatic disulfide; external explicit topology is required')
    for chain in chosen:
        group = [r for r in residues if r['chain'] == chain]
        if group[0]['polymer'] == 'protein':
            first = {a['normalized_name']: a for a in group[0]['atoms']}
            last = {a['normalized_name']: a for a in group[-1]['atoms']}
            require('N' in first and 'C' in last, 'Missing terminal backbone atom')
            require(math.dist(first['N']['xyz_angstrom'], last['C']['xyz_angstrom']) > 3.0,
                    'Possible cyclic chain conflicts with explicit charged termini')
    return {'source_atom_count': len(atoms), 'selected_atom_count': len(selected), 'selected_chains': chosen,
            'excluded_chains': [c for c in available if c not in chosen],
            'excluded_atom_count': len(atoms) - len(selected), 'residues': residues,
            'source_hydrogens_rebuilt': sum(a['element'] == 'H' for r in residues for a in r['atoms'])}


def _templates(force_field):
    templates, mappings = {}, {}
    for stem in ('aminoacids', 'dna', 'rna'):
        path = force_field / (stem + '.rtp')
        if not path.is_file():
            continue
        residue, section = None, None
        for raw in path.read_text().splitlines():
            line = raw.split(';', 1)[0].strip()
            if not line:
                continue
            header = re.fullmatch(r'\[\s*([^\]]+?)\s*\]', line)
            if header:
                name = header[1].strip()
                if name.lower() in {'bondedtypes', 'atoms', 'bonds', 'angles', 'dihedrals', 'impropers', 'exclusions', 'cmap'}:
                    section = name.lower()
                else:
                    residue, section = name, None
                    templates[residue] = {}
            elif section == 'atoms' and residue:
                fields = line.split()
                name = atom_name(fields[0])
                require(name not in templates[residue], 'Duplicate atom in installed residue template')
                # Unselected water/ion/virtual-site entries may share the RTP.
                # Element admission is applied to the selected polymer below.
                templates[residue][name] = {'charge_e': float(fields[2]), 'atom_type': fields[1],
                                           'element': re.sub(r'^[0-9]+', '', name)[0].upper()}
        mapping = force_field / (stem + '.r2b')
        if mapping.is_file():
            for raw in mapping.read_text().splitlines():
                fields = raw.split(';', 1)[0].split()
                if len(fields) >= 5:
                    mappings[fields[0]] = fields[1:5]
    return templates, mappings


def validate_templates(audit, force_field):
    """Require a full installed residue/end-group template; do not invent atoms."""
    templates, mappings = _templates(force_field)
    for residue in audit['residues']:
        name = residue['native_resname']
        position = residue['terminal_position']
        if name in mappings:
            template = mappings[name][{'internal': 0, 'first': 1, 'last': 2}[position]]
        elif residue['polymer'] == 'protein':
            template = ('' if position == 'internal' else 'N' if position == 'first' else 'C') + name
        else:
            template = ('R' if residue['polymer'] == 'RNA' else '') + name + ('' if position == 'internal' else '5' if position == 'first' else '3')
        require(template in templates, f'Installed force field lacks explicit supported template {template}; external preparation required')
        definition = templates[template]
        require(all(a['element'] in 'HCNOSP' for a in definition.values()),
                'Selected template contains unsupported noncanonical/virtual atoms')
        expected_charge = {'ARG': 1, 'LYS': 1, 'ASP': -1, 'GLU': -1, 'HIP': 1}.get(name, 0)
        template_charge = sum(a['charge_e'] for a in definition.values())
        if residue['polymer'] == 'protein':
            expected_charge += 1 if position == 'first' else -1 if position == 'last' else 0
            require(abs(template_charge - expected_charge) < 1e-3,
                    'Installed residue template charge differs from explicit protonation/terminus decision: ' + template)
        else:
            # Nucleic FFs split terminal hydroxyl/phosphate partial charges
            # across residue boundaries. Validate the complete chain below.
            expected_charge = template_charge
        actual = {a['normalized_name'] for a in residue['atoms'] if a['element'] != 'H'}
        expected = {n for n, a in definition.items() if a['element'] != 'H'}
        permitted_addition = {'OXT'} if residue['polymer'] == 'protein' and position == 'last' else set()
        require(not actual - expected, f'Unexpected heavy atoms in {residue["chain"]}:{residue["resid"]}; modified chemistry is not canonicalized: {sorted(actual - expected)}')
        require(not (expected - actual - permitted_addition), f'Missing heavy atoms in {residue["chain"]}:{residue["resid"]}: {sorted(expected - actual - permitted_addition)}')
        residue['template'] = template
        residue['expected_charge_e'] = expected_charge
        residue['expected_heavy_names'] = sorted(expected)
        residue['expected_atom_count'] = len(definition)
        residue['expected_template_atoms'] = definition
    for chain in audit['selected_chains']:
        group = [r for r in audit['residues'] if r['chain'] == chain]
        if group[0]['polymer'] != 'protein':
            require(abs(sum(r['expected_charge_e'] for r in group) + len(group) - 1) < 1e-3,
                    'Nucleic terminal templates do not produce the declared hydroxyl-ended chain charge')
    return audit


def _selected_pdb(audit):
    lines = ['TITLE     Explicitly selected canonical preparation input']
    last_chain = None
    serial = 0
    for residue in audit['residues']:
        if last_chain is not None and residue['chain'] != last_chain:
            lines.append('TER')
        for atom in residue['atoms']:
            if atom['element'] == 'H':
                continue
            serial += 1
            line = atom['line']
            line = 'ATOM  ' + f'{serial:5d}' + line[11:12] + f'{atom["normalized_name"]:>4}' + ' ' + f'{residue["native_resname"]:>3}' + line[20:]
            lines.append(line)
        last_chain = residue['chain']
    return '\n'.join(lines + ['TER', 'END', ''])


def audit_native(audit, ordered_pdb):
    atoms, _ = read_pdb(Path(ordered_pdb).read_bytes())
    groups = []
    identity = None
    for atom in atoms:
        key = atom['chain'], atom['resid'], atom['icode']
        if key != identity:
            groups.append([]); identity = key
        groups[-1].append(atom)
    require(len(groups) == len(audit['residues']), 'Native preparation dropped, merged or added polymer residues')
    report = []
    for source, native in zip(audit['residues'], groups):
        require(native[0]['chain'] == source['chain'], 'Native preparation changed chain identity')
        names = {atom_name(a['name']): a for a in native}
        require(len(names) == len(native), 'Native preparation duplicated an atom name')
        actual_heavy = {name for name, atom in names.items() if element(name, atom['element']) != 'H'}
        require(actual_heavy == set(source['expected_heavy_names']), 'Native heavy atom coverage differs from admitted template')
        require(len(native) == source['expected_atom_count'], 'Native protonation atom count differs from selected template')
        hydrogen_names = {name for name in names if element(name) == 'H'}
        # Histidine ring protons distinguish tautomers with identical net charge.
        if source['canonical_resname'] == 'HIS':
            wanted = {'HID': {'HD1'}, 'HIE': {'HE2'}, 'HIP': {'HD1', 'HE2'}}[source['native_resname']]
            require(hydrogen_names & {'HD1', 'HE2'} == wanted, 'Native histidine tautomer differs from explicit decision')
        mapping, retained_names = [], set()
        for atom in source['atoms']:
            if atom['element'] == 'H':
                continue
            alternatives = [atom['normalized_name']]
            if source['polymer'] == 'protein' and source['terminal_position'] == 'last' and atom['normalized_name'] in {'O', 'OXT'}:
                definitions = source['expected_template_atoms']
                if abs(definitions['O']['charge_e'] - definitions['OXT']['charge_e']) < 1e-6:
                    alternatives = ['O', 'OXT']  # Equivalent carboxylate oxygens may be relabelled.
            matched = [name for name in alternatives if name not in retained_names and
                       math.dist(atom['xyz_angstrom'], names[name]['xyz_angstrom']) <= 0.006]
            require(len(matched) == 1, 'Native preparation moved or ambiguously mapped a supplied heavy atom before box placement')
            retained_names.add(matched[0])
            observed = names[matched[0]]
            mapping.append({'source_serial': atom['serial'], 'source_name': atom['name'],
                            'prepared_serial': observed['serial'], 'prepared_name': observed['name']})
        report.append({'source_chain': source['chain'], 'source_resid': source['resid'], 'source_icode': source['icode'],
                       'source_resname': source['source_resname'], 'prepared_chain': native[0]['chain'],
                       'prepared_resid': native[0]['resid'], 'prepared_resname': native[0]['resname'],
                       'template': source['template'], 'charge_e': source['expected_charge_e'],
                       'heavy_atom_mapping': mapping, 'added_heavy_atoms': sorted(actual_heavy - retained_names),
                       'prepared_hydrogen_count': len(native) - len(actual_heavy)})
    return report, len(atoms)


def _environment(runtime, force_field, water_model):
    runtime = Path(runtime).resolve(strict=True)
    manifest = json.loads((runtime / 'manifest.json').read_text())
    require(manifest.get('schema') == 'bio-md-runtime.v1' and manifest.get('cpu_smoke', {}).get('passed') is True,
            'A qualified bio-md runtime with passing CPU smoke is required')
    require(re.fullmatch(r'[A-Za-z0-9_.-]+', force_field) is not None and force_field not in {'.', '..'}, 'Invalid force-field name')
    require(water_model in {'tip3p', 'spc', 'spce'}, 'This preparation helper currently supports explicit three-site TIP3P/SPC/SPC-E water only')
    libraries = [*runtime.glob('lib/python*/site-packages/pmx/data/mutff'), runtime / 'share/gromacs/top']
    candidates = [library / (force_field + '.ff') for library in libraries if (library / (force_field + '.ff/forcefield.itp')).is_file()]
    require(candidates, 'Requested force field is not installed in this pinned runtime')
    force = candidates[0]
    require((force / (water_model + '.itp')).is_file(), 'Requested water model is not provided by this force field')
    env = os.environ.copy()
    env.update(PATH=str(runtime / 'bin') + os.pathsep + env.get('PATH', ''), GMXLIB=str(force.parent),
               LD_LIBRARY_PATH=str(runtime / 'lib') + (':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else ''),
               GMX_MAXBACKUP='-1', OMP_NUM_THREADS='1', PYTHONNOUSERSITE='1')
    env.pop('PYTHONPATH', None)
    return runtime, force, env, manifest


def prepare(input_pdb, out_dir, *, runtime_prefix, force_field, water_model, salt_molar,
            box_margin_nm, temperature_kelvin, ph, protonation_review, chemistry_decisions,
            chains=None, ion_seed=42):
    for name, value, low, high in [('salt_molar', salt_molar, 0, 2), ('box_margin_nm', box_margin_nm, 1.2, 10),
                                  ('temperature_kelvin', temperature_kelvin, 250, 400), ('ph', ph, 0, 14)]:
        require(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and low <= value <= high,
                f'{name} must be finite in {low}..{high}')
    require(isinstance(protonation_review, str) and 12 <= len(protonation_review.strip()) <= 8192,
            'A concrete protonation/terminus review of 12..8192 characters is required')
    require(type(ion_seed) is int and 0 <= ion_seed < 2**31, 'ion_seed must be a fixed nonnegative 31-bit integer')
    source = Path(input_pdb)
    require(source.is_file() and not source.is_symlink(), 'Input must be an ordinary PDB file')
    data = source.read_bytes()
    root = Path(out_dir).absolute()
    require(not root.exists(), 'Output directory already exists; preserve the earlier preparation and choose a new directory')
    root.mkdir(parents=True, mode=0o700)
    (root / 'source.pdb').write_bytes(data)
    receipt = {'schema': 'bio-md-preparation.v1', 'state': 'preparing', 'source_sha256': hashlib.sha256(data).hexdigest(),
               'source_filename': source.name, 'chemistry_decisions': chemistry_decisions,
               'conditions': {'force_field': force_field, 'water_model': water_model, 'temperature_kelvin': temperature_kelvin,
                              'ionic_strength_molar': salt_molar, 'protonation': {'method': 'explicit operator review', 'pH': ph,
                                'description': protonation_review}}, 'box_margin_nm': box_margin_nm, 'ion_seed': ion_seed,
               'commands': [], 'limitations': LIMITATIONS, 'molecular_dynamics_executed': False, 'paid_compute_requested': False}
    write_json(root / 'receipt.json', receipt)
    try:
        audit = inspect_pdb(data, chemistry_decisions, chains)
        runtime, ff, env, manifest = _environment(runtime_prefix, force_field, water_model)
        validate_templates(audit, ff)
        receipt['runtime'] = {'prefix': str(runtime), 'manifest_sha256': sha(runtime / 'manifest.json'), 'fingerprint': manifest['fingerprint']}
        receipt['force_field_files'] = {str(p.relative_to(ff)): sha(p) for p in sorted(ff.rglob('*')) if p.is_file()}
        receipt['input_selection'] = {k: v for k, v in audit.items() if k != 'residues'}
        native = root / 'native'; native.mkdir()
        logs = root / 'logs'; logs.mkdir()
        (native / 'selected.pdb').write_text(_selected_pdb(audit))
        def run(name, args, stdin=''):
            command = {'step': name, 'argv': [str(runtime / 'bin/gmx'), *args], 'stdin': stdin,
                       'started_at': time.time(), 'log': 'logs/' + name + '.log'}
            receipt['commands'].append(command); write_json(root / 'receipt.json', receipt)
            with (root / command['log']).open('wb') as log:
                result = subprocess.run(command['argv'], cwd=native, env=env, input=stdin.encode(),
                                        stdout=log, stderr=subprocess.STDOUT, timeout=300, check=False)
            command.update(exit_code=result.returncode, finished_at=time.time(), log_sha256=sha(root / command['log']))
            output = (root / command['log']).read_text(errors='replace')
            require(result.returncode == 0, 'Native preparation failed at ' + name + ': ' + output[-5000:])
            if args[0] == 'grompp':
                require(not re.search(r'^\s*WARNING(?:\s|\[|:|$)', output, re.M), 'Native grompp warning at ' + name)
            write_json(root / 'receipt.json', receipt)
        run('pdb2gmx', ['pdb2gmx', '-f', 'selected.pdb', '-o', 'ordered.pdb', '-p', 'system.top',
                       '-ff', force_field, '-water', water_model, '-ignh', '-nomissing', '-chainsep', 'id_or_ter', '-merge', 'no', '-renum', '-rtpres', 'no'])
        native_audit, solute_atoms = audit_native(audit, native / 'ordered.pdb')
        receipt['residue_audit'] = native_audit
        receipt['solute_atom_count'] = solute_atoms
        run('box', ['editconf', '-f', 'ordered.pdb', '-o', 'box.gro', '-c', '-d', str(box_margin_nm), '-bt', 'cubic'])
        run('solvate', ['solvate', '-cp', 'box.gro', '-cs', 'spc216.gro', '-p', 'system.top', '-o', 'solvated.gro'])
        mdp = ('integrator = steep\nnsteps = 0\nemtol = 1000\nemstep = 0.01\ncutoff-scheme = Verlet\n'
               'coulombtype = Cut-off\nrcoulomb = 1.2\nrvdw = 1.2\npbc = xyz\nconstraints = none\n')
        (native / 'ions.mdp').write_text(mdp)
        run('ions-grompp', ['grompp', '-f', 'ions.mdp', '-c', 'solvated.gro', '-p', 'system.top', '-o', 'ions.tpr', '-pp', 'ions-processed.top'])
        before = topology_charge_report(native / 'ions-processed.top')
        expected_charge = sum(r['expected_charge_e'] for r in audit['residues'])
        require(abs(before['charge_a_e'] - expected_charge) < 1e-3, 'Native total charge differs from explicitly chosen residue chemistry')
        require(before['hybrid_atoms'] == 0, 'Canonical preparation unexpectedly generated an alchemical hybrid')
        if salt_molar > 0 or abs(before['charge_a_e']) > 1e-3:
            run('genion', ['genion', '-s', 'ions.tpr', '-p', 'system.top', '-o', 'prepared.gro',
                            '-pname', 'NA', '-nname', 'CL', '-neutral', '-conc', str(salt_molar), '-seed', str(ion_seed)], 'SOL\n')
        else:
            shutil.copyfile(native / 'solvated.gro', native / 'prepared.gro')
        (native / 'verify.mdp').write_text(mdp.replace('coulombtype = Cut-off', 'coulombtype = PME'))
        run('final-grompp', ['grompp', '-f', 'verify.mdp', '-c', 'prepared.gro', '-p', 'system.top', '-o', 'verified.tpr', '-pp', 'prepared.top'])
        final = topology_charge_report(native / 'prepared.top')
        require(abs(final['charge_a_e']) < 1e-3, 'Prepared solvated/ionized topology is not neutral')
        gro = (native / 'prepared.gro').read_text().splitlines()
        count = int(gro[1]); require(count == final['atom_count'] and len(gro) == count + 3, 'Final GRO/TOP atom counts differ')
        box = [float(x) for x in gro[-1].split()]
        require(len(box) == 3 and all(math.isfinite(x) and x > 2.4 for x in box), 'Invalid final orthorhombic box')
        molecules = Counter(line[5:10].strip() for line in gro[2 + solute_atoms:-1])
        require(set(molecules) <= {'SOL', 'NA', 'CL'} and molecules['SOL'] % 3 == 0, 'Unexpected solvent/ion identity')
        receipt['solvation'] = {'box_nm': box, 'volume_nm3': math.prod(box), 'water_molecules': molecules['SOL'] // 3,
                                'sodium_ions': molecules['NA'], 'chloride_ions': molecules['CL'], 'solute_charge_e': expected_charge,
                                'final_charge_e': final['charge_a_e'], 'atom_count': count}
        assets = root / 'assets'; assets.mkdir()
        shutil.copyfile(native / 'prepared.gro', assets / 'prepared.gro')
        shutil.copyfile(native / 'prepared.top', assets / 'prepared.top')
        shutil.copyfile(root / 'source.pdb', assets / 'source.pdb')
        receipt.update(state='complete', asset_directory=str(assets),
                       system={'coordinates': 'prepared.gro', 'topology': 'prepared.top'},
                       asset_sha256={p.name: sha(p) for p in sorted(assets.iterdir())})
        receipt['conditions']['prepared_solvated_ionized'] = True
        write_json(root / 'receipt.json', receipt)
        write_json(assets / 'preparation-receipt.json', receipt)
        return receipt
    except Exception as error:
        receipt.update(state='failed', error=str(error))
        write_json(root / 'receipt.json', receipt)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--runtime', default='/var/lib/bio-md/runtime-cpu', type=Path)
    parser.add_argument('--force-field', required=True)
    parser.add_argument('--water-model', required=True)
    parser.add_argument('--salt-molar', required=True, type=float)
    parser.add_argument('--box-margin-nm', required=True, type=float)
    parser.add_argument('--temperature-kelvin', required=True, type=float)
    parser.add_argument('--ph', required=True, type=float)
    parser.add_argument('--protonation-review', required=True)
    parser.add_argument('--chemistry-decisions', required=True, type=Path,
                        help='Explicit supported termini/side-chain/hydrogen/disulfide decisions and exact histidine map')
    parser.add_argument('--chain', action='append', help='Fixed PDB chain ID; repeat for multiple chains; use _ for blank')
    parser.add_argument('--ion-seed', type=int, default=42)
    args = parser.parse_args(argv)
    result = prepare(args.input, args.out, runtime_prefix=args.runtime, force_field=args.force_field,
                     water_model=args.water_model, salt_molar=args.salt_molar, box_margin_nm=args.box_margin_nm,
                     temperature_kelvin=args.temperature_kelvin, ph=args.ph, protonation_review=args.protonation_review,
                     chemistry_decisions=json.loads(args.chemistry_decisions.read_text()),
                     chains=None if args.chain is None else [' ' if c == '_' else c for c in args.chain], ion_seed=args.ion_seed)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
