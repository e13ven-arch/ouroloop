"""M7: the report (docs/modules/07_BENCH_REPORT.md §4).

A projection of records that already exist, and nothing else. It calls no model, it recomputes every number from
the attempts on disk, and running it twice on the same directory gives the same output — which is the only way a
number in a report can be checked later.

Three separations it keeps:

    Identity before results. What ran, on which tasks, with which versions and what was missing, before any score.
    Pass rate and partial credit never merge. A high partial credit with no completed task is exactly what the
    main metric is there to show.
    Proposed, accepted, installed and supported are four different states. A development acceptance is not a
    final claim, and a claim with no run is `not_run`, not a quiet omission.
"""
from __future__ import annotations

import json
from pathlib import Path

from .benchmark import Results, RunManifest, SplitManifest, summarise
from .evolve.task_gate import per_task
from .evolve import contracts as ct
from .evolve import experience as experience_mod
from .ledger import _read_jsonl, write_json

CLAIM_STATES = ("supported", "not_supported", "inconclusive", "not_run")
# A fixture's numbers are a fixture's. They are aggregated separately and labelled, so nothing can slide from a
# contract test into a business report (docs/modules/08_INTEGRATION_CASES.md §1).
SYNTHETIC = "synthetic_contract_test"


def build_report(root: str | Path, domain: str, *, protocol: str = "ouroloop.bench.result.v1") -> dict:
    root = Path(root)
    results = Results(root, domain)
    attempts = list(results.all())
    manifest = SplitManifest.load(results.manifest_path()) if results.manifest_path().exists() else None
    runs = _runs(results)
    objects = ct.Objects(root)
    events = ct.Events(root).all()
    by_run: dict[str, list] = {}
    for attempt in attempts:
        by_run.setdefault(attempt.run, []).append(attempt)
    arms = {}
    for run_hash, rows in sorted(by_run.items()):
        run = runs.get(run_hash)
        arms[run_hash] = {
            "candidate": rows[0].candidate, "role": getattr(run, "role", ""),
            "split": rows[0].split, "v2": bool(run and run.v2()),
            # Per task, so a claim can be judged the way the plan said: repeats averaged first, families kept.
            "per_task": per_task(rows), "families": {r.task_id: r.family_id for r in rows},
            "final": rows[0].split == "test",
            "model": getattr(run, "model", {}), "prompt_hash": getattr(run, "prompt_hash", ""),
            "release": getattr(run, "system_release_ref", None),
            "worker": getattr(run, "worker_ref", None), "research": getattr(run, "research_ref", None),
            "summary": summarise(rows),
            "invalid": [{"task": r.task_id, "class": r.failure_class, "reason": r.invalid_reason}
                        for r in rows if r.validity == "invalid"],
            "refills": [r.attempt_id for r in rows if r.supersedes],
        }
    receipts = _receipts(objects, events)
    finalization = _finalization(root, objects)
    installs = [e for e in events if e.get("event") in ("installed", "installation_failed", "stale_parent",
                                                        "rolled_back", "revoked")]
    entries = experience_mod.read(root)
    report = {
        "protocol": protocol,
        "identity": {
            "domain": domain,
            "splits": None if manifest is None else {"hash": manifest.hash(), "counts": manifest.counts(),
                                                     "rule": manifest.family_rule, "upstream": manifest.upstream},
            "runs": len(by_run), "attempts": len(attempts),
            "releases": sorted({a["release"] for a in arms.values() if a["release"]}),
            "missing": _missing(arms, manifest),
            "finalization": finalization,
        },
        "task_effect": {"arms": arms},
        "run_cost": {run: {"cost_usd": a["summary"]["cost_usd"], "cost_missing": a["summary"]["cost_missing"],
                           "usage": a["summary"]["usage"], "elapsed": a["summary"]["elapsed"],
                           "tool_calls": a["summary"]["tool_calls"],
                           "terminations": a["summary"]["terminations"]}
                     for run, a in arms.items()},
        "improvement_cost": _improvement_cost(root, entries),
        "improvement_evidence": {
            "experiments": [e for e in entries if e.get("kind") == "experiment"],
            "rounds": [e for e in entries if e.get("kind") == "round"],
            "receipts": receipts,
            "installations": installs,
        },
        "rsi_evidence": _rsi_evidence(objects, events, receipts),
        "claims": {},
    }
    report["claims"] = claim_states(report)
    return report


