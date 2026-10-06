"""v0.5's two train-only phases (docs/design/V0_5_PLAN.md §1.3, §1.4): the learn reading with its refit round, and the
experience library's stream with its snapshot round — on the miniature `rsi` domain, with scripted distillation.
"""
import json

import pytest
import rsi

from ouroloop import bench, policies as P
from ouroloop.agent import harness_package as hp
from ouroloop.evolve import contracts as ct
from ouroloop.evolve import experience as xp
from ouroloop.research import library as L
from ouroloop.research import methods as M
from test_v05 import k_harness, ledger_of, prepared_v5

# ---------------------------------------------------------------------------- the learn reading and the refit round
WRITE_AUDIT = '''import json
import os

METHODS = {"m_write_audit": {"family": "verification", "trigger": "after_writes", "kinds": ["write_done"],
                             "on": "pushback", "footprint": ["body_contains"]}}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _writes():
    try:
        with open(os.path.join(ROOT, ".m_journal.json")) as f:
            return json.load(f).get("writes") or []
    except (OSError, ValueError):
        return []


def after_writes(event, view):
    if event.get("tool") is None and _writes():
        return {"kind": "write_done", "args": {}, "context": {"writes": len(_writes())}}
    return None


def run(ctx, args):
    return json.dumps({"discrepancies": [{"target": w.get("target"), "problem": "re-read the request for the steps "
                                          "it asks after this write"} for w in _writes()[:3]], "unclassified": []})
'''


def audit_files(action="silent"):
    manifest = {**hp.empty_manifest(hp.AGENT_API_VERSION), "tools": [{
        "name": "m_write_audit", "description": "[method] USE WHEN the attempt wrote a record and is about to finish.",
        "input_schema": {"type": "object", "properties": {}}, "entrypoint": "tools/m_write_audit.py:run"}]}
    files, _ = M.kit_complete({}, {"manifest.json": json.dumps(manifest), "tools/m_write_audit.py": WRITE_AUDIT})
    files[M.TABLE_PATH] = M.render_table({"mode": "greedy", "cells": {"m_write_audit|write_done|*":
                                                                      {"action": action}}, "splits": {}})
    return files


def install_w(activity, files):
    """Point the activity at a W whose package is `files` (the fixture's way to a W*, as gap_w0 makes W0)."""
    from ouroloop.ledger import write_json
    objects = activity.objects
    active = ct.SystemRelease.from_json(objects.get(activity.releases.active()["release_ref"]))
    actor = next(objects.load(r, ct.PolicyDescriptor) for r in objects.load(active.worker_ref, ct.PolicySet).policies
                 if objects.load(r, ct.PolicyDescriptor).target == "worker.actor")
    package = hp.build_package(objects, files, hp.Limits.from_json(k_harness(activity)["package"]))
    worker = P.default_worker_set(objects, finish_check=rsi.W0_FINISH,
                                  actor_identity_ref=actor.implementation["model_identity_ref"], package_ref=package)
    release = activity.releases.bootstrap(worker, active.research_ref, active.core_ref, reason="W with a method",
                                          target_harness_ref=active.target_harness_ref,
                                          required_capabilities=active.required_capabilities, activate=False)
    write_json(activity.releases.active_path, {"release_ref": release, "revision": 2, "last_operation_id": None})
    return release


LEARN = {"min_cell_tasks": 2, "p_decide": 0.6, "max_cost_rise": 5.0}


def work_of(cfg):
    return bench.improvement(bench.open_activity(cfg, ".", out=lambda _l: None), template_ref=cfg["rsi"]["task_plan_ref"])


