"""Preview cache correctness, bounded concurrency, and renderer process ownership."""
from concurrent.futures import ThreadPoolExecutor
import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from workbench import library_structure_thumbnails as thumbs
from workbench.common import Error, canonical, digest


def png(width=640, height=480):
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))
    header = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    data = zlib.compress((b'\0' + b'\x40\x80\xc0\xff' * width) * height)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', data) + chunk(b'IEND', b'')


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.api = SimpleNamespace(store=SimpleNamespace(root=self.base / 'state'), actor='alice')
        self.source = self.base / 'retained.pdb'; self.source.write_bytes(b'ATOM exact source\n')
        self.executable = self.base / 'bio-render-headless'
        self.executable.write_text('#!/bin/sh\nexit 0\n'); self.executable.chmod(0o700)
        self.env = patch.dict(os.environ, {'BIO_WORKBENCH_RENDERER': str(self.executable)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.source_receipt = {'ref': 'construct:protein@1', 'entry_id': 'manual-' + 'a' * 32,
            'source_sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
            'source_size': self.source.stat().st_size, 'source_format': 'pdb', 'source_path': self.source}
        self.resolver = patch.object(thumbs, '_resolve', side_effect=self.resolve)
        self.resolver.start(); self.addCleanup(self.resolver.stop)
        self.authorized = True; self.calls = 0; self.renders = 0
        self.params = {key: self.source_receipt[key] for key in ('ref', 'entry_id')}
        self.root = thumbs._root(self.api.store.root)
        self.fingerprint = thumbs.renderer_identity()[1]
        thumbs._heartbeat(self.root, self.fingerprint)

    def resolve(self, api, reference, entry_id):
        self.calls += 1
        if not self.authorized:
            raise Error('not_found', 'Source is not accessible')
        self.assertEqual(reference, self.source_receipt['ref'])
        self.assertEqual(entry_id, self.source_receipt['entry_id'])
        return deepcopy(self.source_receipt)

    def render(self, executable, manifest, output):
        self.renders += 1
        spec = json.loads(manifest.read_bytes())
        self.assertEqual(spec['width'], 640); self.assertEqual(spec['height'], 480)
        self.assertEqual(spec['style'], 'cartoon')
        self.assertEqual(len(spec['structures']), 1)
        self.assertNotEqual(Path(spec['structures'][0]['path']), self.source)
        self.assertEqual(Path(spec['structures'][0]['path']).read_bytes(), self.source.read_bytes())
        data = png()
        return {'renderer': 'bio-workbench-studio', 'width': 640, 'height': 480,
            'sha256': hashlib.sha256(data).hexdigest(),
            'structures': [{'sha256': self.source_receipt['source_sha256']}]}, data

    def status(self):
        return thumbs.status(self.api, self.params)

    def tick(self, runner=None):
        return thumbs.tick(self.api.store.root, runner=runner or self.render)

    def ready(self):
        self.assertEqual(self.status()['state'], 'queued')
        self.assertTrue(self.tick())
        result = self.status(); self.assertEqual(result['state'], 'ready')
        return result

    def read(self, receipt, **extra):
        return thumbs.read(self.api, {**self.params, 'cache_key': receipt['cache_key'],
            'sha256': receipt['sha256'], 'offset': 0, 'length': thumbs.CHUNK, **extra})

    def test_lazy_render_and_exact_cached_identity_survive_rpc_restart(self):
        before = self.source.read_bytes()
        first = self.status(); self.assertEqual(first['state'], 'queued'); self.assertEqual(self.renders, 0)
        self.assertEqual(self.status(), first)
        self.assertTrue(self.tick()); self.assertEqual(self.renders, 1)
        result = self.status(); self.assertEqual(result['state'], 'ready')
        restarted = SimpleNamespace(store=SimpleNamespace(root=self.api.store.root), actor='alice')
        self.assertEqual(thumbs.status(restarted, self.params), result)
        chunk = self.read(result)
        self.assertEqual(base64.b64decode(chunk['data_base64']), png())
        self.assertEqual(chunk['source_sha256'], self.source_receipt['source_sha256'])
        self.assertEqual(chunk['renderer_fingerprint'], self.fingerprint)
        self.assertEqual(chunk['next_offset'], result['size']); self.assertTrue(chunk['eof'])
        self.assertFalse(self.tick()); self.assertEqual(self.renders, 1)
        self.assertEqual(before, self.source.read_bytes())
        self.assertFalse(list(self.root.glob('.render-*')))

    def test_every_cached_lookup_and_read_reauthorizes_gallery_association(self):
        ready = self.ready(); calls = self.calls
        self.status(); self.read(ready)
        self.assertEqual(self.calls, calls + 2)
        self.authorized = False
        for function in (self.status, lambda: self.read(ready)):
            with self.assertRaises(Error) as raised:
                function()
            self.assertEqual(raised.exception.code, 'not_found')
        self.assertEqual(self.renders, 1)

    def test_renderer_fingerprint_changes_key_and_invalidates_old_read(self):
        ready = self.ready()
        self.executable.write_text('#!/bin/sh\n# upgraded renderer closure\nexit 0\n')
        new_fingerprint = thumbs.renderer_identity()[1]
        self.assertNotEqual(new_fingerprint, self.fingerprint)
        thumbs._heartbeat(self.root, new_fingerprint)
        result = self.status(); self.assertEqual(result['state'], 'queued')
        self.assertNotEqual(result['cache_key'], ready['cache_key'])
        with self.assertRaises(Error) as raised:
            self.read(ready)
        self.assertEqual(raised.exception.code, 'conflict')

    def test_changed_structure_is_not_rendered_from_an_old_queue_receipt(self):
        queued = self.status()
        self.source.write_bytes(b'changed coordinates\n')
        self.assertTrue(self.tick())
        result = self.status(); self.assertEqual(result['state'], 'failed')
        self.assertEqual(self.renders, 0)
        self.assertFalse((self.root / 'entries' / queued['cache_key'] / 'image.png').exists())

    def test_renderer_must_match_source_png_and_fixed_dimensions(self):
        for corruption in ('source', 'hash', 'dimensions', 'renderer', 'png'):
            with self.subTest(corruption=corruption):
                for folder in (self.root / 'entries').iterdir():
                    import shutil
                    shutil.rmtree(folder)
                def bad(executable, manifest, output):
                    receipt, data = self.render(executable, manifest, output)
                    if corruption == 'source': receipt['structures'][0]['sha256'] = '0' * 64
                    if corruption == 'hash': receipt['sha256'] = '0' * 64
                    if corruption == 'dimensions': data = png(100, 100); receipt['sha256'] = hashlib.sha256(data).hexdigest()
                    if corruption == 'renderer': receipt['renderer'] = 'different'
                    if corruption == 'png': data = data[:-1] + bytes([data[-1] ^ 1]); receipt['sha256'] = hashlib.sha256(data).hexdigest()
                    return receipt, data
                self.status(); self.tick(bad)
                self.assertEqual(self.status()['state'], 'failed')

    def test_failed_render_backs_off_and_can_retry_without_changing_library(self):
        self.status()
        self.tick(lambda *_: (_ for _ in ()).throw(subprocess.TimeoutExpired('renderer', 60)))
        result = self.status(); self.assertEqual(result['state'], 'failed')
        self.assertGreater(result['retry_after_seconds'], 290)
        self.assertFalse(self.tick())
        with patch.object(thumbs.time, 'time', return_value=time.time() + thumbs.FAILED_SECONDS + 1):
            thumbs._heartbeat(self.root, self.fingerprint)
            self.assertEqual(self.status()['state'], 'queued')
        self.tick(); self.assertEqual(self.status()['state'], 'ready')

    def test_missing_or_stale_worker_does_not_queue_forever_but_cached_image_still_works(self):
        (self.root / 'worker.json').unlink()
        self.assertEqual(self.status()['state'], 'unavailable')
        self.assertEqual(list((self.root / 'entries').iterdir()), [])
        thumbs._heartbeat(self.root, self.fingerprint); ready = self.ready()
        (self.root / 'worker.json').unlink()
        self.assertEqual(self.status()['state'], 'ready')
        self.assertTrue(self.read(ready)['eof'])

    def test_corrupt_cached_png_is_regenerated_and_symlinks_are_not_followed(self):
        ready = self.ready(); image = self.root / 'entries' / ready['cache_key'] / 'image.png'
        image.write_bytes(b'corrupt')
        with self.assertRaises(Error): self.read(ready)
        self.assertEqual(self.status()['state'], 'queued')
        self.tick(); self.assertEqual(self.status()['state'], 'ready')
        image.unlink(); image.symlink_to(self.source)
        self.assertEqual(self.status()['state'], 'queued')
        self.tick()
        # Cache symlink publication fails closed without touching the target.
        self.assertEqual(self.source.read_bytes(), b'ATOM exact source\n')

    def test_input_bounds_and_read_offsets_are_enforced(self):
        ready = self.ready()
        for values in ({'offset': -1}, {'offset': ready['size'] + 1}, {'length': 0},
                       {'length': thumbs.CHUNK + 1}, {'offset': True}, {'sha256': 'x'}):
            with self.subTest(values=values), self.assertRaises(Error): self.read(ready, **values)
        self.assertEqual(self.read(ready, offset=ready['size'])['data_base64'], '')
        with self.assertRaises(Error): thumbs.status(self.api, {**self.params, 'path': '/tmp/user-input'})
        self.assertEqual(self.renders, 1)

    def test_serial_worker_and_concurrent_request_deduplication(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.status(), range(8)))
        self.assertEqual(len({r['cache_key'] for r in results}), 1)
        entered = threading.Event(); release = threading.Event()
        def pause(*args):
            entered.set(); self.assertTrue(release.wait(5)); return self.render(*args)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.tick, pause)
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.status()['state'], 'rendering')
            self.assertFalse(self.tick())
            release.set(); self.assertTrue(first.result())
        self.assertEqual(self.renders, 1)

    def test_queue_count_and_cache_eviction_are_bounded(self):
        with patch.object(thumbs, 'MAX_QUEUED', 2):
            for index in range(2):
                self.source_receipt['source_sha256'] = f'{index:064x}'
                self.assertEqual(self.status()['state'], 'queued')
            self.source_receipt['source_sha256'] = f'{3:064x}'
            self.assertEqual(self.status()['state'], 'busy')
        # Replace these artificial requests with valid rendered cache entries.
        import shutil
        for folder in (self.root / 'entries').iterdir(): shutil.rmtree(folder)
        self.source_receipt['source_sha256'] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        first = self.ready()
        self.source.write_bytes(b'another structure\n')
        self.source_receipt['source_sha256'] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.source_receipt['source_size'] = self.source.stat().st_size
        with patch.object(thumbs, 'MAX_ENTRIES', 1):
            second = self.ready()
        self.assertNotEqual(first['cache_key'], second['cache_key'])
        self.assertEqual(len(list((self.root / 'entries').iterdir())), 1)
        self.assertFalse((self.root / 'entries' / first['cache_key']).exists())

    def test_interrupted_render_recovers_and_discards_only_own_temporary_files(self):
        queued = self.status(); folder = self.root / 'entries' / queued['cache_key']
        record = thumbs._record(folder); record['state'] = 'rendering'; thumbs._write(folder, record)
        abandoned = self.root / '.render-abandoned'; abandoned.mkdir(); (abandoned / 'source.data').write_bytes(b'old')
        unrelated = self.root / 'keep'; unrelated.write_bytes(b'owned elsewhere')
        self.assertTrue(self.tick()); self.assertEqual(self.status()['state'], 'ready')
        self.assertFalse(abandoned.exists()); self.assertEqual(unrelated.read_bytes(), b'owned elsewhere')


