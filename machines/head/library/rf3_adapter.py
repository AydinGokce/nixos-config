"""Lossless construct projection and real CPU chemistry checks for pinned RF3.

The preflight runs native AtomWorks parsing and RF3's chemistry transforms only.
It neither loads model weights nor substitutes a query-only MSA for inference.
"""
import copy
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import subprocess

from chemistry import bonds, components, keys, ligand, polymer, require, write_json

PIN = 'b02eed6a6bdf8f44d14a80cc36e3da13c9f2291c'
ATOMWORKS_VERSION = '2.2.1'
CHAIN_TYPES = {'protein': 'polypeptide(L)', 'dna': 'polydeoxyribonucleotide', 'rna': 'polyribonucleotide'}
AA_CODES = dict(zip('ACDEFGHIKLMNPQRSTVWY',
                    'ALA CYS ASP GLU PHE GLY HIS ILE LYS LEU MET ASN PRO GLN ARG SER THR VAL TRP TYR'.split()))
AA_LETTERS = {value: key for key, value in AA_CODES.items()}
MAX_ASSET_BYTES = 16 * 1024**2


def protein_queries(native):
    """Exact RF3 MSA query rule: canonical CCDs map to letters, other CCDs to X."""
    require(isinstance(native, list) and len(native) == 1, 'RF3 bundle must contain exactly one job')
    result = []
    for component in native[0]['components']:
        if component.get('chain_type', '').lower() == 'polypeptide(l)':
            require(isinstance(component['seq'], list), 'RF3 adapter sequence must be an explicit CCD-code list')
            result.append({'chain_id': component['chain_id'],
                           'sequence': ''.join(AA_LETTERS.get(code.removeprefix('(').removesuffix(')'), 'X') for code in component['seq'])})
    return result


def build(snapshot, destination, assets, options):
    keys(options, {'msa_backend'}, 'RF3 options')
    require(options.get('msa_backend', 'public') in {'public', 'private'}, 'Unknown RF3 MSA backend')
    native_components, descriptions, graphs, planned, circles = [], {}, {}, {}, []
    for component in components(snapshot):
        chain = component['chain_id']
        identity = component['record']['identity']
        kind = identity['molecule_type']
        if kind != 'small_molecule':
            kind, sequence, modifications, circular = polymer(component, snapshot)
            require(len(sequence) >= 4, 'RF3 removes polymers shorter than four residues; short chains are not silently discarded')
            codes = ([AA_CODES[letter] for letter in sequence] if kind == 'protein' else
                     [('D' if kind == 'dna' else '') + letter for letter in sequence])
            for modification in modifications:
                codes[modification['position']-1] = modification['ccd']
            value = {'chain_id': chain, 'chain_type': CHAIN_TYPES[kind], 'is_polymer': True,
                     'seq': ['('+code+')' for code in codes]}
            descriptions[chain] = {'molecule_type': kind, 'residues': codes, 'circular': circular}
            if circular:
                require(kind == 'protein',
                        'RF3 circularity currently requires a peptide; other backbone chemistry is not inferred')
                circles.append([f'{chain}/{codes[-1]}/{len(codes)}/C', f'{chain}/{codes[0]}/1/N'])
        else:
            projected, graph = ligand(component, assets)
            if 'ccd' in projected:
                value = {'chain_id': chain, 'ccd_code': projected['ccd']}
                descriptions[chain] = {'molecule_type': kind, 'residues': [projected['ccd']], 'native_res_id': 1}
            else:
                # Explicit names prevent the upstream graph-name cache from
                # merging molecules with distinct charge or stereochemistry.
                name = 'L:' + hashlib.sha256(projected['smiles'].encode()).hexdigest()[:16]
                value = {'chain_id': chain, 'res_name': name}
                if 'structure_file' in identity:
                    require(identity.get('structure_format', 'sdf').lower() == 'sdf',
                            'RF3 currently requires an SDF attachment; MOL/CIF conversion is not implicit')
                    source = Path(assets[component['construct_ref']][identity['structure_file']])
                    require(source.is_file() and not source.is_symlink() and source.stat().st_size <= MAX_ASSET_BYTES,
                            'Missing, symlinked or oversized RF3 SDF attachment')
                    relative = f'ligands/{chain}.sdf'
                    planned[relative] = source.read_bytes()
                    value['path'] = relative
                else:
                    value['smiles'] = identity['smiles']
                descriptions[chain] = {'molecule_type': kind, 'residues': [name], 'native_res_id': 0}
                graphs[chain] = {**graph, 'canonical_smiles': projected['smiles']}
        native_components.append(value)
    native_bonds = []
    for bond in bonds(snapshot):
        endpoints = []
        for side in ('from', 'to'):
            endpoint = bond[side]
            chain, position = endpoint['chain_id'], endpoint['position']
            description = descriptions[chain]
            atom = endpoint['atom']
            require(re.fullmatch(r'[A-Za-z0-9_\'\"*+.-]+', atom),
                    'RF3 bond atom names cannot contain selection syntax')
            residue = description['residues'][position-1]
            native_position = description.get('native_res_id', position)
            endpoints.append(f'{chain}/{residue}/{native_position}/{atom}')
        require(endpoints not in native_bonds and endpoints[::-1] not in native_bonds,
                'Duplicate RF3 covalent bond')
        native_bonds.append(endpoints)
    native_bonds.extend(circles)
    native = [{'name': 'construct', 'components': native_components}]
    if native_bonds:
        native[0]['bonds'] = native_bonds
    if circles:
        # The runner forwards this to RF3's documented cyclic_chains CLI
        # option; from_json_dict alone does not read the field.
        native[0]['cyclic_chains'] = [pair[0].split('/')[0] for pair in circles]
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for relative, raw in planned.items():
        path = destination/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        require(not path.exists(), 'Refusing to overwrite an RF3 staged asset')
        path.write_bytes(raw)
    require(not (destination/'input.json').exists(), 'Refusing to overwrite RF3 input')
    write_json(destination/'input.json', native)
    return {'entrypoint': 'input.json', 'format': 'rf3-json', 'model_version': PIN,
            'native_source_pin': PIN, 'atomworks_version': ATOMWORKS_VERSION,
            'has_protein': any(value['molecule_type'] == 'protein' for value in descriptions.values()),
            'msa_queries': protein_queries(native), 'expected_chains': descriptions,
            'ligand_graphs': graphs, 'circular_bonds': circles,
            'cyclic_chains': native[0].get('cyclic_chains', []),
            'native_input_notes': [
                'Protein MSAs are mandatory at inference; each chain retains TaxID pairing metadata.',
                'SDF and SMILES stereochemistry and charge are checked through native chemistry features.',
                'Input ligand coordinates specify chemical stereochemistry, not structural restraints.',
                'Unsupported custom monomers, termini, linkage chemistry, isotope/radical or enhanced stereo are rejected.']}


