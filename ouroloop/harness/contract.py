"""The package-side contract (docs/design/HARNESS.md §5): the names package code sees and the shapes it exchanges.

Package code (the agent, tools, hooks, tests) knows only these names, and they mean the same in the benchmark worker
and in production. runtime/worker_runner.py is a standard-library file that runs inside the worker image, so it
carries these constants as an embedded JSON literal (`CONTRACT` there); tests/harness/test_contract.py checks that it
equals `as_json()`. The seed's adapter (seeds/stirrup/agent/stirrup_agent.py) carries the site and return shapes it
dispatches by, and its tests check them against this file too.

ctx                 model(messages, tools, *, role, effort) -> {text, tool_calls: [{id, name, args}], stop}
                    tool(name, args, *, read_only) -> {output, is_error}
                    skills.index -> [{name, description}]; skills.read(name) -> str | None
                    memory.recall(query, k=None) -> [{id, when, content, score}]
                    limits -> the attempt's remaining hard limits (LIMIT_KEYS)
                    view -> a read-only projection (VIEW_KEYS)
                    mark(kind, **data) -> None: the package's own account of its loop, never evidence
hook                hook(ctx, event) -> dict | None, event and return per SITES below; a hook's `tool` is read-only
                    (a hook looks, it does not act; V2b replays it on recorded events). At on_finish and after_turn a
                    hook may call `ctx.model` with role "aux" up to view["hook_aux_calls"] times per attempt (an
                    audit before the agent finishes; DESIGN §6.4, v1.2); at the other sites, and past the cap, the
                    call is the package's defect
tool entry          run(ctx, args) -> {"output": str} | {"output": str, "is_error": true} (a plain str is an output)
agent entry         run(ctx, task) -> str: the final text; the host ends a run at a hard limit by itself
toolcheck script    check(ctx, tools) -> None: tools[name](args) runs a candidate tool; the world goes through a
                    writable broker. Only for the V2a check, never part of a package.
"""
from __future__ import annotations

API_VERSION = "harness_package.v3"
ROLES = ("act", "aux")                         # act: a step of the loop; aux: a call that produces no action
EFFORTS = ("low", "medium", "high", "xhigh", "max")
SITES = ("before_run", "before_model", "before_tool", "after_tool", "after_turn", "on_finish")

# What the adapter hands a hook at each site, and what the hook may answer (None: no action).
EVENTS = {
    "before_run": ("site", "task", "system", "messages"),
    "before_model": ("site", "turn", "role", "messages", "tools"),
    "before_tool": ("site", "turn", "call_id", "name", "args"),
    "after_tool": ("site", "turn", "call_id", "name", "args", "output", "is_error"),
    "after_turn": ("site", "turn", "tool_results"),
    "on_finish": ("site", "turn", "reason", "final_text", "pushbacks"),
}
RETURNS = {
    "before_run": ("system", "messages"),
    "before_model": ("messages",),
    "before_tool": ("block", "args"),
    "after_tool": ("output", "feedback"),
    "after_turn": ("message", "missing"),
    "on_finish": ("pushback", "missing"),
}
ACTIONS = ("none", "block", "modify", "feedback", "pushback")
# v1.6 (DESIGN §6.16): an audit answers {"missing": [...]} and the host says the rest — the agent is told only what has
# no matching write, never to change or remove what it did (an additive pushback). The adapter renders this text.
MISSING_TEXT = ("Keep working on the task: an audit found requirements with no matching write:\n{items}\n"
                "Add what is missing; do not change or remove what you have already done.")


def render_missing(items) -> str:
    return MISSING_TEXT.format(items="\n".join(f"- {str(i).strip()}" for i in items))


def _missing(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(i, str) and i.strip() for i in value)

CTX_NAMES = ("model", "tool", "skills", "memory", "limits", "view", "mark")
LIMIT_KEYS = ("act_calls", "model_calls", "tool_calls", "seconds", "tokens")   # tokens: output tokens
VIEW_KEYS = ("system", "base_tools", "package", "last_usage", "events", "max_output_chars", "hook_aux_calls")
AUX_SITES = ("after_turn", "on_finish")        # the sites whose hooks may call the aux model (view["hook_aux_calls"])
VIEW_EVENTS = 20                               # the broker's latest calls in view["events"]

