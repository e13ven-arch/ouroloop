"""The default agent: today's host loop (agent/loop.py, its business path) as an agent program, the seed package of
v0.3 (docs/DESIGN.md §5 "默认 agent", docs/modules/01_POLICY_RUNTIME.md §1 "默认 agent 即种子包").

Standard library only, no ouroloop import, on purpose: the same bytes run in two places and must behave the same
(PR7-07). The host ships this file as `agent/default_agent.py` of the loaded bundle when a v1 package with code runs
in the isolated worker, and imports it in-process for a package without code; R copies it into a candidate's
`agent/` as the ancestor every evolved loop can be diffed against. It is written against the five names of the
package API (docs/modules/00_CONTRACTS.md §2): ctx.model, ctx.tool, ctx.skills, ctx.limits, ctx.view — nothing else,
so it holds no runtime, no objects, no scorer, whichever side runs it.

What it keeps from the host loop, step for step: the task as the first message under the system prompt the host
composed (ctx.view["system"], W's prompt policies and the package's memory and skill index); one `act` model call a
turn with the base tools, the package's enabled tools and `read_skill` in that order; a turn without tool calls is a
finish request, which the package's on_finish hooks may push back at most MAX_PUSHBACKS times and never on the last
turn; before_tool blocks the call and the rest of the turn ("Stopped before execution", "Not executed"), after_tool
feedback is one user message after the results; package tools run here, in the worker, through their entrypoints;
read_skill answers from ctx.skills; HOOK_RESULT_CHARS and the other truncations are the loop's. What it drops: the
decision points. compact is a plain rule over read/bash outputs, route has one model, retry has no model to say
"yes", and stop has two outcomes only — the agent returns, or the host ends it at a hard limit (M1 §1 宿主不问决策),
so the loop runs until the model finishes and lets the gateway refuse the act call past `max_steps`. The StopGuard's
rule path survives as the pushback cap and the last-turn rule; the completion decision model (C3) has no hook point
here.

Its own events — a pushback, a hook's answer, a package tool's run, a call a before_tool hook blocked or the rest of
that turn it skipped (the broker saw no call, so the mark is what says the model was told "Stopped before
execution") — go to the host through ctx.mark, which records them labelled as the package's report (M0 §2: the
records are host-written); the model and tool calls themselves are recorded by the gateway and the broker, not here. A hook or tool that raises, or answers outside its protocol, raises
out of `run`: at run time that is infrastructure (M1 §1 失败语义), and the draft smoke is where a deterministic
failure is meant to be found first (M0 §3).
"""
import asyncio
import importlib.util
import inspect
import os

# An after_tool hook is handed at most this much of a result; the model itself is handed all of it (M1 §1).
HOOK_RESULT_CHARS = 4000
MAX_PUSHBACKS = 2                     # how often on_finish hooks may send the model back to work (StopGuard's cap)
# A package tool's output has the same bound as a base tool's: K's WorkerLimits.max_output_chars, which the host
# puts in ctx.view["max_output_chars"]; this is the fallback for a view without it.
MAX_TOOL_OUTPUT = 20000
CONTEXT_CHARS = 60000                 # above this, older read/bash outputs are dropped from the history
KEEP_RECENT = 4                       # the latest read/bash outputs are never dropped
COMPACTABLE = ("read", "bash")
DROPPED = "[output removed to save context; run the command or read the file again if you need it]"
PUSHBACK = "{reason}\nKeep working on the task, or explain why it is already done."
READ_SKILL = "read_skill"
HOOK_ACTIONS = {"before_tool": ("continue", "block", "feedback"), "after_tool": ("continue", "feedback"),
                "on_finish": ("continue", "block", "feedback")}
DEFAULT_PATH = "agent/default_agent.py"
DEFAULT_ENTRY = DEFAULT_PATH + ":run"


