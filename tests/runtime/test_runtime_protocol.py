"""The worker protocol (docs/design/RUNTIME.md §4, §8) over the host's ProcessWorker: every op, the callbacks and their
ids, what is the package's error and what is the protocol's, the line bounds, the runner's identity at load, and R's
work tree.

ProcessWorker runs the real runner and protocol in a subprocess but does not isolate it (`qualified = False`); these
tests show the host's semantics. Isolation is shown in test_runtime_worker.py, in the real image.
"""
from __future__ import annotations

import json
import sys

import pytest

from ouroloop.contracts import CandidateDefect, Infrastructure, LimitHit, StaleWorker, WorkerCrashed, WorkerUnavailable
from ouroloop.harness import contract as hc
from ouroloop.runtime.broker import Broker
from ouroloop.runtime.worker import (MAX_LINE, MAX_MODEL_LINE, PACKAGE_OPS, RUNNER, AgentRunError, PackageCallError,
                                     ProcessWorker, RunDefect, runner_digest)
from runtime_world import World, runner_module, session_with

AGENT = '''
def run(ctx, task):
    turn = ctx.model([{"role": "user", "content": task}], tools=[{"name": "api_search"}], role="act")
    call = turn.tool_calls[0]
    out = ctx.tool(call["name"], call["args"])
    return "final: " + turn.text + " / " + out.output + " / stop=" + str(turn.stop)
'''


class StubHost:
    """A host for the protocol: records every callback in order and answers model_call with a canned turn (or an
    error), base_call through the session's own handler over a World, skill_read, memory_recall and mark."""

    def __init__(self, session, turn=None, error=None, extra=None, world=None):
        self.seen, self.turn, self.error, self.extra = [], turn, error, extra or {}
        self.world = world or World()
        self._base = session.base_call_handler(Broker(self.world))

    def model_call(self, reply):
        self.seen.append(reply)
        if self.error:
            return {"op": "model_result", "id": reply["id"], "is_error": True, "error": self.error, **self.extra}
        turn = self.turn or {"text": "ok", "tool_calls": [{"id": "c1", "name": "api_search", "args": {"q": "x"}}],
                             "stop": "tool_use"}
        return {"op": "model_result", "id": reply["id"], **turn, **self.extra}

    def base_call(self, reply):
        self.seen.append(reply)
        return {**self._base(reply), **self.extra}

    def skill_read(self, reply):
        self.seen.append(reply)
        return {"op": "skill_result", "id": reply["id"], "text": "the skill" if reply["name"] == "known" else None}

    def memory_recall(self, reply):
        self.seen.append(reply)
        return {"op": "memory_result", "id": reply["id"],
                "entries": [{"id": "e1", "when": "2026-01-01", "content": "a note", "score": 0.5}]}

    def mark(self, reply):
        self.seen.append(reply)
        return {"op": "mark_result", "id": reply["id"]}

    @property
    def handlers(self):
        return {"model_call": self.model_call, "base_call": self.base_call, "skill_read": self.skill_read,
                "memory_recall": self.memory_recall, "mark": self.mark}


def run_message(entry="agent/main.py:run", task="do x", **extra):
    return {"op": "run", "entry": entry, "task": task, "roles": ["act"], "view": {"events": []}, "limits": {},
            "skills": {"index": []}, **extra}


# ---------------------------------------------------------------------------- the contract and the runner's identity
def test_the_embedded_contract_is_the_harness_contract_and_the_agents_ctx_has_its_names_only():
    """worker_runner.py imports nothing of the host, so it carries harness/contract.py as a JSON literal: the two are
    equal, and the ctx an agent gets in the worker has exactly the contract's names."""
    wr = runner_module()
    assert wr.CONTRACT == hc.as_json()
    assert wr.OPS == hc.OPS and wr.WORK_ACTIONS == hc.WORK_ACTIONS
    ctx = wr.AgentContext({"system": "s"}, {"act_calls": 1}, {})          # built without a pipe: nothing is asked
    assert {n for n in dir(ctx) if not n.startswith("_")} == set(hc.CTX_NAMES)


def test_load_answers_this_hosts_runner_and_its_ops_and_refuses_a_path_outside_the_package():
    session = ProcessWorker(for_tests=True).start()
    try:
        assert session.load({"agent/main.py": AGENT}) == {"runner_sha256": runner_digest(), "ops": list(hc.OPS)}
        with pytest.raises(PackageCallError, match="outside the package"):
            session.load({"../escape.py": "x = 1\n"})
    finally:
        session.close()


