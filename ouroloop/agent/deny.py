"""The deterministic deny list that runs before every tool call: commands that are never run, whatever any model says.
Rules first, model second. It lives with the agent host (gateway.AttemptBroker checks it on every call), apart from the
decision points, so an attempt's broker needs nothing of the decision stack (docs/PLAN.md P1 step 2)."""
from __future__ import annotations

import re

DENY = [re.compile(p) for p in (
    r"\brm\s+-\w*r\w*\s+(/|~|\$HOME)(/?\*)?(\s|;|&|$)",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\b[^\n]*\bof=/dev/",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"\bgit\s+push\b[^\n]*\s(--force|-f)(\s|$)",
    r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(ba|z)?sh\b",
    r"\bchmod\s+-R\s+777\s+/",
)]


def denied(command: str) -> str | None:
    """The deny-list pattern this command matches, or None."""
    for rx in DENY:
        if rx.search(command):
            return rx.pattern
    return None
