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

from .benchmark import Results, RunManifest, SplitManifest, summarise, task_domain
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
    # v0.3 step 5 (M7 §4 按领域分列): an activity over several domains reports each arm per domain as well, and the
    # domains whose test is spent are named so no claim reads them. A one-domain report is what it always was.
    domains = manifest.domains() if manifest is not None else []
    spent = manifest.spent() if manifest is not None else set()
    several = len(domains) > 1
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
        if several:
            arms[run_hash]["per_domain"] = _per_domain(rows, spent)
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
            # each domain's frozen split (hash, rule, counts, strata) and whether its test is spent (M7 §4)
            "domains": [{**d, "spent": d["domain"] in spent} for d in domains] if several else [],
            "spent": sorted(spent),
            "runs": len(by_run), "attempts": len(attempts),
            "releases": sorted({a["release"] for a in arms.values() if a["release"]}),
            "missing": _missing(arms, manifest),
            "finalization": finalization,
        },
        "task_effect": {"arms": arms},
        "run_cost": {run: {"cost_usd": a["summary"]["cost_usd"], "cost_missing": a["summary"]["cost_missing"],
                           # M5 §2 (v0.3): the same number the gate receipt judges, per run; and the provider's
                           # rate-limit refusals the run absorbed as unbilled retries (bench.Improvement.run_arm)
                           "cost_per_attempt": a["summary"].get("cost_per_attempt"),
                           "rate_limited": sum(int(e.get("count") or 0) for e in events
                                               if e.get("event") == "rate_limited" and e.get("run") == run),
                           "usage": a["summary"]["usage"], "elapsed": a["summary"]["elapsed"],
                           "tool_calls": a["summary"]["tool_calls"],
                           "terminations": a["summary"]["terminations"],
                           **({"per_domain": {d: v["cost_per_attempt"] for d, v in a["per_domain"].items()}}
                              if "per_domain" in a else {})}
                     for run, a in arms.items()},
        "improvement_cost": _improvement_cost(root, entries, events),
        "improvement_evidence": {
            "experiments": [e for e in entries if e.get("kind") == "experiment"],
            "rounds": [e for e in entries if e.get("kind") == "round"],
            "receipts": receipts,
            "installations": installs,
        },
        "rsi_evidence": _rsi_evidence(objects, events, receipts),
        "harness": _harness_evidence(root, objects, events, entries, attempts, receipts,
                                     train_total=len(manifest.ids("train")) if manifest is not None else None),
        "external_references": _external_references(root, domain),
        "claims": {},
    }
    if several:
        report["final_by_domain"] = _final_by_domain(arms, spent, finalization)
    v05 = v05_evidence(root, entries, attempts)
    if v05:
        # v0.5 (docs/design/V0_5_PLAN.md §3): only an activity that ran the method or library machinery has the key,
        # so an earlier activity's report is what it was
        report["v05"] = v05
    reference = final_reference(arms, attempts, spent)
    if reference is not None:
        report["final_reference"] = reference
    report["claims"] = claim_states(report)
    return report


def v05_evidence(root: Path, entries: list, attempts: list) -> dict:
    """The v0.5 process evidence (docs/design/V0_5_PLAN.md §3), read off the records only: the learn readings and
    their fitted cells, the refit and library rounds' outcomes, the experience library's stream readout (D, B, the
    learning curve, the credit's rank correlation) and its entries, the playbook's kept and dropped lessons, and per
    final arm how many attempts a method acted in or library entries were shown in — with their pass rates apart, so
    a gain can be split by whether the claimed path was taken. {} when the activity ran none of it."""
    from .research.diagnosis import read_transcript
    fits = [e for e in entries if e.get("kind") == "method_fit"]
    streams = [e for e in entries if e.get("kind") == "library_stream"]
    snapshots = [e for e in entries if e.get("kind") == "library_snapshot"]
    playbooks = [e for e in entries if e.get("kind") == "method_playbook"]
    lib_rows: dict = {}
    for e in entries:
        if e.get("kind") == "library_entry" and e.get("entry_id"):
            lib_rows[e["entry_id"]] = e
    fixed = {}
    for e in entries:
        if e.get("kind") == "ledger" and e.get("fixed_code"):
            row = fixed.setdefault(e["candidate"], {"candidate": e["candidate"]})
            if e.get("stage") == "screened":
                row["screen"] = {k: e.get(k) for k in ("fixes", "sample_regressions", "net_flips", "outcome")}
            elif e.get("stage") == "gated":
                row["gate"] = {k: e.get(k) for k in ("verdict", "gain", "installed")}
            elif e.get("stage") == "drafted":
                row["drafted"] = e.get("status")
    finals: dict = {}
    transcripts = Path(root) / "transcripts"
    for a in attempts:
        if a.split != "test" or a.validity != "valid":
            continue
        events = read_transcript(transcripts, a.evidence)
        acted = any(e.get("kind") == "method_decision" and e.get("action") != "silent" for e in events)
        shown = any(e.get("kind") == "library_retrieval" and e.get("entries") for e in events)
        non_greedy = [e for e in events if e.get("kind") == "method_decision"
                      and (float(e.get("propensity", 1.0) or 0) != 1.0 or e.get("mode") not in (None, "greedy"))]
        arm = finals.setdefault(a.run, {"attempts": 0, "method_acted": [0, 0], "no_method": [0, 0],
                                        "library_shown": [0, 0], "no_library": [0, 0], "non_greedy_decisions": 0})
        arm["attempts"] += 1
        for key, hit in (("method_acted", acted), ("no_method", not acted), ("library_shown", shown),
                         ("no_library", not shown)):
            if hit:
                arm[key][0] += 1
                arm[key][1] += int(bool(a.success))
        arm["non_greedy_decisions"] += len(non_greedy)
    if not (fits or streams or snapshots or playbooks or fixed or any(
            v["method_acted"][0] or v["library_shown"][0] for v in finals.values())):
        return {}
    by_status: dict = {}
    for row in lib_rows.values():
        key = f"{row.get('status')}:{row.get('origin')}"
        by_status[key] = by_status.get(key, 0) + 1
    return {
        "learn": [{k: f.get(k) for k in ("stage", "release", "pairs", "observations", "changed", "moved")}
                  | {"tasks": len(f.get("tasks") or ()),
                     "cells": {c: {k: v.get(k) for k in ("n", "k", "m", "fixes", "regressions", "must_not",
                                                         "p_helps", "interval90", "action", "why")}
                               for c, v in (f.get("cells") or {}).items()}} for f in fits],
        "fixed_rounds": list(fixed.values()),
        "library": {"readout": streams[-1].get("readout") if streams else None,
                    "entries": by_status, "snapshots": [{k: s.get(k) for k in ("outcome", "why", "reason", "entries")}
                                                         for s in snapshots]},
        "playbook": {"sessions": len(playbooks), "kept_lines": len((playbooks[-1].get("text") or "").splitlines())
                     if playbooks else 0, "dropped_citations": sum(len(p.get("dropped") or ()) for p in playbooks)},
        "final_paths": finals,
    }


