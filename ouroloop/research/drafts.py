"""M4 §2 / M5 §1: the pre-freeze draft check and its bounded revisions (docs/modules/04_RESEARCH_LOOP.md §2).

A package patch becomes a complete draft — the parent's files with the patch applied — and the draft is checked
before anything is frozen: the fixed shape, the component kinds K allows, and the literal check that keeps a
train task's answers out of a shared component. A failed check goes back to R with the check's own output and
nothing else, and R may revise at most `max_repairs` times after the first draft. Only a passing draft is frozen
into the proposal; every draft and check is recorded, and a round whose revisions run out ends as an invalid
proposal. A draft that needs the isolated worker for its checks, when the worker is unavailable, is an
infrastructure failure: no revision is consumed and nothing is returned to R.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..agent import harness_package as hp
from ..evolve import contracts as ct
from .diagnosis import clip

MAX_CHECK_OUTPUT = 2000


def payload_of(change: dict, objects: ct.Objects | None):
    """What a change carries, inline or already stored as a change object; None when it is neither or its stored
    object cannot be read (admission's core checks refuse such a change). A proposal is normalised before its
    checks, so both forms reach them."""
    payload = change.get("change")
    if payload is None and "change_ref" in change and objects is not None:
        try:
            payload = objects.get(change["change_ref"]) if objects.has(change["change_ref"]) else None
        except (ct.ContractError, KeyError):
            return None
    return payload


def patch_of(change: dict, objects: ct.Objects) -> dict | None:
    """The package patch a change carries; None for any other change."""
    if change.get("target") != "worker.harness":
        return None
    payload = payload_of(change, objects)
    return payload.get("patch") if isinstance(payload, dict) and isinstance(payload.get("patch"), dict) else None


PROMPT_TARGETS = ("worker.system_prompt", "worker.finish")


def text_of(change: dict, objects: ct.Objects) -> str | None:
    """The instruction text a change gives the actor outside the package: a prompt's text, or a finish check's
    instruction. None for any other change. The decoded text, never its JSON: an escape such as "\\n" must not
    hide a literal from the check."""
    if change.get("target") not in PROMPT_TARGETS:
        return None
    payload = payload_of(change, objects)
    if isinstance(payload, dict) and isinstance(payload.get("instruction_ref"), str):
        try:
            payload = objects.get(payload["instruction_ref"]) if objects.has(payload["instruction_ref"]) else None
        except (ct.ContractError, KeyError):
            return None
    if isinstance(payload, str):
        return payload
    text = payload.get("text", payload.get("instruction")) if isinstance(payload, dict) else None
    return text if isinstance(text, str) else None


from ..agent.worker import WorkerCrashed, WorkerUnavailable, check_package  # noqa: E402


@dataclass
class DraftOutcome:
    status: str                                 # ready | invalid | infrastructure_failed
    proposal: ct.ExperimentProposal | None = None
    package_ref: str | None = None
    drafts: list = field(default_factory=list)  # {"revision", "package_ref", "ok", "errors"}
    reason: str = ""


@dataclass
class Drafts:
    """The fixed part of a round's draft checks: the parent package, K's limits, the forbidden literals, the
    revision bound, where checks are recorded, and the isolated worker when there is one (stage B)."""
    objects: ct.Objects
    parent_ref: str
    limits: hp.Limits
    literals: set = field(default_factory=set)
    max_repairs: int = 3
    events: ct.Events | None = None
    worker: object = None
    round_index: int = 0
    allowed_literals: set = field(default_factory=set)     # tool-schema vocabulary, never a task's answer
    scope: str = ""
    fixture: object = None      # () -> a surface over a synthetic or train-only world, for package tests
    reserved_names: set = field(default_factory=set)       # the host's base tool names: not a package tool's
    image: str | None = None    # the worker image K froze, recorded with every package built here

    def _revise(self, revise, prompt: str, patch: dict, check_output: str):
        """R's revision, shown the parent package the patch applies to when its revise step takes one."""
        import inspect
        try:
            takes_parent = "parent" in inspect.signature(revise).parameters
        except (TypeError, ValueError):
            takes_parent = False
        if not takes_parent:
            return revise(prompt, patch, check_output)
        run, tests = hp.files_of(self.objects, self.parent_ref)
        return revise(prompt, patch, check_output, parent={**run, **tests})

    # ------------------------------------------------------------------ restart (EX6-12)
    def pending(self) -> dict | None:
        """The draft this scope started and never finished: its proposal, the last recorded revision and patch.
        A restart resumes it instead of asking R for a new proposal, so the revision count is not reset."""
        if self.events is None:
            return None
        started, finished, checks = {}, set(), {}
        for e in self.events.all():
            if e.get("scope") != self.scope:
                continue
            if e.get("event") == "draft_started":
                started[e["draft_id"]] = e
            elif e.get("event") == "draft_finished":
                finished.add(e["draft_id"])
            elif e.get("event") == "draft_check" and e.get("draft_id"):
                checks.setdefault(e["draft_id"], []).append(e)
        open_ids = [d for d in started if d not in finished]
        if not open_ids:
            return None
        draft_id = open_ids[-1]
        return {"draft_id": draft_id, "proposal_ref": started[draft_id]["proposal_ref"],
                "checks": checks.get(draft_id, [])}

    def check(self, files: dict) -> tuple[list[str], str | None]:
        """-> (errors, package ref). Raises WorkerUnavailable when the checks need a worker there is not."""
        try:
            manifest = hp.check_shape(files, self.limits)
        except hp.PackageError as e:
            return [str(e)], None
        except (TypeError, ValueError, AttributeError, KeyError) as e:   # a malformed shape is a check error
            return [f"malformed package: {type(e).__name__}: {e}"[:300]], None
        taken = sorted({t["name"] for t in manifest["tools"]} & set(self.reserved_names))
        if taken:
            return [f"package tools {taken} take the names of the host's base tools; choose other names"], None
        hits = hp.literal_hits(files, self.literals, allowed=self.allowed_literals)
        if hits:
            return [hp.describe_hits(hits)], None
        if hp.needs_worker_for_checks(files, manifest):
            if self.worker is None:
                raise WorkerUnavailable("the draft holds code and no isolated worker is available for its checks")
            surface = self.fixture() if self.fixture is not None else None
            try:
                errors = check_package(self.worker, files, manifest, fixture=surface)
            except WorkerCrashed as e:
                raise WorkerUnavailable(f"the isolated worker failed during the check: {e}") from None
            finally:
                closer = getattr(surface, "close", None)
                if callable(closer):
                    closer()
            if errors:
                return errors, None
        return [], hp.build_package(self.objects, files, self.limits, image=self.image)

    def check_texts(self, proposal: ct.ExperimentProposal) -> list[str]:
        """The literal check on the text a proposal gives W outside the package (worker.system_prompt,
        worker.finish): it reaches the actor as directly as a package file, so a train literal in it is the same
        structure error. A prompt change has no draft to revise; the caller makes the round invalid."""
        errors = []
        for change in proposal.changes:
            text = text_of(change, self.objects)
            hits = hp.literal_hits({"text": text}, self.literals, allowed=self.allowed_literals) if text else []
            if hits:
                errors.append(f"{change['target']}: {hp.describe_hits(hits, what='the text')}")
        return errors

    def resolve(self, proposal: ct.ExperimentProposal, *, revise, prompt: str, resume: dict | None = None) -> DraftOutcome:
        """Replace each package patch in the proposal with a checked package, revising within the bound. With
        `resume`, continue a draft a restart interrupted: its recorded revisions count, and its last patch is
        where the next revision starts."""
        from ..ledger import new_id
        if resume is None:
            self.draft_id = new_id("d")
            if self.events is not None:
                self.events.append("draft_started", scope=self.scope, draft_id=self.draft_id,
                                   round=self.round_index, proposal_ref=self.objects.put(proposal))
        else:
            self.draft_id = resume["draft_id"]
        outcome = self._resolve(proposal, revise=revise, prompt=prompt, resume=resume)
        if self.events is not None and outcome.status != "infrastructure_failed":
            # An infrastructure failure leaves the draft open: the activity resumes it once the worker is back.
            self.events.append("draft_finished", scope=self.scope, draft_id=self.draft_id, status=outcome.status,
                               package=outcome.package_ref)
        return outcome

    def _resolve(self, proposal, *, revise, prompt: str, resume: dict | None) -> DraftOutcome:
        changes, drafts = [], []
        for change in proposal.changes:
            patch = patch_of(change, self.objects)
            if change["target"] != "worker.harness" or patch is None:
                changes.append(change)
                continue
            package_ref = None
            first = 0
            if resume and resume["checks"]:
                last = resume["checks"][-1]
                drafts = [{"revision": c["revision"], "draft_ref": c.get("draft"), "package_ref": c.get("package"),
                           "ok": c.get("ok"), "errors": c.get("errors") or []} for c in resume["checks"]]
                if last.get("ok"):
                    package_ref, first = last.get("package"), None
                else:
                    patch = self.objects.get(last["patch"])["patch"] if last.get("patch") else patch
                    first = last["revision"] + 1
                    if first <= self.max_repairs and not last.get("infrastructure"):
                        revised = self._revise(revise, prompt, patch,
                                               clip("\n".join(last.get("errors") or []), MAX_CHECK_OUTPUT))
                        patch = revised if isinstance(revised, dict) and revised else {"invalid": "no patch returned"}
                    elif last.get("infrastructure"):
                        first = last["revision"]          # the worker came back: check the same draft again
            for revision in (range(first, self.max_repairs + 1) if first is not None else ()):
                files, draft_ref = None, None
                try:
                    files = hp.apply_patch(self.objects, self.parent_ref, patch)
                    # Every draft is stored, passing or not, so a rejected revision can be audited.
                    draft_ref = self.objects.put_json("bundle", {"files": {
                        p: self.objects.put_json("file", {"text": files[p]}) for p in sorted(files)}})
                    errors, package_ref = self.check(files)
                except hp.PackageError as e:
                    errors, package_ref = [str(e)], None
                except (TypeError, ValueError, AttributeError, KeyError) as e:
                    errors, package_ref = [f"malformed patch: {type(e).__name__}: {e}"[:300]], None
                except WorkerUnavailable as e:
                    self._record(revision, draft_ref, None, [str(e)], infrastructure=True, patch=patch)
                    drafts.append({"revision": revision, "draft_ref": draft_ref, "package_ref": None, "ok": False,
                                   "errors": [str(e)]})
                    return DraftOutcome("infrastructure_failed", drafts=drafts, reason=str(e))
                self._record(revision, draft_ref, package_ref, errors, patch=patch)
                drafts.append({"revision": revision, "draft_ref": draft_ref, "package_ref": package_ref,
                               "ok": not errors, "errors": errors})
                if not errors:
                    break
                if revision < self.max_repairs:
                    revised = self._revise(revise, prompt, patch, clip("\n".join(errors), MAX_CHECK_OUTPUT))
                    # An empty or unreadable revision is a failed revision, never "apply nothing": applying
                    # nothing would freeze the parent itself as a candidate and pay a gate for a no-op.
                    patch = revised if isinstance(revised, dict) and revised else {"invalid": "no patch returned"}
            if package_ref is None:
                return DraftOutcome("invalid", drafts=drafts,
                                    reason=f"the draft still failed its check after {self.max_repairs} revisions: "
                                           f"{drafts[-1]['errors'][:2]}")
            changes.append({"target": "worker.harness", "kind": "workflow",
                            "change": {"builtin": "harness_package.v1", "package_ref": package_ref}})
        frozen = ct.ExperimentProposal(owner=proposal.owner, hypothesis=proposal.hypothesis,
                                       requirements=proposal.requirements, evidence_refs=proposal.evidence_refs,
                                       changes=tuple(changes), training=proposal.training,
                                       expected=proposal.expected, abandon_if=proposal.abandon_if,
                                       notes=proposal.notes, origin=proposal.origin)
        return DraftOutcome("ready", proposal=frozen,
                            package_ref=next((d["package_ref"] for d in reversed(drafts) if d["ok"]), None),
                            drafts=drafts)

    def _record(self, revision: int, draft_ref, package_ref, errors, *, infrastructure: bool = False,
                patch=None) -> None:
        if self.events is not None:
            self.events.append("draft_check", scope=self.scope, draft_id=getattr(self, "draft_id", None),
                               round=self.round_index, revision=revision, draft=draft_ref, package=package_ref,
                               patch=self.objects.put_json("change", {"patch": patch}) if patch is not None
                               else None, ok=not errors, infrastructure=infrastructure,
                               errors=[e[:300] for e in errors])
