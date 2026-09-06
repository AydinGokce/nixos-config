"""Input projection and native CPU structure construction for OpenFold3 0.5.0."""
from pathlib import Path
import json
from importlib.metadata import version
from chemistry import bonds, components, ligand, polymer, require, write_json

MODEL_VERSION = '0.5.0'


def build(snapshot, destination, assets, options):
    chains, graphs, entity_signatures = [], {}, {}
    require(not bonds(snapshot), 'OpenFold3 0.5.0 exposes covalent_bonds in its schema but does not apply them in its query pipeline; explicit bonds are unsupported')
    for component in components(snapshot):
        chain = component['chain_id']
        identity = component['record']['identity']
        if identity['molecule_type'] == 'small_molecule':
            value, graph = ligand(component, assets)
            if 'ccd' in value:
                value = {'ccd_codes': [value['ccd']]}
            representation = value.get('smiles') or value['ccd_codes'][0]
            signature = ('ligand', json.dumps(value, sort_keys=True))
            chains.append({'molecule_type': 'ligand', 'chain_ids': [chain], **value})
            if graph is not None:
                graphs[chain] = graph
        else:
            kind, sequence, modifications, circular = polymer(component, snapshot)
            representation = sequence
            signature = (kind, json.dumps(modifications, sort_keys=True), circular)
            chains.append({'molecule_type': kind, 'chain_ids': [chain], 'sequence': sequence,
                           'non_canonical_residues': {str(m['position']): m['ccd'] for m in modifications},
                           'cyclic': circular})
        require(representation not in entity_signatures or entity_signatures[representation] == signature,
                'OpenFold3 0.5.0 assigns one entity to identical sequence strings even when polymer type/modifications differ; this assembly is unsupported')
        entity_signatures[representation] = signature
    native = {'queries': {'construct': {'chains': chains}}}
    write_json(Path(destination)/'query.json', native)
    return dict(entrypoint='query.json', format='openfold3-json', has_protein=any(c['molecule_type']=='protein' for c in chains),
                model_version=MODEL_VERSION, ligand_graphs=graphs,
                expected_chains=[c['chain_id'] for c in snapshot['components']])


def preflight(bundle_dir, metadata):
    import numpy as np
    from openfold3.projects.of3_all_atom.config.inference_query_format import InferenceQuerySet
    from openfold3.core.data.primitives.structure.query import structure_with_ref_mols_from_query
    require(version('openfold3') == MODEL_VERSION, 'OpenFold3 parser version differs from the input adapter')
    queries = InferenceQuerySet.from_json(Path(bundle_dir)/metadata['entrypoint'])
    query = queries.queries['construct']
    result = structure_with_ref_mols_from_query(query)
    atoms = result.atom_array
    chains = list(dict.fromkeys(str(value) for value in atoms.chain_id))
    require(chains == metadata['expected_chains'], 'OpenFold3 parser changed chain identity/order')
    require(len(atoms) > 0 and np.isfinite(atoms.coord).all(), 'OpenFold3 native parser returned invalid atoms')
    for chain in query.chains:
        for chain_id in chain.chain_ids:
            if chain.sequence is not None:
                selected = atoms.chain_id == chain_id
                require(len(set(atoms.res_id[selected])) == len(chain.sequence), 'OpenFold3 changed polymer length')
                require(set(bool(x) for x in atoms.is_cyclic[selected]) == {chain.cyclic}, 'OpenFold3 changed circularity')
            for position, expected in (chain.non_canonical_residues or {}).items():
                names = set(atoms.res_name[(atoms.chain_id == chain_id) & (atoms.res_id == position)])
                require(names == {expected}, f'OpenFold3 changed modified residue {chain_id}:{position}')
    return dict(model='openfold3', model_version=MODEL_VERSION, native_parser=True,
                chains=chains, atoms=len(atoms), msa_queries=False, model_inference=False)
