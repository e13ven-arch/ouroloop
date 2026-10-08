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

from ouroloop.adapters import automationbench as ab
from ouroloop.benchmark import Budget, RunManifest, SplitManifest, digest
from ouroloop.chat import ChatTurn, ToolCall
from ouroloop.evolve import contracts as ct
from ouroloop.workspace import Workspace

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


def ws(root) -> Workspace:
    """What an attempt records its session in: the thin workspace the runner shell needs (bench.attempt_workspace)."""
    return Workspace(root)


def harness_table() -> dict:
    """The harness table of a v3-style K: the automationbench_harness_v3 preset's, with no sandbox (the tests that
    need the isolated worker build their own). K fixes `max_effort`, as every K frozen since v0.3 does."""
    from ouroloop import bench
    table = bench.harness_settings({"improvement": {"mode": "automatic", "preset": "automationbench_harness_v3"}})
    return {**table, "sandbox": None, "image": None}


def bootstrap_workspace(root, *, objects: ct.Objects | None = None, worker_ref: str | None = None,
                        research_ref: str | None = None, core_ref: str | None = None, **core_kw):
    """A workspace with W0, R0, K and a bootstrap release installed (formerly policies.bootstrap_workspace, which only
    tests called). -> (Releases, release ref)."""
    from ouroloop import policies as P
    from ouroloop.release import Releases
    objects = objects or ct.Objects(root)
    releases = Releases(root, objects)
    worker_ref = worker_ref or P.default_worker_set(objects)
    research_ref = research_ref or P.default_research_set(objects)
    core_ref = core_ref or P.default_core(objects, **core_kw)
    return releases, releases.bootstrap(worker_ref, research_ref, core_ref)


def workspace(root, split_ref: str = "", worker_ref: str | None = None, research_ref: str | None = None,
              **core_kw):
    """A workspace with W0, R0, a v3-style K and a bootstrap release installed. -> (Releases, release ref, Objects)."""
    objects = ct.Objects(root)
    capabilities = {"training": False, "harness": harness_table(), **core_kw.pop("capabilities", {})}
    releases, rel = bootstrap_workspace(root, objects=objects, worker_ref=worker_ref,
                                          research_ref=research_ref, domain=DOMAIN, split_ref=split_ref,
                                          adapter="ouroloop.adapters.automationbench", scorer="rubric.v1",
                                          toolset=ab.TOOLSET, capabilities=capabilities, **core_kw)
    return releases, rel, objects


def run_manifest(candidate: str, role: str, split: str, *, budget: Budget | None = None,
                 data: str = "", **v2) -> RunManifest:
    return RunManifest(candidate, role, split, {"provider": "scripted", "model": "scripted-1"},
                       prompt_hash=digest({"system": candidate})[:16],
                       environment={"upstream": "fake", "toolset": ab.TOOLSET},
                       budget=budget or Budget(max_steps=8, max_tool_calls=12), data=data, **v2)


def open_reservations(book, scope_id: str | None = None) -> list[dict]:
    """The book's reservations still open or unknown, in `scope_id`'s subtree (all scopes without one): what the
    money-safety tests check is left behind. A read of the book for the tests; nothing in the framework asks it."""
    data = book._read()
    scopes = book.subtree(scope_id, data) if scope_id else set(data["scopes"])
    return [r for r in data["reservations"].values() if r["status"] in ("open", "unknown") and r["scope_id"] in scopes]


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
        # A capability package speaks through the same channels: its memory is in the system prompt, and a skill's
        # text arrives as the result of a read_skill call the worker chose to make.
        read = [r.get("output", "") for m in messages if m.get("role") == "tool" for r in m.get("results") or ()]
        reminded = (FINISH_CHECK_MARK in (system or "") and not self.ignore_finish_check) or \
                   (pushed and not self.ignore_pushback) or any(FINISH_CHECK_MARK in str(r) for r in read)
        usage = {"input_tokens": 100, "output_tokens": 20}
        skills = re.findall(r"^- ([a-z0-9_-]+)", (system or "").split("read_skill tool", 1)[1], re.M) \
            if "read_skill tool" in (system or "") else []
        asked = {c.args.get("name") for m in messages for c in m.get("tool_calls") or () if c.name == "read_skill"}
        unread = [name for name in skills if name not in asked]
        if unread and any(sp.name == "read_skill" for sp in specs or ()):
            return ChatTurn("", [ToolCall(f"k{len(asked)}", "read_skill", {"name": unread[0]})], "tool_use",
                            usage=usage)
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


def collect(root, suite, splits, *, worker_ref=None, provider=None, tasks=(), probe=True, serving=None,
            releases=None, objects=None, split="train", repeats=1, candidate="W0", role="base",
            arm="subject", plan_ref=""):
    """Run some tasks with a bound release under the runner shell and return everything a later step needs.

    `probe` attaches the completion probe, which the runner refuses anywhere but the train split; the runner shell
    asks no stop point, so it labels nothing (the stop decisions the training tests build from are played by
    tests/train/stop_fixtures.py)."""
    from ouroloop import policies as P
    from ouroloop.evolve.task_eval import make_agent_factory, run_tasks
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
    factory = make_agent_factory(ws(root), provider or BusinessWorker(), budget=manifest.budget, bound=bound)
    results = run_tasks(suite, list(tasks or TRAIN), factory, run=manifest, splits=splits, probe=probe,
                        repeats=repeats, arm=arm, plan_ref=plan_ref, release_ref=release)
    return {"objects": objects, "bound": bound, "results": results, "releases": releases, "release": release,
            "run": manifest}


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