def test_the_learn_phase_runs_antithetic_twins_on_train_fits_the_table_and_gates_the_refit(fake_upstream, tmp_path,
                                                                                           monkeypatch):
    cfg, activity, w0 = prepared_v5(fake_upstream, tmp_path, monkeypatch, methods=LEARN)
    w = install_w(activity, audit_files("silent"))
    bench.baseline(cfg, ".", out=lambda _line: None)
    result = bench.learn(cfg, ".", out=lambda _line: None)
    # every train task whose recorded attempt wrote something (the trigger reads the journal's writes); not the hold
    assert result["learned"] and sorted(result["tasks"]) == sorted(["rsi.update_stage", "rsi.notify_after_update",
                                                                    "rsi.notify_after_update_variant",
                                                                    "rsi.notify_once"])
    cell = result["cells"]["m_write_audit|write_done|*"]
    assert cell["k"] == 2 and cell["m"] == 0 and cell["fixes"] == 2 and cell["action"] == "pushback"
    assert result["moved"] == ["m_write_audit|write_done|*"]
    rows = activity.results.all()
    twins = [r for r in rows if bench._arm_of(r.attempt_id) in ("learn_A", "learn_B")]
    assert twins and {r.split for r in twins} == {"train"}
    work = work_of(cfg)
    by_task: dict = {}
    for r in twins:
        for d in M.decisions_of(work.transcript_of(r)):
            assert d["mode"] == "explore" and d["propensity"] == 0.5
            by_task.setdefault(r.task_id, set()).add(d["action"])
    assert by_task and all(actions == {"pushback", "silent"} for actions in by_task.values())   # on once, off once
    fit = [e for e in xp.read(activity.root) if e.get("kind") == "method_fit"]
    assert len(fit) == 1 and fit[0]["changed"] and fit[0]["release"] == w
    refit = ledger_of(activity)["refit"]
    assert refit["declaration"]["carrier"] == "hook" and refit["screen"]["fixes"] == 2
    assert result["refit"]["outcome"] == "installed"                      # the fixture: pushback fixes the notify tasks
    if result["refit"]["outcome"] == "installed":
        installed = P.bind_release(activity.releases.active()["release_ref"], profile="task", services=work.services(),
                                   authorization="installed").package.run_files
        assert M.table_of(installed)["cells"]["m_write_audit|write_done|*"]["action"] == "pushback"
        assert M.table_of(installed)["mode"] == "greedy"
    # receipt 2: every dev attempt's decisions are greedy (propensity 1)
    for r in activity.results.all():
        if r.split == "dev":
            assert M.propensity_problems(work.transcript_of(r)) == []


def test_the_learn_phase_refuses_a_k_without_methods_and_a_dev_run_refuses_an_explore_table(fake_upstream, tmp_path,
                                                                                            monkeypatch):
    from test_research_session import prepared_v3
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path / "v4", monkeypatch)
    with pytest.raises(ValueError, match="needs a K with the method library"):
        bench.learn(cfg, ".", out=lambda _line: None)
    cfg5, activity5, _ = prepared_v5(fake_upstream, tmp_path / "v5", monkeypatch, methods=LEARN)
    install_w(activity5, M.twin_files(audit_files("pushback"), salt="s", twin="A"))
    work = work_of(cfg5)
    with pytest.raises(ct.ContractError, match="explore table"):
        work.run_arm("x", activity5.releases.active()["release_ref"], [(rsi.DEV[0], 0)], plan_ref="", split="dev")
    assert not [r for r in activity5.results.all() if r.split == "dev"]          # refused before anything ran


# ---------------------------------------------------------------------------- the experience library
FINISH = rsi.FINISH_CHECK_MARK


class Distiller:
    """A scripted distillation: one entry a call, each a little different (so none merge), carrying the fixture's
    finish-check phrase — the text that makes the scripted worker check its request before finishing."""

    def __init__(self, *, leak=None):
        self.calls, self.leak = [], leak

    def __call__(self, system, user):
        self.calls.append((system, json.loads(user) if user.startswith("{") else user))
        if system.startswith("Two entries"):
            return json.dumps({"entries": []})
        n = len(self.calls)
        body = f"After the update succeeds, do every other step the request names, then {FINISH}."
        if self.leak:
            body += f" The contact is {self.leak}."
        return json.dumps({"entries": [{"shelf": "crm-messaging", "kind": "workflow",
                                        "title": f"Finish every step of a two-step request (note {n})",
                                        "when": "the request asks to move a contact to the opportunity stage and then "
                                                "post a message in a channel",
                                        "body": body, "avoid": "posting the message twice"}]})


LIB = {"sibling_sim": 0.99, "dedupe_cos": 1.5, "min_other_tasks": 1, "min_score": 0.1}