def _atom_key(array, index):
    return (str(array.chain_id[index]), str(array.res_name[index]), int(array.res_id[index]), str(array.atom_name[index]))


def _inventory(array):
    atoms = {}
    for index in range(len(array)):
        if str(array.element[index]).upper() == 'H':
            continue
        key = _atom_key(array, index)
        require(key not in atoms, 'RF3 native parser duplicated a named atom')
        atoms[key] = (str(array.element[index]), int(array.charge[index]))
    return atoms


def _bond_inventory(array):
    return {tuple(sorted((_atom_key(array, int(left)), _atom_key(array, int(right))))): int(order)
            for left, right, order in array.bonds.as_array()
            if str(array.element[int(left)]).upper() != 'H' and str(array.element[int(right)]).upper() != 'H'}


def _endpoint(text):
    chain, residue, position, atom = text.split('/')
    return chain, residue, int(position), atom


def _parsed_endpoint(text, array, metadata):
    requested = _endpoint(text)
    if metadata['expected_chains'][requested[0]]['molecule_type'] != 'small_molecule':
        return requested
    # AtomWorks normalizes non-polymer author residue numbering during parse.
    # The input native selection uses its raw ligand residue number; bind the
    # parsed atom by the unique explicit chain/residue-name/atom-name triple.
    matches = [key for key in _inventory(array) if (key[0], key[1], key[3]) == (requested[0], requested[1], requested[3])]
    require(len(matches) == 1, 'RF3 lost or duplicated an explicitly named ligand bond atom: '+repr(requested)
            +' available='+repr([key for key in _inventory(array) if key[0] == requested[0]][:20]))
    return matches[0]


