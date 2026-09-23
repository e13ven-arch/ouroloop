import random

import pytest

from ouroloop.decision import Choice, Noul, Spec
from ouroloop.ledger import Ledger, SessionStore, redact
from ouroloop.policy import Policy, cp_upper, fit_threshold, route
from ouroloop.research.ask import NONE_FIT, QuestionError, fit_state, normalize
from ouroloop.types import Question


# ---------------------------------------------------------------- policy
def test_route_modes_and_propensity():
    rng = random.Random(0)
    assert route(Policy(mode="shadow"), "yes", 0.99, rng).route == "shadow"
    assert route(Policy(mode="auto", threshold=None), "yes", 0.99, rng).route == "fallback"
    r = route(Policy(mode="auto", threshold=0.8, explore=0.0), "yes", 0.9, rng)
    assert (r.route, r.propensity) == ("auto", 1.0)
    r = route(Policy(mode="auto", threshold=0.8, explore=1.0), "yes", 0.9, rng)
    assert (r.route, r.explored, r.propensity) == ("fallback", True, 1.0)
    assert route(Policy(mode="auto", threshold=0.8), "yes", 0.7, rng).route == "fallback"
    gate = Policy(mode="gate", threshold=0.8, explore=0.0, conservative="no")
    assert route(gate, "yes", 0.99, rng).route == "fallback"
    assert route(gate, "no", 0.99, rng).route == "auto"


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
    assert targets[open_llm] == ({"yes": 1.0}, 0.5)
    assert frontier not in targets and jev not in targets and unknown not in targets
    with pytest.raises(ValueError):
        led.outcome(env, "yes", source="guess")


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


# ---------------------------------------------------------------- question conversion
def test_normalize_questions():
    q = normalize({"type": "choice", "instructions": "pick", "criteria": {"a": "first", "b": "second"}})
    assert q.labels() == ["a", "b", NONE_FIT]
    assert normalize(Question("choice", "pick", {"a": "x", "b": "y"}), escape=False).labels() == ["a", "b"]
    bad = [
        {"type": "choice", "instructions": "pick", "criteria": {"A": "x", "a": "y"}},
        {"type": "choice", "instructions": "pick", "criteria": {"a": "same", "b": "same"}},
        {"type": "choice", "instructions": "pick", "criteria": {"a": "only"}},
        {"type": "score", "instructions": "rate", "criteria": ["one"]},
        {"type": "score", "instructions": "rate", "criteria": [str(i) for i in range(11)]},
        {"type": "noul", "instructions": "yes?", "criteria": {"maybe": "x"}},
        {"type": "vote", "instructions": "x"},
        {"type": "noul", "instructions": " "},
    ]
    for b in bad:
        with pytest.raises(QuestionError):
            normalize(b)
    assert fit_state("abcdef", 3) == ("abc", True) and fit_state("ab", 3) == ("ab", False)


def test_candidate_order_is_shuffled_at_presentation(tmp_path):
    import random

    from ouroloop.types import Question

    q = Question("choice", "which", {"a": "A", "b": "B", "c": "C", "d": "D (do nothing)"})
    orders = {tuple(q.presented(random.Random(s)).labels()) for s in range(30)}
    assert len(orders) > 1 and all(set(o) == {"a", "b", "c", "d"} for o in orders)
    assert q.presented(random.Random(7)).criteria == q.presented(random.Random(7)).criteria   # reproducible
    assert q.presented(random.Random(3)).criteria["d"] == "D (do nothing)"                    # content preserved
    for fixed in (Question("noul", "q"), Question("score", "q", ["low", "mid", "high"])):
        assert fixed.presented(random.Random(0)).criteria == fixed.criteria     # ordinal and fixed stay put


def test_the_ledger_records_the_order_the_model_was_shown(tmp_path):
    from ouroloop.decision import Choice
    from ouroloop.runtime import Runtime
    from ouroloop.types import Answer

    class Echo:
        """Answers with a fixed preference by label, so probabilities must follow the labels, not the order."""
        name = "echo"

        def decide(self, requests):
            return [Answer({lab: (0.7 if lab == "keep" else 0.3 / (len(r.question.labels()) - 1))
                            for lab in r.question.labels()}, "echo-1") for r in requests]

    point = Choice("pick", "what next", {"keep": "keep going", "stop": "stop", "ask": "ask a person"},
                   view=lambda ctx, params: str(ctx))
    rt = Runtime(tmp_path, {"echo": Echo()}, "echo", seed=0)
    rt.register(point)
    for i in range(12):
        rt.ask("pick", f"context {i}")
    rows = [r.decision for r in rt.ledger.rows()]
    assert len({tuple(r["candidates"]) for r in rows}) > 1          # the order varies between decisions
    for r in rows:
        assert set(r["candidates"]) == {"keep", "stop", "ask"}
        shown = dict(zip(r["candidates"], r["probs"]))
        assert shown["keep"] == 0.7                                  # probabilities align with the recorded order
        assert r["question"]["criteria"]["keep"] == "keep going"
