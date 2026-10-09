"""Diagnosis (docs/design/IMPROVER.md §5; DESIGN §6.3): failed train attempts into cases, cases into causes.

One aux call per failed attempt reads its deterministic features (evidence.py), the checks the grader marked failed
(train grading: type and params) and the end of its trajectory with a trace reference per message, and names one cause
from the vocabulary. A cause outside the vocabulary is `other`; a new name the model proposes joins the vocabulary once
two cases propose it (`vocabulary_extension`; the ledger records it and the next round uses it). The same call
classifies the memory's promotable entries (MEMORY §8): an entry whose cause is mechanizable joins that cause's
cluster as an input; any other becomes a skill candidate of the fixed code.

A case is cached by the request's digest and the model (rounds/notes.jsonl): the same failure under the same prompt is
never asked twice. Causes are the clusters of cases by name, largest first.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..contracts import Record, append_jsonl, digest, read_jsonl, register
from .evidence import AttemptEvidence, Evidence

PROMPTS = Path(__file__).resolve().parent / "prompts"
DEFINITIONS = {
    "excluded_mentioned": "the agent acted on or mentioned an item the request, the data or a policy excluded",
    "missed_action": "a change, message or step the request asked for was not made",
    "wrong_target": "the agent changed or wrote to the wrong record, field, channel or object",
    "missing_recipient": "a message went out without someone it had to reach, or to the wrong people",
    "unread_value": "the agent used a value without reading it from the source the request named",
    "arith_mismatch": "a computed number (a sum, a count, a rate, a date difference) is wrong",
    "real_date": "the agent used the real calendar instead of the date the world's own records give",
    "wrong_location": "the content went to the wrong place: a file, a folder, a channel, a thread",
    "premature_stop": "the agent stopped or declared success before the work was done",
    "tool_error_loop": "the agent repeated failing tool calls instead of reading the error and changing course",
    "context_overflow": "the agent lost track of the task as its context grew (summaries, cut history)",
    "finish_protocol": "the agent did not end the run the way the harness expects (no finish, finish too early)",
    "other": "none of the above",
}
TAIL_CHARS = 12000                    # of the trajectory's end, per case
ITEM_CHARS = 1500                     # of one message in the tail
NAME = re.compile(r"^[a-z][a-z0-9_]{2,39}$")


@register
@dataclass(frozen=True)
class Case(Record):
    TYPE = "case"
    attempt_id: str = ""
    task_id: str = ""
    cause: str = "other"
    mechanizable: bool = False
    evidence_refs: tuple = ()
    assertion_kinds: tuple = ()
    tool_errors: tuple = ()
    termination: str = ""
    explanation: str = ""               # the model's note ("note" is a volatile field of a record: not stored)
    proposed: str = ""


@register
@dataclass(frozen=True)
class Cause(Record):
    TYPE = "cause"
    cause_id: str = ""
    name: str = ""
    cases: tuple = ()                 # case refs
    tasks: tuple = ()
    memory_entries: tuple = ()        # MEMORY §8: cause_input rows of mechanizable promoted entries
    mechanizable: bool = False


class NoteCache:
    """rounds/notes.jsonl: identity (request digest and model) -> case ref; the cases are objects."""

    def __init__(self, root, objects):
        self.path = Path(root) / "rounds" / "notes.jsonl"
        self.objects = objects

    def get(self, identity: str) -> str | None:
        for row in read_jsonl(self.path):
            if row.get("identity") == identity and self.objects.has(row.get("case")):
                return row["case"]
        return None

    def put(self, identity: str, case: Case) -> str:
        ref = self.objects.put(case)
        append_jsonl(self.path, {"identity": identity, "case": ref, "attempt": case.attempt_id})
        return ref


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def vocabulary_text(vocabulary) -> dict:
    return {name: DEFINITIONS.get(name, "a cause a diagnosis proposed and two cases confirmed") for name in vocabulary}


def trajectory_tail(evidence: Evidence, ev: AttemptEvidence, chars: int = TAIL_CHARS) -> list[dict]:
    """The end of the attempt's rebuilt trajectory, each message with its trace ref, within `chars`."""
    messages = evidence.trace.trajectory(ev.session)
    out, used = [], 0
    for m in reversed(messages):
        item = {"ref": (m.get("metadata") or {}).get("ref"), "role": m.get("role"),
                "content": _clip(str(m.get("content") or ""), ITEM_CHARS)}
        if m.get("tool_calls"):
            item["tool_calls"] = [{"name": c["function"]["name"], "arguments": _clip(c["function"]["arguments"], 400)}
                                  for c in m["tool_calls"]]
        size = len(json.dumps(item, ensure_ascii=False))
        if out and used + size > chars:
            break
        out.insert(0, item)
        used += size
    return out


def case_request(evidence: Evidence, ev: AttemptEvidence, vocabulary) -> tuple[str, str]:
    user = {"task_id": ev.task_id, "features": evidence.features(ev),
            "failed_checks": [{"type": c["type"], "params": c["params"]} for c in ev.failed_checks][:20],
            "trajectory_tail": trajectory_tail(evidence, ev), "vocabulary": vocabulary_text(vocabulary)}
    return prompt("diagnose"), json.dumps(user, ensure_ascii=False, default=str, sort_keys=True)


