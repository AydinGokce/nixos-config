"""Set an explicitly requested campaign RNG seed, then run pinned BindCraft.

This preserves upstream settings and filters. Native trajectory seeds are still
chosen by BindCraft and retained in its CSVs; GPU/Rosetta bitwise reproducibility
is not claimed by this campaign seed.
"""
import argparse
from pathlib import Path
import random
import runpy
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--seed', type=int, required=True)
    args, native = parser.parse_known_args()
    if not 0 <= args.seed <= 2147483647:
        parser.error('Campaign seed must be in 0..2147483647')
    import numpy as np
    random.seed(args.seed)
    np.random.seed(args.seed)
    source = args.source.resolve(strict=True)
    sys.path.insert(0, str(source.parent))
    sys.argv = [str(source), *native]
    print('BindCraft campaign seed: ' + str(args.seed), flush=True)
    runpy.run_path(str(source), run_name='__main__')


if __name__ == '__main__':
    main()
