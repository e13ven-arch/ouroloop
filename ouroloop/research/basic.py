"""The default research agent: it targets the largest error group, the LLM proposes (asking the judge
typed questions when it wants a judgment), and every candidate within the budget is tried. The judge also answers
which group to target (research.pick_target) and whether a candidate is worth trying (research.worth_trying), but
those points are not switched on: the answers are recorded, and the defaults above are the actions.
Targets are error groups of decision points (spec and recipe candidates) and, when a task suite is configured, the
harness prompt's failing tasks."""
from __future__ import annotations

import json
import re

from .agent import Candidate, Workspace
from .ask import QuestionError

PROPOSE_SYSTEM = (
    "You are the researcher in a self-improving harness. A small decision model answers typed questions at fixed "
    "decision points; you improve it with small, single changes, each stating the hypothesis it tests. "
    "Reply with JSON only.")

KIND_HELP = {
    "spec": "spec: change one decision point's question wording (instructions), candidate descriptions (criteria) "
            "or view parameters (view_params)",
    "recipe": "recipe: retrain the decision model with a changed training recipe",
}

PROMPT_SYSTEM = (
    "You improve the prompt of a coding agent. The agent solves small coding tasks with four tools (read, write, "
    "edit, bash), and a task scores the share of its hidden checks that pass. Propose small, general changes that "
    "would help on tasks like these, never rules for one particular task. Reply with JSON only.")

PASS_QUESTION = {
    "type": "noul",
    "instructions": "This change will pass the promotion gate: a significant improvement on held-out data with no "
                    "regressions",
    "criteria": {"true": "likely to improve held-out results without regressions",
                 "false": "unlikely to help, or likely to cause regressions"},
}


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_candidates(text: str) -> list[dict]:
    items = parse_json(text).get("candidates", [])
    return [c for c in items if isinstance(c, dict) and isinstance(c.get("change"), dict)]


