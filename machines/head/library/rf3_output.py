"""Audit RF3's predicted chemistry without repairing coordinates or rerunning it.

The native input chemistry pipeline supplies the named atom/bond inventory,
tetrahedral feature constraints and explicitly assigned reference double-bond
stereo. Passing these checks is not a claim of correct folding, bond geometry,
binding affinity or experimental accuracy. Every original output is retained.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import shutil

DIAGNOSTICS = 'rf3-output-validation.json'
EXPECTED = 'rf3-output-expected.json'
POLICY = 'rf3-named-atoms-bonds-requested-stereo-v1'
PIN = 'b02eed6a6bdf8f44d14a80cc36e3da13c9f2291c'


class Error(RuntimeError):
    pass


def _require(condition, message):
    if not condition:
        raise Error(message)


def _hash(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(8*1024**2), b''):
            value.update(chunk)
    return value.hexdigest()


def _write(path, value):
    with Path(path).open('x') as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.write('\n')


def _regular(path, root):
    path, root = Path(path), Path(root).resolve()
    _require(path.is_file() and path.stat().st_size > 0 and not path.is_symlink(),
             'Missing, empty or symlinked RF3 audit file: '+str(path))
    _require(path.resolve().is_relative_to(root), 'RF3 audit file escapes its run directory')
    for parent in path.parents:
        if parent == root:
            break
        _require(not parent.is_symlink(), 'RF3 audit file has a symlinked parent')
    return path


def _key(array, index):
    return (str(array.chain_id[index]), str(array.res_name[index]),
            int(array.res_id[index]), str(array.atom_name[index]))


def _inventory(array):
    _require('charge' in array.get_annotation_categories(), 'RF3 atom array has no explicit formal charge')
    atoms = {}
    for index in range(len(array)):
        key = _key(array, index)
        _require(key not in atoms, 'RF3 output contains duplicate named atoms')
        atoms[key] = (str(array.element[index]).upper(), int(array.charge[index]))
    _require(array.bonds is not None, 'RF3 output has no bond inventory')
    bonds = {}
    for left, right, order in array.bonds.as_array():
        pair = tuple(sorted((_key(array, int(left)), _key(array, int(right)))))
        _require(pair not in bonds and pair[0] != pair[1], 'RF3 output has duplicate or self bonds')
        bonds[pair] = int(order)
    return atoms, bonds


def _compatibility():
    path = Path(__file__).resolve().with_name('rf3_compat.py')
    spec = importlib.util.spec_from_file_location('_rf3_output_compatibility', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install()  # Checks the exact Foundry source and AtomWorks version.
    return {'id': module.CORRECTION, 'sha256': _hash(path)}


def _expected(prepared_input):
    import numpy as np
    from rdkit import Chem
    from rf3.utils.inference import InferenceInput
    from rf3.data.pipelines import build_af3_transform_pipeline

    source = Path(prepared_input).absolute()
    _regular(source, source.parent)
    document = json.loads(source.read_text())
    if isinstance(document, list):
        _require(len(document) == 1, 'RF3 output audit requires exactly one prepared assembly')
        document = document[0]
    job = copy.deepcopy(document)
    _require(isinstance(job, dict) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', job.get('name', '')),
             'Invalid RF3 prepared job name')
    compatibility = _compatibility()
    inputs = {source.name: _hash(source)}
    for component in job['components']:
        for field in ('path', 'msa_path'):
            if field not in component:
                continue
            relative = Path(component[field])
            _require(not relative.is_absolute() and '..' not in relative.parts,
                     'Prepared RF3 asset must stay relative to its validated bundle')
            path = _regular(source.parent/relative, source.parent)
            inputs[str(relative)] = _hash(path)
            component[field] = str(path)
    parsed = InferenceInput.from_json_dict(job)
    parsed.cyclic_chains = job.get('cyclic_chains') or None
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
    _require('AddAF3ChiralFeatures' in completed and 'AddCyclicBonds' in completed,
             'Native RF3 chemistry feature pipeline did not complete')
    array = data['atom_array']
    _require(0 < len(array) <= 50000, 'RF3 output audit exceeds its 50000-atom bound')
    _require(set(map(str, array.chain_id)) == {c['chain_id'] for c in job['components']},
             'Native RF3 feature pipeline changed the prepared chain inventory')
    atoms, bonds = _inventory(array)
    indices = [_key(array, i) for i in range(len(array))]
    tetrahedral = []
    for row in np.asarray(data['feats']['chiral_feats']):
        _require(math.isfinite(float(row[4])) and row[4] != 0, 'Invalid native tetrahedral feature')
        tetrahedral.append({'atoms': [indices[int(i)] for i in row[:4]],
                            'expected_sign': 1 if row[4] > 0 else -1})
    double_bonds, reference_graphs = [], {}
    residues = list(dict.fromkeys(key[:3] for key in indices))
    for chain, residue, position in residues:
        mol = data['rdkit'].get(residue)
        _require(mol is not None, 'Missing native reference graph for '+residue)
        reference_graphs[residue] = Chem.MolToSmiles(Chem.RemoveHs(mol), canonical=True, isomericSmiles=True)
        for bond in mol.GetBonds():
            stereo = bond.GetStereo()
            if stereo in (Chem.BondStereo.STEREONONE, Chem.BondStereo.STEREOANY):
                continue  # Unspecified stereo is not silently made a requirement.
            _require(stereo in (Chem.BondStereo.STEREOE, Chem.BondStereo.STEREOZ,
                                Chem.BondStereo.STEREOTRANS, Chem.BondStereo.STEREOCIS),
                     'Unsupported reference bond stereochemistry')
            neighbors = list(bond.GetStereoAtoms())
            _require(len(neighbors) == 2, 'Assigned double bond lacks native stereo neighbor identities')
            order = [neighbors[0], bond.GetBeginAtomIdx(), bond.GetEndAtomIdx(), neighbors[1]]
            keys = []
            for index in order:
                atom = mol.GetAtomWithIdx(index)
                _require(atom.HasProp('atom_name'), 'Native stereo atom lacks its explicit name')
                key = (chain, residue, position, atom.GetProp('atom_name'))
                _require(key in atoms, 'An assigned stereo-defining atom is absent from native model features')
                keys.append(key)
            double_bonds.append({'atoms': keys, 'expected': 'trans' if stereo in
                                 (Chem.BondStereo.STEREOE, Chem.BondStereo.STEREOTRANS) else 'cis',
                                 'native_stereo': str(stereo)})
    expected = {'schema': 1, 'policy': POLICY, 'source_commit': PIN, 'name': job['name'],
                'prepared_files_sha256': inputs, 'compatibility': compatibility,
                'atoms': [{'identity': key, 'element': value[0], 'charge': value[1]} for key, value in atoms.items()],
                'bonds': [{'atoms': pair, 'order': order} for pair, order in sorted(bonds.items())],
                'tetrahedral': tetrahedral, 'double_bonds': double_bonds,
                'reference_isomeric_smiles': reference_graphs, 'native_chemistry_transforms': completed,
                'msa_search_or_model_inference': False}
    return expected


def _dihedral(points):
    import numpy as np
    a, b, c, d = np.asarray(points, dtype=float)
    axis = c-b
    size = np.linalg.norm(axis)
    if size < 1e-8:
        return None
    axis /= size
    v, w = a-b, d-c
    v -= np.dot(v, axis)*axis
    w -= np.dot(w, axis)*axis
    if min(np.linalg.norm(v), np.linalg.norm(w)) < 1e-8:
        return None
    return float(np.arctan2(np.dot(np.cross(axis, v), w), np.dot(v, w)))


def audit_array(actual, expected):
    """Audit named arrays and assigned stereo; never alter the input array."""
    import numpy as np
    observed_atoms, observed_bonds = _inventory(actual)
    atoms = {tuple(row['identity']): (row['element'], row['charge']) for row in expected['atoms']}
    bonds = {tuple(tuple(key) for key in row['atoms']): row['order'] for row in expected['bonds']}
    result = {'atoms': len(actual), 'expected_atoms': len(atoms),
              'finite_coordinates': bool(np.isfinite(actual.coord).all()),
              'missing_atoms': sorted(atoms.keys()-observed_atoms.keys()),
              'extra_atoms': sorted(observed_atoms.keys()-atoms.keys()),
              'changed_elements_or_charges': [dict(atom=key, expected=atoms[key], observed=observed_atoms[key])
                  for key in sorted(atoms.keys() & observed_atoms.keys()) if atoms[key] != observed_atoms[key]],
              'missing_or_changed_bonds': [dict(atoms=pair, expected=order, observed=observed_bonds.get(pair))
                  for pair, order in bonds.items() if observed_bonds.get(pair) != order],
              'extra_bonds': [dict(atoms=pair, order=order) for pair, order in observed_bonds.items() if pair not in bonds],
              'tetrahedral_checked': len(expected['tetrahedral']), 'tetrahedral_failures': [],
              'double_bonds': []}
    result['identity_and_connectivity_passed'] = not any(result[key] for key in
        ('missing_atoms', 'extra_atoms', 'changed_elements_or_charges', 'missing_or_changed_bonds', 'extra_bonds'))
    if result['identity_and_connectivity_passed'] and result['finite_coordinates']:
        xyz = {_key(actual, i): actual.coord[i] for i in range(len(actual))}
        for row in expected['tetrahedral']:
            angle = _dihedral([xyz[tuple(key)] for key in row['atoms']])
            # Near-planar centers have no reliable handedness; fail explicitly.
            valid = angle is not None and abs(math.sin(angle)) > 1e-6
            if not valid or (1 if angle > 0 else -1) != row['expected_sign']:
                result['tetrahedral_failures'].append({**row, 'observed_degrees': None if angle is None else math.degrees(angle)})
        for row in expected['double_bonds']:
            angle = _dihedral([xyz[tuple(key)] for key in row['atoms']])
            value = math.cos(angle) if angle is not None else 0
            observed = ('cis' if value > 0 else 'trans') if abs(value) > 1e-6 else 'undefined'
            result['double_bonds'].append({**row, 'observed': observed,
                'observed_degrees': None if angle is None else math.degrees(angle), 'passed': observed == row['expected']})
    result['passed'] = bool(result['identity_and_connectivity_passed'] and result['finite_coordinates']
        and not result['tetrahedral_failures'] and len(result['double_bonds']) == len(expected['double_bonds'])
        and all(row['passed'] for row in result['double_bonds']))
    return result


def _audit_file(path, expected):
    from biotite.structure.io.pdbx import CIFFile, get_structure
    cif = CIFFile.read(path)
    _require(len(cif) == 1, 'RF3 sample CIF contains multiple data blocks')
    atom_site = cif.block['atom_site']
    _require('pdbx_formal_charge' in atom_site, 'RF3 output omits explicit formal charges')
    _require(set(atom_site['pdbx_PDB_model_num'].as_array(str)) == {'1'}, 'RF3 sample CIF must contain exactly one model')
    actual = get_structure(cif, model=1, extra_fields=['charge'], include_bonds=True)
    return audit_array(actual, expected)


def _select_and_publish(out_dir, expected, audit_file=_audit_file):
    """Keep raw candidates/ranks; publish only a candidate passing this policy."""
    root = Path(out_dir).resolve()
    name = expected['name']
    directory = root/name
    _require(directory.is_dir() and not directory.is_symlink(), 'RF3 native output directory is missing or symlinked')
    _require(not (root/DIAGNOSTICS).exists() and not (root/EXPECTED).exists(),
             'RF3 output QA already has evidence; refusing to overwrite it')
    _write(root/EXPECTED, expected)
    receipt = {'schema': 1, 'policy': POLICY, 'status': 'failed', 'source_commit': PIN,
               'audit_source_sha256': _hash(__file__), 'expected_source': EXPECTED,
               'expected_sha256': _hash(root/EXPECTED), 'raw_outputs_preserved': True,
               'scope': 'Named atoms, elements, formal charges, declared bonds, finite coordinates and assigned tetrahedral/double-bond stereo; not general structural accuracy.',
               'samples': [], 'original_native_files': {}, 'selected': None}
    backup = root/'rf3-native-ranking-original'
    _require(not backup.exists(), 'RF3 native ranking backup already exists')
    backup.mkdir()
    for path in sorted(directory.glob(name+'_*')):
        if path.is_file():
            _regular(path, root)
            target = backup/path.name
            shutil.copyfile(path, target)
            receipt['original_native_files'][str(path.relative_to(root))] = dict(
                sha256=_hash(path), retained=str(target.relative_to(root)))
    candidates = sorted(directory.glob('seed-*_sample-*'))
    for candidate in candidates:
        row = {'directory': str(candidate.relative_to(root)), 'passed': False}
        receipt['samples'].append(row)
        try:
            _require(candidate.is_dir() and not candidate.is_symlink() and
                     re.fullmatch(r'seed-[0-9]+_sample-[0-9]+', candidate.name), 'Unexpected RF3 sample directory')
            prefix = name+'_'+candidate.name
            files = {kind: _regular(candidate/(prefix+suffix), root) for kind, suffix in
                     {'model': '_model.cif', 'summary': '_summary_confidences.json', 'confidences': '_confidences.json'}.items()}
            summary = json.loads(files['summary'].read_text())
            rank = summary.get('ranking_score')
            _require(not isinstance(rank, bool) and isinstance(rank, (int, float)) and math.isfinite(rank),
                     'RF3 sample has no finite native ranking score')
            json.loads(files['confidences'].read_text())
            row.update(ranking_score=rank, files={kind: dict(path=str(path.relative_to(root)), sha256=_hash(path))
                                                for kind, path in files.items()})
            row['chemistry'] = audit_file(files['model'], expected)
            row['passed'] = row['chemistry']['passed']
        except Exception as error:
            row['error'] = str(error)
    passing = [row for row in receipt['samples'] if row['passed']]
    if not passing:
        receipt['error'] = 'No existing RF3 sample passed the named-atom and requested-stereochemistry audit'
        _write(root/DIAGNOSTICS, receipt)
        raise Error(receipt['error']+'; raw outputs and '+DIAGNOSTICS+' retained')
    selected = sorted(passing, key=lambda row: (-row['ranking_score'], row['directory']))[0]
    receipt['selected'] = {'raw_directory': selected['directory'], 'ranking_score': selected['ranking_score'], 'files': {}}
    for kind, suffix in {'model': '_model.cif', 'summary': '_summary_confidences.json', 'confidences': '_confidences.json'}.items():
        source = root/selected['files'][kind]['path']
        target = directory/(name+suffix)
        _require(not target.is_symlink(), 'Refusing to replace a symlinked canonical RF3 output')
        temporary = directory/('.qa-selected-'+target.name)
        _require(not temporary.exists() and not temporary.is_symlink(), 'RF3 selection temporary path already exists')
        shutil.copyfile(source, temporary)
        _require(_hash(temporary) == selected['files'][kind]['sha256'], 'RF3 selected output copy hash mismatch')
        temporary.replace(target)
        receipt['selected']['files'][kind] = dict(path=str(target.relative_to(root)), sha256=_hash(target),
                                                raw_path=selected['files'][kind]['path'])
    receipt['status'] = 'passed'
    _write(root/DIAGNOSTICS, receipt)
    return receipt


def validate_and_select(out_dir, prepared_input):
    """Public entry point, called only inside the already verified RF3 runtime."""
    root = Path(out_dir).resolve()
    _require(root.is_dir(), 'RF3 output directory does not exist')
    try:
        return _select_and_publish(root, _expected(prepared_input))
    except Exception as error:
        if not (root/DIAGNOSTICS).exists():
            _write(root/DIAGNOSTICS, {'schema': 1, 'policy': POLICY, 'status': 'failed',
                                    'audit_source_sha256': _hash(__file__), 'error': str(error),
                                    'raw_outputs_preserved': True, 'selected': None})
        raise
