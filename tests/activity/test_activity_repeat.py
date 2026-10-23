"""The repetitive stream (DESIGN v2.0 §6.23; evaluate/repeat.py; improver/repeat.py; activity/phases.py `repeat`) on
the fake upstream's rsi domain: the forgetful actor fails the test task that asks for a #ops post on its first
encounter; the learn arm's improver reads that run (no score), writes the hook that pushes the agent back, tries it
once on the same request (a probe), the composer adopts it, the reflector leaves a lesson in the library, and the
task's later encounters pass; the other test task needs nothing and keeps the base; the control arm runs the base
three times and keeps failing; the report pairs the encounters; a second run pays nothing; the phase refuses to run
without the sealed plan; no grading reaches R or the composer."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "tests" / "improver"))
import rsi  # noqa: E402
from rounds import Research  # noqa: E402
from scripted_r import HOOK_CODE, ScriptedR  # noqa: E402

from ouroloop.activity import phases  # noqa: E402
from ouroloop.activity.workspace import open_activity  # noqa: E402
from ouroloop.evaluate.repeat import RepeatPlan  # noqa: E402

NOTIFY, UPDATE, QUIET = "rsi.final_notify_after_update", "rsi.final_update_stage", "rsi.update_no_notify"
# the test split: the notify task (fails first: the forgetful actor), the update task (passes) and the quiet task
# (passes; its request forbids a post — the composer keeps the base against the hook's probe there)


def quiet(_line):
    pass


# the module fixture runs Harness²'s setting (feedback "none": the run alone, every task gets a session); the second
# cut's default (feedback "outcome": one bit, a passing task is left alone) has its own test below
NONE = {"encounters": 3, "feedback": "none", "concurrency": 1}   # one task at a time: the scripted R keeps state


def activity(root, upstream, *, provider, r: ScriptedR, repeat=None, **sections):
    return rsi.activity(root, upstream, provider=provider, research=Research(r), repeat=dict(repeat or NONE),
                        **sections)


def reopen(a, *, provider, r: ScriptedR, repeat=None, **sections):
    cfg = rsi.config(Path(a.config["_base"]), a.config["bench"]["upstream_path"], repeat=dict(repeat or NONE),
                     **sections)
    research = Research(r)
    return open_activity(cfg, out=quiet, worker_factory=a.worker_factory,
                         provider_factory=lambda table: research if table.get("model") == rsi.RESEARCH_MODEL
                         else provider)


@pytest.fixture(scope="module")
def stream(tmp_path_factory, fake_upstream):
    root = tmp_path_factory.mktemp("repeat")
    actor = rsi.BusinessWorker(forget_notify=True)
    r = ScriptedR("repeat")
    a = activity(root, fake_upstream, provider=actor, r=r)
    lines = []
    refs = phases.freeze(a, out=lines.append)
    b = reopen(a, provider=actor, r=r, rsi={"scope_id": "rsi-test", **{k: v for k, v in refs.items()
                                                                       if k != "gate_template"}})
    out = []
    learn = phases.repeat(b, arm="learn", out=out.append)
    paid = actor.calls
    control = phases.repeat(b, arm="control", out=out.append)
    return type("S", (), {})() if False else dict(a=b, actor=actor, r=r, refs=refs, freeze_lines=lines, lines=out,
                                                   learn=learn, control=control, paid_after_learn=paid)


def test_freeze_seals_the_repeat_plan_before_any_test_task_is_read(stream):
    a, refs = stream["a"], stream["refs"]
    assert "repeat_plan_ref" in refs and any(line.startswith("repeat     sealed") for line in stream["freeze_lines"])
    plan = RepeatPlan.from_json(a.objects.get(refs["repeat_plan_ref"]))
    assert sorted(plan.tasks) == sorted(rsi.FINAL) and plan.encounters == 3 and plan.arms == ("learn", "control")
    assert plan.split == "test" and plan.experiment_id == "rsi-test-repeat-test" and plan.feedback == "none"
    assert plan.base == a.seed_release().ref() and plan.core_ref == a.core_ref and plan.split_hash == a.splits.hash()
    (sealed,) = a.events.read("repeat_sealed")
    assert sealed["plan"] == refs["repeat_plan_ref"]


def test_the_learn_arm_adopts_a_hook_for_the_failing_task_and_its_later_encounters_pass(stream):
    a, r = stream["a"], stream["r"]
    scored = {(e["arm"], e["task"], e["encounter"]): e["passed"] for e in a.events.read("repeat_scored")}
    assert [scored[("learn", NOTIFY, k)] for k in (1, 2, 3)] == [False, True, True]
    assert [scored[("learn", UPDATE, k)] for k in (1, 2, 3)] == [True, True, True]
    (adopted,) = a.events.read("repeat_adopted")
    assert adopted["task"] == NOTIFY and adopted["choice"] == "package" and adopted["probes"] == 1
    assert adopted["entry"] == "lib1" and adopted["package"]
    kept = {e["task"]: e for e in a.events.read("repeat_kept")}
    assert kept[UPDATE]["choice"] == "base" and kept[UPDATE]["candidates"] == []          # R handed in nothing
    assert kept[QUIET]["choice"] == "base" and kept[QUIET]["candidates"] == ["package"]   # probed, not adopted
    # the later encounters ran the task's own package: the hook acted there, the first encounter had none
    runs = a.results.runs()
    rows = {(runs[x.run].candidate, x.task_id): x for x in a.results.all() if runs[x.run].role == "repeat"}
    e2 = rows[("learn:e2", NOTIFY)]
    assert runs[e2.run].package_ref == adopted["package"] and e2.success
    assert runs[rows[("learn:e1", NOTIFY)].run].package_ref == a.package(a.seed_release()).ref()
    # the probe is an extra test attempt of its own role, never an encounter
    probes = [x for x in a.results.all() if runs[x.run].role == "probe"]
    assert sorted(p.task_id for p in probes) == sorted([NOTIFY, QUIET])
    assert next(p for p in probes if p.task_id == NOTIFY).success
    assert not {p.attempt_id for p in probes} & {e["attempt"] for e in a.events.read("repeat_scored")}
    # what R saw: the request and the run, no score, no assertion, no grading
    seen = json.dumps(r.task_seen)
    assert "#ops" in r.task_seen["request"]                      # the last session's: the quiet task's or the notify's
    assert "reference_run" in r.task_seen and r.task_seen["reference_run"]["final_text"]
    assert not any(word in seen for word in ("slack_message_contains", "\"passed\"", "assertion", "grading", "partial_credit"))
    try_text = next(t for _, t in r.tries if f"on {NOTIFY}" in t)
    assert try_text.startswith(f"try_task candidates/package on {NOTIFY}") and "No score is read here" in try_text
    assert "hook:remind_ops" in try_text and "slack" in try_text
    # the composer and the reflector read digests only
    composed = next(c for c in r.composed if NOTIFY in c["reference_run"]["session"])
    assert composed["candidates"][0]["acted"] == ["hook:remind_ops"] and "passed" not in json.dumps(composed)
    (reflected,) = r.reflected
    assert reflected["components"] == ["hook:on_finish"] and "passed" not in json.dumps(reflected)
    # the library: one entry with the lesson, the changed files and an index the next session reads
    lib = a.root / "library"
    assert (lib / "lib1" / "EXPERIENCE.md").read_text(encoding="utf-8").startswith("# lib1")
    assert (lib / "lib1" / "middlewares" / "remind_ops.py").read_text(encoding="utf-8") == HOOK_CODE
    entries = json.loads((lib / "entries.json").read_text(encoding="utf-8"))["entries"]
    assert entries[0]["task"] == NOTIFY and entries[0]["experience"]["confidence"] == "medium"
    assert "lib1" in (lib / "INDEX.md").read_text(encoding="utf-8")


def test_the_control_arm_runs_the_base_every_time_and_adopts_nothing(stream):
    a = stream["a"]
    scored = {(e["arm"], e["task"], e["encounter"]): e["passed"] for e in a.events.read("repeat_scored")}
    assert [scored[("control", NOTIFY, k)] for k in (1, 2, 3)] == [False, False, False]
    assert [scored[("control", t, k)] for t in (UPDATE, QUIET) for k in (1, 2, 3)] == [True] * 6
    assert len(list(a.events.read("repeat_adopted"))) == 1                # the learn arm's only
    runs = a.results.runs()
    control = [x for x in a.results.all() if runs[x.run].role == "repeat" and runs[x.run].candidate.startswith("control:")]
    assert len(control) == 9 and {runs[x.run].package_ref for x in control} == {a.package(a.seed_release()).ref()}
    assert stream["learn"]["summary"]["learn"][2]["passed"] == 3 and stream["control"]["summary"]["control"][2]["passed"] == 2


def test_the_report_pairs_the_encounters_and_a_second_run_pays_nothing(stream):
    a, actor = stream["a"], stream["actor"]
    report = phases.report(a, out=quiet)
    rp = report["repeat"]
    assert [row["passed"] for row in rp["arms"]["learn"]] == [2, 3, 3]
    assert [row["passed"] for row in rp["arms"]["control"]] == [2, 2, 2]
    third = round(100 / 3, 6)
    assert rp["differences"]["learn:e3-e1"]["points"] == third and rp["differences"]["control:e3-e1"]["points"] == 0.0
    assert rp["differences"]["learn-control:e3"]["points"] == third and rp["differences"]["learn-control:e1"]["points"] == 0.0
    assert rp["status"] == "complete" and rp["adopted"] == 1 and rp["kept"] == 2 and rp["library"] == 1
    md = (a.root / "report.md").read_text(encoding="utf-8")
    assert "## Repetitive stream" in md and "learn-control:e3: +33.3 points" in md
    calls = actor.calls
    again = phases.repeat(a, arm="learn", out=quiet)
    assert actor.calls == calls and again["summary"]["learn"][2]["passed"] == 3


def test_with_the_one_bit_of_feedback_a_passing_task_is_left_alone_and_a_failing_one_is_told_it_was_sent_back(
        tmp_path, fake_upstream):
    """The second cut (DESIGN §6.23): feedback "outcome" (the default) — the learn arm opens no session on a task
    whose first encounter was accepted (kept, no probe, no composer), and the session on the failing task reads
    `outcome: not accepted` with no detail; the adoption and the later encounters are as before."""
    actor = rsi.BusinessWorker(forget_notify=True)
    r = ScriptedR("repeat")
    a = activity(tmp_path, fake_upstream, provider=actor, r=r, repeat={"encounters": 3, "feedback": "outcome", "concurrency": 1})
    refs = phases.freeze(a, out=quiet)
    plan = RepeatPlan.from_json(a.objects.get(refs["repeat_plan_ref"]))
    assert plan.feedback == "outcome"
    b = reopen(a, provider=actor, r=r, repeat={"encounters": 3, "feedback": "outcome", "concurrency": 1},
               rsi={"scope_id": "rsi-test", **{k: v for k, v in refs.items() if k != "gate_template"}})
    out = []
    phases.repeat(b, arm="learn", out=out.append)
    scored = {(e["task"], e["encounter"]): e["passed"] for e in b.events.read("repeat_scored")}
    assert [scored[(NOTIFY, k)] for k in (1, 2, 3)] == [False, True, True]
    kept = {e["task"]: e for e in b.events.read("repeat_kept")}
    assert set(kept) == {UPDATE, QUIET} and all(e["why"].startswith("accepted on the first encounter") for e in kept.values())
    assert all(e["probes"] == 0 and e["candidates"] == [] for e in kept.values())
    assert r.calls.count("compose") == 1 and r.calls.count("reflect") == 1          # the failing task's only
    assert r.task_seen["task_id"] == NOTIFY and r.task_seen["outcome"] == "not accepted"
    assert "NOT accepted" in r.task_seen["note"] and "passed" not in json.dumps(r.task_seen)
    assert "This request was NOT accepted" in r.system_seen and "{feedback}" not in r.system_seen
    (adopted,) = b.events.read("repeat_adopted")
    assert adopted["task"] == NOTIFY and adopted["choice"] == "package"
    runs = b.results.runs()
    assert [x.task_id for x in b.results.all() if runs[x.run].role == "probe"] == [NOTIFY]
    assert any("kept the base" in line and UPDATE in line for line in out)


def test_with_a_bit_per_attempt_the_accepted_probe_is_adopted_and_the_composer_is_not_asked(tmp_path, fake_upstream):
    """The third cut (DESIGN §6.23): feedback "attempts" (the default) — after each try_task R is told whether that
    run was accepted; the candidate whose run was accepted is adopted without a composer's guess (finance-v15: the
    label-free composer adopted a worse probe and rejected a passing one); with no accepted run the base stays."""
    actor = rsi.BusinessWorker(forget_notify=True)
    r = ScriptedR("repeat")
    a = activity(tmp_path / "yes", fake_upstream, provider=actor, r=r, repeat={"encounters": 3, "concurrency": 1})
    refs = phases.freeze(a, out=quiet)
    assert RepeatPlan.from_json(a.objects.get(refs["repeat_plan_ref"])).feedback == "attempts"
    b = reopen(a, provider=actor, r=r, repeat={"encounters": 3},
               rsi={"scope_id": "rsi-test", **{k: v for k, v in refs.items() if k != "gate_template"}})
    out = []
    phases.repeat(b, arm="learn", out=out.append)
    scored = {(e["task"], e["encounter"]): e["passed"] for e in b.events.read("repeat_scored")}
    assert [scored[(NOTIFY, k)] for k in (1, 2, 3)] == [False, True, True]
    (adopted,) = b.events.read("repeat_adopted")
    assert adopted["task"] == NOTIFY and adopted["choice"] == "package" and adopted["accepted"] == {"package": True}
    assert adopted["why"].startswith("its run on this request was accepted")
    assert "compose" not in r.calls and r.calls.count("reflect") == 1
    try_text = next(t for _, t in r.tries if f"on {NOTIFY}" in t)
    assert "THIS RUN WAS ACCEPTED" in try_text and "After each try_task you are told" in r.system_seen
    assert any("ACCEPTED" in line and "probe of package" in line for line in out)
    # an actor that ignores the hook's pushback: the probe is not accepted, the base stays
    deaf = rsi.BusinessWorker(forget_notify=True, ignore_pushback=True)
    r2 = ScriptedR("repeat")
    c = activity(tmp_path / "no", fake_upstream, provider=deaf, r=r2, repeat={"encounters": 3, "concurrency": 1})
    refs2 = phases.freeze(c, out=quiet)
    d = reopen(c, provider=deaf, r=r2, repeat={"encounters": 3},
               rsi={"scope_id": "rsi-test", **{k: v for k, v in refs2.items() if k != "gate_template"}})
    phases.repeat(d, arm="learn", out=quiet)
    kept = {e["task"]: e for e in d.events.read("repeat_kept")}
    assert kept[NOTIFY]["why"].startswith("no candidate's run was accepted") and kept[NOTIFY]["accepted"] == {"package": False}
    assert list(d.events.read("repeat_adopted")) == []
    assert "This run was NOT accepted" in next(t for _, t in r2.tries if f"on {NOTIFY}" in t)


def test_tasks_run_in_parallel_and_every_encounter_is_on_record(tmp_path, fake_upstream):
    """[repeat] concurrency: the tasks of the stream run in parallel (each task's encounters stay sequential); the
    control arm with two workers scores every task's three encounters once."""
    actor = rsi.BusinessWorker(forget_notify=True)
    r = ScriptedR("repeat")
    a = activity(tmp_path, fake_upstream, provider=actor, r=r, repeat={"encounters": 3, "concurrency": 2})
    refs = phases.freeze(a, out=quiet)
    b = reopen(a, provider=actor, r=r, repeat={"encounters": 3, "concurrency": 2},
               rsi={"scope_id": "rsi-test", **{k: v for k, v in refs.items() if k != "gate_template"}})
    got = phases.repeat(b, arm="control", out=quiet)
    rows = got["summary"]["control"]
    assert [x["tasks"] for x in rows] == [3, 3, 3] and [x["passed"] for x in rows] == [2, 2, 2]
    events = list(b.events.read("repeat_scored"))
    assert len(events) == 9 and len({(e["task"], e["encounter"]) for e in events}) == 9


def test_the_phase_refuses_without_the_sealed_plan_or_with_an_unknown_arm(tmp_path, fake_upstream):
    actor = rsi.BusinessWorker()
    a = activity(tmp_path, fake_upstream, provider=actor, r=ScriptedR("repeat"))
    with pytest.raises(ValueError, match="repeat_plan_ref"):
        phases.repeat(a, arm="learn", out=quiet)
    refs = phases.freeze(a, out=quiet)
    b = reopen(a, provider=actor, r=ScriptedR("repeat"), rsi={"scope_id": "rsi-test", **{k: v for k, v in refs.items()
                                                                                        if k != "gate_template"}})
    with pytest.raises(ValueError, match="arm"):
        phases.repeat(b, arm="other", out=quiet)
    assert actor.calls == 0
