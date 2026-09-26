import json
import random
from pathlib import Path

import pytest

from conftest import accept
from ouroloop import config
from ouroloop.backends import KeywordBackend
from ouroloop.chat import ScriptedChat  # noqa: F401 - keeps the chat module importable in isolation
from ouroloop.decision import Choice, Noul
from ouroloop.demo import demo_llm, run, seed_ledger
from ouroloop.evolve import experience
from ouroloop.evolve.build import build
from ouroloop.evolve.calibrate import calibrate_point, fit_temperature
from ouroloop.evolve.checks import check
from ouroloop.evolve.gate import records
from ouroloop.evolve.pipeline import evolve_model, recipe_evaluator
from ouroloop.evolve.registry import Registry
from ouroloop.evolve.replay import labelled, split, splits
from ouroloop.evolve.train import SignatureTrainer, prepare_job
from ouroloop.points import retry_point
from ouroloop.providers import MockProvider
from ouroloop.release import Revoked
from ouroloop.research import BasicResearcher, Candidate, run_round
from ouroloop.research.loop import run_rounds
from ouroloop.runtime import Runtime
from ouroloop.types import temper

LOOSE = {"min_train": 100, "max_mde": 0.2}


def seeded(tmp_path, sessions=120):
    rt = Runtime(tmp_path, {"mock": KeywordBackend()}, "mock", seed=0)
    rt.register(retry_point())
    seed_ledger(rt, sessions)
    return rt


# ---------------------------------------------------------------- build and checks
def test_build_writes_tde_examples_with_session_splits(tmp_path):
    rt = seeded(tmp_path)
    ds = build(rt, tmp_path / "data")
    sessions = {name: {e["source_id"] for e in part} for name, part in ds.splits.items()}
    assert not (sessions["train"] & sessions["test"]) and not (sessions["calibration"] & sessions["test"])
    e = ds.splits["train"][0]
    assert [c["name"] for c in e["candidates"]] == ["yes", "no"] and abs(sum(e["target"]) - 1) < 1e-9
    assert e["dataset"] == "retry" and e["primitive"] == "noul" and e["template_id"] == rt.points["retry"].spec_hash
    _, test = split(labelled(rt, "retry"))
    assert {r.decision["id"] for r, _, _ in test} == {x["id"] for x in ds.splits["test"]}
    lines = (tmp_path / "data" / "train.jsonl").read_text().splitlines()
    assert len(lines) == len(ds.splits["train"]) and json.loads(lines[0])["id"] == e["id"]


def test_checks_block_defects_and_wait_for_data(tmp_path):
    rt = seeded(tmp_path)
    ds = build(rt, tmp_path / "data")
    assert check(ds, **LOOSE).ok
    assert check(ds).insufficient                                     # 1,000 training examples by default
    for e in ds.splits["train"][:100]:
        e["meta"]["truncated"] = True
    assert any("truncated" in b for b in check(ds, **LOOSE).blocking)
    for e in ds.splits["train"][:100]:
        e["meta"]["truncated"] = False
    leak = ds.splits["test"][0]["state"]
    assert any("13-gram" in b for b in check(ds, eval_states=[leak + " " + leak], **LOOSE).blocking) or \
        len(leak.split()) < 13
    assert any("test states also occur" in w for w in check(ds, **LOOSE).warnings)


def test_position_leak_is_blocked_for_choice_points(tmp_path):
    rt = Runtime(tmp_path, {"mock": KeywordBackend()}, "mock")
    point = rt.register(Choice("route", "which", {"a": "first", "b": "second", "c": "third"}))
    for s in range(60):
        d = rt.ask(point, f"case {s}", session=f"s{s}")
        rt.outcome(d.id, "a" if s % 5 else "b")                 # 80% one label, written first in the definition
    ds = build(rt, tmp_path / "data")
    for e in ds.splits["train"]:        # written in the order predict and a live ask present them, not as defined
        q = point.question(f"case {e['source_id'][1:]}").presented(random.Random(e["id"]))
        assert [c["name"] for c in e["candidates"]] == q.labels()
    assert not any("position" in b for b in check(ds, min_train=10, max_mde=1.0).blocking)
    for part in ds.splits.values():     # a real position leak: the answer always in the first place shown
        for e in part:
            e["target"] = [1.0] + [0.0] * (len(e["candidates"]) - 1)
    assert any("position" in b for b in check(ds, min_train=10, max_mde=1.0).blocking)
    examples = [e for part in ds.splits.values() for e in part]
    for i, e in enumerate(examples):    # two right answers, at every pair of places in turn: no place is favoured
        pair = [(0, 1), (0, 2), (1, 2)][i % 3]
        e["target"] = [0.5 if j in pair else 0.0 for j in range(3)]
    report = check(ds, min_train=10, max_mde=1.0)
    assert abs(report.stats["route.position_skew"]) < 0.02
    assert not any("position" in x for x in report.blocking + report.warnings)