def test_a_worker_running_another_runner_is_stale_at_load_and_at_startup(tmp_path):
    """A worker whose runner is other bytes than this host's worker_runner.py (an image built before the runner
    changed) is refused at its first load with the rebuild command, is not healthy, and its runner_problem names it
    before anything is paid; this host's runner passes."""
    other = tmp_path / "worker_runner.py"
    other.write_text(RUNNER.read_text() + "\n# built from another runner\n")

    class Other(ProcessWorker):
        qualified = True                      # as a Docker image built from an older runner would say

        def command(self):
            return [sys.executable, "-I", str(other)]
    worker = Other(for_tests=True)
    session = worker.start()
    with pytest.raises(StaleWorker, match="rebuild the image: docker build") as info:
        session.load({})
    assert session.proc.poll() is not None and issubclass(StaleWorker, WorkerUnavailable)
    assert runner_digest()[:12] in str(info.value)
    assert worker.healthy() is False and "rebuild the image" in worker.runner_problem()
    fine = ProcessWorker(for_tests=True)
    assert fine.healthy() and fine.runner_problem() is None


# ---------------------------------------------------------------------------- run: the attempt-long op
def test_a_run_asks_the_host_for_every_callback_in_the_agents_order_with_ids_per_kind():
    """The worker asks the host for the model, the tool, skills, memory and marks over the one pipe, in the agent's
    order, each kind with its own monotonic ids; ctx.limits is the latest snapshot an answer carried and ctx.view is
    refreshed in place (a reference the agent keeps is current); the agent's return value is the result."""
    agent = '''
def run(ctx, task):
    view, before = ctx.view, dict(ctx.limits)
    first = ctx.model([{"role": "user", "content": task}], tools=[{"name": "api_search"}], role="act")
    call = first.tool_calls[0]
    out = ctx.tool(call["name"], call["args"])
    second = ctx.model([{"role": "user", "content": task}])
    known, missing = ctx.skills.read("known"), ctx.skills.read("missing")
    notes = ctx.memory.recall("contacts", k=2)
    ctx.mark("progress", step=1)
    return repr([before, ctx.limits, view is ctx.view, view["events"], out.output, known, missing,
                 ctx.skills.index, notes, second.stop])
'''
    session = session_with({"agent/main.py": agent})
    try:
        host = StubHost(session, extra={"limits": {"act_calls": 48}, "view": {"events": ["e1"]}})
        value = session.serve(run_message(limits={"act_calls": 50},
                                          skills={"index": [{"name": "known", "description": "d"}]}),
                              handlers=host.handlers, deadline=10.0)
        assert value == repr([{"act_calls": 50}, {"act_calls": 48}, True, ["e1"], "api_search ok", "the skill", None,
                              [{"name": "known", "description": "d"}],
                              [{"id": "e1", "when": "2026-01-01", "content": "a note", "score": 0.5}], "tool_use"])
        assert [(m["op"], m["id"]) for m in host.seen] == [
            ("model_call", "m1"), ("base_call", "b1"), ("model_call", "m2"), ("skill_read", "k1"),
            ("skill_read", "k2"), ("memory_recall", "r1"), ("mark", "n1")]
        first = host.seen[0]
        assert (first["role"], first["effort"], first["messages"], first["tools"]) == \
            ("act", None, [{"role": "user", "content": "do x"}], [{"name": "api_search"}])
        assert host.seen[2]["tools"] == [] and host.seen[5]["query"] == "contacts" and host.seen[5]["k"] == 2
        assert host.seen[6]["kind"] == "progress" and host.seen[6]["data"] == {"step": 1}
        assert host.world.calls == [("api_search", {"q": "x"})]
    finally:
        session.close()


def test_a_gateway_refusal_is_an_error_the_agent_may_catch():
    agent = ("def run(ctx, task):\n    try:\n        ctx.model([], role='aux')\n"
             "    except RuntimeError as e:\n        return 'refused: ' + str(e)\n    return 'unreachable'\n")
    session = session_with({"agent/main.py": agent})
    try:
        host = StubHost(session, error="role 'aux' is not declared")
        assert session.serve(run_message(), handlers=host.handlers, deadline=10.0) == \
            "refused: role 'aux' is not declared"
    finally:
        session.close()


