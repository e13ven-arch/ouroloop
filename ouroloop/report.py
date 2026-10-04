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
                           # M5 §2 (v0.3): the same number the gate receipt judges, per run; and the provider's
                           # rate-limit refusals the run absorbed as unbilled retries (bench.Improvement.run_arm)
                           "cost_per_attempt": a["summary"].get("cost_per_attempt"),
                           "rate_limited": sum(int(e.get("count") or 0) for e in events
                                               if e.get("event") == "rate_limited" and e.get("run") == run),
                           "usage": a["summary"]["usage"], "elapsed": a["summary"]["elapsed"],
                           "tool_calls": a["summary"]["tool_calls"],
                           "terminations": a["summary"]["terminations"]}
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
    report["claims"] = claim_states(report)
    return report


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
                      "regressions": (r.get("metrics") or {}).get("regressions")})
    cycle = json.loads(cycle_path.read_text(encoding="utf-8")) if cycle_path.exists() else None
    reuse = [{"slot": name, **step["reuse"]} for name, step in ((cycle or {}).get("steps") or {}).items()
             if isinstance(step, dict) and step.get("reuse")]
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
        "ledger": _ledger_report(entries),
        "sessions": [{k: e.get(k) for k in ("scope", "round", "turns", "edits", "trajectory_reads",
                                             "trajectory_bytes", "finishes", "ended")}
                     for e in events if e.get("event") == "research_session"],
        "cycle": None if cycle is None else {"status": cycle.get("status"), "steps": cycle.get("steps")},
        "reuse": reuse,
        "not_run": {"C3": "the first version trains no decision model: C3 belongs to the second version",
                    "distillation": "the first version trains no model: distillation belongs to the second version"},
    }


def _ledger_report(entries: list) -> dict:
    """The ledger as the report lists it (M7 §4 实验台账): one row per candidate from the train-restricted
    projection (experience.ledger_projection — dev task ids never, the gate's classes as counts), the carriers'
    distribution, and the admissions `repeated_mechanism` refused."""
    from .evolve.experience import ledger_projection, ledger_rows
    rows = ledger_rows(entries)
    carriers: dict = {}
    for r in rows:
        carrier = (r.get("declaration") or {}).get("carrier") or "unknown"
        carriers[carrier] = carriers.get(carrier, 0) + 1
    repeated = sum(1 for r in rows if any("repeated_mechanism" in str(x) for x in (r.get("admission") or {}).get("reasons") or ()))
    outcomes: dict = {}
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    return {"candidates": ledger_projection(rows), "carriers": carriers, "outcomes": outcomes,
            "repeated_mechanism_refusals": repeated,
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
                                "the decision model against the same prompt without it", plan)
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
    steps = ((report.get("harness") or {}).get("cycle") or {}).get("steps") or {}
    slots = sorted((name, step) for name, step in steps.items() if name.startswith("slot-") and isinstance(step, dict))
    if not slots:
        return "no research campaign on record"
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
        if harness["cycle"]:
            lines.append(f"- cycle: {harness['cycle']['status']}; steps {sorted(harness['cycle']['steps'] or {})}")
        for r in harness["reuse"]:
            lines.append(f"- reuse after {r['slot']}: {r['state']} — {r['detail']}")
        for name, why in harness["not_run"].items():
            lines.append(f"- {name}: not_run — {why}")
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


def write_report(root: str | Path, domain: str, report: dict) -> dict:
    directory = Path(root) / "bench" / domain
    write_json(directory / "report.json", report)
    (directory / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return {"json": directory / "report.json", "markdown": directory / "report.md"}
