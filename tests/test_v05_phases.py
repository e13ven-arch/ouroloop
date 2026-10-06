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
    # the report reads the host's own check of the method contract over every attempt with a method
    writes = bench.report(cfg, ".", out=lambda _line: None, write=False)["v05"]["m_tool_writes"]
    assert writes["attempts"] > 0 and writes["executed"] == 0


def test_an_interrupted_refit_round_is_finished_on_the_next_learn_without_another_reading(fake_upstream, tmp_path,
                                                                                          monkeypatch):
    from ouroloop.research import fixed
    cfg, activity, w0 = prepared_v5(fake_upstream, tmp_path, monkeypatch, methods=LEARN)
    install_w(activity, audit_files("silent"))
    bench.baseline(cfg, ".", out=lambda _line: None)
    real = fixed.run_fixed_round

    def dies(*a, **kw):
        raise KeyboardInterrupt("stopped by hand")
    monkeypatch.setattr(fixed, "run_fixed_round", dies)
    with pytest.raises(KeyboardInterrupt):
        bench.learn(cfg, ".", out=lambda _line: None)
    twins = len([r for r in activity.results.all() if bench._arm_of(r.attempt_id).startswith("learn_")])
    monkeypatch.setattr(fixed, "run_fixed_round", real)
    result = bench.learn(cfg, ".", out=lambda _line: None)
    assert result["resumed"] and result["stage"] == "learn0" and result["refit"]["outcome"] == "installed"
    assert len([r for r in activity.results.all() if bench._arm_of(r.attempt_id).startswith("learn_")]) == twins
    assert len([e for e in xp.read(activity.root) if e.get("kind") == "method_fit"]) == 1


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
    # the retry is not credited (2026-10-07 review): entries are credited for first encounters only
    shown_first = sum(len(r["shown"]) for r in state["readings"].values())
    shown_retry = sum(len(e.get("entries") or ()) for r in retries for e in work.transcript_of(r)
                      if e.get("kind") == "library_retrieval")
    assert shown_retry > 0 and sum(e["stats"]["n_shown"] for e in state["entries"].values()) == shown_first
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


def test_the_stream_records_an_entrys_near_duplicate_train_tasks_among_its_sources(fake_upstream, tmp_path, monkeypatch):
    """The leave-one-out wiring (2026-10-07 review): an entry's sources are its source task and that task's train
    near-duplicates, so retrieval leaves the entry out for all of them — not its own task alone, and never nothing."""
    cfg, activity, w0 = prepared_v5(fake_upstream, tmp_path, monkeypatch, library={**LIB, "sibling_sim": 0.6})
    bench.baseline(cfg, ".", out=lambda _line: None)
    bench.library_track(cfg, ".", out=lambda _line: None, distiller=Distiller())
    state = json.loads((activity.root / "rsi" / "library_stream.json").read_text())
    texts = {t: rsi.suite(fake_upstream).tasks[t].trigger for t in rsi.TRAIN}
    seen_wide = False
    for e in state["entries"].values():
        source = e["provenance"][0]["task"]
        expected = {L.task_hash(texts[t]) for t in L.near_duplicates(source, texts, 0.6)}
        assert set(e["source_hashes"]) == expected and L.task_hash(texts[source]) in expected
        seen_wide = seen_wide or len(expected) >= 3
    assert seen_wide                         # rsi.update_stage's sources include both notify tasks (cosine >= 0.6)
    for task, reading in state["readings"].items():
        for entry_id in reading["shown"]:
            assert L.task_hash(texts[task]) not in state["entries"][entry_id]["source_hashes"]


def test_the_snapshot_is_held_back_by_d_alone_and_b_is_only_reported():
    """2026-10-07 review: the B >= 3 clause is gone; a stream with D > 0 is gated whatever B is."""
    settings = L.LibrarySettings()
    assert L.snapshot_abort({"D": 1.0, "B": 5}, settings) is None
    assert L.snapshot_abort({"D": 0.0, "B": 0}, settings) == "D 0.0 <= 0.0"
    assert L.snapshot_abort({"D": 2.0, "B": 0}, settings, empty=True) == "the library is empty"
    assert "abort_b" not in settings.to_json()


