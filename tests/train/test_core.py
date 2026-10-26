"""The decision primitives (train/decision.py, policy.py, types.py) and the ledger: the execution rule, thresholds
counted per session, decision points, label sources and old rows."""
import random

import pytest

from reharness.train.decision import Choice, Noul, Spec
from reharness.contracts import redact
from reharness.train.ledger import Ledger, SessionStore
from reharness.train.policy import Policy, cp_upper, fit_threshold, route
from reharness.train.types import Answer, Question


# ---------------------------------------------------------------- policy
def test_the_execution_rule_and_propensity():
    rng = random.Random(0)
    yes = Answer({"yes": 0.9, "no": 0.1})
    assert route(Policy(), yes, "no", 0.8, rng).route == "shadow"               # act is off
    on = Policy(act=True, explore=0.0)
    assert route(on, yes, "no", None, rng).route == "default"                   # no gated threshold
    assert route(on, yes, "yes", 0.8, rng).route == "default"                   # the label is the default anyway
    assert route(on, yes, "no", 0.95, rng).route == "default"                   # not confident enough
    r = route(on, yes, "no", 0.8, rng)
    assert (r.route, r.propensity) == ("act", 1.0)
    r = route(Policy(act=True, explore=1.0), yes, "no", 0.8, rng)
    assert (r.route, r.explored, r.propensity) == ("default", True, 1.0)


def test_clopper_pearson_and_threshold():
    assert abs(cp_upper(0, 100) - (1 - 0.05 ** (1 / 100))) < 1e-4
    conf = [0.9] * 200 + [0.6] * 100
    correct = [True] * 200 + [i % 2 == 0 for i in range(100)]
    assert fit_threshold(conf, correct, risk=0.05) == 0.9
    assert fit_threshold([0.9] * 10, [False] * 10, risk=0.05) is None


# ---------------------------------------------------------------- decision points
def test_spec_apply_and_hash():
    s = Spec("q", {"true": "a", "false": "b"}, {"tail": 3})
    t = s.apply({"criteria": {"false": "c"}, "view_params": {"x": 1}})
    assert t.criteria == {"true": "a", "false": "c"} and t.view_params == {"tail": 3, "x": 1}
    assert s.apply({"instructions": "q2"}).instructions == "q2"
    with pytest.raises(ValueError):
        s.apply({"model": "other"})
    p = Noul("p", "q", {"true": "a", "false": "b"})
    h = p.spec_hash
    p.spec = t
    assert p.spec_hash != h


def test_render_truncates_keeping_the_tail_and_dynamic_candidates():
    p = Noul("p", "q", view=lambda ctx, params: ctx, max_view_chars=5)
    assert p.render("0123456789") == ("56789", True)
    c = Choice("tool", "which tool", lambda ctx: {t: "" for t in ctx["tools"]})
    assert c.question({"tools": ["grep", "sed"]}).labels() == ["grep", "sed"]


# ---------------------------------------------------------------- ledger
def test_training_targets_follow_label_sources(tmp_path):
    led = Ledger(tmp_path)
    env = led.decision(point="p", backend="jev")          # a Jev-made decision: its outcome is still a label
    led.outcome(env, "yes", source="env")
    human = led.decision(point="p")
    led.outcome(human, "no", source="human")
    open_llm = led.decision(point="p")
    led.escalation(open_llm, "yes", None, by="qwen", source="open_llm")
    frontier = led.decision(point="p")
    led.escalation(frontier, "yes", None, by="claude", source="frontier_llm")
    jev = led.decision(point="p")
    led.escalation(jev, "yes", None, by="jev", source="jev")
    unknown = led.decision(point="p")
    led.outcome(unknown, None, source="env", reward=0.0)
    targets = {row.decision["id"]: (t, w) for row, t, w in led.training_rows("p")}
    assert targets[env] == ({"yes": 1.0}, 1.0)
    assert targets[human] == ({"no": 1.0}, 1.0)
    assert not {open_llm, frontier, jev, unknown} & set(targets)       # no model's answer is a training target
    with pytest.raises(ValueError):
        led.outcome(env, "yes", source="guess")


