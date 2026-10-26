"""Serving (docs/DESIGN.md, the four roles): the one way a trained model is asked. A bound model policy, a trained
artifact's load probe and the CLI all get a model's backend here, never from the trainer that made it.

[serving] (formerly [models]) says how, in-process: a ref is fetched from the [store] and loaded by the trainer plugin
that trained it (device: the plugin's device); a model server runs this way. (A [serving] url, one model server
reached over backends/http.py's protocol, went in P1 step 5 with that client: its server, `reharness serve`, was gone
since step 3. Both are on tag archive-2026-10-08.)

A card's backend spec {"type": "model", "model": ref, "trainer": plugin, "options": {...}} is a stored model; any
other spec is a backend built in this process (config.build_backend): a "module:Class" trainer's models are in-process
only, outside the store (tests and demos; a trainer plugin is the way to bring one).
"""
from __future__ import annotations

from .types import Question, Request

KEYS = {"device"}
# The keys of the removed url mode: refused by name, so an old table says what happened rather than "may only set".
SERVER_KEYS = {"url", "token_env", "timeout"}
# Asked of a model as it is deployed: it loads, it answers, and it says which model it is.
PROBE = Request("model id probe", Question("noul", "Is this a probe?"))


class Serving:
    def __init__(self, table: dict | None = None, store=None, plugins: dict | None = None):
        """`store`: the model store refs are fetched from (in-process); `plugins`: {plugin: options} of the config
        (config.plugin_options): the plugins it may load models with, besides this package's."""
        table = dict(table or {})
        if set(table) & SERVER_KEYS:
            raise ValueError(f"[serving] {', '.join(sorted(set(table) & SERVER_KEYS))}: the model-server mode was "
                             f"removed in P1 step 5 (tag archive-2026-10-08); serving loads a ref from the [store] "
                             f"in this process (a \"hf\" store, for models trained elsewhere)")
        unknown = set(table) - KEYS
        if unknown:
            raise ValueError(f"[serving] may only set {sorted(KEYS)}, not {sorted(unknown)}")
        self.device = table.get("device")
        self.store, self.plugins = store, dict(plugins or {})
        self._loaded: dict = {}         # (ref, plugin) -> backend, in-process
        self._used: set = set()         # the plugins that loaded a model: release() asks them to let go

    def backend(self, spec: dict):
        """The backend of a card's backend spec."""
        if spec.get("type") != "model":
            from .config import build_backend
            return build_backend(spec)
        ref, trainer = spec["model"], spec.get("trainer", "tde")    # a card from before plugins names none: TDE's
        if (ref, trainer) not in self._loaded:
            self._loaded[ref, trainer] = self._load(ref, trainer, spec.get("options"))
        return self._loaded[ref, trainer]

    def _load(self, ref: str, trainer: str, options: dict | None):
        from .trainers import builtin, load
        if trainer not in self.plugins and not builtin(trainer):
            raise ValueError(f"no trainer plugin {trainer!r} here: name one of this package's, or of the config")
        if self.store is None:
            raise ValueError("no [store] to load model refs from")
        run = self.store.fetch(ref)
        opts = {**(self.plugins.get(trainer, {}) if options is None else options),
                **({"device": self.device} if self.device else {})}
        backend = load(trainer).backend(run, ref, opts)
        self._used.add(trainer)
        return backend

    def deploy(self, spec: dict) -> str:
        """Load the model and ask it the probe. -> the model id it answers as."""
        return self.backend(spec).decide([PROBE])[0].model

    def drop(self, spec: dict | None) -> None:
        """Let go of a model (a revoked one), and of its memory (a backend's close()); it is loaded again if asked."""
        if not spec or spec.get("type") != "model":
            return
        for key in [k for k in self._loaded if k[0] == spec["model"]]:
            close = getattr(self._loaded.pop(key), "close", None)     # the model itself, not only its wrapper
            if callable(close):
                close()

    def release(self) -> None:
        """Let go of the models resident on the GPU before a training job needs it."""
        from .trainers import load
        for name in sorted(self._used | set(self.plugins)):
            release = getattr(load(name), "release", None)
            if callable(release):
                release()
