"""Strict shared chemical projections for native input adapters."""
import copy
import json
from pathlib import Path
import re
from translation import materialized_identity

ALPHABETS = {'protein': set('ACDEFGHIKLMNPQRSTVWY'), 'dna': set('ACGT'), 'rna': set('ACGU')}


def require(value, message):
    if not value:
        raise ValueError(message)


def keys(value, allowed, context):
    require(isinstance(value, dict), f'{context} must be an object')
    unknown = set(value)-set(allowed)
    require(not unknown, f'{context}: unsupported fields {sorted(unknown)}; exact chemistry was retained in the library')


def components(snapshot):
    if 'assembly_record' in snapshot:
        keys(snapshot['assembly_record']['identity'], {'components', 'bonds'}, 'Assembly identity')
    result = snapshot['components']
    require(result and len(result) <= 62, 'An assembly must have 1..62 explicit chains')
    seen = set()
    for component in result:
        keys(component, {'chain_id', 'construct_ref', 'record'}, 'Assembly component')
        chain = component['chain_id']
        require(isinstance(chain, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_]{0,31}', chain), 'Invalid chain identifier')
        require(chain not in seen, 'Duplicate chain identifier')
        seen.add(chain)
    return result


def ccd(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Z0-9]{1,8}', value), 'CCD identifiers must be uppercase letters/digits')
    return value


def polymer(component, snapshot):
    identity = materialized_identity(component['record'], snapshot)
    keys(identity, {'molecule_type', 'sequence', 'modifications', 'circular', 'termini', 'linkages', 'bonds', 'crosslinks',
                    'encoded_by', 'product_review', 'strand_count', 'molecular_form'},
         f"Chain {component['chain_id']} identity")
    require(identity.get('product_review', {}).get('status') != 'review_required',
            'Protein product definition requires review; its candidate remains visible but is excluded from prediction')
    require(identity.get('strand_count', 1) == 1 and identity.get('molecular_form') != 'plasmid',
            'Whole double-stranded plasmids are not supported prediction inputs; select a defined molecular product')
    molecule = identity['molecule_type']
    require(molecule in ALPHABETS, 'Mixed/custom polymers need a model-specific representation; they cannot be flattened to FASTA')
    sequence = identity.get('sequence')
    require(isinstance(sequence, str) and sequence and set(sequence) <= ALPHABETS[molecule],
            f'{molecule} prediction requires explicit canonical letters with optional CCD residue substitutions; ambiguous letters are not silently replaced')
    require(not identity.get('termini'), 'Explicit terminal chemistry is not supported by this native adapter')
    require(not identity.get('linkages'), 'Explicit backbone linkage chemistry is not supported by this native adapter')
    require(type(identity.get('circular', False)) is bool, 'circular must be boolean')
    modifications = []
    positions = set()
    for modification in identity.get('modifications', []):
        keys(modification, {'position', 'ccd', 'monomer_ref'}, 'Residue modification')
        position = modification.get('position')
        require(type(position) is int and 1 <= position <= len(sequence) and position not in positions,
                'Modification positions must be distinct 1-based residue positions')
        positions.add(position)
        require(('ccd' in modification) != ('monomer_ref' in modification), 'Specify exactly one CCD or monomer reference per modification')
        if 'monomer_ref' in modification:
            ref = modification['monomer_ref']
            require(ref in snapshot.get('monomers', {}), 'Missing pinned modification monomer')
            monomer = snapshot['monomers'][ref]['identity']
            keys(monomer, {'ccd'}, 'Native CCD monomer identity')
            code = ccd(monomer.get('ccd'))
        else:
            code = ccd(modification['ccd'])
        modifications.append({'position': position, 'ccd': code})
    return molecule, sequence, modifications, identity.get('circular', False)


def ligand(component, assets):
    """Return a losslessly parsed graph; original SDF/SMILES remains in snapshot."""
    from rdkit import Chem
    identity = component['record']['identity']
    keys(identity, {'molecule_type', 'smiles', 'ccd', 'structure_file', 'structure_format', 'bonds', 'crosslinks'},
         f"Ligand {component['chain_id']} identity")
    choices = [name for name in ('smiles', 'ccd', 'structure_file') if name in identity]
    require(len(choices) == 1, 'A ligand needs exactly one authoritative SMILES, CCD or attached structure')
    if choices[0] == 'ccd':
        require('structure_format' not in identity, 'structure_format requires a structure attachment')
        return {'ccd': ccd(identity['ccd'])}, None
    if choices[0] == 'smiles':
        require('structure_format' not in identity, 'structure_format requires a structure attachment')
        smiles = identity['smiles']
        require(isinstance(smiles, str) and smiles and not any(c.isspace() for c in smiles), 'SMILES must be one expression without a trailing name')
        mol = Chem.MolFromSmiles(smiles)
    else:
        require(identity.get('structure_format', 'sdf').lower() in {'sdf', 'mol'}, 'Only SDF/MOL structure attachments are supported')
        ref, relative = component['construct_ref'], identity['structure_file']
        require(ref in assets and relative in assets[ref], 'Ligand structure attachment is missing')
        path = Path(assets[ref][relative])
        if identity.get('structure_format', 'sdf').lower() == 'mol':
            mol = Chem.MolFromMolFile(str(path), removeHs=False, sanitize=True, strictParsing=True)
        else:
            supplier = Chem.SDMolSupplier(str(path), removeHs=False, sanitize=True, strictParsing=True)
            require(len(supplier) == 1, 'SDF must contain exactly one molecule; no records are discarded')
            mol = supplier[0]
        require(mol is not None, 'Unable to parse the original ligand structure')
        require(not mol.GetStereoGroups(), 'Enhanced/mixture stereochemistry cannot be represented by this model input')
        # SDF commonly stores ordinary hydrogen atoms explicitly; native SMILES
        # parsers use implicit H. RemoveHs preserves charged/stereochemical H
        # semantics while avoiding a false graph mismatch on the roundtrip.
        smiles = Chem.MolToSmiles(Chem.RemoveHs(mol), isomericSmiles=True)
    require(mol is not None and mol.GetNumAtoms() > 0, 'Invalid or empty ligand graph')
    require(all(atom.GetAtomicNum() > 0 for atom in mol.GetAtoms()), 'Wildcard/dummy attachment atoms are not a complete ligand')
    require(mol.GetNumHeavyAtoms() <= 512, 'Ligand exceeds the 512-heavy-atom native preflight limit; it was retained without truncation')
    require(not any(atom.GetIsotope() or atom.GetNumRadicalElectrons() for atom in mol.GetAtoms()),
            'This native model does not preserve explicit isotope/radical features; the original chemical identity remains in the library')
    require(not any(atom.HasQuery() for atom in mol.GetAtoms()) and not any(bond.HasQuery() for bond in mol.GetBonds()),
            'Query atoms/bonds do not define an exact ligand')
    require(all(str(bond.GetBondType()) in {'SINGLE', 'DOUBLE', 'TRIPLE', 'AROMATIC'} for bond in mol.GetBonds()),
            'The native adapter does not preserve unspecified, dative or other special bond types')
    require(not mol.GetStereoGroups(), 'Enhanced/mixture stereochemistry is not supported')
    require(all(atom.GetAtomMapNum() == 0 for atom in mol.GetAtoms()), 'Mapped-atom SMILES need explicit model atom mapping and are not silently renumbered')
    # Check the exported graph preserves explicit isotope/charge/stereochemistry.
    roundtrip = Chem.MolFromSmiles(smiles)
    require(roundtrip is not None and Chem.MolToSmiles(Chem.RemoveHs(roundtrip), isomericSmiles=True) == Chem.MolToSmiles(Chem.RemoveHs(mol), isomericSmiles=True),
            'Structure-to-SMILES conversion changed chemical identity')
    return {'smiles': smiles}, dict(heavy_atoms=mol.GetNumHeavyAtoms(), formal_charge=Chem.GetFormalCharge(mol),
                                  fragments=len(Chem.GetMolFrags(mol)), original_coordinates_used=False)


def bonds(snapshot):
    result = copy.deepcopy(snapshot.get('bonds', []))
    by_chain = {component['chain_id']: component for component in components(snapshot)}
    for chain, component in by_chain.items():
        identity = component['record']['identity']
        for field in ('bonds', 'crosslinks'):
            for bond in identity.get(field, []):
                bond = copy.deepcopy(bond)
                for side in ('from', 'to'):
                    require(bond[side].get('chain_id', 'A') == 'A', 'Internal construct bonds use implicit chain A')
                    bond[side]['chain_id'] = chain
                result.append(bond)
    for bond in result:
        keys(bond, {'from', 'to', 'order'}, 'Covalent bond')
        require(type(bond.get('order', 1)) in (int, str) and bond.get('order', 1) in (1, 'single'),
                'Only explicit single covalent bonds are supported')
        for side in ('from', 'to'):
            endpoint = bond[side]
            keys(endpoint, {'chain_id', 'position', 'atom'}, 'Bond endpoint')
            require(endpoint.get('chain_id') in by_chain, 'Unknown bond chain')
            component = by_chain[endpoint['chain_id']]
            identity = materialized_identity(component['record'], snapshot)
            length = len(identity.get('sequence', '')) or 1
            endpoint.setdefault('position', 1)
            require(type(endpoint['position']) is int and 1 <= endpoint['position'] <= length, 'Invalid bond residue position')
            require(isinstance(endpoint.get('atom'), str) and endpoint['atom'] and not any(c.isspace() for c in endpoint['atom']),
                    'This adapter requires explicit atom names, not ambiguous numeric atom indices')
        require(bond['from'] != bond['to'], 'Self-bonds are invalid')
    return result


def write_json(path, document):
    Path(path).write_text(json.dumps(document, indent=2, sort_keys=True, allow_nan=False)+'\n')