def test_a_defect_marked_exception_in_run_is_run_defect_and_any_other_is_agent_run_error():
    """In a run, an exception the package marks as its own defect (`ouroloop_defect`: a hook outside its protocol, a
    package tool that raised) is RunDefect, a candidate defect; any other exception, a return value that is not a
    string and an agent entrypoint not named run are AgentRunError, infrastructure. The worker answers on after each."""
    files = {"agent/defect.py": "class HookError(Exception):\n    ouroloop_defect = True\n"
                                "def run(ctx, task):\n    raise HookError('hook g answered 7')\n",
             "agent/boom.py": "def run(ctx, task):\n    raise ValueError('boom')\n",
             "agent/none.py": "def run(ctx, task):\n    return None\n",
             "agent/main.py": "def main(ctx, task):\n    return 'x'\n"}
    assert issubclass(RunDefect, CandidateDefect) and issubclass(AgentRunError, Infrastructure)
    assert not issubclass(AgentRunError, CandidateDefect)
    session = session_with(files)
    try:
        handlers = StubHost(session).handlers
        with pytest.raises(RunDefect, match="hook g answered 7"):
            session.serve(run_message("agent/defect.py:run"), handlers=handlers, deadline=10.0)
        for entry, error, match in (("agent/boom.py:run", AgentRunError, "ValueError: boom"),
                                    ("agent/none.py:run", AgentRunError, "returned NoneType, not a string"),
                                    ("agent/main.py:main", AgentRunError, "must be a function named run")):
            with pytest.raises(error, match=match) as info:
                session.serve(run_message(entry), handlers=handlers, deadline=10.0)
            assert not isinstance(info.value, CandidateDefect)
        assert session.request({"op": "probe", "entries": []}) == "ok"
    finally:
        session.close()


def test_the_deadline_ends_a_hung_run_as_a_worker_crash_never_a_defect():
    """`run` is not a bounded op: a run the host's deadline ends is infrastructure (the limits are the host's to
    enforce), and the hung worker is gone."""
    session = session_with({"agent/main.py": "def run(ctx, task):\n    while True:\n        pass\n"})
    with pytest.raises(WorkerCrashed, match="did not answer run within 1.0s") as info:
        session.serve(run_message(), handlers=StubHost(session).handlers, deadline=1.0)
    assert not isinstance(info.value, CandidateDefect)
    assert session.proc.poll() is not None
    assert "run" not in PACKAGE_OPS and "smoke" in PACKAGE_OPS and "work" not in PACKAGE_OPS


@pytest.mark.parametrize("wrong", ["id", "op"])
def test_a_misordered_or_wrong_op_answer_is_a_protocol_error_not_the_packages(wrong):
    """An answer with another id, or of another op, is a protocol error that `except Exception` in the agent does not
    catch; the host sees the worker breaking the protocol (infrastructure for a run), never the package raising."""
    agent = ("def run(ctx, task):\n    try:\n        ctx.model([], role='act')\n"
             "    except Exception as e:\n        return 'swallowed ' + type(e).__name__\n    return 'x'\n")
    session = session_with({"agent/main.py": agent})
    host = StubHost(session)

    def misanswer(reply):
        answer = host.model_call(reply)
        return {**answer, "id": "m999"} if wrong == "id" else {**answer, "op": "base_result"}
    with pytest.raises(WorkerCrashed, match="out of order") as info:
        session.serve(run_message(), handlers={**host.handlers, "model_call": misanswer}, deadline=10.0)
    assert not isinstance(info.value, (CandidateDefect, AgentRunError))
    assert session.proc.poll() is not None


def test_a_callback_the_host_has_no_handler_for_is_a_protocol_failure():
    session = session_with({"agent/main.py": AGENT})
    with pytest.raises(WorkerCrashed, match="answered 'model_call'"):
        session.serve(run_message(), handlers={"base_call": session.base_call_handler()}, deadline=10.0)


def test_a_handler_at_a_hard_limit_ends_serve_with_limit_hit_and_a_failing_handler_is_raised_as_it_is():
    """A hard limit found by the host's handler ends the attempt there: LimitHit names the dimension and the session
    is closed by force. A handler's own exception (a gateway or broker fault) is the host's error, re-raised unchanged,
    and the worker, blocked waiting for an answer that never comes, is closed with it."""
    session = session_with({"agent/main.py": AGENT})
    host = StubHost(session)

    def over(reply):
        raise LimitHit("act_calls")
    with pytest.raises(LimitHit) as info:
        session.serve(run_message(), handlers={**host.handlers, "model_call": over}, deadline=10.0)
    assert info.value.limit == "act_calls" and session.proc.poll() is not None
    assert not issubclass(LimitHit, (CandidateDefect, Infrastructure))
    session = session_with({"agent/main.py": AGENT})

    def broken(reply):
        raise KeyError("the gateway is down")
    with pytest.raises(KeyError, match="the gateway is down"):
        session.serve(run_message(), handlers={**StubHost(session).handlers, "model_call": broken}, deadline=10.0)
    assert session.proc.poll() is not None


