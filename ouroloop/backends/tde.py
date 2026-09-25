"""A TDE run directory loaded in-process. Needs the `tde` package, torch and a checkpoint: run ouroloop from the TDE
venv on the GPU host. At most `max_loaded` runs stay loaded (the least recently used is let go, and loaded again when
asked), and a training job on the same GPU lets go of all of them (release). Many requests at once (the gate,
calibration) are batched across their states."""
from __future__ import annotations

import importlib.util
import sys
from collections import OrderedDict
from pathlib import Path

from ..types import Answer, Request


class TdeBackend:
    name = "tde"
    max_loaded = 2
    _loaded: OrderedDict = OrderedDict()       # (run, device) -> Decider, least recently used first
    _many = True                               # several requests go through _decide_many

    def __init__(self, run: str, device: str | None = None, decider=None, model: str | None = None):
        """`model`: the id the answers report (a trained run's card says which); by default tde:<run dir name>."""
        if decider is None and importlib.util.find_spec("tde") is None:
            raise ImportError(f"the tde backend needs the tde package, which {sys.executable} cannot import: run "
                              f"ouroloop from the TDE venv on the GPU host (pip install -e <ouroloop> there)")
        self.run = str(Path(run).expanduser())
        self.device = device
        self._decider = decider
        self.model = model or f"tde:{Path(run).name}"

    def _load(self):
        if self._decider is not None:
            return self._decider
        key, loaded = (self.run, self.device), TdeBackend._loaded
        if key not in loaded:
            from tde.inference import Decider
            loaded[key] = Decider.from_run(self.run, device=self.device)
            while len(loaded) > self.max_loaded:
                loaded.popitem(last=False)
                _free()
        loaded.move_to_end(key)
        return loaded[key]

    @classmethod
    def release(cls) -> None:
        """Let go of every loaded run (each loads again when next asked), e.g. before a training job on this GPU."""
        if cls._loaded:
            cls._loaded.clear()
            _free()

    def decide(self, requests: list[Request]) -> list[Answer]:
        decider = self._load()
        if len(requests) > 1 and TdeBackend._many and hasattr(decider, "dtok"):     # a tde Decider: batch them all
            try:
                probs = _decide_many(decider, [(r.state, r.question.to_json()) for r in requests])
                return [Answer.of(r.question.labels(), p, self.model) for r, p in zip(requests, probs)]
            except (ImportError, AttributeError, TypeError) as e:
                TdeBackend._many = False        # this tde's internals differ: its public decide_batch, state by state
                print(f"tde backend: batching across states is off ({type(e).__name__}: {e})", file=sys.stderr)
        by_state: dict[str, list[int]] = {}
        for i, r in enumerate(requests):
            by_state.setdefault(r.state, []).append(i)
        out: list[Answer | None] = [None] * len(requests)
        for state, idx in by_state.items():
            res = decider.decide_batch(state, {f"q{j}": requests[i].question.to_json() for j, i in enumerate(idx)})
            for j, i in enumerate(idx):
                out[i] = Answer.of(requests[i].question.labels(), res[f"q{j}"]["probabilities"], self.model)
        return out


def _decide_many(decider, pairs: list[tuple[str, dict]], size: int = 64) -> list[dict]:
    """tde.inference.Decider.decide_batch, step by step, for requests with different states: the gate and calibration
    score thousands of states, one forward pass each through decide_batch. collate pads a batch of states as it does in
    training. -> the probabilities of each request."""
    import math

    import torch
    from tde.inference import _question_to_example
    from tde.model.encoding import collate
    examples = [_question_to_example(state, q, f"q{i}") for i, (state, q) in enumerate(pairs)]
    mode, device, out = decider.model.mode, decider.device, []
    with torch.no_grad():
        for i in range(0, len(examples), size):
            chunk = examples[i:i + size]
            batch = {k: (v.to(device) if isinstance(v, torch.Tensor) else v)
                     for k, v in collate([decider.dtok.encode(e, mode) for e in chunk], decider.dtok.pad_id,
                                         mode).items()}
            with torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=device.type == "cuda"):
                logits = decider.model(batch)["logits"].float().cpu().tolist()
            for row, e in zip(logits, chunk):
                t = decider.temperature.temperature(e.primitive, e.k) if decider.temperature is not None else 1.0
                z = [x / t for x in row[:e.k]]
                top = max(z)
                p = [math.exp(x - top) for x in z]
                out.append({c.name: x / sum(p) for c, x in zip(e.candidates, p)})
    return out


def _free() -> None:
    import gc
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()
