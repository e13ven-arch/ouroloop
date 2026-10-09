"""The runner shell (docs/design/RUNTIME.md §5, §8): one attempt of the stirrup seed in a ProcessWorker over a small
tool surface, with a scripted actor — the transcript it writes, the four ways it ends, the package's tools and hooks
under the host's rules, the system prompt it composes, and the egress meter under a proxy K."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from ouroloop.contracts import LimitHit, StaleWorker
from ouroloop.evaluate.attempt import Budgeted
from ouroloop.evaluate.budget import Pricing
from ouroloop.evaluate.limits import Budget
from ouroloop.memory.retrieval import TaskMemory
from ouroloop.runtime.records import TERMINATIONS, digest16
from ouroloop.runtime.runner import SKILL_INDEX, EgressMeter, compose_system
from ouroloop.runtime.worker import RUNNER, ProcessWorker
from runtime_world import (ATTEMPT, EffortAware, Script, World, attempt, core, finish, kinds, minimal, of, seed, turn,
                           variant)

USAGE = {"input_tokens": 100, "output_tokens": 20}
CRM = "https://crm.example/contacts"
POST = {"url": CRM, "method": "POST", "body": "{\"stage\": \"opportunity\"}"}
PLAN = {"changes": [{"content": "move the contact to opportunity", "status": "pending"}], "excluded": []}
META = {"task_id": "t1", "split": "train", "task_hash": "h1", "release_ref": None, "core_ref": None, "arm": "subject"}


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    return seed(tmp_path_factory.mktemp("seed"))


def with_tool(code: str, *, writes: bool | None = None, name: str | None = None):
    """Changes to the seed: its plan_changes tool's code replaced, and its manifest entry's `writes` or name."""
    def manifest(m):
        (tool,) = m["tools"]
        if writes is not None:
            tool["writes"] = writes
        if name is not None:
            tool["name"] = name
    return {"tools/plan_changes.py": code}, manifest


# ---------------------------------------------------------------------------- a finished attempt, line by line
def test_a_finished_attempt_writes_its_transcript_in_order_and_the_hooks_lines_come_from_the_adapters_marks(
        tmp_path, base):
    """The seed's write guard blocks the first write before a plan (before_tool) and pushes the first finish back
    (on_finish); each hook that acted is marked by the adapter and written by the host as a `hook` line, labelled the
    package's, never a `mark` line. Every model call is a `gateway_call` (the tool list once, as `tool_specs`), every
    world call a `tool` and a `broker_event`; the `worker_session` line closes the worker before the `end` line."""
    provider = Script([turn("", ("api_fetch", POST), usage=USAGE), turn("", ("plan_changes", PLAN), usage=USAGE),
                       turn("", ("api_fetch", POST), usage=USAGE), finish("Moved the contact.", usage=USAGE)])
    pricing = Pricing(input_per_1k=0.004, output_per_1k=0.02, source="test prices")
    a = attempt(tmp_path, base, provider, task_meta=META, pricing=pricing)
    r = a.result
    assert (r.text, r.termination, r.limit, r.turns, r.error) == ("Moved the contact.", "finished", None, 5, "")
    assert a.world.calls == [("api_fetch", POST)]                        # the blocked first write never ran
    assert kinds(a.lines) == ["task", "tool_specs", "gateway_call", "hook", "mark", "gateway_call", "mark",
                              "gateway_call", "tool", "broker_event", "gateway_call", "hook", "mark", "gateway_call",
                              "mark", "worker_session", "end"]
    assert [line["seq"] for line in a.lines] == list(range(1, len(a.lines) + 1))
    task = a.lines[0]
    assert task == {**task, **META, "attempt_id": ATTEMPT, "package_ref": base.ref(), "text": "search for x"}
    hooks = of(a.lines, "hook")
    assert [(h["site"], h["name"], h["action"], h["source"]) for h in hooks] == [
        ("before_tool", "triage", "block", "package"), ("on_finish", "verify", "pushback", "package")]
    assert "broker_calls" not in hooks[0] and "Before your first change, call plan_changes" in hooks[0]["detail"]
    assert r.hooks == [{k: h[k] for k in ("site", "name", "action", "detail", "turn")} for h in hooks]
    assert [m["mark"] for m in of(a.lines, "mark")] == ["tool_blocked", "package_tool", "pushback", "stirrup_end"]
    assert all(m["source"] == "package" for m in of(a.lines, "mark"))
    assert r.marks == {"hook": 2, "tool_blocked": 1, "package_tool": 1, "pushback": 1, "stirrup_end": 1}
    # the pushback reached the model as the finish's result, and the model finished again
    results = [res["output"] for m in provider.seen[4]["messages"] if m.get("role") == "tool" for res in m["results"]]
    assert any("Keep working on the task" in out for out in results)
    session = of(a.lines, "worker_session")[0]
    assert session["requests"] == 5 + 1 + 6 and session["worker"] == {"kind": "ProcessWorker", "qualified": False}
    end = a.lines[-1]
    assert end == {**end, "termination": "finished", "limit": None, "error": "", "turns": 5, "stop": "tool_use",
                   "final_text_digest": digest16("Moved the contact."),
                   "usage": {"input_tokens": 500, "output_tokens": 100, "cache_read_tokens": 0,
                             "cache_write_tokens": 0}}
    assert end["cost"] == r.cost == pytest.approx(5 * (0.1 * 0.004 + 0.02 * 0.02))
    assert r.tools_called == ["api_fetch"] and r.tool_calls == 1 and r.session == ATTEMPT


def test_the_shell_caps_effort_at_ks_max_effort_and_hands_the_agent_each_calls_usage_and_limits(tmp_path):
    """The gateway's ceiling is K's model.max_effort; each answer carries the call's usage, which the worker puts in
    ctx.view["last_usage"], and the remaining limits, which become ctx.limits."""
    agent = ("def run(ctx, task):\n    before = ctx.view['last_usage']\n"
             "    ctx.model([{'role': 'user', 'content': task}], effort='max')\n"
             "    return repr([before, ctx.view['last_usage'], ctx.limits['act_calls'], ctx.limits['model_calls']])\n")
    provider = EffortAware([turn("x", usage=USAGE)], effort="low")
    a = attempt(tmp_path, minimal(tmp_path / "pkg", agent), provider, k=core(model={"max_effort": "medium"}))
    assert a.result.text == repr([None, USAGE, 7, 19]) and provider.efforts == ["medium"]
    (call,) = of(a.lines, "gateway_call")
    assert (call["effort_requested"], call["effort_sent"]) == ("max", "medium")


# ---------------------------------------------------------------------------- the four endings
def test_a_hard_limit_ends_the_attempt_as_budget_with_its_dimension_and_the_world_keeps_what_ran(tmp_path, base):
    """max_tool_calls 2: the third world call is refused on record, and the attempt ends as termination `budget`,
    limit tool_calls, with an empty final text; the worker is closed before the end line."""
    k = core(limits={"max_tool_calls": 2})
    a = attempt(tmp_path, base, Script([turn("", ("api_search", {"q": "again"}), usage=USAGE)]), k=k)
    assert (a.result.termination, a.result.limit, a.result.text, a.result.stop) == ("budget", "tool_calls", "",
                                                                                    "tool_calls")
    assert a.world.calls == [("api_search", {"q": "again"})] * 2
    assert of(a.lines, "broker_event")[-1]["reason"] == "budget: tool_calls"
    assert kinds(a.lines)[-2:] == ["worker_session", "end"]
    assert a.lines[-1]["termination"] == "budget" and a.lines[-1]["limit"] == "tool_calls"


def test_a_worker_that_outruns_max_seconds_ends_the_attempt_at_seconds(tmp_path, base):
    """A package tool that never returns: the host's deadline (K's max_seconds) ends the run, and since the clock is
    past max_seconds the attempt ends as `budget` at seconds, not as infrastructure."""
    changes, manifest = with_tool("def run(ctx, args):\n    while True:\n        pass\n")
    package = variant(tmp_path, base, changes, manifest=manifest)
    a = attempt(tmp_path, package, Script([turn("", ("plan_changes", PLAN))]), k=core(limits={"max_seconds": 1}))
    assert (a.result.termination, a.result.limit) == ("budget", "seconds")
    assert a.lines[-1]["limit"] == "seconds"


def test_a_package_tool_that_raises_ends_the_attempt_as_the_packages_defect(tmp_path, base):
    changes, manifest = with_tool("def run(ctx, args):\n    raise ValueError('the plan store is down')\n")
    package = variant(tmp_path, base, changes, manifest=manifest)
    a = attempt(tmp_path, package, Script([turn("", ("plan_changes", PLAN))]))
    assert a.result.termination == "harness_error" and a.result.limit is None and a.result.text == ""
    assert "PackageToolError" in a.result.error and "the plan store is down" in a.result.error
    assert a.lines[-1]["termination"] == "harness_error" and kinds(a.lines)[-2:] == ["worker_session", "end"]


def test_a_dead_worker_or_a_failing_provider_ends_the_attempt_as_infrastructure(tmp_path, base):
    """os._exit in a hook mid-run, and a provider that fails: neither is evidence about the package — the attempt is
    `infrastructure` with the cause on the end line."""
    def die(m):
        m["hooks"].append({"name": "die", "site": "after_tool", "entrypoint": "middlewares/die.py:run"})
    package = variant(tmp_path, base, {"middlewares/die.py": "import os\ndef run(ctx, event):\n    os._exit(3)\n"},
                      manifest=die)
    a = attempt(tmp_path / "dead", package, Script([turn("", ("api_search", {"q": "x"}))]))
    assert a.result.termination == "infrastructure" and "WorkerCrashed" in a.result.error
    assert "exited during run" in a.result.error and a.lines[-1]["termination"] == "infrastructure"
    failing = Script([], fail=RuntimeError("the provider answered 500"))
    b = attempt(tmp_path / "provider", base, failing)
    assert b.result.termination == "infrastructure" and "the provider answered 500" in b.result.error
    assert set(TERMINATIONS) == {"finished", "budget", "harness_error", "infrastructure"}


def test_a_stale_worker_is_raised_after_the_end_line_and_never_runs_the_package(tmp_path, base):
    other = tmp_path / "worker_runner.py"
    other.write_text(RUNNER.read_text() + "\n# another runner\n")

    class Stale(ProcessWorker):
        def command(self):
            return [sys.executable, "-I", str(other)]
    provider = Script([finish()])
    a = attempt(tmp_path, base, provider, worker=Stale(for_tests=True), raises=StaleWorker)
    assert provider.n == 0 and a.lines[-1]["termination"] == "infrastructure"
    assert "StaleWorker" in a.lines[-1]["error"]


# ---------------------------------------------------------------------------- the package's tools under the host's rules
def test_a_package_tool_that_takes_a_base_tools_name_is_refused_as_a_defect_before_the_worker_starts(tmp_path, base):
    changes, manifest = with_tool("def run(ctx, args):\n    return 'mine'\n", name="api_search")
    package = variant(tmp_path, base, changes, manifest=manifest)
    provider = Script([finish()])
    a = attempt(tmp_path, package, provider)
    assert a.result.termination == "harness_error" and "take the names of this host's tools" in a.result.error
    assert provider.n == 0 and kinds(a.lines) == ["task", "end"]                 # no worker was started


@pytest.mark.parametrize("writes", [False, True])
def test_a_package_tool_that_does_not_write_reaches_the_world_read_only(tmp_path, base, writes):
    """A package tool's world calls go through the broker like the agent's; when the manifest says the tool does not
    write, each of its calls is read-only and a write is refused on record and never reaches the world."""
    code = ("def run(ctx, args):\n"
            f"    r = ctx.tool('api_fetch', {POST!r})\n"
            "    return {'output': 'wrote: ' + r['output'], 'is_error': r['is_error']}\n")
    changes, manifest = with_tool(code, writes=writes)
    package = variant(tmp_path, base, changes, manifest=manifest)
    a = attempt(tmp_path, package, Script([turn("", ("plan_changes", PLAN)), finish()]))
    assert a.result.termination == "finished"
    (event,) = of(a.lines, "broker_event")
    if writes:
        assert a.world.calls == [("api_fetch", POST)] and not event["refused"] and event["read_only"] is False
    else:
        assert a.world.calls == [] and event["refused"] and event["reason"] == "writes" and event["read_only"] is True
    (mark,) = [m for m in of(a.lines, "mark") if m["mark"] == "package_tool"]
    assert mark["data"]["is_error"] is (not writes) and mark["data"]["broker_calls"] == [
        {"tool": "api_fetch", "is_error": not writes}]


# ---------------------------------------------------------------------------- the system prompt, skills and memory
def test_the_system_prompt_is_composed_in_order_and_skills_and_memory_answer_through_the_host(tmp_path):
    """compose_system appends, in order: the task's own system, prompt/system.md, prompt/finish.md, the index of the
    enabled skills and the memory's opening block. In an attempt the actor is sent it inside the seed's template, is
    offered read_skill and recall, and their answers come from the host's SkillReader and memory reader, each read
    on record."""
    k = core(memory={"enabled": True, "recall_k": 2})
    memory_seed = seed(tmp_path / "seed", k)

    def skills(m):
        m["skills"] = [{"name": "crm", "path": "skills/crm.md", "description": "CRM rules"},
                       {"name": "old", "path": "skills/old.md", "enabled": False}]
    package = variant(tmp_path / "seed", memory_seed, {"skills/crm.md": "Use the contact id.", "skills/old.md": "stale",
                                                       "prompt/system.md": "You work on a CRM.\n"}, manifest=skills, k=k)
    composed = compose_system("TASK SYSTEM", package, "OPENING BLOCK")
    assert composed == "\n\n".join(["TASK SYSTEM", "You work on a CRM.", package.files["prompt/finish.md"].strip(),
                                    SKILL_INDEX + "\n- crm: CRM rules", "OPENING BLOCK"])
    assert compose_system("", memory_seed) == memory_seed.files["prompt/finish.md"].strip()     # blank parts left out
    asked = []

    def reader(query, n):
        asked.append((query, n))
        return [{"id": "e1", "when": "2026-01-02", "content": "Post in #ops after the update.", "score": 0.9}]
    provider = Script([turn("", ("read_skill", {"name": "crm"}), ("recall", {"query": "ops", "k": 9})), finish()])
    memory = TaskMemory(opening="OPENING BLOCK", event={"query": "search for x", "shown": ["e0full"], "scores": [0.7]},
                        recaller=reader)
    a = attempt(tmp_path, package, provider, k=k, system="TASK SYSTEM", memory=memory)
    assert a.result.termination == "finished"
    assert composed in provider.seen[0]["system"] and {"read_skill", "recall"} <= set(provider.seen[0]["tools"])
    assert [(s["name"], s["found"]) for s in of(a.lines, "skill_read")] == [("crm", True)]
    assert kinds(a.lines)[:2] == ["task", "library_retrieval"]               # the start's line follows the task's
    start, retrieval = of(a.lines, "library_retrieval")
    assert (start["site"], start["shown"], start["scores"]) == ("start", ["e0full"], [0.7])
    assert (retrieval["site"], retrieval["query"], retrieval["shown"]) == ("recall", "ops", ["e1"])
    assert asked == [("ops", 2)]                                             # capped at K's recall_k
    results = [res["output"] for m in provider.seen[1]["messages"] if m.get("role") == "tool" for res in m["results"]]
    assert "Use the contact id." in results and any("Post in #ops after the update." in out for out in results)
    assert a.result.skills_read == ["crm"] and a.world.calls == []


# ---------------------------------------------------------------------------- egress under a proxy K
class FakeEgress:
    """An EgressSession's shape, its rows scripted: what the proxy let out (used) and the rows of each poll."""

    def __init__(self, polls, final=()):
        self.polls, self.final = list(polls), list(final)
        self.token_id, self.used, self.caps, self.closed = "abc123def456", 0, [], False
        self.network, self.env = "ouroloop-egress-test", {"HTTP_PROXY": "http://attempt:t@egress:3128"}

    def poll(self, *, max_requests=None):
        self.caps.append(max_requests)
        used, rows = self.polls.pop(0) if self.polls else (self.used, [])
        self.used = used
        return rows

    def close(self):
        self.closed = True
        return self.final