def test_a_fixed_round_keeps_its_ordinal_and_experiment_id_until_it_ends():
    from ouroloop.research.fixed import fixed_experiment_id, next_ordinal, round_ended
    declared = lambda o, op: {"kind": "ledger", "stage": "declared", "fixed_code": True, "ordinal": o,  # noqa: E731
                              "candidate": f"s/f{o}/{op}"}
    ended = lambda o: {"kind": "round", "fixed_code": True, "ordinal": o, "outcome": "rejected"}  # noqa: E731
    rows = [declared(0, "library"), ended(0), declared(1, "refit")]
    assert round_ended(rows, 0)["outcome"] == "rejected" and round_ended(rows, 1) is None
    assert next_ordinal(rows, "refit") == 1                     # the unfinished refit round is resumed
    assert next_ordinal(rows, "library") == 2 == next_ordinal(rows)   # a new library round takes a new ordinal
    assert next_ordinal([]) == 0
    a, b = fixed_experiment_id("s/f1/refit"), fixed_experiment_id("s/f1/refit")
    assert a == b and a.startswith("x_") and a != fixed_experiment_id("s/f2/refit")


def test_a_merge_reply_of_one_bare_entry_is_read():
    """2026-10-07 review: the MERGE prompt asks for one object; parse_distilled read only {"entries": [...]}."""
    bare = {"shelf": "s", "kind": "workflow", "title": "t", "when": "w", "body": "b", "avoid": ""}
    assert L.parse_distilled(json.dumps(bare), max_entries=1) == [bare]
    assert L.parse_distilled(json.dumps({"entries": [bare]}), max_entries=1) == [bare]
    assert L.parse_distilled(json.dumps({"title": "no body"}), max_entries=1) == []


def counts_of(activity):
    from collections import Counter
    return Counter(bench._arm_of(r.attempt_id) for r in activity.results.all())


def test_a_distillation_failure_pauses_the_stream_and_its_resume_pays_no_distillation_twice(fake_upstream, tmp_path,
                                                                                           monkeypatch):
    from ouroloop.providers import ReplyTruncated
    cfg0, act0, _ = prepared_v5(fake_upstream, tmp_path / "ref", monkeypatch, library=LIB)
    bench.baseline(cfg0, ".", out=lambda _line: None)
    ref = Distiller()
    bench.library_track(cfg0, ".", out=lambda _line: None, distiller=ref)
    cfg, act, _ = prepared_v5(fake_upstream, tmp_path / "cut", monkeypatch, library=LIB)
    bench.baseline(cfg, ".", out=lambda _line: None)

    class Flaky(Distiller):
        def __call__(self, system, user):
            if len(self.calls) == 2:
                self.calls.append((system, "failed"))
                raise ReplyTruncated("the research reply stopped at the model's output limit")
            return super().__call__(system, user)
    first, second = Flaky(), Distiller()
    paused = bench.library_track(cfg, ".", out=lambda _line: None, distiller=first)
    assert paused["status"] == "paused" and "ReplyTruncated" in paused["reason"]
    bench.library_track(cfg, ".", out=lambda _line: None, distiller=second)
    # every successful call is paid once: the failed one is the only call made twice
    assert len(first.calls) + len(second.calls) == len(ref.calls) + 1
    assert counts_of(act) == counts_of(act0)                                    # no attempt paid twice either


def test_an_interruption_after_the_pre_attempt_distillation_does_not_pay_it_again(fake_upstream, tmp_path,
                                                                                  monkeypatch):
    cfg0, act0, _ = prepared_v5(fake_upstream, tmp_path / "ref", monkeypatch, library=LIB)
    bench.baseline(cfg0, ".", out=lambda _line: None)
    ref = Distiller()
    bench.library_track(cfg0, ".", out=lambda _line: None, distiller=ref)
    order = json.loads((act0.root / "rsi" / "library_stream.json").read_text())["order"]
    failing = {"rsi.notify_after_update", "rsi.notify_after_update_variant"}            # W0's train failures
    at = next(i for i, t in enumerate(order) if i >= 1 and t not in failing)            # a task W0 passed: distilled
    cfg, act, _ = prepared_v5(fake_upstream, tmp_path / "cut", monkeypatch, library=LIB)
    bench.baseline(cfg, ".", out=lambda _line: None)
    restore = interrupt_arm(monkeypatch, lambda arm: arm == "library_first", after=at)
    first = Distiller()
    with pytest.raises(KeyboardInterrupt):
        bench.library_track(cfg, ".", out=lambda _line: None, distiller=first)
    restore()
    state = json.loads((act.root / "rsi" / "library_stream.json").read_text())
    assert state["pre_distilled"] == state["order"][state["cursor"]]            # the checkpoint before the attempt
    second = Distiller()
    bench.library_track(cfg, ".", out=lambda _line: None, distiller=second)
    assert len(first.calls) + len(second.calls) == len(ref.calls)
    assert counts_of(act) == counts_of(act0)


