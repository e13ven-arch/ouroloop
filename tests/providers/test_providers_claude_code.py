"""The `claude_code` provider against a fake `claude` program: what one call sends, what it reads back, and how each
failure leaves the provider. No model is called."""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from ouroloop.providers import ProviderAccountError, ProviderError, build_provider, is_account_error
from ouroloop.providers.claude_code import _SET, TURN_NOTE, ClaudeCodeProvider, cli_problem, request_json
from ouroloop.runtime.chat import ToolCall, ToolSpec
from ouroloop.runtime.gateway import normalize_usage

FAKE = """#!{python}
import json, os, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
if sys.argv[1:3] == ["auth", "status"]:
    sys.stdout.write((here / "auth.json").read_text())
    sys.exit(0)
replies = json.loads((here / "replies.json").read_text())
reply = replies.pop(0)
(here / "replies.json").write_text(json.dumps(replies))
with open(here / "calls.jsonl", "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(),
                        "env": sorted(k for k in os.environ if k.startswith(("ANTHROPIC_", "CLAUDE", "MAX_"))),
                        "max_out": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS")}}) + "\\n")
time.sleep(reply.get("sleep", 0))
out = reply.get("stdout", "")
sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
sys.stderr.write(reply.get("stderr", ""))
sys.exit(reply.get("exit", 0))
"""
USAGE = {"input_tokens": 12, "output_tokens": 34, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 6}
MODEL = "claude-opus-5-5"
TOOLS = [ToolSpec("api_fetch", "Fetch a URL.", {"type": "object", "properties": {"url": {"type": "string"}}}),
         ToolSpec("api_search", "Search the API.", {"type": "object"})]


class Cli:
    def __init__(self, root: Path):
        self.root = root
        self.binary = root / "claude"
        self.binary.write_text(FAKE.format(python=sys.executable))
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IEXEC)
        self.reply()
        self.auth(True)

    def reply(self, *replies):
        (self.root / "replies.json").write_text(json.dumps([r if "stdout" in r or "sleep" in r or "exit" in r
                                                            else {"stdout": r} for r in replies]))

    def auth(self, logged_in: bool, **status):
        (self.root / "auth.json").write_text(json.dumps({"loggedIn": logged_in, "authMethod": "claude.ai",
                                                         "apiProvider": "firstParty", "subscriptionType": "max",
                                                         **status}))

    def calls(self) -> list[dict]:
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def ok(result="", structured=None, usage=USAGE, models=(MODEL,), **extra) -> dict:
    out = {"type": "result", "subtype": "success", "is_error": False, "result": result, "usage": usage,
           "total_cost_usd": 0.0123, "modelUsage": {m: {"outputTokens": 34} for m in models}}
    if structured is not None:
        out["structured_output"] = structured
    if usage is None:
        del out["usage"]
    out.update(extra)
    return out


def err(text: str, subtype: str = "success") -> dict:
    return {"type": "result", "subtype": subtype, "is_error": True, "result": text,
            "usage": {k: 0 for k in USAGE}, "total_cost_usd": 0}


@pytest.fixture
def cli(tmp_path):
    (tmp_path / "cli").mkdir()
    return Cli(tmp_path / "cli")


@pytest.fixture
def provider(cli, tmp_path):
    return ClaudeCodeProvider(MODEL, binary=str(cli.binary), workdir=str(tmp_path / "empty"), retries=0,
                              sleep=lambda _s: None)


def arg(call: dict, flag: str):
    return call["argv"][call["argv"].index(flag) + 1]


