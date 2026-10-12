"""The adapter over a fake ctx (the package's own code, run by the draft check's test op in the worker): Stirrup's loop
with this package's manifest — the write guard's hooks and plan_changes — against a scripted model and a synthetic
world of one read and one write endpoint. The six hook sites are each driven by a probe hook defined in this file."""
import importlib.util
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POST = "https://slack.com/api/chat.postMessage"
HERE = "tests/test_stirrup_adapter.py"
GUARD = ("ledger", "triage", "anchor", "verify")    # the write guard's hooks: a hook added later brings its own tests


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Reply:
    def __init__(self, text="", calls=(), stop=None):
        self.text = text
        self.tool_calls = [{"id": f"t{i}", "name": n, "args": a} for i, (n, a) in enumerate(calls)]
        self.stop = stop or ("tool_use" if calls else "end")


class Result:
    def __init__(self, output, is_error=False):
        self.output, self.is_error = output, is_error


class Skills:
    def __init__(self, skills=None):
        self.skills = dict(skills or {})
        self.index = [{"name": n, "description": ""} for n in self.skills]

    def read(self, name):
        return self.skills.get(name)


class Memory:
    def __init__(self, entries=()):
        self.entries, self.queries = list(entries), []

    def recall(self, query, k=None):
        self.queries.append((query, k))
        return self.entries[:k or len(self.entries)]


class FakeCtx:
    """The contract's names over a scripted model and a synthetic world; every call and mark is kept."""

    def __init__(self, replies, act_calls=20, pages=None, hooks=None, config=None, memory=None, skills=None):
        manifest = json.load(open(os.path.join(ROOT, "manifest.json")))
        self.view = {"system": "You are an operations agent.", "events": [], "max_output_chars": 20000,
                     "base_tools": [{"name": "api_fetch", "description": "Call an API.",
                                     "schema": {"type": "object", "properties": {"method": {"type": "string"},
                                                                                 "url": {"type": "string"},
                                                                                 "body": {"type": "string"}}}}],
                     "package": {"hooks": [h for h in manifest["hooks"] if h["name"] in GUARD] if hooks is None else hooks, "tools": manifest["tools"],
                                 "memory": {"enabled": memory is not None}, "config": config or manifest["config"]}}
        self.limits = {"act_calls": act_calls, "tokens": 100000}
        self.skills = Skills(skills)
        self.memory = memory
        self.replies, self.sent, self.world, self.marks, self.roles = list(replies), [], [], [], []
        self.pages = dict(pages or {})          # url -> what a GET of it answers

    def model(self, messages, tools=(), role="act", effort=None):
        self.sent.append(messages)
        self.roles.append(role)
        if role == "act":
            self.limits["act_calls"] -= 1
        return self.replies.pop(0)

    def tool(self, name, args=None, *, read_only=False):
        args = dict(args or {})
        if read_only and args.get("method", "GET") != "GET":
            return Result("refused: read-only", True)
        self.world.append((name, args.get("method"), args.get("url")))
        n = len(self.world)
        output = self.pages.get(args.get("url"), '{"ok": true}')
        self.view["events"] = (self.view["events"] + [{"call_id": f"b{n}", "name": name, "args": args,
                                                       "result_excerpt": output, "error": "",
                                                       "truncated": False, "step": 0}])[-20:]
        return Result(output)

    def mark(self, kind, **data):
        self.marks.append((kind, data))

    def kinds(self):
        return [k for k, _ in self.marks]

    def hook_marks(self):
        return [(d["site"], d["name"], d["action"]) for k, d in self.marks if k == "hook"]


def adapter():
    return _load("agent/stirrup_agent.py", "seedtest_stirrup_agent")


# ---------------------------------------------------------------------------- probe hooks for the six sites
# The adapter loads these by path as a module of its own, so what they record is read back through the marks.
SEEN = []


def probe_before_run(ctx, event):
    SEEN.append(("before_run", sorted(event)))
    return {"system": event["system"] + "\nPROBE-SYSTEM", "messages": [{"role": "user", "content": "PROBE-OPENING"}]}


def probe_before_model(ctx, event):
    SEEN.append(("before_model", event["role"], event["turn"]))
    return {"messages": event["messages"] + [{"role": "user", "content": "PROBE-NOTE"}]}


