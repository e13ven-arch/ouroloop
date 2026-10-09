"""V2a, the toolcheck (docs/design/IMPROVER.md §8; DESIGN §6.6): a new tool is checked by running it.

For each train task the declaration claims, R's script `check(ctx, tools)` runs in the worker with the candidate loaded,
calling the candidate's tools (`tools[name](args)`) against that task's fresh world through a writable broker; then
the task's own grader scores the world, must-not assertions included. A task passes when the world passes and the
script called the declared tool. The rung passes at `toolcheck_min_pass` tasks. What goes back to R is whether each
task passed and the failed assertions' types — never their parameters: the grader stays the host's.

It proves the tool is right and that a solution with it exists; whether the actor will use it is V3's question."""
from __future__ import annotations

import json

from ...runtime.broker import Broker
from ...runtime.worker import PackageCallError, smoke_view
from .unit import worker_files


def toolcheck(files: dict[str, str], decl, ctx) -> dict:
    manifest = json.loads(files["manifest.json"])
    tools = [t for t in manifest.get("tools") or () if t.get("enabled", True)]
    if not decl.scripts:
        return {"passed": False, "tasks": {}, "detail": "a tool candidate gives a toolcheck script for each train "
                                                        "task it claims (declaration.scripts)"}
    run = worker_files(files, tests=False)
    results: dict = {}
    for task_id, script in sorted(decl.scripts.items()):
        if task_id not in set(ctx.train_tasks):
            results[task_id] = {"passed": False, "error": "not a train task"}
            continue
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
            score = episode.score()
        finally:
            session.close()
            episode.close()
        failed = sorted({str(a.get("type")) for a in score.assertions if not a.get("passed")
                         and not a.get("excluded")})
        used = decl.site in called
        results[task_id] = {"passed": bool(score.success) and used and not error, "called_tool": used,
                            "failed_kinds": failed, **({"error": error} if error else {})}
    passes = sorted(t for t, r in results.items() if r["passed"])
    need = int(ctx.settings.toolcheck_min_pass)
    detail = (f"{len(passes)} of {len(results)} claimed train tasks pass with the tool (at least {need}): "
              + "; ".join(f"{t}: {'passed' if r['passed'] else 'failed'}"
                          + ("" if r.get("called_tool", True) else f", the script never called {decl.site}")
                          + (f", failed {r['failed_kinds']}" if r.get("failed_kinds") and not r["passed"] else "")
                          + (f", script error: {r['error']}" if r.get("error") else "")
                          for t, r in results.items()))
    return {"passed": len(passes) >= need, "tasks": results, "detail": detail[:3000]}
