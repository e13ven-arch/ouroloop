"""The write guard on synthetic events (the seed's cases). Run by the draft check's test op in the worker, and by the
host's tests/harness/test_seed.py; standard library only."""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(rel):
    spec = importlib.util.spec_from_file_location("seedtest_" + rel.replace("/", "_")[:-3], os.path.join(ROOT, rel))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Ctx:
    """What a hook holds: the attempt's view (with the turn) and a read-only tool, which the guard never uses."""

    def __init__(self, events=(), turn=1):
        self.view = {"base_tools": [{"name": "api_fetch"}, {"name": "api_search"}], "events": list(events),
                     "turn": turn}

    def tool(self, name, args=None, *, read_only=False):
        raise AssertionError("the write guard makes no broker call")


def event(n, method, url, body="", error="", result=""):
    return {"call_id": f"s:b{n}", "name": "api_fetch", "args": {"method": method, "url": url, "body": body},
            "result_excerpt": result, "error": error, "truncated": False, "step": 1}


def after(name, args, output="", turn=1, is_error=False):
    return {"site": "after_tool", "turn": turn, "call_id": f"c{turn}", "name": name, "args": args, "output": output,
            "is_error": is_error}


def before(name, args, turn=1):
    return {"site": "before_tool", "turn": turn, "call_id": f"c{turn}", "name": name, "args": args}


def finishing(turn=1):
    return {"site": "on_finish", "turn": turn, "reason": "finish", "final_text": "Done.", "pushbacks": 0}


def model_call(messages, role="act", turn=1):
    return {"site": "before_model", "turn": turn, "role": role, "messages": messages, "tools": []}


WRITE = {"method": "POST", "url": "https://slack.com/api/chat.postMessage", "body": '{"channel": "C1", "text": "hi"}'}
PLAN = {"changes": [{"content": "post hi", "status": "pending"}], "excluded": []}
INBOX = "https://gmail.googleapis.com/gmail/v1/users/me/messages/m1"
DATED = '{"id": "m1", "internalDate": "1906718400000"}'                      # 2030-06-03


def guard():
    module = _load("middlewares/write_guard.py")
    module.reset()
    module.STATE["today"] = __import__("datetime").date(2032, 1, 1)     # the cases' data is dated 2026-2031
    return module


def _read(g, n, result=DATED, url=INBOX, turn=1):
    e = event(n, "GET", url, result=result)
    return g.ledger(Ctx([e]), after("api_fetch", e["args"], result, turn))


def test_triage_blocks_the_first_unplanned_write_once(ctx=None):
    g = guard()
    first = g.triage(Ctx(), before("api_fetch", WRITE, 2))
    assert first == {"block": {"reason": g.TRIAGE_MESSAGE}}
    assert g.triage(Ctx(), before("api_fetch", WRITE, 3)) is None


def test_reads_and_search_posts_are_never_stopped(ctx=None):
    g = guard()
    for args in ({"method": "GET", "url": "https://x/api/v1/items"}, {"method": "head", "url": "https://x/a"},
                 {"method": "POST", "url": "https://x/crm/v3/objects/contacts/search"},
                 {"method": "POST", "url": "https://x/v1/documents:query?x=1"}):
        assert g.triage(Ctx(), before("api_fetch", args)) is None, args
    assert not g.snapshot()["triaged"]
    assert g.triage(Ctx(), before("api_search", {"query": "post message"})) is None


def test_a_plan_before_the_first_write_means_no_triage(ctx=None):
    g = guard()
    assert g.ledger(Ctx(), after("plan_changes", {**PLAN, "excluded": ["#general"]}, "Plan recorded")) is None
    assert g.triage(Ctx(), before("api_fetch", WRITE)) is None


