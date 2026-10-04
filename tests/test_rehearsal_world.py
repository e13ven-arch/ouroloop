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
         "D": ("004", "019", "064"), "E": ("010", "071", "076"), "F": ("011", "072", "080")}
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
    """§2 and §10.1: the hash puts the names where the tables say, the train order stages E, F and C first, and
    each half of the development order holds one task of every closing kind."""
    assert world["splits"] == {"train": ["010", "011", "002", "000", "001", "004", "005", "006", "008"],
                               "dev": ["013", "014", "018", "019", "071", "072", "027", "031", "034", "054", "056",
                                       "064", "076", "080", "065", "067"],
                               "test": ["003", "007"]}
    dev = world["splits"]["dev"]
    for kind, (train, inner, outer) in KINDS.items():
        assert world["kinds"][train] == world["kinds"][inner] == world["kinds"][outer] == kind
        assert inner in dev[:8] and outer in dev[8:]
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
    """§3 and §10.2: a rule for A, B or D in the instructions or a skill fixes exactly that kind's tasks; the same
    rule for C, E or F fixes nothing; E takes a skill, F a package tool, C a check before finishing that quotes what
    the actor left undone — which fixes A to D but not E or F."""
    runs = world["runs"]

    def fixed(run):
        return {t for t, r in runs[run].items() if r["success"]}
    for kind in "ABD":
        assert fixed(f"memory_{kind}") == PLAIN | set(KINDS[kind]), kind
    for kind in "CEF":
        assert fixed(f"memory_{kind}") == PLAIN, kind
    assert fixed("skill_B") == PLAIN | set(KINDS["B"]) and fixed("skill_E") == PLAIN | set(KINDS["E"])
    assert fixed("tool_F") == PLAIN | set(KINDS["F"]) and "add_contact_note" in runs["tool_F"]["011"]["calls"]
    assert fixed("hook") == PLAIN | {t for k in "ABCD" for t in KINDS[k]}
    assert runs["hook"]["002"]["pushes"] == 1 and runs["hook"]["006"]["pushes"] == 0


def test_the_fake_actor_answers_through_the_gateway(world):
    """M1 §1 宿主作为运行壳 and §5 (the rehearsal under the shell, v0.3): every attempt of the world runs the default agent
    over the host's gateway and broker — one host-written `gateway_call` per model turn (the tool specs once), a
    `broker_event` per base call, the skill read as a `skill_read`, and the hook's answer, the pushback and the
    package tool's run as the package's marks — so the fixture actor is reached as `[llm] type` reaches it and the
    numbers above are the shell's; no attempt of the world hits a hard limit."""
    runs = world["runs"]
    hook = runs["hook"]["002"]["kinds"]
    assert hook.count("gateway_call") == hook.count("assistant") == 4 and hook.count("tool_specs") == 1
    assert hook.count("broker_event") == 2 and hook.count("hook") == 2 and hook.count("pushback") == 1
    skill = runs["skill_B"]["001"]["kinds"]
    assert skill.count("skill_read") == 1 and skill.index("skill_read") < skill.index("broker_event")
    tool = runs["tool_F"]["011"]["kinds"]
    assert tool.count("package_tool") == 1 and tool.count("broker_event") == 2       # the tool's base call is one
    assert runs["tool_F"]["011"]["calls"] == ["api_fetch", "add_contact_note"]
    plain = runs["w0"]["003"]["kinds"]
    assert not {"hook", "pushback", "package_tool", "skill_read"} & set(plain) and "gateway_call" in plain
    assert {r["limit"] for rs in runs.values() for r in rs.values()} == {None}


def test_an_armed_fault_fails_one_attempt_and_is_used_up(world):
    """§10.2: the operator's fault fails the next attempt on its task once; the retry runs as usual."""
    assert world["fault"] == "ProviderError" and world["after_fault"] and world["fault_used_up"]


def test_the_expected_path_runs_end_to_end_offline(tmp_path):
    """§4, §8 and §10 with scripted researchers through bench.cycle, on the v0.3 R path (M4 §1: one coding session
    a round in the worker, the screen, the ledger; the v3 preset sized to the world, §8): segment-0 installs the
    package tool (the skill's gate is refused by the cost term — a skill read is a model call more — and the memory
    for C is screened out), the arms seal different W, the outer comparison runs and accepts R1, the segment after
    it runs its sessions under R1 and installs three memories, an armed fault is refilled once in a gate and once in
    the outer comparison, and the Docker revision loop runs once. A dry run of the world's arithmetic, not the
    rehearsal."""
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
    assert out["tool_events"] > 0 and out["skill_reads"] > 0
    assert sorted(r["arm"] for r in out["refills"]) == ["outer_control#1", "subject#1"]
    assert all(r["valid"] for r in out["refills"])
    # v0.3 step 3: one session and one screen per mainline round (three in each segment); the mainline ledger folds
    # to one entry per candidate across both segments (M4 §5: ids are numbered by the scope's diagnosis ordinal, so
    # segment-1's candidates do not overwrite segment-0's): E's skill rejected by the cost term, F's tool installed,
    # C's memory screened out, then the three memories installed under R1, each screened on its own failing train
    # task plus the sample
    assert out["sessions"] == 6 and out["screens"] == 6
    assert [c["candidate"] for c in out["ledger"]] == [f"rehearsal-dry-run/d{i}/c1" for i in range(6)]
    assert [(c["outcome"], c["carrier"]) for c in out["ledger"]] == [
        ("rejected", "skill"), ("installed", "tool"), ("screened_out", "memory")] + [("installed", "memory")] * 3
