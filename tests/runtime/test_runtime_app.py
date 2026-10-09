"""The exported app (docs/design/RUNTIME.md §3, §8; CONTRACTS §5): an export holds the released package's closure, the
validation summary and the manifest, and nothing else of the workspace; the App runs it only with the caller's
provider in a qualified worker running the validated image."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ouroloop.contracts import Objects, short
from ouroloop.evaluate.attempt import Budgeted
from ouroloop.evaluate.limits import Budget
from ouroloop.evaluate.release import Release
from ouroloop.runtime.app import App, AppError, AppManifest, export, exported_objects
from ouroloop.runtime.records import read, session_file
from ouroloop.runtime.runner import PackageAgent
from ouroloop.runtime.worker import DEFAULT_IMAGE, RUNNER, ProcessWorker, WorkerResources, docker_worker
from runtime_world import Script, World, core, finish, image_present, of, require, seed, turn

IMAGE_ID = "sha256:" + "a" * 64
K = core(worker={"image": DEFAULT_IMAGE, "image_id": IMAGE_ID, "network": "none", "memory": "512m", "pids": 128},
         harness={"limits": {"max_files": 96}}, research_model={"identity": "r-model", "effort": "medium"},
         pricing={"input_per_1k": 0.004}, budget={"usd": 450.0}, improver={"k": 3}, gate={"min_gain": 0.05},
         memory={"enabled": False, "recall_k": 4})
SECRET_PLAYBOOK = "R's playbook: never show this to a deployment"


def workspace(root: Path, k: dict = K):
    """A workspace with the seed released, R's policy, a gate receipt, a ledger, results and a transcript beside it."""
    objects = Objects(root)
    package = seed(root, k)
    improver = objects.put_json("improver", {"playbook": SECRET_PLAYBOOK})
    core_ref = objects.put_json("core", k)
    objects.put_json("receipt", {"verdict": "accepted", "note": SECRET_PLAYBOOK})
    release = Release(package_ref=package.ref(), improver_ref=improver, core_ref=core_ref, reason="seed")
    objects.put(release)
    for path in ("ledger/experience.jsonl", "results/rsi/results.jsonl", "transcripts/run1__t1.jsonl"):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(json.dumps({"secret": SECRET_PLAYBOOK}) + "\n")
    return objects, package, release


class Qualified(ProcessWorker):
    """The host's test worker standing in for an isolating one: it says it is qualified and names an image id."""
    qualified = True

    def __init__(self, image_id: str | None, runner: Path | None = None):
        super().__init__(for_tests=True)
        self._image_id, self._runner = image_id, runner

    def image_id(self):
        return self._image_id

    def command(self):
        return [sys.executable, "-I", str(self._runner)] if self._runner else super().command()


def test_an_export_holds_only_the_package_closure_the_validation_and_the_manifest(tmp_path):
    """Whitelisted object kinds only — package, file, app, validation — and app.json: no release, R policy, core,
    receipt, ledger, results or transcripts; the manifest carries the subset of K the runner shell needs and the
    image the release was validated with."""
    objects, package, release = workspace(tmp_path / "ws")
    dest = tmp_path / "app"
    manifest = export(objects, release, K, dest, validation={"release": short(release.ref()), "reason": "seed"})
    assert isinstance(manifest, AppManifest) and manifest.package_ref == package.ref()
    assert exported_objects(dest) == {"package", "file", "app", "validation"}
    written = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert all(p == "app.json" or p.startswith("rsi/objects/") for p in written)
    assert len([p for p in written if p.startswith("rsi/objects/file/")]) == len(set(package.files.values()))
    assert not any(SECRET_PLAYBOOK in (dest / p).read_text() for p in written)
    assert manifest.core_subset == {"limits": K["limits"], "model": K["model"], "worker": K["worker"],
                                    "harness": {"limits": {"max_files": 96}}, "memory": {"recall_k": 4}}
    assert (manifest.image_id, manifest.release_ref) == (IMAGE_ID, release.ref())
    app = App(dest)
    assert app.package.ref() == package.ref() and app.package.files == package.files
    assert app.describe() == {"package": short(package.ref()), "release": short(release.ref()),
                              "model": "scripted-1", "image_id": IMAGE_ID,
                              "validation": {"release": short(release.ref()), "reason": "seed"}}


