#!/usr/bin/env python3
"""ColabFold-compatible searches for RF3, retaining raw data and explicit pairing.

API contract: Boltz 2.2.1 run_mmseqs2, env (filtered UniRef + environmental),
pairgreedy (UniRef pairing). The server's paired row order is encoded as RF3
pair keys in TaxID fields; these synthetic keys are NOT biological taxonomy.
"""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import prepare as binding

Error = binding.Error
A3M_NAMES = ('uniref.a3m', 'bfd.mgnify30.metaeuk30.smag30.a3m')
PAIR_BASE = 10**19 - 1


def queries(value):
    if not isinstance(value, dict) or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,31}', k)
         or not isinstance(v, str) or not re.fullmatch(r'[ACDEFGHIKLMNPQRSTVWYX]+', v) for k,v in value.items()):
        raise Error('RF3 MSA queries must map explicit protein chain IDs to their exact sequences')
    return value


def input_queries(fasta=None, native_json=None):
    value = binding.fasta_document(fasta)[0] if fasta else binding.document(binding.read_json(native_json))
    return binding.protein_queries(value)


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False)+'\n')


def inventory(root, exclude):
    return {str(p.relative_to(root)): binding.file_hash(p) for p in sorted(root.rglob('*'))
            if p.is_file() and p != root/exclude}


def seal(root, name, value):
    value['files'] = inventory(root, name)
    value.pop('sha256', None)
    value['sha256'] = binding.digest(value)
    save(root/name, value)


def verify_inventory(root, name):
    value = binding.read_json(root/name)
    if value.get('sha256') != binding.digest({k:v for k,v in value.items() if k != 'sha256'}):
        raise Error('RF3 MSA search manifest integrity mismatch')
    for p in (root, *root.parents, *root.rglob('*')):
        if p.is_symlink():
            raise Error('RF3 MSA search bundles must not contain symlinks')
    if value.get('files') != inventory(root, name):
        raise Error('RF3 MSA search file inventory or SHA256 mismatch')
    return value


def split_a3m(data, expected):
    """MMseqs result files are NUL-delimited, one full A3M per unique query."""
    result = {}
    for chunk in data.decode('utf-8').split('\x00'):
        if not chunk.strip():
            continue
        match = re.match(r'>([0-9]+)\r?\n', chunk)
        if not match or int(match[1]) not in expected or int(match[1]) in result:
            raise Error('Unexpected/duplicate query ID in MMseqs A3M response')
        result[int(match[1])] = chunk if chunk.endswith('\n') else chunk+'\n'
    if set(result) != set(expected):
        raise Error('MMseqs response omitted one or more submitted queries')
    return result


def records(text):
    result = []
    for line in text.splitlines(keepends=True):
        if line.startswith('>'):
            result.append([line, ''])
        elif line.strip():
            if not result:
                raise Error('A3M row appears before its header')
            result[-1][1] += line
    if not result or any(not seq.strip() for _,seq in result):
        raise Error('Empty A3M alignment or sequence')
    return result


def merge_alignments(query_map, unpaired, paired=None):
    """Keep all source unpaired rows; encode server pairing without re-pairing."""
    unique = list(dict.fromkeys(query_map.values()))
    ids = {seq: 101+i for i,seq in enumerate(unique)}
    if paired:
        depths = {len(records(text)) for text in paired.values()}
        if len(depths) != 1:
            raise Error('MMseqs paired alignments differ in depth across query chains')
        for ident, text in paired.items():
            expected = unique[ident-101]
            for _, sequence in records(text):
                aligned = re.sub('[a-z]', '', ''.join(sequence.split()))
                if len(aligned) != len(expected):
                    raise Error('MMseqs paired row width differs from its query')
    output = {}
    for chain, seq in query_map.items():
        ident = ids[seq]
        base = records(unpaired[0][ident])
        merged = [base[0]]
        if paired:
            rows = records(paired[ident])
            if ''.join(rows[0][1].split()) != seq:
                raise Error('MMseqs pairing query differs from submitted sequence')
            for index, (header, sequence) in enumerate(rows[1:], 1):
                if re.search(r'(?:^|\s)TaxID=', header):
                    raise Error('Paired server headers already contain TaxID; refusing ambiguous remapping')
                # Gap-only rows mean the source API did not pair this chain.
                # Leave it absent so RF3 inserts gaps for this exact pair key.
                if set(re.sub('[a-z]', '', ''.join(sequence.split()))) == {'-'}:
                    continue
                merged.append([header.rstrip('\r\n')+f' RF3ServerPair={index} TaxID={PAIR_BASE-index}\n', sequence])
        merged.extend(base[1:])
        for source in unpaired[1:]:
            rows = records(source[ident])
            if ''.join(rows[0][1].split()) != seq:
                raise Error('Environmental query differs from submitted sequence')
            merged.extend(rows[1:])
        output[chain] = ''.join(header+sequence for header,sequence in merged)
    return output


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@contextlib.contextmanager
def public_query_lock(deadline):
    """Share the worker clients' NFSv4 lease for the entire public search."""
    path = os.environ.get('BIO_PUBLIC_MSA_LOCK')
    if not path:
        yield  # Standalone library callers may supply their own coordinator.
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Error('Public MSA query lock must be a regular file')
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise Error('RF3 deadline exceeded waiting for public MSA query slot')
                time.sleep(min(0.5, max(0, deadline - time.time())))
        yield
    finally:
        os.close(fd)


