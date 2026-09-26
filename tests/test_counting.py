"""How thresholds and the post-act monitor count risk: per session, a session harmed when any decision the model acted
on in it is wrong; the budget is the harmed share of the sessions it acts in (PLAN_v0.2 M7, M9)."""
import random

import pytest

from ouroloop.decision import Noul, _digest
from ouroloop.evolve.calibrate import calibrate_point
from ouroloop.evolve.monitor import realized, watch
from ouroloop.policy import Policy, cp_lower, cp_upper, fit_threshold, harmed
from ouroloop.runtime import Runtime
from ouroloop.types import Answer


def naive(confidences, correct, sessions, risk):
    """fit_threshold as its docstring says, cut-off by cut-off."""
    best = None
    for cut in sorted(set(confidences), reverse=True):
        by = harmed((s, ok) for conf, ok, s in zip(confidences, correct, sessions) if conf >= cut)
        if cp_upper(0, len(by)) > risk:
            continue
        if cp_upper(sum(by.values()), len(by)) > risk:
            break
        best = cut
    return best


def test_the_fast_fit_is_the_rule_cut_off_by_cut_off():
    for seed in range(20):
        rng = random.Random(seed)
        n, k = rng.randint(50, 400), rng.randint(20, 150)
        conf = [round(rng.uniform(0.5, 1.0), 2) for _ in range(n)]
        correct = [rng.random() < 0.9 + 0.1 * c - 0.02 for c in conf]
        sessions = [f"s{rng.randrange(k)}" for _ in range(n)]
        assert fit_threshold(conf, correct, sessions, 0.2) == naive(conf, correct, sessions, 0.2)


def test_a_small_error_per_decision_over_many_decisions_a_session_buys_no_threshold():
    """Wrong on 1% of its decisions at every confidence, 100 decisions a session: counted per decision it may act on
    all of them, but about two sessions in three would be harmed."""
    rng = random.Random(3)
    conf = [round(rng.uniform(0.6, 1.0), 3) for _ in range(100 * 100)]
    correct = [rng.random() > 0.01 for _ in conf]
    sessions = [f"s{i // 100}" for i in range(len(conf))]
    assert fit_threshold(conf, correct) == 0.6                        # each decision its own session
    assert fit_threshold(conf, correct, sessions) is None
    assert sum(harmed(zip(sessions, correct)).values()) == 72


def test_one_row_per_session_is_the_row_count():
    rng = random.Random(0)
    conf = [rng.uniform(0.6, 1.0) for _ in range(300)]
    correct = [rng.random() < c + 0.1 for c in conf]        # wrong only below 0.9
    assert fit_threshold(conf, correct, [f"s{i}" for i in range(300)]) == fit_threshold(conf, correct) is not None
    few = [f"s{i % 5}" for i in range(300)]                 # 300 rows, but 5 sessions: no evidence for a threshold
    assert fit_threshold([0.9] * 300, [True] * 300, few) is None
    # 59 sessions without a harm buy a 5% threshold, 58 do not, however many rows each has
    assert fit_threshold([0.9] * 590, [True] * 590, [f"s{i % 59}" for i in range(590)]) == 0.9
    assert fit_threshold([0.9] * 580, [True] * 580, [f"s{i % 58}" for i in range(580)]) is None


def test_the_sequence_is_deterministic_and_a_harmed_session_stays_harmed():
    rng = random.Random(1)
    rows = [(round(rng.uniform(0.5, 1.0), 2), rng.random() > 0.03, f"s{rng.randrange(80)}") for _ in range(600)]
    fitted = fit_threshold(*map(list, zip(*rows)), risk=0.2)
    for seed in range(5):                                   # whatever order the rows come in
        random.Random(seed).shuffle(rows)
        assert fit_threshold(*map(list, zip(*rows)), risk=0.2) == fitted
    # a session wrong once at the top is harmed at every lower cut-off, however many right rows join it
    sessions = [f"s{i}" for i in range(60) for _ in range(20)]
    correct = [j > 0 for _ in range(60) for j in range(20)]
    conf = [1.0 if j == 0 else 0.9 for _ in range(60) for j in range(20)]
    assert fit_threshold(conf, correct, sessions, 0.2) is None
    assert harmed(zip(sessions, correct)) == {f"s{i}": True for i in range(60)}


