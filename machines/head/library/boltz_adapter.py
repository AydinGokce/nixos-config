"""Input projection and CPU parser checks for pinned Boltz 2.2.1."""
from pathlib import Path
import json
import os
from importlib.metadata import version
from chemistry import bonds, components, ligand, polymer, require, write_json

MODEL_VERSION = '2.2.1'


def _verify_explicit_bonds(native, structure, covalent_type):
    """Bind every named request to the final native atom and residue indices."""
    chains = {str(row['name']): row for row in structure.chains}

    def endpoint(value):
        chain_name, position, atom_name = value
        require(chain_name in chains, 'Boltz lost an explicit bond chain')
        chain = chains[chain_name]
        require(1 <= position <= int(chain['res_num']), 'Boltz lost an explicit bond residue')
        residue_index = int(chain['res_idx']) + position - 1
        residue = structure.residues[residue_index]
        start = int(residue['atom_idx'])
        matches = [index for index in range(start, start + int(residue['atom_num']))
                   if str(structure.atoms[index]['name']) == atom_name]
        require(len(matches) == 1, 'Boltz lost or duplicated an explicit bond atom')
        return int(chain['asym_id']), residue_index, matches[0]

    checked = []
    for constraint in native.get('constraints', []):
        if 'bond' not in constraint:
            continue
        requested = constraint['bond']
        first, second = endpoint(requested['atom1']), endpoint(requested['atom2'])
        matches = []
        for row in structure.bonds:
            left = tuple(int(row[key + '_1']) for key in ('chain', 'res', 'atom'))
            right = tuple(int(row[key + '_2']) for key in ('chain', 'res', 'atom'))
            if ((left, right) in ((first, second), (second, first))
                    and int(row['type']) == covalent_type):
                matches.append(row)
        require(len(matches) == 1, 'Boltz did not retain the exact requested covalent bond in its native feature array')
        checked.append({'requested': requested,
                        'native': {key: int(matches[0][key]) for key in matches[0].dtype.names}})
    return checked


def build(snapshot, destination, assets, options):
    destination = Path(destination)
    sequences, graphs, grouped = [], {}, {}
    for component in components(snapshot):
        chain = component['chain_id']
        require(len(chain) <= 5, 'Boltz 2.2.1 stores chain IDs in five characters; longer identifiers are not truncated')
        identity = component['record']['identity']
        if identity['molecule_type'] == 'small_molecule':
            value, graph = ligand(component, assets)
            sequences.append({'ligand': {'id': chain, **value}})
            if graph is not None:
                graphs[chain] = graph
        else:
            kind, sequence, modifications, circular = polymer(component, snapshot)
            # Pinned Boltz groups by type and raw sequence, ignoring differences
            # in modifications/circularity. Detect this rather than losing them.
            group = (kind, sequence)
            signature = (json.dumps(modifications, sort_keys=True), circular)
            require(group not in grouped or grouped[group] == signature,
                    'Boltz 2.2.1 merges identical base sequences with different modifications/circularity; this assembly is unsupported')
            grouped[group] = signature
            value = {'id': chain, 'sequence': sequence, 'modifications': modifications, 'cyclic': circular}
            sequences.append({kind: value})
    constraints = []
    chain_types = {component['chain_id']: component['record']['identity'] for component in snapshot['components']}
    for bond in bonds(snapshot):
        for endpoint in (bond['from'], bond['to']):
            identity = chain_types[endpoint['chain_id']]
            require(identity['molecule_type'] != 'small_molecule' or 'ccd' in identity,
                    'Boltz covalent bonds require a CCD ligand; atom names from SMILES/SDF are not silently guessed')
        constraints.append({'bond': {out: [bond[side]['chain_id'], bond[side]['position'], bond[side]['atom']]
                                     for out, side in [('atom1', 'from'), ('atom2', 'to')]}})
    native = {'version': 1, 'sequences': sequences}
    if constraints:
        native['constraints'] = constraints
    write_json(destination/'input.yaml', native)  # JSON is valid YAML; no YAML quoting ambiguity.
    return dict(entrypoint='input.yaml', format='boltz-yaml', has_protein=any('protein' in s for s in sequences),
                model_version=MODEL_VERSION, ligand_graphs=graphs,
                expected_chains=[c['chain_id'] for c in snapshot['components']])


def preflight(bundle_dir, metadata):
    import numpy as np
    from boltz.data import const
    from boltz.data.mol import load_canonicals
    from boltz.data.parse.schema import parse_boltz_schema
    require(version('boltz') == MODEL_VERSION, 'Boltz parser version differs from the input adapter')
    moldir = Path(os.environ.get('BOLTZ_CACHE', '/mnt/bio-shared/cache/boltz'))/'mols'
    ccd = load_canonicals(str(moldir))
    native = json.loads((Path(bundle_dir)/metadata['entrypoint']).read_text())
    # CPU schema parsing alone does not query MSA services. Its protein-MSA
    # completeness guard is satisfied on this private in-memory audit copy.
    for item in native['sequences']:
        if 'protein' in item:
            item['protein']['msa'] = 'empty'
    target = parse_boltz_schema('registry-preflight', native, ccd, moldir, boltz_2=True)
    structure = target.structure
    chains = [str(row['name']) for row in structure.chains]
    require(chains == metadata['expected_chains'], 'Boltz parser changed the requested chain mapping')
    require(len(structure.atoms) > 0, 'Boltz parser produced no atoms')
    parsed_chains = {str(row['name']): row for row in structure.chains}
    for item in native['sequences']:
        kind, definition = next(iter(item.items()))
        parsed = parsed_chains[definition['id']]
        if kind in ('protein', 'dna', 'rna'):
            require(int(parsed['res_num']) == len(definition['sequence']), 'Boltz changed polymer length')
            expected_period = len(definition['sequence']) if definition.get('cyclic') else 0
            require(int(parsed['cyclic_period']) == expected_period, 'Boltz did not retain circularity')
            for modification in definition.get('modifications', []):
                residue = structure.residues[int(parsed['res_idx'])+modification['position']-1]
                require(str(residue['name']) == modification['ccd'], 'Boltz changed a modified residue identity')
    explicit_bonds = _verify_explicit_bonds(native, structure, const.bond_type_ids['COVALENT'])
    return dict(model='boltz2', model_version=MODEL_VERSION, native_parser=True,
                chains=chains, atoms=len(structure.atoms), residues=len(structure.residues),
                explicit_bonds=explicit_bonds,
                msa_queries=False, model_inference=False)
