"""Load ouroloop.toml and build the runtime, backends, LLM, trainer and research agent from it.

A config names the four roles, whatever the number of machines: this process is the control plane; [trainer] (and
[trainers.<name>]) where models train; [store] where they are kept; [serving] how they are asked. One GPU host that
trains, stores and serves is a layout of these tables, not a design (docs/TRAINING_HOSTS.md)."""
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
# Once the gate is frozen, its parameters come from the gate template only (docs/DESIGN.md invariant 4), never from the
# config.
FROZEN = (("research", "min_items"), ("suite", "min_items"), ("checks", None))
# Keys of an old config's [models] table (now [serving]) that name the store; read as its [store] (store_table).
MODELS_STORE_KEYS = {"jobs_root", "hf_prefix"}


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
    if kind == "claude_code":
        from .providers.claude_code import ClaudeCodeProvider
        return ClaudeCodeProvider(**spec)
    return import_object(kind)(**spec)


def build_backend(spec: dict, serving=None):
    """A backend from its config table. {"type": "model", "model": ref, "trainer": plugin, "options": {...}} is a
    trained model, asked through `serving` (serving.Serving: the config's [serving] and [store])."""
    spec = dict(spec)
    kind = spec.pop("type")
    if kind == "mock":
        return KeywordBackend(**spec)
    if kind == "jev":
        return JevBackend(**spec)
    if kind == "tde":
        return TdeBackend(**spec)
    if kind == "model":
        if serving is None:
            raise ValueError("a trained model is asked through serving: build it with the config's [serving]")
        return serving.backend({"type": kind, **spec})
    if kind == "llm":
        return LlmBackend(build_provider(spec["provider"]))
    if kind == "signature":
        from .trainers.signature import SignatureBackend
        return SignatureBackend(spec["path"], signature=spec.get("signature"))
    if kind == "http":
        from .backends.http import HttpBackend
        return HttpBackend(**spec)
    return import_object(kind)(**spec)


def store_table(cfg: dict) -> dict:
    """The [store] table. An old config has none: its [trainer] transport says which, "hf" a store under hf_prefix
    ([trainer] or [models]), "direct" a "dir" store at [models] jobs_root, else [trainer] jobs_root. Without a
    transport it is "direct", unless [models] has hf_prefix: an old model server's config, which loaded refs from the
    Hub. A [store] beside those old keys is an error: keep one."""
    trainer, models = cfg.get("trainer") or {}, cfg.get("models") or {}
    if "store" in cfg:
        old = [f"[trainer] {k}" for k in ("transport", "hf_prefix") if k in trainer]
        old += [f"[models] {k}" for k in sorted(MODELS_STORE_KEYS) if k in models]
        if old:
            raise ValueError(f"[store] and {', '.join(old)}: those are the old way to name the store; keep one")
        return dict(cfg["store"])
    transport = trainer.get("transport", "hf" if models.get("hf_prefix") else "direct")
    if transport not in ("direct", "hf"):
        raise ValueError(f'[trainer] transport {transport!r}: "direct" or "hf" (now a [store] table)')
    if transport == "hf":
        return {"type": "hf", "prefix": trainer.get("hf_prefix") or models.get("hf_prefix")}
    root = models.get("jobs_root") or trainer.get("jobs_root")
    return {"type": "dir", **({"root": root} if root else {})}


def serving_table(cfg: dict) -> dict:
    """The [serving] table; an old config's [models] is read as it, less the keys that now belong to [store]."""
    if "serving" in cfg and "models" in cfg:
        raise ValueError("[serving] and [models]: [models] is the old name of [serving]; keep one")
    if "serving" in cfg:
        return dict(cfg["serving"])
    return {k: v for k, v in (cfg.get("models") or {}).items() if k not in MODELS_STORE_KEYS}


def build_store(cfg: dict):
    from .stores import make
    return make(store_table(cfg))


def build_serving(cfg: dict):
    """How the config's trained models are asked: [serving] over [store], loading with the config's plugins."""
    from .serving import Serving
    return Serving(serving_table(cfg), build_store(cfg), plugin_options(cfg))


def build_trainer(cfg: dict, serving=None):
    """The trainer of the [trainer] table: its type is a trainer plugin (ouroloop.trainers), run as a job with the
    plugin's options ([trainers.<type>], or the table's own `options`) that publishes to [store], its models asked
    through `serving` (default: the config's); or "module:Class", a Trainer made here. An old table's transport and
    hf_prefix are read as the [store] (store_table)."""
    spec = dict(cfg["trainer"])
    kind = spec.pop("type")
    if ":" in kind:
        return import_object(kind)(**spec)
    from .evolve.train import JobTrainer
    for key in ("transport", "hf_prefix"):
        spec.pop(key, None)
    options = spec.pop("options", None)
    options = options if options is not None else (cfg.get("trainers") or {}).get(kind)
    return JobTrainer(kind, options, store=store_table(cfg), serving=serving or build_serving(cfg), **spec)


def plugin_options(cfg: dict) -> dict:
    """{plugin: options} of every trainer plugin the config names: [trainer] type and each [trainers.<name>]."""
    out = {name: dict(options) for name, options in cfg.get("trainers", {}).items()}
    kind = (cfg.get("trainer") or {}).get("type")
    if kind and ":" not in kind:
        out.setdefault(kind, dict(cfg["trainer"].get("options") or {}))
    return out


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
    from .evolve.gate import template_path
    from .evolve.registry import Registry
    root = workspace_root(cfg, base)
    if template_path(root).exists():
        check_frozen(cfg)
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
    from .evolve.pipeline import recipe_evaluator, research_context
    r = dict(cfg.get("research", {}))
    agent = import_object(r.pop("agent", "ouroloop.research.basic:BasicResearcher"))(**r.pop("options", {}))
    judge = rt.backends[r.pop("judge", rt.default_backend)]
    llm = build_provider(cfg["llm"]) if "llm" in cfg else None
    kw = {k: r[k] for k in ("seed",) if k in r}       # gate parameters come from the frozen gate template
    evaluators, context = {}, {}
    if "trainer" in cfg:
        recipe = cfg.get("recipe", {})
        trainer = build_trainer(cfg, rt.serving)
        evaluators["recipe"] = recipe_evaluator(trainer, recipe)
        context = research_context(trainer, recipe)
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
