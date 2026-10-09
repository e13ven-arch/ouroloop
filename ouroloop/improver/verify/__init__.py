"""The verification ladder (docs/design/IMPROVER.md §8; DESIGN §6.6): each rung costs nothing paid and runs only when
the rung below passed. V0 static, V1 unit (the package's tests, probe and smoke in the worker), V2a toolcheck, V2b hook
replay, V2c reach. V3 (the paid train screen) and V4 (the dev gate) are screen.py's."""
from __future__ import annotations

from dataclasses import dataclass, field

from ...contracts import Record, register
from ..declaration import Declaration

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


def run_ladder(files: dict[str, str], decl: Declaration, ctx: VerifyContext, *, rungs: tuple | None = None) -> Verdict:
    """Every rung of the candidate's kind in order; a failed rung ends the climb (the rest are not run)."""
    from .reach import reach_check
    from .replay import replay_check
    from .static import static_check
    from .toolcheck import toolcheck
    from .unit import unit_check
    run = {"V0": static_check, "V1": unit_check, "V2a": toolcheck, "V2b": replay_check, "V2c": reach_check}
    steps: dict = {}
    for rung in rungs or ladder_for(decl.kind):
        result = run[rung](files, decl, ctx)
        steps[rung] = result
        if not result.get("passed"):
            break
    return Verdict(dir=decl.dir, steps=steps)
