"""The verification ladder (docs/design/IMPROVER.md §7 the declaration, §8 the ladder, §13 "阶梯"): every rung a
candidate climbs before money is spent, on a real VerifyContext — the rsi activity of tests/improver/rounds.py with its
baseline run (the forgetful actor fails the two notify tasks and passes the other three train tasks), the parent the
active release's package, the train readings' sessions for V2b, the host's ProcessWorker(for_tests) for V1 and V2.

Covered:
- V0 (verify/static.py), one case per rule: the package shape and K's limits (check_shape: a limit, a path outside
  the shape, a vendored LICENSE, a manifest that is not JSON); a kind or a hook site K does not allow, by check_shape
  and by the diff (a removed prompt or hook); the host-only memory snapshot written by a non-memory candidate (and a
  memory candidate, and a candidate on a parent that holds a snapshot); a forbidden literal on a changed line and not
  on the seed's unchanged lines; a newly added forbidden import or call, the same import already in the parent's file
  is not new, a module that does not parse; the declaration checks (changes nothing; the declared kind not what the
  files change; tool / hook / skill / prompt site; extra kinds besides the declared one and `test`; predicted_affected
  outside train or empty; a removal that deletes nothing).
- components(): each component kind; unchanged manifest entries written raw or normalised are not changes.
- read_declaration (declaration.py): what finish's arguments must hold.
- V1 (verify/unit.py): a tool that raises at import, an agent that crashes on every task, a package test that fails.
- V2a (verify/toolcheck.py): a script that does the task with the tool passes; one that never calls the tool is
  refused; a world the grader fails; a script error; a task that is not train; passes need toolcheck_min_pass.
- V2b (verify/replay.py): triggers on the claimed failing tasks and the false-trigger rate on passing attempts, each
  of the six sites' rebuilt events; a hook that never acts fails; replay_min_triggers; a hook answering outside its
  site's protocol fails.
- V2c (verify/reach.py): a description that reaches train tasks beyond the claimed ones passes with the lexical
  embed; one that reaches none fails; the claimed tasks do not count; reach_min_score; ctx.embed when given.
- run_ladder / ladder_for / Verdict: the rungs per kind, the climb stops at the first failure, passed() and text().
"""
from __future__ import annotations

import copy
import dataclasses
import json

import pytest
from rounds import quiet, round_activity
from scripted_r import CONTACTS, HOOK_CODE, HOOK_ENTRY, HUBSPOT, SLACK, TOOL_CODE, TOOL_ENTRY, script_for

from ouroloop.harness.kinds import MEMORY_PATH
from ouroloop.harness.package import Limits, build
from ouroloop.improver.components import components, without
from ouroloop.improver.declaration import Declaration, read_declaration
from ouroloop.improver.round import Round
from ouroloop.improver.verify import Verdict, VerifyContext, ladder_for, run_ladder
from ouroloop.improver.verify.reach import lexical_embed, reach_check
from ouroloop.improver.verify.replay import replay_check, site_events
from ouroloop.improver.verify.static import static_check
from ouroloop.improver.verify.toolcheck import toolcheck
from ouroloop.improver.verify.unit import unit_check
from ouroloop.activity.presets import AUTOMATIONBENCH_V1
from ouroloop.memory.snapshot import fit_snapshot, snapshot_candidate

FAILING = ("rsi.notify_after_update", "rsi.notify_after_update_variant")
PASSING = ("rsi.update_stage", "rsi.policy_hold", "rsi.notify_once")
TOOL = TOOL_ENTRY["name"]
TOOL_PATH = "tools/post_channel_message.py"
# words every train request shares: a description made of them speaks to every train task
COMMON = ("Read the contact before you change it, keep the change minimal, do not touch any other contact, and check "
          "what each call returned before moving on.")


def refuse(*_a, **_kw):
    raise AssertionError("the ladder pays for nothing: no run_arm")


@pytest.fixture(scope="module")
def ctx(tmp_path_factory, fake_upstream) -> VerifyContext:
    """The round's own VerifyContext (Round.vctx) for round 0 of the tool activity: baseline run, gate frozen."""
    s = round_activity(tmp_path_factory.mktemp("ladder"), fake_upstream, "tool")
    return Round(s.a, 0, run_arm=refuse, out=quiet).vctx()


@pytest.fixture(scope="module")
def parent(ctx):
    return ctx.parent


# ---------------------------------------------------------------------------- candidates
def rewrite(files: dict, change=None, add: dict | None = None, drop=()) -> dict:
    """`files` with paths dropped, added or replaced, and the manifest changed by `change(m)` (dumped as R writes
    it)."""
    out = {p: t for p, t in files.items() if p not in set(drop)}
    out.update(add or {})
    if change is not None:
        m = json.loads(out["manifest.json"])
        change(m)
        out["manifest.json"] = json.dumps(m, indent=1) + "\n"
    return out


def with_tool(parent, code: str = TOOL_CODE, entry: dict | None = None, add: dict | None = None, change=None) -> dict:
    entry = dict(entry or TOOL_ENTRY)

    def edit(m):
        m["tools"].append(entry)
        if change:
            change(m)
    return rewrite(parent.files, edit, {entry["entrypoint"].partition(":")[0]: code, **(add or {})})


def with_hook(parent, code: str, site: str = "on_finish", name: str = "remind_ops", fn: str = "remind",
              enabled: bool = True) -> dict:
    entry = {"name": name, "site": site, "entrypoint": f"middlewares/{name}.py:{fn}"}
    if not enabled:
        entry["enabled"] = False
    return rewrite(parent.files, lambda m: m["hooks"].append(entry), {f"middlewares/{name}.py": code})


def with_skill(parent, description: str, name: str = "ops-notify") -> dict:
    path = f"skills/{name}.md"
    return rewrite(parent.files, lambda m: m["skills"].append({"name": name, "path": path, "description": description}),
                   {path: f"# {name}\n\n{description}\n"})


def appended(parent, path: str, text: str = "\nMore.\n") -> dict:
    return rewrite(parent.files, add={path: parent.files[path] + text})


def decl(kind: str, site, predicted=FAILING, **kw) -> Declaration:
    return Declaration(dir="cand", cause_id="rsi-test/r0:missed_action", kind=kind, site=site,
                       predicted_affected=tuple(predicted), trigger="a request asks for a message in #ops", **kw)


def tool_decl(scripts: dict | None = None, predicted=FAILING, **kw) -> Declaration:
    return decl("tool", TOOL, predicted, scripts={t: script_for(t) for t in FAILING} if scripts is None else scripts,
                **kw)


