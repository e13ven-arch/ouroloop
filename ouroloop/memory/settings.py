"""K's memory section (docs/design/MEMORY.md §3).

The numbers are v6's evidence, not guesses: E0c and E0d calibrated the thresholds on bge-small (unrelated sentences
score 0.53-0.56, related ones 0.67, near-duplicates 0.907), S1's shown counts set shown_k. Everything here enters K and
so the activity's identity; a config may only tighten it (activity/presets.py RULES). How many research calls run at
once is the machine's (the activity's concurrency), not K's.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields

from . import complete_retriever, default_backend, default_retriever

ORIGINS = ("success", "failure", "comparative")


@dataclass(frozen=True)
class Settings:
    enabled: bool = True                  # the activity runs the memory stream; a package's own manifest says if it reads
    shown_k: int = 2                      # entries in the opening block
    recall_k: int = 3                     # entries one recall gives at most
    fetch_factor: int = 5                 # fetch shown_k * this, then leave-one-out and min_score on our side
    min_score: float = 0.62               # E0d
    sibling_sim: float = 0.95             # E0d: a train task this close to the source is a source too
    batch_dedup_threshold: float = 0.85   # E0c: ReMe's dedup within a batch
    store_dedup_threshold: float = 0.9    # E0c: when_to_use against the stored when_to_use, one space ...
    store_dedup_content_threshold: float = 0.85   # ... and the contents this close: else a different lesson
    validation_threshold: float = 0.6     # ReMe's MemoryValidation (its no-JSON default is valid at 0.5)
    success_threshold: float = 1.0        # a trajectory scoring this is a success for ReMe's extraction
    origins: tuple = ORIGINS
    max_active: int = 500
    probation_shown: int = 3              # no retirement before this many showings
    retire_harm_tasks: int = 2            # harm in this many distinct tasks ...
    retire_harm_min_shown: int = 5        # ... once shown this often
    retire_min_shown: int = 3
    retire_mean_credit: float = -0.2      # mean credit per showing at or below this retires
    min_other_tasks: int = 2              # train tasks beyond its sources the when_to_use must reach
    when_chars: int = 400
    content_chars: int = 1600
    traj_tool_chars: int = 4000           # tool outputs' p99 is under 9k chars
    traj_total_chars: int = 60000
    stream_batch: int = 4
    cite: bool = True                     # the opening block asks for a `library-used:` line in the finish reason
    retry: bool = True                    # a failed first encounter is tried once more, with its failed checks
    abort_d: float = 0.0
    heldout_domains: tuple = ("operations",)
    imports: tuple = ()                   # [{release_ref, snapshot_digest}]
    llm_timeout_s: float = 600.0
    promote_fixes: int = 2
    promote_sources: int = 2
    retriever: dict = field(default_factory=default_retriever)
    backend: dict = field(default_factory=default_backend)

    def __post_init__(self):
        object.__setattr__(self, "retriever", complete_retriever(self.retriever))
        object.__setattr__(self, "origins", tuple(self.origins))
        object.__setattr__(self, "heldout_domains", tuple(self.heldout_domains))
        bad = sorted(set(self.origins) - set(ORIGINS))
        if bad or not self.origins:
            raise ValueError(f"memory.origins {list(self.origins)}: a non-empty subset of {list(ORIGINS)}")
        imports = []
        for i in self.imports:
            if not isinstance(i, dict) or set(i) != {"release_ref", "snapshot_digest"} or not all(
                    isinstance(v, str) and v for v in i.values()):
                raise ValueError(f"memory.imports: each is {{release_ref, snapshot_digest}}, not {i!r}")
            imports.append(dict(i))
        object.__setattr__(self, "imports", tuple(imports))
        for name in ("shown_k", "recall_k", "fetch_factor", "max_active", "stream_batch"):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"memory.{name} is at least 1")

    def to_json(self) -> dict:
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            out[f.name] = ([dict(x) if isinstance(x, dict) else x for x in v] if isinstance(v, tuple)
                           else (dict(v) if isinstance(v, dict) else v))
        return out

    @classmethod
    def from_json(cls, d: dict | None) -> "Settings":
        known = {f.name for f in fields(cls)}
        default = cls()
        out = {}
        for k, v in (d or {}).items():
            if k not in known:
                raise ValueError(f"memory.{k}: not a memory setting ({sorted(known)})")
            base = getattr(default, k)
            if isinstance(base, tuple):
                out[k] = tuple(v or ())
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
        return cls.from_json(k.get("memory") or {})


def settings_of(settings) -> Settings:
    return settings if isinstance(settings, Settings) else Settings.from_json(settings)


def preset_table() -> dict:
    """The preset's memory section (activity/presets.py): every field at its default, as K holds it."""
    return Settings().to_json()
