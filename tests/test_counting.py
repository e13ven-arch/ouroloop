"""How a point's sessions are counted for its thresholds and by the post-act monitor: "first" (a terminal decision,
M7's rule) or "sampled" (a decision repeated within a session); one independent sample per session either way. And
the post-act monitor (PLAN_v0.2 M9)."""
import random

import pytest

from ouroloop.decision import Noul, _digest
from ouroloop.evolve.calibrate import calibrate_point
from ouroloop.evolve.monitor import realized, watch
from ouroloop.policy import Policy, cp_lower, cp_upper, fit_threshold, per_session, rank
from ouroloop.runtime import Runtime
from ouroloop.types import Answer


def naive(confidences, correct, sessions, risk, count):
    """fit_threshold as its docstring says, cut-off by cut-off."""
    place = [sessions[:i].count(s) for i, s in enumerate(sessions)]       # a row's place in its session
    best = None
    for cut in sorted(set(confidences), reverse=True):
        mine = {}
        for i, (conf, s) in enumerate(zip(confidences, sessions)):
            if conf >= cut:
                mine.setdefault(s, []).append(i)
        picked = [m[0] if count == "first" else min(m, key=lambda i: rank(s, place[i])) for s, m in mine.items()]
        if cp_upper(0, len(picked)) > risk:
            continue
        if cp_upper(sum(not correct[i] for i in picked), len(picked)) > risk:
            break
        best = cut
    return best


@pytest.mark.parametrize("count", ["first", "sampled"])
def test_the_fast_fit_is_the_rule_cut_off_by_cut_off(count):
    for seed in range(20):
        rng = random.Random(seed)
        n, k = rng.randint(50, 400), rng.randint(20, 150)
        conf = [round(rng.uniform(0.5, 1.0), 2) for _ in range(n)]
        correct = [rng.random() < 0.8 + 0.2 * c - 0.1 for c in conf]
        sessions = [f"s{rng.randrange(k)}" for _ in range(n)]
        assert fit_threshold(conf, correct, sessions, 0.2, count=count) == naive(conf, correct, sessions, 0.2, count)


def test_both_rules_count_one_row_per_session():
    rng = random.Random(0)
    conf = [rng.uniform(0.6, 1.0) for _ in range(300)]
    correct = [rng.random() < c + 0.1 for c in conf]        # wrong only below 0.9
    one_each = [f"s{i}" for i in range(300)]
    assert fit_threshold(conf, correct, one_each, count="first") == fit_threshold(conf, correct, one_each,
                                                                               count="sampled") is not None
    few = [f"s{i % 5}" for i in range(300)]                 # 300 rows, but 5 sessions: no evidence for a threshold
    assert fit_threshold([0.9] * 300, [True] * 300, few, count="first") is None
    assert fit_threshold([0.9] * 300, [True] * 300, few, count="sampled") is None
    for count in ("first", "sampled"):                      # 59 sessions without an error buy a 5% threshold, 58 do not
        assert fit_threshold([0.9] * 590, [True] * 590, [f"s{i % 59}" for i in range(590)], count=count) == 0.9
        assert fit_threshold([0.9] * 580, [True] * 580, [f"s{i % 58}" for i in range(580)], count=count) is None


def test_first_counts_the_first_acting_row_and_sampled_one_chosen_by_the_session_id():
    # each session: its first acting move wrong, the 19 after it right
    sessions = [f"s{i}" for i in range(60) for _ in range(20)]
    correct = [j > 0 for _ in range(60) for j in range(20)]
    conf = [0.9] * len(correct)
    assert fit_threshold(conf, correct, sessions, 0.2, count="first") is None       # 60 of 60 wrong
    assert fit_threshold(conf, correct, sessions, 0.2, count="sampled") == 0.9      # about 3 of 60 wrong
    items = list(zip(sessions, correct))
    first, sampled = per_session(items, "first"), per_session(items, "sampled")
    assert len(first) == len(sampled) == 60 and not any(first.values()) and 50 < sum(sampled.values()) < 60
    assert sampled == per_session(items, "sampled")                                # deterministic
    assert len({min(range(20), key=lambda j: rank(s, j)) for s in first}) > 10      # spread over the rows


@pytest.mark.parametrize("count", ["first", "sampled"])
def test_a_model_above_its_risk_rarely_gets_a_threshold(count):
    """Wrong on 8% of its decisions at every confidence, against a 5% budget. Each cut-off counts one row a session:
    a "sampled" row drawn afresh at every cut-off would give some cut-off a lucky draw almost every time (40 of 40
    here, when the lowest cut-off that passed anywhere was kept)."""
    granted = 0
    for seed in range(40):
        rng = random.Random(seed)
        conf = [round(rng.uniform(0.4, 1.0), 4) for _ in range(100 * 60)]
        correct = [rng.random() > 0.08 for _ in conf]
        granted += fit_threshold(conf, correct, [f"s{i // 60}" for i in range(len(conf))], 0.05,
                                 count=count) is not None
    assert granted <= 6