def with_core(ctx, edit) -> VerifyContext:
    core = copy.deepcopy(ctx.core)
    edit(core["harness"])
    return dataclasses.replace(ctx, core=core)


def with_settings(ctx, **kw) -> VerifyContext:
    return dataclasses.replace(ctx, settings=dataclasses.replace(ctx.settings, **kw))


def v0(files, d, ctx) -> dict:
    return static_check(files, d, ctx)


# ---------------------------------------------------------------------------- the fixture itself
def test_the_context_is_the_rounds_train_view(ctx):
    """What the rungs below rely on: the forgetful actor fails exactly the notify tasks; V2b replays those sessions and
    the passing ones; the V1 fixture is a train task; nothing is a dev or test task."""
    assert sorted(ctx.failed_sessions) == sorted(FAILING)
    assert sorted(ctx.passed_sessions) == sorted(PASSING)
    assert set(ctx.train_tasks) == set(FAILING) | set(PASSING) == set(ctx.train_texts)
    assert ctx.fixture_task in ctx.train_tasks
    assert ctx.parent.ref() == ctx.seed.ref()          # round 0: the parent is the seed


# ---------------------------------------------------------------------------- V0: shape and K
def _over_max_tools(ctx, parent):
    return with_core(ctx, lambda h: h["limits"].update(max_tools=1)), with_tool(parent), "2 tools: K allows 1"


def _outside_shape(ctx, parent):
    return ctx, with_tool(parent, add={"scripts/run.sh": "echo hi\n"}), "not a package file"


def _vendor_license(ctx, parent):
    path = "vendor/stirrup/LICENSE"
    return ctx, with_tool(parent, add={path: parent.files[path] + "\nAll rights waived.\n"}), "differs from the seed's"


def _manifest_not_json(ctx, parent):
    files = with_tool(parent)
    files["manifest.json"] = files["manifest.json"][:-3]
    return ctx, files, "manifest.json is not JSON"


@pytest.mark.parametrize("case", [_over_max_tools, _outside_shape, _vendor_license, _manifest_not_json],
                         ids=["over-a-K-limit", "path-outside-the-shape", "vendored-license", "manifest-not-json"])
def test_v0_refuses_what_check_shape_refuses(ctx, parent, case):
    vctx, files, why = case(ctx, parent)
    got = v0(files, tool_decl(), vctx)
    assert not got["passed"]
    assert why in got["detail"]


def test_v0_passes_a_well_formed_tool_candidate(ctx, parent):
    got = v0(with_tool(parent), tool_decl(), ctx)
    assert got == {"passed": True, "detail": ""}


def test_v0_refuses_a_kind_k_does_not_allow_in_the_package(ctx, parent):
    vctx = with_core(ctx, lambda h: h.update(kinds=[k for k in h["kinds"] if k != "skill"]))
    got = v0(with_skill(parent, COMMON), decl("skill", "ops-notify"), vctx)
    assert not got["passed"]
    assert "component kinds ['skill'] are not allowed by K" in got["detail"]


def test_v0_refuses_a_kind_k_does_not_allow_in_the_diff(ctx, parent):
    """Removing every prompt leaves a package of allowed kinds, but the change is of a kind K does not allow."""
    vctx = with_core(ctx, lambda h: h.update(kinds=[k for k in h["kinds"] if k != "prompt"]))
    files = rewrite(parent.files, lambda m: m.pop("prompt"), drop=("prompt/system.md", "prompt/finish.md"))
    got = v0(files, decl("prompt", None), vctx)
    assert not got["passed"]
    assert got["detail"] == "K does not allow the kinds ['prompt']"


def test_v0_refuses_a_hook_site_k_does_not_allow(ctx, parent):
    vctx = with_core(ctx, lambda h: h.update(sites=[s for s in h["sites"] if s != "after_turn"]))
    got = v0(with_hook(parent, HOOK_CODE, site="after_turn"), decl("hook", "after_turn"), vctx)
    assert not got["passed"]
    assert "site 'after_turn'" in got["detail"]


def test_v0_refuses_a_hook_change_at_a_site_k_does_not_allow(ctx, parent):
    """The seed's on_finish hook removed under a K without on_finish: the package is fine, the change is not."""
    vctx = with_core(ctx, lambda h: h.update(sites=[s for s in h["sites"] if s != "on_finish"]))
    files = rewrite(parent.files, lambda m: m.update(hooks=[h for h in m["hooks"] if h["site"] != "on_finish"]))
    got = v0(files, decl("hook", "on_finish"), vctx)
    assert not got["passed"]
    assert got["detail"] == "K does not allow the hook sites ['on_finish']"


# ---------------------------------------------------------------------------- V0: the memory snapshot
SNAPSHOT = json.dumps({"memory_id": "m" * 16, "when_to_use": "a request asks for #ops", "content": "post it"}) + "\n"


def test_v0_refuses_the_memory_snapshot_written_by_a_non_memory_candidate(ctx, parent):
    files = {**snapshot_candidate(parent, SNAPSHOT)}
    files = rewrite(files, lambda m: m["tools"].append(dict(TOOL_ENTRY)), {TOOL_PATH: TOOL_CODE})
    got = v0(files, tool_decl(), ctx)
    assert not got["passed"]
    assert f"{MEMORY_PATH} is written by the memory" in got["detail"]


def test_v0_passes_the_memory_snapshot_of_the_memory_candidate(ctx, parent):
    files = snapshot_candidate(parent, SNAPSHOT)
    got = v0(files, decl("memory", "snapshot", predicted=(), fixed="snapshot:abc"), ctx)
    assert got == {"passed": True, "detail": ""}
    assert run_ladder(files, decl("memory", "snapshot", predicted=(), fixed="snapshot:abc"), ctx).steps.keys() == {"V0"}


