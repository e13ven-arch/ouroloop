"""v0.5 in the activity (docs/design/V0_5_PLAN.md): the v5 preset's K, R0 and session text; a method candidate through
the draft chain (kit completion, kit check, reach check) to the screen; the learn phase and its refit round; the
experience library's stream, its snapshot round and its retrieval; and v0.4's identities untouched. The fixture is the
miniature `rsi` domain with the scripted worker, so what is shown is the control flow and the records, not a model's
judgement.
"""
import json

import pytest
import rsi

from ouroloop import bench, policies as P
from ouroloop.agent import harness_package as hp
from ouroloop.agent.worker import ProcessWorker
from ouroloop.evolve import contracts as ct
from ouroloop.evolve import experience as xp
from ouroloop.research import library as L
from ouroloop.research import methods as M

V5_COMPONENTS = ("memory", "skills", "on_finish", "before_tool", "after_tool", "tools")


def v5_fixture(monkeypatch, *, methods=None, library=None, **kw):
    from test_research_session import v3_fixture
    preset = v3_fixture(monkeypatch, components=V5_COMPONENTS, **kw)
    v5 = json.loads(json.dumps(preset))
    v5["harness"]["methods"] = M.MethodSettings(**(methods or {})).to_json()
    v5["harness"]["library"] = L.LibrarySettings(**(library or {})).to_json()
    monkeypatch.setitem(bench.PRESETS, "v5_test", v5)
    return v5


def prepared_v5(fake_upstream, tmp_path, monkeypatch, *, rounds=1, methods=None, library=None, k=None, **kw):
    """prepared_v3's activity under the v5 test preset: (cfg, activity, W0 release)."""
    from test_cycle import cycle_config, gap_w0
    preset = v5_fixture(monkeypatch, methods=methods, library=library, **kw)
    preset["schedule"].update(worker_rounds=rounds, patience=max(rounds, 2))
    cfg = cycle_config(fake_upstream, tmp_path)
    del cfg["bench"]["budget"]
    cfg["improvement"]["preset"] = "v5_test"
    if k is not None:
        cfg["improvement"]["max_candidates_per_round"] = k
    cfg["pricing"] = {"input_per_1k": 0.001, "output_per_1k": 0.002, "max_input_tokens": 100000,
                      "max_output_tokens": 32768, "source": "test"}
    cfg["freeze"]["budget"] = {"model_calls": 4000, "tool_calls": 4000, "usd": 50}
    activity, _ = gap_w0(cfg, tmp_path)
    refs = bench.freeze(cfg, ".", out=lambda _line: None)
    cfg = {**cfg, "rsi": {**cfg["rsi"], **refs}}
    return cfg, activity, activity.releases.active()["release_ref"]


def ledger_of(activity):
    return {r["candidate"].rpartition("/")[2]: r for r in xp.ledger_rows(xp.read(activity.root))}


def k_harness(activity, release=None):
    rel = activity.objects.load(release or activity.releases.active()["release_ref"], ct.SystemRelease)
    return activity.objects.load(rel.core_ref, ct.CoreManifest).capabilities["harness"]


AFTER_WRITE = '''METHODS = {"m_after_write": {"family": "verification", "trigger": "fires", "kinds": ["record_write"],
                             "on": "recommend", "footprint": ["exists"]}}


def fires(event, view):
    args = event.get("args") or {}
    if event.get("tool") == "api_fetch" and str(args.get("method", "")).upper() == "%s":
        return {"kind": "record_write", "args": {"query": "post message"}, "context": {}}
    return None


def run(ctx, args):
    r = ctx.call_tool("api_search", {"query": str(args.get("query") or "post message"), "top_k": 3})
    return '{"endpoints_chars": ' + str(len(r.output)) + ', "unclassified": []}'
'''


