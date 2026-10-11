"""Claude through headless Claude Code on this machine's login: a subscription, not an API key ([llm] type =
"claude_code", docs/modules/01_POLICY_RUNTIME.md §1).

Each call is one fresh `claude -p` with no tools and no local customization (CLAUDE.md, memory, skills, plugins,
hooks, MCP, settings files, attachments), in an empty directory, with no saved session, with the framework's system
prompt in place of Claude Code's own, and with the CLI's own retries and model fallbacks off. What the CLI still adds
cannot be turned off under a subscription login: a one-line Agent SDK identity before the system prompt, the account's
e-mail address and today's date as context. The actor's turn note says those are not part of the task.

A call with one user message and no tools (each research step) sends that message as it is, and the reply's text is
the answer. Any other call (the actor's turns) sends one user message of text blocks: the reply format and the offered
tools first, then one block per history entry, as JSON. Earlier blocks are sent again unchanged and new ones are
appended, so each call reads the conversation so far from the prompt cache rather than writing it again.
`--json-schema` holds the reply to {"text", "tool_calls": [{"name", "args"}]}: tool calls are structured output, not
native tool use, and a result says so (docs/ROADMAP.md §3, step 2a).
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from ..runtime.chat import ChatTurn, ToolCall, ToolSpec
from . import ProviderAccountError, ProviderError

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
# The most model requests one CLI call can make: the request, up to 3 continuations the CLI makes on its own after a
# reply hits the output limit (it cannot be turned off), and one structured-output retry. A call's reservation bounds
# all of them, so [pricing] max_output_tokens must cover this many times max_tokens (bench.startup_checks).
REQUESTS_PER_CALL = 5
# Texts of transient failures that mention a limit or a login and are not about the account.
_NOT_ACCOUNT = ("not your usage limit", "temporarily limiting requests", "usually transient",
                "another claude code process is refreshing")
# What the CLI says when a person has to act before anything sent again can succeed: a subscription limit ("You've
# hit your session limit · resets 3pm"), no usage left, a login that is missing or no longer accepted.
_ACCOUNT = ("you've hit your", "you've reached your", "out of usage", "out of extra usage", "usage limit reached",
            "usage allocation", "seat type doesn't include", "usage limit is set to $0", "requires usage credits",
            "not logged in", "run /login", "invalid api key", "oauth token has expired", "oauth token revoked",
            "oauth session expired", "failed to authenticate", "authentication_error", "permission_error",
            "credit balance")
# Turned away before the model did any work: overloaded, or a rate limit (a status code is read as a whole number).
_UNBILLED = re.compile(r"overloaded|rate[_ ]limit|\b(429|529)\b")
_TRANSIENT = re.compile(_UNBILLED.pattern + r"|\b(500|502|503|504)\b|api_error|internal server error|timed out|"
                        r"timeout|econnreset|connection|network|fetch failed|socket")
_TRUNCATED = ("output token maximum", "maximum output")
_REFUSED = ("safeguards flagged", "can't respond to this message", "can't respond to your last message",
            "can't help with this", "usage policy")
_STOP = {"max_tokens": "max_tokens", "refusal": "refusal"}
# The child sees none of these: no API key or token, nothing a Claude Code session that started this process passes
# down, no thinking budget from the shell. The billing itself is the login's: the startup check refuses any login
# that is not a Claude subscription.
_INHERITED = ("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDECODE", "MAX_THINKING_TOKENS")
_SET = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_MAX_RETRIES": "0",                   # every retry is the caller's, booked one by one (M5 §3)
        "CLAUDE_CODE_NO_MODEL_FALLBACK": "1",             # never another model's answer
        "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK": "1",      # a refusal stays a refusal
        "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",           # no files from @-mentions, no per-turn reminders
        "MAX_STRUCTURED_OUTPUT_RETRIES": "1"}

TURN_NOTE = """Below are the tools you can call, and after this block the conversation so far, one entry per block. \
In the conversation, "user" entries are messages to you, "assistant" entries are your own earlier turns with the \
tool calls you made, and "tool" entries are the results of those calls. An e-mail address or today's date attached to this request outside the \
conversation is not part of the task: use only the people, addresses and dates the conversation gives.