def seed_files() -> dict:
    """{DEFAULT_PATH: this file's bytes}: what the host adds to a v1 package's bundle so the worker can run it, and
    what R copies into a candidate that changes the loop. The bytes, not a rewritten copy: the file in the worker
    is the file in the host."""
    with open(__file__, "rb") as f:
        return {DEFAULT_PATH: f.read()}


# ---------------------------------------------------------------------------- the loaded bundle
class _Bundle:
    """The package's code beside this file: `agent/default_agent.py` sits in the bundle root, so a middleware or
    tool entrypoint `middlewares/x.py:run` is a path relative to the parent of this file's directory. Modules are
    loaded by path (no sys.path, no sys.modules entry), once each."""

    def __init__(self):
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.modules = {}

    def entry(self, spec: str):
        module_path, _, func = spec.partition(":")
        if module_path not in self.modules:
            full = os.path.join(self.root, module_path)
            name = "agentpkg_" + module_path.replace("/", "_").replace(".py", "")
            loader = importlib.util.spec_from_file_location(name, full)
            if loader is None or loader.loader is None:
                raise ImportError(f"cannot load {module_path}")
            module = importlib.util.module_from_spec(loader)
            loader.loader.exec_module(module)
            self.modules[module_path] = module
        fn = getattr(self.modules[module_path], func, None)
        if not callable(fn):
            raise AttributeError(f"{spec}: no callable {func}")
        return fn


async def _await(value):
    return await value


def _call(fn, *args):
    """Package entrypoints may be plain or async functions (the v1 API, M1 §1)."""
    value = fn(*args)
    if inspect.isawaitable(value):
        value = asyncio.run(value if asyncio.iscoroutine(value) else _await(value))
    return value


class _Refused:
    """A call the context refused without sending it (a method past its call cap): a tool result in shape."""

    def __init__(self, output: str):
        self.output, self.is_error = output, True

    def __getitem__(self, key):
        return {"output": self.output, "is_error": self.is_error}[key]


class _PackageContext:
    """What a v1 hook or package tool holds (M1 §1 首版能力包 API): `view`, the attempt's projection with this
    turn added, and `call_tool`, the broker through ctx.tool — read-only for a hook, since a hook may only look.
    The calls it made are kept for the host's record of the hook or tool.

    v0.5 (docs/design/V0_5_PLAN.md §1.1): a method — a package tool named with the view's method prefix, present only
    under a K with methods — holds a read-only context too, with `max_calls` base calls a run; a call past the cap is
    answered as an error and never sent. Under such a K a context also has `mark`, which reports a method decision
    (the router) to the host's record, labelled the package's own; without one it has no `mark`, as before."""

    def __init__(self, ctx, turn: int, read_only: bool, max_calls=None):
        self.view = {**ctx.view, "turn": turn}
        self._ctx, self._read_only, self._max_calls = ctx, read_only, max_calls
        self.calls = []
        if isinstance(ctx.view.get("methods"), dict):
            # only under a K with methods: a v0.4 package's hooks and tools hold exactly view and call_tool, as before
            self.mark = self._mark

    def call_tool(self, name: str, args=None):
        if self._max_calls is not None and len(self.calls) >= self._max_calls:
            self.calls.append({"tool": name, "refused": "call cap"})
            return _Refused(f"refused: a method makes at most {self._max_calls} base calls per run")
        res = self._ctx.tool(name, dict(args or {}), read_only=self._read_only)
        self.calls.append({"tool": name, "is_error": bool(res.is_error)})
        return res

    def _mark(self, kind: str, **data):
        self._ctx.mark(kind, **data)


def _plain(value, what: str):
    """A package tool's answer as (output, is_error): a tool result object, a string, or {output, is_error}. Anything
    else is the package's error, as the host read it before (PackageRuntime.call_tool)."""
    if isinstance(value, str):
        return value, False
    if isinstance(value, dict) and isinstance(value.get("output"), str):
        return value["output"], bool(value.get("is_error"))
    output = getattr(value, "output", None)
    if isinstance(output, str):
        return output, bool(getattr(value, "is_error", False))
    raise TypeError(f"tool {what} answered {type(value).__name__}, not a tool result")