def test_the_library_stream_reads_train_only_distils_credits_and_gates_its_snapshot(fake_upstream, tmp_path,
                                                                                   monkeypatch):
    cfg, activity, w0 = prepared_v5(fake_upstream, tmp_path, monkeypatch, library=LIB)
    bench.baseline(cfg, ".", out=lambda _line: None)
    distiller = Distiller()
    result = bench.library_track(cfg, ".", out=lambda _line: None, distiller=distiller)
    readout = result["readout"]
    assert readout["tasks"] == len(rsi.TRAIN) and readout["D"] > 0 and readout["B"] == 0
    assert distiller.calls and all("success" in user for _, user in distiller.calls if isinstance(user, dict))
    rows = activity.results.all()
    stream_rows = [r for r in rows if bench._arm_of(r.attempt_id) in ("library_first", "library_retry")]
    assert stream_rows and {r.split for r in stream_rows} == {"train"}
    state = json.loads((activity.root / "rsi" / "library_stream.json").read_text())
    assert state["status"] == "done" and state["order_hash"] and sorted(state["order"]) == sorted(rsi.TRAIN)
    # leave-one-out: no entry shown to a task names that task among its sources
    entries = state["entries"]
    texts = {t: rsi.suite(fake_upstream).tasks[t].trigger for t in rsi.TRAIN}
    for task, reading in state["readings"].items():
        for entry_id in reading["shown"]:
            assert L.task_hash(texts[task]) not in entries[entry_id]["source_hashes"]
    assert any(reading["shown"] for reading in state["readings"].values())
    # the snapshot is a fixed candidate: screened (leave-one-out), gated, recorded like any other
    snap = ledger_of(activity)["library"]
    assert snap["declaration"]["carrier"] == "skill" and snap["screen"]["net_flips"] is not None
    assert result["snapshot"]["outcome"] == "installed"                   # the fixture: the entries fix the notify tasks
    kinds = {e.get("kind") for e in xp.read(activity.root)}
    assert {"library_entry", "library_stream", "library_snapshot"} <= kinds
    if result["snapshot"]["outcome"] == "installed":
        work = work_of(cfg)
        dev = [r for r in activity.results.all() if r.split == "dev"]
        retrievals = [e for r in dev for e in work.transcript_of(r) if e.get("kind") == "library_retrieval"]
        assert retrievals and all(e["excluded_as_source"] == 0 for e in retrievals)   # dev tasks are never sources
    # the report's v0.5 section: the stream's readout and the snapshot round, from the records alone
    lines = []
    data = bench.report(cfg, ".", out=lines.append, write=False)
    assert data["v05"]["library"]["readout"]["D"] == readout["D"]
    assert data["v05"]["fixed_rounds"][0]["candidate"].endswith("/library")
    assert "## v0.5 process evidence" in lines[0] and "learning curve" in lines[0]


def test_an_activity_without_v05_records_reports_as_before(fake_upstream, tmp_path, monkeypatch):
    from test_research_session import memory_candidate, prepared_v3
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(lambda i: [memory_candidate(i)]))
    lines = []
    data = bench.report(cfg, ".", out=lines.append, write=False)
    assert "v05" not in data and "v0.5" not in lines[0]


def test_a_library_entry_with_a_single_tasks_literal_is_refused_and_the_stream_continues(fake_upstream, tmp_path,
                                                                                         monkeypatch):
    cfg, activity, w0 = prepared_v5(fake_upstream, tmp_path, monkeypatch, library=LIB)
    planted = "INV-ZETA-0077"                       # an identifier of exactly one train task's state
    real = bench.Improvement.train_state_index

    def with_planted(self):
        index, headers = real(self)
        index.setdefault(planted, set()).add("rsi.update_stage")
        return index, headers
    monkeypatch.setattr(bench.Improvement, "train_state_index", with_planted)
    bench.baseline(cfg, ".", out=lambda _line: None)
    bench.library_track(cfg, ".", out=lambda _line: None, distiller=Distiller(leak=planted))
    state = json.loads((activity.root / "rsi" / "library_stream.json").read_text())
    # the placeholder pass replaces the one task's value, and nothing of it reaches an entry or a package
    assert state["entries"]
    assert all(planted not in json.dumps({k: e[k] for k in ("title", "when", "body", "avoid")})
               for e in state["entries"].values())
    assert all("<value>" in e["body"] for e in state["entries"].values())


