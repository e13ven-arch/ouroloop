"""Stage B (docs/modules/01_POLICY_RUNTIME.md §1, 05 §1, 08 IT6-22/23/24): package code in the worker, the broker,
the three hook sites, and code drafts in the draft check.

These tests use ProcessWorker, which runs the real runner and protocol in a subprocess but is NOT isolated
(`qualified = False`): they show the host's semantics. Isolation itself is shown only by the Docker test at the
end, which are skipped when docker or the worker image is not available — a skip, never a pass. CI builds the
image and sets OUROLOOP_TEST_REQUIRE_DOCKER, and there a missing worker fails them instead.
"""
import json
import os

import pytest

import rsi
from ouroloop import bench, policies as P
from ouroloop.agent import Agent, AgentConfig
from ouroloop.agent import harness_package as hp
from ouroloop.agent.tools import ToolResult
from ouroloop.agent.worker import (DEFAULT_IMAGE, Broker, PackageCallError, ProcessWorker, WorkerCrashed,
                                   docker_worker)
from ouroloop.chat import ChatTurn, ToolCall, ToolSpec
from ouroloop.evolve import contracts as ct

CODE = hp.Limits(components=hp.KINDS, package_api=hp.API_VERSION)    # every kind, so the newest API (M0 §2)
FINISH_GUARD = '''
def run(ctx, event):
    sent = any('slack.com' in str(e.get('args', {}).get('url', '')) for e in ctx.view.get('events', []))
    wants = '#ops' in ctx.view.get('task', '')
    if wants and not sent:
        return {"action": "block", "message": "The request also asks for a message in #ops, and none was sent."}
    return {"action": "continue"}
'''


def package(objects, files, manifest_extra):
    manifest = {**hp.empty_manifest(), **manifest_extra}
    return hp.build_package(objects, {"manifest.json": json.dumps(manifest), **files}, CODE)


def bound_with(tmp_path, files, manifest_extra):
    objects = ct.Objects(tmp_path)
    ref = package(objects, files, manifest_extra)
    worker = P.default_worker_set(objects, finish_check=rsi.W0_FINISH, package_ref=ref)
    targets = (*P.targets_of("worker"), "worker.harness", *P.targets_of("research"))
    releases, rel, objects = rsi.workspace(tmp_path, worker_ref=worker, targets=targets)
    return P.bind_release(rel, profile="task", services=P.Services(objects)), objects


class World:
    """A tiny business surface: api_search reads, api_fetch with a non-GET method writes."""

    def __init__(self):
        self.calls = []

    def specs(self):
        return [ToolSpec("api_search", "search", {"type": "object", "properties": {"q": {"type": "string"}}}),
                ToolSpec("api_fetch", "fetch", {"type": "object", "properties": {"url": {"type": "string"},
                                                                                  "method": {"type": "string"}}})]

    def run(self, name, args):
        self.calls.append((name, dict(args)))
        return ToolResult(f"{name} ok")

    def mutates(self, name, args):
        return name == "api_fetch" and str(args.get("method", "GET")).upper() != "GET"

    @property
    def tool_calls(self):
        return len(self.calls)          # what Budgeted reads of an episode, so the World can sit under one


def agent_for(tmp_path, bound, world, provider, **cfg):
    surface = hp.mount(world, bound, worker=ProcessWorker(for_tests=True))
    prompt, _ = bound.system_prompt("You are a business agent.")
    agent = Agent(provider, rsi.runtime(tmp_path), surface, system=prompt,
                  config=AgentConfig(approve="auto", max_turns=cfg.get("max_turns", 8), gated_tools=(),
                                     max_pushbacks=cfg.get("max_pushbacks", 2)))
    if getattr(surface, "runtime", None) is not None:
        surface.runtime.on_event = lambda kind, data: agent._emit(kind, **data)
    return agent


def transcript(tmp_path, agent):
    path = tmp_path / "transcripts" / f"{agent.session}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


# ---------------------------------------------------------------------------- PR6-10
def test_pr6_10_an_on_finish_hook_really_stops_the_finish_and_the_work_gets_done(tmp_path):
    bound, _ = bound_with(tmp_path, {"middlewares/guard.py": FINISH_GUARD},
                          {"hooks": [{"name": "guard", "site": "on_finish", "entrypoint": "middlewares/guard.py:run"}]})
    world = World()
    agent = agent_for(tmp_path, bound, world, rsi.BusinessWorker())
    agent.run("Move Dana Reyes to opportunity stage, then post to #ops with the phrase deal moved.")
    urls = [a.get("url") for n, a in world.calls]
    assert urls == [rsi.HUBSPOT, rsi.SLACK]                     # the pushback made the worker send the message
    events = transcript(tmp_path, agent)
    assert [e for e in events if e["kind"] == "hook" and e["action"] == "block"]
    assert [e for e in events if e["kind"] == "pushback" and e.get("source") == "package"]


def test_pr6_10_a_package_tool_is_exposed_and_its_base_calls_go_through_the_broker(tmp_path):
    tool = "def run(ctx, args):\n    r = ctx.call_tool('api_search', {'q': args.get('q', '')})\n    return 'paged: ' + r.output\n"
    bound, _ = bound_with(tmp_path, {"tools/paged.py": tool},
                          {"tools": [{"name": "paged_search", "description": "search every page",
                                      "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
                                      "entrypoint": "tools/paged.py:run"}]})
    world = World()

    class Uses:
        name, model = "scripted", "s"

        def __init__(self):
            self.n = 0

        def chat(self, system, messages, specs):
            self.n += 1
            assert "paged_search" in [s.name for s in specs]
            if self.n == 1:
                return ChatTurn("", [ToolCall("c1", "paged_search", {"q": "x"})], "tool_use")
            return ChatTurn("done", [], "end")
    agent = agent_for(tmp_path, bound, world, Uses())
    agent.run("find x")
    assert world.calls == [("api_search", {"q": "x"})]
    tool_event = [e for e in transcript(tmp_path, agent) if e["kind"] == "tool" and e["name"] == "paged_search"][0]
    assert tool_event["output"] == "paged: api_search ok" and tool_event["broker_calls"][0]["tool"] == "api_search"


# ---------------------------------------------------------------------------- PR6-11
def test_pr6_11_before_tool_block_stops_the_call_and_the_rest_of_the_turn(tmp_path):
    hook = "def run(ctx, event):\n    if event['tool'] == 'api_fetch':\n        return {'action': 'block', 'message': 'read the record first'}\n    return {'action': 'continue'}\n"
    bound, _ = bound_with(tmp_path, {"middlewares/b.py": hook},
                          {"hooks": [{"name": "b", "site": "before_tool", "entrypoint": "middlewares/b.py:run"}]})
    world = World()

    class TwoCalls:
        name, model = "scripted", "s"

        def __init__(self):
            self.n = 0

        def chat(self, system, messages, specs):
            self.n += 1
            if self.n == 1:
                return ChatTurn("", [ToolCall("a", "api_search", {"q": "r"}),
                                     ToolCall("b", "api_fetch", {"url": "u", "method": "POST"}),
                                     ToolCall("c", "api_search", {"q": "after"})], "tool_use")
            return ChatTurn("stop", [], "end")
    agent = agent_for(tmp_path, bound, world, TwoCalls())
    agent.run("do it")
    assert world.calls == [("api_search", {"q": "r"})]           # the prefix ran; the blocked call and the rest not
    tools = [e for e in transcript(tmp_path, agent) if e["kind"] == "tool"]
    assert [e.get("blocked") or e.get("skipped") or False for e in tools] == [False, True, True]


def test_pr6_11_an_after_tool_exception_is_a_defect_and_the_real_result_is_kept(tmp_path):
    hook = "def run(ctx, event):\n    raise ValueError('broken check')\n"
    bound, _ = bound_with(tmp_path, {"middlewares/a.py": hook},
                          {"hooks": [{"name": "a", "site": "after_tool", "entrypoint": "middlewares/a.py:run"}]})
    world = World()

    class One:
        name, model = "scripted", "s"

        def chat(self, system, messages, specs):
            return ChatTurn("", [ToolCall("a", "api_fetch", {"url": "u", "method": "POST"})], "tool_use")
    agent = agent_for(tmp_path, bound, world, One())
    with pytest.raises(PackageCallError, match="broken check"):
        agent.run("write it")
    assert world.calls == [("api_fetch", {"url": "u", "method": "POST"})]       # executed once, never resent


def test_pr6_11_repeated_on_finish_feedback_is_bounded_by_the_pushback_cap(tmp_path):
    hook = "def run(ctx, event):\n    return {'action': 'feedback', 'message': 'check again'}\n"
    bound, _ = bound_with(tmp_path, {"middlewares/f.py": hook},
                          {"hooks": [{"name": "f", "site": "on_finish", "entrypoint": "middlewares/f.py:run"}]})

    class Finisher:
        name, model = "scripted", "s"

        def chat(self, system, messages, specs):
            return ChatTurn("finished", [], "end")
    agent = agent_for(tmp_path, bound, World(), Finisher(), max_pushbacks=2)
    result = agent.run("anything")
    pushes = [e for e in transcript(tmp_path, agent) if e["kind"] == "pushback" and e.get("source") == "package"]
    assert len(pushes) == 2 and result.turns == 3


def test_an_illegal_hook_answer_is_a_defect_never_continue(tmp_path):
    hook = "def run(ctx, event):\n    return {'action': 'maybe'}\n"
    bound, _ = bound_with(tmp_path, {"middlewares/x.py": hook},
                          {"hooks": [{"name": "x", "site": "on_finish", "entrypoint": "middlewares/x.py:run"}]})

    class Finisher:
        name, model = "scripted", "s"

        def chat(self, system, messages, specs):
            return ChatTurn("finished", [], "end")
    with pytest.raises(PackageCallError, match="answered"):
        agent_for(tmp_path, bound, World(), Finisher()).run("anything")


# ---------------------------------------------------------------------------- PR6-12: the broker
def test_pr6_12_the_broker_refuses_unauthorised_tools_and_hook_writes():
    world = World()
    assert Broker(world)("read_skill", {})[1] is True                     # a package tool or host tool: refused
    assert Broker(world)("shell", {})[1] is True
    out, err = Broker(world, read_only=True)("api_fetch", {"url": "u", "method": "POST"})
    assert err and "read-only" in out and world.calls == []
    assert Broker(world, read_only=True)("api_search", {"q": "x"}) == ("api_search ok", False)


# ---------------------------------------------------------------------------- worker failures (EX6-13, IT6-24)
class Finisher:
    name, model = "scripted", "s"

    def chat(self, system, messages, specs):
        return ChatTurn("finished", [], "end")


def test_a_package_that_kills_its_worker_is_the_candidates_defect(tmp_path):
    """os._exit in a hook: a fresh worker then answers normally, so the package did it (M5 §1)."""
    hook = "import os\ndef run(ctx, event):\n    os._exit(3)\n"
    bound, _ = bound_with(tmp_path, {"middlewares/d.py": hook},
                          {"hooks": [{"name": "d", "site": "on_finish", "entrypoint": "middlewares/d.py:run"}]})
    with pytest.raises(PackageCallError, match="a fresh worker answers normally"):
        agent_for(tmp_path, bound, World(), Finisher()).run("anything")


def test_a_worker_that_dies_on_its_own_is_infrastructure_not_a_defect(tmp_path, monkeypatch):
    """The same death when a fresh worker does not answer either: nothing shows the package caused it."""
    hook = "import os\ndef run(ctx, event):\n    os._exit(3)\n"
    bound, _ = bound_with(tmp_path, {"middlewares/d.py": hook},
                          {"hooks": [{"name": "d", "site": "on_finish", "entrypoint": "middlewares/d.py:run"}]})
    monkeypatch.setattr(ProcessWorker, "healthy", lambda self: False)
    with pytest.raises(WorkerCrashed):
        agent_for(tmp_path, bound, World(), Finisher()).run("anything")
    assert not issubclass(WorkerCrashed, ct.CandidateDefect)


def test_a_package_tool_may_not_take_a_base_tools_name(tmp_path):
    tool = "def run(ctx, args):\n    return 'mine'\n"
    bound, _ = bound_with(tmp_path, {"tools/f.py": tool},
                          {"tools": [{"name": "api_fetch", "description": "shadow", "entrypoint": "tools/f.py:run",
                                      "input_schema": {"type": "object", "properties": {}}}]})
    with pytest.raises(PackageCallError, match="take the names of this host's tools"):
        hp.mount(World(), bound, worker=ProcessWorker(for_tests=True))
    with pytest.raises(hp.PackageError, match="the host's own tool"):
        hp.parse_manifest(json.dumps({**hp.empty_manifest(), "tools": [
            {"name": "read_skill", "description": "d", "entrypoint": "tools/f.py:run",
             "input_schema": {"type": "object"}}]}), {"tools/f.py": tool})


def test_the_draft_check_refuses_a_tool_named_like_a_base_tool(tmp_path):
    from ouroloop.research.drafts import Drafts
    objects = ct.Objects(tmp_path)
    parent = hp.build_package(objects, {"manifest.json": json.dumps(hp.empty_manifest())}, CODE)
    manifest = {**hp.empty_manifest(), "tools": [{"name": "api_search", "description": "d",
                                                  "entrypoint": "tools/s.py:run",
                                                  "input_schema": {"type": "object", "properties": {}}}]}
    drafts = Drafts(objects, parent_ref=parent, limits=CODE, worker=ProcessWorker(for_tests=True),
                    reserved_names={"api_search", "api_fetch"})
    errors, ref = drafts.check({"manifest.json": json.dumps(manifest),
                                "tools/s.py": "def run(ctx, args):\n    return 'x'\n"})
    assert ref is None and "take the names of the host's base tools" in errors[0]


def test_on_finish_does_not_push_back_on_the_last_turn(tmp_path):
    bound, _ = bound_with(tmp_path, {"middlewares/guard.py": FINISH_GUARD},
                          {"hooks": [{"name": "guard", "site": "on_finish", "entrypoint": "middlewares/guard.py:run"}]})
    agent = agent_for(tmp_path, bound, World(), Finisher(), max_turns=1)
    result = agent.run("post to #ops")
    assert result.stop == "end"                                 # finished, not cut off by a pushback
    assert not [e for e in transcript(tmp_path, agent) if e["kind"] == "hook"]


def test_a_package_tool_sees_the_episode_view_even_without_hooks(tmp_path):
    tool = "def run(ctx, args):\n    return 'task=' + str(ctx.view.get('task', ''))[:12]\n"
    bound, _ = bound_with(tmp_path, {"tools/v.py": tool},
                          {"tools": [{"name": "view_tool", "description": "reads the view",
                                      "input_schema": {"type": "object", "properties": {}},
                                      "entrypoint": "tools/v.py:run"}]})

    class Calls:
        name, model = "scripted", "s"

        def __init__(self):
            self.n = 0

        def chat(self, system, messages, specs):
            self.n += 1
            return ChatTurn("", [ToolCall("c1", "view_tool", {})], "tool_use") if self.n == 1 \
                else ChatTurn("done", [], "end")
    agent = agent_for(tmp_path, bound, World(), Calls())
    agent.run("find the record")
    event = [e for e in transcript(tmp_path, agent) if e["kind"] == "tool" and e["name"] == "view_tool"][0]
    assert event["output"] == "task=find the rec"


