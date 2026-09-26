"""Checks against the real TDE repository (skipped when it is not next to this one; set TDE_REPO to point at it).
They prove the training path produces what TDE accepts, without training anything. And the in-process TDE backend on
the GPU host (ouroloop installed into the TDE venv), with TDE stood in for. The training job itself: test_jobs.py."""
import ast
import contextlib
import importlib.machinery
import importlib.util
import json
import os
import sys
import types
from collections import OrderedDict
from pathlib import Path

import pytest

from ouroloop import config
from ouroloop.backends import KeywordBackend, TdeBackend
from ouroloop.decision import Choice, Score
from ouroloop.demo import seed_ledger
from ouroloop.evolve.build import build
from ouroloop.evolve.train import DEFAULT_RECIPE, RECIPE_KEYS, prepare_job, tde_config
from ouroloop.points import retry_point
from ouroloop.runtime import Runtime
from ouroloop.types import Question, Request

REPO = Path(os.environ.get("TDE_REPO", Path(__file__).resolve().parents[2] / "jev"))
needs_tde = pytest.mark.skipif(not (REPO / "tde" / "schema.py").exists(), reason="TDE repository not found")
has_tde = importlib.util.find_spec("tde") is not None
SNAKE = Path(__file__).resolve().parents[1] / "examples" / "snake"


def snake_recipe() -> dict:
    return config.load(SNAKE / "demo.toml")["recipe"]


def all_primitives(tmp_path) -> Runtime:
    rt = Runtime(tmp_path, {"mock": KeywordBackend()}, "mock", seed=0)
    rt.register(retry_point())
    route = rt.register(Choice("route", "Which model tier is enough?", {"small": "a quick edit", "large": "a redesign"}))
    effort = rt.register(Score("effort", "How much work is left?", ["none", "some", "a lot"]))
    seed_ledger(rt, sessions=30)
    for s in range(30):
        rt.outcome(rt.ask(route, f"task {s}", session=f"s{s:03d}").id, "small" if s % 3 else "large")
        rt.outcome(rt.ask(effort, f"task {s}", session=f"s{s:03d}").id, str(s % 3))
    return rt


@needs_tde
def test_built_examples_pass_tdes_own_validation(tmp_path):
    spec = importlib.util.spec_from_file_location("tde_schema", REPO / "tde" / "schema.py")
    schema = importlib.util.module_from_spec(spec)
    sys.modules["tde_schema"] = schema          # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(schema)
    ds = build(all_primitives(tmp_path), tmp_path / "data")
    seen = set()
    for part in ds.splits.values():
        for e in part:
            schema.DecisionExample.from_dict(e).validate()
            seen.add(e["primitive"])
    assert seen == {"noul", "choice", "score"}


