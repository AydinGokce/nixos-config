"""Pinned RF3/AtomWorks transient atom-index compatibility, no chemistry edits."""
import hashlib
from importlib.metadata import version
import os
from pathlib import Path
import subprocess

PIN = 'b02eed6a6bdf8f44d14a80cc36e3da13c9f2291c'
CORRECTION = 'rf3-atomworks22-transient-atom-id-v1'


def install():
    """Match the parser input to AddGlobalAtomIdAnnotation's fresh-index contract.

    All molecular arrays and the original InferenceInput remain untouched.
    Only the copy returned by to_pipeline_input loses its temporary atom_id.
    """
    import numpy as np
    from rf3.utils import inference
    if getattr(inference.InferenceInput.to_pipeline_input, '_bio_correction', None) == CORRECTION:
        return
    source = Path(os.environ.get('RF3_SOURCE_DIR', '/mnt/bio-shared/src/foundry-rf3-'+PIN))
    if version('atomworks') != '2.2.1':
        raise ValueError('RF3 atom-index compatibility requires AtomWorks 2.2.1')
    commit = subprocess.check_output(['git', '-c', 'safe.directory='+str(source), '-C', str(source),
                                      'rev-parse', 'HEAD'], text=True).strip()
    installed = Path(inference.__file__)
    expected = subprocess.check_output(['git', '-c', 'safe.directory='+str(source), '-C', str(source),
                                        'show', PIN+':models/rf3/src/rf3/utils/inference.py'])
    if commit != PIN or hashlib.sha256(installed.read_bytes()).digest() != hashlib.sha256(expected).digest():
        raise ValueError('RF3 atom-index compatibility requires the exact pinned inference source')
    original = inference.InferenceInput.to_pipeline_input

    def to_pipeline_input(instance):
        data = original(instance)
        array = data['atom_array']
        if 'atom_id' in array.get_annotation_categories():
            # Deleting an annotation must never modify atom identities/order,
            # geometry, charge, bonds, or any other native chemical annotation.
            inventory = {name: array.get_annotation(name).copy()
                         for name in array.get_annotation_categories() if name != 'atom_id'}
            coordinates, bonds = array.coord.copy(), array.bonds.as_array().copy()
            array.del_annotation('atom_id')
            unchanged = (all(np.array_equal(array.get_annotation(name), value, equal_nan=True)
                             if value.dtype.kind in 'fc' else np.array_equal(array.get_annotation(name), value)
                             for name, value in inventory.items())
                         and np.array_equal(coordinates, array.coord, equal_nan=True)
                         and np.array_equal(bonds, array.bonds.as_array()))
            if not unchanged:
                raise ValueError('RF3 transient annotation cleanup changed a molecular array')
        return data

    to_pipeline_input._bio_correction = CORRECTION
    to_pipeline_input.__wrapped__ = original
    inference.InferenceInput.to_pipeline_input = to_pipeline_input
