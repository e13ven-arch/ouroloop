"""The merge's replay (docs/design/IMPROVER.md §8, §9.5; DESIGN §6.19, v1.9): before a merge of a frontier entry is
paid for, its hooks are replayed on the attempts the entry fixed.

finance-v8's merge narrowed the entry's hook until it no longer acted on the one held-out task the entry had fixed —
and the screen found that out for $30. The entry keeps, on the host, the session of the candidate's passing attempt on
each confirmed fix (FrontierEntry.fixed_sessions); the merge's hooks that do not call the model are replayed on those
sessions' events at their sites (replay.py's machinery, a read-only broker), and the merge must act on at least
`merge_min_acts` of them. What goes back to R is counts only: the tasks are held out. A merge with no replayable hook
(a tool, a hook that calls the model) is not replayable here; the screen's window holds the entry's fixes instead.
"""
from __future__ import annotations

import json

from ..components import components
from .replay import _triggers, site_events
from .unit import worker_files


def merge_check(files: dict[str, str], decl, ctx) -> dict:
    """The verdict's `merge` step for a declaration with merge_of: {passed, acts_on, of, outcome, detail}."""
    sessions = dict((ctx.merge_sessions or {}).get(decl.merge_of) or {})
    hooks = [c for c in decl.components if c.kind == "hook" and not c.uses_model]
    if not hooks:
        return {"passed": True, "acts_on": None, "of": len(sessions), "outcome": "not_replayable",
                "detail": "the merge has no hook to replay (a tool, or a hook that calls the model): the screen's "
                          "window holds the entry's fixed tasks"}
    if not sessions:
        return {"passed": True, "acts_on": None, "of": 0, "outcome": "not_replayable",
                "detail": "the entry keeps no recorded attempt of its fixes to replay on"}
    manifest = json.loads(files["manifest.json"])
    changed = {(c["name"], c.get("site")) for c in components(ctx.parent, files)
               if c["kind"] == "hook" and c["change"] != "deleted"}
    entries_by_site: dict = {}
    for comp in hooks:
        at_site = [h for h in manifest.get("hooks") or () if h.get("site") == comp.site and h.get("enabled", True)]
        entries = [(h["name"], h["entrypoint"], [x["name"] for x in at_site[:i]]) for i, h in enumerate(at_site)
                   if (h.get("name"), comp.site) in changed]
        if entries:
            entries_by_site[comp.site] = entries
    if not entries_by_site:
        return {"passed": False, "acts_on": 0, "of": len(sessions), "outcome": "no_hook",
                "detail": "none of the merge's declared hooks is a changed hook of the manifest at its site"}
    need = int(getattr(ctx.settings, "merge_min_acts", 1))
    session = ctx.worker.start()
    acts, errors = 0, []
    try:
        session.load(worker_files(files, tests=False))
        for task_id, sid in sorted(sessions.items()):
            events = site_events(ctx.evidence.trace, sid)
            hit = False
            for site, entries in entries_by_site.items():
                got, error = _triggers(session, entries, events[site], task_id=task_id, suite=ctx.suite,
                                       manifest=manifest, site=site)
                errors += [error] if error else []
                hit = hit or got
            acts += int(hit)
    finally:
        session.close()
    passed = acts >= min(need, len(sessions)) and not errors
    detail = (f"the merge acts on {acts} of the {len(sessions)} recorded attempts the entry fixed (at least "
              f"{min(need, len(sessions))})"
              + ("" if passed else ": the conditioned version no longer acts where the entry's mechanism fixed a "
                                   "held-out task — keep the condition that fired there (what the entry's hook said "
                                   "on those attempts is in inputs/frontier.json's fixes_evidence) and narrow only "
                                   "the regression shapes")
              + (f"; the hook failed: {errors[:3]}" if errors else ""))
    return {"passed": passed, "acts_on": acts, "of": len(sessions), "outcome": "ok" if passed else "lost_fixes",
            "detail": detail[:2000]}
