"""The verification ladder (docs/design/IMPROVER.md §8; DESIGN §6.6, v1.2): each rung costs nothing paid. V0 static over
the whole package (shape, limits, literals, the declaration against the diff both ways), V1 unit when the package holds
code (its tests, probe and smoke in the worker), then one rung per component by its kind — V2a toolcheck for a tool,
V2b replay for a hook that does not call the model, V2c reach for a skill; a prompt, a config knob, the agent, a
vendored file and the memory have no rung of their own. V3 (the paid held-out screen) is screen.py's."""
from __future__ import annotations

from dataclasses import dataclass, field

from ...contracts import Record, register
from ..declaration import Component, Declaration

RUNGS = {"tool": "V2a", "hook": "V2b", "skill": "V2c"}
UNIT_KINDS = ("tool", "hook", "agent", "vendor", "config")     # kinds whose change runs V1 (code in the worker)


def ladder_for(component: Component) -> tuple:
    """The component's own rungs after V0 (and V1 when the package holds code)."""
    if component.kind == "hook" and component.uses_model:
        return ()                       # its first check is the paid screen, with its calls metered
    rung = RUNGS.get(component.kind)
    return (rung,) if rung else ()


@register
@dataclass(frozen=True)
class Verdict(Record):
    TYPE = "verdict"
    candidate_ref: str | None = None
    dir: str = ""
    steps: dict = field(default_factory=dict)         # V0, V1 -> {passed, detail}
    components: dict = field(default_factory=dict)    # component key -> {rung: {passed, detail}}

    def passed(self, step: str | None = None) -> bool:
        if step is not None:
            if step in self.steps:
                return bool(self.steps[step].get("passed"))
            return any(bool((rungs.get(step) or {}).get("passed")) for rungs in self.components.values()) \
                and all(bool(rungs[step].get("passed")) for rungs in self.components.values() if step in rungs)
        return bool(self.steps) and all(bool(v.get("passed")) for v in self.steps.values()) \
            and all(bool(v.get("passed")) for rungs in self.components.values() for v in rungs.values())

    def rungs(self) -> dict:
        """rung -> passed, over the steps and every component (a rung passed when every component's did)."""
        out = {k: bool(v.get("passed")) for k, v in self.steps.items()}
        for rungs in self.components.values():
            for rung, v in rungs.items():
                out[rung] = out.get(rung, True) and bool(v.get("passed"))
        return out

    def failed_components(self) -> list[str]:
        return [key for key, rungs in self.components.items() if any(not v.get("passed") for v in rungs.values())]

    def text(self) -> str:
        """The verdict as R reads it (run_check, a revision)."""
        lines = []
        for rung, v in self.steps.items():
            lines.append(f"{rung}: {'passed' if v.get('passed') else 'FAILED'}"
                         + (f" — {v.get('detail')}" if v.get("detail") else ""))
        for key, rungs in self.components.items():
            for rung, v in rungs.items():
                lines.append(f"{rung} ({key}): {'passed' if v.get('passed') else 'FAILED'}"
                             + (f" — {v.get('detail')}" if v.get("detail") else ""))
        return "\n".join(lines) or "nothing checked"


@dataclass
class VerifyContext:
    """What the ladder needs from the activity: the parent and seed packages, K, the object store, the worker, the
    suite (train episodes), the evidence (train transcripts), the forbidden literals, the train tasks' texts, an
    embedder for V2c, the improver's settings, the round's causes."""
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
    cause_ids: tuple = ()               # the round's causes: a component names some of them


def run_ladder(files: dict[str, str], decl: Declaration, ctx: VerifyContext, *, rungs: tuple | None = None) -> Verdict:
    """V0 over the package, V1 when it holds code, then every component's own rung (all of them run, so one revision
    sees every problem); a failed V0 or V1 ends the climb. `rungs` restricts the climb to the named rungs."""
    from .reach import reach_check
    from .replay import replay_check
    from .static import static_check
    from .toolcheck import toolcheck
    from .unit import unit_check
    run = {"V2a": toolcheck, "V2b": replay_check, "V2c": reach_check}
    want = None if rungs is None else set(rungs)
    steps: dict = {}
    if want is None or "V0" in want:
        steps["V0"] = static_check(files, decl, ctx)
        if not steps["V0"].get("passed"):
            return Verdict(dir=decl.dir, steps=steps)
    if (want is None or "V1" in want) and any(c.kind in UNIT_KINDS for c in decl.components):
        steps["V1"] = unit_check(files, decl, ctx)
        if not steps["V1"].get("passed"):
            return Verdict(dir=decl.dir, steps=steps)
    comps: dict = {}
    for c in decl.components:
        for rung in ladder_for(c):
            if want is not None and rung not in want:
                continue
            comps.setdefault(c.key, {})[rung] = run[rung](files, c, ctx)
    return Verdict(dir=decl.dir, steps=steps, components=comps)
