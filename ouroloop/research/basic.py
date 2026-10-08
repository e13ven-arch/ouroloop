"""The default research steps of the task/research profile (docs/modules/04_RESEARCH_LOOP.md §2): a researcher that
proposes changes to registered policy targets from a ResearchContext, using the prompts of the research policy set
that is actually loaded — so replacing R replaces the text of the real request, and a version that only renamed
itself makes identical requests and fails the acceptance. The old decision research's BasicResearcher, which
proposed decision-point spec and recipe changes through the gate record, was removed with it in P1 step 5 (tag
archive-2026-10-08)."""
from __future__ import annotations

import json
import re

from ..evolve import contracts as _ct
from .experiment import NEXT_ACTIONS, ProposalResult as _ProposalResult


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


REQUIREMENT_FORMAT = (
    'Reply with JSON only: {"requirements": [{"id": "...", "context": "...", "expected_behavior": "...", '
    '"observed_behavior": "...", "evidence_refs": ["..."], "acceptance": "...", "recurrence": 1}]}. '
    "Every observed_behavior must cite evidence from the context. Do not propose a fix here.")
PROPOSAL_FORMAT = (
    'Reply with JSON only: {"next_action": "none|collect|repair_runtime|stop", "reason": "...", '
    '"proposals": [{"hypothesis": "...", "requirements": ["..."], "evidence_refs": ["..."], '
    '"changes": [{"target": "...", "kind": "prompt|rule|workflow|model", "change": {...}}], '
    '"training": [], "expected": "...", "abandon_if": "..."}]}. '
    "The targets you may change, and what each accepts, are in the context. Choose the simplest carrier that "
    "could close the gap. Do not fill in results, verdicts or thresholds.")


NOTE_FORMAT = (
    'Reply with JSON only: {"failure_class": "...", "root_cause": "...", "mechanism": "...", '
    '"component": "memory|skills|on_finish|before_tool|after_tool|tools|agent", "generality": "..."}. '
    "component must be one of the allowed components.")