def method_candidate(inputs, *, name="t1", method="PATCH"):
    manifest = {**hp.empty_manifest(hp.AGENT_API_VERSION), "tools": [{
        "name": "m_after_write", "description": "[method] USE WHEN a record was written. OUTPUT the message "
                                                "endpoints. Read-only evidence.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "entrypoint": "tools/m_after_write.py:run"}]}
    return {"dir": name, "files": {"manifest.json": json.dumps(manifest),
                                   "tools/m_after_write.py": AFTER_WRITE % method},
            "declaration": {"mechanism": "a method that finds the message endpoints after a write",
                            "site": "m_after_write", "carrier": "tool",
                            "trigger_condition": "after a record write"}}


# ---------------------------------------------------------------------------- K, R0 and the session's text
def test_the_v5_preset_freezes_the_method_and_library_tables_into_k_and_bootstraps_r0_with_the_methods_text(
        fake_upstream, tmp_path, monkeypatch):
    cfg, activity, w0 = prepared_v5(fake_upstream, tmp_path, monkeypatch)
    harness = k_harness(activity)
    assert harness["methods"] == M.MethodSettings().to_json()
    assert harness["library"] == L.LibrarySettings().to_json()
    prompts = P.prompts_of(activity.objects, activity.objects.load(w0, ct.SystemRelease).research_ref)
    assert prompts["research.propose"] == P.PROPOSE_SESSION_V5
    assert P.PROPOSE_SESSION_V5.startswith(P.PROPOSE_SESSION) and "m_<name>" in P.METHODS_PARAGRAPH
    # the real preset is the v3 preset plus the two tables, nothing else
    v3, v5 = bench.PRESETS["automationbench_harness_v3"], bench.PRESETS["automationbench_harness_v5"]
    assert {k: v for k, v in v5["harness"].items() if k not in ("methods", "library")} == v3["harness"]
    assert {k: v for k, v in v5.items() if k != "harness"} == {k: v for k, v in v3.items() if k != "harness"}


def test_a_config_cannot_set_the_preset_only_tables():
    base = {"improvement": {"mode": "automatic", "preset": "automationbench_harness_v5"}}
    with pytest.raises(ValueError, match="the preset fixes"):
        bench.harness_settings({**base, "harness_evolution": {"methods": {"method_calls": 99}}})
    with pytest.raises(ValueError, match="only a preset"):
        bench.harness_settings({"harness_evolution": {"package_api": "harness_package.v2", "research_session": {},
                                                      "library": {}}})
    with pytest.raises(ValueError, match="method library needs"):
        bench.harness_settings({**base, "harness_evolution": {"components": ["agent", "memory", "skills",
                                                                             "tools"]}})
    k = bench.harness_settings(base)
    assert k["methods"]["method_calls"] == 25 and k["library"]["snapshot_at"] == 50
    v3 = bench.harness_settings({"improvement": {"mode": "automatic", "preset": "automationbench_harness_v3"}})
    assert "methods" not in v3 and "library" not in v3


def test_the_v4_preset_keeps_its_k_r0_and_session_text(fake_upstream, tmp_path, monkeypatch):
    """V0_5_PLAN §6: a v0.4 activity's K has neither table, its R0 is PROPOSE_SESSION, and its session's tools and
    system text carry nothing of v0.5."""
    from test_research_session import memory_candidate, prepared_v3
    from ouroloop.research.session import SESSION_METHODS, TOOLS
    cfg, activity, w0 = prepared_v3(fake_upstream, tmp_path, monkeypatch)
    assert "methods" not in k_harness(activity) and "library" not in k_harness(activity)
    prompts = P.prompts_of(activity.objects, activity.objects.load(w0, ct.SystemRelease).research_ref)
    assert prompts["research.propose"] == P.PROPOSE_SESSION
    seen = []

    class Recording(rsi.SessionScript):
        def session_turn(self, system, messages, tools):
            seen.append([t.name for t in tools] + [t.description for t in tools])
            return super().session_turn(system, messages, tools)
    step = Recording(lambda inputs: [memory_candidate(inputs)])
    bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    system = next(r["system"] for r in step.requests if r["step"] == "session")
    assert SESSION_METHODS[:40] not in system and "playbook" not in system
    assert seen[0] == [t.name for t in TOOLS] + [t.description for t in TOOLS]


def test_the_two_support_arms_share_the_split_the_root_the_limits_and_differ_only_in_the_preset():
    """V0_5_PLAN §3: the v0.5 arm's config is the v0.4 arm's but for its workspace, its scope, its preset and its
    planning estimate — the same pinned split (so the same never-read test), the same root and final share, the same
    per-attempt limits, actor, research model, pricing, harness narrowing and schedule."""
    import tomllib
    from pathlib import Path
    examples = Path(__file__).resolve().parents[1] / "examples" / "automationbench"
    v4 = tomllib.loads((examples / "claude-v4-support.toml").read_text())
    v5 = tomllib.loads((examples / "claude-v5-support.toml").read_text())
    differ = {k for k in set(v4) | set(v5) if v4.get(k) != v5.get(k)}
    assert differ == {"workspace", "rsi", "improvement", "estimate"}
    assert v5["workspace"]["root"] == "workspace-claude-v5-support"
    assert {k: v for k, v in v5["rsi"].items() if k != "scope_id"} == {k: v for k, v in v4["rsi"].items()
                                                                       if k != "scope_id"}
    assert v5["rsi"]["scope_id"] == "support-claude-v5"
    assert {k: v for k, v in v5["improvement"].items() if k != "preset"} == \
        {k: v for k, v in v4["improvement"].items() if k != "preset"}
    assert v5["improvement"]["preset"] == "automationbench_harness_v5"
    assert v5["freeze"] == v4["freeze"] and v5["freeze"]["budget"]["usd"] == 450.0
    loaded = bench.BenchConfig.load(v5, examples)
    assert loaded.frozen_splits == {"support": "3da11232f5d5ff38"} and loaded.domains == ("support",)
    k4, k5 = bench.harness_settings(v4), bench.harness_settings(v5)
    assert {k: v for k, v in k5.items() if k not in ("methods", "library")} == k4
    assert k5["methods"] == M.MethodSettings().to_json() and k5["library"] == L.LibrarySettings().to_json()


# ---------------------------------------------------------------------------- a method through the draft chain
def test_a_method_candidate_is_completed_checked_reached_and_screened(fake_upstream, tmp_path, monkeypatch):
    cfg, activity, w0 = prepared_v5(fake_upstream, tmp_path, monkeypatch)
    step = rsi.SessionScript(lambda inputs: [method_candidate(inputs)])
    bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    t1 = ledger_of(activity)["t1"]
    assert t1["drafts"][-1]["ok"] is True and t1["admission"]["status"] == "admitted"
    assert t1["screen"]["net_flips"] is not None
    package = hp.load_package(activity.objects, t1["package_ref"])
    files = package.run_files
    assert files[M.ROUTER_PATH] == M.router_source() and M.MLIB_PATH in files
    assert M.table_of(files)["cells"] == {"m_after_write|record_write|*": {"action": "recommend"}}
    assert [h["name"] for h in package.manifest["hooks"]] == ["m_router_after", "m_router_before", "m_router_finish"]
    check = [e for e in ct.Events(activity.root).of("draft_check") if e.get("candidate", "").endswith("/t1")][-1]
    reach = check["reach"]["m_after_write"]
    assert reach["fired_tasks"] >= 2 and reach["reached"] >= 1
    assert all(c["fired"] and c["ran"] for c in reach["claimed"].values())
    system = next(r["system"] for r in step.requests if r["step"] == "session")
    from ouroloop.research.session import SESSION_METHODS
    assert system.startswith(P.PROPOSE_SESSION_V5) and SESSION_METHODS[:60] in system


def test_a_method_that_never_fires_goes_back_to_r_with_the_reach_counts(fake_upstream, tmp_path, monkeypatch):
    cfg, activity, w0 = prepared_v5(fake_upstream, tmp_path, monkeypatch)
    outputs = []

    def repair(message, inputs):
        outputs.append(message)
        return []
    step = rsi.SessionScript(lambda inputs: [method_candidate(inputs, method="DELETE")], repair=repair)
    bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    t1 = ledger_of(activity)["t1"]
    assert t1["drafts"][0]["ok"] is False and t1["outcome"] == "invalid"
    assert outputs and "reach_check: m_after_write's trigger fires on 0 recorded train task(s)" in outputs[0]
    assert "fired and ran without error on 0" in outputs[0]


class WithPlaybook(rsi.SessionScript):
    """A session whose finish carries a playbook: lesson lines for the round, in order."""

    def __init__(self, plan, lessons):
        super().__init__(plan)
        self.lessons, self.systems = list(lessons), []

    def session_turn(self, system, messages, tools):
        if len(messages) == 1:
            self.systems.append(system)
        return super().session_turn(system, messages, tools)

    def _declare(self, candidates):
        call = super()._declare(candidates)
        lesson = self.lessons.pop(0) if self.lessons else ""
        return type(call)(call.id, call.name, {**call.args, "playbook": lesson})


def test_a_playbook_lesson_is_kept_only_with_a_matching_ledger_citation_and_shown_to_the_next_session(
        fake_upstream, tmp_path, monkeypatch):
    cfg, activity, w0 = prepared_v5(fake_upstream, tmp_path, monkeypatch, rounds=2)
    # an earlier candidate on the ledger, as a finished round leaves it
    xp.append(activity.root, xp.ledger_row("declared", "seed/d0/x", scope="cyc", declaration={"carrier": "memory"}))
    xp.append(activity.root, xp.ledger_row("screened", "seed/d0/x", scope="cyc", outcome="screened_out", fixes=0,
                                           sample_regressions=2, net_flips=-2))
    good = "Broad checks broke passing tasks [seed/d0/x: sample_regressions 2]"
    step = WithPlaybook(lambda inputs: [method_candidate(inputs)],
                        [good + "\nA made-up lesson [seed/d0/x: fixes 5]\nno citation at all", ""])
    bench.improve(cfg, ".", out=lambda _line: None, researcher=step)
    rows = [e for e in xp.read(activity.root) if e.get("kind") == "method_playbook"]
    assert rows[0]["text"] == good and len(rows[0]["dropped"]) == 2
    assert any("not '5'" in d for d in rows[0]["dropped"]) and any("no citation" in d for d in rows[0]["dropped"])
    assert len(step.systems) == 2 and good not in step.systems[0] and good in step.systems[1]


def test_the_session_inputs_carry_the_method_library_inputs_from_train_only(fake_upstream, tmp_path, monkeypatch):
    cfg, activity, w0 = prepared_v5(fake_upstream, tmp_path, monkeypatch)
    work = bench.improvement(bench.open_activity(cfg, ".", out=lambda _l: None), template_ref=cfg["rsi"]["task_plan_ref"])
    evidence = work.measure(w0)
    experience = xp.read(activity.root)
    bound = P.bind_release(w0, profile="task", services=work.services(), authorization="installed")
    _, drafts, session = work.round_inputs(0, evidence, experience, bound, root=activity.root, research_model={})
    assert drafts.methods is not None and callable(drafts.reach)
    assert session.methods is not None
    names = set(session.extra_inputs)
    assert {"methods.json", "uncovered.json", "method_research.json", "playbook.md",
            "method_kit/README.md", "method_kit/m_router.py", "method_kit/_mlib.py"} <= names
    assert session.extra_inputs["method_kit/m_router.py"] == M.router_source()
    uncovered = json.loads(session.extra_inputs["uncovered.json"])
    refs = {a for c in uncovered for a in c["attempts"]}
    rows = {r.attempt_id: r for r in activity.results.all()}
    assert refs and all(rows[a].split == "train" for a in refs)
    assert all(c["status"] == "no method covers it" for c in uncovered)
