"""The decision runtime (reharness/train/runtime.py): `record` writes the shadow row the v1 runtime wrote (compared
row for row with it in P1 step 4, before that runtime went in step 5), from the phase's default answerer or a rule's
veto, and asks no model; a broken view or rule leaves a row and the fixed default."""
from dataclasses import replace

import pytest

from reharness.train.backends import KeywordBackend
from reharness.train.decision import Noul
from reharness.train.runtime import Runtime


def says_yes(ctx) -> str:
    return "yes"


def breaks(ctx) -> str:
    raise RuntimeError("the rule broke")


def broken_view(ctx, params) -> str:
    raise KeyError("message")


def points() -> list:
    return [Noul("plain", "The task is complete", phases={"here": "no"}),
            Noul("ruled", "The task is complete", phases={"here": {"default": "rule:test_runtime:says_yes",
                                                                   "label": "no"}}),
            Noul("judged", "The task is complete", {"true": "the task is complete", "false": "work remains"},
                 phases={"here": {"default": "llm:judge", "label": "no"}}),
            Noul("broken_rule", "ok?", phases={"here": {"default": "rule:test_runtime:breaks", "label": "no"}}),
            Noul("blind", "ok?", view=broken_view, phases={"here": "no"})]


CASES = [("plain", None), ("plain", "yes"), ("plain", lambda action: "yes" if action == "no" else None),
         ("ruled", None), ("ruled", lambda action: "no"), ("judged", None), ("broken_rule", None), ("blind", None),
         ("plain", lambda action: 1 / 0)]


def run(rt) -> tuple[list, list]:
    for p in points():
        rt.register(p)
    decisions = [rt.record(name, {"message": "the task is complete"}, session=f"s{i}", phase="here", veto=veto)
                 for i, (name, veto) in enumerate(CASES)]
    rows = [{k: v for k, v in e.items() if k not in ("id", "ts", "ctx")} for e in rt.ledger.events()]
    return [replace(d, id="") for d in decisions], rows


def test_record_takes_the_default_or_the_veto_and_asks_no_model(tmp_path):
    rt = Runtime(tmp_path, {"judge": KeywordBackend()}, "judge")
    _, rows = run(rt)
    got = [(r["point"], r["route"], r["action"], r["default"], r.get("answerer"), r.get("error", "")[:12],
            r.get("answerer_error", "")[:12]) for r in rows]
    assert got == [("plain", "shadow", "no", "no", None, "", ""),
                   ("plain", "rule", "yes", "no", None, "", ""),
                   ("plain", "rule", "yes", "no", None, "", ""),
                   ("ruled", "shadow", "yes", "yes", "rule:test_runtime:says_yes", "", ""),
                   ("ruled", "rule", "no", "yes", "rule:test_runtime:says_yes", "", ""),
                   ("judged", "shadow", "yes", "no", "llm:judge", "", ""),
                   ("broken_rule", "shadow", "no", "no", "rule:test_runtime:breaks", "", "RuntimeError"),
                   ("blind", "error", "no", "no", None, "KeyError: 'm", ""),
                   ("plain", "error", "no", "no", None, "ZeroDivision", "")]
    assert {(r["backend"], r["model"], r["probs"], r["propensity"]) for r in rows} == {("none", "", None, 1.0)}
    assert not hasattr(rt, "ask")
    first = next(rt.ledger.events())["id"]
    rt.outcome(first, "yes", note="probe")
    assert list(rt.ledger.events())[-1] == {**list(rt.ledger.events())[-1], "type": "outcome", "decision": first,
                                             "label": "yes", "source": "env", "weight": 1.0, "note": "probe"}


def test_register_checks_each_phases_default(tmp_path):
    rt = Runtime(tmp_path)
    with pytest.raises(ValueError, match="is not one of"):
        rt.register(Noul("bad", "ok?", phases={"here": "maybe"}))
    with pytest.raises(ValueError, match="no backend 'judge'"):
        rt.register(Noul("bad", "ok?", phases={"here": {"default": "llm:judge", "label": "no"}}))
    with pytest.raises(ModuleNotFoundError):
        rt.register(Noul("bad", "ok?", phases={"here": {"default": "rule:nosuchmodule:f", "label": "no"}}))
