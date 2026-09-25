"""M7 offline acceptance: a point acts only by the execution rule, and takes its declared default on errors and
doubts (PLAN_v0.2 §0, M7)."""
import io
import json
import random
import re

import httpx
import pytest

from conftest import GATE, accept
from ouroloop import config, policy
from ouroloop.adapters.claude_code import ClaudeCodeAdapter
from ouroloop.agent import Agent, AgentConfig, Tools
from ouroloop.backends import KeywordBackend, LlmBackend
from ouroloop.backends.http import HttpBackend
from ouroloop.chat import ChatTurn, ScriptedChat, ToolCall
from ouroloop.cli import ledger_stats, main
from ouroloop.demo import seed_ledger
from ouroloop.evolve.build import build
from ouroloop.evolve.calibrate import calibrate_point
from ouroloop.evolve.pipeline import ModelCandidate, promote_model
from ouroloop.evolve.registry import Registry
from ouroloop.evolve.replay import labelled, predict
from ouroloop.evolve.train import TrainResult
from ouroloop.ledger import Ledger
from ouroloop.points import builtin_points, retry_point, stop_point, tool_gate_point
from ouroloop.policy import Policy, fit_threshold
from ouroloop.providers import MockProvider
from ouroloop.research import BasicResearcher, run_round
from ouroloop.runtime import Runtime
from ouroloop.stop import StopGuard
from ouroloop.types import Answer, Question, temper


class Model:
    """Says p(yes) to every question, and reports itself as `model`."""
    name = "model"

    def __init__(self, p_yes: float = 0.99, model: str = "A"):
        self.p_yes, self.model = p_yes, model

    def decide(self, requests):
        return [Answer({"yes": self.p_yes, "no": 1 - self.p_yes}, self.model) for _ in requests]


class Reads:
    """Says what the view tells it to: "say <label> <p>"."""
    name = "reads"

    def decide(self, requests):
        out = []
        for r in requests:
            label, p = re.search(r"say (\w+) ([\d.]+)", r.state).groups()
            out.append(Answer({label: float(p), "no" if label == "yes" else "yes": 1 - float(p)}, "R"))
        return out


class Broken:
    name = "broken"

    def decide(self, requests):
        raise TimeoutError("serve did not answer")


def ctx(say: str = "done") -> dict:
    return {"task": "t", "turns": 1, "files": "", "actions": "", "message": say}


def stop_runtime(root, backend, act: bool = True, **kw) -> Runtime:
    rt = Runtime(root, {"m": backend}, "m", seed=0)
    rt.register(stop_point(policy=Policy(act=act, explore=0.0), **kw))
    return rt


def always_yes(_ctx) -> str:
    return "yes"


# ---------------------------------------------------------------- the act switch and thresholds
def test_a_threshold_lets_only_the_model_it_was_fitted_for_act(tmp_path, gated):
    cfg = {"workspace": {"root": "ws"},
           "backends": {"a": {"type": "test_act:Model", "model": "A"}, "b": {"type": "test_act:Model", "model": "B"}},
           "points": {"default_backend": "a", "stop": {"act": True, "explore": 0.0}}}
    rt = config.build_runtime(cfg, tmp_path)
    gated(rt, "stop", "A", 0.9)
    d = rt.ask("stop", ctx(), phase="per_turn")
    assert (d.route, d.action, d.acted, d.model) == ("act", "yes", True, "A")
    rt.point_backends["stop"] = "b"
    d = rt.ask("stop", ctx(), phase="per_turn")
    assert (d.route, d.action, d.acted, d.model) == ("default", "no", False, "B")


