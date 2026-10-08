"""Trainer plugins: what turns a job's data into a model, and loads that model again by its ref.

ouroloop trains decision models of any kind; a plugin is one kind. The framework does the rest the same way for all:
it builds the data, runs the job on a host (jobs/compute.py, jobs/worker.py), publishes the run to the model
store (stores/: a ref, a job id or <repo>@<commit>) and serves the model by its ref (serving.py), where a candidate
W binds it and the dev gate compares that W with its control (bench.py `_train`). A plugin is a module with:

    NAME             its name, the [trainer] type
    REQUIREMENTS     pip requirement strings its host needs besides ouroloop (`python -m ouroloop.train host init`
                     installs them); the plugin's own package may come from its options' `source`: a local directory
                     (a checkout, which host init sends) or a pip requirement (e.g. a git URL of a private package)
    SUPPORTS_WEIGHTS optional, default False: it weighs each example by meta.weight; if not, the framework hands it
                     full-weight rows only
    NEEDS_PARENT     optional, default False: it trains from a parent model; a job without one is refused before
                     it starts. The parent is the job's parent_model_ref, else the [recipe] or
                     [trainers.<name>] init_from
    RECIPE           optional: its trainer's recipe keys and their defaults ({} or absent: none), on top of the
                     framework's (train.py: min_weight, focus, seed, init_from); research candidates may
                     change them, and any other key is refused
    PATH_KEYS        optional: those of its RECIPE keys that name files on the host, which only a person sets

  worker side (on the host, in the job's directory; spec is spec.json: job, version, parent, recipe, options, ...)
    validate(spec, job_dir) -> dict      what a dry run checks, without training
    train(spec, job_dir, deadline) -> the run directory, job_dir/run, which the worker publishes to the store;
                     reads job_dir/train.jsonl and val.jsonl (the examples dataset.py describes) and maps
                     spec["recipe"] to its trainer, starting from worker.parent_run(spec, job_dir); runs any process
                     through ouroloop.train.jobs.worker.run, which stops it at `deadline`
    version() -> str | None             optional: what it trains with, recorded in result.json and host.json
    check(options) -> dict              optional: run by host init after installing, with the plugin's options, e.g.
                     whether a GPU is usable; "ok": False (with "why") keeps the host from being taken as ready

  serving side (in the process that asks the model: the control plane, or a model server)
    backend(run, ref, options) -> Backend   the model in `run`, a directory the store fetched for `ref`, loaded
                     in-process and answering as `ref`; options: the card's plugin options (a model server: its own
                     config's [trainers.<name>]) and [serving] device. Options say only how to load; what the model is
                     (its key function, its architecture) is read from the run, which train() writes self-describing
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
    """The plugin module `name`: ouroloop.train.trainers.<name> for a plain name, else the module path."""
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
