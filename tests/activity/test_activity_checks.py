"""Startup checks (docs/design/ACTIVITY.md §3, §7, §9; DESIGN §6): one failing case per check of the three kinds —
money, leakage, correctness — each found before a phase pays for anything."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys

import pytest
import rsi

from ouroloop.activity import checks, phases, presets
from ouroloop.activity.checks import startup_checks
from ouroloop.activity.workspace import open_activity
from ouroloop.adapters import automationbench as ab
from ouroloop.harness import seed as sd
from ouroloop.runtime import egress as eg
from ouroloop.runtime import worker as wk
from ouroloop.runtime.worker import DEFAULT_IMAGE, DockerWorker, ProcessWorker, docker_worker


def activity(tmp_path, fake_upstream, **sections):
    """The fixture activity; `sections` are config tables, except `provider` and `worker`, which are objects."""
    return rsi.activity(tmp_path, fake_upstream, **sections)


def problems(a, phase="baseline") -> list[str]:
    return startup_checks(a, phase=phase)


def only(found: list[str], text: str) -> str:
    hits = [p for p in found if text in p]
    assert len(hits) == 1, found
    return hits[0]


def test_the_fixture_activity_passes_every_check(tmp_path, fake_upstream):
    assert problems(activity(tmp_path, fake_upstream)) == []


# ---------------------------------------------------------------------------- money
def test_a_paid_phase_without_a_dollar_root_is_refused_before_any_call(tmp_path, fake_upstream):
    provider = rsi.BusinessWorker()
    a = activity(tmp_path, fake_upstream, provider=provider, budget={"usd": None, "final_usd": None})
    only(problems(a), "[budget] usd is not set")
    with pytest.raises(RuntimeError, match=r"the baseline phase cannot start: \[budget\] usd is not set"):
        phases.baseline(a, out=lambda _line: None)
    assert provider.calls == 0 and list(a.results.all()) == []


@pytest.mark.parametrize("pricing", [{"max_output_tokens": None}, {"hidden_fallback": True}])
def test_prices_that_do_not_bound_a_call_are_refused(tmp_path, fake_upstream, pricing):
    a = activity(tmp_path, fake_upstream, pricing=pricing)
    assert "a usd limit needs a derivable upper bound per call" in only(problems(a), "cannot be enforced")


def test_a_final_share_that_cannot_hold_the_in_flight_reservations_is_refused(tmp_path, fake_upstream):
    a = activity(tmp_path, fake_upstream, budget={"final_usd": 0.1})          # one call's bound is $0.26
    only(problems(a), "the final share $0.1 cannot hold 1 in-flight reservations of $0.2600")


def test_a_model_that_is_not_the_claude_code_cli_is_refused(tmp_path, fake_upstream, monkeypatch):
    table = json.loads(json.dumps(presets.PRESETS[rsi.TEST_PRESET]))
    table["research_model"]["type"] = "openai_compat"
    monkeypatch.setitem(presets.PRESETS, "rsi_test_openai", table)
    a = activity(tmp_path, fake_upstream, harness={"preset": "rsi_test_openai"},
                 research_model={"type": "openai_compat"})
    only(problems(a), "[research_model] type 'openai_compat': the actor and R run through the claude_code CLI")


def test_an_effort_below_ks_is_refused(tmp_path, fake_upstream):
    a = activity(tmp_path, fake_upstream, research_model={"effort": "low"})
    only(problems(a), "[research_model] effort 'low' is not K's 'medium'")


def test_max_tokens_times_the_requests_of_one_cli_call_must_fit_the_reservation(tmp_path, fake_upstream):
    # [pricing] max_output_tokens is 5000: one CLI call can make 5 requests of up to 1000 tokens, not of 1001
    assert not [p for p in problems(activity(tmp_path / "a", fake_upstream)) if "max_tokens" in p]
    a = activity(tmp_path / "b", fake_upstream, model={"max_tokens": 1001})
    only(problems(a), "[model] max_tokens 1001 times the 5 requests one CLI call can make is above [pricing] "
                      "max_output_tokens 5000")


def test_a_phase_that_calls_no_model_has_no_money_check(tmp_path, fake_upstream):
    a = activity(tmp_path, fake_upstream, budget={"usd": None, "final_usd": None})
    assert startup_checks(a, phase="smoke", needs_model=False) == []


# ---------------------------------------------------------------------------- leakage
def test_upstream_drift_from_the_frozen_split_is_refused(tmp_path, fake_upstream, monkeypatch):
    first = activity(tmp_path, fake_upstream)
    task = rsi.DEV[0]
    real = ab.load_suite

    def changed(domain, path=None):
        suite = real(domain, path)
        tasks = {**suite.tasks, task: dataclasses.replace(suite.tasks[task], contract="another-contract")}
        return ab.Suite(domain, tasks, {t: suite.grading(t) for t in suite.ids()}, suite.versions)
    monkeypatch.setattr(ab, "load_suite", changed)
    lines = []
    a = open_activity(first.config, out=lines.append, worker_factory=first.worker_factory,
                      provider_factory=first.provider_factory)
    assert a.core_ref == first.core_ref and a.drift and task in a.drift[0]
    assert any(line.startswith("DRIFT") and task in line for line in lines)
    only(problems(a), "upstream drifted from the frozen split")


def test_a_test_read_ends_development(tmp_path, fake_upstream):
    a = activity(tmp_path, fake_upstream)
    a.events.append("test_read", plan="finalization:sha256:" + "0" * 64, experiment="x", tasks=2)
    for phase in ("smoke", "baseline", "library", "round"):
        only(startup_checks(a, phase=phase), "1 final run(s) already read the test tasks")
    assert not [p for p in problems(a, "evaluate") if "test tasks" in p]


def test_the_library_phase_needs_the_memory_on_and_a_line_k_does_not_hold_out(tmp_path, fake_upstream):
    off = activity(tmp_path / "off", fake_upstream)
    only(problems(off, "library"), "[memory] enabled = false: this activity runs no memory stream")
    assert problems(off) == []                                  # the baseline of a W0 reads no memory
    held = activity(tmp_path / "held", fake_upstream, memory={"enabled": True, "heldout_domains": ["operations", "rsi"]})
    only(problems(held, "library"), "rsi is a held-out business line")


def test_the_library_phase_needs_the_sidecar_k_pinned(tmp_path, fake_upstream, monkeypatch):
    """No venv for the lock (here: an empty directory named as the venv) is refused before anything is paid."""
    monkeypatch.setenv("OUROLOOP_MEMORY_VENV", str(tmp_path / "no-venv"))
    a = activity(tmp_path, fake_upstream, memory={"enabled": True})
    only(problems(a, "library"), "the memory sidecar's venv: ")


# ---------------------------------------------------------------------------- correctness
def test_a_code_change_under_the_activity_is_refused(tmp_path, fake_upstream, monkeypatch):
    a = activity(tmp_path, fake_upstream)
    monkeypatch.setattr(checks, "code_version", lambda: "0000000000000000")
    only(problems(a), f"K froze code {a.core.code_version} and this checkout is 0000000000000000")


def test_a_seed_source_changed_under_the_activity_is_refused(tmp_path, fake_upstream, monkeypatch):
    a = activity(tmp_path, fake_upstream)
    copy = tmp_path / "seeds"
    shutil.copytree(sd.SEEDS, copy, ignore=shutil.ignore_patterns("__pycache__"))
    system = copy / "stirrup" / "prompt" / "system.md"
    system.write_text(system.read_text(encoding="utf-8") + "\nAlways double-check.\n", encoding="utf-8")
    monkeypatch.setattr(sd, "SEEDS", copy)
    only(problems(a), "the seed's source changed under the activity")
    monkeypatch.setattr(sd, "SEEDS", tmp_path / "missing")
    only(problems(a), "no seed named 'stirrup'")


def test_no_worker_is_refused(tmp_path, fake_upstream):
    a = activity(tmp_path, fake_upstream)
    a.worker_factory = lambda: None
    only(problems(a), "no qualified worker: Docker or the image ouroloop-worker:py3.12")
    assert startup_checks(a, phase="smoke", needs_model=False) == []           # the checks-only smoke needs none


class StaleProcessWorker(ProcessWorker):
    """A process worker that runs another worker_runner.py than the host's: an image built before the runner
    changed."""

    def __init__(self, runner):
        super().__init__(for_tests=True)
        self.runner = runner

    def command(self) -> list[str]:
        return [sys.executable, "-I", str(self.runner)]


def test_a_worker_running_another_runner_is_refused(tmp_path, fake_upstream):
    runner = tmp_path / "worker_runner.py"
    runner.write_text(wk.RUNNER.read_text(encoding="utf-8") + "\n# built from an older checkout\n", encoding="utf-8")
    a = activity(tmp_path, fake_upstream, worker=StaleProcessWorker(runner))
    found = only(problems(a), "the worker cannot serve this host")
    assert f"this host's is {wk.runner_digest()[:12]}" in found


def test_a_proxy_k_needs_its_egress_image(tmp_path, fake_upstream, monkeypatch):
    table = json.loads(json.dumps(presets.PRESETS[rsi.TEST_PRESET]))
    table["worker"]["network"] = {"mode": "proxy", "allow": [{"host": "slack.com", "port": 443, "methods": ["POST"]}]}
    monkeypatch.setitem(presets.PRESETS, "rsi_test_proxy", table)
    a = activity(tmp_path, fake_upstream, harness={"preset": "rsi_test_proxy"})
    monkeypatch.setattr(eg, "DEFAULT_IMAGE", "ouroloop-egress:not-built")
    only(problems(a), "K's network is the egress proxy and the image ouroloop-egress:not-built is not built")


# ---------------------------------------------------------------------------- the qualified worker K pins
@pytest.fixture(scope="module")
def docker():
    """Docker and the worker image. Without them these tests are skipped, never passed; where the image was built
    for the full run (OUROLOOP_TEST_REQUIRE_DOCKER) a missing worker fails them."""
    if docker_worker() is None:
        if os.environ.get("OUROLOOP_TEST_REQUIRE_DOCKER"):
            pytest.fail(f"OUROLOOP_TEST_REQUIRE_DOCKER is set and docker or the {DEFAULT_IMAGE} image is not available")
        pytest.skip(f"docker or the {DEFAULT_IMAGE} image is not available")


def test_ks_docker_worker_serves_this_host_and_a_pin_it_does_not_hold_is_no_worker(docker, tmp_path, fake_upstream):
    a = activity(tmp_path / "a", fake_upstream)
    a.worker_factory = None                                        # the worker K describes, as `ouroloop` builds it
    worker = a.worker()
    assert isinstance(worker, DockerWorker) and worker.qualified and worker.runner_problem() is None
    assert problems(a) == []
    report = phases.smoke(a, out=lambda _line: None)
    assert report["ok"] and [r["validity"] for r in report["attempts"]] == ["valid"]
    # K pins the image by its content id: a tag that now names other bytes is not K's worker
    cfg = rsi.config(tmp_path / "b", fake_upstream, worker={"image_id": "sha256:" + "0" * 64})
    rsi.write_split(tmp_path / "b", rsi.suite(fake_upstream))
    pinned = open_activity(cfg, out=lambda _line: None, provider_factory=lambda _table: rsi.BusinessWorker())
    assert pinned.k["worker"]["image_id"] == "sha256:" + "0" * 64
    only(problems(pinned), f"no qualified worker: Docker or the image {DEFAULT_IMAGE} (pinned id sha256:000")
    assert startup_checks(pinned, phase="smoke", needs_model=False) == []


def test_the_claude_cli_an_activity_first_ran_on_is_recorded_and_a_changed_one_refused(tmp_path, monkeypatch):
    """The CLI is the actor's transport: its version is recorded at the first paid phase, and a later paid phase on
    another version is refused (a comparison holds the transport fixed). A test's providers record nothing."""
    from types import SimpleNamespace
    from ouroloop.activity import checks
    from ouroloop.contracts import Events
    import ouroloop.providers.claude_code as cc
    version = {"now": "2.1.288 (Claude Code)"}
    monkeypatch.setattr(cc, "cli_version", lambda binary="claude": version["now"])
    table = {"type": "claude_code", "model": "m", "effort": "medium", "max_tokens": 1, "timeout": 1}
    a = SimpleNamespace(provider_factory=None, model_table=lambda section="model": table, events=Events(tmp_path))
    assert checks.cli_versions(a, "baseline") == (None, "2.1.288 (Claude Code)")
    checks.require([], phase="baseline", activity=a)
    checks.require([], phase="library", activity=a)
    assert [e["version"] for e in a.events.read("cli_version")] == ["2.1.288 (Claude Code)"]
    version["now"] = "2.2.0 (Claude Code)"
    assert checks.cli_versions(a, "round") == ("2.1.288 (Claude Code)", "2.2.0 (Claude Code)")
    assert checks.cli_versions(a, "report") == (None, None)                 # a phase that calls no model
    scripted = SimpleNamespace(provider_factory=lambda t: None, model_table=a.model_table, events=a.events)
    assert checks.cli_versions(scripted, "round") == (None, None)