class RendererProcessTests(unittest.TestCase):
    def test_timeout_kills_entire_group_and_strips_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); script = root / 'renderer'; result = root / 'result.json'
            script.write_text('#!' + sys.executable + '\n' +
                'import json,os,subprocess,sys,time\n' +
                'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\n' +
                'with open(' + repr(str(result)) + ',"w") as stream:\n' +
                ' json.dump({"pid":os.getpid(),"child":child.pid,"env":dict(os.environ)},stream)\n' +
                'time.sleep(60)\n')
            script.chmod(0o700)
            with patch.dict(os.environ, {'BIO_WORKBENCH_ACTOR': 'private', 'API_KEY': 'not-for-renderer'}), \
                    patch.object(thumbs, 'RENDER_SECONDS', 1):
                with self.assertRaises(subprocess.TimeoutExpired):
                    thumbs._invoke(str(script), root / 'manifest.json', root / 'out.png')
            data = json.loads(result.read_text())
            self.assertNotIn('BIO_WORKBENCH_ACTOR', data['env']); self.assertNotIn('API_KEY', data['env'])
            self.assertEqual(data['env']['LP_NUM_THREADS'], '2')
            self.assertEqual(data['env']['OMP_NUM_THREADS'], '2')
            for pid in (data['pid'], data['child']):
                proc = Path(f'/proc/{pid}/stat')
                if proc.exists():
                    self.assertEqual(proc.read_text().split()[2], 'Z', f'process {pid} is still running')


if __name__ == '__main__':
    unittest.main()