def test_a_model_call_may_carry_eight_megabytes_and_every_other_line_is_bounded():
    """MAX_MODEL_LINE: a model_call carrying a 7.5 MB history reaches the host; every other op keeps MAX_LINE, so a
    1.5 MB base_call ends the session; a model_call past MAX_MODEL_LINE is read as oversized and ends it too."""
    assert MAX_LINE < 1_500_000 < 7_500_000 < MAX_MODEL_LINE < 9_000_000
    big = ("def run(ctx, task):\n    n = int(task)\n"
           "    return 'got ' + ctx.model([{'role': 'user', 'content': 'x' * n}], role='act').text\n")
    session = session_with({"agent/main.py": big})
    try:
        host = StubHost(session, turn={"text": "ok", "tool_calls": [], "stop": "end"})
        assert session.serve(run_message(task="7500000"), handlers=host.handlers, deadline=60.0) == "got ok"
        assert len(host.seen[0]["messages"][0]["content"]) == 7_500_000
        with pytest.raises(WorkerCrashed, match=f"longer than {MAX_MODEL_LINE} characters"):
            session.serve(run_message(task="9000000"), handlers=host.handlers, deadline=60.0)
    finally:
        session.close(force=True)
    wide = "def run(ctx, task):\n    return ctx.tool('api_search', {'q': 'y' * 1_500_000}).output\n"
    session = session_with({"agent/main.py": wide})
    with pytest.raises(WorkerCrashed, match=f"sent a base_call line longer than {MAX_LINE} characters"):
        session.serve(run_message(), handlers=StubHost(session).handlers, deadline=30.0)


# ---------------------------------------------------------------------------- the bounded ops
def test_the_smoke_tours_each_offered_tool_once_against_the_fixture_then_ends_within_small_limits():
    """smoke: the agent against the worker's canned model — the first call that offers tools gets one tool_use turn
    calling each offered tool with the smallest arguments its schema allows, every later call the end turn — with
    the fixture broker for the tools and ctx.limits counting down the smoke's own limits. An agent that loops is
    stopped at the dimension it exhausts, and `except Exception` does not swallow the stop; an undeclared role is
    refused. Each is the package's error (PackageCallError), never infrastructure."""
    touring = ("def run(ctx, task):\n"
               "    tools = [{'name': 'api_search', 'schema': {'type': 'object', 'required': ['q', 'n'],"
               " 'properties': {'q': {'type': 'string'}, 'n': {'type': 'integer'}}}},"
               " {'name': 'api_fetch', 'schema': {'type': 'object'}}]\n"
               "    first = ctx.model([{'role': 'user', 'content': task}], tools=tools)\n"
               "    for c in first.tool_calls:\n        ctx.tool(c['name'], c['args'])\n"
               "    second = ctx.model([{'role': 'user', 'content': task}], tools=tools)\n"
               "    assert ctx.skills.read('any') is None and ctx.memory.recall('q') == []\n"
               "    counts = [ctx.limits[k] for k in ('act_calls', 'model_calls', 'tool_calls', 'tokens')]\n"
               "    return repr([first.stop, [(c['name'], c['args']) for c in first.tool_calls], second.stop,"
               " second.tool_calls, counts, 0 < ctx.limits['seconds'] <= 30, ctx.view['smoke']])\n")
    looping = "def run(ctx, task):\n    while True:\n        ctx.model([], role='act')\n"
    swallowing = ("def run(ctx, task):\n    try:\n        while True:\n            ctx.model([], role='act')\n"
                  "    except Exception as e:\n        return 'swallowed ' + type(e).__name__\n")
    tool_loop = "def run(ctx, task):\n    while True:\n        ctx.tool('api_search', {})\n"
    aux = "def run(ctx, task):\n    return ctx.model([], role='aux').text\n"
    session = session_with({"agent/tour.py": touring, "agent/loop.py": looping, "agent/swallow.py": swallowing,
                            "agent/tools.py": tool_loop, "agent/aux.py": aux})

    def smoke(entry, world=None):
        return session.request({"op": "smoke", "entry": f"agent/{entry}.py:run", "roles": ["act"], "task": "t",
                                "view": {"events": []}}, broker=Broker(world or World()))
    try:
        world = World()
        assert smoke("tour", world) == repr([
            "tool_use", [("api_search", {"q": "smoke", "n": 0}), ("api_fetch", {})], "end", [], [3, 8, 38, 19998],
            True, True])
        assert world.calls == [("api_search", {"q": "smoke", "n": 0}), ("api_fetch", {})]
        for entry, dimension in (("loop", "act_calls"), ("swallow", "act_calls"), ("tools", "tool_calls")):
            with pytest.raises(PackageCallError, match=rf"did not finish within the smoke's limits \({dimension}\)"):
                smoke(entry)
        with pytest.raises(PackageCallError, match="'aux' is not declared"):
            smoke("aux")
    finally:
        session.close()


