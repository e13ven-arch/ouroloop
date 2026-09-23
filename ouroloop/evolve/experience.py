"""Experience records: one line per evaluated candidate (PLAN §4.8). Not promoted does not mean useless."""
from __future__ import annotations

from pathlib import Path

from ..ledger import _append_jsonl, _read_jsonl, now, redact


def verdict(deltas: list[float], tol: float = 1e-9) -> str:
    """What changed item by item (negative delta = better): improved, regressed, mixed or unchanged."""
    gains = any(d < -tol for d in deltas)
    losses = any(d > tol for d in deltas)
    return "mixed" if gains and losses else "improved" if gains else "regressed" if losses else "unchanged"


def append(root: str | Path, record: dict) -> None:
    _append_jsonl(Path(root) / "experience.jsonl", redact({"ts": now(), **record}))


def read(root: str | Path) -> list[dict]:
    return list(_read_jsonl(Path(root) / "experience.jsonl"))
