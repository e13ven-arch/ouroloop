"""The phases of an activity on the miniature upstream's rsi domain (docs/design/ACTIVITY.md §3, §4, §5, §9): smoke,
freeze, baseline, the final, report, export and cycle, end to end with the stirrup seed in a ProcessWorker and the
scripted actor; and the stops of the phases that come with stages 2 and 3."""
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
from ouroloop.contracts import short
from ouroloop.evaluate.final import FinalPlan
from ouroloop.evaluate.release import GateReceipt, NotAuthorized, Release
from ouroloop.harness.package import Limits, apply_patch, build
from ouroloop.runtime.app import App, exported_objects
from ouroloop.runtime.records import session_file

REPO = Path(__file__).resolve().parents[2]


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


def test_freeze_with_a_final_table_is_refused_while_w_star_is_w0(tmp_path, fake_upstream):
    a = rsi.activity(tmp_path, fake_upstream, final={})
    with pytest.raises(ValueError, match="the final compares W\\* with W0, and nothing was installed yet"):
        phases.freeze(a, out=quiet)
    assert not (a.root / "rsi" / "finalization").exists()


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
    # the memory is off in this K: the library step is done by being skipped, and the rounds wait for stage 3
    at_library = phases.cycle(a, until="library", out=quiet)
    assert at_library["at"] == "library" and at_library["steps"]["library"]["skipped"] == "[memory] enabled = false"
    for until in (None, "round-0", "round-2"):
        with pytest.raises(NotImplementedError, match="stage 3"):
            phases.cycle(a, until=until, out=quiet)
    assert provider.calls == calls


def test_the_round_names_its_stage(tmp_path, fake_upstream):
    a = rsi.activity(tmp_path, fake_upstream, freeze_k=False)
    with pytest.raises(NotImplementedError, match=r"improver package \(stage 3"):
        phases.round_(a, 0, out=quiet)


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
    assert "- test read 0 time(s)" in written["report.md"].decode()


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
    """A second package installed by a scripted gate: W0's prompt with one more line, its dev reading taken under
    the candidate release, an accepted receipt registered, and the CAS install on the seed release."""
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
    return candidate


def test_a_sealed_final_runs_w0_and_w_star_on_test_and_the_report_counts_its_reads(tmp_path, fake_upstream):
    a = rsi.activity(tmp_path, fake_upstream)
    phases.smoke(a, out=quiet)                                              # W0's priced reading
    w_star = install_w_star(a)
    assert rows(a, "test") == []
    lines = []
    b = reopen(a, final={})
    refs = phases.freeze(b, out=lines.append)
    assert any(line.startswith("estimate   W_star: 6 attempts to run") for line in lines)
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
                                "status": "completed", "claims": ["C1"], "arms": report["final"][0]["arms"]}]
    assert {arm: s["attempts"] for arm, s in report["final"][0]["arms"].items()} == {"W0": 6, "W_star": 6}
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


def test_only_an_installed_release_exports(tmp_path, fake_upstream):
    a = rsi.activity(tmp_path, fake_upstream)
    seed = a.active_release()
    never = Release(parent_ref=seed.ref(), package_ref=seed.package_ref, improver_ref=seed.improver_ref,
                    core_ref=seed.core_ref, receipt_ref=None, reason="import")
    a.objects.put(never)
    with pytest.raises(NotAuthorized, match="is not an installed, unrevoked release"):
        phases.export(a, never.ref(), tmp_path / "app", out=quiet)
    assert not (tmp_path / "app").exists()


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
                        and m not in ("ouroloop.memory", "ouroloop.memory.settings"))))
"""


def test_the_smoke_and_baseline_path_load_no_improver_memory_or_training_code(tmp_path, fake_upstream):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    done = subprocess.run([sys.executable, "-c", HOT_PATH, str(REPO), fake_upstream, str(tmp_path)],
                          capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) == []
