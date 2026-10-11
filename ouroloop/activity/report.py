"""The activity's report (docs/design/ACTIVITY.md §3, §9): built offline from what is on disk, no model called, the
same bytes every time it is written (nothing in it reads the clock).

    activity   scope, K, preset, code version, the split's hash and counts
    baseline   W0's train reading and its dev readings
    rounds     each round of the improver: causes, the focus list, candidates and the rungs they passed, the held-out
               screens with the accept rule's answer, the install on the working line, the stop, the round's spend
    gate       the release gate at the end of the activity: the working line against W0 on dev, and the release
    memory     the memory stream's readout (D, the halves, the entries), what it dropped and lost, its snapshot
    final      each arm of the sealed final, W* - W0 on test with its 95% interval over families (C1), and how many
               times this activity ran its final
    spend      what each phase reserved and settled, from the budget book; the steps sum to the root
"""
from __future__ import annotations

import json

from ..contracts import read_jsonl, short, write_json
from ..evaluate.results import summarise


def _runs_by_candidate(activity, split: str) -> dict:
    runs = activity.results.runs()
    out: dict = {}
    for r in activity.results.all():
        run = runs.get(r.run)
        if run is None or r.split != split:
            continue
        out.setdefault(run.candidate, []).append(r)
    return out


def _latest(rows: list) -> list:
    keep: dict = {}
    for r in rows:
        keep[(r.task_id, r.attempt)] = r
    return [keep[k] for k in sorted(keep)]


def _brief(summary: dict) -> dict:
    return {k: summary.get(k) for k in ("tasks", "attempts", "invalid", "passed", "pass_rate", "partial_credit",
                                        "cost_usd", "cost_missing", "cost_per_attempt")}


def spend(activity) -> dict:
    """Per phase account (`<scope>:<phase>`): the dollars and calls settled or held, and the root's total."""
    book = activity.budgets._read()
    if activity.scope_id not in book.get("scopes", {}):
        return {"root": None, "phases": {}}
    phases = {}
    # the root's own children, each with its subtree (a round's account holds its steps' accounts): they sum to it
    for name in sorted(n for n, e in book["scopes"].items() if e.get("parent") == activity.scope_id):
        used = activity.budgets.usage(name, book)
        phases[name.split(":", 1)[-1]] = {k: round(v, 6) for k, v in used.items() if v}
    root = activity.budgets.usage(activity.scope_id, book)
    return {"root": {k: round(v, 6) for k, v in root.items() if v}, "phases": phases,
            "limits": book["scopes"][activity.scope_id].get("limits"),
            "allocations": book["scopes"][activity.scope_id].get("allocations")}


def memory_report(activity) -> dict | None:
    """The stream's ledger (memory/<domain>/stream.json) as the report reads it, and its snapshot's outcome."""
    path = activity.root / "memory" / activity.domain / "stream.json"
    if not path.exists():
        return None
    ledger = json.loads(path.read_text(encoding="utf-8"))
    snapshots = [e for e in activity.events.read("library_snapshot")]
    last = snapshots[-1] if snapshots else None
    snapshot = None
    if last is not None:
        fit = last.get("fit")           # what the snapshot kept within K's max_memory_bytes (MEMORY §7.1)
        snapshot = {**{k: last.get(k) for k in ("outcome", "why", "package_ref", "snapshot_digest")},
                    "fit": None if not fit else {**{k: fit.get(k) for k in ("entries", "kept", "bytes", "max_bytes")},
                                                 "omitted": len(fit.get("omitted") or ())}}
    return {"status": ledger.get("status"), "processed": len(ledger.get("processed") or {}),
            "planned": len((ledger.get("plan") or {}).get("order") or ()), "lost": len(ledger.get("lost") or {}),
            "readout": ledger.get("readout"), "counters": ledger.get("counters"),
            "dropped": len(ledger.get("dropped") or ()), "imports": len(ledger.get("imports") or ()),
            "snapshot": snapshot}