def _verify_connections(before, after, native, metadata):
    old_atoms, new_atoms = _inventory(before), _inventory(after)
    allowed_leaving = set()
    for pair in metadata['circular_bonds']:
        chain, residue, position, _ = _endpoint(pair[0])
        allowed_leaving.add((chain, residue, position, 'OXT'))
    removed = set(old_atoms)-set(new_atoms)
    require(removed <= allowed_leaving and not set(new_atoms)-set(old_atoms),
            'RF3 removed/added heavy atoms while forming a covalent bond; explicit leaving-group chemistry needs a tested representation: '
            +repr({'removed': sorted(removed)[:20], 'added': sorted(set(new_atoms)-set(old_atoms))[:20]}))
    require(all(old_atoms[key] == new_atoms[key] for key in new_atoms),
            'RF3 changed an element or formal charge while forming a covalent bond; this reaction is not silently inferred')
    old_bonds = {pair: order for pair, order in _bond_inventory(before).items() if not (set(pair) & removed)}
    new_bonds = _bond_inventory(after)
    for pair, order in old_bonds.items():
        require(new_bonds.get(pair) == order, 'RF3 changed an existing bond order while forming the requested bond')
    requested = {tuple(sorted(_parsed_endpoint(endpoint, after, metadata) for endpoint in pair))
                 for pair in native.get('bonds', [])}
    require(all(pair[0] in new_atoms and pair[1] in new_atoms and new_bonds.get(pair) == 1 for pair in requested),
            'RF3 did not preserve a requested named single covalent bond')
    require(set(new_bonds)-set(old_bonds) <= requested, 'RF3 introduced an unrequested covalent bond')
    return {'requested_bonds': native.get('bonds', []), 'removed_terminal_atoms': [list(key) for key in sorted(removed)]}


def _canonical(mol):
    from rdkit import Chem
    return Chem.MolToSmiles(Chem.RemoveHs(mol), canonical=True, isomericSmiles=True)


