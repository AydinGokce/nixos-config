"""The experimental runtime retains the original worker CPU admission guard."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class WorkerPlatformTests(unittest.TestCase):
    def test_prefetch_rejects_unsupported_guest_before_native_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = {
                'uname': '#!/bin/sh\nprintf "%s\\n" "$TEST_ARCH"\n',
                'grep': '#!/bin/sh\nexit "$TEST_AVX_STATUS"\n',
                'python3': '#!/bin/sh\nprintf "%s\\n" "$*" > "$TEST_CALLED"\n',
            }
            for name, source in scripts.items():
                path = root/name; path.write_text(source); path.chmod(0o755)
            marker = root/'called'
            for arch, avx, expected in (('Linux aarch64', '0', 'Linux x86_64 required'),
                                        ('Linux x86_64', '1', 'requires AVX2'),
                                        ('Linux x86_64', '0', None)):
                marker.unlink(missing_ok=True)
                env = dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
                    TEST_ARCH=arch, TEST_AVX_STATUS=avx, TEST_CALLED=str(marker),
                    BIO_MSA_SEARCH_PROFILE='mapped-prefetch-128gb-v1', MSA_TOOLS_ROOT=str(root/'runtime'))
                result = subprocess.run(['bash', '-c', 'source "$1"', 'platform-test',
                    str(Path(__file__).with_name('tools.sh'))], env=env, text=True, capture_output=True)
                with self.subTest(arch=arch, avx=avx):
                    if expected:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(expected, result.stderr)
                        self.assertFalse(marker.exists())
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn('native_runtime.py verify --root', marker.read_text())


if __name__ == '__main__':
    unittest.main()
