import json
import fcntl
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import msa


class SearchTests(unittest.TestCase):
    def test_public_query_lock_excludes_worker_lock_and_releases_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp)/'public-msa.lock'
            with patch.dict(os.environ, {'BIO_PUBLIC_MSA_LOCK': str(lock)}):
                with open(lock, 'a') as worker:
                    fcntl.flock(worker, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.assertRaisesRegex(msa.Error, 'waiting for public MSA'):
                        with msa.public_query_lock(time.time() - 1):
                            self.fail('An occupied query lease was acquired')
                with self.assertRaisesRegex(RuntimeError, 'native search failed'):
                    with msa.public_query_lock(time.time() + 5):
                        raise RuntimeError('native search failed')
                with open(lock, 'a') as worker:
                    fcntl.flock(worker, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_no_protein_needs_no_api_and_does_not_claim_public_or_private_search(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(msa,'Client',side_effect=AssertionError('No API needed')):
            path=msa.search({},Path(tmp)/'empty','unused','private',10**20)
            receipt=msa.validate_search(path,{})
            self.assertEqual(receipt['source'],'none')
            self.assertIsNone(receipt['endpoint'])
            self.assertEqual(receipt['chain_msas'],{})

    def test_multi_query_demultiplex_retains_headers_and_insertions(self):
        raw=b'>101\nACD\n>hit\t12\nAkkCD\n\x00>102\nEFG\n>x\nEF-\n\x00'
        result=msa.split_a3m(raw,[101,102])
        self.assertEqual(result[101],'>101\nACD\n>hit\t12\nAkkCD\n')
        for bad in (raw+b'>101\nACD\n',raw.split(b'\x00')[0]):
            with self.assertRaises(msa.Error): msa.split_a3m(bad,[101,102])

    def test_pair_encoding_preserves_server_order_and_gap_mask(self):
        queries={'P':'ACD','Q':'EFG','R':'ACD'}
        raw=[{101:'>101\nACD\n>orig\t1\nAkCD\n',102:'>102\nEFG\n>x\nEFG\n'},
             {101:'>101\nACD\n>env\nA-D\n',102:'>102\nEFG\n>env2\nE-G\n'}]
        paired={101:'>101\nACD\n>a\nAkCD\n>b\nA-D\n',102:'>102\nEFG\n>c\nE-G\n>missing\naa---\n'}
        result=msa.merge_alignments(queries,raw,paired)
        first=str(msa.PAIR_BASE-1);second=str(msa.PAIR_BASE-2)
        self.assertGreater(first,second)
        self.assertIn('TaxID='+first,result['P']);self.assertIn('TaxID='+first,result['Q'])
        self.assertIn('TaxID='+second,result['P']);self.assertNotIn('TaxID='+second,result['Q'])
        self.assertIn('>orig\t1\nAkCD\n',result['P'])
        self.assertEqual(result['P'],result['R'])
        self.assertLess(result['P'].index(first),result['P'].index(second))
        self.assertNotIn('missing',result['Q'])
        invalid=dict(paired)
        invalid[102]=paired[102].replace('aa---','aa--')
        with self.assertRaisesRegex(msa.Error,'width'): msa.merge_alignments(queries,raw,invalid)
        paired[102]='>102\nEFG\n'
        with self.assertRaises(msa.Error): msa.merge_alignments(queries,raw,paired)

    def test_search_error_does_not_create_query_only_result(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(msa.Client,'search',side_effect=msa.Error('unavailable')):
            path=Path(tmp)/'search'
            with self.assertRaisesRegex(msa.Error,'unavailable'):
                msa.search({'A':'ACD'},path,'https://api.colabfold.com','public',10**20)
            self.assertFalse((path/'search.json').exists())

    def test_search_manifest_and_binding_tamper_rejected(self):
        raw=[{101:'>101\nACD\n>source\nAkCD\n'},{101:'>101\nACD\n>env\nA-D\n'}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(msa.Client,'search',return_value=raw):
            path=msa.search({'A':'ACD'},Path(tmp)/'search','https://api.colabfold.com','public',10**20)
            value=msa.validate_search(path,{'A':'ACD'})
            self.assertEqual(value['chain_msas']['A']['depth'],3)
            with self.assertRaises(msa.Error): msa.validate_search(path,{'A':'ACE'})
            (path/'msas/A.a3m').write_text('>q\nACD\n')
            with self.assertRaisesRegex(msa.Error,'inventory'): msa.validate_search(path)

    def test_private_origin_and_provenance_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(msa.Error):
                msa.search({'A':'ACD'},Path(tmp)/'x','http://127.0.0.1:8081','private',10**20)
            with self.assertRaises(msa.Error): msa.Client('https://api.colabfold.com','private',10**20)
            with self.assertRaises(msa.Error): msa.Client('http://elsewhere:8081','private',10**20)

    def test_private_api_never_follows_redirect_or_uses_environment_proxy(self):
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header('Location','/would-be-redirected')
                self.end_headers()
            def log_message(self,*args): pass
        server=HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with patch.dict('os.environ',{'http_proxy':'http://127.0.0.1:1','no_proxy':''}):
                client=msa.Client(f'http://127.0.0.1:{server.server_port}','private',time.time()+5)
                with self.assertRaisesRegex(msa.Error,'HTTP 302'): client.request('/source')
            self.assertEqual(requests,['/source'])
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_complete_search_is_bound_into_prepared_inventory(self):
        from argparse import Namespace
        raw=[{101:'>101\nACDE\n>hit\nACDE\n'},{101:'>101\nACDE\n>env\nA-DE\n'}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(msa.Client,'search',return_value=raw):
            root=Path(tmp);fasta=root/'input.fasta';fasta.write_text('>chain\nACDE\n')
            search=msa.search({'A':'ACDE'},root/'search','https://api.colabfold.com','public',10**20)
            args=Namespace(fasta=fasta,native_json=None,search_bundle=search,out=root/'prepared',name='test')
            path=msa.prepare_input(args)
            manifest=msa.binding.validate(path)
            self.assertEqual(manifest['chain_msas']['A']['depth'],3)
            self.assertIn('msa-search/search.json',manifest['files'])
            (path.parent/'msa-search/search.json').write_text('{}')
            with self.assertRaises(msa.Error): msa.binding.validate(path)

if __name__=='__main__': unittest.main()
