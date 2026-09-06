"""Small, dependency-lazy utilities for sequential resident native adapters."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import threading
import time


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(8 << 20), b""):
            value.update(block)
    return value.hexdigest()


def normalized(value, paths):
    value = deepcopy(value)
    for path in paths:
        node = value
        for part in path[:-1]:
            node = node.get(part, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict) and path[-1] in node:
            node[path[-1]] = "<job-path>"
    return value


def equivalent(base, candidate, relocations):
    if normalized(base, relocations) != normalized(candidate, relocations):
        raise ValueError("Job changes the resident model's pinned native settings")
    return deepcopy(candidate)


class AdapterBase:
    model = ""
    package = ""
    version = ""
    relocations = ()

    def __init__(self, config):
        self.config = deepcopy(config)
        if config.get("model") != self.model:
            raise ValueError("Adapter/model configuration mismatch")
        self.base = deepcopy(config["native_config"])
        self.loaded = False
        self._lock = threading.Lock()
        self._jobs = set()

    def check_load(self):
        if self.loaded:
            raise RuntimeError("Adapter is already loaded")
        if importlib.metadata.version(self.package) != self.version:
            raise ValueError(f"{self.model} requires {self.package}=={self.version}")
        checkpoint = self.config["checkpoint"]
        path = Path(checkpoint["path"]).resolve(strict=True)
        if not path.is_file() or sha256(path) != checkpoint["sha256"]:
            raise ValueError("Resident checkpoint content does not match its pin")
        return path

    @contextmanager
    def job(self, job, output_dir):
        if not self.loaded:
            raise RuntimeError("Adapter.load() must finish before prediction")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Resident adapters accept one job at a time")
        try:
            identity = job["id"]
            if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", identity):
                raise ValueError("Invalid native job ID")
            if identity in self._jobs:
                raise ValueError("Job ID was already attempted in this resident process")
            if job.get("model", self.model) != self.model:
                raise ValueError("Job model mismatch")
            if job.get("settings", self.config.get("settings", {})) != self.config.get("settings", {}):
                raise ValueError("Per-job scientific settings require a different resident config")
            seeds = job["seeds"]
            if (not isinstance(seeds, list) or not seeds or len(set(seeds)) != len(seeds)
                    or any(type(seed) is not int or not 0 <= seed < 2**32 for seed in seeds)):
                raise ValueError("Unique uint32 seeds are required")
            native = equivalent(self.base, job.get("native_config", self.base), self.relocations)
            entry = Path(job["native_input"]).resolve(strict=True)
            if not entry.is_file():
                raise ValueError("Native input must be a prepared file")
            out = Path(output_dir).absolute()
            if out.is_symlink() or (out.exists() and any(out.iterdir())):
                raise ValueError("Prediction output must be a fresh empty directory")
            out.mkdir(parents=True, exist_ok=True)
            out = out.resolve(strict=True)
            self._jobs.add(identity)
            yield native, entry, out, list(seeds)
        finally:
            self._lock.release()

    def result(self, out, structures, settings, started, **extra):
        paths = sorted(Path(path).resolve(strict=True) for path in structures)
        if not paths or any(not p.is_relative_to(out) or not p.is_file() or not p.stat().st_size for p in paths):
            raise RuntimeError("Native prediction did not produce complete nonempty structures")
        return dict(structures=[str(p) for p in paths], native_settings=settings,
                    timings_seconds={"predict_total": time.monotonic() - started}, **extra)


def capture_rng(torch, numpy):
    import random
    return dict(python=random.getstate(), numpy=numpy.random.get_state(),
                torch=torch.get_rng_state().clone(),
                cuda=[state.clone() for state in torch.cuda.get_rng_state_all()]
                if torch.cuda.is_available() else [])


def restore_rng(state, torch, numpy):
    import random
    random.setstate(state["python"])
    numpy.random.set_state(state["numpy"])
    torch.set_rng_state(state["torch"].clone())
    if state["cuda"]:
        torch.cuda.set_rng_state_all([value.clone() for value in state["cuda"]])


def resident_strategy(device):
    """Keep prediction weights on the selected device across Trainer teardown.

    Mirrors Lightning 2.x's inference teardown, omitting only model.cpu().
    Training/optimizers are explicitly outside this adapter's contract.
    """
    from pytorch_lightning.strategies import SingleDeviceStrategy

    class ResidentSingleDeviceStrategy(SingleDeviceStrategy):
        def teardown(self):
            if self.optimizers:
                raise RuntimeError("Resident prediction strategy cannot own optimizers")
            self.precision_plugin.teardown()
            if self.accelerator is not None:
                self.accelerator.teardown()
            self.checkpoint_io.teardown()

    return ResidentSingleDeviceStrategy(device=device)