def test_an_outcome_with_several_right_labels_is_a_uniform_target_and_a_bad_list_is_refused(tmp_path):
    led = Ledger(tmp_path)
    several = led.decision(point="p")
    led.outcome(several, ["left", "up", "right"], source="env")
    mixed = led.decision(point="p")
    led.outcome(mixed, ["left", "up"], source="env")
    led.outcome(mixed, "up", source="human")
    targets = {row.decision["id"]: (t, w) for row, t, w in led.training_rows("p")}
    assert targets[several] == ({"left": 1 / 3, "up": 1 / 3, "right": 1 / 3}, 1.0)
    assert targets[mixed] == ({"left": 0.25, "up": 0.75}, 1.0)
    for bad in ([], ["up", "up"], ["up", 1], [None], (), ("up",), {"up"}, 1):     # a list only, not another iterable
        with pytest.raises(ValueError):
            led.outcome(several, bad, source="env")
    assert [e["label"] for e in led.events() if e["type"] == "outcome"] == [["left", "up", "right"], ["left", "up"],
                                                                            "up"]


def test_redaction_and_session_store(tmp_path):
    assert "sk-" not in redact("key sk-abcdefghijklmnopqrstuvwx here")
    assert "[REDACTED]" in redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
    assert redact({"a": ["api_key=hunter2"]}) == {"a": ["[REDACTED]"]}
    leaks = ["ZHIPUAI_API_KEY=0123456789abcdef0123456789abcdef.AbCdEfGh12345678", 'config {"password": "hunter2"}',
             "GITHUB_TOKEN=ghp_" + "a" * 36, "export AWS_SECRET_ACCESS_KEY=abc/def", "xoxb-1234567890-abcdefghij",
             "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
             "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"]
    for text in leaks:
        out = redact(text)
        assert "[REDACTED]" in out and not any(s in out for s in ("0123456789abcdef", "hunter2", "ghp_a", "abc/def",
                                                                  "xoxb-", "eyJzdWIi", "MIIEow")), (text, out)
    assert redact("tokens: 5 per step, max_tokens=4096") == "tokens: 5 per step, max_tokens=4096"
    store = SessionStore(tmp_path)
    ref = store.put("s1", {"cmd": "ls"})
    assert ref == "s1#0" and store.put("s1", {"cmd": "pwd"}) == "s1#1"
    reopened = SessionStore(tmp_path)
    assert reopened.get("s1#1") == {"cmd": "pwd"}
    assert reopened.put("s1", {"cmd": "cd"}) == "s1#2"


# ---------------------------------------------------------------- the order a question is shown in
def test_candidate_order_is_shuffled_at_presentation(tmp_path):
    import random

    from reharness.train.types import Question

    q = Question("choice", "which", {"a": "A", "b": "B", "c": "C", "d": "D (do nothing)"})
    orders = {tuple(q.presented(random.Random(s)).labels()) for s in range(30)}
    assert len(orders) > 1 and all(set(o) == {"a", "b", "c", "d"} for o in orders)
    assert q.presented(random.Random(7)).criteria == q.presented(random.Random(7)).criteria   # reproducible
    assert q.presented(random.Random(3)).criteria["d"] == "D (do nothing)"                    # content preserved
    for fixed in (Question("noul", "q"), Question("score", "q", ["low", "mid", "high"])):
        assert fixed.presented(random.Random(0)).criteria == fixed.criteria     # ordinal and fixed stay put


# ---------------------------------------------------------------- thresholds counted per session (formerly test_act)
def test_rows_of_one_session_are_not_independent_evidence():
    rng = random.Random(0)
    conf = [rng.uniform(0.6, 1.0) for _ in range(100)]
    correct = [rng.random() < 0.99 for _ in range(100)]
    independent = fit_threshold(conf, correct, [f"s{i}" for i in range(100)])
    five = fit_threshold(conf, correct, [f"s{i % 5}" for i in range(100)])
    assert independent is not None and (five is None or five >= independent)
    assert five is None