def test_v0_passes_the_snapshot_of_a_library_over_ks_memory_bytes_once_it_is_fitted(ctx, parent):
    """The fixed candidate of a library whose whole rows exceed the preset's max_memory_bytes (finance-v2's, 550 KB
    against 256 KiB) fails V0 as it was written then; fitted (memory/snapshot.py fit_snapshot) it passes, with the
    credited entry in it."""
    cap = Limits.from_core(ctx.core).max_memory_bytes
    assert cap == AUTOMATIONBENCH_V1["harness"]["limits"]["max_memory_bytes"]
    rows = [{"memory_id": f"{i:016x}", "memory_type": "procedural", "author": "research/reme:success",
             "when_to_use": f"When a request asks for #ops, case {i}", "content": f"Post it, case {i}. " + "x" * 1600,
             "metadata": {"ol_line": "rsi", "ol_split": "train", "ol_source_line": "rsi", "ol_sources": "[]",
                          "ol_trace_refs": json.dumps([f"run/t{i}/r0/first#0:L1"] * 3),
                          "ol_stats": json.dumps({"fixes": int(i == 299), "n_cited": int(i == 299)})}}
            for i in range(300)]
    whole = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    d = decl("memory", "snapshot", predicted=(), fixed="snapshot:abc")
    got = v0(snapshot_candidate(parent, whole), d, ctx)
    assert not got["passed"] and f"bytes in {MEMORY_PATH}: K allows {cap}" in got["detail"]
    text, fit = fit_snapshot(whole, cap)
    files = snapshot_candidate(parent, text)
    assert v0(files, d, ctx) == {"passed": True, "detail": ""}
    assert 0 < fit["kept"] < 300 and f"{299:016x}" in text and len(text.encode("utf-8")) <= cap


def test_v0_on_a_parent_with_a_snapshot_refuses_a_change_of_it_and_not_its_presence(ctx, parent):
    limits = Limits.from_core(ctx.core)
    held = build(ctx.objects, snapshot_candidate(parent, SNAPSHOT), limits=limits, seed=ctx.seed)
    vctx = dataclasses.replace(ctx, parent=held)
    kept = with_tool(held)
    assert v0(kept, tool_decl(), vctx) == {"passed": True, "detail": ""}
    changed = {**kept, MEMORY_PATH: SNAPSHOT + SNAPSHOT.replace("m" * 16, "n" * 16)}
    assert components(held, changed) == [{"kind": "tool", "name": TOOL, "change": "added"},
                                         {"kind": "memory", "name": "snapshot", "change": "modified"}]
    got = v0(changed, tool_decl(), vctx)
    assert not got["passed"]
    assert f"{MEMORY_PATH} is written by the memory" in got["detail"]


# ---------------------------------------------------------------------------- V0: literals
def test_v0_refuses_a_forbidden_literal_on_a_changed_line(ctx, parent):
    vctx = dataclasses.replace(ctx, forbidden={"ACME-7781"})
    code = TOOL_CODE.replace('SLACK = ', 'ACCOUNT = "ACME-7781"\nSLACK = ')
    got = v0(with_tool(parent, code), tool_decl(), vctx)
    assert not got["passed"]
    assert f"{TOOL_PATH}:3: 'ACME-7781'" in got["detail"]
    assert "task-specific values" in got["detail"]


def test_v0_searches_only_the_lines_changed_against_the_seed(ctx, parent):
    """`plan_changes` stands on the seed's own lines: forbidden or not, it is found only where a candidate writes it."""
    assert "plan_changes" in parent.files["manifest.json"] and "plan_changes" in parent.files["tools/plan_changes.py"]
    vctx = dataclasses.replace(ctx, forbidden={"plan_changes"})
    assert v0(with_tool(parent), tool_decl(), vctx) == {"passed": True, "detail": ""}
    code = TOOL_CODE + "\n\nAFTER = 'plan_changes'\n"
    got = v0(with_tool(parent, code), tool_decl(), vctx)
    assert not got["passed"]
    assert f"{TOOL_PATH}:" in got["detail"] and "'plan_changes'" in got["detail"]
    assert "tools/plan_changes.py" not in got["detail"]


# ---------------------------------------------------------------------------- V0: imports and calls
@pytest.mark.parametrize("line,what", [
    ("import subprocess", "imports subprocess"),
    ("from socket import create_connection", "imports socket"),
    ("import urllib.request", "imports urllib.request"),
    ("import pickle as p", "imports pickle"),
    ("X = eval('1 + 1')", "calls eval"),
    ("import os\nos.system('true')", "calls os.system"),
    ("import os\nos.popen('true')", "calls os.popen"),
    ("exec('x = 1')", "calls exec"),
    ("M = __import__('socket')", "calls __import__"),
])
def test_v0_refuses_a_newly_added_forbidden_import_or_call(ctx, parent, line, what):
    got = v0(with_tool(parent, line + "\n" + TOOL_CODE), tool_decl(), ctx)
    assert not got["passed"]
    assert f"{TOOL_PATH}:" in got["detail"] and what in got["detail"]
    assert "reaches the world through ctx.tool" in got["detail"]


def test_v0_refuses_a_module_that_does_not_parse(ctx, parent):
    got = v0(with_tool(parent, TOOL_CODE + "\ndef broken(:\n"), tool_decl(), ctx)
    assert not got["passed"]
    assert "does not parse" in got["detail"]


def test_v0_an_import_the_parents_file_already_has_is_not_new(ctx, parent):
    """The seed's egress tool imports http.client and urllib: editing that file is not adding them; a new import in it
    is new, and so is the same import in another file."""
    path = "agent/http_fetch.py"
    assert "import urllib.request" in parent.files[path]
    edited = rewrite(parent.files, add={path: parent.files[path] + "\n# the candidate's comment\n"})
    assert v0(edited, decl("agent", None), ctx) == {"passed": True, "detail": ""}
    more = rewrite(parent.files, add={path: parent.files[path] + "\nimport socket\n"})
    got = v0(more, decl("agent", None), ctx)
    assert not got["passed"]
    assert "imports socket" in got["detail"] and "urllib" not in got["detail"] and "http.client" not in got["detail"]
    other = v0(with_tool(parent, "import urllib.request\n" + TOOL_CODE), tool_decl(), ctx)
    assert not other["passed"] and "imports urllib.request" in other["detail"]


@pytest.mark.parametrize("helper", ["execute", "evaluate_stage", "compiled_rules"])
def test_v0_a_function_named_like_a_forbidden_call_is_not_one(ctx, parent, helper):
    code = TOOL_CODE.replace("def run(ctx, args):", f"def {helper}(x):\n    return x\n\n\ndef run(ctx, args):\n"
                                                  f"    args = {helper}(args)")
    assert v0(with_tool(parent, code), tool_decl(), ctx) == {"passed": True, "detail": ""}


@pytest.mark.parametrize("line", ["from os import system\nsystem('true')", "import os as o\no.system('true')"])
def test_v0_os_system_by_another_name_is_still_a_call_of_it(ctx, parent, line):
    got = v0(with_tool(parent, line + "\n" + TOOL_CODE), tool_decl(), ctx)
    assert not got["passed"]
    assert "os.system" in got["detail"]


