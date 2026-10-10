"""R's own policy (docs/design/IMPROVER.md §10; DESIGN §6.9): the estimates by (line, cause, kind) and the research
playbook, both learned from the ledger, carried from one activity to the next.

The playbook is rewritten at a round's end by one aux call; a lesson stays only if it cites ledger rows that exist
(`[L12, L40]`). Within an activity the release's improver_ref stays R0: the policy evolves in the ledger, and
`export_policy` freezes it as an ImproverPolicy (the prompts, the estimates, the playbook, the settings) for the next
activity's R1, which `import_policy` installs into a new workspace's ledger.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..contracts import Record, register, write_json
from .diagnose import PROMPTS

LESSON = re.compile(r"^\s*-\s+(?P<text>.+?)\s*\[(?P<refs>[^\]]+)\]\s*$")
MAX_LESSONS = 12
ROWS_SHOWN = 80


@register
@dataclass(frozen=True)
class ImproverPolicy(Record):
    TYPE = "improver_policy"
    prompts_ref: str = ""
    estimates_ref: str = ""
    playbook_ref: str = ""
    settings_ref: str = ""


def playbook_path(root) -> Path:
    return Path(root) / "ledger" / "playbook.md"


def read_playbook(root) -> str:
    path = playbook_path(root)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def check_lessons(text: str, known_ids: set) -> tuple[list[str], list[str]]:
    """(kept lessons, dropped lines): a lesson is kept when every id it cites is a ledger row's and it cites one."""
    kept, dropped = [], []
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        m = LESSON.match(line)
        refs = [r.strip() for r in m.group("refs").split(",")] if m else []
        if m and refs and all(r in known_ids for r in refs):
            kept.append(f"- {m.group('text').strip()} [{', '.join(refs)}]")
        else:
            dropped.append(line.strip()[:300])
    return kept[:MAX_LESSONS], dropped


def update_playbook(root, ledger, *, researcher, line: str, hide=frozenset()) -> dict:
    """The playbook from the ledger's projection; `hide` the held-out train tasks (R reads the playbook)."""
    rows = ledger.projection(line=line, hide=hide)[-ROWS_SHOWN:]
    system = (PROMPTS / "playbook.md").read_text(encoding="utf-8")
    user = json.dumps({"playbook": read_playbook(root), "ledger_rows": rows}, ensure_ascii=False, default=str)
    turn = researcher.chat(system, [{"role": "user", "content": user}], [])
    kept, dropped = check_lessons(getattr(turn, "text", "") or "", ledger.ids())
    old = read_playbook(root)
    if not kept and (old.strip() or getattr(turn, "stop", "") in ("refusal", "max_tokens")):
        # a reply with no lesson the ledger backs (refused, cut, or all uncited) does not wipe what was learned
        return {"lessons": len([x for x in old.splitlines() if x.strip()]), "dropped": dropped, "kept_previous": True}
    text = "\n".join(kept) + ("\n" if kept else "")
    path = playbook_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"lessons": len(kept), "dropped": dropped}


def export_policy(activity) -> str:
    """The improver's learned policy as one object: the prompts, the estimates, the playbook, K's improver settings."""
    root, objects = activity.root, activity.objects
    prompts = {p.name: p.read_text(encoding="utf-8") for p in sorted(PROMPTS.glob("*.md"))}
    estimates = Path(root) / "ledger" / "estimates.json"
    policy = ImproverPolicy(
        prompts_ref=objects.put_json("prompts", prompts),
        estimates_ref=objects.put_json("estimates",
                                       json.loads(estimates.read_text(encoding="utf-8")) if estimates.exists() else {}),
        playbook_ref=objects.put_json("playbook", {"text": read_playbook(root)}),
        settings_ref=objects.put_json("improver_settings", dict(activity.k.get("improver") or {})))
    return objects.put(policy)


def import_policy(activity, policy_ref: str, source_objects=None) -> None:
    """A policy exported by another activity into this one's ledger: its estimates and playbook (the prompts and the
    settings are the code's and K's). Refused once this activity has learned anything of its own."""
    objects = source_objects or activity.objects
    policy = ImproverPolicy.from_json(objects.get(policy_ref))
    ledger_dir = Path(activity.root) / "ledger"
    if (ledger_dir / "experience.jsonl").exists():
        raise ValueError("this activity's ledger has rows already: a policy is imported before the first round")
    write_json(ledger_dir / "estimates.json", objects.get(policy.estimates_ref))
    ledger_dir.mkdir(parents=True, exist_ok=True)
    playbook_path(activity.root).write_text(objects.get(policy.playbook_ref).get("text") or "", encoding="utf-8")
