"""How a threshold counts risk (train/policy.py fit_threshold): per session, a session harmed when any decision the
model acted on in it is wrong; the budget is the harmed share of the sessions it acts in (docs/DESIGN.md invariant
2). The calibration and the post-act monitor that counted the same way went with the champion pointer in P1 step 5."""
import random

import pytest

from reharness.train.decision import Noul, _digest
from reharness.train.policy import cp_upper, fit_threshold, harmed


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
