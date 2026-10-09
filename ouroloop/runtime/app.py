"""The exported app (docs/design/RUNTIME.md §3; CONTRACTS §5): what runs a released W with nothing else.

An export directory holds `app.json` (the AppManifest's ref) and a whitelist of objects: the package and its files,
the AppManifest, and the validation summary. The manifest carries the subset of K the runner shell needs (limits,
model, worker, harness.limits) and the worker image the release was validated with. No R, no ledger, no trajectories,
no results, no credentials: the actor is a provider the caller builds from the export's model description, and the
package's code runs only in a qualified worker running the validated image. Importing an export installs nothing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..contracts import ContractError, Objects, Record, check_refs, copy_closure, register, short, write_json
from ..harness.package import load as load_package
from .records import Transcript, session_file
from .runner import PackageAgent
from .worker import usable_worker

CORE_SUBSET = ("limits", "model", "worker")
POINTER = "app.json"


class AppError(RuntimeError):
    """The export cannot run as asked: no provider, an unqualified worker, another image than the validated one."""


@register
@dataclass(frozen=True)
class AppManifest(Record):
    TYPE = "app"
    package_ref: str = ""
    core_subset: dict = field(default_factory=dict)
    validation_ref: str = ""
    image_id: str | None = None
    release_ref: str | None = None

    def __post_init__(self) -> None:
        check_refs([self.package_ref], kind="package", what="package")
        check_refs([self.validation_ref], kind="validation", what="validation summary")


def core_subset(core: dict) -> dict:
    out = {k: core[k] for k in CORE_SUBSET if k in core}
    out["harness"] = {"limits": dict((core.get("harness") or {}).get("limits") or {})}
    out["memory"] = {"recall_k": (core.get("memory") or {}).get("recall_k")}
    return out


def export(objects: Objects, release, core: dict, destination: Path, *, validation: dict) -> AppManifest:
    """Write the export of `release` (any object with package_ref and ref()) under `destination`: the package's
    closure, the validation summary and the manifest. -> the AppManifest."""
    destination = Path(destination)
    target = Objects(destination)
    copy_closure(objects, target, [release.package_ref])
    validation_ref = target.put_json("validation", dict(validation))
    image_id = (core.get("worker") or {}).get("image_id")
    manifest = AppManifest(package_ref=release.package_ref, core_subset=core_subset(core),
                           validation_ref=validation_ref, image_id=image_id, release_ref=release.ref())
    ref = target.put(manifest)
    write_json(destination / POINTER, {"manifest_ref": ref})
    return manifest


class App:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.objects = Objects(self.directory)
        pointer = json.loads((self.directory / POINTER).read_text(encoding="utf-8"))
        self.manifest: AppManifest = self.objects.load(pointer["manifest_ref"], AppManifest)
        self.package = load_package(self.objects, self.manifest.package_ref)
        self.core = dict(self.manifest.core_subset)

    def agent(self, *, provider, surface, worker, attempt_id: str, system: str = "", memory=None, pricing=None,
              task_meta: dict | None = None) -> PackageAgent:
        """The runner shell over this app. The worker must be a qualified one running the validated image."""
        if provider is None:
            raise AppError("an app runs with the caller's provider for its model; none was given")
        if not usable_worker(worker) or not getattr(worker, "qualified", False):
            raise AppError("this app's package runs code, and the worker given is not an isolated one")
        if self.manifest.image_id and worker.image_id() != self.manifest.image_id:
            raise AppError(f"the worker runs image {worker.image_id()}, and this app was validated with "
                           f"{self.manifest.image_id}")
        problem = worker.runner_problem()
        if problem:
            raise AppError(f"the isolated worker cannot serve this host: {problem}")
        transcript = Transcript(session_file(self.directory, attempt_id))
        return PackageAgent(provider=provider, package=self.package, core=self.core, surface=surface, worker=worker,
                            transcript=transcript, system=system, attempt_id=attempt_id, task_meta=task_meta,
                            memory=memory, pricing=pricing)

    def describe(self) -> dict:
        return {"package": short(self.manifest.package_ref),
                "release": short(self.manifest.release_ref) if self.manifest.release_ref else None,
                "model": (self.core.get("model") or {}).get("identity"), "image_id": self.manifest.image_id,
                "validation": self.objects.get(self.manifest.validation_ref)}


def exported_objects(directory: Path) -> set[str]:
    """Every object kind under an export: the whitelist test reads this."""
    root = Objects(directory).dir
    if not root.is_dir():
        raise ContractError(f"{directory} is not an export")
    return {p.name for p in root.iterdir() if p.is_dir()}
