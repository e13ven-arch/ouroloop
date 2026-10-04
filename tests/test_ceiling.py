"""v0.3 step 4, the ceiling experiment (docs/ROADMAP.md §3 v0.3 第 4 步; examples/automationbench/ceiling/README.md):
the hand-written agent package passes the draft checks an R candidate gets; the runner runs both arms on a frozen
task list under the runner shell and writes the per-arm table; the task list is read, never chosen; `--checks-only`
calls no model; the decision rule as written before any run.

The world is the rsi fixture and the model a scripted one that answers the ceiling agent's two aux requests and
otherwise acts — it posts the notification only after an evidence table was put in front of it. That is the fixture
deciding what the mechanism does, on purpose: these tests check the wiring and the bookkeeping, never a result.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import rsi

from ouroloop import bench
from ouroloop.adapters import automationbench as ab
from ouroloop.agent import harness_package as hp
from ouroloop.agent.worker import DEFAULT_IMAGE, ProcessWorker, docker_worker
from ouroloop.benchmark import Results
from ouroloop.chat import ChatTurn, ToolCall
from ouroloop.evolve import contracts as ct
from ouroloop.research.drafts import Drafts

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "automationbench"
CEILING = EXAMPLES / "ceiling" / "package"
WSTAR = EXAMPLES / "ceiling" / "wstar_package"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load("ceiling_run", EXAMPLES / "ceiling_run.py")
AGENT = _load("ceiling_agent_for_tests", CEILING / "agent" / "ceiling_agent.py")


def files_of(root: Path) -> dict:
    return RUNNER.read_package(root)


# ---------------------------------------------------------------------------- the package's draft checks
def v3_limits() -> hp.Limits:
    h = bench.PRESETS["automationbench_harness_v3"]["harness"]
    return hp.Limits(components=tuple(h["components"]), package_api=h["package_api"], max_files=h["max_files"],
                     max_package_bytes=h["max_package_bytes"], max_memory_chars=h["max_memory_chars"],
                     max_skill_chars=h["max_skill_chars"], max_skills=h["max_skills"])


def drafts_for(tmp_path, fake_upstream, worker) -> Drafts:
    """A Drafts as a round under the v3 preset builds it: K's package limits, the train state's literals, the base
    tool names reserved, a train episode as the fixture world, the given worker."""
    suite = rsi.suite(fake_upstream)
    literals = set()
    for t in rsi.TRAIN:
        literals |= hp.literals_of(suite.grading(t).initial_state)
    names = {s.name for s in suite.episode(rsi.TRAIN[0]).specs()}
    objects = ct.Objects(tmp_path / "objects")
    return Drafts(objects, parent_ref=hp.empty_package(objects), limits=v3_limits(), literals=literals,
                  worker=worker, allowed_literals=names, reserved_names=names,
                  fixture=lambda: suite.episode(rsi.TRAIN[0]))


def test_the_hand_package_passes_the_draft_checks_an_r_candidate_gets(fake_upstream, tmp_path):
    """Shape and K's kinds, the literal check, the isolated probe, the agent smoke and the package's own tests, in
    a worker: the ceiling package and the W* package (its v1 manifest as installed) both pass, the ceiling within
    K's file and byte caps, its agent declaring the aux role its evidence table and check use."""
    drafts = drafts_for(tmp_path, fake_upstream, ProcessWorker(for_tests=True))
    for root in (CEILING, WSTAR):
        files = files_of(root)
        errors, ref = drafts.check(files)
        assert errors == [] and ref, (root.name, errors)
    view = hp.load_package(drafts.objects, drafts.check(files_of(CEILING))[1])
    assert view.api_version == "harness_package.v2" and view.agent["entrypoint"] == "agent/ceiling_agent.py:run"
    assert view.roles == ("act", "aux") and view.memory
    assert [h["name"] for h in view.manifest["hooks"]] == ["finish_audit"]
    files = files_of(CEILING)
    assert len(files) <= v3_limits().max_files
    assert sum(len(t.encode("utf-8")) for t in files.values()) <= v3_limits().max_package_bytes
    # W*'s text components are carried over byte for byte: the ceiling is W* plus mechanisms, not W0 plus them.
    wstar = files_of(WSTAR)
    for path in ("memory.md", "middlewares/finish_audit.py", "tests/test_finish_audit.py"):
        assert files[path] == wstar[path]


