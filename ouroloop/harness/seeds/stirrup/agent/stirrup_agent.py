"""Stirrup's agent loop as this package's agent program: run(ctx, task) -> str (harness_package.v3).

Stirrup 0.2.0 (Artificial Analysis, MIT) is vendored unmodified under vendor/stirrup/, so R can read and edit the loop
itself. This file is the only seam between Stirrup and the host (docs/design/HARNESS.md §5, §7). It uses the names of
the package contract and nothing else:

  LLMClient  -> CtxClient: Stirrup's history -> ctx.model(messages, tools, role, effort) in the gateway's shapes
                (system / user / assistant{text, tool_calls} / tool{results}), the reply back as an AssistantMessage,
                with the input tokens the host reports in ctx.view["last_usage"].
  Tool       -> base tools through ctx.tool, package tools through their entrypoints, read_skill through ctx.skills,
                recall through ctx.memory, http_fetch only when the worker has egress.
  hooks      -> the six sites, dispatched here (SITES): before_run (system prompt and opening messages), before_model
                (the messages of each act call, non-destructively), before_tool (block or rewrite the arguments),
                after_tool (rewrite the output, add feedback), after_turn (a message after the turn's results) and
                on_finish (push the finish back, at most MAX_PUSHBACKS times and never on the last act call).
  logger     -> CtxLogger: the summary flag and ctx.mark; prints nothing (fd 1 is the protocol's).

What Stirrup brings: its finish tool (a turn without tool calls is not a finish), its turns-remaining warnings, its
context summarization (at `config.summarize_at` of the window, asked as role "aux") with context-overflow unwinding,
sub-agents (Agent.to_tool), and its system prompt template around ctx.view["system"]. Not used: its clients, skills
loader, code backends, web tools (they ignore the proxy), cache and SIGINT handler.

A hook gets a ctx without `model` and with a read-only `tool`; a package tool gets one without `model`, whose `tool`
is read-only when the manifest says the tool does not write. A hook or package tool that raises, or a hook that answers
outside its site's protocol, ends the run as the package's defect (HookError, PackageToolError).

The seven fixes over the 2026-10-08 prototype, each with its test (tests/test_stirrup_adapter.py):
  1. an awaitable hook or tool runs on a persistent helper loop when this thread already runs Stirrup's loop;
  2. finish honours a before_tool block earlier in the same turn ("Not executed", mark tool_skipped);
  3. finish takes `reason` alone (Stirrup's FinishParams makes `paths` required, and there is no file system);
  4. a summary request is known by the logger's callbacks, not by its prompt, so R may edit the summarizer prompt;
  5. TokenUsage.input is the input the host reports (input + cache read + cache creation), else chars / 4;
  6. the IMPLICIT_FINISH_AFTER-th text-only act reply in a row is turned into a finish call (mark implicit_finish);
  7. the run ends with mark `stirrup_end` (finished, implicit, turns, summaries, pushbacks, maxrss_mb).

Records: the gateway and the broker write every model and tool call host-side; this file marks only what they cannot
see: a hook that acted (mark "hook", turned into the host's `hook` line), a summary, a pushback, a blocked or skipped
call, an implicit finish, a package tool's or a Stirrup-native tool's run, an http_fetch, a sub-agent's start and end,
and the run's end.
"""
import asyncio
import importlib
import importlib.util
import inspect
import json
import os
import re
import resource
import sys
import threading
import time
import uuid
from typing import Annotated

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the loaded bundle's root
VENDOR = os.path.join(ROOT, "vendor")
# stirrup.core.models imports moviepy, which imports numpy, whose OpenBLAS starts one thread per host CPU at import.
# Under the worker's pids limit that import fails once the host has more CPUs than the limit leaves: one thread each.
THREAD_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
for _var in THREAD_VARS:
    os.environ.setdefault(_var, "1")
if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)
import stirrup  # noqa: E402

if not os.path.abspath(stirrup.__file__).startswith(VENDOR + os.sep):
    # A stirrup imported from the image's site-packages would run other bytes than the package's: refuse.
    raise ImportError(f"stirrup must come from this package's vendor/stirrup, not {stirrup.__file__}")

from pydantic import BaseModel, ConfigDict, Field, ValidationError  # noqa: E402
from stirrup import Agent  # noqa: E402
from stirrup.core.agent import SubAgentParams  # noqa: E402
from stirrup.core.exceptions import ContextOverflowError  # noqa: E402
from stirrup.core.models import (AssistantMessage, EmptyParams, SystemMessage, TextBlock, TokenUsage,  # noqa: E402
                                 Tool, ToolCall, ToolMessage, ToolResult, UserMessage, joined_text, tool_call_blocks)
from stirrup.tools.finish import SIMPLE_FINISH_TOOL  # noqa: E402
from stirrup.tools.finish import FinishParams as UpstreamFinishParams  # noqa: E402
from stirrup.utils.logging import AgentLoggerBase  # noqa: E402

# ---------------------------------------------------------------------------- the contract (harness/contract.py)
SITES = ("before_run", "before_model", "before_tool", "after_tool", "after_turn", "on_finish")
RETURNS = {"before_run": ("system", "messages"), "before_model": ("messages",), "before_tool": ("block", "args"),
           "after_tool": ("output", "feedback"), "after_turn": ("message",), "on_finish": ("pushback",)}

