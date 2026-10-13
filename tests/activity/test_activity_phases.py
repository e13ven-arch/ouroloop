"""The phases of an activity on the miniature upstream's rsi domain (docs/design/ACTIVITY.md §3, §4, §5, §9; v1.2):
smoke, freeze, baseline (and its import from another workspace), the release gate at the end of the activity, the
final, report, export and cycle, end to end with the stirrup seed in a ProcessWorker and the scripted actor."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import rsi

from ouroloop.activity import phases
from ouroloop.activity.checks import startup_checks
from ouroloop.activity.workspace import open_activity
from ouroloop.contracts import write_json, short
from ouroloop.evaluate.final import FinalPlan
from ouroloop.evaluate.release import GateReceipt, NotAuthorized, Release
from ouroloop.harness.package import Limits, apply_patch, build
from ouroloop.runtime.app import App, exported_objects
from ouroloop.runtime.records import session_file

REPO = Path(__file__).resolve().parents[2]
TOOL_NAME = "post_channel_message"            # the scripted R's tool (tests/improver/scripted_r.py)


def quiet(_line):
    pass


class Crash(BaseException):
    """The process dying mid-run: nothing in the loop may swallow it."""


class CrashingWorker(rsi.BusinessWorker):
    """The scripted actor, until its n-th call."""

    def __init__(self, crash_at: int, **kw):
        super().__init__(**kw)
        self.crash_at = crash_at

    def chat(self, system, messages, specs, **kw):
        if self.calls + 1 >= self.crash_at:
            self.calls += 1
            raise Crash(f"the process died at call {self.calls}")
        return super().chat(system, messages, specs, **kw)


def book(a) -> dict:
    return json.loads((a.root / "rsi" / "budget" / "book.json").read_text(encoding="utf-8"))


def rows(a, split=None) -> list:
    return [r for r in a.results.all() if split is None or r.split == split]


def reopen(a, *, provider=None, **sections):
    """The activity of the same workspace under `rsi.config(**sections)`, opened as `ouroloop` opens it: the split on
    disk is read, not written again."""
    cfg = rsi.config(Path(a.config["_base"]), a.config["bench"]["upstream_path"], **sections)
    factory = (lambda _table: provider) if provider is not None else a.provider_factory
    return open_activity(cfg, out=quiet, worker_factory=a.worker_factory, provider_factory=factory)


# ---------------------------------------------------------------------------- smoke
def test_smoke_checks_only_calls_no_model_and_freezes_nothing(tmp_path, fake_upstream):
    provider = rsi.BusinessWorker()
    a = rsi.activity(tmp_path, fake_upstream, provider=provider, freeze_k=False)
    lines = []
    report = phases.smoke(a, checks_only=True, out=lines.append)
    assert report["ok"] and report["problems"] == [] and report["attempts"] == []
    assert report["splits"]["hash"] == a.splits.hash() == a.core.split_hash
    assert report["contract"]["failed"] == [] and report["leaks"] == [] and report["literals"]["findings"] == []
    assert provider.calls == 0 and rows(a) == []
    assert not (a.root / "core.json").exists() and a.releases.active_ref() is None
    assert not (a.root / "rsi" / "budget" / "book.json").exists()                  # no budget root either
    assert [line.split()[0] for line in lines if not line.startswith(" ")] == \
        ["upstream", "splits", "families", "contract", "audit", "literals", "result"]


def test_smoke_runs_one_dev_attempt_through_the_runner_shell(tmp_path, fake_upstream):
    provider = rsi.BusinessWorker()
    a = rsi.activity(tmp_path, fake_upstream, provider=provider)
    report = phases.smoke(a, out=quiet)
    assert report["ok"] and len(report["attempts"]) == 1
    attempt = report["attempts"][0]
    assert attempt["task_id"] == a.splits.ids("dev")[0] and attempt["split"] == "dev"
    assert attempt["system_release_ref"] == a.releases.active_ref() and attempt["validity"] == "valid"
    assert provider.calls > 0 and report["summary"]["cost_usd"] > 0
    assert session_file(a.root, attempt["evidence"]).exists()
    scopes = book(a)["scopes"]
    assert scopes["rsi-test:smoke"]["parent"] == "rsi-test" and scopes["rsi-test"]["limits"] == {"usd": 50.0}


def test_smoke_reads_dev_only(tmp_path, fake_upstream):
    provider = rsi.BusinessWorker()
    a = rsi.activity(tmp_path, fake_upstream, provider=provider)
    lines = []
    report = phases.smoke(a, tasks=[rsi.TRAIN[0], rsi.FINAL[0]], out=lines.append)
    assert not report["ok"] and report["attempts"] == [] and provider.calls == 0
    assert any(line.startswith("NOTE") and rsi.FINAL[0] in line and "the smoke reads dev only" in line
               for line in lines)
    assert rsi.FINAL[0] not in report["contract"].get("checked", [])


# ---------------------------------------------------------------------------- freeze
def test_freeze_prints_the_rsi_lines_and_is_idempotent(tmp_path, fake_upstream):
    a = rsi.activity(tmp_path, fake_upstream)
    lines = []
    refs = phases.freeze(a, out=lines.append)
    assert refs["core_ref"] == a.core_ref and refs["split_ref"] == a.splits.hash() and refs["scope_id"] == "rsi-test"
    rsi_lines = lines[lines.index("[rsi]"):]
    assert rsi_lines == ["[rsi]", 'scope_id = "rsi-test"', f'core_ref = "{a.core_ref}"',
                         f'split_ref = "{a.splits.hash()}"']
    root = a.root_account().remaining()
    assert root["limits"] == {"usd": 50.0} and root["allocations"] == {"final": {"usd": 10.0}}
    assert phases.freeze(a, out=quiet) == refs
    # the config the user fills in names the same activity
    named = reopen(a, rsi={k: v for k, v in refs.items() if k != "gate_template"})
    assert named.core_ref == a.core_ref and phases.freeze(named, out=quiet) == refs


def test_the_gate_template_is_ks_tasks_limits_and_thresholds(tmp_path, fake_upstream):
    a = rsi.activity(tmp_path, fake_upstream, gate={"min_gain": 0.1, "repeats": 2, "order_seed": 7},
                     limits={"max_steps": 10})
    template = phases.gate_template(a)
    assert template.task_ids == tuple(a.splits.ids("dev")) and sorted(template.task_ids) == sorted(rsi.DEV)
    assert (template.core_ref, template.split_ref) == (a.core_ref, a.splits.hash())
    assert (template.repeats, template.order_seed, template.control_pooling, template.confirmation_repeats) == \
        (2, 7, "pooled", 0)
    assert template.per_attempt_budget == {"max_steps": 10, "max_model_calls": 30, "max_tool_calls": 20,
                                           "max_seconds": 120, "max_tokens": 50000}
    assert template.retry_policy == {"max_infrastructure_retries": 1}
    gate = template.gate()
    assert (gate.min_gain, gate.max_regression_rate, gate.regression_drop, gate.min_tasks, gate.min_groups,
            gate.missing_cost_policy, gate.cost_beta0) == (0.1, 0.1, 0.5, 1, 1, "refuse", None)
    assert phases.freeze(a, out=quiet)["gate_template"] == template.ref()
    assert [e["plan"] for e in a.events.read("gate_template")] == [template.ref()]


def test_a_gate_over_fewer_dev_tasks_than_min_tasks_is_not_frozen(tmp_path, fake_upstream):
    a = rsi.activity(tmp_path, fake_upstream, gate={"min_tasks": 4})
    with pytest.raises(ValueError, match="freeze refused: the gate runs 3 dev tasks, fewer than its min_tasks 4"):
        phases.freeze(a, out=quiet)
    assert list(a.events.read("gate_template")) == [] and not (a.root / "rsi" / "budget" / "book.json").exists()


def test_freeze_with_a_final_table_seals_nothing_while_w_star_is_w0(tmp_path, fake_upstream):
    """The first freeze of a config that already names its final: the gate and the budget root are frozen and the
    [rsi] lines printed; the final is not sealed (nothing is installed) and says so."""
    a = rsi.activity(tmp_path, fake_upstream, final={})
    lines = []
    refs = phases.freeze(a, out=lines.append)
    assert "final_plan_ref" not in refs and any(l.startswith("final      not sealed: the final compares W* with W0, "
                                                             "and no release passed the release gate yet")
                                                for l in lines)
    assert "[rsi]" in lines and list(a.events.read("gate_template"))
    assert not (a.root / "rsi" / "finalization").exists()


def test_freeze_seals_no_final_while_a_round_is_unfinished(tmp_path, fake_upstream):
    """W* is the active release when the final is sealed: with a round still running, nothing is sealed."""
    a = rsi.activity(tmp_path, fake_upstream)
    phases.smoke(a, out=quiet)
    install_w_star(a)
    write_json(a.root / "rounds" / "r1" / "round.json", {"round_id": "x/r1", "index": 1, "status": "running",
                                                         "steps": {}})
    lines = []
    refs = phases.freeze(reopen(a, final={}), out=lines.append)
    assert "final_plan_ref" not in refs and any("round r1 is not finished" in l for l in lines)


# ---------------------------------------------------------------------------- baseline and cycle
def test_baseline_reads_train_once_and_dev_control_readings_times_and_pays_once(tmp_path, fake_upstream):
    provider = rsi.BusinessWorker()
    a = rsi.activity(tmp_path, fake_upstream, provider=provider, gate={"control_readings": 2})
    report = phases.baseline(a, out=quiet)
    assert report["release"] == a.releases.active_ref()
    assert report["train"]["tasks"] == len(rsi.TRAIN) and [d["tasks"] for d in report["dev"]] == [3, 3]
    train, dev = rows(a, "train"), rows(a, "dev")
    assert sorted(r.task_id for r in train) == sorted(rsi.TRAIN)
    assert sorted(r.task_id for r in dev) == sorted(rsi.DEV * 2)
    runs = a.results.runs()
    assert sorted(runs[r.run].candidate for r in dev) == ["base"] * 3 + ["base:reading-2"] * 3
    assert {r.system_release_ref for r in train + dev} == {a.releases.active_ref()}
    calls, spent = provider.calls, book(a)["reservations"]
    again = phases.baseline(a, out=quiet)
    assert provider.calls == calls and book(a)["reservations"] == spent and len(rows(a)) == len(train + dev)
    assert again["train"] == report["train"] and again["dev"] == report["dev"]


def test_a_baseline_cut_by_a_crash_resumes_and_pays_only_for_what_is_missing(tmp_path, fake_upstream):
    a = rsi.activity(tmp_path, fake_upstream, provider=CrashingWorker(12))
    with pytest.raises(Crash):
        phases.baseline(a, out=quiet)
    before = {r.attempt_id: r.to_json() for r in rows(a)}
    assert 0 < len(before) < len(rsi.TRAIN)
    resumed = rsi.BusinessWorker()
    a.provider_factory = lambda _table: resumed
    phases.baseline(a, out=quiet)
    after = {r.attempt_id: r for r in rows(a)}
    assert len(after) == len(rows(a)) == len(rsi.TRAIN) + len(rsi.DEV)        # each attempt recorded once
    assert all(after[k].to_json() == v for k, v in before.items())             # what was paid for was kept
    assert sorted(r.task_id for r in rows(a, "train") if r.attempt_id not in before) == \
        sorted(set(rsi.TRAIN) - {r["task_id"] for r in before.values()})


def test_cycle_stops_after_the_baseline_resumes_without_paying_and_names_the_stage_past_it(tmp_path, fake_upstream):
    provider = rsi.BusinessWorker()
    a = rsi.activity(tmp_path, fake_upstream, provider=provider)
    with pytest.raises(ValueError, match="--until 'round-9'"):
        phases.cycle(a, until="round-9", out=quiet)
    assert provider.calls == 0
    first = phases.cycle(a, until="baseline", out=quiet)
    assert first["status"] == "stopped" and first["at"] == "baseline"
    assert first["steps"]["baseline"]["train"]["tasks"] == len(rsi.TRAIN)
    assert json.loads((a.root / "rsi" / "cycle.json").read_text(encoding="utf-8"))["steps"] == first["steps"]
    calls = provider.calls
    assert phases.cycle(a, until="baseline", out=quiet) == first
    # the memory is off in this K: the library step is done by being skipped; a round waits for the frozen gate plan
    at_library = phases.cycle(a, until="library", out=quiet)
    assert at_library["at"] == "library" and at_library["steps"]["library"]["skipped"] == "[memory] enabled = false"
    for until in (None, "round-0", "round-2"):
        with pytest.raises(RuntimeError, match="no frozen gate plan"):
            phases.cycle(a, until=until, out=quiet)
    assert provider.calls == calls


# ---------------------------------------------------------------------------- report
def test_the_report_is_the_same_bytes_twice_and_its_phase_spend_sums_to_the_root(tmp_path, fake_upstream):
    a = rsi.activity(tmp_path, fake_upstream)
    phases.smoke(a, out=quiet)
    phases.baseline(a, out=quiet)
    first = phases.report(a, out=quiet)
    written = {p: (a.root / p).read_bytes() for p in ("report.json", "report.md")}
    second = phases.report(a, out=quiet)
    assert {p: (a.root / p).read_bytes() for p in ("report.json", "report.md")} == written
    assert first == second == json.loads(written["report.json"])
    spend = first["spend"]
    assert set(spend["phases"]) == {"smoke", "baseline"}
    for dim in ("usd", "model_calls", "input_tokens", "output_tokens"):
        assert sum(p[dim] for p in spend["phases"].values()) == pytest.approx(spend["root"][dim])
    settled = sum(r["measured"]["usd"] for r in book(a)["reservations"].values() if r["status"] == "settled")
    assert spend["root"]["usd"] == pytest.approx(settled) and spend["limits"] == {"usd": 50.0}
    assert first["baseline"]["train"]["tasks"] == len(rsi.TRAIN) and len(first["baseline"]["dev"]) == 1
    assert first["test_reads"] == 0 and first["final"] == [] and first["release"] == short(a.releases.active_ref())
    assert "- test read 0 time(s) by this activity's final" in written["report.md"].decode()


# ---------------------------------------------------------------------------- the final
def test_evaluate_refuses_without_a_sealed_plan(tmp_path, fake_upstream):
    provider = rsi.BusinessWorker()
    for name, ref in (("none", None), ("unknown", "finalization:sha256:" + "0" * 64)):
        a = rsi.activity(tmp_path / name, fake_upstream, provider=provider,
                         rsi={"scope_id": "rsi-test", "final_plan_ref": ref})
        with pytest.raises(ValueError, match=r"\[rsi\] final_plan_ref must name the final plan `ouroloop freeze` "
                                             r"sealed"):
            phases.evaluate(a, out=quiet)
        assert list(a.events.read("test_read")) == [] and rows(a, "test") == []
    assert provider.calls == 0


def install_w_star(a) -> Release:
    """A second package on the working line and promoted to the release line by a scripted gate: W0's prompt with one
    more line, its dev reading taken under the candidate release, an accepted receipt registered, the CAS install on
    the seed release and the promotion (EVALUATE §7, v1.2: W* is the release line's)."""
    w0 = a.package()
    files = apply_patch(w0, {"prompt/system.md": w0.files["prompt/system.md"] + "\nSay which records you changed.\n"})
    w1 = build(a.objects, files, limits=Limits.from_core(a.k), seed=w0, parent=w0)
    parent = a.active_release()
    receipt = GateReceipt(plan_ref=phases.freeze(a, out=quiet)["gate_template"], subject_ref=w1.ref(),
                          control_ref=w0.ref(), parent_release=parent.ref(), verdict="accepted",
                          reasons=("scripted",), metrics={"gain": 0.34})
    receipt_ref = a.objects.put(receipt)
    candidate = a.releases.candidate(w1.ref(), receipt_ref)
    phases.run_arm(a, release=candidate, split="dev", candidate="gate", role="candidate",
                   pairs=[(t, 0) for t in a.splits.ids("dev")], account=a.account("gate"))
    a.releases.register_receipt(receipt_ref, plan=receipt.plan_ref)
    installed = a.releases.install(candidate, receipt_ref, expected_parent=parent.ref(), operation_id="gate-1")
    assert installed.status == "installed" and a.releases.active_ref() == candidate.ref()
    released = a.releases.promote(candidate, receipt_ref, operation_id="promote-1")
    assert a.releases.released_ref() == released.ref() and released.parent_ref == candidate.ref()
    return released


def test_a_sealed_final_runs_w0_and_w_star_on_test_and_the_report_counts_its_reads(tmp_path, fake_upstream):
    a = rsi.activity(tmp_path, fake_upstream)
    phases.smoke(a, out=quiet)                                              # W0's priced reading
    w_star = install_w_star(a)
    assert rows(a, "test") == []
    lines = []
    b = reopen(a, final={})
    refs = phases.freeze(b, out=lines.append)
    assert any(line.startswith(f"estimate   W_star: {3 * len(rsi.FINAL)} attempts to run") for line in lines)
    assert f'final_plan_ref = "{refs["final_plan_ref"]}"' in lines
    plan = FinalPlan.from_json(b.objects.get(refs["final_plan_ref"]))
    assert plan.arms == {"W0": b.seed_release().ref(), "W_star": w_star.ref()} and plan.claims == ("C1",)
    assert plan.tasks == tuple(b.splits.ids("test")) and sorted(plan.tasks) == sorted(rsi.FINAL)
    assert (plan.repeats, plan.core_ref, plan.split_hash) == (3, b.core_ref, b.splits.hash())
    assert list(b.events.read("test_read")) == [] and rows(b, "test") == []      # sealing reads no test task
    assert phases.freeze(b, out=quiet) == refs                                 # the same plan seals again

    provider = rsi.BusinessWorker()
    named = {k: v for k, v in refs.items() if k != "gate_template"}
    c = reopen(a, provider=provider, final={}, rsi=named)
    out = phases.evaluate(c, out=quiet)
    assert set(out["arms"]) == {"W0", "W_star"} and out["estimate"]["expected"] is not None
    test = rows(c, "test")
    assert [r.attempt_id for r in test if r.validity != "valid"] == []      # every arm ran, W* too
    assert len(test) == 2 * len(rsi.FINAL) * 3
    assert {r.system_release_ref for r in test} == {plan.arms["W0"], plan.arms["W_star"]}
    assert all(str(r.pair_key).startswith(f"{refs['final_plan_ref']}/") for r in test)
    final_spend = c.account("final").remaining(allocation="final")
    assert final_spend["used"]["usd"] > 0
    report = phases.report(c, out=quiet)
    assert report["test_reads"] == 1
    assert report["final"] == [{"experiment": "rsi-test-final", "plan": short(refs["final_plan_ref"]),
                                "status": "completed", "claims": ["C1"], "arms": report["final"][0]["arms"],
                                "c1": report["final"][0]["c1"]}]
    c1 = report["final"][0]["c1"]                                            # W* - W0 over the test tasks
    assert c1["tasks"] == len(rsi.FINAL) and c1["low"] <= c1["points"] <= c1["high"] and c1["confidence"] == 0.95
    assert "W* - W0:" in (c.root / "report.md").read_text(encoding="utf-8")
    assert {arm: s["attempts"] for arm, s in report["final"][0]["arms"].items()} == \
        {"W0": 3 * len(rsi.FINAL), "W_star": 3 * len(rsi.FINAL)}
    assert "final" in report["spend"]["phases"]

    calls = provider.calls
    phases.evaluate(c, out=quiet)                                           # run again: nothing is paid twice
    assert provider.calls == calls and len(rows(c, "test")) == len(test)
    assert phases.report(c, out=quiet)["test_reads"] == 2                  # but the read is counted
    assert any("2 final run(s) already read the test tasks" in p for p in startup_checks(c, phase="baseline"))


# ---------------------------------------------------------------------------- export
def test_export_writes_the_seed_release_as_an_app(tmp_path, fake_upstream):
    a = rsi.activity(tmp_path, fake_upstream)
    seed = a.active_release()
    manifest = phases.export(a, None, tmp_path / "app", out=quiet)
    assert manifest.package_ref == seed.package_ref and manifest.release_ref == seed.ref()
    assert exported_objects(tmp_path / "app") == {"app", "file", "package", "validation"}
    app = App(tmp_path / "app")
    assert app.describe()["validation"] == {"release": short(seed.ref()), "reason": "seed"}
    assert app.package.ref() == seed.package_ref and app.core["model"]["identity"] == "scripted-1"
    assert set(app.core) == {"limits", "model", "worker", "harness", "memory"}
    exported = {p.stem for p in (tmp_path / "app").rglob("*.json")}
    assert seed.improver_ref.split(":")[-1] not in exported                      # R never ships


def test_only_a_released_release_exports(tmp_path, fake_upstream):
    a = rsi.activity(tmp_path, fake_upstream)
    seed = a.active_release()
    never = Release(parent_ref=seed.ref(), package_ref=seed.package_ref, improver_ref=seed.improver_ref,
                    core_ref=seed.core_ref, receipt_ref=None, reason="import")
    a.objects.put(never)
    with pytest.raises(NotAuthorized, match="is not a released, unrevoked release"):
        phases.export(a, never.ref(), tmp_path / "app", out=quiet)
    assert not (tmp_path / "app").exists()
    # a working-line release a held-out screen installed is not released either; the gate's promotion is
    phases.smoke(a, out=quiet)
    released = install_w_star(a)
    working = a.releases.load(released.parent_ref)
    with pytest.raises(NotAuthorized, match="is not a released, unrevoked release"):
        phases.export(a, working.ref(), tmp_path / "app", out=quiet)
    manifest = phases.export(a, None, tmp_path / "app", out=quiet)
    assert manifest.release_ref == released.ref() and manifest.package_ref == released.package_ref
    assert App(tmp_path / "app").describe()["validation"]["reason"] == "gate"


# ---------------------------------------------------------------------------- the release gate
def improved(tmp_path, fake_upstream):
    """The rsi activity after one improver round installed the scripted R's tool package on the working line."""
    import sys
    sys.path.insert(0, str(REPO / "tests" / "improver"))
    from rounds import round_activity
    s = round_activity(tmp_path, fake_upstream, "tool")
    result = phases.round_(s.a, 0, out=quiet)
    assert result.accepted and s.a.releases.active_ref() == result.installed
    return s


def test_the_release_gate_reads_the_working_line_against_w0_on_dev_and_promotes_it(tmp_path, fake_upstream):
    s = improved(tmp_path, fake_upstream)
    a = s.a
    work, seed = a.active_release(), a.seed_release()
    assert a.releases.released_ref() is None
    calls = s.actor.calls
    lines = []
    got = phases.gate(a, out=lines.append)
    assert got["status"] == "done" and got["working"] == work.ref() and got["verdict"] == "accepted", got
    assert got["released"] and got["fixed_count"] == 1 and got["regressed_count"] == 0     # dev_notify_after_update
    released = a.releases.load(got["released"])
    assert (released.reason, released.parent_ref, released.package_ref, released.receipt_ref) == \
        ("gate", work.ref(), work.package_ref, got["receipt"])
    assert a.releases.released_ref() == released.ref() and a.releases.active_ref() == work.ref()   # two lines
    receipt = GateReceipt.from_json(a.objects.get(got["receipt"]))
    assert (receipt.subject_ref, receipt.control_ref, receipt.parent_release) == \
        (work.package_ref, seed.package_ref, seed.ref())
    assert a.releases.registered_receipt(got["receipt"])
    # the working line's dev reading is the gate's, the control the baseline's pool: W0 read nothing new
    dev = [r for r in rows(a, "dev")]
    runs = a.results.runs()
    assert {runs[r.run].package_ref for r in dev} == {seed.package_ref, work.package_ref}
    assert not any(runs[r.run].role == "control" for r in dev)
    assert s.actor.calls > calls
    assert json.loads((a.root / "rsi" / "gate.json").read_text(encoding="utf-8"))["released"] == released.ref()
    assert [e["event"] for e in a.events.read("release_gate")] == ["release_gate"]
    assert [e["event"] for e in a.events.read("released")] == ["released"]
    assert lines[-1].startswith("gate       accepted (full): gain ")
    # run again: on record, nothing paid
    calls = s.actor.calls
    assert phases.gate(a, out=quiet) == got and s.actor.calls == calls
    # the final now compares W* (the release line) with W0
    b = reopen(a, final={}, improver=a.config["improver"])
    refs = phases.freeze(b, out=quiet)
    plan = FinalPlan.from_json(b.objects.get(refs["final_plan_ref"]))
    assert plan.arms == {"W0": seed.ref(), "W_star": released.ref()}
    report = phases.report(b, write=False, out=quiet)
    assert report["gate"]["verdict"] == "accepted" and report["released"] == short(released.ref())
    assert report["release"] == short(work.ref())


def test_the_release_gate_has_nothing_to_do_on_w0_and_refuses_an_unfinished_round(tmp_path, fake_upstream):
    a = rsi.activity(tmp_path, fake_upstream)
    phases.freeze(a, out=quiet)
    got = phases.gate(a, out=quiet)
    assert got == {"status": "nothing_to_gate", "why": "the working line is W0", "working": a.seed_release().ref()}
    assert a.releases.released_ref() is None and not (a.root / "rsi" / "gate.json").exists()
    s = improved(tmp_path / "improved", fake_upstream)
    write_json(s.a.root / "rounds" / "r1" / "round.json", {"round_id": "x/r1", "index": 1, "status": "running",
                                                           "steps": {}})
    with pytest.raises(ValueError, match="gate refused: round r1 is not finished"):
        phases.gate(s.a, out=quiet)


def test_the_cycle_runs_the_gate_only_under_until_gate(tmp_path, fake_upstream):
    import sys
    sys.path.insert(0, str(REPO / "tests" / "improver"))
    from rounds import round_activity
    s = round_activity(tmp_path, fake_upstream, "tool")
    first = phases.cycle(s.a, out=quiet)
    assert first["status"] == "stopped" and first["at"] == "rounds" and "gate" not in first["steps"]
    assert first["steps"]["round-0"]["accepted"] and not first["steps"]["round-1"]["accepted"]
    assert first["reason"] == "2 rounds without an acceptance" and first["rounds_ended"]["at"] == "round-2"
    assert s.a.releases.released_ref() is None
    second = phases.cycle(s.a, until="gate", out=quiet)
    assert second["status"] == "done" and second["at"] == "gate"
    assert second["steps"]["gate"]["verdict"] == "accepted" and s.a.releases.released_ref() == \
        second["steps"]["gate"]["released"]
    assert {k: v for k, v in second["steps"].items() if k.startswith("round")} == \
        {k: v for k, v in first["steps"].items() if k.startswith("round")}         # no round ran again


# ---------------------------------------------------------------------------- the two-way split: release by decision
def test_an_activity_without_dev_runs_rounds_without_a_gate_template_releases_by_decision_and_seals_the_final(
        tmp_path, fake_upstream):
    """Stage 4c (ACTIVITY §4, EVALUATE §6-§7): no dev, no baseline phase, no gate; the working line is released by
    decision after its own train reading, the final has W0 and W* on the test half, export reads the release line."""
    import sys
    sys.path.insert(0, str(REPO / "tests" / "improver"))
    from rounds import round_activity, round_state
    s = round_activity(tmp_path, fake_upstream, "tool", baseline=False, freeze=False, splits_table=rsi.NO_DEV_SPLITS)
    a = s.a
    assert a.splits.ids("dev") == [] and sorted(a.splits.ids("test")) == sorted(rsi.DEV + rsi.FINAL)
    assert sorted(a.splits.ids("train")) == sorted(rsi.TRAIN)
    lines = []
    refs = phases.freeze(a, out=lines.append)
    assert "gate_template" not in refs and list(a.events.read("gate_template")) == []
    assert any(line.startswith("gate       no dev tasks in this split: no gate template") for line in lines)
    assert startup_checks(a, phase="round") == []                     # a round needs no template here
    with pytest.raises(ValueError, match="release refused: the working line is W0"):
        phases.release(a, out=quiet)
    assert a.releases.released_ref() is None
    # round 0 reads train itself (no baseline phase) and accepts the scripted R's package
    r0 = phases.round_(a, 0, out=quiet)
    assert r0.accepted and round_state(a, 0)["steps"]["measure"]["measured"] == len(rsi.TRAIN)
    work = a.active_release()
    assert work.ref() == r0.installed
    assert phases.gate(a, out=quiet) == {"status": "nothing_to_gate", "why": "no dev tasks", "working": work.ref()}
    # the release: the studied tasks are read under W*'s own release (the held-out ones ran under the base at the
    # screen), then the promotion by decision, idempotent
    calls = s.actor.calls
    got = phases.release(a, out=lines.append)
    assert got["status"] == "done" and got["working"] == work.ref() and got["measured"] == len(rsi.STUDY)
    released = a.releases.load(got["released"])
    assert (released.reason, released.parent_ref, released.package_ref, released.receipt_ref) == \
        ("decision", work.ref(), work.package_ref, None)
    assert a.releases.released_ref() == released.ref() and a.releases.active_ref() == work.ref()
    runs = a.results.runs()
    measured = [r for r in rows(a, "train") if runs[r.run].candidate == f"release:{short(work.ref())}"]
    assert sorted(r.task_id for r in measured) == sorted(rsi.STUDY)
    assert {r.system_release_ref for r in measured} == {work.ref()} and s.actor.calls > calls
    (event,) = a.events.read("release_decision")
    assert event["released"] == released.ref() and event["measured"] == len(rsi.STUDY)
    calls = s.actor.calls
    assert phases.release(a, out=quiet) == {**got, "measured": 0} and s.actor.calls == calls
    assert json.loads((a.root / "rsi" / "release.json").read_text(encoding="utf-8"))["released"] == released.ref()
    # the final: sealed with W0 and W* over the test half, priced from W*'s own reading; run; the report's numbers
    b = reopen(a, final={}, improver=a.config["improver"])
    refs = phases.freeze(b, out=lines.append)
    plan = FinalPlan.from_json(b.objects.get(refs["final_plan_ref"]))
    assert plan.arms == {"W0": a.seed_release().ref(), "W_star": released.ref()}
    assert sorted(plan.tasks) == sorted(rsi.DEV + rsi.FINAL) and plan.repeats == 3
    assert any(line.startswith("estimate   W_star:") for line in lines)
    named = {k: v for k, v in refs.items() if k != "gate_template"}
    c = reopen(a, final={}, improver=a.config["improver"], rsi=named)
    out = phases.evaluate(c, out=quiet)
    assert set(out["arms"]) == {"W0", "W_star"}
    test = rows(c, "test")
    assert len(test) == 2 * len(plan.tasks) * 3 and all(r.validity == "valid" for r in test)
    report = phases.report(c, write=False, out=quiet)
    (final,) = report["final"]
    assert set(final["arms"]) == {"W0", "W_star"} and final["arms"]["W_star"]["passed"] >= final["arms"]["W0"]["passed"]
    assert report["released"] == short(released.ref()) and report["gate"] is None
    manifest = phases.export(c, None, tmp_path / "app", out=quiet)
    assert manifest.release_ref == released.ref()
    assert App(tmp_path / "app").describe()["validation"] == {"release": short(released.ref()), "reason": "decision"}


def test_the_smoke_of_an_activity_without_dev_reads_one_train_task(tmp_path, fake_upstream):
    provider = rsi.BusinessWorker()
    a = rsi.activity(tmp_path, fake_upstream, provider=provider, splits_table=rsi.NO_DEV_SPLITS)
    checks = phases.smoke(a, checks_only=True, out=quiet)
    assert checks["ok"] and checks["attempts"] == [] and provider.calls == 0
    lines = []
    report = phases.smoke(a, out=lines.append)
    assert report["ok"] and len(report["attempts"]) == 1
    assert report["attempts"][0]["task_id"] == a.splits.ids("train")[0] and report["attempts"][0]["split"] == "train"
    assert provider.calls > 0 and not any(r.split == "test" for r in rows(a))
    noted = phases.smoke(a, tasks=[rsi.FINAL[0]], out=lines.append)
    assert not noted["ok"] and any("are not train tasks: the smoke reads train only" in line for line in lines)


def test_an_activity_with_dev_releases_by_its_gate_not_by_decision(tmp_path, fake_upstream):
    s = improved(tmp_path, fake_upstream)
    with pytest.raises(ValueError, match="release refused: this activity has dev tasks"):
        phases.release(s.a, out=quiet)
    assert s.a.releases.released_ref() is None


def test_a_zero_dev_ratio_freezes_a_split_without_dev_tasks(tmp_path, fake_upstream):
    """[bench] ratios with dev 0 under the stratified rule: every family is train or test (ACTIVITY §4)."""
    cfg = rsi.config(tmp_path, fake_upstream, bench={"ratios": {"train": 0.5, "dev": 0.0, "test": 0.5},
                                                     "split_rule": "stratified"})
    a = open_activity(cfg, out=quiet, freeze_k=False, worker_factory=lambda: None,
                      provider_factory=lambda _t: rsi.BusinessWorker())
    assert a.splits.ids("dev") == [] and a.splits.ids("train") and a.splits.ids("test")
    assert len(a.splits.ids("train")) + len(a.splits.ids("test")) == len(rsi.TRAIN) + len(rsi.DEV) + len(rsi.FINAL)
    assert phases.gate_template(a) is None


# ---------------------------------------------------------------------------- the working line across activities
def test_an_activity_seeded_from_another_ones_release_line_starts_at_its_w_star(tmp_path, fake_upstream):
    """DESIGN §6.7 (v1.3), EVALUATE §7: [rsi] seed_from makes the source's released W* this activity's seed (reason
    import), R's estimates and playbook come along, the checks and the smoke read the imported seed, and the final's
    W0 arm is it."""
    import sys
    sys.path.insert(0, str(REPO / "tests" / "improver"))
    from rounds import round_activity
    first = round_activity(tmp_path / "first", fake_upstream, "tool", baseline=False, freeze=False,
                           splits_table=rsi.NO_DEV_SPLITS)
    phases.freeze(first.a, out=quiet)
    assert phases.round_(first.a, 0, out=quiet).accepted
    released = a_released = phases.release(first.a, out=quiet)["released"]
    w_star = first.a.releases.load(released)
    assert (first.a.root / "ledger" / "estimates.json").exists() and (first.a.root / "ledger" / "playbook.md").exists()
    lines = []
    second = rsi.activity(tmp_path / "second", fake_upstream, out=lines.append, splits_table=rsi.NO_DEV_SPLITS,
                          improver=first.a.config["improver"],
                          rsi={"scope_id": "rsi-test", "seed_from": str(first.a.root)})
    seed = second.seed_release()
    assert (seed.reason, seed.parent_ref, seed.package_ref, seed.receipt_ref) == ("import", None, w_star.package_ref,
                                                                                  None)
    assert second.active_release().ref() == seed.ref() and second.releases.released_ref() is None
    assert second.objects.has(w_star.package_ref)
    assert TOOL_NAME in [t["name"] for t in json.loads(second.package().files["manifest.json"])["tools"]]
    (event,) = second.events.read("seed_imported")
    assert (event["source"], event["source_release"], event["package"]) == (str(first.a.root), a_released,
                                                                            w_star.package_ref)
    assert sorted(event["policy_files"]) == ["ledger/estimates.json", "ledger/playbook.md"]
    assert event["source_package"] == w_star.package_ref and event["rebased"] == []     # the same code seed: nothing to replay
    assert event["source_line"] == "released"
    assert (second.root / "ledger" / "estimates.json").read_bytes() == \
        (first.a.root / "ledger" / "estimates.json").read_bytes()
    assert any(line.startswith("release    ") and "imported" in line for line in lines)
    # the checks and the smoke take the imported seed as the seed; the final compares with it
    assert startup_checks(second, phase="round") == []
    report = phases.smoke(second, checks_only=True, out=quiet)
    assert report["ok"] and report["literals"]["findings"] == []
    phases.freeze(second, out=quiet)
    assert phases.final_plan(second).arms == {"W0": seed.ref()}
    # opened again, the same seed
    again = rsi.activity(tmp_path / "second", fake_upstream, splits_table=rsi.NO_DEV_SPLITS,
                         improver=first.a.config["improver"], rsi={"scope_id": "rsi-test", "seed_from": str(first.a.root)})
    assert again.seed_release() == seed and len(list(again.events.read("seed_imported"))) == 1
    # a source without a release line, or no workspace at all, is refused before anything is written
    from ouroloop.activity.config import ConfigError
    bare = rsi.activity(tmp_path / "bare", fake_upstream, splits_table=rsi.NO_DEV_SPLITS)
    with pytest.raises(ConfigError, match="has no release line"):
        rsi.activity(tmp_path / "third", fake_upstream, splits_table=rsi.NO_DEV_SPLITS,
                     rsi={"scope_id": "rsi-test", "seed_from": str(bare.root)})
    with pytest.raises(ConfigError, match="is not an activity's workspace"):
        rsi.activity(tmp_path / "fourth", fake_upstream, rsi={"scope_id": "rsi-test", "seed_from": str(tmp_path / "x")})
    # a refused import leaves no K behind (2026-10-13): the next freeze, under whatever code, starts clean
    assert not (tmp_path / "third" / "core.json").exists() and not (tmp_path / "fourth" / "core.json").exists()
    assert not (tmp_path / "third" / "rsi" / "events.jsonl").exists()


def test_an_activity_can_be_seeded_from_another_ones_working_line(tmp_path, fake_upstream):
    """DESIGN §6.7 (2026-10-12): [rsi] seed_line = "working" imports the head of the source's working line — a source
    that closed without a final was never released; its head passed its own held-out screen. Refused without the knob
    when the source has no release line; the event names the line."""
    import sys
    sys.path.insert(0, str(REPO / "tests" / "improver"))
    from rounds import round_activity
    from ouroloop.activity.config import ConfigError
    first = round_activity(tmp_path / "first", fake_upstream, "tool", baseline=False, freeze=False,
                           splits_table=rsi.NO_DEV_SPLITS)
    phases.freeze(first.a, out=quiet)
    assert phases.round_(first.a, 0, out=quiet).accepted
    head = first.a.active_release()
    assert first.a.releases.released_ref() is None and head.reason == "screen"
    with pytest.raises(ConfigError, match="has no release line"):
        rsi.activity(tmp_path / "refused", fake_upstream, splits_table=rsi.NO_DEV_SPLITS,
                     improver=first.a.config["improver"], rsi={"scope_id": "rsi-test", "seed_from": str(first.a.root)})
    with pytest.raises(ConfigError, match="seed_line is one of"):
        rsi.activity(tmp_path / "bad", fake_upstream, splits_table=rsi.NO_DEV_SPLITS,
                     rsi={"scope_id": "rsi-test", "seed_from": str(first.a.root), "seed_line": "active"})
    with pytest.raises(ConfigError, match="seed_line needs seed_from"):
        rsi.activity(tmp_path / "bad2", fake_upstream, splits_table=rsi.NO_DEV_SPLITS,
                     rsi={"scope_id": "rsi-test", "seed_line": "working"})
    lines = []
    second = rsi.activity(tmp_path / "second", fake_upstream, out=lines.append, splits_table=rsi.NO_DEV_SPLITS,
                          improver=first.a.config["improver"],
                          rsi={"scope_id": "rsi-test", "seed_from": str(first.a.root), "seed_line": "working"})
    seed = second.seed_release()
    assert (seed.reason, seed.package_ref) == ("import", head.package_ref)
    (event,) = second.events.read("seed_imported")
    assert (event["source_line"], event["source_release"], event["package"]) == ("working", head.ref(),
                                                                                 head.package_ref)
    assert any("'s working " in line for line in lines)
    assert event["origin"] == {"workspace": str(first.a.root), "release": first.a.seed_release().ref()}
    assert startup_checks(second, phase="round") == []
    phases.freeze(second, out=quiet)
    assert phases.final_plan(second).arms == {"W0": seed.ref()}
    # a chain: the third imports the second's working head (its imported seed); the replay's baseline is the first's
    # code seed, so R's package comes through whole and nothing is replayed under the same code
    third = rsi.activity(tmp_path / "third", fake_upstream, splits_table=rsi.NO_DEV_SPLITS,
                         improver=first.a.config["improver"],
                         rsi={"scope_id": "rsi-test", "seed_from": str(second.root), "seed_line": "working"})
    (event3,) = third.events.read("seed_imported")
    assert event3["origin"] == event["origin"] and event3["rebased"] == [] and event3["kept_over_code"] == []
    assert third.seed_release().package_ref == head.package_ref
    assert TOOL_NAME in [t["name"] for t in json.loads(third.package().files["manifest.json"])["tools"]]


def test_an_imported_w_star_is_replayed_onto_this_codes_seed():
    """DESIGN §6.7 (v1.6): the files R never touched take the new seed's version, a file the new seed adds comes in, R's
    changes and additions stay, R's deletions stay deleted."""
    from ouroloop.activity.workspace import rebase_files
    src_seed = {"agent/a.py": "old adapter", "middlewares/g.py": "guard", "prompt/system.md": "p", "tools/t.py": "t"}
    w_star = {"agent/a.py": "old adapter", "middlewares/g.py": "guard", "prompt/system.md": "R's prompt",
              "middlewares/scope.py": "R's hook"}                                        # R deleted tools/t.py
    new_seed = {"agent/a.py": "new adapter", "middlewares/g.py": "guard", "prompt/system.md": "p2",
                "tools/t.py": "t", "middlewares/new.py": "new"}
    files, rebased, kept = rebase_files(src_seed, w_star, new_seed)
    assert rebased == ["agent/a.py", "middlewares/new.py"] and kept == ["prompt/system.md"]
    assert files["agent/a.py"] == "new adapter" and files["middlewares/new.py"] == "new"
    assert files["prompt/system.md"] == "R's prompt" and files["middlewares/scope.py"] == "R's hook"
    assert "tools/t.py" not in files and files["middlewares/g.py"] == "guard"
    assert rebase_files(src_seed, dict(src_seed), src_seed) == (src_seed, [], [])


# ---------------------------------------------------------------------------- the baseline's import
def test_the_baseline_is_imported_from_a_workspace_of_the_same_attempt_identity(tmp_path, fake_upstream):
    """ACTIVITY §4, EVALUATE §8 (v1.2): the second workspace copies the first one's W0 readings, grading rows and
    transcripts and pays nothing; a workspace of another identity imports nothing."""
    provider = rsi.BusinessWorker()
    first = rsi.activity(tmp_path / "first", fake_upstream, provider=provider)
    phases.baseline(first, out=quiet)
    paid = provider.calls
    # the second workspace starts from the first one's frozen split (as a real one copies it): the same identity
    import shutil
    target = tmp_path / "second" / "ws" / "bench" / rsi.DOMAIN / "splits.json"
    target.parent.mkdir(parents=True)
    shutil.copy(first.results.manifest_path(), target)
    second = rsi.activity(tmp_path / "second", fake_upstream, provider=provider,
                          rsi={"scope_id": "rsi-test", "baseline_from": str(first.root)})
    assert second.core.attempt_identity(second.seed_release().package_ref) == \
        first.core.attempt_identity(first.seed_release().package_ref)
    lines = []
    report = phases.baseline(second, out=lines.append)
    imported = report["imported"]
    assert imported["imported"] and imported["attempts"] == len(rsi.TRAIN) + len(rsi.DEV)
    assert provider.calls == paid                                       # nothing paid
    assert report["train"]["tasks"] == len(rsi.TRAIN) and report["train"]["attempts"] == len(rsi.TRAIN)
    assert sorted(r.task_id for r in rows(second, "train")) == sorted(rsi.TRAIN)
    assert sorted(r.task_id for r in rows(second, "dev")) == sorted(rsi.DEV)
    assert {r.attempt_id for r in rows(second)} == {r.attempt_id for r in rows(first)}
    for r in rows(second):
        assert session_file(second.root, r.evidence or r.attempt_id).exists()
    (event,) = second.events.read("baseline_imported")
    assert event["source"] == str(first.root) and event["attempts"] == imported["attempts"]
    assert any(line.startswith("baseline   imported") for line in lines)
    # what the rounds and the gate read is there: the train reading per task, the grading rows, the dev pool
    from ouroloop.improver.evidence import Evidence
    from ouroloop.improver.screen import control_pool, template
    assert Evidence(second).missing(second.seed_release().package_ref) == []
    assert all(second.results.grading(r.attempt_id) for r in rows(second, "train"))
    phases.freeze(second, out=quiet)
    assert len(control_pool(second, second.seed_release().package_ref, template(second).task_ids)) == len(rsi.DEV)
    # a second import copies nothing twice
    assert phases.baseline(second, out=quiet)["imported"]["attempts"] == 0
    assert len(list(rows(second))) == len(rsi.TRAIN) + len(rsi.DEV)
    # another identity (another step limit) imports nothing and pays for its own baseline
    target = tmp_path / "other" / "ws" / "bench" / rsi.DOMAIN / "splits.json"
    target.parent.mkdir(parents=True)
    shutil.copy(first.results.manifest_path(), target)
    other = rsi.activity(tmp_path / "other", fake_upstream, provider=provider, limits={"max_steps": 11},
                         rsi={"scope_id": "rsi-test", "baseline_from": str(first.root)})
    report = phases.baseline(other, out=quiet)
    assert not report["imported"]["imported"] and "AttemptIdentity differs" in report["imported"]["why"]
    assert provider.calls > paid


# ---------------------------------------------------------------------------- the hot path
HOT_PATH = """
import json, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests", sys.argv[2]]
import rsi
from ouroloop.activity import phases
root = Path(sys.argv[3])
a = rsi.activity(root, sys.argv[2])
phases.smoke(a, checks_only=True, out=lambda _l: None)
phases.smoke(a, out=lambda _l: None)
phases.baseline(a, limit=1, out=lambda _l: None)
# K's memory section reads its defaults from ouroloop.memory.settings (standard library only); W0's path starts no
# memory: no client, bridge, service, stream or reader, and never the sidecar's own modules
print(json.dumps(sorted(m for m in sys.modules if m.startswith(("ouroloop.train", "ouroloop.improver",
                                                                 "ouroloop.memory"))
                        and m not in ("ouroloop.memory", "ouroloop.memory.settings", "ouroloop.improver",
                                      "ouroloop.improver.settings"))))
"""


def test_the_smoke_and_baseline_path_load_no_improver_memory_or_training_code(tmp_path, fake_upstream):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    done = subprocess.run([sys.executable, "-c", HOT_PATH, str(REPO), fake_upstream, str(tmp_path)],
                          capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) == []