def test_the_app_refuses_a_missing_provider_an_unqualified_worker_another_image_and_a_stale_runner(tmp_path):
    objects, _, release = workspace(tmp_path / "ws")
    export(objects, release, K, tmp_path / "app", validation={})
    app = App(tmp_path / "app")
    kw = dict(surface=None, attempt_id="app:t1:0:subject")
    with pytest.raises(AppError, match="caller's provider"):
        app.agent(provider=None, worker=Qualified(IMAGE_ID), **kw)
    with pytest.raises(AppError, match="not an isolated one"):
        app.agent(provider=Script([finish()]), worker=ProcessWorker(for_tests=True), **kw)
    other = "sha256:" + "b" * 64
    with pytest.raises(AppError, match=f"runs image {other}, and this app was validated with {IMAGE_ID}"):
        app.agent(provider=Script([finish()]), worker=Qualified(other), **kw)
    stale = tmp_path / "worker_runner.py"
    stale.write_text(RUNNER.read_text() + "\n# another runner\n")
    with pytest.raises(AppError, match="cannot serve this host"):
        app.agent(provider=Script([finish()]), worker=Qualified(IMAGE_ID, stale), **kw)


def test_the_app_runs_the_released_package_under_the_runner_shell_with_ks_subset(tmp_path):
    k = core(limits={"max_tool_calls": 1}, worker=K["worker"])
    objects, package, release = workspace(tmp_path / "ws", k)
    dest = tmp_path / "app"
    export(objects, release, k, dest, validation={})
    app = App(dest)
    world = World()
    surface = Budgeted(world, Budget.from_core(app.core["limits"]))
    agent = app.agent(provider=Script([turn("", ("api_search", {"q": "a"})), turn("", ("api_search", {"q": "b"}))]),
                      surface=surface, worker=Qualified(IMAGE_ID), attempt_id="app:t1:0:subject")
    assert isinstance(agent, PackageAgent) and agent.package.ref() == package.ref()
    result = agent.run("look up a and b")
    assert (result.termination, result.limit) == ("budget", "tool_calls")             # K's limit travelled with it
    assert world.calls == [("api_search", {"q": "a"})]
    lines = read(session_file(dest, "app:t1:0:subject"))
    assert lines[0]["package_ref"] == package.ref() and of(lines, "end")[0]["limit"] == "tool_calls"


def test_the_app_runs_in_the_validated_image(tmp_path):
    """The export of a release validated with the real worker image runs there: the qualified worker by the pinned
    content id, the seed's agent in it, the attempt finished."""
    require(image_present(DEFAULT_IMAGE), f"docker or the {DEFAULT_IMAGE} image is not available")
    image_id = docker_worker(WorkerResources()).image_id()
    k = core(worker={"image": DEFAULT_IMAGE, "image_id": image_id, "network": "none"})
    objects, package, release = workspace(tmp_path / "ws", k)
    export(objects, release, k, tmp_path / "app", validation={"reason": "seed"})
    app = App(tmp_path / "app")
    worker = docker_worker(WorkerResources.from_core(app.core))
    world = World()
    agent = app.agent(provider=Script([turn("", ("api_search", {"q": "x"})), finish("Found it.")]),
                      surface=Budgeted(world, Budget.from_core(app.core["limits"])), worker=worker,
                      attempt_id="app:t2:0:subject")
    result = agent.run("find x")
    assert (result.termination, result.text) == ("finished", "Found it.") and world.calls == [("api_search", {"q": "x"})]
    (session,) = of(read(session_file(tmp_path / "app", "app:t2:0:subject")), "worker_session")
    assert session["worker"]["kind"] == "DockerWorker" and session["worker"]["image_id"] == image_id
