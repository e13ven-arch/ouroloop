"""The decision and training stack stays off the bench path (docs/PLAN.md P1 step 2).

Under a v3 or v5 K the runner shell asks no decision point, so nothing an activity runs there needs the decision
runtime, the built-in points, the feedback builders or the training code (ouroloop/train/, P1 step 4). Each check runs in a fresh interpreter,
because this process has imported everything the other tests use.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ouroloop import bench
from ouroloop.agent.runner import release_max_effort
from ouroloop.evolve import contracts as ct
from ouroloop.ledger import _read_jsonl

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent

# The plan's three: the training subpackage (since step 4 also the typed decision contracts, the feedback builders,
# the datasets, train_artifact, the backends, stores, serving, the trainer plugins and StopGuard) and the built-in
# decision points.
REQUIRED = ("ouroloop.train", "ouroloop.points")
# What stays of the decision stack outside the subpackage until it goes with the v1 loop and the champion pointer
# (step 5): off the path as well.
DECISION_STACK = ("ouroloop.runtime", "ouroloop.evolve.gate", "ouroloop.evolve.registry", "ouroloop.evolve.replay",
                  "ouroloop.evolve.monitor", "ouroloop.evolve.plan", "ouroloop.evolve.build", "ouroloop.evolve.calibrate",
                  "ouroloop.evolve.pipeline", "ouroloop.agent.loop", "ouroloop.research.loop",
                  "ouroloop.research.agent", "ouroloop.research.basic")

# Per preset: the smoke's checks (`--phase smoke --checks-only`), the smoke's one attempt, then the baseline phase
# limited to one dev task (which also measures one train task) — open_activity, the startup checks, the budget book,
# run_arm and the runner shell, with the scripted actor of tests/test_attempt_behaviour.py — and the measurement a
# round would hand R, with its feedback.
SCRIPT = r"""
import json, sys
from pathlib import Path
upstream, tmp = sys.argv[1], Path(sys.argv[2])
sys.path.insert(0, upstream)
from _pytest.monkeypatch import MonkeyPatch
from ouroloop import bench, cli  # noqa: F401 - the CLI module is on the path of `ouroloop bench` too
from ouroloop.adapters import automationbench as ab
from ouroloop.benchmark import Results, SplitManifest
import test_attempt_behaviour as tab

quiet = lambda _line: None
mp = MonkeyPatch()
for preset in ("automationbench_harness_v3", "automationbench_harness_v5"):
    root = tmp / preset
    suite = ab.load_suite(tab.DOMAIN, upstream)
    fams, rule = ab.families(suite, tab.FAMILIES)
    SplitManifest(tab.DOMAIN, suite.versions, rule, suite.entries(fams), dict(tab.SPLITS)).freeze(
        Results(root, tab.DOMAIN).manifest_path())
    cfg = tab.v3_config(upstream, root, mp, preset=preset)
    checks = bench.run_phase(cfg, ".", phase="smoke", run_agent=False, out=quiet)
    smoke = bench.run_phase(cfg, ".", phase="smoke", tasks=[tab.DEV_TASK], limit=1, out=quiet)
    refs = bench.freeze(cfg, ".", out=quiet)
    cfg = {**cfg, "rsi": {**cfg["rsi"], **refs}}
    baseline = bench.baseline(cfg, ".", tasks=[tab.DEV_TASK], limit=1, out=quiet)
    # what a round's measurement hands R, read back from the baseline's train attempt
    work = bench.improvement(bench.open_activity(cfg, ".", out=quiet), template_ref=cfg["rsi"]["task_plan_ref"])
    measured = work.last_measurement(work.releases.active()["release_ref"])
    print(json.dumps({"preset": preset, "checks_ok": checks["ok"], "smoke_attempts": len(smoke["attempts"]),
                      "baseline_attempts": sum(a["summary"]["attempts"] + a["train"]["attempts"]
                                               for a in baseline["arms"].values()),
                      "measured": len(measured["train"]), "feedback": measured["feedback"].summary()}))
