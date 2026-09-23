import json
import random
from pathlib import Path

import pytest

from ouroloop import config
from ouroloop.backends import KeywordBackend
from ouroloop.chat import ScriptedChat  # noqa: F401 - keeps the chat module importable in isolation
from ouroloop.decision import Choice
from ouroloop.demo import demo_llm, run, seed_ledger
from ouroloop.evolve import experience
from ouroloop.evolve.build import build
from ouroloop.evolve.calibrate import calibrate_point, fit_temperature
from ouroloop.evolve.checks import check
from ouroloop.evolve.pipeline import evolve_model
from ouroloop.evolve.registry import Registry
from ouroloop.evolve.replay import labelled, split, splits
from ouroloop.evolve.train import (RemoteTdeBackend, RemoteTdeTrainer, SignatureTrainer, TrainJob, prepare_job)
from ouroloop.points import retry_point
from ouroloop.providers import MockProvider
from ouroloop.research import BasicResearcher, run_round
from ouroloop.research.loop import run_rounds
from ouroloop.runtime import Runtime
from ouroloop.types import Question, Request, temper

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
        rt.outcome(d.id, "a")                                         # the answer is always the first option
    report = check(build(rt, tmp_path / "data"), min_train=10, max_mde=1.0)
    assert any("position" in b for b in report.blocking)


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
    rt.set_calibration("retry", 2.0, 0.9)
    fresh = Runtime(tmp_path, {"mock": KeywordBackend()}, "mock")
    point = fresh.register(retry_point())
    assert point.policy.threshold == 0.9 and fresh.temperature("retry") == 2.0


def test_registry_promote_and_rollback(tmp_path):
    reg = Registry(tmp_path)
    assert reg.next_version() == "m_0001" and reg.champion() is None
    reg.record({"version": "m_0001", "gate": "accepted"})
    reg.record({"version": "m_0002", "gate": "accepted"})
    reg.promote("m_0001")
    reg.promote("m_0002")
    assert reg.champion()["version"] == "m_0002" and reg.next_version() == "m_0003"
    assert reg.rollback() == "m_0001" and reg.champion()["version"] == "m_0001"
    assert reg.rollback() is None and reg.champion() is None


# ---------------------------------------------------------------- the model pipeline
def test_evolve_model_trains_gates_and_promotes(tmp_path):
    rt = seeded(tmp_path)
    card = evolve_model(rt, SignatureTrainer(), {"min_weight": 1.0}, check_kwargs=LOOSE)
    assert card["gate"] == "accepted" and card["version"] == "m_0001"
    assert rt.point_backends["retry"] == "champion" and rt.backends["champion"].name == "signature"
    assert Registry(tmp_path).champion()["version"] == "m_0001"
    assert rt.calibration.get("retry")["model"] == "m_0001"
    assert experience.read(tmp_path)[-1]["kind"] == "model"
    # the promoted model is what a fresh runtime loads for a point on "champion"
    cfg = {"backends": {"mock": {"type": "mock"}}, "points": {"default_backend": "mock",
           "retry": {"define": "ouroloop.points:retry_point", "backend": "champion"}}}
    fresh = config.build_runtime(cfg, tmp_path)
    assert fresh.backend_for("retry").name == "signature"


def test_evolve_model_waits_when_data_is_thin(tmp_path):
    rt = seeded(tmp_path, sessions=20)
    card = evolve_model(rt, SignatureTrainer(), check_kwargs={"min_train": 1000})
    assert card["gate"] == "insufficient_data" and Registry(tmp_path).champion() is None
    assert rt.point_backends.get("retry") != "champion"


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
    (data / "calibration.jsonl").write_text(json.dumps({"id": "c"}) + "\n")
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
    assert (data / "tde" / "calibration.jsonl").exists()


class FakeHost:
    """Stands in for ssh/rsync: remembers commands, answers the polling and prediction steps."""

    def __init__(self):
        self.commands = []

    def __call__(self, args, input=None):
        self.commands.append((args, input))
        out = ""
        if args[0] == "ssh":
            command = args[-1]
            if "cat " in command and "exit_code" in command:
                out = "0\n"
            elif command.endswith(" -") and "prepare_job" in (input or ""):
                out = "~/tde/ouroloop_jobs/m_0001/data/tde/config.yaml\n"
        elif args[0] == "rsync" and not args[-1].count(":"):   # a pull: write predictions locally
            Path(args[-1]).write_text(json.dumps({"i": 0, "probabilities": {"yes": 0.8, "no": 0.2}}) + "\n")
        return type("P", (), {"stdout": out})()


def test_remote_trainer_runs_detached_and_backend_batches(tmp_path):
    host = FakeHost()
    (tmp_path / "data").mkdir()
    trainer = RemoteTdeTrainer("user@gpu-host", "~/tde", runner=host, sleep=lambda s: None)
    result = trainer.train(TrainJob("m_0001", tmp_path / "data", {"epochs": 1}, "runs/base"))
    ssh = [a[-1] for a, _ in host.commands if a[0] == "ssh"]
    assert any("nohup bash -c" in c and "scripts/train.py --config" in c for c in ssh)
    assert not any("pkill" in c for c in ssh)
    assert result.run == "~/tde/ouroloop_jobs/m_0001/run" and result.backend["type"] == "tde_remote"
    backend = trainer.backend(result)
    ans = backend.decide([Request("state", Question("noul", "q"))])
    assert isinstance(backend, RemoteTdeBackend) and ans[0].probs == {"yes": 0.8, "no": 0.2}


# ---------------------------------------------------------------- research with recipes and questions
def test_demo_round_promotes_a_spec_and_a_model(tmp_path):
    root, res = run(tmp_path, seed=0)
    assert [(e["kind"], e["gate"]) for e in res.evaluated] == [("spec", "accepted"), ("spec", "rejected"),
                                                              ("recipe", "accepted")]
    assert Registry(tmp_path).champion()["version"] == "m_0001"


def test_llm_can_ask_the_judge_before_finalizing(tmp_path):
    rt = seeded(tmp_path)
    prompts = []

    def llm(system, prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return json.dumps({"candidates": [], "questions": [
                {"type": "choice", "instructions": "Which error family dominates?", "state": "missing modules",
                 "criteria": {"transient": "network trouble", "permanent": "missing modules and bad commands"}}]})
        return json.dumps({"candidates": [{"kind": "spec", "change": {"instructions": "Will a rerun succeed?"},
                                           "hypothesis": "after consulting"}]})

    res = run_round(rt, BasicResearcher(seed=0), MockProvider(llm), KeywordBackend(), seed=0)
    assert "Answers from the decision model:" in prompts[1] and "Which error family dominates?" in prompts[1]
    assert [e["hypothesis"] for e in res.evaluated] == ["after consulting"]
    asked = [r for r in rt.ledger.rows() if r.decision["point"] == "ask:research"]
    assert len(asked) == 1 and "none_fit" in asked[0].decision["candidates"]


def test_rounds_stop_when_nothing_is_promoted(tmp_path):
    rt = seeded(tmp_path)
    stale = MockProvider(lambda s, p: json.dumps({"candidates": [{"kind": "spec", "hypothesis": "noop",
                                                                   "change": {"view_params": {"unused": 1}}}]}))
    results = run_rounds(rt, BasicResearcher(seed=0), stale, KeywordBackend(), rounds=5, patience=2)
    assert len(results) == 2 and all(not r.promoted for r in results)
    assert demo_llm().model == "scripted-demo"
