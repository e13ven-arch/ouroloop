"""An offline dry run of the rehearsal's expected path (docs/REHEARSAL.md §4, §8, §10), printed as JSON.

It runs `bench.cycle` on the rehearsal world with scripted researchers that answer the way §4 expects R to:
under R0's preference order, a working note or a skill first, and a check before finishing only once a note for
that failure was tried and rejected — which is what the scripted research slot proposes as R1. It checks the
world's arithmetic before anyone answers by hand; it is not the rehearsal, which goes through the CLI with no
injected researcher. It needs Docker and the worker image, and runs in its own process for the same reason
check_world.py does.

    PYTHONPATH=examples/rehearsal .venv/bin/python examples/rehearsal/dry_run.py [WORKDIR]
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

from ouroloop import bench, policies as P
from ouroloop.agent import harness_package as hp
from ouroloop.evolve import contracts as ct
from ouroloop.research.experiment import ProposalResult

HERE = Path(__file__).resolve().parent
KIND = {"marketing.reh_000": "A", "marketing.reh_001": "B", "marketing.reh_002": "C", "marketing.reh_004": "D",
        "marketing.reh_010": "E", "marketing.reh_011": "F"}
FAULT = "upgraded to the team plan"         # a plain task of the second development half (§10.3)
RULES = {
    "A": "When a request ends by asking you to post a message in a Slack channel, make that post before finishing.",
    "B": "When a request ends by asking you to tag the contact, add that tag before finishing.",
    "C": "When a request ends by asking you to log a follow-up task, create that task before finishing.",
    "D": "When a request ends by naming a new owner for the contact, assign that owner before finishing.",
    "E": "When a request ends by asking you to add the contact to a mailing list, add them to that list.",
}
NOTE_TOOL = (
    "import base64\nimport json\n\n\n"
    "def add_contact_note(ctx, args):\n"
    "    body = {'id': args['contact_id'], 'note_b64': base64.urlsafe_b64encode(args['text'].encode()).decode()}\n"
    "    result = ctx.call_tool('api_fetch', {'method': 'POST', 'body': json.dumps(body),\n"
    "                                         'url': 'https://api.hubapi.com/crm/v3/objects/contacts/notes'})\n"
    "    return result.output\n")
# The manifest entry of that tool, as the scripted R writes it; check_world.py installs the same entry and code,
# so the offline check exercises exactly what the dry run installs.
NOTE_TOOL_ENTRY = {
    "name": "add_contact_note", "description": "Add a note to a contact from its plain text: it is encoded and "
    "posted for you.", "entrypoint": "tools/notes.py:add_contact_note",
    "input_schema": {"type": "object", "properties": {"contact_id": {"type": "string"}, "text": {"type": "string"}},
                     "required": ["contact_id", "text"]}}
NOTE_TOOL_TEST = (
    "import importlib.util, os\n"
    "def test_the_tool_answers_with_text(ctx):\n"
    "    path = os.path.join(os.path.dirname(__file__), '..', 'tools', 'notes.py')\n"
    "    spec = importlib.util.spec_from_file_location('notes', path)\n"
    "    module = importlib.util.module_from_spec(spec)\n"
    "    spec.loader.exec_module(module)\n"
    "    assert isinstance(module.add_contact_note(ctx, {'contact_id': 'nobody', 'text': 'a note'}), str)\n")
R1_SENTENCE = (" When a working note or a skill for a failure was already tried and rejected, propose a check "
               "before finishing (an on_finish hook) for that failure instead.")
R1_PROPOSE = P.PROPOSE_PACKAGE + R1_SENTENCE
HOOK = (
    "def check(ctx, event):\n"
    "    final = str(event.get('final_message', ''))\n"
    "    if 'Not done:' in final:\n"
    "        return {'action': 'block', 'message': 'Before finishing, do this: ' + final.split('Not done:', 1)[1].strip()}\n"
    "    return {'action': 'continue'}\n")
HOOK_TEST = (
    "import importlib.util, os\n"
    "def _check():\n"
    "    path = os.path.join(os.path.dirname(__file__), '..', 'middlewares', 'closing_check.py')\n"
    "    spec = importlib.util.spec_from_file_location('closing_check', path)\n"
    "    module = importlib.util.module_from_spec(spec)\n"
    "    spec.loader.exec_module(module)\n"
    "    return module.check\n"
    "def test_a_skipped_closing_step_sends_the_actor_back(ctx):\n"
    "    assert _check()(ctx, {'final_message': 'Done. Not done: the closing step.', 'turn': 3})['action'] == 'block'\n"
    "def test_a_finished_task_is_let_through(ctx):\n"
    "    assert _check()(ctx, {'final_message': 'Done: all of it.', 'turn': 3})['action'] == 'continue'\n")


class Scripted:
    """The worker rounds' research step, scripted to §4 — under the v0.3 R path (docs/modules/04_RESEARCH_LOOP.md §1,
    R 作为有界编码会话): notes as before, and the proposal as a coding session played by tool calls. It reads the
    session's first message (the round's inputs: notes, coverage, ledger projection, experience) and, under R0's
    preference order, writes a working note or a skill for the first failing kind — a tool for F, a skill for E — or,
    when the ledger shows that kind's note or skill rejected or screened out and the loaded propose text carries
    R1's sentence, an on_finish check; then it declares the one candidate and finishes. A check output comes back
    into the same session and is answered by rewriting the hook without its bad import. It keeps the requests it
    was rendered from, as the real researcher does, so the campaign can check that the candidate arm ran R1's text."""
    origin = "scripted"

    def __init__(self, *, broken_hook_first: bool = False, faults: Path | None = None, arm_on: tuple = ()):
        self.requests = []
        self.broken_hook_first = broken_hook_first
        self.faults, self.arm_on, self.sessions = faults, arm_on, 0
        self.repairs = 0
        self._queue: list = []
        self._inputs: dict = {}

    def note_request(self, summary, prompt, *, allowed=()):
        return prompt, json.dumps({"allowed_components": list(allowed), "failure": summary}, sort_keys=True)

    def note(self, system, user):
        failure = json.loads(user)["failure"]
        self.requests.append({"step": "note", "system": system})
        kind = KIND.get(failure["task_id"], "?")
        return {"failure_class": f"closing step skipped ({kind})", "root_cause": failure["turns"][-1]["text"][:300],
                "mechanism": "do the closing step a request ends with before finishing", "component": "memory",
                "generality": "any request that ends with a closing step"}

    # ------------------------------------------------------------------ the session
    def session_turn(self, system, messages, tools):
        from ouroloop.chat import ChatTurn, ToolCall
        self.requests.append({"step": "session", "system": system})
        last = messages[-1]
        if len(messages) == 1:
            if self.sessions in self.arm_on:
                # the operator arms the fault before this answer (§10.3): the next attempt on that task fails once
                self.faults.mkdir(parents=True, exist_ok=True)
                (self.faults / "infra_once").write_text(FAULT)
            self.sessions += 1
            self._inputs = json.loads(messages[0]["content"])
            # as a real R would: read the current package's manifest and memory before changing them
            reads = [ToolCall("r1", "read_file", {"path": "package/manifest.json"})]
            if "memory.md" in (self._inputs.get("package_files") or ()):
                reads.append(ToolCall("r2", "read_file", {"path": "package/memory.md"}))
            self._queue = [reads, None]            # None: the plan is made once the reads have answered
        elif self._queue and self._queue[0] is None and last.get("role") == "tool":
            results = {r["id"]: r["output"] for r in last["results"]}
            self._inputs["manifest"] = json.loads(results["r1"])
            self._inputs["memory"] = results.get("r2", "")
            self._queue = self._plan(self._inputs, system)
        elif last.get("role") == "user" and "failed its draft check" in str(last.get("content", "")):
            self.repairs += 1
            self._queue = [[ToolCall("w1", "write_file", {"path": "candidates/c1/middlewares/closing_check.py",
                                                         "text": HOOK})],
                           [self._finish(self._inputs, "C", "hook")]]
        if not self._queue:
            return ChatTurn("nothing more to do", [], "end")
        return ChatTurn("", self._queue.pop(0), "tool_use")

    def _plan(self, inputs: dict, system: str) -> list:
        from ouroloop.chat import ToolCall
        kinds = [KIND[t] for t in inputs["coverage"].get("included") or () if t in KIND]
        if not kinds:
            return [[ToolCall("f1", "finish", {"next_action": "stop", "reason": "no failure left to diagnose"})]]
        kind = kinds[0]
        tried = any(f"[{kind}]" in str(row.get("declaration", {}).get("mechanism", ""))
                    and row.get("outcome") in ("rejected", "screened_out") for row in inputs.get("ledger") or ()) \
            or any(e.get("kind") == "experiment" and f"[{kind}]" in str(e.get("hypothesis")) and e.get("outcome") == "rejected"
                   for e in inputs.get("experience") or ())
        how = ("tool" if kind == "F" else "skill" if kind == "E" else
               "hook" if R1_SENTENCE in system and tried else "skill" if tried else "memory")
        files = self._files(kind, how)
        writes = [ToolCall(f"w{i}", "write_file", {"path": f"candidates/c1/{path}", "text": text})
                  for i, (path, text) in enumerate(sorted(files.items()))]
        return [writes, [self._finish(inputs, kind, how)]]

    def _finish(self, inputs: dict, kind: str, how: str):
        from ouroloop.chat import ToolCall
        notes = inputs.get("notes") or ()
        site = {"memory": "memory.md", "skill": f"skills/closing-{kind.lower()}.md", "tool": NOTE_TOOL_ENTRY["name"],
                "hook": "on_finish"}[how]
        carrier = {"memory": "memory", "skill": "skill", "tool": "tool", "hook": "hook"}[how]
        return ToolCall("f1", "finish", {"candidates": [{
            "dir": "c1", "mechanism": f"[{kind}] closing step by {how}", "site": site, "carrier": carrier,
            "predicted_affected": sorted({n["task_id"] for n in notes}), "trigger_condition": "always",
            "notes": [n["note_ref"] for n in notes]}]})

    def _files(self, kind: str, how: str) -> dict:
        """The candidate's files over the current package: the manifest and memory it read from package/ with the
        one component added, as R's session writes whole files."""
        manifest = dict(self._inputs.get("manifest") or hp.empty_manifest())
        write = {}
        if how == "memory":
            manifest["memory"] = {"enabled": True}
            write["memory.md"] = (self._inputs.get("memory", "").rstrip() + "\n" + RULES[kind]).strip() + "\n"
        elif how == "tool":
            manifest["tools"] = [*manifest.get("tools", []), dict(NOTE_TOOL_ENTRY)]
            write["tools/notes.py"] = NOTE_TOOL
            write["tests/test_notes.py"] = NOTE_TOOL_TEST
        elif how == "skill":
            name = f"closing-{kind.lower()}"
            manifest["skills"] = [*manifest.get("skills", []),
                                  {"name": name, "path": f"skills/{name}.md", "description": "closing steps"}]
            write[f"skills/{name}.md"] = RULES[kind] + "\n"
        else:
            manifest["hooks"] = [*manifest.get("hooks", []), {"name": "closing_check", "site": "on_finish",
                                                              "entrypoint": "middlewares/closing_check.py:check"}]
            broken = self.broken_hook_first
            self.broken_hook_first = False
            write["middlewares/closing_check.py"] = ("import requests\n" if broken else "") + HOOK
            write["tests/test_closing_check.py"] = HOOK_TEST
        write["manifest.json"] = json.dumps(manifest)
        return write