def pass_at_k(n: int, c: int, k: int) -> float:
    """The unbiased pass@k of one task with c passes in n attempts: 1 − C(n−c, k) / C(n, k) (k ≤ n)."""
    from math import comb
    return 1.0 - comb(n - c, k) / comb(n, k) if n - c >= k else 1.0


def final_reference(arms: dict, attempts: list, spent=frozenset()) -> dict | None:
    """The equal-compute reference beside W* (docs/design/V0_4_PLAN.md §1.8; M3 §2.2 v0.4): W0's oracle pass@k from
    the final's own W0 repeats — per task n valid attempts and c passes, the unbiased 1 − C(n−c, k)/C(n, k), averaged
    over the tasks, for k = 1 .. the least n — next to W*'s pass rate on the same tasks and the two arms' cost per
    attempt. It is an upper bound for "let W0 try k times and keep the best" at k times W0's cost, costs nothing extra
    (the final already runs W0 that many times), and is not part of C1's verdict. None without a final W0 arm. A
    spent domain's tasks are left out, as in the claim."""
    final_runs = {run: a for run, a in arms.items() if a.get("final")}
    by_role: dict[str, dict[str, list]] = {}
    for r in attempts:
        arm = final_runs.get(r.run)
        if arm is None or r.validity != "valid" or task_domain(r.task_id) in spent:
            continue
        by_role.setdefault(arm["role"], {}).setdefault(r.task_id, []).append(bool(r.success))
    base = by_role.get("base")
    if not base:
        return None
    k_max = min(len(v) for v in base.values())
    out = {"label": "equal-compute reference: W0's oracle pass@k over its own final repeats (an upper bound for "
                    "best-of-k W0 at k times W0's cost; not part of C1's verdict)",
           "tasks": len(base), "repeats": k_max,
           "base_pass_at_k": {str(k): round(sum(pass_at_k(len(v), sum(v), k) for v in base.values()) / len(base), 4)
                              for k in range(1, k_max + 1)}}
    evolved = by_role.get("evolved")
    if evolved:
        shared = sorted(set(evolved) & set(base))
        out["evolved_pass_rate"] = round(sum(sum(evolved[t]) / len(evolved[t]) for t in shared) / len(shared), 4) \
            if shared else None
        costs = {role: [a["summary"].get("cost_per_attempt") for a in final_runs.values() if a["role"] == role]
                 for role in ("base", "evolved")}
        b = [c for c in costs["base"] if c is not None]
        e = [c for c in costs["evolved"] if c is not None]
        if b and e and sum(b) > 0:
            ratio = (sum(e) / len(e)) / (sum(b) / len(b))
            out["cost_ratio"] = round(ratio, 4)
            out["k_at_equal_cost"] = max(1, round(ratio))
    return out


def _per_domain(rows: list, spent: set) -> dict:
    """One run's attempts per domain (M7 §4 按领域分列): tasks, passes, the pass rate and cost per valid attempt, in
    the order the domains first appear; a test row of a spent domain says so (it is never part of a claim)."""
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(task_domain(r.task_id), []).append(r)
    out = {}
    for d, part in by.items():
        s = summarise(part)
        out[d] = {"tasks": s["tasks"], "passed": s["passed"], "pass_rate": s["pass_rate"],
                  "attempts": s["attempts"], "invalid": s["invalid"], "cost_per_attempt": s.get("cost_per_attempt"),
                  **({"spent": True} if d in spent and part[0].split == "test" else {})}
    return out


def _final_by_domain(arms: dict, spent: set, finalization: dict | None) -> dict:
    """The final table per domain (M7 §4): each role's pass rate per domain over its final-split runs, and the
    evolved-minus-base difference per domain; a spent domain's rows are marked `spent` and left out of the claim
    rows, which pool only the domains a new claim may read. The matched-cost best-of-k arm (M3 §2.2, optional) is
    listed when a run of it is on record and `not_run` otherwise."""
    final = [a for a in arms.values() if a.get("final")]
    roles: dict[str, list] = {}
    for a in final:
        roles.setdefault(a["role"], []).append(a)
    table: dict[str, dict] = {}
    for role, runs in roles.items():
        merged = _merge(runs)
        for task, value in merged.items():
            row = table.setdefault(task_domain(task), {"spent": task_domain(task) in spent, "roles": {}})
            row["roles"].setdefault(role, []).append(value)
    for row in table.values():
        row["roles"] = {role: {"tasks": len(v), "pass_rate": round(sum(v) / len(v), 4)} for role, v in row["roles"].items()}
        base, evolved = row["roles"].get("base"), row["roles"].get("evolved")
        if base and evolved:
            row["difference"] = round(evolved["pass_rate"] - base["pass_rate"], 4)
    claim = {}
    for role, runs in roles.items():
        merged = {t: v for t, v in _merge(runs).items() if task_domain(t) not in spent}
        if merged:
            claim[role] = {"tasks": len(merged), "pass_rate": round(sum(merged.values()) / len(merged), 4)}
    if claim.get("base") and claim.get("evolved"):
        claim["difference"] = round(claim["evolved"]["pass_rate"] - claim["base"]["pass_rate"], 4)
    return {"domains": table, "claim": claim, "spent": sorted(spent),
            "best_of_k": "ran" if "matched_cost" in roles else "not_run",
            "plan": (finalization or {}).get("plan")}