# ---------------------------------------------------------------------------- knobs R may change
CONTEXT_WINDOW_TOKENS = 200_000       # the actor's window; Stirrup summarizes at config.summarize_at of it
MAX_TURNS = 50                        # config.max_turns overrides; the host's act-call limit caps it
SUMMARIZE_AT = 0.7                    # config.summarize_at overrides
SUMMARY_ROLE = "aux"                  # the manifest declares it; "act" would spend act calls on summaries
CHARS_PER_TOKEN = 4                   # input-token estimate when the host reports no usage
IMPLICIT_FINISH_AFTER = 2             # text-only act replies in a row that end the run as a finish (fix 6)
MAX_PUSHBACKS = 2                     # how often on_finish hooks may send the model back to work
PUSHBACK = "{reason}\nKeep working on the task, or explain why it is already done."
HOOK_DETAIL_CHARS = 300               # of a hook's answer, in its mark
TURN_RESULT_CHARS = 4000              # of a tool's output, in the after_turn event
MAX_TOOL_OUTPUT = 20000               # when the view gives no max_output_chars
NOTE_CHARS = 4000                     # feedback shown after a turn's results
READ_SKILL = "read_skill"
RECALL = "recall"
STIRRUP_TOOLS = ()                    # Stirrup's own tools to offer, by name of NATIVE_TOOLS; none in the seed
NATIVE_TOOLS = {"calculator": ("stirrup.tools.calculator", "CALCULATOR_TOOL")}
STIRRUP_TOOL_CHARS = 2000             # of a native tool's output, in its mark
EGRESS_FLAG = "OUROLOOP_EGRESS"       # "1" in the worker's environment when K's network is the proxy
HTTP_FETCH = "agent/http_fetch.py"
# Sub-agents to offer the model when config.subagent_depth >= 1, each {"name", "description", "system_prompt",
# optional "max_turns" and "tools" (the names it may call; all but the sub-agents by default)}; none in the seed.
SUB_AGENTS = ()
SUB_AGENT_MAX_TURNS = 20
OVERFLOW = re.compile(r"prompt is too long|context.{0,24}(length|window|limit)|too many (input )?tokens", re.I)
INPUT_KEYS = (("input_tokens",), ("cache_read_input_tokens", "cache_read_tokens"),
              ("cache_creation_input_tokens", "cache_write_tokens"))
FINISH = SIMPLE_FINISH_TOOL.name


class HookError(Exception):
    """A hook raised or answered outside its site's protocol: the package's defect (the host reads the flag)."""
    ouroloop_defect = True


class PackageToolError(Exception):
    """A package tool raised or answered something that is not a tool result: the package's defect."""
    ouroloop_defect = True


# ---------------------------------------------------------------------------- the loaded bundle
class Bundle:
    """The package's code beside this file, loaded by path (no sys.path entry), once each per attempt."""

    def __init__(self):
        self.modules = {}

    def entry(self, spec: str):
        module_path, _, func = spec.partition(":")
        if module_path not in self.modules:
            loader = importlib.util.spec_from_file_location(
                "agentpkg_" + module_path.replace("/", "_").replace(".py", ""), os.path.join(ROOT, module_path))
            if loader is None or loader.loader is None:
                raise ImportError(f"cannot load {module_path}")
            module = importlib.util.module_from_spec(loader)
            loader.loader.exec_module(module)
            self.modules[module_path] = module
        fn = getattr(self.modules[module_path], func, None)
        if not callable(fn):
            raise AttributeError(f"{spec}: no callable {func}")
        return fn


# ---------------------------------------------------------------------------- fix 1: awaitables under a running loop
class _HelperLoop:
    """One daemon thread with its own event loop, started on first use. An async hook or package tool called from
    inside Stirrup's running loop runs to completion here while the loop's thread waits, so ctx calls stay one at a
    time."""

    def __init__(self):
        self.loop = None
        self.thread = None
        self._lock = threading.Lock()

    def run(self, awaitable):
        with self._lock:
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                self.thread = threading.Thread(target=self.loop.run_forever, name="stirrup-agent-helper", daemon=True)
                self.thread.start()
        if threading.current_thread() is self.thread:
            raise RuntimeError("an awaitable package entrypoint called another one from inside its own coroutine")
        return asyncio.run_coroutine_threadsafe(awaitable, self.loop).result()


HELPER = _HelperLoop()


async def _await(value):
    return await value


def _call(fn, *args):
    """Package entrypoints may be plain or async functions."""
    value = fn(*args)
    if inspect.isawaitable(value):
        coroutine = value if asyncio.iscoroutine(value) else _await(value)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        return HELPER.run(coroutine)
    return value


