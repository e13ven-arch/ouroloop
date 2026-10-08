"""The exported app (docs/modules/03_EVALUATION_RELEASE.md §4): what runs a released W with nothing else.

An export directory holds `deployment.json` and the whitelisted objects. `App` assembles the worker policies from
those objects alone — the prompts, the finish instruction, the stop rule, the actor built from its recorded
provider configuration (the key comes from the environment variable the configuration names), and the capability
package, whose code runs only in a qualified worker. No research policy, no workspace, no training process and no
results store is read. Importing an export only checks it; it never installs anything.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import policies as P
from .evolve import contracts as ct


class App:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.objects = ct.Objects(self.directory)
        pointer = json.loads((self.directory / "deployment.json").read_text(encoding="utf-8"))
        self.manifest = self.objects.load(pointer["manifest_ref"], ct.DeploymentManifest)

    def bind(self, *, provider_factory=None) -> P.BoundRuntime:
        """The worker policies of the export, bound as a session binds them — only the worker targets exist."""
        services = P.Services(self.objects, provider_factory=provider_factory)
        bound = P.BoundRuntime(release_ref=self.manifest.source_release_ref, profile="deployment",
                               authorization="exported", services=services)
        for ref in self.objects.load(self.manifest.worker_ref, ct.PolicySet).policies:
            p = self.objects.load(ref, ct.PolicyDescriptor)
            bound.worker[p.target] = p
            P._bind_one(bound, p.target, p, services)
        return bound

    def agent_factory(self, rt, *, budget, provider=None, worker=None, provider_factory=None,
                      allow_unqualified_worker: bool = False):
        """-> make_agent(surface, task, system) running this app. The actor is the one the export pins; a
        provider passed here is used only when the export pins none. A package that runs code runs only in a
        qualified worker whose image is the one the export pins; `allow_unqualified_worker` exists for protocol
        tests and says so where it is used."""
        from .config import build_provider
        from .evolve.task_eval import make_agent_factory
        bound = self.bind(provider_factory=provider_factory or (lambda identity, config: build_provider(config)))
        if bound.package is not None and bound.package.runs_code and worker is not None \
                and not allow_unqualified_worker:
            pinned = (self.manifest.external_dependencies or {}).get("worker") or {}
            if not getattr(worker, "qualified", False):
                raise P.BindError("this app's package runs code, and the worker given is not an isolated one")
            if pinned.get("image_id") and worker.image_id() != pinned["image_id"]:
                raise P.BindError(f"the worker runs image {worker.image_id()}, and this app was validated with "
                                  f"{pinned['image_id']}")
            # The image's runner must be this host's worker_runner.py (docs/modules/01_POLICY_RUNTIME.md §1 默认
            # agent 即种子包): an image built from another would fail the package's runs as if they were its own.
            problem = worker.runner_problem()
            if problem:
                raise P.BindError(f"the isolated worker cannot serve this host: {problem}")
        actor = bound.actor if bound.actor_identity is not None else provider
        if actor is None:
            raise P.BindError("the export pins no actor and none was given")
        # The export carries K's `max_effort` instead of K itself: the app runs under the runner shell (gateway,
        # broker, default agent) as the activity did, at that ceiling; an app exported from an older K has none.
        core = self.objects.get(self.manifest.required_core_ref) if self.manifest.required_core_ref else {}
        return make_agent_factory(rt, actor, budget=budget, bound=bound, worker=worker,
                                  max_effort=(core or {}).get("max_effort"))

    def describe(self) -> dict:
        return {"release": ct.short(self.manifest.source_release_ref), "package": self.manifest.package_ref,
                "dependencies": self.manifest.external_dependencies,
                "validation": self.objects.get(self.manifest.validation_ref)}