def test_a_mechanism_cannot_switch_itself_off_an_undeclared_aux_role_fails_the_smoke(fake_upstream, tmp_path):
    """The evidence table and the check are aux calls; a manifest that drops `aux` is refused by the smoke rather than
    running as W* with the mechanisms silently off."""
    drafts = drafts_for(tmp_path, fake_upstream, ProcessWorker(for_tests=True))
    files = files_of(CEILING)
    manifest = json.loads(files["manifest.json"])
    manifest["agent"]["roles"] = ["act"]
    errors, ref = drafts.check({**files, "manifest.json": json.dumps(manifest)})
    assert ref is None and errors and errors[0].startswith("agent_smoke:") and "'aux'" in errors[0]


@pytest.fixture
def qualified():
    worker = docker_worker()
    if worker is None:
        pytest.skip(f"docker or the {DEFAULT_IMAGE} image is not available")
    return worker


def test_the_hand_package_passes_the_draft_checks_in_the_docker_worker(fake_upstream, tmp_path, qualified):
    drafts = drafts_for(tmp_path, fake_upstream, qualified)
    for root in (CEILING, WSTAR):
        errors, ref = drafts.check(files_of(root))
        assert errors == [] and ref, (root.name, errors)


# ---------------------------------------------------------------------------- the decision rule
def task(split, wstar, ceiling, persistent=None):
    return {"split": split, "persistent": persistent, "arms": {"wstar": list(wstar), "ceiling": list(ceiling)}}


def test_the_decision_rule_as_written_before_any_run():
    F, T = False, True
    still = {f"d{i}": task("dev", [F, F], [T, F] if i < 3 else [F, F], persistent=True) for i in range(5)}
    d = RUNNER.decide(still, 2)
    assert (d["numbers"]["R"], d["numbers"]["X"], d["numbers"]["needed"]) == (5, 3, 3) and d["verdict"] == "search"
    assert d["persistent"] == {"still_failing": 5, "fixed": 3, "broken": 0, "wstar_passes": 0}
    # fixes that also break tasks W* passes here are counted apart, and the ceiling must fix more than it breaks
    broken = {**still, "t1": task("train", [T, F], [F, F]), "t2": task("train", [T, T], [F, F]),
              "t3": task("train", [F, T], [F, F])}
    d = RUNNER.decide(broken, 2)
    assert (d["numbers"]["X"], d["numbers"]["B"], d["numbers"]["E"]) == (3, 3, 3) and d["verdict"] == "training"
    assert d["by_split"]["train"] == {"still_failing": 0, "fixed": 0, "broken": 3, "wstar_passes": 3}
    fewer = {**{k: v for k, v in still.items() if k in ("d0", "d3")}, "t1": task("train", [T, T], [T, T])}
    assert RUNNER.decide(fewer, 2)["verdict"] == "inconclusive"            # W* still fails only 2: < 4
    under = {**still, "d4": task("dev", [F, F], [F])}                       # one attempt missing: no verdict
    d = RUNNER.decide(under, 2)
    assert d["verdict"] == "incomplete" and d["unread"] == ["d4"]
    half = {**still, "d5": task("dev", [F, F], [F, F])}                   # exactly half is not more than half
    d = RUNNER.decide(half, 2)
    assert (d["numbers"]["R"], d["numbers"]["X"], d["numbers"]["needed"]) == (6, 3, 4) and d["verdict"] == "training"


# ---------------------------------------------------------------------------- the runner
def executed(messages) -> dict:
    """rsi._done over the calls that ran: a call answered with an error (one the agent stopped before execution
    included) did not happen."""
    kept = []
    for i, m in enumerate(messages):
        answer = messages[i + 1] if i + 1 < len(messages) and messages[i + 1].get("role") == "tool" else {}
        failed = {r.get("id") for r in answer.get("results") or () if r.get("is_error")}
        kept.append({**m, "tool_calls": [c for c in m.get("tool_calls") or () if c.id not in failed]})
    return rsi._done(kept)


