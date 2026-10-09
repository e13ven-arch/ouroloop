"""The completion probe (ouroloop/train/probe.py): the stop point's environment label — was the task actually complete
when the agent decided to stop? Called directly here, on an episode of the miniature upstream and decisions the test
records: the runner shell asks no decision point, so no attempt makes a stop decision for it to label."""
import json

import pytest

from ouroloop.adapters import automationbench as ab
from ouroloop.evaluate.attempt import Budgeted
from ouroloop.evaluate.limits import Budget
from ouroloop.train.probe import CompletionProbe, ProbeError
from ouroloop.train.runtime import Runtime

LEAD = "marketing.lead_followup"
UPDATE = ("api_fetch", {"method": "PATCH", "url": "https://api.hubapi.com/crm/v3/objects/contacts",
                        "body": json.dumps({"id": "c1", "stage": "opportunity"})})
POST = ("api_fetch", {"method": "POST", "url": "https://slack.com/api/chat.postMessage",
                      "body": json.dumps({"channel": "C1", "text": "quarterly pipeline review"})})


@pytest.fixture
def suite(fake_upstream):
    return ab.load_suite("marketing", fake_upstream)


def probe(tmp_path, suite, **kw):
    rt = Runtime(tmp_path / "ws")
    episode = suite.episode(LEAD)
    surface = Budgeted(episode, Budget())
    return rt, surface, CompletionProbe(rt, episode, surface=surface, **kw)


def stop(rt, probe_, changes: int) -> str:
    """A stop decision made now, snapped as a host would snap it."""
    did = rt.ledger.decision(session="s1", point="stop", ctx="s1#0", phase="on_finish")
    probe_.snap(did, changes)
    return did


def labels(rt) -> dict:
    return {e["decision"]: e for e in rt.ledger.events() if e["type"] == "outcome"}


def test_a_finished_task_labels_its_stop_decision_yes(tmp_path, suite):
    rt, surface, p = probe(tmp_path, suite)
    for name, args in (UPDATE, POST):
        surface.run(name, args)
    did = stop(rt, p, 2)
    assert p.label() == 1
    label = labels(rt)[did]
    assert (label["label"], label["score"], label["assertions"]) == ("yes", 1.0, "2/2")


def test_stopping_too_early_is_labelled_no(tmp_path, suite):
    """One of the two required writes done: the task is not complete, and the stop decision says so."""
    rt, surface, p = probe(tmp_path, suite)
    surface.run(*UPDATE)
    did = stop(rt, p, 1)
    assert p.label() == 1
    assert (labels(rt)[did]["label"], labels(rt)[did]["score"]) == ("no", 0.5)


def test_a_label_is_an_env_outcome_that_training_may_read(tmp_path, suite):
    rt, surface, p = probe(tmp_path, suite)
    stop(rt, p, 0)
    p.label()
    outcomes = list(labels(rt).values())
    assert outcomes and all(e["source"] == "env" and e["note"] == "completion probe" for e in outcomes)
    assert rt.ledger.training_rows("stop")          # the builder can draw from these


def test_each_state_is_labelled_from_its_own_snapshot(tmp_path, suite):
    """The decision made before the second write must not be labelled from the state after it, and decisions on one
    state share its snapshot."""
    rt, surface, p = probe(tmp_path, suite)
    surface.run(*UPDATE)
    early, same = stop(rt, p, 1), stop(rt, p, 1)
    surface.run(*POST)
    late = stop(rt, p, 2)
    assert len(p.snaps) == 2 and p.label() == 3
    found = labels(rt)
    assert [(found[d]["label"], found[d]["score"]) for d in (early, same, late)] == [("no", 0.5), ("no", 0.5),
                                                                                    ("yes", 1.0)]
    assert all(s["world"] is None for s in p.snaps)        # the worlds are let go once labelled


def test_the_probe_scores_only_when_it_labels(tmp_path, suite, monkeypatch):
    """An official scorer used as an online checker would quietly correct the agent's mistakes: a snapshot is a copy,
    and scoring happens in label(), after the episode."""
    seen = []
    original = ab.Episode.score
    monkeypatch.setattr(ab.Episode, "score", lambda self, world=None: (seen.append(1), original(self, world))[1])
    rt, surface, p = probe(tmp_path, suite)
    surface.run(*UPDATE)
    stop(rt, p, 1)
    assert seen == []
    p.label()
    assert seen == [1]


def test_a_world_that_cannot_be_copied_stays_unlabelled(tmp_path, suite, monkeypatch):
    rt, surface, p = probe(tmp_path, suite)
    monkeypatch.setattr(surface.episode, "snapshot", lambda: (_ for _ in ()).throw(RuntimeError("no copy")))
    did = stop(rt, p, 0)
    assert p.label() == 0
    label = labels(rt)[did]
    assert label["label"] is None and "could not be copied" in label["error"]
    assert not rt.ledger.training_rows("stop")


def test_a_scorer_that_fails_leaves_the_label_unknown_never_no(tmp_path, suite, monkeypatch):
    rt, surface, p = probe(tmp_path, suite)
    did = stop(rt, p, 0)
    monkeypatch.setattr(ab.Episode, "score", lambda self, world=None: 1 / 0)
    assert p.label() == 0
    label = labels(rt)[did]
    assert label["label"] is None and label["error"].startswith("ZeroDivisionError")


def test_snapshots_are_bounded(tmp_path, suite):
    rt, surface, p = probe(tmp_path, suite, max_snapshots=2)
    for i in range(5):
        p.snap(f"d{i}", i)
    assert sum(s["world"] is not None for s in p.snaps) == 2 and p.dropped == 3


def test_a_surface_that_cannot_report_changes_is_refused(suite):
    class Opaque:
        def specs(self):
            return []

        def run(self, name, args):
            return None

    with pytest.raises(ProbeError, match="which calls change the world"):
        CompletionProbe(None, suite.episode(LEAD), surface=Opaque())