def entry_request(entry: dict, vocabulary) -> tuple[str, str]:
    row = entry.get("row") or {}
    user = {"memory_entry": {"when": row.get("when_to_use") or entry.get("when"),
                             "content": row.get("content") or entry.get("content")},
            "question": "Which cause of the vocabulary does this lesson guard against? Answer with the JSON object; "
                        "evidence_refs may be empty.", "vocabulary": vocabulary_text(vocabulary)}
    return prompt("diagnose"), json.dumps(user, ensure_ascii=False, default=str, sort_keys=True)


def read_reply(text: str) -> dict:
    """The JSON object of a reply (fenced or bare); {} when there is none."""
    text = str(text or "")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            value = json.loads(c)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - n // 3] + f" [... {len(text) - n} characters ...] " + text[-n // 3:]


def _ask(researcher, system: str, user: str) -> dict:
    turn = researcher.chat(system, [{"role": "user", "content": user}], [])
    return read_reply(getattr(turn, "text", "") or "")


def model_of(researcher) -> str:
    return str(getattr(researcher, "model", "") or getattr(researcher, "name", "") or "model")


def diagnose_case(evidence: Evidence, ev: AttemptEvidence, *, researcher, vocabulary, mechanizable,
                  cache: NoteCache) -> tuple[str, Case, bool]:
    """One failed attempt's case. -> (case ref, case, whether it came from the cache)."""
    system, user = case_request(evidence, ev, vocabulary)
    identity = digest({"attempt": ev.attempt_id, "system": system, "user": user, "model": model_of(researcher)})
    ref = cache.get(identity)
    if ref is not None:
        return ref, Case.from_json(cache.objects.get(ref)), True
    reply = _ask(researcher, system, user)
    cause = str(reply.get("cause") or "other")
    proposed = str(reply.get("proposed") or "").strip().lower().replace(" ", "_").replace("-", "_")
    if cause not in vocabulary:
        proposed = proposed or cause.strip().lower().replace(" ", "_").replace("-", "_")
        cause = "other"
    refs = tuple(r for r in reply.get("evidence_refs") or () if isinstance(r, str) and r.startswith(ev.session + ":L"))
    case = Case(attempt_id=ev.attempt_id, task_id=ev.task_id, cause=cause, mechanizable=cause in set(mechanizable),
                evidence_refs=refs, assertion_kinds=tuple(sorted({str(c["type"]) for c in ev.failed_checks})),
                tool_errors=tuple(sorted(set(ev.tool_errors))), termination=ev.result.termination,
                explanation=str(reply.get("note") or "")[:1200],
                proposed=proposed if cause == "other" and NAME.match(proposed or "") else "")
    return cache.put(identity, case), case, False


def classify_entries(entries: list[dict], *, researcher, vocabulary, cache: NoteCache | None = None) \
        -> dict[str, str]:
    """{entry id: cause} for the memory's promotable entries (a cause outside the vocabulary is other). With a cache
    an entry asked about before under the same request and model is not asked again."""
    out = {}
    for e in entries:
        eid = str(e.get("memory_id") or e.get("id"))
        system, user = entry_request(e, vocabulary)
        identity = digest({"entry": eid, "system": system, "user": user, "model": model_of(researcher)})
        ref = cache.get(identity) if cache is not None else None
        if ref is not None:
            out[eid] = Case.from_json(cache.objects.get(ref)).cause
            continue
        cause = str(_ask(researcher, system, user).get("cause") or "other")
        out[eid] = cause if cause in vocabulary else "other"
        if cache is not None:
            cache.put(identity, Case(attempt_id=f"entry:{eid}", cause=out[eid]))
    return out


def cluster(cases: list[tuple[str, Case]], *, round_id: str, mechanizable, memory_inputs: dict | None = None) \
        -> list[Cause]:
    """Causes from (case ref, case): one per name, its cases, its tasks; memory entries joined by cause name."""
    by: dict[str, list[tuple[str, Case]]] = {}
    for ref, case in cases:
        by.setdefault(case.cause, []).append((ref, case))
    for name in (memory_inputs or {}):
        by.setdefault(name, [])
    out = []
    for name, items in by.items():
        out.append(Cause(cause_id=f"{round_id}:{name}", name=name, cases=tuple(r for r, _ in items),
                         tasks=tuple(dict.fromkeys(c.task_id for _, c in items)),
                         memory_entries=tuple((memory_inputs or {}).get(name) or ()),
                         mechanizable=name in set(mechanizable)))
    return sorted(out, key=lambda c: (-len(c.tasks), -len(c.memory_entries), c.name))


def vocabulary_extension(cases: list[Case], vocabulary) -> list[str]:
    """New cause names proposed by at least two cases (distinct attempts), not already in the vocabulary."""
    count: dict[str, set] = {}
    for c in cases:
        if c.proposed and c.proposed not in set(vocabulary):
            count.setdefault(c.proposed, set()).add(c.attempt_id)
    return sorted(name for name, attempts in count.items() if len(attempts) >= 2)