class CeilingWorld(rsi.BusinessWorker):
    """The scripted actor: answers the evidence-table request with a table and the check with a JSON verdict;
    otherwise it updates the contact the request names and posts the notification only once an evidence table is in
    its history. Every call is counted, with the effort it was made at."""
    calls: list = []

    def __init__(self, effort=None, **kw):
        super().__init__(**kw)
        self.effort = effort

    def chat(self, system, messages, specs, effort=None):
        last = messages[-1] if messages else {}
        content = str(last.get("content") or "") if last.get("role") == "user" else ""
        usage = {"input_tokens": 100, "output_tokens": 20}
        if content.startswith(AGENT.EVIDENCE_PROMPT[:40]):
            CeilingWorld.calls.append(("evidence", effort))
            return ChatTurn("| 1 | post the phrase in #ops | the request | the request | VERIFIED |", [], "end",
                            usage=usage)
        if content.startswith(AGENT.CHECK_PROMPT[:30]):
            CeilingWorld.calls.append(("check", effort))
            return ChatTurn('{"verdict": "finish"}', [], "end", usage=usage)
        CeilingWorld.calls.append(("act", effort))
        request = next((m["content"] for m in messages if m.get("role") == "user"), "")
        want, done = rsi._needs(request), executed(messages)
        tabled = any("Evidence table, built before the first write" in str(m.get("content") or "")
                     for m in messages if m.get("role") == "user")
        if want["update"] and not done["updated"]:
            return ChatTurn("", [ToolCall("t1", "api_fetch", {
                "method": "PATCH", "url": rsi.HUBSPOT,
                "body": json.dumps({"id": want["contact"], "stage": "opportunity"})})], "tool_use", usage=usage)
        if want["notify"] and tabled and done["sent"] == 0:
            return ChatTurn("", [ToolCall("t2", "api_fetch", {
                "method": "POST", "url": rsi.SLACK,
                "body": json.dumps({"channel": "C1", "text": f"{want['phrase']} for the contact"})})],
                "tool_use", usage=usage)
        return ChatTurn("Done.", [], "end", usage=usage)


@pytest.fixture
def ceiling_world(fake_upstream, tmp_path, monkeypatch):
    """The v3 preset on the rsi fixture (no docker image: a ProcessWorker named for tests; the gate's least task
    count the fixture's three dev tasks), a frozen two-task list (one dev, one train), and the config the runner
    reads. -> (cfg, base, tasks path)."""
    preset = json.loads(json.dumps(bench.PRESETS["automationbench_harness_v3"]))
    preset["domain"] = rsi.DOMAIN
    preset["harness"].update(sandbox=None)
    preset["task_plan"]["gate"].update(min_groups=1, bootstrap_replicates=200, min_tasks=len(rsi.DEV))
    monkeypatch.setitem(bench.PRESETS, "ceiling_test", preset)
    monkeypatch.setattr(bench, "isolated_worker", lambda settings: ProcessWorker(for_tests=True) if settings else None)
    monkeypatch.setattr(CeilingWorld, "calls", [])
    root = tmp_path / "ws"
    manifest = rsi.splits(rsi.suite(fake_upstream))
    manifest.freeze(Results(root, rsi.DOMAIN).manifest_path())
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({
        "schema": RUNNER.SCHEMA, "source": {"split_hash": manifest.hash()},
        "dev": [{"task_id": "rsi.dev_notify_after_update", "persistent": True, "failure_kinds": ["missing_write"]}],
        "train": [{"task_id": "rsi.notify_after_update", "failure_kinds": ["missing_write"]}]}, indent=1))
    cfg = {"workspace": {"root": "ws"},
           "bench": {"domain": rsi.DOMAIN, "upstream_path": fake_upstream, "families": dict(rsi.FAMILIES),
                     "ratios": {"train": 0.5, "dev": 0.25, "test": 0.25}, "concurrency": 2},
           "llm": {"type": "test_ceiling:CeilingWorld", "effort": "high"},
           "backends": {"mock": {"type": "mock"}},
           "rsi": {"scope_id": "ceiling", "repeats": 1},
           "improvement": {"mode": "automatic", "preset": "ceiling_test", "max_candidates_per_round": 1},
           "pricing": {"input_per_1k": 0.001, "output_per_1k": 0.002, "max_input_tokens": 100000,
                       "max_output_tokens": 32768, "source": "test"},
           "freeze": {"budget": {"usd": 30.0}},
           "ceiling": {"tasks": str(tasks), "attempts": 2,
                       "packages": {"wstar": str(WSTAR), "ceiling": str(CEILING)},
                       "estimate": {"wstar": 0.65, "ceiling": 0.9}}}
    return cfg, tmp_path, tasks