def probe_before_tool(ctx, event):
    SEEN.append(("before_tool", event["name"], event["call_id"]))
    if event["name"] == "api_fetch":
        return {"args": {**event["args"], "url": event["args"]["url"] + "?probe=1"}}
    return None


def probe_after_tool(ctx, event):
    SEEN.append(("after_tool", event["name"], event["is_error"]))
    return {"output": event["output"] + " [probed]", "feedback": "PROBE-FEEDBACK"}


def probe_after_turn(ctx, event):
    SEEN.append(("after_turn", event["turn"], [r["name"] for r in event["tool_results"]]))
    return {"message": "PROBE-TURN"}


def probe_on_finish(ctx, event):
    SEEN.append(("on_finish", event["reason"], event["pushbacks"]))
    return {"pushback": "PROBE-PUSHBACK"} if event["pushbacks"] == 0 else None


def probe_bad(ctx, event):
    return {"nonsense": True}


def probe_audit(ctx, event):
    """An on_finish hook that asks the aux model once (HARNESS §7.1 rule 4: the call passes before_model first)."""
    reply = ctx.model([{"role": "system", "content": "You audit."}, {"role": "user", "content": "AUDIT"}], role="aux")
    SEEN.append(("audit", getattr(reply, "text", "")))
    return None


def _probes():
    return [{"name": f"p_{s}", "site": s, "entrypoint": f"{HERE}:probe_{s}", "enabled": True}
            for s in ("before_run", "before_model", "before_tool", "after_tool", "after_turn", "on_finish")]


def test_the_sites_are_the_contracts(ctx=None):
    module = adapter()
    assert module.SITES == ("before_run", "before_model", "before_tool", "after_tool", "after_turn", "on_finish")
    assert set(module.RETURNS) == set(module.SITES)


def test_each_of_the_six_sites_fires_acts_and_is_marked(ctx=None):
    fake = FakeCtx([Reply("", [("api_fetch", {"method": "GET", "url": "https://x/a"})]),
                    Reply("", [("finish", {"reason": "first"})]), Reply("", [("finish", {"reason": "Done."})])],
                   hooks=_probes())
    assert adapter().run(fake, "Do the task.") == "Done."
    first = fake.sent[0]
    assert first[0]["role"] == "system" and "PROBE-SYSTEM" in first[0]["content"]          # before_run: system
    assert any(m.get("content") == "PROBE-OPENING" for m in first)                        # before_run: messages
    assert first[-1] == {"role": "user", "content": "PROBE-NOTE"}                          # before_model
    assert fake.world == [("api_fetch", "GET", "https://x/a?probe=1")]                     # before_tool: args
    second = fake.sent[1]
    results = [r for m in second if m["role"] == "tool" for r in m["results"]]
    assert results[0]["output"].endswith(" [probed]")                                      # after_tool: output
    notes = [m["content"] for m in second if m["role"] == "user"]
    assert any("PROBE-FEEDBACK" in n and "PROBE-TURN" in n for n in notes)                 # after_tool, after_turn
    third = [r for m in fake.sent[2] if m["role"] == "tool" for r in m["results"]]
    assert third[-1]["output"].startswith("PROBE-PUSHBACK")                                # on_finish: pushback
    sites = {s for s, _, _ in fake.hook_marks()}
    assert sites == {"before_run", "before_model", "before_tool", "after_tool", "after_turn", "on_finish"}
    assert ("before_tool", "p_before_tool", "modify") in fake.hook_marks()
    assert ("on_finish", "p_on_finish", "pushback") in fake.hook_marks()
    assert fake.kinds().count("pushback") == 1            # the second finish (pushbacks=1) went through


def test_a_hooks_aux_call_passes_through_the_before_model_hooks(ctx=None):
    fake = FakeCtx([Reply("", [("finish", {"reason": "Done."})]), Reply("all fine")],
                   hooks=[{"name": "p_before_model", "site": "before_model", "entrypoint": f"{HERE}:probe_before_model"},
                          {"name": "audit", "site": "on_finish", "entrypoint": f"{HERE}:probe_audit"}])
    fake.view["hook_aux_calls"] = 1
    assert adapter().run(fake, "Do the task.") == "Done."
    assert fake.roles == ["act", "aux"] and fake.replies == []
    act, aux = fake.sent
    assert act[-1] == {"role": "user", "content": "PROBE-NOTE"}
    # the audit's own messages, then the before_model hook's note at their end (the probes' own module is the
    # adapter's load, so what they saw is read back through the marks and the messages)
    assert aux[:2] == [{"role": "system", "content": "You audit."}, {"role": "user", "content": "AUDIT"}]
    assert aux[-1] == {"role": "user", "content": "PROBE-NOTE"} and len(aux) == 3
    assert fake.hook_marks().count(("before_model", "p_before_model", "modify")) == 2
    assert [d["site"] for k, d in fake.marks if k == "hook_aux_call"] == ["on_finish"]


