"""An offline dry run of the rehearsal's expected path (docs/REHEARSAL.md §4, §8), printed as JSON.

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
KIND = {"marketing.reh_000": "A", "marketing.reh_001": "B", "marketing.reh_002": "C", "marketing.reh_004": "D"}
RULES = {
    "A": "When a request ends by asking you to post a message in a Slack channel, make that post before finishing.",
    "B": "When a request ends by asking you to tag the contact, add that tag before finishing.",
    "C": "When a request ends by asking you to log a follow-up task, create that task before finishing.",
    "D": "When a request ends by naming a new owner for the contact, assign that owner before finishing.",
}
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
    """The worker rounds' research step, scripted to §4. It keeps the requests it was rendered from, as the real
    researcher does, so the campaign can check that the candidate arm ran R1's text."""

    def __init__(self, *, broken_hook_first: bool = False):
        self.requests = []
        self.broken_hook_first = broken_hook_first

    def note_request(self, summary, prompt, *, allowed=()):
        return prompt, json.dumps({"allowed_components": list(allowed), "failure": summary}, sort_keys=True)

    def note(self, system, user):
        failure = json.loads(user)["failure"]
        self.requests.append({"step": "note", "system": system})
        kind = KIND.get(failure["task_id"], "?")
        return {"failure_class": f"closing step skipped ({kind})", "root_cause": failure["turns"][-1]["text"][:300],
                "mechanism": "do the closing step a request ends with before finishing", "component": "memory",
                "generality": "any request that ends with a closing step"}

    def summarize(self, context, prompt, notes, coverage, experience=(), *, package=None, allowed=()):
        self.requests.append({"step": "propose", "system": prompt})
        kinds = [KIND[t] for t in coverage.get("included") or () if t in KIND]
        if not kinds:
            return ProposalResult(next_action="stop", reason="no failure left to diagnose")
        kind = kinds[0]
        tried = [e for e in experience if e.get("kind") == "experiment" and f"[{kind}]" in str(e.get("hypothesis"))
                 and e.get("verdict") == "rejected"]
        how = ("hook" if R1_SENTENCE in prompt and tried else "skill" if tried or kind == "B" else "memory")
        patch = self._patch(dict(package or {}), kind, how)
        return ProposalResult(proposals=[ct.ExperimentProposal(
            owner="worker", hypothesis=f"[{kind}] a {how} for the closing step the failure skipped",
            changes=({"target": "worker.harness", "kind": "workflow", "change": {"patch": patch}},),
            expected="the tasks with this closing step pass", abandon_if="the gate shows no gain",
            origin="scripted")])

    def _patch(self, files: dict, kind: str, how: str) -> dict:
        manifest = json.loads(files.get("manifest.json") or json.dumps(hp.empty_manifest()))
        write = {}
        if how == "memory":
            manifest["memory"] = {"enabled": True}
            write["memory.md"] = (files.get("memory.md", "").rstrip() + "\n" + RULES[kind]).strip() + "\n"
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
        return {"write": write}

    def revise(self, prompt, patch, check_output):
        self.requests.append({"step": "revise", "system": prompt})
        write = dict(patch["write"])
        write["middlewares/closing_check.py"] = write["middlewares/closing_check.py"].replace("import requests\n", "")
        return {"write": write}


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


def main(workdir: Path) -> dict:
    cfg = tomllib.loads((HERE / "rehearsal.toml").read_text())
    cfg["workspace"]["root"] = str(workdir / "ws")
    cfg["bench"]["upstream_path"] = str(HERE / "upstream")
    cfg["research"]["llm"]["dir"] = str(workdir / "manual")
    cfg["rsi"]["scope_id"] = "rehearsal-dry-run"
    lines = []
    refs = bench.freeze(cfg, str(workdir), out=lines.append)
    cfg = {**cfg, "rsi": {**cfg["rsi"], **refs}}
    made = []

    def arm():
        made.append(Scripted(broken_hook_first=len(made) == 2))      # the candidate's first hook fails its check
        return made[-1]
    result = bench.cycle(cfg, str(workdir), out=lines.append, researcher=Scripted(), slot_researcher=SlotProposer(),
                         arm_researcher=arm)
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
            "revisions": sum(1 for m in made for r in m.requests if r["step"] == "revise"),
            "hook_events": sum(1 for p in Path(activity.root).rglob("transcripts/*.jsonl")
                               for line in p.read_text().splitlines() if '"kind": "hook"' in line),
            "tail": lines[-12:]}


if __name__ == "__main__":
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="rehearsal-dry-run-"))
    try:
        json.dump(main(work), sys.stdout, indent=1, default=str)
    finally:
        if len(sys.argv) <= 1:
            shutil.rmtree(work, ignore_errors=True)
