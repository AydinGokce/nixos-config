from copy import deepcopy

from .common import CHUNK, UPLOAD, WIRE, number, require, string

FOLDING = {'boltz2', 'protenix', 'openfold3', 'rf3'}
FORMATS = ['sequence', 'fasta', 'smiles', 'ccd', 'sdf', 'pdb', 'mmcif', 'library-json', 'contigs']
MOLECULES = ['protein', 'dna', 'rna', 'ligand', 'assembly', 'structure']
TIMEOUT = {'type': 'integer', 'minimum': 60, 'maximum': 85500, 'default': 7200,
           'description': 'Existing managed worker work timeout in seconds; cleanup remains bounded separately.'}
MODEL_NAMES = ['esm2_t6_8M_UR50D', 'esm2_t12_35M_UR50D', 'esm2_t30_150M_UR50D',
               'esm2_t33_650M_UR50D', 'esm2_t36_3B_UR50D', 'esm2_t48_15B_UR50D']


def model(ident, name, workflow, molecules, formats, settings=None, description='', enabled=True):
    return {'id': ident, 'name': name, 'workflow': workflow, 'enabled': enabled,
            'disabled_reason': None if enabled else 'RFAA is parked; use a supported folding model.',
            'molecule_types': molecules, 'input_formats': formats,
            'settings': {'timeout': TIMEOUT, **(settings or {})}, 'description': description}


MODELS = [
    model('boltz2', 'Boltz 2', 'folding', MOLECULES[:-1], FORMATS[:5] + ['library-json'],
          {'seed': {'type': 'integer', 'minimum': 0, 'maximum': 2147483647,
                    'description': 'Explicit native seed. Seed 42 can use the qualified resident profile.'}},
          'Proteins, DNA, RNA, and native supported ligands/modifications. Native CPU parser checks every assembly.'),
    model('protenix', 'Protenix', 'folding', MOLECULES[:-1], FORMATS[:5] + ['library-json'],
          description='Native protein/nucleic acid/ligand inputs. Unsupported chemistry, including MSE converted to MET, rejects before launch.'),
    model('openfold3', 'OpenFold3', 'folding', MOLECULES[:-1], FORMATS[:5] + ['library-json'],
          description='Native mixed inputs. Explicit covalent bonds are rejected when native features cannot preserve them.'),
    model('rf3', 'RoseTTAFold3', 'folding', MOLECULES[:-1], FORMATS[:5] + ['library-json'],
          description='Native mixed inputs; polymers require at least four residues. Mandatory output chemistry checks remain enabled.'),
    model('esm', 'ESM-2', 'sequence-analysis', ['protein'], ['sequence', 'fasta', 'library-json'],
          {'sub': {'type': 'string', 'enum': ['score', 'embed', 'logits', 'mutate'], 'default': 'score'},
           'model': {'type': 'string', 'enum': MODEL_NAMES, 'default': 'esm2_t33_650M_UR50D'},
           'mutations': {'type': 'string', 'description': 'For mutate: comma-separated substitutions such as A12G,V14L.'}},
          'Canonical protein scoring, embeddings, logits, or explicit substitution effects. No sequence database required.'),
    model('evolvepro', 'EVOLVEpro', 'variant-ranking', ['protein'], ['sequence', 'fasta', 'library-json'],
          {'sub': {'type': 'string', 'enum': ['rank', 'embed'], 'default': 'rank'},
           'model': {'type': 'string', 'enum': MODEL_NAMES, 'default': 'esm2_t33_650M_UR50D'},
           'num': {'type': 'integer', 'minimum': 1, 'maximum': 10000, 'default': 12},
           'regressor': {'type': 'string', 'enum': ['rf', 'xgb'], 'default': 'rf'},
           'seed': {'type': 'integer', 'minimum': 0, 'maximum': 2147483647, 'default': 0},
           'labels_upload_id': {'type': 'string', 'description': 'Completed CSV upload with variant,activity columns.'}},
          'A FASTA remains a variant set. Labels are optional; native validation preserves every variant and requires an unmeasured candidate.'),
    model('mpnn', 'ProteinMPNN', 'sequence-design', ['structure'], ['pdb'],
          {'num': {'type': 'integer', 'minimum': 1, 'maximum': 10000, 'default': 8},
           'temperature': {'type': 'number', 'minimum': 0.0001, 'maximum': 10, 'default': 0.1}},
          'Canonical protein backbone PDB with complete N/CA/C/O atoms. Produces sequences, not folding predictions.'),
    model('rfdiffusion', 'RFdiffusion', 'backbone-design', ['structure'], ['pdb', 'contigs'],
          {'num': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 2},
           'contigs': {'type': 'string', 'description': 'Native contig ranges, e.g. [50-50] or [A1-20/30-30].'}},
          'Backbone generation or motif-conditioned design. Explicit contig ranges are required.'),
    model('rfaa', 'RoseTTAFold All-Atom (parked)', 'folding', MOLECULES[:-1], [], enabled=False),
]


def catalog():
    return {'version': 1, 'models': deepcopy(MODELS), 'input_formats': FORMATS,
            'molecule_types': MOLECULES, 'msa_backends': ['public', 'private'],
            'executions': ['auto', 'resident', 'ephemeral'],
            'limits': {'wire_bytes': WIRE, 'chunk_bytes': CHUNK, 'upload_bytes': UPLOAD,
                       'inputs': 128, 'pairs': 512, 'list_entries': 100}}


def specification(ident):
    found = next((m for m in MODELS if m['id'] == ident), None)
    require(found is not None, 'Unknown model: ' + str(ident))
    return found


def check_settings(ident, values):
    require(isinstance(values, dict), 'Model settings must be an object')
    allowed = specification(ident)['settings']
    require(not set(values) - set(allowed), 'Unsupported settings for ' + ident + ': ' + ', '.join(sorted(set(values) - set(allowed))))
    for key, value in values.items():
        spec = allowed[key]
        if spec['type'] == 'integer':
            number(value, key, spec['minimum'], spec['maximum'])
        elif spec['type'] == 'number':
            require(type(value) in (int, float) and spec['minimum'] <= value <= spec['maximum'], 'Invalid ' + key)
        else:
            string(value, key, 4096)
        if 'enum' in spec:
            require(value in spec['enum'], 'Unsupported ' + key)
    return deepcopy(values)