def test_a_new_view_version_voids_the_old_threshold(tmp_path, gated):
    rt = stop_runtime(tmp_path, Model(), view_version="1")
    gated(rt, "stop", "A", 0.9)
    assert rt.ask("stop", ctx(), phase="per_turn").acted
    rt.register(stop_point(policy=Policy(act=True, explore=0.0), view_version="2"))
    d = rt.ask("stop", ctx(), phase="per_turn")
    assert (d.route, d.action) == ("default", "no")
    rt.register(stop_point(policy=Policy(act=True, explore=0.0), view_version="1"))
    assert rt.ask("stop", ctx(), phase="per_turn").acted
    rt.register(stop_point(policy=Policy(act=True, explore=0.0), view_version="1", max_view_chars=500))
    assert not rt.ask("stop", ctx(), phase="per_turn").acted        # the view is cut shorter


def test_a_rollback_brings_back_the_previous_champions_temperature_and_threshold(tmp_path, gated):
    cfg = {"workspace": {"root": "ws"}, "registry": {"base": "base"},
           "backends": {"base": {"type": "test_act:Model", "model": "base"}},
           "points": {"default_backend": "base", "stop": {"backend": "champion", "act": True, "explore": 0.0}}}
    rt = config.build_runtime(cfg, tmp_path)
    reg = Registry(rt.root)
    for version, model, parent in (("m_0001", "A", None), ("m_0002", "B", "m_0001")):
        accept(rt.root, {"version": version, "parent": parent, "points": ["stop"], "model_id": model,
                         "backend": {"type": "test_act:Model", "model": model}})
        reg.promote(version, parent)
    gated(rt, "stop", "A", {"per_turn": 0.6}, temperature=2.0)
    gated(rt, "stop", "B", {"per_turn": None}, temperature=0.5)

    def ask(session):
        d = config.build_runtime(cfg, tmp_path).ask("stop", ctx(), session=session, phase="per_turn")
        return d.model, d.acted, round(d.confidence, 6)

    assert ask("s1") == ("B", False, round(temper({"yes": 0.99, "no": 0.01}, 0.5)["yes"], 6))
    assert reg.rollback() == "m_0001"
    assert ask("s2") == ("A", True, round(temper({"yes": 0.99, "no": 0.01}, 2.0)["yes"], 6))   # a new session


def test_promotion_checks_the_model_id_the_backend_reports(tmp_path):
    rt = stop_runtime(tmp_path, Model())
    reg = Registry(rt.root)
    accept(rt.root, {"version": "m_0001"})
    cand = ModelCandidate("m_0001", {}, None, result=TrainResult("run", {}, "A@renamed"), backend=Model(model="A"))
    with pytest.raises(ValueError, match="reports model 'A'"):
        promote_model(rt, cand, reg, GATE)
    assert reg.champion() is None
    cand.result.model_id = "A"
    promote_model(rt, cand, reg, GATE)
    assert reg.champion()["version"] == "m_0001"


def test_a_changed_spec_replays_at_the_same_models_temperature(tmp_path, gated):
    rt = stop_runtime(tmp_path, Model(0.9), act=False)
    gated(rt, "stop", "A", None, temperature=0.5)
    for i in range(3):
        rt.outcome(rt.record("stop", ctx(), session=f"s{i}", phase="per_turn").id, "yes")
    p, rows = rt.points["stop"], labelled(rt, "stop")
    _, champion = predict(rt, p, p.spec, rows)
    _, challenger = predict(rt, p, p.spec.apply({"instructions": "Is the task done?"}), rows)
    assert champion == challenger and champion[0]["yes"] == temper({"yes": 0.9, "no": 0.1}, 0.5)["yes"]


def test_old_format_ungated_and_tied_answers_never_act(tmp_path, gated):
    path = tmp_path / "harness" / "calibration.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"stop": {"temperature": 1.0, "threshold": 0.5}}))    # per point, before M7
    rt = stop_runtime(tmp_path, Model())
    assert rt.ask("stop", ctx(), phase="per_turn").route == "default"
    rt.set_calibration("stop", "A", 1.0, {"per_turn": 0.5})                            # fitted, but never gated
    assert rt.ask("stop", ctx(), phase="per_turn").route == "default"
    stored = json.loads(path.read_text())
    assert stored["version"] == 2 and "stop" not in stored                            # discarded, not migrated
    tied = stop_runtime(tmp_path / "tie", Model(0.5))
    gated(tied, "stop", "A", 0.5)
    d = tied.ask("stop", ctx(), phase="per_turn")
    assert (d.route, d.action, d.label) == ("default", "no", None)