class FakePool:
    def __init__(self, egress):
        self.egress, self.asked = egress, []

    def session(self, attempt_id, *, read_only=False, max_requests=None):
        self.asked.append((attempt_id, read_only, max_requests))
        return self.egress


class ProxyWorker(ProcessWorker):
    """The host's test worker under a proxy K: its egress sessions come from the pool, and it records the session each
    worker session started with."""

    def __init__(self, pool):
        super().__init__(for_tests=True)
        self.pool, self.started_with = pool, []

    def egress_session(self, attempt_id, *, read_only=False, max_requests=None):
        return self.pool.session(attempt_id, read_only=read_only, max_requests=max_requests)

    def start(self, egress=None):
        self.started_with.append(egress)
        return super().start()


GET = {"method": "GET", "host": "api.example.com", "port": 443, "status": 200, "reason": None}
CAPPED = {**GET, "status": 429, "reason": "request_cap"}
PROXY_K = core(worker={"network": {"mode": "proxy", "allow": [{"host": "api.example.com", "port": 443,
                                                                "methods": ["GET"]}]}},
               limits={"max_tool_calls": 3})


def test_under_a_proxy_k_the_attempt_opens_its_egress_session_capped_at_its_tool_calls_and_records_the_rows(
        tmp_path, base):
    """Under K's proxy network the attempt opens its egress session with the tool calls left as the cap, starts the
    worker on it, polls it at every callback (moving the cap as base calls are made) and writes every row as an
    `egress` line; proxied requests count as tool calls; the summary is on the worker_session line."""
    egress = FakeEgress([(1, [GET])], final=[{"method": "CONNECT", "status": 200, "open": True}])
    worker = ProxyWorker(FakePool(egress))
    a = attempt(tmp_path, base, Script([turn("", ("api_search", {"q": "x"})), finish()]), k=PROXY_K, worker=worker)
    assert a.result.termination == "finished"
    assert worker.pool.asked == [(ATTEMPT, False, 3)] and worker.started_with == [egress] and egress.closed
    rows = of(a.lines, "egress")
    assert [(r["method"], r["status"]) for r in rows] == [("GET", 200), ("CONNECT", 200)] and rows[1]["open"] is True
    assert kinds(a.lines).index("egress") < kinds(a.lines).index("gateway_call")     # polled before the first call
    assert egress.caps[0] == 3 and egress.caps[-1] == 2                                # the base call moved the cap
    assert (a.surface.tool_calls, a.surface.base_tool_calls) == (2, 1)
    assert of(a.lines, "worker_session")[0]["egress"] == {"token_id": "abc123def456", "requests": 1, "rows": 2}


