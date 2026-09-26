"""Load ouroloop.toml and build the runtime, backends, LLM, trainer and research agent from it."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from . import policy
from .backends import JevBackend, KeywordBackend, LlmBackend, TdeBackend
from .points import BUILTIN
from .policy import Policy
from .runtime import Runtime, import_object

# The only keys of a [points.X] table: risk comes from the gate template (M8), defaults from the point's definition.
POINT_KEYS = {"backend", "define", "act", "explore"}
# Once the gate is frozen, its parameters come from the gate template only (PLAN_v0.2 M8), never from the config.
FROZEN = (("research", "min_items"), ("suite", "min_items"), ("checks", None))
# How trained models (refs) are served: a model server, or in-process from the jobs' directories (PLAN_v0.2 M9).
MODELS_KEYS = {"url", "token_env", "timeout", "jobs_root", "hf_prefix", "device"}


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
    if kind == "manual":
        from .providers.manual import ManualProvider
        return ManualProvider(**spec)
    return import_object(kind)(**spec)


def build_backend(spec: dict, models: dict | None = None):
    """A backend from its config table. {"type": "model", "model": ref} is a trained model, served as `models` (the
    config's [models] table) says: by a model server (url, token_env), or in-process from jobs_root (and hf_prefix
    for <repo>@<commit> refs)."""
    spec = dict(spec)
    kind = spec.pop("type")
    if kind == "mock":
        return KeywordBackend(**spec)
    if kind == "jev":
        return JevBackend(**spec)
    if kind == "tde":
        return TdeBackend(**spec)
    if kind == "model":
        return model_backend(spec["model"], models)
    if kind == "llm":
        return LlmBackend(build_provider(spec["provider"]))
    if kind == "signature":
        from .evolve.train import SignatureBackend
        return SignatureBackend(spec["path"], signature=spec.get("signature"))
    if kind == "http":
        from .backends.http import HttpBackend
        return HttpBackend(**spec)
    return import_object(kind)(**spec)


def check_models(models: dict | None) -> dict:
    """The [models] table, which serves every trained model: a model server (url) or jobs_root."""
    models = models or {}
    unknown = set(models) - MODELS_KEYS
    if unknown:
        raise ValueError(f"[models] may only set {sorted(MODELS_KEYS)}, not {sorted(unknown)}")
    if not models.get("url") and not models.get("jobs_root"):
        raise ValueError("the config needs a [models] table with url (a model server) or jobs_root")
    return models


def model_backend(ref: str, models: dict | None):
    """The backend of a model ref, as [models] serves it: the model server checks the ref itself (timeout: per chunk
    of requests, long enough for the server's first load of a model), in-process it must be one a job yields
    (worker.locate)."""
    from .evolve.worker import locate
    models = check_models(models)
    if models.get("url"):
        from .backends.http import HttpBackend
        return HttpBackend(models["url"], model=ref, token_env=models.get("token_env", "OUROLOOP_TOKEN"),
                           timeout=float(models.get("timeout", 600)))
    run, revision = locate(ref, models["jobs_root"], models.get("hf_prefix"))
    return TdeBackend(run, device=models.get("device"), model=ref, revision=revision)


def build_trainer(spec: dict, models: dict | None = None):
    """A trainer from the [trainer] table; `models` is the [models] table its models are served by."""
    from .evolve.train import SignatureTrainer, TdeTrainer
    spec = dict(spec)
    kind = spec.pop("type")
    if kind == "signature":
        return SignatureTrainer(**spec)
    if kind == "tde":
        return TdeTrainer(**spec, models=models)
    return import_object(kind)(**spec)


def load(path: str | Path = "ouroloop.toml") -> dict:
    """Read a config; its optional top-level env_file (relative to the config) is loaded like .env."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    if cfg.get("env_file"):
        load_dotenv(Path(path).resolve().parent / cfg["env_file"])
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
    from .evolve.gate import template_path
    from .evolve.registry import Registry
    root = workspace_root(cfg, base)
    if template_path(root).exists():
        check_frozen(cfg)
    backends = {name: build_backend(s) for name, s in cfg.get("backends", {"mock": {"type": "mock"}}).items()}
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
    rt = Runtime(root, backends, default, point_backends, seed=cfg.get("seed"), models=cfg.get("models"))
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
    # Everything besides the prompt and the task that changes a run belongs in the identity of its records.
    identity = "+".join(str(s.get("model", s["type"])) for s in (large, small) if s)
    identity = f"{identity}/max_turns={cap}" if cap else identity

    def make(workdir, task, prompt):
        return Agent(provider, rt, Tools(workdir, descriptions=prompt.tools), tiers=tiers, system=prompt.system,
                     config=AgentConfig(approve="auto", max_turns=min(task.max_turns, cap or task.max_turns),
                                        verify_command=task.verify), session_meta={"agent": identity})

    return make, identity


def build_research(cfg: dict, rt: Runtime, base: str | Path = "."):
    """-> (agent, llm, judge backend, extra run_round arguments)."""
    from .evolve.pipeline import recipe_evaluator
    from .evolve.train import DEFAULT_RECIPE, PATH_KEYS, RECIPE_KEYS
    r = dict(cfg.get("research", {}))
    agent = import_object(r.pop("agent", "ouroloop.research.basic:BasicResearcher"))(**r.pop("options", {}))
    judge = rt.backends[r.pop("judge", rt.default_backend)]
    llm = build_provider(cfg["llm"]) if "llm" in cfg else None
    kw = {k: r[k] for k in ("seed",) if k in r}       # gate parameters come from the frozen gate template
    evaluators, context = {}, {}
    if "trainer" in cfg:
        recipe = cfg.get("recipe", {})
        evaluators["recipe"] = recipe_evaluator(build_trainer(cfg["trainer"], cfg.get("models")), recipe)
        context = {"recipe": {**DEFAULT_RECIPE, **recipe}, "recipe_keys": sorted(RECIPE_KEYS - PATH_KEYS)}
    if "suite" in cfg:
        from .agent.suite import load_suite
        from .evolve.harness import SuiteRunner, prompt_evaluator, suite_context
        s = cfg["suite"]
        tasks = load_suite(Path(base) / s.get("path", "tasks"))
        register_builtin_points(rt)
        runner = SuiteRunner(rt.root, *build_agent_factory(cfg, rt))
        evaluators["prompt"] = prompt_evaluator(tasks, runner, **{k: s[k] for k in ("repeats", "screen") if k in s})
        static, current = context, suite_context(tasks, runner)
        context = lambda: {**static, **current()}   # noqa: E731 - recomputed each round: the champion may change
    if evaluators:
        kw["evaluators"] = evaluators
    if context:
        kw["context"] = context
    return agent, llm, judge, kw