# ---------------------------------------------------------------------------- the first version's evidence
def _harness_evidence(root: Path, objects: ct.Objects, events: list, entries: list, attempts: list,
                      receipts: list, train_total: int | None = None) -> dict | None:
    """What the capability package was and did (docs/modules/07_BENCH_REPORT.md §4), from records only.

    The package of each release, what the actor actually read and which hooks and package tools fired, how long
    the isolated worker ran, which train tasks each measurement ran (a rerun of the failures is its own row, never
    a whole train score), how much of each round's failures the notes covered, what the draft checks refused and
    what each research step cost; the gates with their frozen thresholds; the cycle's steps and whether an
    installed R was then used. None for an activity that never ran the first version.
    """
    diagnosis = [e for e in entries if e.get("kind") == "diagnosis"]
    cycle_path = root / "rsi" / "cycle.json"
    first = bool(diagnosis) or cycle_path.exists() or any(e.get("event") == "model_observed" for e in events)
    if not first:
        return None
    packages = {}
    for release in sorted({a.system_release_ref for a in attempts if a.system_release_ref}):
        info = _package_of(objects, release)
        if info is not None:
            packages[release] = info
    behaviour = {"sessions": 0, "skill_reads": {}, "hooks": {}, "package_tools": {}, "worker_sessions": 0,
                 "worker_seconds": 0.0, "gateway": gateway_summary(())}
    transcripts = root / "transcripts"
    for attempt in attempts:
        if not attempt.evidence or attempt.system_release_ref not in packages:
            continue
        behaviour["sessions"] += 1
        rows = list(_read_jsonl(transcripts / f"{attempt.evidence}.jsonl"))
        _tally_gateway(rows, behaviour["gateway"])
        for row in rows:
            kind = row.get("kind")
            if kind == "assistant":
                for call in row.get("tool_calls") or ():
                    if call.get("name") == "read_skill":
                        name = str((call.get("args") or {}).get("name", ""))
                        behaviour["skill_reads"][name] = behaviour["skill_reads"].get(name, 0) + 1
            elif kind == "hook":
                site = behaviour["hooks"].setdefault(str(row.get("site")), {})
                site[str(row.get("action"))] = site.get(str(row.get("action")), 0) + 1
            elif kind == "package_tool":
                name = str(row.get("tool"))
                behaviour["package_tools"][name] = behaviour["package_tools"].get(name, 0) + 1
            elif kind == "worker_session":
                behaviour["worker_sessions"] += 1
                behaviour["worker_seconds"] = round(behaviour["worker_seconds"] + float(row.get("seconds") or 0), 3)
    measurements: dict = {}
    for attempt in attempts:
        if "/measure" in (attempt.attempt_id or "") and attempt.system_release_ref:
            m = measurements.setdefault(attempt.system_release_ref, {"release": attempt.system_release_ref,
                                                                     "tasks": 0, "passed": 0})
            m["tasks"] += 1
            m["passed"] += int(bool(attempt.success))
    for m in measurements.values():
        m["scope"] = ("all train tasks" if train_total is not None and m["tasks"] >= train_total
                      else "a rerun of the previous version's failures, not a train score")
    drafts = {"checks": 0, "refused": 0, "infrastructure": 0, "finished": {}}
    for e in events:
        if e.get("event") == "draft_check":
            drafts["checks"] += 1
            drafts["refused"] += int(not e.get("ok"))
            drafts["infrastructure"] += int(bool(e.get("infrastructure")))
        elif e.get("event") == "draft_finished":
            drafts["finished"][e.get("status")] = drafts["finished"].get(e.get("status"), 0) + 1
    gates = []
    for r in receipts:
        if r["profile"] != "task":
            continue
        plan = objects.get(r["plan"]) if r.get("plan") and objects.has(r["plan"]) else {}
        gate = (plan or {}).get("gate_plan") or {}
        gates.append({"receipt": r.get("ref"), "verdict": r["verdict"], "min_gain": gate.get("min_gain"),
                      "max_regression_rate": gate.get("max_regression_rate"),
                      "gain": (r.get("metrics") or {}).get("gain"),
                      "regression_rate": (r.get("metrics") or {}).get("regression_rate"),
                      "regressions": (r.get("metrics") or {}).get("regressions"),
                      # v0.3 step 5: a multi-domain gate's gain per domain (means and counts; no task id)
                      **({"per_domain": r["metrics"]["per_domain"]} if (r.get("metrics") or {}).get("per_domain")
                         else {})})
    cycle = json.loads(cycle_path.read_text(encoding="utf-8")) if cycle_path.exists() else None
    reuse = [{"slot": name, **step["reuse"]} for name, step in ((cycle or {}).get("steps") or {}).items()
             if isinstance(step, dict) and step.get("reuse")]
    # M7 §4 余量 (v0.3): each research slot's headroom — the inner dev tasks the start W never passed — and, over
    # several domains, the same count per domain and how the halves split across domains (counts, never task ids)
    headroom = [{"slot": name, "headroom": step.get("headroom"), "outcome": step.get("outcome"),
                 **({"by_domain": step["headroom_by_domain"]} if "headroom_by_domain" in step else {}),
                 **({"halves_by_domain": step["halves_by_domain"]} if "halves_by_domain" in step else {})}
                for name, step in sorted(((cycle or {}).get("steps") or {}).items())
                if name.startswith("slot-") and isinstance(step, dict) and step.get("headroom") is not None]
    return {
        "packages": packages, "behaviour": behaviour,
        "measurements": list(measurements.values()),
        "measurement_note": "a measurement after an install reruns only the previous failures: its row is that "
                            "subset, and the passes it did not rerun belong to the version that produced them",
        "notes": [{k: d.get(k) for k in ("round", "working_set", "measured", "passed", "noted", "failures",
                                         "reused", "included", "not_included")}
                  | {"invalid": len(d.get("invalid") or ())}
                  for d in diagnosis],
        "drafts": drafts, "research_cost": _research_cost(root), "gates": gates,
        "research_steps": [{k: e.get(k) for k in ("scope", "stage", "release", "research_ref", "context_ref")}
                           | {"requests": len(e.get("steps") or ())}
                           for e in events if e.get("event") == "research_steps"],
        # v0.3 step 3 (M4 §5, M7 §4): the engine's ledger, per candidate — the declaration, how the drafts and the
        # admission went, the screen (train task ids, net flips, thin_screen), the gate's receipt ref and its three
        # per-task classes as counts; dev task ids stay in the receipt. And how often admission refused a repeat.
        "ledger": _ledger_report(entries, objects),
        "sessions": [{k: e.get(k) for k in ("scope", "round", "turns", "edits", "trajectory_reads",
                                             "trajectory_bytes", "finishes", "ended")}
                     | ({"transcript_ref": e["transcript_ref"]} if e.get("transcript_ref") else {})
                     for e in events if e.get("event") == "research_session"],
        "cycle": None if cycle is None else {"status": cycle.get("status"), "steps": cycle.get("steps"),
                                             **({"research_deferred": cycle["research_deferred"]}
                                                if cycle.get("research_deferred") else {})},
        "reuse": reuse,
        "headroom": headroom,
        # M7 §1 (2026-10-06): rounds run beyond the frozen schedule, each a recorded decision with its reason
        "extensions": [{k: e.get(k) for k in ("ts", "segment", "by", "reason", "rounds", "at_round",
                                              "frozen_worker_rounds", "frozen_patience", "extended_total")}
                       for e in events if e.get("event") == "schedule_extended"],
        "not_run": {"C3": "the first version trains no decision model: C3 belongs to the second version",
                    "distillation": "the first version trains no model: distillation belongs to the second version"},
    }