def test_verify_pushes_back_once_with_the_plan_the_ledger_and_the_excluded(ctx=None):
    g = guard()
    g.ledger(Ctx(), after("plan_changes", {"changes": [{"content": "post hi in #ops", "status": "completed"}],
                                           "excluded": ["Dana Reyes"]}))
    g.ledger(Ctx([event(1, "POST", WRITE["url"], WRITE["body"])]), after("api_fetch", WRITE, '{"ok": true}', 2))
    answer = g.verify(Ctx(), finishing(3))
    assert set(answer) == {"pushback"} and answer["pushback"].startswith(g.VERIFY_MESSAGE)
    for text in ("[completed] post hi in #ops", "- Dana Reyes", "POST https://slack.com/api/chat.postMessage -> ok"):
        assert text in answer["pushback"], text
    assert g.EMPTY_LEDGER not in answer["pushback"]
    assert g.verify(Ctx(), finishing(4)) is None


def test_an_empty_ledger_is_named(ctx=None):
    g = guard()
    message = g.verify(Ctx(), finishing())["pushback"]
    assert g.EMPTY_LEDGER in message and "none recorded" in message


def test_the_ledger_stays_whole_past_the_views_twenty_events(ctx=None):
    g = guard()
    events = []
    for n in range(1, 31):
        events.append(event(n, "POST" if n % 3 == 0 else "GET", f"https://x/api/{n}", body=f"b{n}"))
        g.ledger(Ctx(events[-20:]), after("api_fetch", events[-1]["args"], "{}", n))
    assert [w["url"] for w in g.snapshot()["writes"]] == [f"https://x/api/{n}" for n in range(3, 31, 3)]


def test_dates_skip_due_expiry_and_end_fields(ctx=None):
    g = guard()
    text = ('{"created_at": "2026-02-10T09:00:00Z", "due_date": "2026-09-30", "expires_at": "2027-01-01", '
            '"endTime": "2026-12-01", "sent": "Mon, 03 Jun 2030 08:00:00 +0000", "ts": "1772000000.000200", '
            '"contacts": "2030-01-01"}')
    assert g.newest_date(text) == ("2030-06-03", "sent")
    assert g.newest_date('{"due": "2026-05-01", "end_date": "2026-05-02"}') is None


def test_a_record_systems_stamp_and_the_workers_own_day_are_not_dates(ctx=None):
    """HARNESS §7.1: finance-v2's anchored real_date failures read QuickBooks' MetaData and Xero's UpdatedDateUTC, the
    simulated world's clock stamps; and nothing the data holds can be today."""
    g = guard()
    stamped = ('{"Id": "7", "TxnDate": "2026-02-28", "MetaData": {"CreateTime": "2031-12-30T10:00:00Z", '
               '"LastUpdatedTime": "2031-12-30T10:00:00Z"}, "UpdatedDateUTC": "2031-12-30T10:00:00", '
               '"date_modified": "2031-12-31", "createdAt": "2026-03-01T00:00:00Z"}')
    assert g.newest_date(stamped) == ("2026-03-01", "createdAt")
    g.STATE["today"] = __import__("datetime").date(2026, 3, 1)
    assert g.newest_date(stamped) == ("2026-02-28", "TxnDate")              # today itself is the clock's
    assert g.newest_date('{"sent": "2026-03-13"}') is None                   # so is anything after it


def test_the_anchor_rides_on_every_act_call_once_a_date_is_read(ctx=None):
    g = guard()
    history = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    assert g.anchor(Ctx(), model_call(history)) is None                   # nothing dated read yet
    _read(g, 1)
    tool_turn = history + [{"role": "assistant", "text": "", "tool_calls": []},
                           {"role": "tool", "results": [{"id": "c1", "output": DATED}]}]
    answer = g.anchor(Ctx(), model_call(tool_turn, turn=2))
    note = {"role": "user", "content": g.DATE_MESSAGE.format(date="2030-06-03", field="internalDate")}
    assert answer["messages"] == tool_turn + [note]
    assert tool_turn[-1]["role"] == "tool"                                 # the history itself is not changed
    # the next call keeps the note where it was given: the earlier call is a prefix of this one (the prompt cache)
    later = tool_turn + [{"role": "assistant", "text": "", "tool_calls": []},
                         {"role": "tool", "results": [{"id": "c2", "output": "{}"}]}]
    again = g.anchor(Ctx(), model_call(later, turn=3))["messages"]
    assert again == tool_turn + [note] + later[len(tool_turn):]
    assert again[:len(answer["messages"])] == answer["messages"]
    assert g.anchor(Ctx(), model_call(later, role="aux")) is None          # a summary call is left alone
    assert g.snapshot()["anchored"] == ("2030-06-03", "internalDate")