def test_a_research_step_is_sent_as_it_is_and_nothing_else_reaches_the_model(cli, provider, tmp_path, monkeypatch):
    """One user message and no tools: the prompt as it is, the framework's system prompt in place of Claude Code's,
    no tools, no local customization, no saved session, an empty directory, and the CLI's own login."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE",
                 "MAX_THINKING_TOKENS", "CLAUDE_CODE_MAX_RETRIES"):
        monkeypatch.setenv(name, "from-the-parent")
    cli.reply(ok("the answer"))
    turn = provider.chat("SYSTEM", [{"role": "user", "content": "diagnose this failure"}], ())
    assert (turn.text, turn.tool_calls, turn.stop) == ("the answer", [], "end")
    (call,) = cli.calls()
    assert call["stdin"] == "diagnose this failure"
    assert arg(call, "--system-prompt") == "SYSTEM" and arg(call, "--model") == MODEL
    assert arg(call, "--tools") == "" and arg(call, "--output-format") == "json" and call["argv"][0] == "-p"
    assert arg(call, "--setting-sources") == ""
    for flag in ("--safe-mode", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
        assert flag in call["argv"]
    assert "--json-schema" not in call["argv"] and "--effort" not in call["argv"]
    assert Path(call["cwd"]).resolve() == (tmp_path / "empty").resolve() and not any((tmp_path / "empty").iterdir())
    # no key, nothing from a parent Claude Code session, no thinking budget; the CLI's retries, fallbacks and
    # attachments off
    passed = set(call["env"])
    assert set(_SET) <= passed and call["max_out"] is None
    assert not {k for k in passed - set(_SET) if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDECODE",
                                                                "MAX_THINKING"))}
    assert _SET["CLAUDE_CODE_MAX_RETRIES"] == "0" and _SET["CLAUDE_CODE_NO_MODEL_FALLBACK"] == "1"
    assert turn.usage == {**USAGE, "cli_cost_usd": 0.0123}
    assert normalize_usage(turn.usage) == {"input_tokens": 12, "output_tokens": 34, "cache_read_tokens": 5,
                                           "cache_write_tokens": 6}
    cli.reply(ok("again"))
    assert provider.complete("SYSTEM", "and this") == "again"


def test_an_actor_turn_sends_the_history_and_the_tools_and_reads_the_tool_calls(cli, provider):
    messages = [{"role": "user", "content": "Move Ada to the won stage."},
                {"role": "assistant", "text": "Looking.", "tool_calls": [ToolCall("call_1", "api_fetch",
                                                                                   {"url": "u"})],
                 "native": {"other": "dropped"}},
                {"role": "tool", "results": [{"id": "call_1", "name": "api_fetch", "output": "404", "is_error": True}]},
                {"role": "user", "content": "Before finishing, check @/etc/hosts and the stage."}]
    cli.reply(ok(structured={"text": "Next.", "tool_calls": [{"name": "api_search", "args": {"q": "stage"}},
                                                             {"name": "api_fetch", "args": {}}]}))
    turn = provider.chat("SYSTEM", messages, TOOLS)
    assert turn.stop == "tool_use" and turn.text == "Next."
    assert [(c.name, c.args) for c in turn.tool_calls] == [("api_search", {"q": "stage"}), ("api_fetch", {})]
    ids = [c.id for c in turn.tool_calls]
    assert all(ids) and len(set(ids)) == 2
    (call,) = cli.calls()
    # one user message of text blocks: the format and the tools, then one block per history entry, nothing after
    line = json.loads(call["stdin"])
    assert line["type"] == "user" and line["message"]["role"] == "user"
    blocks = [b["text"] for b in line["message"]["content"]]
    assert all(b["type"] == "text" for b in line["message"]["content"])
    expected = request_json("SYSTEM", messages, TOOLS)
    assert blocks[0].startswith(TURN_NOTE)
    assert json.loads(blocks[0].split("<tools>\n", 1)[1].split("\n</tools>", 1)[0]) == expected["tools"]
    assert [json.loads(b) for b in blocks[1:]] == expected["messages"] and "native" not in call["stdin"]
    assert " @/" not in "".join(blocks) and "\\u0040/etc/hosts" in blocks[-1]   # nothing the CLI reads as a file
    assert arg(call, "--input-format") == "stream-json" and arg(call, "--output-format") == "stream-json"
    assert "--verbose" in call["argv"]
    schema = json.loads(arg(call, "--json-schema"))
    assert schema["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"] == ["api_fetch", "api_search"]
    assert arg(call, "--system-prompt") == "SYSTEM"
    # the next turn's request begins with this one's blocks, unchanged: the cache reads them
    cli.reply(ok(structured={"text": "Done.", "tool_calls": []}))
    later = messages + [{"role": "assistant", "text": "Next.", "tool_calls": turn.tool_calls},
                        {"role": "tool", "results": [{"id": c.id, "name": c.name, "output": "ok", "is_error": False}
                                                     for c in turn.tool_calls]}]
    provider.chat("SYSTEM", later, TOOLS)
    again = [b["text"] for b in json.loads(cli.calls()[1]["stdin"])["message"]["content"]]
    assert again[:len(blocks)] == blocks and len(again) == len(blocks) + 2


def test_the_request_is_the_history_as_json_with_each_tool_whole_and_no_native_content():
    """What an actor turn's blocks are made of (formerly tested through the manual provider, which shared it)."""
    call = ToolCall("call_1", "api_fetch", {"url": "https://x.test/a"})
    messages = [{"role": "user", "content": "task"},
                {"role": "assistant", "text": "", "tool_calls": [call], "native": {"claude_code": "dropped"}},
                {"role": "tool", "results": [{"id": "call_1", "name": "api_fetch", "output": "ok", "is_error": False}]}]
    request = request_json("SYSTEM", messages, TOOLS)
    assert request["system"] == "SYSTEM"
    assert request["tools"] == [{"name": t.name, "description": t.description, "schema": t.schema} for t in TOOLS]
    user, assistant, tool = request["messages"]
    assert user == messages[0] and tool == messages[2]
    assert assistant == {"role": "assistant", "text": "",
                         "tool_calls": [{"id": "call_1", "name": "api_fetch", "args": {"url": "https://x.test/a"}}]}
    assert messages[1]["tool_calls"] == [call] and "native" in messages[1]          # the caller's history is kept
    assert request_json("judge", [{"role": "user", "content": "is it?"}], []) == {
        "system": "judge", "messages": [{"role": "user", "content": "is it?"}], "tools": []}