def _finalization(root: Path, objects: ct.Objects) -> dict | None:
    """The sealed final plan and its state, if this workspace has one. Without it there is no formal claim to
    make: a development comparison describes the search that produced the version, not the version."""
    directory = root / "rsi" / "finalization"
    for path in sorted(directory.glob("*.json")) if directory.exists() else ():
        state = json.loads(path.read_text(encoding="utf-8"))
        try:
            plan = objects.get(state["plan"])
        except (KeyError, ct.ContractError):
            continue
        return {"plan": state["plan"], "status": state.get("status"), "claims": plan.get("claims", []),
                "statistical_plan": plan.get("statistical_plan", {}), "task_ids": plan.get("task_ids", []),
                "repeats": plan.get("repeats", 1), "attempts": len(state.get("attempts", []))}
    return None


def _runs(results: Results) -> dict:
    out = {}
    directory = results.root / "runs"
    for path in sorted(directory.glob("*.json")) if directory.exists() else ():
        try:
            run = RunManifest.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError):
            continue
        out[run.hash()] = run
    return out


def _receipts(objects: ct.Objects, events: list) -> list:
    out = []
    for event in events:
        if event.get("event") != "evaluation_completed" or not event.get("receipt"):
            continue
        try:
            receipt = objects.load(event["receipt"], ct.EvaluationReceipt)
        except (KeyError, ct.ContractError):
            continue
        out.append({"ref": event["receipt"], "profile": receipt.profile, "verdict": receipt.verdict,
                    "subject": receipt.subject_ref, "control": receipt.control_ref, "plan": receipt.plan_ref,
                    "metrics": receipt.metrics, "uncertainty": receipt.uncertainty, "costs": receipt.costs,
                    "reasons": list(receipt.reasons)})
    return out


def _missing(arms: dict, manifest) -> list:
    missing = []
    roles = {a["role"] for a in arms.values()}
    for role in ("base", "evolved"):
        if role not in roles:
            missing.append(f"no {role} run on record")
    if not any(r.startswith("frontier") for r in roles):
        missing.append("no frontier run on record: C2 cannot be complete")
    elif len([r for r in roles if r.startswith("frontier")]) < 2:
        missing.append("only one frontier vendor on record: C2 is incomplete")
    if any(not a["v2"] for a in arms.values()):
        missing.append("some runs predate the v2 manifest and carry no release identity")
    return missing


def _improvement_cost(root: Path, entries: list) -> dict:
    """What the improving cost, by scope, from the budget book. Unknown stays unknown; nothing is amortised here
    because the assumption behind an amortisation belongs to whoever publishes it."""
    path = Path(root) / "rsi" / "budget" / "book.json"
    if not path.exists():
        return {"scopes": {}, "note": "no budget book in this workspace"}
    book = json.loads(path.read_text(encoding="utf-8"))
    per_scope: dict = {}
    for reservation in book.get("reservations", {}).values():
        scope = per_scope.setdefault(reservation["scope_id"], {"settled": {}, "unknown": 0, "breaches": 0})
        if reservation["status"] == "settled":
            for key, value in (reservation.get("measured") or {}).items():
                scope["settled"][key] = round(scope["settled"].get(key, 0) + float(value), 6)
        elif reservation["status"] == "unknown":
            scope["unknown"] += 1
    for breach in book.get("breaches", []):
        per_scope.setdefault(breach["scope_id"], {"settled": {}, "unknown": 0, "breaches": 0})["breaches"] += 1
    return {"scopes": per_scope, "experiments": sum(1 for e in entries if e.get("kind") == "experiment"),
            "failed_experiments": sum(1 for e in entries if e.get("kind") == "experiment"
                                      and e.get("verdict") in ("rejected", "invalid", "inconclusive")),
            "note": "settled measurements only; unknown reservations are counted, not priced"}


