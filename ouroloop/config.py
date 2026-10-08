"""Load a config, and build its LLM providers. The decision backends, the model store, serving and the trainer are
built by the training stack (ouroloop/train/config.py); the v1 decision runtime and research agent below go with the
v1 loop (P1 step 5)."""
from __future__ import annotations

import importlib
import os
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import Runtime

# The only keys of a [points.X] table: risk comes from the gate template (M8), defaults from the point's definition.
POINT_KEYS = {"backend", "define", "act", "explore"}
# Once the gate is frozen, its parameters come from the gate template only (docs/DESIGN.md invariant 4), never from the
# config.
FROZEN = (("research", "min_items"), ("suite", "min_items"), ("checks", None))


def import_object(ref: str):
    """"package.module:name" -> the object."""
    module, _, attr = ref.partition(":")
    return getattr(importlib.import_module(module), attr)


def load_dotenv(path: str | Path = ".env") -> None:
    """Read KEY=VALUE lines into the environment without overriding variables that are already set."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def build_provider(spec: dict):
    spec = dict(spec)
    kind = spec.pop("type")
    if kind == "anthropic":
        from .providers.anthropic import AnthropicProvider
        return AnthropicProvider(**spec)
    if kind == "openai_compat":
        from .providers.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(**spec)
    if kind == "claude_code":
        from .providers.claude_code import ClaudeCodeProvider
        return ClaudeCodeProvider(**spec)
    return import_object(kind)(**spec)


def load(path: str | Path = "ouroloop.toml") -> dict:
    """Read a config; its optional top-level env_file (relative to the config) is loaded like .env. The options of
    the [trainer] type's plugin, [trainers.<type>], are also its `options`, so the table builds the trainer alone."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    if cfg.get("env_file"):
        load_dotenv(Path(path).resolve().parent / cfg["env_file"])
    trainer = cfg.get("trainer")
    if isinstance(trainer, dict) and "options" not in trainer and trainer.get("type") in cfg.get("trainers", {}):
        cfg["trainer"] = {**trainer, "options": cfg["trainers"][trainer["type"]]}
    return cfg


def workspace_root(cfg: dict, base: str | Path = ".") -> Path:
    return Path(base) / cfg.get("workspace", {}).get("root", ".")


def check_frozen(cfg: dict) -> None:
    """A config that sets a gate parameter is an error once the gate is frozen: loosening one is not a retry."""
    bad = [f"[{t}] {k}" if k else f"[{t}]" for t, k in FROZEN if t in cfg and (k is None or k in cfg[t])]
    if bad:
        raise ValueError(f"the gate is frozen: remove {', '.join(bad)} from the config; gate parameters come from "
                         f"the gate template (ouroloop gate freeze)")


def build_runtime(cfg: dict, base: str | Path = ".") -> Runtime:
    """The v1 decision runtime: the config's backends and points, the champion pointer. Only the v1 loop (agent.loop)
    and the old CLI commands need it; an attempt under the runner shell records its session in a workspace.Workspace."""
    from .train import policy
    from .evolve.gate import template_path
    from .evolve.registry import Registry
    from .points import BUILTIN
    from .train.policy import Policy
    from .runtime import Runtime
    root = workspace_root(cfg, base)
    if template_path(root).exists():
        check_frozen(cfg)
    from .train.config import build_backend, build_serving
    serving = build_serving(cfg)
    backends = {name: build_backend(s, serving) for name, s in cfg.get("backends", {"mock": {"type": "mock"}}).items()}
    points = dict(cfg.get("points", {}))
    default = points.pop("default_backend", next(iter(backends)))
    # "champion" is the latest promoted model: the runtime reads it from the registry whenever a session opens.
    # Before the first promotion it is the [registry] base backend. The config gives the initial mapping of points
    # to backends; the champion's card adds the points it serves.
    champion = Registry(root).champion()
    if "champion" not in backends and cfg.get("registry", {}).get("base"):
        backends["champion"] = backends[cfg["registry"]["base"]]
    point_backends = {name: s["backend"] for name, s in points.items() if "backend" in s}
    point_backends.update({name: "champion" for name in (champion or {}).get("points", [])})
    missing = {b for b in [default, *point_backends.values()]
               if b not in backends and not (b == "champion" and champion)}
    if missing:
        raise ValueError(f"unknown backends {sorted(missing)}; a point on 'champion' needs [registry] base "
                         f"until a model has been promoted")
    if "fallback" in cfg:
        raise ValueError("the top-level fallback is gone: declare a default answerer in the phases of a point")
    rt = Runtime(root, backends, default, point_backends, seed=cfg.get("seed"), serving=serving)
    for name, s in points.items():
        if set(s) - POINT_KEYS:
            raise ValueError(f"[points.{name}] may only set {sorted(POINT_KEYS)}, not {sorted(set(s) - POINT_KEYS)}")
        if not isinstance(s.get("act", False), bool):       # act = "false" would switch the point on
            raise ValueError(f"[points.{name}] act must be true or false, not {s['act']!r}")
        if s.get("act") and not policy.may_act(name):
            raise ValueError(f"[points.{name}] act = true: of the built-in points only {sorted(policy.ACTING)} "
                             f"may act in v0.2")
        if "define" not in s and name not in BUILTIN:
            raise ValueError(f'[points.{name}] needs define = "module:function"')
        point = import_object(s["define"])() if "define" in s else BUILTIN[name]()
        if point.name != name:
            raise ValueError(f"[points.{name}] defines a point named {point.name!r}")
        # The config is the only switch: a definition cannot turn its own point on.
        point.policy = Policy(bool(s.get("act", False)), float(s.get("explore", point.policy.explore)))
        rt.register(point)
    return rt


def register_builtin_points(rt: Runtime) -> None:
    """Built-in points the config does not define keep their defaults."""
    from .points import BUILTIN
    for name, make in BUILTIN.items():
        if name not in rt.points:
            rt.register(make())


def build_research(cfg: dict, rt: Runtime):
    """-> (agent, llm, judge backend, extra run_round arguments)."""
    if "suite" in cfg:
        raise ValueError("[suite]: the coding-task suites and their prompt evolution were removed in P1 step 3 "
                         "(tag archive-2026-10-08)")
    from .evolve.pipeline import recipe_evaluator, research_context
    r = dict(cfg.get("research", {}))
    agent = import_object(r.pop("agent", "ouroloop.research.basic:BasicResearcher"))(**r.pop("options", {}))
    judge = rt.backends[r.pop("judge", rt.default_backend)]
    llm = build_provider(cfg["llm"]) if "llm" in cfg else None
    kw = {k: r[k] for k in ("seed",) if k in r}       # gate parameters come from the frozen gate template
    evaluators, context = {}, {}
    if "trainer" in cfg:
        from .train.config import build_trainer
        recipe = cfg.get("recipe", {})
        trainer = build_trainer(cfg, rt.serving)
        evaluators["recipe"] = recipe_evaluator(trainer, recipe)
        context = research_context(trainer, recipe)
    if evaluators:
        kw["evaluators"] = evaluators
    if context:
        kw["context"] = context
    return agent, llm, judge, kw