print(json.dumps(sorted(m for m in sys.modules if m == "ouroloop" or m.startswith("ouroloop."))))
"""


def loaded(module: str, modules: list[str]) -> list[str]:
    return [m for m in modules if m == module or m.startswith(module + ".")]


def test_a_v3_and_a_v5_bench_path_load_no_decision_or_training_code(fake_upstream, tmp_path):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO), str(TESTS)])}
    run = subprocess.run([sys.executable, "-c", SCRIPT, fake_upstream, str(tmp_path)], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stderr[-4000:]
    *phases, modules = [json.loads(line) for line in run.stdout.splitlines() if line.startswith(("{", "["))]
    # the path really ran: both presets, their checks, the smoke's attempt and the baseline's two attempts
    assert [p["preset"] for p in phases] == ["automationbench_harness_v3", "automationbench_harness_v5"]
    assert all(p["checks_ok"] and p["smoke_attempts"] == 1 and p["baseline_attempts"] == 2 and p["measured"] == 1
               for p in phases), phases
    assert all(p["feedback"] == {"records": 0, "valid": 0, "labels": {}, "excluded": {},
                                 "builder": "completion_snapshot@v1", "conflicted": 0, "unknown": 0} for p in phases)
    assert "ouroloop.bench" in modules and "ouroloop.agent.runner" in modules and "ouroloop.workspace" in modules
    assert [m for name in REQUIRED for m in loaded(name, modules)] == []
    assert [m for name in DECISION_STACK for m in loaded(name, modules)] == []


# ---------------------------------------------------------------------------- the thin workspace
def _bench_runtime(root: Path):
    """The v1 runtime as open_activity built it before this step, for comparison."""
    from ouroloop import config
    rt = config.build_runtime({"workspace": {"root": str(root)}, "backends": {"mock": {"type": "mock"}},
                               "points": {"default_backend": "mock"}}, ".")
    config.register_builtin_points(rt)
    return rt


def test_the_workspace_writes_the_session_row_and_release_file_the_v1_runtime_wrote(tmp_path):
    from ouroloop.workspace import BUILTIN_SPEC, Workspace
    harness = {"system": "s", "tools": {"api_fetch": "fetch"}, "agent": {"entry": "e", "roles": ["act"],
                                                                         "placement": "host"}}
    meta = {"arm": "base", "scope": "x", "task": "t1", "split": "dev", "held_out": True}
    records = {}
    for name, ws in (("runtime", _bench_runtime(tmp_path / "a")), ("workspace", Workspace(tmp_path / "b"))):
        release = ws.session("s_1", meta, harness=harness)
        ws.session("s_1", {**meta, "again": True})               # reopened: the release it was fixed at, no harness
        rows = [{k: v for k, v in e.items() if k != "ts"} for e in ws.ledger.events()]
        saved = json.loads((Path(ws.root) / "sessions" / "s_1.release.json").read_text(encoding="utf-8"))
        records[name] = (release.to_json(), rows, saved)
    assert records["workspace"] == records["runtime"]
    assert records["workspace"][0]["spec"] == BUILTIN_SPEC and records["workspace"][0]["model"] is None


def test_a_runner_shell_activity_gets_the_thin_workspace_and_an_older_k_the_v1_runtime(fake_upstream, tmp_path,
                                                                                         monkeypatch):
    import test_attempt_behaviour as tab
    from ouroloop.workspace import Workspace
    tab.run_baseline(fake_upstream, tmp_path, monkeypatch)
    cfg = tab.v3_config(fake_upstream, tmp_path / "ws", monkeypatch)
    activity = bench.open_activity(cfg, ".", out=lambda _line: None)
    assert release_max_effort(activity.objects, activity.release_ref) == "medium"
    assert isinstance(activity.rt, Workspace)
    assert activity.rt._serving is None                      # nothing asked for serving: none was built
    from ouroloop.train.config import build_serving
    serving = activity.rt.serving                            # a model policy to bind asks: the config's, built once
    assert serving.store.table == build_serving(cfg).store.table and activity.rt.serving is serving
    v1 = bench.attempt_workspace({"workspace": {"root": str(tmp_path / "v1")}}, ".", runner_shell=False)
    assert type(v1).__name__ == "Runtime" and "stop" in v1.points


# ---------------------------------------------------------------------------- feedback only where it can exist
def test_a_runner_shell_measurement_hands_r_the_empty_feedback_set_build_feedback_would_have(fake_upstream,
                                                                                              tmp_path, monkeypatch):
    import test_attempt_behaviour as tab
    from ouroloop.train import feedback as fb
    tab.run_baseline(fake_upstream, tmp_path, monkeypatch)
    cfg = tab.v3_config(fake_upstream, tmp_path / "ws", monkeypatch)
    work = bench.improvement(bench.open_activity(cfg, ".", out=lambda _line: None))
    release = work.releases.active()["release_ref"]
    assert bench.FEEDBACK_BUILDER == fb.COMPLETION_SNAPSHOT.ref
    built = fb.build_feedback(work.rt, objects=work.objects, scope_id=work.scope_id)
    calls = []
    monkeypatch.setattr(fb, "build_feedback", lambda *a, **kw: calls.append(kw) or built)
    empty = work.feedback(release)
    assert calls == [] and isinstance(empty, ct.FeedbackSet)
    assert empty.summary() == built.summary() and empty.records == built.records == []
    # a W that binds worker.stop to a model (or a core with training on) reads the ledger's decisions again
    monkeypatch.setattr(bench.Improvement, "_decides", lambda self, ref: True)
    assert work.feedback(release) is built and len(calls) == 1


def test_decides_reads_the_core_and_the_kind_of_the_bound_decision_target(fake_upstream, tmp_path, monkeypatch):
    import dataclasses
    import test_attempt_behaviour as tab
    from ouroloop import policies as P
    tab.run_baseline(fake_upstream, tmp_path, monkeypatch)
    cfg = tab.v3_config(fake_upstream, tmp_path / "ws", monkeypatch)
    work = bench.improvement(bench.open_activity(cfg, ".", out=lambda _line: None))
    objects = work.objects
    release = work.releases.active()["release_ref"]
    assert work._decides(release) is False                   # W0: worker.stop is the rule default, training off
    active = objects.load(release, ct.SystemRelease)
    model = P.descriptor(objects, "worker.stop", "model", {"instructions": "done?"}, model_ref="m@1",
                         inference={"model_id": "m"})
    with_model = objects.put(dataclasses.replace(active, worker_ref=P.replace_policies(objects, active.worker_ref,
                                                                                        [model])))
    assert work._decides(with_model) is True
    core = objects.load(active.core_ref, ct.CoreManifest)
    training = objects.put(dataclasses.replace(core, capabilities={**core.capabilities, "training": True}))
    assert work._decides(objects.put(dataclasses.replace(active, core_ref=training))) is True


# ---------------------------------------------------------------------------- a rule stop policy builds no point
def test_a_rule_stop_policy_builds_its_business_point_only_when_a_loop_asks(tmp_path):
    from ouroloop import policies as P
    from ouroloop.train.views import business_stop_point
    objects = ct.Objects(tmp_path)
    rule = P.descriptor(objects, "worker.stop", "rule", {"builtin": "default.v1"})
    decider = P._bind_stop(rule, P.Services(objects))
    assert decider._point is None and decider.kind == "rule" and decider.default == "yes"
    assert decider.propose({}) == ("yes", "default")         # proposing the default needs no point either
    assert decider._point is None
    assert decider.point.spec_hash == business_stop_point().spec_hash      # the v1 loop's StopGuard reads this
    assert P.fixed_defaults(P.Services(objects)).stop.point.spec_hash == business_stop_point().spec_hash


def test_a_model_policy_builds_the_serving_it_is_loaded_through_only_then(tmp_path):
    from ouroloop import policies as P
    built = []

    class Serving:
        def backend(self, spec):
            raise RuntimeError("no model here")

    def make():
        built.append(1)
        return Serving()
    objects = ct.Objects(tmp_path)
    rule = P.descriptor(objects, "worker.stop", "rule", {"builtin": "default.v1"})
    P._bind_stop(rule, P.Services(objects, serving=make))
    assert built == []
    model = P.descriptor(objects, "worker.stop", "model", {"instructions": "done?"}, model_ref="m@1",
                         inference={"model_id": "m"})
    with pytest.raises(P.BindError, match="could not be loaded: RuntimeError: no model here"):
        P._bind_stop(model, P.Services(objects, serving=make))
    assert built == [1]


def test_the_session_rows_of_a_bench_run_read_back_as_before(fake_upstream, tmp_path, monkeypatch):
    """Ledger.sessions() (the v2 data pipeline's reader) over the rows the thin workspace wrote."""
    import test_attempt_behaviour as tab
    from ouroloop.ledger import Ledger
    from ouroloop.workspace import BUILTIN_SPEC
    tab.run_baseline(fake_upstream, tmp_path, monkeypatch)
    root = tmp_path / "ws"
    sessions, _ = Ledger(root / "ledger").sessions()
    results = list(_read_jsonl(root / "bench" / "rsi" / "results.jsonl"))
    assert sorted(sessions) == sorted(r["evidence"] for r in results)
    for r in results:
        info = sessions[r["evidence"]]
        assert info["task"] == r["task_id"] and info["split"] == r["split"]
        assert info["held_out"] is (r["split"] != "train") and info["attempt_id"] == r["attempt_id"]
        assert info["release"]["spec"] == BUILTIN_SPEC and info["champion"] is None
        saved = json.loads((root / "sessions" / f"{r['evidence']}.release.json").read_text(encoding="utf-8"))
        assert saved == {"release": info["release"], "champion": None}