class Useless(Distiller):
    """Entries without the finish-check phrase: they change nothing the scripted worker does."""

    def __call__(self, system, user):
        out = json.loads(super().__call__(system, user))
        for e in out["entries"]:
            e["body"] = "After the update succeeds, report what you did."
        return json.dumps(out)


def test_a_failed_first_encounter_is_retried_with_its_failed_checks_and_a_library_without_effect_is_not_gated(
        fake_upstream, tmp_path, monkeypatch):
    cfg, activity, w0 = prepared_v5(fake_upstream, tmp_path, monkeypatch, library=LIB)
    bench.baseline(cfg, ".", out=lambda _line: None)
    result = bench.library_track(cfg, ".", out=lambda _line: None, distiller=Useless())
    state = json.loads((activity.root / "rsi" / "library_stream.json").read_text())
    failed = {t: r for t, r in state["readings"].items() if r["lib"] == 0}
    assert set(failed) == {"rsi.notify_after_update", "rsi.notify_after_update_variant"}
    assert all(r["retry"] == 0 for r in failed.values())                   # retried once, still failing
    retries = [r for r in activity.results.all() if bench._arm_of(r.attempt_id) == "library_retry"]
    assert sorted(r.task_id for r in retries) == sorted(failed) and {r.split for r in retries} == {"train"}
    note = L.retry_note(activity.results.grading(failed["rsi.notify_after_update"]["attempt"]))
    assert "slack_message_contains" in note and "stage updated" in note
    work = work_of(cfg)
    retry = next(r for r in retries if r.task_id == "rsi.notify_after_update")
    first = next(r for r in activity.results.all() if r.attempt_id == failed["rsi.notify_after_update"]["attempt"])
    assert "A previous attempt at this very task" in json.dumps(work.transcript_of(retry))     # the note reached it
    assert "A previous attempt at this very task" not in json.dumps(work.transcript_of(first))
    # D = 0 (the first encounters match W0's own train readings): the snapshot is not gated, nothing is paid for it
    assert result["readout"]["D"] == 0 and result["snapshot"]["outcome"] == "not_gated"
    assert "library" not in ledger_of(activity)
    assert [e for e in xp.read(activity.root) if e.get("kind") == "library_snapshot"][-1]["outcome"] == "not_gated"


def test_an_interrupted_stream_continues_where_it_stopped_and_refuses_another_w(fake_upstream, tmp_path, monkeypatch):
    cfg, activity, w0 = prepared_v5(fake_upstream, tmp_path, monkeypatch, library=LIB)
    bench.baseline(cfg, ".", out=lambda _line: None)

    class Dies(Distiller):
        def __call__(self, system, user):
            if len(self.calls) >= 2:
                raise KeyboardInterrupt("stopped by hand")
            return super().__call__(system, user)
    with pytest.raises(KeyboardInterrupt):
        bench.library_track(cfg, ".", out=lambda _line: None, distiller=Dies())
    path = activity.root / "rsi" / "library_stream.json"
    state = json.loads(path.read_text())
    stopped_at = state["cursor"]
    assert 0 < stopped_at < len(rsi.TRAIN)
    before = {r.attempt_id for r in activity.results.all()}
    lines = []
    bench.library_track(cfg, ".", out=lines.append, distiller=Distiller())
    assert lines[1].startswith(f"library    n={stopped_at + 1}/")                 # continues, not from the start
    again = [r for r in activity.results.all() if r.attempt_id not in before]
    assert all(bench._arm_of(r.attempt_id) != "library_first" or r.task_id not in state["readings"] for r in again)
    # a stream does not continue over another W: rewind the state and point the activity elsewhere
    state = json.loads(path.read_text())
    state.update(status="streaming", cursor=1, base_release="release:sha256:" + "0" * 64)
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="not continued over another W"):
        bench.library_track(cfg, ".", out=lambda _line: None, distiller=Distiller())


def test_the_library_phase_refuses_a_k_without_the_library(fake_upstream, tmp_path, monkeypatch):
    from test_research_session import prepared_v3
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="needs a K with the experience library"):
        bench.library_track(cfg, ".", out=lambda _line: None, distiller=Distiller())