def _rsi_evidence(objects: ct.Objects, events: list, receipts: list) -> dict:
    """The R0 -> R1 chain: who proposed it, what compared it, whether it was installed and what it then produced.

    `discovery` is carried through because it is the difference between a loop that works and a loop that found
    something: a change written by hand exercises the same path and proves nothing about discovery (RC-10).
    """
    research = [r for r in receipts if r["profile"] == "research"]
    installed = [e for e in events if e.get("event") == "installed"]
    return {"research_receipts": research,
            "installed_releases": [e.get("release") for e in installed],
            "discovery": sorted({r["metrics"].get("discovery") for r in research if r["metrics"].get("discovery")}),
            "campaign_arms": [e for e in events if e.get("event") == "campaign_arm"],
            "note": "a research policy that was installed but produced no worker proposal has not yet been shown "
                    "to drive the next round"}


def claim_states(report: dict) -> dict:
    """Each claim's state, from what is on record.

    Support comes from the sealed final plan's paired interval, in the direction the plan named, and from nothing
    else: a development acceptance describes the search that produced the version, and an interval containing zero
    is not a demonstrated gain (docs/modules/07_BENCH_REPORT.md §4).
    """
    arms = report["task_effect"]["arms"]
    receipts = report["improvement_evidence"]["receipts"]
    plan = (report.get("identity") or {}).get("finalization")
    by_role = {}
    for arm in arms.values():
        by_role.setdefault(arm["role"], []).append(arm)
    out = {}
    out["C1"] = _pair_claim(by_role.get("evolved"), by_role.get("base"), "iteration over the same base", plan)
    frontier = [a for role, rows in by_role.items() if role.startswith("frontier") for a in rows]
    if not frontier or not by_role.get("evolved"):
        out["C2"] = {"state": "not_run", "detail": "no frontier comparison on record"}
    else:
        complete = len({a["role"] for a in frontier}) >= 2
        out["C2"] = {**_pair_claim(by_role.get("evolved"), frontier, "position against leading models", plan),
                     "complete": complete}
        if not complete:
            out["C2"]["state"] = "inconclusive"
            out["C2"]["detail"] += "; fewer than two vendors, so C2 is incomplete"
    out["C3"] = _pair_claim(by_role.get("evolved"), by_role.get("prompt_best"),
                            "the decision model against the same prompt without it", plan)
    research = [r for r in receipts if r["profile"] == "research"]
    if not research:
        out["C4"] = {"state": "not_run", "detail": "no research campaign on record"}
    else:
        last = research[-1]
        supported = last["verdict"] == "accepted" and last["uncertainty"].get("significant")
        out["C4"] = {"state": "supported" if supported else
                     "not_supported" if last["verdict"] == "rejected" else "inconclusive",
                     "detail": f"{last['verdict']}: {'; '.join(last['reasons'])[:200]}",
                     "metrics": last["metrics"]}
    return out


def _pair_claim(subject, control, what: str, plan: dict | None = None) -> dict:
    """One claim's state. With final-split runs on both sides and a sealed plan, the paired interval decides."""
    if not subject or not control:
        return {"state": "not_run", "detail": f"{what}: both sides are not on record"}
    s, c = _rate(subject), _rate(control)
    if s is None or c is None:
        return {"state": "inconclusive", "detail": f"{what}: no valid attempts on one side"}
    final = [a for a in subject if a.get("final")], [a for a in control if a.get("final")]
    if plan and all(final):
        interval = _final_interval(final[0], final[1], plan)
        if interval["low"] is None:
            return {"state": "inconclusive", "detail": f"{what}: the final interval could not be computed",
                    "interval": interval}
        if interval["low"] > 0:
            return {"state": "supported",
                    "detail": f"{what}: paired difference {interval['point']}, interval "
                              f"[{interval['low']}, {interval['high']}] above zero on the sealed final plan",
                    "interval": interval}
        if interval["high"] < 0:
            return {"state": "not_supported",
                    "detail": f"{what}: paired difference {interval['point']}, interval "
                              f"[{interval['low']}, {interval['high']}] below zero", "interval": interval}
        return {"state": "inconclusive",
                "detail": f"{what}: paired difference {interval['point']}, interval "
                          f"[{interval['low']}, {interval['high']}] contains zero", "interval": interval}
    if s > c:
        return {"state": "inconclusive",
                "detail": f"{what}: {s} against {c} on development records; a formal claim needs the sealed "
                          f"final plan's paired interval"}
    return {"state": "not_supported", "detail": f"{what}: {s} against {c}"}