# What R is told about the package API (docs/modules/00_CONTRACTS.md §2 包 API v2, M1 §1 首版能力包 API, v0.3): the
# manifest of both versions, the optional `agent` entry, the ctx surface as the host provides it, the default agent
# a package without an entry runs under (so the v1 rules below keep their meaning), and the test rules. Every claim
# here is one the gateway, the broker or the default agent enforces; a sentence that promised more would cost R a
# revision when the draft check or the gateway refused it.
PACKAGE_API = (
    "The package API (write only the kinds in allowed_components). manifest.json = {\"api_version\": "
    "\"harness_package.v1\" or \"harness_package.v2\", \"memory\": {\"enabled\": true|false}, \"skills\": [{\"name\": "
    "\"<name>\", \"path\": \"skills/<name>.md\", \"description\": \"<when to read it>\"}], \"hooks\": [{\"name\": "
    "\"<name>\", \"site\": \"before_tool|after_tool|on_finish\", \"entrypoint\": "
    "\"middlewares/<file>.py:<function>\"}], \"tools\": [{\"name\": \"<name>\", \"description\": \"...\", "
    "\"input_schema\": {\"type\": \"object\", \"properties\": {...}}, \"entrypoint\": "
    "\"tools/<file>.py:<function>\"}], and under v2 at most one optional \"agent\": {\"entrypoint\": "
    "\"agent/<file>.py:run\", \"roles\": [\"act\"] or [\"act\", \"aux\"]}}. "
    "A package without an agent entry runs under the default agent, the host's own loop shipped as "
    "agent/default_agent.py (the task under the composed system prompt; one act model call a turn offering the "
    "base tools, the enabled package tools and read_skill; the three hook sites; at most 2 on_finish pushbacks), "
    "exactly as before, so memory, skills, hooks and tools keep the meaning below. An agent entry replaces that "
    "loop with yours: def run(ctx, task) -> str, the string being the final message, and returning is finishing; "
    "whether hooks and package tools are called is then your code's business. ctx has five names and one "
    "self-report. ctx.model(messages, tools=(), role=\"act\", effort=None) -> .text, .tool_calls [{id, name, args}], "
    ".stop; messages are [{\"role\": \"system\"|\"user\"|\"assistant\"|\"tool\", ...}] as the default agent sends "
    "them (a first system message is the system prompt; an assistant entry has text and tool_calls; a tool entry "
    "has results [{id, name, output, is_error}]), tools are [{name, description, schema}], and the calls the model "
    "returns are yours to execute; role must be one the manifest's roles declare, else the call is refused as an "
    "error you may catch; effort is low|medium|high|xhigh|max or None (the pinned default), and a request above "
    "K's max_effort is sent at max_effort, never refused. ctx.tool(name, args, read_only=False) -> .output, "
    ".is_error: one base tool (ctx.view[\"base_tools\"] lists them with their schemas) through the host's broker. "
    "ctx.skills.index is [{name, description}], ctx.skills.read(name) -> the text or None, ctx.skills.memory the "
    "memory text. ctx.limits is the remaining act_calls, model_calls, tool_calls, seconds and tokens (None = no "
    "such limit); a call past one ends the attempt, scored as the world stands. ctx.view = {task, system, "
    "base_tools, package, policies, events, max_output_chars}. ctx.mark(kind, **data) records a note labelled as "
    "the package's own report; nothing reads it as evidence. Nothing else is reachable from the package: no "
    "scorer, provider, store, record or file outside it. "
    "memory.md is text the actor always sees; a skill's text is read on "
    "demand with read_skill, listed by name and description. A hook is def f(ctx, event) returning "
    "{\"action\": \"continue\"|\"block\"|\"feedback\", \"message\": \"...\"}. before_tool: event = {tool, "
    "args, turn}; block or feedback both stop that call and the rest of the turn, with the message as the "
    "result. after_tool: event = {tool, args, result, is_error, turn}, where result is at most the first 4000 "
    "characters of what the tool returned; only continue or feedback, and feedback is a note added after the "
    "tool results. on_finish: event = {final_message, turn}; block or feedback both "
    "send the actor back to work with the message. A tool is def f(ctx, args) returning a string, and must not "
    "take a base tool's name. A tool's name is letters, digits, '_' and '-' (at most 64); its input_schema is a "
    "JSON object schema whose properties are schemas and whose arrays give items; \"enabled\": false keeps an "
    "entry unused. A hook's or tool's ctx.view is the agent's view above with turn added; each event in events is "
    "{call_id, name, args, result_excerpt, error, truncated, step}: result_excerpt is the first part of the result and truncated "
    "says only that this excerpt is shorter than the result. The actor itself is always handed the whole "
    "result: nothing in a hook's input means the actor saw a cut. ctx.call_tool(name, args) calls one of the episode's base tools, "
    "which `tools` in the input lists with their parameters (read-only from a hook), and returns .output and "
    ".is_error. tests/*.py hold test_* functions taking ctx, run "
    "against a train-only fixture world, where ctx.view is {\"fixture\": true}. A test file is loaded by its path "
    "and the package root is not on sys.path, so a test reaches package code with "
    "importlib.util.spec_from_file_location(os.path.join(os.path.dirname(__file__), '..', '<path>')), not an "
    "import. Standard library only; no network and no files outside the package.")
PACKAGE_FORMAT = (
    'Reply with JSON only: {"requirements": [{"id": "...", "context": "...", "expected_behavior": "...", '
    '"observed_behavior": "...", "acceptance": "...", "recurrence": 1}], "next_action": '
    '"none|collect|repair_runtime|stop", "reason": "...", "proposals": [{"hypothesis": "...", "requirements": '
    '["..."], "changes": [{"target": "worker.harness", "kind": "workflow", "change": {"patch": {"write": '
    '{"<path>": "<text>"}, "delete": ["<path>"]}}}], "expected": "...", "abandon_if": "..."}]}. '
    "A package holds manifest.json, memory.md and skills/<name>.md (and code files only where allowed); the "
    "manifest must list every skill and enable memory when memory.md is used. `targets` shows each registered "
    "target and, under `current`, the instruction text W runs with now: change or merge with it rather than "
    "adding a component that repeats it; other targets may be changed instead of the package. Each kind has its "
    "own change shape: a package change is {\"target\": \"worker.harness\", \"kind\": \"workflow\", \"change\": "
    "{\"patch\": {...}}}; a prompt change is {\"target\": \"<target>\", \"kind\": \"prompt\", \"change\": {\"text\": "
    "\"<the complete new text>\"}} - the whole text W should run with, not a patch. Do not fill in results, "
    "verdicts or thresholds. " + PACKAGE_API)
