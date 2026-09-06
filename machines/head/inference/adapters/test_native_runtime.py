"""Run in a pinned model venv: real Torch/Lightning CPU control-state checks."""
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters._common import capture_rng, restore_rng, resident_strategy


class NativeRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import numpy as np
            import torch
            import pytorch_lightning as pl
        except ImportError as exc:
            raise unittest.SkipTest(str(exc))
        cls.np, cls.torch, cls.pl = np, torch, pl

    def test_full_rng_restore_survives_intervening_consumption(self):
        torch, np = self.torch, self.np
        random.seed(42)
        np.random.seed(42)
        torch.manual_seed(42)
        # A real parameter constructor advances CPU Torch RNG, as Boltz does.
        torch.nn.Linear(17, 23)
        state = capture_rng(torch, np)
        def sample():
            return random.random(), np.random.random(), torch.rand(7)
        expected = sample()
        for _ in range(3):
            sample()
            restore_rng(state, torch, np)
            actual = sample()
            self.assertEqual(expected[0:2], actual[0:2])
            torch.testing.assert_close(expected[2], actual[2], rtol=0, atol=0)

    def test_lightning_predict_teardown_does_not_offload_or_replace_model(self):
        torch, pl = self.torch, self.pl
        class Model(pl.LightningModule):
            def __init__(self):
                super().__init__()
                self.weight = torch.nn.Parameter(torch.tensor([2.0]))
            def predict_step(self, batch, batch_idx):
                return batch * self.weight
            def cpu(self):
                raise AssertionError("Inference teardown tried to offload the resident model")
        model = Model()
        weight_id = id(model.weight)
        for value in (3.0, 7.0):
            trainer = pl.Trainer(accelerator="cpu", devices=1,
                                 strategy=resident_strategy("cpu"), logger=False,
                                 enable_checkpointing=False, enable_progress_bar=False)
            output = trainer.predict(model, dataloaders=torch.utils.data.DataLoader([torch.tensor([value])]))
            self.assertEqual(output[0].item(), value * 2)
            self.assertEqual(id(model.weight), weight_id)


if __name__ == "__main__":
    unittest.main()