def preflight(bundle_dir, metadata):
    import numpy as np
    from rdkit import Chem
    from atomworks.io.tools.inference import components_to_atom_array
    from atomworks.io.tools.rdkit import atom_array_to_rdkit
    from rf3.utils.inference import InferenceInput
    from rf3.data.pipelines import build_af3_transform_pipeline
    import rf3_compat
    rf3_compat.install()
    require(version('atomworks') == ATOMWORKS_VERSION, 'AtomWorks differs from the pinned RF3 input adapter')
    source = Path(os.environ.get('RF3_SOURCE_DIR', '/mnt/bio-shared/src/foundry-rf3-'+PIN))
    require(subprocess.check_output(['git', '-c', 'safe.directory='+str(source), '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() == PIN,
            'RF3 source pin differs from the input adapter')
    root = Path(bundle_dir).resolve()
    document = json.loads((root/metadata['entrypoint']).read_text())
    native = copy.deepcopy(document[0])
    for component in native['components']:
        if 'path' in component:
            relative = Path(component['path'])
            require(not relative.is_absolute() and '..' not in relative.parts, 'RF3 asset escapes its input bundle')
            component['path'] = str(root/relative)
    unconnected = dict(native)
    unconnected.pop('bonds', None)
    before = InferenceInput.from_json_dict(unconnected).atom_array
    parsed = InferenceInput.from_json_dict(native)
    parsed.cyclic_chains = native.get('cyclic_chains') or None
    array = parsed.atom_array
    require(len(array) and len(array) <= 50000, 'RF3 native preflight exceeds the 50000-atom CPU validation bound')
    require(set(map(str, array.chain_id)) == set(metadata['expected_chains']), 'RF3 changed the input chain mapping')
    for chain, expected in metadata['expected_chains'].items():
        selected = array[array.chain_id == chain]
        residues = list(dict.fromkeys(zip(map(int, selected.res_id), map(str, selected.res_name))))
        require([name for _, name in residues] == expected['residues'], 'RF3 changed a polymer or CCD residue identity')
    connections = _verify_connections(before, array, native, metadata)
    ligand_checks = {}
    bonded_chains = {_endpoint(endpoint)[0] for pair in native.get('bonds', []) for endpoint in pair}
    for chain, expected in metadata['ligand_graphs'].items():
        selected = before[before.chain_id == chain]
        mol = atom_array_to_rdkit(selected, hydrogen_policy='infer', attempt_fixing_corrupted_molecules=False)
        require(_canonical(mol) == _canonical(Chem.MolFromSmiles(expected['canonical_smiles'])),
                f'RF3 lost ligand {chain} charge, connectivity or stereochemistry during native parsing')
        ligand_checks[chain] = {'canonical_smiles': _canonical(mol), 'native_atom_names': list(map(str, selected.atom_name)),
                               'native_residue_ids': sorted(set(map(int, selected.res_id))),
                               'formal_charge': int(sum(selected.charge)), 'covalently_connected': chain in bonded_chains}
    # Execute the pinned RF3 factory's chemistry and bond transforms, skipping
    # only the four MSA operations. No query-only MSA is invented or retained.
    pipeline = build_af3_transform_pipeline(is_inference=True, protein_msa_dirs=[], rna_msa_dirs=[],
        residue_cache_dir=None, crop_size=None, undesired_res_names=[], fallback_conformer_to_input_coords=False,
        use_element_for_atom_names_of_atomized_tokens=True, diffusion_batch_size=1)
    data = parsed.to_pipeline_input()
    completed = []
    for transform in pipeline.transforms:
        name = type(transform).__name__
        if name == 'ConvertToTorch':
            break
        if name in {'LoadPolymerMSAs', 'PairAndMergePolymerMSAs', 'EncodeMSA', 'FillFullMSAFromEncoded'}:
            continue
        data = transform(data)
        completed.append(name)
    require('AddAF3ChiralFeatures' in completed, 'RF3 native chemistry pipeline did not reach chiral feature generation')
    require('AddCyclicBonds' in completed and 'token_bonds' in data['feats'],
            'RF3 native token-bond feature generation did not complete')
    feature_array = data['atom_array']
    parsed_atoms, feature_atoms = _inventory(array), _inventory(feature_array)
    removed = set(parsed_atoms)-set(feature_atoms)
    allowed_terminal = {key for key in parsed_atoms if
                        metadata['expected_chains'][key[0]]['molecule_type'] == 'protein' and key[3] == 'OXT'
                        or metadata['expected_chains'][key[0]]['molecule_type'] in {'dna', 'rna'} and key[3] == 'OP3'}
    require(removed <= allowed_terminal and not set(feature_atoms)-set(parsed_atoms),
            'RF3 chemistry transforms removed or added molecular atoms beyond the native terminal-oxygen convention')
    require(set(map(str, feature_array.chain_id)) == set(metadata['expected_chains']),
            'RF3 chemistry transforms discarded a requested chain')
    require(all(parsed_atoms[key] == feature_atoms[key] for key in feature_atoms),
            'RF3 chemistry transforms changed a chemical element or formal charge')
    feature_index = {_atom_key(feature_array, i): i for i in range(len(feature_array))}
    checked_token_bonds = []
    for pair in native.get('bonds', []):
        endpoints = [_parsed_endpoint(endpoint, feature_array, metadata) for endpoint in pair]
        require(all(endpoint in feature_index for endpoint in endpoints), 'RF3 dropped a requested bond atom during featurization')
        token_ids = [int(feature_array.token_id[feature_index[endpoint]]) for endpoint in endpoints]
        require(bool(data['feats']['token_bonds'][tuple(token_ids)]),
                'RF3 does not condition this requested bond in its native token features; ordinary polymer-polymer crosslinks are unsupported')
        checked_token_bonds.append({'atoms': pair, 'token_ids': token_ids})
    for chain, expected in metadata['ligand_graphs'].items():
        residue = metadata['expected_chains'][chain]['residues'][0]
        reference_mol = data['rdkit'].get(residue)
        require(reference_mol is not None and _canonical(reference_mol) == _canonical(Chem.MolFromSmiles(expected['canonical_smiles'])),
                f'RF3 lost ligand {chain} charge, connectivity or stereochemistry in reference molecule features')
        ligand_checks[chain]['reference_graph_verified'] = True
    require(np.asarray(data['feats']['ref_mask']).all(), 'RF3 left input atoms without a valid native reference conformer')
    require(np.isfinite(data['feats']['ref_pos']).all(), 'RF3 generated nonfinite reference conformer coordinates')
    require(np.array_equal(np.asarray(data['feats']['ref_charge']), np.asarray(data['atom_array'].charge)),
            'RF3 reference features changed formal charges')
    return {'model': 'rf3', 'model_version': PIN, 'atomworks_version': ATOMWORKS_VERSION, 'native_parser': True,
            'input_annotation_compatibility': rf3_compat.CORRECTION,
            'atoms': len(array), 'chains': list(metadata['expected_chains']), 'connections': connections,
            'feature_atoms': len(feature_array), 'terminal_atoms_omitted_by_native_model': [list(key) for key in sorted(removed)],
            'token_bonds': checked_token_bonds,
            'chiral_features': data['feats']['chiral_feats'].tolist(),
            'ligands': ligand_checks, 'chemistry_transforms': completed, 'msa_queries': False, 'model_inference': False}
