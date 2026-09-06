"""Real localhost HTTP checks for exact per-target audit/export behavior."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import databases
import server


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.payload = b'\x1f\x8b\x00\xffbinary-template\x00'
        received = self.received = []
        payload = self.payload
        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                received.append((self.path, body, self.headers.get('Authorization')))
                response = b'{"id":"target-ticket","status":"PENDING"}'
                self.send_response(202); self.send_header('Content-Length',str(len(response))); self.end_headers()
                self.wfile.write(response)
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type','application/octet-stream')
                self.send_header('Content-Length',str(len(payload) + (10 if self.path == '/incomplete' else 0)))
                self.end_headers(); self.wfile.write(payload)
            def log_message(self, *args):
                pass
        self.upstream = ThreadingHTTPServer(('127.0.0.1',0), Upstream)
        self.proxy = server.audit_proxy(self.root/'audit',0,self.upstream.server_port)
        self.threads = []
        for instance in (self.upstream,self.proxy):
            thread = threading.Thread(target=instance.serve_forever,kwargs={'poll_interval':.01})
            thread.start(); self.threads.append(thread)
        self.url = f'http://127.0.0.1:{self.proxy.server_port}'

    def tearDown(self):
        for instance in (self.proxy,self.upstream):
            instance.shutdown(); instance.server_close()
        for thread in self.threads:
            thread.join()
        self.tmp.cleanup()

    def records(self):
        return [(p.parent,databases.load(p)) for p in sorted((self.root/'audit').glob('*/exchange.json'))]

    def test_exact_request_and_binary_result_are_committed_before_response(self):
        payload = b'q=%3E101%0AACDE%0A&mode=env'
        request = urllib.request.Request(self.url+'/ticket/msa',data=payload,headers={'Authorization':'secret-local-token'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status,202)
            ticket = json.load(response)
        self.assertEqual(ticket['id'],'target-ticket')
        record, metadata = self.records()[0]
        self.assertTrue(metadata['complete'])
        self.assertEqual((record/'request.body').read_bytes(),payload)
        self.assertEqual(self.received,[('/ticket/msa',payload,'secret-local-token')])
        self.assertNotIn('secret-local-token',(record/'exchange.json').read_text())
        with urllib.request.urlopen(self.url+'/template/1abc_A') as response:
            self.assertEqual(response.read(),self.payload)
        record, metadata = self.records()[-1]
        self.assertTrue(metadata['complete'])
        self.assertEqual((record/'response.body').read_bytes(),self.payload)
        self.assertEqual(metadata['response_sha256'],hashlib.sha256(self.payload).hexdigest())

    def test_truncated_upstream_never_becomes_complete_audit(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.url+'/incomplete')
        self.assertEqual(error.exception.code,502)
        # Error response and final metadata can complete on adjacent thread turns.
        for _ in range(100):
            record, metadata = self.records()[0]
            if 'error' in metadata:break
            time.sleep(.01)
        self.assertFalse(metadata['complete'])
        self.assertIn('Incomplete upstream',metadata['error'])
        self.assertEqual((record/'response.body').read_bytes(),self.payload)

    def test_disconnected_client_does_not_drop_upstream_artifact(self):
        with socket.create_connection(('127.0.0.1',self.proxy.server_port)) as client:
            client.sendall(b'GET /template/1abc_A HTTP/1.0\r\nHost: localhost\r\n\r\n')
            client.shutdown(socket.SHUT_RDWR)
        for _ in range(100):
            records = self.records()
            if records and records[0][1].get('complete'):break
            time.sleep(.01)
        self.assertTrue(records[0][1]['complete'])
        self.assertEqual((records[0][0]/'response.body').read_bytes(),self.payload)

    def test_export_includes_only_observed_ticket_and_failed_job_script(self):
        urllib.request.urlopen(urllib.request.Request(self.url+'/ticket/msa',data=b'q=test')).close()
        results = self.root/'results'
        for ticket in ('target-ticket','unrelated-ticket'):
            source = results/ticket; source.mkdir(parents=True)
            (source/'msa.sh').write_text('full backend script')
            (source/'job.fasta').write_text('>101\nACDE\n')
            (source/'huge-scratch').write_text('never copy this')
        databases.write_json(self.root/'config.json',{'paths':{'results':str(results)}})
        retained = server.export_jobs(self.root/'audit',self.root/'config.json',self.root/'export')
        self.assertEqual(set(retained),{'target-ticket'})
        self.assertEqual(set(retained['target-ticket']),{'msa.sh','job.fasta'})
        self.assertFalse((self.root/'export/unrelated-ticket').exists())
        self.assertFalse((self.root/'export/target-ticket/huge-scratch').exists())


if __name__ == '__main__':
    unittest.main()
