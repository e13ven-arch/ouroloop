"""The verification ladder (docs/design/IMPROVER.md §8; DESIGN §6.6): each rung costs nothing paid and runs only when
the rung below passed. V0 static, V1 unit (the package's tests, probe and smoke in the worker), V2a toolcheck, V2b hook
replay, V2c reach. V3 (the paid train screen) and V4 (the dev gate) are screen.py's."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from ...contracts import Record, register
from ..declaration import Declaration

SUPPORT = {"tool": "V2a", "hook": "V2b", "skill": "V2c"}     # a supporting component's own rung (IMPROVER §7.1)
LADDERS = {"tool": ("V0", "V1", "V2a"), "hook": ("V0", "V1", "V2b"), "skill": ("V0", "V2c"), "prompt": ("V0",),
           "agent": ("V0", "V1"), "vendor": ("V0", "V1"), "config": ("V0", "V1"), "memory": ("V0",),
           "removal": ("V0",), "test": ("V0",)}


def ladder_for(kind: str) -> tuple:
    return LADDERS.get(kind, ("V0",))


@register
@dataclass(frozen=True)
class Verdict(Record):
    TYPE = "verdict"
    candidate_ref: str | None = None
    dir: str = ""
    steps: dict = field(default_factory=dict)     # rung -> {passed, detail}

    def passed(self, step: str | None = None) -> bool:
        if step is not None:
            return bool((self.steps.get(step) or {}).get("passed"))
        return bool(self.steps) and all(bool(v.get("passed")) for v in self.steps.values())

    def text(self) -> str:
        """The verdict as R reads it (run_check, a revision)."""
        lines = []
        for rung, v in self.steps.items():
            lines.append(f"{rung}: {'passed' if v.get('passed') else 'FAILED'}"
                         + (f" — {v.get('detail')}" if v.get("detail") else ""))
        return "\n".join(lines) or "nothing checked"


@dataclass
class VerifyContext:
    """What the ladder needs from the activity: the parent and seed packages, K, the object store, the worker, the
    suite (train episodes), the evidence (train transcripts), the forbidden literals, the train tasks' texts, an
    embedder for V2c, the improver's settings."""
    parent: object
    seed: object
    core: dict
    objects: object
    worker: object
    suite: object
    evidence: object
    forbidden: set
    train_tasks: tuple
    train_texts: dict
    embed: object
    settings: object
    fixture_task: str | None = None
    passed_sessions: dict = field(default_factory=dict)     # task_id -> session of a passing train attempt (V2b)
    failed_sessions: dict = field(default_factory=dict)     # task_id -> session of a failing train attempt (V2b)
    study_tasks: tuple = ()             # the train tasks R studies (predicted_affected names only these); () = all
    cause_ids: tuple = ()           # the causes this round assigned: a declaration names one of them


def supporting(files: dict[str, str], decl: Declaration, ctx: VerifyContext) -> list[tuple[str, Declaration]]:
    """(rung key, shadow declaration) for every supporting tool, hook or skill a candidate carries beside its declared
    component (IMPROVER §7.1): each climbs its own rung under a declaration that names it, keyed `V2x@<name>`."""
    if decl.fixed or decl.kind not in ("tool", "hook", "skill", "prompt"):
        return []
    from ..components import components
    out = []
    for c in components(ctx.parent, files):
        if c["kind"] not in SUPPORT or c.get("change") == "deleted":
            continue
        own = c["kind"] == decl.kind and (c.get("site") == decl.site if c["kind"] == "hook" else c["name"] == decl.site)
        if own:
            continue
        site = c.get("site") if c["kind"] == "hook" else c["name"]
        out.append((f"{SUPPORT[c['kind']]}@{c['name']}", replace(decl, kind=c["kind"], site=site)))
    return out


def run_ladder(files: dict[str, str], decl: Declaration, ctx: VerifyContext, *, rungs: tuple | None = None) -> Verdict:
    """Every rung of the candidate's kind in order, then the rung of each supporting component it carries; a failed
    rung ends the climb (the rest are not run)."""
    from .reach import reach_check
    from .replay import replay_check
    from .static import static_check
    from .toolcheck import toolcheck
    from .unit import unit_check
    run = {"V0": static_check, "V1": unit_check, "V2a": toolcheck, "V2b": replay_check, "V2c": reach_check}
    plan = [(r, r, decl) for r in (rungs or ladder_for(decl.kind))]
    if rungs is None:
        extras = supporting(files, decl, ctx)
        if extras and "V1" not in {p[0] for p in plan} and any(k.startswith(("V2a", "V2b")) for k, _ in extras):
            plan.insert(1, ("V1", "V1", decl))                 # code travels with a text candidate: its tests, probe, smoke
        plan += [(key, key.partition("@")[0], d) for key, d in extras]
    steps: dict = {}
    for key, rung, d in plan:
        result = run[rung](files, d, ctx)
        steps[key] = result
        if not result.get("passed"):
            break
    return Verdict(dir=decl.dir, steps=steps)