def session_with(files, **limits):
    from ouroloop.agent.worker import WorkerLimits
    worker = ProcessWorker(WorkerLimits(**limits), for_tests=True) if limits else ProcessWorker(for_tests=True)
    session = worker.start()
    session.load(files)
    return session


def test_sys_exit_in_package_code_is_the_packages_error_not_a_dead_worker():
    session = session_with({"tools/t.py": "import sys\ndef run(ctx, args):\n    sys.exit(1)\n"})
    try:
        with pytest.raises(PackageCallError, match="SystemExit"):
            session.request({"op": "call_tool", "entry": "tools/t.py:run", "args": {}, "view": {}})
        # and the session still answers afterwards
        assert session.request({"op": "probe", "entries": []}) == "ok"
    finally:
        session.close()


def test_a_package_that_hangs_is_stopped_at_the_limit_and_charged_to_the_package():
    session = session_with({"tools/t.py": "def run(ctx, args):\n    while True:\n        pass\n"},
                           request_seconds=1.0)
    with pytest.raises(PackageCallError, match="did not answer call_tool"):
        session.request({"op": "call_tool", "entry": "tools/t.py:run", "args": {}, "view": {}})
    assert session.proc.poll() is not None                  # the hung process is gone


def test_a_chatty_package_does_not_fill_a_pipe_and_hang():
    """stderr is drained as it comes: 400 KB of prints and a raw write to fd 1 do not block or corrupt anything."""
    tool = ("import os\ndef run(ctx, args):\n    for _ in range(4000):\n        print('x' * 100)\n"
            "    os.write(1, b'not protocol\\n')\n    return 'done'\n")
    session = session_with({"tools/t.py": tool}, request_seconds=10.0)
    try:
        value = session.request({"op": "call_tool", "entry": "tools/t.py:run", "args": {}, "view": {}})
        assert value == {"output": "done", "is_error": False}
        assert "not protocol" in session.stderr_tail()
    finally:
        session.close()


def test_package_tool_output_and_malformed_broker_calls_are_bounded():
    tool = ("def run(ctx, args):\n    bad = ctx.call_tool('api_search', [1])\n"
            "    return ('E' if bad.is_error else 'ok') + 'y' * 5_000_000\n")
    session = session_with({"tools/t.py": tool}, max_output_chars=100)
    try:
        value = session.request({"op": "call_tool", "entry": "tools/t.py:run", "args": {}, "view": {}},
                                broker=Broker(World()))
        assert value["output"] == "E" + "y" * 99            # the malformed call was answered as an error
    finally:
        session.close()


# ---------------------------------------------------------------------------- code drafts (RL6-18 code part)
def test_a_code_draft_is_probed_and_tested_in_the_worker_and_revised(tmp_path):
    from ouroloop.research.drafts import Drafts
    objects = ct.Objects(tmp_path)
    parent = hp.build_package(objects, {"manifest.json": json.dumps(hp.empty_manifest())}, CODE)
    manifest = {**hp.empty_manifest(),
                "hooks": [{"name": "g", "site": "on_finish", "entrypoint": "middlewares/g.py:run"}]}
    broken = {"write": {"manifest.json": json.dumps(manifest), "middlewares/g.py": "def other(ctx, e):\n    pass\n"}}
    fixed = {"write": {"manifest.json": json.dumps(manifest), "middlewares/g.py": FINISH_GUARD,
                       "tests/test_g.py": "def test_imports(ctx):\n    assert True\n"}}
    drafts = Drafts(objects, parent_ref=parent, limits=CODE, worker=ProcessWorker(for_tests=True), fixture=World)
    seen = []
    proposal = ct.ExperimentProposal(owner="worker", changes=({"target": "worker.harness", "kind": "workflow",
                                                                "change": {"patch": broken}},))
    out = drafts.resolve(proposal, revise=lambda prompt, patch, check: seen.append(check) or fixed, prompt="p")
    assert out.status == "ready" and "isolated_probe" in seen[0] and "no callable run" in seen[0]
    view = hp.load_package(objects, out.package_ref)
    assert view.runs_code and view.test_files                     # tests kept apart from the run bundle


def test_a_code_draft_without_a_worker_is_infrastructure_and_stays_open(tmp_path):
    from ouroloop.research.drafts import Drafts
    objects = ct.Objects(tmp_path)
    parent = hp.build_package(objects, {"manifest.json": json.dumps(hp.empty_manifest())}, CODE)
    manifest = {**hp.empty_manifest(),
                "hooks": [{"name": "g", "site": "on_finish", "entrypoint": "middlewares/g.py:run"}]}
    patch = {"write": {"manifest.json": json.dumps(manifest), "middlewares/g.py": FINISH_GUARD}}
    events = ct.Events(tmp_path)
    drafts = Drafts(objects, parent_ref=parent, limits=CODE, worker=None, events=events, scope="s")
    proposal = ct.ExperimentProposal(owner="worker", changes=({"target": "worker.harness", "kind": "workflow",
                                                                "change": {"patch": patch}},))
    out = drafts.resolve(proposal, revise=lambda *a: pytest.fail("no revision on infrastructure"), prompt="p")
    assert out.status == "infrastructure_failed"
    assert drafts.pending() is not None                           # resumed once the worker is back
    drafts.worker = ProcessWorker(for_tests=True)
    again = drafts.resolve(proposal, revise=lambda *a: pytest.fail("no revision needed"), prompt="p",
                           resume=drafts.pending())
    assert again.status == "ready" and drafts.pending() is None


# ---------------------------------------------------------------------------- IT6-22 code part, end to end
class GuardResearcher:
    """Proposes the on_finish guard as a package change."""

    def __init__(self):
        self.requests = []

    def note_request(self, summary, prompt, *, allowed=()):
        return prompt, json.dumps({"failure": summary}, sort_keys=True)

    def note(self, system, user):
        return {"failure_class": "missed_step", "root_cause": "finished early", "mechanism": "check on finish",
                "component": "on_finish", "generality": "multi-step requests"}

    def summarize(self, context, prompt, notes, coverage, experience=(), *, package=None, allowed=()):
        from ouroloop.research.experiment import ProposalResult
        manifest = {**hp.empty_manifest(),
                    "hooks": [{"name": "guard", "site": "on_finish", "entrypoint": "middlewares/guard.py:run"}]}
        patch = {"write": {"manifest.json": json.dumps(manifest), "middlewares/guard.py": FINISH_GUARD}}
        return ProposalResult(proposals=[ct.ExperimentProposal(
            owner="worker", hypothesis="a finish guard catches the missing message", origin="scripted",
            changes=({"target": "worker.harness", "kind": "workflow", "change": {"patch": patch}},))])

    def revise(self, prompt, patch, check_output):
        pytest.fail(f"no revision expected: {check_output}")


def test_it6_22_a_code_component_is_generated_checked_installed_and_changes_behaviour(fake_upstream, tmp_path,
                                                                                     monkeypatch):
    from test_harness_loop import v06_prepared
    monkeypatch.setattr(bench, "isolated_worker", lambda settings: ProcessWorker(for_tests=True) if settings else None)
    cfg, activity, w0 = v06_prepared(fake_upstream, tmp_path)
    # the same activity, with a core that allows the on_finish kind
    core = activity.objects.load(ct.SystemRelease.from_json(activity.objects.get(w0)).core_ref, ct.CoreManifest)
    harness = {**core.capabilities["harness"], "sandbox": "docker",
               "package": {**core.capabilities["harness"]["package"],
                           "components": ["memory", "skills", "on_finish"]}}
    core2 = activity.objects.put(ct.CoreManifest(**{**{f: getattr(core, f) for f in core.__dataclass_fields__},
                                                     "capabilities": {**core.capabilities, "harness": harness}}))
    rel = ct.SystemRelease.from_json(activity.objects.get(w0))
    release = activity.releases.bootstrap(rel.worker_ref, rel.research_ref, core2, reason="code allowed",
                                          activate=False, target_harness_ref=rel.target_harness_ref,
                                          required_capabilities=rel.required_capabilities)
    from ouroloop.ledger import write_json
    write_json(activity.releases.active_path, {"release_ref": release, "revision": 2, "last_operation_id": None})
    cfg["rsi"]["task_plan_ref"] = bench.freeze_task_plan(
        activity.objects, core_ref=core2, split_ref=activity.splits.hash(), task_ids=tuple(rsi.DEV),
        gate=__import__("ouroloop.evolve.task_gate", fromlist=["GatePlan"]).GatePlan(
            min_gain=0.05, min_groups=1, bootstrap_replicates=200))
    result = bench.improve(cfg, ".", out=lambda _line: None, researcher=GuardResearcher())
    assert "installed" in [r["outcome"] for r in result["rounds"]], result["rounds"]
    bound = P.bind_release(result["release"], profile="task", services=P.Services(activity.objects),
                           authorization="x")
    assert bound.package.runs_code and bound.package.manifest["hooks"][0]["site"] == "on_finish"
    hooks = [e for e in ct.Events(activity.root).all() if e.get("event") == "draft_check"]
    assert hooks and hooks[-1]["ok"]


# ---------------------------------------------------------------------------- IT6-23: isolation (needs docker)
@pytest.fixture
def qualified():
    """The Docker worker of DEFAULT_IMAGE. Isolation is not shown by any other test, so without docker or the image
    these tests are skipped, never passed; where CI has built the image (OUROLOOP_TEST_REQUIRE_DOCKER, set by
    .github/workflows/tests.yml) a missing worker fails them, so a broken build or a renamed tag is not a quiet skip."""
    worker = docker_worker()
    if worker is None:
        if os.environ.get("OUROLOOP_TEST_REQUIRE_DOCKER"):
            pytest.fail(f"OUROLOOP_TEST_REQUIRE_DOCKER is set and docker or the {DEFAULT_IMAGE} image is not available")
        pytest.skip(f"docker or the {DEFAULT_IMAGE} image is not available")
    return worker


def test_it6_23_package_code_in_the_docker_worker_cannot_reach_the_host(qualified):
    """What package code can see and do inside the qualified worker: no host file, no network, an environment of
    exactly PATH and LANG (the host's is not forwarded and the base image's is dropped), not root, a read-only
    root filesystem, and a small writable /tmp."""
    session = qualified.start()
    try:
        session.load({"tools/t.py": "import os, socket\n"
                                    "def run(ctx, args):\n"
                                    "    out = {'host_path': os.path.exists(args['path'])}\n"
                                    "    try:\n"
                                    "        socket.create_connection(('1.1.1.1', 53), timeout=2); out['net'] = True\n"
                                    "    except OSError:\n"
                                    "        out['net'] = False\n"
                                    "    out['env'] = sorted(os.environ)\n"
                                    "    out['proc_env'] = sorted(k.split(b'=')[0].decode() for k in\n"
                                    "                             open('/proc/self/environ', 'rb').read().split(b'\\0') if k)\n"
                                    "    out['uid'] = os.getuid()\n"
                                    "    try:\n"
                                    "        open('/runner/x', 'w').write('x'); out['root_writable'] = True\n"
                                    "    except OSError:\n"
                                    "        out['root_writable'] = False\n"
                                    "    open('/tmp/scratch', 'w').write('ok'); out['tmp'] = open('/tmp/scratch').read()\n"
                                    "    return __import__('json').dumps(out)\n"})
        value = session.request({"op": "call_tool", "entry": "tools/t.py:run",
                                 "args": {"path": str(__import__('pathlib').Path.cwd())}, "view": {}})
        seen = json.loads(value["output"])
        assert seen == {"host_path": False, "net": False, "env": ["LANG", "PATH"], "proc_env": ["LANG", "PATH"],
                        "uid": 65534, "root_writable": False, "tmp": "ok"}
    finally:
        session.close()


def test_a_hung_package_in_docker_is_its_defect_and_its_container_is_removed(qualified):
    import subprocess
    from ouroloop.agent.worker import WorkerLimits
    worker = qualified
    worker.limits = WorkerLimits(request_seconds=3.0)
    session = worker.start()
    name = worker._names[id(session)]
    session.load({"tools/t.py": "def run(ctx, args):\n    while True:\n        pass\n"})
    with pytest.raises(PackageCallError, match="did not answer call_tool"):
        session.request({"op": "call_tool", "entry": "tools/t.py:run", "args": {}, "view": {}})
    left = subprocess.run(["docker", "ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"],
                          capture_output=True, text=True).stdout.strip()
    assert left == ""                                           # the container itself is gone, not only its client


def test_the_qualified_worker_runs_the_image_k_froze_by_its_content_id(qualified):
    from ouroloop import bench
    image_id = qualified.image_id()
    worker = bench.isolated_worker({"sandbox": "docker", "image": DEFAULT_IMAGE, "image_id": image_id,
                                    "test_timeout_seconds": 7})
    assert worker.pinned_id == image_id and image_id in worker.command("n")
    assert worker.limits.request_seconds == 7.0
    assert worker.healthy()
    assert bench.isolated_worker({"sandbox": "docker", "image": DEFAULT_IMAGE,
                                  "image_id": "sha256:" + "0" * 64}) is None      # another image: not this activity's


# ---------------------------------------------------------------------------- v0.3: the run protocol (M1 §1 宿主作为运行壳, IT6-29/43)
AGENT = '''
def run(ctx, task):
    turn = ctx.model([{"role": "user", "content": task}], tools=[{"name": "api_search"}], role="act")
    call = turn.tool_calls[0]
    out = ctx.tool(call["name"], call["args"])
    return "final: " + turn.text + " / " + out.output + " / stop=" + str(turn.stop)
'''


class StubHost:
    """A host for the protocol tests, no Gateway yet: records every callback op in order and answers model_call
    with the canned turn given (or an error), base_call through the session's own handler on a World."""

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
        return self._base(reply)

    def skill_read(self, reply):
        self.seen.append(reply)
        return {"op": "skill_result", "id": reply["id"], "text": "the skill" if reply["name"] == "known" else None}

    @property
    def handlers(self):
        return {"model_call": self.model_call, "base_call": self.base_call, "skill_read": self.skill_read}


def run_message(entry="agent/main.py:run", task="do x", **extra):
    return {"op": "run", "entry": entry, "task": task, "roles": ["act"], "view": {"task": task}, "limits": {},
            "skills": {"index": [], "memory": None}, **extra}


def test_an_agent_run_returns_its_string_and_the_host_saw_one_model_call_then_one_base_call():
    """M1 §1: the worker asks the host for the model and the tool over the one pipe, in the agent's order; the
    agent's return value is the result (IT6-29 protocol part, no gateway yet)."""
    session = session_with({"agent/main.py": AGENT})
    try:
        host = StubHost(session)
        value = session.serve(run_message(), handlers=host.handlers, deadline=10.0)
        assert value == "final: ok / api_search ok / stop=tool_use"
        assert [m["op"] for m in host.seen] == ["model_call", "base_call"]
        assert host.seen[0]["id"] == "m1" and host.seen[0]["role"] == "act" and host.seen[0]["effort"] is None
        assert host.seen[0]["messages"] == [{"role": "user", "content": "do x"}]
        assert host.seen[0]["tools"] == [{"name": "api_search"}]
        assert host.seen[1]["id"] == "b1" and host.world.calls == [("api_search", {"q": "x"})]
    finally:
        session.close()