def _ledger_report(entries: list, objects=None) -> dict:
    """The ledger as the report lists it (M7 §4 实验台账): one row per candidate from the train-restricted
    projection (experience.ledger_projection — dev task ids never, the gate's classes as counts), the carriers'
    distribution, and the admissions `repeated_mechanism` refused."""
    from .evolve.experience import DEV_TASK_LISTS, ledger_projection, ledger_rows
    rows = ledger_rows(entries)
    carriers: dict = {}
    for r in rows:
        carrier = (r.get("declaration") or {}).get("carrier") or "unknown"
        carriers[carrier] = carriers.get(carrier, 0) + 1
    repeated = sum(1 for r in rows if any("repeated_mechanism" in str(x) for x in (r.get("admission") or {}).get("reasons") or ()))
    outcomes: dict = {}
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    # v0.3 step 5 (M7 §4, IT6-36): the gate's three per-task classes counted per domain, over every candidate the
    # activity's own ledger holds with its task lists (an arm's E0 holds counts only and adds nothing here)
    by_domain: dict = {}
    for r in rows:
        for cls in DEV_TASK_LISTS:
            value = (r.get("gate") or {}).get(cls)
            for task in value if isinstance(value, list) else ():
                d = by_domain.setdefault(task_domain(task), {k: 0 for k in DEV_TASK_LISTS})
                d[cls] += 1
    return {"candidates": ledger_projection(rows, objects), "carriers": carriers, "outcomes": outcomes,
            "repeated_mechanism_refusals": repeated,
            **({"by_domain": by_domain} if len(by_domain) > 1 else {}),
            "note": "dev task ids stay in the receipts; the gate's fixed/regressed/still_failing are counts here"}


def _package_of(objects: ct.Objects, release_ref: str) -> dict | None:
    """The package a release mounts, as a summary: its kinds and names, never its text. `agent` is the entry that
    runs it (M1 §4): the manifest's own, or the default agent's for a package without one (every v1 package)."""
    from .agent import harness_package as hp
    from .agent.default_agent import DEFAULT_ENTRY
    from .policies import parse_builtin
    try:
        release = ct.SystemRelease.from_json(objects.get(release_ref))
        for ref in objects.load(release.worker_ref, ct.PolicySet).policies:
            p = objects.load(ref, ct.PolicyDescriptor)
            if p.target != "worker.harness":
                continue
            config = parse_builtin(p.implementation["builtin"], objects.get(p.implementation["config_ref"]))
            run, tests = hp.files_of(objects, config["package_ref"])
            manifest = hp.parse_manifest(run["manifest.json"], {**run, **tests})
            agent = manifest.get("agent") if isinstance(manifest.get("agent"), dict) else {}
            return {"package_ref": config["package_ref"], "files": len(run), "test_files": len(tests),
                    "api_version": manifest.get("api_version") or hp.API_VERSIONS[0],
                    "agent": agent.get("entrypoint") or DEFAULT_ENTRY,
                    "roles": list(agent.get("roles") or ["act"]),
                    "skills": [s["name"] for s in manifest["skills"]],
                    "hooks": [{"name": h["name"], "site": h["site"]} for h in manifest["hooks"]],
                    "tools": [t["name"] for t in manifest["tools"]],
                    "memory": bool(manifest["memory"].get("enabled")),
                    "kinds": sorted(hp.kinds_of(manifest, {**run, **tests}))}
    except (KeyError, ct.ContractError, ValueError, TypeError):
        return None
    return None


# ---------------------------------------------------------------------------- what the runner shell's records say
def gateway_summary(transcripts) -> dict:
    """What the host-written records of the runner shell say about how the attempts ran (docs/modules/01_POLICY_RUNTIME.md
    §4 Inspection, v0.3), tallied over `transcripts` (each a list of transcript rows): how many attempts ran under the
    shell (`attempts`: a transcript with a `gateway_call` line), which agent entry ran where (`entries`, `placements`:
    the `agent` record the runner writes — the entry, and `worker` or `host`), the model calls and output tokens by
    role (`by_role`, the `assistant` events' `role`, tallied as Gateway.by_role tallies them), the efforts the calls
    were made at (`efforts`) and how many requests the gateway capped at K's `max_effort` (`effort_caps`: a
    `requested_effort` on record other than the `effort` used), and which hard limit ended an attempt (`limits_hit`,
    the `end` event's `limit`, by dimension). Read from the records alone, never from a package's own account (M0 §2
    记录由宿主写). An attempt under the v1 loop adds nothing: it wrote none of these lines, and a zero for it would read
    as a shell attempt that made no call (observed=false, not 0)."""
    out = {"attempts": 0, "entries": {}, "placements": {}, "by_role": {}, "efforts": {}, "effort_caps": 0,
           "limits_hit": {}}
    for rows in transcripts:
        _tally_gateway(list(rows), out)
    return out