def test_a_malformed_or_anonymous_calibration_never_acts(tmp_path, gated):
    cases = {"NaN threshold": ("A", float("nan"), 1.0, 0.51),
             "negative temperature": ("A", 0.9, -0.1, 0.2),     # it would turn p(yes) = 0.2 into a confident yes
             "no model id": ("", 0.5, 1.0, 0.99)}               # it names no model, so it is fitted for none
    for i, (case, (model, threshold, temperature, p_yes)) in enumerate(cases.items()):
        rt = stop_runtime(tmp_path / str(i), Model(p_yes, model))
        gated(rt, "stop", model, threshold, temperature=temperature)
        d = rt.ask("stop", ctx(), phase="per_turn")
        assert (d.action, d.acted) == ("no", False), case


# ---------------------------------------------------------------- thresholds counted per session
def test_rows_of_one_session_are_not_independent_evidence():
    rng = random.Random(0)
    conf = [rng.uniform(0.6, 1.0) for _ in range(100)]
    correct = [rng.random() < 0.99 for _ in range(100)]
    independent = fit_threshold(conf, correct, [f"s{i}" for i in range(100)])
    five = fit_threshold(conf, correct, [f"s{i % 5}" for i in range(100)])
    assert independent is not None and (five is None or five >= independent)
    assert five is None


def test_59_sessions_without_an_error_are_needed_and_only_a_sessions_first_act_counts():
    def fit(n, rows=1):
        # each session acts first on a right answer; its later rows, all wrong, never act
        conf, correct, sessions = [], [], []
        for i in range(n):
            for j in range(rows):
                conf.append(0.9)
                correct.append(j == 0)
                sessions.append(f"s{i}")
        return fit_threshold(conf, correct, sessions)

    assert fit(59) == 0.9 and fit(58) is None
    assert fit(59, rows=3) == 0.9


def test_a_phase_threshold_is_fitted_on_the_decisions_that_would_act(tmp_path):
    rt = stop_runtime(tmp_path, Reads(), act=False)
    conf, correct = [], []
    for i in range(1000):
        if i < 950:                                   # 95%: the default "no", right
            say, label = "say no 0.9", "no"
        else:                                         # the "yes" answers, 30% of them wrong
            say, label = "say yes 0.99", "no" if i % 10 < 3 else "yes"
        d = rt.record("stop", ctx(say), session=f"s{i:04d}", phase="per_turn")
        rt.outcome(d.id, label)
        conf.append(float(say.split()[-1]))
        correct.append(say.split()[1] == label)
    cal = calibrate_point(rt, "stop", test_frac=0.0, cal_frac=1.0)
    assert cal.model == "R" and cal.thresholds == {"per_turn": None, "on_finish": None}
    assert fit_threshold(conf, correct) == 0.9        # pooled over every row, the right "no"s would buy one


def test_calibration_counts_a_sessions_rows_once(tmp_path):
    rt = stop_runtime(tmp_path, Reads(), act=False)
    for i in range(120):                              # 120 right "yes" answers, but from 30 sessions
        rt.outcome(rt.record("stop", ctx("say yes 0.99"), session=f"s{i % 30:02d}", phase="per_turn").id, "yes")
    assert calibrate_point(rt, "stop", test_frac=0.0, cal_frac=1.0).thresholds["per_turn"] is None
    assert fit_threshold([0.99] * 120, [True] * 120) == 0.99      # counted by row, they would buy one


