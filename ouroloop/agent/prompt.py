"""The harness prompt: the agent's system prompt and tool descriptions, the part of the harness that prompt
candidates change (PLAN §4.8). The champion lives in <workspace>/harness/prompt.json, with its history."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..ledger import now
from .loop import SYSTEM
from .tools import SPECS


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class HarnessPrompt:
    system: str = SYSTEM                             # "{workdir}" is replaced with the working directory
    tools: dict = field(default_factory=dict)        # tool name -> description; other tools keep the built-in one

    @classmethod
    def from_json(cls, data: dict) -> HarnessPrompt:
        return cls(data.get("system") or SYSTEM, dict(data.get("tools") or {}))

    def to_json(self) -> dict:
        return {"system": self.system, "tools": dict(sorted(self.tools.items()))}

    def descriptions(self) -> dict[str, str]:
        """The description of every tool, overrides applied."""
        return {s.name: self.tools.get(s.name, s.description) for s in SPECS}

    def apply(self, change: dict) -> HarnessPrompt:
        unknown = set(change) - {"system", "tools"}
        if unknown:
            raise ValueError(f"a prompt change sets system and tools, not {sorted(unknown)}")
        tools = {**self.tools, **(change.get("tools") or {})}
        bad = set(tools) - {s.name for s in SPECS}
        if bad:
            raise ValueError(f"unknown tools {sorted(bad)}")
        system = change.get("system", self.system)
        if not isinstance(system, str) or not system.strip() or not all(isinstance(v, str) and v.strip()
                                                                         for v in tools.values()):
            raise ValueError("the system prompt and tool descriptions must be non-empty text")
        return HarnessPrompt(system, tools)

    def same_as(self, other: HarnessPrompt) -> bool:
        """Equal once whitespace is ignored. Such a change can only add noise, so it is never run."""
        def norm(p: HarnessPrompt) -> tuple:
            return _squash(p.system), tuple((k, _squash(v)) for k, v in sorted(p.descriptions().items()))
        return norm(self) == norm(other)

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(self.to_json(), sort_keys=True).encode()).hexdigest()[:12]

    def render(self, workdir: str | Path) -> str:
        return self.system.replace("{workdir}", str(workdir))


class PromptStore:
    """The champion harness prompt, with its history."""

    def __init__(self, root: str | Path):
        self.path = Path(root) / "harness" / "prompt.json"
        self.data = (json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists()
                     else {"prompt": None, "history": []})

    def get(self) -> HarnessPrompt:
        return HarnessPrompt.from_json(self.data["prompt"]) if self.data.get("prompt") else HarnessPrompt()

    def set(self, prompt: HarnessPrompt, note: str = "") -> None:
        self.data["history"].append({"ts": now(), "hash": prompt.hash(), "prompt": prompt.to_json(), "note": note})
        self.data["prompt"] = prompt.to_json()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
