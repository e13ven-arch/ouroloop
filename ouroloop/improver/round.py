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

v1.9 (DESIGN §6.19): between the free rungs and the paid screen each of R's packages is tried on the studied failing
tasks it claims (trial.py), its results handed back to the session for a bounded number of revisions, and only a
package that fixed one of them in-sample is screened; a stream round's screen window also holds the fixed and
regressed tasks of the frontier entries merged this round; the research sample is priced after the screen, not
before it for every package.

A stream round (v1.3, IMPROVER §3.1; activity/phases.py `stream`) is the same round over `pool`, the stream's scored
tasks: nothing is measured (the pool's latest attempt of each task is the reading, the working line's own where it has
one), the studied and held-out parts are the activity's split restricted to the pool, the screen runs on the most
recent stream_screen_max held-out tasks, and the round lives under rounds/s<i> as `<scope>/s<i>`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
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
from .evidence import NOT_READINGS, TRAIN, Evidence, dev_histogram
from .frontier import Frontier, FrontierEntry
from .ledger import Ledger, scrub
from .policy import read_playbook, update_playbook
from .screen import (ScreenResult, accept, condition, cost_per_attempt, fix_evidence, fixed_sessions, pick_best,
                     pick_order, price, regression_evidence, research_run, research_sample, screen)
from .session import CodingSession, WorkTree
from .trial import TrialResult, feedback as trial_feedback, trial as run_trial, trial_tasks
from .stack import stack
from .settings import CODE_KINDS, Settings
from .verify import Verdict, VerifyContext, run_ladder
from .verify.unit import worker_files  # noqa: F401 - re-exported for the tests

PROMPTS = dg.PROMPTS
STEPS = ("measure", "evidence", "diagnosed", "focused", "implemented", "verified", "screened", "accepted", "stacked",
         "credited", "playbook")
PRICE_MARGIN = 0.15                     # over the means on record: candidates cost what they cost
REGRESSIONS_SHOWN = 8                   # v1.8: packages of this line whose regression evidence R reads, newest first
MERGE_DIR = "merge"                     # v1.8: the candidate directory of a frontier entry's conditioned version