# ---------------------------------------------------------------------------- a scripted coding session (v0.3)
class SessionScript:
    """A research step for the coding-session round (docs/modules/04_RESEARCH_LOOP.md §1, v0.3): notes as the
    scripted note researchers write them, and the session played by tool calls from a plan.

    `plan(inputs) -> [candidate]` reads the session's first message (the round's inputs as JSON: notes, coverage,
    package_files, ledger, histogram, k, limits) and returns the candidates to make, each
    {"dir", "files": {path: text}, "declaration": {mechanism, site, carrier, trigger_condition, differs_from?}} —
    `notes` and `predicted_affected` are filled from the inputs' notes when the candidate leaves them out, so the
    declaration cites what the round really gave it. Each candidate's files go out in one turn of write_file calls
    and the last turn is `finish`. `repair(message, inputs) -> [candidate]` answers a check output in the same
    session: its candidates' files are written and `finish` is called again. The script reads only what the
    session hands it and never looks up a workspace.

    v0.4 (docs/design/V0_4_PLAN.md §1.3): with `read` (the default) the session's first turn reads, with
    read_trajectory, one listed failed attempt among each candidate's predicted tasks and the first listed passing
    attempt — what admission's read floors ask of a candidate under the v3 preset. `read=False` plays a session that
    reads nothing."""
    origin = "scripted"

    def __init__(self, plan, *, repair=None, note=None, read: bool = True):
        self.plan, self.repair, self._note, self.read = plan, repair, note, read
        self.requests: list = []
        self.seen: list = []
        self.inputs: dict | None = None
        self._queue: list = []

    def note_request(self, summary, prompt, *, allowed=()):
        return prompt, json.dumps({"allowed": list(allowed), "failure": summary}, sort_keys=True)

    def note(self, system, user):
        failure = json.loads(user)["failure"]
        self.requests.append({"step": "note", "task": failure["task_id"]})
        self.seen.append(user)
        if self._note is not None:
            return self._note(failure)
        missed = any("slack" in json.dumps(c).lower() or "message" in json.dumps(c).lower()
                     for c in failure["checks"] if not c["passed"])
        return {"failure_class": "missed_step" if missed else "other",
                "root_cause": "finished after the update without doing the rest of the request",
                "mechanism": "re-read the request and check every step before finishing",
                "component": "memory", "generality": "any multi-step request can end early"}

    def _declare(self, candidates: list) -> ToolCall:
        notes = [n for n in (self.inputs or {}).get("notes") or ()]
        declared = []
        for c in candidates:
            d = dict(c["declaration"])
            d.setdefault("notes", [n["note_ref"] for n in notes])
            d.setdefault("predicted_affected", sorted({n["task_id"] for n in notes}))
            d.setdefault("trigger_condition", "always")
            declared.append({"dir": c["dir"], **d})
        return ToolCall("f1", "finish", {"candidates": declared})

    def _reads(self, candidates: list) -> list:
        """One read_trajectory per candidate of a listed failed attempt among its predicted tasks (the notes' tasks
        when it names none), and one of the first listed passing attempt."""
        listed = list((self.inputs or {}).get("trajectories") or ())
        notes = [n["task_id"] for n in (self.inputs or {}).get("notes") or ()]
        refs = []
        for c in candidates:
            predicted = set(c["declaration"].get("predicted_affected") or notes)
            hit = next((e["attempt_ref"] for e in listed if e.get("outcome") != "passing"
                        and e.get("task_id") in predicted), None)
            if hit and hit not in refs:
                refs.append(hit)
        passing = next((e["attempt_ref"] for e in listed if e.get("outcome") == "passing"), None)
        if passing:
            refs.append(passing)
        return [ToolCall(f"r{i}", "read_trajectory", {"attempt_ref": ref, "limit": 4096}) for i, ref in enumerate(refs)]

    def _enqueue(self, candidates: list) -> None:
        if not candidates:
            # nothing to propose: the session ends with a next_action, as the summary call answered `stop`
            self._queue.append([ToolCall("f1", "finish", {"next_action": "stop", "reason": "nothing to change"})])
            return
        for c in candidates:
            self._queue.append([ToolCall(f"w{i}", "write_file", {"path": f"candidates/{c['dir']}/{path}", "text": text})
                                for i, (path, text) in enumerate(sorted(c["files"].items()))])
        self._queue.append([self._declare(candidates)])

    def session_turn(self, system, messages, tools):
        self.requests.append({"step": "session", "system": system, "prompt_hash": ct.digest({"system": system})[:16]})
        last = messages[-1]
        self.seen.append(json.dumps(last, default=str))
        if len(messages) == 1:
            # a new session: its first message is the round's inputs and nothing else (one session per round)
            self.inputs = json.loads(messages[0]["content"])
            self._queue = []
            candidates = self.plan(self.inputs)
            reads = self._reads(candidates) if self.read and candidates else []
            if reads:
                self._queue.append(reads)
            self._enqueue(candidates)
        elif last.get("role") == "user" and "failed its draft check" in str(last.get("content", "")):
            self._queue = []
            fixed = self.repair(str(last["content"]), self.inputs) if self.repair is not None else []
            if fixed:
                self._enqueue(fixed)
            else:
                return ChatTurn("I cannot fix this.", [], "end")
        if not self._queue:
            return ChatTurn("done", [], "end")
        calls = self._queue.pop(0)
        return ChatTurn("", calls, "tool_use")