# ---------------------------------------------------------------------------- V0: the declaration
def test_v0_refuses_a_candidate_that_changes_nothing(ctx, parent):
    got = v0(dict(parent.files), tool_decl(), ctx)
    assert got == {"passed": False, "detail": "the candidate changes nothing"}


def test_v0_refuses_a_declared_kind_the_files_do_not_change(ctx, parent):
    got = v0(with_tool(parent), decl("hook", "on_finish"), ctx)
    assert not got["passed"]
    assert "declared kind hook but the files change ['tool']" in got["detail"]


@pytest.mark.parametrize("kind,site,build_files,why", [
    ("tool", "plan_changes", lambda p: with_tool(p), "a tool candidate's site names the tool it adds or changes"),
    ("tool", "nonexistent", lambda p: with_tool(p), "a tool candidate's site names the tool it adds or changes"),
    ("tool", "plan_changes", lambda p: rewrite(p.files, lambda m: m.update(tools=[]), drop=("tools/plan_changes.py",)),
     "a tool candidate's site names the tool it adds or changes"),
    ("hook", "before_tool", lambda p: with_hook(p, HOOK_CODE), "a hook candidate's site is the site of the hook"),
    ("skill", "other-skill", lambda p: with_skill(p, COMMON), "a skill candidate's site names the skill"),
    ("prompt", "finish", lambda p: appended(p, "prompt/system.md"),
     "a prompt candidate's site is system or finish"),
], ids=["tool-unchanged", "tool-unknown", "tool-deleted", "hook", "skill", "prompt"])
def test_v0_refuses_a_site_that_is_not_the_changed_component(ctx, parent, kind, site, build_files, why):
    got = v0(build_files(parent), decl(kind, site), ctx)
    assert not got["passed"]
    assert why in got["detail"]


@pytest.mark.parametrize("kind,site,build_files", [
    ("tool", TOOL, lambda p: with_tool(p)),
    ("tool", "plan_changes", lambda p: rewrite(p.files, add={"tools/plan_changes.py": p.files["tools/plan_changes.py"]
                                                              + "\n# clearer\n"})),
    ("hook", "on_finish", lambda p: with_hook(p, HOOK_CODE)),
    ("hook", "on_finish", lambda p: rewrite(p.files, lambda m: m.update(
        hooks=[h for h in m["hooks"] if h["site"] != "on_finish"]))),
    ("skill", "ops-notify", lambda p: with_skill(p, COMMON)),
    ("prompt", "system", lambda p: appended(p, "prompt/system.md")),
    ("prompt", None, lambda p: rewrite(p.files, add={"prompt/finish.md": p.files["prompt/finish.md"] + "\nMore.\n"})),
    ("config", None, lambda p: rewrite(p.files, lambda m: m["config"].update(max_turns=30))),
], ids=["tool-added", "tool-modified", "hook-added", "hook-removed", "skill", "prompt-system", "prompt-no-site",
        "config"])
def test_v0_passes_a_site_that_is_the_changed_component(ctx, parent, kind, site, build_files):
    assert v0(build_files(parent), decl(kind, site), ctx) == {"passed": True, "detail": ""}


def test_v0_refuses_extra_kinds_besides_the_declared_one_and_test(ctx, parent):
    both = rewrite(with_tool(parent), lambda m: m["hooks"].append(dict(HOOK_ENTRY)),
                   {"middlewares/remind_ops.py": HOOK_CODE})
    got = v0(both, tool_decl(), ctx)
    assert not got["passed"]
    assert "the files also change ['hook']" in got["detail"]
    test = "def test_phrase(ctx=None):\n    assert True\n"
    tested = with_tool(parent, add={"tests/test_notify.py": test},
                       change=lambda m: m["tests"].append("tests/test_notify.py"))
    assert v0(tested, tool_decl(), ctx) == {"passed": True, "detail": ""}
    assert v0(both, tool_decl(fixed="promotion:x"), ctx) == {"passed": True, "detail": ""}   # the fixed code's own


@pytest.mark.parametrize("predicted,why", [
    (("rsi.dev_update_stage",), "predicted_affected names ['rsi.dev_update_stage'], which are not train tasks"),
    ((), "predicted_affected names no train task"),
], ids=["outside-train", "empty"])
def test_v0_refuses_predicted_affected_outside_train_or_empty(ctx, parent, predicted, why):
    got = v0(with_tool(parent), tool_decl(predicted=predicted), ctx)
    assert got == {"passed": False, "detail": why}
    assert v0(with_tool(parent), tool_decl(predicted=predicted, fixed="removal:x"), ctx)["passed"]


def test_v0_a_removal_must_delete_a_tool_a_hook_or_a_skill(ctx, parent):
    gone = without(parent, {"kind": "tool", "name": "plan_changes"})
    removal = decl("removal", "plan_changes", predicted=(), fixed="removal:tool:plan_changes")
    assert v0(gone, removal, ctx) == {"passed": True, "detail": ""}
    assert run_ladder(gone, removal, ctx).steps.keys() == {"V0"}
    nothing_deleted = {"a prompt edit": appended(parent, "prompt/system.md"), "an added tool": with_tool(parent),
                       "an edited tool": appended(parent, "tools/plan_changes.py", "\n# clearer\n")}
    for what, files in nothing_deleted.items():
        got = v0(files, removal, ctx)
        assert got == {"passed": False, "detail": "a removal deletes a tool, a hook or a skill"}, what


