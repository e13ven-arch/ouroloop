"""Experience records: one line per evaluated candidate. Not promoted does not mean useless."""
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


# ---------------------------------------------------------------------------- the task/research profile
# docs/modules/04_RESEARCH_LOOP.md §3. One line per experiment, installed or not: an experiment that was rejected
# is what keeps the next round from proposing the same thing, so "not promoted" is not "not worth recording".
SUMMARY_STATUS = ("supported", "negative", "regression", "inconclusive", "expired")


def experiment_record(*, experiment_id: str, proposal_ref: str, owner: str, hypothesis: str, changes,
                      requirements=(), build=None, receipt=None, installation=None, activity=None,
                      release_before: str = "", release_after: str = "", costs=None, evidence_refs=(),
                      open_questions=(), targets=(), tags=(), notes: str = "") -> dict:
    """The facts of one experiment, whether or not it was installed.

    What went in, what was actually assembled, what it did while running, what the full-task comparison said, what
    regressed, what it cost, and what is still open. `verdict` and `installed` stay separate fields because they
    answer different questions, and a later reader that only had one of them would have to guess the other.
    """
    metrics = dict(getattr(receipt, "metrics", None) or {})
    return {"kind": "experiment", "experiment": experiment_id, "proposal": proposal_ref, "owner": owner,
            "hypothesis": hypothesis, "changes": [dict(c) for c in changes],
            "requirements": [str(r) for r in requirements], "targets": sorted(targets or
                                                                             {c["target"] for c in changes}),
            "tags": sorted(tags or ()),
            "build": getattr(build, "status", None), "trained": bool(getattr(build, "artifacts", {}) or {}),
            "activity": dict(activity or {}),
            "verdict": getattr(receipt, "verdict", None), "receipt": receipt.ref() if receipt else None,
            "reasons": list(getattr(receipt, "reasons", ()) or ()),
            "metrics": metrics, "regression_rate": metrics.get("regression_rate"),
            "uncertainty": dict(getattr(receipt, "uncertainty", None) or {}),
            "installed": getattr(installation, "status", None) == "installed",
            "installation": getattr(installation, "status", None),
            "release_before": release_before, "release_after": release_after,
            "costs": dict(costs or {}), "evidence_refs": list(evidence_refs),
            "open_questions": list(open_questions), "notes": notes}


def summary_record(*, summary_ref: str, source_refs, producer_policy_ref: str, applicable_to: dict,
                   status: str, counterexamples=(), text: str = "") -> dict:
    """A derived summary. The producer is kept because the words may be a model's; the numbers are the
    framework's, and a summary is never evidence in its own right (docs/modules/04_RESEARCH_LOOP.md §3)."""
    if status not in SUMMARY_STATUS:
        raise ValueError(f"summary status {status!r}: one of {list(SUMMARY_STATUS)}")
    return {"kind": "summary", "summary_ref": summary_ref, "source_refs": list(source_refs),
            "producer_policy_ref": producer_policy_ref, "applicable_to": dict(applicable_to), "status": status,
            "counterexamples": list(counterexamples), "text": text}


def snapshot(root: str | Path) -> tuple[list[dict], str]:
    """The experience a round may read, and a ref for it: the round's visible snapshot is fixed, so a proposal can
    be traced to what was readable when it was made."""
    from .contracts import digest
    entries = read(root)
    return entries, f"experience:{digest([e for e in entries])[:16]}"


def requirement_status(entries: list[dict], requirement_id: str) -> str:
    """What the history says about one requirement: addressed, regressed, or still open.

    `addressed` only ever means "development evidence supported it": a later counterexample appends a regression
    and the original conclusion stays on record rather than being erased.
    """
    status = "open"
    for entry in entries:
        if requirement_id not in (entry.get("requirements") or ()):
            continue
        if entry.get("kind") == "summary":
            status = {"regression": "regressed", "supported": "addressed"}.get(entry.get("status"), status)
        elif entry.get("installed") and entry.get("verdict") == "accepted":
            status = "addressed"
        elif entry.get("regression_rate"):
            status = "regressed"
    return status
