"""A candidate's declaration (docs/design/IMPROVER.md §7; DESIGN §6.10): what R says a candidate is — or what the fixed
code says of its own candidates — and what the ladder checks the files against."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import Record, register

KINDS = ("tool", "hook", "skill", "prompt", "agent", "config", "vendor", "memory", "removal")
FIELDS = ("dir", "cause_id", "kind", "site", "predicted_affected", "trigger")


@register
@dataclass(frozen=True)
class Declaration(Record):
    TYPE = "declaration"
    dir: str = ""
    cause_id: str = ""
    kind: str = ""
    site: str | None = None              # tool: its name; hook: its site; skill: its name; prompt: system|finish
    predicted_affected: tuple = ()      # train task ids it should fix
    trigger: str = ""                   # when it acts
    differs_from: tuple = ()            # ({ref, how}) of earlier candidates it is not a repeat of
    scripts: dict = field(default_factory=dict)     # task_id -> toolcheck script (tool candidates)
    fixed: str = ""                     # "" for R's; the fixed code's source otherwise (snapshot, promotion, removal)

    def __post_init__(self):
        object.__setattr__(self, "predicted_affected", tuple(self.predicted_affected or ()))
        object.__setattr__(self, "differs_from", tuple(dict(d) for d in self.differs_from or ()))
        object.__setattr__(self, "scripts", dict(self.scripts or {}))


def read_declaration(raw, *, dirs, kinds_allowed) -> tuple[Declaration | None, str]:
    """A declaration from `finish`'s arguments, or why it is not one (handed back to R as the tool's error)."""
    if not isinstance(raw, dict):
        return None, "a declaration is an object"
    missing = [f for f in FIELDS if raw.get(f) in (None, "") and f != "site"]
    if missing:
        return None, f"it lacks {missing}"
    if raw["dir"] not in set(dirs):
        return None, f"dir {raw['dir']!r} is not a candidate directory you wrote ({sorted(dirs) or 'none yet'})"
    if raw["kind"] not in set(kinds_allowed):
        return None, f"kind {raw['kind']!r} is not one this round assigned ({sorted(kinds_allowed)})"
    predicted = raw.get("predicted_affected")
    if not isinstance(predicted, list) or not all(isinstance(t, str) for t in predicted):
        return None, "predicted_affected is a list of train task ids"
    scripts = raw.get("scripts") or {}
    if not isinstance(scripts, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in scripts.items()):
        return None, "scripts maps a train task id to a toolcheck script"
    differs = raw.get("differs_from") or []
    if not isinstance(differs, list) or not all(isinstance(d, dict) and isinstance(d.get("ref"), str)
                                                for d in differs):
        return None, "differs_from is a list of {ref, how}"
    return Declaration(dir=str(raw["dir"]), cause_id=str(raw["cause_id"]), kind=str(raw["kind"]),
                       site=None if raw.get("site") in (None, "") else str(raw["site"]),
                       predicted_affected=tuple(predicted), trigger=str(raw["trigger"])[:500],
                       differs_from=tuple({"ref": d["ref"], "how": str(d.get("how") or "")[:500]} for d in differs),
                       scripts={k: v for k, v in scripts.items()}), ""
