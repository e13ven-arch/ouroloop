"""The training stack's part of a config: decision backends, the model store, serving and the trainer.

A config names the four roles, whatever the number of machines: this process is the control plane; [trainer] (and
[trainers.<name>]) where models train; [store] where they are kept; [serving] how they are asked. One GPU host that
trains, stores and serves is a layout of these tables, not a design (docs/TRAINING_HOSTS.md)."""
from __future__ import annotations

from ..config import build_provider, import_object

# Keys of an old config's [models] table (now [serving]) that name the store; read as its [store] (store_table).
MODELS_STORE_KEYS = {"jobs_root", "hf_prefix"}


def build_backend(spec: dict, serving=None):
    """A backend from its config table. {"type": "model", "model": ref, "trainer": plugin, "options": {...}} is a
    trained model, asked through `serving` (serving.Serving: the config's [serving] and [store])."""
    from .backends import JevBackend, KeywordBackend, LlmBackend, TdeBackend
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
    """The trainer of the [trainer] table: its type is a trainer plugin (trainers/), run as a job with the
    plugin's options ([trainers.<type>], or the table's own `options`) that publishes to [store], its models asked
    through `serving` (default: the config's); or "module:Class", a Trainer made here. An old table's transport and
    hf_prefix are read as the [store] (store_table)."""
    spec = dict(cfg["trainer"])
    kind = spec.pop("type")
    if ":" in kind:
        return import_object(kind)(**spec)
    from .jobs.trainer import JobTrainer
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