def test_a_turn_without_tool_calls_ends_and_a_json_result_text_is_read(cli, provider):
    cli.reply({"stdout": "\n".join([json.dumps({"type": "system", "subtype": "init"}),
                                     json.dumps(ok('Here it is: {"text": "Done: Ada is won.", "tool_calls": []}'))])})
    turn = provider.chat("S", [{"role": "user", "content": "task"}], TOOLS)
    assert (turn.text, turn.tool_calls, turn.stop) == ("Done: Ada is won.", [], "end")


def test_a_reply_that_breaks_the_turn_format_is_asked_again_then_fails_as_retryable(cli, provider):
    bad = ok(structured={"text": "x", "tool_calls": [{"name": "crm_lookup", "args": {}}]})
    provider.retries = 1
    cli.reply(bad, ok(structured={"text": "fine", "tool_calls": []}))
    assert provider.chat("S", [{"role": "user", "content": "task"}], TOOLS).text == "fine"
    assert len(cli.calls()) == 2
    provider.retries = 0
    for reply in (bad, ok("no JSON at all"), ok(structured={"text": "x", "tool_calls": [{"name": "api_fetch",
                                                                                         "args": "u"}]})):
        cli.reply(reply)
        with pytest.raises(ProviderError, match="turn format") as e:
            provider.chat("S", [{"role": "user", "content": "task"}], TOOLS)
        assert e.value.retryable and not e.value.unbilled and not is_account_error(e.value)


@pytest.mark.parametrize("reply", [
    {**err("You've hit your session limit · resets 3pm"), "api_error_status": 429},
    {**err("You've hit your Opus limit · resets Oct 6, 3pm"), "api_error_status": 429},
    err("You've hit your limit · resets 3pm"),
    err("You're out of usage credits · resets 3pm"),
    err("Failed to authenticate: OAuth session expired and could not be refreshed"),
    err("Invalid API key · Please run /login"),
    {**err("API Error: 401 something"), "api_error_status": 401},
    {"stdout": "", "stderr": "Error: not logged in", "exit": 1},
    ok("You've hit your session limit · resets 3pm", usage={k: 0 for k in USAGE}),
])
def test_the_subscription_limit_and_a_lost_login_pause_the_run(cli, provider, reply):
    """A person has to act (wait for the limit, log in again): an account error, never a task's failure."""
    cli.reply(reply)
    with pytest.raises(ProviderAccountError) as e:
        provider.chat("S", [{"role": "user", "content": "x"}], ())
    assert is_account_error(e.value) and not e.value.retryable