# ---------------------------------------------------------------- calibration and registry
def test_temperature_and_threshold_fitting(tmp_path):
    rng = random.Random(0)
    true_p = [rng.uniform(0.55, 0.95) for _ in range(400)]
    targets = [{"yes": 1.0} if rng.random() < p else {"no": 1.0} for p in true_p]
    sharpened = [temper({"yes": p, "no": 1 - p}, 0.5) for p in true_p]   # overconfident by a factor 2
    assert 1.6 < fit_temperature(sharpened, targets) < 2.5
    rt = seeded(tmp_path)
    cal = calibrate_point(rt, "retry")
    assert cal is not None and cal.n == len(splits(labelled(rt, "retry"))["calibration"])
    assert cal.model == "mock-keyword" and set(cal.thresholds) == {"after_failure"}
    rt.set_calibration("retry", "mock-keyword", 2.0, {"after_failure": 0.9})
    fresh = Runtime(tmp_path, {"mock": KeywordBackend()}, "mock")
    fresh.register(retry_point())
    assert fresh.temperature("retry", "mock-keyword") == 2.0 and fresh.temperature("retry", "other") == 1.0
    assert fresh.threshold("retry", "mock-keyword", "after_failure") is None      # no gate record: it never acts


def test_registry_promote_and_rollback(tmp_path):
    reg = Registry(tmp_path)
    assert reg.next_version() == "m_0001" and reg.champion() is None
    accept(tmp_path, {"version": "m_0001", "parent": None, "gate": "accepted", "model_id": "A"})
    accept(tmp_path, {"version": "m_0002", "parent": "m_0001", "gate": "accepted", "model_id": "B"})
    reg.promote("m_0001", None)
    reg.promote("m_0002", "m_0001")
    assert reg.champion()["version"] == "m_0002" and reg.next_version() == "m_0003"
    assert reg.rollback() == "m_0001" and reg.champion()["version"] == "m_0001"
    assert Revoked(tmp_path / "harness" / "revoked.json").ids() == {"m_0002", "B"}     # the retired model
    assert reg.rollback() is None and reg.champion() is None


# ---------------------------------------------------------------- the model pipeline
def test_evolve_model_trains_gates_and_promotes(tmp_path, frozen):
    rt = seeded(tmp_path, 400)                  # enough calibration sessions for a threshold at the 5% risk
    frozen(tmp_path, cal_frac={"*": 0.5})
    card = evolve_model(rt, SignatureTrainer(), {"min_weight": 1.0})
    assert card["gate"] == "accepted" and card["version"] == "m_0001"
    assert rt.backend_name("retry") == "champion" and rt.backend_for("retry").name == "signature"
    assert Registry(tmp_path).champion()["version"] == "m_0001" and card["model_id"] == "signature:m_0001"
    cal = rt.calibration_for("retry", "signature:m_0001")        # keyed by the id the backend reports
    assert cal["version"] == "m_0001" and cal["gate"] == card["gate_record"]     # stored by the frozen gate
    assert experience.read(tmp_path)[-1]["kind"] == "model"
    # the promoted model is what a fresh runtime loads for a point on "champion"
    cfg = {"backends": {"mock": {"type": "mock"}}, "points": {"default_backend": "mock",
           "retry": {"define": "ouroloop.points:retry_point", "backend": "champion"}}}
    fresh = config.build_runtime(cfg, tmp_path)
    assert fresh.backend_for("retry").name == "signature"


def test_evolve_model_waits_when_data_is_thin(tmp_path, frozen):
    rt = seeded(tmp_path, sessions=20)
    frozen(tmp_path, checks={"min_train": 1000, "max_mde": 0.2})
    card = evolve_model(rt, SignatureTrainer())
    assert card["gate"] == "insufficient_data" and Registry(tmp_path).champion() is None
    assert rt.backend_name("retry") != "champion" and not records(tmp_path)[0].used


def test_champion_needs_a_base_before_the_first_promotion(tmp_path):
    cfg = {"backends": {"mock": {"type": "mock"}}, "points": {"default_backend": "mock",
           "retry": {"define": "ouroloop.points:retry_point", "backend": "champion"}}}
    with pytest.raises(ValueError, match="registry"):
        config.build_runtime(cfg, tmp_path)
    cfg["registry"] = {"base": "mock"}
    assert config.build_runtime(cfg, tmp_path).backend_for("retry").name == "mock"