def test_a_gateway_error_is_raised_inside_the_agent_and_the_agent_may_report_it():
    """M0 §2: a model_result with is_error is a RuntimeError in the agent's hands, not the end of the run."""
    agent = ("def run(ctx, task):\n    try:\n        ctx.model([], role='aux')\n"
             "    except RuntimeError as e:\n        return 'refused: ' + str(e)\n    return 'unreachable'\n")
    session = session_with({"agent/main.py": agent})
    try:
        host = StubHost(session, error="role 'aux' is not declared")
        assert session.serve(run_message(), handlers=host.handlers,
                             deadline=10.0) == "refused: role 'aux' is not declared"
    finally:
        session.close()


def test_limits_and_view_follow_the_hosts_answers_and_skills_are_read_through_the_host():
    """M0 §2: ctx.limits is the latest snapshot the host attached, ctx.view is refreshed in place by an answer
    carrying one, ctx.skills.read goes to the host (None for a missing skill); ids are monotonic per kind."""
    agent = ("def run(ctx, task):\n    view = ctx.view\n    before = dict(ctx.limits)\n"
             "    ctx.model([], role='act')\n    ctx.model([], role='act')\n"
             "    return repr([before, ctx.limits, view.get('events'), ctx.skills.read('known'),"
             " ctx.skills.read('missing'), ctx.skills.index, ctx.skills.memory])\n")
    session = session_with({"agent/main.py": agent})
    try:
        host = StubHost(session, turn={"text": "", "tool_calls": [], "stop": "end"},
                        extra={"limits": {"act_calls": 48}, "view": {"task": "do x", "events": ["e1"]}})
        value = session.serve(run_message(limits={"act_calls": 50}, skills={"index": [{"name": "known",
                                                                                         "description": "d"}],
                                                                              "memory": "remember"}),
                              handlers=host.handlers, deadline=10.0)
        assert value == repr([{"act_calls": 50}, {"act_calls": 48}, ["e1"], "the skill", None,
                              [{"name": "known", "description": "d"}], "remember"])
        assert [m["id"] for m in host.seen] == ["m1", "m2", "k1", "k2"]
    finally:
        session.close()


def test_an_agent_that_raises_at_run_time_is_infrastructure_not_a_candidate_defect():
    """M1 §1 失败语义 / IT6-43: a run-time exception is AgentRunError, which is not a CandidateDefect; so is a
    return value that is not a string."""
    from ouroloop.agent.worker import AgentRunError
    assert not issubclass(AgentRunError, ct.CandidateDefect)
    session = session_with({"agent/a.py": "def run(ctx, task):\n    raise ValueError('boom')\n",
                            "agent/b.py": "def run(ctx, task):\n    return None\n"})
    try:
        with pytest.raises(AgentRunError, match="boom"):
            session.serve(run_message("agent/a.py:run"), handlers=StubHost(session).handlers, deadline=10.0)
        with pytest.raises(AgentRunError, match="returned NoneType, not a string"):
            session.serve(run_message("agent/b.py:run"), handlers=StubHost(session).handlers, deadline=10.0)
    finally:
        session.close()


def test_the_agent_entrypoint_must_be_a_function_named_run():
    """M0 §2: the agent's entrypoint names a callable `run`; another name is the package's error, before import."""
    from ouroloop.agent.worker import AgentRunError
    session = session_with({"agent/a.py": "def main(ctx, task):\n    return 'x'\n"})
    try:
        with pytest.raises(AgentRunError, match="must be a function named run"):
            session.serve(run_message("agent/a.py:main"), handlers=StubHost(session).handlers, deadline=10.0)
    finally:
        session.close()


def test_a_handler_that_hits_a_hard_limit_ends_serve_with_limit_hit_and_the_worker_is_gone():
    """M5 §2: a hard limit found by the host's handler ends the attempt there: LimitHit names the dimension and the
    session is closed by force, so nothing of the run goes on."""
    from ouroloop.agent.worker import LimitHit
    session = session_with({"agent/main.py": AGENT})
    host = StubHost(session)

    def over(reply):
        raise LimitHit("act_calls")
    with pytest.raises(LimitHit) as info:
        session.serve(run_message(), handlers={**host.handlers, "model_call": over}, deadline=10.0)
    assert info.value.limit == "act_calls" and "act_calls" in str(info.value)
    assert session.proc.poll() is not None
    assert not issubclass(LimitHit, ct.CandidateDefect)


def test_a_handler_that_fails_on_the_host_ends_the_session_and_its_error_is_raised_as_it_is():
    """serve(): a handler's own exception (a gateway or broker fault) is the host's error, re-raised unchanged; the
    worker, blocked inside its exchange for an answer that never comes, is closed with it, so the session is not
    left in a state no later request could use."""
    session = session_with({"agent/main.py": AGENT})
    host = StubHost(session)

    def broken(reply):
        raise RuntimeError("the gateway is down")
    with pytest.raises(RuntimeError, match="the gateway is down"):
        session.serve(run_message(), handlers={**host.handlers, "model_call": broken}, deadline=10.0)
    assert session.proc.poll() is not None


def test_a_reply_out_of_order_is_a_protocol_error_not_the_packages():
    """worker_runner exchange(): an answer with another id is a protocol error that `except Exception` in the agent
    does not catch; the host sees the worker breaking the protocol (infrastructure for a run), not the package
    raising."""
    agent = ("def run(ctx, task):\n    try:\n        ctx.model([], role='act')\n"
             "    except Exception as e:\n        return 'swallowed ' + type(e).__name__\n    return 'x'\n")
    session = session_with({"agent/main.py": agent})
    host = StubHost(session)

    def wrong_id(reply):
        return {**host.model_call(reply), "id": "m999"}
    with pytest.raises(WorkerCrashed, match="out of order"):
        session.serve(run_message(), handlers={**host.handlers, "model_call": wrong_id}, deadline=10.0)


def test_a_callback_the_host_has_no_handler_for_is_a_protocol_failure():
    """serve(): a callback op outside `handlers` is the worker speaking outside the protocol the host offered."""
    session = session_with({"agent/main.py": AGENT})
    with pytest.raises(WorkerCrashed, match="answered 'model_call'"):
        session.serve(run_message(), handlers={"base_call": session.base_call_handler()}, deadline=10.0)


def test_an_eight_megabyte_model_call_passes_the_reader_and_a_v1_line_is_still_bounded():
    """worker.py MAX_MODEL_LINE: a model_call carrying a full history of 7.5 MB reaches the host; every other op
    keeps the MAX_LINE bound, so a 1.5 MB base_call still ends the session as oversized."""
    from ouroloop.agent.worker import MAX_LINE, MAX_MODEL_LINE
    assert MAX_LINE < 1_500_000 < 7_500_000 < MAX_MODEL_LINE
    big = ("def run(ctx, task):\n    turn = ctx.model([{'role': 'user', 'content': 'x' * 7_500_000}], role='act')\n"
           "    return 'got ' + turn.text\n")
    session = session_with({"agent/main.py": big}, request_seconds=60.0)
    try:
        host = StubHost(session, turn={"text": "ok", "tool_calls": [], "stop": "end"})
        assert session.serve(run_message(), handlers=host.handlers, deadline=60.0) == "got ok"
        assert len(host.seen[0]["messages"][0]["content"]) == 7_500_000
    finally:
        session.close()
    wide = "def run(ctx, task):\n    return ctx.tool('api_search', {'q': 'y' * 1_500_000}).output\n"
    session = session_with({"agent/main.py": wide})
    with pytest.raises(WorkerCrashed, match="longer than 1000000"):
        session.serve(run_message(), handlers=StubHost(session).handlers, deadline=30.0)


def test_the_deadline_ends_a_hung_run_as_a_worker_crash_never_a_defect():
    """M1 §1 失败语义: `run` is not a bounded op, so a run the host's deadline ends is infrastructure (the limits
    are the host's to enforce), and the hung worker is gone."""
    session = session_with({"agent/main.py": "def run(ctx, task):\n    while True:\n        pass\n"})
    with pytest.raises(WorkerCrashed, match="did not answer run within 1.0s"):
        session.serve(run_message(), handlers=StubHost(session).handlers, deadline=1.0)
    assert session.proc.poll() is not None
    from ouroloop.agent.worker import PACKAGE_OPS
    assert "run" not in PACKAGE_OPS and "smoke" in PACKAGE_OPS


AGENT_MANIFEST = {**hp.empty_manifest(hp.AGENT_API_VERSION),
                  "agent": {"entrypoint": "agent/main.py:run", "roles": ["act"]}}


def test_the_smoke_runs_the_agent_against_a_canned_model_and_the_fixture_broker():
    """M0 §3 / check_package: with an `agent` entry the draft check smokes the agent — the canned end turn, tools
    through the fixture broker — and a clean agent gives no problems. Its ctx.limits are the remaining counts M0 §2
    defines, integers taken down by each call, with seconds the request's deadline."""
    smoke_agent = ("def run(ctx, task):\n    turn = ctx.model([{'role': 'user', 'content': task}], role='act')\n"
                   "    out = ctx.tool('api_search', {'q': task})\n    assert ctx.skills.read('any') is None\n"
                   "    counts = [ctx.limits[k] for k in ('act_calls', 'model_calls', 'tool_calls', 'tokens')]\n"
                   "    assert counts == [4, 9, 39, 19999], counts\n"
                   "    assert type(ctx.limits['seconds']) is int and 0 < ctx.limits['seconds'] <= 30\n"
                   "    return turn.text + '/' + str(turn.stop) + '/' + out.output\n")
    from ouroloop.agent.worker import check_package
    world = World()
    problems = check_package(ProcessWorker(for_tests=True), {"agent/main.py": smoke_agent}, AGENT_MANIFEST,
                             fixture=world)
    assert problems == [] and world.calls == [("api_search", {"q": "smoke"})]
    session = session_with({"agent/main.py": smoke_agent})
    try:
        assert session.request({"op": "smoke", "entry": "agent/main.py:run", "roles": ["act"], "task": "t"},
                               broker=Broker(World())) == "ok/end/api_search ok"
    finally:
        session.close()


def test_an_agent_that_loops_on_the_model_is_stopped_by_the_smokes_act_calls_limit():
    """M0 §2 / check_package: the smoke's limits are small integers, so an agent that never returns is found before
    any paid run — the sixth act call is refused, `except Exception` in the agent does not swallow the refusal, and
    the problem names the dimension; an agent that paces itself by ctx.limits finishes within them."""
    from ouroloop.agent.worker import check_package
    looping = "def run(ctx, task):\n    while True:\n        ctx.model([], role='act')\n"
    swallowing = ("def run(ctx, task):\n    try:\n        while True:\n            ctx.model([], role='act')\n"
                  "    except Exception as e:\n        return 'swallowed ' + type(e).__name__\n")
    stopped = "agent_smoke: the agent did not finish within the smoke's limits (act_calls)"
    for agent in (looping, swallowing):
        assert check_package(ProcessWorker(for_tests=True), {"agent/main.py": agent}, AGENT_MANIFEST,
                             fixture=World()) == [stopped]
    tools = "def run(ctx, task):\n    while True:\n        ctx.tool('api_search', {})\n"
    assert check_package(ProcessWorker(for_tests=True), {"agent/main.py": tools}, AGENT_MANIFEST,
                         fixture=World()) == ["agent_smoke: the agent did not finish within the smoke's limits "
                                              "(tool_calls)"]
    paced = ("def run(ctx, task):\n    n = 0\n    while ctx.limits['act_calls'] > 0:\n"
             "        ctx.model([], role='act')\n        n += 1\n    return str(n)\n")
    assert check_package(ProcessWorker(for_tests=True), {"agent/main.py": paced}, AGENT_MANIFEST,
                         fixture=World()) == []
    session = session_with({"agent/main.py": paced})
    try:
        assert session.request({"op": "smoke", "entry": "agent/main.py:run", "roles": ["act"], "task": "t"}) == "5"
    finally:
        session.close()


def test_an_undeclared_role_or_a_crash_in_the_smoke_is_a_draft_problem():
    """M0 §2/§3, IT6-43: a role the manifest does not declare and an exception in run are both caught by the smoke
    as `agent_smoke` problems of the draft — a defect, charged to the draft, never infrastructure."""
    from ouroloop.agent.worker import check_package
    aux = "def run(ctx, task):\n    return ctx.model([], role='aux').text\n"
    manifest = AGENT_MANIFEST
    problems = check_package(ProcessWorker(for_tests=True), {"agent/main.py": aux}, manifest, fixture=World())
    assert len(problems) == 1 and problems[0].startswith("agent_smoke: ") and "'aux' is not declared" in problems[0]
    crash = "def run(ctx, task):\n    raise KeyError('nope')\n"
    problems = check_package(ProcessWorker(for_tests=True), {"agent/main.py": crash}, manifest, fixture=World())
    assert problems == ["agent_smoke: KeyError: 'nope'"]
    session = session_with({"agent/main.py": aux})
    try:
        with pytest.raises(PackageCallError, match="not declared"):
            session.request({"op": "smoke", "entry": "agent/main.py:run", "roles": ["act"], "task": "t"})
    finally:
        session.close()


def test_the_smokes_canned_model_calls_each_offered_tool_once_with_the_smallest_arguments_then_ends():
    """worker_runner.SmokeContext (M0 §3): the first model call that offers tools is answered with one tool_use turn
    calling each offered tool — a placeholder for each required property of its schema, {} without one — and every
    later call with the end turn, so an agent's own tool path runs once against the fixture before any paid call."""
    from ouroloop.agent.worker_runner import minimal_args
    assert minimal_args({"type": "object", "properties": {"q": {"type": "string"}}}) == {}
    assert minimal_args({"type": "object", "required": ["q", "n", "on", "k"],
                         "properties": {"q": {"type": "string"}, "n": {"type": "integer"}, "on": {"type": "boolean"},
                                        "k": {"type": "string", "enum": ["a", "b"]}}}) == \
        {"q": "smoke", "n": 0, "on": False, "k": "a"}
    assert minimal_args(None) == {} and minimal_args({"required": ["x"]}) == {"x": "smoke"}
    touring = ("def run(ctx, task):\n"
               "    tools = [{'name': 'api_search', 'schema': {'type': 'object', 'required': ['q'],"
               " 'properties': {'q': {'type': 'string'}}}}, {'name': 'api_fetch', 'schema': {'type': 'object'}}]\n"
               "    first = ctx.model([{'role': 'user', 'content': task}], tools=tools)\n"
               "    for c in first.tool_calls:\n        ctx.tool(c['name'], c['args'])\n"
               "    second = ctx.model([{'role': 'user', 'content': task}], tools=tools)\n"
               "    return repr([first.stop, [(c['name'], c['args']) for c in first.tool_calls], second.stop,"
               " second.tool_calls])\n")
    session = session_with({"agent/main.py": touring})
    try:
        world = World()
        value = session.request({"op": "smoke", "entry": "agent/main.py:run", "roles": ["act"], "task": "t"},
                                broker=Broker(world))
        assert value == repr(["tool_use", [("api_search", {"q": "smoke"}), ("api_fetch", {})], "end", []])
        assert world.calls == [("api_search", {"q": "smoke"}), ("api_fetch", {})]
    finally:
        session.close()