# ---------------------------------------------------------------- errors take the default
def test_errors_and_abstentions_take_the_default_and_leave_one_row_each(tmp_path, gated):
    empty = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"answers": [{"probs": {}, "model": "A"}]})))
    cases = {"llm parse failure": (LlmBackend(MockProvider(lambda s, p: "hard to say")), ctx(), "default",
                                   "abstained"),
             "backend raises": (Broken(), ctx(), "error", "error"),
             "http empty probabilities": (HttpBackend("http://serve", client=empty), ctx(), "default", "abstained"),
             "render raises": (Model(), {}, "error", "error")}
    for i, (case, (backend, c, route, field)) in enumerate(cases.items()):
        rt = stop_runtime(tmp_path / str(i), backend)
        for model in ("A", "mock:scripted"):
            gated(rt, "stop", model, 0.0)              # any confidence would do: only a decisive answer may act
        d = rt.ask("stop", c, phase="per_turn")         # a failing-open "yes" would end the session here
        (row,) = rt.ledger.rows()
        assert (d.route, d.action, d.acted) == (route, "no", False), case
        assert row.decision["route"] == route and row.decision[field] and row.decision["action"] == "no", case
        assert rt.sessions.get(row.decision["ctx"]) == c, case           # the raw state is kept
    rt = Runtime(tmp_path / "judge", {"j": LlmBackend(MockProvider(lambda s, p: "hard to say"))}, "j")
    d = rt.ask_question("research.worth_trying", Question("noul", "Worth trying?"), "state", default="yes")
    (row,) = rt.ledger.rows()
    assert (d.label, d.action, row.decision["route"], row.decision.get("abstained")) == (None, "yes", "shadow", True)


class Raw:
    """Answers with these probabilities as they are, the way a custom backend may."""
    name = "raw"

    def __init__(self, probs: dict):
        self.probs = probs

    def decide(self, requests):
        return [Answer(dict(self.probs), "A") for _ in requests]