# ---------------------------------------------------------------------------- components
@pytest.mark.parametrize("edit,expected", [
    (lambda p: with_tool(p), [{"kind": "tool", "name": TOOL, "change": "added"}]),
    (lambda p: rewrite(p.files, add={"tools/plan_changes.py": p.files["tools/plan_changes.py"] + "\n# x\n"}),
     [{"kind": "tool", "name": "plan_changes", "change": "modified"}]),
    (lambda p: rewrite(p.files, lambda m: m["tools"][0].update(description="Plan first.")),
     [{"kind": "tool", "name": "plan_changes", "change": "modified"}]),
    (lambda p: rewrite(p.files, lambda m: m.update(hooks=[h for h in m["hooks"] if h["name"] != "verify"])),
     [{"kind": "hook", "name": "verify", "change": "deleted", "site": "on_finish"}]),
    (lambda p: rewrite(p.files, add={"middlewares/write_guard.py": p.files["middlewares/write_guard.py"] + "\n# x\n"}),
     [{"kind": "hook", "name": n, "change": "modified", "site": s} for n, s in
      sorted([("anchor", "before_model"), ("ledger", "after_tool"), ("triage", "before_tool"),
              ("verify", "on_finish")])]),
    (lambda p: with_skill(p, COMMON), [{"kind": "skill", "name": "ops-notify", "change": "added"}]),
    (lambda p: rewrite(p.files, add={"prompt/finish.md": "Check the request.\n"}),
     [{"kind": "prompt", "name": "finish", "change": "modified"}]),
    (lambda p: rewrite(p.files, lambda m: m["prompt"].pop("finish"), drop=("prompt/finish.md",)),
     [{"kind": "prompt", "name": "finish", "change": "deleted"}]),
    (lambda p: rewrite(p.files, add={"agent/stirrup_agent.py": p.files["agent/stirrup_agent.py"] + "\n# x\n"}),
     [{"kind": "agent", "name": "agent", "change": "modified"}]),
    (lambda p: rewrite(p.files, lambda m: m["config"].update(shown_k=3)),
     [{"kind": "config", "name": "shown_k", "change": "modified"}]),
    (lambda p: rewrite(p.files, add={"vendor/stirrup/NOTES.txt": "x\n"}),
     [{"kind": "vendor", "name": "stirrup", "change": "modified"}]),
    (lambda p: snapshot_candidate(p, SNAPSHOT), [{"kind": "memory", "name": "snapshot", "change": "modified"}]),
    (lambda p: rewrite(p.files, lambda m: m["model"].update(effort="low")),
     [{"kind": "model", "name": "model", "change": "modified"}]),
    (lambda p: rewrite(p.files, add={"tests/test_write_guard.py": p.files["tests/test_write_guard.py"] + "\n# x\n"}),
     [{"kind": "test", "name": "tests", "change": "modified"}]),
], ids=["tool-added", "tool-file", "tool-entry", "hook-deleted", "hook-file", "skill", "prompt", "prompt-deleted",
        "agent", "config", "vendor", "memory", "model", "test"])
def test_components_name_each_changed_component(parent, edit, expected):
    assert components(parent, edit(parent)) == expected


def test_components_do_not_report_unchanged_entries_written_raw_or_normalised(parent):
    """The parent's manifest file holds its entries raw (no hooks' or tools' `enabled`); its parsed manifest holds them
    normalised. Neither form of an unchanged entry is a change."""
    raw = json.loads(parent.files["manifest.json"])
    assert all("enabled" not in h for h in raw["hooks"]) and all("enabled" not in t for t in raw["tools"])
    assert components(parent, dict(parent.files)) == []
    normalised = rewrite(parent.files, lambda m: m.update(parent.manifest.to_json()))
    assert normalised["manifest.json"] != parent.files["manifest.json"]
    assert components(parent, normalised) == []
    reordered = {**parent.files, "manifest.json": json.dumps(raw, sort_keys=True) + "\n"}
    assert components(parent, reordered) == []
    added = rewrite(normalised, lambda m: m["tools"].append(dict(TOOL_ENTRY)), {TOOL_PATH: TOOL_CODE})
    assert components(parent, added) == [{"kind": "tool", "name": TOOL, "change": "added"}]


@pytest.mark.parametrize("edit", [lambda m: m["agent"].update(roles=["aux", "act"]),
                                  lambda m: m["memory"].update(snapshot=None)], ids=["agent-roles", "memory-null"])
def test_components_an_entry_parse_manifest_normalises_is_unchanged(ctx, parent, edit):
    written = rewrite(with_tool(parent), edit)
    assert components(parent, written) == [{"kind": "tool", "name": TOOL, "change": "added"}]
    assert v0(written, tool_decl(), ctx) == {"passed": True, "detail": ""}


# ---------------------------------------------------------------------------- read_declaration
RAW = {"dir": "notify", "cause_id": "rsi-test/r0:missed_action", "kind": "tool", "site": TOOL,
       "predicted_affected": list(FAILING), "trigger": "a request asks for #ops", "scripts": {FAILING[0]: "x"}}


@pytest.mark.parametrize("raw,why", [
    ([RAW], "a declaration is an object"),
    ({**RAW, "trigger": ""}, "it lacks ['trigger']"),
    ({k: v for k, v in RAW.items() if k != "cause_id"}, "it lacks ['cause_id']"),
    ({**RAW, "dir": "other"}, "dir 'other' is not a candidate directory you wrote"),
    ({**RAW, "kind": "prompt"}, "kind 'prompt' is not one this round assigned"),
    ({**RAW, "predicted_affected": FAILING[0]}, "predicted_affected is a list of train task ids"),
    ({**RAW, "predicted_affected": [1]}, "predicted_affected is a list of train task ids"),
    ({**RAW, "scripts": {FAILING[0]: 3}}, "scripts maps a train task id to a toolcheck script"),
    ({**RAW, "differs_from": [{"how": "x"}]}, "differs_from is a list of {ref, how}"),
    ({**RAW, "dir": "memory-snapshot"}, "are the fixed code's candidates"),
    ({**RAW, "dir": "promoted-1a2b3c4d"}, "are the fixed code's candidates"),
    ({**RAW, "dir": "remove-tool-x"}, "are the fixed code's candidates"),
])
def test_read_declaration_refuses_what_finish_must_not_take(raw, why):
    got, error = read_declaration(raw, dirs=["notify"], kinds_allowed=["tool", "hook"])
    assert got is None and why in error


def test_read_declaration_reads_finish_arguments(ctx):
    got, error = read_declaration({**RAW, "site": "", "trigger": "t" * 900, "differs_from": [{"ref": "a", "how": 1}]},
                                  dirs=["notify"], kinds_allowed=["tool"])
    assert error == ""
    assert got.site is None and got.predicted_affected == FAILING and len(got.trigger) == 500
    assert got.differs_from == ({"ref": "a", "how": "1"},) and got.scripts == {FAILING[0]: "x"}
    assert Declaration.from_json(ctx.objects.get(ctx.objects.put(got))) == got


# ---------------------------------------------------------------------------- V1
def test_v1_passes_the_tool_candidate(ctx, parent):
    assert unit_check(with_tool(parent), tool_decl(), ctx) == {"passed": True, "detail": ""}