def _tally_gateway(rows: list, into: dict) -> None:
    from .evolve.execution import normalize_usage
    if not any(r.get("kind") == "gateway_call" for r in rows):
        return
    into["attempts"] += 1
    for r in rows:
        kind = r.get("kind")
        if kind == "agent":
            _bump(into["entries"], r.get("entry"))
            _bump(into["placements"], r.get("placement"))
        elif kind == "assistant" and r.get("role"):
            tally = into["by_role"].setdefault(str(r["role"]), {"calls": 0, "tokens": 0})
            tally["calls"] += 1
            usage = r.get("usage") or {}
            tally["tokens"] += int(normalize_usage(usage)["output_tokens"]) if usage else 0
            _bump(into["efforts"], r.get("effort"))
            if r.get("requested_effort") is not None and r.get("requested_effort") != r.get("effort"):
                into["effort_caps"] += 1
        elif kind == "end" and r.get("limit"):
            _bump(into["limits_hit"], r.get("limit"))


def _bump(counts: dict, key) -> None:
    key = str(key)
    counts[key] = counts.get(key, 0) + 1


def _research_cost(root: Path) -> dict:
    """Settled research spend by step (note, propose, revise, research_slot, ...), from the budget book's
    operation labels. Unknown reservations are counted, not priced."""
    path = root / "rsi" / "budget" / "book.json"
    if not path.exists():
        return {}
    out: dict = {}
    for reservation in json.loads(path.read_text(encoding="utf-8")).get("reservations", {}).values():
        operation = str(reservation.get("operation_id") or "")
        if ":research" not in operation:
            continue
        step = operation.split(":research", 1)[1].split("#", 1)[0].lstrip(":") or "unlabelled"
        row = out.setdefault(step, {"calls": 0, "unknown": 0, "settled": {}})
        row["calls"] += 1
        if reservation.get("status") == "settled":
            for key, value in (reservation.get("measured") or {}).items():
                row["settled"][key] = round(row["settled"].get(key, 0) + float(value), 6)
        elif reservation.get("status") == "unknown":
            row["unknown"] += 1
    return out


EXTERNAL_KEYS = ("source_url", "retrieved_at", "model", "effort", "score", "metric", "task_set", "domain",
                 "protocol_notes")


def _external_references(root: Path, domain: str) -> list:
    """Published numbers (docs/modules/07_BENCH_REPORT.md §4), from `bench/<domain>/external_references.json`:
    background material with its source and task set, never a local attempt. An entry missing a field is kept
    and marked incomplete rather than repaired."""
    path = root / "bench" / domain / "external_references.json"
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [{**{k: row.get(k) for k in EXTERNAL_KEYS},
             "incomplete": sorted(k for k in EXTERNAL_KEYS if row.get(k) in (None, ""))}
            for row in rows if isinstance(row, dict)]


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


def _rate_limited(events: list) -> dict:
    """The provider's rate-limit refusals absorbed as unbilled retries (M5 §2, v0.3), summed from the `rate_limited`
    events: the task attempts' (every arm) and the research steps' (arm `research`) apart."""
    out = {"attempts": 0, "research": 0}
    for e in events:
        if e.get("event") == "rate_limited":
            out["research" if e.get("arm") == "research" else "attempts"] += int(e.get("count") or 0)
    return out


def _improvement_cost(root: Path, entries: list, events: list = ()) -> dict:
    """What the improving cost, by scope, from the budget book. Unknown stays unknown; nothing is amortised here
    because the assumption behind an amortisation belongs to whoever publishes it."""
    path = Path(root) / "rsi" / "budget" / "book.json"
    if not path.exists():
        return {"scopes": {}, "note": "no budget book in this workspace", "rate_limited": _rate_limited(events)}
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
            "rate_limited": _rate_limited(events),
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
    # M7 §2 (v0.3 step 5): a spent domain's test is never in a claim's interval, whatever runs are on record
    spent = set((report.get("identity") or {}).get("spent") or ())
    by_role = {}
    for arm in arms.values():
        by_role.setdefault(arm["role"], []).append(arm)
    out = {}
    out["C1"] = _pair_claim(by_role.get("evolved"), by_role.get("base"), "iteration over the same base", plan,
                            spent=spent)
    frontier = [a for role, rows in by_role.items() if role.startswith("frontier") for a in rows]
    if not frontier or not by_role.get("evolved"):
        out["C2"] = {"state": "not_run", "detail": "no frontier comparison on record"}
    else:
        complete = len({a["role"] for a in frontier}) >= 2
        out["C2"] = {**_pair_claim(by_role.get("evolved"), frontier, "position against leading models", plan,
                                   spent=spent), "complete": complete}
        if not complete:
            out["C2"]["state"] = "inconclusive"
            out["C2"]["detail"] += "; fewer than two vendors, so C2 is incomplete"
    if not frontier and report.get("external_references"):
        # Published numbers only: background, not a same-task comparison (docs/modules/07_BENCH_REPORT.md §4).
        out["C2"] = {"state": "inconclusive", "evidence_kind": "external_reference",
                     "detail": f"{len(report['external_references'])} published results cited; no local run on "
                               f"the same tasks, so no position is claimed"}
    if report.get("harness"):
        # The first version trains no decision model; the evolved/prompt_best mapping would misname a package
        # or prompt change as a decision-model contribution.
        out["C3"] = {"state": "not_run", "detail": report["harness"]["not_run"]["C3"]}
    else:
        out["C3"] = _pair_claim(by_role.get("evolved"), by_role.get("prompt_best"),
                                "the decision model against the same prompt without it", plan, spent=spent)
    research = [r for r in receipts if r["profile"] == "research"]
    if not research:
        out["C4"] = {"state": "not_run", "detail": _slots_without_a_campaign(report)}
    else:
        last = research[-1]
        supported = last["verdict"] == "accepted" and last["uncertainty"].get("significant")
        out["C4"] = {"state": "supported" if supported else
                     "not_supported" if last["verdict"] == "rejected" else "inconclusive",
                     "detail": f"{last['verdict']}: {'; '.join(last['reasons'])[:200]}",
                     "metrics": last["metrics"]}
    return out