def test_a_model_just_above_its_risk_gets_a_threshold_at_most_alpha_of_the_time():
    """Harms 6% of its sessions at every confidence, against a 5% budget, one row a session: each cut-off is a
    Clopper-Pearson test at alpha = 5%. Keeping the lowest cut-off that passes anywhere, after others failed, lets
    luck pick among dozens of nested tests: some subset of 59 or more sessions without a harm turned up in a
    quarter of the calibrations. Fixed-sequence testing (Learn-then-Test) stops at the first cut-off that fails."""
    granted = 0
    for seed in range(100):
        rng = random.Random(seed)
        conf = [round(rng.uniform(0.4, 1.0), 4) for _ in range(6000)]
        correct = [rng.random() > 0.06 for _ in conf]
        granted += fit_threshold(conf, correct, risk=0.05) is not None
    assert granted <= 7


def test_the_sequence_ends_at_the_first_cut_off_that_fails():
    # 70 sessions right at 0.99, then 10 wrong at 0.9, then 400 right at 0.8: 0.8 alone would pass (10 of 480
    # harmed, bound 3.4%), but 0.9 failed first, so 0.99 stands; and the sequence starts at the first cut-off with
    # enough sessions to pass at all (59), whatever came before it
    conf = [0.99] * 70 + [0.9] * 10 + [0.8] * 400
    correct = [True] * 70 + [False] * 10 + [True] * 400
    assert cp_upper(10, 480) <= 0.05 and fit_threshold(conf, correct) == 0.99
    assert fit_threshold([1.0] + conf[:80], [False] + correct[:80]) is None       # 1 of 71 harmed: the start fails
    assert fit_threshold([1.0] * 3 + [0.99] * 100, [False] * 3 + [True] * 100) is None
    assert fit_threshold([1.0] * 58 + [0.99] * 100, [True] * 58 + [True] * 100) == 0.99


def test_a_points_spec_hash_has_no_counting_rule():
    with pytest.raises(TypeError):
        Noul("check", "ok?", phases={"here": "no"}, count="sampled")
    point = Noul("check", "ok?", phases={"here": "no"})
    assert point.spec_hash == _digest(["noul", point.spec.to_json(), "1", 4000, "tail", {"here": "no"}])  # unchanged


def test_a_rule_answerer_is_part_of_the_routing_default_a_threshold_is_keyed_by():
    """The rule's answer is what the model is compared with: a threshold fitted against one rule is not one against
    another, nor against the fixed label alone."""
    fixed = Noul("check", "ok?", phases={"here": "no"})
    one, other = (Noul("check", "ok?", phases={"here": {"default": f"rule:rules:{fn}", "label": "no"}})
                  for fn in ("one", "other"))
    assert len({fixed.spec_hash, one.spec_hash, other.spec_hash}) == 3


class Says:
    """Answers "yes" with probability p to everything, as model A."""
    name = "says"

    def __init__(self, p=0.9, model="A"):
        self.p, self.model = p, model

    def decide(self, requests):
        return [Answer({"yes": self.p, "no": 1 - self.p}, self.model) for _ in requests]


def acting(tmp_path):
    rt = Runtime(tmp_path, {"says": Says()}, "says", seed=0)
    rt.register(Noul("check", "ok?", phases={"here": "no"}, policy=Policy(act=True, explore=0.0)))
    return rt


def play(rt, sessions, wrong=(0,), start=0):
    """Sessions of 20 decisions: the model says yes to all; the environment says "no" at the steps in `wrong`."""
    for i in range(start, start + sessions):
        for j in range(20):
            d = rt.ask("check", {"i": i, "j": j}, session=f"s{i:03d}")
            rt.outcome(d.id, "no" if j in wrong else "yes", source="env")