def test_v1_catches_a_tool_that_raises_at_import(ctx, parent):
    got = unit_check(with_tool(parent, "raise RuntimeError('broken at import')\n" + TOOL_CODE), tool_decl(), ctx)
    assert not got["passed"]
    assert got["detail"].startswith("probe:") and "broken at import" in got["detail"]


def test_v1_catches_an_agent_that_crashes_on_every_task(ctx, parent):
    path = "agent/stirrup_agent.py"
    crash = parent.files[path] + "\n\ndef run(ctx, task):\n    raise RuntimeError('the agent crashes')\n"
    got = unit_check(rewrite(parent.files, add={path: crash}), decl("agent", None), ctx)
    assert not got["passed"]
    assert got["detail"].startswith("smoke:") and "the agent crashes" in got["detail"]


def test_v1_catches_a_package_test_that_fails(ctx, parent):
    test = "def test_phrase_required(ctx=None):\n    assert 1 == 2, 'the candidate test fails'\n"
    files = with_tool(parent, add={"tests/test_notify.py": test},
                      change=lambda m: m["tests"].append("tests/test_notify.py"))
    got = unit_check(files, tool_decl(), ctx)
    assert not got["passed"]
    assert got["detail"].startswith("test: test_notify.py:test_phrase_required: AssertionError")


def test_v1_runs_the_package_tests_on_a_fresh_train_world(ctx, parent):
    """The fixture is a new episode of a train task: a package test's write reaches that world's API."""
    test = ("import json\n\n\ndef test_post(ctx):\n"
            f"    res = ctx.tool('api_fetch', {{'method': 'POST', 'url': '{SLACK}', "
            "'body': json.dumps({'channel': 'C1', 'text': 'a test of the world'})})\n"
            "    assert not res.is_error and '\"ok\": true' in res.output, res.output[:200]\n")
    files = with_tool(parent, add={"tests/test_world.py": test},
                      change=lambda m: m["tests"].append("tests/test_world.py"))
    assert unit_check(files, tool_decl(), ctx) == {"passed": True, "detail": ""}


# ---------------------------------------------------------------------------- V2a
def direct_script(task_id: str) -> str:
    """Does the task with the base tool alone: the world passes, the candidate's tool is never called."""
    return ("import json\n\ndef check(ctx, tools):\n"
            f"    ctx.tool('api_fetch', {{'method': 'PATCH', 'url': '{HUBSPOT}', "
            f"'body': json.dumps({{'id': '{CONTACTS[task_id]}', 'stage': 'opportunity'}})}})\n"
            f"    ctx.tool('api_fetch', {{'method': 'POST', 'url': '{SLACK}', "
            f"'body': json.dumps({{'channel': 'C1', 'text': 'stage updated'}})}})\n")


def test_v2a_passes_scripts_that_do_the_task_with_the_tool(ctx, parent):
    got = toolcheck(with_tool(parent), tool_decl(), ctx)
    assert got["passed"]
    assert got["tasks"] == {t: {"passed": True, "called_tool": True, "failed_kinds": []} for t in FAILING}


def test_v2a_refuses_a_script_that_never_calls_the_tool(ctx, parent):
    got = toolcheck(with_tool(parent), tool_decl({t: direct_script(t) for t in FAILING}), ctx)
    assert not got["passed"]
    assert got["tasks"] == {t: {"passed": False, "called_tool": False, "failed_kinds": []} for t in FAILING}
    assert f"the script never called {TOOL}" in got["detail"]


def test_v2a_passes_need_toolcheck_min_pass(ctx, parent):
    scripts = {FAILING[0]: script_for(FAILING[0]), FAILING[1]: direct_script(FAILING[1])}
    got = toolcheck(with_tool(parent), tool_decl(scripts), ctx)
    assert not got["passed"]
    assert [t for t, r in got["tasks"].items() if r["passed"]] == [FAILING[0]]
    assert got["detail"].startswith("1 of 2 claimed train tasks pass with the tool (at least 2)")
    assert toolcheck(with_tool(parent), tool_decl(scripts), with_settings(ctx, toolcheck_min_pass=1))["passed"]
    assert not toolcheck(with_tool(parent), tool_decl(), with_settings(ctx, toolcheck_min_pass=3))["passed"]


def test_v2a_counts_only_claimed_train_tasks_the_current_w_fails(ctx, parent):
    """A script per claimed failing task (DESIGN §6.6): one for a task the current W passes, or one the declaration
    does not claim, is not a pass however the world ends."""
    notify = "rsi.notify_once"                     # passed by the current W; a script that posts would pass it
    scripts = {**{t: script_for(t) for t in FAILING}, notify: script_for(notify, "single update")}
    got = toolcheck(with_tool(parent), tool_decl(scripts, predicted=(*FAILING, notify)), ctx)
    assert got["passed"] and not got["tasks"][notify]["passed"]
    assert "not a task the declaration claims that the current W fails" in got["tasks"][notify]["error"]
    unclaimed = toolcheck(with_tool(parent), tool_decl(predicted=FAILING[:1]), with_settings(ctx, toolcheck_min_pass=2))
    assert not unclaimed["passed"] and [t for t, r in unclaimed["tasks"].items() if r["passed"]] == [FAILING[0]]


def test_v2a_a_world_the_grader_fails_hands_back_the_assertion_types_only(ctx, parent):
    got = toolcheck(with_tool(parent), tool_decl({t: script_for(t, "all done") for t in FAILING}), ctx)
    assert not got["passed"]
    assert got["tasks"] == {t: {"passed": False, "called_tool": True, "failed_kinds": ["slack_message_contains"]}
                            for t in FAILING}
    assert "stage updated" not in json.dumps(got)          # the grader's parameter never goes back to R


def test_v2a_a_script_error_or_a_task_outside_train_is_not_a_pass(ctx, parent):
    scripts = {FAILING[0]: "def check(ctx, tools):\n    raise ValueError('the script gives up')\n",
               FAILING[1]: "x = 1\n", "rsi.dev_update_stage": script_for(FAILING[0])}
    got = toolcheck(with_tool(parent), tool_decl(scripts), with_settings(ctx, toolcheck_min_pass=1))
    assert not got["passed"]
    assert not any(r["passed"] for r in got["tasks"].values())
    assert "the script gives up" in got["tasks"][FAILING[0]]["error"]
    assert "check(ctx, tools)" in got["tasks"][FAILING[1]]["error"]
    assert got["tasks"]["rsi.dev_update_stage"] == {"passed": False, "error": "not a train task"}
    none = toolcheck(with_tool(parent), tool_decl({}), ctx)
    assert not none["passed"] and "declaration.scripts" in none["detail"]