# ---------------------------------------------------------------------------- the loop
def _turns_left(ctx) -> bool:
    """Whether another act call is allowed after this one: the host loop's `turn < max_turns`, read from the
    remaining act calls the host reports (None means no such limit)."""
    left = (ctx.limits or {}).get("act_calls")
    return left is None or left > 0


def _run_hooks(ctx, bundle, hooks, site: str, event: dict, turn: int):
    """-> (action, messages). The enabled hooks at `site` in manifest order; the first block or feedback of a
    before_tool/on_finish hook ends the chain; after_tool collects feedback. An answer outside the site's protocol
    is never read as continue (M1 §1): it raises, and the run ends."""
    messages = []
    for hook in hooks:
        if hook.get("site") != site:
            continue
        hctx = _PackageContext(ctx, turn, read_only=True)
        value = _call(bundle.entry(hook["entrypoint"]), hctx, event)
        if not isinstance(value, dict) or value.get("action") not in HOOK_ACTIONS[site] or \
                sorted(set(value) - {"action", "message"}):
            raise ValueError(f"hook {hook['name']} at {site} answered {str(value)[:120]}: an action of "
                             f"{list(HOOK_ACTIONS[site])} with an optional message")
        action, message = value["action"], str(value.get("message") or "")[:2000]
        ctx.mark("hook", hook=hook["name"], site=site, action=action, message=message[:300],
                 tool=event.get("tool"), broker_calls=hctx.calls)
        if action == "continue":
            continue
        messages.append(message or f"{hook['name']} asks you to check before going on")
        if site != "after_tool":
            return action, messages
    return ("feedback" if messages else "continue"), messages


def _compact(messages, items) -> None:
    """The compaction rule: when the live read/bash outputs exceed CONTEXT_CHARS, the older ones beyond KEEP_RECENT
    are replaced by DROPPED in the history. A rule, not a decision: the compact point asked a model which output
    was still needed, and the host no longer asks (M1 §1)."""
    live = [it for it in items if not it["dropped"]]
    if sum(len(it["output"]) for it in live) <= CONTEXT_CHARS:
        return
    for it in live[:-KEEP_RECENT] if KEEP_RECENT else live:
        mi, ri = it["ref"]
        messages[mi]["results"][ri]["output"] = DROPPED
        it["dropped"] = True


