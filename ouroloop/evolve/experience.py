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
                      open_questions=(), targets=(), tags=(), trained: bool = False, notes: str = "") -> dict:
    """The facts of one experiment, whether or not it was installed.

    What went in, what was actually assembled, what it did while running, what the full-task comparison said, what
    regressed, what it cost, and what is still open. `verdict` and `installed` stay separate fields because they
    answer different questions, and a later reader that only had one of them would have to guess the other.
    """
    # Everything the receipt measured except its per-task regression list: that names dev tasks, and it stays with
    # the receipt, where a report reads it (docs/modules/00_CONTRACTS.md §4). The rate is kept. The per-domain
    # breakdown of a multi-domain gate (v0.3 step 5) stays with the receipt too: the report lists it, and R's view
    # of dev stays the overall gain and the failure-type histogram.
    metrics = {k: v for k, v in (getattr(receipt, "metrics", None) or {}).items()
               if k not in ("regressions", "per_domain")}
    return {"kind": "experiment", "experiment": experiment_id, "proposal": proposal_ref, "owner": owner,
            "hypothesis": hypothesis, "changes": [dict(c) for c in changes],
            "requirements": [str(r) for r in requirements], "targets": sorted(targets or
                                                                             {c["target"] for c in changes}),
            "tags": sorted(tags or ()),
            "build": getattr(build, "status", None), "trained": bool(trained),
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


# ---------------------------------------------------------------------------- the engine-owned ledger (M4 §5, v0.3)
# One candidate, several rows: the log is append-only, so each stage of a candidate — declared, drafted, admitted,
# screened, gated — is its own `kind="ledger"` row with the candidate's id, and `ledger_rows` folds them into one
# entry per candidate. Only fixed code writes them (the round driver, research/session.py): the package has no
# handle on E in the worker and the session has no tool that writes outside its candidate directories, so every
# conclusion here carries a reading ref a reviewer can check against the host-written records.
LEDGER_STAGES = ("declared", "drafted", "admitted", "screened", "gated")
# The candidate outcomes that make a later candidate on the same normalised (mechanism, site) a repeat the
# admission refuses without `differs_from` (M4 §5 准入 repeated_mechanism). An infrastructure end, an inconclusive
# gate or a declaration that never reached a verdict say nothing about the mechanism and trigger nothing.
REPEAT_OUTCOMES = ("rejected", "screened_out")
DEV_TASK_LISTS = ("fixed", "regressed", "still_failing")      # the gate stage's per-task lists: dev task ids


def ledger_row(stage: str, candidate: str, **fields) -> dict:
    """One ledger row, as the round driver appends it (`append`). A screen row's outcome is one of SCREEN_OUTCOMES:
    any other value is refused here, where every row is made, rather than folded as something it is not."""
    if stage not in LEDGER_STAGES:
        raise ValueError(f"ledger stage {stage!r}: one of {list(LEDGER_STAGES)}")
    if stage == "screened" and fields.get("outcome") not in SCREEN_OUTCOMES:
        raise ValueError(f"screen outcome {fields.get('outcome')!r}: one of {list(SCREEN_OUTCOMES)}")
    return {"kind": "ledger", "stage": stage, "candidate": candidate, **fields}


# The screen stage's outcomes (M4 §2, v0.3, 2026-10-05): the one that pays the gate, those that cleared the floor
# but lost the pick, those below it, those with no reading of their own making, those whose attempts were lost to
# infrastructure or the evaluator (refill included; not the candidate's fault, no repeat trigger), and the
# placeholder a screen carries until the pick — written as such when the round is interrupted after paying it
# (folded as pending: the round runs again). The round driver refuses any other value when it writes the row.
SCREEN_OUTCOMES = ("selected", "not_selected", "screened_out", "invalid", "lost", "interrupted")


def ledger_rows(entries, scope: str | None = None) -> list[dict]:
    """The ledger folded: one entry per candidate, in the order the candidates were declared, each carrying its
    declaration, its drafts, its admission, its screen and its gate as the stages recorded them, and `outcome` — the
    candidate's end as the record stands: `dropped` / `declaration_mismatch` (never a draft), `invalid` (a directory
    the worker could not hand back, a failed last draft, a refused admission or a screen with no reading of its own making), `blocked` (admission answered
    collect or repair_runtime: not the candidate's fault), `screened_out`, `not_selected`, `lost` (the screen's
    attempts lost to infrastructure or the evaluator), the gate's verdict or `installed`, else `pending`.
    `proposal_ref` comes from the admitted row, where it is written."""
    out: dict[str, dict] = {}
    for row in entries or ():
        if row.get("kind") != "ledger" or (scope is not None and row.get("scope") not in (None, scope)):
            continue
        entry = out.setdefault(row["candidate"], {"candidate": row["candidate"], "outcome": "pending"})
        stage = row.get("stage")
        if stage == "declared":
            entry.update({k: row.get(k) for k in ("scope", "round", "ordinal", "release", "research_ref", "dir",
                                                  "declaration", "declaration_ref", "diff", "carrier")})
            if row.get("fixed_code"):
                entry["fixed_code"] = True      # v0.5: a candidate fixed code declared (research.fixed)
            if row.get("status") in ("dropped", "declaration_mismatch", "invalid"):
                entry["outcome"] = row["status"]
                entry["reason"] = row.get("reason")
        elif stage == "drafted":
            entry["drafts"] = list(row.get("drafts") or ())
            entry["package_ref"] = row.get("package_ref")
            if row.get("status") == "invalid":
                entry["outcome"], entry["reason"] = "invalid", row.get("reason")
        elif stage == "admitted":
            entry["admission"] = {"status": row.get("status"), "reasons": list(row.get("reasons") or ())}
            if row.get("proposal_ref"):
                entry["proposal_ref"] = row["proposal_ref"]
            if row.get("status") in ("collect", "repair_runtime"):
                entry["outcome"], entry["reason"] = "blocked", "; ".join(row.get("reasons") or ())[:300]
            elif row.get("status") != "admitted":
                entry["outcome"], entry["reason"] = "invalid", "; ".join(row.get("reasons") or ())[:300]
        elif stage == "screened":
            entry["screen"] = {k: row.get(k) for k in ("tasks", "failing", "sample", "before", "after", "net_flips",
                                                       "cost_per_attempt", "thin_screen", "outcome", "reason",
                                                       "lost_to")}
            # v0.4 (docs/design/V0_4_PLAN.md §1.6): the screen's fixes and sample regressions, where recorded
            entry["screen"].update({k: row[k] for k in ("fixes", "sample_regressions") if k in row})
            for k in ("experiment_id", "proposal_ref"):
                if row.get(k):
                    entry[k] = row[k]
            if row.get("outcome") in ("screened_out", "not_selected", "invalid", "lost"):
                entry["outcome"] = row["outcome"]
                if row.get("reason"):
                    entry["reason"] = row["reason"]
        elif stage == "gated":
            entry["gate"] = {k: row.get(k) for k in ("verdict", "receipt", "reading_kind", "gain", "control_readings",
                                                     "cost_per_attempt", *DEV_TASK_LISTS)}
            if row.get("by_domain"):
                entry["gate"]["by_domain"] = row["by_domain"]      # an arm's E0: the counts stand for the lists
            entry["outcome"] = "installed" if row.get("installed") else str(row.get("verdict") or "pending")
    return list(out.values())


def gate_by_domain(gate: dict, per_domain: dict | None = None) -> dict:
    """A gate's outcome per upstream domain (M4 §1 台账投影, v0.3 step 5, 2026-10-06): the fixed / regressed /
    still_failing counts per domain — from the per-task lists the activity's ledger keeps (their ids' `domain.`
    prefix), or the counts an arm's E0 carries in `by_domain` — and, from the receipt's `metrics.per_domain`, each
    domain's two dev means and their difference. Counts and means only, never a task id; {} for a gate over one
    domain, or over ids that name none."""
    out: dict = {}
    stored = gate.get("by_domain")
    if isinstance(stored, dict):
        out = {d: dict(v) for d, v in stored.items()}
    else:
        for cls in DEV_TASK_LISTS:
            value = gate.get(cls)
            if not isinstance(value, list):
                continue
            for task in value:
                if "." not in str(task):
                    return {}
                counts = out.setdefault(str(task).split(".", 1)[0], {k: 0 for k in DEV_TASK_LISTS})
                counts[cls] += 1
    for d, m in (per_domain or {}).items():
        if isinstance(m, dict):
            row = out.setdefault(d, {k: 0 for k in DEV_TASK_LISTS})
            row.update({k: m.get(k) for k in ("subject", "control", "gain") if k in m})
    return out if len(out) > 1 else {}


def ledger_projection(rows: list[dict], objects=None) -> list[dict]:
    """What R reads of the ledger (M4 §1 台账投影, train-restricted): every candidate's declaration, how its drafts
    and admission went, its screen result with the train task ids and net flips, its verdict, and the gate's three
    per-task classes as counts only — dev task ids never appear, test never does. The declaration ref is shown so a
    later candidate can cite the row it `differs_from`. Over several domains (2026-10-06) the gate also shows each
    domain's counts and dev means (`gate_by_domain`), the means read off the receipt when `objects` holds it."""
    out = []
    for e in rows:
        d = e.get("declaration") or {}
        entry = {"candidate": e.get("candidate"), "round": e.get("round"), "outcome": e.get("outcome"),
                 "declaration_ref": e.get("declaration_ref"),
                 "declaration": {k: d.get(k) for k in ("mechanism", "site", "carrier", "trigger_condition",
                                                       "differs_from", "predicted_affected")
                                 if d.get(k) not in (None, [], "")}}
        if e.get("reason"):
            entry["reason"] = str(e["reason"])[:300]
        if e.get("drafts") is not None:
            entry["drafts"] = {"checked": len(e["drafts"]), "passed": any(x.get("ok") for x in e["drafts"])}
        if e.get("admission"):
            entry["admission"] = e["admission"]["status"]
        if e.get("screen"):
            s = e["screen"]
            entry["screen"] = {k: s.get(k) for k in ("tasks", "net_flips", "cost_per_attempt", "thin_screen",
                                                     "outcome", "lost_to") if k != "lost_to" or s.get(k)}
            # v0.4 (V0_4_PLAN §1.6): train counts R can read its screen by — fixes, and the harm on passing tasks
            entry["screen"].update({k: s[k] for k in ("fixes", "sample_regressions") if k in s})
        if e.get("gate"):
            g = e["gate"]
            entry["gate"] = {**{k: g.get(k) for k in ("verdict", "reading_kind", "gain", "control_readings",
                                                      "cost_per_attempt")},
                             # a list of dev task ids in the activity's own ledger; already a count in an arm's E0
                             **{k: _count(g.get(k)) for k in DEV_TASK_LISTS}}
            per_domain = None
            receipt = g.get("receipt")
            if objects is not None and receipt and objects.has(receipt):
                per_domain = (objects.get(receipt).get("metrics") or {}).get("per_domain")
            split = gate_by_domain(g, per_domain)
            if split:
                entry["gate"]["by_domain"] = split
        out.append(entry)
    return out


def _count(value) -> int:
    return int(value) if isinstance(value, int) else len(value or ())


def strip_dev_task_ids(row: dict) -> dict:
    """A ledger row as it may enter an arm's E0 (M6 §2, v0.3): the gate stage's per-task lists become counts, since
    the first half of dev is what both arms then optimise on and the second half is the outer pool. Other rows are
    returned as they are."""
    if row.get("kind") != "ledger" or row.get("stage") != "gated":
        return row
    split = gate_by_domain(row)
    return {**row, **{k: len(row.get(k) or ()) for k in DEV_TASK_LISTS if k in row},
            **({"by_domain": split} if split else {})}


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
