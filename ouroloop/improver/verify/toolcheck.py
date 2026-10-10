"""V2a, the toolcheck (docs/design/IMPROVER.md §8, §8.1; DESIGN §6.6): a new tool is checked by running it.

Two proofs per claimed train task, either enough; the rung passes at `toolcheck_min_pass` tasks:

- a script: R's `check(ctx, tools)` runs in the worker with the candidate loaded, calling the candidate's tools
  (`tools[name](args)`) against that task's fresh world through a writable broker; then the task's own grader scores
  the world, must-not assertions included. The task passes when the world passes and the script called the tool;
- a probe (IMPROVER §8.1): `probes[task]` is a list of calls of the tool, args taken from the task's trajectory and an
  optional `expect` substring; the host writes the script itself, and the task passes when every call returns
  without error and its output holds `expect`. It proves the tool runs on the real interface and answers what R
  expects; it does not prove the task can be done with it (the script does), nor that the actor will use it (V3).

A task with both is judged by its script. What goes back to R is whether each task passed, by which proof, the failed
assertions' types (a script) or the probe's error (truncated) — never the grader's parameters."""
from __future__ import annotations

import json

from ...runtime.broker import Broker
from ...runtime.worker import PackageCallError, smoke_view
from .unit import worker_files


def probe_script(tool: str, probes: list[dict]) -> str:
    """The host's own check script for a tool's probes: each call must return without error, with `expect` in its
    output when one is given."""
    lines = ["import json", "", "def check(ctx, tools):"]
    for i, probe in enumerate(probes):
        args = json.dumps(json.dumps(probe.get("args") or {}))
        lines += [f"    out = tools[{tool!r}](json.loads({args}))",
                  "    out = out if isinstance(out, dict) else {'output': out}",
                  f"    if out.get('is_error'):",
                  f"        raise RuntimeError('probe {i}: the tool answered an error: ' + str(out.get('output'))[:200])"]
        if probe.get("expect"):
            want = json.dumps(probe["expect"])
            lines += [f"    if {want} not in str(out.get('output')):",
                      f"        raise RuntimeError('probe {i}: the output lacks ' + {want} + ': ' "
                      f"+ str(out.get('output'))[:200])"]
    return "\n".join(lines) + "\n"


def toolcheck(files: dict[str, str], decl, ctx) -> dict:
    manifest = json.loads(files["manifest.json"])
    tools = [t for t in manifest.get("tools") or () if t.get("enabled", True)]
    if not decl.scripts and not decl.probes:
        return {"passed": False, "tasks": {}, "detail": "a tool candidate gives, for each train task it claims, a "
                                                        "probe (declaration.probes) or a toolcheck script "
                                                        "(declaration.scripts)"}
    run = worker_files(files, tests=False)
    results: dict = {}
    claimed = set(decl.predicted_affected) & set(ctx.failed_sessions or ())
    for task_id in sorted(set(decl.scripts) | set(decl.probes)):
        if task_id not in set(ctx.study_tasks or ctx.train_tasks):      # held out or not train: one answer
            results[task_id] = {"passed": False, "error": "not one of the train tasks you were given"}
            continue
        if task_id not in claimed:          # a proof per claimed train task the current W fails (DESIGN §6.6)
            results[task_id] = {"passed": False, "error": "not a task the declaration claims that the current W fails"}
            continue
        by = "script" if decl.scripts.get(task_id) else "probe"
        script = decl.scripts.get(task_id) or probe_script(decl.site, decl.probes[task_id])
        episode = ctx.suite.episode(task_id)
        session = ctx.worker.start()
        error = ""
        try:
            session.load(run)
            view = smoke_view(manifest, episode, session.limits.max_output_chars)
            try:
                answer = session.request({"op": "check", "script": script, "tools": tools, "view": view},
                                         broker=Broker(episode, read_only=False))
                called = list((answer or {}).get("called") or ())
            except PackageCallError as e:
                called, error = [], str(e)[:300]
            score = episode.score() if by == "script" else None
        finally:
            session.close()
            episode.close()
        used = decl.site in called
        if by == "script":
            failed = sorted({str(a.get("type")) for a in score.assertions if not a.get("passed")
                             and not a.get("excluded")})
            results[task_id] = {"passed": bool(score.success) and used and not error, "by": by, "called_tool": used,
                                "failed_kinds": failed, **({"error": error} if error else {})}
        else:
            results[task_id] = {"passed": used and not error, "by": by, "called_tool": used,
                                "probes": len(decl.probes[task_id]), **({"error": error} if error else {})}
    passes = sorted(t for t, r in results.items() if r["passed"])
    need = int(ctx.settings.toolcheck_min_pass)
    detail = (f"{len(passes)} of {len(results)} claimed train tasks pass with the tool (at least {need}): "
              + "; ".join(f"{t}: {'passed' if r['passed'] else 'failed'}"
                          + (f" by {r['by']}" if r.get("by") else "")
                          + ("" if r.get("called_tool", True) else f", the script never called {decl.site}")
                          + (f", failed checks {r['failed_kinds']}" if r.get("failed_kinds") else "")
                          + (f", {r['error']}" if r.get("error") else "")
                          for t, r in results.items()))
    return {"passed": len(passes) >= need, "tasks": results, "detail": detail[:3000]}