def run(ctx, task: str) -> str:
    """One attempt. Returns the final message; returning is finishing (M1 §3)."""
    view = ctx.view
    package = view.get("package") or {}
    bundle = _Bundle()
    hooks = [h for h in package.get("hooks") or () if h.get("enabled", True)]
    tools = {t["name"]: t for t in package.get("tools") or () if t.get("enabled", True)}
    skills = [s.get("name") for s in (ctx.skills.index or ())]
    specs = [dict(t) for t in view.get("base_tools") or ()]
    specs += [{"name": t["name"], "description": t["description"], "schema": t["input_schema"]} for t in tools.values()]
    if skills:
        specs.append({"name": READ_SKILL, "description": "Read one of the skills listed in your instructions. Returns "
                                                         "its text.",
                      "schema": {"type": "object", "properties": {"name": {"type": "string", "enum": skills}},
                                 "required": ["name"], "additionalProperties": False}})
    messages = [{"role": "system", "content": view.get("system") or ""}, {"role": "user", "content": task}]
    items = []                        # read/bash outputs the compaction rule may drop
    pushbacks, text, turn = 0, "", 0
    while True:
        turn += 1
        _compact(messages, items)
        reply = ctx.model(messages, specs, role="act")
        calls = [{"id": c["id"], "name": c["name"], "args": dict(c.get("args") or {})} for c in reply.tool_calls]
        messages.append({"role": "assistant", "text": reply.text, "tool_calls": calls})
        text = reply.text
        if reply.stop != "tool_use" or not calls:
            if hooks and reply.stop in ("end", "other") and _turns_left(ctx) and pushbacks < MAX_PUSHBACKS:
                # on_finish of the package: a block or feedback is a bounded pushback, never on the last turn.
                action, notes = _run_hooks(ctx, bundle, hooks, "on_finish",
                                           {"final_message": text[:4000], "turn": turn}, turn)
                if action != "continue":
                    pushbacks += 1
                    reason = " ".join(notes)
                    messages.append({"role": "user", "content": PUSHBACK.format(reason=reason)})
                    ctx.mark("pushback", reason=reason[:300])
                    continue
            return text
        results, blocked, feedback = [], None, []
        for call in calls:
            if blocked is not None:
                # A before_tool hook stopped an earlier call of this turn: the rest are not executed, and what
                # already ran stays as it is — nothing is rolled back. The broker sees no call, so the mark is the
                # only trace of what the model was told.
                results.append({"id": call["id"], "name": call["name"], "is_error": True,
                                "output": f"Not executed: an earlier call in this turn was stopped ({blocked})"})
                ctx.mark("tool_skipped", tool=call["name"], call_id=call["id"], turn=turn)
                continue
            if hooks:
                action, notes = _run_hooks(ctx, bundle, hooks, "before_tool",
                                           {"tool": call["name"], "args": call["args"], "turn": turn}, turn)
                if action != "continue":
                    blocked = " ".join(notes)[:2000]
                    results.append({"id": call["id"], "name": call["name"], "is_error": True,
                                    "output": f"Stopped before execution: {blocked}"})
                    ctx.mark("tool_blocked", tool=call["name"], call_id=call["id"], turn=turn, reason=blocked[:300])
                    continue
            output, is_error = _execute(ctx, bundle, tools, skills, call, turn)
            results.append({"id": call["id"], "name": call["name"], "output": output, "is_error": is_error})
            if hooks:
                _, notes = _run_hooks(ctx, bundle, hooks, "after_tool",
                                      {"tool": call["name"], "args": call["args"],
                                       "result": output[:HOOK_RESULT_CHARS], "is_error": is_error, "turn": turn},
                                      turn)
                feedback += notes
            if call["name"] in COMPACTABLE:
                items.append({"ref": (len(messages), len(results) - 1), "output": output, "dropped": False})
        messages.append({"role": "tool", "results": results})
        if feedback:
            # after_tool feedback is its own visible message; the tool results above are unchanged.
            messages.append({"role": "user", "content": "\n".join(feedback)[:4000]})


def _execute(ctx, bundle, tools, skills, call, turn):
    """One tool call of the model: a package tool runs here through its entrypoint (its base calls go through
    ctx.tool, so the broker sees each one), read_skill answers from ctx.skills, anything else is a base tool
    through the broker."""
    name, args = call["name"], call["args"]
    if name in tools:
        methods = ctx.view.get("methods") if isinstance(ctx.view.get("methods"), dict) else None
        if methods and name.startswith(str(methods.get("prefix") or "m_")):
            # v0.5: a method reads only, and makes at most the K's base calls a run
            tctx = _PackageContext(ctx, turn, read_only=True, max_calls=int(methods.get("max_calls") or 25))
        else:
            tctx = _PackageContext(ctx, turn, read_only=False)
        output, is_error = _plain(_call(bundle.entry(tools[name]["entrypoint"]), tctx, args), name)
        output = str(output)[:int(ctx.view.get("max_output_chars") or MAX_TOOL_OUTPUT)]
        ctx.mark("package_tool", tool=name, is_error=is_error, output=output[:2000], broker_calls=tctx.calls)
        return output, is_error
    if name == READ_SKILL and skills:
        skill = str(args.get("name", ""))
        skill_text = ctx.skills.read(skill)
        if skill_text is None:
            return f"no skill named {skill!r}; the skills are {skills}", True
        return skill_text, False
    res = ctx.tool(name, args)
    return str(res.output), bool(res.is_error)
