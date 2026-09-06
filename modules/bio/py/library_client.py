#!/usr/bin/env python3
"""SSH client and verified local snapshots for the head's construct library."""
import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid


def require(condition, message):
    if not condition:
        raise ValueError(message)


def no_symlinks(path):
    path = Path(path).absolute()
    for item in [*reversed(path.parents), path]:
        if item.is_symlink():
            raise ValueError(f'Symlinks are not allowed for backup/output paths: {item}')


def atomic_json(path, value, overwrite=True):
    no_symlinks(path)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with temporary.open('x') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        if overwrite:
            temporary.replace(path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def registry_module():
    path = os.environ.get('BIO_LIBRARY_REGISTRY', '/etc/bio/library/registry.py')
    spec = importlib.util.spec_from_file_location('bio_library_registry', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def context_module():
    registry_path = Path(os.environ.get('BIO_LIBRARY_REGISTRY', '/etc/bio/library/registry.py'))
    path = registry_path.with_name('context.py')
    spec = importlib.util.spec_from_file_location('bio_library_context', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def normalize_origin(value):
    require(isinstance(value, dict) and set(value) == {'head', 'library_root'}, 'Invalid backup origin')
    head, root = value['head'], value['library_root']
    require(isinstance(head, str) and head and isinstance(root, str) and root.startswith('/'), 'Invalid backup origin')
    return {'head': head.lower(), 'library_root': '/' + posixpath.normpath(root).lstrip('/')}


def check_backup_origin(destination, origin, *, bind=False):
    """Reject mixed histories before transfer; never infer origin from chemistry."""
    destination = Path(destination)
    origin = normalize_origin(origin) if origin is not None else None
    marker = destination/'origin.json'
    known, legacy = [], False
    if marker.exists() or marker.is_symlink():
        no_symlinks(marker)
        record = json.loads(marker.read_text())
        require(isinstance(record, dict) and record.get('kind') == 'bio-library-backup-origin' and record.get('schema') == 1,
                'Invalid backup origin marker; select a separate backup destination')
        known.append(normalize_origin(record['origin']))
    paths = list(destination.glob('library-*.tar.gz.json'))
    latest = destination/'latest.json'
    if latest.exists() or latest.is_symlink():
        paths.append(latest)
    for path in paths:
        no_symlinks(path)
        record = json.loads(path.read_text())
        if not isinstance(record, dict) or record.get('kind') != 'bio-library-local-backup' or record.get('schema') != 1:
            continue
        if 'origin' in record:
            known.append(normalize_origin(record['origin']))
        else:
            legacy = True
    require(not known or origin is not None and all(value == origin for value in known),
            'Backup destination belongs to a different head/library root; select a separate backup destination')
    require(not legacy or known or origin is None,
            'Existing backups have no recorded origin; preserve them and select a separate backup destination, or explicitly migrate their verified origin')
    if bind and origin is not None and not marker.exists():
        atomic_json(marker, dict(schema=1, kind='bio-library-backup-origin', origin=origin), overwrite=False)
    return origin


class Client:
    def __init__(self, runner=subprocess.run):
        self.runner = runner
        host = os.environ.get('BIO_CLUSTER_HEAD', '')
        user = os.environ.get('BIO_CLUSTER_USER', 'root')
        key = Path(os.environ.get('BIO_CLUSTER_SSHKEY', str(Path.home()/'.ssh/datacrunch_ed25519'))).expanduser()
        require(host and not host.startswith('-') and all(c.isalnum() or c in '.:-' for c in host), 'Configure a valid BIO_CLUSTER_HEAD')
        require(user and all(c.isalnum() or c in '_-' for c in user), 'Invalid SSH user')
        require(key.is_file(), f'SSH key is missing: {key}')
        self.destination = user + '@' + host
        remote_root = os.environ.get('BIO_LIBRARY_REMOTE_ROOT') or '/var/lib/bio-library'
        require(remote_root.startswith('/'), 'BIO_LIBRARY_REMOTE_ROOT must be an absolute head path')
        self.origin = normalize_origin({'head': host, 'library_root': remote_root})
        # Make the recorded origin explicit even if the remote login shell has
        # a different BIO_LIBRARY_ROOT in its environment.
        self.library_command = ['env', 'BIO_LIBRARY_ROOT='+self.origin['library_root'], 'bio-library']
        self.options = ['-i', str(key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                        '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=15',
                        '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']

    def ssh(self, args, **kwargs):
        return self.runner(['ssh', *self.options, self.destination, shlex.join(map(str, args))],
                           check=True, timeout=600, **kwargs)

    def copy_to(self, local, remote):
        self.runner(['scp', '-q', *self.options, str(local), self.destination + ':' + remote], check=True, timeout=600)

    def copy_from(self, remote, local):
        self.runner(['scp', '-q', *self.options, self.destination + ':' + remote, str(local)], check=True, timeout=600)

    def temporary(self):
        path = '/tmp/bio-library-client-' + uuid.uuid4().hex
        self.ssh(['mkdir', '-m', '0700', '--', path])
        return path

    def remove_temporary(self, path):
        require(re.fullmatch(r'/tmp/bio-library-client-[0-9a-f]{32}', path) is not None,
                'Refusing cleanup outside this client temporary directory')
        try:
            self.ssh(['rm', '-rf', '--', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.SubprocessError, OSError):
            print('bio-library: remote temporary directory could not be removed: ' + path, file=sys.stderr)

    def export(self, output):
        output = Path(output).expanduser()
        no_symlinks(output)
        require(not output.exists() and not output.is_symlink(), f'Output already exists: {output}')
        output.parent.mkdir(parents=True, exist_ok=True)
        check_backup_origin(output.parent, self.origin)
        remote = self.temporary()
        partial = output.with_name('.' + output.name + '.' + uuid.uuid4().hex + '.part')
        try:
            self.ssh([*self.library_command, 'export-snapshot', '--out', remote + '/snapshot.tar.gz'],
                     stdout=subprocess.DEVNULL)
            self.copy_from(remote + '/snapshot.tar.gz', partial)
            registry_module().verify_backup(partial)
            with partial.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            no_symlinks(output)
            os.link(partial, output)
            return dict(path=str(output), sha256=digest, bytes=output.stat().st_size, origin=dict(self.origin))
        finally:
            partial.unlink(missing_ok=True)
            self.remove_temporary(remote)

    def forward(self, arguments):
        require(arguments, 'Expected a library command; use --help')
        # Argparse accepts both --file PATH and --file=PATH. Normalize before
        # deciding which paths belong to this workstation rather than the head.
        normalized = []
        file_options = {'--json', '--fasta', '--sdf', '--attachment', '--description', '--brief', '--markdown', '--out'}
        options = True
        for value in arguments:
            option, separator, payload = value.partition('=')
            if options and separator and option in file_options:
                normalized.extend((option, payload))
            else:
                normalized.append(value)
            if value == '--':
                options = False
        arguments = normalized
        command = arguments[0]
        if command == 'context':
            parser = argparse.ArgumentParser(prog='bio-library context', description='Export exact project goals and constructs for agent analysis; runs no models.')
            parser.add_argument('ref')
            parser.add_argument('--out', type=Path, required=True)
            args = parser.parse_args(arguments[1:])
            print(json.dumps(self.context(args.ref, args.out), indent=2)); return
        if command == 'export-snapshot':
            parser = argparse.ArgumentParser(prog='bio-library export-snapshot')
            parser.add_argument('--out', type=Path, required=True)
            args = parser.parse_args(arguments[1:])
            print(json.dumps(self.export(args.out), indent=2)); return
        staged = None
        forwarded = []
        local_output = None
        try:
            position = 0
            while position < len(arguments):
                value = arguments[position]
                if value in ('--json', '--fasta', '--sdf', '--attachment', '--description', '--brief', '--markdown'):
                    require(position+1 < len(arguments), f'{value} needs a value')
                    raw = arguments[position+1]
                    name = None
                    if value == '--attachment':
                        require('=' in raw, 'Attachments use NAME=LOCAL_FILE')
                        name, raw = raw.split('=', 1)
                    path = Path(raw).expanduser().resolve(strict=True)
                    require(path.is_file(), 'Input must be a regular file')
                    if staged is None:
                        staged = self.temporary()
                    remote_directory = staged + '/' + str(position)
                    self.ssh(['mkdir', '-m', '0700', '--', remote_directory])
                    remote = remote_directory + '/' + path.name
                    self.copy_to(path, remote)
                    forwarded += [value, (name + '=' if name is not None else '') + remote]
                    position += 2
                elif value == '--out' and command == 'snapshot':
                    require(position+1 < len(arguments), '--out needs a value')
                    local_output = Path(arguments[position+1]).expanduser()
                    no_symlinks(local_output)
                    require(not local_output.exists() and not local_output.is_symlink(), 'Snapshot output already exists')
                    position += 2
                else:
                    forwarded.append(value); position += 1
            if local_output is None:
                self.ssh([*self.library_command, *forwarded])
            else:
                result = self.ssh([*self.library_command, *forwarded], capture_output=True, text=True)
                document = json.loads(result.stdout)
                registry_module().verify_document(document)
                local_output.parent.mkdir(parents=True, exist_ok=True)
                atomic_json(local_output, document, overwrite=False)
                print(str(local_output))
        finally:
            if staged is not None:
                self.remove_temporary(staged)

    def context(self, ref, output):
        output = Path(output).expanduser().absolute()
        no_symlinks(output)
        require(not output.exists(), f'Workspace already exists: {output}')
        output.parent.mkdir(parents=True, exist_ok=True)
        shown = self.ssh([*self.library_command, 'show', ref], capture_output=True, text=True)
        record = registry_module().verify_document(json.loads(shown.stdout))
        require(record.get('kind') == 'project', 'Context requires a project reference')
        pinned_ref = registry_module().reference(record)
        remote = self.temporary()
        try:
            self.ssh([*self.library_command, 'context', pinned_ref, '--archive', remote + '/context.tar.gz'],
                     stdout=subprocess.DEVNULL)
            with tempfile.TemporaryDirectory(prefix='.bio-context-', dir=output.parent) as local:
                archive = Path(local)/'context.tar.gz'
                self.copy_from(remote + '/context.tar.gz', archive)
                module = context_module()
                verified = module.verify_archive(archive)
                require(verified.get('project_ref') == pinned_ref
                        and verified.get('records', {}).get(pinned_ref, {}).get('sha256') == record['sha256'],
                        'Transferred context differs from the requested project revision')
                manifest = module.extract_archive(archive, output)
            # Working notes are outside the immutable, checksummed input set.
            atomic_json(output/'analysis'/'origin.json', dict(schema=1, origin=dict(self.origin),
                        exported_at=dt.datetime.now(dt.timezone.utc).isoformat()), overwrite=False)
            return dict(directory=str(output), project_ref=manifest.get('project_ref'),
                        manifest_sha256=manifest.get('sha256'), origin=dict(self.origin), verified=True)
        finally:
            self.remove_temporary(remote)


def retained_receipts(receipts, now):
    """Retain newest snapshots in 24 hourly, 30 daily and 12 monthly buckets."""
    keep = set()
    for unit, limit in [('hour', 24), ('day', 30), ('month', 12)]:
        buckets = {}
        for receipt in sorted(receipts, key=lambda item: item['created_at'], reverse=True):
            when = dt.datetime.fromisoformat(receipt['created_at'])
            require(when.tzinfo is not None and when <= now + dt.timedelta(minutes=5), 'Invalid backup timestamp')
            bucket = when.strftime({'hour':'%Y-%m-%dT%H', 'day':'%Y-%m-%d', 'month':'%Y-%m'}[unit])
            if bucket not in buckets and len(buckets) < limit:
                buckets[bucket] = receipt['filename']
        keep.update(buckets.values())
    return keep


def backup(destination, client=None, now=None):
    destination = Path(destination).expanduser()
    no_symlinks(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    now = now or dt.datetime.now(dt.timezone.utc)
    lock_fd = os.open(destination/'.backup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    require(stat.S_ISREG(os.fstat(lock_fd).st_mode), 'Backup lock must be a regular file')
    with os.fdopen(lock_fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        active_client = client or Client()
        origin = check_backup_origin(destination, getattr(active_client, 'origin', None), bind=True)
        filename = 'library-' + now.strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8] + '.tar.gz'
        output = destination / filename
        result = active_client.export(output)
        if origin is not None:
            require(result.get('origin') == origin, 'Export origin changed during backup; no snapshots were pruned')
        receipt = dict(schema=1, kind='bio-library-local-backup', filename=filename,
                       created_at=now.isoformat(), **result)
        atomic_json(destination/(filename+'.json'), receipt)
        # Restore every successful snapshot to an isolated temporary directory.
        # This detects invalid references as well as intact compressed bytes.
        module = registry_module()
        with tempfile.TemporaryDirectory(prefix='.restore-check-', dir=destination) as temp:
            restored = Path(temp)/'library'
            module.restore_backup(output, restored)
            module.Registry(restored).verify()
        check_backup_origin(destination, origin)
        receipt['restore_checked'] = True
        atomic_json(destination/(filename+'.json'), receipt)
        atomic_json(destination/'latest.json', receipt)
        owned = []
        for path in destination.glob('library-*.tar.gz.json'):
            if path.is_symlink():
                continue
            item = json.loads(path.read_text())
            if item.get('kind') == 'bio-library-local-backup' and item.get('schema') == 1 and item.get('restore_checked') is True:
                # Originless legacy files can coexist with an explicitly bound
                # directory, but their unknown history must never be pruned.
                if origin is not None and item.get('origin') != origin:
                    continue
                name = item.get('filename', '')
                require(Path(name).name == name and path.name == name+'.json', 'Invalid backup receipt filename')
                if (destination/name).is_file() and not (destination/name).is_symlink():
                    owned.append(item)
        keep = retained_receipts(owned, now)
        for item in owned:
            if item['filename'] not in keep:
                (destination/item['filename']).unlink()
                (destination/(item['filename']+'.json')).unlink()
        return receipt


def main():
    os.umask(0o077)
    arguments = sys.argv[1:]
    if not arguments or arguments in (['--help'], ['-h']):
        print('''bio-library COMMAND [OPTIONS]

Store and reuse molecular constructs on the configured cloud head.
  import           Import --fasta FILE --type protein|dna|rna --id NAME,
                   --sdf FILE --id NAME, --smiles TEXT --id NAME, or --json FILE
  list / show REF  Find constructs, monomers, assemblies and projects
  revise REF       Create an immutable revision with --json PATCH
  describe REF     Read its purpose; revise with --markdown FILE
  project          Create --id NAME --brief FILE [--member REF ...]
  context REF      Export a Codex project workspace with --out DIRECTORY
  context-verify   Verify frozen project inputs locally: DIRECTORY
  snapshot REF     Resolve all pinned components; --out FILE saves locally
  check REF        Check model input compatibility with --model MODEL
  verify / reindex Check stored records or rebuild the searchable index
  backup           Pull and restore-check a snapshot in ~/bio-library-backups
  export-snapshot  Export a verified backup with --out LOCAL.tar.gz
  restore-local    Restore --from LOCAL.tar.gz --to EMPTY_DIRECTORY

Prediction: bio-fold MODEL --construct REF | --assembly REF
Models: boltz2, protenix, openfold3, rf3, rfaa; canonical proteins: esm, evolvepro
Import accepts --description FILE for intended function and success criteria.
Compatibility checks use the CPU parser for native folding inputs including
private RF3; other private MSA inputs and ESM/EVOLVEpro use canonical FASTA checks.
Project exports run no models. Predictions alone do not establish function.
Use bio-library COMMAND --help for command-specific options.''')
    elif arguments[0] == 'context-verify':
        parser = argparse.ArgumentParser(prog='bio-library context-verify')
        parser.add_argument('directory', type=Path)
        args = parser.parse_args(arguments[1:])
        print(json.dumps(context_module().verify_directory(args.directory), indent=2))
    elif arguments[0] == 'backup':
        parser = argparse.ArgumentParser(prog='bio-library backup')
        parser.add_argument('--destination', default=str(Path.home()/'bio-library-backups'))
        args = parser.parse_args(arguments[1:])
        print(json.dumps(backup(args.destination), indent=2))
    elif arguments and arguments[0] == 'restore-local':
        parser = argparse.ArgumentParser(prog='bio-library restore-local')
        parser.add_argument('--from', required=True, dest='source', type=Path)
        parser.add_argument('--to', required=True, dest='destination', type=Path)
        args = parser.parse_args(arguments[1:])
        print(json.dumps(registry_module().restore_backup(args.source, args.destination), indent=2))
    else:
        Client().forward(arguments or ['--help'])


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('bio-library: ' + str(error), file=sys.stderr)
        sys.exit(1)