# ---------------------------------------------------------------------------- V2b
def site_hook(condition: str, answer: str) -> str:
    return f"def remind(ctx, event):\n    if {condition}:\n        return {answer}\n    return None\n"


# per site: a hook that acts on what the rebuilt event of that site carries; on the train attempts it acts on both
# notify tasks and on exactly one passing attempt
SITE_HOOKS = {
    "before_run": ('"#ops" in event["task"] and "finish tool" in event["system"] and event["messages"] == []',
                   '{"system": event["system"] + " Do every part of the request."}'),
    "before_model": ('event["role"] == "act" and event["turn"] >= 1 and "api_fetch" in [t["name"] for t in '
                     'event["tools"]] and any("opportunity stage" in str(m.get("content")) for m in event["messages"] '
                     'if m.get("role") == "user")', '{"messages": event["messages"]}'),
    "before_tool": ('event["name"] == "api_fetch" and "hubapi" in event["args"].get("url", "") and event["call_id"]',
                    '{"block": {"reason": "read the request again first"}}'),
    "after_tool": ('event["name"] == "api_fetch" and \'"opportunity"\' in event["output"] and '
                   'event["is_error"] is False',
                   '{"feedback": "the record is updated; now the rest of the request"}'),
    "after_turn": ('any(r["name"] == "api_fetch" and "opportunity" in r["output"] for r in event["tool_results"])',
                   '{"message": "the record is updated; now the rest of the request"}'),
    # the seed's verify pushes back on every attempt's first finish and answers first: the candidate, after it in the
    # manifest, sees the second, with one pushback before it (as live)
    "on_finish": ('event["reason"] == "finish" and "record is updated" in event["final_text"] and "posted" not in '
                  'event["final_text"] and event["pushbacks"] == 1', '{"pushback": "Keep working on the task"}'),
}


@pytest.mark.parametrize("site", list(SITE_HOOKS))
def test_v2b_replays_each_sites_events_from_the_transcripts(ctx, parent, site):
    files = with_hook(parent, site_hook(*SITE_HOOKS[site]), site=site)
    verdict = run_ladder(files, decl("hook", site), ctx)
    assert list(verdict.steps) == ["V0", "V1", "V2b"]
    got = verdict.steps["V2b"]
    assert got["passed"], got["detail"]
    assert got["fired"] == list(FAILING)
    assert got["false_trigger_rate"] == 0.3333


def test_v2b_counts_triggers_on_the_claimed_failing_tasks_and_the_rate_on_passing_ones(ctx, parent):
    files = with_hook(parent, HOOK_CODE)
    got = replay_check(files, decl("hook", "on_finish"), ctx)
    assert got["passed"]
    assert got["fired"] == list(FAILING)
    assert got["false_trigger_rate"] == 0.6667          # update_stage and policy_hold say nothing was posted
    assert "acts on 2 of 2 claimed failing tasks (at least 2)" in got["detail"]
    assert "on 2 of 3 passing attempts (rate 0.6667)" in got["detail"]


def test_v2b_a_hook_that_never_acts_fails(ctx, parent):
    got = replay_check(with_hook(parent, "def remind(ctx, event):\n    return None\n"), decl("hook", "on_finish"), ctx)
    assert not got["passed"]
    assert got["fired"] == [] and got["false_trigger_rate"] == 0.0


def test_v2b_needs_replay_min_triggers_claimed_failing_tasks(ctx, parent):
    files = with_hook(parent, HOOK_CODE)
    one = decl("hook", "on_finish", predicted=FAILING[:1])
    got = replay_check(files, one, ctx)
    assert not got["passed"] and got["fired"] == [FAILING[0]]
    assert replay_check(files, one, with_settings(ctx, replay_min_triggers=1))["passed"]
    passing_only = replay_check(files, decl("hook", "on_finish", predicted=PASSING), ctx)
    assert not passing_only["passed"]
    assert "none of the claimed tasks has a failing train attempt" in passing_only["detail"]


def test_v2b_a_hook_answering_outside_its_sites_protocol_fails(ctx, parent):
    """It pushes back on both failing tasks, and answers notify_once's finish with a key on_finish does not take."""
    code = site_hook('"posted" not in event["final_text"]', '{"pushback": "Keep working on the task"}').replace(
        "    return None\n", "    return {\"message\": \"done\"}\n")
    got = replay_check(with_hook(parent, code), decl("hook", "on_finish"), ctx)
    assert got["fired"] == list(FAILING)
    assert not got["passed"]
    assert "the hook failed" in got["detail"] and "rsi.notify_once" in got["detail"]


def test_v2b_replays_only_an_enabled_changed_hook_at_the_declared_site(ctx, parent):
    elsewhere = replay_check(with_hook(parent, HOOK_CODE, site="after_turn"), decl("hook", "on_finish"), ctx)
    assert elsewhere == {"passed": False, "detail": "no hook of the candidate changed at on_finish"}
    disabled = replay_check(with_hook(parent, HOOK_CODE, enabled=False), decl("hook", "on_finish"), ctx)
    assert not disabled["passed"] and disabled["detail"].startswith("no hook of the candidate changed")


def test_site_events_rebuild_a_train_attempt(ctx):
    events = site_events(ctx.evidence.trace, ctx.failed_sessions[FAILING[0]])
    assert [e["task"] for e in events["before_run"]] == [ctx.train_texts[FAILING[0]]]
    acts = [e["turn"] for e in events["before_model"]]
    assert acts == list(range(1, len(acts) + 1)) and len(acts) >= 3
    assert [e["turn"] for e in events["after_turn"]] == acts
    assert [e["name"] for e in events["before_tool"]][:2] == ["plan_changes", "api_fetch"]
    assert {e["call_id"] for e in events["after_tool"]} <= {e["call_id"] for e in events["before_tool"]}
    assert [e["pushbacks"] for e in events["on_finish"]] == list(range(len(events["on_finish"])))
    # as the seed passes them: reason "finish" and the finish's own reason as final_text; finish is no tool call
    finishes = [c for c in ctx.evidence.trace.calls(ctx.failed_sessions[FAILING[0]]) if c.get("role") == "act"
                for c in (c.get("response") or {}).get("tool_calls") or () if c.get("name") == "finish"]
    assert [(e["reason"], e["final_text"]) for e in events["on_finish"]] == \
        [("finish", f["args"]["reason"]) for f in finishes]
    assert "finish" not in {e["name"] for e in events["before_tool"]}
    assert all(set(r) == {"call_id", "name", "is_error", "output"} for e in events["after_turn"]
               for r in e["tool_results"])
    assert events["on_finish"][0]["acted"] == ["verify"]          # the seed's verify pushed the first one back