def test_a_reply_from_another_model_pauses_the_run_and_is_not_booked_as_free(cli, provider):
    cli.reply(ok("an answer", models=(MODEL, "claude-sonnet-5-5")))
    with pytest.raises(ProviderAccountError, match="claude-sonnet-5-5") as e:
        provider.chat("S", [{"role": "user", "content": "x"}], ())
    assert is_account_error(e.value) and not e.value.unbilled
    cli.reply(ok("an answer", models=(f"{MODEL}[1m]",)))           # the same model, as the CLI may name it
    assert provider.chat("S", [{"role": "user", "content": "x"}], ()).text == "an answer"
    alias = ClaudeCodeProvider("opus", binary=str(cli.binary), workdir=provider.workdir, retries=0)
    cli.reply(ok("an answer", models=("claude-opus-5-5",)))
    assert alias.chat("S", [{"role": "user", "content": "x"}], ()).text == "an answer"


def test_overload_and_server_errors_are_retryable_and_overload_is_unbilled(cli, provider):
    provider.retries = 1
    cli.reply(err("API Error: 529 Overloaded"), ok("fine"))
    assert provider.chat("S", [{"role": "user", "content": "x"}], ()).text == "fine" and len(cli.calls()) == 2
    provider.retries = 0
    for text, unbilled in (("API Error: 529 Overloaded", True), ("API Error: 429 rate_limit_error", True),
                           ("API Error: 500 Internal server error", False)):
        cli.reply(err(text))
        with pytest.raises(ProviderError) as e:
            provider.chat("S", [{"role": "user", "content": "x"}], ())
        assert e.value.retryable and e.value.unbilled is unbilled and not is_account_error(e.value), text
    # texts that mention a limit or a login but say they are transient
    for text in ("API Error: Server is temporarily limiting requests (not your usage limit) · retrying",
                 "Failed to refresh OAuth token: another Claude Code process is refreshing it or exited mid-refresh. "
                 "This is usually transient; retry in a minute"):
        cli.reply({**err(text), "api_error_status": 429})
        with pytest.raises(ProviderError) as e:
            provider.chat("S", [{"role": "user", "content": "x"}], ())
        assert e.value.retryable and e.value.unbilled and not is_account_error(e.value), text
    cli.reply(err("Prompt is too long: 215000 tokens > 200000 maximum"))     # a number is not a status code
    with pytest.raises(ProviderError) as e:
        provider.chat("S", [{"role": "user", "content": "x"}], ())
    assert not e.value.retryable and not is_account_error(e.value)


def test_a_cut_or_declined_reply_is_a_stop_the_caller_reads(cli, provider):
    """As from an API provider: a declined or cut reply is a stop reason with no text, never an answer."""
    refused = {**err("API Error: Claude Opus 5.5's safeguards flagged this message (https://www.anthropic.com/legal/"
                     "aup). This sometimes happens with safe, normal conversations."), "stop_reason": "refusal"}
    for tools in (TOOLS, ()):
        cli.reply(refused)
        turn = provider.chat("S", [{"role": "user", "content": "x"}], tools)
        assert (turn.stop, turn.text, turn.tool_calls) == ("refusal", "", [])
    cli.reply({**err("API Error: Claude's response exceeded the 32000 output token maximum."),
               "api_error": "max_output_tokens", "stop_reason": "stop_sequence"})
    assert provider.chat("S", [{"role": "user", "content": "x"}], TOOLS).stop == "max_tokens"
    cli.reply(ok("half", stop_reason="max_tokens"))
    assert provider.chat("S", [{"role": "user", "content": "x"}], ()).stop == "max_tokens"