def _slots_without_a_campaign(report: dict) -> str:
    """How each research slot ended when no research receipt is on record, from rsi/cycle.json
    (docs/modules/07_BENCH_REPORT.md §4): a slot that ended before its campaign says how, not that there is none."""
    cycle = (report.get("harness") or {}).get("cycle") or {}
    steps = cycle.get("steps") or {}
    slots = sorted((name, step) for name, step in steps.items() if name.startswith("slot-") and isinstance(step, dict))
    if not slots:
        # v0.3 step 5: a schedule that runs fewer slots than the preset plans says so (bench.cycle)
        deferred = cycle.get("research_deferred")
        return f"research slot {deferred['reason']}" if deferred else "no research campaign on record"
    out = []
    for name, step in slots:
        outcome, why = step.get("outcome"), step.get("reason") or "no reason recorded"
        where = f"during campaign {ct.short(step['campaign'])}" if step.get("campaign") else "before a campaign ran"
        if outcome == "no_proposal":
            out.append(f"{name}: the research policy proposed no change to itself ({why})")
        elif outcome == "no_slot_pool":
            # M6 §2 (v0.3): the dev split holds no two gates of min_tasks tasks; R was not asked
            out.append(f"{name}: not opened, the dev split is too small for two gates of min_tasks ({why})")
        elif outcome == "no_headroom":
            # M6 §2 (v0.3): the start W left too few inner tasks unsolved for a candidate to show a gain
            out.append(f"{name}: not opened for want of headroom ({why}); never-passed inner tasks: "
                       f"{step.get('headroom')}")
        elif outcome == "invalid" and step.get("stage") == "reply":
            out.append(f"{name}: the research slot's reply was invalid ({why})")
        elif step.get("stage") == "admission":
            out.append(f"{name}: admission refused the proposed R ({why})")
        elif step.get("stage") == "build":
            out.append(f"{name}: the proposed R did not build ({why})")
        elif outcome == "budget_exhausted":
            out.append(f"{name}: the budget was spent {where} ({why})")
        elif outcome == "paused":
            out.append(f"{name}: paused {where} ({why}); the slot resumes when the cycle runs again")
        elif step.get("campaign"):
            out.append(f"{name}: campaign {ct.short(step['campaign'])} is planned and has no receipt")
        else:
            out.append(f"{name}: ended {outcome or 'without an outcome'} before a campaign ({why})")
    return "; ".join(out)[:600]


def _pair_claim(subject, control, what: str, plan: dict | None = None, *, spent=frozenset()) -> dict:
    """One claim's state. With final-split runs on both sides and a sealed plan, the paired interval decides; the
    tasks of spent domains (M7 §2) are not in it."""
    if not subject or not control:
        return {"state": "not_run", "detail": f"{what}: both sides are not on record"}
    s, c = _rate(subject), _rate(control)
    if s is None or c is None:
        return {"state": "inconclusive", "detail": f"{what}: no valid attempts on one side"}
    final = [a for a in subject if a.get("final")], [a for a in control if a.get("final")]
    if plan and all(final):
        interval = _final_interval(final[0], final[1], plan, spent=spent)
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
    # Before a final exists: the dev readings only (stage 0/1 review, 2026-10-05) — a train-side screen or a train
    # measurement of a W is not a development reading, and averaging them in would compare different task sets.
    s, c = _rate([a for a in subject if a.get("split") == "dev"]), _rate([a for a in control if a.get("split") == "dev"])
    if s is None or c is None:
        return {"state": "inconclusive", "detail": f"{what}: no valid dev attempts on one side"}
    if s > c:
        return {"state": "inconclusive",
                "detail": f"{what}: {s} against {c} on development records; a formal claim needs the sealed "
                          f"final plan's paired interval"}
    return {"state": "not_supported", "detail": f"{what}: {s} against {c}"}


