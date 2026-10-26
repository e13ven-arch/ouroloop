"""V1, the unit rung (docs/design/IMPROVER.md §8): the candidate's code run in the isolated worker before anything is
paid — every entrypoint imported (probe), the agent driven by the runner's scripted model over a train world (smoke:
finds deterministic crashes and protocol violations), the package's own tests. The fixture is a fresh episode of one
train task; nothing of dev or test is a fixture. A worker that is not there or dies is infrastructure, raised."""
from __future__ import annotations

import json

from ...harness.package import HOST_KINDS, classify
from ...runtime.worker import check_package


def worker_files(files: dict[str, str], *, tests: bool = True) -> dict[str, str]:
    """What the worker loads of a candidate: everything but the host-only memory (and the tests unless asked)."""
    return {p: t for p, t in files.items() if classify(p) not in HOST_KINDS and (tests or classify(p) != "test")}


def unit_check(files: dict[str, str], decl, ctx) -> dict:
    manifest = json.loads(files["manifest.json"])
    fixture = ctx.suite.episode(ctx.fixture_task) if ctx.fixture_task else None
    try:
        errors = check_package(ctx.worker, worker_files(files), manifest, fixture=fixture)
    finally:
        if fixture is not None:
            fixture.close()
    return {"passed": not errors, "detail": "; ".join(errors)[:3000]}
