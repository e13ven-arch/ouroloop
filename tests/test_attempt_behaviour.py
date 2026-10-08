"""What one attempt does, pinned on the stirrup seed (docs/design/CONTRACTS.md §10; RUNTIME.md §8; PLAN rule 15).

One attempt at a fixed train task of the miniature `rsi` domain, through the production path of an activity: the seed
release {W0, R0, K} bootstrapped from the test config, the activity's budget book and metered actor, `phases.run_arm`,
the runner shell, and the stirrup seed's agent in a ProcessWorker. The scripted actor below answers from what it is
sent, and the attempt it makes touches each part of the shell once: two base calls in one turn, the seed's triage hook
blocking the first write, the package tool plan_changes, a call the environment answers with an error, two writes,
and the seed's verify hook pushing back the first finish. This test pins

- the model calls: what the actor received (system prompt, messages by role, tool names, the effort keyword) and what
  it answered, and the gateway's account of each (role, the effort requested and sent);
- the base tool calls with their arguments and outputs, and the broker's decision on each;
- the hook lines and the package's marks;
- the result row's outcome fields, the grading row, and the transcript's line kinds, end line and worker session.

Timestamps, durations, memory readings and the gateway's random call ids are dropped; every hash and ref is replaced by
the order it first appears in, so a change to any framework file (which changes `code_version`, hence K and every ref)
does not move the fixture, and a change of behaviour does. To re-record it after a change that is meant to alter
behaviour (a new seed, a new hook, a new prompt; and only then, with the diff explained in the commit):

    OUROLOOP_CAPTURE_BEHAVIOUR=1 .venv/bin/python -m pytest -q tests/test_attempt_behaviour.py
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path

import rsi
from ouroloop.activity import phases
from ouroloop.contracts import read_jsonl
from ouroloop.runtime.chat import ChatTurn, ToolCall
from ouroloop.runtime.records import session_file

FIXTURE = Path(__file__).parent / "fixtures" / "attempt_behaviour_stirrup.json"
CAPTURE = os.environ.get("OUROLOOP_CAPTURE_BEHAVIOUR") == "1"
TASK = "rsi.notify_after_update"


# ---------------------------------------------------------------------------- the scripted actor
class ScriptedActor:
    """A deterministic actor that reads the request and the tool results it is sent: it discovers the endpoints (two
    searches in one turn), tries a write with the wrong method until the API answers it, plans when a result asks it to,
    updates the contact the request names, posts the phrase the request asks for, and finishes with the finish tool
    when one is offered (again, when the finish is pushed back). Every call it receives is kept in `seen`."""
    name = "scripted-actor"
    model = "scripted-actor-1"
    supports_history_edits = True

    def __init__(self):
        self.seen: list = []

    def chat(self, system: str, messages: list[dict], tools, effort: str | None = None) -> ChatTurn:
        self.seen.append({"system": system, "messages": _plain(messages), "tools": [t.name for t in tools],
                          "effort": effort})
        names = {t.name for t in tools}
        task = next((m["content"] for m in messages if m.get("role") == "user"), "")
        calls = [c for m in messages if m.get("role") == "assistant" for c in m.get("tool_calls") or ()]
        results = {r.get("id"): r for m in messages if m.get("role") == "tool" for r in m.get("results") or ()}
        latest = next((m.get("results") or [] for m in reversed(messages) if m.get("role") == "tool"), [])
        n = len([m for m in messages if m.get("role") == "assistant"])
        usage = {"input_tokens": 400 + 50 * n, "output_tokens": 30 + n}
        contact = next((cid for person, cid in rsi.CONTACTS.items() if person in task), None)
        phrase = re.search(r"phrase ([a-z ]+?)(?:,|\.|$)", task)

        def answer(call) -> dict | None:
            """The API's answer to a call, or None when none reached it (a hook's words are not JSON)."""
            try:
                return json.loads(results[call.id].get("output") or "") if call.id in results else None
            except ValueError:
                return None
        fetches = [c for c in calls if c.name == "api_fetch"]
        answered = {c.args.get("method") for c in fetches if answer(c) is not None}

        def fetch(call_id: str, method: str, url: str, body: dict, text: str = "") -> ChatTurn:
            return ChatTurn(text, [ToolCall(call_id, "api_fetch", {"method": method, "url": url,
                                                                    "body": json.dumps(body)})], "tool_use",
                            usage=usage)
        if not calls:
            return ChatTurn("Finding the endpoints.", [
                ToolCall("a1", "api_search", {"query": "update contact stage"}),
                ToolCall("a2", "api_search", {"query": "post message channel"})], "tool_use", usage=usage)
        planned = any(c.name == "plan_changes" for c in calls)
        if "plan_changes" in names and not planned and any("plan_changes" in str(r.get("output")) for r in latest):
            changes = [{"content": "move the contact to opportunity", "status": "pending"}]
            if phrase:
                changes.append({"content": f"post {phrase.group(1).strip()} in #ops", "status": "pending"})
            return ChatTurn("Planning first.", [ToolCall(f"p{n}", "plan_changes", {"changes": changes,
                                                                                    "excluded": []})],
                            "tool_use", usage=usage)
        if "PUT" not in answered:
            return fetch(f"w{n}", "PUT", rsi.HUBSPOT, {"id": "c0"})
        if contact and "opportunity stage" in task and "PATCH" not in answered:
            return fetch(f"u{n}", "PATCH", rsi.HUBSPOT, {"id": contact, "stage": "opportunity"},
                         "Updating the contact.")
        if phrase and "POST" not in answered:
            return fetch(f"n{n}", "POST", rsi.SLACK, {"channel": "C1", "text": f"{phrase.group(1).strip()} for the "
                                                                                 f"contact"})
        done = [f"{c.args['method']} {c.args['url'].rsplit('/', 1)[-1]}" for c in fetches
                if isinstance(answer(c), dict) and "error" not in answer(c)]
        reason = "Done: " + ", ".join(done) + "."
        if "finish" in names:
            return ChatTurn("", [ToolCall(f"f{n}", "finish", {"reason": reason})], "tool_use", usage=usage)
        return ChatTurn(reason, [], "end", usage=usage)


def _plain(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _plain(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items() if k != "native"}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------- what is pinned
def _message(m: dict) -> dict:
    """A history entry as the fixture keeps it: its role and what it says, tool calls and results by name."""
    role = m.get("role")
    if role == "assistant":
        return {"role": role, "text": m.get("text", ""),
                "tool_calls": [{"name": c["name"], "args": c["args"]} for c in m.get("tool_calls") or ()]}
    if role == "tool":
        return {"role": role, "results": [{"name": r.get("name"), "is_error": r.get("is_error")}
                                          for r in m.get("results") or ()]}
    return {"role": role, "content": m.get("content")}


OUTCOME = ("task_id", "family_id", "split", "attempt", "candidate", "success", "partial_credit", "assertions_passed",
           "assertions_scored", "usage", "cost_usd", "steps", "tool_calls", "termination", "stop", "limit",
           "validity", "invalid_reason", "failure_class", "attempt_id", "pair_key", "supersedes", "evidence",
           "system_release_ref")


def capture(actor: ScriptedActor, activity, row) -> dict:
    lines = list(read_jsonl(session_file(activity.root, row.evidence)))
    of = lambda kind: [line for line in lines if line["kind"] == kind]       # noqa: E731
    grading = [g for g in read_jsonl(activity.results.grading_path()) if g["attempt_id"] == row.attempt_id]
    return {
        "system": sorted({call["system"] for call in actor.seen}),
        "model_calls": [{"messages": [_message(m) for m in call["messages"]], "tools": call["tools"],
                         "effort": call["effort"]} for call in actor.seen],
        "gateway_calls": [{k: g[k] for k in ("role", "effort_requested", "effort_sent", "messages_prefix", "model",
                                              "reply", "usage")} for g in of("gateway_call")],
        "tool_specs": [[s["name"] for s in t["specs"]] for t in of("tool_specs")],
        "tools": [{k: t[k] for k in ("name", "args", "output", "is_error", "read_only")} for t in of("tool")],
        "broker_events": [{k: b[k] for k in ("name", "mutates", "refused", "reason", "read_only")}
                          for b in of("broker_event")],
        "hooks": [{k: h[k] for k in ("site", "name", "action", "detail")} for h in of("hook")],
        "marks": [{"mark": m["mark"], "data": m["data"]} for m in of("mark")],
        "kinds": [line["kind"] for line in lines],
        "task": {k: v for k, v in of("task")[0].items() if k not in ("kind", "seq")},
        "worker_session": {k: v for k, v in of("worker_session")[0].items() if k not in ("kind", "seq")},
        "end": {k: v for k, v in of("end")[0].items() if k not in ("kind", "seq")},
        "outcome": {k: getattr(row, k) for k in OUTCOME},
        "grading": [{k: g[k] for k in ("task_id", "split", "assertions")} for g in grading],
    }


# ---------------------------------------------------------------------------- normalisation
DROP = {"ts", "ms", "seconds", "elapsed", "maxrss_mb"}
HEX = re.compile(r"(?<![0-9a-f])[0-9a-f]{12,64}(?![0-9a-f])")
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?")


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in DROP}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def normalise(got: dict, paths: dict) -> dict:
    """Volatile fields dropped; the test's own paths named; every hash and ref replaced by the order it first appears
    in, so two places that held the same value still hold the same placeholder. (The gateway's call ids, which end in
    random hex, are not kept: a call is known by its place in the list.)"""
    text = json.dumps(_strip(got), sort_keys=True, ensure_ascii=False)
    for path, name in sorted(paths.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(path, name)
    text = STAMP.sub("<ts>", text)
    seen: dict = {}

    def placeholder(match) -> str:
        return seen.setdefault(match.group(0), f"<h{len(seen) + 1}>")
    text = HEX.sub(placeholder, text)
    return json.loads(text)


def run_one(upstream: str, tmp_path: Path) -> dict:
    actor = ScriptedActor()
    activity = rsi.activity(tmp_path, upstream, provider=actor)
    (row,) = phases.run_arm(activity, release=activity.active_release(), split="train", candidate="behaviour",
                            role="base", pairs=[(TASK, 0)], account=activity.account("behaviour"))
    return normalise(capture(actor, activity, row), {str(tmp_path): "<tmp>", str(upstream): "<upstream>"})


# ---------------------------------------------------------------------------- the test
def test_one_attempt_on_the_stirrup_seed_behaves_as_recorded(fake_upstream, tmp_path):
    got = run_one(fake_upstream, tmp_path)
    # the shape this test relies on, whatever the fixture says: the attempt ran through every part it is meant to
    assert got["outcome"]["validity"] == "valid" and got["outcome"]["split"] == "train"
    assert [h["action"] for h in got["hooks"]] == ["block", "pushback"]
    assert {t["name"] for t in got["tools"]} == {"api_search", "api_fetch"}
    assert "plan_changes" in {m["data"].get("tool") for m in got["marks"]}
    if CAPTURE:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    want = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for key in ("outcome", "grading", "kinds", "end", "worker_session", "task", "hooks", "marks", "tools",
                "broker_events", "tool_specs", "system"):
        assert got[key] == want[key], f"{key} differs from the recorded attempt"
    assert len(got["model_calls"]) == len(want["model_calls"])
    for i, (g, w) in enumerate(zip(got["model_calls"], want["model_calls"])):
        assert g == w, f"model call {i} differs"
    for i, (g, w) in enumerate(zip(got["gateway_calls"], want["gateway_calls"])):
        assert g == w, f"gateway call {i} differs"
    assert got == want