@needs_tde
def test_generated_config_only_uses_real_trainconfig_fields():
    tree = ast.parse((REPO / "tde" / "train.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TrainConfig")
    fields = {s.target.id for s in cls.body if isinstance(s, ast.AnnAssign)}
    inherited = {"backbone", "readout", "marker", "pool", "branch_layers", "branch_through_backbone", "finetune"}
    for recipe in ({**DEFAULT_RECIPE, "max_steps": 10}, {**DEFAULT_RECIPE, **snake_recipe()}):
        cfg = tde_config(recipe, "runs/x", "runs/parent", "m_0001")
        assert set(cfg) | {"data_dir"} | inherited <= fields


# ---------------------------------------------------------------- the local TDE path on the GPU host
@pytest.mark.skipif(has_tde, reason="tde is importable here")
def test_without_tde_the_local_path_fails_at_once_and_says_why(tmp_path):
    with pytest.raises(ImportError, match="TDE venv"):
        TdeBackend("runs/m_0001")
    with pytest.raises(ImportError, match="TDE venv"):
        config.build_backend({"type": "tde", "run": "runs/m_0001"})
    with pytest.raises(ImportError, match="TDE venv"):
        config.build_backend({"type": "model", "model": "m_0001_x"}, {"jobs_root": str(tmp_path)})


def test_the_tde_backend_keeps_two_runs_loaded_and_lets_go_of_them_for_training(monkeypatch):
    loads = []

    class Decider:
        @classmethod
        def from_run(cls, run, device=None):
            loads.append(run)
            return cls()

        def decide_batch(self, state, questions):
            return {k: {"probabilities": {"yes": 0.7, "no": 0.3}} for k in questions}

    tde, inference = types.ModuleType("tde"), types.ModuleType("tde.inference")
    tde.__spec__, inference.Decider = importlib.machinery.ModuleSpec("tde", None), Decider
    monkeypatch.setitem(sys.modules, "tde", tde)
    monkeypatch.setitem(sys.modules, "tde.inference", inference)
    monkeypatch.setattr(TdeBackend, "_loaded", OrderedDict())
    a, b, c = (TdeBackend(f"~/runs/{x}", model=f"tde:m_000{x}") for x in "abc")
    q = Request("board", Question("noul", "ok?"))
    for backend in (a, b, a, c, a, b):
        assert backend.decide([q])[0].model == backend.model
    assert [Path(r).name for r in loads] == ["a", "b", "c", "b"]     # c let b go, a (just used) stayed
    assert all(r == os.path.expanduser(f"~/runs/{Path(r).name}") for r in loads)
    TdeBackend.release()                                             # a training job takes the GPU
    a.decide([q])
    assert Path(loads[-1]).name == "a" and len(TdeBackend._loaded) == 1


class Tensor:
    def __init__(self, rows):
        self.rows = rows

    def to(self, device):
        return self

    def float(self):
        return self

    def cpu(self):
        return self

    def tolist(self):
        return self.rows


def fake_tde(monkeypatch, forward):
    """Stand-ins for torch and the tde modules _decide_many uses; `forward(examples)` gives the logits."""
    torch = types.SimpleNamespace(no_grad=contextlib.nullcontext, autocast=lambda **kw: contextlib.nullcontext(),
                                  bfloat16="bf16", Tensor=Tensor)

    def to_example(state, q, qid):
        names = ["yes", "no"] if q["type"] == "noul" else list(q["criteria"])
        return types.SimpleNamespace(state=state, k=len(names), primitive=q["type"],
                                     candidates=[types.SimpleNamespace(name=n) for n in names])

    modules = {"torch": torch, "tde": types.ModuleType("tde"),
               "tde.inference": types.SimpleNamespace(_question_to_example=to_example),
               "tde.model.encoding": types.SimpleNamespace(collate=lambda encoded, pad, mode: {"x": Tensor(encoded)})}
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    class Model:
        mode, calls = "joint", []

        def __call__(self, batch):
            self.calls.append(len(batch["x"].rows))
            return {"logits": Tensor(forward(batch["x"].rows))}
    return Model()


def test_many_requests_are_batched_across_states_as_decide_batch_would(monkeypatch):
    import math
    model = fake_tde(monkeypatch, lambda examples: [[math.log(len(e.state)), 0.0, -9.0] for e in examples])
    decider = types.SimpleNamespace(model=model, device=types.SimpleNamespace(type="cpu"), temperature=None,
                                    dtok=types.SimpleNamespace(encode=lambda e, mode: e, pad_id=0))
    monkeypatch.setattr(TdeBackend, "_many", True)
    backend = TdeBackend("runs/m_0001", decider=decider)
    choice = Question("choice", "which?", {"a": "", "b": "", "c": ""})
    out = backend.decide([Request("x" * (i % 3 + 1), choice) for i in range(100)])
    assert model.calls == [64, 36]                                     # two forward passes, not a hundred
    assert out[1].probs == pytest.approx({"a": 2 / 3.0001234, "b": 1 / 3.0001234, "c": math.exp(-9) / 3.0001234},
                                         rel=1e-3)
    decider.temperature = types.SimpleNamespace(temperature=lambda primitive, k: 2.0)
    assert backend.decide([Request("xx", choice)] * 2)[0].probs["a"] == pytest.approx(
        math.sqrt(2) / (math.sqrt(2) + 1 + math.exp(-4.5)))
    del decider.dtok.pad_id                                            # a tde whose internals differ
    decider.decide_batch = lambda state, qs: {k: {"probabilities": {"a": 1, "b": 1, "c": 2}} for k in qs}
    assert backend.decide([Request("x", choice)] * 2)[0].probs == {"a": 0.25, "b": 0.25, "c": 0.5}
    assert TdeBackend._many is False                                   # from then on, state by state


@needs_tde
def test_the_batched_path_mirrors_tdes_own_decide_batch():
    inference = (REPO / "tde" / "inference.py").read_text(encoding="utf-8")
    for used in ("def _question_to_example(state: str, q: dict, qid: str", "from tde.model.encoding import collate",
                 "collate([self.dtok.encode(e, mode) for e in chunk], self.dtok.pad_id, mode)",
                 'self.model(batch)["logits"].float().cpu()', "self.temperature.apply(z, e.primitive)",
                 "self.dtok, self.model, self.cfg, self.device, self.temperature"):
        assert used in inference, used
    temperature = (REPO / "tde" / "calibration" / "temperature.py").read_text(encoding="utf-8")
    assert "def temperature(self, primitive: str, k: int)" in temperature
    assert "z = logits / self.temperature(primitive, len(logits))" in temperature


def test_a_tilde_in_a_run_path_is_the_training_machines_home(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "train.jsonl").write_text(json.dumps({"id": 1}) + "\n")
    cfg = tde_config(DEFAULT_RECIPE, "~/tde/ouroloop_jobs/m_0001/run", "~/tde/runs/base", "m_0001")
    written = json.loads(Path(prepare_job(str(data), json.dumps(cfg))).read_text())
    assert written["out_dir"] == os.path.expanduser("~/tde/ouroloop_jobs/m_0001/run")      # not a directory named ~
    assert written["init_from"] == os.path.expanduser("~/tde/runs/base")
    assert (data / "tde" / "calibration.jsonl").read_text() == ""      # no val split: TDE has nothing to select on


def test_the_snake_recipe_is_small_and_fast():
    recipe = snake_recipe()
    assert set(recipe) <= RECIPE_KEYS
    cfg = tde_config({**DEFAULT_RECIPE, **recipe}, "run", None, "m_0001")
    assert cfg["epochs"] == 1.0 and 1000 <= cfg["train_limit"] <= 10000 and not cfg["gradient_checkpointing"]
    assert recipe["init_from"].endswith("tde-general-v0.1") and recipe["min_weight"] == 1.0
    for name in ("demo.toml", "offline.toml"):
        cfg = config.load(SNAKE / name)
        assert cfg["points"]["move"]["act"] and set(cfg["recipe"]) <= RECIPE_KEYS