def test_a_hook_answering_outside_its_protocol_is_the_packages_defect(ctx=None):
    fake = FakeCtx([Reply("", [("api_fetch", {"method": "GET", "url": "https://x/a"})])],
                   hooks=[{"name": "bad", "site": "before_tool", "entrypoint": f"{HERE}:probe_bad"}])
    module = adapter()
    try:
        module.run(fake, "Do the task.")
    except module.HookError as e:
        assert getattr(e, "ouroloop_defect", False) and "bad" in str(e)
    else:
        raise AssertionError("an answer outside the protocol must end the run")


def test_a_stopped_write_and_a_finish_in_one_turn_do_not_end_the_attempt(ctx=None):
    write = ("api_fetch", {"method": "POST", "url": POST, "body": "{}"})
    plan = ("plan_changes", {"changes": [{"content": "post the update in #ops", "status": "pending"}],
                             "excluded": []})
    fake = FakeCtx([Reply("Posting.", [write, ("finish", {"reason": "Posted."})]),
                    Reply("", [plan]), Reply("", [write]), Reply("", [("finish", {"reason": "Posted."})]),
                    Reply("", [("finish", {"reason": "Posted the update in #ops."})])])
    assert adapter().run(fake, "Post the update in #ops.") == "Posted the update in #ops."
    assert fake.world == [("api_fetch", "POST", POST)]
    kinds = fake.kinds()
    assert kinds.count("tool_blocked") == 1 and ("tool_skipped", "finish") in [(k, d.get("tool")) for k, d in
                                                                               fake.marks]
    assert kinds.count("pushback") == 1 and kinds[-1] == "stirrup_end"
    end = fake.marks[-1][1]
    assert end["finished"] is True and end["implicit"] is False and end["turns"] == 5 and end["pushbacks"] == 1
    results = [r for m in fake.sent[1] if m["role"] == "tool" for r in m["results"]]
    assert results[0]["output"].startswith("Stopped before execution: Before your first change, call plan_changes")
    assert results[1]["output"].startswith("Not executed: an earlier call in this turn was stopped")
    assert ("before_tool", "triage", "block") in fake.hook_marks()
    assert ("on_finish", "verify", "pushback") in fake.hook_marks()


def test_two_text_only_replies_finish_the_attempt(ctx=None):
    fake = FakeCtx([Reply("Nothing to change here."), Reply("The request needs no change."),
                    Reply("No change is needed: the record is already right.")])
    text = adapter().run(fake, "Check the record.")
    assert text == "No change is needed: the record is already right."
    kinds = fake.kinds()
    assert kinds.count("implicit_finish") == 2 and kinds.count("pushback") == 1      # verify sent it back once
    assert fake.marks[-1] == ("stirrup_end", {**fake.marks[-1][1], "finished": True, "implicit": True, "turns": 3})
    assert fake.world == [] and fake.roles == ["act", "act", "act"]


def test_recall_goes_through_ctx_memory(ctx=None):
    memory = Memory([{"id": "e1", "when": "a refund is asked", "content": "check the order first", "score": 0.9}])
    fake = FakeCtx([Reply("", [("recall", {"query": "refund", "k": 1})]), Reply("", [("finish", {"reason": "ok"})]),
                    Reply("", [("finish", {"reason": "ok"})])], memory=memory)
    adapter().run(fake, "Handle the refund.")
    assert memory.queries == [("refund", 1)]
    results = [r for m in fake.sent[1] if m["role"] == "tool" for r in m["results"]]
    assert results[0]["output"].startswith("[e1] When: a refund is asked")


def test_no_recall_tool_without_memory_and_sub_agents_only_with_depth(ctx=None):
    fake = FakeCtx([Reply("", [("finish", {"reason": "ok"})]), Reply("", [("finish", {"reason": "ok"})])],
                   config={"max_turns": 50, "summarize_at": 0.7, "shown_k": 2, "subagent_depth": 0})
    module = adapter()
    module.SUB_AGENTS = ({"name": "researcher", "description": "Reads.", "system_prompt": "Read only."},)
    module.run(fake, "Do it.")
    client = module.CtxClient(fake)
    tools = module.Tools(fake, client)
    assert "recall" not in [s["name"] for s in tools.specs()]
    assert "researcher" not in json.dumps(fake.sent[0])