# ---------------------------------------------------------------------------- ctx: one lock around the host
def _get(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


class _Locked:
    def __init__(self, target, lock):
        self._target, self._lock = target, lock


class _LockedSkills(_Locked):
    @property
    def index(self):
        return self._target.index

    def read(self, name):
        with self._lock:
            return self._target.read(name)


class _LockedMemory(_Locked):
    def recall(self, query, k=None):
        with self._lock:
            return self._target.recall(query, k)


class _NoMemory:
    def recall(self, query, k=None):
        return []


class LockedCtx:
    """The worker's ctx with one lock around every call that goes to the host: the pipe carries one exchange at a
    time. `view` and `limits` are the ctx's own, live."""

    def __init__(self, ctx):
        self._ctx, self._lock = ctx, threading.Lock()
        self.skills = _LockedSkills(ctx.skills, self._lock)
        memory = getattr(ctx, "memory", None)
        self.memory = _LockedMemory(memory, self._lock) if memory is not None else _NoMemory()

    @property
    def view(self):
        return self._ctx.view

    @property
    def limits(self):
        return self._ctx.limits

    def model(self, messages, tools=(), role="act", effort=None):
        with self._lock:
            return self._ctx.model(messages, tools, role=role, effort=effort)

    def tool(self, name, args=None, *, read_only=False):
        with self._lock:
            return self._ctx.tool(name, args, read_only=read_only) if read_only else self._ctx.tool(name, args)

    def mark(self, kind, **data):
        with self._lock:
            return self._ctx.mark(kind, **data)


AUX_SITES = ("after_turn", "on_finish")   # harness/contract.py AUX_SITES: hooks there may call the aux model


class SubCtx:
    """What a hook or a package tool holds: the view with the turn, the limits, skills, memory, mark, and `tool`
    (read-only for a hook and for a tool that does not write). `model` is a hook's at after_turn and on_finish only,
    role aux, at most view["hook_aux_calls"] calls per attempt across those hooks (an audit before the agent
    finishes); a tool, a hook at another site or a call past the cap is the package's defect (HookError)."""

    def __init__(self, ctx, turn: int, *, read_only: bool, site: str | None = None, aux=None):
        self.view = {**ctx.view, "turn": turn}
        self.limits, self.skills, self.memory = ctx.limits, ctx.skills, ctx.memory
        self._ctx, self._read_only, self._site, self._aux = ctx, read_only, site, aux
        self.calls = []

    def tool(self, name, args=None, *, read_only=False):
        res = self._ctx.tool(name, dict(args or {}), read_only=self._read_only or read_only)
        self.calls.append({"tool": name, "is_error": bool(_get(res, "is_error"))})
        return res

    def model(self, messages, tools=(), role="aux", effort=None):
        if self._site is None or self._aux is None:
            raise HookError("a package tool has no model: tools are deterministic code")
        if self._site not in AUX_SITES:
            raise HookError(f"a {self._site} hook has no model: only after_turn and on_finish hooks may call the aux "
                            f"model")
        cap = int(self.view.get("hook_aux_calls") or 0)
        if self._aux["calls"] >= cap:
            raise HookError(f"hook aux calls: the package's hooks made {self._aux['calls']} aux calls this attempt, "
                            f"the cap (hook_aux_calls)")
        self._aux["calls"] += 1
        self._ctx.mark("hook_aux_call", site=self._site, turn=self.view.get("turn"), call=self._aux["calls"])
        return self._ctx.model(messages, tools, role="aux", effort=effort)

    def mark(self, kind, **data):
        self._ctx.mark(kind, **data)


# ---------------------------------------------------------------------------- hooks
def _messages(value) -> bool:
    return isinstance(value, list) and all(isinstance(m, dict) and isinstance(m.get("role"), str) for m in value)


def _action(site: str, value) -> str:
    """The action a hook's answer is (none, block, modify, feedback, pushback), or HookError: harness/contract.py's
    action_of, which the host applies to the same answers (tests check the two agree)."""
    if value is None:
        return "none"
    if not isinstance(value, dict):
        raise HookError(f"a {site} hook answers a dict or None, not {type(value).__name__}")
    extra = sorted(set(value) - set(RETURNS[site]))
    if extra:
        raise HookError(f"a {site} hook may answer {list(RETURNS[site])}, not {extra}")
    if not value:
        return "none"
    if site == "before_run":
        if "system" in value and not isinstance(value["system"], str):
            raise HookError("before_run: system is a string")
        if "messages" in value and not _messages(value["messages"]):
            raise HookError("before_run: messages is a list of {role, content}")
        return "modify"
    if site == "before_model":
        if not _messages(value["messages"]):
            raise HookError("before_model: messages is a list of message objects")
        return "modify"
    if site == "before_tool":
        if "block" in value and "args" in value:
            raise HookError("before_tool: block or args, not both")
        if "block" in value:
            block = value["block"]
            if not isinstance(block, dict) or set(block) != {"reason"} or not isinstance(block["reason"], str):
                raise HookError("before_tool: block is {reason: str}")
            return "block"
        if not isinstance(value["args"], dict):
            raise HookError("before_tool: args is an object")
        return "modify"
    if site == "after_tool":
        for key in ("output", "feedback"):
            if key in value and not isinstance(value[key], str):
                raise HookError(f"after_tool: {key} is a string")
        return "feedback" if "feedback" in value and "output" not in value else "modify"
    if site == "after_turn":
        if not isinstance(value["message"], str):
            raise HookError("after_turn: message is a string")
        return "feedback"
    if not isinstance(value["pushback"], str):
        raise HookError("on_finish: pushback is a string")
    return "pushback"


class Hooks:
    """The package's enabled hooks by site, in manifest order. Each answer is checked against its site's protocol;
    every hook that acts is marked ("hook": site, name, action, detail), which the host records as a `hook` line."""

    def __init__(self, ctx, bundle: Bundle, hooks):
        self.ctx, self.bundle = ctx, bundle
        self.by_site = {s: [h for h in hooks or () if h.get("site") == s and h.get("enabled", True)] for s in SITES}
        self.aux = {"calls": 0}         # the attempt's aux calls by hooks, against view["hook_aux_calls"]

    def has(self, site: str) -> bool:
        return bool(self.by_site[site])

    def _fire(self, site: str, hook: dict, event: dict, turn: int):
        hctx = SubCtx(self.ctx, turn, read_only=True, site=site, aux=self.aux)
        try:
            value = _call(self.bundle.entry(hook["entrypoint"]), hctx, {"site": site, **event})
        except HookError:
            raise
        except Exception as e:  # noqa: BLE001 - the hook's own failure is the package's defect
            raise HookError(f"hook {hook['name']} at {site} raised {type(e).__name__}: {str(e)[:300]}") from e
        try:
            action = _action(site, value)
        except HookError as e:
            raise HookError(f"hook {hook['name']} at {site}: {e}") from None
        if action != "none":
            self.ctx.mark("hook", site=site, name=hook["name"], action=action,
                          detail=json.dumps(value, ensure_ascii=False, default=str)[:HOOK_DETAIL_CHARS],
                          turn=turn, broker_calls=hctx.calls)
        return value, action

    def before_run(self, task: str, system: str) -> tuple[str, list]:
        added = []
        for hook in self.by_site["before_run"]:
            value, action = self._fire("before_run", hook, {"task": task, "system": system, "messages": list(added)},
                                       0)
            if action == "modify":
                system = value.get("system", system)
                added += [dict(m) for m in value.get("messages") or ()]
        return system, added

    def before_model(self, turn: int, role: str, messages: list, tools: list) -> list:
        for hook in self.by_site["before_model"]:
            value, action = self._fire("before_model", hook, {"turn": turn, "role": role, "messages": messages,
                                                              "tools": tools}, turn)
            if action == "modify":
                messages = [dict(m) for m in value["messages"]]
        return messages

    def before_tool(self, turn: int, call_id, name: str, args: dict) -> tuple[str | None, dict]:
        for hook in self.by_site["before_tool"]:
            value, action = self._fire("before_tool", hook, {"turn": turn, "call_id": call_id, "name": name,
                                                             "args": dict(args)}, turn)
            if action == "block":
                return value["block"]["reason"] or f"{hook['name']} asks you to check before going on", args
            if action == "modify":
                args = dict(value["args"])
        return None, args

    def after_tool(self, turn: int, call_id, name: str, args: dict, output: str, is_error: bool) -> tuple[str, list]:
        feedback = []
        for hook in self.by_site["after_tool"]:
            value, action = self._fire("after_tool", hook, {"turn": turn, "call_id": call_id, "name": name,
                                                            "args": dict(args), "output": output,
                                                            "is_error": is_error}, turn)
            if action in ("modify", "feedback"):
                output = value.get("output", output)
                if value.get("feedback"):
                    feedback.append(value["feedback"])
        return output, feedback

    def after_turn(self, turn: int, tool_results: list) -> list:
        out = []
        for hook in self.by_site["after_turn"]:
            value, action = self._fire("after_turn", hook, {"turn": turn, "tool_results": list(tool_results)}, turn)
            if action == "feedback" and value["message"]:
                out.append(value["message"])
        return out

    def on_finish(self, turn: int, reason: str, final_text: str, pushbacks: int) -> str | None:
        for hook in self.by_site["on_finish"]:
            value, action = self._fire("on_finish", hook, {"turn": turn, "reason": reason, "final_text": final_text,
                                                           "pushbacks": pushbacks}, turn)
            if action == "pushback":
                return value["pushback"] or f"{hook['name']} asks you to check before finishing"
        return None


# ---------------------------------------------------------------------------- Stirrup history <-> gateway shapes
class AnyArgs(BaseModel):
    """Arguments as the model sent them: the broker and package tools check their own schemas."""
    model_config = ConfigDict(extra="allow")


class SpecTool(Tool):
    """A Stirrup Tool that carries the JSON schema the model is shown (a base tool's or a package tool's own)."""
    raw_schema: dict | None = None


class FinishArgs(BaseModel):
    """Explanation for why the task is complete or cannot proceed."""
    # fix 3: `reason` alone is required; Stirrup's descriptions are read from its own model, so an edit there shows
    reason: Annotated[str, Field(description=UpstreamFinishParams.model_fields["reason"].description)]
    paths: Annotated[list[str], Field(default_factory=list,
                                      description=UpstreamFinishParams.model_fields["paths"].description)]


class Refused(Exception):
    """The actor refused (stop "refusal"): the attempt ends with the text."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(c if isinstance(c, str) else getattr(c, "text", None) or f"[{type(c).__name__} omitted]"
                     for c in content or ())


def _args(raw: str) -> dict:
    try:
        value = json.loads(raw) if raw and raw.strip() else {}
    except json.JSONDecodeError:
        return {"_unparsed": raw[:2000]}
    return value if isinstance(value, dict) else {"_value": value}


def tool_specs(tools: dict) -> list[dict]:
    specs = []
    for t in tools.values():
        schema = getattr(t, "raw_schema", None)
        if schema is None:
            schema = {"type": "object", "properties": {}} if t.parameters is EmptyParams else \
                t.parameters.model_json_schema()
        specs.append({"name": t.name, "description": t.description, "schema": schema})
    return specs


def _maxrss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1 << 20 if sys.platform == "darwin" else 1 << 10),
                 1)


def _turns_left(ctx) -> bool:
    """Whether another act call is allowed after this one (None: no such limit)."""
    left = (ctx.limits or {}).get("act_calls")
    return left is None or left > 0


class CtxClient:
    """stirrup.core.models.LLMClient over ctx.model. One call at a time (the pipe carries one exchange)."""

    def __init__(self, ctx, *, effort=None):
        self.ctx, self.effort = ctx, effort
        self.hooks = None                      # Hooks, set once the tools exist
        self.turn = 0                          # act calls answered so far
        self.current_id = None                 # the AssistantMessage whose tool calls are running now
        self.notes: dict[str, str] = {}        # assistant id -> feedback shown after its results
        self.summaries = 0
        self.summarizing = False               # fix 4: set by CtxLogger between start and complete
        self.summary_calls = 0
        self.text_only = 0                     # fix 6: act replies in a row with text and no tool call
        self.implicit_ids: set[str] = set()
        self.pending: list[tuple[str, str]] = []   # (tool name, call id) of this turn, in Stirrup's execution order

    @property
    def model_slug(self) -> str:
        return "ouroloop-gateway"

    @property
    def context_window_tokens(self) -> int:
        return CONTEXT_WINDOW_TOKENS

    def add_note(self, owner, lines) -> None:
        if lines:
            prior = self.notes.get(owner)
            self.notes[owner] = "\n".join(([prior] if prior else []) + list(lines))[:NOTE_CHARS]

    def convert(self, messages) -> list[dict]:
        out: list[dict] = []
        pending_note = None
        for m in messages:
            if not isinstance(m, ToolMessage) and pending_note:
                out.append({"role": "user", "content": pending_note})
                pending_note = None
            if isinstance(m, SystemMessage):
                text = _text(m.content)
                # The gateway takes the first system message as the provider's system; a later one (a sub-agent's
                # own prompt) is shown as a user message rather than dropped.
                out.append({"role": "system", "content": text} if not out else
                           {"role": "user", "content": f"[instructions]\n{text}"})
            elif isinstance(m, AssistantMessage):
                calls = [{"id": c.tool_call_id, "name": c.name, "args": _args(c.arguments)}
                         for c in tool_call_blocks(m.blocks)]
                out.append({"role": "assistant", "text": joined_text(m.blocks) or "", "tool_calls": calls})
                pending_note = self.notes.get(m.id)
            elif isinstance(m, ToolMessage):
                result = {"id": m.tool_call_id, "name": m.name or "", "output": _text(m.content),
                          "is_error": not m.success}
                if out and out[-1]["role"] == "tool":
                    out[-1]["results"].append(result)       # one tool entry per turn
                else:
                    out.append({"role": "tool", "results": [result]})
            else:                                           # UserMessage, SummaryMessage, TurnWarningMessage
                out.append({"role": "user", "content": _text(m.content)})
        if pending_note:
            out.append({"role": "user", "content": pending_note})
        return out

    def turn_state(self) -> tuple:
        return self.current_id, list(self.pending), self.text_only, self.summarizing

    def restore_turn(self, state: tuple) -> None:
        self.current_id, pending, self.text_only, self.summarizing = state
        self.pending = list(pending)

    def take_call_id(self, name: str) -> str | None:
        """The id of the next call of `name` this turn: Stirrup hands an executor its arguments only, and runs the
        calls in a known order (every ordinary call, then finish)."""
        for i, (n, call_id) in enumerate(self.pending):
            if n == name:
                del self.pending[i]
                return call_id
        return None

    def usage(self, reply, sent, specs, before) -> TokenUsage:
        """fix 5: the input the host reports for this call, else an estimate from what was sent."""
        last = self.ctx.view.get("last_usage")
        inputs = output = 0
        if isinstance(last, dict):
            for names in INPUT_KEYS:
                inputs += int(next((last[k] for k in names if last.get(k) is not None), 0) or 0)
            output = int(last.get("output_tokens") or 0)
        if not inputs:
            inputs = (len(json.dumps(sent)) + len(json.dumps(specs))) // CHARS_PER_TOKEN
        if not output:
            after = (self.ctx.limits or {}).get("tokens")
            output = before - after if isinstance(before, int) and isinstance(after, int) else \
                len(_get(reply, "text") or "") // CHARS_PER_TOKEN
        return TokenUsage(input=inputs, answer=max(output, 0))

    async def generate(self, messages, tools) -> AssistantMessage:
        role = SUMMARY_ROLE if self.summarizing else "act"
        sent, specs = self.convert(messages), tool_specs(tools)
        if role == "act" and self.hooks is not None and self.hooks.has("before_model"):
            sent = self.hooks.before_model(self.turn + 1, role, sent, specs)
        if role == SUMMARY_ROLE:
            self.summary_calls += 1
            self.ctx.mark("stirrup_summary", phase="call", call=self.summary_calls, turn=self.turn,
                          messages=len(sent), tools=len(specs))
        before = (self.ctx.limits or {}).get("tokens")
        self.ctx.view.pop("last_usage", None)           # what the host reports for this call, not the last one's
        started = time.perf_counter()
        try:
            reply = self.ctx.model(sent, specs, role=role, effort=self.effort)
        except RuntimeError as e:              # the gateway refused the call
            if OVERFLOW.search(str(e)):
                raise ContextOverflowError(str(e)) from e
            raise
        ended = time.perf_counter()
        if role == "act":
            self.turn += 1
        text, stop = _get(reply, "text") or "", _get(reply, "stop")
        if stop == "refusal":
            raise Refused(text)
        usage = self.usage(reply, sent, specs, before)
        calls = [dict(c) for c in _get(reply, "tool_calls") or ()]
        implicit = False
        if role == "act":
            if calls or stop not in ("end", "other"):
                self.text_only = 0
            else:
                self.text_only += 1
                # fix 6: a turn without tool calls would be answered "Please continue the task" up to the turn limit;
                # the n-th such reply in a row is a finish with its text as the reason, and passes on_finish
                if self.text_only >= IMPLICIT_FINISH_AFTER and FINISH in tools:
                    implicit = True
                    calls = [{"id": f"implicit_{uuid.uuid4().hex[:16]}", "name": FINISH,
                              "args": {"reason": text, "paths": []}}]
        blocks = [TextBlock(text=text)] if text else []
        blocks += [ToolCall(tool_call_id=c.get("id") or uuid.uuid4().hex, name=c["name"],
                            arguments=json.dumps(c.get("args") or {})) for c in calls]
        message = AssistantMessage(blocks=blocks, token_usage=usage, request_start_time=started,
                                   request_end_time=ended, metadata={"stop": stop, "role": role})
        if role == "act":
            self.current_id = message.id
            ordered = sorted(tool_call_blocks(message.blocks), key=lambda c: c.name == FINISH)
            self.pending = [(c.name, c.tool_call_id) for c in ordered]
            if implicit:
                self.implicit_ids.add(message.id)
                self.ctx.mark("implicit_finish", turn=self.turn, text_only=self.text_only, chars=len(text),
                              call_id=calls[0]["id"])
        return message


class CtxLogger(AgentLoggerBase):
    """Stirrup's logger seam. Prints nothing; raises and lowers the summary flag (fix 4); marks the summaries; and,
    for the attempt's own loop (not a sub-agent's), runs the after_turn hooks once a turn's tools have run."""

    def __init__(self, ctx, client: CtxClient, tools=None, *, sub: bool = False):
        self.ctx, self.client, self.tools, self.sub = ctx, client, tools, sub
        self.name, self.model, self.max_turns, self.depth = "", None, None, 0
        self.finish_params = self.run_metadata = self.output_dir = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def on_step(self, step, tool_calls=0, input_tokens=0, output_tokens=0):
        if self.sub or self.tools is None:
            return
        results = self.tools.take_turn_results()
        if self.tools.hooks.has("after_turn"):
            self.client.add_note(self.client.current_id, self.tools.hooks.after_turn(self.client.turn, results))

    def assistant_message(self, turn, max_turns, assistant_message):
        pass

    def user_message(self, user_message):
        pass

    def task_message(self, task):
        pass

    def tool_result(self, tool_message):
        pass

    def context_summarization_start(self, pct_used, cutoff):
        self.client.summarizing, self.client.summary_calls = True, 0
        self.ctx.mark("stirrup_summary", phase="start", pct_used=round(pct_used, 3), cutoff=cutoff,
                      turn=self.client.turn)

    def context_summarization_complete(self, summary, bridge):
        self.client.summarizing = False
        self.client.summaries += 1
        self.ctx.mark("stirrup_summary", phase="done", chars=len(summary), calls=self.client.summary_calls,
                      turn=self.client.turn)

    def debug(self, message, *args):
        pass

    info = warning = error = debug