def test_a_reply_the_cli_continued_after_the_output_limit_is_a_cut_reply(cli, tmp_path):
    """The CLI asks again by itself after a reply hits the limit, and `result` is only the last part."""
    p = ClaudeCodeProvider(MODEL, max_tokens=1000, binary=str(cli.binary), workdir=str(tmp_path / "empty"),
                           retries=0)
    plain = [{"role": "user", "content": "x"}]
    for reply in (ok("the tail", num_turns=2), ok("the tail", usage={**USAGE, "output_tokens": 1000})):
        cli.reply(reply)
        turn = p.chat("S", plain, ())
        assert (turn.stop, turn.text) == ("max_tokens", "")
    cli.reply(ok(structured={"text": "t", "tool_calls": []}, usage={**USAGE, "output_tokens": 1200}))
    assert p.chat("S", plain, TOOLS).stop == "max_tokens"
    # a structured reply takes more turns when the CLI asks again for the format: not a cut reply (smoke, 2026-10-03)
    cli.reply(ok(structured={"text": "All done: 7 mentions handled.", "tool_calls": []}, num_turns=3))
    turn = p.chat("S", plain, TOOLS)
    assert (turn.stop, turn.text) == ("end", "All done: 7 mentions handled.")
    cli.reply(ok("whole", num_turns=1, usage={**USAGE, "output_tokens": 999}))
    assert p.complete("S", "x") == "whole"


def test_a_missing_cli_or_a_directory_that_is_not_empty_pauses_before_any_call(cli, provider, tmp_path):
    missing = ClaudeCodeProvider(MODEL, binary=str(tmp_path / "nowhere" / "claude"), workdir=provider.workdir,
                                 retries=0)
    with pytest.raises(ProviderAccountError, match="not found"):
        missing.chat("S", [{"role": "user", "content": "x"}], ())
    provider.workdir.mkdir(parents=True, exist_ok=True)
    (provider.workdir / "notes.md").write_text("the answers")
    with pytest.raises(ProviderAccountError, match="must stay empty"):
        provider.chat("S", [{"role": "user", "content": "x"}], ())
    assert cli.calls() == []


def test_a_slow_reply_is_a_retryable_failure_that_may_have_been_billed(cli, provider):
    provider.timeout = 0.5
    cli.reply({"stdout": ok("late"), "sleep": 5})
    with pytest.raises(ProviderError, match="no reply within") as e:
        provider.chat("S", [{"role": "user", "content": "x"}], ())
    assert e.value.retryable and not e.value.unbilled


def test_an_answer_without_usage_is_not_booked_as_free(cli, provider):
    cli.reply(ok("an answer", usage=None))
    with pytest.raises(ProviderError, match="no usage") as e:
        provider.chat("S", [{"role": "user", "content": "x"}], ())
    assert not e.value.unbilled and not e.value.retryable


def test_effort_and_the_output_limit_reach_the_cli(cli, tmp_path):
    p = ClaudeCodeProvider(MODEL, effort="high", max_tokens=32000, binary=str(cli.binary),
                           workdir=str(tmp_path / "empty"), retries=0)
    cli.reply(ok("x"))
    p.chat("S", [{"role": "user", "content": "x"}], ())
    (call,) = cli.calls()
    assert arg(call, "--effort") == "high" and call["max_out"] == "32000"


def test_the_config_builds_it_without_running_anything(tmp_path):
    p = build_provider({"type": "claude_code", "model": MODEL, "binary": str(tmp_path / "nowhere"), "effort": "high"})
    assert isinstance(p, ClaudeCodeProvider) and (p.name, p.model, p.supports_history_edits) == \
        ("claude_code", MODEL, True)
    assert not (tmp_path / "nowhere").exists()


def test_the_startup_check_asks_for_an_effort_and_a_subscription_login(cli, tmp_path):
    table = {"binary": str(cli.binary), "effort": "medium", "workdir": str(tmp_path / "empty")}
    assert cli_problem(table) is None
    assert "set effort" in cli_problem({**table, "effort": None})
    cli.auth(False)
    assert "not logged in" in cli_problem(table)
    cli.auth(True, authMethod="console")                       # an Anthropic Console login is billed per use
    assert "not a Claude subscription" in cli_problem(table)
    cli.auth(True, apiKeySource="apiKeyHelper")                 # so is a key from Claude Code's settings
    assert "not a Claude subscription" in cli_problem(table)
    assert "not on PATH" in cli_problem({**table, "binary": str(tmp_path / "nowhere")})
    assert cli.calls() == []                         # asking about the login is not a model call
    assert os.access(cli.binary, os.X_OK)