def _final_interval(subject: list, control: list, plan: dict, *, spent=frozenset()) -> dict:
    """The paired difference over the final tasks, resampled over families, with the plan's own parameters. A spent
    domain's tasks are left out (M7 §2): they are reported apart, never pooled into a claim's interval."""
    from .evolve.task_gate import paired_bootstrap
    stats = dict(plan.get("statistical_plan") or {})
    s_tasks = {t: v for t, v in _merge(subject).items() if task_domain(t) not in spent}
    c_tasks = {t: v for t, v in _merge(control).items() if task_domain(t) not in spent}
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
    if identity.get("domains"):
        # v0.3 step 5 (M7 §4 按领域分列): the pooled rows above, each domain's below; a spent test is marked
        lines += ["", "### By domain", "", "| domain | split | rule | train | dev | test |", "|---|---|---|---|---|---|"]
        for d in identity["domains"]:
            c = d.get("counts") or {}
            lines.append(f"| {d['domain']} | `{d.get('hash')}` | {d.get('split_rule') or 'hash'} | "
                         f"{(c.get('train') or {}).get('tasks')} | {(c.get('dev') or {}).get('tasks')} | "
                         f"{(c.get('test') or {}).get('tasks')}{' (spent)' if d.get('spent') else ''} |")
        lines += ["", "| run | role | split | domain | tasks | passed | pass rate | cost/attempt |",
                  "|---|---|---|---|---|---|---|---|"]
        for run, arm in report["task_effect"]["arms"].items():
            for d, v in (arm.get("per_domain") or {}).items():
                lines.append(f"| `{run[:8]}` | {arm['role']} | {arm['split']} | {d}{' (spent)' if v.get('spent') else ''}"
                             f" | {v['tasks']} | {v['passed']} | {v['pass_rate']} | {v['cost_per_attempt']} |")
    final = report.get("final_by_domain")
    if final and final.get("domains"):
        lines += ["", "### Final by domain", "", "| domain | base | evolved | difference |", "|---|---|---|---|"]
        for d, row in final["domains"].items():
            roles = row["roles"]
            lines.append(f"| {d}{' (spent: not in the claim)' if row['spent'] else ''} | "
                         f"{(roles.get('base') or {}).get('pass_rate')} | {(roles.get('evolved') or {}).get('pass_rate')} | "
                         f"{row.get('difference')} |")
        claim = final.get("claim") or {}
        lines.append(f"| claim (domains not spent) | {(claim.get('base') or {}).get('pass_rate')} | "
                     f"{(claim.get('evolved') or {}).get('pass_rate')} | {claim.get('difference')} |")
        lines.append(f"- matched-cost best-of-k arm: {final['best_of_k']}")
    reference = report.get("final_reference")
    if reference:
        # v0.4 (V0_4_PLAN §1.8): beside W*, labelled, never the claim
        lines += ["", "### Equal-compute reference", "", f"- {reference['label']}",
                  f"- W0 pass@k over {reference['tasks']} tasks × {reference['repeats']}: "
                  + ", ".join(f"pass@{k} {v}" for k, v in reference["base_pass_at_k"].items()),
                  f"- W* pass rate: {reference.get('evolved_pass_rate')}; cost per attempt W*/W0: "
                  f"{reference.get('cost_ratio')} (k at equal cost {reference.get('k_at_equal_cost')})"]
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
    harness = report.get("harness")
    if harness:
        lines += ["", "## Capability package", ""]
        for release, pkg in harness["packages"].items():
            lines.append(f"- {ct.short(release)}: package {ct.short(pkg['package_ref'])}, {pkg['files']} files, "
                         f"kinds {pkg['kinds'] or 'none'}, skills {pkg['skills'] or 'none'}, hooks "
                         f"{[h['site'] for h in pkg['hooks']] or 'none'}, tools {pkg['tools'] or 'none'}, "
                         f"memory {'on' if pkg['memory'] else 'off'}"
                         + (f", agent {pkg['agent']} roles {pkg['roles']}" if pkg.get("agent") else ""))
        b = harness["behaviour"]
        lines += [f"- behaviour over {b['sessions']} sessions: skill reads {b['skill_reads'] or 'none'}; hooks "
                  f"{b['hooks'] or 'none'}; package tools {b['package_tools'] or 'none'}; worker "
                  f"{b['worker_sessions']} sessions, {b['worker_seconds']} s"]
        g = b.get("gateway") or {}
        if g.get("attempts"):
            # The runner shell's own facts (M1 §4), from the host's records: which program ran where, what it asked
            # the model as whom, what the gateway capped, and which hard limit ended an attempt.
            lines.append(f"- runner shell over {g['attempts']} attempts: agent {g['entries']} placed {g['placements']}; "
                         f"model calls by role {g['by_role']}; efforts {g['efforts']}, {g['effort_caps']} requests "
                         f"capped at K's max_effort; limits hit {g['limits_hit'] or 'none'}")
        for m in harness["measurements"]:
            lines.append(f"- measured {ct.short(m['release'])}: {m['passed']}/{m['tasks']} passed — {m['scope']}")
        lines.append(f"- {harness['measurement_note']}")
        for n in harness["notes"]:
            lines.append(f"- notes round {n['round']}: {n['noted']}/{n['failures']} failures of {n['working_set']} "
                         f"({n['reused']} reused, {n['invalid']} invalid, {len(n['not_included'] or ())} not "
                         f"included)")
        d = harness["drafts"]
        lines.append(f"- drafts: {d['checks']} checks, {d['refused']} refused, {d['infrastructure']} on "
                     f"infrastructure; finished {d['finished'] or 'none'}")
        for g in harness["gates"]:
            # None: a receipt filed before the list was kept; []: a gate that saw no task fall
            regressed = "not recorded" if g["regressions"] is None else ", ".join(
                f"{r['task']} {r['control']}->{r['subject']}" for r in g["regressions"]) or "none"
            lines.append(f"- gate {g['verdict']}: gain {g['gain']} (min_gain {g['min_gain']}), regression rate "
                         f"{g['regression_rate']} (max {g['max_regression_rate']}); regressed: {regressed}")
            if g.get("per_domain"):
                lines.append("  - by domain: " + "; ".join(f"{d} {v['subject']} vs {v['control']} ({v['gain']:+}, "
                                                            f"{v['tasks']} tasks)" for d, v in g["per_domain"].items()))
        lines.append(f"- research cost by step: {json.dumps(harness['research_cost'], sort_keys=True)}")
        ledger = harness.get("ledger") or {}
        if ledger.get("candidates"):
            lines.append(f"- ledger: {len(ledger['candidates'])} candidates, carriers "
                         f"{json.dumps(ledger['carriers'], sort_keys=True)}, outcomes "
                         f"{json.dumps(ledger['outcomes'], sort_keys=True)}, repeated_mechanism refusals "
                         f"{ledger['repeated_mechanism_refusals']}")
            for c in ledger["candidates"]:
                d, s, g = c.get("declaration") or {}, c.get("screen") or {}, c.get("gate") or {}
                lines.append(f"  - {c['candidate']}: {d.get('carrier')} at {d.get('site')} — {d.get('mechanism')}; "
                             f"screen net flips {s.get('net_flips')}{' (thin)' if s.get('thin_screen') else ''}; "
                             f"{c['outcome']}" + (f"; gate gain {g.get('gain')}, fixed {g.get('fixed')}, regressed "
                                                  f"{g.get('regressed')}, still failing {g.get('still_failing')}"
                                                  if g else ""))
        for s in harness.get("sessions") or ():
            lines.append(f"- session round {s['round']}: {s['turns']} turns, {s['edits']} edits, "
                         f"{s['trajectory_reads']} trajectory reads ({s['trajectory_bytes']} bytes), "
                         f"{s['finishes']} finish{'es' if s['finishes'] != 1 else ''}"
                         + (f"; ended: {s['ended']}" if s.get("ended") else ""))
        if ledger.get("by_domain"):
            lines.append(f"- ledger by domain (fixed/regressed/still failing): "
                         f"{json.dumps(ledger['by_domain'], sort_keys=True)}")
        for h in harness.get("headroom") or ():
            lines.append(f"- headroom {h['slot']}: {h['headroom']} inner tasks never passed"
                         + (f"; by domain {json.dumps(h['by_domain'], sort_keys=True)}" if "by_domain" in h else "")
                         + (f"; halves {json.dumps(h['halves_by_domain'], sort_keys=True)}"
                            if h.get("halves_by_domain") else "") + (f" ({h['outcome']})" if h.get("outcome") else ""))
        if harness["cycle"]:
            lines.append(f"- cycle: {harness['cycle']['status']}; steps {sorted(harness['cycle']['steps'] or {})}")
        for x in harness.get("extensions") or ():
            lines.append(f"- schedule extended: {x['segment']} by {x['rounds']} round(s) from round {x['at_round']} "
                         f"(frozen worker_rounds {x['frozen_worker_rounds']}, patience {x['frozen_patience']}; "
                         f"{x['extended_total']} added in all) by {x['by']}: {x['reason']}")
        for r in harness["reuse"]:
            lines.append(f"- reuse after {r['slot']}: {r['state']} — {r['detail']}")
        for name, why in harness["not_run"].items():
            lines.append(f"- {name}: not_run — {why}")
    if report.get("v05"):
        lines += ["", "## v0.5 process evidence (train only, not a claim)", ""] + _v05_lines(report["v05"])
    if report.get("external_references"):
        lines += ["", "## Published results (background, not a local comparison)", "",
                  "| model | effort | score | metric | task set | source | retrieved |", "|---|---|---|---|---|---|---|"]
        for r in report["external_references"]:
            lines.append(f"| {r['model']} | {r['effort']} | {r['score']} | {r['metric']} | {r['task_set']} | "
                         f"{r['source_url']} | {r['retrieved_at']} |")
    lines += ["", "## Improvement cost", "",
              f"- {report['improvement_cost'].get('experiments', 0)} experiments, "
              f"{report['improvement_cost'].get('failed_experiments', 0)} without an accepted verdict",
              f"- scopes: {json.dumps(report['improvement_cost'].get('scopes', {}), sort_keys=True)}",
              f"- rate-limit refusals absorbed as unbilled retries: "
              f"{json.dumps(report['improvement_cost'].get('rate_limited', {}), sort_keys=True)}",
              f"- {report['improvement_cost'].get('note', '')}"]
    return "\n".join(lines)