def test_a_hook_answers_its_raw_value_through_a_read_only_broker():
    """hook: one hook's answer comes back as it is — the host checks it against the site's shape (action_of), so an
    answer outside the protocol reaches the host to be refused there; the hook's ctx.tool is read-only whatever the
    request says (a hook looks, it does not act); an answer that is not JSON is the package's defect."""
    hook = ("def run(ctx, event):\n"
            "    read = ctx.tool('api_search', {'q': event['name']})\n"
            "    write = ctx.tool('api_fetch', {'url': 'u', 'method': 'POST'})\n"
            "    return {'block': {'reason': read.output + ' / ' + str(write.is_error) + ' / ' + write.output[:18]}}\n")
    files = {"middlewares/g.py": hook, "middlewares/odd.py": "def run(ctx, event):\n    return {'action': 'maybe'}\n",
             "middlewares/bad.py": "def run(ctx, event):\n    return {1, 2}\n"}
    session = session_with(files)
    try:
        world = World()
        event = {"site": "before_tool", "turn": 1, "call_id": "c1", "name": "api_fetch", "args": {}}
        value = session.request({"op": "hook", "entry": "middlewares/g.py:run", "event": event, "view": {}},
                                broker=Broker(world))
        assert value == {"block": {"reason": "api_search ok / True / a read-only caller"}}
        assert hc.action_of("before_tool", value) == "block"
        assert world.calls == [("api_search", {"q": "api_fetch"})]              # the write never reached the world
        odd = session.request({"op": "hook", "entry": "middlewares/odd.py:run", "event": event, "view": {}})
        assert odd == {"action": "maybe"}
        with pytest.raises(hc.HookAnswerError):
            hc.action_of("before_tool", odd)
        with pytest.raises(PackageCallError, match="not JSON"):
            session.request({"op": "hook", "entry": "middlewares/bad.py:run", "event": event, "view": {}})
    finally:
        session.close()


def test_call_tool_answers_the_tools_result_bounded_and_read_only_refuses_its_writes():
    """call_tool: a package tool's answer, its output clipped at the worker's max_output_chars; with read_only (a tool
    the manifest says does not write) its world writes are refused and its reads pass; a malformed broker call is
    answered as an error, never raised on the host."""
    tool = ("def run(ctx, args):\n"
            "    bad = ctx.tool('api_search', [1])\n"
            "    w = ctx.tool('api_fetch', {'url': 'u', 'method': 'POST'})\n"
            "    return ('E' if bad.is_error else 'ok') + ('W' if w.is_error else 'w') + 'y' * 5000\n")
    session = session_with({"tools/t.py": tool,
                            "tools/err.py": "def run(ctx, args):\n    return {'output': 'nope', 'is_error': True}\n"},
                           max_output_chars=100)
    try:
        world = World()
        message = {"op": "call_tool", "entry": "tools/t.py:run", "args": {}, "view": {}}
        assert session.request({**message, "read_only": True}, broker=Broker(world)) == \
            {"output": "EW" + "y" * 98, "is_error": False}
        assert world.calls == []
        assert session.request(message, broker=Broker(world))["output"].startswith("Ew")
        assert world.calls == [("api_fetch", {"url": "u", "method": "POST"})]
        assert session.request({**message, "entry": "tools/err.py:run"}) == {"output": "nope", "is_error": True}
    finally:
        session.close()


def test_probe_imports_every_entry_and_a_missing_one_is_the_packages_error():
    session = session_with({"tools/t.py": "def run(ctx, args):\n    return 'x'\n",
                            "tools/broken.py": "import no_such_module\n"})
    try:
        assert session.request({"op": "probe", "entries": ["tools/t.py:run"]}) == "ok"
        for entries, match in ((["tools/t.py:other"], "no callable other"), (["tools/broken.py:run"], "no_such_module"),
                               (["tools/absent.py:run"], "absent")):
            with pytest.raises(PackageCallError, match=match):
                session.request({"op": "probe", "entries": entries})
    finally:
        session.close()


def test_test_runs_the_packages_tests_against_the_fixture_broker():
    tests = ("def test_reads(ctx):\n    assert ctx.view == {'fixture': True}\n"
             "    assert ctx.tool('api_search', {'q': 'z'}).output == 'api_search ok'\n"
             "def test_fails(ctx):\n    assert 1 == 2, 'one is not two'\n"
             "def test_exits(ctx):\n    raise SystemExit(4)\n"
             "def helper(ctx):\n    raise AssertionError('not a test')\n")
    session = session_with({"tests/test_a.py": tests, "tests/test_b.py": "import no_such_module\n"})
    try:
        world = World()
        outcome = session.request({"op": "test"}, broker=Broker(world))
        assert outcome["passed"] == ["test_a.py:test_reads"]
        assert [(f["test"], f["error"].split(":")[0]) for f in outcome["failed"]] == [
            ("test_a.py:test_exits", "SystemExit"), ("test_a.py:test_fails", "AssertionError"),
            ("test_b.py", "ModuleNotFoundError")]
        assert world.calls == [("api_search", {"q": "z"})]
    finally:
        session.close()


