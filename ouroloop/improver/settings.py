"""K's improver section (docs/design/IMPROVER.md §2; DESIGN §6.4), v1.2.

One package a round, a focus list instead of slots, held-out acceptance instead of a per-round gate. The priors follow
what was measured, not one number for every layer: the text layers by our three finals (a low mean, a small
uncertainty), the code layers by the outside readings on the same benchmark (a higher uncertainty), so the focus list
ranks code first. Everything here enters K; a config may only tighten it (activity/presets.py).
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
    packages_per_round: int = 3                  # the candidate packages R hands in a round, each one mechanism; each is
                                                 # screened, the accepted ones are stacked (IMPROVER §9.4, v1.3)
    audit_hook_share: float = 0.3                # the share of cases in text-only causes at which the focus list
                                                 # suggests an aux-model audit hook (IMPROVER §6, v1.3)
    stream_screen_max: int = 30                  # a streaming round screens at most this many recent held-out tasks
    fixed_per_round: int = 2                     # the fixed code's candidates per round (snapshot, promotions, removals)
    session_max_turns: int = 40
    session_max_edits: int = 48
    session_max_read_bytes: int = 65536          # one read (read_trajectory or read_file)
    session_max_read_total: int = 196608         # all reads of a session: the transcript is re-sent every turn
    session_compact_tokens: int = 120000         # past this many input tokens the oldest outputs are elided
    diagnose_max_cases: int = 60
    diagnose_successes: int = 6
    causes: tuple = CAUSES
    mechanizable: tuple = MECHANIZABLE
    priors: dict = field(default_factory=lambda: {k: list(v) for k, v in PRIORS.items()})
    require_code_component: bool = True          # a mechanizable cause and no tool or hook in the package: on the ledger
    l5_per_round: int = 1                        # agent, config or vendor changes a round, at most
    stall_rounds: int = 3
    hook_aux_calls: int = 2                      # aux model calls an on_finish or after_turn hook may make per attempt
    toolcheck_min_pass: int = 2
    replay_min_triggers: int = 2
    reach_min_tasks: int = 2
    reach_min_score: float = 0.2
    holdout_share: float = 0.4                   # the train tasks R never sees; V3 screens there only
    accept_min_net_flips: int = 1                # held-out acceptance: conditioned fixes minus regressions, at least
    accept_max_regressions: int = 1              # held-out passing tasks that fail now, at most
    accept_floor_tolerance: int = 1              # held-out score at least the activity's best so far minus this
    removal_zero_credit_rounds: int = 3
    round_share: float = 0.15                    # a round's account, as a share of the root's dollars

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
        if self.packages_per_round < 1 or self.session_max_turns < 1:
            raise ValueError("improver.packages_per_round and improver.session_max_turns are at least 1")
        if not 0 < float(self.holdout_share) < 1:
            raise ValueError("improver.holdout_share is a share of the train tasks, in (0, 1): the screen needs a "
                             "held-out part")
        if int(self.hook_aux_calls) < 0 or int(self.l5_per_round) < 0:
            raise ValueError("improver.hook_aux_calls and improver.l5_per_round are counts (0 forbids)")
        if int(self.accept_min_net_flips) < 1:
            raise ValueError("improver.accept_min_net_flips is at least 1: an acceptance without a fix is a tie")
        if int(self.accept_max_regressions) < 0 or int(self.accept_floor_tolerance) < 0:
            raise ValueError("improver.accept_max_regressions and improver.accept_floor_tolerance are counts")
        if not 0 <= float(self.audit_hook_share) <= 1:
            raise ValueError("improver.audit_hook_share is a share of the cases, in [0, 1]")
        if int(self.stream_screen_max) < 1:
            raise ValueError("improver.stream_screen_max is at least 1")
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


RULES = {"packages_per_round": "cap", "fixed_per_round": "cap", "session_max_turns": "cap", "session_max_edits": "cap",
         "session_max_read_bytes": "cap", "session_max_read_total": "cap", "session_compact_tokens": "cap",
         "diagnose_max_cases": "cap", "diagnose_successes": "cap", "causes": "superset", "mechanizable": "superset",
         "priors": "fixed", "require_code_component": "fixed", "l5_per_round": "cap", "stall_rounds": "cap",
         "hook_aux_calls": "cap", "toolcheck_min_pass": "floor", "replay_min_triggers": "floor",
         "reach_min_tasks": "floor", "reach_min_score": "floor", "holdout_share": "floor",
         "accept_min_net_flips": "floor", "accept_max_regressions": "cap", "accept_floor_tolerance": "cap",
         "removal_zero_credit_rounds": "fixed", "round_share": "cap", "audit_hook_share": "floor",
         "stream_screen_max": "cap"}