def test_a_code_package_without_an_agent_entry_is_smoked_through_the_default_agent():
    """check_package (M0 §3, M1 §1 失败语义): every package that runs code is smoked — a v1 package through the
    default agent, added to the loaded files as the host adds it at run time — against a view carrying the fixture's
    base tools and the package's components, so its hooks and package tools each execute once: a sound package
    passes; a before_tool hook that raises, or answers outside its protocol, fails the smoke as the draft's failure
    (a revision, never infrastructure); a package tool that never stops calling the broker is stopped by the smoke's
    tool_calls limit; a text-only package and a tests-only one are not smoked."""
    from ouroloop.agent.worker import check_package
    hooks = {"hooks": [{"name": "g", "site": "on_finish", "entrypoint": "middlewares/g.py:run"},
                       {"name": "b", "site": "before_tool", "entrypoint": "middlewares/b.py:run"},
                       {"name": "a", "site": "after_tool", "entrypoint": "middlewares/a.py:run"}]}
    sound = {"middlewares/g.py": FINISH_GUARD,
             "middlewares/b.py": "def run(ctx, event):\n    r = ctx.call_tool('api_search', {'q': 'look'})\n"
                                 "    return {'action': 'continue'} if not r.is_error else {'action': 'block'}\n",
             "middlewares/a.py": "def run(ctx, event):\n    assert 'result' in event\n    return {'action': 'continue'}\n",
             "tests/test_g.py": "def test_ok(ctx):\n    assert True\n"}
    manifest = {**hp.empty_manifest(), **hooks}
    world = World()
    assert check_package(ProcessWorker(for_tests=True), sound, manifest, fixture=world) == []
    # the smoke called each of the fixture's tools once, and the before_tool hook's read ran before each
    assert [n for n, _ in world.calls] == ["api_search", "api_search", "api_search", "api_fetch"]
    raising = {**sound, "middlewares/b.py": "def run(ctx, event):\n    raise ValueError('broken check')\n"}
    assert check_package(ProcessWorker(for_tests=True), raising, manifest, fixture=World()) == \
        ["agent_smoke: ValueError: broken check"]
    illegal = {**sound, "middlewares/b.py": "def run(ctx, event):\n    return {'action': 'maybe'}\n"}
    (problem,) = check_package(ProcessWorker(for_tests=True), illegal, manifest, fixture=World())
    assert problem.startswith("agent_smoke: ValueError: hook b at before_tool answered {'action': 'maybe'}")
    writing = {**sound, "middlewares/b.py": "def run(ctx, event):\n"
                                           "    r = ctx.call_tool('api_fetch', {'url': 'u', 'method': 'POST'})\n"
                                           "    return {'action': 'block', 'message': r.output[:60]}\n"}
    world = World()
    assert check_package(ProcessWorker(for_tests=True), writing, manifest, fixture=world) == []
    assert world.calls == []                       # a hook's broker is read-only in the smoke as in an attempt
    looping_tool = {"tools/t.py": "def run(ctx, args):\n    while True:\n        ctx.call_tool('api_search', {})\n"}
    tool_manifest = {**hp.empty_manifest(), "tools": [{"name": "forever", "description": "d", "entrypoint": "tools/t.py:run",
                                                       "input_schema": {"type": "object", "properties": {}}}]}
    assert check_package(ProcessWorker(for_tests=True), looping_tool, tool_manifest, fixture=World()) == \
        ["agent_smoke: the agent did not finish within the smoke's limits (tool_calls)"]
    bad_tool = {"tools/t.py": "def run(ctx, args):\n    return 7\n"}
    assert check_package(ProcessWorker(for_tests=True), bad_tool, tool_manifest, fixture=World()) == \
        ["agent_smoke: TypeError: tool forever answered int, not a tool result"]
    tests_only = {"tests/test_g.py": "def test_ok(ctx):\n    assert True\n"}
    assert check_package(ProcessWorker(for_tests=True), tests_only, hp.empty_manifest(), fixture=World()) == []
    assert check_package(ProcessWorker(for_tests=True), sound, {**manifest, "agent": None}, fixture=World()) == []


# ---------------------------------------------------------------------------- v0.3: the runner shell (M1 §1 宿主作为运行壳, IT6-29/30/43, PR7)
from types import SimpleNamespace  # noqa: E402

from ouroloop.agent.gateway import Gateway  # noqa: E402
from ouroloop.agent.runner import PackageAgent  # noqa: E402
from ouroloop.agent.worker import LimitHit  # noqa: E402
from ouroloop.benchmark import Budget  # noqa: E402
from ouroloop.evolve.task_eval import Budgeted, make_agent_factory, run_attempt  # noqa: E402

FOUR = ("tool", "assistant", "pushback", "hook")
NOTIFY_TASK = "Move Dana Reyes to opportunity stage, then post to #ops with the phrase deal moved."


def shell_agent(tmp_path, bound, world, provider, *, budget=None, worker=None, placement=None, max_effort="high"):
    """The runner shell over the World: what task_eval builds for a release whose K was frozen under v0.3."""
    budget = budget or Budget(max_steps=8, max_tool_calls=12)
    surface = Budgeted(world, budget)
    make = make_agent_factory(rsi.runtime(tmp_path), provider, budget=budget, bound=bound,
                              worker=worker or ProcessWorker(for_tests=True), max_effort=max_effort)
    agent = make(surface, SimpleNamespace(task_id="t1"), "You are a business agent.")
    assert isinstance(agent, PackageAgent)
    if placement is not None:
        agent.placement = placement
    return agent


def kinds(events, only=FOUR):
    return [e["kind"] for e in events if e["kind"] in only]


def trace(events):
    """The (kind, name) sequence of a run as both loops record it: every model turn with its stop, every hook with
    its answer, every pushback, and every tool call by name. The two loops differ in two shapes and nowhere else: the
    v1 loop writes a blocked or skipped call as a `tool` event flagged blocked/skipped, the default agent as a
    `tool_blocked`/`tool_skipped` mark; and a package tool is one `tool` event in the v1 loop carrying its base calls,
    while under the shell the broker records the base calls themselves and the outer call is a `package_tool` mark —
    so a package tool's event is projected to the base calls it made, the sequence the broker saw in both."""
    out = []
    for e in events:
        kind = e["kind"]
        if kind == "tool":
            if e.get("blocked"):
                out.append(("tool_blocked", e["name"]))
            elif e.get("skipped"):
                out.append(("tool_skipped", e["name"]))
            elif e.get("broker_calls") is not None:
                out += [("tool", c["tool"]) for c in e["broker_calls"]]
            else:
                out.append(("tool", e["name"]))
        elif kind in ("tool_blocked", "tool_skipped"):
            out.append((kind, e["tool"]))
        elif kind == "hook":
            out.append(("hook", e["hook"], e["action"]))
        elif kind == "assistant":
            out.append(("assistant", e["stop"]))
        elif kind == "pushback":
            out.append(("pushback",))
    return out


def v2_bound(tmp_path, agent_code, roles=("act",), **manifest_extra):
    manifest = {"api_version": hp.AGENT_API_VERSION,
                "agent": {"entrypoint": "agent/main.py:run", "roles": list(roles)}, **manifest_extra}
    return bound_with(tmp_path, {"agent/main.py": agent_code}, manifest)


class Scripted:
    """A provider with the three-argument chat of the fixtures: the gateway must never hand it a keyword."""
    name, model = "scripted", "s"

    def __init__(self, turns, usage=None):
        self.turns, self.usage, self.n, self.seen = list(turns), usage or {}, 0, []

    def chat(self, system, messages, specs):
        self.seen.append((system, [dict(m) for m in messages], [s.name for s in specs]))
        turn = self.turns[min(self.n, len(self.turns) - 1)]
        self.n += 1
        return ChatTurn(turn.text, turn.tool_calls, turn.stop, usage=dict(self.usage))


class EffortAware(Scripted):
    """A provider whose chat takes `effort`, as the claude_code provider's does."""

    def __init__(self, turns, effort="medium"):
        super().__init__(turns)
        self.effort, self.efforts = effort, []

    def chat(self, system, messages, specs, effort=None):
        self.efforts.append(effort)
        return super().chat(system, messages, specs)


BLOCKER = ("def run(ctx, event):\n    if event['tool'] == 'api_fetch':\n"
           "        return {'action': 'block', 'message': 'read the record first'}\n    return {'action': 'continue'}\n")


def test_it6_29_a_v1_package_under_the_default_agent_makes_the_same_calls_as_the_host_loop(tmp_path):
    """IT6-29 / PR7-08 (M1 §1 默认 agent 即种子包): on the same World with the same scripted actor, the default agent
    under the runner shell issues the tool-call sequence of agent.loop.Agent, the same final message and turn count,
    and the same (kind, name) sequence of model turns, tool calls, hook answers, blocks, skips and pushbacks — a plain
    task, a package whose on_finish hook pushes back once, a package with a tool whose base call goes through the
    broker, and a package whose before_tool hook blocks a call and so skips the rest of the turn."""
    cases = {
        "plain": ({}, {}, rsi.BusinessWorker, NOTIFY_TASK),
        "on_finish": ({"middlewares/guard.py": FINISH_GUARD},
                      {"hooks": [{"name": "guard", "site": "on_finish", "entrypoint": "middlewares/guard.py:run"}]},
                      rsi.BusinessWorker, NOTIFY_TASK),
        "tool": ({"tools/paged.py": "def run(ctx, args):\n    r = ctx.call_tool('api_search', {'q': args.get('q', '')})\n"
                                    "    return 'paged: ' + r.output\n"},
                 {"tools": [{"name": "paged_search", "description": "search every page",
                             "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
                             "entrypoint": "tools/paged.py:run"}]},
                 lambda: Scripted([ChatTurn("", [ToolCall("c1", "paged_search", {"q": "x"})], "tool_use"),
                                   ChatTurn("done", [], "end")]), "find x"),
        "before_tool": ({"middlewares/b.py": BLOCKER},
                        {"hooks": [{"name": "b", "site": "before_tool", "entrypoint": "middlewares/b.py:run"}]},
                        lambda: Scripted([ChatTurn("", [ToolCall("a", "api_search", {"q": "r"}),
                                                        ToolCall("b", "api_fetch", {"url": "u", "method": "POST"}),
                                                        ToolCall("c", "api_search", {"q": "after"})], "tool_use"),
                                          ChatTurn("stop", [], "end")]), "do it"),
    }
    for name, (files, manifest, provider, task) in cases.items():
        old_world, new_world = World(), World()
        bound, _ = bound_with(tmp_path / name / "old", files, manifest)
        old = agent_for(tmp_path / name / "old", bound, old_world, provider())
        old_result = old.run(task)
        bound, _ = bound_with(tmp_path / name / "new", files, manifest)
        new = shell_agent(tmp_path / name / "new", bound, new_world, provider())
        new_result = new.run(task)
        assert new_world.calls == old_world.calls, name
        assert new_result.text == old_result.text and new_result.turns == old_result.turns, name
        old_events, new_events = transcript(tmp_path / name / "old", old), transcript(tmp_path / name / "new", new)
        assert trace(new_events) == trace(old_events), (name, trace(new_events), trace(old_events))
        if name != "before_tool":           # a block is the one event the two loops write under different kinds
            assert kinds(new_events) == kinds(old_events), name
        if name == "on_finish":
            assert new_world.calls == [("api_fetch", {"method": "PATCH", "url": rsi.HUBSPOT,
                                                      "body": '{"id": "c1", "stage": "opportunity"}'}),
                                       ("api_fetch", {"method": "POST", "url": rsi.SLACK,
                                                      "body": '{"channel": "C1", "text": "deal moved for the contact"}'})]
            pushes = [e for e in new_events if e["kind"] == "pushback"]
            assert len(pushes) == 1 and pushes[0]["source"] == "package"
            assert [e["action"] for e in new_events if e["kind"] == "hook"] == ["block", "continue"]
        if name == "tool":
            assert trace(new_events) == [("assistant", "tool_use"), ("tool", "api_search"), ("assistant", "end")]
            assert [e["tool"] for e in new_events if e["kind"] == "package_tool"] == ["paged_search"]
        if name == "before_tool":
            assert new_world.calls == [("api_search", {"q": "r"})]       # the prefix ran, the block and the rest not
            assert trace(new_events) == [("assistant", "tool_use"), ("hook", "b", "continue"), ("tool", "api_search"),
                                         ("hook", "b", "block"), ("tool_blocked", "api_fetch"),
                                         ("tool_skipped", "api_search"), ("assistant", "end")]
            assert [e["source"] for e in new_events if e["kind"] in ("tool_blocked", "tool_skipped")] == ["package"] * 2
        # the default agent of a package with code ran in the worker; the host wrote the session's cost
        assert (new.placement == "worker") == bool(files)
        assert bool([e for e in new_events if e["kind"] == "worker_session"]) == bool(files)


def test_it6_29_an_agent_component_runs_in_the_worker_and_every_model_and_tool_call_goes_through_the_host(tmp_path):
    """IT6-29 / PR7-02 / PR7-03 / PR7-12 (M0 §2 轨迹记录, gateway_call.v1 / broker_event.v1): a v2 package's agent runs
    in the worker; one ctx.model and one ctx.tool give exactly one `gateway_call` and one `broker_event`, host-written,
    each naming the attempt and the gateway line the pinned model's identity; the `assistant` event carries role,
    effort and the host's call id; the gateway's `tools` are names with schema digests and the full specs are one
    `tool_specs` line written once; the view's base tools are `base_tools`; what the package reports through
    ctx.mark under a host kind's name lands as a labelled `package_mark`; and the gateway's prefix/tail is the
    agent's own history, reconstructible."""
    agent = ("def run(ctx, task):\n"
             "    assert 'tools' not in ctx.view and [t['name'] for t in ctx.view['base_tools']] == ['api_search', 'api_fetch']\n"
             "    turn = ctx.model([{'role': 'system', 'content': ctx.view['system']}, {'role': 'user', 'content': task}],"
             " tools=[{'name': 'api_search', 'description': 's', 'schema': {'type': 'object'}}])\n"
             "    out = ctx.tool('api_search', {'q': turn.text})\n"
             "    ctx.mark('gateway_call', forged=True)\n"
             "    return 'final ' + out.output + ' ' + str(ctx.limits['act_calls'])\n")
    bound, _ = v2_bound(tmp_path, agent)
    world = World()
    provider = Scripted([ChatTurn("hello", [], "end")], usage={"input_tokens": 10, "output_tokens": 3})
    shell = shell_agent(tmp_path, bound, world, provider)
    shell.session_meta["attempt_id"] = "run/t1/0"                    # what run_attempt sets before run()
    result = shell.run("do x")
    assert shell.placement == "worker"
    assert result.text == "final api_search ok 7" and result.turns == 1 and result.stop == "end"
    events = transcript(tmp_path, shell)
    gateway = [e for e in events if e["kind"] == "gateway_call"]
    broker = [e for e in events if e["kind"] == "broker_event"]
    assert len(gateway) == 1 and len(broker) == 1 and world.calls == [("api_search", {"q": "hello"})]
    (assistant,) = [e for e in events if e["kind"] == "assistant"]
    assert assistant["role"] == "act" and assistant["effort"] is None and assistant["requested_effort"] is None
    assert assistant["call_id"].startswith(shell.session + ":") and assistant["call_id"] == gateway[0]["call_id"]
    assert assistant["usage"] == {"input_tokens": 10, "output_tokens": 3} and assistant["seq"] == 1
    assert gateway[0]["attempt_id"] == "run/t1/0" and broker[0]["attempt_id"] == "run/t1/0"
    assert gateway[0]["model_identity_ref"] is None                 # this fixture release pins no actor
    assert gateway[0]["messages_prefix"] == 0
    (spec,) = gateway[0]["tools"]
    assert spec["name"] == "api_search" and len(spec["schema_digest"]) == 16
    (specs,) = [e for e in events if e["kind"] == "tool_specs"]
    assert specs["attempt_id"] == "run/t1/0" and specs["specs"] == [{"name": "api_search", "description": "s",
                                                                      "schema": {"type": "object"}}]
    assert gateway[0]["messages_tail"] == [{"role": "system", "content": provider.seen[0][0]},
                                           {"role": "user", "content": "do x"}]
    assert gateway[0]["response"] == {"text": "hello", "tool_calls": [], "stop": "end"}
    assert broker[0]["after_call_id"] == gateway[0]["call_id"] and broker[0]["tool"] == "api_search"
    assert broker[0]["gate"] == {"decision": "run"} and broker[0]["output_chars"] == len("api_search ok")
    (forged,) = [e for e in events if e["kind"] == "package_mark"]
    assert forged["mark"] == "gateway_call" and forged["source"] == "package" and forged["forged"] is True
    assert "native" not in json.dumps(events)
    assert [e["kind"] for e in events if e["kind"] in ("tool", "skill_read")] == ["tool"]