def test_calibration_counts_harmed_sessions(tmp_path):
    rt = acting(tmp_path / "once")
    play(rt, 60)                            # one wrong decision in every session: every session harmed
    rows = [(r, *r.target()) for r in rt.ledger.rows("check")]
    assert calibrate_point(rt, "check", rows=rows, risk=0.2).thresholds["here"] is None
    rt = acting(tmp_path / "never")
    play(rt, 60, wrong=())
    rows = [(r, *r.target()) for r in rt.ledger.rows("check")]
    assert calibrate_point(rt, "check", rows=rows, risk=0.2).thresholds["here"] is not None


def test_the_phases_that_would_act_share_the_points_risk(tmp_path):
    """The monitor and the gate count a point's harmed sessions over all its phases: two phases wrong in different
    sessions, each within the risk alone, would harm a quarter of the sessions together."""
    rt = Runtime(tmp_path, {"says": Says()}, "says", seed=0)
    rt.register(Noul("check", "ok?", phases={"a": "no", "b": "no"}, policy=Policy(act=True, explore=0.0)))
    for i in range(400):
        for phase, wrong in (("a", i < 50), ("b", 50 <= i < 100)):
            d = rt.ask("check", {"i": i, "phase": phase}, session=f"s{i:03d}", phase=phase)
            rt.outcome(d.id, "no" if wrong else "yes", source="env")
    rows = [(r, *r.target()) for r in rt.ledger.rows("check")]
    by = harmed((r.decision["session"], max(t, key=t.get) == "yes") for r, t, _ in rows)
    assert (sum(by.values()), len(by)) == (100, 400)
    alone = calibrate_point(rt, "check", rows=[r for r in rows if r[0].decision["phase"] == "a"], risk=0.2)
    assert alone.thresholds["a"] is not None
    assert calibrate_point(rt, "check", rows=rows, risk=0.2).thresholds == {"a": None, "b": None}


def test_the_monitor_revokes_a_model_that_harms_more_sessions_than_the_budget(tmp_path, gated):
    rt = acting(tmp_path)
    gated(rt, "check", "A", 0.5)
    rng = random.Random(0)
    for i in range(40):                      # acts on every decision; wrong on 3% of them
        for j in range(20):
            d = rt.ask("check", {"i": i, "j": j}, session=f"s{i:03d}")
            assert d.acted
            rt.outcome(d.id, "no" if rng.random() < 0.03 else "yes", source="env")
    bad, n = realized(rt, "check")
    assert n == 40 and 10 < bad < 30 and cp_lower(bad, n) > 0.05
    assert watch(rt, 0.5) == []                              # within a loose budget: nothing happens
    (event,) = watch(rt, 0.05)
    assert (event["model"], event["harmed"], event["sessions"]) == ("A", bad, 40)
    assert watch(rt, 0.05) == []                             # once
    assert [e for e in rt.ledger.events() if e["type"] == "monitor"][0]["model"] == "A"
    d = rt.ask("check", {"i": 99}, session="s_after")        # never acts again, in any session
    assert (d.route, d.acted, d.action) == ("default", False, "no")
    assert rt.ledger.rows("check")[-1].decision["revoked"] == "A"


def test_the_monitor_counts_a_session_once_however_many_of_its_acts_are_wrong(tmp_path, gated):
    rt = acting(tmp_path / "harmed")
    gated(rt, "check", "A", 0.5)
    play(rt, 40, wrong=(0, 5, 10))
    assert realized(rt, "check") == (40, 40) and watch(rt, 0.05)
    rt = acting(tmp_path / "right")
    gated(rt, "check", "A", 0.5)
    play(rt, 40, wrong=())
    assert realized(rt, "check") == (0, 40) and watch(rt, 0.05) == []
    assert realized(rt, "check", sessions={"s000", "s001"}) == (0, 2)
