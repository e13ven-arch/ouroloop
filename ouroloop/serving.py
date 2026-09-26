"""Serving (docs/DESIGN.md, the four roles): the one way a trained model is asked. The runtime's sessions, calibration,
the gate's test scoring and the deploy probe all get a model's backend here, never from the trainer that made it.

[serving] (formerly [models]) says how:

    in-process          no url: a ref is fetched from the [store] and loaded by the trainer plugin that trained it
                        (device: the plugin's device); a model server runs this way
    url, token_env      one model server, `ouroloop serve` with the same [store], on any host that can read the
                        store (a "dir" store: the host that holds it; "hf": anywhere); timeout: per chunk of requests,
                        long enough for the server's first load of a model

A card's backend spec {"type": "model", "model": ref, "trainer": plugin, "options": {...}} is a stored model; any
other spec is a backend built in this process (config.build_backend), with or without a url: a "module:Class"
trainer's models are in-process only, outside the store (tests and demos; a trainer plugin is the way to bring one).
"""
from __future__ import annotations

from .types import Question, Request

KEYS = {"url", "token_env", "timeout", "device"}
# Asked of a model as it is deployed: it loads, it answers, and it says which model it is.
PROBE = Request("model id probe", Question("noul", "Is this a probe?"))


class Serving:
    def __init__(self, table: dict | None = None, store=None, plugins: dict | None = None):
        """`store`: the model store refs are fetched from (in-process); `plugins`: {plugin: options} of the config
        (config.plugin_options): the plugins it may load models with, besides this package's."""
        table = dict(table or {})
        unknown = set(table) - KEYS
        if unknown:
            raise ValueError(f"[serving] may only set {sorted(KEYS)}, not {sorted(unknown)}")
        self.url, self.device = table.get("url"), table.get("device")
        self.token_env = table.get("token_env", "OUROLOOP_TOKEN")
        self.timeout = float(table.get("timeout", 600))
        self.store, self.plugins = store, dict(plugins or {})
        self._loaded: dict = {}         # (ref, plugin) -> backend, in-process
        self._used: set = set()         # the plugins that loaded a model: release() asks them to let go

    def backend(self, spec: dict):
        """The backend of a card's backend spec."""
        if spec.get("type") != "model":
            from .config import build_backend
            return build_backend(spec)
        ref, trainer = spec["model"], spec.get("trainer", "tde")    # a card from before plugins names none: TDE's
        if self.url:
            return self._server(ref, trainer)
        if (ref, trainer) not in self._loaded:
            self._loaded[ref, trainer] = self._load(ref, trainer, spec.get("options"))
        return self._loaded[ref, trainer]

    def _server(self, ref: str | None = None, trainer: str | None = None):
        from .backends.http import HttpBackend
        return HttpBackend(self.url, model=ref, trainer=trainer, token_env=self.token_env, timeout=self.timeout)

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
        """Load the model and ask it the probe. -> the model id it answers as (a model server: POST /load)."""
        if self.url and spec.get("type") == "model":
            return self._server(spec["model"], spec.get("trainer", "tde")).load()
        return self.backend(spec).decide([PROBE])[0].model

    def drop(self, spec: dict | None) -> None:
        """Let go of a model (a revoked one), and of its memory (a backend's close()); it is loaded again if asked."""
        if not spec or spec.get("type") != "model":
            return
        if self.url:
            self._server(spec["model"]).drop()
        else:
            for key in [k for k in self._loaded if k[0] == spec["model"]]:
                close = getattr(self._loaded.pop(key), "close", None)     # the model itself, not only its wrapper
                if callable(close):
                    close()

    def release(self) -> None:
        """Let go of the models resident on the GPU before a training job needs it (a model server: POST /release)."""
        if self.url:
            self._server().release()
            return
        from .trainers import load
        for name in sorted(self._used | set(self.plugins)):
            release = getattr(load(name), "release", None)
            if callable(release):
                release()