# The worker protocol (docs/design/RUNTIME.md §4): host -> worker ops, and the worker's callbacks with their answers.
OPS = ("load", "run", "smoke", "hook", "call_tool", "probe", "test", "check", "work")
CALLBACKS = {"model_call": "model_result", "base_call": "base_result", "skill_read": "skill_result",
             "memory_recall": "memory_result", "mark": "mark_result"}
CALLBACK_IDS = {"base_call": "b", "model_call": "m", "skill_read": "k", "mark": "n", "memory_recall": "r"}
WORK_ACTIONS = ("init", "list", "read", "write", "edit", "delete", "export")


class HookAnswerError(ValueError):
    """A hook answered outside its site's protocol. The caller turns it into the candidate's defect."""


def action_of(site: str, value) -> str:
    """The action a hook's answer is, as the host records it: none, block, modify, feedback or pushback. A key outside
    the site's return shape, a value of the wrong type, or an answer that is not a dict or None raises
    HookAnswerError."""
    if site not in SITES:
        raise HookAnswerError(f"unknown hook site {site!r}: one of {list(SITES)}")
    if value is None:
        return "none"
    if not isinstance(value, dict):
        raise HookAnswerError(f"a {site} hook answers a dict or None, not {type(value).__name__}")
    extra = sorted(set(value) - set(RETURNS[site]))
    if extra:
        raise HookAnswerError(f"a {site} hook may answer {list(RETURNS[site])}, not {extra}")
    if not value:
        return "none"
    if site == "before_run":
        if "system" in value and not isinstance(value["system"], str):
            raise HookAnswerError("before_run: system is a string")
        if "messages" in value and not _messages(value["messages"]):
            raise HookAnswerError("before_run: messages is a list of {role, content}")
        return "modify"
    if site == "before_model":
        if not _messages(value["messages"]):
            raise HookAnswerError("before_model: messages is a list of message objects")
        return "modify"
    if site == "before_tool":
        if "block" in value and "args" in value:
            raise HookAnswerError("before_tool: block or args, not both")
        if "block" in value:
            block = value["block"]
            if not isinstance(block, dict) or set(block) != {"reason"} or not isinstance(block["reason"], str):
                raise HookAnswerError("before_tool: block is {reason: str}")
            return "block"
        if not isinstance(value["args"], dict):
            raise HookAnswerError("before_tool: args is an object")
        return "modify"
    if site == "after_tool":
        for key in ("output", "feedback"):
            if key in value and not isinstance(value[key], str):
                raise HookAnswerError(f"after_tool: {key} is a string")
        return "feedback" if "feedback" in value and "output" not in value else "modify"
    if site == "after_turn":
        if "missing" in value:
            if "message" in value:
                raise HookAnswerError("after_turn: message or missing, not both")
            if not _missing(value["missing"]):
                raise HookAnswerError("after_turn: missing is a non-empty list of non-empty strings")
            return "feedback"
        if not isinstance(value["message"], str):
            raise HookAnswerError("after_turn: message is a string")
        return "feedback"
    if "missing" in value:
        if "pushback" in value:
            raise HookAnswerError("on_finish: pushback or missing, not both")
        if not _missing(value["missing"]):
            raise HookAnswerError("on_finish: missing is a non-empty list of non-empty strings")
        return "pushback"
    if not isinstance(value["pushback"], str):
        raise HookAnswerError("on_finish: pushback is a string")
    return "pushback"


def _messages(value) -> bool:
    return isinstance(value, list) and all(isinstance(m, dict) and isinstance(m.get("role"), str) for m in value)


def as_json() -> dict:
    """The constants the worker runner embeds (RUNTIME §4)."""
    return {"api_version": API_VERSION, "roles": list(ROLES), "efforts": list(EFFORTS), "sites": list(SITES),
            "events": {k: list(v) for k, v in EVENTS.items()}, "returns": {k: list(v) for k, v in RETURNS.items()},
            "actions": list(ACTIONS), "ctx": list(CTX_NAMES), "limits": list(LIMIT_KEYS), "view": list(VIEW_KEYS),
            "ops": list(OPS), "callbacks": dict(CALLBACKS), "callback_ids": dict(CALLBACK_IDS),
            "work_actions": list(WORK_ACTIONS)}
