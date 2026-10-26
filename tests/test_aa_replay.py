"""The A/A replay behind ROADMAP v0.3 step 2's exit condition (examples/automationbench/aa_replay.py): the same
version read several times, every directional pair judged as candidate + confirmation against the pool of the
rest, through the production gate. A small synthetic fixture whose counts are worked out by hand; the number the
record keeps comes from the example workspaces (docs/ROADMAP.md §3 v0.3 第 2 步)."""
import importlib.util
import sys
from pathlib import Path

import pytest

from reharness.evaluate.results import Results, TaskResult

SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "automationbench" / "aa_replay.py"


def load():
    spec = importlib.util.spec_from_file_location("aa_replay", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["aa_replay"] = module
    spec.loader.exec_module(module)
    return module


TASKS = [f"t{i}" for i in range(1, 7)]


def reading(passing: set, cost: float = 0.4) -> list[TaskResult]:
    return [TaskResult(t, t, "dev", 0, "base", "run", "man", success=t in passing,
                       partial_credit=1.0 if t in passing else 0.5, cost_usd=cost) for t in TASKS]


def test_the_replay_judges_each_pair_as_a_confirmed_candidate_against_the_pooled_rest():
    """Five readings of one version over six tasks: three identical (t1–t3), one lucky (t1–t4), one short (t1–t2).
    Worked by hand — the old rule (fresh control, any drop) accepts the lucky reading against each other reading
    (4) and the three ordinary ones against the short one (3): 7/20. The new rule clears the same 7 first readings
    against the pooled rest; the confirmation (second version: it must clear min_gain alone) keeps six of them —
    the lucky reading confirmed by an ordinary one (confirmation +0.056 against the pool, which holds the short
    reading) and an ordinary one confirmed by the lucky one (+0.222) — and refuses the lucky reading confirmed by
    the short one (pooled gain 0, confirmation −0.167): 6/20."""
    aa = load()
    readings = [reading({"t1", "t2", "t3"}), reading({"t1", "t2", "t3"}), reading({"t1", "t2", "t3", "t4"}),
                reading({"t1", "t2"}), reading({"t1", "t2", "t3"})]
    out = aa.replay(readings, {t: t for t in TASKS}, TASKS)
    assert out["pairs_total"] == 20 and out["readings"] == 5
    assert out["pass_rates"] == [0.5, 0.5, 0.6667, 0.3333, 0.5]
    assert out["old_rule_accepted"] == 7 and out["first_accepted"] == 7 and out["accepted"] == 6
    refused = [p for p in out["pairs"] if p["first_verdict"] == "accepted" and p["verdict"] != "accepted"]
    assert [(p["candidate"], p["confirmation"]) for p in refused] == [(2, 3)]
    assert refused[0]["pooled_gain"] == 0.0 and refused[0]["confirmation_gain"] < 0
    assert any("below the plan's min_gain" in r for r in refused[0]["reasons"])
    # the same costs on every reading: the cost term changes nothing here
    assert out["accepted_with_cost_term"] == out["accepted"]
    # identical readings: nothing is accepted under either rule
    same = aa.replay([reading({"t1", "t2", "t3"}) for _ in range(4)], {t: t for t in TASKS}, TASKS)
    assert (same["pairs_total"], same["old_rule_accepted"], same["first_accepted"], same["accepted"]) == (12, 0, 0, 0)


def test_one_high_reading_among_equals_is_accepted_by_the_old_rule_and_by_no_confirmation():
    """The shape of the real record (one 48% reading among four at 36%), in miniature: four readings at t1–t3 and one
    at t1–t4. The old rule accepts the high reading against every other one (4/20); so did the first version of the
    confirmation (a confirmation at exactly 0 against the pool is "≥ 0"); under the second version the high reading
    clears its first reading four times and every confirmation — an ordinary reading against a pool of ordinary
    readings, gain 0 — is refused: 0/20."""
    aa = load()
    ordinary, high = {"t1", "t2", "t3"}, {"t1", "t2", "t3", "t4"}
    out = aa.replay([reading(ordinary), reading(ordinary), reading(high), reading(ordinary), reading(ordinary)],
                    {t: t for t in TASKS}, TASKS)
    assert out["old_rule_accepted"] == 4 and out["first_accepted"] == 4 and out["accepted"] == 0
    refused = [p for p in out["pairs"] if p["first_verdict"] == "accepted"]
    assert all(p["candidate"] == 2 and p["confirmation_gain"] == 0.0 and p["verdict"] == "rejected" for p in refused)
    assert all(any("confirmation reading alone has gain 0.0, below the plan's min_gain 0.05" in r
                   for r in p["reasons"]) for p in refused)


def test_the_example_workspaces_hold_five_dev_and_four_test_readings_of_w0():
    """The records the exit number is read from (docs/ROADMAP.md §3 第 2a.9 步, RESULTS.md): W0's three dev readings
    in v0.1's workspace and two in v0.2's, all at 25 tasks; four complete test readings at 23 tasks (final-1's W0
    arm, cut short by its share, is not one). The dev pass rates are the 36% and 48% on record."""
    from test_record_compat import RECORDS, ReadOnly
    aa = load()
    workspaces = tuple(RECORDS / w.name for w in aa.WORKSPACES)
    if not all(Results(w, aa.DOMAIN).path.exists() for w in workspaces):
        pytest.skip(f"the v0.1/v0.2 example workspaces are not under {RECORDS} (git-ignored run records; set "
                    f"REHARNESS_RECORDS_ROOT)")
    guards = [ReadOnly(w) for w in workspaces]
    for guard in guards:
        guard.read(Results(guard.root, aa.DOMAIN).path)
    dev, families, tasks = aa.w0_readings(workspaces, split="dev")
    assert len(dev) == 5 and len(tasks) == 25 and all(len(r) == 25 for r in dev)
    rates = sorted(round(sum(r.success for r in reading) / 25, 2) for reading in dev)
    assert rates == [0.36, 0.36, 0.36, 0.36, 0.48]
    test, _, test_tasks = aa.w0_readings(workspaces, split="test")
    assert len(test) == 4 and len(test_tasks) == 23 and all(len(r) == 23 for r in test)
    assert families and all(families[t] == t for t in tasks)
    for guard in guards:
        guard.check()
