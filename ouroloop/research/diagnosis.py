"""M4 §1: what the first version's research step reads about a failure, and the per-task notes
(docs/modules/04_RESEARCH_LOOP.md §1).

A failure summary is a fixed projection of one failed train attempt: the request, what the agent did turn by turn,
the tool errors, the host events (a skill read, a pushback) and every scored check with its outcome. Only train
attempts are ever summarised; the grading detail comes from the results store's train-only reader, and assertions the
grader excluded (declared unscored, or true at the start and still true at the end) are left out. A free assertion
the agent broke is scored as a failure upstream, so it is shown like any other failed check.

Notes are written one failure at a time through the bound `research.diagnose`, and a round's summary call reads at
most `max_notes` of them. A note's identity is the digest of the exact request that produced it plus the model that
answered, so a changed diagnose prompt, a changed allowed-kind list or a different model never reuses an old note.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..evolve import contracts as ct
from ..ledger import _append_jsonl, _read_jsonl, now

TRAIN = ("train",)


@dataclass(frozen=True)
class DiagnosisLimits:
    """Fixed by the preset/K; R cannot raise them (docs/modules/07_BENCH_REPORT.md §2)."""
    max_notes: int = 30            # notes the summary call reads per round, cached and new together
    max_entry_chars: int = 600     # any single message, argument list or tool output in a summary
    max_turns: int = 40            # turns kept per summary (the last ones)
    max_task_chars: int = 4000

    def to_json(self) -> dict:
        return {"max_notes": self.max_notes, "max_entry_chars": self.max_entry_chars,
                "max_turns": self.max_turns, "max_task_chars": self.max_task_chars}


def clip(text, limit: int) -> str:
    """The head and the tail of a long entry: what an agent concludes, or what a tool returned last, is at the end
    as often as at the start, and a head-only cut hides it.

    The mark says whose cut it is. The agent had the whole text; a mark that read like a cut the agent saw made R
    write a hook against tool outputs the agent never had cut (the first Claude activity, 2026-10-03)."""
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    head = limit - limit // 2
    return (f"{text[:head]} [... {len(text) - limit} characters left out of this summary; the agent had the whole "
            f"text ...] {text[len(text) - limit // 2:]}")


def read_transcript(transcripts: Path, session: str) -> list[dict]:
    path = Path(transcripts) / f"{session}.jsonl"
    return list(_read_jsonl(path)) if session and path.exists() else []


def failure_summary(result, transcript: list[dict], grading: list[dict], limits: DiagnosisLimits) -> dict:
    """failure_summary.v1 of one failed train attempt. Raises if handed anything but a train attempt: this
    projection is where the train-only rule is enforced, not a courtesy of the caller."""
    if result.split not in TRAIN:
        raise ct.ContractError(f"{result.task_id} is a {result.split} attempt: only train attempts are summarised")
    n = limits.max_entry_chars
    turns, errors, events, task_text = [], [], [], ""
    for e in transcript:
        kind = e.get("kind")
        if kind == "task":
            task_text = e.get("text", "")
        elif kind == "assistant":
            turns.append({"turn": len(turns) + 1, "text": clip(e.get("text") or "", n),
                          "actions": [{"tool": c.get("name"), "args": clip(c.get("args") or {}, n)}
                                      for c in e.get("tool_calls") or ()]})
        elif kind == "tool":
            turn = len(turns)
            entry = {"tool": e.get("name"), "error": bool(e.get("is_error")), "output": clip(e.get("output") or "", n)}
            if turns:
                turns[-1].setdefault("results", []).append(entry)
            if entry["error"]:
                errors.append({"turn": turn, "tool": entry["tool"], "error": entry["output"]})
            if e.get("name") == "read_skill":
                events.append({"turn": turn, "event": "read_skill", "found": not entry["error"]})
        elif kind == "pushback":
            events.append({"turn": len(turns), "event": "pushback", "reason": clip(e.get("reason") or "", n)})
    # The agent's last message is its own account of what it did and did not do — the one message a diagnosis
    # most needs whole — so it keeps up to the task text's limit rather than a single entry's.
    for e in reversed(transcript):
        if e.get("kind") == "assistant" and turns:
            turns[-1]["text"] = clip(e.get("text") or "", limits.max_task_chars)
            break
    # Every scored assertion with its outcome, so R sees what the agent did complete as well as what it missed.
    # Excluded ones (declared unscored, or true at the start and still true) never counted and are left out.
    checks = [{"type": a.get("type"), "params": a.get("params"), "passed": bool(a.get("passed"))}
              for a in grading if not a.get("excluded")]
    return {"task_id": result.task_id, "family_id": result.family_id, "attempt_id": result.attempt_id,
            "task_text": clip(task_text, limits.max_task_chars), "termination": result.termination,
            "partial_credit": result.partial_credit, "turns": turns[-limits.max_turns:],
            "turns_total": len(turns), "tool_errors": errors, "events": events, "checks": checks}


def failure_summaries(results, *, store, transcripts: Path, limits: DiagnosisLimits) -> list[dict]:
    """One summary per failed train task: its latest valid failed attempt. Dev and final attempts are skipped
    here and refused by failure_summary if one slips through."""
    latest: dict = {}
    for r in results:
        if r.split in TRAIN and r.validity == "valid" and not r.success:
            latest[r.task_id] = r
    return [failure_summary(r, read_transcript(transcripts, r.evidence), store.grading(r.attempt_id), limits)
            for r in latest.values()]


def select_failures(summaries: list[dict], *, order: list[str], last_included: dict, cap: int) -> tuple:
    """-> (selected, not selected). Least recently included first — a failure never included comes first — and
    ties in the frozen task order, so the tail of a long failure list is not starved round after round."""
    position = {t: i for i, t in enumerate(order)}
    ranked = sorted(summaries, key=lambda s: (last_included.get(s["task_id"], -1),
                                              position.get(s["task_id"], len(position)), s["task_id"]))
    return ranked[:max(cap, 0)], ranked[max(cap, 0):]


def last_included(experience, scope: str | None = None) -> dict:
    """task -> the position of the latest diagnosis that read a note on it. Positions count diagnosis records
    across the whole activity, in log order, so a restart or a new improve call does not reset them and the
    least-recently-read ordering holds from one session to the next."""
    out: dict = {}
    position = 0
    for row in experience or ():
        if row.get("kind") == "diagnosis" and (scope is None or row.get("scope") == scope):
            for task in row.get("included") or ():
                out[task] = position
            position += 1
    return out


def diagnosis_count(experience, scope: str | None = None) -> int:
    return sum(1 for row in experience or () if row.get("kind") == "diagnosis"
               and (scope is None or row.get("scope") == scope))


REASON_KINDS = (("pairs are incomplete", "incomplete_pairs"), ("evaluator failure", "evaluator_failure"),
                ("candidate defect", "candidate_defect"), ("below the plan's min_gain", "below_min_gain"),
                ("regress", "regression"), ("budget", "budget_exhausted"), ("infrastructure", "infrastructure"),
                ("invalid", "invalid"))


def experience_projection(rows) -> list[dict]:
    """What the summary call may read of the experience log: fixed enums and aggregate figures only. Receipt
    reasons can name dev tasks and carry the scorer's error text, so they are mapped to fixed categories with
    counts and never passed on (docs/modules/04_RESEARCH_LOOP.md §1, docs/modules/00_CONTRACTS.md §4)."""
    out = []
    for row in rows or ():
        kind = row.get("kind")
        entry = {"kind": kind, "outcome": row.get("outcome") or row.get("verdict"),
                 "installed": row.get("installed"), "hypothesis": str(row.get("hypothesis") or "")[:500]}
        if kind == "experiment":
            entry["targets"] = sorted({c.get("target") for c in row.get("changes") or () if isinstance(c, dict)})
            metrics = row.get("metrics") or {}
            entry["metrics"] = {k: metrics.get(k) for k in ("gain", "regression_rate", "n_families", "n_pairs")
                                if k in metrics}
            reasons = " ".join(str(r) for r in row.get("reasons") or ())
            entry["reason_kinds"] = sorted({k for needle, k in REASON_KINDS if needle in reasons.lower()})
        if kind == "diagnosis":
            entry.update({k: row.get(k) for k in ("noted", "failures", "reused", "measured", "passed")})
        if kind == "collection":
            entry.update({k: row.get(k) for k in ("batch", "valid", "attempts")})
        out.append({k: v for k, v in entry.items() if v not in (None, "", [], {})})
    return out


# ---------------------------------------------------------------------------- notes and their identity
NOTE_FIELDS = ("failure_class", "root_cause", "mechanism", "component", "generality")
NOTE_CHARS = 800


def invalid_note(note: dict, components) -> str:
    """Why a note is unusable, or "". A note names its failure and mechanism, and a component K allows."""
    if not note.get("failure_class") or not note.get("mechanism"):
        return "the note names no failure class or mechanism"
    if components and note.get("component") not in components:
        return f"component {note.get('component')!r} is not one of {list(components)}"
    return ""


def note_identity(system: str, user: str, model: dict) -> str:
    return ct.digest({"system": system, "user": user, "model": model})


@dataclass
class NoteCache:
    """<workspace>/rsi/notes.jsonl: identity -> note ref. Notes themselves are content-addressed objects.

    `shared` is a read-only cache of notes prepared for more than one branch (a campaign's first round): a hit
    there is copied into this workspace and recorded as a shared input, never written back."""
    root: Path
    objects: ct.Objects
    shared: "NoteCache | None" = None

    @property
    def path(self) -> Path:
        return Path(self.root) / "rsi" / "notes.jsonl"

    def get(self, identity: str) -> str | None:
        for row in _read_jsonl(self.path):
            if row.get("identity") == identity:
                return row["note_ref"]
        if self.shared is not None:
            ref = self.shared.get(identity)
            if ref is not None:
                ct.copy_closure(self.shared.objects, self.objects, [ref])
                _append_jsonl(self.path, {"ts": now(), "identity": identity, "note_ref": ref, "shared": True})
                return ref
        return None

    def put(self, identity: str, note: dict, summary: dict) -> str:
        ref = self.objects.put_json("note", {"identity": identity, "task_id": summary["task_id"],
                                             "attempt_id": summary.get("attempt_id"), "partition": "train",
                                             "note": note})
        _append_jsonl(self.path, {"ts": now(), "identity": identity, "note_ref": ref})
        return ref


@dataclass
class Diagnosis:
    """The fixed part of one round's diagnosis: which failures, which limits, which cache, which model."""
    summaries: list = field(default_factory=list)
    order: list = field(default_factory=list)
    history: dict = field(default_factory=dict)
    limits: DiagnosisLimits = field(default_factory=DiagnosisLimits)
    cache: NoteCache | None = None
    model: dict = field(default_factory=dict)
    round_index: int = 0
    scope: str = "train failures of the latest measurement"     # the working set's description
    measured: int = 0                  # train tasks in the working set
    passed: int = 0                    # of those, passed
    components: tuple = ()            # the kinds K allows, injected into every note request
    package: dict = field(default_factory=dict)      # the current package's files, for the summary call

    def run(self, researcher, prompt: str) -> tuple[list[dict], dict]:
        """-> (notes, coverage). One research.diagnose request per selected failure; a cached note is reused only
        on its full identity. Coverage says how many of the working set's failures the notes cover."""
        selected, rest = select_failures(self.summaries, order=self.order, last_included=self.history,
                                         cap=self.limits.max_notes)
        notes, reused, invalid = [], 0, []
        for summary in selected:
            system, user = researcher.note_request(summary, prompt, allowed=tuple(self.components))
            identity = note_identity(system, user, self.model)
            ref = self.cache.get(identity) if self.cache is not None else None
            if ref is not None:
                reused += 1
                note = self.cache.objects.get(ref)["note"]
            else:
                raw = researcher.note(system, user)
                note = {k: str((raw or {}).get(k, ""))[:NOTE_CHARS] for k in NOTE_FIELDS}
                # why the reply could not be read at all (refused, empty, not JSON), when the researcher says
                asked = (getattr(researcher, "requests", None) or [{}])[-1]
                unread = asked.get("unreadable") if isinstance(asked, dict) and asked.get("step") == "note" else ""
                problem = unread or invalid_note(note, self.components)
                if problem:
                    # Not cached: a formatting slip must not blank this failure's diagnosis for good.
                    invalid.append({"task_id": summary["task_id"], "problem": problem})
                    continue
                ref = self.cache.put(identity, note, summary) if self.cache is not None else None
            notes.append({"task_id": summary["task_id"], "note_ref": ref, **note})
        coverage = {"noted": len(notes), "failures": len(self.summaries), "reused": reused,
                    "invalid": invalid, "included": [s["task_id"] for s in selected],
                    "not_included": [s["task_id"] for s in rest], "working_set": self.scope,
                    "measured": self.measured, "passed": self.passed, "round": self.round_index}
        return notes, coverage


def train_literals(store) -> set:
    """The forbidden literals of the draft check: task-specific values in the assertion params of train attempts,
    read from the grading store's raw record (docs/modules/00_CONTRACTS.md §3)."""
    from ..agent.harness_package import literals_of
    out: set = set()
    for row in _read_jsonl(store.grading_path()):
        if row.get("split") in TRAIN:
            out |= literals_of([a.get("params") for a in row.get("assertions") or ()])
    return out
