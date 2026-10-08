"""Load a config, and build its LLM providers. The decision backends, the model store, serving and the trainer are
built by the training stack (ouroloop/train/config.py)."""
from __future__ import annotations

import importlib
import os
import tomllib
from pathlib import Path


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