def test_a_rate_limit_refusal_is_unbilled_retryable_and_counted(cli, provider, tmp_path, monkeypatch):
    """M5 §2 (v0.3), EX6-18: a 429/529 from the CLI is a retryable, unbilled refusal; the meter releases its
    reservation, sends the request again, counts the refusal in `rate_limited`, and books only the call that
    answered. Nothing pauses and no attempt is invalid."""
    import time
    from ouroloop.evaluate.budget import Budgets, MeteredProvider, Pricing
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    account = Budgets(tmp_path / "book").account("arm", {"model_calls": 10})
    metered = MeteredProvider(provider, account, Pricing(), operation="arm", max_retries=2)
    cli.reply(err("API Error: 429 rate_limit_error"), err("API Error: 529 Overloaded"), ok("fine"))
    assert metered.chat("S", [{"role": "user", "content": "x"}], ()).text == "fine"
    assert metered.rate_limited == 2 and len(cli.calls()) == 3
    assert [r.status for r in metered.receipts] == ["released", "released", "settled"]
    assert account.remaining()["used"]["model_calls"] == 1
    # a server error is retried too, but it is not a rate-limit refusal and is not counted as one
    cli.reply(err("API Error: 500 Internal server error"), ok("fine"))
    metered.chat("S", [{"role": "user", "content": "y"}], ())
    assert metered.rate_limited == 2 and metered.receipts[-2].status == "unknown"


# ---------------------------------------------------------------------------- stage 0/1 review (2026-10-05): the child's session
PARENT = """
import json, sys, threading
from ouroloop.providers.claude_code import ClaudeCodeProvider
p = ClaudeCodeProvider({model!r}, binary={binary!r}, workdir={workdir!r}, retries=0, timeout=60)
out, done = {{}}, threading.Event()

def call():
    try:
        out["text"] = p.complete("S", "hello")
    except BaseException as e:
        out["error"] = f"{{type(e).__name__}}: {{e}}"
    finally:
        done.set()
threading.Thread(target=call).start()
try:
    while not done.wait(0.05):
        pass
except KeyboardInterrupt:
    print("interrupted", flush=True)          # as evaluate.attempt.run_tasks: what is in flight finishes
    done.wait(30)
print(json.dumps(out), flush=True)
"""


def test_a_terminal_interrupt_reaches_the_parent_and_not_the_claude_child(cli, tmp_path):
    """Stage 0/1 review, item 5: the `claude` child runs in a session of its own, so a Ctrl-C — SIGINT to the
    foreground process group, the parent's — reaches only the parent; the call in flight finishes and its reply
    comes back (and is settled by the meter), where before the child died under the same signal and its call was
    left `unknown` at the full bound."""
    import signal
    import subprocess
    import time
    cli.reply({"stdout": ok("finished anyway"), "sleep": 2})
    script = tmp_path / "parent.py"
    script.write_text(PARENT.format(model=MODEL, binary=str(cli.binary), workdir=str(tmp_path / "empty")))
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    parent = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=env, start_new_session=True)
    try:
        for _ in range(200):                              # wait until the child has started and is sleeping
            if cli.calls():
                break
            time.sleep(0.05)
        assert cli.calls(), parent.stderr.read() if parent.poll() is not None else "the child never started"
        os.killpg(parent.pid, signal.SIGINT)             # what a terminal's Ctrl-C does
        out, err = parent.communicate(timeout=30)
    finally:
        if parent.poll() is None:
            parent.kill()
    assert "interrupted" in out, err
    assert json.loads(out.strip().splitlines()[-1]) == {"text": "finished anyway"}, err


