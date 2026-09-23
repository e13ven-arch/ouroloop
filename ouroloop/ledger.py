"""Append-only JSONL ledger of decisions, escalations and outcomes (PLAN §2.4), plus the context store
that lets a changed view be re-rendered offline, and the champion spec store."""
from __future__ import annotations

import json
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Labels from these sources may become training targets (with this weight). Jev outputs and frontier-LLM
# answers never do: they are kept for evaluation only.
SOURCE_WEIGHTS = {"env": 1.0, "human": 1.0, "open_llm": 0.5}
EVAL_ONLY = {"frontier_llm", "jev"}
SOURCES = set(SOURCE_WEIGHTS) | EVAL_ONLY

# Applied to everything ouroloop writes: ledger, session contexts, transcripts, run records, experience.
_SECRETS = [re.compile(p) for p in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"(?i)\b\w*(api[_-]?key|access[_-]?key|token|secret|passw(or)?d|credential)\w*[\"']?\s*[=:]\s*"
    r"(?![\d.,]+(\s|$))\S+",                                 # a number (max_tokens: 4096) is no secret
    r"sk-[A-Za-z0-9_\-]{16,}",
    r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}",
    r"AKIA[0-9A-Z]{16}",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}",
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    r"\b[0-9a-f]{32}\.[A-Za-z0-9]{16}\b",                  # Zhipu API keys
)]


def redact(obj: Any) -> Any:
    if isinstance(obj, str):
        for rx in _SECRETS:
            obj = rx.sub("[REDACTED]", obj)
        return obj
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000):x}{uuid.uuid4().hex[:6]}"


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> Iterator[dict]:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


@dataclass
class Row:
    """One decision joined with what happened afterwards."""
    decision: dict
    escalations: list[dict] = field(default_factory=list)
    outcomes: list[dict] = field(default_factory=list)

    def target(self) -> tuple[dict[str, float], float] | None:
        """The training target (a label distribution) and its weight, or None if nothing trainable was observed.
        Outcomes win over escalations; conflicting signals become a weighted distribution."""
        for events in (self.outcomes, self.escalations):
            votes: dict[str, float] = defaultdict(float)
            for ev in events:
                if ev.get("label") is not None and ev.get("source") in SOURCE_WEIGHTS:
                    votes[str(ev["label"])] += SOURCE_WEIGHTS[ev["source"]] * float(ev.get("weight", 1.0))
            total = sum(votes.values())
            if total > 0:
                return {lab: v / total for lab, v in votes.items()}, min(1.0, total)
        return None


class Ledger:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def append(self, event: dict) -> dict:
        event = redact({"ts": now(), **event})
        _append_jsonl(self.root / f"{event['ts'][:10]}.jsonl", event)
        return event

    def decision(self, did: str | None = None, **fields) -> str:
        did = did or new_id("d")
        self.append({"type": "decision", "id": did, **fields})
        return did

    def escalation(self, decision: str, label: str | None, probs: dict | None, by: str, source: str) -> None:
        if source not in SOURCES:
            raise ValueError(f"unknown label source {source!r}")
        self.append({"type": "escalation", "decision": decision, "label": label, "probs": probs, "by": by,
                     "source": source})

    def outcome(self, decision: str, label: str | None, source: str = "env", weight: float = 1.0, **extra) -> None:
        if source not in SOURCES:
            raise ValueError(f"unknown label source {source!r}")
        self.append({"type": "outcome", "decision": decision, "label": label, "source": source, "weight": weight,
                     **extra})

    def events(self) -> Iterator[dict]:
        for path in sorted(self.root.glob("*.jsonl")):
            yield from _read_jsonl(path)

    def rows(self, point: str | None = None) -> list[Row]:
        rows: dict[str, Row] = {}
        later: list[dict] = []
        for ev in self.events():
            if ev["type"] == "decision":
                if point is None or ev.get("point") == point:
                    rows[ev["id"]] = Row(ev)
            else:
                later.append(ev)
        for ev in later:
            row = rows.get(ev.get("decision"))
            if row is not None:
                (row.outcomes if ev["type"] == "outcome" else row.escalations).append(ev)
        return list(rows.values())

    def training_rows(self, point: str | None = None) -> list[tuple[Row, dict[str, float], float]]:
        """Rows that carry a trainable target. Decision probabilities are never targets, whatever the backend."""
        out = []
        for row in self.rows(point):
            t = row.target()
            if t is not None:
                out.append((row, *t))
        return out


class SessionStore:
    """Raw contexts by session and step, so a changed view function can be re-rendered offline."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._cache: dict[str, dict[int, Any]] = {}

    def _load(self, session: str) -> dict[int, Any]:
        if session not in self._cache:
            self._cache[session] = {e["step"]: e["ctx"] for e in _read_jsonl(self.root / f"{session}.jsonl")}
        return self._cache[session]

    def put(self, session: str, ctx: Any, step: int | None = None) -> str:
        steps = self._load(session)   # a reopened workspace continues its sessions' step numbers
        if step is None:
            step = max(steps, default=-1) + 1
        ctx = redact(ctx)
        _append_jsonl(self.root / f"{session}.jsonl", {"step": step, "ctx": ctx})
        steps[step] = ctx
        return f"{session}#{step}"

    def get(self, ref: str) -> Any:
        session, _, step = ref.rpartition("#")
        return self._load(session)[int(step)]


class CalibrationStore:
    """Per-point temperature and threshold, fitted on the calibration split (PLAN §4.5)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def get(self, point: str) -> dict:
        return self.data.get(point, {})

    def set(self, point: str, entry: dict) -> None:
        self.data[point] = {**entry, "ts": now()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


class SpecStore:
    """The current champion spec of each decision point, with its history."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def get(self, point: str) -> dict | None:
        entry = self.data.get(point)
        return entry["spec"] if entry else None

    def set(self, point: str, spec: dict, note: str = "") -> None:
        entry = self.data.setdefault(point, {"spec": None, "history": []})
        entry["history"].append({"ts": now(), "spec": spec, "note": note})
        entry["spec"] = spec
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
