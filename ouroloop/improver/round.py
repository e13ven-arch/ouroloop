"""One round of the improver (docs/design/IMPROVER.md §3; DESIGN §6.1, v1.2).

    measure -> evidence -> diagnosed -> focused -> implemented -> verified -> screened -> accepted | rejected
            -> credited -> playbook

One package a round: R reads a focus list (the diagnosed causes with the kinds that have paid off), builds one
candidate package of as many components as its mechanism needs, and the package is screened once on every held-out
train task. An accepted screen installs the package on the working line (reason `screen`); a rejected one leaves the
line alone and the next round diagnoses the same W again. Dev is never read here: the release gate is the activity's
last phase (activity/phases.py `gate`).

Each step's output is kept in rounds/r<i>/round.json under the step's name and reused when the round runs again, so an
interrupted round pays only for what it had not done. A round is bound to the release it started on: if another
install moved the line meanwhile, the round is refused rather than continued on a W it did not diagnose.

Money: the round spends from its own account `<scope>:round-<i>` (round_share of the root's dollars), one child per
step kind (measure, diagnose, session, screen, policy). Before the research is paid, one screen of the whole held-out
part and the research calls are priced from what is on record; what cannot be priced, or does not fit, does not start.
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
from .allocate import Estimates, Focus, focus as focus_of, stalled
from .archive import Archive
from .components import components
from .credit import credit as credit_of, footprints, removal_candidates, summary
from .declaration import Declaration, fixed_declaration, read_declaration
from .evidence import Evidence, dev_histogram
from .ledger import Ledger
from .policy import read_playbook, update_playbook
from .screen import ScreenResult, accept, condition, cost_per_attempt, pick_best, pick_order, price, screen
from .session import CodingSession, WorkTree
from .stack import stack
from .settings import CODE_KINDS, Settings
from .verify import Verdict, VerifyContext, run_ladder
from .verify.unit import worker_files  # noqa: F401 - re-exported for the tests

PROMPTS = dg.PROMPTS
STEPS = ("measure", "evidence", "diagnosed", "focused", "implemented", "verified", "screened", "accepted", "stacked",
         "credited", "playbook")
PRICE_MARGIN = 0.15                     # over the means on record: candidates cost what they cost
REPLAY_SAMPLE = 6                       # passing train attempts V2b reports a hook's false-trigger rate on


@dataclass
class RoundResult:
    round_id: str
    status: str = "running"             # done | stopped
    causes: int = 0
    components: int = 0                 # the components R's candidate package declared
    candidates: int = 0                 # R's packages and the fixed code's candidates verified
    ladder: dict = field(default_factory=dict)       # rung -> candidates that passed it
    screen: dict | None = None          # the chosen screen: {dir, net_flips, holdout_score, best_so_far, accepted}
    accepted: bool = False
    installed: str | None = None        # the working line's release after the round's installs
    stacked: list = field(default_factory=list)      # the dirs stacked on the first installed package (§9.4)
    stop: str | None = None
    spend: dict = field(default_factory=dict)


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
            # the line moved under the round, unless the round's own install moved it (killed before its accepted
            # step was saved: the install's replay is a no-op)
            own = activity.releases.recover(f"install:{self.round_id}".replace("/", "_"))
            if self.state["base_release"] != active.ref() and self.state.get("status") not in ("done", "stopped") \
                    and "accepted" not in self.state["steps"] and getattr(own, "status", None) != "installed":
                raise ValueError(f"round {self.i} started on {short(self.state['base_release'])} and the line is now "
                                 f"{short(active.ref())}: a round is not continued on another W")
        else:
            self.state = {"round_id": self.round_id, "index": self.i, "base_release": active.ref(), "steps": {},
                          "status": "running", "improver_version": activity.core.improver_version}
        self.base = activity.releases.load(self.state["base_release"])
        self.package = activity.package(self.base)
        self.account = self._account()

    @property
    def work_ref(self) -> str:
        """The package ref the round reads train evidence of: the working line's."""
        return self.base.package_ref

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
        accepted = steps.get("accepted") or {}
        screened = steps.get("screened") or {}
        chosen = None
        if screened.get("best"):
            res = ScreenResult.from_json(self.a.objects.get(next(r["screen"] for r in screened["screens"]
                                                                 if r["dir"] == screened["best"])))
            chosen = {"dir": res.candidate, "net_flips": res.net_flips, "holdout_score": res.holdout_score,
                      "best_so_far": res.best_so_far, "accepted": res.accepted}
        stacked = steps.get("stacked") or {}
        return RoundResult(round_id=self.round_id, status=self.state.get("status", "running"),
                           causes=len((steps.get("diagnosed") or {}).get("causes") or ()),
                           components=sum(len(i.get("components") or ()) for i in
                                          (steps.get("implemented") or {}).get("candidates") or ()),
                           candidates=len((steps.get("implemented") or {}).get("candidates") or ()) +
                           len(verified.get("fixed") or ()),
                           ladder=verified.get("ladder") or {}, screen=chosen,
                           accepted=bool(accepted.get("installed")),
                           installed=stacked.get("line") or accepted.get("installed"),
                           stacked=list(stacked.get("stacked") or ()),
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
            raise Stop("nothing_to_fix: the working line passes every studied train task")
        if "diagnosed" not in self.state["steps"]:
            self._floor()
        self.step("diagnosed", self.diagnosed)
        self.step("focused", self.focused)
        self.step("implemented", self.implemented)
        verified = self.step("verified", self.verified)
        if not verified["passed"]:
            raise Stop("no_candidate: no candidate passed its free rungs")
        self.step("screened", self.screened)
        got = self.step("accepted", self.accepted)
        if got.get("installed"):
            self.step("stacked", self.stacked)
            self.step("credited", self.credited)
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
        """Before the round pays for diagnosis and R's session, the least it must pay to have an outcome fits: one
        screen of the whole held-out part, and the research it is about to pay — a call per case to diagnose and per
        turn of the session, at the research calls' mean on record (left out before any is on record)."""
        usd, why = price(self.a, self.work_ref, screen_attempts=len(self.split()[1]))
        if usd is None:
            raise Stop(f"unpriced: {why}")
        mean = self._research_mean()
        if mean is not None:
            cases = min(len(self.state["steps"]["evidence"]["failed"]), self.s.diagnose_max_cases)
            usd += mean * (cases + self.s.session_max_turns)
        self._afford(usd, "one held-out screen and the research")

    def split(self) -> tuple[tuple, tuple]:
        """(the train tasks R studies, the train tasks held out from it), fixed for the activity: whole families,
        ranked by a hash of K's order seed and the family, are held out while they fit in holdout_share of the train
        tasks (a family that would overshoot it is skipped; one task at least stays studied). V3 screens on the
        held-out part only, so a screen reads whether a candidate fixes tasks R never saw."""
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

    def holdout_score(self, package_ref: str) -> int | None:
        """A package's passes on the held-out tasks it has a valid train reading of (None: none read)."""
        held = set(self.split()[1])
        reading = {t: r for t, r in self.evidence.reading(package_ref).items() if t in held}
        return sum(1 for r in reading.values() if r.success) if reading else None

    def best_so_far(self) -> int:
        """The working line's best held-out score in this activity: the baseline's and every release installed since
        (IMPROVER §9: the floor the accept rule tolerates a step below)."""
        scores = [self.holdout_score(rel.package_ref) for rel in self.a.releases.history()]
        return max([s for s in scores if s is not None] or [0])

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
        got = self._measure(self.base, self.work_ref, "measure")
        held = set(self.split()[1])
        reading = self.evidence.reading(self.work_ref)
        got["holdout_passed"] = sum(1 for t, r in reading.items() if t in held and r.success)
        got["holdout"] = len(held)
        self.ledger.row("measured", round_id=self.round_id, line=self.line, passed=got["passed"], steps=got["tasks"],
                        holdout_passed=got["holdout_passed"], holdout=got["holdout"])
        self.out(f"round      {self.round_id}: W {short(self.base.ref())} passes {got['passed']}/{got['tasks']} train "
                 f"tasks ({got['holdout_passed']}/{got['holdout']} held out; {got['measured']} measured now)")
        return got

    def gather(self) -> dict:
        study = set(self.split()[0])
        failed = [e for e in self.evidence.failed(self.work_ref) if e.task_id in study]
        order = {t: i for i, t in enumerate(self.a.splits.ids("train"))}
        failed.sort(key=lambda e: (self.ledger.last_round_of(e.task_id), order.get(e.task_id, 0)))
        chosen = failed[:self.s.diagnose_max_cases]
        successes = self.evidence.best_successes(self.work_ref, self.s.diagnose_successes, tasks=study)
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
        done = self._fixed_done()
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

    # ------------------------------------------------------------------ the focus list
    def focused(self) -> dict:
        estimates = Estimates(self.a.root, self.s)
        held = set(self.split()[1])
        f = focus_of(self.causes(), estimates, self.archive, settings=self.s, line=self.line,
                     allowed=self.a.k["harness"]["kinds"], hide=held,
                     stalled=stalled(self.ledger, self.s.stall_rounds), train_available=False)
        self.ledger.row("focused", round_id=self.round_id, line=self.line,
                        causes=[{"cause": r["name"], "tasks": len(r["tasks"]),
                                 "kinds": [k["kind"] for k in r["kinds"]]} for r in f.ranked],
                        require_code=f.require_code, l5_suggested=f.l5_suggested)
        for note in f.notes:
            if note.startswith("l6_unavailable"):
                self.ledger.row("l6_unavailable", round_id=self.round_id, line=self.line, reason=note)
        self.out(f"round      {self.round_id}: focus " + ", ".join(
            f"{r['name']}({len(r['tasks'])})->{'/'.join(k['kind'] for k in r['kinds'][:3])}" for r in f.ranked)
            + ("; a tool or hook is required" if f.require_code else ""))
        return {"focus": self.a.objects.put(f)}

    def focus(self) -> Focus:
        return Focus.from_json(self.a.objects.get(self.state["steps"]["focused"]["focus"]))

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
                                              for t in passed[:REPLAY_SAMPLE]},
                             study_tasks=study,
                             cause_ids=tuple(c.cause_id for c in self.causes())
                             if "diagnosed" in self.state["steps"] else ())

    def with_memory(self, files: dict) -> dict:
        """A candidate's files with the parent's host-only memory snapshot put back (R's tree never holds it)."""
        out = {p: t for p, t in files.items() if p != MEMORY_PATH}
        if MEMORY_PATH in self.package.files:
            out[MEMORY_PATH] = self.package.files[MEMORY_PATH]
        return out

    # ------------------------------------------------------------------ the session
    def inputs(self) -> tuple[dict, set]:
        steps = self.state["steps"]
        study, held = (set(p) for p in self.split())
        from ..evaluate.attempt import task_hash
        held_hashes = {task_hash(self.a.suite.tasks[t]) for t in held}
        readable = set()
        cause_rows = []
        for c in self.causes():
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
                                                  if not set(e.get("distilled_from") or ()) & held_hashes]})
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
        f = self.focus()
        focus_rows = [{**r, "tasks": [t for t in r["tasks"] if t in study],
                       "held_out_tasks": sum(1 for t in r["tasks"] if t in held)} for r in f.ranked]
        files = {"focus.json": {"ranked": focus_rows, "require_code": f.require_code, "l5_suggested": f.l5_suggested,
                                "l6_suggested": f.l6_suggested, "notes": list(f.notes)},
                 "causes.json": cause_rows, "successes.json": successes,
                 "ledger.json": self.ledger.projection(line=self.line, hide=held),
                 "scoreboard.json": self.ledger.scoreboard(),
                 "histogram.json": dev_histogram(self.a, self.base.package_ref), "base_tools.json": base_tools}
        out = {name: json.dumps(v, ensure_ascii=False, indent=1, default=str) for name, v in files.items()}
        out["playbook.md"] = read_playbook(self.a.root)
        out["package_api.md"] = api
        return out, readable

    def _declared(self, decl, files: dict, verdict) -> dict:
        self.ledger.row("declared", round_id=self.round_id, line=self.line, dir=decl.dir, mechanism=decl.mechanism,
                        kind=decl.kind, site=decl.site, cause=list(decl.causes), trigger=decl.mechanism,
                        components=[{"kind": c.kind, "site": c.site, "cause_ids": list(c.cause_ids),
                                     "uses_model": c.uses_model} for c in decl.components],
                        predicted_affected=list(decl.predicted_affected), differs_from=list(decl.differs_from))
        return {"dir": decl.dir, "declaration": self.a.objects.put(decl),
                "components": [c.key for c in decl.components],
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

    def implemented(self) -> dict:
        if not self.causes():
            return {"candidates": [], "session": None}
        saved = self.state.get("session_result")
        if saved is not None:       # the session ran before the step was cut short: its candidates, not paid again
            vctx = self.vctx()
            out = []
            for item in saved["candidates"]:
                decl = Declaration.from_json(self.a.objects.get(item["declaration"]))
                files = self.a.objects.get(item["files"])
                out.append(self._declared(decl, files, run_ladder(files, decl, vctx)))
            self.out(f"round      {self.round_id}: the session's {len(out)} packages, as it left them")
            return {"candidates": out, "session": saved["session"]}
        inputs, readable = self.inputs()
        kinds = [k for k in self.a.k["harness"]["kinds"] if k not in ("memory", "model", "test")] + ["removal"]
        system = (PROMPTS / "propose.md").read_text(encoding="utf-8")
        for name, value in {"packages": self.s.packages_per_round, "max_turns": self.s.session_max_turns,
                            "max_edits": self.s.session_max_edits, "max_read": self.s.session_max_read_bytes,
                            "max_read_total": self.s.session_max_read_total, "l5_per_round": self.s.l5_per_round,
                            "hook_aux_calls": self.s.hook_aux_calls,
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
                return unit_check(self.with_memory(files), None, vctx)["detail"] \
                    or "the package tests, the probe and the smoke pass"

            session = CodingSession(researcher=self.researcher("session"), work=tree, trace=self.evidence.trace,
                                    check=check, tests=tests, settings=self.s, system=system, inputs={
                                        "round": self.round_id, "focus": json.loads(inputs["focus.json"]),
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
                                   f"call finish again with the same declaration.")
                    again, _ = tree.fetch(decl.dir)
                    if again is not None:
                        files = self.with_memory(again)
                        verdict = run_ladder(files, decl, vctx)
                        self.state["session_result"]["candidates"] = [
                            {"declaration": self.a.objects.put(d), "files": self.a.objects.put_json(
                                "candidate_files", files if d.dir == decl.dir else f)} for d, f in fetched]
                        self._save()
                out.append(self._declared(decl, files, verdict))
        finally:
            wsession.close(force=True)
            if session is not None:
                transcript = self.a.objects.put_json("session_transcript", session.transcript())
                write_json(self.dir / "session.json", {**session.record(), "transcript": transcript})
        self.out(f"round      {self.round_id}: the session declared {len(out)} package(s) with "
                 f"{sum(len(i['components']) for i in out)} components "
                 f"({session.turns if session else 0} turns, {session.edits if session else 0} edits"
                 + (f"; ended: {session.ended}" if session and session.ended else "") + ")")
        return {"candidates": out, "session": str(self.dir / "session.json")}

    # ------------------------------------------------------------------ the fixed code's candidates
    def _fixed_done(self) -> set:
        """The fixed candidates' sources earlier rounds settled: declared, and not left `not_selected` by their
        screen (one accepted but not chosen comes back the next round)."""
        done = set()
        for r in self.ledger.rows(stage="declared"):
            if r.get("source") and r.get("round_id") != self.round_id:
                done.add(r["source"])
        for r in self.ledger.rows(stage="screened"):
            if r.get("source") and r.get("outcome") == "not_selected" and r.get("round_id") != self.round_id:
                done.discard(r["source"])
        return done

    def fixed_candidates(self) -> list[tuple[Declaration, dict]]:
        """The library's snapshot not yet on the ladder, the memory's promoted skills, removal candidates — at most
        fixed_per_round, in that order."""
        out: list[tuple[Declaration, dict]] = []
        done = self._fixed_done()
        for e in reversed(list(self.a.events.read("library_snapshot"))):
            src = f"snapshot:{e.get('snapshot_digest')}"
            if e.get("outcome") == "candidate" and src not in done and e.get("package_ref"):
                snap = load_package(self.a.objects, e["package_ref"])
                from ..memory.snapshot import snapshot_candidate
                files = snapshot_candidate(self.package, snap.memory_text)
                out.append((fixed_declaration("memory-snapshot", source=src, kind="memory", site=None,
                                              cause_id=f"{self.round_id}:memory",
                                              trigger="every attempt reads the library"), files))
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
                out.append((fixed_declaration(f"promoted-{mid[:8]}", source=f"promotion:{mid}", kind="skill",
                                              site=name, cause_id=f"{self.round_id}:memory",
                                              trigger="the skill index lists it",
                                              predicted_affected=sorted(ids[h] for h in distilled_from(e)
                                                                        if h in ids)), files))
        history = [r.get("history") for r in self.ledger.rows(stage="credited") if r.get("history")]
        for comp, files in removal_candidates(history, self.package, zero_rounds=self.s.removal_zero_credit_rounds):
            src = f"removal:{comp['kind']}:{comp['name']}:{short(self.base.ref())}"
            if src not in done:
                out.append((fixed_declaration(f"remove-{comp['kind']}-{comp['name']}"[:60], source=src,
                                              kind="removal", site=comp["name"], cause_id=f"{self.round_id}:bloat",
                                              trigger=comp["why"]), files))
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
            for rung, ok in verdict.rungs().items():
                ladder[rung] = ladder.get(rung, 0) + bool(ok)
            self.ledger.row("verified", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                            passed=verdict.passed(), outcome="duplicate" if dup else "",
                            failed_components=verdict.failed_components(), reason=verdict.text()[:800])
            if verdict.passed() and not dup:
                passed.append(item)
        for decl, files in self.fixed_candidates():
            vctx = vctx or self.vctx()
            verdict = run_ladder(files, decl, vctx)
            self.ledger.row("declared", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                            site=decl.site, source=decl.fixed, cause=list(decl.causes))
            if "removal" in decl.kinds:
                self.ledger.row("removal_proposed", round_id=self.round_id, line=self.line, dir=decl.dir,
                                site=decl.site, reason=decl.mechanism)
            self.archive.put(self.i, decl.dir, files, decl, verdict, line=self.line)
            for rung, ok in verdict.rungs().items():
                ladder[rung] = ladder.get(rung, 0) + bool(ok)
            self.ledger.row("verified", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                            passed=verdict.passed(), reason=verdict.text()[:800])
            item = {"dir": decl.dir, "declaration": self.a.objects.put(decl), "components": [c.key for c in
                                                                                             decl.components],
                    "files": self.a.objects.put_json("candidate_files", files), "verdict": self.a.objects.put(verdict)}
            fixed.append(decl.dir)
            if verdict.passed():
                passed.append(item)
        # the code requirement (IMPROVER §6): a mechanizable cause and no tool or hook past its free rungs
        mech = [c for c in self.causes() if c.mechanizable]
        code = [p for p in passed if any(c.kind in CODE_KINDS for c in
                                         Declaration.from_json(self.a.objects.get(p["declaration"])).components)]
        if self.s.require_code_component and mech and not code:
            for c in mech:
                self.ledger.row("no_code_candidate", round_id=self.round_id, line=self.line, cause=c.name)
        self.out(f"round      {self.round_id}: {len(passed)} candidates passed their free rungs ({ladder})")
        return {"passed": passed, "fixed": fixed, "ladder": ladder}

    # ------------------------------------------------------------------ V3: the held-out screen
    def screened(self) -> dict:
        from ..evaluate.gate import transport_problem
        problem = transport_problem(self.a)
        if problem:
            raise Stop(problem)
        reading = self.evidence.reading(self.work_ref)
        held = list(self.split()[1])
        best_so_far = self.best_so_far()
        items = list(self.state["steps"]["verified"]["passed"])
        decls = {i["dir"]: Declaration.from_json(self.a.objects.get(i["declaration"])) for i in items}
        # R's package first, then the fixed code's (the snapshot last); each is paid while it fits what the round has
        # left beside the screens before it (a resumed step re-reads its attempts for free)
        items.sort(key=lambda i: (bool(decls[i["dir"]].fixed), "memory" in decls[i["dir"]].kinds))
        to_pay = lambda chosen: sum(max(len(held) - self._recorded(f"r{self.i}:{i['dir']}", "screen"), 0)  # noqa
                                    for i in chosen)
        chosen, dropped = [], []
        for item in items:
            usd, why = price(self.a, self.work_ref, screen_attempts=to_pay([*chosen, item]))
            if usd is None:
                raise Stop(f"unpriced: {why}")
            need, left = self._need(usd)
            (chosen if left is None or need <= left + 1e-9 else dropped).append(item)
        if dropped and not chosen:
            usd, _ = price(self.a, self.work_ref, screen_attempts=to_pay(dropped[:1]))
            self._afford(usd, "the held-out screen")
        for item in dropped:
            self.ledger.row("screened", round_id=self.round_id, line=self.line, dir=item["dir"],
                            kind=decls[item["dir"]].kind, outcome="not_run",
                            reason="budget: its screen does not fit what the round has left beside the screens "
                                   "before it")
        usd, _ = price(self.a, self.work_ref, screen_attempts=to_pay(chosen))
        results, rows = [], []
        for item in chosen:
            decl = decls[item["dir"]]
            files = self.a.objects.get(item["files"])
            try:
                package = build(self.a.objects, files, limits=Limits.from_core(self.a.k),
                                seed=self.a.package(self.a.seed_release()),
                                parent=None if "memory" in decl.kinds else self.package)
            except PackageError as e:
                self.ledger.row("screened", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                                outcome="invalid", reason=f"build: {e}"[:300])
                continue
            comps = components(self.package, files)
            res = screen(self.a, run_arm=self.run_arm, name=f"r{self.i}:{decl.dir}", package=package,
                         base_release=self.base, holdout=held, reading=reading, evidence=self.evidence,
                         conditions=condition(comps), account=self.child("screen"),
                         retries=int(self.a.k["gate"]["max_infrastructure_retries"]), best_so_far=best_so_far,
                         settings=self.s)
            results.append(res)
            rows.append({"dir": decl.dir, "screen": self.a.objects.put(res), "package": package.ref()})
            self.archive.put(self.i, decl.dir, files, decl, Verdict.from_json(self.a.objects.get(item["verdict"])),
                             screen=res, package_ref=package.ref(), line=self.line)
            self.ledger.row("screened", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                            cause=list(decl.causes), predicted_affected=list(decl.predicted_affected),
                            fixed=list(res.fixed), regressed=list(res.regressed), net_flips=res.net_flips,
                            fixes=len(res.fixed), holdout_score=res.holdout_score, best_so_far=res.best_so_far,
                            outcome=res.outcome, accepted=res.accepted, reasons=list(res.reasons),
                            reason=res.reason, **({"source": decl.fixed} if decl.fixed else {}))
            if res.outcome == "screened" and not decl.fixed:
                self._observe(decl, res, cost=(res.cost or 0.0) * len(res.holdout))
            if res.outcome == "budget_exhausted":
                raise Stop("budget: the budget refused a screen attempt")
        best = pick_best(results)
        picked = next((r for r, res in zip(rows, results) if res is best), None) if best is not None else None
        for r, res in zip(rows, results):
            if res.accepted and picked is not None and r is not picked:
                decl = decls[r["dir"]]
                self.ledger.row("screened", round_id=self.round_id, line=self.line, dir=r["dir"], kind=decl.kind,
                                outcome="not_selected", reason=f"accepted, and {picked['dir']} is installed this round",
                                **({"source": decl.fixed} if decl.fixed else {}))
        self.out(f"round      {self.round_id}: screened " + ", ".join(
            f"{r['dir']} net {res.net_flips} score {res.holdout_score}/{len(res.holdout)} "
            f"({'accepted' if res.accepted else res.outcome if res.outcome != 'screened' else 'rejected'})"
            for r, res in zip(rows, results)) + (f"; {picked['dir']} is installed" if picked else "; none accepted"))
        return {"screens": rows, "best": picked["dir"] if picked else None,
                "best_package": picked["package"] if picked else None, "usd_priced": usd, "best_so_far": best_so_far}

    def _observe(self, decl: Declaration, res: ScreenResult, *, cost: float, reading: dict | None = None) -> None:
        """One observation per (component, cause) of a screened package (IMPROVER §6, §10): the component's
        conditioned net flips on the cause's held-out tasks, as a share of the cause's failing held-out tasks. After
        an acceptance, `reading` (the new W's) replaces the screen's observation under the same key."""
        causes = {c.cause_id: c for c in self.causes()}
        estimates = Estimates(self.a.root, self.s)
        failing = set(res.failing)
        for comp in decl.components:
            shown = set(res.footprints.get(comp.key) or res.footprints.get("prompt:*") or ())
            for cid in comp.cause_ids:
                cause = causes.get(cid)
                if cause is None:
                    continue
                tasks = set(cause.tasks)
                fixed = {t for t in res.fixed if t in tasks and t in shown}
                regressed = {t for t in res.regressed if t in tasks}
                denominator = max(len(failing & tasks), 1)
                estimates.update(self.line, cause.name, comp.kind, fixes=(len(fixed) - len(regressed)) / denominator,
                                 cost=cost, key=f"{self.round_id}:{decl.dir}:{comp.key}")

    # ------------------------------------------------------------------ acceptance
    def accepted(self) -> dict:
        sc = self.state["steps"]["screened"]
        if not sc.get("best"):
            self.ledger.row("rejected", round_id=self.round_id, line=self.line,
                            reason="no screened package meets the accept rule")
            return {"installed": None}
        res = ScreenResult.from_json(self.a.objects.get(next(r["screen"] for r in sc["screens"]
                                                             if r["dir"] == sc["best"])))
        done = accept(self.a, res, round_id=self.round_id, base_release=self.base)
        item = next(i for i in self.state["steps"]["verified"]["passed"] if i["dir"] == sc["best"])
        decl = Declaration.from_json(self.a.objects.get(item["declaration"]))
        receipt_ref = self.a.objects.put(done)
        self.archive.put(self.i, sc["best"], self.a.objects.get(item["files"]), decl,
                         Verdict.from_json(self.a.objects.get(item["verdict"])), screen=res, receipt_ref=receipt_ref,
                         package_ref=res.package_ref, line=self.line)
        if done.status == "installed":
            self.ledger.row("accepted", round_id=self.round_id, line=self.line, dir=sc["best"], kind=decl.kind,
                            release=done.target_release, net_flips=res.net_flips, holdout_score=res.holdout_score,
                            best_so_far=res.best_so_far)
            self.ledger.row("installed", round_id=self.round_id, line=self.line, dir=sc["best"],
                            release=done.target_release)
            self.out(f"round      {self.round_id}: {sc['best']} installed on the working line as "
                     f"{short(done.target_release)} (held out {res.holdout_score}/{len(res.holdout)}, net "
                     f"{res.net_flips})")
        else:
            self.ledger.row("rejected", round_id=self.round_id, line=self.line, dir=sc["best"],
                            reason=f"install {done.status}: {done.reason}")
            self.out(f"round      {self.round_id}: {sc['best']} was accepted and not installed: {done.status}")
        return {"installed": done.target_release if done.status == "installed" else None, "status": done.status,
                "receipt": receipt_ref}

    # ------------------------------------------------------------------ stacking the other accepted packages (§9.4)
    def stacked(self) -> dict:
        """Each further accepted package, in pick order, laid on the working line as it stands when it touches nothing
        the line changed since the round's base; the stacked package climbs V0 and V1 and installs under its own
        screen's acceptance. A collision or a failed rung is `not_stacked`, on the ledger."""
        sc = self.state["steps"]["screened"]
        first = sc["best"]
        results = {r["dir"]: ScreenResult.from_json(self.a.objects.get(r["screen"])) for r in sc["screens"]}
        order = [r for r in pick_order(results.values()) if r.candidate.rpartition(":")[2] != first]
        line_ref = self.state["steps"]["accepted"]["installed"]
        base_files = {p: t for p, t in self.package.files.items()}
        stacked, refused = [], []
        for res in order:
            name = res.candidate.rpartition(":")[2]
            if not res.accepted:
                continue
            line = self.a.releases.load(line_ref)
            line_package = self.a.package(line)
            item = next(i for i in self.state["steps"]["verified"]["passed"] if i["dir"] == name)
            decl = Declaration.from_json(self.a.objects.get(item["declaration"]))
            cand_files = self.a.objects.get(item["files"])
            files, conflicts = stack(base_files, line_package.files, cand_files)
            if conflicts:
                self.ledger.row("not_stacked", round_id=self.round_id, line=self.line, dir=name, kind=decl.kind,
                                conflicts=conflicts, reason=f"conflict with the working line: {conflicts}")
                refused.append(name)
                continue
            vctx = self.vctx()
            vctx.parent = line_package
            vctx.cause_ids = ()                  # the declaration was checked against the base already
            verdict = run_ladder(files, decl, vctx, rungs=("V0", "V1"))
            if not verdict.passed():
                self.ledger.row("not_stacked", round_id=self.round_id, line=self.line, dir=name, kind=decl.kind,
                                reason=f"stacked on {short(line_ref)}: {verdict.text()[:500]}")
                refused.append(name)
                continue
            try:
                package = build(self.a.objects, files, limits=Limits.from_core(self.a.k),
                                seed=self.a.package(self.a.seed_release()), parent=line_package)
            except PackageError as e:
                self.ledger.row("not_stacked", round_id=self.round_id, line=self.line, dir=name, kind=decl.kind,
                                reason=f"build: {e}"[:300])
                refused.append(name)
                continue
            from dataclasses import replace
            stacked_res = replace(res, package_ref=package.ref(), outcome="stacked", stacked_on=line_ref)
            screen_ref = self.a.objects.put(stacked_res)
            release = self.a.releases.screened(package.ref(), screen_ref, parent=line)
            done = self.a.releases.install_screened(release, screen_ref, expected_parent=line_ref,
                                                    operation_id=f"install:{self.round_id}:{name}".replace("/", "_"))
            if done.status != "installed":
                self.ledger.row("not_stacked", round_id=self.round_id, line=self.line, dir=name, kind=decl.kind,
                                reason=f"install {done.status}: {done.reason}")
                refused.append(name)
                continue
            line_ref = done.target_release
            self.archive.put(self.i, name, files, decl, verdict, screen=stacked_res,
                             receipt_ref=self.a.objects.put(done), package_ref=package.ref(), line=self.line)
            self.ledger.row("accepted", round_id=self.round_id, line=self.line, dir=name, kind=decl.kind,
                            release=line_ref, net_flips=res.net_flips, holdout_score=res.holdout_score,
                            best_so_far=res.best_so_far, stacked_on=stacked_res.stacked_on)
            self.ledger.row("installed", round_id=self.round_id, line=self.line, dir=name, release=line_ref,
                            stacked_on=stacked_res.stacked_on)
            stacked.append(name)
            self.out(f"round      {self.round_id}: {name} stacked on the working line as {short(line_ref)} "
                     f"(its own screen: net {res.net_flips}, {res.holdout_score}/{len(res.holdout)})")
        if refused:
            self.out(f"round      {self.round_id}: not stacked: {refused}")
        return {"stacked": stacked, "not_stacked": refused, "line": line_ref}

    # ------------------------------------------------------------------ credit
    def credited(self) -> dict:
        """The accepted package's credit, from the screen's held-out reading (IMPROVER §10): for every changed
        component, the delta on the held-out tasks it showed on; the estimates' observations of this package replaced
        by it; the footprints kept for the anti-bloat rule. The studied part is read by the next round's measure."""
        new = self.a.releases.load(self.state["steps"]["accepted"]["installed"])
        new_package = self.a.package(new)
        sc = self.state["steps"]["screened"]
        res = ScreenResult.from_json(self.a.objects.get(next(r["screen"] for r in sc["screens"]
                                                             if r["dir"] == sc["best"])))
        item = next(i for i in self.state["steps"]["verified"]["passed"] if i["dir"] == sc["best"])
        decl = Declaration.from_json(self.a.objects.get(item["declaration"]))
        held = set(res.holdout)
        parent_reading = {t: r for t, r in self.evidence.reading(self.base.package_ref).items() if t in held}
        child_reading = {t: r for t, r in self.evidence.reading(new.package_ref).items() if t in held}
        comps = [c for c in components(self.a.package(self.base), new_package.files) if c["kind"] != "test"]
        credits = credit_of(new.ref(), parent_reading, child_reading, comps=comps, evidence=self.evidence)
        table = summary(credits)
        if not decl.fixed:
            self._observe(decl, res, cost=float((self.spend().get("used") or {}).get("usd") or 0.0))
        if decl.fixed.startswith("promotion:"):     # the entry's skill is installed: superseded
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
