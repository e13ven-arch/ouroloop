"""Load ouroloop.toml and build the runtime, backends, LLM, trainer and research agent from it."""
from __future__ import annotations

import importlib
import os
import tomllib
from pathlib import Path

from .backends import JevBackend, KeywordBackend, LlmBackend, TdeBackend
from .points import BUILTIN
from .runtime import Runtime


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


def import_object(ref: str):
    """"package.module:name" -> the object."""
    module, _, attr = ref.partition(":")
    return getattr(importlib.import_module(module), attr)


def build_provider(spec: dict):
    spec = dict(spec)
    kind = spec.pop("type")
    if kind == "anthropic":
        from .providers.anthropic import AnthropicProvider
        return AnthropicProvider(**spec)
    if kind == "openai_compat":
        from .providers.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(**spec)
    return import_object(kind)(**spec)


def build_backend(spec: dict):
    spec = dict(spec)
    kind = spec.pop("type")
    if kind == "mock":
        return KeywordBackend(**spec)
    if kind == "jev":
        return JevBackend(**spec)
    if kind == "tde":
        return TdeBackend(**spec)
    if kind == "llm":
        return LlmBackend(build_provider(spec["provider"]))
    if kind == "signature":
        from .evolve.train import SignatureBackend
        return SignatureBackend(spec["path"])
    if kind == "tde_remote":
        from .evolve.train import RemoteTdeBackend
        return RemoteTdeBackend(**spec)
    if kind == "http":
        from .backends.http import HttpBackend
        return HttpBackend(**spec)
    return import_object(kind)(**spec)


def build_trainer(spec: dict):
    from .evolve.train import LocalTdeTrainer, RemoteTdeTrainer, SignatureTrainer
    spec = dict(spec)
    kind = spec.pop("type")
    if kind == "signature":
        return SignatureTrainer()
    if kind == "local_tde":
        return LocalTdeTrainer(**spec)
    if kind == "remote_tde":
        return RemoteTdeTrainer(**spec)
    return import_object(kind)(**spec)


def load(path: str | Path = "ouroloop.toml") -> dict:
    """Read a config; its optional top-level env_file (relative to the config) is loaded like .env."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    if cfg.get("env_file"):
        load_dotenv(Path(path).resolve().parent / cfg["env_file"])
    return cfg


def build_runtime(cfg: dict, base: str | Path = ".") -> Runtime:
    from .evolve.registry import Registry
    backends = {name: build_backend(s) for name, s in cfg.get("backends", {"mock": {"type": "mock"}}).items()}
    points = dict(cfg.get("points", {}))
    default = points.pop("default_backend", next(iter(backends)))
    root = Path(base) / cfg.get("workspace", {}).get("root", ".")
    # "champion" is the latest promoted model; before the first promotion it is the [registry] base backend.
    champion = Registry(root).champion()
    if "champion" not in backends:
        if champion and champion.get("backend"):
            backends["champion"] = build_backend(champion["backend"])
        elif cfg.get("registry", {}).get("base"):
            backends["champion"] = backends[cfg["registry"]["base"]]
    point_backends = {name: s["backend"] for name, s in points.items() if "backend" in s}
    missing = {b for b in [default, *point_backends.values()] if b not in backends}
    if missing:
        raise ValueError(f"unknown backends {sorted(missing)}; a point on 'champion' needs [registry] base "
                         f"until a model has been promoted")
    rt = Runtime(root, backends, default, point_backends,
                 backends.get(cfg["fallback"]) if cfg.get("fallback") else None, seed=cfg.get("seed"))
    for name, s in points.items():
        if "define" not in s and name not in BUILTIN:
            raise ValueError(f'[points.{name}] needs define = "module:function"')
        point = import_object(s["define"])() if "define" in s else BUILTIN[name]()
        if point.name != name:
            raise ValueError(f"[points.{name}] defines a point named {point.name!r}")
        rt.register(point)
    return rt


def register_builtin_points(rt: Runtime) -> None:
    """Built-in points the config does not define keep their defaults."""
    for name, make in BUILTIN.items():
        if name not in rt.points:
            rt.register(make())


def build_agent_factory(cfg: dict, rt: Runtime):
    """-> (make_agent(workdir, task, prompt), identity): the coding agent that suite runs put to work.
    [suite.llm] and [suite.llm_small] override [llm] and [llm_small]; [suite] max_turns caps every task."""
    from .agent import Agent, AgentConfig, Tools
    suite = cfg.get("suite", {})
    large = suite.get("llm") or cfg.get("llm")
    if not large:
        raise ValueError("suite runs need an [llm] or [suite.llm] table")
    small = suite.get("llm_small") if "llm" in suite else cfg.get("llm_small")
    provider = build_provider(large)
    tiers = {"large": provider, "small": build_provider(small)} if small else None
    cap = suite.get("max_turns")

    def make(workdir, task, prompt):
        return Agent(provider, rt, Tools(workdir, descriptions=prompt.tools), tiers=tiers, system=prompt.system,
                     config=AgentConfig(approve="auto", max_turns=min(task.max_turns, cap or task.max_turns),
                                        verify_command=task.verify))

    # Everything besides the prompt and the task that changes a run belongs in the identity of its records.
    identity = "+".join(str(s.get("model", s["type"])) for s in (large, small) if s)
    return make, f"{identity}/max_turns={cap}" if cap else identity


def build_research(cfg: dict, rt: Runtime, base: str | Path = "."):
    """-> (agent, llm, judge backend, extra run_round arguments)."""
    from .evolve.pipeline import recipe_evaluator
    from .evolve.train import DEFAULT_RECIPE, RECIPE_KEYS
    r = dict(cfg.get("research", {}))
    agent = import_object(r.pop("agent", "ouroloop.research.basic:BasicResearcher"))(**r.pop("options", {}))
    judge = rt.backends[r.pop("judge", rt.default_backend)]
    llm = build_provider(cfg["llm"]) if "llm" in cfg else None
    kw = {k: r[k] for k in ("min_items", "test_frac", "seed") if k in r}
    evaluators, context = {}, {}
    if "trainer" in cfg:
        recipe = cfg.get("recipe", {})
        evaluators["recipe"] = recipe_evaluator(build_trainer(cfg["trainer"]), recipe, check_kwargs=cfg.get("checks"))
        context = {"recipe": {**DEFAULT_RECIPE, **recipe}, "recipe_keys": sorted(RECIPE_KEYS)}
    if "suite" in cfg:
        from .agent.suite import load_suite
        from .evolve.harness import SuiteRunner, prompt_evaluator, suite_context
        s = cfg["suite"]
        tasks = load_suite(Path(base) / s.get("path", "tasks"))
        register_builtin_points(rt)
        runner = SuiteRunner(rt.root, *build_agent_factory(cfg, rt))
        evaluators["prompt"] = prompt_evaluator(tasks, runner, **{k: s[k] for k in ("repeats", "screen", "min_items")
                                                                   if k in s})
        static, current = context, suite_context(tasks, runner)
        context = lambda: {**static, **current()}   # noqa: E731 - recomputed each round: the champion may change
    if evaluators:
        kw["evaluators"] = evaluators
    if context:
        kw["context"] = context
    return agent, llm, judge, kw