def _v05_lines(v05: dict) -> list:
    lines = []
    for f in v05.get("learn") or ():
        lines.append(f"- learn {f['stage']}: {f['tasks']} train tasks, {f['pairs']} twin pairs, {f['observations']} "
                     f"paired decisions; " + (f"moved {f['moved']}" if f.get("changed") else "the table stayed"))
        for cell, c in (f.get("cells") or {}).items():
            lines.append(f"  - {cell}: n {c.get('n')}, footprint up {c.get('k')} / down {c.get('m')}, fixes "
                         f"{c.get('fixes')}, regressions {c.get('regressions')}, must-not {c.get('must_not')}, "
                         f"P(helps) {c.get('p_helps')} {c.get('interval90')} -> {c.get('action')} ({c.get('why')})")
    for r in v05.get("fixed_rounds") or ():
        lines.append(f"- fixed round {r['candidate']}: drafted {r.get('drafted')}, screen {r.get('screen')}, gate "
                     f"{r.get('gate')}")
    lib = v05.get("library") or {}
    readout = lib.get("readout")
    if readout:
        lines.append(f"- library stream: D {readout['D']} on {readout['tasks']} tasks, B {readout['B']}; halves "
                     f"{readout.get('first_half')} -> {readout.get('second_half')} (P second half better "
                     f"{readout.get('p_second_half_better')}); credit rank correlation "
                     f"{readout.get('credit_rank_correlation')}; entries {json.dumps(readout.get('entries'))}; shown "
                     f"per attempt {readout.get('shown_per_attempt')}")
        lines.append("  - learning curve (paired effect per window of 10): " + ", ".join(
            f"{w['from']}-{w['to']}: {w['effect']:+.2f} (n {w['n']})" for w in readout.get("windows") or ()))
    if lib.get("entries"):
        lines.append(f"- library entries by status and origin: {json.dumps(lib['entries'], sort_keys=True)}")
    for s in lib.get("snapshots") or ():
        lines.append(f"- library snapshot: {s.get('outcome')}" + (f" ({s.get('why') or s.get('reason')})"
                                                                   if s.get("why") or s.get("reason") else ""))
    pb = v05.get("playbook") or {}
    if pb.get("sessions"):
        lines.append(f"- playbook: {pb['kept_lines']} lessons kept, {pb['dropped_citations']} citations dropped over "
                     f"{pb['sessions']} sessions")
    for run, arm in (v05.get("final_paths") or {}).items():
        def rate(pair):
            return f"{pair[1]}/{pair[0]}" if pair[0] else "-"
        lines.append(f"- final run {run}: method acted {rate(arm['method_acted'])} passed, no method "
                     f"{rate(arm['no_method'])}; library shown {rate(arm['library_shown'])}, not shown "
                     f"{rate(arm['no_library'])}; non-greedy decisions {arm['non_greedy_decisions']} (must be 0)")
    return lines


def write_report(root: str | Path, domain: str, report: dict) -> dict:
    directory = Path(root) / "bench" / domain
    write_json(directory / "report.json", report)
    (directory / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return {"json": directory / "report.json", "markdown": directory / "report.md"}