def _final_interval(subject: list, control: list, plan: dict) -> dict:
    """The paired difference over the final tasks, resampled over families, with the plan's own parameters."""
    from .evolve.task_gate import paired_bootstrap
    stats = dict(plan.get("statistical_plan") or {})
    s_tasks = _merge(subject)
    c_tasks = _merge(control)
    families = {task: family for arm in (*subject, *control) for task, family in (arm.get("families") or {}).items()}
    return paired_bootstrap(s_tasks, c_tasks, families, seed=int(stats.get("bootstrap_seed", 0)),
                            replicates=int(stats.get("bootstrap_replicates", 2000)),
                            confidence=float(stats.get("confidence", 0.95)))


def _merge(arms: list) -> dict:
    """Task -> its mean over these runs. Two runs of one identity are one arm."""
    totals: dict[str, list] = {}
    for arm in arms:
        for task, value in (arm.get("per_task") or {}).items():
            totals.setdefault(task, []).append(float(value))
    return {task: sum(values) / len(values) for task, values in totals.items()}


def _rate(arms: list) -> float | None:
    rates = [a["summary"]["pass_rate"] for a in arms if a["summary"]["pass_rate"] is not None]
    return round(sum(rates) / len(rates), 4) if rates else None


def render_markdown(report: dict) -> str:
    identity = report["identity"]
    lines = [f"# ouroloop report: {identity['domain']}", "",
             f"- protocol: `{report['protocol']}`",
             f"- attempts: {identity['attempts']} over {identity['runs']} runs",
             f"- splits: {identity['splits']['hash'] if identity['splits'] else 'none'} "
             f"{identity['splits']['counts'] if identity['splits'] else ''}",
             f"- releases: {', '.join(ct.short(r) for r in identity['releases']) or 'none'}"]
    for line in identity["missing"]:
        lines.append(f"- MISSING: {line}")
    lines += ["", "## Task effect", "",
              "| run | candidate | role | split | tasks | passed | pass rate | partial credit | invalid |",
              "|---|---|---|---|---|---|---|---|---|"]
    for run, arm in report["task_effect"]["arms"].items():
        s = arm["summary"]
        lines.append(f"| `{run[:8]}` | {arm['candidate']} | {arm['role']} | {arm['split']} | {s['tasks']} | "
                     f"{s['passed']} | {s['pass_rate']} | {s['partial_credit']} | {s['invalid']} |")
    lines += ["", "## Run cost", "", "| run | usd | missing | usage | tool calls |", "|---|---|---|---|---|"]
    for run, cost in report["run_cost"].items():
        lines.append(f"| `{run[:8]}` | {'unknown' if cost['cost_usd'] is None else cost['cost_usd']} | "
                     f"{cost['cost_missing']} | {cost['usage']} | {cost['tool_calls']} |")
    lines += ["", "## Improvement evidence", ""]
    for entry in report["improvement_evidence"]["experiments"]:
        lines.append(f"- {entry.get('experiment')}: {entry.get('hypothesis', '')[:80]} -> "
                     f"verdict={entry.get('verdict')} installed={entry.get('installed')} "
                     f"targets={entry.get('targets')}")
    for install in report["improvement_evidence"]["installations"]:
        lines.append(f"- {install.get('event')}: {ct.short(install['release']) if install.get('release') else ''} "
                     f"{install.get('reason', '')}"[:160])
    lines += ["", "## RSI evidence", "",
              f"- research receipts: {len(report['rsi_evidence']['research_receipts'])}",
              f"- discovery: {report['rsi_evidence']['discovery'] or 'none recorded'}",
              f"- installed releases: {len(report['rsi_evidence']['installed_releases'])}",
              "", "## Claims", ""]
    for claim, state in report["claims"].items():
        lines.append(f"- **{claim}**: {state['state']} — {state['detail']}")
    lines += ["", "## Improvement cost", "",
              f"- {report['improvement_cost'].get('experiments', 0)} experiments, "
              f"{report['improvement_cost'].get('failed_experiments', 0)} without an accepted verdict",
              f"- scopes: {json.dumps(report['improvement_cost'].get('scopes', {}), sort_keys=True)}",
              f"- {report['improvement_cost'].get('note', '')}"]
    return "\n".join(lines)


def write_report(root: str | Path, domain: str, report: dict) -> dict:
    directory = Path(root) / "bench" / domain
    write_json(directory / "report.json", report)
    (directory / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return {"json": directory / "report.json", "markdown": directory / "report.md"}