@pytest.mark.parametrize("count", ["first", "sampled"])
def test_a_model_just_above_its_risk_gets_a_threshold_at_most_alpha_of_the_time(count):
    """Wrong on 6% of its decisions at every confidence, against a 5% budget, 100 sessions: each cut-off is a
    Clopper-Pearson test at alpha = 5%. Keeping the lowest cut-off that passes anywhere, after others failed, lets
    luck pick among dozens of nested tests: some subset of 59 or more sessions without an error turned up in a
    quarter of the calibrations. Fixed-sequence testing (Learn-then-Test) stops at the first cut-off that fails."""
    granted = 0
    for seed in range(100):
        rng = random.Random(seed)
        conf = [round(rng.uniform(0.4, 1.0), 4) for _ in range(100 * 60)]
        correct = [rng.random() > 0.06 for _ in conf]
        granted += fit_threshold(conf, correct, [f"s{i // 60}" for i in range(len(conf))], 0.05,
                                 count=count) is not None
    assert granted <= 7


def test_the_sequence_ends_at_the_first_cut_off_that_fails():
    # 70 sessions right at 0.99, then 10 wrong at 0.9, then 400 right at 0.8: 0.8 alone would pass (10 of 480
    # wrong, bound 3.4%), but 0.9 failed first, so 0.99 stands; and the sequence starts at the first cut-off with
    # enough sessions to pass at all (59), whatever came before it
    conf = [0.99] * 70 + [0.9] * 10 + [0.8] * 400
    correct = [True] * 70 + [False] * 10 + [True] * 400
    assert cp_upper(10, 480) <= 0.05 and fit_threshold(conf, correct) == 0.99
    assert fit_threshold([1.0] + conf[:80], [False] + correct[:80]) is None       # 1 of 71 wrong: the start fails
    assert fit_threshold([1.0] * 3 + [0.99] * 100, [False] * 3 + [True] * 100) is None
    assert fit_threshold([1.0] * 58 + [0.99] * 100, [True] * 58 + [True] * 100) == 0.99


def test_a_point_declares_its_count_and_its_thresholds_are_keyed_by_it():
    with pytest.raises(ValueError, match="count"):
        Noul("check", "ok?", phases={"here": "no"}, count="last")
    first, sampled = Noul("check", "ok?", phases={"here": "no"}), Noul("check", "ok?", phases={"here": "no"},
                                                                         count="sampled")
    assert first.spec_hash != sampled.spec_hash
    assert first.spec_hash == _digest(["noul", first.spec.to_json(), "1", 4000, "tail", {"here": "no"}])  # unchanged


class Says:
    """Answers "yes" with probability p to everything, as model A."""
    name = "says"

    def __init__(self, p=0.9, model="A"):
        self.p, self.model = p, model

    def decide(self, requests):
        return [Answer({"yes": self.p, "no": 1 - self.p}, self.model) for _ in requests]


def acting(tmp_path, count="sampled"):
    rt = Runtime(tmp_path, {"says": Says()}, "says", seed=0)
    rt.register(Noul("check", "ok?", phases={"here": "no"}, count=count, policy=Policy(act=True, explore=0.0)))
    return rt


def play(rt, sessions, first_wrong=True, start=0):
    """Sessions of 20 decisions: the model says yes to all; the environment says the first was "no"."""
    for i in range(start, start + sessions):
        for j in range(20):
            d = rt.ask("check", {"i": i, "j": j}, session=f"s{i:03d}")
            rt.outcome(d.id, "no" if j == 0 and first_wrong else "yes", source="env")


@pytest.mark.parametrize("count", ["first", "sampled"])
def test_calibration_counts_as_the_point_declares(tmp_path, count):
    rt = acting(tmp_path, count)
    play(rt, 60)
    cal = calibrate_point(rt, "check", rows=[(r, *r.target()) for r in rt.ledger.rows("check")], risk=0.2)
    assert (cal.thresholds["here"] is None) == (count == "first")       # 60 of 60 wrong; about 3 of 60


def test_the_monitor_revokes_a_model_whose_acting_errors_exceed_the_budget(tmp_path, gated):
    rt = acting(tmp_path, "sampled")
    gated(rt, "check", "A", 0.5)
    for i in range(40):                      # acts, and is wrong on the whole second half of each session
        for j in range(10):
            d = rt.ask("check", {"i": i, "j": j}, session=f"s{i:03d}")
            assert d.acted
            rt.outcome(d.id, "no" if j >= 5 else "yes", source="env")
    errors, n = realized(rt, "check")
    assert n == 40 and 10 < errors < 30 and cp_lower(errors, n) > 0.05
    assert watch(rt, 0.5) == []                              # within a loose budget: nothing happens
    (event,) = watch(rt, 0.05)
    assert (event["model"], event["errors"], event["sessions"]) == ("A", errors, 40)
    assert watch(rt, 0.05) == []                             # once
    assert [e for e in rt.ledger.events() if e["type"] == "monitor"][0]["model"] == "A"
    d = rt.ask("check", {"i": 99}, session="s_after")        # never acts again, in any session
    assert (d.route, d.acted, d.action) == ("default", False, "no")
    assert rt.ledger.rows("check")[-1].decision["revoked"] == "A"


def test_the_monitor_counts_as_the_point_declares(tmp_path, gated):
    for count, revoked in (("first", True), ("sampled", False)):
        rt = acting(tmp_path / count, count)
        gated(rt, "check", "A", 0.5)
        play(rt, 40)                        # every session's first acting decision wrong, the other 19 right
        assert bool(watch(rt, 0.05)) == revoked, count
    rt = acting(tmp_path / "right")
    gated(rt, "check", "A", 0.5)
    play(rt, 40, first_wrong=False)
    assert realized(rt, "check") == (0, 40) and watch(rt, 0.05) == []
    assert realized(rt, "check", sessions={"s000", "s001"}) == (0, 2)