def test_the_proxy_cap_follows_a_base_call_as_soon_as_it_is_answered(tmp_path):
    """Package code may go out with no callback between its requests (urllib, straight through the proxy): the host
    moves the proxy's cap right after it answers a base call, not at the next callback, so a base call followed by
    raw requests cannot take the attempt past max_tool_calls between two polls."""
    package = minimal(tmp_path, "def run(ctx, task):\n    ctx.tool('api_search', {'q': 'x'})\n    return 'done'\n")
    egress = FakeEgress([])
    a = attempt(tmp_path, package, Script([finish()]), k=PROXY_K, worker=ProxyWorker(FakePool(egress)))
    assert a.result.text == "done" and a.world.calls == [("api_search", {"q": "x"})]
    assert egress.caps == [3, 2]                      # before the base call, and at once after it


def test_a_request_the_proxy_refused_at_the_cap_ends_the_attempt_at_tool_calls(tmp_path, base):
    egress = FakeEgress([(1, [GET]), (2, [GET, CAPPED])])
    a = attempt(tmp_path, base, Script([turn("", ("api_search", {"q": "x"})), finish()]), k=PROXY_K,
                worker=ProxyWorker(FakePool(egress)))
    assert (a.result.termination, a.result.limit) == ("budget", "tool_calls")
    assert [r["status"] for r in of(a.lines, "egress")] == [200, 200, 429]
    assert of(a.lines, "worker_session")[0]["egress"]["requests"] == 2 and egress.closed