def _plain(value, what: str) -> tuple[str, bool]:
    """A package tool's answer as (output, is_error): {output, is_error}, a str, or an object with those."""
    if isinstance(value, str):
        return value, False
    if isinstance(value, dict) and isinstance(value.get("output"), str):
        return value["output"], bool(value.get("is_error"))
    output = getattr(value, "output", None)
    if isinstance(output, str):
        return output, bool(getattr(value, "is_error", False))
    raise PackageToolError(f"tool {what} answered {type(value).__name__}, not a tool result")


# ---------------------------------------------------------------------------- the tools the model is offered
class Tools:
    """Every tool the model is offered, with the hook sites around each call: before_tool may block the call and the
    rest of the turn (finish included) or rewrite its arguments; after_tool may rewrite the output and add feedback,
    shown as one user message after the turn's results. on_finish wraps the finish tool: a pushback answers the
    finish success=False, so Stirrup goes on, at most MAX_PUSHBACKS times and never on the last act call."""

    def __init__(self, ctx, client: CtxClient):
        self.ctx, self.client = ctx, client
        view = ctx.view
        package = view.get("package") or {}
        self.config = dict(package.get("config") or {})
        self.bundle = Bundle()
        self.hooks = Hooks(ctx, self.bundle, package.get("hooks") or ())
        self.pkg_tools = {t["name"]: t for t in package.get("tools") or () if t.get("enabled", True)}
        self.skills = [_get(s, "name") for s in (ctx.skills.index or ())]
        self.memory = bool((package.get("memory") or {}).get("enabled"))
        self.blocked: dict[str, str] = {}      # assistant id -> why an earlier call of that turn was stopped
        self.pushbacks = 0
        self.finished_implicit = False
        self.turn_results: list[dict] = []
        self.sub_agents: dict[str, Tool] = {}
        self.sub_specs: list[dict] = []
        self.native = {}
        for name in STIRRUP_TOOLS:
            module, attr = NATIVE_TOOLS[name]
            tool = getattr(importlib.import_module(module), attr)
            self.native[tool.name] = tool
        self.fetch = self.bundle.entry(f"{HTTP_FETCH}:run") if os.environ.get(EGRESS_FLAG) == "1" else None
        self.fetch_spec = self.bundle.modules[HTTP_FETCH].SPEC if self.fetch is not None else None

    def specs(self) -> list[dict]:
        specs = [dict(t) for t in self.ctx.view.get("base_tools") or ()]
        specs += [{"name": t["name"], "description": t["description"], "schema": t["input_schema"]}
                  for t in self.pkg_tools.values()]
        if self.skills:
            specs.append({"name": READ_SKILL, "description": "Read one of the skills listed in your instructions. "
                          "Returns its text.",
                          "schema": {"type": "object", "properties": {"name": {"type": "string", "enum": self.skills}},
                                     "required": ["name"], "additionalProperties": False}})
        if self.memory:
            specs.append({"name": RECALL, "description": "Search the notes kept from earlier tasks of this kind: what "
                          "to do when, and what to avoid. Returns the notes that match the query.",
                          "schema": {"type": "object", "properties": {
                              "query": {"type": "string", "description": "What you need advice about."},
                              "k": {"type": "integer", "description": "How many notes at most."}},
                              "required": ["query"], "additionalProperties": False}})
        specs += [{"name": t.name, "description": t.description, "schema": t.parameters.model_json_schema()}
                  for t in self.native.values()]
        if self.fetch_spec is not None:
            specs.append(dict(self.fetch_spec))
        return specs + [dict(s) for s in self.sub_specs]

    def stirrup_tools(self, names=None) -> list[Tool]:
        return [SpecTool(name=s["name"], description=s.get("description") or "", parameters=AnyArgs,
                         executor=self._executor(s["name"]), raw_schema=s.get("schema") or {"type": "object"})
                for s in self.specs() if names is None or s["name"] in names]

    def _executor(self, name: str):
        async def execute(params: AnyArgs) -> ToolResult:
            return await self.call(name, params.model_dump())
        return execute

    def take_turn_results(self) -> list:
        out, self.turn_results = self.turn_results, []
        return out

    def add_sub_agent(self, name: str, description: str, system_prompt: str = "", *, max_turns: int | None = None,
                      tools=None) -> None:
        """A sub-agent the model may call by `name` (Agent.to_tool()): Stirrup's own loop over this client and these
        tools, with a plain finish; its calls run the same hooks and are this attempt's calls."""
        taken = {s["name"] for s in self.specs()} | {FINISH}
        if name in taken or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name or ""):
            raise ValueError(f"sub-agent {name!r}: a tool name of its own (taken: {sorted(taken)})")
        allowed = None if tools is None else set(tools)
        own = [t for t in self.stirrup_tools(allowed) if t.name not in self.sub_agents]
        agent = Agent(client=self.client, name=name, system_prompt=system_prompt or None, tools=own,
                      finish_tool=self.finish_tool(sub=True), max_turns=max_turns or SUB_AGENT_MAX_TURNS,
                      run_sync_in_thread=False, logger=CtxLogger(self.ctx, self.client, sub=True))
        self.sub_agents[name] = agent.to_tool(description=description)
        self.sub_specs.append({"name": name, "description": description,
                               "schema": {"type": "object", "properties": {
                                   "task": {"type": "string",
                                            "description": SubAgentParams.model_fields["task"].description}},
                                   "required": ["task"]}})

    async def _sub_agent(self, name: str, args: dict, call_id, turn) -> tuple[str, bool]:
        try:
            params = SubAgentParams.model_validate({"task": args.get("task")})
        except ValidationError:
            return "Tool arguments are not valid: a sub-agent takes {\"task\": \"...\"}", True
        saved, results = self.client.turn_state(), self.turn_results
        started = self.client.turn
        self.ctx.mark("sub_agent", phase="start", name=name, call_id=call_id, turn=turn)
        try:
            result = await self.sub_agents[name].executor(params)
        finally:
            self.client.restore_turn(saved)
            self.turn_results = results
        output, is_error = _text(result.content)[:self._limit()], not result.success
        self.ctx.mark("sub_agent", phase="done", name=name, call_id=call_id, turn=turn,
                      turns=self.client.turn - started, is_error=is_error, chars=len(output))
        return output, is_error

    def _skip(self, name: str, call_id, turn) -> ToolResult:
        owner = self.client.current_id
        self.ctx.mark("tool_skipped", tool=name, call_id=call_id, turn=turn)
        return ToolResult(content=f"Not executed: an earlier call in this turn was stopped ({self.blocked[owner]})",
                          success=False)

    async def call(self, name: str, args: dict) -> ToolResult:
        turn, owner, ctx = self.client.turn, self.client.current_id, self.ctx
        call_id = self.client.take_call_id(name)
        if owner in self.blocked:
            return self._skip(name, call_id, turn)
        if self.hooks.has("before_tool"):
            reason, args = self.hooks.before_tool(turn, call_id, name, args)
            if reason is not None:
                self.blocked[owner] = reason[:2000]
                ctx.mark("tool_blocked", tool=name, call_id=call_id, turn=turn, reason=reason[:300])
                return ToolResult(content=f"Stopped before execution: {self.blocked[owner]}", success=False)
        if name in self.sub_agents:
            output, is_error = await self._sub_agent(name, args, call_id, turn)
        elif name in self.native:
            output, is_error = self._native(name, args, call_id, turn)
        elif self.fetch is not None and name == self.fetch_spec["name"]:
            output, is_error = self._fetch(args, call_id, turn)
        else:
            output, is_error = self._execute(name, args, turn)
        if self.hooks.has("after_tool"):
            output, feedback = self.hooks.after_tool(turn, call_id, name, args, output, is_error)
            self.client.add_note(owner, feedback)
        self.turn_results.append({"call_id": call_id, "name": name, "is_error": is_error,
                                  "output": output[:TURN_RESULT_CHARS]})
        return ToolResult(content=output, success=not is_error)

    def _limit(self) -> int:
        return int(self.ctx.view.get("max_output_chars") or MAX_TOOL_OUTPUT)

    def _execute(self, name: str, args: dict, turn: int) -> tuple[str, bool]:
        """A package tool through its entrypoint (its world calls go through ctx.tool, so the broker sees each one),
        read_skill from ctx.skills, recall from ctx.memory, anything else a base tool through the broker."""
        if name in self.pkg_tools:
            tool = self.pkg_tools[name]
            tctx = SubCtx(self.ctx, turn, read_only=not tool.get("writes", False))
            try:
                answer = _call(self.bundle.entry(tool["entrypoint"]), tctx, args)
            except PackageToolError:
                raise
            except Exception as e:  # noqa: BLE001 - the tool's own failure is the package's defect
                raise PackageToolError(f"tool {name} raised {type(e).__name__}: {str(e)[:300]}") from e
            output, is_error = _plain(answer, name)
            output = output[:self._limit()]
            self.ctx.mark("package_tool", tool=name, is_error=is_error, output=output[:2000], broker_calls=tctx.calls)
            return output, is_error
        if name == READ_SKILL and self.skills:
            skill = str(args.get("name", ""))
            text = self.ctx.skills.read(skill)
            if text is None:
                return f"no skill named {skill!r}; the skills are {self.skills}", True
            return text, False
        if name == RECALL and self.memory:
            k = args.get("k")
            entries = self.ctx.memory.recall(str(args.get("query") or ""), k if isinstance(k, int) else None)
            if not entries:
                return "No notes match the query.", False
            return "\n\n".join(f"[{_get(e, 'id')}] When: {_get(e, 'when')}\n{_get(e, 'content')}"
                               for e in entries), False
        res = self.ctx.tool(name, args)
        return str(_get(res, "output", "")), bool(_get(res, "is_error"))

    def _native(self, name: str, args: dict, call_id, turn) -> tuple[str, bool]:
        tool = self.native[name]
        try:
            params = tool.parameters.model_validate(args)
        except ValidationError:
            output, is_error = "Tool arguments are not valid", True
        else:
            result = _call(tool.executor, params)
            output, is_error = _text(result.content)[:self._limit()], not result.success
        self.ctx.mark("stirrup_tool", tool=name, call_id=call_id, turn=turn, args=args,
                      output=output[:STIRRUP_TOOL_CHARS], is_error=is_error)
        return output, is_error

    def _fetch(self, args: dict, call_id, turn) -> tuple[str, bool]:
        answer = _call(self.fetch, self.ctx, args)
        output, is_error = str(answer.get("output", ""))[:self._limit()], bool(answer.get("is_error"))
        self.ctx.mark("http_fetch", call_id=call_id, turn=turn, method=str(args.get("method") or "GET")[:10],
                      url=str(args.get("url") or "")[:500], is_error=is_error, chars=len(output))
        return output, is_error

    def finish_tool(self, *, sub: bool = False) -> Tool:
        """The attempt's finish (on_finish may push it back), or with `sub` a sub-agent's, which ends the sub-agent
        only. Neither is executed after a stopped call in its turn."""
        async def finish(params: FinishArgs) -> ToolResult:
            turn, owner = self.client.turn, self.client.current_id
            call_id = self.client.take_call_id(FINISH)
            implicit = owner in self.client.implicit_ids
            if owner in self.blocked:
                # fix 2: a write that before_tool stopped in this turn must not be followed by the end of the attempt
                return self._skip(FINISH, call_id, turn)
            if sub:
                return await SIMPLE_FINISH_TOOL.executor(params)
            if self.hooks.has("on_finish") and _turns_left(self.ctx) and self.pushbacks < MAX_PUSHBACKS:
                pushback = self.hooks.on_finish(turn, "implicit" if implicit else "finish", params.reason,
                                                self.pushbacks)
                if pushback is not None:
                    self.pushbacks += 1
                    self.ctx.mark("pushback", reason=pushback[:300], call_id=call_id, turn=turn, implicit=implicit)
                    return ToolResult(content=PUSHBACK.format(reason=pushback), success=False)
            result = await SIMPLE_FINISH_TOOL.executor(params)
            if result.success:
                self.finished_implicit = implicit
            return result
        return Tool(name=FINISH, description=SIMPLE_FINISH_TOOL.description, parameters=FinishArgs, executor=finish)