def test_malformed_answers_never_act(tmp_path, gated):
    def served(body: str):
        return HttpBackend("http://serve", client=httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"}))))

    cases = {"NaN from serve": served('{"answers": [{"probs": {"yes": 0.3, "no": NaN}, "model": "A"}]}'),
             "-Infinity from serve": served('{"answers": [{"probs": {"yes": 0.3, "no": -Infinity}, "model": "A"}]}'),
             "negative from serve": served('{"answers": [{"probs": {"yes": 0.3, "no": -0.5}, "model": "A"}]}'),
             "an unknown label": Raw({"yes": 0.05, "no": 0.05, "maybe": 0.9}),
             "a raw NaN": Raw({"yes": 0.9, "no": float("nan")}),
             "raw scores": Raw({"yes": 2.0, "no": 0.7}),
             "a raw negative": Raw({"yes": 1.5, "no": -0.5})}
    for i, (case, backend) in enumerate(cases.items()):
        rt = stop_runtime(tmp_path / str(i), backend)
        gated(rt, "stop", "A", 0.5)
        d = rt.ask("stop", ctx(), phase="per_turn")     # acting on "yes" would end the session
        assert (d.action, d.acted) == ("no", False), case
        assert d.route in ("default", "error"), case


# ---------------------------------------------------------------- configuration and label sources
@pytest.mark.parametrize("bad", [{"points": {"stop": {"risk": 0.2}}}, {"points": {"stop": {"default": "no"}}},
                                 {"points": {"tool_gate": {"act": True}}}, {"fallback": "mock"},
                                 {"points": {"stop": {"act": "false"}}}])        # a string would switch stop on
def test_config_that_loosens_the_rules_is_a_startup_error(tmp_path, bad):
    cfg = {"backends": {"mock": {"type": "mock"}}, **bad,
           "points": {"default_backend": "mock", **bad.get("points", {})}}
    with pytest.raises(ValueError):
        config.build_runtime(cfg, tmp_path)


def acting_retry():
    return retry_point(policy=Policy(act=True))


def test_only_the_config_switches_a_point_on(tmp_path):
    cfg = {"backends": {"mock": {"type": "mock"}},
           "points": {"default_backend": "mock", "stop": {"act": True}, "retry": {"define": "test_act:acting_retry"}}}
    rt = config.build_runtime(cfg, tmp_path)
    assert rt.points["stop"].policy.act and not rt.points["retry"].policy.act


def test_a_declared_default_answerer_answers_when_the_model_does_not_act(tmp_path):
    reply = {"text": '{"label": "no"}'}
    llm = LlmBackend(MockProvider(lambda s, p: reply["text"], source="open_llm"))
    rt = Runtime(tmp_path, {"m": Model(), "frontier": llm}, "m", seed=0)
    rt.register(stop_point(policy=Policy(act=True, explore=0.0),
                           phases={"per_turn": {"default": "rule:test_act:always_yes", "label": "no"},
                                   "on_finish": {"default": "llm:frontier", "label": "yes"}}))
    d = rt.ask("stop", ctx(), phase="on_finish")          # no gated threshold: the model does not act
    assert (d.route, d.label, d.action) == ("default", "yes", "no")
    reply["text"] = "hard to say"
    assert rt.ask("stop", ctx(), phase="on_finish").action == "yes"       # unparsable: the fixed label
    assert rt.ask("stop", ctx(), phase="per_turn").action == "yes"        # a rule may answer too
    rows = [r.decision for r in rt.ledger.rows()]
    assert [(r["route"], r["answerer"], "answerer_error" in r) for r in rows] == [
        ("default", "llm:frontier", False), ("default", "llm:frontier", True), ("default", "rule:test_act:always_yes",
                                                                               False)]
    assert rt.ledger.training_rows() == [] and all(not r.escalations for r in rt.ledger.rows())
    rt.outcome(rows[0]["id"], "yes", source="env")          # the environment's label is the target, not the LLM's
    assert [(r.decision["id"], t) for r, t, _ in rt.ledger.training_rows()] == [(rows[0]["id"], {"yes": 1.0})]
    with pytest.raises(ValueError, match="no backend"):
        rt.register(stop_point(phases={"per_turn": {"default": "llm:nowhere", "label": "no"}}))


def test_a_shadow_stop_takes_its_answerers_default_without_asking_the_model(tmp_path):
    rt = Runtime(tmp_path, {"m": Broken()}, "m", seed=0)
    rt.register(stop_point(phases={"per_turn": {"default": "rule:test_act:always_yes", "label": "no"},
                                   "on_finish": "yes"}))
    assert StopGuard(rt, "s")("per_turn", ctx(), 1).stop            # act is off: StopGuard only records
    (row,) = [r.decision for r in rt.ledger.rows()]
    assert (row["route"], row["action"], row["answerer"], row["backend"]) == ("shadow", "yes",
                                                                             "rule:test_act:always_yes", "none")


def test_calibrate_reports_and_writes_nothing(tmp_path, capsys):
    (tmp_path / "ouroloop.toml").write_text('[workspace]\nroot = "ws"\n\n[backends.mock]\ntype = "mock"\n\n'
                                            '[points]\ndefault_backend = "mock"\n\n[points.retry]\n')
    seed_ledger(config.build_runtime(config.load(tmp_path / "ouroloop.toml"), tmp_path), 120)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert main(["calibrate", "--config", str(tmp_path / "ouroloop.toml")]) == 0
    assert "retry: model=mock-keyword" in capsys.readouterr().out
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


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


class Naysayer:
    """A judge that says "no" to every candidate and picks the last option of every choice."""
    name = "naysayer"

    def decide(self, requests):
        out = []
        for r in requests:
            labels = r.question.labels()
            pick = "no" if r.question.type == "noul" else labels[-1]
            out.append(Answer({lab: 0.97 if lab == pick else 0.03 / (len(labels) - 1) for lab in labels}, "nay"))
        return out


def test_research_points_are_shadow_and_take_their_defaults(tmp_path, frozen):
    rt = Runtime(tmp_path, {"mock": KeywordBackend()}, "mock", seed=0)
    rt.register(retry_point())
    seed_ledger(rt, 40)
    frozen(tmp_path)
    llm = MockProvider(lambda s, p: json.dumps({"candidates": [
        {"kind": "spec", "change": {"instructions": "Will a rerun succeed?"}, "hypothesis": "judged no"}]}))
    res = run_round(rt, BasicResearcher(consult=False), llm, Naysayer())
    assert [e["hypothesis"] for e in res.evaluated] == ["judged no"]            # tried all the same
    rows = {r.decision["point"]: r.decision for r in rt.ledger.rows()}
    worth, pick = rows["research.worth_trying"], rows["research.pick_target"]
    assert (worth["route"], worth["action"], worth["probs"][worth["candidates"].index("no")]) == ("shadow", "yes", 0.97)
    largest = max(res.report.clusters, key=lambda c: c["n"])["id"]
    assert (pick["route"], pick["action"]) == ("shadow", largest) and pick["probs"][-1] == 0.97


# ---------------------------------------------------------------- rules
def test_the_deny_list_runs_before_every_tool_call_and_leaves_a_rule_row(tmp_path):
    rt = Runtime(tmp_path / "ws", {"mock": KeywordBackend()}, "mock", seed=0)
    for p in builtin_points():
        rt.register(p)
    work = tmp_path / "work"
    work.mkdir()
    chat = ScriptedChat([ChatTurn("", [ToolCall("b", "bash", {"command": "touch ran; echo reboot"})], "tool_use"),
                         ChatTurn("ok", [], "end")])
    Agent(chat, rt, Tools(work), config=AgentConfig(gated_tools=())).run("t")
    assert not (work / "ran").exists()
    (gate,) = [r.decision for r in rt.ledger.rows() if r.decision["point"] == "tool_gate"]
    assert (gate["route"], gate["action"]) == ("rule", "no")


def test_the_adapter_never_allows_whatever_the_route(tmp_path, gated, monkeypatch):
    monkeypatch.setattr(policy, "ACTING", {"stop", "tool_gate"})     # beyond v0.2: tool_gate acting, to see "act"
    outs, routes = set(), set()
    for i, (backend, live) in enumerate([(Model(0.99), True), (Model(0.01), True), (Broken(), True),
                                         (Model(0.99), False)]):
        rt = Runtime(tmp_path / str(i), {"m": backend}, "m", seed=0)
        rt.register(tool_gate_point(policy=Policy(act=True, explore=0.0)))
        gated(rt, "tool_gate", "A", 0.5)
        cc = ClaudeCodeAdapter(rt, live=live, enforce_deny_list=True)
        for j, command in enumerate(("ls", "touch x; echo reboot")):
            out = cc.handle({"hook_event_name": "PreToolUse", "session_id": "s", "cwd": "/w", "tool_name": "Bash",
                             "tool_use_id": f"t{j}", "tool_input": {"command": command}})
            outs.add((command, out and out["hookSpecificOutput"]["permissionDecision"]))
        routes |= {r.decision["route"] for r in rt.ledger.rows()}
    assert routes == {"act", "default", "error", "shadow", "rule"}
    assert outs == {("ls", None), ("touch x; echo reboot", "deny")}


def test_the_hook_denies_even_when_the_runtime_cannot_start_or_record(tmp_path, monkeypatch, capsys):
    ok = '[workspace]\nroot = "ws"\n\n[backends.mock]\ntype = "mock"\n'
    configs = {"pre-M7 fallback": 'fallback = "mock"\n' + ok, "unwritable ledger": ok}
    for i, (case, text) in enumerate(configs.items()):
        path = tmp_path / f"{i}.toml"
        path.write_text(text)
        if case == "unwritable ledger":
            monkeypatch.setattr(Ledger, "append", lambda self, event: (_ for _ in ()).throw(OSError("disk full")))
        for command, expected in (("rm -rf /", "deny"), ("ls", None)):
            monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
                "hook_event_name": "PreToolUse", "session_id": "s", "cwd": "/w", "tool_name": "Bash",
                "tool_use_id": "t", "tool_input": {"command": command}})))
            assert main(["hook", "claude-code", "--config", str(path), "--enforce-deny-list"]) == 0
            out = capsys.readouterr().out
            assert (json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None) == expected, case