def test_checks_only_runs_every_check_and_calls_no_model(ceiling_world, monkeypatch, capsys):
    """`--checks-only` through the script's main: the plans frozen, W0 bootstrapped, the startup checks and both
    draft checks pass; no model is called, no attempt recorded, and the budget root is not opened."""
    cfg, base, _tasks = ceiling_world
    monkeypatch.setattr(RUNNER.config_mod, "load", lambda path: cfg)
    assert RUNNER.main(["--config", str(base / "ceiling.toml"), "--checks-only"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert CeilingWorld.calls == []
    assert not Results(base / "ws", rsi.DOMAIN).path.exists()
    book = json.loads((base / "ws" / "rsi" / "budget" / "book.json").read_text()) \
        if (base / "ws" / "rsi" / "budget" / "book.json").exists() else {"scopes": {}}
    assert "ceiling" not in book["scopes"]
    assert any(line.startswith("result     checks passed; no model was called") for line in lines)
    assert any(line.startswith("arm        ceiling: harness_package.v2 agent=agent/ceiling_agent.py:run")
               for line in lines)
    assert any("8 attempts (4 per arm: 2 tasks x 2)" in line for line in lines)
    assert not any(line.startswith("NOTE") for line in lines)              # $30 holds 3 reservations of $0.17


def test_the_runner_runs_both_arms_on_the_frozen_tasks_and_writes_the_table(ceiling_world):
    """Both arms on exactly the two listed tasks, 2 attempts each, under the runner shell (the ceiling's agent in the
    worker, W*'s v1 package under the default agent), recorded like any arm; the table, the assertion types, the cost
    per attempt and the decision numbers are written to the workspace and the task file is untouched."""
    cfg, base, tasks = ceiling_world
    before = (tasks.read_bytes(), tasks.stat().st_mtime_ns)
    out = []
    report = RUNNER.run(cfg, base, out=out.append)
    assert report["ok"], report["problems"]
    assert (tasks.read_bytes(), tasks.stat().st_mtime_ns) == before
    results = Results(base / "ws", rsi.DOMAIN)
    rows = list(results.all())
    assert {r.task_id for r in rows} == {"rsi.dev_notify_after_update", "rsi.notify_after_update"}
    arms = {r.attempt_id.rpartition("#")[0].rpartition("/")[2] for r in rows}
    assert arms == {"wstar", "ceiling"} and len([r for r in rows if r.validity == "valid"]) == 8
    result = json.loads((base / "ws" / "ceiling" / "result.json").read_text())
    per = result["per_task"]
    assert per["rsi.dev_notify_after_update"]["arms"] == {"wstar": [False, False], "ceiling": [True, True]}
    assert per["rsi.notify_after_update"]["arms"] == {"wstar": [False, False], "ceiling": [True, True]}
    d = result["decision"]
    assert (d["numbers"]["R"], d["numbers"]["X"], d["numbers"]["B"]) == (2, 2, 0)
    assert d["verdict"] == "inconclusive"                    # two tasks are fewer than the rule's four
    assert result["arms"]["wstar"]["failed_assertion_types"] == {"slack_message_contains": 4}
    assert result["arms"]["ceiling"]["failed_assertion_types"] == {}
    assert result["arms"]["ceiling"]["cost_per_attempt"] and result["arms"]["wstar"]["cost_per_attempt"]
    assert result["by_failure_kind"]["missing_write"]["fixed"] == 2
    assert (base / "ws" / "ceiling" / "result.md").read_text().startswith("# Ceiling experiment")
    # every model call of the ceiling arm asked for high effort; the W* arm's default agent asked for none and ran
    # at the actor's pinned "high" — the same effort in both arms
    asked = [effort for kind, effort in CeilingWorld.calls if kind in ("evidence", "check")]
    assert asked and set(asked) == {"high"}
    transcripts = {r.evidence: r.attempt_id for r in rows}
    efforts = {"wstar": set(), "ceiling": set()}
    for session, attempt in transcripts.items():
        arm = attempt.rpartition("#")[0].rpartition("/")[2]
        for line in (base / "ws" / "transcripts" / f"{session}.jsonl").read_text().splitlines():
            event = json.loads(line)
            if event.get("kind") == "gateway_call":
                efforts[arm].add((event["role"], event["effort"], event["requested_effort"]))
    assert efforts["ceiling"] == {("act", "high", "high"), ("aux", "high", "high")}
    assert efforts["wstar"] == {("act", "high", None)}
    # each lane's plan refills an attempt lost to infrastructure up to three times
    event = next(e for e in ct.Events(base / "ws").of("ceiling_run"))
    objects = ct.Objects(base / "ws")
    assert sorted(event["plans"]) == ["ceiling/dev", "ceiling/train", "wstar/dev", "wstar/train"]
    assert {objects.get(ref)["retry_policy"]["max_infrastructure_retries"] for ref in event["plans"].values()} == {3}
    assert result["arms"]["ceiling"]["limits"] == {} and result["arms"]["ceiling"]["package_crashes"] == []
    # a rerun pays for nothing again
    calls = len(CeilingWorld.calls)
    again = RUNNER.run(cfg, base, out=lambda _line: None)
    assert again["ok"] and len(CeilingWorld.calls) == calls
    assert len(list(Results(base / "ws", rsi.DOMAIN).all())) == len(rows)


def test_the_runner_never_picks_tasks_a_test_task_or_another_split_is_refused(ceiling_world):
    """The task file is the only source of tasks: one naming a test task, or chosen on another split, is refused
    before anything runs."""
    cfg, base, tasks = ceiling_world
    spec = json.loads(tasks.read_text())
    bad = base / "bad.json"
    bad.write_text(json.dumps({**spec, "dev": spec["dev"] + [{"task_id": rsi.FINAL[0]}]}))
    report = RUNNER.run({**cfg, "ceiling": {**cfg["ceiling"], "tasks": str(bad)}}, base, out=lambda _line: None)
    assert not report["ok"] and any("is a test task" in p for p in report["problems"])
    other = base / "other.json"
    other.write_text(json.dumps({**spec, "source": {"split_hash": "0000"}}))
    report = RUNNER.run({**cfg, "ceiling": {**cfg["ceiling"], "tasks": str(other)}}, base, out=lambda _line: None)
    assert not report["ok"] and any("was chosen on split 0000" in p for p in report["problems"])
    assert CeilingWorld.calls == [] and not Results(base / "ws", rsi.DOMAIN).path.exists()
    with pytest.raises(ValueError, match="names a task twice"):
        dup = base / "dup.json"
        dup.write_text(json.dumps({**spec, "train": spec["dev"]}))
        RUNNER.load_tasks(dup)


def test_the_frozen_task_file_and_the_w_star_package_are_the_ones_the_docs_describe(tmp_path):
    """examples/automationbench/ceiling/tasks.json: 6 dev tasks (4 failed on every one of W*'s 3 dev readings, 2 on
    the latest two) and the 7 train tasks W*'s last train measurement failed; no test task; types and counts only.
    The W* package beside it is v0.2's installed package byte for byte: its files give the recorded bundle refs."""
    spec = RUNNER.load_tasks(EXAMPLES / "ceiling" / "tasks.json")
    assert (len(spec["dev"]), len(spec["train"])) == (6, 7)
    s = spec["spec"]
    assert s["counts"] == {"dev": 6, "dev_persistent": 4, "train": 7, "total": 13}
    assert s["source"]["w_star"]["release_ref"].startswith("release:sha256:6cd37b8e49a4")
    assert all(e["failed_readings"] >= 2 for e in s["dev"]) and all(e["failed_latest_reading"] for e in s["dev"])
    assert "params" not in json.dumps(s["dev"] + s["train"])
    files = files_of(WSTAR)
    objects = ct.Objects(tmp_path)
    run = {p: t for p, t in files.items() if hp.classify(p) != "test"}
    tests = {p: t for p, t in files.items() if hp.classify(p) == "test"}
    assert hp._bundle(objects, run) == s["source"]["w_star"]["package_bundle_ref"]
    assert hp._bundle(objects, tests) == s["source"]["w_star"]["package_test_bundle_ref"]


def test_the_budget_roots_hard_stop_ends_the_run_and_leaves_no_verdict(ceiling_world):
    """A root too small for the run: the call that cannot be reserved with nothing in flight ends its attempt as
    budget_exhausted, no further arm is started, and the decision is `incomplete` with the numbers it has."""
    cfg, base, _tasks = ceiling_world
    cfg = {**cfg, "freeze": {"budget": {"usd": 0.168}}}           # one call reserves up to $0.1655 here
    out = []
    report = RUNNER.run(cfg, base, out=out.append)
    result = report["result"]
    assert result["stopped"] == "the budget root's hard stop" and result["decision"]["verdict"] == "incomplete"
    rows = list(Results(base / "ws", rsi.DOMAIN).all())
    assert any(r.failure_class == "budget_exhausted" for r in rows)
    assert len([r for r in rows if r.validity == "valid"]) < 8
    assert any(line.startswith("STOPPED") for line in out)
    # the pre-run note compares the estimate with the root less (1 + concurrency) reservations
    assert any(line.startswith("NOTE       the nominal estimate $6.2 is above the root less 3 reservations")
               for line in out)


def row(arm, task_id, repeat, *, success=False, retry=0, invalid="", failure_class="", termination="finished",
        limit=None, split="dev"):
    from ouroloop.benchmark import TaskResult
    return TaskResult(task_id, task_id, split, repeat, arm, "run", "man", success=success,
                      partial_credit=1.0 if success else 0.5, cost_usd=0.5, attempt_id=f"run/{task_id}/r{repeat}/"
                      f"{arm}#{retry}", validity="invalid" if invalid else "valid", invalid_reason=invalid,
                      failure_class=failure_class or ("" if success or invalid else "task_outcome"),
                      termination="harness_error" if invalid else termination, limit=limit)


def test_a_crash_of_the_arms_own_package_is_a_failed_attempt_and_an_environment_failure_leaves_the_pair_unread():
    """The rule's reading of invalid attempts, decided before the run: the final row of a repeat (its last refill)
    invalid because the arm's agent program raised at run time (AgentRunError, as task_eval records it under the
    runner shell) or the candidate could not be assembled counts as a failed attempt of that arm; one lost to the
    environment (a provider or worker failure, the budget) leaves the pair unread. A refill that ends valid is the
    attempt. Attempts a hard limit ended are listed per arm and dimension."""
    tasks = {"dev": ["d1", "d2", "d3"], "train": [], "spec": {"dev": [{"task_id": "d1"}, {"task_id": "d2"},
                                                                     {"task_id": "d3"}]}}
    crash = "harness: AgentRunError: KeyError: 'text'"
    rows = {"wstar": [row("wstar", t, i) for t in ("d1", "d2", "d3") for i in (0, 1)],
            "ceiling": [
                # d1: the package crashed on repeat 0 and on all three refills; repeat 1 passed -> 1/2, read
                *[row("ceiling", "d1", 0, retry=k, invalid=crash, failure_class="infrastructure") for k in range(4)],
                row("ceiling", "d1", 1, success=True),
                # d2: a provider failure, then a valid refill that passed; repeat 1 cut by the seconds limit
                row("ceiling", "d2", 0, invalid="harness: ProviderError: overloaded", failure_class="infrastructure"),
                row("ceiling", "d2", 0, retry=1, success=True),
                row("ceiling", "d2", 1, termination="budget", limit="seconds"),
                # d3: the worker died on repeat 1 and every refill: the pair is unread
                row("ceiling", "d3", 0),
                *[row("ceiling", "d3", 1, retry=k, invalid="harness: WorkerCrashed: the worker exited",
                      failure_class="infrastructure") for k in range(4)]]}
    result = RUNNER.tabulate(rows, tasks, None, 2)
    per = result["per_task"]
    assert per["d1"]["arms"]["ceiling"] == [False, True] and per["d1"]["detail"]["ceiling"]["package_crash"] == [
        True, False]
    assert per["d2"]["arms"]["ceiling"] == [True, False]
    assert per["d3"]["arms"]["ceiling"] == [False]
    d = result["decision"]
    assert d["unread"] == ["d3"] and d["verdict"] == "incomplete" and d["read"] == 2
    arm = result["arms"]["ceiling"]
    assert [c["attempt_id"] for c in arm["package_crashes"]] == ["run/d1/r0/ceiling#3"]
    assert [u["attempt_id"] for u in arm["unread_attempts"]] == ["run/d3/r1/ceiling#3"]
    assert arm["limits"] == {"seconds": ["run/d2/r1/ceiling#0"]}
    assert result["arms"]["wstar"]["limits"] == {} and result["arms"]["wstar"]["package_crashes"] == []
    text = RUNNER.render({**result})
    assert "limits seconds 1 (run/d2/r1/ceiling#0)" in text and "package crashes 1" in text and "unread 1" in text
    defect = {"wstar": rows["wstar"], "ceiling": [row("ceiling", t, i, invalid="candidate: tools shadow a base tool",
                                                      failure_class="candidate_defect")
                                                  for t in ("d1", "d2", "d3") for i in (0, 1)]}
    d = RUNNER.tabulate(defect, tasks, None, 2)["decision"]
    assert d["unread"] == [] and d["numbers"]["R"] == 3 and d["numbers"]["X"] == 0
