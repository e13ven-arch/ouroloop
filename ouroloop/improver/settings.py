"""K's improver section (docs/design/IMPROVER.md §2; DESIGN §6.4).

The priors follow what was measured, not one number for every layer: the text layers by our three finals (a low mean,
a small uncertainty), the code layers by the outside readings on the same benchmark (a higher uncertainty), so the
exploration slots fall on code first. Everything here enters K; a config may only tighten it (activity/presets.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields

CAUSES = ("excluded_mentioned", "missed_action", "wrong_target", "missing_recipient", "unread_value", "arith_mismatch",
          "real_date", "wrong_location", "premature_stop", "tool_error_loop", "context_overflow", "finish_protocol",
          "other")
MECHANIZABLE = ("excluded_mentioned", "arith_mismatch", "real_date", "wrong_location", "tool_error_loop")
LOOP_CAUSES = ("context_overflow", "finish_protocol", "tool_error_loop")
CODE_KINDS = ("tool", "hook")
TEXT_KINDS = ("skill", "prompt")
L5_KINDS = ("agent", "config", "vendor")
PRIORS = {"tool": (0.3, 0.3), "hook": (0.3, 0.3), "skill": (0.08, 0.1), "prompt": (0.08, 0.1), "agent": (0.1, 0.3),
          "config": (0.1, 0.2), "vendor": (0.05, 0.3), "model": (0.0, 0.5)}


@dataclass(frozen=True)
class Settings:
    k: int = 3                                   # R's candidates per round
    fixed_per_round: int = 2                     # the fixed code's candidates per round (snapshot, promotions, removals)
    session_max_turns: int = 40
    session_max_edits: int = 32
    session_max_read_bytes: int = 65536          # one read (read_trajectory or read_file)
    session_max_read_total: int = 196608         # all reads of a session: the transcript is re-sent every turn
    session_compact_tokens: int = 120000         # past this many input tokens the oldest outputs are elided
    diagnose_max_cases: int = 60
    diagnose_successes: int = 6
    causes: tuple = CAUSES
    mechanizable: tuple = MECHANIZABLE
    priors: dict = field(default_factory=lambda: {k: list(v) for k, v in PRIORS.items()})
    explore_slots: int = 1
    l5_slots: int = 1
    code_guarantee: bool = True
    stall_rounds: int = 3
    toolcheck_min_pass: int = 2
    replay_min_triggers: int = 2
    reach_min_tasks: int = 2
    reach_min_score: float = 0.2
    screen_passing_sample: int = 6
    screen_min_net_flips: int = 0
    screen_min_fixes: int = 1
    screen_max_sample_regressions: int = 1
    thin_screen_tasks: int = 3
    removal_zero_credit_rounds: int = 3
    holdout_share: float = 0.4                   # the train tasks R never sees; V3 screens there only (0: none)
    round_share: float = 0.25

    def __post_init__(self):
        for name in ("causes", "mechanizable"):
            value = getattr(self, name)
            object.__setattr__(self, name, (value,) if isinstance(value, str) else tuple(value))
        if "other" not in self.causes:
            raise ValueError("improver.causes keeps 'other': a case outside the vocabulary goes there")
        if not set(self.mechanizable) <= set(self.causes):
            raise ValueError(f"improver.mechanizable {sorted(set(self.mechanizable) - set(self.causes))} are not causes")
        priors = {str(k): [float(v[0]), float(v[1])] for k, v in dict(self.priors).items()}
        object.__setattr__(self, "priors", priors)
        if self.k < 1 or self.session_max_turns < 1:
            raise ValueError("improver.k and improver.session_max_turns are at least 1")
        if not 0 <= float(self.holdout_share) < 1:
            raise ValueError("improver.holdout_share is a share of the train tasks, in [0, 1)")
        if not 0 < float(self.round_share) <= 1:
            raise ValueError("improver.round_share is a share of the root, in (0, 1]")

    def prior(self, kind: str) -> tuple[float, float]:
        mean, unc = self.priors.get(kind, (0.0, 0.5))
        return float(mean), float(unc)

    def to_json(self) -> dict:
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            out[f.name] = list(v) if isinstance(v, tuple) else ({k: list(x) for k, x in v.items()}
                                                                  if isinstance(v, dict) else v)
        return out

    @classmethod
    def from_json(cls, d: dict | None) -> "Settings":
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(d or {}) - known)
        if unknown:
            raise ValueError(f"improver.{unknown[0]}: not an improver setting ({sorted(known)})")
        default = cls()
        out = {}
        for k, v in (d or {}).items():
            base = getattr(default, k)
            if isinstance(base, tuple):
                out[k] = (v,) if isinstance(v, str) else tuple(v or ())
            elif isinstance(base, dict):
                out[k] = dict(v or {})
            elif isinstance(base, bool):
                out[k] = bool(v)
            elif isinstance(base, int):
                out[k] = int(v)
            elif isinstance(base, float):
                out[k] = float(v)
            else:
                out[k] = v
        return cls(**out)

    @classmethod
    def from_core(cls, k: dict) -> "Settings":
        return cls.from_json(k.get("improver") or {})


def preset_table() -> dict:
    """The preset's improver section (activity/presets.py): every field at its default, as K holds it."""
    return Settings().to_json()


RULES = {"k": "cap", "fixed_per_round": "cap", "session_max_turns": "cap", "session_max_edits": "cap",
         "session_max_read_bytes": "cap", "session_max_read_total": "cap", "session_compact_tokens": "cap",
         "diagnose_max_cases": "cap",
         "diagnose_successes": "cap", "causes": "superset", "mechanizable": "superset", "priors": "fixed",
         "explore_slots": "cap", "l5_slots": "cap", "code_guarantee": "fixed", "stall_rounds": "cap",
         "toolcheck_min_pass": "floor", "replay_min_triggers": "floor", "reach_min_tasks": "floor",
         "reach_min_score": "floor", "screen_passing_sample": "cap", "screen_min_net_flips": "floor",
         "screen_min_fixes": "floor", "screen_max_sample_regressions": "cap", "thin_screen_tasks": "floor",
         "removal_zero_credit_rounds": "fixed", "holdout_share": "floor", "round_share": "cap"}