def test_a_timeout_kills_the_child_and_what_it_started(tmp_path):
    """Stage 0/1 review, item 5: with the child in its own session the timeout no longer relies on a shared signal:
    the provider kills the child's whole process group — the `claude` program and anything it started — and the call
    is the retryable failure it always was."""
    import time
    binary = tmp_path / "claude"
    binary.write_text(f"""#!{sys.executable}
import os, subprocess, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
(here / "pids").write_text(f"{{os.getpid()}} {{child.pid}}")
time.sleep(60)
""")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    provider = ClaudeCodeProvider(MODEL, binary=str(binary), workdir=str(tmp_path / "empty"), retries=0, timeout=1.5,
                                  sleep=lambda _s: None)
    with pytest.raises(ProviderError, match="no reply within") as e:
        provider.chat("S", [{"role": "user", "content": "x"}], ())
    assert e.value.retryable
    pids = [int(p) for p in (tmp_path / "pids").read_text().split()]

    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        # an exited child not yet reaped by its parent is a zombie, not a running program
        import subprocess
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(state) and not state.startswith("Z")
    for _ in range(50):
        if not any(alive(p) for p in pids):
            break
        time.sleep(0.1)
    assert not any(alive(p) for p in pids), pids


def test_an_answer_that_is_not_a_turn_is_booked_at_what_it_used_not_at_the_bound(cli, provider, tmp_path,
                                                                                    monkeypatch):
    """The model answered (its use reported) but not as a turn — a call to a tool that was not offered: the meter
    settles the call at its measured cost, retries, and keeps nothing at the bound. A call whose failure came with no
    reported use stays unknown."""
    import time
    from ouroloop.evaluate.budget import Budgets, MeteredProvider, Pricing
    from ouroloop.runtime.chat import ToolSpec
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    account = Budgets(tmp_path / "book").account("arm", {"usd": 100.0})
    pricing = Pricing(input_per_1k=0.004, output_per_1k=0.02, cache_read_per_1k=0.0002, cache_write_per_1k=0.008,
                      max_input_tokens=1000, max_output_tokens=1000)
    metered = MeteredProvider(provider, account, pricing, operation="arm", max_retries=1)
    spec = ToolSpec("look", "Look.", {"type": "object", "properties": {}})
    cli.reply(ok(structured={"text": "", "tool_calls": [{"name": "launch", "args": {}}]}),
              ok(structured={"text": "done", "tool_calls": []}))
    assert metered.chat("S", [{"role": "user", "content": "x"}], [spec]).text == "done"
    assert [r.status for r in metered.receipts] == ["settled", "settled"]
    assert metered.receipts[0].measured == pricing.measure(dict(USAGE))


# ---------------------------------------------------------------------------- stage 4: a call with no system
def test_a_call_with_no_system_sends_a_plain_one_not_a_blank(cli, provider):
    """ReMe's calls are a single user message: the API refuses a system text that is only whitespace (400), so a call
    without one sends NO_SYSTEM, never " " (finance-v1's library lost its first step to this)."""
    from ouroloop.providers.claude_code import NO_SYSTEM
    for system in ("", "   ", None):
        cli.reply(ok("fine"))
        assert provider.chat(system, [{"role": "user", "content": "Summarise this."}], []).text == "fine"
        assert arg(cli.calls()[-1], "--system-prompt") == NO_SYSTEM
    cli.reply(ok("fine"))
    provider.chat("SYSTEM", [{"role": "user", "content": "x"}], [])
    assert arg(cli.calls()[-1], "--system-prompt") == "SYSTEM"


def test_a_request_the_api_refuses_as_malformed_is_released_not_held_at_the_bound(cli, provider, tmp_path,
                                                                                  monkeypatch):
    """A 400 (a blank text block, a prompt too long) is refused before any model runs: the meter releases its
    reservation and does not retry the same request."""
    from ouroloop.evaluate.budget import Budgets, MeteredProvider, Pricing
    account = Budgets(tmp_path / "book").account("arm", {"model_calls": 10})
    metered = MeteredProvider(provider, account, Pricing(), operation="arm", max_retries=2)
    for text in ("API Error: 400 system: text content blocks must contain non-whitespace text",
                 "API Error: 400 Prompt is too long"):
        cli.reply(err(text))
        with pytest.raises(ProviderError):
            metered.chat("S", [{"role": "user", "content": "x"}], ())
        assert metered.receipts[-1].status == "released"
    assert len(cli.calls()) == 2 and account.remaining()["used"].get("model_calls", 0) == 0