def gate_report(activity) -> dict | None:
    """The release gate's record (rsi/gate.json), without its refs."""
    path = activity.root / "rsi" / "gate.json"
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    return {"status": state.get("status"), "verdict": state.get("verdict"), "kind": state.get("kind"),
            "gain": state.get("gain"), "partial_gain": state.get("partial_gain"),
            "fixed_count": state.get("fixed_count"), "regressed_count": state.get("regressed_count"),
            "working": short(state["working"]) if state.get("working") else None,
            "released": short(state["released"]) if state.get("released") else None,
            "reasons": state.get("reasons") or []}


def rounds_report(activity) -> list[dict]:
    """Each round as rounds/r<i>/round.json, the ledger and the budget book hold it (no timestamps). The improver's
    modules are loaded only when there is a round to read (a phase before the rounds stays off its path)."""
    paths = sorted((activity.root / "rounds").glob("r*/round.json"), key=lambda p: int(p.parent.name[1:]))
    if not paths:
        return []
    from ..improver.allocate import Focus
    from ..improver.diagnose import Cause
    from ..improver.ledger import Ledger
    from ..improver.screen import ScreenResult
    ledger = Ledger(activity.root)
    book = activity.budgets._read()
    out = []
    for path in paths:
        state = json.loads(path.read_text(encoding="utf-8"))
        steps = state.get("steps") or {}
        causes = [Cause.from_json(activity.objects.get(r)) for r in (steps.get("diagnosed") or {}).get("causes") or ()]
        focus = Focus.from_json(activity.objects.get(steps["focused"]["focus"])) if steps.get("focused") else None
        verified = [{"dir": r.get("dir"), "kind": r.get("kind"), "passed": r.get("passed"),
                     **({"outcome": r["outcome"]} if r.get("outcome") else {})}
                    for r in ledger.rows(stage="verified", round_id=state["round_id"])]
        screens = []
        for r in (steps.get("screened") or {}).get("screens") or ():
            sc = ScreenResult.from_json(activity.objects.get(r["screen"]))
            screens.append({"dir": r["dir"], "outcome": sc.outcome, "fixed": len(sc.fixed),
                            "unconditioned": len(sc.unconditioned), "regressed": len(sc.regressed),
                            "net_flips": sc.net_flips, "holdout_score": sc.holdout_score, "holdout": len(sc.holdout),
                            "best_so_far": sc.best_so_far, "accepted": sc.accepted, "reasons": list(sc.reasons)})
        accepted = steps.get("accepted") or {}
        scope = f"{activity.scope_id}:round-{state['index']}"
        used = activity.budgets.usage(scope, book) if scope in book.get("scopes", {}) else {}
        out.append({"round": state["round_id"], "status": state.get("status"), "stop": state.get("stop"),
                    "causes": {c.name: len(c.tasks) for c in causes},
                    "focus": [{"cause": r["name"], "tasks": len(r["tasks"]), "kinds": [k["kind"] for k in r["kinds"]]}
                              for r in focus.ranked] if focus else [],
                    "require_code": bool(focus and focus.require_code),
                    "candidates": verified, "ladder": (steps.get("verified") or {}).get("ladder") or {},
                    "screens": screens, "best": (steps.get("screened") or {}).get("best"),
                    "installed": short(accepted["installed"]) if accepted.get("installed") else None,
                    "spend": {k: round(v, 6) for k, v in used.items() if v}})
    return out


