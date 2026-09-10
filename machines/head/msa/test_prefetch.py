import concurrent.futures
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import prefetch


class PrefetchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.paths=[self.root/'one.idx',self.root/'two.idx']
        for i,path in enumerate(self.paths):path.write_bytes(bytes([i+1])*(1024*1024+17))

    def test_parallel_reads_cover_exact_bytes_without_changing_indexes(self):
        before={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in self.paths}
        real=os.pread;calls=[];lock=threading.Lock();barrier=threading.Barrier(4)
        def read(fd,size,offset):
            with lock:
                calls.append((fd,size,offset));first=len(calls)<=4
            if first:barrier.wait(timeout=3)
            return real(fd,size,offset)
        with mock.patch.object(prefetch.os,'pread',side_effect=read):
            value=prefetch.load(self.paths,time.time()+10)
        self.assertEqual(value['readers'],4)
        self.assertEqual(value['read_bytes'],sum(path.stat().st_size for path in self.paths))
        self.assertEqual(len({fd for fd,_,_ in calls[:4]}),4)
        self.assertTrue(value['read_only'])
        self.assertEqual(before,{path:hashlib.sha256(path.read_bytes()).hexdigest() for path in self.paths})

    def test_expired_and_cancelled_loads_issue_no_reads(self):
        with mock.patch.object(prefetch.os,'pread') as reads:
            with self.assertRaisesRegex(ValueError,'timed out'):prefetch.load(self.paths,time.time()-1)
            with self.assertRaisesRegex(ValueError,'interrupted'):prefetch.load(self.paths,time.time()+1,lambda:True)
        reads.assert_not_called()

    def test_reader_failure_cancels_peers_and_restores_tuning(self):
        entered=[];exited=[]
        class Tuning:
            claim={'devices':[]};skipped=[]
            def __enter__(self):entered.append(True);return self
            def __exit__(self,*_):exited.append(True)
        with mock.patch.object(prefetch,'ReadAhead',return_value=Tuning()), \
             mock.patch.object(prefetch.os,'pread',side_effect=OSError('fixture read failure')):
            with self.assertRaisesRegex(OSError,'fixture read failure'):prefetch.load(self.paths,time.time()+5)
        self.assertEqual(entered,exited)

    def tuning(self):
        device=self.paths[0].stat().st_dev;key=f'{os.major(device)}:{os.minor(device)}'
        sysfs=self.root/'sys';(sysfs/key).mkdir(parents=True)
        knob=sysfs/key/'read_ahead_kb';knob.write_text('128\n')
        locks=self.root/'locks';locks.mkdir()
        return knob,prefetch.ReadAhead(self.paths,self.root/'claim',sysfs,locks)

    def test_readahead_restores_after_cancellation_and_repeat_cleanup(self):
        knob,tuning=self.tuning()
        with self.assertRaisesRegex(ValueError,'cancelled'):
            with tuning:
                self.assertEqual(int(knob.read_text()),15360)
                raise ValueError('cancelled')
        self.assertEqual(int(knob.read_text()),128)
        self.assertTrue(prefetch.restore(self.root/'claim')['restored'])
        self.assertEqual(int(knob.read_text()),128)

    def test_restoration_rejects_unrelated_tuning_change(self):
        knob,tuning=self.tuning()
        with self.assertRaisesRegex(ValueError,'independently'):
            with tuning:knob.write_text('999\n')
        self.assertEqual(int(knob.read_text()),999)

    def test_final_full_residency_still_required_after_parallel_load(self):
        import session
        cache=session.IndexCache(self.paths)
        before={'indexes':[],'total_bytes':sum(p.stat().st_size for p in self.paths),'fully_resident':False}
        try:
            with mock.patch.object(cache,'residency',return_value=before),mock.patch.object(prefetch,'load') as reads:
                with self.assertRaisesRegex(ValueError,'evicted'):cache.warm('prefetch',time.time()+10,0)
            reads.assert_called_once()
        finally:cache.close()


if __name__=='__main__':unittest.main()