# ---------------------------------------------------------------- M6 rows still read
def test_rows_written_by_m6_still_read(tmp_path):
    rt = Runtime(tmp_path, {"r": Reads()}, "r", seed=0)
    for p in builtin_points():
        rt.register(p)
    stop = rt.points["stop"]
    for i in range(60):
        s = f"s_{i:03d}"
        rt.ledger.append({"type": "session", "session": s, "task": "t", "held_out": False, "agent": "glm/max_turns=4"})
        for phase, default, moment in (("per_turn", "no", "after a turn"), ("on_finish", "yes", "the agent wants "
                                                                                             "to finish")):
            c = {**ctx("say yes 0.9" if i % 2 else "say no 0.8"), "moment": moment}
            view, _ = stop.render(c)
            did = rt.ledger.decision(         # the fields the M6 code wrote, in its order
                session=s, ctx=rt.sessions.put(s, c), point="stop", spec_hash="fe55fe1febdf", backend="none",
                model="", view=view, view_chars=len(view), truncated=False, question=stop.question(c).to_json(),
                candidates=["yes", "no"], probs=None, route="shadow", explored=False, propensity=1.0, action=None,
                usage={}, phase=phase, default=default)
            rt.outcome(did, "yes" if i % 2 else "no", source="env", note="probe", score=1.0)
        g = {"task": "t", "workdir": "/w", "tool": "bash", "detail": "command: say no 0.7"}
        did = rt.ledger.decision(session=s, ctx=rt.sessions.put(s, g), point="tool_gate", spec_hash="b30fa9a7b0e1",
                                 backend="mock", model="mock-keyword", view="v", view_chars=1, truncated=False,
                                 question={"type": "noul", "instructions": "q"}, candidates=["yes", "no"],
                                 probs=[0.5, 0.5], route="shadow", explored=False, propensity=1.0, action=None,
                                 usage={}, phase=None, default=None)
        rt.outcome(did, "no", source="human", note="approval")
        r = {"command": "make", "stderr": "say no 0.6", "exit_code": 1}
        route, action = ("auto", "no") if i % 2 else ("fallback", None)      # M6 routes of an asked point
        did = rt.ledger.decision(session=s, ctx=rt.sessions.put(s, r), point="retry", spec_hash="531169554799",
                                 backend="mock", model="mock-keyword", view="v", view_chars=1, truncated=False,
                                 question={"type": "noul", "instructions": "q"}, candidates=["yes", "no"],
                                 probs=[0.4, 0.6], route=route, explored=False, propensity=0.95, action=action,
                                 usage={}, phase=None, default=None)
        rt.ledger.escalation(did, "yes", {"yes": 1.0, "no": 0.0}, "llm:qwen", "open_llm")   # M6: half a target
        if i % 2:
            rt.outcome(did, "no", note="rerun by the agent")
    assert [len(rt.ledger.training_rows(p)) for p in ("stop", "tool_gate", "retry")] == [120, 60, 30]
    ds = build(rt, tmp_path / "data", ["stop", "tool_gate", "retry"])
    assert sum(len(part) for part in ds.splits.values()) == 210
    stats = ledger_stats(rt.ledger.rows())
    assert [(stats[p]["labelled"], stats[p]["scored"]) for p in ("stop", "tool_gate", "retry")] == [
        (120, 0), (60, 60), (30, 30)]
    _, probs = predict(rt, stop, stop.spec, labelled(rt, "stop"))
    assert len(probs) == 120
    assert set(calibrate_point(rt, "retry", test_frac=0.1, cal_frac=0.9, min_items=10).thresholds) == {"after_failure"}
    cal = calibrate_point(rt, "stop", test_frac=0.1, cal_frac=0.5, min_items=10)
    assert set(cal.thresholds) == {"per_turn", "on_finish"} and cal.accuracy == 1.0
    cal = calibrate_point(rt, "tool_gate", test_frac=0.1, cal_frac=0.5, min_items=10)
    assert set(cal.thresholds) == {"before_call"}             # a row without a phase is in the point's only one
