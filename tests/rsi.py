"""The shared fixture of docs/modules/08_INTEGRATION_CASES.md §1: the `rsi` domain of the miniature upstream, a
scripted worker that responds to what it is actually given, and a workspace with W0/R0/K installed.

The scripted worker never looks at a candidate's name. It reads the request, the system prompt and the tool
results, so a test that shows a new policy fixing a task shows the policy changing behaviour — not a fixture
deciding to be nice to a version it recognises. Every report built from these tasks is marked
`synthetic_contract_test`; the numbers are the fixture's, not a measurement.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ouroloop.adapters import automationbench as ab
from ouroloop.backends import KeywordBackend
from ouroloop.benchmark import Budget, RunManifest, SplitManifest, digest
from ouroloop.chat import ChatTurn, ToolCall
from ouroloop.evolve import contracts as ct
from ouroloop.points import builtin_points
from ouroloop.runtime import Runtime

DOMAIN = "rsi"
SLACK = "https://slack.com/api/chat.postMessage"
HUBSPOT = "https://api.hubapi.com/crm/v3/objects/contacts"
CONTACTS = {"Dana Reyes": "c1", "Sam Okafor": "c2", "Lea Fontaine": "c3", "Kai Mercer": "c4"}
# Variants of one workflow are one family, declared rather than guessed (docs/DESIGN.md §3.1.2).
FAMILIES = {"rsi.update_stage": "rsi.update",
            "rsi.notify_after_update": "rsi.notify_after_update",
            "rsi.notify_after_update_variant": "rsi.notify_after_update",
            "rsi.policy_hold": "rsi.hold",
            "rsi.notify_once": "rsi.notify_once",
            "rsi.dev_notify_after_update": "rsi.dev_notify_after_update",
            "rsi.dev_update_stage": "rsi.dev_update",
            "rsi.dev_policy_hold": "rsi.dev_hold",
            "rsi.final_notify_after_update": "rsi.final_notify_after_update",
            "rsi.final_update_stage": "rsi.final_update"}
SPLITS = {"rsi.update": "train", "rsi.notify_after_update": "train", "rsi.hold": "train",
          "rsi.notify_once": "train", "rsi.dev_notify_after_update": "dev", "rsi.dev_update": "dev",
          "rsi.dev_hold": "dev", "rsi.final_notify_after_update": "test", "rsi.final_update": "test"}
TRAIN = [t for t, f in FAMILIES.items() if SPLITS[f] == "train"]
DEV = [t for t, f in FAMILIES.items() if SPLITS[f] == "dev"]
FINAL = [t for t, f in FAMILIES.items() if SPLITS[f] == "test"]


def suite(path) -> ab.Suite:
    return ab.load_suite(DOMAIN, path)


def splits(s: ab.Suite) -> SplitManifest:
    fams, rule = ab.families(s, FAMILIES)
    return SplitManifest(DOMAIN, s.versions, rule, s.entries(fams), dict(SPLITS))


def runtime(root) -> Runtime:
    rt = Runtime(root, {"mock": KeywordBackend()}, "mock", seed=0)
    for p in builtin_points():
        rt.register(p)
    return rt


def workspace(root, split_ref: str = "", worker_ref: str | None = None, research_ref: str | None = None,
              **core_kw):
    """A workspace with W0, R0, K and a bootstrap release installed. -> (Releases, release ref, Objects)."""
    from ouroloop import policies as P
    objects = ct.Objects(root)
    releases, rel = P.bootstrap_workspace(root, objects=objects, worker_ref=worker_ref,
                                          research_ref=research_ref, domain=DOMAIN, split_ref=split_ref,
                                          adapter="ouroloop.adapters.automationbench", scorer="rubric.v1",
                                          toolset=ab.TOOLSET, **core_kw)
    return releases, rel, objects


def run_manifest(candidate: str, role: str, split: str, *, budget: Budget | None = None,
                 data: str = "", **v2) -> RunManifest:
    return RunManifest(candidate, role, split, {"provider": "scripted", "model": "scripted-1"},
                       prompt_hash=digest({"system": candidate})[:16],
                       environment={"upstream": "fake", "toolset": ab.TOOLSET},
                       budget=budget or Budget(max_steps=8, max_tool_calls=12), data=data, **v2)


W0_FINISH = "Report what you did when you are finished."


def w0(objects) -> str:
    """The starting worker version: no finish check, so the scripted agent stops after the update."""
    from ouroloop import policies as P
    return P.default_worker_set(objects, finish_check=W0_FINISH)


def w1(objects) -> str:
    """The same version with a finish-check instruction bound to worker.finish."""
    from ouroloop import policies as P
    return P.replace_policies(objects, w0(objects), [
        P.descriptor(objects, "worker.finish", "workflow",
                     {"builtin": "finish_check.v1",
                      "instruction_ref": objects.put_json("change", {"text": P.FINISH_CHECK})})])


# ---------------------------------------------------------------------------- the scripted worker
PUSHBACK = "Keep working on the task"
FINISH_CHECK_MARK = "name the tool result that shows it is done"


def _needs(text: str) -> dict:
    """What the request asks for, read from the request itself."""
    name = next((n for n in CONTACTS if n in text), None)
    phrase = re.search(r"phrase ([a-z ]+?)(?:,|\.|$)", text)
    return {"update": bool(name) and "opportunity stage" in text and "change nothing" not in text,
            "contact": CONTACTS.get(name or "", ""),
            "notify": "#ops" in text and phrase is not None,
            "phrase": (phrase.group(1).strip() if phrase else ""),
            "hold": "change nothing" in text}


def _done(messages: list[dict]) -> dict:
    """What has already been done, read from the tool calls in the conversation."""
    updated, sent = False, 0
    for m in messages:
        for call in m.get("tool_calls") or ():
            if call.name != "api_fetch":
                continue
            body = json.loads(call.args.get("body") or "{}")
            updated = updated or (call.args.get("url") == HUBSPOT and body.get("stage") == "opportunity")
            sent += int(call.args.get("url") == SLACK)
    return {"updated": updated, "sent": sent}


class BusinessWorker:
    """A scripted agent over the rsi tasks.

    It updates the record, and it posts the notification only when something in front of it says to check the
    request again: a finish-check instruction in the system prompt, or a pushback from the guard. Without either
    it finishes after the update, which is the behaviour gap the loop starts from (IT-01).
    """
    name = "scripted"
    model = "scripted-1"

    def __init__(self, *, repeat_notify: bool = False, ignore_finish_check: bool = False,
                 ignore_pushback: bool = False):
        self.repeat_notify = repeat_notify              # sends twice: the regression on rsi.notify_once
        self.ignore_finish_check = ignore_finish_check
        self.ignore_pushback = ignore_pushback
        self.calls = 0

    def chat(self, system: str, messages: list[dict], specs) -> ChatTurn:
        self.calls += 1
        task = next((m["content"] for m in messages if m.get("role") == "user"), "")
        want, done = _needs(task), _done(messages)
        pushed = any(PUSHBACK in str(m.get("content", "")) for m in messages if m.get("role") == "user")
        reminded = (FINISH_CHECK_MARK in (system or "") and not self.ignore_finish_check) or \
                   (pushed and not self.ignore_pushback)
        usage = {"input_tokens": 100, "output_tokens": 20}
        if want["hold"]:
            return ChatTurn("The request is not allowed by the data policy, so I changed nothing.", [], "end",
                            usage=usage)
        if want["update"] and not done["updated"]:
            return ChatTurn("", [ToolCall("t1", "api_fetch", {
                "method": "PATCH", "url": HUBSPOT,
                "body": json.dumps({"id": want["contact"], "stage": "opportunity"})})], "tool_use", usage=usage)
        if want["notify"] and (reminded or not want["update"]) and done["sent"] == 0:
            return ChatTurn("", [ToolCall("t2", "api_fetch", {
                "method": "POST", "url": SLACK,
                "body": json.dumps({"channel": "C1", "text": f"{want['phrase']} for the contact"})})],
                "tool_use", usage=usage)
        if want["notify"] and self.repeat_notify and done["sent"] == 1:
            return ChatTurn("", [ToolCall("t3", "api_fetch", {
                "method": "POST", "url": SLACK,
                "body": json.dumps({"channel": "C1", "text": f"{want['phrase']} again"})})], "tool_use",
                usage=usage)
        return ChatTurn("Done: the record is updated." + (" Message posted." if done["sent"] else ""), [],
                        "end", usage=usage)


# ---------------------------------------------------------------------------- a fixture decision model
class PhraseDecider:
    """A decision backend that reads the view: the task is complete when the phrase the request asked for appears
    in a tool result, and the record has been updated if the request asked for that.

    A fixture, not a training result. Its quality is defined here, so a test that shows the model changing what
    the agent does shows the wiring; whether a real small model can learn this is what a real experiment answers.
    """
    name = "phrase"

    def __init__(self, model: str = "trained-1", confidence: float = 0.9):
        self.model, self.confidence = model, confidence
        self.views: list[str] = []

    def decide(self, requests):
        from ouroloop.types import Answer
        out = []
        for r in requests:
            self.views.append(r.state)
            head, _, tail = r.state.partition("tool calls and what they returned")
            phrase = re.search(r"phrase ([a-z ]+?)(?:,|\.|$)", head)
            needs_update = "opportunity stage" in head and "change nothing" not in head
            done = (phrase is None or phrase.group(1).strip() in tail) and (
                not needs_update or "PATCH" in tail)
            p = self.confidence if done else 1 - self.confidence
            out.append(Answer({"yes": p, "no": 1 - p}, self.model))
        return out


class PhraseTrainer:
    """Trains nothing and says so: it reads the dataset the framework built (so a missing file is still an error)
    and returns a PhraseDecider as the artifact."""
    supports_weights = True
    name = "phrase"

    def __init__(self, model: str = "trained-1", fail: bool = False, confidence: float = 0.9):
        self.model, self.fail, self.confidence = model, fail, confidence
        self.jobs: list = []
        self.attached: list = []

    def train(self, job):
        from ouroloop.evolve.train import TrainError, TrainResult
        if self.fail:
            raise TrainError("CUDA out of memory")
        rows = sum(1 for _ in open(Path(job.data_dir) / "train.jsonl", encoding="utf-8"))
        self.jobs.append({"version": job.version, "rows": rows, "recipe": dict(job.recipe)})
        return TrainResult(self.model, {"type": "rsi:PhraseDecider", "model": self.model,
                                       "confidence": self.confidence},
                           {"id": f"job-{job.version}", "host": "local", "result": {"rows": rows}})


class UnknownJobTrainer(PhraseTrainer):
    """A trainer whose remote job state cannot be read: `attach` returns nothing, and nothing is resubmitted."""

    def attach(self, job_id):
        self.attached.append(job_id)
        return None


def collect(root, suite, splits, *, worker_ref=None, provider=None, tasks=(), probe=True, serving=None,
            releases=None, objects=None, rt=None, split="train", repeats=1, candidate="W0", role="base",
            arm="subject", plan_ref="", stop_view=None):
    """Run some tasks with a bound release and return everything a later step needs.

    With `probe`, the completion probe labels each stop decision from the state it was made on, which is what the
    train split is for; the runner refuses it anywhere else.
    """
    from ouroloop import policies as P
    from ouroloop.evolve.task_eval import make_agent_factory, run_tasks
    rt = rt or runtime(root)
    objects = objects or ct.Objects(root)
    if releases is None:
        releases, release, objects = workspace(root, split_ref=splits.hash(), worker_ref=worker_ref)
    release = releases.active()["release_ref"]
    bound = P.bind_release(release, profile="task", services=P.Services(objects, serving=serving),
                           authorization="bootstrap")
    manifest = run_manifest(candidate, role, split, data=splits.hash(), system_release_ref=release,
                            worker_ref=objects.load(release, ct.SystemRelease).worker_ref,
                            research_ref=objects.load(release, ct.SystemRelease).research_ref,
                            core_ref=objects.load(release, ct.SystemRelease).core_ref)
    factory = make_agent_factory(rt, provider or BusinessWorker(), budget=manifest.budget, bound=bound,
                                objects=objects, stop_view=stop_view)
    results = run_tasks(suite, list(tasks or TRAIN), factory, run=manifest, splits=splits, probe=probe,
                        repeats=repeats, arm=arm, plan_ref=plan_ref, release_ref=release)
    return {"rt": rt, "objects": objects, "bound": bound, "results": results, "releases": releases,
            "release": release, "run": manifest}


# ---------------------------------------------------------------------------- a scripted research step
class ScriptedResearcher:
    """A research step that reads the context it is given.

    It names a gap only where the results it can see show tasks that half-passed, and it cites evidence that is
    actually in the context. It proposes the carrier it was configured for, so a test can exercise the
    prompt-only path and the training path through the same loop. It never looks at a candidate's name, and it
    cannot see a task outside its partitions, because the context does not contain one.
    """

    def __init__(self, *, carrier: str = "prompt", instruction: str | None = None, min_labels: int = 4,
                 cite_missing: bool = False, target: str = "worker.finish", follow_prompt: bool = False):
        self.carrier = carrier
        self.instruction = instruction
        self.min_labels = min_labels
        self.cite_missing = cite_missing       # RL-04: cite evidence that does not exist
        self.target = target
        # With follow_prompt, the change it proposes depends on what the loaded research policy's propose prompt
        # asks for: that is how a campaign can tell two research policies apart by what they produce.
        self.follow_prompt = follow_prompt
        self.seen: list = []
        self.requests: list = []

    def diagnose(self, context, prompt: str):
        self.requests.append({"step": "diagnose", "prompt": prompt,
                              "prompt_hash": ct.digest({"system": prompt})[:16]})
        self.seen.append(context)
        out = []
        for task_id, entry in sorted((context.results or {}).get("tasks", {}).items()):
            if entry.get("pass_rate") in (None, 1.0):
                continue
            if not entry.get("mean_partial_credit"):
                continue
            out.append({"id": f"req:{task_id}", "context": f"{task_id} in {entry['split']}",
                        "expected_behavior": "every requirement of the request is carried out, including the "
                                             "one stated at the end",
                        "observed_behavior": f"the task scored {entry['mean_partial_credit']} and completed "
                                             f"{entry['pass_rate']}",
                        "evidence_refs": list(context.evidence[:2]), "acceptance": "the task completes",
                        "recurrence": entry.get("attempts", 1)})
        return out

    def propose(self, context, prompt: str, requirements, experience=()):
        from ouroloop.research.experiment import ProposalResult
        self.requests.append({"step": "propose", "prompt": prompt,
                              "prompt_hash": ct.digest({"system": prompt})[:16]})
        if not requirements:
            return ProposalResult([], next_action="collect", reason="no task in the context shows a gap")
        evidence = tuple(context.evidence[:2])
        if self.cite_missing:
            evidence = ("evidence:sha256:" + "f" * 64,)
        if self.carrier == "train":
            labels = int((context.feedback or {}).get("valid") or 0)
            if labels < self.min_labels:
                return ProposalResult([], next_action="collect",
                                      reason=f"{labels} labels is too few to train on")
            options = (context.targets or {}).get("worker.stop.training")
            if not options:
                return ProposalResult([], next_action="stop",
                                      reason="this activity has no trainer, so training cannot be proposed")
            proposal = ct.ExperimentProposal(
                owner="worker", hypothesis="a learned completion judgement stops the agent from finishing early",
                requirements=tuple(r["id"] for r in requirements), evidence_refs=evidence, changes=(),
                training=({"point": options["point"], "feedback_ref": options["feedback"][0],
                           "data_selection_ref": options["data_selection_ref"],
                           "recipe_ref": options["recipe_ref"]},),
                expected="the model proposes no at the moment the notification is still missing")
            return ProposalResult([proposal], next_action="none", evidence_refs=evidence)
        text = self.instruction
        if text is None:
            from ouroloop.policies import FINISH_CHECK
            text = FINISH_CHECK
            if self.follow_prompt and "finish check" not in (prompt or "").lower():
                # The prompt did not ask for a check of the request's requirements, so this step proposes the
                # blander change it was told to: the step follows its instructions, not a version name.
                text = "Keep your final message short and say what you did."
        change = ({"target": self.target, "kind": "workflow",
                   "change": {"builtin": "finish_check.v1", "instruction": text}}
                  if self.carrier == "workflow" else
                  {"target": self.target, "kind": "prompt", "change": {"text": text}})
        proposal = ct.ExperimentProposal(
            owner="worker", hypothesis="the agent finishes before doing the step stated at the end of the request",
            requirements=tuple(r["id"] for r in requirements), evidence_refs=evidence,
            changes=(change,), expected="the half-passing tasks complete, and the others do not regress",
            abandon_if="the tasks that already passed start failing")
        return ProposalResult([proposal], next_action="none", evidence_refs=evidence)


class ResearchPolicyProposer:
    """A research step that proposes a change to its own R, from what the research history shows.

    It looks for rounds that ended without an installed candidate — evidence that the way this policy proposes
    changes is not closing the gaps — and proposes a propose-prompt that names the missing step. It reads the
    experience it was given; it does not know which version it is running as.
    """

    def __init__(self, *, target: str = "research.propose", instruction: str | None = None):
        self.target = target
        self.instruction = instruction
        self.requests: list = []

    def diagnose(self, context, prompt: str):
        self.requests.append({"step": "diagnose", "prompt_hash": ct.digest({"system": prompt})[:16]})
        idle = [e for e in (context.experience or ())
                if e.get("kind") == "round" or (e.get("kind") == "experiment" and not e.get("installed"))]
        half = [t for t, entry in ((context.results or {}).get("tasks") or {}).items()
                if entry.get("pass_rate") not in (None, 1.0) and entry.get("mean_partial_credit")]
        if not idle and not half:
            return []
        return [{"id": "req:research.propose", "context": "the research history of this activity",
                 "expected_behavior": "a proposal names the step the request asks for at the end and how it "
                                      "would be checked",
                 "observed_behavior": f"{len(idle)} rounds installed nothing while {len(half)} tasks kept "
                                      f"half-passing",
                 "evidence_refs": list(context.evidence[:1]), "acceptance": "the next proposal closes the gap",
                 "recurrence": max(len(idle), 1)}]

    def propose(self, context, prompt: str, requirements, experience=()):
        from ouroloop.research.experiment import ProposalResult
        self.requests.append({"step": "propose", "prompt_hash": ct.digest({"system": prompt})[:16]})
        if not requirements:
            return ProposalResult([], next_action="collect", reason="the research history shows no gap yet")
        from ouroloop.policies import PROPOSE
        text = self.instruction or (
            PROPOSE + " When a request states a requirement at its end, propose a finish check that names the "
                      "tool result showing each requirement is done.")
        proposal = ct.ExperimentProposal(
            owner="research", hypothesis="a propose step that asks for a finish check produces candidates that "
                                        "close end-of-request gaps",
            requirements=tuple(r["id"] for r in requirements), evidence_refs=tuple(context.evidence[:1]),
            changes=({"target": self.target, "kind": "prompt", "change": {"text": text}},),
            expected="the worker candidates this policy proposes close the gap the requirement names")
        return ProposalResult([proposal], next_action="none", evidence_refs=tuple(context.evidence[:1]))
