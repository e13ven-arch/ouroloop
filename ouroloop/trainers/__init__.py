"""Trainer plugins: what turns a job's data into a model, and loads that model again by its ref.

ouroloop trains decision models of any kind; a plugin is one kind. The framework does the rest the same way for all:
it builds the data, runs the job on a host (evolve/compute.py, ouroloop/worker.py), gates, calibrates and serves the
model by its ref (a job id, or <repo>@<commit> on Hugging Face). A plugin is a module with:

    NAME             its name, the [trainer] type
    REQUIREMENTS     pip requirement strings its host needs besides ouroloop (`ouroloop host init` installs them);
                     the plugin's own package may come from its options' `source`: a local directory (a checkout,
                     which host init sends) or a pip requirement (e.g. a git URL of a private package)
    SUPPORTS_WEIGHTS optional, default False: it weighs each example by meta.weight; if not, the framework hands it
                     full-weight rows only
    NEEDS_PARENT     optional, default False: it trains from a parent model; a job without one is refused before
                     it starts

  worker side (on the host, in the job's directory; spec is spec.json: job, version, parent, recipe, options, ...)
    validate(spec, job_dir) -> dict      what a dry run checks, without training
    train(spec, job_dir, deadline) -> the run directory, which the worker publishes with transport "hf"; reads
                     job_dir/train.jsonl and val.jsonl (evolve/build.py's examples) and maps spec["recipe"] to its
                     trainer; runs any process through ouroloop.worker.run, which stops it at `deadline`
    version() -> str | None             optional: what it trains with, recorded in result.json and host.json
    check() -> dict                     optional: run by host init after installing, e.g. whether a GPU is usable

  control side (in the process that asks the model)
    backend(ref, options) -> Backend    the model a ref names, loaded in-process; options: the plugin's
                     [trainers.<name>] table and the [models] table (jobs_root, hf_prefix, device). The model server
                     loads refs through the same call
    release()                           optional: let go of loaded models (a job is about to use the GPU)

A plugin in this package is named by its module name ("tde", "signature"); any other by its module path
("mypackage.mytrainer"), which must then be installed on the host as well (REQUIREMENTS or source).
"""
from __future__ import annotations

import importlib
import importlib.util
import re

NAME = re.compile(r"[a-z][a-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")


def load(name: str):
    """The plugin module `name`: ouroloop.trainers.<name> for a plain name, else the module path."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(f"not a trainer plugin name: {name!r}")
    module = importlib.import_module(f"{__name__}.{name}" if "." not in name else name)
    for attr in ("validate", "train", "backend"):
        if not callable(getattr(module, attr, None)):
            raise ImportError(f"trainer plugin {name!r} has no {attr}()")
    return module


def builtin(name: str) -> bool:
    """Whether `name` is a plugin of this package (a model server loads only these, and those in its config)."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        return False
    return importlib.util.find_spec(f"{__name__}.{name}") is not None


def version(plugin) -> str | None:
    fn = getattr(plugin, "version", None)
    return fn() if callable(fn) else None