def test_the_meter_caps_the_proxy_at_the_calls_left_writes_the_rows_and_ends_at_the_cap():
    """EgressMeter alone: the cap is max_tool_calls less the base calls; each poll writes its rows and counts the
    proxy's `used` into the surface; a row refused at the cap ends the attempt at tool_calls; close() writes the rows
    the session still held and raises nothing."""
    world = World()
    surface = Budgeted(world, Budget(max_steps=5, max_tool_calls=3))
    written = []
    meter = EgressMeter(surface, 3, lambda kind, row: written.append((kind, row)))
    egress = FakeEgress([(1, [GET]), (1, []), (2, [GET, CAPPED])], final=[{"method": "CONNECT", "open": True}])
    assert meter.open(SimpleNamespace(egress_session=lambda attempt_id, max_requests=None: egress), "run/t/0") is egress
    assert meter.cap() == 3
    meter.poll()
    assert surface.tool_calls == 1 and egress.caps[-1] == 3
    surface.run("api_search", {})
    meter.poll()
    assert egress.caps[-1] == 2
    with pytest.raises(LimitHit) as hit:
        meter.poll()
    assert hit.value.limit == "tool_calls" and surface.limit == "tool_calls"
    assert meter.close() == {"token_id": "abc123def456", "requests": 2, "rows": 4}
    assert [k for k, _ in written] == ["egress"] * 4 and written[-1][1]["open"] is True
    assert EgressMeter(surface, None, lambda k, r: None).cap() is None


def test_a_none_network_opens_no_egress_session(tmp_path, base):
    worker = ProxyWorker(FakePool(None))
    a = attempt(tmp_path, base, Script([finish()]), worker=worker)
    assert worker.pool.asked == [] and worker.started_with == [None] and "egress" not in of(a.lines, "worker_session")[0]
    assert json.loads(json.dumps(a.lines))                                            # every line is plain JSON
