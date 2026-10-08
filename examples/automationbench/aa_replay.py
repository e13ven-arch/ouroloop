"""The A/A replay of ROADMAP v0.3 step 2's exit condition (docs/ROADMAP.md §3 v0.3 第 2 步, docs/modules/08 §3).

W0 was read on the Marketing dev split five times across the v0.1 and v0.2 activities (the baseline and two gate
control arms in `workspace-claude`, the baseline and one control arm in `workspace-claude-full`), and on the test
split four times (the two repeats of v0.1's final in `workspace-claude`, the two of final-2 in
`workspace-claude-full-final`; final-1's W0 arm in `workspace-claude-full` was cut short by its share and is not a
complete reading, so `w0_readings` leaves it out). Every pair of those readings is the same version
twice, so a gate rule that accepts one of them against the others accepts noise. The 2026-10-04 analysis found the
v0.1/v0.2 rule — a fresh control, min_gain 0.05, any drop a regression — accepting 4 of the 20 directional pairs.

This script replays the same readings under the v0.3 rule, through the production `judge` and nothing else:

    one directional pair = (the reading taken as the candidate's first reading, the reading taken as its
                            confirmation reading); the control is the pool of every other reading (M3 §2.1(a))
    the first reading is judged against the pooled control (min_gain 0.05, regression_drop 0.5, max_regression_rate
        as the preset's); a first reading that clears is "confirmed" by the second reading (M3 §2.1(b), second
        version of the rule): the candidate's per-task means are then over both readings, and the pair is accepted
        only when the confirmation reading alone also clears min_gain against the same pooled control (the first
        version asked it only to be ≥ 0, and accepted 4/20 dev pairs — the record below the numbers keeps both)

Five readings give 5 × 4 = 20 directional pairs with a three-reading pooled control; four test readings give 12
with a two-reading pool. The old rule is replayed beside it (candidate i against the single fresh reading j, any
drop a regression) so the 4/20 of the analysis is reproduced from the same records. The cost term is reported
separately (`accepted_with_cost_term`): the readings carry `cli_cost_usd`, and the same version costs about the
same each time, so it is not what tells noise apart and is not part of the exit number.

    .venv/bin/python examples/automationbench/aa_replay.py            # the three example workspaces
    .venv/bin/python examples/automationbench/aa_replay.py --json     # the same, as JSON

No model is called; nothing is written.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from ouroloop.evaluate.gate import GatePlan, TaskEvalPlan, judge  # noqa: E402
from ouroloop.evaluate.results import Results, TaskResult, pair_key  # noqa: E402
from ouroloop.evaluate.splits import SplitManifest  # noqa: E402

DOMAIN = "marketing"
WORKSPACES = (HERE / "workspace-claude", HERE / "workspace-claude-full", HERE / "workspace-claude-full-final")
# The v3 preset's gate (now activity/presets.py automationbench_v1's [gate]) less the cost term, which
# the exit condition does not name; `min_groups` 5 is far under the 25 single-task families of Marketing.
NEW_RULE = GatePlan(min_gain=0.05, min_groups=5, max_regression_rate=0.1, regression_drop=0.5,
                    bootstrap_replicates=200)
WITH_COST = GatePlan(min_gain=0.05, min_groups=5, max_regression_rate=0.1, regression_drop=0.5, cost_beta0=0.10,
                     cost_beta1=2.0, bootstrap_replicates=200)
OLD_RULE = GatePlan(min_gain=0.05, min_groups=5, max_regression_rate=0.1, bootstrap_replicates=200)


def w0_readings(workspaces=WORKSPACES, *, split: str = "dev") -> tuple[list[list[TaskResult]], dict, list[str]]:
    """-> (readings, families, task ids). A reading is one complete pass of W0 over the split: one run at one
    repeat index, every task valid. W0 is the worker the `base` dev run of each workspace pins — the same policy
    set in every workspace, since the activities started from the same model and the same empty package."""
    readings: list[list[TaskResult]] = []
    families: dict = {}
    tasks: list[str] = []
    for root in workspaces:
        results = Results(root, DOMAIN)
        if not results.path.exists():
            continue
        manifest = SplitManifest.load(results.manifest_path())
        tasks = tasks or list(manifest.ids(split))
        families.update({e.task_id: e.family_id for e in manifest.entries})
        runs = results.runs()
        base = [r for r in runs.values() if r.candidate == "base" and r.split == "dev"]
        w0 = {r.worker_ref for r in base} or {r.worker_ref for r in runs.values() if r.candidate == "base"}
        if len(w0) != 1:
            raise SystemExit(f"{root}: cannot name W0 from its base runs ({sorted(w0)})")
        (w0_ref,) = w0
        by_reading: dict[tuple[str, int], dict[str, TaskResult]] = {}
        for row in results.all():
            run = runs.get(row.run)
            if run is None or run.worker_ref != w0_ref or row.split != split or row.validity != "valid":
                continue
            by_reading.setdefault((row.run, row.attempt), {})[row.task_id] = row
        for key in sorted(by_reading, key=lambda k: (runs[k[0]].candidate, k)):
            rows = by_reading[key]
            if set(rows) >= set(tasks):
                readings.append([rows[t] for t in tasks])
    return readings, families, tasks


def rekeyed(reading: list[TaskResult], plan: TaskEvalPlan, index: int) -> list[TaskResult]:
    """The reading's rows under the replay plan's pair keys, each reading as its own run: what `judge` reads of a
    candidate arm or a pooled control, with the recorded success and cost untouched."""
    out = []
    for row in reading:
        data = {**row.to_json(), "attempt": 0, "run": f"reading-{index}",
                "attempt_id": f"reading-{index}/{row.task_id}/r0/subject#0",
                "pair_key": pair_key(plan.ref(), row.task_id, 0)}
        out.append(TaskResult.from_json(data))
    return out


def under(plan: TaskEvalPlan, rows: list[TaskResult]) -> list[TaskResult]:
    """The same rows keyed to another plan (another gate, hence another plan ref): a pair key names its plan, and
    `judge` reads a row under a foreign key as a missing pair."""
    return [TaskResult.from_json({**r.to_json(), "pair_key": pair_key(plan.ref(), r.task_id, 0)}) for r in rows]


def replay(readings: list[list[TaskResult]], families: dict, tasks: list[str], *, gate: GatePlan = NEW_RULE,
           old_gate: GatePlan = OLD_RULE, cost_gate: GatePlan | None = WITH_COST) -> dict:
    """Every directional pair of readings under the new rule and the old one. -> the counts and each pair's numbers."""
    plan = TaskEvalPlan(candidate_ref="package:sha256:" + "a" * 64, control_ref="package:sha256:" + "a" * 64,
                        core_ref="core:sha256:" + "0" * 64, split_ref="aa", task_ids=tuple(tasks), repeats=1,
                        gate_plan=gate.to_json(), control_pooling="pooled", confirmation_repeats=1)
    old_plan = TaskEvalPlan.from_json({**{k: v for k, v in plan.to_json().items() if k != "ref"},
                                       "gate_plan": old_gate.to_json(), "control_pooling": None,
                                       "confirmation_repeats": None})
    cost_plan = TaskEvalPlan.from_json({**{k: v for k, v in plan.to_json().items() if k != "ref"},
                                        "gate_plan": cost_gate.to_json()}) if cost_gate is not None else None
    keyed = [rekeyed(r, plan, i) for i, r in enumerate(readings)]
    out = {"readings": len(readings), "tasks": len(tasks),
           "pass_rates": [round(sum(r.success for r in reading) / len(reading), 4) for reading in readings],
           "pairs": [], "pairs_total": 0, "first_accepted": 0, "accepted": 0, "accepted_with_cost_term": 0,
           "old_rule_accepted": 0}
    for i in range(len(keyed)):
        for k in range(len(keyed)):
            if k == i:
                continue
            out["pairs_total"] += 1
            control = [row for n, reading in enumerate(keyed) if n not in (i, k) for row in reading]
            first = judge(plan, keyed[i], control, families, control_pooled=True)
            pair = {"candidate": i, "confirmation": k, "first_gain": first[2]["gain"], "first_verdict": first[0],
                    "verdict": first[0]}
            if first[0] == "accepted":
                out["first_accepted"] += 1
                final = judge(plan, keyed[i], control, families, control_pooled=True, confirmation=keyed[k],
                              first={"gain": first[2]["gain"], "verdict": first[0]})
                pair.update(pooled_gain=final[2]["gain"], confirmation_gain=final[2]["confirmation_gain"],
                            verdict=final[0], reasons=list(final[1]))
                out["accepted"] += final[0] == "accepted"
                if cost_plan is not None:
                    costed = judge(cost_plan, under(cost_plan, keyed[i]), under(cost_plan, control), families,
                                   control_pooled=True, confirmation=under(cost_plan, keyed[k]),
                                   first={"gain": first[2]["gain"], "verdict": first[0]})
                    pair["with_cost_term"] = costed[0]
                    out["accepted_with_cost_term"] += costed[0] == "accepted"
            # the v0.1/v0.2 rule on the same records: candidate i against the one fresh reading k
            old = judge(old_plan, under(old_plan, keyed[i]), under(old_plan, keyed[k]), families)
            pair["old_rule"] = {"gain": old[2]["gain"], "regression_rate": old[2]["regression_rate"],
                                "verdict": old[0]}
            out["old_rule_accepted"] += old[0] == "accepted"
            out["pairs"].append(pair)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace", action="append",
                        help="a workspace root; default: the three example workspaces (workspace-claude, "
                             "workspace-claude-full, workspace-claude-full-final)")
    parser.add_argument("--json", action="store_true", help="print the replay as JSON")
    args = parser.parse_args(argv)
    workspaces = tuple(Path(w) for w in args.workspace) if args.workspace else WORKSPACES
    report = {}
    for split in ("dev", "test"):
        readings, families, tasks = w0_readings(workspaces, split=split)
        if len(readings) < 3:
            report[split] = {"readings": len(readings), "note": "fewer than three readings: no pooled control"}
            continue
        report[split] = replay(readings, families, tasks)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    for split, r in report.items():
        if "note" in r:
            print(f"{split}: {r['note']}")
            continue
        print(f"{split}: {r['readings']} readings of W0 over {r['tasks']} tasks, pass rates {r['pass_rates']}")
        print(f"  old rule (fresh control, any drop):           {r['old_rule_accepted']}/{r['pairs_total']} accepted")
        print(f"  new rule, first reading vs pooled control:   {r['first_accepted']}/{r['pairs_total']} cleared")
        print(f"  new rule, after the confirmation reading:    {r['accepted']}/{r['pairs_total']} accepted")
        print(f"  the same with the cost term (β0 .10, β1 2.0): {r['accepted_with_cost_term']}/{r['pairs_total']}")
        for p in r["pairs"]:
            if p["first_verdict"] == "accepted" or p["old_rule"]["verdict"] == "accepted":
                print(f"    candidate reading {p['candidate']} / confirmation {p['confirmation']}: first "
                      f"{p['first_gain']:+.3f} ({p['first_verdict']})"
                      + (f", pooled {p['pooled_gain']:+.3f}, confirmation {p['confirmation_gain']:+.3f} -> "
                         f"{p['verdict']}" if "pooled_gain" in p else "")
                      + f"; old rule vs reading {p['confirmation']}: {p['old_rule']['gain']:+.3f} "
                        f"({p['old_rule']['verdict']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