RESEARCH_FORMAT = (
    "This call is a research slot. The text above is research.propose: this research policy's instructions for "
    "worker rounds, where they change the capability package. This call proposes a change to those instructions "
    "themselves - research.propose or research.diagnose, whose current texts are in the input - not to the "
    "package. Where the text above and this format differ (what to read, what to change, what to reply), this "
    "format governs. Read how the research policy's own rounds went: the verdicts and aggregate figures of the "
    "experience log, the note coverage, the drafts that failed their checks. Propose at most one change to one of "
    "the two prompts that would make the next rounds' notes or proposals better, as its complete new text. Reply "
    'with JSON only: {"next_action": "none|stop", "reason": "...", "proposals": [{"hypothesis": "...", "target": '
    '"research.diagnose|research.propose", "text": "<the complete new prompt>", "expected": "...", '
    '"abandon_if": "..."}]}. Do not write task answers, task ids or scores into a prompt.')
REVISE_FORMAT = (
    'The draft package below failed its check. Reply with JSON only: {"patch": {"write": {"<path>": "<text>"}, '
    '"delete": ["<path>"]}} — the corrected patch against the parent package shown. ' + PACKAGE_API)


def _as_list(value) -> list:
    """A model's field that should be a list: a single object is one item, anything else is none."""
    return value if isinstance(value, list) else [value] if isinstance(value, dict) else []


def _as_int(value, default: int = 1) -> int:
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return default