# ---------------------------------------------------------------- training jobs
def test_prepare_job_mixes_replay_and_inherits_the_parent_architecture(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "train.jsonl").write_text("".join(json.dumps({"id": i}) + "\n" for i in range(10)))
    (data / "val.jsonl").write_text("".join(json.dumps({"id": f"v{i}"}) + "\n" for i in range(50)))
    (tmp_path / "replay.jsonl").write_text("".join(json.dumps({"id": f"r{i}"}) + "\n" for i in range(100)))
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "config.json").write_text(json.dumps({"backbone": "gliclass", "readout": "joint", "marker": "mask"}))
    path = prepare_job(str(data), json.dumps({"out_dir": "o", "init_from": str(parent), "epochs": 3}),
                       str(tmp_path / "replay.jsonl"), 0.5, 0)
    cfg = json.loads(Path(path).read_text())
    assert cfg["backbone"] == "gliclass" and cfg["data_dir"].endswith("tde") and cfg["epochs"] == 3
    ids = [json.loads(line)["id"] for line in (data / "tde" / "train.jsonl").read_text().splitlines()]
    assert len(ids) == 15 and sum(str(i).startswith("r") for i in ids) == 5
    # what TDE selects on: the val split, shuffled, as TDE evaluates only its first rows (in order, the oldest)
    val = [json.loads(line)["id"] for line in (data / "tde" / "calibration.jsonl").read_text().splitlines()]
    assert sorted(val) == sorted(f"v{i}" for i in range(50)) and val[:10] != [f"v{i}" for i in range(10)]


# ---------------------------------------------------------------- research with recipes and questions
class RecipeAgent:
    def step(self, ws):
        return [Candidate("recipe", "retry", {"min_weight": 1.0}, "train on the point's own outcomes")]


def test_demo_rounds_promote_a_spec_then_a_model(tmp_path):
    root, res = run(tmp_path, seed=0)
    assert [(e["kind"], e["gate"]) for e in res.evaluated] == [("spec", "accepted")]     # one verdict per round
    rt = Runtime(tmp_path, {"mock": KeywordBackend()}, "mock", seed=1)
    rt.register(retry_point())
    seed_ledger(rt, 40, seed=1, start=400)          # new sessions for the next round's gate
    res = run_round(rt, RecipeAgent(), None, KeywordBackend(),
                    evaluators={"recipe": recipe_evaluator(SignatureTrainer())})
    assert [(e["kind"], e["gate"], e["record"]) for e in res.evaluated] == [("recipe", "accepted", "r_0002")]
    assert Registry(tmp_path).champion()["version"] == "m_0001"


def test_llm_can_ask_the_judge_before_finalizing(tmp_path, frozen):
    rt = seeded(tmp_path)
    frozen(tmp_path)
    prompts = []

    def llm(system, prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return json.dumps({"candidates": [], "questions": [
                {"type": "choice", "instructions": "Which error family dominates?", "state": "missing modules",
                 "criteria": {"transient": "network trouble", "permanent": "missing modules and bad commands"}}]})
        return json.dumps({"candidates": [{"kind": "spec", "change": {"instructions": "Will a rerun succeed?"},
                                           "hypothesis": "after consulting"}]})

    res = run_round(rt, BasicResearcher(), MockProvider(llm), KeywordBackend(), seed=0)
    assert "Answers from the decision model:" in prompts[1] and "Which error family dominates?" in prompts[1]
    assert [e["hypothesis"] for e in res.evaluated] == ["after consulting"]
    asked = [r for r in rt.ledger.rows() if r.decision["point"] == "ask:research"]
    assert len(asked) == 1 and "none_fit" in asked[0].decision["candidates"]


def test_rounds_stop_when_nothing_is_promoted(tmp_path, frozen):
    rt = seeded(tmp_path)
    frozen(tmp_path)
    stale = MockProvider(lambda s, p: json.dumps({"candidates": [{"kind": "spec", "hypothesis": "noop",
                                                                   "change": {"view_params": {"unused": 1}}}]}))
    results = run_rounds(rt, BasicResearcher(), stale, KeywordBackend(), rounds=5, patience=2)
    assert len(results) == 2 and all(not r.promoted for r in results)
    assert demo_llm().model == "scripted-demo"


def test_focus_keeps_every_default_error_and_a_sample_of_the_rest(tmp_path):
    """A row whose label is not the routing default is a default error; focus keeps all of them and samples the
    others so that the errors are that share of the training rows."""
    from ouroloop.evolve.pipeline import focused
    from ouroloop.evolve.replay import gold, labelled
    rt = Runtime(tmp_path, {"mock": KeywordBackend()}, "mock", seed=0)
    rt.register(Noul("check", "ok?", phases={"here": "no"}))
    for i in range(10):
        for j in range(20):
            d = rt.record("check", {"i": i, "j": j}, session=f"s{i:03d}")
            rt.outcome(d.id, "yes" if j == 0 else "no", source="env")
    rows = labelled(rt, "check")
    for focus, n in ((1.0, 10), (0.5, 20), (0.1, 100), (0.01, 200)):
        kept = focused(rt, "check", rows, focus, seed=0)
        assert len(kept) == n and sum(gold(t) == "yes" for _, t, _ in kept) == 10
        assert [r[0].decision["ts"] for r in kept] == sorted(r[0].decision["ts"] for r in kept)
    assert focused(rt, "check", rows, 0.5, seed=0) == focused(rt, "check", rows, 0.5, seed=0)
