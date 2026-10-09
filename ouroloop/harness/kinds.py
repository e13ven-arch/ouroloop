"""The component kinds of a harness package (docs/design/HARNESS.md §3, docs/DESIGN.md §3.1).

Each kind has a place in the package (a path pattern, or a manifest entry), says whether it is code, whether only the
host writes it, which rungs of the acceptance ladder it climbs before money is spent (IMPROVER §8), and on which
attempts it is credited (IMPROVER §9): `all` attempts, the ones that `invoked` a tool, `triggered` a hook, `cited` a
memory entry or `read` a skill; `none` for a package's own tests, which never run in an attempt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

KINDS = ("agent", "vendor", "tool", "hook", "prompt", "skill", "memory", "model", "config", "test")
CODE_KINDS = ("agent", "vendor", "tool", "hook", "test")
TEXT_KINDS = ("prompt", "skill")
HOST_KINDS = ("memory",)                      # host-written; R cannot write it; the worker never loads it
CREDITS = ("all", "invoked", "triggered", "cited", "read", "none")

# path -> kind; a path that matches none of these is not a package file
PATTERNS = (
    ("manifest", re.compile(r"^manifest\.json$")),
    ("agent", re.compile(r"^agent/[a-z0-9_]+\.py$")),
    ("vendor", re.compile(r"^vendor/[a-z0-9_]+/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+$")),
    ("tool", re.compile(r"^tools/[a-z0-9_]+\.py$")),
    ("hook", re.compile(r"^middlewares/[a-z0-9_]+\.py$")),
    ("prompt", re.compile(r"^prompt/(system|finish)\.md$")),
    ("skill", re.compile(r"^skills/[a-z0-9][a-z0-9_/-]*\.md$")),
    ("memory", re.compile(r"^memory/snapshot\.jsonl$")),
    ("test", re.compile(r"^tests/[a-z0-9_]+\.py$")),
)
MEMORY_PATH = "memory/snapshot.jsonl"


@dataclass(frozen=True)
class Kind:
    name: str
    code: bool
    host_only: bool
    manifest_key: str                          # where the manifest lists it
    ladder: tuple[str, ...]                    # $0 rungs before V3
    credit: str


KIND_TABLE: dict[str, Kind] = {k.name: k for k in (
    Kind("agent", True, False, "agent", ("V0", "V1"), "all"),
    Kind("vendor", True, False, "vendor", ("V0", "V1"), "all"),
    Kind("tool", True, False, "tools", ("V0", "V1", "V2a"), "invoked"),
    Kind("hook", True, False, "hooks", ("V0", "V1", "V2b"), "triggered"),
    Kind("prompt", False, False, "prompt", ("V0",), "all"),
    Kind("skill", False, False, "skills", ("V0", "V2c"), "read"),
    Kind("memory", False, True, "memory", ("V0", "V2c"), "cited"),
    Kind("model", False, False, "model", ("V0",), "all"),
    Kind("config", False, False, "config", ("V0", "V1"), "all"),
    Kind("test", True, False, "tests", ("V0",), "none"),
)}
assert tuple(KIND_TABLE) == KINDS