def test_the_gateway_names_the_pinned_models_identity_and_writes_the_tool_specs_once_per_change(tmp_path):
    """M0 §2 gateway_call.v1: `model_identity_ref` is the bound release's actor identity (its content ref), and the
    tool list's full specs are written as `tool_specs` on the first call and again only when the list changes."""
    from ouroloop.agent.gateway import Gateway
    agent = ("def run(ctx, task):\n    a = [{'name': 'api_search', 'schema': {'type': 'object'}}]\n"
             "    b = a + [{'name': 'api_fetch', 'schema': {'type': 'object'}}]\n"
             "    for tools in (a, a, b, a):\n        ctx.model([{'role': 'user', 'content': task}], tools=tools)\n"
             "    return 'done'\n")
    bound, objects = v2_bound(tmp_path, agent)
    identity = ct.ModelIdentity(role="actor", provider="scripted", base_model="s", inference_ref=objects.put_json("change", {}))
    bound.actor_identity = identity
    shell = shell_agent(tmp_path, bound, World(), Scripted([ChatTurn("x", [], "end")]), budget=Budget(max_steps=8, max_tool_calls=4))
    assert shell.run("t").text == "done"
    events = transcript(tmp_path, shell)
    assert {e["model_identity_ref"] for e in events if e["kind"] == "gateway_call"} == {identity.ref()}
    specs = [[s["name"] for s in e["specs"]] for e in events if e["kind"] == "tool_specs"]
    assert specs == [["api_search"], ["api_search", "api_fetch"], ["api_search"]]
    digests = [[t["schema_digest"] for t in e["tools"]] for e in events if e["kind"] == "gateway_call"]
    assert digests[0] == digests[1] == digests[3] and len(digests[2]) == 2
    assert isinstance(shell.gateway, Gateway) and shell.gateway.model_identity_ref == identity.ref()


def test_it6_30_the_host_asks_no_decision_point_the_gateway_records_what_the_agent_sent_and_stop_is_return_or_a_hard_limit(tmp_path):
    """IT6-30 (M1 §1 网关 / 宿主不问决策): the gateway neither compacts nor rewrites the messages — an appending
    agent's second call is recorded as the prefix it shares with the first plus its tail, and the two lines rebuild
    the full history exactly as sent; the host asks no decision point (no decision ids, no decision or observation
    record, no stop question); and the attempt ends in two ways only: the agent's return is termination `finished`
    with the last act call's stop, and a hard limit is `budget` naming its dimension."""
    agent = ("def run(ctx, task):\n"
             "    messages = [{'role': 'user', 'content': task}]\n"
             "    first = ctx.model(messages)\n"
             "    messages.append({'role': 'assistant', 'text': first.text, 'tool_calls': []})\n"
             "    messages.append({'role': 'user', 'content': 'and then?'})\n"
             "    return ctx.model(messages).text\n")
    bound, _ = v2_bound(tmp_path, agent)
    shell = shell_agent(tmp_path, bound, World(), Scripted([ChatTurn("one", [], "end"), ChatTurn("two", [], "end")]))
    result = shell.run("q")
    assert (result.text, result.stop, result.limit) == ("two", "end", None)
    events = transcript(tmp_path, shell)
    gateway = [e for e in events if e["kind"] == "gateway_call"]
    assert [g["messages_prefix"] for g in gateway] == [0, 1]
    full = gateway[0]["messages_tail"][:gateway[1]["messages_prefix"]] + gateway[1]["messages_tail"]
    assert full == [{"role": "user", "content": "q"}, {"role": "assistant", "text": "one", "tool_calls": []},
                    {"role": "user", "content": "and then?"}]
    assert shell.decision_ids() == [] and shell.harness()["stop"] == {"point": "return_or_limit", "max_pushbacks": 2}
    assert not [e for e in events if e["kind"] in ("decision", "observation", "stop")]
    assert events[-1]["kind"] == "end" and events[-1]["stop"] == "end" and "limit" not in events[-1]
    # the same agent under one act call: the second call is refused before the model, the attempt is `budget`
    bound, _ = v2_bound(tmp_path / "limit", agent)
    provider = Scripted([ChatTurn("one", [], "end"), ChatTurn("two", [], "end")])
    shell = shell_agent(tmp_path / "limit", bound, World(), provider, budget=Budget(max_steps=1, max_tool_calls=4))
    result = shell.run("q")
    assert (result.text, result.stop, result.limit) == ("", "act_calls", "act_calls") and provider.n == 1
    assert transcript(tmp_path / "limit", shell)[-1]["limit"] == "act_calls"


def test_effort_above_max_effort_is_capped_on_the_record_and_the_request_is_kept(tmp_path):
    """PR7-04 (M0 §2): `effort="max"` under K's max_effort "high" is sent at "high" and recorded as effort "high",
    requested_effort "max"; a call with no request runs at the provider's own tier; a provider whose chat has no
    `effort` parameter is never handed one."""
    agent = ("def run(ctx, task):\n    a = ctx.model([{'role': 'user', 'content': task}], effort='max')\n"
             "    b = ctx.model([{'role': 'user', 'content': task}])\n    return a.text + b.text\n")
    bound, _ = v2_bound(tmp_path, agent)
    provider = EffortAware([ChatTurn("a", [], "end"), ChatTurn("b", [], "end")], effort="medium")
    shell = shell_agent(tmp_path, bound, World(), provider, max_effort="high")
    assert shell.run("t").text == "ab" and provider.efforts == ["high", None]
    assistants = [e for e in transcript(tmp_path, shell) if e["kind"] == "assistant"]
    assert [(e["effort"], e["requested_effort"]) for e in assistants] == [("high", "max"), ("medium", None)]
    gateway = [e for e in transcript(tmp_path, shell) if e["kind"] == "gateway_call"]
    assert [(e["effort"], e["requested_effort"]) for e in gateway] == [("high", "max"), ("medium", None)]
    # the three-argument fixture provider: no keyword, the request still on the record
    bound, _ = v2_bound(tmp_path / "plain", agent)
    plain = Scripted([ChatTurn("a", [], "end"), ChatTurn("b", [], "end")])
    shell = shell_agent(tmp_path / "plain", bound, World(), plain, max_effort="high")
    assert shell.run("t").text == "ab"
    assistants = [e for e in transcript(tmp_path / "plain", shell) if e["kind"] == "assistant"]
    assert [(e["effort"], e["requested_effort"]) for e in assistants] == [("high", "max"), (None, None)]


def test_an_undeclared_role_is_refused_by_the_gateway_without_a_model_call_and_recorded(tmp_path):
    """PR7-04 (M1 §1 网关): a role the manifest does not declare is answered as an error the agent may catch, the
    model is not called, and the host records the refusal as a protocol event."""
    agent = ("def run(ctx, task):\n    try:\n        ctx.model([{'role': 'user', 'content': task}], role='aux')\n"
             "    except RuntimeError as e:\n        return 'refused: ' + str(e)\n    return 'unreachable'\n")
    bound, _ = v2_bound(tmp_path, agent, roles=("act",))
    provider = Scripted([ChatTurn("never", [], "end")])
    shell = shell_agent(tmp_path, bound, World(), provider)
    result = shell.run("t")
    assert result.text.startswith("refused: role 'aux' is not declared") and provider.n == 0
    events = transcript(tmp_path, shell)
    assert [e["kind"] for e in events if e["kind"] in ("protocol", "assistant", "gateway_call")] == ["protocol"]


class Capturing(ProcessWorker):
    """ProcessWorker that keeps the sessions it started, so a test can see the worker is gone."""

    def __init__(self):
        super().__init__(for_tests=True)
        self.sessions = []

    def start(self):
        session = super().start()
        self.sessions.append(session)
        return session


def test_each_hard_limit_ends_the_attempt_as_budget_and_names_itself_and_the_worker_is_gone(tmp_path):
    """PR7-05 / EX6-16 (M0 §2 单次尝试硬上限, M5 §2): an agent looping on ctx.model under max_steps 2 ends with
    limit act_calls (the third act call is refused before the model is called), one looping on aux calls under
    max_model_calls 3 with limit model_calls, one whose replies cross max_tokens with limit tokens; the final
    message is empty, the worker session is closed, and the TaskResult of such an attempt is a valid `budget`."""
    looping = "def run(ctx, task):\n    while True:\n        ctx.model([{'role': 'user', 'content': task}])\n"
    aux = ("def run(ctx, task):\n    ctx.model([{'role': 'user', 'content': task}])\n"
           "    while True:\n        ctx.model([{'role': 'user', 'content': task}], role='aux')\n")
    cases = [("act", looping, ("act",), Budget(max_steps=2, max_tool_calls=12), {}, "act_calls", 2),
             ("model", aux, ("act", "aux"), Budget(max_steps=50, max_tool_calls=12, max_model_calls=3), {},
              "model_calls", 3),
             ("tokens", looping, ("act",), Budget(max_steps=50, max_tool_calls=12, max_tokens=150),
              {"output_tokens": 100}, "tokens", 2)]
    for name, code, roles, budget, usage, limit, calls in cases:
        bound, _ = v2_bound(tmp_path / name, code, roles=roles)
        provider = Scripted([ChatTurn("x", [], "end")], usage=usage)
        worker = Capturing()
        shell = shell_agent(tmp_path / name, bound, World(), provider, budget=budget, worker=worker)
        result = shell.run("t")
        assert (result.text, result.limit, result.stop) == ("", limit, limit), name
        assert provider.n == calls, (name, provider.n)              # the refused call never reached the model
        assert all(s.proc.poll() is not None for s in worker.sessions), name
        events = transcript(tmp_path / name, shell)
        assert events[-2]["kind"] == "worker_session"                 # the session closed before the attempt's end
        assert events[-1] == {**events[-1], "kind": "end", "stop": limit, "limit": limit}
        assert len([e for e in events if e["kind"] == "gateway_call"]) == calls


def test_it6_43_a_crash_at_run_time_is_infrastructure_a_crash_in_the_draft_check_is_a_draft_failure_and_a_hard_limit_is_budget(fake_upstream, tmp_path):
    """IT6-43 / PR7-06 (M1 §1 失败语义): through run_attempt, an agent that loops ends as termination `budget` with
    `limit` act_calls — a valid attempt, scored, never `step_limit` — and an agent that raises in `run` is an invalid
    attempt of failure_class infrastructure, not candidate_defect; the same crashing code in the draft check is an
    `agent_smoke` problem of the draft (a revision, PackageCallError's CandidateDefect kind), and the three never
    stand in for one another."""
    from ouroloop.agent.worker import AgentRunError, check_package
    suite, splits = rsi.suite(fake_upstream), rsi.splits(rsi.suite(fake_upstream))
    looping = "def run(ctx, task):\n    while True:\n        ctx.model([{'role': 'user', 'content': task}])\n"
    crashing = "def run(ctx, task):\n    raise KeyError('nope')\n"
    for name, code in (("loop", looping), ("crash", crashing)):
        bound, objects = v2_bound(tmp_path / name, code)
        manifest = rsi.run_manifest("c", "evolved", "train", budget=Budget(max_steps=2, max_tool_calls=12),
                                    data=splits.hash())
        factory = make_agent_factory(rsi.runtime(tmp_path / name), Scripted([ChatTurn("x", [], "end")]),
                                     budget=manifest.budget, bound=bound, worker=ProcessWorker(for_tests=True),
                                     max_effort="high")
        attempt = run_attempt(suite, "rsi.update_stage", factory, run=manifest, splits=splits)
        try:
            r = attempt.result
            if name == "loop":
                assert (r.validity, r.termination, r.limit, r.stop) == ("valid", "budget", "act_calls", "act_calls")
                assert r.steps == 2 and r.success is False and r.failure_class == "task_outcome"
            else:
                assert (r.validity, r.termination, r.failure_class) == ("invalid", "harness_error", "infrastructure")
                assert "KeyError" in r.invalid_reason and r.limit is None
        finally:
            if attempt.episode is not None:
                attempt.episode.close()
    # the same crash in the draft check: the draft's failure, charged to the draft, never infrastructure
    assert check_package(ProcessWorker(for_tests=True), {"agent/main.py": crashing}, AGENT_MANIFEST,
                         fixture=World()) == ["agent_smoke: KeyError: 'nope'"]
    assert issubclass(PackageCallError, ct.CandidateDefect) and not issubclass(AgentRunError, ct.CandidateDefect)
    assert not issubclass(LimitHit, (ct.CandidateDefect, AgentRunError, WorkerCrashed))


def test_the_tool_calls_and_seconds_limits_end_the_attempt_through_the_shell_and_the_refusal_is_on_record(tmp_path):
    """PR7-05 (M0 §2 单次尝试硬上限, M5 §2) for the two limits the surface counts: an agent looping on ctx.tool under
    max_tool_calls 3 ends with limit tool_calls after the surface refuses the fourth call — recorded as a broker event
    whose gate says refused for budget, not run — and an agent that outlives max_seconds ends with limit seconds,
    whether the gateway finds the clock past the limit or the host's deadline ends the worker; the world keeps what
    ran before."""
    import time
    tools = "def run(ctx, task):\n    while True:\n        ctx.tool('api_search', {'q': 'again'})\n"
    bound, _ = v2_bound(tmp_path / "tools", tools)
    world = World()
    shell = shell_agent(tmp_path / "tools", bound, world, Scripted([ChatTurn("x", [], "end")]),
                        budget=Budget(max_steps=8, max_tool_calls=3))
    result = shell.run("t")
    assert (result.text, result.limit, result.stop) == ("", "tool_calls", "tool_calls")
    assert world.calls == [("api_search", {"q": "again"})] * 3           # the fourth never reached the world
    events = transcript(tmp_path / "tools", shell)
    broker = [e for e in events if e["kind"] == "broker_event"]
    assert [b["gate"]["decision"] for b in broker] == ["run"] * 3 + ["refused"]
    assert broker[-1]["gate"] == {"decision": "refused", "reason": "budget", "limit": "tool_calls"} and broker[-1]["is_error"]
    assert events[-1]["limit"] == "tool_calls"
    slow = ("import time\ndef run(ctx, task):\n    ctx.tool('api_search', {'q': 'first'})\n    time.sleep(2.5)\n"
            "    ctx.model([{'role': 'user', 'content': task}])\n    return 'late'\n")
    bound, _ = v2_bound(tmp_path / "seconds", slow)
    world, provider = World(), Scripted([ChatTurn("x", [], "end")])
    shell = shell_agent(tmp_path / "seconds", bound, world, provider,
                        budget=Budget(max_steps=8, max_tool_calls=12, max_seconds=1.0))
    t0 = time.monotonic()
    result = shell.run("t")
    assert (result.text, result.limit, result.stop) == ("", "seconds", "seconds")
    assert provider.n == 0 and world.calls == [("api_search", {"q": "first"})] and time.monotonic() - t0 < 10
    assert transcript(tmp_path / "seconds", shell)[-1]["limit"] == "seconds"


