"""One round of the improver (docs/design/IMPROVER.md §3; DESIGN §6.1).

    measure -> evidence -> diagnosed -> allocated -> implemented -> verified -> screened -> gated
            -> installed | rejected -> credited

Each step's output is kept in rounds/r<i>/round.json under the step's name and reused when the round runs again, so an
interrupted round pays only for what it had not done. A round is bound to the release it started on: if another
install moved the line meanwhile, the round is refused rather than continued on a W it did not diagnose.

Money: the round spends from its own account `<scope>:round-<i>` (round_share of the root's dollars), one child per
step kind (measure, diagnose, session, screen, gate, policy). Before the first screen, the screens and the gate are
priced from the current W's costs on record; what cannot be priced, or does not fit, does not start.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..contracts import BudgetError, MemorySidecarError, short, write_json
from ..evaluate.gate import refill_attempts
from ..harness.contract import EVENTS, RETURNS
from ..harness.kinds import MEMORY_PATH
from ..harness.literals import forbidden as forbidden_of, literal_index
from ..harness.package import Limits, PackageError, build, load as load_package
from ..runtime.worker import WorkerUnavailable
from . import diagnose as dg
from .allocate import Estimates, allocate, stalled
from .archive import Archive
from .components import components
from .credit import credit as credit_of, footprints, removal_candidates, summary
from .declaration import Declaration, read_declaration
from .evidence import Evidence, dev_histogram
from .ledger import Ledger
from .policy import read_playbook, update_playbook
from .screen import (ScreenResult, condition, cost_per_attempt, gate as run_gate, gate_attempts, passing_sample,
                     pick_best, price, screen)
from .session import CodingSession, WorkTree
from .settings import CODE_KINDS, Settings
from .verify import Verdict, VerifyContext, run_ladder
from .verify.unit import worker_files

PROMPTS = dg.PROMPTS
STEPS = ("measure", "evidence", "diagnosed", "allocated", "implemented", "verified", "screened", "gated", "credited",
         "playbook")


@dataclass
class RoundResult:
    round_id: str
    status: str = "running"             # done | stopped
    causes: int = 0
    assignments: int = 0
    candidates: int = 0
    ladder: dict = field(default_factory=dict)       # rung -> candidates that passed it
    screened: str | None = None         # the candidate that paid the gate
    gate: dict | None = None
    installed: str | None = None
    carried: str | None = None          # the package carried to the next round instead of a gate (IMPROVER §9.2)
    gated: bool = False                 # whether this round paid a gate
    stop: str | None = None
    spend: dict = field(default_factory=dict)


PRICE_MARGIN = 0.15                     # over the means on record: candidates cost what they cost


class Stop(Exception):
    """The round ends here, on record (nothing to fix, no candidate, money)."""


class Round:
    def __init__(self, activity, index: int, *, run_arm, out=print):
        self.a, self.i, self.run_arm, self.out = activity, int(index), run_arm, out
        self.s = Settings.from_core(activity.k)
        self.round_id = f"{activity.scope_id}/r{self.i}"
        self.dir = Path(activity.root) / "rounds" / f"r{self.i}"
        self.state_path = self.dir / "round.json"
        self.ledger = Ledger(activity.root)
        self.archive = Archive(activity.root, activity.objects)
        self.evidence = Evidence(activity)
        self.line = activity.domain
        active = activity.active_release()
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            # the line moved under the round, unless the round's own install moved it (killed before its gate step
            # was saved: the gate's replay is free and the install's replay a no-op)
            own = activity.releases.recover(f"install:{self.round_id}".replace("/", "_"))
            if self.state["base_release"] != active.ref() and self.state.get("status") not in ("done", "stopped") \
                    and "gated" not in self.state["steps"] and getattr(own, "status", None) != "installed":
                raise ValueError(f"round {self.i} started on {short(self.state['base_release'])} and the line is now "
                                 f"{short(active.ref())}: a round is not continued on another W")
        else:
            self.state = {"round_id": self.round_id, "index": self.i, "base_release": active.ref(), "steps": {},
                          "status": "running"}
        self.base = activity.releases.load(self.state["base_release"])
        # the package the round works on: what an earlier round carried (IMPROVER §9.2), while it is the line's base
        # the carry was taken on; else the base's own
        self.carry = self._carry()
        self.package = load_package(activity.objects, self.carry["package_ref"]) if self.carry \
            else activity.package(self.base)
        self.account = self._account()

    # ------------------------------------------------------------------ the carry (IMPROVER §9.2)
    @property
    def carry_path(self) -> Path:
        return Path(self.a.root) / "rounds" / "carry.json"

    def _carry(self) -> dict | None:
        if not self.carry_path.exists():
            return None
        carry = json.loads(self.carry_path.read_text(encoding="utf-8"))
        if not carry.get("package_ref") or carry.get("base") != self.base.ref():
            return None                     # the line moved: a carry taken on another base is not continued
        if self.state.get("carry") is not None:         # the round's own record of what it started with
            return self.state["carry"] or None
        self.state["carry"] = carry
        return carry

    def _write_carry(self, carry: dict | None) -> None:
        write_json(self.carry_path, carry or {"package_ref": None, "base": self.base.ref(), "dirs": []})

    @property
    def work_ref(self) -> str:
        """The package ref the round reads train evidence of: the carried package's, else the base's."""
        return self.package.ref()

    def gate_round(self) -> bool:
        return (self.i + 1) % max(int(self.s.gate_every), 1) == 0

    # ------------------------------------------------------------------ state, money, models
    def _save(self) -> None:
        write_json(self.state_path, self.state)

    def step(self, name: str, fn):
        if name in self.state["steps"]:
            return self.state["steps"][name]
        value = fn()
        self.state["steps"][name] = value
        self._save()
        return value

    def _account(self):
        root = self.a.root_account()
        usd = (root.remaining().get("limits") or {}).get("usd")
        limits = {"usd": round(float(usd) * self.s.round_share, 6)} if usd is not None else {}
        return root.child(f"{self.a.scope_id}:round-{self.i}", limits)

    def child(self, kind: str):
        return self.account.child(f"{self.a.scope_id}:round-{self.i}:{kind}", {})

    def researcher(self, kind: str):
        return self.a.research_provider(f"round-{self.i}:{kind}", account=self.child(kind))

    def spend(self) -> dict:
        state = self.account.remaining()
        return {"used": state.get("used"), "limits": state.get("limits")}

    def stop(self, why: str) -> RoundResult:
        self.state.update(status="stopped", stop=why)
        self._save()
        self.ledger.row("stopped", round_id=self.round_id, line=self.line, reason=why)
        self.out(f"round      {self.round_id} stopped: {why}")
        return self.result()

    def result(self) -> RoundResult:
        steps = self.state["steps"]
        verified = steps.get("verified") or {}
        gated = steps.get("gated") or None
        return RoundResult(round_id=self.round_id, status=self.state.get("status", "running"),
                           causes=len((steps.get("diagnosed") or {}).get("causes") or ()),
                           assignments=len((steps.get("allocated") or {}).get("assignments") or ()),
                           candidates=len((steps.get("implemented") or {}).get("candidates") or ()) +
                           len(verified.get("fixed") or ()),
                           ladder=verified.get("ladder") or {}, screened=(steps.get("screened") or {}).get("best"),
                           gate=gated, installed=(gated or {}).get("installed"),
                           carried=(steps.get("carried") or {}).get("package_ref"), gated=gated is not None,
                           stop=self.state.get("stop"), spend=self.spend())

    # ------------------------------------------------------------------ the round
    def run(self) -> RoundResult:
        if self.state.get("status") in ("done", "stopped"):
            return self.result()
        try:
            return self._run()
        except Stop as e:
            return self.stop(str(e))
        except BudgetError as e:
            return self.stop(f"budget: {str(e)[:200]}")

    def _run(self) -> RoundResult:
        self.step("measure", self.measure)
        ev = self.step("evidence", self.gather)
        if not ev["failed"]:
            if self.carry and self.gate_round():            # nothing left to fix on the carried package: gate it
                gated = self.step("gated", self.gated)
                if gated.get("installed"):
                    self.step("credited", self.credited)
                self.step("playbook", self.playbook)
                self.state["status"] = "done"
                self._save()
                return self.result()
            raise Stop("nothing_to_fix: the current W passes every train task it was read on")
        if "diagnosed" not in self.state["steps"]:
            self._floor()
        self.step("diagnosed", self.diagnosed)
        self.step("allocated", self.allocated)
        self.step("implemented", self.implemented)
        verified = self.step("verified", self.verified)
        screened = self.step("screened", self.screened) if verified["passed"] else {"best": None}
        # the gate's cadence (IMPROVER §9.2): a carry round keeps the best screened candidate for the next round; a
        # gate round pays the gate for it — or, with nothing new, for the package carried so far
        if screened.get("best") and not self.gate_round():
            self.step("carried", self.carried)
        elif screened.get("best") or (self.gate_round() and self.carry):
            gated = self.step("gated", self.gated)
            if gated.get("installed"):
                self.step("credited", self.credited)
        elif not verified["passed"]:
            raise Stop("no_candidate: no candidate passed its free rungs")
        else:
            self.ledger.row("rejected", round_id=self.round_id, line=self.line, reason="no eligible screen")
        self.step("playbook", self.playbook)
        self.state["status"] = "done"
        self._save()
        return self.result()

    def _need(self, usd: float) -> tuple[float, float | None]:
        """(what `usd` at the means on record needs with PRICE_MARGIN for the spread and two calls' reservation bound,
        what the round has left; None when it has no dollar limit). Every call reserves the bound, so a round can
        never spend its last bound, and one call in flight beside the last keeps an arm from stalling."""
        left = (self.account.remaining().get("effective_remaining") or {}).get("usd")
        bound = float(self.a.pricing().bound().get("usd") or 0.0)
        return round(usd * (1 + PRICE_MARGIN) + 2 * bound, 6), (None if left is None else float(left))

    def _afford(self, usd: float, what: str) -> None:
        """Stop the round on record unless `usd` fits what the round has left (_need)."""
        need, left = self._need(usd)
        if left is not None and need > left + 1e-9:
            raise Stop(f"budget: {what} would need up to ${need:.2f} (${usd:.2f} at the means on record, "
                       f"+{PRICE_MARGIN:.0%}, and two calls' bound) and the round has ${left:.2f}")

    def _research_mean(self) -> float | None:
        """The mean cost of one research call on record in this activity (the memory's bridge, earlier rounds'
        diagnosis, sessions and playbooks), or None before any."""
        book = self.a.budgets._read()
        costs = [float((r.get("measured") or {}).get("usd") or 0.0) for r in (book.get("reservations") or {}).values()
                 if r.get("status") == "settled" and str(r.get("scope_id") or "").startswith(self.a.scope_id + ":")
                 and str(r.get("scope_id")).rpartition(":")[2] in ("memory", "diagnose", "session", "policy")]
        return sum(costs) / len(costs) if costs else None

    def _floor(self) -> None:
        """Before the round pays for diagnosis and R's session, the least the round must pay to have an outcome fits:
        the gate, one small screen (the passing sample and thin_screen_tasks failing tasks), and the research it is
        about to pay — a call per case to diagnose and per turn of the session, at the research calls' mean on record
        (left out before any is on record)."""
        usd, why = price(self.a, self.base.package_ref,
                         screen_attempts=self.s.screen_passing_sample + self.s.thin_screen_tasks)
        if usd is None:
            raise Stop(f"unpriced: {why}")
        mean = self._research_mean()
        if mean is not None:
            cases = min(len(self.state["steps"]["evidence"]["failed"]), self.s.diagnose_max_cases)
            usd += mean * (cases + self.s.session_max_turns)
        self._afford(usd, "the gate, the least screen and the research")

    def split(self) -> tuple[tuple, tuple]:
        """(the train tasks R studies, the train tasks held out from it), fixed for the activity: whole families,
        ranked by a hash of K's order seed and the family, are held out while they fit in holdout_share of the train
        tasks (a family that would overshoot it is skipped; one task at least stays studied).
        V3 screens on the held-out part only, so a screen reads whether a candidate fixes tasks R never saw."""
        if getattr(self, "_split", None) is None:
            import hashlib
            train = list(self.a.splits.ids("train"))
            want = min(int(round(len(train) * float(self.s.holdout_share))), max(len(train) - 1, 0))
            families: dict = {}
            for t in train:
                families.setdefault(self.a.splits.family_of(t), []).append(t)
            seed = int(self.a.k["gate"]["order_seed"])
            held: list = []
            for fam in sorted(families, key=lambda f: hashlib.sha256(f"holdout:{seed}:{f}".encode()).hexdigest()):
                if len(held) + len(families[fam]) <= want:      # a family that would overshoot the share is skipped
                    held += families[fam]
            self._split = (tuple(t for t in train if t not in set(held)), tuple(t for t in train if t in set(held)))
        return self._split

    def _recorded(self, candidate: str, role: str) -> int:
        """The valid attempts on record under a run of this name and role."""
        runs = self.a.results.runs()
        return sum(1 for r in self.a.results.all() if r.validity == "valid" and runs.get(r.run) is not None
                   and runs[r.run].candidate == candidate and runs[r.run].role == role)

    # ------------------------------------------------------------------ measure, evidence
    def _measure(self, release, package_ref: str, kind: str, package=None) -> dict:
        missing = self.evidence.missing(package_ref)
        if missing:
            def run(arm, chosen, retry=0, supersedes=None):
                # the run is this round's: an attempt an earlier round's budget refused is read again, not handed back
                return self.run_arm(self.a, release=release, split="train",
                                    candidate=f"train:{short(release.ref())}:r{self.i}", role="measure",
                                    pairs=chosen, account=self.child(kind), arm=arm, retry=retry,
                                    supersedes=supersedes, notes="the W's train reading", package=package)
            rows = refill_attempts(run("subject", [(t, 0) for t in missing]), run, "subject",
                                   int(self.a.k["gate"]["max_infrastructure_retries"]))
            if any(r.failure_class == "budget_exhausted" for r in rows):
                raise Stop("budget: the budget refused a train reading")
        reading = self.evidence.reading(package_ref)
        return {"package": package_ref, "measured": len(missing), "tasks": len(reading),
                "passed": sum(1 for r in reading.values() if r.success)}

    def measure(self) -> dict:
        got = self._measure(self.base, self.work_ref, "measure", package=self.package if self.carry else None)
        self.ledger.row("measured", round_id=self.round_id, line=self.line, passed=got["passed"], steps=got["tasks"],
                        **({"carried": short(self.work_ref)} if self.carry else {}))
        self.out(f"round      {self.round_id}: W {short(self.base.ref())}"
                 + (f" carrying {short(self.work_ref)}" if self.carry else "")
                 + f" passes {got['passed']}/{got['tasks']} train tasks ({got['measured']} measured now)")
        return got

    def gather(self) -> dict:
        failed = self.evidence.failed(self.work_ref)
        order = {t: i for i, t in enumerate(self.a.splits.ids("train"))}
        failed.sort(key=lambda e: (self.ledger.last_round_of(e.task_id), order.get(e.task_id, 0)))
        chosen = failed[:self.s.diagnose_max_cases]
        successes = self.evidence.best_successes(self.work_ref, self.s.diagnose_successes,
                                                 tasks=self.split()[0])
        return {"failed": [e.attempt_id for e in chosen], "failed_tasks": [e.task_id for e in chosen],
                "not_diagnosed": [e.task_id for e in failed[self.s.diagnose_max_cases:]],
                "successes": [{"task_id": e.task_id, "session": e.session} for e in successes]}

    def _rows(self, attempt_ids) -> list:
        wanted = set(attempt_ids)
        return [r for r in self.a.results.all() if r.attempt_id in wanted]

    # ------------------------------------------------------------------ diagnosis
    def vocabulary(self) -> tuple:
        added = [n for r in self.ledger.rows(stage="vocabulary_added") for n in r.get("names") or ()]
        return tuple(dict.fromkeys([*self.s.causes, *added]))

    def memory_entries(self) -> dict:
        """The memory's stream ledger's entries that passed the promotion counts (MEMORY §8), or {}."""
        path = Path(self.a.root) / "memory" / self.a.domain / "stream.json"
        if not path.exists():
            return {}
        from ..memory.rules import distilled_from
        from ..memory.settings import Settings as MemorySettings
        ms = MemorySettings.from_core(self.a.k)
        entries = json.loads(path.read_text(encoding="utf-8")).get("entries") or {}
        done = {r.get("source") for r in self.ledger.rows(stage="declared")       # earlier rounds' (a resumed
                if r.get("source") and r.get("round_id") != self.round_id}         # step declares it again)
        return {k: e for k, e in entries.items() if e.get("status") == "active" and f"promotion:{k}" not in done
                and int((e.get("stats") or {}).get("fixes") or 0) >= ms.promote_fixes
                and len(distilled_from(e)) >= ms.promote_sources}

    def diagnosed(self) -> dict:
        ev = self.state["steps"]["evidence"]
        researcher = self.researcher("diagnose")
        vocabulary = self.vocabulary()
        cache = dg.NoteCache(self.a.root, self.a.objects)
        cases, cached = [], 0
        for row in self._rows(ev["failed"]):
            ref, case, hit = dg.diagnose_case(self.evidence, self.evidence.of(row), researcher=researcher,
                                              vocabulary=vocabulary, mechanizable=self.s.mechanizable, cache=cache)
            cases.append((ref, case))
            cached += hit
        entries = self.memory_entries()
        entry_causes = dg.classify_entries(list(entries.values()), researcher=researcher, vocabulary=vocabulary,
                                           cache=cache) if entries else {}
        from ..memory.promote import cause_input
        from ..memory.rules import promotable
        from ..memory.settings import Settings as MemorySettings
        promoted = promotable(entries, MemorySettings.from_core(self.a.k),
                              mechanizable=lambda e: entry_causes.get(e.get("memory_id")) in set(self.s.mechanizable))
        memory_inputs: dict[str, list] = {}
        skills = []
        for e, how in promoted:
            if how == "cause":
                memory_inputs.setdefault(entry_causes[e["memory_id"]], []).append(cause_input(e))
            else:
                skills.append(e["memory_id"])
        causes = dg.cluster(cases, round_id=self.round_id, mechanizable=self.s.mechanizable,
                            memory_inputs=memory_inputs)
        added = dg.vocabulary_extension([c for _, c in cases], vocabulary)
        if added:
            self.ledger.row("vocabulary_added", round_id=self.round_id, line=self.line, names=added)
        self.ledger.row("diagnosed", round_id=self.round_id, line=self.line, index=self.i,
                        tasks=sorted({c.task_id for _, c in cases}),
                        causes={c.name: len(c.tasks) for c in causes})
        self.out(f"round      {self.round_id}: {len(cases)} cases ({cached} cached) in {len(causes)} causes: "
                 + ", ".join(f"{c.name} {len(c.tasks)}" for c in causes))
        return {"causes": [self.a.objects.put(c) for c in causes], "cases": [r for r, _ in cases],
                "promotion_skills": skills}

    def causes(self) -> list:
        return [dg.Cause.from_json(self.a.objects.get(r)) for r in self.state["steps"]["diagnosed"]["causes"]]

    # ------------------------------------------------------------------ layering
    def allocated(self) -> dict:
        previous = f"{self.a.scope_id}/r{self.i - 1}"
        no_code = {r.get("cause") for r in self.ledger.rows(stage="no_code_candidate", round_id=previous)}
        estimates = Estimates(self.a.root, self.s)
        causes, study, held = self.causes(), *(set(p) for p in self.split())
        not_assigned, in_sample = [], []
        if held:        # a cause is assigned only where R has the cases V2a and V2b need; one with nothing held out is
            # screened in sample (IMPROVER §9.1: the gate decides)
            need = max(int(self.s.toolcheck_min_pass), int(self.s.replay_min_triggers), 1)
            not_assigned = [{"cause": c.name, "studied": len(study & set(c.tasks)), "held_out": len(held & set(c.tasks))}
                            for c in causes if len(study & set(c.tasks)) < need]
            causes = [c for c in causes if len(study & set(c.tasks)) >= need]
            in_sample = [c.name for c in causes if not (held & set(c.tasks))]
        assignments, notes = allocate(causes, estimates, settings=self.s, line=self.line,
                                      allowed=self.a.k["harness"]["kinds"], no_code=no_code,
                                      stalled=stalled(self.ledger, self.s.stall_rounds), train_available=False)
        self.ledger.row("allocated", round_id=self.round_id, line=self.line,
                        assignments=[{"cause": a.cause, "kind": a.kind, "slots": a.slots,
                                      "exploration": a.exploration} for a in assignments],
                        **({"not_assigned": not_assigned} if not_assigned else {}),
                        **({"in_sample": in_sample} if in_sample else {}))
        for note in notes:
            if note.startswith("l6_unavailable"):
                self.ledger.row("l6_unavailable", round_id=self.round_id, line=self.line, reason=note)
        self.out(f"round      {self.round_id}: assigned " + ", ".join(f"{a.cause}->{a.kind}"
                                                                     + ("*" if a.exploration else "")
                                                                     + (f"x{a.slots}" if a.slots > 1 else "")
                                                                     for a in assignments))
        return {"assignments": [self.a.objects.put(a) for a in assignments], "notes": notes, "in_sample": in_sample}

    def assignments(self) -> list:
        from .allocate import Assignment
        return [Assignment.from_json(self.a.objects.get(r)) for r in self.state["steps"]["allocated"]["assignments"]]

    # ------------------------------------------------------------------ verification
    def vctx(self) -> VerifyContext:
        train = tuple(self.a.splits.ids("train"))
        reading = self.evidence.reading(self.work_ref)
        rows = [{"task_id": t, "assertions": [dict(x) for x in self.a.suite.grading(t).assertions],
                 "state": self.a.suite.grading(t).initial_state, "prompt": self.a.suite.tasks[t].trigger}
                for t in train]
        study = self.split()[0]
        failed = [t for t in study if t in reading and not reading[t].success]
        passed = [t for t in study if t in reading and reading[t].success]
        # V2c reads reach on lexical_embed's scale, the one reach_min_score is calibrated on (bge-small puts unrelated
        # texts at 0.5 and above: on it 0.2 would pass every description)
        embed = None
        return VerifyContext(parent=self.package, seed=self.a.package(self.a.seed_release()), core=self.a.k,
                             objects=self.a.objects, worker=self.a.worker(), suite=self.a.suite,
                             evidence=self.evidence, forbidden=forbidden_of(literal_index(rows)), train_tasks=train,
                             train_texts={t: self.a.suite.tasks[t].trigger for t in train}, embed=embed,
                             settings=self.s, fixture_task=(failed or passed or list(train))[0],
                             failed_sessions={t: reading[t].evidence or reading[t].attempt_id for t in failed},
                             passed_sessions={t: reading[t].evidence or reading[t].attempt_id
                                              for t in passed[:self.s.screen_passing_sample]},
                             study_tasks=study,
                             cause_ids=tuple(a.cause_id for a in self.assignments())
                             if "allocated" in self.state["steps"] else ())

    def with_memory(self, files: dict) -> dict:
        """A candidate's files with the parent's host-only memory snapshot put back (R's tree never holds it)."""
        out = {p: t for p, t in files.items() if p != MEMORY_PATH}
        if MEMORY_PATH in self.package.files:
            out[MEMORY_PATH] = self.package.files[MEMORY_PATH]
        return out

    # ------------------------------------------------------------------ the session
    def inputs(self, assignments) -> tuple[dict, set]:
        steps = self.state["steps"]
        causes = {c.cause_id: c for c in self.causes()}
        study, held = (set(p) for p in self.split())
        from ..evaluate.attempt import task_hash
        held_hashes = {task_hash(self.a.suite.tasks[t]) for t in held}
        readable = set()
        cause_rows = []
        for a in assignments:
            c = causes[a.cause_id]
            rows = []
            for ref in c.cases:
                case = dg.Case.from_json(self.a.objects.get(ref))
                if case.task_id not in study:
                    continue                                # held out: R never reads it
                row = next(iter(self._rows([case.attempt_id])), None)
                session = (row.evidence or row.attempt_id) if row is not None else ""
                readable.add(session)
                rows.append({"task_id": case.task_id, "session": session, "note": case.explanation,
                             "evidence_refs": list(case.evidence_refs), "failed_assertion_kinds":
                             list(case.assertion_kinds), "tool_errors": list(case.tool_errors),
                             "termination": case.termination})
            cause_rows.append({"cause_id": c.cause_id, "name": c.name, "mechanizable": c.mechanizable,
                               "definition": dg.DEFINITIONS.get(c.name, ""),
                               "tasks": [t for t in c.tasks if t in study],
                               "held_out_tasks": sum(1 for t in c.tasks if t in held), "cases": rows,
                               # an entry distilled from a held-out task's trajectory is that task's: not shown
                               "memory_entries": [e for e in c.memory_entries
                                                  if not set(e.get("distilled_from") or ()) & held_hashes],
                               "earlier": self.archive.for_prompt(self.line, c.name, hide=held)})
        successes = steps["evidence"]["successes"]
        readable |= {s["session"] for s in successes}
        episode = self.a.suite.episode(self.a.splits.ids("train")[0])
        try:
            base_tools = [{"name": s.name, "description": s.description, "schema": s.schema} for s in episode.specs()]
        finally:
            episode.close()
        sites = "\n".join(f"    {site}: event {list(EVENTS[site])} -> may answer {list(RETURNS[site])}"
                          for site in EVENTS)
        api = (PROMPTS / "package_api.md").read_text(encoding="utf-8").replace("{sites}", sites)
        files = {"assignments.json": [{"cause_id": a.cause_id, "cause": a.cause, "kind": a.kind, "slots": a.slots,
                                       "exploration": a.exploration} for a in assignments],
                 "causes.json": cause_rows, "successes.json": successes,
                 "ledger.json": self.ledger.projection(line=self.line, hide=held),
                 "scoreboard.json": self.ledger.scoreboard(),
                 "histogram.json": dev_histogram(self.a, self.base.package_ref), "base_tools.json": base_tools}
        out = {name: json.dumps(v, ensure_ascii=False, indent=1, default=str) for name, v in files.items()}
        out["playbook.md"] = read_playbook(self.a.root)
        out["package_api.md"] = api
        return out, readable

    def _declared(self, decl, files: dict, verdict) -> dict:
        self.ledger.row("declared", round_id=self.round_id, line=self.line, dir=decl.dir, cause_id=decl.cause_id,
                        cause=decl.cause_id.rpartition(":")[2], kind=decl.kind, site=decl.site, trigger=decl.trigger,
                        predicted_affected=list(decl.predicted_affected), differs_from=list(decl.differs_from))
        return {"dir": decl.dir, "declaration": self.a.objects.put(decl),
                "files": self.a.objects.put_json("candidate_files", files), "verdict": self.a.objects.put(verdict)}

    def _session(self, session: CodingSession, tree: WorkTree):
        """Start R's session, or go on with the one a provider failure stopped (its messages, counters and candidate
        directories were saved): the turns already paid are not paid again. A provider failure saves the session
        and is raised; the round is resumed when the provider answers again."""
        from ..providers import ProviderError
        saved = self.state.get("session_checkpoint")
        try:
            if saved is None:
                return session.start()
            session.restore(self.a.objects.get(saved["state"]))
            for name, files in self.a.objects.get(saved["files"]).items():
                self._restore_dir(tree, name, files)
            self.out(f"round      {self.round_id}: R's session goes on from turn {session.turns}")
            return session.resume()
        except (ProviderError, KeyboardInterrupt):
            files = {}
            for name in session.dirs:
                got, _ = tree.fetch(name)
                if got is not None:
                    files[name] = got
            self.state["session_checkpoint"] = {"state": self.a.objects.put_json("session_state", session.state()),
                                                "files": self.a.objects.put_json("candidate_dirs", files)}
            self._save()
            raise

    def _restore_dir(self, tree: WorkTree, name: str, files: dict) -> None:
        """A saved candidate directory, exactly: the files it lacks of the package deleted first (the first delete
        seeds it from the package), then its files written, the ones that shrink first, so the directory's size only
        grows toward the saved one. A refused step stops here, the checkpoint kept, rather than go on with a tree that
        is not what R wrote."""
        parent = {p: t for p, t in self.package.files.items() if p != MEMORY_PATH}
        steps = [("delete", p, None) for p in sorted(set(parent) - set(files))]
        delta = lambda p: len(files[p].encode("utf-8")) - len(parent.get(p, "").encode("utf-8"))   # noqa: E731
        steps += [("write", p, files[p]) for p in sorted(files, key=lambda p: (delta(p), p))
                  if files[p] != parent.get(p)]                                  # the seed holds the others
        if not steps:
            steps = [("write", p, files[p]) for p in sorted(files)[:1]]           # an unchanged dir: seed it
        for action, path, text in steps:
            value, bad = tree.do(action, path=f"candidates/{name}/{path}", **({"text": text} if text is not None
                                                                                 else {}))
            if bad:
                raise RuntimeError(f"R's saved session cannot be restored: candidates/{name}/{path}: {value}")

    def _unused(self, assignments, out: list) -> None:
        """An assigned (cause, kind) R declared nothing for: on the ledger, and an observation of no fix for its
        estimate, so the exploration does not keep landing where R does not go."""
        decls = [Declaration.from_json(self.a.objects.get(i["declaration"])) for i in out]
        if len(decls) >= min(self.s.k, sum(a.slots for a in assignments)):
            return                  # R declared as many candidates as it could: an empty slot is the cap's
        declared = {(d.cause_id, d.kind) for d in decls}
        estimates = Estimates(self.a.root, self.s)
        for a in assignments:
            if (a.cause_id, a.kind) not in declared:
                self.ledger.row("unused", round_id=self.round_id, line=self.line, cause=a.cause, kind=a.kind)
                estimates.update(self.line, a.cause, a.kind, fixes=0.0,
                                 key=f"{self.round_id}:unused:{a.cause}:{a.kind}")

    def implemented(self) -> dict:
        assignments = self.assignments()
        if not assignments:
            return {"candidates": [], "session": None}
        saved = self.state.get("session_result")
        if saved is not None:       # the session ran before the step was cut short: its candidates, not paid again
            vctx = self.vctx()
            out = []
            for item in saved["candidates"]:
                decl = Declaration.from_json(self.a.objects.get(item["declaration"]))
                files = self.a.objects.get(item["files"])
                out.append(self._declared(decl, files, run_ladder(files, decl, vctx)))
            self.out(f"round      {self.round_id}: the session's {len(out)} candidates, as it left them")
            self._unused(assignments, out)
            return {"candidates": out, "session": saved["session"]}
        inputs, readable = self.inputs(assignments)
        kinds = sorted({a.kind for a in assignments})
        k = min(self.s.k, sum(a.slots for a in assignments))
        system = (PROMPTS / "propose.md").read_text(encoding="utf-8")
        for name, value in {"k": k, "max_turns": self.s.session_max_turns, "max_edits": self.s.session_max_edits,
                            "max_read": self.s.session_max_read_bytes,
                            "max_read_total": self.s.session_max_read_total,
                            "max_tokens": int(self.a.k["research_model"]["max_tokens"])}.items():
            system = system.replace("{" + name + "}", str(value))
        worker = self.a.worker()
        if worker is None:
            raise WorkerUnavailable("R's coding session needs the isolated worker and none is available")
        wsession = worker.start()
        vctx = self.vctx()
        tree = WorkTree(wsession)
        session = None
        try:
            wsession.load({})
            tree.init({p: t for p, t in self.package.files.items() if p != MEMORY_PATH}, inputs,
                      max_bytes=Limits.from_core(self.a.k).max_package_bytes)

            def check(dir_name: str, raw: dict) -> str:
                files, error = tree.fetch(dir_name)
                if files is None:
                    return f"candidates/{dir_name} cannot be read back: {error}"
                decl, why = read_declaration({**raw, "dir": dir_name}, dirs=[dir_name], kinds_allowed=kinds)
                if why:
                    return f"the declaration is not readable: {why}"
                return run_ladder(self.with_memory(files), decl, vctx).text()

            def tests(dir_name: str) -> str:
                files, error = tree.fetch(dir_name)
                if files is None:
                    return f"candidates/{dir_name} cannot be read back: {error}"
                from .verify.unit import unit_check
                return unit_check(self.with_memory(files), Declaration(dir=dir_name, kind="agent"), vctx)["detail"] \
                    or "the package tests, the probe and the smoke pass"

            session = CodingSession(researcher=self.researcher("session"), work=tree, trace=self.evidence.trace,
                                    check=check, tests=tests, settings=self.s, k=k, system=system, inputs={
                                        "round": self.round_id, "assignments": json.loads(inputs["assignments.json"]),
                                        "inputs": sorted(f"inputs/{n}" for n in inputs)},
                                    readable=readable, kinds=kinds)
            result = self._session(session, tree)
            fetched = []
            for decl in result.declarations:
                files, error = tree.fetch(decl.dir)
                if files is not None:
                    fetched.append((decl, self.with_memory(files)))
            # on record before anything else can fail: a resumed step does not pay for the session again
            self.state["session_result"] = {"session": str(self.dir / "session.json"), "candidates": [
                {"declaration": self.a.objects.put(d), "files": self.a.objects.put_json("candidate_files", f)}
                for d, f in fetched]}
            self.state.pop("session_checkpoint", None)
            self._save()
            out = []
            for decl, files in fetched:
                verdict = run_ladder(files, decl, vctx)
                if not verdict.passed() and not session.ended:
                    session.revise(f"candidates/{decl.dir} failed its check:\n{verdict.text()}\nEdit its files and "
                                   f"call finish again with the same declarations.")
                    again, _ = tree.fetch(decl.dir)
                    if again is not None:
                        files = self.with_memory(again)
                        verdict = run_ladder(files, decl, vctx)
                out.append(self._declared(decl, files, verdict))
        finally:
            wsession.close(force=True)
            if session is not None:
                transcript = self.a.objects.put_json("session_transcript", session.transcript())
                write_json(self.dir / "session.json", {**session.record(), "transcript": transcript})
        self.out(f"round      {self.round_id}: the session declared {len(out)} candidates "
                 f"({session.turns if session else 0} turns, {session.edits if session else 0} edits"
                 + (f"; ended: {session.ended}" if session and session.ended else "") + ")")
        self._unused(assignments, out)
        return {"candidates": out, "session": str(self.dir / "session.json")}

    # ------------------------------------------------------------------ the fixed code's candidates
    def fixed_candidates(self) -> list[tuple[Declaration, dict]]:
        """The library's snapshot not yet on the ladder, the memory's promoted skills, removal candidates — at most
        fixed_per_round, in that order."""
        out: list[tuple[Declaration, dict]] = []
        done = {r.get("source") for r in self.ledger.rows(stage="declared")       # earlier rounds' (a resumed
                if r.get("source") and r.get("round_id") != self.round_id}         # step declares it again)
        for e in reversed(list(self.a.events.read("library_snapshot"))):
            src = f"snapshot:{e.get('snapshot_digest')}"
            if e.get("outcome") == "candidate" and src not in done and e.get("package_ref"):
                snap = load_package(self.a.objects, e["package_ref"])
                from ..memory.snapshot import snapshot_candidate
                files = snapshot_candidate(self.package, snap.memory_text)
                out.append((Declaration(dir="memory-snapshot", cause_id=f"{self.round_id}:memory", kind="memory",
                                        site="snapshot", trigger="every attempt reads the library", fixed=src), files))
                break
        path = Path(self.a.root) / "memory" / self.a.domain / "stream.json"
        entries = json.loads(path.read_text(encoding="utf-8")).get("entries") if path.exists() else {}
        from ..memory.promote import skill_candidate
        for mid in self.state["steps"]["diagnosed"].get("promotion_skills") or ():
            e = (entries or {}).get(mid)
            if e is not None:
                files = skill_candidate(e, self.package)
                name = next(s["name"] for s in json.loads(files["manifest.json"])["skills"]
                            if s["name"].endswith(mid[:8]))
                from ..memory.rules import distilled_from
                from ..evaluate.attempt import task_hash
                ids = {task_hash(self.a.suite.tasks[t]): t for t in self.a.splits.ids("train")}
                out.append((Declaration(dir=f"promoted-{mid[:8]}", cause_id=f"{self.round_id}:memory", kind="skill",
                                        site=name, predicted_affected=tuple(sorted(ids[h] for h in distilled_from(e)
                                                                                   if h in ids)),
                                        trigger="the skill index lists it", fixed=f"promotion:{mid}"), files))
        history = [r.get("history") for r in self.ledger.rows(stage="credited") if r.get("history")]
        for comp, files in removal_candidates(history, self.package, zero_rounds=self.s.removal_zero_credit_rounds):
            src = f"removal:{comp['kind']}:{comp['name']}:{short(self.base.ref())}"
            if src not in done:
                out.append((Declaration(dir=f"remove-{comp['kind']}-{comp['name']}"[:60], cause_id=f"{self.round_id}:"
                                        f"bloat", kind="removal", site=comp["name"], trigger=comp["why"],
                                        fixed=src), files))
        return out[:self.s.fixed_per_round]

    def verified(self) -> dict:
        vctx = None
        passed, fixed, ladder = [], [], {}
        for item in self.state["steps"]["implemented"]["candidates"]:
            verdict = Verdict.from_json(self.a.objects.get(item["verdict"]))
            decl = Declaration.from_json(self.a.objects.get(item["declaration"]))
            files = self.a.objects.get(item["files"])
            dup = self.archive.duplicate(files, before_round=self.i)
            self.archive.put(self.i, decl.dir, files, decl, verdict, line=self.line)
            for rung, v in verdict.steps.items():
                ladder[rung] = ladder.get(rung, 0) + bool(v.get("passed"))
            self.ledger.row("verified", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                            passed=verdict.passed(), outcome="duplicate" if dup else "",
                            reason=verdict.text()[:500])
            if verdict.passed() and not dup:
                passed.append(item)
        for decl, files in self.fixed_candidates():
            vctx = vctx or self.vctx()
            verdict = run_ladder(files, decl, vctx)
            self.ledger.row("declared", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                            site=decl.site, source=decl.fixed, cause=decl.cause_id.rpartition(":")[2])
            if decl.kind == "removal":
                self.ledger.row("removal_proposed", round_id=self.round_id, line=self.line, dir=decl.dir,
                                site=decl.site, reason=decl.trigger)
            self.archive.put(self.i, decl.dir, files, decl, verdict, line=self.line)
            for rung, v in verdict.steps.items():
                ladder[rung] = ladder.get(rung, 0) + bool(v.get("passed"))
            self.ledger.row("verified", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                            passed=verdict.passed(), reason=verdict.text()[:500])
            item = {"dir": decl.dir, "declaration": self.a.objects.put(decl),
                    "files": self.a.objects.put_json("candidate_files", files), "verdict": self.a.objects.put(verdict)}
            fixed.append(decl.dir)
            if verdict.passed():
                passed.append(item)
        # the code guarantee: a mechanizable cause and no code candidate past its V2
        mech = [c for c in self.causes() if c.mechanizable]
        code = [p for p in passed if Declaration.from_json(self.a.objects.get(p["declaration"])).kind in CODE_KINDS]
        if mech and not code:
            for c in mech:
                self.ledger.row("no_code_candidate", round_id=self.round_id, line=self.line, cause=c.name)
        self.out(f"round      {self.round_id}: {len(passed)} candidates passed their free rungs ({ladder})")
        return {"passed": passed, "fixed": fixed, "ladder": ladder}

    # ------------------------------------------------------------------ V3 and V4
    def screened(self) -> dict:
        reading = self.evidence.reading(self.work_ref)
        held = self.split()[1]
        in_sample_causes = set((self.state["steps"].get("allocated") or {}).get("in_sample") or ())
        # V3 reads the held-out train tasks: a candidate's failing set is the held-out tasks of its cause the current W
        # still fails (the fixed code's: every held-out task it fails), its sample the held-out tasks it passes. With
        # nothing held out, the claimed and the cause's tasks and a sample of all train tasks (as before).
        order = list(held) if held else self.a.splits.ids("train")
        sample = passing_sample(reading, order=order, size=self.s.screen_passing_sample,
                                seed=int(self.a.k["gate"]["order_seed"]), round_index=self.i)
        failing_now = {t for t, r in reading.items() if not r.success}
        causes = {c.cause_id: c for c in self.causes()}
        plans = []
        for item in self.state["steps"]["verified"]["passed"]:
            decl = Declaration.from_json(self.a.objects.get(item["declaration"]))
            cause = causes.get(decl.cause_id)
            # a cause with nothing held out is screened on the studied tasks it claims, in sample (IMPROVER §9.1)
            in_sample = bool(held) and cause is not None and cause.name in in_sample_causes and not decl.fixed
            claimed = set(cause.tasks if cause is not None else ()) \
                | (set(decl.predicted_affected) if (not held or in_sample) else set())
            if decl.fixed:                            # the fixed code's candidates claim every failing task
                claimed |= failing_now
            pool = list(self.split()[0]) if in_sample else order
            failing = [t for t in pool if t in claimed and t in failing_now]
            plans.append((item, decl, failing, in_sample))
        # The screens are paid in this order — R's candidates as declared, then the fixed code's, the library's
        # snapshot last (it claims every failing task) — and each is taken while the screens taken so far, it and the
        # gate fit what the round has left (each screen's attempts less those on record: a resumed step re-reads them
        # for free). The credit step's train reading is not priced here: it is paid only after an install, and when
        # the budget refuses it the credit waits for the next round's measure.
        plans.sort(key=lambda p: (bool(p[1].fixed), p[1].kind == "memory"))
        to_pay = lambda chosen: sum(max(len(set(f) | set(sample)) - self._recorded(f"r{self.i}:{d.dir}", "screen"),
                                        0) for _, d, f, _s in chosen)                    # noqa: E731
        chosen, dropped = [], []
        for plan in [p for p in plans if p[2]]:
            usd, why = price(self.a, self.work_ref, screen_attempts=to_pay([*chosen, plan]), dev_ref=self.base.package_ref)
            if usd is None:
                raise Stop(f"unpriced: {why}")
            need, left = self._need(usd)
            (chosen if left is None or need <= left + 1e-9 else dropped).append(plan)
        if dropped and not chosen:
            usd, _ = price(self.a, self.work_ref, screen_attempts=to_pay(dropped[:1]), dev_ref=self.base.package_ref)
            self._afford(usd, "the screens and the gate")
        for _item, decl, failing, _s in dropped:
            self.ledger.row("screened", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                            outcome="not_run", reason="budget: its screen and the gate do not fit what the round has "
                                                      "left beside the screens before it")
        plans = [p for p in plans if p not in dropped]
        usd, _ = price(self.a, self.work_ref, screen_attempts=to_pay([p for p in plans if p[2]]),
                       dev_ref=self.base.package_ref)
        results, rows = [], []
        for item, decl, failing, in_sample in plans:
            files = self.a.objects.get(item["files"])
            try:
                package = build(self.a.objects, files, limits=Limits.from_core(self.a.k),
                                seed=self.a.package(self.a.seed_release()),
                                parent=None if decl.kind == "memory" else self.package)
            except PackageError as e:
                self.ledger.row("screened", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                                outcome="invalid", reason=f"build: {e}"[:300])
                continue
            res = screen(self.a, run_arm=self.run_arm, name=f"r{self.i}:{decl.dir}", package=package,
                         base_release=self.base, failing=failing, sample=sample, reading=reading,
                         evidence=self.evidence, conditions=condition(decl, components(self.package, files)),
                         account=self.child("screen"), retries=int(self.a.k["gate"]["max_infrastructure_retries"]),
                         thin_tasks=self.s.thin_screen_tasks, in_sample=in_sample)
            results.append(res)
            rows.append({"dir": decl.dir, "screen": self.a.objects.put(res), "package": package.ref()})
            self.archive.put(self.i, decl.dir, files, decl, Verdict.from_json(self.a.objects.get(item["verdict"])),
                             screen=res, package_ref=package.ref(), line=self.line)
            self.ledger.row("screened", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                            cause=decl.cause_id.rpartition(":")[2], predicted_affected=list(decl.predicted_affected),
                            fixed=list(res.fixed), regressed=list(res.regressed), net_flips=res.net_flips,
                            fixes=len(res.fixed), outcome=res.outcome, reason=res.reason,
                            **({"in_sample": True} if res.in_sample else {}))
            cause = causes.get(decl.cause_id)
            if res.outcome == "screened" and cause is not None and not decl.fixed:
                Estimates(self.a.root, self.s).update(self.line, cause.name, decl.kind,
                                                      fixes=res.net_flips / max(len(res.failing), 1),
                                                      cost=(res.cost or 0.0) * len(res.tasks),
                                                      key=f"{self.round_id}:{decl.dir}")
            if res.outcome == "budget_exhausted":
                raise Stop("budget: the budget refused a screen attempt")
        best = pick_best(results, self.s)
        chosen = next((r for r, res in zip(rows, results) if res is best), None) if best is not None else None
        self.out(f"round      {self.round_id}: screened " + ", ".join(
            f"{r['dir']} net {res.net_flips} ({res.outcome})" for r, res in zip(rows, results))
            + (f"; {chosen['dir']} pays the gate" if chosen else "; none is eligible"))
        return {"screens": rows, "best": chosen["dir"] if chosen else None,
                "best_package": chosen["package"] if chosen else None, "usd_priced": usd}

    def carried(self) -> dict:
        """A carry round (IMPROVER §9.2): the best screened candidate is carried to the next round instead of paying
        the gate; the next round works on it, and the next gate round gates what has accumulated."""
        sc = self.state["steps"]["screened"]
        dirs = [*((self.carry or {}).get("dirs") or ()), f"r{self.i}:{sc['best']}"]
        carry = {"package_ref": sc["best_package"], "base": self.base.ref(), "dirs": dirs, "since": self.round_id}
        self._write_carry(carry)
        self.ledger.row("carried", round_id=self.round_id, line=self.line, dir=sc["best"],
                        package=short(sc["best_package"]), dirs=dirs)
        self.out(f"round      {self.round_id}: {sc['best']} carried to the next round ({len(dirs)} carried in all); "
                 f"the gate comes at round {((self.i // max(int(self.s.gate_every), 1)) + 1) * max(int(self.s.gate_every), 1) - 1}")
        return carry

    def _same_cli(self) -> None:
        """The gate compares the candidate with readings of the current W taken earlier: the claude CLI they ran on
        must be the one the activity recorded (an update mid-round would mix two transports in one comparison)."""
        rows = list(self.a.events.read("cli_version"))
        if not rows or getattr(self.a, "provider_factory", None) is not None:
            return
        from ..providers.claude_code import cli_version
        now = cli_version()
        if now != rows[0].get("version"):
            raise Stop(f"cli: the claude CLI is {now or 'unreadable'} and this activity ran on {rows[0].get('version')}"
                       f": no gate on mixed transports (reinstall it, with DISABLE_AUTOUPDATER=1)")

    def gated(self) -> dict:
        sc = self.state["steps"].get("screened") or {}
        best = sc.get("best")
        # the gated package: this round's best screened candidate (built on what was carried), else the carry itself
        package_ref = sc["best_package"] if best else self.carry["package_ref"]
        name = best or "carry"
        package = load_package(self.a.objects, package_ref)
        self._same_cli()
        if not self._recorded(f"{self.round_id}:gate", "subject"):      # a resumed gate's own attempts are on record
            dev = cost_per_attempt(self.a, self.base.package_ref, "dev")
            train = cost_per_attempt(self.a, self.work_ref, "train")
            if best:
                res = ScreenResult.from_json(self.a.objects.get(next(r["screen"] for r in sc["screens"]
                                                                     if r["dir"] == best)))
                cost = res.cost or 0.0
            else:
                cost = cost_per_attempt(self.a, package_ref, "train") or 0.0
            scale = max(1.0, cost / train) if train else 1.0             # the candidate costs what it costs
            if dev is not None:
                self._afford(gate_attempts(self.a, self.base.package_ref) * dev * scale, "the gate")
        got = run_gate(self.a, run_arm=self.run_arm, round_id=self.round_id, package=package,
                       base_release=self.base, account=self.child("gate"))
        if best:
            item = next(i for i in self.state["steps"]["verified"]["passed"]
                        if Declaration.from_json(self.a.objects.get(i["declaration"])).dir == best)
            screened = next(r for r in sc["screens"] if r["dir"] == best)
            self.archive.put(self.i, best, self.a.objects.get(item["files"]),
                             Declaration.from_json(self.a.objects.get(item["declaration"])),
                             Verdict.from_json(self.a.objects.get(item["verdict"])),
                             screen=ScreenResult.from_json(self.a.objects.get(screened["screen"])),
                             receipt_ref=got["receipt"], package_ref=package_ref, line=self.line)
        carried = list((self.carry or {}).get("dirs") or ())
        self.ledger.row("gated", round_id=self.round_id, line=self.line, dir=name, verdict=got["verdict"],
                        gain=got.get("gain"), fixed_count=got["fixed_count"], regressed_count=got["regressed_count"],
                        **({"carried": carried} if carried else {}))
        if got.get("installed"):
            self.ledger.row("installed", round_id=self.round_id, line=self.line, dir=name,
                            release=got["installed"], **({"carried": carried} if carried else {}))
        else:
            self.ledger.row("rejected", round_id=self.round_id, line=self.line, dir=name,
                            verdict=got["verdict"])
            if self.carry:
                self.ledger.row("carry_dropped", round_id=self.round_id, line=self.line, dirs=carried,
                                verdict=got["verdict"])
        if self.carry or best:
            self._write_carry(None)             # gated, installed or not: the next round starts from the line
        self.out(f"round      {self.round_id}: the gate says {got['verdict']} (gain {got.get('gain')})"
                 + (f" for {name} with {carried} carried" if carried else "")
                 + (f"; installed {short(got['installed'])}" if got.get("installed") else ""))
        return got

    # ------------------------------------------------------------------ credit
    def credited(self) -> dict:
        new = self.a.releases.load(self.state["steps"]["gated"]["installed"])
        new_package = self.a.package(new)
        try:
            self._measure(new, new.package_ref, "measure")
        except (Stop, BudgetError) as e:
            # the install stands; its credit is not recorded (the next round measures the new W for its own reading)
            self.ledger.row("credited", round_id=self.round_id, line=self.line, release=new.ref(), component=[],
                            credit={}, reason=f"not credited: {str(e)[:200]}")
            return {"release": new.ref(), "credit": {}, "not_credited": str(e)[:300]}
        parent_reading = self.evidence.reading(self.base.package_ref)
        child_reading = self.evidence.reading(new.package_ref)
        comps = [c for c in components(self.a.package(self.base), new_package.files) if c["kind"] != "test"]
        credits = credit_of(new.ref(), parent_reading, child_reading, comps=comps, evidence=self.evidence)
        table = summary(credits)
        sc = self.state["steps"].get("screened") or {}
        # the gated package was this round's screened candidate: its cause's estimate learns from the install; a carry
        # gated on its own (nothing left to fix this round) learnt at its screens (IMPROVER §9.2)
        decl = next((Declaration.from_json(self.a.objects.get(i["declaration"]))
                     for i in (self.state["steps"].get("verified") or {}).get("passed") or ()
                     if Declaration.from_json(self.a.objects.get(i["declaration"])).dir == sc.get("best")), None) \
            if sc.get("best") else None
        cause = next((c for c in self.causes() if c.cause_id == decl.cause_id), None) \
            if decl is not None and "diagnosed" in self.state["steps"] else None
        if decl is not None and cause is not None and not decl.fixed:
            # the cause's net fixes where a component of the declared kind showed (IMPROVER §10)
            shown = {c.task_id for c in credits if c.conditioned and c.kind == decl.kind}
            fixes = sum(int(child_reading[t].success) - int(parent_reading[t].success) for t in cause.tasks
                        if t in child_reading and t in parent_reading and t in shown)
            Estimates(self.a.root, self.s).update(self.line, cause.name, decl.kind,
                                                  fixes=fixes / max(len(cause.tasks), 1),
                                                  cost=float((self.spend().get("used") or {}).get("usd") or 0.0),
                                                  key=f"{self.round_id}:{decl.dir}")
        if decl is not None and decl.fixed.startswith("promotion:"):   # the entry's skill is installed: superseded
            self.a.memory().supersede(decl.fixed.partition(":")[2], by=new.ref())
        history = {"footprints": footprints(child_reading, new_package, self.evidence),
                   "installed": {k: v["credit"] for k, v in table.items()}}
        self.ledger.row("credited", round_id=self.round_id, line=self.line, release=new.ref(),
                        component=sorted(table), credit={k: v["credit"] for k, v in table.items()},
                        history=history)
        return {"release": new.ref(), "credit": table}

    # ------------------------------------------------------------------ the playbook
    def playbook(self) -> dict:
        try:
            got = update_playbook(self.a.root, self.ledger, researcher=self.researcher("policy"), line=self.line,
                                  hide=set(self.split()[1]))
        except (BudgetError, MemorySidecarError) as e:
            return {"skipped": str(e)[:200]}
        self.ledger.row("playbook", round_id=self.round_id, line=self.line, status=f"{got['lessons']} lessons")
        return got


def run_round(activity, index: int, *, run_arm, out=print) -> RoundResult:
    return Round(activity, index, run_arm=run_arm, out=out).run()