def test_59_sessions_without_a_harm_are_needed_and_one_wrong_act_harms_its_session():
    def fit(n, rows=1, later_right=True):
        # each session acts first on a right answer, then on its later rows
        conf, correct, sessions = [], [], []
        for i in range(n):
            for j in range(rows):
                conf.append(0.9)
                correct.append(j == 0 or later_right)
                sessions.append(f"s{i}")
        return fit_threshold(conf, correct, sessions)

    assert fit(59) == 0.9 and fit(58) is None
    assert fit(59, rows=3) == 0.9 and fit(59, rows=3, later_right=False) is None


def test_llm_escalations_and_gate_verdicts_are_never_training_targets(tmp_path):
    led = Ledger(tmp_path)
    escalated = led.decision(point="p")
    led.escalation(escalated, "yes", None, by="qwen", source="open_llm")
    judged = led.decision(point="p")
    led.outcome(judged, "yes", source="gate")
    observed = led.decision(point="p")
    led.escalation(observed, "yes", None, by="qwen", source="open_llm")
    led.outcome(observed, "no", source="env")
    assert [(r.decision["id"], t, w) for r, t, w in led.training_rows()] == [(observed, {"no": 1.0}, 1.0)]


def test_rows_written_by_m6_still_read(tmp_path):
    """The fields the M6 code wrote, in its order, for the stop, tool_gate and retry points: the targets the ledger
    reads from them (the v2 data pipeline's projector reads old ledgers this way)."""
    led, sessions = Ledger(tmp_path / "ledger"), SessionStore(tmp_path / "sessions")
    question = Noul("stop", "The task is complete").question({}).to_json()
    for i in range(60):
        s = f"s_{i:03d}"
        led.append({"type": "session", "session": s, "task": "t", "held_out": False, "agent": "glm/max_turns=4"})
        for phase, default in (("per_turn", "no"), ("on_finish", "yes")):
            c = {"task": "t", "turns": 1, "files": "", "actions": "", "message": "say yes 0.9"}
            did = led.decision(session=s, ctx=sessions.put(s, c), point="stop", spec_hash="fe55fe1febdf",
                               backend="none", model="", view="v", view_chars=1, truncated=False, question=question,
                               candidates=["yes", "no"], probs=None, route="shadow", explored=False, propensity=1.0,
                               action=None, usage={}, phase=phase, default=default)
            led.outcome(did, "yes" if i % 2 else "no", source="env", note="probe", score=1.0)
        g = {"task": "t", "workdir": "/w", "tool": "bash", "detail": "command: say no 0.7"}
        did = led.decision(session=s, ctx=sessions.put(s, g), point="tool_gate", spec_hash="b30fa9a7b0e1",
                           backend="mock", model="mock-keyword", view="v", view_chars=1, truncated=False,
                           question={"type": "noul", "instructions": "q"}, candidates=["yes", "no"], probs=[0.5, 0.5],
                           route="shadow", explored=False, propensity=1.0, action=None, usage={}, phase=None,
                           default=None)
        led.outcome(did, "no", source="human", note="approval")
        r = {"command": "make", "stderr": "say no 0.6", "exit_code": 1}
        route, action = ("auto", "no") if i % 2 else ("fallback", None)      # M6 routes of an asked point
        did = led.decision(session=s, ctx=sessions.put(s, r), point="retry", spec_hash="531169554799",
                           backend="mock", model="mock-keyword", view="v", view_chars=1, truncated=False,
                           question={"type": "noul", "instructions": "q"}, candidates=["yes", "no"], probs=[0.4, 0.6],
                           route=route, explored=False, propensity=0.95, action=action, usage={}, phase=None,
                           default=None)
        led.escalation(did, "yes", {"yes": 1.0, "no": 0.0}, "llm:qwen", "open_llm")   # M6: never a target
        if i % 2:
            led.outcome(did, "no", note="rerun by the agent")
    assert [len(led.training_rows(p)) for p in ("stop", "tool_gate", "retry")] == [120, 60, 30]
    held, _ = led.sessions()
    assert len(held) == 60 and not any(info["held_out"] for info in held.values())
    assert {"stop", "tool_gate", "retry"} == set().union(*(info["labelled"] for info in held.values()))
