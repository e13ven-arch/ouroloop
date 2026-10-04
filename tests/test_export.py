"""The minimal app (docs/modules/03_EVALUATION_RELEASE.md §4; 08 IT6-11/25; ER6-06/07/14): a whitelisted export of an
installed release runs a task from a clean directory, with no research policy, workspace or training material."""
import json
import shutil

import pytest

import rsi
from ouroloop import bench, policies as P
from ouroloop.agent import harness_package as hp
from ouroloop.app import App
from ouroloop.evolve import contracts as ct
from ouroloop.release import NotAuthorized, export_deployment
from test_harness_loop import NoteResearcher, v06_prepared


def installed(fake_upstream, tmp_path):
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path / "work")
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=NoteResearcher())
    assert result["release"] != w0
    return activity, result["release"], w0


def test_it6_11_the_export_runs_a_task_from_a_clean_directory(fake_upstream, tmp_path):
    activity, release, w0 = installed(fake_upstream, tmp_path)
    export_deployment(activity.releases, release, tmp_path / "app")
    kinds = {p.parent.name for p in (tmp_path / "app" / "rsi" / "objects").rglob("*.json")}
    assert kinds <= {"policyset", "policy", "change", "model_identity", "package", "bundle", "file", "manifest",
                     "deployment"}
    shutil.rmtree(tmp_path / "work")                                # the workspace is gone
    app = App(tmp_path / "app")
    deps = app.describe()["dependencies"]
    assert deps["model_service"]["provider"] == "rsi:BusinessWorker" and deps["model_service"]["offline"] is False
    suite = rsi.suite(fake_upstream)
    make = app.agent_factory(rsi.runtime(tmp_path / "run"), budget=rsi.run_manifest("app", "app", "dev").budget)
    task = suite.tasks["rsi.dev_notify_after_update"]
    episode = suite.episode(task.task_id)
    agent = make(episode, task, task.system)
    agent.run(task.trigger)
    assert episode.score().success                                  # the exported package made the task pass
    assert app.describe()["validation"]["verdict"] == "accepted"


def test_er6_06_the_export_carries_no_research_policy_tests_notes_or_credentials(fake_upstream, tmp_path):
    activity, release, w0 = installed(fake_upstream, tmp_path)
    export_deployment(activity.releases, release, tmp_path / "app")
    rel = activity.objects.load(release, ct.SystemRelease)
    exported = {p.stem for p in (tmp_path / "app" / "rsi" / "objects").rglob("*.json")}
    assert ct.parse_ref(rel.research_ref)[1] not in exported        # R never ships
    text = "\n".join(p.read_text() for p in (tmp_path / "app").rglob("*.json"))
    assert "api_key\"" not in text and "/Users/" not in text and "note" not in {
        p.parent.name for p in (tmp_path / "app" / "rsi" / "objects").rglob("*.json")}


def test_only_an_installed_release_exports(fake_upstream, tmp_path):
    activity, release, w0 = installed(fake_upstream, tmp_path)
    with pytest.raises(NotAuthorized):
        export_deployment(activity.releases, w0, tmp_path / "app")      # W0 was pointed at, never installed


def test_er6_14_a_code_package_ships_its_run_bundle_and_not_its_tests(tmp_path):
    objects = ct.Objects(tmp_path / "w")
    limits = hp.Limits(components=hp.KINDS)
    manifest = {**hp.empty_manifest(), "hooks": [{"name": "g", "site": "on_finish",
                                                  "entrypoint": "middlewares/g.py:run"}]}
    package = hp.build_package(objects, {"manifest.json": json.dumps(manifest),
                                         "middlewares/g.py": "def run(ctx, e):\n    return {'action': 'continue'}\n",
                                         "tests/test_g.py": "def test_x(ctx):\n    pass\n"}, limits)
    worker = P.default_worker_set(objects, package_ref=package)
    targets = (*P.targets_of("worker"), "worker.harness", *P.targets_of("research"))
    releases, rel, objects = rsi.workspace(tmp_path / "w", worker_ref=worker, targets=targets)
    ct.Events(tmp_path / "w").append("installed", release=rel)
    export_deployment(releases, rel, tmp_path / "app")
    bundle = objects.load(package, ct.HarnessPackage)
    exported = {p.stem for p in (tmp_path / "app" / "rsi" / "objects").rglob("*.json")}
    assert ct.parse_ref(bundle.bundle_ref)[1] in exported and ct.parse_ref(bundle.test_bundle_ref)[1] not in exported
    app = App(tmp_path / "app")
    bound = app.bind()
    assert bound.package.runs_code and bound.package.test_files == {}
    # its code runs only in an isolated worker: a plain subprocess is refused unless a test says otherwise
    from ouroloop.agent.worker import RUNNER, ProcessWorker
    with pytest.raises(P.BindError, match="not an isolated one"):
        app.agent_factory(rsi.runtime(tmp_path / "rt"), budget=None, provider=object(), worker=ProcessWorker(for_tests=True))
    assert app.agent_factory(rsi.runtime(tmp_path / "rt"), budget=None, provider=object(), worker=ProcessWorker(for_tests=True),
                             allow_unqualified_worker=True)
    # and only in one whose runner is this host's: an image built from another worker_runner.py is refused with the
    # command that rebuilds it (M1 §1 默认 agent 即种子包), while one running the host's runner serves
    import sys
    other = tmp_path / "worker_runner.py"
    other.write_text(RUNNER.read_text() + "\n# built from another runner\n")

    class Isolated(ProcessWorker):
        qualified = True

    class Stale(Isolated):
        def command(self):
            return [sys.executable, "-I", str(other)]
    with pytest.raises(P.BindError, match="cannot serve this host: .* rebuild the image: docker build"):
        app.agent_factory(rsi.runtime(tmp_path / "rt"), budget=None, provider=object(), worker=Stale(for_tests=True))
    assert app.agent_factory(rsi.runtime(tmp_path / "rt"), budget=None, provider=object(), worker=Isolated(for_tests=True))


def test_er6_06_a_local_path_in_a_prompt_refuses_the_export(tmp_path):
    objects = ct.Objects(tmp_path / "w")
    worker = P.default_worker_set(objects, system_prompt="Read /Users/leon/secret/notes.txt first.")
    releases, rel, objects = rsi.workspace(tmp_path / "w", worker_ref=worker)
    ct.Events(tmp_path / "w").append("installed", release=rel)
    with pytest.raises(ValueError, match="local path"):
        export_deployment(releases, rel, tmp_path / "app")
    # and nothing of what it refused is left behind
    assert not list((tmp_path / "app").rglob("*.json")) if (tmp_path / "app").exists() else True
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".export-")]
