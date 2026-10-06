"""Offline checks of the rehearsal's world and fake actor (docs/REHEARSAL.md §8), printed as JSON.

Every attempt runs through the runner shell's host objects (docs/modules/01_POLICY_RUNTIME.md §1 宿主作为运行壳, v0.3):
the fixture actor behind a Gateway, the episode behind an AttemptBroker, the package's text behind a SkillReader, the
package's marks behind Marks, and the default agent — the seed package's own bytes — loaded from a bundle laid out
as the worker lays it out (the package's hooks and tools beside `agent/default_agent.py`), over a HostContext. So the
check exercises the path the production cycle runs the fixture actor through: the actor is reached as `[llm] type`
reaches it, every model turn is a host-written `gateway_call`, every base call a `broker_event`, and a hook's answer,
a pushback and a package tool's run are the package's marks. The hook and the tool are the dry run's own
(dry_run.HOOK, dry_run.NOTE_TOOL): what the scripted R installs there is what the actor answers to here.

Run in its own process: the rehearsal upstream is a top-level `automationbench` package, as the test fixture's
is, and one process can import only one of them. `tests/test_rehearsal_world.py` runs it and reads the JSON.

    PYTHONPATH=examples/rehearsal .venv/bin/python examples/rehearsal/check_world.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

from ouroloop import policies as P
from ouroloop.adapters import automationbench as ab
from ouroloop.agent import harness_package as hp
from ouroloop.agent.default_agent import DEFAULT_PATH, seed_files
from ouroloop.agent.gateway import (DEFAULT_MAX_EFFORT, AttemptBroker, Gateway, HostContext, HostFault, Marks,
                                    SkillReader)
from ouroloop.agent.runner import attempt_view
from ouroloop.agent.worker import LimitHit
from ouroloop.benchmark import Budget, assign_splits
from ouroloop.evolve.task_eval import Budgeted

from dry_run import HOOK, NOTE_TOOL, NOTE_TOOL_ENTRY
from rehearsal_actor import RehearsalActor

UPSTREAM = Path(__file__).resolve().parent / "upstream"
# The five hard limits of rehearsal.toml's [bench.budget] (M0 §2): what the shell counts an attempt against here.
BUDGET = Budget(max_steps=8, max_tool_calls=12, max_seconds=60, max_model_calls=20, max_tokens=4000)
RULES = {
    "A": "When a request ends by asking you to post a message in a Slack channel, make that post before finishing.",
    "B": "When a request ends by asking you to tag the contact, add that tag before finishing.",
    "C": "When a request ends by asking you to log a follow-up task, create that task before finishing.",
    "D": "When a request ends by naming a new owner for the contact, assign that owner before finishing.",
    "E": "When a request ends by asking you to add the contact to a mailing list, add them to that list.",
    "F": "When a request ends by asking for a call note, encode the note with base64_encode and post it.",
}
HOOK_ENTRY = {"name": "closing_check", "site": "on_finish", "entrypoint": "middlewares/closing_check.py:check"}
_BUNDLES: dict = {}        # files -> the TemporaryDirectory holding that bundle, kept for the process's lifetime


class Twice:
    """The actor called twice per turn with the same input, as the old hand-rolled loop called it: a fake actor whose
    answer depended on anything but its input would make the rehearsal's numbers irreproducible, and the check
    would not know. The gateway sees a provider with the three-argument chat; it hands it no keyword."""
    name = "rehearsal-actor"

    def __init__(self, actor):
        self.actor, self.model = actor, actor.model

    def chat(self, system, messages, tools):
        turn = self.actor.chat(system, messages, tools)
        again = self.actor.chat(system, list(messages), tools)
        assert (turn.text, [(c.name, c.args) for c in turn.tool_calls]) == \
            (again.text, [(c.name, c.args) for c in again.tool_calls]), "the actor is not deterministic"
        return turn


def package(*, memory: str = "", skills: dict | None = None, hook: bool = False, tool: bool = False) -> hp.PackageView:
    """The capability package of one variant, as R's would be stored and the host reads it: a v1 manifest with the
    memory, the skills, the dry run's on_finish hook and the dry run's note tool, parsed by the host's own reader so
    a malformed entry fails here as it would fail a draft check."""
    manifest, files = hp.empty_manifest(), {}
    if memory:
        manifest["memory"] = {"enabled": True}
        files["memory.md"] = memory
    for name, text in (skills or {}).items():
        manifest["skills"].append({"name": name, "path": f"skills/{name}.md", "description": "when the request asks for it"})
        files[f"skills/{name}.md"] = text
    if hook:
        manifest["hooks"].append(dict(HOOK_ENTRY))
        files["middlewares/closing_check.py"] = HOOK
    if tool:
        manifest["tools"].append(dict(NOTE_TOOL_ENTRY))
        files["tools/notes.py"] = NOTE_TOOL
    files["manifest.json"] = json.dumps(manifest)
    return hp.PackageView("package:rehearsal", hp.parse_manifest(files["manifest.json"], files), files)


def default_agent(run_files: dict):
    """`run` of the default agent, loaded from a bundle holding the package's files and the seed agent's bytes — the
    layout the worker loads (runner.PackageAgent._run_in_worker), so a hook or tool entrypoint resolves beside the
    agent exactly as it does there. One bundle per distinct package; the module is loaded by path, not imported."""
    key = tuple(sorted(run_files.items()))
    if key not in _BUNDLES:
        directory = tempfile.TemporaryDirectory(prefix="rehearsal-bundle-")
        root = Path(directory.name)
        for path, text in {**run_files, DEFAULT_PATH: seed_files()[DEFAULT_PATH].decode("utf-8")}.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(f"rehearsal_default_agent_{len(_BUNDLES)}", root / DEFAULT_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _BUNDLES[key] = (directory, module.run)
    return _BUNDLES[key][1]


def attempt(suite, task_id: str, *, memory: str = "", skills: dict | None = None, hook: bool = False,
            tool: bool = False, actor=None) -> dict:
    """One attempt under the shell: the system prompt composed as policies.BoundRuntime.system_prompt composes it
    (the task's own, W0's finish check, the package's memory and skill index), the five names over the host's
    gateway, broker, skills and marks, and the default agent run over them. The attempt ends as the shell ends it:
    `run` returns the final message, or a hard limit ends it with an empty one (LimitHit -> `limit`); the actor's own
    failure — the armed fault (§10.2) — leaves as itself, as the attempt runner would record it as infrastructure.
    `calls` are the tool calls the actor asked for, in order, read from the host's `assistant` records; `pushes` the
    package's `pushback` marks; `kinds` every record kind in order, for the shape checks."""
    episode, task = suite.episode(task_id), suite.tasks[task_id]
    pkg = package(memory=memory, skills=skills, hook=hook, tool=tool)
    system = "\n\n".join(p for p in (task.system, P.FINISH_CHECK, pkg.prompt_block()) if p)
    surface = Budgeted(episode, BUDGET)
    records: list[dict] = []

    def record(kind: str, data: dict) -> None:
        records.append({"kind": kind, **data})
    gateway = Gateway(Twice(actor or RehearsalActor()), roles=pkg.roles, max_effort=DEFAULT_MAX_EFFORT, budget=BUDGET,
                      record=record, surface=surface)
    view = attempt_view(task.trigger, system, surface, None, pkg)
    broker = AttemptBroker(surface, record=record, gateway=gateway, view=view)
    ctx = HostContext(gateway, broker, SkillReader(pkg, record=record), Marks(record), view)
    text, limit = "", None
    try:
        text = default_agent(pkg.run_files)(ctx, task.trigger)
    except HostFault as fault:
        if isinstance(fault.error, LimitHit):
            limit = fault.error.limit
        else:
            raise fault.error from None
    score = episode.score()
    return {"success": bool(score.success), "text": text, "limit": limit,
            "calls": [c["name"] for r in records if r["kind"] == "assistant" for c in r["tool_calls"]],
            "pushes": sum(1 for r in records if r["kind"] == "pushback"),
            "kinds": [r["kind"] for r in records]}


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
    runs["hook"] = {t: attempt(suite, t, hook=True) for t in ids}
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