class BasicResearcher:
    def __init__(self, budget: int = 2, max_candidates: int = 3, consult: bool = True, max_questions: int = 3):
        self.budget = budget                  # candidates evaluated per round, in the order they were proposed
        self.max_candidates = max_candidates
        self.consult = consult                # let the LLM ask the judge typed questions before it finalizes
        self.max_questions = max_questions

    def step(self, ws: Workspace) -> list[Candidate]:
        out = []
        if ws.report.clusters:
            target, target_id = self._pick_target(ws, ws.report.clusters)
            situation = (f"Decision point: {target['point']}; error group: {target['n']} cases, {target['gold']} "
                         f"predicted as {target['predicted']}, signature {target['signature']}")
            out += self._filter(ws, situation, self._propose(ws, target, target_id))
        suite = [s for s in ws.context.get("suite", []) if s["score"] is not None]
        if "prompt" in ws.kinds and any(s["score"] < 1 for s in suite):
            failing = [s["task"] for s in suite if s["score"] < 1]
            situation = (f"Harness prompt of the coding agent; {len(suite) - len(failing)} of {len(suite)} "
                         f"development tasks fully solved; failing: {', '.join(failing)}")
            out += self._filter(ws, situation, self._propose_prompt(ws, suite))
        return out

    def _pick_target(self, ws: Workspace, clusters: list[dict]) -> tuple[dict, str | None]:
        if len(clusters) == 1:
            return clusters[0], None
        shown = sorted(clusters, key=lambda c: -c["n"])[:20]      # the largest group first: it is the default
        criteria = {c["id"]: f"{c['point']}: {c['n']} errors where {c['gold']} was predicted as {c['predicted']}, "
                             f"signature {c['signature']}" for c in shown}
        state = "\n".join([f"{p}: n={m['n']} brier={m['brier']:.3f} accuracy={m['accuracy']:.3f}"
                           for p, m in ws.report.points.items()] + [f"{k}: {v}" for k, v in criteria.items()])
        d = ws.ask("research.pick_target", {
            "type": "choice", "criteria": criteria,
            "instructions": "Which error group, if fixed, is most likely to yield a promoted improvement?"}, state,
            default=shown[0]["id"])
        return next(c for c in shown if c["id"] == d.action), d.id

    def _prompt(self, ws: Workspace, target: dict) -> str:
        point = target["point"]
        past = [e for e in ws.experience if point in str(e.get("point", ""))][-8:]
        kinds = [KIND_HELP.get(k, k) for k in ws.kinds if k != "prompt"]
        if "recipe" in ws.kinds and ws.context.get("recipe") is not None:
            kinds.append(f"  current recipe: {json.dumps(ws.context['recipe'], ensure_ascii=False)}; "
                         f"allowed keys: {', '.join(ws.context.get('recipe_keys', []))}")
        lines = [
            f"Decision point: {point} ({ws.point_type(point)})",
            f"Current spec (JSON): {json.dumps(ws.spec(point), ensure_ascii=False)}",
            f"Error group {target['id']}: {target['n']} dev-split decisions where the right answer was "
            f"\"{target['gold']}\" but the model said \"{target['predicted']}\". Error signature: {target['signature']}.",
            "Examples of the view the model saw:",
            *[f"---\n{ex}" for ex in target["examples"]],
            "---",
            "Candidate kinds you may propose:", *[f"- {k}" for k in kinds],
            "Past candidates for this point:" if past else "No past candidates for this point.",
            *[f"- {e.get('kind', 'spec')} {json.dumps(e.get('change'), ensure_ascii=False)} -> {e.get('gate')} "
              f"({e.get('verdict', '')})" for e in past],
            f'Return {{"candidates": [{{"kind": "...", "change": {{...}}, "hypothesis": "...", "cost": "low"}}]}} '
            f"with at most {self.max_candidates} candidates.",
        ]
        return "\n".join(lines + self._consult_help())

    def _consult_help(self) -> list[str]:
        if not self.consult:
            return []
        return [f'If a judgment would help you choose (for example which cause is more likely), you may also add up '
                f'to {self.max_questions} typed questions under "questions": each {{"type": "noul" or "choice", '
                f'"instructions": "...", "criteria": {{...}}, "state": "the facts it depends on"}}. A decision model '
                f"answers them, and then you give your final candidates."]

    def _prompt_for_harness(self, ws: Workspace, suite: list[dict]) -> str:
        past = [e for e in ws.experience if e.get("kind") == "prompt"][-8:]
        lines = [
            "Current system prompt ({workdir} stands for the working directory):", ws.context["prompt"]["system"],
            "Tool descriptions:", *[f"- {name}: {desc}" for name, desc in ws.context["tools"].items()],
            f"Development tasks: {sum(s['score'] == 1 for s in suite)} of {len(suite)} fully solved. "
            f"Held-out tasks are not shown. The failing ones:",
        ]
        for s in suite:
            if s["score"] < 1:
                lines += [f"--- {s['task']}: score {s['score']:.2f} after {s['turns']} turns (stop: {s['stop']})",
                          f"task: {s['prompt']}", "last tool calls:", *[f"  {a}" for a in s["actions"]],
                          f"failed checks:\n{s['failed_checks']}", f"final message: {s['final_message']}"]
        lines += [
            "---",
            "Past prompt candidates (what they changed, and the score change per development task):"
            if past else "No past prompt candidates.",
            *[line for e in past for line in self._past_prompt(e)],
            f'Return {{"candidates": [{{"kind": "prompt", "change": {{"system": "the complete new system prompt"}}, '
            f'"hypothesis": "...", "cost": "high"}}]}} with at most {self.max_candidates} candidates. A change may '
            f'also set "tools": {{"<tool name>": "new description"}}.',
        ]
        return "\n".join(lines + self._consult_help())

    @staticmethod
    def _past_prompt(e: dict) -> list[str]:
        d = e.get("details") or {}
        effects = ("not recorded" if d.get("effects") is None
                   else ", ".join(f"{t} {v:+.2f}" for t, v in d["effects"].items()) or "no task changed")
        return [f"- {e.get('gate')} ({e.get('verdict', '')}; {'; '.join(e.get('reasons') or [])}): "
                f"{str(e.get('hypothesis', ''))[:200]}",
                *[f"    {line[:200]}" for line in (d.get("diff") or [])[:6]], f"    effects: {effects}"]

    def _consult(self, ws: Workspace, questions: list) -> list[str]:
        answers = []
        for q in questions[:self.max_questions]:
            if not isinstance(q, dict):
                continue
            state = str(q.pop("state", "") or "")
            try:
                d = ws.ask("ask:research", q, state or q.get("instructions", ""))
            except (QuestionError, KeyError, TypeError):
                continue
            answers.append(f"- {q.get('instructions')} -> {d.label or 'no clear answer'} (p={d.confidence:.2f})")
        return answers

    def _ask_llm(self, ws: Workspace, system: str, prompt: str) -> list[dict]:
        """The LLM proposes; if it asked typed questions, the judge answers and the LLM gives its final list."""
        first = ws.llm(system, prompt)
        data = parse_json(first)
        items = parse_candidates(first)
        if self.consult and data.get("questions"):
            answers = self._consult(ws, list(data["questions"]))
            if answers:
                final = ws.llm(system, "\n".join([
                    prompt, "", f"Your first proposal: {json.dumps(data, ensure_ascii=False)}",
                    "Answers from the decision model:", *answers,
                    'Now return the final JSON with "candidates" only.']))
                items = parse_candidates(final) or items
        return items[:self.max_candidates]

    def _propose_prompt(self, ws: Workspace, suite: list[dict]) -> list[Candidate]:
        return [Candidate("prompt", "harness", c["change"], str(c.get("hypothesis", "")), str(c.get("cost", "high")),
                          {"target": "suite"})
                for c in self._ask_llm(ws, PROMPT_SYSTEM, self._prompt_for_harness(ws, suite))
                if str(c.get("kind", "prompt")) == "prompt"]

    def _propose(self, ws: Workspace, target: dict, target_id: str | None) -> list[Candidate]:
        out = []
        for c in self._ask_llm(ws, PROPOSE_SYSTEM, self._prompt(ws, target)):
            kind = str(c.get("kind", "spec"))
            if kind not in ws.kinds or kind == "prompt":
                continue
            meta = {"target": target["id"]}
            if target_id:
                meta["pick_target"] = target_id
            out.append(Candidate(kind, target["point"], c["change"], str(c.get("hypothesis", "")),
                                 str(c.get("cost", "low")), meta))
        return out

    def _filter(self, ws: Workspace, situation: str, candidates: list[Candidate]) -> list[Candidate]:
        """The candidates within the budget that research.worth_trying clears: all of them while it is not switched
        on. The judge's answer is recorded, and scored against the gate's verdict."""
        chosen = []
        for c in candidates[:self.budget]:
            state = "\n".join([
                situation,
                f"Proposed {c.kind} change: {json.dumps(c.change, ensure_ascii=False)}",
                f"Hypothesis: {c.hypothesis}",
                f"Cost: {c.cost}",
            ])
            d = ws.ask("research.worth_trying", PASS_QUESTION, state, default="yes")
            c.meta["worth_trying"] = d.id
            c.meta["p_pass"] = d.probs["yes"]
            if d.action == "yes":
                chosen.append(c)
        return chosen