class PolicyResearcher:
    """The researcher of the task profile: a fixed base model, prompts from the loaded research policy set.

    It proposes and nothing more. It cannot write a verdict, a threshold or a result — those fields do not exist
    on a proposal — and every reference it cites is checked against the context it was given, so a hallucinated
    evidence id is an invalid proposal rather than a fact (RL-04).
    """

    origin = "llm"                          # what this researcher's candidates are: a model proposed them (M0 §3)

    def __init__(self, llm, *, max_proposals: int = 1, max_state_chars: int = 12000):
        self.llm = llm                      # .complete(system, prompt) -> text; .turn(system, messages, tools) -> ChatTurn
        self.max_proposals = max_proposals
        self.max_state_chars = max_state_chars
        self.requests: list[dict] = []      # what was actually sent, so a test can see which R ran

    def session_turn(self, system: str, messages: list, tools: list):
        """One model call of R's coding session (docs/modules/04_RESEARCH_LOOP.md §1 R 作为有界编码会话, v0.3): the
        research model under `research.propose`'s text plus the session format, with the session's tools, metered
        and labelled `session` like every research call. Each turn is a request on record (step "session", the
        prompt digest, the digest of the latest user message), so the reuse check reads which R's text drove the
        session (M6 §4). The completion interface answers with a ChatTurn; one without `turn` cannot run a session."""
        turn = getattr(self.llm, "turn", None)
        if turn is None:
            raise _ct.ContractError("the research completion interface has no tool-calling turn: a coding session "
                                    "needs the chat provider, not a plain completion")
        latest = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        request = {"step": "session", "system": system, "prompt_hash": _ct.digest({"system": system})[:16],
                   "input_hash": _ct.digest({"input": str(latest.get("content", ""))})[:16]}
        self.requests.append(request)
        label = getattr(self.llm, "label", None)
        if callable(label):
            with label("session"):
                return turn(system, messages, tools)
        return turn(system, messages, tools)

    def diagnose(self, context, prompt: str) -> list[dict]:
        text = self._ask(prompt, context, REQUIREMENT_FORMAT, "diagnose")
        out = []
        for item in parse_json(text).get("requirements", []) or []:
            if not isinstance(item, dict) or not item.get("observed_behavior"):
                continue
            cited = [r for r in item.get("evidence_refs") or [] if r in set(context.evidence)]
            out.append({**{k: item.get(k, "") for k in ("id", "context", "expected_behavior",
                                                        "observed_behavior", "acceptance")},
                        "evidence_refs": cited, "recurrence": int(item.get("recurrence") or 1),
                        "uncited": [r for r in item.get("evidence_refs") or [] if r not in set(context.evidence)]})
        return out

    def propose(self, context, prompt: str, requirements, experience=()) -> _ProposalResult:
        text = self._ask(prompt, context, PROPOSAL_FORMAT, "propose",
                         extra={"requirements": requirements, "experience": list(experience)[:10]})
        data = parse_json(text)
        proposals = []
        for item in (data.get("proposals") or [])[:self.max_proposals]:
            if not isinstance(item, dict):
                continue
            try:
                proposals.append(_ct.ExperimentProposal(
                    owner="worker", hypothesis=str(item.get("hypothesis", ""))[:2000],
                    requirements=tuple(str(r) for r in item.get("requirements") or ()),
                    evidence_refs=tuple(r for r in item.get("evidence_refs") or () if r in set(context.evidence)),
                    changes=tuple(item.get("changes") or ()), training=tuple(item.get("training") or ()),
                    expected=str(item.get("expected", ""))[:1000],
                    abandon_if=str(item["abandon_if"])[:500] if item.get("abandon_if") else None))
            except _ct.ContractError:
                continue                    # a malformed proposal is dropped here and counted by the caller
        action = data.get("next_action") if data.get("next_action") in ("collect", "repair_runtime", "stop",
                                                                       "none") else "none"
        return _ProposalResult(proposals=proposals, next_action=action if not proposals else "none",
                               reason=str(data.get("reason", ""))[:500],
                               evidence_refs=tuple(context.evidence))

    def _complete(self, step: str, system: str, text: str, request: dict | None = None) -> str:
        """One research request, labelled with its step where the completion interface can carry a label, so
        the budget book says what notes, summaries and draft revisions each cost (docs/modules/07 §4). A refusal
        comes back as no text, with the refusal kept on the request for `_read` to name. `request` is the record
        this call belongs to; without it, the last one made — which notes written concurrently (M5 §2, v0.3)
        cannot rely on, so `note_with_record` passes its own."""
        from ..providers import ReplyRefused
        request = request if request is not None else (self.requests[-1] if self.requests else None)
        if request is not None and "input_hash" not in request:
            # What the step was given, as a digest beside the prompt's: the round's record can then show R's input
            # without keeping task text in it.
            request["input_hash"] = _ct.digest({"input": text})[:16]
        label = getattr(self.llm, "label", None)
        try:
            if callable(label):
                with label(step):
                    return self.llm.complete(system, text)
            return self.llm.complete(system, text)
        except ReplyRefused as e:
            if request is not None:
                request["refused"] = str(e)[:200]
            return ""

    def _read(self, reply: str, request: dict | None = None) -> tuple[dict, str]:
        """The reply as the JSON object R meant, or why it cannot be read (docs/modules/04_RESEARCH_LOOP.md §1): an
        empty reply, a refusal or text with no JSON object in it decides nothing, so it is never read as {} and
        from there as a default decision."""
        request = request if request is not None else (self.requests[-1] if self.requests else {})
        refused = request.get("refused")
        if refused:
            # the refusal as the completion interface stated it, which already says it declined
            return {}, refused if refused.startswith("the research model declined") else \
                f"the research model declined the request: {refused}"
        if not reply.strip():
            return {}, "the research reply was empty"
        data = parse_json(reply)
        if not data and not re.search(r"\{\s*\}", reply):
            return {}, "the research reply was not readable JSON"
        return data, ""

    def _ask(self, prompt: str, context, fmt: str, step: str, extra: dict | None = None) -> str:
        state = context.prompt_state(self.max_state_chars)
        if extra:
            state += "\n" + json.dumps(extra, ensure_ascii=False, default=str)[:4000]
        system = f"{prompt}\n\n{fmt}" if prompt else fmt
        self.requests.append({"step": step, "system": system, "prompt_hash": _ct.digest({"system": system})[:16]})
        return self._complete(step, system, state)

    # ------------------------------------------------------------------ the first version's diagnosis
    # docs/modules/04_RESEARCH_LOOP.md §1: one note per failed train attempt through the bound research.diagnose,
    # then one summary call through research.propose over the notes, their coverage and the current package.
    def note_request(self, summary: dict, prompt: str, *, allowed: tuple = ()) -> tuple[str, str]:
        """The exact (system, user) text of one note request. Its digest is the note's identity, so everything
        that can change the answer — the prompt, the allowed kinds, the summary — is inside it."""
        system = f"{prompt}\n\n{NOTE_FORMAT}" if prompt else NOTE_FORMAT
        user = json.dumps({"allowed_components": list(allowed), "failure": summary}, ensure_ascii=False,
                          sort_keys=True, default=str)
        return system, user

    def note(self, system: str, user: str) -> dict:
        """One note. A reply that cannot be read gives no note, and why it could not (refused, empty, not JSON) is
        kept on the request, where the diagnosis records it."""
        return self.note_with_record(system, user)[0]

    def note_with_record(self, system: str, user: str) -> tuple[dict, str]:
        """One note and why it could not be read ("" when it could). The request record is this call's own, so
        notes asked for concurrently (M5 §2, v0.3) each keep their own refusal or unreadable reason, whatever
        order the answers arrive in."""
        request = {"step": "note", "system": system, "prompt_hash": _ct.digest({"system": system})[:16]}
        self.requests.append(request)
        data, problem = self._read(self._complete("note", system, user, request), request)
        if problem:
            request["unreadable"] = problem
        return data, problem

    def summarize(self, context, prompt: str, notes: list, coverage: dict, experience=(), *, package=None,
                  allowed: tuple = ()) -> _ProposalResult:
        """The summary call: Requirements plus at most one change. Its input is train-restricted by
        construction — notes on train failures, the package, allowed kinds, the targets, the experience log's
        verdicts and aggregate figures — and not the full context state, which lists per-task dev outcomes."""
        from .diagnosis import experience_projection
        # Every part is bounded at its source (note fields, package limits, ten experience rows), so the input is
        # sent whole: slicing serialised JSON would silently drop the coverage, the allowed kinds or the package.
        state = {"goal": context.goal, "coverage": coverage, "allowed_components": list(allowed),
                 "package": package or {}, "targets": context.targets,
                 # the host's base tools: what a package tool or hook can call (M4 §1)
                 "tools": list(getattr(context, "tools", ()) or ()),
                 # `experience` is what research.retrieve selected, in its order (newest first by default)
                 "experience": experience_projection(list(experience)[:10]), "budget": context.budget,
                 "notes": notes}
        system = f"{prompt}\n\n{PACKAGE_FORMAT}" if prompt else PACKAGE_FORMAT
        self.requests.append({"step": "propose", "system": system,
                              "prompt_hash": _ct.digest({"system": system})[:16]})
        text = json.dumps(state, ensure_ascii=False, default=str)
        data, problem = self._read(self._complete("propose", system, text))
        requirements = []
        for item in _as_list(data.get("requirements")):
            if isinstance(item, dict) and item.get("observed_behavior"):
                requirements.append({k: str(item.get(k, ""))[:1000] for k in
                                     ("id", "context", "expected_behavior", "observed_behavior", "acceptance")}
                                    | {"recurrence": _as_int(item.get("recurrence")), "evidence_refs": []})
        proposals = []
        listed = data.get("proposals")
        if listed is not None and not isinstance(listed, (list, dict)):
            problem = problem or "the research reply's proposals is not a list of proposals"
        for item in _as_list(listed):        # every one is read: a malformed one past the cap is not dropped
            try:
                if not isinstance(item, dict):
                    raise _ct.ContractError(f"a proposal is an object, not {type(item).__name__}")
                proposals.append(_ct.ExperimentProposal(
                    owner="worker", hypothesis=str(item.get("hypothesis", ""))[:2000],
                    requirements=tuple(str(r) for r in item.get("requirements") or ()),
                    changes=tuple(item.get("changes") or ()), training=(),
                    expected=str(item.get("expected", ""))[:1000],
                    abandon_if=str(item["abandon_if"])[:500] if item.get("abandon_if") else None,
                    origin="llm"))
            except _ct.ContractError as e:
                # Never dropped and the rest read on: a reply with a malformed proposal is not R's decision.
                problem = problem or f"a proposal in the research reply is malformed: {str(e)[:300]}"
        action = data.get("next_action")
        if not problem and not proposals and action not in NEXT_ACTIONS:
            problem = (f"the research reply has no proposal and no next_action" if action is None else
                       f"the research reply's next_action {action!r} is not one of {list(NEXT_ACTIONS)}")
        proposals, dropped = proposals[:self.max_proposals], proposals[self.max_proposals:]
        result = _ProposalResult(proposals=[] if problem else proposals, dropped=() if problem else tuple(dropped),
                                 next_action="none" if problem or proposals else action,
                                 reason=problem or str(data.get("reason", ""))[:500],
                                 evidence_refs=tuple(context.evidence), unreadable=bool(problem))
        result.requirements = tuple(requirements)
        return result

    def propose_research(self, context, prompt: str, current: dict, experience=(), *,
                         allowed: tuple = ("research.diagnose", "research.propose")) -> _ProposalResult:
        """A research slot's proposal (docs/modules/04_RESEARCH_LOOP.md §5): the current R proposes a change to
        its own prompts from how its rounds went. The input is the experience log's fixed projection — verdicts,
        reason kinds, aggregate figures — and R's own prompt texts; never a per-task dev outcome."""
        from .diagnosis import experience_projection
        state = {"goal": context.goal, "current_prompts": {t: current.get(t, "") for t in allowed},
                 "experience": experience_projection(list(experience)[-20:]), "budget": context.budget}
        system = f"{prompt}\n\n{RESEARCH_FORMAT}" if prompt else RESEARCH_FORMAT
        self.requests.append({"step": "propose", "system": system,
                              "prompt_hash": _ct.digest({"system": system})[:16]})
        data, problem = self._read(self._complete("research_slot", system,
                                                  json.dumps(state, ensure_ascii=False, default=str)))
        # docs/modules/04_RESEARCH_LOOP.md §1: a reply that cannot be read, or a proposal that cannot be one,
        # decides nothing. Every proposal is checked; one that is not a candidate makes the whole reply unreadable
        # rather than being skipped, so what is left is never read as R declining. The text is taken whole: K's
        # prompt length limit decides at the build, not a cut here.
        listed = data.get("proposals")
        if listed is not None and not isinstance(listed, list):
            problem = problem or "the research reply's proposals is not a list of proposals"
        proposals = []
        for item in _as_list(listed):
            target, text = (item.get("target"), item.get("text")) if isinstance(item, dict) else (None, None)
            why = (f"it is a {type(item).__name__}, not an object" if not isinstance(item, dict) else
                   f"its target {target!r} is not one of {list(allowed)}" if target not in allowed else
                   "it has no new prompt text" if not isinstance(text, str) or not text.strip() else
                   f"its text is the current {target} prompt" if text.strip() == current.get(target, "").strip()
                   else "")
            if why:
                problem = problem or f"a proposal in the research reply is malformed: {why}"
                continue
            proposals.append(_ct.ExperimentProposal(
                owner="research", hypothesis=str(item.get("hypothesis", ""))[:2000],
                changes=({"target": target, "kind": "prompt", "change": {"text": text}},),
                expected=str(item.get("expected", ""))[:1000], abandon_if=str(item.get("abandon_if", ""))[:1000],
                origin="llm"))           # a model proposed it; the entry point is the automatic loop
        action = data.get("next_action")
        if not problem and not proposals and action not in ("none", "stop"):
            problem = ("the research reply has no proposal and no next_action" if action is None else
                       f"the research reply's next_action {action!r} is not one of ['none', 'stop']")
        # more than one proposal: propose_experiment keeps the first and records the rest as dropped
        return _ProposalResult([] if problem else proposals, next_action="none" if problem or proposals else action,
                               reason=problem or str(data.get("reason", ""))[:500], unreadable=bool(problem))

    def revise(self, prompt: str, patch: dict, check_output: str, parent: dict | None = None,
               tools=()) -> dict:
        """One bounded revision of a draft package. It sees the parent package, the patch and the check's own
        output only — never a task result or a score (docs/modules/04_RESEARCH_LOOP.md §2)."""
        system = f"{prompt}\n\n{REVISE_FORMAT}" if prompt else REVISE_FORMAT
        self.requests.append({"step": "revise", "system": system,
                              "prompt_hash": _ct.digest({"system": system})[:16]})
        data = parse_json(self._complete("revise", system, json.dumps(
            {"parent": parent or {}, "patch": patch, "check_output": check_output, "tools": list(tools)},
            ensure_ascii=False,
            default=str)))
        return data.get("patch") if isinstance(data.get("patch"), dict) else {}
