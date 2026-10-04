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
                   "    assert counts == [4, 9, 9, 19999], counts\n"
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


def test_a_manifest_without_an_agent_entry_is_checked_as_before():
    """check_package reads `agent` defensively: v1 manifests (no entry) run probe and tests only, unchanged."""
    from ouroloop.agent.worker import check_package
    files = {"middlewares/g.py": FINISH_GUARD, "tests/test_g.py": "def test_ok(ctx):\n    assert True\n"}
    manifest = {**hp.empty_manifest(), "hooks": [{"name": "g", "site": "on_finish",
                                                  "entrypoint": "middlewares/g.py:run"}]}
    assert check_package(ProcessWorker(for_tests=True), files, manifest, fixture=World()) == []
    assert check_package(ProcessWorker(for_tests=True), files, {**manifest, "agent": None}, fixture=World()) == []