# ---------------------------------------------------------------------------- the task/research profile
# docs/modules/04_RESEARCH_LOOP.md §2. BasicResearcher above proposes changes to decision-point specs and recipes
# through the legacy gate. This one proposes changes to registered policy targets, from a ResearchContext, using
# the prompts of the research policy set that is actually loaded — so replacing R replaces the text of the real
# request, and a version that only renamed itself makes identical requests and fails the acceptance.
from ..evolve import contracts as _ct
from .experiment import ProposalResult as _ProposalResult

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


class PolicyResearcher:
    """The researcher of the task profile: a fixed base model, prompts from the loaded research policy set.

    It proposes and nothing more. It cannot write a verdict, a threshold or a result — those fields do not exist
    on a proposal — and every reference it cites is checked against the context it was given, so a hallucinated
    evidence id is an invalid proposal rather than a fact (RL-04).
    """

    def __init__(self, llm, *, max_proposals: int = 1, max_state_chars: int = 12000):
        self.llm = llm                      # .complete(system, prompt) -> text
        self.max_proposals = max_proposals
        self.max_state_chars = max_state_chars
        self.requests: list[dict] = []      # what was actually sent, so a test can see which R ran

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

    def _ask(self, prompt: str, context, fmt: str, step: str, extra: dict | None = None) -> str:
        state = context.prompt_state(self.max_state_chars)
        if extra:
            state += "\n" + json.dumps(extra, ensure_ascii=False, default=str)[:4000]
        system = f"{prompt}\n\n{fmt}" if prompt else fmt
        self.requests.append({"step": step, "system": system, "prompt_hash": _ct.digest({"system": system})[:16]})
        return self.llm.complete(system, state)