Write your next turn as one JSON object: {"text": "...", "tool_calls": [{"name": "<tool>", "args": {...}}]}. To call \
tools, list them in tool_calls; they run in order, and you see their results in your next turn. To end your turn \
without calling a tool, leave tool_calls empty and put your message in text."""


# The system prompt of a call that brings none (ReMe's calls are a single user message): the API refuses a system text
# block that is only whitespace (400), and leaving the flag out would give the CLI's own long system prompt.
NO_SYSTEM = "Answer the request in the user's message."
# The API refused the request as malformed before any model ran (a blank text block, a prompt too long): not billed.
_INVALID = re.compile(r"api error: 400\b|invalid_request_error|prompt is too long")


def child_env(max_tokens: int | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_INHERITED)}
    env.update(_SET)
    if max_tokens:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(int(max_tokens))
    return env


def default_workdir() -> Path:
    return Path(tempfile.gettempdir()) / "ouroloop-claude-code"


def cli_problem(table: dict) -> str | None:
    """Why a `claude_code` table cannot make a call, or None. The effort is set, so the pinned inference
    configuration fixes it rather than the CLI's settings; the CLI is on PATH and logged in with a Claude
    subscription, not an API key or a Console account billed per use. Run before a paid phase
    (bench.startup_checks); it asks the CLI about its login, not the model."""
    if not table.get("effort"):
        return "set effort (low, medium, high, xhigh or max): without it the CLI's own settings decide it"
    binary = str(table.get("binary") or "claude")
    if shutil.which(binary) is None:
        return f"the claude CLI {binary!r} is not on PATH: install Claude Code and run `claude auth login`"
    workdir = Path(table["workdir"]).expanduser() if table.get("workdir") else default_workdir()
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run([binary, "auth", "status"], capture_output=True, text=True, timeout=60,
                              env=child_env(), stdin=subprocess.DEVNULL, cwd=workdir)
        status = json.loads(done.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return f"`{binary} auth status` could not be read: {type(e).__name__}: {e}"[:300]
    if not isinstance(status, dict) or not status.get("loggedIn"):
        return f"the claude CLI {binary!r} is not logged in: run `claude auth login`"
    method, provider = status.get("authMethod"), status.get("apiProvider", "firstParty")
    if method != "claude.ai" or provider != "firstParty" or status.get("apiKeySource"):
        return (f"the claude CLI {binary!r} is logged in with {method!r} through {provider!r}"
                f"{' with an API key from ' + repr(status['apiKeySource']) if status.get('apiKeySource') else ''}, "
                f"not a Claude subscription: calls would be billed per use. Run `claude auth login --claudeai`")
    return None


def cli_version(binary: str = "claude") -> str | None:
    """The claude CLI's version (`claude --version`, no model call), or None when it cannot be read."""
    try:
        done = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60, env=child_env(),
                              stdin=subprocess.DEVNULL, cwd=default_workdir() if default_workdir().exists() else None)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (done.stdout or "").strip()[:100] or None if done.returncode == 0 else None


def turn_schema(names: list[str]) -> dict:
    """The reply format of a turn: text, and tool calls to the offered tools only."""
    if names:
        calls = {"type": "array", "items": {
            "type": "object", "properties": {"name": {"type": "string", "enum": names}, "args": {"type": "object"}},
            "required": ["name", "args"], "additionalProperties": False}}
    else:
        calls = {"type": "array", "maxItems": 0}
    return {"type": "object", "properties": {"text": {"type": "string"}, "tool_calls": calls},
            "required": ["text", "tool_calls"], "additionalProperties": False}


def _json(value) -> str:
    # "@" is written as its JSON escape, which decodes to the same text: the CLI reads " @path" in a prompt as a file
    # to attach, and a turn must not reach a file through what an earlier turn wrote.
    return json.dumps(value, ensure_ascii=False, indent=1).replace("@", "\\u0040")


def request_json(system: str, messages: list[dict], tools: list[ToolSpec]) -> dict:
    """The request as JSON: history entries as chat.py defines them, tool calls as objects, no provider's native
    content."""
    history = []
    for m in messages:
        m = {k: v for k, v in m.items() if k != "native"}
        if m.get("tool_calls"):
            m["tool_calls"] = [{"id": c.id, "name": c.name, "args": c.args} for c in m["tool_calls"]]
        history.append(m)
    return {"system": system, "messages": history,
            "tools": [{"name": t.name, "description": t.description, "schema": t.schema} for t in tools]}


