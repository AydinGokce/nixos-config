#!/usr/bin/env python3
"""Handle successful HHsearch results with no usable coordinate templates.

RFAA d69ab3a discards hits with fewer than ten aligned resolved residues, then
unconditionally concatenates the remaining hits. Empty tensors let its existing
TemplFeaturize no-template path handle this legitimate search result. Malformed
files and search failures continue to raise their original errors.
"""
from pathlib import Path
import sys


EMPTY_RESULT = '''    # No usable coordinates after RFAA's template filtering.
    if not xyz:
        return (torch.empty((0, ChemData().NTOTAL, 3)),
                torch.empty((0, ChemData().NTOTAL), dtype=torch.bool),
                torch.empty((0, 2), dtype=torch.int64),
                torch.empty((0, 8)), torch.empty((0, 3)),
                torch.empty((0,), dtype=torch.int64), [])

'''


def patch(path):
    source = path.read_text()
    start = source.index("def parse_templates_raw(")
    end = source.index("\ndef ", start + 1)
    function = source[start:end]
    if EMPTY_RESULT in function:
        return
    anchor = "    xyz = np.vstack(xyz).astype(np.float32)\n"
    if function.count(anchor) != 1:
        raise ValueError("RFAA template parser changed; empty-template patch cannot be applied")
    function = function.replace(anchor, EMPTY_RESULT + anchor)
    path.write_text(source[:start] + function + source[end:])


if __name__ == "__main__":
    try:
        patch(Path(sys.argv[1]))
    except (IndexError, OSError, ValueError) as error:
        sys.exit(f"rfaa: template patch failed: {error}")
