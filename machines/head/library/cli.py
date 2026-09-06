#!/usr/bin/env python3
"""Head construct library CLI, including model input compatibility checks."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import registry


def main():
    if len(sys.argv) == 1 or sys.argv[1:] in (['--help'], ['-h']):
        print('Check model input compatibility: bio-library check REF --model MODEL\n'
              'Native folding inputs use the CPU parser; private RF3 supports native assemblies. Other private MSA inputs and ESM/EVOLVEpro use canonical FASTA checks.\n'
              'No prediction or MSA queries.\n')
    if len(sys.argv) < 2 or sys.argv[1] != 'check':
        return registry.main()
    parser = argparse.ArgumentParser(description='Check model input compatibility for a pinned construct/assembly. Native folding inputs and private RF3 use the CPU parser; other private MSA inputs and ESM/EVOLVEpro use canonical FASTA checks. No prediction or MSA queries.')
    parser.add_argument('ref')
    parser.add_argument('--model', required=True, choices=['boltz2', 'protenix', 'openfold3', 'rfaa', 'rf3', 'esm', 'evolvepro'])
    parser.add_argument('--root', default=os.environ.get('BIO_LIBRARY_ROOT', '/var/lib/bio-library'))
    parser.add_argument('--msa-backend', choices=['public', 'private'], default='public')
    args = parser.parse_args(sys.argv[2:])
    if args.msa_backend == 'private' and args.model not in {'boltz2', 'protenix', 'openfold3', 'rf3'}:
        raise ValueError('This model does not use the shared private MSA backend')
    with tempfile.TemporaryDirectory(prefix='bio-library-check-') as work:
        command = [sys.executable, str(Path(__file__).with_name('runtime.py')), '--root', args.root,
                   '--ref', args.ref, '--model', args.model, '--out', work+'/input', '--msa-backend', args.msa_backend]
        if args.msa_backend == 'private' and args.model != 'rf3':
            command.append('--plain-fasta')
        result = subprocess.run(command, text=True, capture_output=True)
        if result.stderr:
            print(result.stderr, end='', file=sys.stderr)
        if result.returncode:
            return result.returncode
        data = json.loads(result.stdout)
        data.pop('bundle', None)
        data.pop('entrypoint', None)
        data['compatible'] = True
        print(json.dumps(data, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, registry.Error) as error:
        print('bio-library: '+str(error), file=sys.stderr)
        sys.exit(2)
