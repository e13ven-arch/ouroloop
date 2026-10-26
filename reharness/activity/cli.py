"""Command line (docs/design/ACTIVITY.md §3): reharness bench | freeze | export.

    reharness bench --config C --phase P [--until U] [--round I] [--task T] [--limit N] [--checks-only] [--out J]
                   [--concurrency N]
    reharness freeze --config C
    reharness export --config C [--release R] --to DIR

The training stack's commands are `python -m reharness.train`. Exit 1 means a check failed, not that the agent scored
badly; exit 2 means the command could not run as asked.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

from ..contracts import load_dotenv
from .config import ConfigError, load_config
from .phases import PHASES

CYCLE = "cycle"


def _open(args, *, freeze_k: bool = True):
    from .workspace import open_activity
    config = load_config(args.config)
    if getattr(args, "concurrency", None) is not None:
        # a setting of the machine: it enters no frozen plan, so a resume may lower it
        config = {**config, "bench": {**dict(config.get("bench") or {}), "concurrency": int(args.concurrency)}}
    return open_activity(config, freeze_k=freeze_k)


def _lock(root: Path):
    """One process per activity: a paid phase holds <root>/rsi/activity.lock while it runs (two processes would
    both pay for the same missing attempts). -> the open file, or None when another process holds it."""
    import fcntl
    path = Path(root) / "rsi" / "activity.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def task_index(until: str | None) -> int | None:
    """`--until task-K` of the repetitive stream: the task after which it stops."""
    if until is None:
        return None
    m = re.fullmatch(r"task-(\d+)", str(until))
    if not m:
        raise ValueError(f"--until {until!r}: the repeat phase stops after a task, `--until task-K`")
    return int(m.group(1))


def batch_index(until: str | None) -> int | None:
    """`--until batch-K` of the stream: the batch after which it stops."""
    if until is None:
        return None
    m = re.fullmatch(r"batch-(\d+)", str(until))
    if not m:
        raise ValueError(f"--until {until!r}: the stream stops after a batch, `--until batch-K`")
    return int(m.group(1))


def cmd_bench(args) -> int:
    from ..adapters.automationbench import UpstreamError
    from . import phases
    if args.checks_only and args.phase != "smoke":
        print(f"reharness bench: --checks-only is a smoke option; --phase {args.phase} would ignore it and run for real",
              file=sys.stderr)
        return 2
    activity = lock = None
    try:
        activity = _open(args, freeze_k=not (args.phase == "smoke" and args.checks_only))
        if args.phase not in ("report", "export") and not (args.phase == "smoke" and args.checks_only):
            lock = _lock(activity.root)
            if lock is None:
                print(f"reharness bench: another reharness process is running this activity ({activity.root}): let it "
                      f"finish or stop it with one Ctrl-C, then run again", file=sys.stderr)
                return 2
        if args.phase == "smoke":
            report = phases.smoke(activity, tasks=args.task, limit=args.limit, checks_only=args.checks_only)
        elif args.phase == "freeze":
            report = phases.freeze(activity)
        elif args.phase == "baseline":
            report = phases.baseline(activity, limit=args.limit)
        elif args.phase == "library":
            report = phases.library(activity)
        elif args.phase == "round":
            report = asdict(phases.round_(activity, int(args.round or 0)))
        elif args.phase == "stream":
            report = phases.stream(activity, until=batch_index(args.until))
            fe = report.get("first_encounter") or {}
            print(f"stream     {report['status']}" + (f" at {report['at']}" if report.get("at") else "")
                  + (f": {report['reason']}" if report.get("reason") else "")
                  + f"; first encounters {fe.get('passed')}/{fe.get('tasks')}")
        elif args.phase == "repeat":
            report = phases.repeat(activity, arm=args.arm or "learn", until=task_index(args.until))
            rows = (report.get("summary") or {}).get(report["arm"]) or []
            print(f"repeat     {report['arm']}: {report['tasks']} tasks; "
                  + ", ".join(f"e{r['encounter']} {r['passed']}/{r['tasks']}" for r in rows)
                  + f"; spent ${report.get('spent') or 0:.2f}")
        elif args.phase == "gate":
            report = phases.gate(activity)
        elif args.phase == "release":
            report = phases.release(activity)
        elif args.phase == "evaluate":
            report = phases.evaluate(activity)
        elif args.phase == "report":
            report = phases.report(activity)
        elif args.phase == "export":
            print("reharness bench: export is `reharness export --config C --to DIR`", file=sys.stderr)
            return 2
        else:
            report = phases.cycle(activity, until=args.until)
            used = (activity.root_account().remaining().get("used") or {}).get("usd")
            print(f"cycle      {report['status']}" + (f" at {report['at']}" if report.get("at") else "")
                  + (f": {report['reason']}" if report.get("reason") else "")
                  + (f"; the activity has spent ${used:.2f}" if used is not None else ""))
    except (UpstreamError, ConfigError, NotImplementedError, ValueError, RuntimeError) as e:
        print(f"reharness bench: {e}", file=sys.stderr)
        return 2
    finally:
        if activity is not None:
            activity.close()            # the memory's sidecar, when a phase started one
        if lock is not None:
            lock.close()
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"report: {args.out}")
    return 0 if report.get("ok", True) else 1


def cmd_freeze(args) -> int:
    from ..adapters.automationbench import UpstreamError
    from . import phases
    lock = None
    try:
        activity = _open(args)
        lock = _lock(activity.root)
        if lock is None:
            print(f"reharness freeze: another reharness process is running this activity ({activity.root}): let it "
                  f"finish first", file=sys.stderr)
            return 2
        phases.freeze(activity)
    except (UpstreamError, ConfigError, ValueError, RuntimeError) as e:
        print(f"reharness freeze: {e}", file=sys.stderr)
        return 2
    finally:
        if lock is not None:
            lock.close()
    return 0


def cmd_export(args) -> int:
    from ..evaluate.release import NotAuthorized
    from . import phases
    try:
        phases.export(_open(args), args.release, Path(args.to))
    except (NotAuthorized, ConfigError, ValueError) as e:
        print(f"reharness export: {e}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(".env")
    p = argparse.ArgumentParser(prog="reharness")
    sub = p.add_subparsers(dest="cmd", required=True)
    bn = sub.add_parser("bench", help="run a phase of an activity")
    bn.set_defaults(fn=cmd_bench)
    bn.add_argument("--config", required=True, help="e.g. examples/automationbench/quickstart.toml")
    bn.add_argument("--phase", choices=[*PHASES, CYCLE], default="smoke")
    bn.add_argument("--until", help="cycle: stop after baseline, library, round-K or gate (the release gate); "
                                    "stream: stop after batch-K")
    bn.add_argument("--round", type=int, help="round: which round of the schedule")
    bn.add_argument("--arm", choices=["learn", "control"], help="repeat: which arm of the repetitive stream to run")
    bn.add_argument("--task", action="append", help="smoke: only this dev task; repeatable")
    bn.add_argument("--limit", type=int, help="smoke and baseline: at most this many tasks")
    bn.add_argument("--checks-only", action="store_true", help="smoke: run the checks and call no model")
    bn.add_argument("--out", help="write the phase's report as JSON")
    bn.add_argument("--concurrency", type=int, help="attempts in flight for this run; enters no frozen plan")
    fz = sub.add_parser("freeze", help="freeze the gate template, the budget root and the final plan; print [rsi]")
    fz.set_defaults(fn=cmd_freeze)
    fz.add_argument("--config", required=True)
    ex = sub.add_parser("export", help="export an installed release as an app")
    ex.set_defaults(fn=cmd_export)
    ex.add_argument("--config", required=True)
    ex.add_argument("--release", default=None, help="a released ref (default: the release line's W*)")
    ex.add_argument("--to", required=True)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
