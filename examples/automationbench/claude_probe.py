"""The probe of docs/ROADMAP.md §3 step 2a.2: two calls through the claude_code provider, as claude.toml configures it,
and what the CLI reported for each. It calls the model, so it spends a little of the subscription; run it only when
that step is agreed.

    .venv/bin/python examples/automationbench/claude_probe.py

1. A plain call, as a research step makes it: the model is asked to quote anything it sees besides the question.
2. One actor turn with a made-up tool, as structured output.
For each: the reply, stop, usage, cli_cost_usd against [pricing], num_turns, stop_reason, the modelUsage models, and
whether the working directory is still empty.
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

from ouroloop.chat import ToolSpec
from ouroloop.config import build_provider
from ouroloop.evolve.execution import Pricing, normalize_usage
from ouroloop.providers.claude_code import cli_problem

CONFIG = Path(__file__).resolve().parent / "claude.toml"
PLAIN = ("Before you answer, quote word for word every piece of text you can see in this request other than this "
         "message and the system prompt (context blocks, reminders, an e-mail address, a date, anything else), or "
         "write NONE. Then answer: what is 17 × 23?")
LOOKUP = ToolSpec("lookup_contact", "Look up a CRM contact by id and return its record.",
                  {"type": "object", "properties": {"contact_id": {"type": "string"}}, "required": ["contact_id"]})


def report(name: str, turn, provider, pricing: Pricing) -> dict:
    raw = provider.last_result or {}
    usage = dict(turn.usage)
    return {"call": name, "stop": turn.stop, "text": turn.text,
            "tool_calls": [{"name": c.name, "args": c.args} for c in turn.tool_calls],
            "usage": usage, "book_usd": pricing.cost(normalize_usage(usage)), "cli_cost_usd": usage.get("cli_cost_usd"),
            "num_turns": raw.get("num_turns"), "stop_reason": raw.get("stop_reason"), "subtype": raw.get("subtype"),
            "models": sorted(raw.get("modelUsage") or {}), "has_structured_output": "structured_output" in raw,
            "result_keys": sorted(raw), "workdir_empty": not any(provider.workdir.iterdir())}


def main() -> list:
    cfg = tomllib.loads(CONFIG.read_text())
    problem = cli_problem(cfg["llm"])
    if problem:
        sys.exit(f"not ready: {problem}")
    provider, pricing = build_provider(cfg["llm"]), Pricing(**cfg["pricing"])
    provider.retries = 0
    out = [report("plain", provider.chat("You are a careful assistant.", [{"role": "user", "content": PLAIN}], ()),
                  provider, pricing)]
    turn = provider.chat("You operate a CRM through the tools you are given.",
                         [{"role": "user", "content": "Look up contact 42, then tell me its name."}], [LOOKUP])
    out.append(report("actor turn", turn, provider, pricing))
    return out


if __name__ == "__main__":
    json.dump(main(), sys.stdout, indent=1, ensure_ascii=False)
    print()