class SlotProposer:
    """The research slot, scripted to §4: after a rejected note, prefer a check before finishing."""

    def __init__(self):
        self.requests = []

    def propose_research(self, context, prompt, current, experience=(), *, allowed=()):
        self.requests.append({"step": "propose", "system": prompt})
        return ProposalResult(proposals=[ct.ExperimentProposal(
            owner="research", hypothesis="a note rejected once is not tried again as a note",
            changes=({"target": "research.propose", "kind": "prompt", "change": {"text": R1_PROPOSE}},),
            origin="scripted")])


def v3_preset_for_the_world() -> dict:
    """The v3 preset (M7 §2) sized to the rehearsal world (docs/REHEARSAL.md §8, v0.3 step 3): the world has 16 dev
    tasks and 6 closing kinds, so the gate's least task count is 8 (two halves of 8 for the slot), the headroom
    check asks for 2 never-passed inner tasks (W_EF leaves A–D), and the screen takes the preset's numbers. The
    real preset's 25/6 are Marketing's; the dry run checks the world's arithmetic under the v0.3 rules, not the
    numbers of a paid activity."""
    preset = json.loads(json.dumps(bench.PRESETS["automationbench_harness_v3"]))
    preset["task_plan"]["gate"]["min_tasks"] = 8
    preset["campaign"]["min_headroom_tasks"] = 2
    return preset


