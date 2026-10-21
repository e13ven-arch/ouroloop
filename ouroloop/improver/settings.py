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
          "rule_not_applied", "vocabulary_mismatch", "omitted_report",     # v1.5 (DESIGN §6.15; IMPROVER §5)
          "other")
MECHANIZABLE = ("excluded_mentioned", "arith_mismatch", "real_date", "wrong_location", "tool_error_loop",
                "vocabulary_mismatch")
LOOP_CAUSES = ("context_overflow", "finish_protocol", "tool_error_loop")
CODE_KINDS = ("tool", "hook")
TEXT_KINDS = ("skill", "prompt")
L5_KINDS = ("agent", "config", "vendor")
PRIORS = {"tool": (0.3, 0.3), "hook": (0.3, 0.3), "skill": (0.08, 0.1), "prompt": (0.08, 0.1), "agent": (0.1, 0.3),
          "config": (0.1, 0.2), "vendor": (0.05, 0.3), "model": (0.0, 0.5)}
# v1.8 (DESIGN §6.18): what R reads of a package's confirmed regressions, each level holding the one before it
REGRESSION_LEVELS = ("counts", "abstract", "research")


@dataclass(frozen=True)
class Settings:
    packages_per_round: int = 3                  # the candidate packages R hands in a round, each one mechanism; each is
                                                 # screened, the accepted ones are stacked (IMPROVER §9.4, v1.3)
    audit_hook_share: float = 0.3                # the share of cases in text-only causes at which the focus list
                                                 # suggests an aux-model audit hook (IMPROVER §6, v1.3)
    stream_screen_max: int = 30                  # a streaming round screens at most this many recent held-out tasks
    failures_max_cases: int = 30                 # inputs/failures.json: at most this many cases side by side (§7, v1.5)
    failures_case_chars: int = 4000              # one case's JSON at most (the writes' excerpts shrink to fit)
    audit_additive_only: bool = True             # v1.6: a hook that called the model answers {"missing": [...]} only
    audit_max_false_alarm: float = 0.25          # v1.6: V2d — the share of passing attempts an audit may fire on
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
    replay_max_false_trigger: float = 0.2        # v1.7: V2b — the share of the passing attempts replayed a hook may act on
                                                 # (v1.8: from 0.25; the hook finance-v7 installed was near 1.0)
    reach_min_tasks: int = 2
    reach_min_score: float = 0.2
    holdout_share: float = 0.4                   # the train tasks R never sees; V3 screens there only
    accept_min_net_flips: int = 1                # held-out acceptance: confirmed fixes minus confirmed regressions, at least
    accept_max_regressions: int = 0              # held-out passing tasks that fail now, confirmed, at most (v1.7: from 1)
    accept_min_whole_fixes: int = 2              # v1.7: confirmed fixes a package whose footprint is every attempt needs
    confirm_readings: int = 2                    # v1.7: readings each side of a flipped task has before it counts
    accept_floor_tolerance: int = 1              # held-out score at least the activity's best so far minus this
    removal_zero_credit_rounds: int = 3
    round_share: float = 0.15                    # a round's account, as a share of the root's dollars
    # v1.8 (DESIGN §6.18; IMPROVER §9.5): accumulation without regression
    regression_evidence: str = "research"        # what R reads of a package's confirmed regressions: counts | abstract
                                                 # (de-identified records of each one) | research (the abstract records,
                                                 # and a sample of studied passing tasks run under the candidate, the
                                                 # regressed ones readable as two trajectories)
    regression_research_sample: int = 6          # the research level's sample of studied passing tasks, per package
    frontier_max: int = 4                        # partial packages kept on the frontier (rounds/frontier.json), at most
    merge_per_round: int = 1                     # frontier entries R is asked to merge (a conditioned version) a round
    frontier_min_fixes: int = 1                  # confirmed fixes a rejected package needs to be a partial one
    frontier_max_attempts: int = 2               # merges a frontier entry gets before it is abandoned
    text_additive_only: bool = True              # prompt/*.md and existing skills/*.md only grow: a package that deletes
                                                 # or rewrites existing text holds that one component, declared rewrite
    heuristic_terms_min: int = 3                 # V0 notes a hook or tool whose trigger is a list of this many words
    # v1.9 (DESIGN §6.19; IMPROVER §9.6): the in-sample trial — R's package runs on the studied failing tasks it
    # predicts before the paid held-out screen, the results go back to R, and only a package that fixes one there is
    # screened; regressions by footprint; the merge's replay; the replay sample
    trial_max_tasks: int = 4                     # studied failing tasks a package is tried on, per iteration (0: off)
    trial_max_rounds: int = 2                    # revisions R gets after a trial that fixed too little
    trial_max_attempts: int = 40                 # trial attempts a round pays for, over its packages and iterations
    trial_min_fixes: int = 1                     # in-sample fixes a package needs to reach the screen (0: feedback
                                                 # only; the user's to set either way)
    regression_by_footprint: bool = True         # a package of tool/hook/skill components regresses only where one acted
    merge_min_acts: int = 1                      # a merge's hook must act on this many of the entry's fixed attempts
    try_per_task: int = 2                        # v1.11 (DESIGN §6.21): R's try_task on one studied task, at most (one
                                                 # rollout is weak evidence; the probe is bounded as Harness²'s is)
    scoped_packages: bool = True                 # v1.10 (DESIGN §6.20): with packages_per_round >= 2, the first package
                                                 # is invariant (how the agent works), the second domain (what a
                                                 # practitioner knows); the declaration's `scope` names which
    replay_sample: int = 12                      # passing train attempts V2b (and V2d) replays for the false-trigger rate

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
        if not 0 <= float(self.audit_max_false_alarm) <= 1:
            raise ValueError("improver.audit_max_false_alarm is a share, in [0, 1]")
        if not 0 <= float(self.replay_max_false_trigger) <= 1:
            raise ValueError("improver.replay_max_false_trigger is a share, in [0, 1]")
        if int(self.accept_min_whole_fixes) < 1 or int(self.confirm_readings) < 1:
            raise ValueError("improver.accept_min_whole_fixes and improver.confirm_readings are at least 1")
        if int(self.failures_max_cases) < 0 or int(self.failures_case_chars) < 500:
            raise ValueError("improver.failures_max_cases is a count (0: no failures.json); failures_case_chars at least "
                             "500")
        if not 0 < float(self.round_share) <= 1:
            raise ValueError("improver.round_share is a share of the root, in (0, 1]")
        if self.regression_evidence not in REGRESSION_LEVELS:
            raise ValueError(f"improver.regression_evidence is one of {list(REGRESSION_LEVELS)}")
        if min(int(self.regression_research_sample), int(self.frontier_max), int(self.merge_per_round)) < 0:
            raise ValueError("improver.regression_research_sample, frontier_max and merge_per_round are counts "
                             "(0 turns the research sample, the frontier or the merges off)")
        if min(int(self.frontier_min_fixes), int(self.frontier_max_attempts), int(self.heuristic_terms_min)) < 1:
            raise ValueError("improver.frontier_min_fixes, frontier_max_attempts and heuristic_terms_min are at least 1")
        if min(int(self.trial_max_tasks), int(self.trial_max_rounds), int(self.trial_max_attempts),
               int(self.trial_min_fixes)) < 0:
            raise ValueError("improver.trial_max_tasks, trial_max_rounds, trial_max_attempts and trial_min_fixes are "
                             "counts (trial_max_tasks 0 turns the trial off; trial_min_fixes 0 keeps it as feedback only)")
        if int(self.merge_min_acts) < 1 or int(self.replay_sample) < 1:
            raise ValueError("improver.merge_min_acts and replay_sample are at least 1")

    def evidence_at_least(self, level: str) -> bool:
        """Whether regression_evidence holds `level` (each level holds the ones before it)."""
        return REGRESSION_LEVELS.index(self.regression_evidence) >= REGRESSION_LEVELS.index(level)

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
         "stream_screen_max": "cap", "failures_max_cases": "cap", "failures_case_chars": "cap",
         "audit_additive_only": "fixed", "audit_max_false_alarm": "cap", "replay_max_false_trigger": "cap",
         "accept_min_whole_fixes": "floor", "confirm_readings": "floor",
         # v1.8: the level is the user's (any of the three; the sample's size is capped), the frontier's sizes capped,
         # its entry threshold a floor, the text rule fixed, the word-list threshold a floor
         "regression_evidence": "free", "regression_research_sample": "cap", "frontier_max": "cap",
         "merge_per_round": "cap", "frontier_min_fixes": "floor", "frontier_max_attempts": "cap",
         "text_additive_only": "fixed", "heuristic_terms_min": "floor",
         # v1.9: the trial's sizes capped, its entry threshold a floor; regressions by footprint fixed; the merge's
         # replay threshold and the replay sample floors (a larger sample reads the false-trigger rate better)
         "trial_max_tasks": "cap", "trial_max_rounds": "cap", "trial_max_attempts": "cap",
         "trial_min_fixes": "free", "regression_by_footprint": "fixed", "merge_min_acts": "floor",
         "replay_sample": "floor",
         # v1.10: the two scopes are fixed
         "scoped_packages": "fixed",
         # v1.11: R's tries per task capped
         "try_per_task": "cap"}
