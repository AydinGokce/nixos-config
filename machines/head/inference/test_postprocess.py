import concurrent.futures
from pathlib import Path
import sys
import tempfile
import unittest

from inference.common import atomic_json, inventory, read, sha256
from inference.postprocess import process


class PostprocessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        raw = self.root / 'raw'; raw.mkdir()
        (raw / 'model.cif').write_text('immutable raw model')
        self.receipt = {'attempt': 1, 'result': {}, 'output_dir': str(raw), 'files': inventory(raw)}
        script = self.root / 'score.py'
        script.write_text('import json,pathlib,sys,time\nroot=pathlib.Path(sys.argv[1])\ntime.sleep(.05)\n(root/"result.json").write_text(json.dumps({"status":"complete","score":1}))\n')
        self.job = {'id': 'job1', 'postprocess': {'kind': 'pinned-command',
            'source_files': {sys.executable: sha256(sys.executable), str(script): sha256(script)},
            'argv': [sys.executable, str(script), '{output}'], 'output_dir': str(self.root / 'cpu')}}

    def test_concurrent_execution_reuses_verified_completion_and_retains_raw(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: process(self.job, self.receipt), range(2)))
        self.assertEqual(values[0], values[1])
        attempts = list((self.root / 'cpu/job1/attempt-1').glob('execution-*'))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(inventory(self.root / 'raw'), self.receipt['files'])
        Path(values[0]['postprocess']['result_file']).write_text('{"status":"complete","score":100}')
        with self.assertRaises(ValueError):
            process(self.job, self.receipt)

    def test_source_change_and_result_path_escape_are_rejected(self):
        self.job['postprocess']['result_file'] = '../outside.json'
        with self.assertRaisesRegex(ValueError, 'within its own'):
            process(self.job, self.receipt)
        script = self.root / 'score.py'; script.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            process(self.job, self.receipt)
