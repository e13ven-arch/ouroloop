"""A candidate package's declaration (docs/design/IMPROVER.md §7; DESIGN §6.5, v1.2): what R says the package is — one
mechanism, the components it changes, each with the causes it works on and the proof the ladder needs — or what the
fixed code says of its own candidates. V0 checks the files against it in both directions (static.py)."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import Record, register

KINDS = ("tool", "hook", "skill", "prompt", "agent", "config", "vendor", "memory", "removal")
NAMED = ("tool", "hook", "skill", "prompt", "config", "vendor", "removal")    # kinds whose component has a site
PROVING = ("tool", "hook")                       # kinds whose component claims train tasks (V2a, V2b need them)
RESERVED = ("memory-snapshot", "promoted-", "remove-")     # the fixed code's candidate directories
DEFAULT_DIR = "package"
MECHANISM_CHARS = 600
TRIGGER_CHARS = 500


@register
@dataclass(frozen=True)
class Component(Record):
    """One changed component. `site`: a tool's or skill's name, a hook's site, a prompt's key (system | finish), a
    config knob, a vendored package, a removal's component name; the agent and the memory have none."""
    TYPE = "component"
    kind: str = ""
    site: str | None = None
    cause_ids: tuple = ()               # the round's causes it works on
    predicted_affected: tuple = ()      # train task ids it should fix (tool, hook: required)
    trigger: str = ""                   # when it acts
    scripts: dict = field(default_factory=dict)     # task_id -> toolcheck script (a tool)
    probes: dict = field(default_factory=dict)      # task_id -> [{args, expect}] (a tool, IMPROVER §8.1)
    uses_model: bool = False            # a hook that calls the aux model (no V2b; metered at V3)
    rewrite: bool = False               # v1.8: a prompt or skill that deletes or rewrites existing text (V0 refuses it
                                        # otherwise); the package holds this component alone and is screened as a
                                        # whole-footprint one (accept_min_whole_fixes)

    def __post_init__(self):
        object.__setattr__(self, "cause_ids", tuple(self.cause_ids or ()))
        object.__setattr__(self, "predicted_affected", tuple(self.predicted_affected or ()))
        object.__setattr__(self, "scripts", dict(self.scripts or {}))
        object.__setattr__(self, "probes", {k: [dict(x) for x in v] for k, v in dict(self.probes or {}).items()})

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.site}" if self.site else self.kind


SCOPES = ("", "invariant", "domain")      # v1.10: a package's scope (DESIGN §6.20)


@register
@dataclass(frozen=True)
class Declaration(Record):
    TYPE = "declaration"
    dir: str = DEFAULT_DIR
    mechanism: str = ""                 # one sentence: what the package does differently
    components: tuple = ()              # (Component)
    differs_from: tuple = ()            # ({ref, how}) of earlier candidates it is not a repeat of
    fixed: str = ""                     # "" for R's; the fixed code's source otherwise (snapshot, promotion, removal)
    merge_of: str = ""                  # v1.8: the frontier entry this package is the conditioned version of
    scope: str = ""                     # v1.10 (DESIGN §6.20): invariant (how the agent works) | domain (what a
                                        # practitioner knows) | "" (unscoped)

    def __post_init__(self):
        comps = tuple(c if isinstance(c, Component) else Component.from_json(dict(c)) for c in self.components or ())
        object.__setattr__(self, "components", comps)
        object.__setattr__(self, "differs_from", tuple(dict(d) for d in self.differs_from or ()))

    # ------------------------------------------------------------------ what the package is, read off its parts
    @property
    def kinds(self) -> tuple:
        return tuple(dict.fromkeys(c.kind for c in self.components))

    @property
    def kind(self) -> str:
        """The components' kinds joined (`tool+hook`): the ledger's and the archive's summary."""
        return "+".join(self.kinds) or "none"

    @property
    def sites(self) -> tuple:
        return tuple(dict.fromkeys(c.site for c in self.components if c.site))

    @property
    def site(self) -> str | None:
        return ",".join(self.sites) or None

    @property
    def cause_ids(self) -> tuple:
        return tuple(dict.fromkeys(i for c in self.components for i in c.cause_ids))

    @property
    def causes(self) -> tuple:
        return tuple(dict.fromkeys(i.rpartition(":")[2] for i in self.cause_ids))

    @property
    def predicted_affected(self) -> tuple:
        return tuple(dict.fromkeys(t for c in self.components for t in c.predicted_affected))

    @property
    def trigger(self) -> str:
        return self.mechanism

    def to_json(self) -> dict:
        d = super().to_json()
        d["components"] = [c.to_json() for c in self.components]
        return d


