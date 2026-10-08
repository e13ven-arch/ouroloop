"""A config, its preset, and K (docs/design/ACTIVITY.md §3; CONTRACTS §4, §9).

A config file names a preset ([harness] preset) and may only tighten it (presets.RULES). The merged sections, with the
domain's frozen split hash and the framework's code version, are K: frozen as `core.json` in the workspace and as a
`core` object whose ref covers every byte. Any change of a `.py` file is a new code_version and so a new K (PLAN rule
13); [rsi] core_ref, once `ouroloop freeze` printed it, pins K, and a config that freezes to another K is refused.

Old table names are not read: [llm] is [model], [research] is [research_model], [improvement] and
[harness_evolution] are [harness] and [improver], [freeze.*] are [gate], [schedule], [budget] and [final]. The configs
of the old workspaces run at their tags (PLAN rule 15).
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..contracts import ContractError, Record, load_dotenv, register
from ..harness.contract import EFFORTS
from .presets import RULES, preset

SECTIONS = ("bench", "limits", "model", "research_model", "pricing", "harness", "worker", "memory", "improver", "gate",
            "schedule", "budget", "final")
OTHER_TABLES = ("workspace", "rsi", "env_file")
RSI_KEYS = ("scope_id", "core_ref", "split_ref", "final_plan_ref")
OLD_TABLES = {"llm": "[model]", "research": "[research_model]", "improvement": "[harness] preset and [improver]",
              "harness_evolution": "[harness]", "freeze": "[gate], [schedule], [budget] and [final]",
              "estimate": "nothing: the final's estimate reads the costs on record",
              "trainer": "the training stack's config (python -m ouroloop.train)",
              "trainers": "the training stack's config (python -m ouroloop.train)"}
PLACEHOLDER = ("<", "TODO", "changeme")


class ConfigError(ContractError):
    """A config that names an unknown or old table or key, or loosens its preset. Raised before anything runs."""


# ---------------------------------------------------------------------------- reading
def load_config(path: str | Path) -> dict:
    """The TOML config, its optional env_file loaded like .env (relative to the config), and `_base`, the config's
    directory, which relative paths in it are read against."""
    path = Path(path)
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    if cfg.get("env_file"):
        load_dotenv(path.resolve().parent / cfg["env_file"])
    cfg["_base"] = str(path.resolve().parent)
    return cfg


def check_tables(cfg: dict) -> None:
    old = sorted(set(cfg) & set(OLD_TABLES))
    if old:
        raise ConfigError("; ".join(f"[{t}] is not read any more: it is {OLD_TABLES[t]} (docs/design/CONTRACTS.md §9)"
                                    for t in old))
    unknown = sorted(set(cfg) - set(SECTIONS) - set(OTHER_TABLES) - {"_base"})
    if unknown:
        raise ConfigError(f"unknown tables {unknown}: a config has {list(SECTIONS + OTHER_TABLES)}")
    rsi = dict(cfg.get("rsi") or {})
    extra = sorted(set(rsi) - set(RSI_KEYS))
    if extra:
        raise ConfigError(f"[rsi] does not take {extra}; it takes {list(RSI_KEYS)}")
    for key, value in rsi.items():
        if isinstance(value, str) and value.startswith(PLACEHOLDER):
            raise ConfigError(f"[rsi] {key} = {value!r} is a placeholder: run `ouroloop freeze` and name its ref")


# ---------------------------------------------------------------------------- merging (only tighten)
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)([kmg]?)b?$", re.I)


def size_bytes(value) -> int:
    m = _SIZE.match(str(value).strip())
    if not m:
        raise ConfigError(f"size {value!r}: a number of bytes with k, m or g")
    return int(float(m.group(1)) * {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30}[m.group(2).lower()])


def _number(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) and not (isinstance(value, str)
                                                                               and re.fullmatch(r"\d+(\.\d+)?", value)):
        raise ConfigError(f"{where} = {value!r}: a number")
    return float(value)


def _allow(network) -> list:
    return list(network.get("allow") or []) if isinstance(network, dict) else []


def _merge_value(rule, given, default, where: str):
    if isinstance(rule, dict):
        given = dict(given or {})
        unknown = sorted(set(given) - set(rule))
        if unknown:
            raise ConfigError(f"{where} does not take {unknown}; it takes {sorted(rule)}")
        return {k: _merge_value(r, given[k], (default or {}).get(k), f"{where}.{k}") if k in given
                else (default or {}).get(k) for k, r in rule.items()}
    if rule == "free" or default is None and rule in ("cap", "floor", "bytes", "effort"):
        return given
    if rule == "fixed":
        if given != default:
            raise ConfigError(f"{where} = {given!r}: the preset fixes {default!r}")
        return given
    if rule == "cap":
        if _number(given, where) > _number(default, where) + 1e-12:
            raise ConfigError(f"{where} = {given!r} is above the preset's {default!r}: a config may only lower it")
        return given
    if rule == "floor":
        if _number(given, where) < _number(default, where) - 1e-12:
            raise ConfigError(f"{where} = {given!r} is below the preset's {default!r}: a config may only raise it")
        return given
    if rule == "bytes":
        if size_bytes(given) > size_bytes(default):
            raise ConfigError(f"{where} = {given!r} is above the preset's {default!r}: a config may only lower it")
        return given
    if rule == "effort":
        if given not in EFFORTS or EFFORTS.index(given) > EFFORTS.index(default):
            raise ConfigError(f"{where} = {given!r}: an effort at most the preset's {default!r} ({list(EFFORTS)})")
        return given
    if rule == "subset":
        if not isinstance(given, list) or sorted(set(given) - set(default or ())):
            raise ConfigError(f"{where} = {given!r}: a subset of the preset's {default!r}")
        return [x for x in default if x in given]
    if rule == "ranges":
        given, out = dict(given or {}), dict(default or {})
        for key, pair in given.items():
            if key not in out:
                raise ConfigError(f"{where}.{key}: the preset has no such knob ({sorted(out)})")
            lo, hi = pair
            if lo < out[key][0] or hi > out[key][1] or lo > hi:
                raise ConfigError(f"{where}.{key} = {pair!r}: a range inside the preset's {out[key]!r}")
            out[key] = [lo, hi]
        return out
    if rule == "network":
        if given == "none":
            return "none"
        if default == "none" or not isinstance(given, dict) or given.get("mode") != "proxy":
            raise ConfigError(f"{where} = {given!r}: the preset's network is {default!r}, and a config may only "
                              f"narrow it")
        wanted = {json.dumps(a, sort_keys=True) for a in _allow(given)}
        allowed = {json.dumps(a, sort_keys=True) for a in _allow(default)}
        if wanted - allowed:
            raise ConfigError(f"{where}: the allowlist names hosts the preset does not allow")
        return given
    raise ConfigError(f"{where}: unknown rule {rule!r}")


def merge(config: dict, preset_table: dict) -> dict:
    """The config's sections over the preset's, each value moved only in its rule's direction. -> K's sections."""
    out = {}
    for section in SECTIONS:
        given = dict(config.get(section) or {})
        if section == "harness":
            given.pop("preset", None)
        out[section] = _merge_value(RULES[section], given, preset_table.get(section), f"[{section}]")
    return out


# ---------------------------------------------------------------------------- K
@register
@dataclass(frozen=True)
class Core(Record):
    """K: the sections of CONTRACTS §4, the preset's name, the frozen split's hash and the code version."""
    TYPE = "core"
    preset: str = ""
    sections: dict = field(default_factory=dict)
    split_hash: str = ""
    code_version: str = ""

    def json(self) -> dict:
        """K as one document (what core.json holds and what every package reads)."""
        return {**self.sections, "preset": self.preset, "split_hash": self.split_hash,
                "code_version": self.code_version}


def preset_name(config: dict) -> str:
    name = (config.get("harness") or {}).get("preset")
    if not name:
        raise ConfigError("[harness] preset is required: it is what the config tightens")
    return name


def resolve(config: dict) -> dict:
    """The config's sections after the preset's defaults, the tighten-only rule and the derived fields."""
    check_tables(config)
    try:
        table = preset(preset_name(config))
    except KeyError as e:
        raise ConfigError(str(e)) from None
    sections = merge(config, table)
    bench = sections["bench"]
    if not bench.get("domain"):
        raise ConfigError("[bench] domain is required")
    model = sections["model"]
    if not model.get("model"):
        raise ConfigError("[model] model names the actor (the claude_code CLI's model)")
    model["provider"], model["identity"], model["max_effort"] = model["type"], model["model"], model["effort"]
    research = sections["research_model"]
    research["model"] = research.get("model") or model["model"]
    research["identity"] = research["model"]
    sections["harness"]["api_version"] = "harness_package.v3"
    if (sections["gate"].get("control_pooling") or "fresh") == "pooled" and sections["gate"]["control_readings"] < 1:
        raise ConfigError("[gate] control_readings: at least one W0 dev reading for a pooled control")
    budget = sections["budget"]
    if budget.get("final_usd") is not None and budget.get("usd") is not None \
            and float(budget["final_usd"]) >= float(budget["usd"]):
        raise ConfigError("[budget] final_usd is a share of usd and leaves the rest something to spend")
    return sections


def freeze_core(config: dict, *, split_hash: str, code_version: str) -> Core:
    """K, frozen. Same config, same split, same code: same ref. [bench] concurrency is a setting of the machine
    (Activity.concurrency reads it from the config), not of K: a resume may change it."""
    sections = resolve(config)
    sections["bench"].pop("concurrency", None)
    return Core(preset=preset_name(config), sections=sections, split_hash=split_hash, code_version=code_version)


def core_problems(core: Core) -> list[str]:
    """What makes a frozen K unusable: the actor's effort over its cap, or an unknown seed."""
    problems = []
    model = core.sections.get("model") or {}
    if model.get("effort") not in EFFORTS:
        problems.append(f"[model] effort {model.get('effort')!r}: one of {list(EFFORTS)}")
    from ..harness.seed import SEEDS
    seed = (core.sections.get("harness") or {}).get("seed")
    if not (SEEDS / str(seed)).is_dir():
        problems.append(f"[harness] seed {seed!r} is not a seed of this framework ({sorted(p.name for p in SEEDS.iterdir() if p.is_dir())})")
    return problems


def workspace_root(config: dict) -> Path:
    base = Path(config.get("_base") or ".")
    root = (config.get("workspace") or {}).get("root")
    if not root:
        raise ConfigError("[workspace] root is required")
    path = Path(root).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def upstream_path(config: dict) -> str | None:
    path = (config.get("bench") or {}).get("upstream_path")
    if not path:
        return None
    p = Path(path).expanduser()
    return str(p if p.is_absolute() else (Path(config.get("_base") or ".") / p).resolve())
