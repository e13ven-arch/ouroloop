"""The data checks before training (train/checks.py) and the temperature fit (train/calibrate.py), kept in [train]
for V2 §3.2 and §3.4 (docs/modules/02_EVIDENCE_TRAINING.md §3c). Their tests were in test_evolve, which went with
the v1 pipeline in P1 step 5c; `check` reads only `ds.splits`, so they run here on examples written by hand."""
import random
from types import SimpleNamespace

from reharness.train.calibrate import fit_temperature
from reharness.train.checks import check, min_detectable_difference, ngrams
from reharness.train.types import temper

LOOSE = {"min_train": 100, "max_mde": 0.2}
SPLITS = (("train", 210), ("calibration", 60), ("test", 120))


def example(split: str, i: int, *, primitive: str = "noul", labels=("yes", "no"), answer: int | None = None) -> dict:
    """One example as dataset.to_example writes it: a state of more than 13 words, unique to its split and index."""
    answer = (0 if i % 3 else 1) if answer is None else answer
    return {"id": f"{split}-{i}", "dataset": "retry" if primitive == "noul" else "route", "primitive": primitive,
            "question": "retry the call?", "candidates": [{"name": n} for n in labels],
            "target": [1.0 if j == answer else 0.0 for j in range(len(labels))], "meta": {},
            "state": f"session {split} {i}: the call to the billing api failed with error {i % 7} after step {i} "
                     f"of the plan"}


def dataset(**kw) -> SimpleNamespace:
    return SimpleNamespace(splits={name: [example(name, i, **kw) for i in range(n)] for name, n in SPLITS})


def test_checks_block_defects_and_wait_for_data():
    ds = dataset()
    report = check(ds, **LOOSE)
    assert report.ok and report.blocking == report.insufficient == report.warnings == []
    assert report.stats["test_seen_in_train"] == 0.0
    assert report.stats["test_mde"] == round(min_detectable_difference(120), 4) < 0.2
    # the defaults: 1,000 training examples, and a test set that detects a 3-point difference
    assert sorted(m.split(";")[0] for m in check(ds).insufficient) == [
        "210 training examples", "a 120-item test set detects only 9.9% differences"]
    for e in ds.splits["train"][:100]:
        e["meta"]["truncated"] = True
    assert any("truncated" in b for b in check(ds, **LOOSE).blocking)
    for e in ds.splits["train"][:100]:
        e["meta"]["truncated"] = False
    # a training state sharing a 13-gram with an evaluation set blocks; an unrelated evaluation set does not
    leak = ds.splits["train"][0]["state"]
    assert len(leak.split()) >= 13 and ngrams(leak)
    assert any("13-gram" in b for b in check(ds, eval_states=[leak], **LOOSE).blocking)
    assert check(ds, eval_states=["an evaluation question written in words no training state uses anywhere at all"],
                 **LOOSE).ok
    # a state seen in training again at test is reported, not blocked: real logs repeat themselves
    ds.splits["test"][0] = dict(ds.splits["train"][5], id="test-0")
    report = check(ds, **LOOSE)
    assert report.ok and any("test states also occur" in w for w in report.warnings)
    assert report.stats["test_seen_in_train"] == round(1 / 120, 4)


def test_position_leak_is_blocked_for_choice_points():
    labels = ("a", "b", "c")
    ds = SimpleNamespace(splits={name: [example(name, i, primitive="choice", labels=labels, answer=i % 3)
                                        for i in range(n)] for name, n in SPLITS})
    report = check(ds, min_train=10, max_mde=1.0)
    assert not any("position" in b for b in report.blocking) and report.stats["route.position_skew"] == 0.0
    for part in ds.splits.values():     # a real position leak: the answer always in the first place shown
        for e in part:
            e["target"] = [1.0, 0.0, 0.0]
    assert any("position" in b for b in check(ds, min_train=10, max_mde=1.0).blocking)
    examples = [e for part in ds.splits.values() for e in part]
    for i, e in enumerate(examples):    # two right answers, at every pair of places in turn: no place is favoured
        pair = [(0, 1), (0, 2), (1, 2)][i % 3]
        e["target"] = [0.5 if j in pair else 0.0 for j in range(3)]
    report = check(ds, min_train=10, max_mde=1.0)
    assert abs(report.stats["route.position_skew"]) < 0.02
    assert not any("position" in x for x in report.blocking + report.warnings)


def test_a_temperature_fitted_by_nll_undoes_overconfidence():
    rng = random.Random(0)
    true_p = [rng.uniform(0.55, 0.95) for _ in range(400)]
    targets = [{"yes": 1.0} if rng.random() < p else {"no": 1.0} for p in true_p]
    sharpened = [temper({"yes": p, "no": 1 - p}, 0.5) for p in true_p]   # overconfident by a factor 2
    assert 1.6 < fit_temperature(sharpened, targets) < 2.5
    assert fit_temperature([], []) == 1.0