# ---------------------------------------------------------------------------- the entrypoint
async def _main(ctx, task: str) -> str:
    client = CtxClient(ctx, effort=((ctx.view.get("package") or {}).get("model") or {}).get("effort"))
    tools = Tools(ctx, client)
    client.hooks = tools.hooks
    if int(tools.config.get("subagent_depth", 1)) >= 1:
        for spec in SUB_AGENTS:
            spec = dict(spec)
            tools.add_sub_agent(spec.pop("name"), spec.pop("description"), spec.pop("system_prompt", ""), **spec)
    system = ctx.view.get("system") or ""
    opening = []
    if tools.hooks.has("before_run"):
        system, opening = tools.hooks.before_run(task, system)
    max_turns = int(tools.config.get("max_turns") or MAX_TURNS)
    left = (ctx.limits or {}).get("act_calls")
    host_capped = isinstance(left, int) and 0 < left <= max_turns
    if isinstance(left, int) and left > 0:
        max_turns = min(max_turns, left)
    agent = Agent(client=client, name="stirrup_agent", system_prompt=system, tools=tools.stirrup_tools(),
                  finish_tool=tools.finish_tool(), max_turns=max_turns,
                  context_summarization_cutoff=float(tools.config.get("summarize_at") or SUMMARIZE_AT),
                  run_sync_in_thread=False, logger=CtxLogger(ctx, client, tools))
    init = [UserMessage(content=str(m.get("content") or "")) for m in opening] + [UserMessage(content=task)] \
        if opening else task
    finished, text = None, ""
    try:
        async with agent.session(cache_on_interrupt=False) as session:
            finished, history, _ = await session.run(init)
        last = next((m for group in reversed(history) for m in reversed(group)
                     if isinstance(m, AssistantMessage) and joined_text(m.blocks)), None)
        text = finished.reason if finished is not None else (joined_text(last.blocks) if last else "")
    except Refused as r:
        text = r.text
    finally:
        client.summarizing = False               # fix 4: an exception out of a summary lowers the flag too
    if finished is None and host_capped and not _turns_left(ctx):
        # Stirrup stopped at the host's act-call limit without finishing: one more act call lets the host end the
        # attempt at its limit (refused before any model is called), so the record says budget, act_calls — not a
        # run that finished
        ctx.mark("stirrup_end", finished=False, implicit=False, turns=client.turn, summaries=client.summaries,
                 pushbacks=tools.pushbacks, maxrss_mb=_maxrss_mb())
        client.ctx.model([{"role": "user", "content": task}], [], role="act", effort=client.effort)
    # fix 7: the run's end as the agent saw it
    ctx.mark("stirrup_end", finished=finished is not None, implicit=finished is not None and tools.finished_implicit,
             turns=client.turn, summaries=client.summaries, pushbacks=tools.pushbacks, maxrss_mb=_maxrss_mb())
    return text or ""


def run(ctx, task: str) -> str:
    return asyncio.run(_main(LockedCtx(ctx), task))