def test_check_runs_a_toolcheck_whose_tools_write_through_the_hosts_broker():
    """check (the V2a check): check(ctx, tools) calls the candidate's tools by name; a tool the manifest says writes
    reaches the world through the host's writable broker, one that does not is read-only; the answer names the tools
    called in order. A failed assertion and a script without check() are the package's errors."""
    tool = ("def run(ctx, args):\n"
            "    r = ctx.tool('api_fetch', {'url': args['url'], 'method': 'POST'})\n"
            "    return {'output': r.output, 'is_error': r.is_error}\n")
    script = ("def check(ctx, tools):\n"
              "    assert tools['writer']({'url': 'a'}) == {'output': 'api_fetch ok', 'is_error': False}\n"
              "    refused = tools['reader']({'url': 'b'})\n"
              "    assert refused['is_error'] and 'read-only' in refused['output'], refused\n")
    tools = [{"name": "writer", "entrypoint": "tools/w.py:run", "writes": True},
             {"name": "reader", "entrypoint": "tools/w.py:run", "writes": False}]
    session = session_with({"tools/w.py": tool})
    try:
        world = World()
        value = session.request({"op": "check", "script": script, "tools": tools, "view": {}}, broker=Broker(world))
        assert value == {"ok": True, "called": ["writer", "reader"]}
        assert world.calls == [("api_fetch", {"url": "a", "method": "POST"})]
        failing = "def check(ctx, tools):\n    assert tools['writer']({'url': 'c'})['is_error'], 'it wrote'\n"
        with pytest.raises(PackageCallError, match="AssertionError: it wrote"):
            session.request({"op": "check", "script": failing, "tools": tools, "view": {}}, broker=Broker(World()))
        with pytest.raises(PackageCallError, match="defines no check"):
            session.request({"op": "check", "script": "x = 1\n", "tools": tools, "view": {}})
    finally:
        session.close()


def test_package_code_that_exits_hangs_or_floods_stderr_in_a_bounded_op_is_the_packages():
    """sys.exit in a tool is the package's error and the session answers afterwards; a tool that hangs is stopped at
    request_seconds and charged to the package (a fresh worker answers normally), its process gone; 400 KB of prints
    and a raw write to fd 1 neither block the pipe nor corrupt the protocol."""
    session = session_with({"tools/exit.py": "import sys\ndef run(ctx, args):\n    sys.exit(1)\n",
                            "tools/chatty.py": "import os\ndef run(ctx, args):\n    for _ in range(4000):\n"
                                               "        print('x' * 100)\n    os.write(1, b'not protocol\\n')\n"
                                               "    return 'done'\n"}, request_seconds=10.0)
    try:
        with pytest.raises(PackageCallError, match="SystemExit"):
            session.request({"op": "call_tool", "entry": "tools/exit.py:run", "args": {}, "view": {}})
        assert session.request({"op": "call_tool", "entry": "tools/chatty.py:run", "args": {}, "view": {}}) == \
            {"output": "done", "is_error": False}
        assert "not protocol" in session.stderr_tail()
    finally:
        session.close()
    session = session_with({"tools/hang.py": "def run(ctx, args):\n    while True:\n        pass\n"},
                           request_seconds=1.0)
    with pytest.raises(PackageCallError, match="did not answer call_tool"):
        session.request({"op": "call_tool", "entry": "tools/hang.py:run", "args": {}, "view": {}})
    assert session.proc.poll() is not None


# ---------------------------------------------------------------------------- work: R's coding session's tree
def work_session(package=None, inputs=None):
    session = ProcessWorker(for_tests=True).start()
    session.load({})
    answer = session.work("init", package=package or {"manifest.json": "{}", "prompt/system.md": "old\n",
                                                      "skills/a.md": "A\n"},
                          inputs=inputs or {"notes.json": "[]"})
    assert answer == {"ok": True, "value": {"ok": True}}
    return session