class Client:
    def __init__(self, endpoint, source, deadline):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
            raise Error('MSA endpoint must be an origin without credentials, query or path')
        if source == 'private':
            if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost'):
                raise Error('Private RF3 MSA search must use the worker localhost audit API')
        elif parsed.scheme != 'https':
            raise Error('Public RF3 MSA search requires HTTPS')
        self.endpoint, self.deadline = endpoint.rstrip('/'), deadline
        self.opener = (urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                       if source == 'private' else urllib.request.build_opener())
    def remaining(self):
        remaining = self.deadline-time.time()
        if remaining <= 0:
            raise Error('RF3 MSA search deadline exceeded; no prediction submitted')
        return remaining
    def pause(self):
        time.sleep(min(5, self.remaining()))
    def request(self, path, data=None, dest=None):
        body = urllib.parse.urlencode(data).encode() if data else None
        request = urllib.request.Request(self.endpoint+path, data=body,
                    headers={'User-Agent':'bio-rf3-msa/1 (ColabFold API; Boltz 2.2.1 contract)'})
        for attempt in range(6):
            try:
                with self.opener.open(request, timeout=min(60, self.remaining())) as response:
                    if dest:
                        with Path(dest).open('wb') as out:
                            while chunk := response.read(1024*1024):
                                self.remaining()
                                out.write(chunk)
                        return None
                    return json.load(response)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code not in (429,500,502,503,504):
                    raise Error(f'MSA API HTTP {exc.code}') from None
                if attempt == 5:
                    raise Error('MSA API request failed after six bounded attempts') from exc
                self.pause()
    def search(self, sequences, kind, mode, out):
        query = ''.join(f'>{101+i}\n{seq}\n' for i,seq in enumerate(sequences))
        while True:
            status = self.request('/ticket/'+kind, {'q':query,'mode':mode})
            if status.get('status') not in ('UNKNOWN','RATELIMIT'):
                break
            self.pause()
        ticket = status.get('id')
        if not isinstance(ticket, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', ticket):
            raise Error('MSA server rejected job: '+str(status.get('status')))
        while status.get('status') in ('UNKNOWN','RUNNING','PENDING'):
            self.pause()
            status = self.request('/ticket/'+ticket)
        if status.get('status') != 'COMPLETE':
            raise Error('MSA job did not complete: '+str(status.get('status')))
        out.mkdir(parents=True)
        save(out/'request.json', {'endpoint':self.endpoint,'ticket':ticket,'kind':kind,'mode':mode,'query':query})
        self.request('/result/download/'+ticket, dest=out/'out.tar.gz')
        names = A3M_NAMES if kind == 'msa' else ('pair.a3m',)
        with tarfile.open(out/'out.tar.gz', 'r:gz') as archive:
            for name in names:
                matching = [m for m in archive.getmembers() if m.name.removeprefix('./') == name]
                if len(matching) != 1 or not matching[0].isfile():
                    raise Error('MSA archive missing/duplicate/nonregular '+name)
                with archive.extractfile(matching[0]) as handle, (out/name).open('wb') as dest:
                    shutil.copyfileobj(handle,dest)
        return [split_a3m((out/name).read_bytes(), range(101,101+len(sequences))) for name in names]


def search(query_map, out, endpoint, source, deadline, provenance=None):
    query_map = queries(query_map)
    if source not in ('public','private') or (query_map and (source == 'private') != bool(provenance)):
        raise Error('Private searches require exact database provenance; public searches must not claim it')
    out = Path(out).absolute()
    if out.exists():
        raise Error('MSA search destination already exists')
    out.mkdir(parents=True)
    unique = list(dict.fromkeys(query_map.values()))
    pair = None
    if unique:
        client = Client(endpoint, source, deadline)
        guard = public_query_lock(deadline) if source == 'public' else contextlib.nullcontext()
        with guard:
            raw = client.search(unique, 'msa', 'env', out/'raw/unpaired')
            if len(unique) > 1:
                pair = client.search(unique, 'pair', 'pairgreedy', out/'raw/paired')[0]
        alignments = merge_alignments(query_map, raw, pair)
    else:
        alignments = {}
    evidence = {}
    (out/'msas').mkdir()
    for chain, alignment in alignments.items():
        path = out/'msas'/(chain+'.a3m')
        path.write_text(alignment)
        evidence[chain] = dict(binding.validate_a3m(path, query_map[chain]), path=str(path.relative_to(out)))
    if provenance:
        identity = binding.read_json(provenance)
        if not isinstance(identity,dict) or not identity:
            raise Error('Missing private database identity')
        shutil.copyfile(provenance,out/'database-provenance.json')
    result = {'schema':1,'kind':'rf3-msa-search','queries':query_map,'source':source if unique else 'none',
              'endpoint':endpoint if unique else None,'mode':'env' if unique else None,'pairing_mode':'pairgreedy' if pair else None,
              'pairing_encoding': 'server paired row order encoded as synthetic descending 19-digit TaxID keys; not NCBI taxonomy' if pair else 'native TaxID headers unchanged',
              'chain_msas':evidence, 'completed_at_epoch':time.time()}
    seal(out, 'search.json', result)
    validate_search(out, query_map)
    return out


def validate_search(root, expected=None):
    root = Path(root).absolute()
    value = verify_inventory(root, 'search.json')
    if value.get('schema') != 1 or value.get('kind') != 'rf3-msa-search':
        raise Error('Unsupported RF3 MSA search bundle')
    query_map = queries(value['queries'])
    if expected is not None and query_map != expected:
        raise Error('RF3 MSA queries differ from prediction input')
    if set(query_map) != set(value['chain_msas']):
        raise Error('RF3 search chain inventory differs')
    for chain, seq in query_map.items():
        row = value['chain_msas'][chain]
        if row['path'] != 'msas/'+chain+'.a3m':
            raise Error('RF3 search chain path differs')
        checked = binding.validate_a3m(root/row['path'], seq)
        if any(row.get(k) != v for k,v in checked.items()):
            raise Error('RF3 search alignment evidence differs')
    return value


def prepare_input(args):
    expected = input_queries(args.fasta,args.native_json)
    with tempfile.TemporaryDirectory(prefix='rf3-search-') as scratch:
        result = Path(args.search_bundle) if args.search_bundle else search(expected,Path(scratch)/'search',
                   args.server_url,args.source,args.deadline,args.database_provenance)
        receipt = validate_search(result,expected)
        mapping = {chain:str((result/row['path']).absolute()) for chain,row in receipt['chain_msas'].items()}
        save(Path(scratch)/'mapping.json',mapping)
        path = binding.prepare(out=args.out,fasta=args.fasta,native_json=args.native_json,
                               msa_map=Path(scratch)/'mapping.json',name=args.name)
        shutil.copytree(result,path.parent/'msa-search')
        manifest = binding.read_json(path.parent/'msa-manifest.json')
        manifest['search_sha256'] = receipt['sha256']
        seal(path.parent,'msa-manifest.json',manifest)
        binding.validate(path)
        return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    extract = commands.add_parser('queries')
    bind = commands.add_parser('prepare')
    for sub in (extract,bind):
        inputs = sub.add_mutually_exclusive_group(required=True)
        inputs.add_argument('--fasta');inputs.add_argument('--native-json')
    for sub in (bind, commands.add_parser('search')):
        sub.add_argument('--out',required=True)
        sub.add_argument('--server-url',default='https://api.colabfold.com')
        sub.add_argument('--source',choices=('public','private'),default='public')
        sub.add_argument('--deadline',type=float,default=time.time()+7200)
        sub.add_argument('--database-provenance')
        if sub != bind: sub.add_argument('--queries',required=True)
    bind.add_argument('--search-bundle');bind.add_argument('--name',default='rf3_job')
    check = commands.add_parser('validate-search')
    check.add_argument('--input',required=True);check.add_argument('--queries')
    query_check = commands.add_parser('validate-queries')
    query_check.add_argument('--input',required=True)
    args = parser.parse_args()
    if args.command == 'queries':
        print(json.dumps(input_queries(args.fasta,args.native_json)))
    elif args.command == 'validate-queries':
        print(json.dumps(queries(binding.read_json(args.input))))
    elif args.command == 'prepare': print(prepare_input(args))
    elif args.command == 'search':
        print(search(binding.read_json(args.queries),args.out,args.server_url,args.source,args.deadline,args.database_provenance))
    else:
        print(json.dumps(validate_search(args.input,binding.read_json(args.queries) if args.queries else None)))


if __name__ == '__main__':
    try: main()
    except (Error,OSError,ValueError,KeyError,TypeError,tarfile.TarError) as exc:
        print('rf3-msa: '+str(exc),file=sys.stderr)
        sys.exit(2)
