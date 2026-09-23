"""Checks against the real TDE repository (skipped when it is not next to this one; set TDE_REPO to point at it).
They prove the training path produces what TDE accepts, without training anything."""
import ast
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from ouroloop.backends import KeywordBackend
from ouroloop.decision import Choice, Score
from ouroloop.demo import seed_ledger
from ouroloop.evolve.build import build
from ouroloop.evolve.train import DEFAULT_RECIPE, LocalTdeTrainer, TrainJob, tde_config
from ouroloop.points import retry_point
from ouroloop.runtime import Runtime

REPO = Path(os.environ.get("TDE_REPO", Path(__file__).resolve().parents[2] / "jev"))
needs_tde = pytest.mark.skipif(not (REPO / "tde" / "schema.py").exists(), reason="TDE repository not found")


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
    cfg = tde_config({**DEFAULT_RECIPE, "max_steps": 10}, "runs/x", "runs/parent", "m_0001")
    inherited = {"backbone", "readout", "marker", "pool", "branch_layers", "branch_through_backbone", "finetune"}
    assert set(cfg) | {"data_dir"} | inherited <= fields


FAKE_TRAIN = """
import argparse, json, pathlib
ap = argparse.ArgumentParser(); ap.add_argument("--config"); a = ap.parse_args()
cfg = json.loads(pathlib.Path(a.config).read_text())
out = pathlib.Path(cfg["out_dir"]); out.mkdir(parents=True, exist_ok=True)
n = sum(1 for _ in open(pathlib.Path(cfg["data_dir"]) / "train.jsonl"))
(out / "config.json").write_text(json.dumps({**cfg, "n_train": n})); (out / "best.pt").write_bytes(b"")
"""


def test_local_trainer_runs_the_repositorys_train_script(tmp_path):
    repo = tmp_path / "tde_repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "train.py").write_text(FAKE_TRAIN)
    (repo / "data").mkdir()
    (repo / "data" / "replay.jsonl").write_text("".join(json.dumps({"id": f"r{i}"}) + "\n" for i in range(500)))
    parent = repo / "runs" / "parent"
    parent.mkdir(parents=True)
    (parent / "config.json").write_text(json.dumps({"backbone": "gliclass-modern-base"}))
    rt = Runtime(tmp_path / "ws", {"mock": KeywordBackend()}, "mock")
    rt.register(retry_point())
    seed_ledger(rt, sessions=40)
    ds = build(rt, tmp_path / "data")
    recipe = {**DEFAULT_RECIPE, "replay": "data/replay.jsonl", "replay_ratio": 0.5}
    result = LocalTdeTrainer(str(repo), python=sys.executable).train(TrainJob("m_0001", ds.dir, recipe, str(parent)))
    cfg = json.loads((Path(result.run) / "config.json").read_text())
    assert (Path(result.run) / "best.pt").exists() and result.backend == {"type": "tde", "run": result.run}
    assert cfg["backbone"] == "gliclass-modern-base" and cfg["data_dir"].endswith("tde")
    assert cfg["n_train"] == len(ds.splits["train"]) + int(len(ds.splits["train"]) * 0.5)
