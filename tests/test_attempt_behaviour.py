"""What one attempt does, pinned before slimming (docs/PLAN.md P1; decision of 2026-10-08).

Hash identity with the existing workspaces is not required: no existing activity is resumed at HEAD, so ids, refs,
digests and timestamps are normalised away here. What is pinned is behaviour: under the K of the
`automationbench_harness_v3` preset and, separately, of the `automationbench_harness_v5` one (the base of the next
preset: its method and library tables), each on the miniature `rsi` domain with W0/R0/K bootstrapped from the
configuration as a real activity's are, the baseline phase limited to one dev task and one train task runs two
attempts through the production path (open_activity, the startup checks, the activity's budget book,
Improvement.run_arm, the runner shell). The scripted actor below answers from what it is sent, and for both attempts
this test pins

- the model-call sequence the actor received (system prompt, messages by role, tool names, the effort keyword) and
  what it answered;
- the attempt's transcript on record (model calls with their roles and efforts, tool specs, every tool call with
  its arguments, gate decision and output, the end event);
- the outcome (the result row's outcome fields) and the grading row.

The fixture was captured from the code at 8434c03, before any slimming step, under the v3 K. Under the v5 K the
same capture, made at tag archive-2026-10-08 (the same ouroloop/ and tests/fake_upstream as 8434c03; the P1 review,
2026-10-08), is byte for byte this fixture: W0 holds no library entry and no method, so nothing of v5's tables reaches
these attempts, and both presets are pinned by the one file. To recapture it after a change that is meant to alter
behaviour (and only then, with the diff explained in the commit; a v5 run that differs then needs its own file):

    OUROLOOP_CAPTURE_BEHAVIOUR=1 .venv/bin/python -m pytest -q tests/test_attempt_behaviour.py
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
from pathlib import Path

import pytest

from ouroloop import bench
from ouroloop.adapters import automationbench as ab
from ouroloop.agent.worker import ProcessWorker
from ouroloop.benchmark import Results, SplitManifest
from ouroloop.chat import ChatTurn, ToolCall
from ouroloop.ledger import _read_jsonl

FIXTURE = Path(__file__).parent / "fixtures" / "attempt_behaviour_v3.json"
PRESETS = ("automationbench_harness_v3", "automationbench_harness_v5")
CAPTURE = os.environ.get("OUROLOOP_CAPTURE_BEHAVIOUR") == "1"

DOMAIN = "rsi"
DEV_TASK = "rsi.dev_notify_after_update"
# The miniature upstream's rsi families and their splits (the same declaration tests/rsi.py makes), copied so this
# test does not move when that fixture is migrated.
FAMILIES = {"rsi.update_stage": "rsi.update",
            "rsi.notify_after_update": "rsi.notify_after_update",
            "rsi.notify_after_update_variant": "rsi.notify_after_update",
            "rsi.policy_hold": "rsi.hold",
            "rsi.notify_once": "rsi.notify_once",
            "rsi.dev_notify_after_update": "rsi.dev_notify_after_update",
            "rsi.dev_update_stage": "rsi.dev_update",
            "rsi.dev_policy_hold": "rsi.dev_hold",
            "rsi.final_notify_after_update": "rsi.final_notify_after_update",
            "rsi.final_update_stage": "rsi.final_update"}
SPLITS = {"rsi.update": "train", "rsi.notify_after_update": "train", "rsi.hold": "train",
          "rsi.notify_once": "train", "rsi.dev_notify_after_update": "dev", "rsi.dev_update": "dev",
          "rsi.dev_hold": "dev", "rsi.final_notify_after_update": "test", "rsi.final_update": "test"}

SLACK = "https://slack.com/api/chat.postMessage"
HUBSPOT = "https://api.hubapi.com/crm/v3/objects/contacts"
CONTACTS = {"Dana Reyes": "c1", "Sam Okafor": "c2", "Lea Fontaine": "c3", "Kai Mercer": "c4"}


# ---------------------------------------------------------------------------- the scripted actor
class ScriptedActor:
    """A deterministic actor that reads the request it is sent and the tool results it got back: it discovers the
    endpoints (two searches in one turn), makes one call that fails (a wrong method), updates the contact the request
    names, posts the phrase the request asks for, and finishes. Every call it receives is kept in `seen`."""
    name = "scripted-actor"
    supports_history_edits = True
    seen: list = []

    def __init__(self, model: str = "scripted-actor-1", effort: str | None = None):
        self.model, self.effort = model, effort

    def chat(self, system: str, messages: list[dict], tools, effort: str | None = None) -> ChatTurn:
        ScriptedActor.seen.append({"system": system, "messages": _plain(messages), "tools": [t.name for t in tools],
                                   "effort": effort})
        task = next((m["content"] for m in messages if m.get("role") == "user"), "")
        calls = [c for m in messages if m.get("role") == "assistant" for c in m.get("tool_calls") or ()]
        n = len([m for m in messages if m.get("role") == "assistant"])
        usage = {"input_tokens": 400 + 50 * n, "output_tokens": 30 + n}
        name = next((x for x in CONTACTS if x in task), None)
        phrase = re.search(r"phrase ([a-z ]+?)(?:,|\.|$)", task)
        done = {(c.name, (c.args or {}).get("method"), (c.args or {}).get("url")) for c in calls}
        if not calls:
            return ChatTurn("Finding the endpoints.", [
                ToolCall("a1", "api_search", {"query": "update contact stage"}),
                ToolCall("a2", "api_search", {"query": "post message channel"})], "tool_use", usage=usage)
        if ("api_fetch", "PUT", HUBSPOT) not in done:
            return ChatTurn("", [ToolCall("a3", "api_fetch", {"method": "PUT", "url": HUBSPOT,
                                                              "body": json.dumps({"id": "c0"})})],
                            "tool_use", usage=usage)
        if name and "opportunity stage" in task and ("api_fetch", "PATCH", HUBSPOT) not in done:
            return ChatTurn("Updating the contact.", [ToolCall("a4", "api_fetch", {
                "method": "PATCH", "url": HUBSPOT, "body": json.dumps({"id": CONTACTS[name], "stage": "opportunity"})})],
                "tool_use", usage=usage)
        if phrase and ("api_fetch", "POST", SLACK) not in done:
            return ChatTurn("", [ToolCall("a5", "api_fetch", {
                "method": "POST", "url": SLACK,
                "body": json.dumps({"channel": "C1", "text": f"{phrase.group(1).strip()} for the contact"})})],
                "tool_use", usage=usage)
        return ChatTurn("Done: " + ", ".join(sorted(f"{m} {u.rsplit('/', 1)[-1]}" for _, m, u in done if m)) + ".",
                        [], "end", usage=usage)


def _plain(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _plain(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items() if k != "native"}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------- normalisation
DROP = {"ts", "elapsed", "observed_at"}
HEX = re.compile(r"(?<![0-9a-f])[0-9a-f]{12,64}(?![0-9a-f])")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?")


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in DROP}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def normalise(capture: dict, paths: dict) -> dict:
    """Timestamps and wall-clock durations dropped; the test's own paths named; every id, ref and digest replaced by
    the order it first appears in (so two places that held the same hash still hold the same placeholder)."""
    text = json.dumps(_strip(capture), sort_keys=True, ensure_ascii=False)
    for path, name in sorted(paths.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(path, name)
    text = STAMP.sub("<ts>", text)
    seen: dict = {}

    def placeholder(match) -> str:
        return seen.setdefault(match.group(0), f"<h{len(seen) + 1}>")
    text = UUID.sub(placeholder, text)
    text = HEX.sub(placeholder, text)
    return json.loads(text)


# ---------------------------------------------------------------------------- the activity
OUTCOME = ("task_id", "family_id", "split", "attempt", "candidate", "success", "partial_credit", "assertions_passed",
           "assertions_scored", "usage", "cost_usd", "steps", "tool_calls", "termination", "stop", "limit",
           "validity", "invalid_reason", "failure_class", "attempt_id", "pair_key", "supersedes", "evidence")


def v3_config(upstream: str, root: Path, monkeypatch, preset: str = "automationbench_harness_v3") -> dict:
    """The v3 preset on the rsi domain, as tests/test_research_session.py's v3_fixture adapts it: no image (a
    ProcessWorker for tests stands in for the Docker worker), the gate's least counts the domain's three dev tasks.
    `preset` adapts another preset the same way (tests/test_hot_path_boundary.py runs the v5 one)."""
    preset = copy.deepcopy(bench.PRESETS[preset])
    preset["domain"] = DOMAIN
    preset["harness"].update(sandbox=None)
    preset["task_plan"]["gate"].update(min_groups=1, bootstrap_replicates=200, min_tasks=3)
    preset["schedule"].update(worker_rounds=1)
    preset["campaign"].update(worker_round_limit=1, min_groups=1, min_headroom_tasks=1)
    monkeypatch.setitem(bench.PRESETS, "v3_behaviour", preset)
    monkeypatch.setattr(bench, "isolated_worker", lambda settings: ProcessWorker(for_tests=True) if settings else None)
    return {"workspace": {"root": str(root)},
            "bench": {"domain": DOMAIN, "upstream_path": upstream, "families": dict(FAMILIES),
                      "ratios": {"train": 0.5, "dev": 0.25, "test": 0.25}},
            "backends": {"mock": {"type": "mock"}},
            "points": {"default_backend": "mock"},
            "llm": {"type": f"{__name__}:ScriptedActor", "model": "scripted-actor-1", "effort": "medium"},
            "rsi": {"scope_id": "behaviour", "repeats": 1},
            "improvement": {"mode": "automatic", "preset": "v3_behaviour"},
            "harness_evolution": {"max_effort": "medium"},
            "pricing": {"input_per_1k": 0.001, "output_per_1k": 0.002, "max_input_tokens": 100000,
                        "max_output_tokens": 32768, "source": "test"},
            "freeze": {"budget": {"model_calls": 4000, "tool_calls": 4000, "usd": 50}}}


def run_baseline(upstream: str, tmp_path: Path, monkeypatch, preset: str = "automationbench_harness_v3") -> dict:
    root = tmp_path / "ws"
    suite = ab.load_suite(DOMAIN, upstream)
    fams, rule = ab.families(suite, FAMILIES)
    SplitManifest(DOMAIN, suite.versions, rule, suite.entries(fams), dict(SPLITS)).freeze(
        Results(root, DOMAIN).manifest_path())
    cfg = v3_config(upstream, root, monkeypatch, preset=preset)
    refs = bench.freeze(cfg, ".", out=lambda _line: None)
    cfg = {**cfg, "rsi": {**cfg["rsi"], **refs}}
    ScriptedActor.seen = []
    report = bench.baseline(cfg, ".", out=lambda _line: None, tasks=[DEV_TASK], limit=1)
    results = Results(root, DOMAIN)
    rows = list(_read_jsonl(results.path))
    grading = {g["attempt_id"]: g for g in _read_jsonl(results.grading_path())}
    attempts = []
    for row in rows:
        attempts.append({
            "outcome": {k: row.get(k) for k in OUTCOME},
            "grading": {k: grading.get(row["attempt_id"], {}).get(k) for k in ("task_id", "split", "assertions")},
            "transcript": list(_read_jsonl(root / "transcripts" / f"{row['evidence']}.jsonl")),
        })
    return {"model_calls": ScriptedActor.seen, "attempts": attempts,
            "summary": {arm: {k: v["summary"][k] for k in ("tasks", "attempts", "passed", "pass_rate", "invalid")}
                        | {"train": {k: v["train"][k] for k in ("tasks", "attempts", "passed", "pass_rate")}}
                        for arm, v in report["arms"].items()}}


@pytest.mark.parametrize("preset", PRESETS)
def test_one_baseline_attempt_per_split_behaves_as_before_slimming(fake_upstream, tmp_path, monkeypatch, preset):
    got = normalise(run_baseline(fake_upstream, tmp_path, monkeypatch, preset=preset),
                    {str(tmp_path): "<tmp>", str(fake_upstream): "<upstream>"})
    # the shape this test relies on, whatever the fixture says: one dev and one train attempt, the actor called
    assert [a["outcome"]["split"] for a in got["attempts"]] == ["dev", "train"]
    assert got["model_calls"] and all(a["grading"]["assertions"] for a in got["attempts"])
    if CAPTURE and preset == PRESETS[0]:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    want = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert got["summary"] == want["summary"]
    for g, w in zip(got["attempts"], want["attempts"]):
        assert g["outcome"] == w["outcome"]
        assert g["grading"] == w["grading"]
    assert len(got["model_calls"]) == len(want["model_calls"])
    for i, (g, w) in enumerate(zip(got["model_calls"], want["model_calls"])):
        assert g == w, f"model call {i} differs"
    assert len(got["attempts"]) == len(want["attempts"])
    for g, w in zip(got["attempts"], want["attempts"]):
        assert [e.get("kind") for e in g["transcript"]] == [e.get("kind") for e in w["transcript"]]
        for i, (ge, we) in enumerate(zip(g["transcript"], w["transcript"])):
            assert ge == we, f"{g['outcome']['split']} transcript event {i} ({we.get('kind')}) differs"
    assert got == want