def test_effort_reaches_the_provider_through_the_meter_and_the_metered_call_is_booked(tmp_path):
    """PR7-04 (M0 §2 / M5 §2): in the bench the gateway's provider is a MeteredProvider; the per-call `effort` passes
    through it to the provider that takes it, already capped at K's max_effort, and every call the gateway makes is
    one reserved and settled metered call."""
    from ouroloop.evolve.execution import Budgets, MeteredProvider, Pricing
    agent = ("def run(ctx, task):\n    a = ctx.model([{'role': 'user', 'content': task}], effort='max')\n"
             "    b = ctx.model([{'role': 'user', 'content': task}], effort='low')\n"
             "    c = ctx.model([{'role': 'user', 'content': task}])\n    return a.text + b.text + c.text\n")
    bound, _ = v2_bound(tmp_path, agent)
    inner = EffortAware([ChatTurn("a", [], "end"), ChatTurn("b", [], "end"), ChatTurn("c", [], "end")], effort="medium")
    account = Budgets(tmp_path / "budgets").account("run", {"usd": 10.0, "model_calls": 10})
    metered = MeteredProvider(inner, account, Pricing(0.003, 0.015, 4000, 1000, source="snapshot"), max_retries=0)
    shell = shell_agent(tmp_path, bound, World(), metered, max_effort="high")
    assert shell.run("t").text == "abc"
    assert inner.efforts == ["high", "low", None] and metered.calls == 3
    assert [r.status for r in metered.receipts] == ["settled"] * 3
    assert account.remaining()["used"]["model_calls"] == 3.0
    gateway = [e for e in transcript(tmp_path, shell) if e["kind"] == "gateway_call"]
    assert [(e["effort"], e["requested_effort"]) for e in gateway] == [("high", "max"), ("low", "low"), ("medium", None)]


def test_the_v02_w_star_shape_runs_under_the_default_agent_in_the_worker(tmp_path):
    """PR7-08 / IT6-29 (M0 §1: a v1 package is a v2 package with the default agent): memory.md plus an on_finish
    middleware, a v1 manifest, runs in the worker with the default agent added to the loaded bundle; the hook's
    block is a pushback, the message the request asked for is sent, and the package's ref is what it was."""
    files = {"memory.md": "Check the whole request before finishing.", "middlewares/guard.py": FINISH_GUARD}
    manifest = {"memory": {"enabled": True},
                "hooks": [{"name": "guard", "site": "on_finish", "entrypoint": "middlewares/guard.py:run"}]}
    bound, objects = bound_with(tmp_path, files, manifest)
    assert bound.package.api_version == "harness_package.v1" and bound.package.agent is None
    ref_before = bound.package.ref
    world = World()
    shell = shell_agent(tmp_path, bound, world, rsi.BusinessWorker())
    assert shell.placement == "worker" and shell.entry == "agent/default_agent.py:run"
    result = shell.run(NOTIFY_TASK)
    assert [a.get("url") for _, a in world.calls] == [rsi.HUBSPOT, rsi.SLACK]
    assert result.text.startswith("Done") and result.turns == 4       # update, finish (pushed back), post, finish
    events = transcript(tmp_path, shell)
    assert [e["action"] for e in events if e["kind"] == "hook"] == ["block", "continue"]
    assert [e["source"] for e in events if e["kind"] == "pushback"] == ["package"]
    assert "Check the whole request" in [e for e in events if e["kind"] == "gateway_call"][0]["messages_tail"][0]["content"]
    assert bound.package.ref == ref_before and "agent/default_agent.py" not in bound.package.run_files
    assert bound.activity.to_json()["executed"]["worker.harness"] == {"hook:guard": 2}


def test_a_text_only_v1_package_runs_in_process_and_its_records_match_the_worker_placement(tmp_path):
    """PR7-07 (M1 §1 默认 agent 即种子包): a package of memory and a skill runs the default agent in the host process
    over the same gateway and broker; forced into the worker, the same package produces the same record kinds in the
    same order (ids, times and the worker's own session cost aside), and the skill read is a host-written event."""
    files = {"memory.md": "notes", "skills/check.md": rsi.FINISH_CHECK_MARK + " before you finish."}
    manifest = {"memory": {"enabled": True}, "skills": [{"name": "check", "path": "skills/check.md",
                                                         "description": "the finish check"}]}
    runs = {}
    for placement in ("host", "worker"):
        bound, _ = bound_with(tmp_path / placement, files, manifest)
        world = World()
        shell = shell_agent(tmp_path / placement, bound, world, rsi.BusinessWorker(), placement=placement)
        if placement == "host":
            assert shell.placement == "host"            # the package's own choice: no code, no worker
        result = shell.run(NOTIFY_TASK)
        assert [a.get("url") for _, a in world.calls] == [rsi.HUBSPOT, rsi.SLACK], placement
        runs[placement] = (result, [e for e in transcript(tmp_path / placement, shell)
                                    if e["kind"] != "worker_session"])
    host, worker = runs["host"], runs["worker"]
    assert host[0].text == worker[0].text and host[0].turns == worker[0].turns == 4
    assert [e["kind"] for e in host[1]] == [e["kind"] for e in worker[1]]
    reads = [e for e in host[1] if e["kind"] == "skill_read"]
    assert reads == [{**reads[0], "name": "check", "found": True}]
    tools = [e for e in host[1] if e["kind"] == "tool"]
    assert tools[0]["name"] == "read_skill" and tools[0]["output"].startswith(rsi.FINISH_CHECK_MARK)
    assert [e["kind"] for e in host[1]].count("worker_session") == 0
    assert any(e["kind"] == "worker_session" for e in transcript(tmp_path / "worker", shell))


def test_a_hooks_broker_calls_are_read_only_under_the_default_agent(tmp_path):
    """PR6-12 under the runner shell (M1 §1 broker): a before_tool hook that tries to write through its ctx is refused
    by the broker, the refusal is on the host's record, and the world is unchanged by it."""
    hook = ("def run(ctx, event):\n    r = ctx.call_tool('api_fetch', {'url': 'u', 'method': 'POST'})\n"
            "    return {'action': 'feedback', 'message': 'hook saw: ' + r.output[:40]}\n")
    bound, _ = bound_with(tmp_path, {"middlewares/w.py": hook},
                          {"hooks": [{"name": "w", "site": "before_tool", "entrypoint": "middlewares/w.py:run"}]})
    world = World()
    provider = Scripted([ChatTurn("", [ToolCall("a", "api_search", {"q": "r"})], "tool_use"), ChatTurn("done", [], "end")])
    shell = shell_agent(tmp_path, bound, world, provider)
    assert shell.run("look").text == "done" and world.calls == []        # the hook blocked the call; its write never ran
    events = transcript(tmp_path, shell)
    (hook_event,) = [e for e in events if e["kind"] == "hook"]
    assert hook_event["action"] == "feedback" and hook_event["broker_calls"] == [{"tool": "api_fetch", "is_error": True}]
    (refused,) = [e for e in events if e["kind"] == "broker_event"]
    assert refused["gate"] == {"decision": "refused", "reason": "writes"} and refused["is_error"]
    assert provider.seen[1][1][-1]["results"][0]["output"].startswith("Stopped before execution: hook saw: a hook may only")


def test_the_gateway_pre_checks_the_act_limit_before_calling_the_provider():
    """M5 §2: the limit is checked before the call, so the refused call costs nothing; the snapshot counts down."""
    calls = []

    class P:
        name, model = "p", "p"

        def chat(self, system, messages, specs):
            calls.append(messages)
            return ChatTurn("ok", [], "end", usage={"output_tokens": 5})
    records = []
    gw = Gateway(P(), roles=("act",), max_effort="high", budget=Budget(max_steps=1, max_tool_calls=3, max_tokens=50),
                 record=lambda kind, data: records.append(kind))
    snapshot = gw.limits()
    assert snapshot.pop("seconds") in (899, 900)                      # whole seconds left, floored
    assert snapshot == {"act_calls": 1, "model_calls": None, "tool_calls": None, "tokens": 50}
    reply = gw.model([{"role": "user", "content": "x"}], [], "act")
    assert reply["limits"]["act_calls"] == 0 and reply["limits"]["tokens"] == 45 and len(calls) == 1
    with pytest.raises(LimitHit) as info:
        gw.model([{"role": "user", "content": "x"}], [], "act")
    assert info.value.limit == "act_calls" and len(calls) == 1
    assert records == ["assistant", "tool_specs", "gateway_call"]      # the tool list once, with its first call


def test_a_v2_package_binds_under_the_v2_builtin_and_its_release_names_the_capability(tmp_path):
    """M0 §1 (v0.3): the worker.harness built-in is the package's own API, so default_worker_set and the draft
    check choose v1/v2 from the manifest; a candidate release pinning a v2 package names `harness_package.v2` in
    required_capabilities, and a v2 package declared as v1 is refused at binding."""
    from ouroloop.release import Releases, required_capabilities_of
    objects = ct.Objects(tmp_path)
    v1 = hp.build_package(objects, {"manifest.json": json.dumps(hp.empty_manifest())}, CODE)
    v2 = package(objects, {"agent/main.py": AGENT}, {"api_version": hp.AGENT_API_VERSION,
                                                       "agent": {"entrypoint": "agent/main.py:run", "roles": ["act"]}})
    assert P.package_builtin(objects, v1) == "harness_package.v1" and P.package_builtin(objects, v2) == "harness_package.v2"
    w1 = P.default_worker_set(objects, finish_check=rsi.W0_FINISH, package_ref=v1)
    w2 = P.default_worker_set(objects, finish_check=rsi.W0_FINISH, package_ref=v2)
    assert required_capabilities_of(objects, ("generation", "harness_package.v1"), w1) == ("generation", "harness_package.v1")
    assert required_capabilities_of(objects, ("generation", "harness_package.v1"), w2) == \
        ("generation", "harness_package.v1", "harness_package.v2")
    assert required_capabilities_of(objects, None, w2) is None                 # a v2 release declares none
    targets = (*P.targets_of("worker"), "worker.harness", *P.targets_of("research"))
    releases, rel, objects = rsi.workspace(tmp_path, worker_ref=w1, targets=targets)
    parent = ct.SystemRelease.from_json(objects.get(rel))
    booted = releases.bootstrap(w1, parent.research_ref, parent.core_ref, activate=False,
                                required_capabilities=("generation", "harness_package.v1"))
    candidate = releases.candidate_release(booted, w2, permit_ref=objects.put_json("change", {"permit": 1}))
    assert "harness_package.v2" in ct.SystemRelease.from_json(objects.get(candidate)).required_capabilities
    assert isinstance(releases, Releases)
    mislabelled = P.policy_set(objects, "worker", [
        *(objects.load(r, ct.PolicyDescriptor) for r in objects.load(w1, ct.PolicySet).policies
          if objects.load(r, ct.PolicyDescriptor).target != "worker.harness"),
        P.descriptor(objects, "worker.harness", "workflow", {"builtin": "harness_package.v1", "package_ref": v2})])
    bad = releases.bootstrap(mislabelled, parent.research_ref, parent.core_ref, activate=False)
    with pytest.raises(P.BindError, match="the built-in is the package's own API"):
        P.bind_release(bad, profile="task", services=P.Services(objects))


# ---------------------------------------------------------------------------- v0.3 slice C review: the host's checks on what it is handed
def test_a_malformed_model_call_is_answered_as_an_error_the_agent_may_catch_and_never_raised_on_the_host(tmp_path):
    """gateway.malformed (M1 §1 网关, as base_call_handler already does for a tool call): a model call whose tools or
    messages are not the protocol's shapes — a tool given as a bare string, a message that is not an object, a tool
    entry without results — is answered {is_error, error, limits} and recorded as a protocol event; the model is not
    called, the attempt is not infrastructure, and the agent goes on."""
    agent = ("def run(ctx, task):\n    seen = []\n"
             "    for messages, tools in (([{'role': 'user', 'content': task}], ['api_search']),"
             " (['hello'], []), ([{'role': 'tool'}], []),"
             " ([{'role': 'assistant', 'text': '', 'tool_calls': [{'name': 'x', 'args': 'no'}]}], []),"
             " ([{'role': 'user', 'content': task}], [{'name': 'api_search', 'schema': 'no'}])):\n"
             "        try:\n            ctx.model(messages, tools=tools)\n            seen.append('called')\n"
             "        except RuntimeError as e:\n            seen.append(str(e)[:40])\n"
             "    return ' | '.join(seen) + ' | ' + ctx.model([{'role': 'user', 'content': task}]).text\n")
    bound, _ = v2_bound(tmp_path, agent)
    provider = Scripted([ChatTurn("fine", [], "end")])
    shell = shell_agent(tmp_path, bound, World(), provider)
    result = shell.run("t")
    assert result.text.endswith(" | fine") and "called" not in result.text and provider.n == 1
    assert result.text.count("malformed model call: ") == 5
    events = transcript(tmp_path, shell)
    protocol = [e for e in events if e["kind"] == "protocol"]
    assert len(protocol) == 5 and all(e["error"].startswith("malformed model call: ") for e in protocol)
    assert protocol[0]["error"] == "malformed model call: tools[0] is an object with a string name, not str"
    assert len([e for e in events if e["kind"] == "gateway_call"]) == 1
    # in-process the same call is the same error, raised in the agent's hands as ModelCallError
    from ouroloop.agent.gateway import Gateway, HostContext, ModelCallError
    gw = Gateway(provider, roles=("act",), max_effort="high", budget=Budget(), record=lambda k, d: None)
    assert gw.model([{"role": "user", "content": "x"}], ["api_search"])["is_error"]
    assert gw.model(None, [])["is_error"] and gw.model([{"content": "no role"}], [])["is_error"]
    assert gw.model([{"role": "assistant", "tool_calls": "nope"}], [])["is_error"]
    assert gw.model([{"role": "tool", "results": [1]}], [])["is_error"]
    ctx = HostContext(gw, broker=None, skills=None, marks=None, view={})
    with pytest.raises(ModelCallError, match="malformed model call"):
        ctx.model([{"role": "user", "content": "x"}], ["api_search"])


