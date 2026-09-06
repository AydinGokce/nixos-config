import unittest

from inference.control_storage import mount_rows, validate


class ControlStorageTests(unittest.TestCase):
    def setUp(self):
        self.source = 'nfs.example:/volume/inference-control'
        self.mount = '/mnt/bio-inference-control'
        self.row = {'source': self.source, 'target': self.mount, 'fstype': 'nfs4',
                    'options': 'rw,hard,vers=4.1,nosharecache,lookupcache=none,acregmin=0,acregmax=0,acdirmin=0,acdirmax=0'}

    def test_automount_wrapper_still_finds_real_nfs(self):
        tree = {'filesystems': [{'source': 'systemd-1', 'target': self.mount,
                                'fstype': 'autofs', 'children': [self.row]}]}
        rows = mount_rows(tree, self.mount)
        self.assertEqual(rows, [self.row])
        self.assertEqual(validate(rows[0], self.source, self.mount), self.row)

    def test_wrong_export_and_stale_metadata_options_are_refused(self):
        for change in ({'source': 'nfs.example:/another-volume/inference-control'},
                       {'fstype': 'ext4'}, {'options': self.row['options'].replace('acdirmax=0', 'acdirmax=60')},
                       {'options': self.row['options'].replace('nosharecache', 'sharecache')},
                       {'options': self.row['options'].replace('lookupcache=none', 'lookupcache=all')}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate(dict(self.row, **change), self.source, self.mount)


if __name__ == '__main__':
    unittest.main()