def confirm_allowance(held: int, settings) -> int:
    """The attempts a screen is priced with beyond its held-out tasks, for the confirmation (DESIGN §6.17): a fifth of
    the tasks flipping (two at least), each read again under the candidate and W topped up to confirm_readings."""
    return max(2, held // 5) * (2 * int(settings.confirm_readings) - 1)


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
    def __init__(self, activity, index: int, *, run_arm, out=print, pool=None):
        self.a, self.i, self.run_arm, self.out = activity, int(index), run_arm, out
        self.s = Settings.from_core(activity.k)
        # a stream round works from `pool` (the stream's scored tasks, in its order) under rounds/s<i>; a batch round
        # measures the working line on every train task under rounds/r<i>
        self.tag = f"s{self.i}" if pool is not None else f"r{self.i}"
        self.round_id = f"{activity.scope_id}/{self.tag}"
        self.dir = Path(activity.root) / "rounds" / self.tag
        self.state_path = self.dir / "round.json"
        self.ledger = Ledger(activity.root)
        self.archive = Archive(activity.root, activity.objects)
        self.evidence = Evidence(activity)
        self.frontier = Frontier(activity.root, self.s)        # v1.8: the partial packages R is asked to merge
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
                          "status": "running", "improver_version": activity.core.improver_version,
                          "account": f"{activity.scope_id}:round-{self.tag if pool is not None else self.i}"}
            if pool is not None:
                self.state["pool"] = list(pool)
        # a resumed stream round keeps the pool it started on
        self.pool = list(self.state["pool"]) if "pool" in self.state else None
        self.base = activity.releases.load(self.state["base_release"])
        self.package = activity.package(self.base)
        self.account = self._account()

    @property
    def stream(self) -> bool:
        return self.pool is not None

    @property
    def work_ref(self) -> str:
        """The package ref the round reads train evidence of: the working line's."""
        return self.base.package_ref

    def reading(self) -> dict:
        """The W's train reading the round works from: the working line's own (a batch round), or the pool's latest
        attempts, the working line's own where it has one (a stream round, IMPROVER §3.1)."""
        if self.stream:
            return self.evidence.latest(self.pool, prefer=self.work_ref)
        return self.evidence.reading(self.work_ref)

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
        return root.child(self.state.get("account") or f"{self.a.scope_id}:round-{self.i}", limits)

    def child(self, kind: str):
        return self.account.child(f"{self.account.scope_id}:{kind}", {})

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
        trials = int(self.s.trial_max_attempts) if int(self.s.trial_max_tasks) > 0 else 0     # v1.9
        usd, why = price(self.a, self.work_ref, screen_attempts=len(self.screen_holdout()) + trials)
        if usd is None:
            raise Stop(f"unpriced: {why}")
        mean = self._research_mean()
        if mean is not None:
            cases = min(len(self.state["steps"]["evidence"]["failed"]), self.s.diagnose_max_cases)
            usd += mean * (cases + self.s.session_max_turns)
        self._afford(usd, "one held-out screen, the trials and the research")

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
            held_set = set(held)
            if self.stream:
                # the pool's part of the same split, in the stream's order: a task's side never changes (§3.1)
                self._split = (tuple(t for t in self.pool if t not in held_set),
                               tuple(t for t in self.pool if t in held_set))
            else:
                self._split = (tuple(t for t in train if t not in held_set), tuple(t for t in train if t in held_set))
        return self._split

    def screen_holdout(self) -> list[str]:
        """The held-out tasks the screen runs on: every one (a batch round); the stream's most recent stream_screen_max
        of them (a stream round, §3.1) and, v1.9, the fixed and regressed tasks of the frontier entries this round
        merges — a merge is read where the entry fixed and broke, whatever the window."""
        held = list(self.split()[1])
        if not self.stream:
            return held
        wanted = set(held[-int(self.s.stream_screen_max):])
        for e in self._merge_entries():
            wanted |= set(e.fixed) | set(e.regressed)
        return [t for t in held if t in wanted]

    def _merge_entries(self) -> list[FrontierEntry]:
        """The frontier entries this round merges: focus.json's once the focus is on record, else the picks the
        focus would make (the frontier only moves after the screens)."""
        if "focused" in self.state["steps"]:
            return [e for e in (self.frontier.get(m["entry"]) for m in self.focus().merge) if e is not None]
        return self.frontier.picks(self.line, int(self.s.merge_per_round))

    def _recorded(self, candidate: str, role: str, package_ref: str | None = None) -> int:
        """The valid attempts on record under a run of this name and role (and package, when given)."""
        runs = self.a.results.runs()
        return sum(1 for r in self.a.results.all() if r.validity == "valid" and runs.get(r.run) is not None
                   and runs[r.run].candidate == candidate and runs[r.run].role == role
                   and (package_ref is None or runs[r.run].package_ref == package_ref))

    def _trial_used(self) -> int:
        """The trial attempts this round has on record, over its packages and iterations (v1.9: trial_max_attempts)."""
        runs = self.a.results.runs()
        return sum(1 for r in self.a.results.all() if runs.get(r.run) is not None and runs[r.run].role == "trial"
                   and str(runs[r.run].candidate).startswith(f"{self.tag}:"))

    def holdout_score(self, package_ref: str) -> int | None:
        """A package's passes on the screen's held-out tasks it has a valid train reading of (None: none read)."""
        held = set(self.screen_holdout())
        reading = {t: r for t, r in self.evidence.reading(package_ref).items() if t in held}
        return sum(1 for r in reading.values() if r.success) if reading else None

    def best_so_far(self) -> int:
        """The working line's best held-out score in this activity: the baseline's and every release installed since
        (IMPROVER §9: the floor the accept rule tolerates a step below); in a stream round, the pool's reading of the
        screen's tasks too, whichever line read each (§3.1)."""
        scores = [self.holdout_score(rel.package_ref) for rel in self.a.releases.history()]
        if self.stream:
            held = set(self.screen_holdout())
            scores.append(sum(1 for t, r in self.reading().items() if t in held and r.success))
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
        if self.stream:         # no measurement: the pool's latest attempts are the reading (§3.1)
            reading = self.reading()
            got = {"package": self.work_ref, "measured": 0, "tasks": len(reading),
                   "passed": sum(1 for r in reading.values() if r.success), "pool": len(self.pool)}
        else:
            got = self._measure(self.base, self.work_ref, "measure")
            reading = self.reading()
        held = set(self.split()[1])
        got["holdout_passed"] = sum(1 for t, r in reading.items() if t in held and r.success)
        got["holdout"] = len(held)
        protected, kept = self.protected(held, reading)
        got["protected"], got["protected_passed"] = len(protected), len(kept)
        self.ledger.row("measured", round_id=self.round_id, line=self.line, passed=got["passed"], steps=got["tasks"],
                        holdout_passed=got["holdout_passed"], holdout=got["holdout"], protected=len(protected),
                        protected_passed=len(kept))
        self.out(f"round      {self.round_id}: W {short(self.base.ref())} passes {got['passed']}/{got['tasks']} "
                 f"{'scored tasks of the stream' if self.stream else 'train tasks'} ({got['holdout_passed']}/"
                 f"{got['holdout']} held out, {len(kept)}/{len(protected)} of the protected set; {got['measured']} "
                 f"measured now)")
        return got

    def protected(self, held, reading: dict) -> tuple[list[str], list[str]]:
        """v1.8 (DESIGN §6.18): the protected set — the held-out tasks whose first valid train reading in this
        activity passed — and those of them the current W passes. The activity's success is this count never going
        down from round to round; the report draws it."""
        first: dict = {}
        runs = self.a.results.runs()
        for r in self.a.results.all():
            run = runs.get(r.run)
            if r.task_id in held and r.task_id not in first and r.split == TRAIN and r.validity == "valid" \
                    and run is not None and run.role not in NOT_READINGS:
                first[r.task_id] = r
        order = {t: i for i, t in enumerate(self.a.splits.ids(TRAIN))}
        protected = sorted((t for t, r in first.items() if r.success), key=lambda t: order.get(t, len(order)))
        return protected, [t for t in protected if t in reading and reading[t].success]

    def gather(self) -> dict:
        study = set(self.split()[0])
        reading = self.reading()
        failed = [self.evidence.of(r) for t, r in reading.items() if t in study and not r.success]
        if self.stream:         # the latest batches first (§3.1)
            position = {t: i for i, t in enumerate(self.pool)}
            failed.sort(key=lambda e: (self.ledger.last_round_of(e.task_id), -position.get(e.task_id, 0)))
        else:
            order = {t: i for i, t in enumerate(self.a.splits.ids("train"))}
            failed.sort(key=lambda e: (self.ledger.last_round_of(e.task_id), order.get(e.task_id, 0)))
        chosen = failed[:self.s.diagnose_max_cases]
        successes = self.evidence.cleanest({t: r for t, r in reading.items() if t in study},
                                           self.s.diagnose_successes)
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
        # v1.8 (DESIGN §6.18): the frontier's entries R is asked to merge this round, one package each
        picks = self.frontier.picks(self.line, int(self.s.merge_per_round))
        if picks:
            f = replace(f, merge=tuple(self._merge_item(e, f, i) for i, e in enumerate(picks)))
        self.ledger.row("focused", round_id=self.round_id, line=self.line,
                        causes=[{"cause": r["name"], "tasks": len(r["tasks"]),
                                 "kinds": [k["kind"] for k in r["kinds"]]} for r in f.ranked],
                        require_code=f.require_code, l5_suggested=f.l5_suggested,
                        **({"merge": [e.id for e in picks]} if picks else {}))
        for note in f.notes:
            if note.startswith("l6_unavailable"):
                self.ledger.row("l6_unavailable", round_id=self.round_id, line=self.line, reason=note)
        self.out(f"round      {self.round_id}: focus " + ", ".join(
            f"{r['name']}({len(r['tasks'])})->{'/'.join(k['kind'] for k in r['kinds'][:3])}" for r in f.ranked)
            + ("; a tool or hook is required" if f.require_code else "")
            + (f"; merge {', '.join(e.id for e in picks)} from the frontier" if picks else ""))
        return {"focus": self.a.objects.put(f)}

    def focus(self) -> Focus:
        return Focus.from_json(self.a.objects.get(self.state["steps"]["focused"]["focus"]))

    def _merge_item(self, entry: FrontierEntry, f: Focus, index: int = 0) -> dict:
        """One frontier entry as focus.json names it: its counts, the candidate directory its merge goes in, and the
        cause ids a merge declares — this round's id of each cause the entry's package named, or the entry's own
        where the cause is not diagnosed this round."""
        decl = Declaration.from_json(self.a.objects.get(entry.declaration_ref))
        by_name = {r["name"]: r["cause_id"] for r in f.ranked}
        cause_ids = sorted({by_name.get(i.rpartition(":")[2], i) for i in decl.cause_ids})
        return {"entry": entry.id, "round": entry.tag, "dir": entry.dir, "mechanism": entry.mechanism,
                "kind": entry.kind, "fixes": len(entry.fixed), "regressions": len(entry.regressed),
                "attempts": entry.attempts, "cause_ids": cause_ids, "heuristic_terms": dict(entry.heuristic_terms),
                "candidate_dir": MERGE_DIR if index == 0 else f"{MERGE_DIR}-{index + 1}"}

    def merge_ids(self) -> tuple:
        """The frontier entries this round merges (focus.json's `merge`): what a declaration's merge_of may name."""
        if "focused" not in self.state["steps"]:
            return ()
        return tuple(m["entry"] for m in self.focus().merge)

    # ------------------------------------------------------------------ verification
    def vctx(self) -> VerifyContext:
        train = tuple(self.a.splits.ids("train"))
        reading = self.reading()
        rows = [{"task_id": t, "assertions": [dict(x) for x in self.a.suite.grading(t).assertions],
                 "state": self.a.suite.grading(t).initial_state, "prompt": self.a.suite.tasks[t].trigger}
                for t in train]
        study = self.split()[0]
        failed = [t for t in study if t in reading and not reading[t].success]
        passed = [t for t in study if t in reading and reading[t].success]
        # V2c reads reach on lexical_embed's scale, the one reach_min_score is calibrated on (bge-small puts unrelated
        # texts at 0.5 and above: on it 0.2 would pass every description)
        embed = None
        cause_ids = tuple(c.cause_id for c in self.causes()) if "diagnosed" in self.state["steps"] else ()
        if "focused" in self.state["steps"]:       # v1.8: a merge may name the entry's own causes
            cause_ids = tuple(dict.fromkeys([*cause_ids, *(i for m in self.focus().merge for i in m["cause_ids"])]))
        return VerifyContext(parent=self.package, seed=self.a.package(self.a.seed_release()), core=self.a.k,
                             objects=self.a.objects, worker=self.a.worker(), suite=self.a.suite,
                             evidence=self.evidence, forbidden=forbidden_of(literal_index(rows)), train_tasks=train,
                             train_texts={t: self.a.suite.tasks[t].trigger for t in train}, embed=embed,
                             settings=self.s, fixture_task=(failed or passed or list(train))[0],
                             failed_sessions={t: reading[t].evidence or reading[t].attempt_id for t in failed},
                             passed_sessions={t: reading[t].evidence or reading[t].attempt_id
                                              for t in passed[:int(self.s.replay_sample)]},
                             study_tasks=study, cause_ids=cause_ids,
                             aux=self.researcher("verify"), hook_aux_calls=int(self.s.hook_aux_calls),
                             merge_sessions={e.id: dict(e.fixed_sessions)
                                             for e in self.frontier.active(self.line)})     # v1.9

    def _archive(self, *args, **kw) -> dict:
        return self.archive.put(self.i, *args, tag=self.tag, **kw)

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
        # v1.5 (DESIGN §6.15): the studied failures side by side, the persistent ones first, by cause
        failures = []
        for c in self.causes():
            for ref in c.cases:
                case = dg.Case.from_json(self.a.objects.get(ref))
                if case.task_id not in study:
                    continue
                row = next(iter(self._rows([case.attempt_id])), None)
                if row is None:
                    continue
                n = self.evidence.persistence(case.task_id)
                failures.append({**self.evidence.digest(self.evidence.of(row), chars=self.s.failures_case_chars),
                                 "cause": c.name, "cause_id": c.cause_id, "note": case.explanation,
                                 "persistence": n, "persistent": n >= 2})
        failures.sort(key=lambda f: (not f["persistent"], -f["persistence"]))
        failures = failures[:int(self.s.failures_max_cases)]
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
        regressions, research_sessions = self._regressions_input(held)
        readable |= research_sessions
        files = {"focus.json": {"ranked": focus_rows, "require_code": f.require_code, "l5_suggested": f.l5_suggested,
                                "l6_suggested": f.l6_suggested, "notes": list(f.notes), "merge": list(f.merge)},
                 "regressions.json": regressions, "frontier.json": self._frontier_input(held),
                 "causes.json": cause_rows, "successes.json": successes,
                 "ledger.json": self.ledger.projection(line=self.line, hide=held),
                 "scoreboard.json": self.ledger.scoreboard(),
                 "histogram.json": dev_histogram(self.a, self.base.package_ref), "base_tools.json": base_tools,
                 "failures.json": {"note": "each studied failure side by side: the request, the checks the grader marked "
                                           "failed with their parameters, the agent's writes in order, what it said at "
                                           "the end; persistent = failed under more than one installed package",
                                   "cases": failures},
                 "audits.json": {"note": "every audit (a hook calling the model) tried on this line: its dry run on "
                                         "recorded attempts — hits on the claimed failing tasks, false alarms on passing "
                                         "ones with what the audit said — and its screen when it got there; start the "
                                         "next audit from these errors",
                                 "audits": self.ledger.audits(self.line)}}
        out = {name: json.dumps(v, ensure_ascii=False, indent=1, default=str) for name, v in files.items()}
        out["playbook.md"] = read_playbook(self.a.root)
        out["package_api.md"] = api
        return out, readable

    # ------------------------------------------------------------------ v1.8: what R reads of regressions, the frontier
    def _regression_view(self, ref: str) -> tuple[dict, set]:
        """One package's regression evidence as R reads it at K's level: the counts; the de-identified records at
        `abstract` and above; the research sample with the two sessions of each regressed studied task at `research`.
        -> (the view, the sessions R may read)."""
        obj = self.a.objects.get(ref)
        view = {"round": obj.get("round_id"), "dir": obj.get("dir"), "kind": obj.get("kind"),
                "mechanism": obj.get("mechanism"), "fixes": obj.get("fixes"), "regressions": obj.get("regressions"),
                "heuristic_terms": obj.get("heuristic_terms") or {}}
        sessions: set = set()
        if self.s.evidence_at_least("abstract"):
            view["evidence"] = [{k: v for k, v in r.items() if k != "ref"} for r in obj.get("records") or ()]
            # v1.9: why the mechanism worked, one record per confirmed fix
            view["fixes_evidence"] = [{k: v for k, v in r.items() if k != "ref"}
                                      for r in obj.get("fixes_evidence") or ()]
        if self.s.evidence_at_least("research"):
            research = obj.get("research") or {}
            view["research"] = {"sample": list(research.get("sample") or ()),
                                "regressed": [{"task_id": t, **s} for t, s in (research.get("sessions") or {}).items()]}
            sessions = {s for pair in (research.get("sessions") or {}).values() for s in pair.values()}
        return view, sessions

    def _regressions_input(self, held) -> tuple[dict, set]:
        """inputs/regressions.json: the regression evidence of this line's screened packages, the newest first
        (REGRESSIONS_SHOWN at most); held-out task ids never appear (the records carry none; a text is scrubbed)."""
        rows = [r for r in self.ledger.rows(stage="screened") if r.get("line") in (None, self.line)
                and r.get("regression_ref")]
        packages, sessions = [], set()
        for r in reversed(rows[-REGRESSIONS_SHOWN:]):
            view, read = self._regression_view(r["regression_ref"])
            view.update(accepted=bool(r.get("accepted")), partial=bool(r.get("partial")))
            packages.append(view)
            sessions |= read
        level = self.s.regression_evidence
        note = {"counts": "each earlier package of this line that broke held-out tasks it had passed: how many it "
                          "fixed and broke",
                "abstract": "each earlier package of this line that broke held-out tasks: one de-identified record per "
                            "confirmed regression — the assertion kinds that failed, the components of the package that "
                            "acted on that attempt with what they said, the agent's writes before and after by route "
                            "(no values), the termination; no task id, no request",
                "research": "each earlier package of this line that broke held-out tasks: one de-identified record per "
                            "confirmed regression (the assertion kinds that failed, what the package's components said "
                            "on that attempt, the writes before and after by route) and `research`: studied tasks the "
                            "working line passes, run once under the package — the regressed ones name two sessions "
                            "you can read_trajectory, the working line's and the package's"}[level]
        out = {"note": note + ". A regression is the package acting where the task's own documents asked for the "
                       "opposite: narrow the mechanism's condition to what the documents say, do not try another "
                       "global rule", "level": level, "packages": packages}
        return scrub(out, held), sessions

    def _frontier_input(self, held) -> dict:
        """inputs/frontier.json: this line's active frontier entries — each one's diff against the working line it was
        built on, its mechanism and components, the counts of confirmed fixes and regressions, its regression evidence
        and how often it was merged."""
        entries = []
        for e in self.frontier.active(self.line):
            regressions = self._regression_view(e.regression_ref)[0] if e.regression_ref else None
            entries.append(self.frontier.for_prompt(
                e, base_files=load_package(self.a.objects, e.base_package_ref).files,
                files=self.a.objects.get(e.files_ref), declaration=self.a.objects.get(e.declaration_ref),
                regressions=regressions, hide=held, scrub=lambda v: scrub(v, held)))
        return {"note": "packages of earlier rounds that fixed held-out tasks and were rejected only because they broke "
                        "others (confirmed both ways). focus.json's `merge` names the one(s) to merge this round: hand "
                        "in a conditioned version of the same mechanism in candidates/<candidate_dir>/, its trigger "
                        "narrowed so it does not act on the regression shapes, declared with merge_of",
                "entries": entries}

    def _declared_row(self, decl) -> None:
        """The declaration on the ledger (once: a row written again is the row on record)."""
        self.ledger.row("declared", round_id=self.round_id, line=self.line, dir=decl.dir, mechanism=decl.mechanism,
                        kind=decl.kind, site=decl.site, cause=list(decl.causes), trigger=decl.mechanism,
                        components=[{"kind": c.kind, "site": c.site, "cause_ids": list(c.cause_ids),
                                     "uses_model": c.uses_model} for c in decl.components],
                        predicted_affected=list(decl.predicted_affected), differs_from=list(decl.differs_from))

    def _declared(self, decl, files: dict, verdict, trial: dict | None = None) -> dict:
        self._declared_row(decl)
        return {"dir": decl.dir, "declaration": self.a.objects.put(decl),
                "components": [c.key for c in decl.components],
                "files": self.a.objects.put_json("candidate_files", files), "verdict": self.a.objects.put(verdict),
                "trial": trial}

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
                verdict = run_ladder(files, decl, vctx)
                trial = item.get("trial")
                if trial is None and verdict.passed() and not decl.fixed and int(self.s.trial_max_tasks) > 0:
                    # killed in the trial: its attempts are on record, the revision is not; one trial, no revision
                    res = self._trial_once(decl, files, iteration=0)
                    trial = self._trial_summary([res])
                out.append(self._declared(decl, files, verdict, trial))
            self.out(f"round      {self.round_id}: the session's {len(out)} packages, as it left them")
            return {"candidates": out, "session": saved["session"]}
        inputs, readable = self.inputs()
        kinds = [k for k in self.a.k["harness"]["kinds"] if k not in ("memory", "model", "test")] + ["removal"]
        merge_ids = self.merge_ids()
        system = (PROMPTS / "propose.md").read_text(encoding="utf-8")
        merge_text = ""
        if merge_ids:
            picks = self.focus().merge
            merge_text = (
                "\nThis round also merges from the frontier (inputs/frontier.json): "
                + "; ".join(f"entry {m['entry']} ({m['kind']}, {m['fixes']} confirmed fixes, {m['regressions']} "
                            f"confirmed regressions, merged {m['attempts']} time(s) before) -> candidates/"
                            f"{m['candidate_dir']}/" for m in picks)
                + ". One of your packages is that entry's conditioned version: the same mechanism and components, "
                  "its trigger narrowed so it no longer acts on the regression shapes the evidence shows (it acts "
                  "only when the task's own documents or request ask for it; a tool plus a hook with a predicate "
                  "where a hook alone fired on everything), declared with \"merge_of\": \"<entry id>\" and the "
                  "entry's cause_ids from focus.json's merge item. It is verified and screened like any package; "
                  "accepted, it is installed. Do not hand the entry in again as it is: it has a confirmed "
                  "regression.\n")
        for name, value in {"packages": self.s.packages_per_round, "max_turns": self.s.session_max_turns,
                            "max_edits": self.s.session_max_edits, "max_read": self.s.session_max_read_bytes,
                            "max_read_total": self.s.session_max_read_total, "l5_per_round": self.s.l5_per_round,
                            "hook_aux_calls": self.s.hook_aux_calls,
                            "max_tokens": int(self.a.k["research_model"]["max_tokens"]),
                            "replay_max_false_trigger": f"{float(self.s.replay_max_false_trigger):.0%}",
                            "accept_min_whole_fixes": self.s.accept_min_whole_fixes,
                            "heuristic_terms_min": self.s.heuristic_terms_min, "merge": merge_text,
                            "trial_max_tasks": self.s.trial_max_tasks, "trial_max_rounds": self.s.trial_max_rounds,
                            "trial_min_fixes": self.s.trial_min_fixes}.items():
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
                decl, why = read_declaration({**raw, "dir": dir_name}, dirs=[dir_name], kinds_allowed=kinds,
                                             merge_ids=merge_ids)
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
                                    readable=readable, kinds=kinds, merge_ids=merge_ids)
            result = self._session(session, tree)
            fetched = []
            for decl in result.declarations:
                files, error = tree.fetch(decl.dir)
                if files is not None:
                    fetched.append((decl, self.with_memory(files)))
                    self._declared_row(decl)         # on the ledger before its trials (v1.9)
            # v1.6 (DESIGN §6.16): the focus names several causes and R handed in one package — on the ledger
            if len(fetched) == 1 and len(self.focus().ranked) >= 2 and self.s.packages_per_round > 1:
                self.ledger.row("single_package", round_id=self.round_id, line=self.line, dir=fetched[0][0].dir,
                                causes=len(self.focus().ranked), reason=str(session.ended or "")[:300])
            # on record before anything else can fail: a resumed step does not pay for the session again
            self.state["session_result"] = {"session": str(self.dir / "session.json"), "candidates": [
                {"declaration": self.a.objects.put(d), "files": self.a.objects.put_json("candidate_files", f)}
                for d, f in fetched]}
            self.state.pop("session_checkpoint", None)
            self._save()
            if session.ended:        # v1.9: a session at a cap, on the ledger
                self.ledger.row("session_ended", round_id=self.round_id, line=self.line,
                                reason=str(session.ended)[:300], turns=session.turns, edits=session.edits,
                                declared=len(result.declarations))
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
                        self._save_candidate(decl, files)
                # v1.9: the in-sample trial, R's revisions, and the summary the verified step gates on
                files, verdict, trial = self._trials(session, tree, decl, files, verdict, vctx)
                out.append(self._declared(decl, files, verdict, trial))
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

    def _save_candidate(self, decl: Declaration, files: dict, trial: dict | None = None) -> None:
        """The session's record of one candidate directory as it stands now (a resumed step reads it)."""
        items = self.state["session_result"]["candidates"]
        for item in items:
            if Declaration.from_json(self.a.objects.get(item["declaration"])).dir == decl.dir:
                item["files"] = self.a.objects.put_json("candidate_files", files)
                if trial is not None:
                    item["trial"] = trial
        self._save()

    # ------------------------------------------------------------------ v1.9: the in-sample trial
    def _trial_once(self, decl: Declaration, files: dict, *, iteration: int) -> TrialResult:
        """One trial of a package (IMPROVER §9.6): the studied failing tasks it claims, priced against what the round
        has left, run once under the candidate (role `trial`, the round's trial account). Attempts on record under
        the same package are read, not paid."""
        name = f"{self.tag}:{decl.dir}"
        reading = self.reading()
        study_failing = [t for t in self.split()[0] if t in reading and not reading[t].success]
        room = int(self.s.trial_max_attempts) - self._trial_used()
        tasks = trial_tasks(decl, causes=self.causes(), study_failing=study_failing,
                            n=min(int(self.s.trial_max_tasks), max(room, 0)))
        base = dict(candidate=name, iteration=int(iteration))
        if not tasks:
            return TrialResult(**base, outcome="not_run",
                               reason="no studied failing task it claims" if room > 0 else
                               f"the round's {self.s.trial_max_attempts} trial attempts are used")
        try:
            package = build(self.a.objects, files, limits=Limits.from_core(self.a.k),
                            seed=self.a.package(self.a.seed_release()), parent=self.package)
        except PackageError as e:
            return TrialResult(**base, tasks=tuple(tasks), outcome="invalid", reason=f"build: {e}"[:300])
        to_pay = max(len(tasks) - self._recorded(name, "trial", package.ref()), 0)
        if to_pay:
            usd, why = price(self.a, self.work_ref, screen_attempts=to_pay)
            if usd is None:
                return TrialResult(**base, package_ref=package.ref(), tasks=tuple(tasks), outcome="not_run",
                                   reason=f"unpriced: {why}")
            need, left = self._need(usd)
            if left is not None and need > left + 1e-9:
                return TrialResult(**base, package_ref=package.ref(), tasks=tuple(tasks), outcome="budget",
                                   reason=f"the trial would need up to ${need:.2f} and the round has ${left:.2f}")
        comps = components(self.package, files)
        conditions = [{"kind": "prompt", "name": "*"}] if any(c.rewrite for c in decl.components) \
            else condition(comps)
        res = run_trial(self.a, run_arm=self.run_arm, name=name, package=package, base_release=self.base, tasks=tasks,
                        evidence=self.evidence, conditions=conditions, account=self.child("trial"),
                        retries=int(self.a.k["gate"]["max_infrastructure_retries"]), iteration=iteration)
        if res.outcome == "budget":
            raise Stop("budget: the budget refused a trial attempt")
        return res

    def _trialed_row(self, decl: Declaration, res: TrialResult) -> None:
        self.ledger.row("trialed", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                        iteration=res.iteration, outcome=res.outcome, trial_tasks=list(res.tasks),
                        trial_fixed=list(res.fixed), trial_failed=list(res.failed), fixes=len(res.fixed),
                        attempts=res.attempts, reason=res.reason,
                        passed=(len(res.fixed) >= int(self.s.trial_min_fixes)) if res.outcome == "ok" else None)

    def _trial_summary(self, results: list) -> dict:
        """What the implemented step keeps of a package's trials: the iterations, the attempts, whether a trial read
        the package (`ran`), the latest reading's fixes and whether they reach trial_min_fixes (`passed`; None when
        no trial read it, which opens no gate)."""
        read = [r for r in results if r.outcome == "ok"]
        last = read[-1] if read else None
        return {"iterations": [{"iteration": r.iteration, "outcome": r.outcome, "tasks": list(r.tasks),
                                "fixed": list(r.fixed), "failed": list(r.failed), "attempts": r.attempts,
                                "reason": r.reason, "trial": self.a.objects.put(r)} for r in results],
                "attempts": sum(r.attempts for r in results), "ran": last is not None,
                "fixes": len(last.fixed) if last else 0,
                "passed": (len(last.fixed) >= int(self.s.trial_min_fixes)) if last else None}

    def _trials(self, session: CodingSession, tree: WorkTree, decl: Declaration, files: dict, verdict: Verdict,
                vctx: VerifyContext) -> tuple[dict, Verdict, dict | None]:
        """The in-sample trial of one package and R's revisions (DESIGN §6.19; IMPROVER §9.6): the package that passed
        its free rungs runs on the studied failing tasks it claims; a trial that fixed fewer than trial_min_fixes
        comes back to the session with every task's outcome and session, R edits and finishes again, the ladder
        and the trial run again — trial_max_rounds revisions at most, within the round's trial_max_attempts. A trial
        that fixed enough does not go back (the session's cost is R's). -> (files, verdict, the summary)."""
        if decl.fixed or int(self.s.trial_max_tasks) <= 0 or not verdict.passed():
            return files, verdict, None
        results: list = []
        revisions = int(self.s.trial_max_rounds)
        iteration = 0
        while True:
            res = self._trial_once(decl, files, iteration=iteration)
            self._trialed_row(decl, res)
            results.append(res)
            self._save_candidate(decl, files, self._trial_summary(results))
            self.out(f"round      {self.round_id}: trial of {decl.dir} (iteration {iteration}): {res.outcome}"
                     + (f", {len(res.fixed)} fixed of {len(res.tasks)} tried" if res.outcome == "ok" else
                        f" ({res.reason})"))
            if res.outcome != "ok" or len(res.fixed) >= int(self.s.trial_min_fixes):
                break
            left = int(self.s.trial_max_attempts) - self._trial_used()
            if revisions <= 0 or left <= 0 or session.ended:
                break
            text = trial_feedback(res, evidence=self.evidence, activity=self.a, revisions_left=revisions,
                                  attempts_left=left, min_fixes=int(self.s.trial_min_fixes))
            session.readable |= {s for s in res.sessions.values() if s}      # studied tasks: R may read them
            session.revise(text)
            revisions -= 1
            iteration += 1
            again, _ = tree.fetch(decl.dir)
            if again is None:
                break
            files = self.with_memory(again)
            verdict = run_ladder(files, decl, vctx)
            self._save_candidate(decl, files)
            if not verdict.passed() and revisions > 0 and not session.ended:
                session.revise(f"candidates/{decl.dir} failed its check after the revision:\n{verdict.text()}\nEdit "
                               f"its files and call finish again with the same declaration.")
                revisions -= 1
                again, _ = tree.fetch(decl.dir)
                if again is not None:
                    files = self.with_memory(again)
                    verdict = run_ladder(files, decl, vctx)
                    self._save_candidate(decl, files)
            if not verdict.passed():
                break
        return files, verdict, self._trial_summary(results)

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
            self._archive(decl.dir, files, decl, verdict, line=self.line)
            for rung, ok in verdict.rungs().items():
                ladder[rung] = ladder.get(rung, 0) + bool(ok)
            replays = [c["V2b"] for c in verdict.components.values() if isinstance(c, dict) and "V2b" in c]
            terms = (verdict.steps.get("V0") or {}).get("heuristic_terms") or {}
            loose = (verdict.steps.get("V0") or {}).get("unregistered_hooks") or {}
            # v1.9: a package whose trial read it and fixed too little in-sample is not screened
            trial = item.get("trial") or {}
            no_fix = bool(trial.get("ran")) and trial.get("passed") is False
            self.ledger.row("verified", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                            passed=verdict.passed(), outcome="duplicate" if dup else "no_trial_fix" if no_fix else "",
                            failed_components=verdict.failed_components(), reason=verdict.text()[:800],
                            **({"false_trigger_rate": max(float(x.get("false_trigger_rate") or 0) for x in replays)}
                               if replays else {}), **({"heuristic_terms": terms} if terms else {}),
                            **({"unregistered_hooks": loose} if loose else {}),
                            **({"merge_of": decl.merge_of} if decl.merge_of else {}),
                            **({"trial_fixed": list(trial["iterations"][-1]["fixed"]),
                                "attempts": trial.get("attempts")} if trial.get("iterations") else {}))
            self._audited(decl, verdict)
            if verdict.passed() and not dup and not no_fix:
                passed.append(item)
            elif no_fix:
                self.out(f"round      {self.round_id}: {decl.dir} fixed {trial.get('fixes')} of its trial tasks "
                         f"in-sample (trial_min_fixes {self.s.trial_min_fixes}): not screened")
        for decl, files in self.fixed_candidates():
            vctx = vctx or self.vctx()
            verdict = run_ladder(files, decl, vctx)
            self.ledger.row("declared", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                            site=decl.site, source=decl.fixed, cause=list(decl.causes))
            if "removal" in decl.kinds:
                self.ledger.row("removal_proposed", round_id=self.round_id, line=self.line, dir=decl.dir,
                                site=decl.site, reason=decl.mechanism)
            self._archive(decl.dir, files, decl, verdict, line=self.line)
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

    def _audited(self, decl: Declaration, verdict: Verdict) -> None:
        """v1.6 (DESIGN §6.16): each audit's dry run on the ledger — hits, false alarms with the audit's own words on
        the studied tasks, what it named — so the next audit starts from this one's errors (Ledger.audits)."""
        for comp in decl.components:
            got = (verdict.components.get(comp.key) or {}).get("V2d")
            if not got:
                continue
            self.ledger.row("audited", round_id=self.round_id, line=self.line, dir=decl.dir, kind="hook", site=comp.site,
                            passed=bool(got.get("passed")), hits=list(got.get("hits") or ()),
                            named=list(got.get("named") or ()), false_alarms=list(got.get("false_alarms") or ()),
                            false_alarm_rate=got.get("false_alarm_rate"), misfires=list(got.get("misfires") or ()),
                            reason=str(got.get("detail") or "")[:800])

    # ------------------------------------------------------------------ V3: the held-out screen
    def screened(self) -> dict:
        from ..evaluate.gate import transport_problem
        problem = transport_problem(self.a)
        if problem:
            raise Stop(problem)
        reading = self.reading()
        held = self.screen_holdout()
        best_so_far = self.best_so_far()
        items = list(self.state["steps"]["verified"]["passed"])
        decls = {i["dir"]: Declaration.from_json(self.a.objects.get(i["declaration"])) for i in items}
        # R's package first, then the fixed code's (the snapshot last); each is paid while it fits what the round has
        # left beside the screens before it (a resumed step re-reads its attempts for free)
        items.sort(key=lambda i: (bool(decls[i["dir"]].fixed), "memory" in decls[i["dir"]].kinds))
        allowance = confirm_allowance(len(held), self.s)
        # v1.9: the research sample is priced after the screen, for the regressing packages only (priced for every
        # package up front, it squeezed finance-v8's third package out of the round)
        to_pay = lambda chosen: sum(max(len(held) + allowance  # noqa
                                        - self._recorded(f"{self.tag}:{i['dir']}", "screen")
                                        - self._recorded(f"{self.tag}:{i['dir']}", "confirm"), 0) for i in chosen)
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
            # v1.8: a rewrite of existing text is screened as a whole-footprint package
            conditions = [{"kind": "prompt", "name": "*"}] if any(c.rewrite for c in decl.components) \
                else condition(comps)
            res = screen(self.a, run_arm=self.run_arm, name=f"{self.tag}:{decl.dir}", package=package,
                         base_release=self.base, holdout=held, reading=reading, evidence=self.evidence,
                         conditions=conditions, account=self.child("screen"),
                         retries=int(self.a.k["gate"]["max_infrastructure_retries"]), best_so_far=best_so_far,
                         settings=self.s)
            if res.outcome == "budget_exhausted":
                raise Stop("budget: the budget refused a screen attempt")
            if decl.merge_of:        # v1.9: how many of the entry's fixes the merge kept
                entry = self.frontier.get(decl.merge_of)
                res = replace(res, kept_fixes=len(set(res.fixed) & set(entry.fixed)) if entry is not None else None)
            res = replace(self._regressions(decl, item, res), merge_of=decl.merge_of)
            results.append(res)
            rows.append({"dir": decl.dir, "screen": self.a.objects.put(res), "package": package.ref()})
            self._archive(decl.dir, files, decl, Verdict.from_json(self.a.objects.get(item["verdict"])),
                             screen=res, package_ref=package.ref(), line=self.line)
            self.ledger.row("screened", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                            cause=list(decl.causes), predicted_affected=list(decl.predicted_affected),
                            fixed=list(res.fixed), regressed=list(res.regressed), net_flips=res.net_flips,
                            fixes=len(res.fixed), flipped_fixed=list(res.flipped_fixed),
                            flipped_regressed=list(res.flipped_regressed), noisy=list(res.noisy),
                            unconfirmed=list(res.unconfirmed), confirm_attempts=res.confirm_attempts,
                            holdout_score=res.holdout_score, best_so_far=res.best_so_far,
                            outcome=res.outcome, accepted=res.accepted, reasons=list(res.reasons),
                            reason=res.reason, partial=res.partial, regression_ref=res.regression_ref,
                            research_sample=len(res.research_sample),
                            research_regressed=list(res.research_regressed),
                            unconditioned_regressed=list(res.unconditioned_regressed), acted=len(res.acted),
                            footprint_share=res.footprint_share,            # v1.9
                            **({"kept_fixes": res.kept_fixes} if res.kept_fixes is not None else {}),
                            **({"source": decl.fixed} if decl.fixed else {}),
                            **({"merge_of": decl.merge_of} if decl.merge_of else {}))
            if res.outcome == "screened" and not decl.fixed:
                self._observe(decl, res, cost=(res.cost or 0.0) * len(res.holdout))
        self._frontier_after_screens(chosen, decls, results)
        best = pick_best(results)
        picked = next((r for r, res in zip(rows, results) if res is best), None) if best is not None else None
        for r, res in zip(rows, results):
            if res.accepted and picked is not None and r is not picked:
                decl = decls[r["dir"]]
                self.ledger.row("screened", round_id=self.round_id, line=self.line, dir=r["dir"], kind=decl.kind,
                                outcome="not_selected", reason=f"accepted, and {picked['dir']} is installed this round",
                                **({"source": decl.fixed} if decl.fixed else {}))
        self.out(f"round      {self.round_id}: screened " + ", ".join(
            f"{r['dir']} net {res.net_flips} score {res.holdout_score}/{len(res.holdout)}, confirmed "
            f"{len(res.fixed)}/{len(res.flipped_fixed)} fixes {len(res.regressed)}/{len(res.flipped_regressed)} "
            f"regressions{f', noisy {len(res.noisy)}' if res.noisy else ''}"
            f"{f', {len(res.unconditioned_regressed)} broke with no footprint' if res.unconditioned_regressed else ''}"
            f", acted on {len(res.acted)}/{len(res.holdout)}"
            f"{f', kept {res.kept_fixes} of the entry\'s fixes' if res.kept_fixes is not None else ''} "
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

    # ------------------------------------------------------------------ v1.8: regression evidence, the frontier
    def _regressions(self, decl: Declaration, item: dict, res: ScreenResult) -> ScreenResult:
        """A screened package with confirmed regressions: its evidence at K's level (screen.regression_evidence; the
        research level runs the sample of studied passing tasks under the candidate), stored as an object the
        `screened` row and the frontier entry reference, and written to rounds/<tag>/regressions.json."""
        if res.outcome != "screened" or not res.regressed or decl.fixed:
            return res
        changed = [c for c in components(self.package, self.a.objects.get(item["files"])) if c["change"] != "deleted"]
        tools = [c["name"] for c in changed if c["kind"] == "tool"]
        hooks = [c["name"] for c in changed if c["kind"] == "hook"]
        records = regression_evidence(self.a, res, evidence=self.evidence, base_package_ref=self.work_ref,
                                      tool_names=tools, hook_names=hooks) \
            if self.s.evidence_at_least("abstract") else []
        # v1.9: why the mechanism worked, one record per confirmed fix — the frontier hands it to the merge
        fixes = fix_evidence(self.a, res, evidence=self.evidence, base_package_ref=self.work_ref, tool_names=tools,
                             hook_names=hooks) if self.s.evidence_at_least("abstract") else []
        research = {"sample": [], "regressed": [], "sessions": {}, "outcome": "not_run"}
        if self.s.evidence_at_least("research") and int(self.s.regression_research_sample) > 0:
            verdict = Verdict.from_json(self.a.objects.get(item["verdict"]))
            fired = [t for rungs in verdict.components.values() for t in (rungs.get("V2b") or {}).get("false_triggers")
                     or ()]
            reading = self.reading()
            sample = research_sample(self.split()[0], reading, fired=fired, n=self.s.regression_research_sample,
                                     seed=res.candidate)
            # v1.9: priced now, for this package alone, against what the round has left after its screens
            to_pay = max(len(sample) - self._recorded(res.candidate, "research", res.package_ref), 0)
            usd, why = price(self.a, self.work_ref, screen_attempts=to_pay) if to_pay else (0.0, "")
            need, left = self._need(usd) if usd is not None else (None, None)
            if usd is None or (left is not None and need > left + 1e-9):
                research = {"sample": [], "regressed": [], "sessions": {}, "outcome": "budget",
                            "reason": why or f"the sample of {len(sample)} would need up to ${need:.2f} and the round "
                                             f"has ${left:.2f}"}
                self.out(f"round      {self.round_id}: no research sample for {decl.dir}: {research['reason']}")
            else:
                got = research_run(self.a, run_arm=self.run_arm, name=res.candidate,
                                   package=load_package(self.a.objects, res.package_ref), base_release=self.base,
                                   sample=sample, reading=reading, account=self.child("screen"),
                                   retries=int(self.a.k["gate"]["max_infrastructure_retries"]))
                if got["outcome"] == "budget_exhausted":
                    raise Stop("budget: the budget refused a research-sample attempt")
                research = {k: got[k] for k in ("sample", "regressed", "sessions", "outcome")}
        terms = (Verdict.from_json(self.a.objects.get(item["verdict"])).steps.get("V0") or {}).get("heuristic_terms")
        obj = {"candidate": res.candidate, "round_id": self.round_id, "dir": decl.dir, "kind": decl.kind,
               "mechanism": decl.mechanism, "level": self.s.regression_evidence, "fixes": len(res.fixed),
               "regressions": len(res.regressed), "records": [r.to_json() for r in records],
               "fixes_evidence": [r.to_json() for r in fixes],
               "research": research, "heuristic_terms": terms or {}}
        ref = self.a.objects.put_json("regression_evidence_set", obj)
        path = self.dir / "regressions.json"
        file = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"packages": []}
        file["packages"] = [p for p in file["packages"] if p.get("dir") != decl.dir] + [{**obj, "ref": ref,
                                                                                        "regressed": list(res.regressed)}]
        write_json(path, file)
        return replace(res, regression_ref=ref, research_sample=tuple(research["sample"]),
                       research_regressed=tuple(research["regressed"]))

    def _entry_of(self, decl: Declaration, item: dict, res: ScreenResult) -> FrontierEntry:
        verdict = Verdict.from_json(self.a.objects.get(item["verdict"]))
        return FrontierEntry(id="", round_index=self.i, tag=self.tag, dir=decl.dir, line=self.line,
                             files_ref=item["files"], declaration_ref=item["declaration"],
                             base_package_ref=self.work_ref, fixed=res.fixed, regressed=res.regressed,
                             regression_ref=res.regression_ref, screen_ref=self.a.objects.put(res),
                             mechanism=decl.mechanism, kind=decl.kind,
                             heuristic_terms=(verdict.steps.get("V0") or {}).get("heuristic_terms") or {},
                             fixed_sessions=fixed_sessions(self.a, res))        # v1.9: what a merge is replayed on

    def _frontier_after_screens(self, items: list, decls: dict, results: list) -> None:
        """The frontier's in and out after the screens (DESIGN §6.18): a partial package of R's goes on it (ledger
        `partial`; the weakest beyond frontier_max dropped, `frontier_pruned`); a rejected merge counts one more
        attempt on its entry — a partial one takes the entry's place — and at frontier_max_attempts the entry is
        `abandoned`. An accepted merge is marked `merged` when it is installed (accepted, stacked)."""
        by_dir = {i["dir"]: i for i in items}
        for res in results:
            name = res.candidate.rpartition(":")[2]
            decl, item = decls[name], by_dir[name]
            if decl.fixed or res.outcome != "screened":
                continue
            if decl.merge_of:
                if res.accepted:
                    continue
                replacement = self._entry_of(decl, item, res) if res.partial else None
                entry, outcome = self.frontier.failed(decl.merge_of, round_id=self.round_id, dir_name=decl.dir,
                                                      replacement=replacement)
                if outcome == "abandoned":
                    self.ledger.row("abandoned", round_id=self.round_id, line=self.line, dir=decl.dir,
                                    entry=decl.merge_of, attempts=entry.attempts,
                                    reason=f"its merges were rejected {entry.attempts} time(s) "
                                           f"(frontier_max_attempts {self.s.frontier_max_attempts})")
                elif outcome == "replaced":
                    self.ledger.row("partial", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                                    entry=decl.merge_of, fixes=len(res.fixed), regressed=list(res.regressed),
                                    attempts=entry.attempts, reason="a rejected merge, itself partial, takes the "
                                                                    "entry's place")
                if outcome in ("abandoned", "retried", "replaced"):
                    self.out(f"round      {self.round_id}: merge {decl.dir} of {decl.merge_of} rejected: the entry is "
                             f"{outcome} (attempts {entry.attempts})")
            elif res.partial:
                entry, dropped = self.frontier.add(self._entry_of(decl, item, res))
                self.ledger.row("partial", round_id=self.round_id, line=self.line, dir=decl.dir, kind=decl.kind,
                                entry=entry.id, fixes=len(res.fixed), regressed=list(res.regressed),
                                reason="rejected only for its confirmed regressions: on the frontier for a merge")
                for d in dropped:
                    self.ledger.row("frontier_pruned", round_id=self.round_id, line=self.line, entry=d,
                                    reason=f"dropped: the frontier keeps {self.s.frontier_max} entries")
                self.out(f"round      {self.round_id}: {decl.dir} is partial ({len(res.fixed)} fixes, "
                         f"{len(res.regressed)} regressions): frontier entry {entry.id}"
                         + (f"; dropped {dropped}" if dropped else ""))

    def _merged(self, dir_name: str, decl: Declaration, release_ref: str) -> None:
        if not decl.merge_of:
            return
        self.frontier.merged(decl.merge_of, round_id=self.round_id, dir_name=dir_name)
        self.ledger.row("merged", round_id=self.round_id, line=self.line, dir=dir_name, entry=decl.merge_of,
                        release=release_ref)
        self.out(f"round      {self.round_id}: frontier entry {decl.merge_of} merged by {dir_name}")

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
        self._archive(sc["best"], self.a.objects.get(item["files"]), decl,
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
            self._merged(sc["best"], decl, done.target_release)
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
            self._archive(name, files, decl, verdict, screen=stacked_res,
                             receipt_ref=self.a.objects.put(done), package_ref=package.ref(), line=self.line)
            self.ledger.row("accepted", round_id=self.round_id, line=self.line, dir=name, kind=decl.kind,
                            release=line_ref, net_flips=res.net_flips, holdout_score=res.holdout_score,
                            best_so_far=res.best_so_far, stacked_on=stacked_res.stacked_on)
            self.ledger.row("installed", round_id=self.round_id, line=self.line, dir=name, release=line_ref,
                            stacked_on=stacked_res.stacked_on)
            stacked.append(name)
            self.out(f"round      {self.round_id}: {name} stacked on the working line as {short(line_ref)} "
                     f"(its own screen: net {res.net_flips}, {res.holdout_score}/{len(res.holdout)})")
            self._merged(name, decl, line_ref)
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
        parent_reading = {t: r for t, r in self.reading().items() if t in held}
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
        # v1.8: frontier entries whose fixes the working line passes every one of by now leave it
        line_ref = (self.state["steps"].get("stacked") or {}).get("line") or new.ref()
        line_reading = self.evidence.reading(self.a.releases.load(line_ref).package_ref)
        passes = {t for t, r in (line_reading or child_reading).items() if r.success}
        for entry_id in self.frontier.prune(self.line, passes):
            self.ledger.row("frontier_pruned", round_id=self.round_id, line=self.line, entry=entry_id,
                            reason="subsumed: the working line passes every held-out task it fixed")
            self.out(f"round      {self.round_id}: frontier entry {entry_id} subsumed by the working line")
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


def run_round(activity, index: int, *, run_arm, out=print, pool=None) -> RoundResult:
    """One round; with `pool` (the stream's scored tasks, in its order), the stream's round over them (§3.1)."""
    return Round(activity, index, run_arm=run_arm, out=out, pool=pool).run()
