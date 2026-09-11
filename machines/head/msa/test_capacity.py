import os
import unittest
from unittest.mock import patch

import capacity


class CapacityAllowanceTests(unittest.TestCase):
    def test_default_and_override_only_allow_bounded_whole_seconds(self):
        self.assertEqual(capacity.wait_seconds(environ={}), 7200)
        self.assertEqual(capacity.wait_seconds(environ={'BIO_MSA_CAPACITY_WAIT_SECONDS': '1800'}), 1800)
        self.assertEqual(capacity.wait_seconds(0, environ={}), 0)
        for value in (-1, 7201, True, float('nan'), 'bad'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                capacity.wait_seconds(value)

    def test_workbench_only_extends_private_msa_orchestration(self):
        with patch.dict(os.environ, {'BIO_MSA_CAPACITY_WAIT_SECONDS': '7200'}):
            self.assertEqual(capacity.preparation_allowance({'msa_backend': 'private', 'msa_applicable': True}), 7200)
            self.assertEqual(capacity.preparation_allowance({'msa_backend': 'public', 'msa_applicable': True}), 0)
            self.assertEqual(capacity.preparation_allowance({'msa_backend': 'private', 'msa_applicable': False}), 0)
            self.assertEqual(capacity.preparation_allowance({}), 0)
            self.assertEqual(capacity.preparation_allowance({'msa_backend': 'private', 'msa_applicable': True,
                'environment': {'BIO_MSA_CAPACITY_WAIT_SECONDS': '120'}}), 120)


if __name__ == '__main__':
    unittest.main()
