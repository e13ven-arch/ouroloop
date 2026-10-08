"""Append-only JSONL ledger of sessions, decisions, escalations and outcomes, plus the context store that lets a changed
view be re-rendered offline. (The champion's calibration and spec stores went with the champion pointer in P1 step 5;
tag archive-2026-10-08.)"""
from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Data lineage and selection (docs/DESIGN.md §8, invariant 6): `source` says where an answer or a label came from,
# and which sources a fit, a threshold or an evaluation uses is the data plan's choice. Until v2.0 replaces this gate
# and its defaults with that selection (docs/design/V2_TRAINING_PIPELINE.md §2.5, §3.8, §7), the ledger still gates
# by source: only what the environment and people say becomes a training target here, and answers of any model or
# LLM, and labels derived from them such as a promotion gate's verdict, are kept for evaluation only.
SOURCE_WEIGHTS = {"env": 1.0, "human": 1.0}
EVAL_ONLY = {"frontier_llm", "open_llm", "jev", "gate"}
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


_APPEND = threading.Lock()     # concurrent attempts append from several threads; a line is written whole


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _APPEND, open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _read_jsonl(path: Path) -> Iterator[dict]:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


@contextmanager
def locked(path: Path):
    """An exclusive lock on `path` (through a .lock file beside it), shared by every process that writes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(f"{path.name}.lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def write_json(path: Path, obj: Any) -> None:
    """Replace a JSON file in one step: a reader sees the old content or the new, never half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def file_key(path: Path) -> tuple | None:
    """Changes whenever the file is replaced or rewritten; None when there is none."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


@dataclass
class Row:
    """One decision joined with what happened afterwards."""
    decision: dict
    escalations: list[dict] = field(default_factory=list)
    outcomes: list[dict] = field(default_factory=list)

    def target(self) -> tuple[dict[str, float], float] | None:
        """The training target (a label distribution) and its weight, or None if nothing trainable was observed.
        Only outcomes count, never escalations; conflicting signals become a weighted distribution. An outcome that
        names several right labels splits its weight evenly among them."""
        votes: dict[str, float] = defaultdict(float)
        for ev in self.outcomes:
            if ev.get("label") is not None and ev.get("source") in SOURCE_WEIGHTS:
                labels = ev["label"] if isinstance(ev["label"], list) else [ev["label"]]
                for lab in labels:
                    votes[str(lab)] += SOURCE_WEIGHTS[ev["source"]] * float(ev.get("weight", 1.0)) / len(labels)
        total = sum(votes.values())
        return ({lab: v / total for lab, v in votes.items()}, min(1.0, total)) if total > 0 else None


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

    def outcome(self, decision: str, label: str | list[str] | None, source: str = "env", weight: float = 1.0,
                **extra) -> None:
        """What happened after `decision`: `label` is the right answer, a list of answers that are all right, or None
        when none was (no training or calibration reads it)."""
        if source not in SOURCES:
            raise ValueError(f"unknown label source {source!r}")
        if not (label is None or isinstance(label, str) or isinstance(label, list) and label
                and all(isinstance(x, str) for x in label) and len(set(label)) == len(label)):
            raise ValueError(f"label {label!r}: a string, None, or a non-empty list of distinct strings")
        self.append({"type": "outcome", "decision": decision, "label": label, "source": source, "weight": weight,
                     **extra})

    def events(self) -> Iterator[dict]:
        for path in sorted(self.root.glob("*.jsonl")):
            yield from _read_jsonl(path)

    def sessions(self) -> tuple[dict[str, dict], int]:
        """Every session by id: the fields of its session event (task, held_out, agent, release; M6 events carry the
        first three, a session without one has none), "start", the position of its first event in the ledger,
        "points", where it made decisions, and "labelled", where a decision with a stored context has a trainable
        outcome so far. And the number of events read: a session that starts later has a start at or past it.
        Held out is for good: a session any of whose events says so, and every session of a task any session of
        which was held out, whatever a later event says (docs/DESIGN.md invariant 3: never trained or calibrated on)."""
        out: dict[str, dict] = {}
        decided: dict[str, tuple[str, str]] = {}
        n = 0
        for n, ev in enumerate(self.events(), 1):
            if ev.get("type") == "outcome":
                s, point = decided.get(ev.get("decision"), (None, None))
                if s and ev.get("label") is not None and ev.get("source") in SOURCE_WEIGHTS:
                    out[s]["labelled"].add(point)
                continue
            s = ev.get("session")
            if s is None or ev.get("type") not in ("session", "decision"):
                continue
            info = out.setdefault(s, {"start": n - 1, "points": set(), "labelled": set()})
            if ev["type"] == "session":
                held = info.get("held_out") or ev.get("held_out")
                info.update({k: v for k, v in ev.items()
                             if k not in ("type", "session", "ts", "start", "points", "labelled")})
                if "held_out" in info:
                    info["held_out"] = bool(held)
            else:
                info["points"].add(ev.get("point"))
                if ev.get("ctx"):
                    decided[ev.get("id")] = (s, ev.get("point"))
        held_tasks = {info["task"] for info in out.values() if info.get("held_out") and info.get("task")}
        for info in out.values():
            if info.get("task") in held_tasks:
                info["held_out"] = True
        return out, n

    def rows(self, point: str | None = None) -> list[Row]:
        rows: dict[str, Row] = {}
        later: list[dict] = []
        for ev in self.events():
            if ev["type"] == "decision":
                if point is None or ev.get("point") == point:
                    rows[ev["id"]] = Row(ev)
            elif ev["type"] in ("outcome", "escalation"):     # not session events
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


_SESSION_ID = re.compile(r"\w[\w.@:-]{0,199}")


class SessionStore:
    """Raw contexts by session and step, so a changed view function can be re-rendered offline."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._cache: dict[str, dict[int, Any]] = {}

    def path(self, session: str, suffix: str = ".jsonl") -> Path:
        """A session's file here. The id comes from callers (serve's clients among them), so it may not name a path
        elsewhere, such as ../ledger/<day>, whose lines would then break every read of the ledger."""
        if not isinstance(session, str) or not _SESSION_ID.fullmatch(session):
            raise ValueError(f"session id {session!r}: up to 200 letters, digits and _ . @ : -, starting with a "
                             f"letter or digit")
        return self.root / f"{session}{suffix}"

    def _load(self, session: str) -> dict[int, Any]:
        if session not in self._cache:
            self._cache[session] = {e["step"]: e["ctx"] for e in _read_jsonl(self.path(session))}
        return self._cache[session]

    def put(self, session: str, ctx: Any, step: int | None = None) -> str:
        steps = self._load(session)   # a reopened workspace continues its sessions' step numbers
        if step is None:
            step = max(steps, default=-1) + 1
        ctx = redact(ctx)
        _append_jsonl(self.path(session), {"step": step, "ctx": ctx})
        steps[step] = ctx
        return f"{session}#{step}"

    def get(self, ref: str) -> Any:
        session, _, step = ref.rpartition("#")
        return self._load(session)[int(step)]