def test_a_newer_date_gets_its_own_note_and_a_summary_starts_the_notes_over(ctx=None):
    g = guard()
    history = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    _read(g, 1)
    first = g.anchor(Ctx(), model_call(history, turn=2))["messages"]
    g.STATE["date"] = ("2030-07-01", "sent")                              # a newer date read later
    grown = history + [{"role": "assistant", "text": "", "tool_calls": []}]
    second = g.anchor(Ctx(), model_call(grown, turn=3))["messages"]
    assert second[:len(first)] == first and "2030-07-01" in second[-1]["content"]
    assert [m["content"] for m in second if "newest date" in str(m.get("content"))] == \
        [first[-1]["content"], second[-1]["content"]]
    # a summary shortened the history: one note, at its end
    short = [{"role": "system", "content": "s"}]
    assert g.anchor(Ctx(), model_call(short, turn=4))["messages"] == short + [second[-1]]
    # a sub-agent's conversation (another first message) has its own notes
    sub = [{"role": "system", "content": "sub"}, {"role": "user", "content": "look"}]
    assert g.anchor(Ctx(), model_call(sub, turn=5))["messages"] == sub + [second[-1]]


def test_no_date_is_read_from_a_writes_own_result_but_reads_go_on_after_it(ctx=None):
    """The benchmark's world stamps the records an agent creates with the real clock: a write's own result never
    moves the anchor; a read after the write does (an attempt whose first action is a write still gets its anchor,
    HARNESS §7.1), and a stamp of today in it is left out like any other."""
    g = guard()
    _read(g, 1)
    stamped = '{"id": "r9", "created_at": "2031-10-08T12:00:00Z"}'
    write = event(2, "POST", WRITE["url"], WRITE["body"], result=stamped)
    g.ledger(Ctx([write]), after("api_fetch", WRITE, stamped, 2))
    assert g.snapshot()["date"] == ("2030-06-03", "internalDate")           # the write's result: not read
    _read(g, 3, result='{"id": "m2", "internalDate": "1909310400000"}', url=INBOX + "2", turn=3)   # 2030-07-03
    assert g.snapshot()["date"] == ("2030-07-03", "internalDate")           # a later read still counts
    g.STATE["today"] = __import__("datetime").date(2030, 7, 3)
    _read(g, 4, result='{"id": "m3", "internalDate": "1909396800000"}', url=INBOX + "3", turn=4)   # 2030-07-04
    assert g.snapshot()["date"] == ("2030-07-03", "internalDate")           # today or later: the clock's


def test_a_planned_time_is_not_today_whether_named_or_nested(ctx=None):
    g = guard()
    event_row = ('{"events": [{"created": "2026-02-20T10:00:00Z", "start": {"dateTime": "2026-05-01T10:00:00Z"}, '
                 '"end": {"date": "2026-05-02"}}], "close_date": "2026-06-30", "warnAt": "2026-07-01", '
                 '"eventDate": "2026-08-01"')
    assert g.newest_date(event_row) == ("2026-02-20", "created")
    assert g.newest_date(event_row, words=g.DATE_WORDS_42, not_today=g.NOT_TODAY_42, nested=False) == \
        ("2026-08-01", "eventDate")
    assert g.newest_date(event_row[:60]) == ("2026-02-20", "created")
