# The package's contract

A package is a directory with `manifest.json`:

    {"api_version": "harness_package.v3",
     "agent": {"entrypoint": "agent/<file>.py:run", "roles": ["act", "aux"]},
     "vendor": [{"name": "stirrup", "provenance": "vendor/stirrup/PROVENANCE.json"}],
     "tools": [{"name", "description", "input_schema", "entrypoint": "tools/<file>.py:run", "writes": bool}],
     "hooks": [{"name", "site", "entrypoint": "middlewares/<file>.py:<function>"}],
     "prompt": {"system": "prompt/system.md", "finish": "prompt/finish.md"},
     "skills": [{"name", "path": "skills/<name>.md", "description"}],
     "memory": {...}  (the host's; do not change it),
     "model": {...}   (do not change it),
     "config": {"max_turns", "summarize_at", "shown_k", "subagent_depth"} (inside K's ranges),
     "tests": ["tests/<file>.py"]}

Every file is UTF-8 text. Package code reaches the world only through ctx.

## ctx

    ctx.model(messages, tools, *, role="act", effort=None) -> {text, tool_calls: [{id, name, args}], stop}
    ctx.tool(name, args, *, read_only=False) -> {output, is_error}      # the episode's base tools
    ctx.skills.index -> [{name, description}];  ctx.skills.read(name) -> str | None
    ctx.memory.recall(query, k=None) -> [{id, when, content, score}]
    ctx.limits -> the attempt's remaining act_calls, model_calls, tool_calls, seconds, tokens
    ctx.view -> {system, base_tools, package, last_usage, events, max_output_chars}
    ctx.mark(kind, **data) -> None                                      # the package's own account of its loop

## A tool

    def run(ctx, args: dict) -> {"output": str} | {"output": str, "is_error": True}

It is offered to the agent with its manifest name, description and input_schema. It may call ctx.tool for the base
tools (read_only unless the manifest says writes: true). A tool raising an exception is the package's defect.

## A hook

    def <function>(ctx, event: dict) -> dict | None

A hook's ctx has no model and its tool is read-only: a hook looks and answers, it does not act. Per site:

{sites}

None (or {}) is no action.

A before_model hook that changes the messages must keep every earlier message of the call where it was and as it was:
add a note at the end of the first call that needs it and keep it at that place in later calls. The provider caches
the beginning of each call; a call whose earlier messages differ from the previous call's is paid in full, several
times the cost of an attempt.

## A toolcheck script (V2a, never part of the package)

    def check(ctx, tools):
        out = tools["<your tool>"]({...})     # runs the candidate's tool against the task's fresh world
        ...                                   # do the rest of the task's work with tools[...] if needed

The host then scores the world with the task's own checks.
