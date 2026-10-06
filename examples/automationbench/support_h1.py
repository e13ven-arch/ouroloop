"""H1 of support's two-arm test (docs/design/V0_5_PLAN.md §3): the v0.5 arm's final W* against the v0.4 arm's final W*,
paired by task.

The two arms ran at equal caps and the same schedule, each in its own workspace, and each sealed its own final on
support's 25 test tasks x 3 before either final ran. This script reads the two completed finals off the records —
`rsi/finalization/<experiment>.json` (which attempt belongs to which arm) and the results store (each attempt's
pass/fail) — and computes the pre-registered reading:

    per task, the mean pass rate over its repeats in each arm; the paired difference W*_v5(t) - control(t);
    the point estimate is the mean over tasks; the 95% interval is the 2.5th and 97.5th percentiles of 2000
    bootstrap means over tasks (seed 1);
    lower bound > 0                       -> "v0.5 better"
    lower bound <= 0 and point <= 0       -> "not better"
    otherwise                             -> "uncertain"

The control is the v0.4 arm's final W* (arm `evolved`). The v0.4 arm runs its final only with an install
(V0_4_PLAN §3); without one its W* is W0, and the control is then the v0.5 final's own W0 arm (`base`), which reads the
same version on the same tasks in the same final. The v0.5 arm always runs its final (W* = W0 when it installed
nothing). Each arm's actual spend is printed beside H1 (the arms had equal caps, not equal spend).

    .venv/bin/python examples/automationbench/support_h1.py --v5 examples/automationbench/workspace-claude-v5-support \\
        --v4 examples/automationbench/workspace-claude-v4-support [--json]

No model is called; nothing is written.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

REPLICATES = 2000
SEED = 1


def final_outcomes(root, domain: str = "support") -> dict | None:
    """{"arms": {arm: {task: [0/1, ...]}}, "experiment_id", "plan"} of a workspace's completed final, or None when it
    has none. Valid test attempts only, by the arm the finalization state records for each attempt id."""
    from ouroloop.benchmark import Results
    directory = Path(root) / "rsi" / "finalization"
    states = [json.loads(p.read_text(encoding="utf-8")) | {"experiment_id": p.stem}
              for p in sorted(directory.glob("*.json"))] if directory.exists() else []
    completed = [s for s in states if s.get("status") == "completed"]
    if not completed:
        return None
    if len(completed) > 1:
        raise ValueError(f"{root}: {len(completed)} completed finals; H1 reads the one sealed for the two-arm test")
    state = completed[0]
    arm_of = {a["attempt_id"]: a["arm"] for a in state.get("attempts") or ()}
    arms: dict = {}
    for r in Results(root, domain).all():
        if r.attempt_id in arm_of and r.split == "test" and r.validity == "valid":
            arms.setdefault(arm_of[r.attempt_id], {}).setdefault(r.task_id, []).append(int(bool(r.success)))
    return {"arms": arms, "experiment_id": state["experiment_id"], "plan": state.get("plan")}


def spend(root) -> float | None:
    """The workspace's recorded spend in notional dollars: the usage of the budget book's root scopes (each with every
    phase under it), or None without a book."""
    from ouroloop.evolve.execution import Budgets
    budgets = Budgets(root)
    if not budgets.path.exists():
        return None
    book = budgets._read()
    roots = [name for name, entry in (book.get("scopes") or {}).items() if not entry.get("parent")]
    return round(sum(float(budgets.usage(name, book).get("usd", 0.0)) for name in roots), 4) if roots else None


def h1(subject: dict, control: dict, *, replicates: int = REPLICATES, seed: int = SEED) -> dict:
    """The paired reading over the tasks both arms read: point, 95% bootstrap interval, verdict."""
    tasks = sorted(set(subject) & set(control))
    if not tasks:
        raise ValueError("the two arms share no task")
    diffs = [sum(subject[t]) / len(subject[t]) - sum(control[t]) / len(control[t]) for t in tasks]
    point = sum(diffs) / len(diffs)
    rng = random.Random(seed)
    means = sorted(sum(diffs[rng.randrange(len(diffs))] for _ in diffs) / len(diffs) for _ in range(replicates))
    low, high = means[int(0.025 * replicates)], means[int(0.975 * replicates) - 1]
    verdict = "v0.5 better" if low > 0 else ("not better" if point <= 0 else "uncertain")
    return {"tasks": len(tasks), "point": round(point, 6), "interval95": [round(low, 6), round(high, 6)],
            "verdict": verdict, "replicates": replicates, "seed": seed,
            "subject_rate": round(sum(sum(subject[t]) / len(subject[t]) for t in tasks) / len(tasks), 6),
            "control_rate": round(sum(sum(control[t]) / len(control[t]) for t in tasks) / len(tasks), 6)}


def support_h1(v5_root, v4_root, *, domain: str = "support", replicates: int = REPLICATES, seed: int = SEED) -> dict:
    v5 = final_outcomes(v5_root, domain)
    if v5 is None or "evolved" not in v5["arms"]:
        raise ValueError(f"{v5_root}: the v0.5 arm always runs its final, and none is completed here")
    v4 = final_outcomes(v4_root, domain)
    if v4 is not None and "evolved" in v4["arms"]:
        control, source = v4["arms"]["evolved"], f"the v0.4 arm's final W* ({v4['experiment_id']})"
    else:
        if "base" not in v5["arms"]:
            raise ValueError(f"{v5_root}: the v0.4 arm has no final, and the v0.5 final has no W0 arm to pair with")
        control, source = v5["arms"]["base"], ("the v0.5 final's own W0 arm: the v0.4 arm installed nothing, so it "
                                               "has no final and its W* is W0")
    out = h1(v5["arms"]["evolved"], control, replicates=replicates, seed=seed)
    return {**out, "subject": f"the v0.5 arm's final W* ({v5['experiment_id']})", "control": source,
            "spend_usd": {"v5": spend(v5_root), "v4": spend(v4_root)}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--v5", required=True, help="the v0.5 arm's workspace root")
    parser.add_argument("--v4", required=True, help="the v0.4 arm's workspace root")
    parser.add_argument("--domain", default="support")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    out = support_h1(args.v5, args.v4, domain=args.domain)
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        print(f"H1  {out['subject']} vs {out['control']}")
        print(f"    {out['tasks']} tasks: {out['subject_rate']:.3f} vs {out['control_rate']:.3f}; paired difference "
              f"{out['point']:+.3f}, 95% interval [{out['interval95'][0]:+.3f}, {out['interval95'][1]:+.3f}] "
              f"({out['replicates']} replicates, seed {out['seed']}) -> {out['verdict']}")
        print(f"    actual spend: v0.5 arm ${out['spend_usd']['v5']}, v0.4 arm ${out['spend_usd']['v4']} "
              f"(equal caps, not equal spend)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