def c1_difference(activity, plan: dict, plan_ref: str, test: dict) -> dict | None:
    """W* - W0 on the final's test tasks: per task the mean success of each arm, the paired difference and its 95%
    interval resampled over families (evaluate.gate.paired_bootstrap), in points."""
    from ..evaluate.gate import paired_bootstrap
    arms = plan.get("arms") or {}
    if "W_star" not in arms or "W0" not in arms:
        return None
    means = {}
    for arm in ("W_star", "W0"):
        rows = [r for r in _latest([r for r in test.get(arm, []) if str(r.pair_key).startswith(f"{plan_ref}/")])
                if r.validity == "valid"]
        by_task: dict = {}
        for r in rows:
            by_task.setdefault(r.task_id, []).append(float(r.success))
        means[arm] = {t: sum(v) / len(v) for t, v in by_task.items()}
    if not means["W_star"] or not means["W0"]:
        return None
    tasks = sorted(set(means["W_star"]) & set(means["W0"]))
    got = paired_bootstrap(means["W_star"], means["W0"], {t: activity.splits.family_of(t) for t in tasks},
                           seed=int(activity.k["gate"].get("order_seed") or 0))
    pts = (lambda v: None if v is None else round(100 * v, 1))
    return {"tasks": got["tasks"], "families": got["groups"], "points": pts(got["point"]), "low": pts(got["low"]),
            "high": pts(got["high"]), "confidence": got["confidence"]}


def build_report(activity) -> dict:
    train = _runs_by_candidate(activity, "train")
    dev = _runs_by_candidate(activity, "dev")
    baseline = {"train": _brief(summarise(_latest(train["base"]))) if "base" in train else None,
                "dev": [_brief(summarise(_latest(rows))) for name, rows in sorted(dev.items())
                        if name == "base" or name.startswith("base:reading-")]}
    finals = []
    for path in sorted((activity.root / "rsi" / "finalization").glob("*.json")):
        state = json.loads(path.read_text(encoding="utf-8"))
        plan = activity.objects.get(state["plan"])
        arms = {}
        test = _runs_by_candidate(activity, "test")
        for arm in sorted(plan.get("arms") or {}):
            rows = [r for r in test.get(arm, []) if str(r.pair_key).startswith(f"{state['plan']}/")]
            arms[arm] = _brief(summarise(_latest(rows))) if rows else None
        finals.append({"experiment": plan.get("experiment_id"), "plan": short(state["plan"]),
                       "status": state.get("status"), "claims": plan.get("claims"), "arms": arms,
                       "c1": c1_difference(activity, plan, state["plan"], test)})
    events = list(read_jsonl(activity.events.path))
    return {"activity": {"scope": activity.scope_id, "core": short(activity.core_ref), "preset": activity.core.preset,
                         "attempt_version": activity.core.attempt_version,
                         "improver_version": activity.core.improver_version, "domain": activity.domain,
                         "split": activity.splits.hash(), "counts": activity.splits.counts()},
            "release": short(activity.releases.active_ref()) if activity.releases.active_ref() else None,
            "released": short(activity.releases.released_ref()) if activity.releases.released_ref() else None,
            "baseline": baseline, "rounds": rounds_report(activity), "gate": gate_report(activity),
            "memory": memory_report(activity),
            "final": finals,
            "test_reads": sum(1 for e in events if e.get("event") == "test_read"),
            "spend": spend(activity)}


