"""Model stores (docs/DESIGN.md, the four roles): where a trained model lives, apart from where it was trained and where
it is served.

The training worker publishes each run to the store once; what comes back is an immutable model ref, the only thing
a model policy and a revocation hold. Serving fetches a ref from the same store, in the process that binds the
policy. A store is a module of this package named by its [store] type, with a class STORE:

    TYPE                the [store] type
    ON_TRAINING_HOST    its models are on the machine that trains them ("dir" with a training host): serving here
                        cannot load them, so JobTrainer refuses that pairing (use "hf"), and a cloud instance, gone
                        after its job, cannot hold them
    STORE(table)        the [store] table; any key it does not know is a ValueError
    .table              the table, as a job's spec.json carries it to the worker
    .job(ref)           the job id a ref of this store names, or None when it is not one of its refs
    .publish(run, job_dir, job_id) -> ref      worker side: put the run in the store, once
    .fetch(ref) -> a directory on this machine that holds the run; a ref that is not the store's is a ValueError
    .here() -> the store as seen from this machine, where the worker runs on this machine too (paths made absolute)
    .in_place(jobs_root) -> whether a run published from a job under jobs_root stays where it is: nothing to copy
                        or upload after the job's deadline
    ENV                 environment variables publishing needs: required here for a cloud job, and given to its instance
    REQUIREMENTS        pip requirements of publishing and fetching, installed on a training host by host init
    probe() -> dict     on a training machine: what its host.json records of the store there (hosts.manifest)
    ready(record) -> list[str]  why a training machine with that record (None: none) cannot publish to it

    dir     a directory, root = path: on this machine, or with a training host on that host; the ref is the job id,
            and the run is <root>/<job id>/run
    hf      private Hugging Face repositories <prefix><job id>; the ref is <repo>@<commit>

Adding a store (an object store, say) is a new module here.
"""
from __future__ import annotations

import importlib
import pkgutil
import re

JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
TYPE = re.compile(r"[a-z][a-z0-9_]*")


def kind(name) -> type:
    """The store class of a [store] type."""
    if not isinstance(name, str) or not TYPE.fullmatch(name):
        raise ValueError(f"[store] type {name!r}: \"dir\" or \"hf\"")
    try:
        return importlib.import_module(f"{__name__}.{name}").STORE
    except ModuleNotFoundError:
        raise ValueError(f"[store] type {name!r}: \"dir\" or \"hf\"") from None


def types() -> list[str]:
    """Every store type of this package."""
    return sorted(m.name for m in pkgutil.iter_modules(__path__) if TYPE.fullmatch(m.name))


def make(table: dict | None):
    """The store a [store] table describes (default: a "dir" store at ~/.reharness/jobs)."""
    table = dict(table or {"type": "dir"})
    return kind(table.get("type"))(table)


def check_keys(table: dict, keys: set) -> None:
    unknown = set(table) - keys - {"type"}
    if unknown:
        raise ValueError(f"[store] of type {table['type']!r} may only set {sorted(keys)}, not {sorted(unknown)}")
