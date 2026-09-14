"""Local process tests for exact, cancelled and failed unpublished range copies."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import aws_transfer as ranges
import block_cache as content


class RangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.pending = self.root/'source', self.root/'pending'
        self.source.mkdir(); self.pending.mkdir()

    def plan(self, data, streams=32):
        (self.source/'database.idx').write_bytes(data)
        ranges.initialize(self.pending, 'database.idx', len(data))
        return dict(entry=dict(path='database.idx', source_metadata=content.metadata(self.source/'database.idx')),
                    streams=streams, ranges=ranges.split(len(data), streams))

    def command(self, plan, slot, *, fail=False):
        receiver = ("import json,sys;sys.path.insert(0,"+repr(str(Path(ranges.__file__).parent))+");from pathlib import Path;import aws_transfer as r;span=json.loads(sys.argv[3]);print(json.dumps(r.receive(Path(sys.argv[1]),'database.idx',int(sys.argv[2]),span,sys.stdin.buffer,ready=lambda row:print(json.dumps(row),flush=True))),flush=True)")
        return [sys.executable, '-B', '-c', 'raise RuntimeError("failed receiver")' if fail else receiver,
                str(self.pending), str(plan['entry']['source_metadata']['size']), json.dumps(plan['ranges'][slot])]

    def test_thirty_two_processes_exact_bytes_and_metadata_with_four_handshakes(self):
        data = bytes(range(256))*8193+b'unaligned-tail'
        plan = self.plan(data)
        snapshots = []
        real_popen = subprocess.Popen
        counters = dict(active=0, peak=0, launched=0)
        lock = threading.Lock()
        class PendingHello:
            def __init__(self, stream): self.stream = stream
            def __getattr__(self, name): return getattr(self.stream, name)
            def readline(self, *args):
                try: return self.stream.readline(*args)
                finally:
                    with lock: counters['active'] -= 1
        def launch(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            with lock:
                counters['active'] += 1; counters['launched'] += 1
                counters['peak'] = max(counters['peak'], counters['active'])
            child.stdout = PendingHello(child.stdout)
            return child
        with patch.object(ranges.subprocess, 'Popen', side_effect=launch):
            receipts = ranges.transfer(self.source, plan, lambda slot: self.command(plan, slot), callback=snapshots.append)
        self.assertEqual(len(receipts), 32)
        self.assertEqual(counters['launched'], 32)
        self.assertLessEqual(counters['peak'], 4)
        self.assertEqual(counters['active'], 0)
        self.assertEqual((self.pending/'database.idx').read_bytes(), data)
        self.assertEqual(snapshots[-1]['bytes_sent'], len(data))
        self.assertEqual(snapshots[-1]['completed_range_bytes'], len(data))
        content.preserve(self.pending/'database.idx', plan['entry']['source_metadata'])
        for key in ('mode', 'uid', 'gid', 'mtime_ns', 'size'):
            self.assertEqual(content.metadata(self.pending/'database.idx')[key], plan['entry']['source_metadata'][key])
        self.assertFalse((self.root/'ready.json').exists())

    def test_nonoverlap_coverage_and_invalid_ranges(self):
        for size in (1, 7, 16, 17, 32, 33, 472_852_773_888):
            offset = 0
            for row in ranges.split(size, 32):
                self.assertEqual(row['offset'], offset)
                offset += row['length']
            self.assertEqual(offset, size)
        for streams in (0, 33, True):
            with self.assertRaisesRegex(RuntimeError, 'stream count'):
                ranges.split(100, streams)
        ranges.initialize(self.pending, 'x', 8)
        for offset, length in ((-1, 1), (0, 0), (7, 2), (True, 1)):
            with self.assertRaisesRegex(RuntimeError, 'bounds'):
                ranges.receive(self.pending, 'x', 8, dict(offset=offset,length=length), io.BytesIO(b'ab'))

    def test_short_extra_partial_write_cancel_and_complete_retry(self):
        ranges.initialize(self.pending, 'x', 8)
        span = dict(offset=0,length=8)
        for raw, message in ((b'abc', 'Short'), (b'123456789', 'Extra')):
            with self.assertRaisesRegex(RuntimeError, message):
                ranges.receive(self.pending, 'x', 8, span, io.BytesIO(raw))
        cancel = threading.Event()
        class CancelAfterFirstRead(io.BytesIO):
            def read(inner, size=-1):
                result = super().read(min(size, 3)); cancel.set(); return result
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            ranges.receive(self.pending, 'x', 8, span, CancelAfterFirstRead(b'12345678'), cancel)
        ranges.initialize(self.pending, 'x', 8)
        self.assertEqual((self.pending/'x').read_bytes(), b'\0'*8)
        real_write = os.pwrite
        with patch.object(ranges.os, 'pwrite', side_effect=lambda fd, data, at: real_write(fd, data[:2], at)):
            ranges.receive(self.pending, 'x', 8, span, io.BytesIO(b'abcdefgh'))
        self.assertEqual((self.pending/'x').read_bytes(), b'abcdefgh')
        with patch.object(ranges.os, 'pwrite', return_value=0), self.assertRaisesRegex(RuntimeError, 'Short'):
            ranges.receive(self.pending, 'x', 8, span, io.BytesIO(b'abcdefgh'))

    def test_cancel_during_process_registration_kills_owned_children(self):
        plan = self.plan(b'a'*128, streams=8)
        entered, release = threading.Event(), threading.Event()
        cancelled = threading.Event()
        real_popen, children = subprocess.Popen, []
        def delayed(*args, **kwargs):
            child = real_popen([sys.executable, '-B', '-c', 'import time;time.sleep(60)'], **kwargs)
            children.append(child); entered.set()
            self.assertTrue(release.wait(5))
            return child
        def release_after_cancel():
            self.assertTrue(entered.wait(5))
            cancelled.set()
            release.set()
        helper = threading.Thread(target=release_after_cancel)
        helper.start()
        def budget(_):
            self.assertTrue(entered.wait(5))
            raise RuntimeError('budget deadline')
        started = time.monotonic()
        with patch.object(ranges.subprocess, 'Popen', side_effect=delayed):
            with self.assertRaises(RuntimeError):
                ranges.transfer(self.source, plan, lambda slot: self.command(plan, slot), cancelled=cancelled, callback=budget)
        helper.join(5)
        self.assertFalse(helper.is_alive())
        self.assertLess(time.monotonic()-started, 6)
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_receiver_failure_cancels_other_ranges(self):
        plan = self.plan(b'a'*1024*1024, streams=16)
        with self.assertRaises((RuntimeError, ValueError)):
            ranges.transfer(self.source, plan, lambda slot: self.command(plan, slot, fail=slot==0))
        self.assertFalse((self.root/'ready.json').exists())

    def test_source_changed_since_inventory_is_rejected(self):
        plan = self.plan(b'a'*128)
        (self.source/'database.idx').write_bytes(b'b'*128)
        with self.assertRaisesRegex(RuntimeError, 'metadata differs'):
            ranges.transfer(self.source, plan, lambda slot: self.fail('must not launch'))

    def test_symlink_hardlink_and_traversal_refused(self):
        (self.root/'outside').write_bytes(b'protected')
        (self.pending/'link').symlink_to(self.root/'outside')
        os.link(self.root/'outside', self.pending/'hard')
        for name in ('link', 'hard', '../outside', '/etc/passwd', 'a/../outside'):
            with self.assertRaises(RuntimeError):
                ranges.initialize(self.pending, name, 8)
        self.assertEqual((self.root/'outside').read_bytes(), b'protected')

    def test_ssh_has_independent_authenticated_connections(self):
        argv = ranges.ssh_argv('root@192.0.2.1', '/private/key', '/private/known')
        for value in ('StrictHostKeyChecking=yes','IdentitiesOnly=yes','ControlMaster=no','ControlPath=none','ControlPersist=no'):
            self.assertIn(value, argv)


if __name__ == '__main__':
    unittest.main()
