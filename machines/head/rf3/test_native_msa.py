"""Native AtomWorks pairing proof; run in the pinned RF3 environment, no GPU."""
import tempfile
from pathlib import Path
import unittest
import importlib.util

AVAILABLE = bool(importlib.util.find_spec("numpy") and importlib.util.find_spec("atomworks"))
if AVAILABLE:
    import numpy as np
    from atomworks.ml.transforms.msa._msa_loading_utils import parse_a3m
    from atomworks.ml.transforms.msa._msa_pairing_utils import join_multiple_msas_by_tax_id


@unittest.skipUnless(AVAILABLE,"requires pinned RF3/AtomWorks runtime")
class NativePairingTests(unittest.TestCase):
    def load(self, root, name, text):
        path = root/name
        path.write_text(text)
        msa, ins, tax_ids = parse_a3m(str(path))
        return {"msa":msa, "ins":ins, "tax_ids":tax_ids,
                "msa_is_padded_mask":np.zeros(msa.shape,dtype=bool),
                "sequence_similarity":np.mean(msa == msa[0],axis=1)}

    def test_unique_pair_keys_preserve_pair_partners_insertions_and_unpaired_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = self.load(root,"a.a3m", ">query\nACDE\n>pair0 TaxID=9000000000000000002\nACddDE\n"
                          ">pair1 TaxID=9000000000000000001\nA-DE\n>unpaired\nACEE\n")
            b = self.load(root,"b.a3m", ">query\nFGHI\n>pair0 TaxID=9000000000000000002\nF-GI\n"
                          ">pair1 TaxID=9000000000000000001\nFGqHI\n>unpaired\nFGII\n")
            for dense in (False,True):
                merged = join_multiple_msas_by_tax_id([a,b],unpaired_padding=np.array(b"-",dtype=a['msa'].dtype),dense=dense)
                rows = [b"".join(row).decode() for row in merged["msa"]]
                self.assertEqual(rows[:3],["ACDEFGHI","ACDEF-GI","A-DEFGHI"])
                self.assertEqual(merged["ins"][1].tolist(),[0,0,2,0,0,0,0,0])
                self.assertEqual(merged["ins"][2].tolist(),[0,0,0,0,0,0,1,0])
                self.assertEqual(merged['all_paired'][:3].tolist(),[True,True,True])
                self.assertFalse(merged['any_paired'][3:].any())
                self.assertTrue(any("ACEE" in row for row in rows[3:]))
                self.assertTrue(any("FGII" in row for row in rows[3:]))

    def test_omitted_all_gap_chain_preserves_partial_pair_and_padding_mask(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = self.load(root,'a',">query\nAC\n>p TaxID=9001\nAE\n")
            b = self.load(root,'b',">query\nFG\n>p TaxID=9001\nFH\n")
            c = self.load(root,'c',">query\nIK\n")
            merged = join_multiple_msas_by_tax_id([a,b,c],unpaired_padding=np.array(b"-",dtype=a['msa'].dtype))
            self.assertEqual(b"".join(merged['msa'][1]).decode(),"AEFH--")
            self.assertTrue(merged['any_paired'][1])
            self.assertFalse(merged['all_paired'][1])
            self.assertEqual(merged['msa_is_padded_mask'][1].tolist(),[False,False,False,False,True,True])


if __name__ == '__main__':
    unittest.main()