def render_markdown(report: dict) -> str:
    a = report["activity"]
    lines = [f"# {a['scope']}", "",
             f"K {a['core']} (preset {a['preset']}, attempt code {a['attempt_version']}, improver code "
             f"{a['improver_version']}), domain {a['domain']}, split {a['split']}",
             f"working line {report['release']}, release line {report.get('released')}", "", "## Baseline (W0)"]
    b = report["baseline"]
    if b["train"]:
        lines.append(f"- train: {b['train']['passed']}/{b['train']['tasks']} passed, pass rate {b['train']['pass_rate']}")
    for i, d in enumerate(b["dev"], 1):
        lines.append(f"- dev reading {i}: {d['passed']}/{d['tasks']} passed, pass rate {d['pass_rate']}, "
                     f"cost/attempt {d['cost_per_attempt']}")
    if not b["train"] and not b["dev"]:
        lines.append("- not run")
    lines += ["", "## Memory"]
    m = report["memory"]
    if m is None:
        lines.append("- no stream")
    else:
        ro = m["readout"] or {}
        lines.append(f"- stream {m['status']}: {m['processed']}/{m['planned']} train tasks ({m['lost']} lost), "
                     f"{m['dropped']} memories dropped at the checks")
        if ro:
            lines.append(f"- D {ro.get('D')}, B {ro.get('B')}, halves {ro.get('first_half')} -> {ro.get('second_half')} "
                         f"(P {ro.get('p_second_half_better')}), entries {ro.get('entries')}")
        if m["snapshot"]:
            s = m["snapshot"]
            lines.append(f"- snapshot {s['outcome']}" + (f": {s['why']}" if s.get("why") else
                                                         f" {short(s['package_ref'])}" if s.get("package_ref") else ""))
    lines += ["", "## Rounds"]
    for r in report["rounds"]:
        lines.append(f"- {r['round']} {r['status']}" + (f" ({r['stop']})" if r.get("stop") else "")
                     + f": causes {r['causes']}; focus "
                     + ", ".join(f"{f['cause']}->{'/'.join(f['kinds'][:3])}" for f in r["focus"])
                     + (" (a tool or hook required)" if r.get("require_code") else ""))
        if r["candidates"]:
            lines.append("  candidates " + ", ".join(
                f"{c['dir']} ({c['kind']}, {c.get('outcome') or ('passed' if c['passed'] else 'failed')})"
                for c in r["candidates"]) + f"; rungs passed {r['ladder']}")
        for sc in r["screens"]:
            lines.append(f"  screen {sc['dir']}: {sc['outcome']}, held out {sc['holdout_score']}/{sc['holdout']} "
                         f"(best so far {sc['best_so_far']}), fixed {sc['fixed']} (unconditioned "
                         f"{sc['unconditioned']}), regressed {sc['regressed']}, net {sc['net_flips']}: "
                         + ("accepted" if sc["accepted"] else "rejected" + (f" ({'; '.join(sc['reasons'])})"
                                                                           if sc["reasons"] else "")))
        if r["installed"]:
            lines.append(f"  installed {r['best']} on the working line as {r['installed']}")
        lines.append(f"  spend {r['spend']}")
    if not report["rounds"]:
        lines.append("- none run")
    lines += ["", "## Release gate"]
    g = report.get("gate")
    if not g:
        lines.append("- not run")
    elif g["status"] != "done":
        lines.append(f"- {g['status']}")
    else:
        lines.append(f"- working line {g['working']} against W0 on dev: {g['verdict']} ({g['kind']}), gain {g['gain']}, "
                     f"partial {g['partial_gain']}, dev fixed {g['fixed_count']} broke {g['regressed_count']}"
                     + (f"; released {g['released']}" if g["released"] else "; no release")
                     + (f"; {'; '.join(g['reasons'])}" if g["reasons"] and not g["released"] else ""))
    lines += ["", "## Final"]
    for f in report["final"]:
        lines.append(f"- {f['experiment']} ({f['plan']}, {f['status']}): " + ", ".join(
            f"{arm} {s['passed']}/{s['tasks']}" if s else f"{arm} not run" for arm, s in f["arms"].items()))
        c = f.get("c1")
        if c:
            lines.append(f"  W* - W0: {c['points']} points [{c['low']}, {c['high']}] ({c['confidence']:.0%}, "
                         f"{c['tasks']} tasks in {c['families']} families)")
    if not report["final"]:
        lines.append("- no final plan sealed")
    lines += [f"- test read {report['test_reads']} time(s) by this activity's final", "", "## Spend"]
    s = report["spend"]
    lines.append(f"- root {s['root']} of {s.get('limits')}")
    for phase, used in s["phases"].items():
        lines.append(f"- {phase}: {used}")
    return "\n".join(lines) + "\n"


def write_report(activity, report: dict) -> dict:
    json_path, md_path = activity.root / "report.json", activity.root / "report.md"
    write_json(json_path, report)
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}
