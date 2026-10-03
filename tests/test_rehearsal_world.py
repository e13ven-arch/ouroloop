"""The pre-paid rehearsal's fixture world and fake actor (docs/REHEARSAL.md §8).

The rehearsal upstream is a top-level `automationbench` package, as tests/fake_upstream is, and one process can
import only one of them; so each check runs examples/rehearsal's own script in a process of its own and reads the
JSON it prints.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ouroloop.agent.worker import DEFAULT_IMAGE, docker_worker

REHEARSAL = Path(__file__).resolve().parent.parent / "examples" / "rehearsal"
KINDS = {"A": ("000", "013", "034"), "B": ("001", "014", "054"), "C": ("002", "018", "056"),
         "D": ("004", "019", "064")}
PLAIN = {"003", "005", "006", "007", "008", "027", "031", "065", "067"}


def run(script: str, *args) -> dict:
    env = {**os.environ, "PYTHONPATH": str(REHEARSAL)}
    done = subprocess.run([sys.executable, str(REHEARSAL / script), *map(str, args)], capture_output=True,
                          text=True, env=env, cwd=REHEARSAL, timeout=900)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout)


@pytest.fixture(scope="module")
def world():
    return run("check_world.py")


def test_the_world_splits_and_orders_its_tasks_as_specified(world):
    """§2: the hash puts the names where the table says, and the row order makes each half of the development
    order hold one task of every closing kind."""
    assert world["splits"] == {"train": ["000", "001", "002", "004", "005", "006", "008"],
                               "dev": ["013", "014", "018", "019", "027", "031", "034", "054", "056", "064",
                                       "065", "067"],
                               "test": ["003", "007"]}
    dev = world["splits"]["dev"]
    for kind, (train, inner, outer) in KINDS.items():
        assert world["kinds"][train] == world["kinds"][inner] == world["kinds"][outer] == kind
        assert inner in dev[:6] and outer in dev[6:]
    assert {t for t, k in world["kinds"].items() if k == "P"} == PLAIN
    assert world["contract"] == []                 # the first development task passes the scorer contract check


def test_w0_passes_only_the_plain_tasks_and_says_what_it_skipped(world):
    w0 = world["runs"]["w0"]
    assert {t for t, r in w0.items() if r["success"]} == PLAIN
    for kind, tasks in KINDS.items():
        for t in tasks:
            assert "Not done: the closing step at the end of the request" in w0[t]["text"], (kind, t)
    # the built-in fault: a tool that is not offered, an error, and the attempt goes on
    assert w0["005"]["calls"][0] == "crm_lookup" and w0["005"]["success"]


def test_each_rule_flips_its_own_kind_and_c_only_answers_a_check_before_finishing(world):
    """§3: a rule for A, B or D in the instructions or a skill fixes exactly that kind's tasks; the same rule for C
    fixes nothing; a check before finishing that quotes what the actor left undone fixes every kind."""
    runs = world["runs"]
    for kind in "ABD":
        assert {t for t, r in runs[f"memory_{kind}"].items() if r["success"]} == PLAIN | set(KINDS[kind]), kind
    assert {t for t, r in runs["memory_C"].items() if r["success"]} == PLAIN
    assert {t for t, r in runs["skill_B"].items() if r["success"]} == PLAIN | set(KINDS["B"])
    assert all(r["success"] for r in runs["hook"].values())
    assert runs["hook"]["002"]["pushes"] == 1 and runs["hook"]["006"]["pushes"] == 0


def test_the_expected_path_runs_end_to_end_offline(tmp_path):
    """§4 with scripted researchers through bench.cycle: segment-0 installs, the arms seal different W, the outer
    comparison runs and accepts R1, and the segment after it is shown to use R1. A dry run of the world's
    arithmetic, not the rehearsal."""
    if docker_worker() is None:
        if os.environ.get("OUROLOOP_TEST_REQUIRE_DOCKER"):
            pytest.fail(f"OUROLOOP_TEST_REQUIRE_DOCKER is set and docker or the {DEFAULT_IMAGE} image is missing")
        pytest.skip(f"docker or the {DEFAULT_IMAGE} image is not available")
    out = run("dry_run.py", tmp_path)
    steps = out["steps"]
    assert out["status"] == "done" and steps["segment-0"]["stop_reason"] == "rounds_done"
    assert steps["slot-0"]["verdict"] == "accepted" and steps["slot-0"]["installed"] is True
    assert steps["slot-0"]["reuse"]["state"] == "verified"
    assert [a["arm"] for a in out["arms"]] == ["control", "candidate"] and all(a["installed"] for a in out["arms"])
    research = out["research"]
    assert research["metrics"]["identical_arms"] == [] and research["metrics"]["incomplete"] == {}
    assert research["metrics"]["candidate_outer_pass_rate"] > research["metrics"]["control_outer_pass_rate"]
    assert out["revisions"] == 1 and out["hook_events"] > 0      # the Docker revision loop, and the hook ran