def test_the_work_tree_seeds_a_candidate_from_the_package_and_writes_under_candidates_only():
    """work: init lays out package/, inputs/ and candidates/; the first write under candidates/<dir>/ seeds <dir>
    with the package's files; list and read see the whole tree; write, edit and delete refuse package/, inputs/, the
    root, a traversal and a bare directory; export hands back one candidate's files; an unknown action or dir is the
    tool's error, never a worker failure; a second init starts a fresh tree."""
    session = work_session()
    try:
        assert session.work("list")["value"] == ["inputs/notes.json", "package/manifest.json",
                                                 "package/prompt/system.md", "package/skills/a.md"]
        assert session.work("read", path="package/prompt/system.md")["value"] == "old\n"
        written = session.work("write", path="candidates/c1/prompt/system.md", text="new\n")
        assert written == {"ok": True, "value": {"written": 4, "seeded": True}}
        assert session.work("export", dir="c1")["value"] == {"manifest.json": "{}", "prompt/system.md": "new\n",
                                                            "skills/a.md": "A\n"}
        assert session.work("write", path="candidates/c1/skills/b.md", text="B\n")["value"]["seeded"] is False
        assert session.work("edit", path="candidates/c1/skills/a.md", old="A", new="AA")["ok"]
        assert session.work("read", path="candidates/c1/skills/a.md")["value"] == "AA\n"
        twice = session.work("edit", path="candidates/c1/skills/a.md", old="A", new="x")
        assert not twice["ok"] and "occurs 2 times" in twice["error"]
        assert session.work("delete", path="candidates/c1/prompt/system.md")["ok"]
        assert session.work("list", path="candidates/c1")["value"] == ["manifest.json", "skills/a.md", "skills/b.md"]
        for bad in ("package/prompt/system.md", "inputs/notes.json", "system.md", "candidates/c1", "candidates/../x",
                    "/etc/passwd", "candidates/c 1/x.md", "candidates/c1/../../x"):
            assert not session.work("write", path=bad, text="x")["ok"], bad
        assert session.work("read", path="package/prompt/system.md")["value"] == "old\n"
        assert not session.work("read", path="candidates/none/x.md")["ok"]
        assert not session.work("export", dir="none")["ok"] and not session.work("export", dir="../c1")["ok"]
        assert "unknown work action" in session.work("squash")["error"]
        assert not session.work("edit", path="candidates/c1/skills/a.md", old="", new="x")["ok"]
        assert not session.work("write", path="candidates/c1/skills/a.md", text=None)["ok"]
        assert session.work("list", path="package/")["value"] == session.work("list", path="package")["value"]
        assert not session.work("write", path="candidates/c1/", text="x")["ok"]
        session.work("init", package={"manifest.json": "{}"}, inputs={})
        assert session.work("list")["value"] == ["package/manifest.json"]
    finally:
        session.close()


def test_a_work_action_runs_no_code_and_its_failures_are_the_tools_or_the_workers_never_the_packages():
    """work runs no package code — a Python file written to a candidate is text — so a write onto a directory, under
    a file, of text that cannot be encoded, or a malformed request each come back as the tool's `ok: false` and the
    worker answers the next action; a worker gone under it is WorkerCrashed, never a CandidateDefect."""
    session = work_session()
    try:
        code = "import os\nos.system('echo pwned')\nraise SystemExit(3)\n"
        assert session.work("write", path="candidates/c1/tools/t.py", text=code)["ok"]
        assert session.work("read", path="candidates/c1/tools/t.py")["value"] == code and session.proc.poll() is None
        onto_dir = session.work("write", path="candidates/c1/skills", text="x")
        assert not onto_dir["ok"] and "is a directory" in onto_dir["error"]
        under_file = session.work("write", path="candidates/c1/skills/a.md/x.md", text="x")
        assert not under_file["ok"] and "is a file, not a directory" in under_file["error"]
        assert not session.work("edit", path="candidates/c1/skills/a.md/x.md", old="a", new="b")["ok"]
        bad_text = session.work("write", path="candidates/c1/bad.md", text="\ud800")
        assert not bad_text["ok"] and bad_text["error"].startswith("UnicodeEncodeError")
        malformed = session.work("init", package=["not", "a", "mapping"], inputs={})
        assert not malformed["ok"] and malformed["error"].startswith("AttributeError")
        assert session.proc.poll() is None
        session.proc.kill()
        session.proc.wait(timeout=5)
        with pytest.raises(WorkerCrashed) as info:
            session.work("list")
        assert not isinstance(info.value, CandidateDefect)
    finally:
        session.close(force=True)