def _component(raw, *, kinds_allowed, index: int) -> tuple[Component | None, str]:
    where = f"components[{index}]"
    if not isinstance(raw, dict):
        return None, f"{where} is an object"
    kind = raw.get("kind")
    if kind not in KINDS:
        return None, f"{where}.kind {kind!r}: one of {list(KINDS)}"
    if kind not in set(kinds_allowed):
        return None, f"{where}.kind {kind!r} is not a kind K allows ({sorted(kinds_allowed)})"
    site = raw.get("site")
    if kind in NAMED and not (isinstance(site, str) and site):
        return None, f"{where}.site names the {kind} (a tool's or skill's name, a hook's site, a prompt's key, a " \
                     f"config knob, a vendored package, a removed component)"
    causes = raw.get("cause_ids")
    if not isinstance(causes, list) or not causes or not all(isinstance(c, str) and c for c in causes):
        return None, f"{where}.cause_ids is a non-empty list of the round's cause ids"
    predicted = raw.get("predicted_affected") or []
    if not isinstance(predicted, list) or not all(isinstance(t, str) for t in predicted):
        return None, f"{where}.predicted_affected is a list of train task ids"
    if kind in PROVING and not predicted:
        return None, f"{where}.predicted_affected names the train tasks a {kind} should fix"
    if not isinstance(raw.get("trigger"), str) or not raw.get("trigger"):
        return None, f"{where}.trigger says when it acts"
    scripts = raw.get("scripts") or {}
    if not isinstance(scripts, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in scripts.items()):
        return None, f"{where}.scripts maps a train task id to a toolcheck script"
    probes = raw.get("probes") or {}
    if not isinstance(probes, dict) or not all(
            isinstance(k, str) and isinstance(v, list) and v and all(
                isinstance(x, dict) and isinstance(x.get("args"), dict)
                and (x.get("expect") is None or isinstance(x.get("expect"), str)) for x in v)
            for k, v in probes.items()):
        return None, f"{where}.probes maps a train task id to a list of {{args: object, expect: string or null}}"
    uses_model = bool(raw.get("uses_model"))
    if uses_model and kind != "hook":
        return None, f"{where}.uses_model is a hook's (an on_finish or after_turn hook that calls the aux model)"
    rewrite = bool(raw.get("rewrite"))
    if rewrite and kind not in ("prompt", "skill"):
        return None, f"{where}.rewrite is a prompt's or a skill's (one that deletes or rewrites existing text)"
    return Component(kind=kind, site=None if site in (None, "") else str(site), cause_ids=tuple(causes),
                     predicted_affected=tuple(predicted), trigger=str(raw["trigger"])[:TRIGGER_CHARS],
                     scripts=dict(scripts),
                     probes={k: [{"args": dict(x["args"]), "expect": x.get("expect")} for x in v]
                             for k, v in probes.items()}, uses_model=uses_model, rewrite=rewrite), ""


def read_declaration(raw, *, dirs, kinds_allowed, merge_ids=()) -> tuple[Declaration | None, str]:
    """A declaration from `finish`'s arguments, or why it is not one (handed back to R as the tool's error). `dirs`
    are the candidate directories the session wrote; a declaration without `dir` is candidates/package's. `merge_ids`
    are the frontier entries this round merges: a declaration's `merge_of` names one of them (v1.8)."""
    if not isinstance(raw, dict):
        return None, "a declaration is an object"
    name = raw.get("dir") or DEFAULT_DIR
    if not isinstance(name, str):
        return None, "dir is a candidate directory's name"
    if name.startswith(RESERVED):
        return None, f"dir {name!r}: names starting with {list(RESERVED)} are the fixed code's candidates'"
    if name not in set(dirs):
        return None, f"dir {name!r} is not a candidate directory you wrote ({sorted(dirs) or 'none yet'})"
    mechanism = raw.get("mechanism")
    if not isinstance(mechanism, str) or not mechanism.strip():
        return None, "mechanism is one sentence: what the package does differently"
    comps_raw = raw.get("components")
    if not isinstance(comps_raw, list) or not comps_raw:
        return None, "components is a non-empty list: one entry per component the package changes"
    comps, seen = [], set()
    for i, item in enumerate(comps_raw):
        comp, why = _component(item, kinds_allowed=kinds_allowed, index=i)
        if why:
            return None, why
        if comp.key in seen:
            return None, f"components[{i}]: {comp.key} is declared twice"
        seen.add(comp.key)
        comps.append(comp)
    differs = raw.get("differs_from") or []
    if not isinstance(differs, list) or not all(isinstance(d, dict) and isinstance(d.get("ref"), str)
                                                for d in differs):
        return None, "differs_from is a list of {ref, how}"
    merge_of = raw.get("merge_of") or ""
    if not isinstance(merge_of, str):
        return None, "merge_of is the id of the frontier entry this package is the conditioned version of"
    if merge_of and merge_of not in set(merge_ids):
        return None, f"merge_of {merge_of!r} is not a frontier entry this round merges " \
                     f"({sorted(merge_ids) or 'none: focus.json lists none'})"
    scope = raw.get("scope") or ""
    if not isinstance(scope, str) or scope not in SCOPES:
        return None, f"scope is one of {list(SCOPES[1:])} (invariant: how the agent works; domain: what a practitioner " \
                     f"knows), or left out"
    return Declaration(dir=name, mechanism=mechanism.strip()[:MECHANISM_CHARS], components=tuple(comps),
                       differs_from=tuple({"ref": d["ref"], "how": str(d.get("how") or "")[:TRIGGER_CHARS]}
                                          for d in differs), merge_of=merge_of, scope=scope), ""


def fixed_declaration(dir_name: str, *, source: str, kind: str, site: str | None, cause_id: str, trigger: str,
                      predicted_affected=()) -> Declaration:
    """The fixed code's one-component declaration (a snapshot, a promoted skill, a removal)."""
    return Declaration(dir=dir_name, mechanism=trigger, fixed=source,
                       components=(Component(kind=kind, site=site, cause_ids=(cause_id,),
                                             predicted_affected=tuple(predicted_affected), trigger=trigger),))


def component_of(decl_or_component, kind: str) -> Component:
    """The component a rung checks: the one given, or a declaration's first component of `kind` (a one-component
    declaration handed to a rung directly, as the tests and run_check do)."""
    if isinstance(decl_or_component, Component):
        return decl_or_component
    comps = [c for c in decl_or_component.components if c.kind == kind]
    if not comps:
        raise ValueError(f"the declaration has no {kind} component")
    return comps[0]