# ---------------------------------------------------------------------------- V2c
def test_v2c_passes_a_description_that_reaches_train_tasks_beyond_the_claimed(ctx, parent):
    verdict = run_ladder(with_skill(parent, COMMON), decl("skill", "ops-notify"), ctx)
    assert list(verdict.steps) == ["V0", "V2c"]
    got = verdict.steps["V2c"]
    assert got["passed"]
    assert got["reached"] == sorted(PASSING)


def test_v2c_fails_a_description_that_reaches_no_train_task(ctx, parent):
    files = with_skill(parent, "Bake sourdough bread with rye flour and a pinch of salt.", name="bread")
    got = reach_check(files, decl("skill", "bread"), ctx)
    assert not got["passed"] and got["reached"] == []


def test_v2c_the_claimed_tasks_do_not_count(ctx, parent):
    files = with_skill(parent, COMMON)
    got = reach_check(files, decl("skill", "ops-notify", predicted=FAILING + PASSING[:2]), ctx)
    assert not got["passed"] and got["reached"] == [PASSING[2]]
    assert reach_check(files, decl("skill", "ops-notify", predicted=FAILING + PASSING[:2]),
                       with_settings(ctx, reach_min_tasks=1))["passed"]


def test_v2c_needs_reach_min_score(ctx, parent):
    files = with_skill(parent, COMMON)
    vectors = lexical_embed(["ops notify: " + COMMON] + [ctx.train_texts[t] for t in PASSING])
    lowest = min(sum(a * b for a, b in zip(vectors[0], v)) for v in vectors[1:])
    assert lowest >= ctx.settings.reach_min_score
    got = reach_check(files, decl("skill", "ops-notify"), with_settings(ctx, reach_min_score=round(lowest, 4) + 0.01))
    assert len(got["reached"]) < len(PASSING)
    assert not reach_check(files, decl("skill", "ops-notify"), with_settings(ctx, reach_min_score=0.99))["passed"]


def test_v2c_uses_the_contexts_embedder_when_there_is_one(ctx, parent):
    """With the memory on, the round hands the sidecar's embedder; here one that maps every text to one vector."""
    seen = []

    def embed(texts, query=False):
        seen.append(list(texts))
        return [[1.0, 0.0] for _ in texts]
    files = with_skill(parent, "Bake sourdough bread.", name="bread")
    got = reach_check(files, decl("skill", "bread"), dataclasses.replace(ctx, embed=embed))
    assert got["passed"] and got["reached"] == sorted(PASSING)
    assert seen[0][0] == "bread: Bake sourdough bread." and len(seen[0]) == 1 + len(ctx.train_tasks)


def test_v2c_fails_a_skill_the_manifest_does_not_name(ctx, parent):
    got = reach_check(with_skill(parent, COMMON), decl("skill", "other"), ctx)
    assert got == {"passed": False, "detail": "no skill named 'other' in the candidate's manifest"}


# ---------------------------------------------------------------------------- run_ladder, ladder_for, Verdict
@pytest.mark.parametrize("kind,rungs", [
    ("tool", ("V0", "V1", "V2a")), ("hook", ("V0", "V1", "V2b")), ("skill", ("V0", "V2c")), ("prompt", ("V0",)),
    ("agent", ("V0", "V1")), ("vendor", ("V0", "V1")), ("config", ("V0", "V1")), ("memory", ("V0",)),
    ("removal", ("V0",)), ("unknown", ("V0",)),
])
def test_ladder_for_names_the_rungs_of_each_kind(kind, rungs):
    assert ladder_for(kind) == rungs


def test_run_ladder_climbs_every_rung_of_a_tool(ctx, parent):
    verdict = run_ladder(with_tool(parent), tool_decl(), ctx)
    assert list(verdict.steps) == ["V0", "V1", "V2a"]
    assert verdict.passed() and all(verdict.passed(r) for r in ("V0", "V1", "V2a"))
    assert verdict.dir == "cand"
    assert verdict.text().splitlines()[:2] == ["V0: passed", "V1: passed"]
    assert verdict.text().splitlines()[2].startswith("V2a: passed — 2 of 2 claimed train tasks pass")


def test_run_ladder_stops_at_the_first_failed_rung(ctx, parent):
    at_v0 = run_ladder(with_tool(parent, "import socket\n" + TOOL_CODE), tool_decl(), ctx)
    assert list(at_v0.steps) == ["V0"] and not at_v0.passed() and not at_v0.passed("V0")
    at_v1 = run_ladder(with_tool(parent, "raise RuntimeError('broken at import')\n" + TOOL_CODE), tool_decl(), ctx)
    assert list(at_v1.steps) == ["V0", "V1"]
    assert at_v1.passed("V0") and not at_v1.passed("V1") and not at_v1.passed("V2a") and not at_v1.passed()
    assert at_v1.text().startswith("V0: passed\nV1: FAILED — probe:")
    at_v2 = run_ladder(with_tool(parent), tool_decl({t: direct_script(t) for t in FAILING}), ctx)
    assert list(at_v2.steps) == ["V0", "V1", "V2a"] and at_v2.passed("V1") and not at_v2.passed()
    skill = run_ladder(with_skill(parent, "Bake bread.", name="bread"), decl("skill", "bread"), ctx)
    assert list(skill.steps) == ["V0", "V2c"] and not skill.passed()


def test_run_ladder_takes_the_rungs_it_is_given(ctx, parent):
    verdict = run_ladder(with_tool(parent), tool_decl(), ctx, rungs=("V0",))
    assert list(verdict.steps) == ["V0"] and verdict.passed()


def test_verdict_passed_and_text():
    v = Verdict(dir="d", steps={"V0": {"passed": True, "detail": ""}, "V1": {"passed": False, "detail": "probe: boom"}})
    assert v.text() == "V0: passed\nV1: FAILED — probe: boom"
    assert v.passed("V0") and not v.passed("V1") and not v.passed("V2a") and not v.passed()
    assert Verdict(dir="d", steps={"V0": {"passed": True}}).passed()
    assert not Verdict(dir="d").passed()
    assert Verdict(dir="d").text() == "nothing checked"
    assert Verdict.from_json(json.loads(json.dumps(v.to_json()))) == v