def main(workdir: Path) -> dict:
    cfg = tomllib.loads((HERE / "rehearsal.toml").read_text())
    cfg["workspace"]["root"] = str(workdir / "ws")
    cfg["bench"]["upstream_path"] = str(HERE / "upstream")
    cfg["research"]["llm"]["dir"] = str(workdir / "manual")
    cfg["rsi"]["scope_id"] = "rehearsal-dry-run"
    cfg["llm"]["fault_dir"] = str(workdir / "actor-faults")      # before freezing: bootstrap pins the actor's table
    # v0.3 step 3: the dry run walks the new R path — the coding session, k candidates, the screen, the ledger —
    # which only a package-API-v2 core runs; attempts stay serial so the world's arithmetic reads as in §10.
    bench.PRESETS["rehearsal_v3"] = v3_preset_for_the_world()
    cfg["improvement"]["preset"] = "rehearsal_v3"
    cfg["bench"]["concurrency"] = 1
    lines = []
    refs = bench.freeze(cfg, str(workdir), out=lines.append)
    cfg = {**cfg, "rsi": {**cfg["rsi"], **refs}}
    faults = Path(cfg["llm"]["fault_dir"])
    made = []

    def arm():
        # the candidate's first hook fails its check; its second answer (a stop) comes with the fault armed for
        # the outer comparison that follows
        made.append(Scripted(broken_hook_first=len(made) == 2, faults=faults, arm_on=(1,) if len(made) == 2 else ()))
        return made[-1]
    result = bench.cycle(cfg, str(workdir), out=lines.append, researcher=Scripted(faults=faults, arm_on=(0,)),
                         slot_researcher=SlotProposer(), arm_researcher=arm)
    activity = bench.open_activity(cfg, str(workdir), out=lambda _l: None)
    events = ct.Events(activity.root)
    receipt = result["steps"].get("slot-0", {}).get("receipt")
    research = activity.objects.load(receipt, ct.EvaluationReceipt) if receipt else None
    return {"status": result["status"],
            "steps": {k: {f: v for f, v in s.items() if f in ("stop_reason", "outcome", "reason", "verdict",
                                                                 "installed", "reuse")}
                      for k, s in result["steps"].items() if isinstance(s, dict)},
            "arms": [{"arm": e["arm"], "installed": len(e["installed"]), "stop": e["stop_reason"]}
                     for e in events.of("campaign_arm")],
            "research": {"verdict": research.verdict, "metrics": {k: research.metrics.get(k) for k in (
                "gain", "tasks", "incomplete", "identical_arms", "candidate_outer_pass_rate",
                "control_outer_pass_rate")}, "reasons": list(research.reasons)} if research else None,
            "revisions": sum(m.repairs for m in made),
            "sessions": len(list(events.of("research_session"))),
            "ledger": [{k: row.get(k) for k in ("candidate", "outcome", "carrier")}
                       for row in __import__("ouroloop.evolve.experience", fromlist=["x"]).ledger_rows(
                           __import__("ouroloop.evolve.experience", fromlist=["x"]).read(activity.root))],
            "screens": len(list(events.of("train_screen"))),
            "hook_events": sum(1 for p in Path(activity.root).rglob("transcripts/*.jsonl")
                               for line in p.read_text().splitlines() if '"kind": "hook"' in line),
            "tool_events": sum(1 for p in Path(activity.root).rglob("transcripts/*.jsonl")
                               for line in p.read_text().splitlines() if '"kind": "package_tool"' in line),
            "skill_reads": sum(1 for p in Path(activity.root).rglob("transcripts/*.jsonl")
                               for line in p.read_text().splitlines() if '"name": "read_skill"' in line
                               and '"kind": "assistant"' in line),
            "refills": [{"arm": r.attempt_id.split("/")[-1], "task": r.task_id, "valid": r.validity == "valid"}
                        for r in bench.Results(activity.root, activity.bench.domain).all() if r.supersedes],
            "tail": lines[-12:]}


if __name__ == "__main__":
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="rehearsal-dry-run-"))
    try:
        json.dump(main(work), sys.stdout, indent=1, default=str)
    finally:
        if len(sys.argv) <= 1:
            shutil.rmtree(work, ignore_errors=True)