def test_pr7_09_ctx_has_the_five_names_and_mark_and_nothing_else():
    """PR7-09 (M0 §2 包 API v2: ctx 的五个名字加 ctx.mark): both contexts a package can be handed — HostContext in the
    host process, AgentContext in the worker — expose model, tool, skills, limits, view and mark and no other public
    name. HostContext slots them and keeps the host objects in closures, so any other attribute is an AttributeError
    to read and to set, and the gateway, broker, provider or surface behind it are not reachable through it."""
    from ouroloop.agent.gateway import AttemptBroker, HostContext, Marks, SkillReader
    from ouroloop.agent.worker_runner import AgentContext
    six = {"model", "tool", "skills", "limits", "view", "mark"}
    assert set(HostContext.__slots__) == six
    worker_ctx = AgentContext({"task": "t"}, {"act_calls": 1}, {})       # built without a pipe: nothing is asked
    assert {n for n in dir(worker_ctx) if not n.startswith("_")} == six
    quiet = lambda kind, data: None  # noqa: E731
    gw = Gateway(Scripted([ChatTurn("x", [], "end")]), roles=("act",), max_effort="high", budget=Budget(), record=quiet)
    broker = AttemptBroker(Budgeted(World(), Budget()), record=quiet, gateway=gw)
    ctx = HostContext(gw, broker, SkillReader(None, record=quiet), Marks(quiet), {"task": "t"})
    assert {n for n in dir(ctx) if not n.startswith("_")} == six
    for name in ("gateway", "broker", "provider", "rt", "surface", "episode", "objects", "record"):
        with pytest.raises(AttributeError):
            getattr(ctx, name)
    with pytest.raises(AttributeError):
        ctx.extra = 1
    assert ctx.model([{"role": "user", "content": "hi"}], role="act").text == "x" and ctx.limits["act_calls"] == 49


def test_a_mark_cannot_set_the_records_own_fields(tmp_path):
    """gateway.Marks (M0 §2 记录由宿主写): a package's mark data is stripped of `kind`, `ts`, `source` and `mark`
    before it is written — a forged `kind` or `ts` would otherwise overwrite the host's in the line, since _emit
    spreads the data last — so a hook mark with a forged time and label lands as a `hook` line with the host's time
    and `source: "package"`, a mark of an unknown kind carrying `mark` keeps the kind it asked for, and a `kind` in
    the data (reachable through the protocol, not the API) never reaches the record."""
    agent = ("def run(ctx, task):\n"
             "    ctx.mark('hook', hook='h', site='on_finish', action='continue', ts='1999', source='host')\n"
             "    ctx.mark('note', mark='assistant', ts=0, text='mine')\n"
             "    ctx.mark('pushback', reason='r', source='host')\n    return 'ok'\n")
    bound, _ = v2_bound(tmp_path, agent)
    shell = shell_agent(tmp_path, bound, World(), Scripted([ChatTurn("x", [], "end")]))
    assert shell.run("t").text == "ok"
    events = transcript(tmp_path, shell)
    # `agent` is the host's line naming the entry and its placement (M1 §4), written before the package's first mark
    assert [e["kind"] for e in events] == ["task", "agent", "hook", "package_mark", "pushback", "worker_session", "end"]
    assert events[1] == {**events[1], "entry": "agent/main.py:run", "placement": "worker", "roles": ["act"]}
    assert [e["source"] for e in events if e["kind"] in ("hook", "package_mark", "pushback")] == ["package"] * 3
    (hook,) = [e for e in events if e["kind"] == "hook"]
    assert hook == {**hook, "hook": "h", "site": "on_finish", "action": "continue", "source": "package"}
    assert hook["ts"] != "1999" and hook["ts"].endswith("Z")
    (note,) = [e for e in events if e["kind"] == "package_mark"]
    assert note["mark"] == "note" and note["text"] == "mine" and note["source"] == "package" and note["ts"] != 0
    assert "forged" not in json.dumps(events) and json.dumps(events).count('"kind": "gateway_call"') == 0
    from ouroloop.agent.gateway import Marks
    records = []
    Marks(lambda k, d: records.append((k, d))).mark("package_tool", {"tool": "t", "kind": "assistant", "mark": "x"})
    assert records == [("package_tool", {"tool": "t", "source": "package"})]


def test_the_worker_is_handed_only_the_events_after_a_tool_call_and_its_view_stays_whole(tmp_path):
    """gateway.AttemptBroker.handler / worker_runner AgentContext._ask: a base_result carries the limits and the one
    view key a tool call changes, `events`, which the worker merges into its view in place; the task, the base tools
    and the package stay as `run` gave them, and a reference the agent keeps is current."""
    agent = ("def run(ctx, task):\n    view = ctx.view\n    before = sorted(view)\n"
             "    ctx.tool('api_search', {'q': 'a'})\n    ctx.tool('api_search', {'q': 'b'})\n"
             "    return repr([before == sorted(ctx.view), view is ctx.view, [e['args'] for e in view['events']],"
             " view['task'], [t['name'] for t in view['base_tools']], ctx.view['max_output_chars']])\n")
    bound, _ = v2_bound(tmp_path, agent)
    from ouroloop.agent.worker import WorkerLimits
    worker = ProcessWorker(WorkerLimits(max_output_chars=777), for_tests=True)
    shell = shell_agent(tmp_path, bound, World(), Scripted([ChatTurn("x", [], "end")]), worker=worker)
    assert shell.run("the task").text == repr([True, True, [{"q": "a"}, {"q": "b"}], "the task",
                                                ["api_search", "api_fetch"], 777])
    # on the host side the base_result answer itself carries events only
    from ouroloop.agent.gateway import AttemptBroker, Gateway
    gw = Gateway(Scripted([]), roles=("act",), max_effort="high", budget=Budget(), record=lambda k, d: None)
    view = {"task": "t", "base_tools": [], "events": []}
    broker = AttemptBroker(World(), record=lambda k, d: None, gateway=gw, view=view)
    session = ProcessWorker(for_tests=True).start()
    try:
        answer = broker.handler(session)({"op": "base_call", "id": "b1", "name": "api_search", "args": {"q": "x"}})
    finally:
        session.close(force=True)
    assert set(answer) == {"op", "id", "output", "is_error", "limits", "view"} and set(answer["view"]) == {"events"}
    assert answer["view"]["events"][0]["name"] == "api_search"


def test_a_package_tools_output_is_clipped_at_ks_max_output_chars(tmp_path):
    """default_agent._execute (M1 §1 broker): a package tool's output has the same bound as a base tool's —
    WorkerLimits.max_output_chars, handed to the agent in ctx.view — not a constant of the agent's."""
    from ouroloop.agent.worker import WorkerLimits
    tool = "def run(ctx, args):\n    return 'y' * 5000\n"
    manifest = {"tools": [{"name": "long", "description": "d", "entrypoint": "tools/l.py:run",
                           "input_schema": {"type": "object", "properties": {}}}]}
    provider = Scripted([ChatTurn("", [ToolCall("c1", "long", {})], "tool_use"), ChatTurn("done", [], "end")])
    bound, _ = bound_with(tmp_path, {"tools/l.py": tool}, manifest)
    worker = ProcessWorker(WorkerLimits(max_output_chars=120), for_tests=True)
    shell = shell_agent(tmp_path, bound, World(), provider, worker=worker)
    assert shell.run("t").text == "done"
    (result_turn,) = [m for m in provider.seen[1][1] if m.get("role") == "tool"]
    assert result_turn["results"][0]["output"] == "y" * 120
    (mark,) = [e for e in transcript(tmp_path, shell) if e["kind"] == "package_tool"]
    assert mark["tool"] == "long" and len(mark["output"]) == 120


def test_the_failure_summary_reads_a_blocked_turn_from_the_gateway_log_and_not_the_hooks_own_reads(tmp_path):
    """research.diagnosis.failure_summary (M4 §1) under the runner shell: a turn's results are what the model saw,
    read from the gateway log — the executed call's output, the blocked call's "Stopped before execution", the
    skipped call's "Not executed" — while the before_tool hook's own read through the broker is a `tool` event the
    model never saw and is not attributed to it; the hook's answer and the block are events labelled the package's
    report; and the error list names the blocked and skipped calls."""
    from ouroloop.research import diagnosis as dg
    hook = ("def run(ctx, event):\n    ctx.call_tool('api_search', {'q': 'hook-read'})\n"
            "    if event['tool'] == 'api_fetch':\n        return {'action': 'block', 'message': 'read the record first'}\n"
            "    return {'action': 'continue'}\n")
    bound, _ = bound_with(tmp_path, {"middlewares/b.py": hook},
                          {"hooks": [{"name": "b", "site": "before_tool", "entrypoint": "middlewares/b.py:run"}]})
    provider = Scripted([ChatTurn("", [ToolCall("a", "api_search", {"q": "r"}),
                                       ToolCall("b", "api_fetch", {"url": "u", "method": "POST"}),
                                       ToolCall("c", "api_search", {"q": "after"})], "tool_use"),
                         ChatTurn("I stopped.", [], "end")])
    world = World()
    shell = shell_agent(tmp_path, bound, world, provider)
    assert shell.run("do it").text == "I stopped."
    events = transcript(tmp_path, shell)
    assert [e["name"] for e in events if e["kind"] == "tool"] == ["api_search", "api_search", "api_search"]  # 2 hook reads + 1

    class Row:
        task_id = family_id = "t1"
        split, success, validity, termination, partial_credit, evidence, attempt_id = \
            "train", False, "valid", "finished", 0.0, "", "a/t1"
    s = dg.failure_summary(Row(), events, [], dg.DiagnosisLimits())
    assert len(s["turns"]) == 2 and [a["tool"] for a in s["turns"][0]["actions"]] == ["api_search", "api_fetch", "api_search"]
    results = s["turns"][0]["results"]
    assert [(r["tool"], r["error"]) for r in results] == [("api_search", False), ("api_fetch", True), ("api_search", True)]
    assert results[0]["output"] == "api_search ok"
    assert results[1]["output"] == "Stopped before execution: read the record first"
    assert results[2]["output"].startswith("Not executed: an earlier call in this turn was stopped")
    assert "hook-read" not in json.dumps(s) and "results" not in s["turns"][1]
    assert [(e["turn"], e["tool"]) for e in s["tool_errors"]] == [(1, "api_fetch"), (1, "api_search")]
    assert [(e["event"], e.get("action") or e.get("tool")) for e in s["events"]] == \
        [("hook", "continue"), ("hook", "block"), ("tool_blocked", "api_fetch"), ("tool_skipped", "api_search")]
    assert all(e["source"] == "package" for e in s["events"])
    assert s["turns"][1]["text"] == "I stopped."
    # an older transcript without gateway records keeps the tool-event path
    old = [e for e in events if e["kind"] not in ("gateway_call", "tool_specs")]
    assert len(dg.failure_summary(Row(), old, [], dg.DiagnosisLimits())["turns"][0]["results"]) == 3


def test_a_worker_running_another_runner_is_refused_at_load_and_named_at_startup(tmp_path, monkeypatch):
    """worker.StaleWorker (M1 §1 默认 agent 即种子包: the host and the image carry the same runner bytes): a worker
    whose runner is other bytes than this host's worker_runner.py is refused at its first `load` with the command
    that rebuilds the image, is not healthy, and is named by bench.worker_problems / config_harness_problems /
    harness_checks before anything is paid; this host's own runner passes and names itself and its ops."""
    import sys
    from ouroloop.agent.worker import RUNNER, StaleWorker, WorkerUnavailable, runner_digest
    other = tmp_path / "worker_runner.py"
    other.write_text(RUNNER.read_text() + "\n# built from another runner\n")

    class Other(ProcessWorker):
        qualified = True                      # as a Docker image built from an older runner would say

        def command(self):
            return [sys.executable, "-I", str(other)]

        def describe(self):
            return {**super().describe(), "image": "ouroloop-worker:stale"}
    worker = Other(for_tests=True)
    session = worker.start()
    with pytest.raises(StaleWorker, match="rebuild the image: docker build -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .") as info:
        session.load({})
    assert session.proc.poll() is not None and issubclass(StaleWorker, WorkerUnavailable)
    assert runner_digest()[:12] in str(info.value) and "another runner" in str(info.value)
    assert worker.healthy() is False and "rebuild the image" in worker.runner_problem()
    assert bench.worker_problems(worker) == [f"the isolated worker ouroloop-worker:stale cannot serve this host: {worker.runner_problem()}"]
    # a run and a draft check in it are refused the same way, before any package code runs
    bound, _ = v2_bound(tmp_path, AGENT)
    with pytest.raises(StaleWorker):
        shell_agent(tmp_path, bound, World(), Scripted([ChatTurn("x", [], "end")]), worker=worker).run("t")
    from ouroloop.agent.worker import check_package
    with pytest.raises(StaleWorker):
        check_package(worker, {"agent/main.py": AGENT}, AGENT_MANIFEST, fixture=World())
    # startup: the config check and the activity check both name it when K allows code
    monkeypatch.setattr(bench, "isolated_worker", lambda settings: worker if settings else None)
    cfg = {"harness_evolution": {"components": ["memory", "skills", "on_finish"], "sandbox": "docker"}}
    (problem,) = bench.config_harness_problems(cfg)
    assert "cannot serve this host" in problem and "rebuild the image" in problem
    assert bench.config_harness_problems({"harness_evolution": {"components": ["memory", "skills"], "sandbox": "docker"}}) == []
    # this host's runner names itself, and the host accepts it
    fine = ProcessWorker(for_tests=True)
    assert fine.healthy() and fine.runner_problem() is None
    session = fine.start()
    try:
        identity = session.load({})
        assert identity["runner_sha256"] == runner_digest() and "run" in identity["ops"] and "smoke" in identity["ops"]
    finally:
        session.close()


def test_the_default_agent_runs_a_v1_package_through_run_in_the_qualified_worker(qualified, tmp_path):
    """IT6-29 / PR7-01 / PR7-08 in the real image (docker/worker.Dockerfile): a v1 package with an on_finish hook runs
    under the runner shell in the Docker worker — the default agent added to its loaded bundle, one `run` for the
    attempt, every model call through the gateway and every tool call through the broker — and does what it does in
    the ProcessWorker tests: the hook's block is a pushback, the message the request asked for is sent, the worker
    session's cost is on record, and the image's runner is this host's."""
    files = {"memory.md": "Check the whole request before finishing.", "middlewares/guard.py": FINISH_GUARD}
    manifest = {"memory": {"enabled": True},
                "hooks": [{"name": "guard", "site": "on_finish", "entrypoint": "middlewares/guard.py:run"}]}
    bound, _ = bound_with(tmp_path, files, manifest)
    assert qualified.runner_problem() is None
    world = World()
    shell = shell_agent(tmp_path, bound, world, rsi.BusinessWorker(), worker=qualified)
    assert shell.placement == "worker" and shell.entry == "agent/default_agent.py:run"
    result = shell.run(NOTIFY_TASK)
    assert [a.get("url") for _, a in world.calls] == [rsi.HUBSPOT, rsi.SLACK]
    assert result.text.startswith("Done") and result.turns == 4 and result.limit is None
    events = transcript(tmp_path, shell)
    assert [e["action"] for e in events if e["kind"] == "hook"] == ["block", "continue"]
    assert [e["source"] for e in events if e["kind"] == "pushback"] == ["package"]
    (session,) = [e for e in events if e["kind"] == "worker_session"]
    assert session["worker"] == "DockerWorker" and session["requests"] >= 6
    assert len([e for e in events if e["kind"] == "gateway_call"]) == 4
    assert len([e for e in events if e["kind"] == "broker_event"]) == 2
    # the same package through the draft check in the image: the default agent is smoked there too
    from ouroloop.agent.worker import check_package
    assert check_package(qualified, files, {**hp.empty_manifest(), **manifest}, fixture=World()) == []


# ---------------------------------------------------------------------------- v0.3 step 3: the session's /work tree
def work_session(package=None, inputs=None):
    session = ProcessWorker(for_tests=True).start()
    session.load({})
    answer = session.work("init", package=package or {"manifest.json": "{}", "memory.md": "old\n",
                                                      "skills/a.md": "A\n"},
                          inputs=inputs or {"notes.json": "[]"})
    assert answer == {"ok": True, "value": {"ok": True}}
    return session


