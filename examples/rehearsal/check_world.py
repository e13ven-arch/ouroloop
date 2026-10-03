"""Offline checks of the rehearsal's world and fake actor (docs/REHEARSAL.md §8), printed as JSON.

Run in its own process: the rehearsal upstream is a top-level `automationbench` package, as the test fixture's
is, and one process can import only one of them. `tests/test_rehearsal_world.py` runs it and reads the JSON.

    PYTHONPATH=examples/rehearsal .venv/bin/python examples/rehearsal/check_world.py
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path

from ouroloop import policies as P
from ouroloop.adapters import automationbench as ab
from ouroloop.benchmark import assign_splits
from ouroloop.chat import ToolSpec

from rehearsal_actor import KEEP_WORKING, SKILLS_HEADER, RehearsalActor

UPSTREAM = Path(__file__).resolve().parent / "upstream"
MEMORY = "Working notes for this kind of task:\n"
RULES = {
    "A": "When a request ends by asking you to post a message in a Slack channel, make that post before finishing.",
    "B": "When a request ends by asking you to tag the contact, add that tag before finishing.",
    "C": "When a request ends by asking you to log a follow-up task, create that task before finishing.",
    "D": "When a request ends by naming a new owner for the contact, assign that owner before finishing.",
    "E": "When a request ends by asking you to add the contact to a mailing list, add them to that list.",
    "F": "When a request ends by asking for a call note, encode the note with base64_encode and post it.",
}
NOTE_TOOL = ToolSpec("add_contact_note", "Add a note to a contact from its plain text.",
                     {"type": "object", "properties": {"contact_id": {"type": "string"}, "text": {"type": "string"}},
                      "required": ["contact_id", "text"]})


def note_tool(episode, args: dict) -> tuple[str, bool]:
    """What a package tool R would write does: encode the text and post it through the base tool."""
    body = {"id": args["contact_id"], "note_b64": base64.urlsafe_b64encode(args["text"].encode()).decode()}
    r = episode.run("api_fetch", {"method": "POST", "url": "https://api.hubapi.com/crm/v3/objects/contacts/notes",
                                  "body": json.dumps(body)})
    return r.output, r.is_error


def attempt(suite, task_id: str, *, memory: str = "", skills: dict | None = None, hook=None, tool: bool = False,
            actor=None) -> dict:
    """One attempt driven the way the agent loop drives it: tools run on the episode, read_skill answers from
    `skills`, and an on_finish `hook(final_message) -> message | None` sends the actor back, at most twice."""
    episode, task = suite.episode(task_id), suite.tasks[task_id]
    parts = [task.system, P.FINISH_CHECK]
    if memory:
        parts.append(MEMORY + memory)
    if skills:
        parts.append(SKILLS_HEADER + "\n" + "\n".join(f"- {name}: when the request asks for it" for name in skills))
    system = "\n\n".join(parts)
    specs = list(episode.specs()) + ([ToolSpec("read_skill", "Read one skill.", {"type": "object"})] if skills
                                     else []) + ([NOTE_TOOL] if tool else [])
    messages, pushes, calls, text = [{"role": "user", "content": task.trigger}], 0, [], ""
    actor = actor or RehearsalActor()
    for _turn in range(8):
        turn = actor.chat(system, messages, specs)
        again = actor.chat(system, list(messages), specs)
        assert (turn.text, [(c.name, c.args) for c in turn.tool_calls]) == \
            (again.text, [(c.name, c.args) for c in again.tool_calls]), "the actor is not deterministic"
        messages.append({"role": "assistant", "text": turn.text, "tool_calls": turn.tool_calls})
        if turn.stop == "tool_use":
            results = []
            for c in turn.tool_calls:
                calls.append(c.name)
                if c.name == "read_skill":
                    results.append({"id": c.id, "name": c.name, "output": (skills or {}).get(c.args["name"], ""),
                                    "is_error": False})
                elif c.name == NOTE_TOOL.name:
                    output, error = note_tool(episode, c.args)
                    results.append({"id": c.id, "name": c.name, "output": output, "is_error": error})
                else:
                    r = episode.run(c.name, c.args)
                    results.append({"id": c.id, "name": c.name, "output": r.output, "is_error": r.is_error})
            messages.append({"role": "tool", "results": results})
            continue
        text = turn.text
        message = hook(text) if hook and pushes < 2 else None
        if message:
            pushes += 1
            messages.append({"role": "user", "content": f"{message}\n{KEEP_WORKING}"})
            continue
        break
    score = episode.score()
    return {"success": bool(score.success), "text": text, "calls": calls, "pushes": pushes}


def quote_what_is_not_done(final: str):
    """The kind of on_finish hook R would write: send the actor back with what its own message says is not done."""
    return f"Before finishing, do this: {final.split('Not done:', 1)[1].strip()}" if "Not done:" in final else None


def main() -> dict:
    suite = ab.load_suite("marketing", UPSTREAM)
    ids = suite.ids()
    splits = assign_splits(ids, {"train": 0.5, "dev": 0.25, "test": 0.25}, 0)
    by_split = {s: [t.rsplit("_", 1)[1] for t in ids if splits[t] == s] for s in ("train", "dev", "test")}
    kinds = {t: suite.tasks[t].trigger for t in ids}

    def kind(t):
        text = kinds[t]
        return ("A" if "Then post" in text else "B" if "Then tag" in text else "C" if "follow-up task" in text
                else "D" if "owner." in text else "E" if "mailing list" in text else "F" if "call note" in text
                else "P")
    out = {"splits": by_split, "kinds": {t.rsplit("_", 1)[1]: kind(t) for t in ids},
           "contract": ab.contract_check(suite, [next(t for t in ids if splits[t] == "dev")]).get("failed")}
    runs = {"w0": {t: attempt(suite, t) for t in ids}}
    for k, rule in RULES.items():
        runs[f"memory_{k}"] = {t: attempt(suite, t, memory=rule) for t in ids}
    runs["skill_B"] = {t: attempt(suite, t, skills={"closing-tags": RULES["B"]}) for t in ids}
    runs["skill_E"] = {t: attempt(suite, t, skills={"closing-lists": RULES["E"]}) for t in ids}
    runs["tool_F"] = {t: attempt(suite, t, tool=True) for t in ids}
    runs["hook"] = {t: attempt(suite, t, hook=quote_what_is_not_done) for t in ids}
    # the armed fault (§10.2): the next attempt whose request holds the text fails once, then the file is gone
    with tempfile.TemporaryDirectory() as faults:
        (Path(faults) / "infra_once").write_text("upgraded to the team plan")
        actor = RehearsalActor(fault_dir=faults)
        target = next(t for t in ids if "upgraded to the team plan" in kinds[t])
        try:
            attempt(suite, target, actor=actor)
            out["fault"] = "did not fail"
        except Exception as e:  # noqa: BLE001 - the fault is an exception, which the runner records
            out["fault"] = type(e).__name__
        out["after_fault"] = attempt(suite, target, actor=actor)["success"]
        out["fault_used_up"] = not (Path(faults) / "infra_once").exists()
    out["runs"] = {name: {t.rsplit("_", 1)[1]: r for t, r in rs.items()} for name, rs in runs.items()}
    return out


if __name__ == "__main__":
    json.dump(main(), sys.stdout, indent=1)
