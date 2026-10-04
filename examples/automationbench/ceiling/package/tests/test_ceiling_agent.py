"""The ceiling agent's own tests, run by the draft check in the worker (package_test): a scripted ctx, no host."""
import importlib.util
import json
import os


def _agent():
    path = os.path.join(os.path.dirname(__file__), "..", "agent", "ceiling_agent.py")
    spec = importlib.util.spec_from_file_location("ceiling_agent_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Reply:
    def __init__(self, text="", tool_calls=(), stop="end"):
        self.text, self.tool_calls, self.stop = text, list(tool_calls), stop


class _Result:
    def __init__(self, output, is_error=False):
        self.output, self.is_error = output, is_error


class _Skills:
    index, memory = [], None

    def read(self, name):
        return None


class _Ctx:
    """The five names and mark. `act` is the act turns in order; `aux` answers an aux call from its request."""

    def __init__(self, act, aux, outputs=None, hooks=()):
        self.act, self.aux_answer, self.outputs = list(act), aux, dict(outputs or {})
        self.view = {"system": "s", "max_output_chars": 200 * 100, "package": {"hooks": list(hooks)},
                     "base_tools": [{"name": "api_fetch", "description": "d", "schema": {"type": "object"}}]}
        self.limits = {"act_calls": 50, "model_calls": 200, "tool_calls": 200, "seconds": 900, "tokens": 4 * 10 ** 5}
        self.skills = _Skills()
        self.models, self.tools, self.marks = [], [], []

    def model(self, messages, tools=(), role="act", effort=None):
        self.models.append({"role": role, "effort": effort, "messages": json.loads(json.dumps(messages)),
                            "tools": [t["name"] for t in tools]})
        self.limits["model_calls"] -= 1
        if role == "aux":
            return _Reply(self.aux_answer(messages[-1]["content"]))
        self.limits["act_calls"] -= 1
        turn = self.act.pop(0) if self.act else []
        if isinstance(turn, str):
            return _Reply(turn)
        return _Reply("", [{"id": "c%d" % i, "name": n, "args": a} for i, (n, a) in enumerate(turn)], "tool_use")

    def tool(self, name, args=None, read_only=False):
        self.tools.append({"name": name, "args": dict(args or {}), "read_only": read_only})
        self.limits["tool_calls"] -= 1
        out = self.outputs.get(args.get("url")) if args else None
        return _Result(out if out is not None else '{"ok": true}')

    def mark(self, kind, **data):
        self.marks.append(kind)


def _get(url):
    return ("api_fetch", {"method": "GET", "url": url})


def _post(url, body="{}"):
    return ("api_fetch", {"method": "POST", "url": url, "body": body})


def _answers(table="| 1 | send the report | ok | doc | VERIFIED |", verdicts=('{"verdict": "finish"}',)):
    verdicts = list(verdicts)

    def answer(prompt):
        if prompt.startswith("Before the first write"):
            return table
        return verdicts.pop(0) if verdicts else '{"verdict": "finish"}'
    return answer


def test_an_evidence_table_comes_before_the_first_write_and_every_call_asks_for_high_effort(ctx):
    a = _agent()
    c = _Ctx([[_get("u/doc")], [_post("u/send")], [_post("u/send")], "done"], _answers())
    assert a.run(c, "task") == "done"
    assert [t["args"]["method"] for t in c.tools] == ["GET", "POST"]          # the first POST waited for the table
    roles = [m["role"] for m in c.models]
    assert roles == ["act", "act", "aux", "act", "act", "aux"]
    assert all(m["effort"] == "high" for m in c.models)
    after_table = c.models[3]["messages"]
    assert "Evidence table, built before the first write" in after_table[-1]["content"]
    assert "send the report" in after_table[-1]["content"]
    assert c.models[2]["tools"] == c.models[1]["tools"]                    # the aux call shares the act prefix
    assert "tool_blocked" in c.marks and "evidence_table" in c.marks


def test_the_check_before_finishing_reads_the_executed_writes_and_sends_back_its_gaps(ctx):
    a = _agent()
    verdicts = ['{"verdict": "continue", "gaps": ["the report was not sent to the second recipient"]}',
                '{"verdict": "continue", "gaps": ["still"]}']
    c = _Ctx([[_post("u/a", '{"to": "x"}')], [_post("u/a", '{"to": "x"}')], "first", [_post("u/b")], "second",
              "third"], _answers(verdicts=verdicts))
    assert a.run(c, "task") == "third"               # two checks at most: the finish after the second is final
    checks = [m for m in c.models if m["role"] == "aux" and m["messages"][-1]["content"].startswith("Check")]
    assert len(checks) == 2
    assert "POST u/a" in checks[0]["messages"][-1]["content"] and "POST u/b" in checks[1]["messages"][-1]["content"]
    pushed = c.models[5]["messages"][-1]["content"]
    assert "the report was not sent to the second recipient" in pushed


def test_an_unreadable_check_finishes(ctx):
    a = _agent()
    c = _Ctx(["nothing to write"], _answers(verdicts=["no json here"]))
    assert a.run(c, "task") == "nothing to write"
    assert [m["role"] for m in c.models] == ["act", "aux"]


def test_read_full_returns_every_character_in_pages_and_reads_a_later_page_from_its_cache(ctx):
    a = _agent()
    long_a, long_b = "a" * (150 * 100), "b" * (150 * 100)
    reqs = [{"url": "u/a"}, {"url": "u/b"}]
    c = _Ctx([[("read_full", {"requests": reqs})], [("read_full", {"requests": reqs, "page": 1})], "done"],
             _answers(), outputs={"u/a": long_a, "u/b": long_b})
    assert a.run(c, "task") == "done"
    assert [t["read_only"] for t in c.tools] == [True, True]                  # two GETs, the second page cached
    first = c.models[1]["messages"][-1]["results"][0]["output"]
    second = c.models[2]["messages"][-1]["results"][0]["output"]
    assert first.startswith("page 1 of 2") and len(first) <= 200 * 100 and "page=1" in first
    assert second.startswith("page 2 of 2")
    joined = first + second
    assert joined.count("a") >= len(long_a) and joined.count("b") >= len(long_b)


def test_read_full_refuses_what_it_cannot_do_without_raising(ctx):
    a = _agent()
    c = _Ctx([[("read_full", {"requests": []})], [("read_full", {"requests": [{"url": "u"}] * 30})], "done"],
             _answers())
    assert a.run(c, "task") == "done"
    assert c.tools == []
    assert c.models[1]["messages"][-1]["results"][0]["is_error"]


def test_a_clipped_or_paged_result_gets_a_reading_note(ctx):
    a = _agent()
    clipped = "head\n[... 987 characters omitted ...]\ntail"
    paged = json.dumps({"messages": [{"id": "m1", "threadId": "t1"}], "nextPageToken": "p2"})
    c = _Ctx([[_get("u/big")], [_get("u/list")], "done"], _answers(), outputs={"u/big": clipped, "u/list": paged})
    assert a.run(c, "task") == "done"
    note1 = c.models[1]["messages"][-1]["content"]
    note2 = c.models[2]["messages"][-1]["content"]
    assert "987 characters" in note1 and "clipped by the host" in note1
    assert "more pages exist" in note2 and "by id or snippet only" in note2


def test_the_on_finish_hook_of_the_package_speaks_before_the_check(ctx):
    a = _agent()
    hook = {"name": "finish_audit", "site": "on_finish", "entrypoint": "middlewares/finish_audit.py:finish_audit"}
    c = _Ctx(["done", "audited\nFINISH-AUDIT: done"], _answers(), hooks=[hook])
    assert a.run(c, "task") == "audited\nFINISH-AUDIT: done"
    assert [m["role"] for m in c.models] == ["act", "act", "aux"]
    assert "FINISH-AUDIT: done" in c.models[1]["messages"][-1]["content"]      # W*'s audit pushed back first


def test_a_search_or_query_post_is_a_read_and_the_first_real_write_waits_for_the_table(ctx):
    a = _agent()
    search = ("api_fetch", {"method": "POST", "url": "ads/customers/c/googleAds:search", "body": "{}"})
    contacts = ("api_fetch", {"method": "POST", "url": "crm/objects/contacts/search?x=1", "body": "{}"})
    mutate = ("api_fetch", {"method": "POST", "url": "ads/customers/c/campaigns:mutate", "body": "{}"})
    c = _Ctx([[_get("u/doc")], [search, contacts], [mutate], [mutate], "done"], _answers())
    assert a.run(c, "task") == "done"
    assert [t["args"]["url"] for t in c.tools] == ["u/doc", "ads/customers/c/googleAds:search",
                                                    "crm/objects/contacts/search?x=1",
                                                    "ads/customers/c/campaigns:mutate"]
    assert [m["role"] for m in c.models] == ["act", "act", "act", "aux", "act", "act", "aux"]
    check = c.models[-1]["messages"][-1]["content"]
    assert "campaigns:mutate" in check and "googleAds:search" not in check and "contacts/search" not in check
    assert a._is_write("api_fetch", {"method": "POST", "url": "x/customers:findOrCreate"})
    assert a._is_write("api_fetch", {"method": "POST", "url": "x/contacts:findOrCreateLead"})
    assert not a._is_write("api_fetch", {"method": "POST", "url": "x/company/q/query"})
    assert not a._is_write("api_fetch", {"method": "POST", "url": "x/items:find"})


def test_an_empty_next_page_is_no_page_signal(ctx):
    a = _agent()
    empty = json.dumps({"total": 0, "results": [], "paging": {"next": {"after": None}}})
    after = json.dumps({"results": [{"id": "1", "properties": {}, "createdAt": "x", "archived": False}],
                        "paging": {"next": {"after": "abc"}}})
    more = json.dumps({"ok": True, "messages": [], "has_more": True})
    c = _Ctx([[_get("u/empty")], [_get("u/after")], [_get("u/more")], "done"], _answers(),
             outputs={"u/empty": empty, "u/after": after, "u/more": more})
    assert a.run(c, "task") == "done"
    assert c.models[1]["messages"][-1]["role"] == "tool"                      # no note after the empty search
    assert "more pages exist" in c.models[2]["messages"][-1]["content"]
    assert "after" in c.models[2]["messages"][-1]["content"]
    assert "more pages exist" in c.models[3]["messages"][-1]["content"]
    assert a._page_key({"paging": {"next": {"after": None}}}) is None
    assert a._page_key({"has_more": False, "next_cursor": ""}) is None