def test_the_work_tree_seeds_a_candidate_from_the_package_and_writes_under_candidates_only():
    """M4 §1 (v0.3), worker_runner `work`: `init` lays out package/, inputs/ and candidates/; the first write under
    candidates/<dir>/ seeds <dir> with the package's files; list/read see the whole tree; write/edit/delete refuse
    package/, inputs/, the tree's root, a traversal and a bare directory; `export` hands back one candidate's files;
    an unknown action or dir is the tool's error, never a worker failure."""
    session = work_session()
    try:
        assert session.work("list")["value"] == ["inputs/notes.json", "package/manifest.json", "package/memory.md",
                                                 "package/skills/a.md"]
        assert session.work("read", path="package/memory.md")["value"] == "old\n"
        written = session.work("write", path="candidates/c1/memory.md", text="new\n")
        assert written["ok"] and written["value"]["seeded"] is True and written["value"]["written"] == 4
        assert session.work("export", dir="c1")["value"] == {"manifest.json": "{}", "memory.md": "new\n",
                                                            "skills/a.md": "A\n"}
        again = session.work("write", path="candidates/c1/skills/b.md", text="B\n")
        assert again["value"]["seeded"] is False
        edited = session.work("edit", path="candidates/c1/skills/a.md", old="A", new="AA")
        assert edited["ok"] and session.work("read", path="candidates/c1/skills/a.md")["value"] == "AA\n"
        twice = session.work("edit", path="candidates/c1/skills/a.md", old="A", new="x")
        assert not twice["ok"] and "occurs 2 times" in twice["error"]
        assert session.work("delete", path="candidates/c1/memory.md")["ok"]
        assert session.work("list", path="candidates/c1")["value"] == ["manifest.json", "skills/a.md", "skills/b.md"]
        for bad in ("package/memory.md", "inputs/notes.json", "memory.md", "candidates/c1", "candidates/../x",
                    "/etc/passwd", "candidates/c 1/x.md", "candidates/c1/../../x"):
            answer = session.work("write", path=bad, text="x")
            assert not answer["ok"], bad
        assert session.work("read", path="package/memory.md")["value"] == "old\n"      # untouched
        assert not session.work("read", path="candidates/none/x.md")["ok"]
        assert not session.work("export", dir="none")["ok"] and not session.work("export", dir="../c1")["ok"]
        assert "unknown work action" in session.work("squash")["error"]
        assert not session.work("edit", path="candidates/c1/skills/a.md", old="", new="x")["ok"]
        assert not session.work("write", path="candidates/c1/skills/a.md", text=None)["ok"]
        # a second init starts a fresh tree: nothing of the first remains
        session.work("init", package={"manifest.json": "{}"}, inputs={})
        assert session.work("list")["value"] == ["package/manifest.json"]
    finally:
        session.close()


def test_a_work_action_is_never_a_package_error_and_runs_no_code():
    """M1 §1: the tree op runs no package code — a Python file written to a candidate is text to it — so a failure
    in it is the worker's (WorkerCrashed), not a CandidateDefect, and `work` is not among PACKAGE_OPS."""
    from ouroloop.agent.worker import PACKAGE_OPS, WorkerCrashed
    assert "work" not in PACKAGE_OPS
    session = work_session()
    try:
        code = "import os\nos.system('echo pwned')\nraise SystemExit(3)\n"
        assert session.work("write", path="candidates/c1/tools/t.py", text=code)["ok"]
        assert session.work("read", path="candidates/c1/tools/t.py")["value"] == code
        assert session.proc.poll() is None                                   # the worker is alive: nothing ran
        session.proc.kill()
        session.proc.wait(timeout=5)
        with pytest.raises(WorkerCrashed):
            session.work("list")
    finally:
        session.close(force=True)


def test_a_filesystem_failure_inside_a_work_action_is_the_tools_error_and_the_worker_lives():
    """M4 §1 (2026-10-05 review): a write onto a directory, a write whose parent segment is a file, and a text that
    cannot be encoded each come back as the tool's `ok: false` — never as a kind "package" error that would end the
    round as a candidate defect — and the worker answers the next action."""
    session = work_session()
    try:
        assert session.work("write", path="candidates/c1/skills/b.md", text="B")["ok"]       # seeds c1 (skills/a.md)
        onto_dir = session.work("write", path="candidates/c1/skills", text="x")
        assert not onto_dir["ok"] and "is a directory" in onto_dir["error"]
        under_file = session.work("write", path="candidates/c1/memory.md/x.md", text="x")
        assert not under_file["ok"] and "is a file, not a directory" in under_file["error"]
        edited_under_file = session.work("edit", path="candidates/c1/memory.md/x.md", old="a", new="b")
        assert not edited_under_file["ok"]
        bad_text = session.work("write", path="candidates/c1/bad.md", text="\ud800")
        assert not bad_text["ok"] and bad_text["error"].startswith("UnicodeEncodeError")
        assert session.proc.poll() is None
        assert session.work("read", path="candidates/c1/memory.md")["value"] == "old\n"
        assert "bad.md" not in session.work("list", path="candidates/c1")["value"]
    finally:
        session.close()


def test_the_work_tree_refuses_a_write_past_ks_byte_caps_and_every_answer_fits_a_protocol_line():
    """M4 §1 (2026-10-05 review, rechecked): `init` carries K's max_package_bytes as the candidate directory's cap and
    a per-file cap; a write or edit that would cross either is refused before the disk is touched. The protocol line
    is a separate bound, measured escaped: with byte caps far above it, a write whose directory would take more than
    LINE_BUDGET characters once json.dumps escapes it (an emoji is 12, a control character 6) is refused too, so the
    export of every legal candidate fits one line; and an answer that would not fit is never sent."""
    from ouroloop.agent.worker import MAX_LINE
    from ouroloop.agent.worker_runner import LINE_BUDGET
    session = ProcessWorker(for_tests=True).start()
    session.load({})
    assert session.work("init", package={"manifest.json": "{}"}, inputs={}, max_bytes=100, max_file_bytes=60)["ok"]
    try:
        big = session.work("write", path="candidates/c1/a.md", text="x" * 61)
        assert not big["ok"] and "a file of a candidate may hold at most 60 bytes" in big["error"]
        assert session.work("write", path="candidates/c1/a.md", text="x" * 50)["ok"]
        assert session.work("write", path="candidates/c1/b.md", text="y" * 40)["ok"]       # 2 + 50 + 40 = 92
        over = session.work("write", path="candidates/c1/c.md", text="z" * 20)             # 112 > 100
        assert not over["ok"] and "may hold at most 100 bytes in all" in over["error"]
        grown = session.work("edit", path="candidates/c1/b.md", old="y" * 40, new="y" * 55)   # 2 + 50 + 55 = 107
        assert not grown["ok"] and "in all" in grown["error"]
        assert session.proc.poll() is None
        # byte caps of 10 MB: only the escaped line bounds what follows
        assert session.work("init", package={"manifest.json": "{}"}, inputs={}, max_bytes=10_000_000,
                            max_file_bytes=10_000_000)["ok"]
        emoji = session.work("write", path="candidates/c1/e.md", text="\U0001F600" * 80_000)   # 320 KB, 960k escaped
        assert not emoji["ok"] and "on the protocol line once escaped" in emoji["error"]
        controls = session.work("write", path="candidates/c1/c.md", text="\x01" * 160_000)    # 160 KB, 960k escaped
        assert not controls["ok"] and "on the protocol line once escaped" in controls["error"]
        assert session.work("write", path="candidates/c1/e.md", text="\U0001F600" * 60_000)["ok"]   # 720k escaped
        more = session.work("write", path="candidates/c1/f.md", text="\x02" * 40_000)          # + 240k: over
        assert not more["ok"] and session.proc.poll() is None
        exported = session.work("export", dir="c1")
        assert exported["ok"] and exported["value"]["e.md"] == "\U0001F600" * 60_000
        assert len(json.dumps({"op": "result", "value": exported})) < LINE_BUDGET < MAX_LINE
        page = session.work("read", path="candidates/c1/e.md")
        assert page["ok"] and len(json.dumps({"op": "result", "value": page})) < LINE_BUDGET
        # a package seeded past the line (the host's own files, never refused at init): the write that seeds it is
        # refused, and the export that would not fit answers the protocol's refusal instead of an oversized line
        assert session.work("init", package={"manifest.json": "{}", "memory.md": "\U0001F600" * 100_000}, inputs={},
                            max_bytes=10_000_000, max_file_bytes=10_000_000)["ok"]
        assert not session.work("write", path="candidates/c2/x.md", text="x")["ok"]
        refused = session.work("export", dir="c2")
        assert refused == {"ok": False, "error": "the answer would exceed the protocol line; read it in pieces"}
        assert session.proc.poll() is None and session.work("list", path="package")["ok"]
    finally:
        session.close()


def test_read_file_pages_a_long_non_ascii_inputs_file_within_the_protocol_line():
    """M4 §1 (2026-10-05 recheck): `read` answers at most READ_CHARS characters from `offset`, halved while the page
    would not fit the line escaped, and says how many characters follow and where to read next; a 200k-character
    non-ASCII inputs file (inputs/ledger.json grows without bound) is read whole in pages, every answer one line."""
    from ouroloop.agent.worker_runner import LINE_BUDGET, READ_CHARS
    text = ("\u00e9" * 100_000) + ("\U0001F600" * 60_000) + ("\x03" * 40_000)
    session = work_session(inputs={"ledger.json": text})
    try:
        pages, offset = [], 0
        while True:
            answer = session.work("read", path="inputs/ledger.json", offset=offset)
            assert answer["ok"] and len(json.dumps({"op": "result", "value": answer})) < LINE_BUDGET
            body, _, tail = answer["value"].partition("\n[... ")
            assert len(body) <= READ_CHARS
            pages.append(body)
            offset += len(body)
            if not tail:
                break
            assert tail.startswith(f"{len(text) - offset} characters follow; read again with offset {offset}]")
        assert "".join(pages) == text and len(pages) >= 3
        first = session.work("read", path="inputs/ledger.json", limit=10)["value"]
        assert first.startswith("\u00e9" * 10 + "\n[... ") and session.work("read", path="inputs/ledger.json",
                                                                         offset=len(text) - 2)["value"] == "\x03\x03"
        assert not session.work("read", path="inputs/ledger.json", offset="abc")["ok"]
    finally:
        session.close()


def test_a_work_path_is_bounded_and_strict_utf8_and_every_failure_is_the_tools():
    """M4 §1 (2026-10-05 recheck): a path of more than 32 segments or 1024 characters is refused before the disk is
    touched (a 1500-level path used to recurse in os.makedirs and go out as kind "package"); a path that is not
    strict UTF-8 — the lone surrogates of surrogateescape — is refused, so no raw-byte filename reaches list/export;
    any other Exception inside a work action (an AttributeError of a malformed request) is the tool's error too; the
    worker answers the next action."""
    session = work_session()
    try:
        malformed = session.work("init", package=["not", "a", "mapping"], inputs={})
        assert not malformed["ok"] and malformed["error"].startswith("AttributeError")
        assert session.work("init", package={"manifest.json": "{}", "memory.md": "old\n"}, inputs={})["ok"]
        deep = session.work("write", path="candidates/c1/" + "/".join(["a"] * 1500) + "/x.md", text="x")
        assert not deep["ok"] and "at most 32 segments" in deep["error"]
        long = session.work("write", path="candidates/c1/" + "a" * 1100 + ".md", text="x")
        assert not long["ok"] and "1024 characters" in long["error"]
        raw = session.work("write", path="candidates/c1/\udc80x.md", text="x")
        assert not raw["ok"] and "not strict UTF-8" in raw["error"]
        assert not session.work("read", path="package/\udcffy.md")["ok"]
        assert session.proc.poll() is None
        listed = session.work("list")["value"]
        assert all(name.encode("utf-8") for name in listed) and not any("\udc80" in n for n in listed)
        assert session.work("write", path="candidates/c1/" + "/".join(["a"] * 29) + "/x.md", text="x")["ok"]
    finally:
        session.close()


def test_an_accepted_candidate_directory_exports_whole_at_the_line_budget_boundary():
    """M4 §1 (2026-10-05, second recheck): a write reserves the reply's fixed wrapper, so a directory accepted at
    exactly LINE_BUDGET − EXPORT_WRAPPER escaped characters (paths measured against the realpath'd candidate
    directory) still exports as one line within the budget, and one character more is refused."""
    from ouroloop.agent.worker_runner import EXPORT_WRAPPER, LINE_BUDGET
    session = ProcessWorker(for_tests=True).start()
    session.load({})
    try:
        assert session.work("init", package={"manifest.json": "{}"}, inputs={}, max_bytes=10_000_000,
                            max_file_bytes=10_000_000)["ok"]
        held = len(json.dumps("manifest.json")) + len(json.dumps("{}")) + 4
        n = LINE_BUDGET - EXPORT_WRAPPER - held - (len(json.dumps("big.md")) + 2 + 4)
        assert session.work("write", path="candidates/c1/big.md", text="x" * n)["ok"]
        exported = session.work("export", dir="c1")
        assert exported["ok"] and len(exported["value"]["big.md"]) == n
        assert len(json.dumps({"op": "result", "value": exported})) <= LINE_BUDGET
        over = session.work("write", path="candidates/c1/big.md", text="x" * (n + 1))
        assert not over["ok"] and f"at most {LINE_BUDGET - EXPORT_WRAPPER})" in over["error"]
    finally:
        session.close()


def test_ws_prompt_files_are_outside_the_package_byte_cap_inside_the_line_and_cleared_by_writing_them_empty():
    """M4 §1 (2026-10-05, second recheck): worker/system_prompt.md and worker/finish.md are W's prompt texts, not
    package files — they count toward neither K byte cap (harness_package.check_shape never sees them) but do count
    toward the protocol line; deleting one is refused, a prompt is cleared by writing it empty."""
    session = ProcessWorker(for_tests=True).start()
    session.load({})
    try:
        assert session.work("init", package={"manifest.json": "{}", "worker/system_prompt.md": "P" * 900,
                                             "worker/finish.md": "F" * 50}, inputs={}, max_bytes=1000)["ok"]
        assert session.work("write", path="candidates/c1/memory.md", text="m" * 500)["ok"]      # 2 + 500 counted
        assert session.work("write", path="candidates/c1/worker/system_prompt.md", text="Q" * 1200)["ok"]
        over = session.work("write", path="candidates/c1/skills/a.md", text="s" * 600)       # 2 + 500 + 600
        assert not over["ok"] and "at most 1000 bytes in all" in over["error"]
        escaped = session.work("write", path="candidates/c1/worker/finish.md", text="\U0001F600" * 80_000)
        assert not escaped["ok"] and "on the protocol line once escaped" in escaped["error"]
        refused = session.work("delete", path="candidates/c1/worker/finish.md")
        assert not refused["ok"] and "to clear a prompt, write it empty" in refused["error"]
        assert session.work("write", path="candidates/c1/worker/finish.md", text="")["ok"]
        assert session.work("export", dir="c1")["value"]["worker/finish.md"] == ""
        assert session.work("delete", path="candidates/c1/memory.md")["ok"]                   # a package file may go
    finally:
        session.close()