def interrupt_arm(monkeypatch, when, *, after: int = 0):
    """Stop a phase by hand inside an arm: at the (after+1)-th arm `when` picks, half its attempts run, then
    KeyboardInterrupt. -> a callable that restores run_arm."""
    real = bench.Improvement.run_arm
    seen = {"n": 0}

    def run_arm(self, arm, release_ref, order, **kw):
        if when(arm):
            seen["n"] += 1
            if seen["n"] == after + 1:
                order = list(order)
                real(self, arm, release_ref, order[:max(1, len(order) // 2)], **kw)
                raise KeyboardInterrupt(f"stopped during {arm}")
        return real(self, arm, release_ref, order, **kw)
    monkeypatch.setattr(bench.Improvement, "run_arm", run_arm)
    return lambda: monkeypatch.setattr(bench.Improvement, "run_arm", real)


def fixed_rows(activity, operator):
    from collections import Counter
    rows = xp.read(activity.root)
    stages = Counter(e["stage"] for e in rows if e.get("kind") == "ledger" and e["candidate"].endswith(f"/{operator}"))
    ends = [e for e in rows if e.get("kind") == "round" and e.get("fixed_code")]
    experiments = [e for e in rows if e.get("kind") == "experiment"]
    return stages, ends, experiments


@pytest.mark.parametrize("arm", ["train_screen", "subject", "confirm"])
def test_an_interrupted_snapshot_round_resumes_where_it_stopped_and_pays_nothing_twice(fake_upstream, tmp_path,
                                                                                      monkeypatch, arm):
    """2026-10-07 review: stopped by hand during the snapshot round's screen, its gate's first reading or its
    confirmation, the phase run again finishes the same round — the same ordinal, candidate and experiment id — with
    the attempts an uninterrupted run makes, one ledger row a stage, one round row, one experiment record, and the
    stream's records written once."""
    cfg0, act0, _ = prepared_v5(fake_upstream, tmp_path / "ref", monkeypatch, library=LIB)
    bench.baseline(cfg0, ".", out=lambda _line: None)
    ref = bench.library_track(cfg0, ".", out=lambda _line: None, distiller=Distiller())
    cfg, act, _ = prepared_v5(fake_upstream, tmp_path / "cut", monkeypatch, library=LIB)
    bench.baseline(cfg, ".", out=lambda _line: None)
    restore = interrupt_arm(monkeypatch, lambda a: a == arm)
    with pytest.raises(KeyboardInterrupt):
        bench.library_track(cfg, ".", out=lambda _line: None, distiller=Distiller())
    restore()
    assert json.loads((act.root / "rsi" / "library_stream.json").read_text())["status"] == "snapshot"
    again = Distiller()
    result = bench.library_track(cfg, ".", out=lambda _line: None, distiller=again)
    assert again.calls == []                                                    # the stream is not distilled again
    assert result["snapshot"]["outcome"] == ref["snapshot"]["outcome"] == "installed"
    assert counts_of(act) == counts_of(act0)                                    # nothing paid twice
    stages, ends, experiments = fixed_rows(act, "library")
    assert stages == fixed_rows(act0, "library")[0] == {"declared": 1, "drafted": 1, "admitted": 1, "screened": 1,
                                                         "gated": 1}
    assert len(ends) == 1 and ends[0]["outcome"] == "installed" and len(experiments) == 1
    kinds = [e.get("kind") for e in xp.read(act.root)]
    assert kinds.count("library_stream") == 1 and kinds.count("library_snapshot") == 1
    assert kinds.count("library_entry") == [e.get("kind") for e in xp.read(act0.root)].count("library_entry")
    assert experiments[0]["experiment"] == ends[0]["experiment_id"]
    assert experiments[0]["experiment"].startswith("x_")
    # a gate stopped mid-way continues from the experiment it froze (not a second freeze of the proposal)
    assert experiments[0]["notes"].startswith("states:" if arm == "train_screen" else "resumed:")


def test_a_snapshot_round_that_ended_answers_with_its_recorded_outcome(fake_upstream, tmp_path, monkeypatch):
    """2026-10-07 review: stopped after the snapshot round wrote its round row but before the stream saved `done`,
    the phase run again reads the ended round back — no stage, round row or attempt is written twice."""
    cfg, act, _ = prepared_v5(fake_upstream, tmp_path, monkeypatch, library=LIB)
    bench.baseline(cfg, ".", out=lambda _line: None)
    first = bench.library_track(cfg, ".", out=lambda _line: None, distiller=Distiller())
    assert first["snapshot"]["outcome"] == "installed"
    path = act.root / "rsi" / "library_stream.json"
    state = json.loads(path.read_text())
    state.pop("snapshot")
    state["status"] = "snapshot"
    path.write_text(json.dumps(state))
    paid, rows = counts_of(act), len(xp.read(act.root))
    again = bench.library_track(cfg, ".", out=lambda _line: None, distiller=Distiller())
    assert again["snapshot"]["outcome"] == "installed"
    assert counts_of(act) == paid and len(xp.read(act.root)) == rows
    stages, ends, experiments = fixed_rows(act, "library")
    assert stages["gated"] == 1 and len(ends) == 1 and len(experiments) == 1


def test_r_still_gets_its_three_rounds_when_a_fixed_rounds_gate_is_open(fake_upstream, tmp_path, monkeypatch):
    """2026-10-07 review: a fixed round's experiment never counts as one of R's rounds — R's segment leaves it open
    for its phase, runs its own three rounds, and the phase finishes the fixed round afterwards."""
    from test_research_session import memory_candidate
    cfg, act, _ = prepared_v5(fake_upstream, tmp_path, monkeypatch, rounds=3, library=LIB)
    bench.baseline(cfg, ".", out=lambda _line: None)
    restore = interrupt_arm(monkeypatch, lambda a: a == "subject")
    with pytest.raises(KeyboardInterrupt):
        bench.library_track(cfg, ".", out=lambda _line: None, distiller=Distiller())
    restore()
    calls = {"n": 0}

    def plan(inputs):
        calls["n"] += 1
        return [memory_candidate(inputs, name=f"m{calls['n']}", text="Be careful.\n",
                                 mechanism=f"note number {calls['n']} for careful work")]
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=rsi.SessionScript(plan))
    rounds = [r for r in result["rounds"] if r["outcome"] != "stopped"]
    from ouroloop.research.fixed import fixed_experiment_id
    fixed_id = fixed_experiment_id(f"{act.rsi.scope_id}/f0/library")
    assert len(rounds) == 3 and calls["n"] == 3
    assert all(r.get("experiment_id") != fixed_id for r in result["rounds"])
    finished = bench.library_track(cfg, ".", out=lambda _line: None, distiller=Distiller())
    assert finished["snapshot"]["outcome"] in ("installed", "rejected", "stale_parent")
    stages, ends, _ = fixed_rows(act, "library")
    assert stages["gated"] == 1 and len(ends) == 1


def test_an_interrupted_refit_gate_is_finished_by_the_next_learn(fake_upstream, tmp_path, monkeypatch):
    cfg, activity, w0 = prepared_v5(fake_upstream, tmp_path, monkeypatch, methods=LEARN)
    install_w(activity, audit_files("silent"))
    bench.baseline(cfg, ".", out=lambda _line: None)
    restore = interrupt_arm(monkeypatch, lambda a: a == "subject")
    with pytest.raises(KeyboardInterrupt):
        bench.learn(cfg, ".", out=lambda _line: None)
    restore()
    twins = counts_of(activity)
    result = bench.learn(cfg, ".", out=lambda _line: None)
    assert result["resumed"] and result["stage"] == "learn0" and result["refit"]["outcome"] == "installed"
    after = counts_of(activity)
    assert after["learn_A"] == twins["learn_A"] and after["learn_B"] == twins["learn_B"]   # no new reading
    stages, ends, experiments = fixed_rows(activity, "refit")
    assert stages == {"declared": 1, "drafted": 1, "admitted": 1, "screened": 1, "gated": 1}
    assert len(ends) == 1 and len(experiments) == 1
    assert len([e for e in xp.read(activity.root) if e.get("kind") == "method_fit"]) == 1


def test_learn_and_a_fixed_round_refuse_before_paying_what_the_budget_cannot_hold(fake_upstream, tmp_path,
                                                                                  monkeypatch):
    cfg, activity, w0 = prepared_v5(fake_upstream, tmp_path, monkeypatch, methods=LEARN, library=LIB)
    install_w(activity, audit_files("silent"))
    bench.baseline(cfg, ".", out=lambda _line: None)
    before = counts_of(activity)
    monkeypatch.setattr(bench.Improvement, "_verify_bound", lambda self: {"model_calls": 10 ** 9,
                                                                          "tool_calls": 10 ** 9})
    result = bench.learn(cfg, ".", out=lambda _line: None)
    assert result == {**result, "learned": False, "reason": "budget_exhausted"}
    assert counts_of(activity) == before                                        # not one twin attempt
    out = bench.library_track(cfg, ".", out=lambda _line: None, distiller=Distiller())
    assert out["snapshot"]["outcome"] == "budget_exhausted"
    assert counts_of(activity)["train_screen"] == 0                            # the snapshot's screen never ran
    _stages, ends, _ = fixed_rows(activity, "library")
    assert len(ends) == 1 and ends[0]["outcome"] == "budget_exhausted"


def test_the_library_phase_refuses_a_k_without_the_library(fake_upstream, tmp_path, monkeypatch):
    from test_research_session import prepared_v3
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="needs a K with the experience library"):
        bench.library_track(cfg, ".", out=lambda _line: None, distiller=Distiller())
