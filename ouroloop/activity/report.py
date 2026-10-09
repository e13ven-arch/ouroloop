"""The activity's report (docs/design/ACTIVITY.md §3, §9): built offline from what is on disk, no model called, the
same bytes every time it is written (nothing in it reads the clock).

    activity   scope, K, preset, code version, the split's hash and counts
    baseline   W0's train reading and its dev readings
    rounds     each round of the improver (stage 3): causes, candidates, rungs passed, the gate's reading, the install
    memory     the memory stream's readout (D, the halves, the entries), what it dropped and lost, its snapshot
    final      each arm of the sealed final, and how many times this domain's test was read
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
    for name in sorted(activity.budgets.subtree(activity.scope_id, book) - {activity.scope_id}):
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
    return {"status": ledger.get("status"), "processed": len(ledger.get("processed") or {}),
            "planned": len((ledger.get("plan") or {}).get("order") or ()), "lost": len(ledger.get("lost") or {}),
            "readout": ledger.get("readout"), "counters": ledger.get("counters"),
            "dropped": len(ledger.get("dropped") or ()), "imports": len(ledger.get("imports") or ()),
            "snapshot": None if last is None else {k: last.get(k) for k in ("outcome", "why", "package_ref",
                                                                            "snapshot_digest")}}


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
                       "status": state.get("status"), "claims": plan.get("claims"), "arms": arms})
    events = list(read_jsonl(activity.events.path))
    return {"activity": {"scope": activity.scope_id, "core": short(activity.core_ref), "preset": activity.core.preset,
                         "code_version": activity.core.code_version, "domain": activity.domain,
                         "split": activity.splits.hash(), "counts": activity.splits.counts()},
            "release": short(activity.releases.active_ref()) if activity.releases.active_ref() else None,
            "baseline": baseline, "rounds": [], "memory": memory_report(activity), "final": finals,
            "test_reads": sum(1 for e in events if e.get("event") == "test_read"),
            "spend": spend(activity)}


def render_markdown(report: dict) -> str:
    a = report["activity"]
    lines = [f"# {a['scope']}", "",
             f"K {a['core']} (preset {a['preset']}, code {a['code_version']}), domain {a['domain']}, split {a['split']}",
             f"active release {report['release']}", "", "## Baseline (W0)"]
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
    lines += ["", "## Rounds", "- none (the improver comes with stage 3)", "", "## Final"]
    for f in report["final"]:
        lines.append(f"- {f['experiment']} ({f['plan']}, {f['status']}): " + ", ".join(
            f"{arm} {s['passed']}/{s['tasks']}" if s else f"{arm} not run" for arm, s in f["arms"].items()))
    if not report["final"]:
        lines.append("- no final plan sealed")
    lines += [f"- test read {report['test_reads']} time(s)", "", "## Spend"]
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