def test_the_work_tree_refuses_a_write_past_ks_byte_caps_and_every_answer_fits_a_protocol_line():
    """init carries K's max_package_bytes as the candidate directory's cap and a per-file cap, and a write or edit that
    would cross either is refused before the disk is touched. The protocol line is a separate bound measured escaped
    (an emoji is 12 characters, a control character 6): a write whose directory would not export in one line is
    refused, so every legal candidate exports whole; an answer that would not fit is never sent."""
    wr = runner_module()
    session = ProcessWorker(for_tests=True).start()
    session.load({})
    try:
        assert session.work("init", package={"manifest.json": "{}"}, inputs={}, max_bytes=100, max_file_bytes=60)["ok"]
        big = session.work("write", path="candidates/c1/a.md", text="x" * 61)
        assert not big["ok"] and "a file of a candidate may hold at most 60 bytes" in big["error"]
        assert session.work("write", path="candidates/c1/a.md", text="x" * 50)["ok"]
        assert session.work("write", path="candidates/c1/b.md", text="y" * 40)["ok"]       # 2 + 50 + 40 = 92
        over = session.work("write", path="candidates/c1/c.md", text="z" * 20)
        assert not over["ok"] and "may hold at most 100 bytes in all" in over["error"]
        grown = session.work("edit", path="candidates/c1/b.md", old="y" * 40, new="y" * 55)
        assert not grown["ok"] and "in all" in grown["error"]
        assert session.work("init", package={"manifest.json": "{}"}, inputs={}, max_bytes=10_000_000,
                            max_file_bytes=10_000_000)["ok"]
        for text in ("\U0001F600" * 80_000, "\x01" * 160_000):                      # 960k characters escaped
            refused = session.work("write", path="candidates/c1/e.md", text=text)
            assert not refused["ok"] and "on the protocol line once escaped" in refused["error"]
        assert session.work("write", path="candidates/c1/e.md", text="\U0001F600" * 60_000)["ok"]
        assert not session.work("write", path="candidates/c1/f.md", text="\x02" * 40_000)["ok"]
        exported = session.work("export", dir="c1")
        assert exported["ok"] and exported["value"]["e.md"] == "\U0001F600" * 60_000
        assert len(json.dumps({"op": "result", "value": exported})) < wr.LINE_BUDGET < MAX_LINE
        # the export of a directory accepted at exactly the budget fits, and one character more is refused
        assert session.work("init", package={"manifest.json": "{}"}, inputs={}, max_bytes=10_000_000,
                            max_file_bytes=10_000_000)["ok"]
        held = len(json.dumps("manifest.json")) + len(json.dumps("{}")) + 4
        n = wr.LINE_BUDGET - wr.EXPORT_WRAPPER - held - (len(json.dumps("big.md")) + 2 + 4)
        assert session.work("write", path="candidates/c2/big.md", text="x" * n)["ok"]
        whole = session.work("export", dir="c2")
        assert len(whole["value"]["big.md"]) == n and len(json.dumps({"op": "result", "value": whole})) <= wr.LINE_BUDGET
        assert not session.work("write", path="candidates/c2/big.md", text="x" * (n + 1))["ok"]
        # a package seeded past the line (the host's own files): the seeding write is refused, the export answers
        # the protocol's refusal instead of an oversized line
        assert session.work("init", package={"manifest.json": "{}", "skills/m.md": "\U0001F600" * 100_000},
                            inputs={}, max_bytes=10_000_000, max_file_bytes=10_000_000)["ok"]
        assert not session.work("write", path="candidates/c3/x.md", text="x")["ok"]
        assert session.work("export", dir="c3") == {
            "ok": False, "error": "the answer would exceed the protocol line; read it in pieces"}
        assert session.proc.poll() is None
    finally:
        session.close()


def test_read_pages_a_long_non_ascii_file_within_the_protocol_line():
    """read answers at most READ_CHARS characters from `offset`, halved while the page would not fit the line escaped,
    and says how many characters follow and where to read next, so an inputs file of any size is read whole."""
    wr = runner_module()
    text = ("\u00e9" * 100_000) + ("\U0001F600" * 60_000) + ("\x03" * 40_000)
    session = work_session(inputs={"ledger.json": text})
    try:
        pages, offset = [], 0
        while True:
            answer = session.work("read", path="inputs/ledger.json", offset=offset)
            assert answer["ok"] and len(json.dumps({"op": "result", "value": answer})) < wr.LINE_BUDGET
            body, _, tail = answer["value"].partition("\n[... ")
            assert len(body) <= wr.READ_CHARS
            pages.append(body)
            offset += len(body)
            if not tail:
                break
            assert tail.startswith(f"{len(text) - offset} characters follow; read again with offset {offset}]")
        assert "".join(pages) == text and len(pages) >= 3
        assert session.work("read", path="inputs/ledger.json", limit=10)["value"].startswith("\u00e9" * 10 + "\n[... ")
        assert not session.work("read", path="inputs/ledger.json", offset="abc")["ok"]
    finally:
        session.close()


def test_a_work_path_is_bounded_and_strict_utf8():
    """A path of more than 32 segments or 1024 characters is refused before the disk is touched, and one that is not
    strict UTF-8 (a lone surrogate) is refused, so no raw-byte filename reaches list or export."""
    session = work_session()
    try:
        deep = session.work("write", path="candidates/c1/" + "/".join(["a"] * 1500) + "/x.md", text="x")
        assert not deep["ok"] and "at most 32 segments" in deep["error"]
        long = session.work("write", path="candidates/c1/" + "a" * 1100 + ".md", text="x")
        assert not long["ok"] and "1024 characters" in long["error"]
        raw = session.work("write", path="candidates/c1/\udc80x.md", text="x")
        assert not raw["ok"] and "not strict UTF-8" in raw["error"]
        assert not session.work("read", path="package/\udcffy.md")["ok"]
        assert session.work("write", path="candidates/c1/" + "/".join(["a"] * 29) + "/x.md", text="x")["ok"]
        assert session.proc.poll() is None
    finally:
        session.close()
