"""Command line: ouroloop bench | freeze | export.

The training stack's commands (decide, smoke --jev, host) are `python -m ouroloop.train` (ouroloop/train/cli.py). The
v1 commands (run, suite, demo, research, evolve, deploy, plan, calibrate, gate, models, serve, hook, ledger, replay)
are on tag archive-2026-10-08."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config


def cmd_bench(args) -> int:
    """A benchmark phase on a configured domain. Exit 1 means a check failed, not that the agent scored badly.

    Each phase takes only the arguments that mean something for it, so `--checks-only` cannot quietly turn an
    improvement round into something else.
    """
    from .adapters.automationbench import UpstreamError
    from .bench import run_phase
    if getattr(args, "checks_only", False) and args.phase != "smoke":
        # A flag a phase does not read must not be taken as a promise: `--checks-only` on a paid phase would run
        # that phase for real (2026-10-05: a pilot cycle started paying under it).
        print(f"ouroloop bench: --checks-only is a smoke option; --phase {args.phase} would ignore it and run "
              f"for real. Run `--phase smoke --checks-only` for the startup checks.", file=sys.stderr)
        return 2
    cfg = config.load(args.config)
    if getattr(args, "concurrency", None) is not None:
        # M5 §2 (v0.3): the attempts in flight for this run only. A setting of the machine — it enters no frozen
        # plan's identity, so lowering it on a resume changes nothing about the comparison being finished.
        cfg = {**cfg, "bench": {**dict(cfg.get("bench") or {}), "concurrency": int(args.concurrency)}}
    kw = {"smoke": dict(tasks=args.task, limit=args.limit, run_agent=not args.checks_only,
                        replace_splits=args.replace_splits),
          "baseline": dict(tasks=args.task, limit=args.limit),
          "collect": dict(tasks=args.task, repeats=args.repeats),
          "improve": dict(rounds=args.rounds, reason=args.reason),
          "research": dict(campaign_ref=args.plan),
          "evaluate": dict(plan_ref=args.plan),
          "report": {}, "cycle": dict(until=args.until),
          # v0.5 (docs/design/V0_5_PLAN.md §3): train-only phases of a v5 activity
          "library": {}, "learn": {}}[args.phase]
    try:
        report = run_phase(cfg, Path(args.config).resolve().parent, phase=args.phase,
                           **{k: v for k, v in kw.items() if v is not None})
    except (UpstreamError, NotImplementedError, ValueError, TypeError) as e:
        print(f"ouroloop bench: {e}", file=sys.stderr)
        return 2
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"report: {args.out}")
    return 0 if report.get("ok", True) else 1


def cmd_freeze(args) -> int:
    """Freeze the [freeze.*] tables of a config into workspace objects, and print the [rsi] lines naming them."""
    from .adapters.automationbench import UpstreamError
    from .bench import freeze
    cfg = config.load(args.config)
    try:
        freeze(cfg, Path(args.config).resolve().parent)
    except (UpstreamError, ValueError, TypeError) as e:
        print(f"ouroloop freeze: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_export(args) -> int:
    """Export the runnable subset of an installed release as a minimal app (docs/modules/03_EVALUATION_RELEASE.md
    §4). The active release unless one is named."""
    from .bench import open_activity
    from .release import NotAuthorized, export_deployment
    cfg = config.load(args.config)
    try:
        activity = open_activity(cfg, Path(args.config).resolve().parent, bootstrap=False, out=lambda _line: None)
        release = args.release or activity.releases.active()["release_ref"]
        manifest = export_deployment(activity.releases, release, Path(args.to))
    except (NotAuthorized, ValueError) as e:
        print(f"ouroloop export: {e}", file=sys.stderr)
        return 2
    print(f"exported   {release} -> {args.to} (manifest {manifest})")
    return 0


def main(argv: list[str] | None = None) -> int:
    from .bench import PHASES
    config.load_dotenv(".env")
    p = argparse.ArgumentParser(prog="ouroloop")
    sub = p.add_subparsers(dest="cmd", required=True)
    bn = sub.add_parser("bench", help="AutomationBench: check the runner against upstream and run full tasks")
    bn.set_defaults(fn=cmd_bench)
    bn.add_argument("--config", required=True, help="e.g. examples/automationbench/claude-v4-finance.toml")
    bn.add_argument("--phase", choices=list(PHASES), default="smoke",
                    help="smoke: versions, frozen splits, contract checks, input audit and a few dev attempts; "
                         "then baseline, collect, improve, research, evaluate, report")
    bn.add_argument("--task", action="append", help="only this task; repeatable (development tasks only)")
    bn.add_argument("--limit", type=int, help="how many tasks to attempt (default: [bench] smoke_tasks)")
    bn.add_argument("--checks-only", action="store_true", help="run the checks and call no model")
    bn.add_argument("--replace-splits", action="store_true",
                    help="freeze the task groups again; refused once the workspace holds results")
    bn.add_argument("--out", help="write the phase report as JSON")
    bn.add_argument("--concurrency", type=int,
                    help="attempts in flight at once for this run, over [bench] concurrency; a machine setting that "
                         "enters no frozen plan (M5 §2)")
    bn.add_argument("--rounds", type=int, help="improve: how many worker rounds (default: the frozen schedule); "
                                               "on a cycle activity whose last segment has ended, that many more "
                                               "rounds, recorded as an extension beyond the frozen schedule")
    bn.add_argument("--reason", help="improve --rounds on an ended segment: why the frozen schedule is extended "
                                     "(required; recorded with the extension)")
    bn.add_argument("--repeats", type=int, help="collect: attempts per training task")
    bn.add_argument("--plan", help="research/evaluate: the frozen plan to run, instead of the one in [rsi]")
    bn.add_argument("--until", help="cycle: stop after this step and pay for nothing past it: baseline, segment-N, "
                                    "segment-N/round-K or slot-N; running the cycle again continues from there")
    fz = sub.add_parser("freeze", help="freeze the plans in [freeze.*] (task plan, schedule, budget, final plan) "
                                       "into the workspace and print the [rsi] lines that name them")
    fz.set_defaults(fn=cmd_freeze)
    fz.add_argument("--config", required=True, help="e.g. examples/automationbench/claude-v4-finance.toml")
    ex = sub.add_parser("export", help="export an installed release as a minimal app: W, the package's run bundle "
                                       "and the model binding; no research policy, data or tests")
    ex.add_argument("--config", default="ouroloop.toml")
    ex.add_argument("--release", default=None, help="an installed release ref (default: the active one)")
    ex.add_argument("--to", required=True, help="the directory to write the app into")
    ex.set_defaults(fn=cmd_export)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