def test_a_sub_agent_shares_the_guard_and_hands_the_turn_back(ctx=None):
    """A sub-agent reads a dated page through the same hooks; the parent's next act call carries the date anchor, and
    the parent's write and the guard's ledger are one attempt's."""
    inbox = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    read = ("api_fetch", {"method": "GET", "url": inbox})
    write = ("api_fetch", {"method": "POST", "url": POST, "body": "{}"})
    plan = ("plan_changes", {"changes": [{"content": "post the update in #ops", "status": "pending"}],
                             "excluded": []})
    fake = FakeCtx([Reply("", [("researcher", {"task": "Find the newest message."}), plan]),     # parent, turn 1
                    Reply("", [read]),                                                            # sub-agent
                    Reply("", [("finish", {"reason": "The newest message is m1."})]),             # sub-agent
                    Reply("", [write]),                                                           # parent
                    Reply("", [("finish", {"reason": "Posted."})]),                               # verify: back
                    Reply("", [("finish", {"reason": "Posted the update."})])],
                   pages={inbox: '{"messages": [{"id": "m1", "internalDate": "1748908800000"}]}'})
    module = adapter()
    module.SUB_AGENTS = ({"name": "researcher", "description": "Reads what the task needs and reports it.",
                          "system_prompt": "Read only; report what you found."},)
    assert module.run(fake, "Post the update in #ops.") == "Posted the update."
    assert fake.world == [("api_fetch", "GET", inbox), ("api_fetch", "POST", POST)]
    assert fake.roles == ["act"] * 6
    marks = [(k, d.get("phase")) for k, d in fake.marks if k == "sub_agent"]
    assert marks == [("sub_agent", "start"), ("sub_agent", "done")]
    done = [d for k, d in fake.marks if k == "sub_agent" and d["phase"] == "done"][0]
    assert done["turns"] == 2 and done["is_error"] is False and done["call_id"] == "t0"
    sub = fake.sent[1]
    assert sub[0]["role"] == "system" and "Read only; report what you found." in sub[0]["content"]
    assert not any(m["role"] == "assistant" for m in sub) and "Find the newest message." in json.dumps(sub)
    parent = fake.sent[3]
    results = [r for m in parent if m["role"] == "tool" for r in m["results"]]
    assert [r["name"] for r in results] == ["researcher", "plan_changes"]
    assert "The newest message is m1." in results[0]["output"] and not results[0]["is_error"]
    assert "2025-06-03 (field internalDate)" in parent[-1]["content"]          # the anchor rides on the call
    assert not any("field internalDate" in json.dumps(m) for m in fake.sent[1])
    assert "tool_blocked" not in fake.kinds()
    verify = [r for m in fake.sent[5] if m["role"] == "tool" for r in m["results"]][-1]
    assert "POST " + POST + " -> ok" in verify["output"]


def test_every_act_call_extends_the_one_before(ctx=None):
    """With the write guard's hooks on and a date read, each act call's messages begin with the previous call's: the
    provider's prompt cache reads the earlier part instead of writing it again. A hook added to the package must keep
    this (inputs/package_api.md says so)."""
    inbox = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    plan = ("plan_changes", {"changes": [{"content": "post the update in #ops", "status": "pending"}], "excluded": []})
    fake = FakeCtx([Reply("", [("api_fetch", {"method": "GET", "url": inbox})]), Reply("", [plan]),
                    Reply("", [("api_fetch", {"method": "POST", "url": POST, "body": "{}"})]),
                    Reply("", [("finish", {"reason": "Posted."})]), Reply("", [("finish", {"reason": "Posted it."})])],
                   pages={inbox: '{"messages": [{"id": "m1", "internalDate": "1748908800000"}]}'})
    assert adapter().run(fake, "Post the update in #ops.") == "Posted it."
    acts = [m for m, r in zip(fake.sent, fake.roles) if r == "act"]
    assert len(acts) == 5 and any("field internalDate" in json.dumps(m) for m in acts)
    for before, after in zip(acts, acts[1:]):
        assert after[:len(before)] == before