def turn_blocks(system: str, messages: list[dict], tools: list[ToolSpec]) -> list[str]:
    """An actor turn's user message: the reply format and the tools, then one block per history entry. Nothing
    follows the last entry, so the next turn's request begins with this one's blocks unchanged."""
    request = request_json(system, messages, tools)
    return [f"{TURN_NOTE}\n\n<tools>\n{_json(request['tools'])}\n</tools>"] + [_json(m) for m in request["messages"]]


def result_of(stdout: str) -> dict | None:
    """The CLI's result: the last `result` line of a stream, or the one JSON object of `--output-format json`."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                return obj
    try:
        obj = json.loads(stdout)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _structured(result: dict):
    """The structured reply: the CLI's own field, else the result text read as JSON."""
    if isinstance(result.get("structured_output"), dict):
        return result["structured_output"]
    text = str(result.get("result") or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


# ---------------------------------------------------------------------------- the `claude` children
# Each call's `claude` child runs in a session of its own (stage 0/1 review, 2026-10-05): a terminal's Ctrl-C goes to
# the foreground process group, and the child is no longer in it, so it reaches only this process — whose interrupt
# handling (evaluate.attempt.run_tasks) lets the attempts in flight finish and their calls settle, instead of every child
# dying under the same signal and leaving its call `unknown` at the full reservation bound. What the parent no longer
# waits for it kills itself: a call past its timeout (the child's whole process group), a wait the parent's own
# thread was interrupted in (concurrency 1: the attempt runs on the main thread), and, at interpreter exit, any child
# still running.
_CHILDREN: set = set()
_CHILDREN_LOCK = threading.Lock()


def _kill_group(proc: subprocess.Popen) -> None:
    """The child and anything it started (its own process group, start_new_session)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


@atexit.register
def _kill_abandoned() -> None:
    with _CHILDREN_LOCK:
        live = list(_CHILDREN)
    for proc in live:
        if proc.poll() is None:
            _kill_group(proc)


def run_child(cmd: list, *, input: str, timeout: float, cwd, env) -> subprocess.CompletedProcess:
    """subprocess.run(cmd, input=..., capture_output=True, text=True, timeout=...) with the child in a session of its
    own, killed with its process group on a timeout or when the waiting thread is interrupted."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            cwd=cwd, env=env, start_new_session=True)
    with _CHILDREN_LOCK:
        _CHILDREN.add(proc)
    try:
        try:
            out, err = proc.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.communicate()
            raise
        except BaseException:
            _kill_group(proc)
            proc.wait()
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    finally:
        with _CHILDREN_LOCK:
            _CHILDREN.discard(proc)


class ClaudeCodeProvider:
    name = "claude_code"
    source = "frontier_llm"            # lineage (DESIGN §8, invariant 6); eval-only in the ledger until v2.0
    supports_history_edits = True      # the whole history is sent again each call; nothing is replayed verbatim

    def __init__(self, model: str, effort: str | None = None, max_tokens: int | None = None,
                 timeout: float = 600.0, binary: str = "claude", retries: int = 2, workdir: str | None = None,
                 sleep=time.sleep):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.binary = binary
        self.retries = retries         # attempts after a transient failure; a metered caller sets 0 and books each
        self.workdir = Path(workdir).expanduser() if workdir else default_workdir()
        self.sleep = sleep
        self.last_result: dict | None = None

    # ------------------------------------------------------------------------ the provider interface
    def complete(self, system: str, prompt: str) -> str:
        return self.chat(system, [{"role": "user", "content": prompt}], []).text

    def chat(self, system: str, messages: list[dict], tools: list[ToolSpec], effort: str | None = None) -> ChatTurn:
        """One turn. `effort` is this call's reasoning tier, passed to the CLI as --effort when given; None keeps the
        provider's own (the tier W0 pinned). The gateway (agent/gateway.py) sets it per call for an agent program
        that asks for one, already capped at K's `max_effort` (docs/modules/00_CONTRACTS.md §2)."""
        plain = not tools and len(messages) == 1 and messages[0].get("role") == "user"
        if plain:
            prompt, schema = str(messages[0].get("content", "")), None
        else:
            content = [{"type": "text", "text": block} for block in turn_blocks(system, messages, tools)]
            prompt = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
            schema = turn_schema([t.name for t in tools])
        for attempt in range(self.retries + 1):
            try:
                return self._call(system, prompt, schema, {t.name for t in tools}, effort=effort)
            except ProviderAccountError:
                raise
            except ProviderError as e:
                if not e.retryable or attempt == self.retries:
                    raise
            self.sleep(min(30.0, 2.0 ** attempt))
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------------ one CLI call
    def _command(self, system: str, schema: dict | None, effort: str | None = None) -> list[str]:
        cmd = [self.binary, "-p", "--model", self.model, "--tools", "", "--safe-mode", "--setting-sources", "",
               "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands",
               "--system-prompt", system if (system or "").strip() else NO_SYSTEM]
        if effort or self.effort:
            cmd += ["--effort", str(effort or self.effort)]
        if schema is None:
            return cmd + ["--output-format", "json"]
        # Text blocks reach the model as blocks only through a stream; a stream's output needs --verbose.
        return cmd + ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                      "--json-schema", json.dumps(schema)]

    def _empty_dir(self) -> Path:
        self.workdir.mkdir(parents=True, exist_ok=True)
        if any(self.workdir.iterdir()):
            raise ProviderAccountError(f"the claude CLI runs in {self.workdir}, which must stay empty and is not: "
                                       f"it would read what is there")
        return self.workdir

    def _call(self, system: str, prompt: str, schema: dict | None, offered: set, effort: str | None = None) -> ChatTurn:
        try:
            done = run_child(self._command(system, schema, effort), input=prompt, timeout=self.timeout,
                             cwd=self._empty_dir(), env=child_env(self.max_tokens))
        except FileNotFoundError as e:
            raise ProviderAccountError(f"the claude CLI {self.binary!r} was not found ({e}): install Claude Code "
                                       f"and run `claude auth login`") from None
        except subprocess.TimeoutExpired:
            raise ProviderError(f"the claude CLI gave no reply within {self.timeout:.0f}s", retryable=True) from None
        result = result_of(done.stdout or "")
        if not isinstance(result, dict):
            raise self._failure(f"exit {done.returncode}: {(done.stderr or done.stdout or '').strip()[-400:]}")
        self.last_result = result          # the CLI's own report of the last call, for a probe or a post-mortem
        usage = self._usage(result)
        # A refusal or a reply cut at the output limit comes back as an error that says so; the caller reads both
        # as an API provider's stop reasons.
        if str(result.get("stop_reason") or "") == "refusal":
            return ChatTurn("", [], "refusal", usage=usage or {})
        if result.get("api_error") == "max_output_tokens":
            return ChatTurn("", [], "max_tokens", usage=usage or {})
        if result.get("is_error") or done.returncode != 0 or result.get("subtype", "success") != "success":
            text = f"{result.get('subtype', '')}: {result.get('result', '')} {(done.stderr or '').strip()[-200:]}"
            lowered = text.lower()
            if any(m in lowered for m in _REFUSED):
                return ChatTurn("", [], "refusal", usage=usage or {})
            if any(m in lowered for m in _TRUNCATED):
                return ChatTurn("", [], "max_tokens", usage=usage or {})
            if "structured_output" in lowered or "structured output" in lowered:
                # the model answered and the CLI gave up on the format: what it used is reported, and booked
                raise ProviderError(f"the reply did not follow the turn format: {text[:300]}", retryable=True,
                                    usage=usage)
            raise self._failure(text, result.get("api_error_status"))
        answer = str(result.get("result", "")).lower()
        if not (usage or {}).get("output_tokens") and any(m in answer for m in _ACCOUNT) \
                and not any(m in answer for m in _NOT_ACCOUNT):
            raise ProviderAccountError(f"claude CLI: {str(result.get('result'))[:300]}")   # a limit, not an answer
        try:
            self._check_model(result)
        except ProviderError as e:
            e.usage = usage                # the call ran (another model answered): booked at what it used
            raise
        if usage is None:
            # A call settled without usage would be booked as free; it is booked at its bound instead.
            raise ProviderError("the claude CLI reported no usage for a call it answered")
        if self._continued(result, usage, schema):
            # The CLI went on by itself after the output limit, and `result` is only the last part: a cut reply.
            return ChatTurn("", [], "max_tokens", usage=usage)
        stop = _STOP.get(str(result.get("stop_reason") or ""))
        if schema is None:
            return ChatTurn(str(result.get("result") or ""), [], stop or "end", usage=usage)
        if stop:
            return ChatTurn("", [], stop, usage=usage)
        try:
            text, calls = self._turn(_structured(result), offered)
        except ProviderError as e:
            e.usage = usage                # an answer that is not a turn: it was paid for, and is booked at its use
            raise
        return ChatTurn(text, calls, "tool_use" if calls else "end", usage=usage)

    def _continued(self, result: dict, usage: dict, schema: dict | None) -> bool:
        """Whether the CLI asked the model again after the output limit: no single request can reach max_tokens
        output and still have finished, and a plain call needs one request. A structured reply may take more turns
        without being cut (the CLI asks again for a reply that did not use the format), so for it only the output
        counts."""
        if self.max_tokens and usage.get("output_tokens", 0) >= int(self.max_tokens):
            return True
        return schema is None and int(result.get("num_turns") or 1) > 1

    @staticmethod
    def _failure(text: str, status=None) -> ProviderError:
        lowered = text.lower()
        if any(m in lowered for m in _NOT_ACCOUNT):
            return ProviderError(f"claude CLI: {text[:300]}", retryable=True, unbilled=True)
        if any(m in lowered for m in _ACCOUNT) or status in (401, 403):
            return ProviderAccountError(f"claude CLI: {text[:300]}")
        if status == 400 or _INVALID.search(lowered):
            return ProviderError(f"claude CLI: {text[:300]}", unbilled=True)
        if status in (429, 529) or _UNBILLED.search(lowered):
            return ProviderError(f"claude CLI: {text[:300]}", retryable=True, unbilled=True)
        if (isinstance(status, int) and status >= 500) or _TRANSIENT.search(lowered):
            return ProviderError(f"claude CLI: {text[:300]}", retryable=True)
        return ProviderError(f"claude CLI: {text[:300]}")

    def _check_model(self, result: dict) -> None:
        """The answer comes from the configured model. A subscription can move a session to another model when a
        limit is reached; such a reply must not count as this model's, so the run stops until a person acts."""
        used = sorted(result.get("modelUsage") or {})
        if not used or not self.model.startswith("claude-"):
            return
        other = [m for m in used if not m.startswith(self.model)]
        if other:
            error = ProviderAccountError(f"the claude CLI answered with {other}, not {self.model}")
            error.unbilled = False          # the call ran: its reservation is kept, not released
            raise error

    @staticmethod
    def _usage(result: dict) -> dict | None:
        raw = result.get("usage")
        if not isinstance(raw, dict):
            return None
        usage = {k: int(raw.get(k) or 0) for k in USAGE_KEYS}
        if isinstance(result.get("total_cost_usd"), (int, float)):
            usage["cli_cost_usd"] = float(result["total_cost_usd"])
        return usage

    @staticmethod
    def _turn(reply, offered: set) -> tuple[str, list[ToolCall]]:
        def bad(why: str):
            raise ProviderError(f"the reply did not follow the turn format: {why}", retryable=True)

        if not isinstance(reply, dict):
            bad("no JSON object")
        text, calls = reply.get("text", ""), reply.get("tool_calls") or []
        if not isinstance(text, str):
            bad('"text" is not a string')
        if not isinstance(calls, list):
            bad('"tool_calls" is not a list')
        out = []
        for c in calls:
            if not isinstance(c, dict) or c.get("name") not in offered:
                bad(f"a call to a tool that was not offered: {json.dumps(c)[:120]}")
            args = c.get("args", {})
            if not isinstance(args, dict):
                bad(f"the args of {c['name']} are not an object")
            out.append(ToolCall(f"toolu_{uuid.uuid4().hex[:24]}", c["name"], args))
        return text, out
