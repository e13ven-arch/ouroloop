"""M5: the shared execution state machine and the total budget (docs/modules/05_EXECUTION_BUDGET.md).

Three records that a single `promoted` boolean would have collapsed, and that mean different things:

    the state         how far this experiment got
    EvaluationReceipt what the evidence says
    InstallationReceipt whether it is in effect

Everything paid for goes through one book. A leaf operation reserves an upper bound before the call and settles
against the measurement afterwards; ancestors are the sum of their subtree, so entering a nested arm cannot reset
anything and two arms of one comparison cannot between them spend more than their parent has. A response that was
lost keeps its worst-case bound occupying the budget until someone confirms what happened: an unknown call is not
a free call.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import threading

from ..ledger import locked, new_id, now, write_json
from . import contracts as ct

# ---------------------------------------------------------------------------- states
STATES = ("drafted", "admitted", "building", "screening", "evaluating", "evaluated", "confirming", "accepted",
          "installing", "installed", "rejected", "inconclusive", "invalid", "screened_out", "installation_failed",
          "stale_parent", "cancelled")
TERMINAL = frozenset({"installed", "rejected", "inconclusive", "invalid", "screened_out", "installation_failed",
                      "stale_parent", "cancelled"})
# Where each state may go. No necessary preflight is skippable, and nothing walks back out of a terminal state.
# `screening` and `screened_out` were the v1 dev screen's (removed in P1 step 6): no step enters them now; they stay
# so an older experiment log still reads, and one left in `screening` resumes at `evaluating`. `confirming` (M5 §1,
# v0.3) sits between a provisional `evaluated` — the first reading's verdict, written with its numbers and no
# receipt — and the final `evaluated` that carries the receipt: the candidate arm is read again there, under the
# same frozen plan.
NEXT: dict[str, frozenset] = {
    "drafted": frozenset({"admitted", "invalid", "cancelled"}),
    "admitted": frozenset({"building", "invalid", "inconclusive", "cancelled"}),
    "building": frozenset({"screening", "evaluating", "invalid", "inconclusive", "cancelled"}),
    "screening": frozenset({"evaluating", "screened_out", "invalid", "inconclusive", "cancelled"}),
    "evaluating": frozenset({"evaluated", "invalid", "inconclusive", "cancelled"}),
    "evaluated": frozenset({"accepted", "rejected", "inconclusive", "invalid", "confirming"}),
    "confirming": frozenset({"evaluated", "invalid", "inconclusive", "cancelled"}),
    "accepted": frozenset({"installing", "cancelled"}),
    "installing": frozenset({"installed", "installation_failed", "stale_parent"}),
    "installation_failed": frozenset({"installing"}),      # the same accepted artifact may be installed again
}


class StateError(RuntimeError):
    """A transition the state machine does not allow. A terminal state is never rewritten."""


class BudgetError(RuntimeError):
    """A budget the ledger cannot use as asked: a scope that does not exist or exists with other limits, an
    unknown dimension, an allocation that does not fit. The budget is not the one the activity froze, which a
    person has to look at (docs/modules/05_EXECUTION_BUDGET.md §3). A spent budget is the subclass below."""


class BudgetMismatch(BudgetError):
    """The budget asked for is not the one the activity froze: a scope reopened with other limits, a dimension or a
    scope the book does not have, an allocation that does not fit. A person looks at it; the activity pauses
    (docs/modules/05_EXECUTION_BUDGET.md §3). A BudgetError that is neither this nor BudgetExhausted is a misuse of
    the ledger, and nothing handles it as a pause."""


class BudgetExhausted(BudgetError):
    """The operation would exceed a limit of this scope or of one of its ancestors, or the scope stopped after a
    breach: the budget is spent. Not a fault: whatever runs on it ends, and nothing is to be repaired."""


@dataclass
class Operation:
    """One paid or side-effecting step. The id is determined by the experiment, the step and the frozen inputs,
    so a controller that restarts recomputes the same id and continues instead of paying twice."""
    id: str
    experiment_id: str
    step: str
    input_refs: tuple[str, ...] = ()
    status: str = "intent"                    # intent | running | done | failed | cancelled
    attempts: tuple = ()                      # one entry per external try, each with its own attempt id
    output_refs: tuple[str, ...] = ()
    reservation_ids: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {"id": self.id, "experiment_id": self.experiment_id, "step": self.step,
                "input_refs": list(self.input_refs), "status": self.status, "attempts": list(self.attempts),
                "output_refs": list(self.output_refs), "reservation_ids": list(self.reservation_ids)}


def operation_id(experiment_id: str, step: str, input_refs) -> str:
    return f"op_{ct.digest({'experiment': experiment_id, 'step': step, 'inputs': sorted(input_refs)})[:16]}"


class Execution:
    """The journal and the current state of one experiment, coordinated by the workspace lock.

    The intent is written before the step runs and the result before the state advances, so recovery compares the
    journal with the facts on disk — the job, the artifact, the pointer — instead of trusting the last line.
    """

    def __init__(self, root: str | Path, experiment_id: str, *, events: ct.Events | None = None):
        self.root = Path(root)
        self.dir = self.root / "rsi" / "execution"
        self.experiment_id = experiment_id
        self.events = events or ct.Events(self.root)

    @property
    def journal(self) -> Path:
        return self.dir / f"{self.experiment_id}.jsonl"

    @property
    def snapshot(self) -> Path:
        return self.dir / f"{self.experiment_id}.state.json"

    def state(self) -> dict:
        if self.snapshot.exists():
            return json.loads(self.snapshot.read_text(encoding="utf-8"))
        return self.rebuild()

    def rebuild(self) -> dict:
        """The state from the journal alone: the snapshot is a cache, not the record."""
        status, history = "drafted", []
        if self.journal.exists():
            for line in self.journal.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    status = entry["status"]
                    history.append(entry)
        return {"experiment_id": self.experiment_id, "status": status, "history": history}

    def advance(self, status: str, *, reason: str = "", **fields) -> dict:
        if status not in STATES:
            raise StateError(f"unknown state {status!r}: one of {list(STATES)}")
        with locked(self.snapshot):
            state = self.state()
            current = state["status"]
            if current in TERMINAL and current != "installation_failed":
                raise StateError(f"{self.experiment_id} is {current}, a terminal state: {status} would rewrite it")
            allowed = NEXT.get(current, frozenset())
            if status != current and status not in allowed and not (status == "cancelled" and
                                                                   current not in TERMINAL):
                raise StateError(f"{self.experiment_id}: {current} -> {status} is not a transition "
                                 f"({sorted(allowed)})")
            entry = {"ts": now(), "status": status, "reason": reason, **fields}
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with open(self.journal, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            state = {"experiment_id": self.experiment_id, "status": status,
                     "history": [*state["history"], entry]}
            write_json(self.snapshot, state)
        self.events.append("state", experiment=self.experiment_id, status=status, reason=reason)
        return state

    # ------------------------------------------------------------------ operations
    def operation(self, step: str, input_refs=()) -> Operation:
        """The operation for this step and these inputs, as recorded or as a fresh intent."""
        oid = operation_id(self.experiment_id, step, input_refs)
        path = self.root / "rsi" / "operations" / f"{oid}.json"
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            return Operation(saved["id"], saved.get("experiment_id", self.experiment_id), saved.get("step", step),
                             tuple(saved.get("input_refs", ())), saved.get("status", "intent"),
                             tuple(saved.get("attempts", ())), tuple(saved.get("output_refs", ())),
                             tuple(saved.get("reservation_ids", ())))
        op = Operation(oid, self.experiment_id, step, tuple(input_refs))
        self.put(op)
        return op

    def put(self, op: Operation) -> Operation:
        write_json(self.root / "rsi" / "operations" / f"{op.id}.json", {**op.to_json(), "ts": now()})
        return op

    def finish(self, op: Operation, status: str, *, output_refs=()) -> Operation:
        op.status = status
        op.output_refs = tuple(output_refs)
        return self.put(op)


# ---------------------------------------------------------------------------- budget
# Only dimensions that can be observed and bounded are switched on for a plan; the others stay None and are
# recorded rather than enforced, which is why a plan with unmeasurable pricing may not claim a dollar budget.
LIMITS = ("usd", "model_calls", "input_tokens", "output_tokens", "tool_calls", "training_seconds", "wall_seconds",
          "candidates", "rounds", "train_jobs")


@dataclass
class Reservation:
    id: str
    scope_id: str
    operation_id: str
    bound: dict
    status: str = "open"                      # open | settled | unknown | released
    measured: dict = field(default_factory=dict)
    allocation: str | None = None


@dataclass
class BudgetReceipt:
    reservation_id: str
    scope_id: str
    status: str
    measured: dict
    bound: dict
    breach: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"reservation_id": self.reservation_id, "scope_id": self.scope_id, "status": self.status,
                "measured": dict(self.measured), "bound": dict(self.bound), "breach": dict(self.breach)}


class Budgets:
    """One book for every scope of one activity: <workspace>/rsi/budget/book.json.

    A scope's usage is the sum over its subtree's reservations, which is what keeps a parent honest without
    double counting: every reservation belongs to exactly one leaf scope and is counted once there.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "rsi" / "budget" / "book.json"
        self.events = ct.Events(self.root)

    def _read(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"scopes": {}, "reservations": {}, "breaches": [], "stopped": []}

    def account(self, scope_id: str, limits: dict | None = None, *, parent: str | None = None) -> "BudgetAccount":
        """Open or reopen a scope. Reopening with other limits is refused: a scope's limits are what a comparison
        was declared under, and raising them later is not a recovery."""
        limits = {k: v for k, v in (limits or {}).items() if v is not None}
        unknown = sorted(set(limits) - set(LIMITS))
        if unknown:
            raise BudgetMismatch(f"unknown budget dimensions {unknown}: {list(LIMITS)}")
        with locked(self.path):
            book = self._read()
            saved = book["scopes"].get(scope_id)
            if saved is None:
                book["scopes"][scope_id] = {"parent": parent, "limits": limits, "allocations": {}}
                write_json(self.path, book)
            elif saved["limits"] != limits or saved.get("parent") != parent:
                raise BudgetMismatch(f"scope {scope_id!r} already exists with other limits: {saved['limits']}")
        return BudgetAccount(self, scope_id)

    # -------------------------------------------------------------- reading
    def scope(self, scope_id: str, book: dict | None = None) -> dict:
        book = book or self._read()
        if scope_id not in book["scopes"]:
            raise BudgetMismatch(f"no budget scope {scope_id!r}")
        return book["scopes"][scope_id]

    def subtree(self, scope_id: str, book: dict) -> set:
        out, frontier = {scope_id}, [scope_id]
        while frontier:
            parent = frontier.pop()
            for name, entry in book["scopes"].items():
                if entry.get("parent") == parent and name not in out:
                    out.add(name)
                    frontier.append(name)
        return out

    def ancestors(self, scope_id: str, book: dict) -> list[str]:
        out, cur = [], self.scope(scope_id, book).get("parent")
        while cur:
            out.append(cur)
            cur = book["scopes"].get(cur, {}).get("parent")
        return out

    def usage(self, scope_id: str, book: dict, *, allocation: str | None = None) -> dict:
        """What this scope's subtree has committed: settled measurements plus the bounds still outstanding.

        An unknown reservation counts at its bound. Releasing it needs someone to confirm that the call never
        happened; "we did not hear back" is not that confirmation.
        """
        scopes = self.subtree(scope_id, book)
        total = {k: 0.0 for k in LIMITS}
        for r in book["reservations"].values():
            if r["scope_id"] not in scopes or r["status"] == "released":
                continue
            if allocation is not None and r.get("allocation") != allocation:
                continue
            source = r["measured"] if r["status"] == "settled" else r["bound"]
            for k, v in source.items():
                if k in total and isinstance(v, (int, float)):
                    total[k] += float(v)
        return total

    def touched(self, scope_id: str, book: dict) -> set:
        """The dimensions this subtree has actually reserved or spent, whatever the amount.

        A measured zero is a measurement: reporting it as absent would read as "training was never run" when a
        job ran and cost less than the resolution of the clock.
        """
        scopes = self.subtree(scope_id, book)
        out: set = set()
        for r in book["reservations"].values():
            if r["scope_id"] in scopes and r["status"] != "released":
                out |= {k for k in (*r["bound"], *(r.get("measured") or {})) if k in LIMITS}
        return out

    def stopped(self, scope_id: str, book: dict | None = None) -> str:
        book = book or self._read()
        for entry in book.get("stopped", []):
            if entry["scope_id"] in {scope_id, *self.ancestors(scope_id, book)}:
                return entry["reason"]
        return ""

    # -------------------------------------------------------------- writing
    def allocate(self, scope_id: str, name: str, limits: dict) -> dict:
        """Set aside part of a scope for a purpose that may not be spent elsewhere — the full evaluation and the
        install probe of a candidate, so training cannot consume the budget that was to verify it (EX-03).

        This is an allocation, not a reservation: it constrains what other operations may reserve and is never
        itself a spend.
        """
        limits = {k: float(v) for k, v in limits.items() if v is not None and k in LIMITS}
        with locked(self.path):
            book = self._read()
            scope = self.scope(scope_id, book)
            scope.setdefault("allocations", {})[name] = limits
            over = self._exceeds(scope_id, book, {}, allocation=None, extra_allocation=name)
            if over:
                raise BudgetMismatch(f"allocation {name!r} does not fit in {scope_id!r}: {over}")
            write_json(self.path, book)
        return limits

    def _exceeds(self, scope_id: str, book: dict, bound: dict, *, allocation: str | None,
                 extra_allocation: str | None = None) -> dict:
        """Which dimension of which scope this bound would break, if any.

        An allocation is a limit in both directions: nothing else may eat it, and what is tagged with it may not
        exceed it. A verification that no longer fits in its earmark is a candidate that cannot be verified, and
        the answer to that is not to start it (EX-03).
        """
        if allocation is not None:
            # The allocation sits on this scope or on an ancestor: an activity's root sets aside the final
            # comparison's share, which the final spends from its own phase account.
            for owner in (scope_id, *self.ancestors(scope_id, book)):
                limits = (self.scope(owner, book).get("allocations") or {}).get(allocation)
                if limits is None:
                    continue
                spent = self.usage(owner, book, allocation=allocation)
                for dim, limit in limits.items():
                    total = spent.get(dim, 0.0) + float(bound.get(dim, 0.0))
                    if total > float(limit) + 1e-9:
                        return {"scope_id": owner, "dimension": dim, "limit": limit, "would_be": round(total, 6),
                                "allocation": allocation}
                break
        for name in (scope_id, *self.ancestors(scope_id, book)):
            entry = self.scope(name, book)
            used = self.usage(name, book)
            for dim, limit in entry["limits"].items():
                # Every allocation of this scope other than the one being spent blocks its unused part.
                blocked = 0.0
                for alloc, limits in (entry.get("allocations") or {}).items():
                    if alloc == allocation or dim not in limits:
                        continue
                    spent = self.usage(name, book, allocation=alloc).get(dim, 0.0)
                    blocked += max(limits[dim] - spent, 0.0)
                total = used.get(dim, 0.0) + float(bound.get(dim, 0.0)) + blocked
                if total > float(limit) + 1e-9:
                    return {"scope_id": name, "dimension": dim, "limit": limit, "would_be": round(total, 6),
                            "blocked_by_allocations": round(blocked, 6)}
            if extra_allocation and name == scope_id:
                for dim, value in (entry.get("allocations") or {}).get(extra_allocation, {}).items():
                    limit = entry["limits"].get(dim)
                    if limit is not None and used.get(dim, 0.0) + value > float(limit) + 1e-9:
                        return {"scope_id": name, "dimension": dim, "limit": limit,
                                "would_be": round(used.get(dim, 0.0) + value, 6)}
        return {}

    def headroom(self, scope_id: str, book: dict, *, allocation: str | None = None) -> dict:
        """What one more reservation from this scope may still take, per limited dimension: the tightest of every
        limit on the way to the root, less the unspent part of each allocation other than the one being spent, and
        — when spending an allocation — what is left of it. The same arithmetic as `_exceeds`, so what is shown as
        remaining is what a reservation is actually allowed."""
        room: dict = {}
        if allocation is not None:
            for owner in (scope_id, *self.ancestors(scope_id, book)):
                limits = (self.scope(owner, book).get("allocations") or {}).get(allocation)
                if limits is None:
                    continue
                spent = self.usage(owner, book, allocation=allocation)
                room = {dim: round(float(limit) - spent.get(dim, 0.0), 6) for dim, limit in limits.items()}
                break
        for name in (scope_id, *self.ancestors(scope_id, book)):
            entry = self.scope(name, book)
            used = self.usage(name, book)
            for dim, limit in entry["limits"].items():
                blocked = 0.0
                for alloc, limits in (entry.get("allocations") or {}).items():
                    if alloc == allocation or dim not in limits:
                        continue
                    blocked += max(float(limits[dim]) - self.usage(name, book, allocation=alloc).get(dim, 0.0), 0.0)
                left = round(float(limit) - used.get(dim, 0.0) - blocked, 6)
                room[dim] = min(room.get(dim, left), left)
        return room

    def reserve(self, scope_id: str, operation_id: str, bound: dict, *, allocation: str | None = None):
        """Hold an upper bound before the call. Refused when it would break this scope or any ancestor."""
        bound = {k: float(v) for k, v in bound.items() if v is not None and k in LIMITS}
        with locked(self.path):
            book = self._read()
            reason = self.stopped(scope_id, book)
            if reason:
                raise BudgetExhausted(f"no further paid operations in {scope_id!r}: {reason}")
            over = self._exceeds(scope_id, book, bound, allocation=allocation)
            if over:
                raise BudgetExhausted(f"{operation_id} would exceed {over['dimension']} of {over['scope_id']}: "
                                  f"limit {over['limit']}, would be {over['would_be']}")
            rid = new_id("res")
            book["reservations"][rid] = {"id": rid, "scope_id": scope_id, "operation_id": operation_id,
                                        "bound": bound, "status": "open", "measured": {},
                                        "allocation": allocation, "ts": now()}
            write_json(self.path, book)
        return Reservation(rid, scope_id, operation_id, bound, "open", {}, allocation)

    def settle(self, reservation_id: str, measured: dict | None = None, status: str = "settled") -> BudgetReceipt:
        """Close a reservation against what it actually cost.

        `unknown` keeps the bound occupying the budget. `released` is only for a call confirmed never to have
        happened. A measurement above the bound is a breach: it is recorded, further paid operations in that
        scope stop, and the money that was already spent is not undone.
        """
        if status not in ("settled", "unknown", "released"):
            raise BudgetError(f"unknown settlement {status!r}")
        measured = {k: float(v) for k, v in (measured or {}).items() if v is not None and k in LIMITS}
        with locked(self.path):
            book = self._read()
            r = book["reservations"].get(reservation_id)
            if r is None:
                raise BudgetError(f"no reservation {reservation_id}")
            breach = {}
            if status == "settled":
                # Only a dimension the reservation actually bounded can be breached. One it could not bound (no
                # token cap was declared, say) is measured and counted, not compared with an imaginary zero.
                breach = {k: {"bound": r["bound"][k], "measured": v} for k, v in measured.items()
                          if k in r["bound"] and v > float(r["bound"][k]) + 1e-9}
            r["status"], r["measured"] = status, measured if status == "settled" else {}
            if breach:
                r["measured"] = measured         # the real spend is what is recorded, over the bound or not
                book["breaches"].append({"reservation": reservation_id, "scope_id": r["scope_id"],
                                         "breach": breach, "ts": now()})
                book["stopped"].append({"scope_id": r["scope_id"], "reason": f"budget_breach {sorted(breach)}",
                                        "ts": now()})
            write_json(self.path, book)
        receipt = BudgetReceipt(reservation_id, r["scope_id"], status, r["measured"], r["bound"], breach)
        if breach:
            self.events.append("budget_breach", scope=r["scope_id"], reservation=reservation_id, breach=breach)
        return receipt

    def open_reservations(self, scope_id: str | None = None) -> list[dict]:
        book = self._read()
        scopes = self.subtree(scope_id, book) if scope_id else set(book["scopes"])
        return [r for r in book["reservations"].values()
                if r["status"] in ("open", "unknown") and r["scope_id"] in scopes]


@dataclass
class BudgetAccount:
    """One scope's handle on the shared book."""
    budgets: Budgets
    scope_id: str

    def child(self, scope_id: str, limits: dict) -> "BudgetAccount":
        """A nested scope. Its limits are checked against this one every time it reserves, so the arms of a
        comparison cannot between them spend more than the comparison has."""
        return self.budgets.account(scope_id, limits, parent=self.scope_id)

    def reserve(self, operation_id: str, upper_bound: dict, *, allocation: str | None = None) -> Reservation:
        return self.budgets.reserve(self.scope_id, operation_id, upper_bound, allocation=allocation)

    def settle(self, reservation_id: str, measured: dict | None = None, status: str = "settled") -> BudgetReceipt:
        return self.budgets.settle(reservation_id, measured, status)

    def hold(self, operation_id: str, bound: dict) -> str:
        """Set `bound` aside in this scope until it is released: a reservation no call settles, counted at its
        bound on every scope up to the root, so nothing else in the activity can spend it meanwhile. One hold per
        operation id: holding again returns the one on record, open or already released, so a restart neither
        sets the amount aside twice nor takes back what was already handed to what it was held for."""
        book = self.budgets._read()
        for r in book["reservations"].values():
            if r["scope_id"] == self.scope_id and r["operation_id"] == operation_id:
                return r["id"]
        return self.reserve(operation_id, bound).id

    def allocate(self, name: str, limits: dict) -> dict:
        return self.budgets.allocate(self.scope_id, name, limits)

    def remaining(self, *, allocation: str | None = None) -> dict:
        book = self.budgets._read()
        entry = self.budgets.scope(self.scope_id, book)
        used = self.budgets.usage(self.scope_id, book)
        out = {dim: round(float(limit) - used.get(dim, 0.0), 6) for dim, limit in entry["limits"].items()}
        touched = self.budgets.touched(self.scope_id, book)
        # What can still be spent here, whichever scope's limit binds: a phase's account usually has no limits
        # of its own and spends from the activity's root, so its own `remaining` alone would show no cap at all.
        # What another purpose has set aside (the final's share) is not spendable here and is not shown as such.
        effective = self.budgets.headroom(self.scope_id, book, allocation=allocation)
        return {"limits": dict(entry["limits"]),
                "used": {k: round(v, 6) for k, v in used.items() if v or k in touched},
                "remaining": out, "effective_remaining": effective,
                "allocations": dict(entry.get("allocations") or {}),
                "unknown": sum(1 for r in book["reservations"].values()
                               if r["status"] == "unknown" and r["scope_id"] in
                               self.budgets.subtree(self.scope_id, book)),
                "stopped": self.budgets.stopped(self.scope_id, book)}

    def for_research(self) -> dict:
        """What a research step is told about money, read now: what is left wherever the binding limit sits,
        and what this account has spent."""
        remaining = self.remaining()
        return {"remaining": remaining.get("effective_remaining") or {}, "used": remaining.get("used") or {}}

    def affordable(self, bound: dict, *, allocation: str | None = None) -> bool:
        book = self.budgets._read()
        return not self.budgets.stopped(self.scope_id, book) and not self.budgets._exceeds(
            self.scope_id, book, {k: float(v) for k, v in bound.items() if v is not None and k in LIMITS},
            allocation=allocation)


def spend(account: BudgetAccount, operation_id: str, bound: dict, call: Callable[[], Any], *,
          measure: Callable[[Any], dict] | None = None, allocation: str | None = None):
    """Reserve, call, settle. -> (result, receipt).

    A call that raises before it can be measured settles as unknown rather than free: the provider may well have
    charged for it, and only a request id or a job id can say otherwise.
    """
    reservation = account.reserve(operation_id, bound, allocation=allocation)
    try:
        result = call()
    except BaseException:
        account.settle(reservation.id, None, "unknown")
        raise
    receipt = account.settle(reservation.id, (measure or (lambda _r: {}))(result), "settled")
    return result, receipt


# ---------------------------------------------------------------------------- the experiment loop
def execute_experiment(experiment, handlers: dict, *, budget: BudgetAccount, execution: Execution,
                       expected_parent: str | None = None) -> dict:
    """Build, evaluate and install one experiment, recording every step's state and operation.

    The handlers belong to the owning modules (build to M1/M2, evaluate to M3 or M6, install to M3); this loop
    only decides the order, what the budget must allow before a step starts, and which state each result means.
    A candidate that cannot be verified is not started: an experiment that could only train is not an experiment
    (EX-03).

    Called on an experiment that was interrupted, it continues from the state on record instead of starting over
    (docs/modules/05_EXECUTION_BUDGET.md §4): a finished build is reused from its operation rather than trained
    again, an evaluation reruns only the attempts it had not finished (the evaluate handler resumes by attempt
    id), an accepted receipt goes straight to installing, and an install replays its own operation, which commits
    at most once. Nothing is redecided: not the candidate, the plan, the seed or the thresholds.

    v0.3 (M5 §1): an evaluate handler that takes `confirming` is handed a callback; it calls it when a first
    reading cleared the gate and the plan asks for a confirmation reading. The callback writes the provisional
    `evaluated` event (verdict and gain, no receipt — nothing installable exists yet) and enters `confirming`. A
    restart in `confirming`, or at the provisional `evaluated`, runs the evaluate handler again: it finds the
    first reading's attempts on record and pays only for the confirmation's missing ones (EX6-14).
    """
    state = execution.state()
    status, history = state["status"], state["history"]
    out: dict = {"experiment_id": execution.experiment_id, "states": [], "receipt": None, "installation": None,
                 "resumed_from": status if status != "drafted" else None}
    if handlers.get("begin"):
        handlers["begin"](experiment)
    if status in TERMINAL and status != "installation_failed":
        out["states"].append(status)
        return out

    def step(new: str, **fields) -> None:
        out["states"].append(new)
        execution.advance(new, **fields)

    def recorded(field_name: str) -> str | None:
        return next((e[field_name] for e in reversed(history) if e.get(field_name)), None)

    receipt = None
    if status in ("evaluated", "accepted", "installing", "installation_failed") and recorded("receipt"):
        receipt = handlers["load_receipt"](recorded("receipt"))
    # The states a resume enters the evaluation at: mid-reading, mid-confirmation, or at the provisional verdict.
    mid_evaluation = ("evaluating", "confirming", "evaluated")
    if receipt is None:
        if status == "drafted":
            step("admitted", experiment=experiment.ref())
            if not budget.affordable(handlers.get("verify_bound") or {}, allocation="verification"):
                step("inconclusive", reason="budget_exhausted")
                return out
        op = execution.operation("build", (experiment.proposal_ref,))
        if op.status == "done" and op.output_refs:
            from .contracts import BuildResult
            build = BuildResult(status="ready", candidate_ref=op.output_refs[0],
                                artifacts={"reused": "build operation already done"})
        else:
            if status != "building":
                step("building")
            build = handlers["build"](experiment, budget)
            execution.finish(op, "done" if build.status == "ready" else "failed",
                             output_refs=(build.candidate_ref,) if build.candidate_ref else ())
        out["build"] = build
        if build.status != "ready":
            step("invalid" if build.status == "invalid" else "inconclusive",
                 reason="; ".join(build.errors)[:300])
            return out
        if status not in mid_evaluation or out["states"]:
            step("evaluating")

        def confirming(first: dict) -> None:
            """The first reading cleared the gate: say so provisionally, then confirm (M5 §1, v0.3). On a resume
            that is already past these events nothing is written twice."""
            current = execution.state()["status"]
            if current == "evaluating":
                step("evaluated", provisional=True, verdict=first.get("verdict"), gain=first.get("gain"))
                current = "evaluated"
            if current == "evaluated":
                step("confirming")
        op = execution.operation("evaluate", (build.candidate_ref,))
        evaluate = handlers["evaluate"]
        receipt = evaluate(build.candidate_ref, budget, confirming=confirming) if _takes(evaluate, "confirming") \
            else evaluate(build.candidate_ref, budget)
        execution.finish(op, "done", output_refs=(receipt.ref(),))
        step("evaluated", receipt=receipt.ref(), verdict=receipt.verdict)
        if receipt.verdict != "accepted":
            step(receipt.verdict if receipt.verdict in STATES else "invalid",
                 reason="; ".join(receipt.reasons)[:300], receipt=receipt.ref())
            out["receipt"] = receipt
            return out
        step("accepted", receipt=receipt.ref())
    elif status == "evaluated":
        if receipt.verdict != "accepted":
            step(receipt.verdict if receipt.verdict in STATES else "invalid",
                 reason="; ".join(receipt.reasons)[:300], receipt=receipt.ref())
            out["receipt"] = receipt
            return out
        step("accepted", receipt=receipt.ref())
    out["receipt"] = receipt
    install = handlers.get("install")
    if install is None:
        return out
    step("installing")
    installation = install(receipt, expected_parent)
    out["installation"] = installation
    step(installation.status, reason=installation.reason or "", release=installation.target_release)
    return out


def _takes(handler, name: str) -> bool:
    """Whether a handler accepts the keyword `name`: an older handler (a fixture's two-argument evaluate) is called
    as it always was."""
    import inspect
    try:
        parameters = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def unfinished(root: str | Path) -> list[dict]:
    """Experiments whose last recorded state is not final, with the experiment ref they were admitted with.

    A failed installation counts as final here: retrying it is a person's decision (a new install operation on the
    same accepted artifact), not something a restart should do on its own every time it starts.
    """
    directory = Path(root) / "rsi" / "execution"
    out = []
    for path in sorted(directory.glob("*.state.json")) if directory.exists() else ():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["status"] in TERMINAL:
            continue
        ref = next((e.get("experiment") for e in state["history"] if e.get("experiment")), None)
        out.append({"experiment_id": state["experiment_id"], "status": state["status"], "experiment_ref": ref})
    return out


# ---------------------------------------------------------------------------- paid calls
def normalize_usage(usage: dict | None) -> dict:
    """One provider's usage in one vocabulary: uncached input, cache reads, cache writes, output.

    The providers disagree. Anthropic reports `input_tokens` without the cached part and the cache reads and writes
    beside it; OpenAI-compatible APIs report `prompt_tokens` with the cached part inside and `completion_tokens` for
    the output. Reading one as the other prices a call at zero or counts its cache twice — and a zero is exactly
    the number an unknown cost must never become.
    """
    usage = dict(usage or {})
    if "input_tokens" in usage or "output_tokens" in usage:
        return {"input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
                "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0)}
    details = usage.get("prompt_tokens_details") or {}
    cached = int((details.get("cached_tokens") if isinstance(details, dict) else 0) or 0)
    return {"input_tokens": max(int(usage.get("prompt_tokens") or 0) - cached, 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "cache_read_tokens": cached, "cache_write_tokens": 0}


@dataclass(frozen=True)
class Pricing:
    """What a call can cost at most, and what it did cost.

    A strict dollar budget needs a derivable upper bound: prices per token, a cap on input and output, and every
    fallback declared. A provider that may silently retry or fall back to another model has no bound we can
    compute, so a plan that asked for a strict dollar limit does not start rather than reporting a limit it never
    enforced (docs/modules/05_EXECUTION_BUDGET.md §3).

    Cached tokens are priced by their own rates. A call that used the cache when no cache rate was declared has
    an unknown cost, not the cost of its uncached part.
    """
    input_per_1k: float | None = None
    output_per_1k: float | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    declared_fallbacks: tuple[str, ...] = ()
    hidden_fallback: bool = False             # the provider may fall back to something it does not report
    source: str = ""                          # where the prices came from, with their date
    cache_read_per_1k: float | None = None
    cache_write_per_1k: float | None = None

    @property
    def bounded(self) -> bool:
        return not self.hidden_fallback and None not in (self.input_per_1k, self.output_per_1k,
                                                         self.max_input_tokens, self.max_output_tokens)

    def bound(self) -> dict:
        """The upper bound of one call, for the reservation."""
        out = {"model_calls": 1}
        if self.max_input_tokens is not None:
            out["input_tokens"] = self.max_input_tokens
        if self.max_output_tokens is not None:
            out["output_tokens"] = self.max_output_tokens
        if self.bounded:
            out["usd"] = (self.max_input_tokens / 1000 * self.input_per_1k
                          + self.max_output_tokens / 1000 * self.output_per_1k) * (1 + len(self.declared_fallbacks))
        return out

    def measure(self, usage: dict) -> dict:
        """The measured cost of one call, in budget dimensions. `usd` is present only when every token class the
        call used has a declared price."""
        u = normalize_usage(usage)
        out = {"model_calls": 1, "input_tokens": u["input_tokens"] + u["cache_read_tokens"] + u["cache_write_tokens"],
               "output_tokens": u["output_tokens"]}
        usd = self.cost(u)
        if usd is not None:
            out["usd"] = usd
        return out

    def cost(self, usage: dict) -> float | None:
        """USD for a normalized usage, or None when some part of it has no declared price."""
        if self.input_per_1k is None or self.output_per_1k is None:
            return None
        parts = [(usage.get("input_tokens", 0), self.input_per_1k),
                 (usage.get("output_tokens", 0), self.output_per_1k),
                 (usage.get("cache_read_tokens", 0), self.cache_read_per_1k),
                 (usage.get("cache_write_tokens", 0), self.cache_write_per_1k)]
        if any(tokens and price is None for tokens, price in parts):
            return None
        return round(sum(tokens / 1000 * price for tokens, price in parts if tokens), 8)


def call_bound_problem(pricing: Pricing, limits: dict, what: str = "the budget") -> str:
    """Why not even one call fits these limits, or "": every call reserves its worst case first, so a limit
    below one call's bound refuses every call, and that is found out here rather than mid-run."""
    bound = pricing.bound()
    for dim, limit in (limits or {}).items():
        if dim in bound and float(bound[dim]) > float(limit) + 1e-9:
            return (f"one call reserves up to {round(float(bound[dim]), 6)} {dim} and {what} allows {limit}: every "
                    f"call would be refused (raise the limit, or lower [pricing] max_input_tokens/"
                    f"max_output_tokens to what one request can really carry)")
    return ""


def check_pricing(pricing: Pricing, limits: dict) -> str:
    """Why this pricing cannot serve these limits, or "". A dollar limit without a derivable bound is refused
    here, before anything is paid for (EX-07)."""
    if limits.get("usd") is not None and not pricing.bounded:
        missing = [name for name, value in (("input price", pricing.input_per_1k),
                                           ("output price", pricing.output_per_1k),
                                           ("input cap", pricing.max_input_tokens),
                                           ("output cap", pricing.max_output_tokens)) if value is None]
        return (f"a usd limit needs a derivable upper bound per call; missing {missing or ['a bounded fallback']}"
                + (" and the provider may fall back to a model it does not report" if pricing.hidden_fallback
                   else ""))
    return ""


def _retryable(e: Exception) -> bool:
    """Whether sending the same request again may succeed: the provider's own say when it has one, else a rate
    limit, a server error, a dropped connection or a timeout. A request the provider refused as malformed is not
    sent — and paid for — again."""
    flag = getattr(e, "retryable", None)
    if flag is not None:
        return bool(flag)
    status = getattr(e, "status_code", None)
    if isinstance(status, int):
        return status in (408, 409, 429) or status >= 500
    return isinstance(e, (ConnectionError, TimeoutError)) or type(e).__name__ in ("APIConnectionError",
                                                                                  "APITimeoutError")


class MeteredProvider:
    """A chat provider whose every call is reserved before it happens and settled after.

    The retry limit is here rather than inside the provider: a client that retries on its own as often as it
    likes spends money nobody reserved, and each retry is a new external call with its own reservation, because
    only a provider that supports an idempotency key can promise otherwise.
    """

    def __init__(self, provider, account: BudgetAccount, pricing: Pricing, *, operation: str = "chat",
                 max_retries: int = 1, allocation: str | None = None):
        self.provider = provider
        self.account = account
        self.pricing = pricing
        self._operation = operation
        self._labels = threading.local()   # a step's label on this thread's calls (see `operation`)
        # A provider that retries by itself would send requests this meter never reserved: its retries become
        # the meter's, each one reserved and settled, and only for errors that are worth sending again. The
        # provider is not changed for good — it may be shared — its own retries are switched off per call.
        inner = getattr(provider, "retries", None)
        if isinstance(inner, int) and inner > 0:
            max_retries = max(max_retries, inner)
        self.max_retries = max_retries
        self.allocation = allocation
        self.name = getattr(provider, "name", "metered")
        self.model = getattr(provider, "model", "")
        self._lock = threading.Lock()      # concurrent attempts share one meter: ids and in-flight count
        self._settled = threading.Condition(self._lock)      # a call in flight has settled: headroom may be back
        self._in_flight = 0
        self._own = None                   # the provider's own retries, read while no metered call is in flight
        self.receipts: list[BudgetReceipt] = []
        self.calls = 0
        # M5 §2 (v0.3): the provider's rate-limit refusals (429/529/overloaded, `unbilled`) this meter absorbed as
        # retries — never charged, never a pause; counted so a run's record can list them.
        self.rate_limited = 0

    @property
    def operation(self) -> str:
        """The operation the next call is booked under. A research step's label (bench._Completions.label) is set
        on the thread that runs the step, so notes written concurrently (M5 §2, v0.3) each carry their own label
        and never one another's; the base operation is the meter's."""
        return getattr(self._labels, "operation", None) or self._operation

    @operation.setter
    def operation(self, value: str) -> None:
        if value == self._operation:
            self._labels.operation = None          # the label restored: this thread books under the base again
        else:
            self._labels.operation = value

    def _reserve(self, reservation_id: str, bound: dict, last: Exception | None):
        """One call's reservation. Refused while other calls of this meter are in flight, it waits for one of them
        to settle and asks again: their bounds hold the headroom, and the budget is not spent until they are. It
        is exhausted only when a reservation is refused with nothing in flight — then every further call of the
        scope is, as before."""
        while True:
            with self._settled:
                try:
                    reservation = self.account.reserve(reservation_id, bound, allocation=self.allocation)
                except BudgetError:
                    if self._in_flight > 0:
                        self._settled.wait(timeout=60.0)
                        continue
                    if last is not None:
                        # a retry the budget cannot cover: the provider's failure is the outcome
                        raise _as_provider_error(last) from (None if isinstance(last, _provider_error()) else last)
                    raise
                # reserved and counted in one step, so a refusal elsewhere sees this call as in flight
                if self._in_flight == 0:
                    self._own = getattr(self.provider, "retries", None)     # in flight, the provider's is 0
                self._in_flight += 1
                if isinstance(self._own, int):
                    self.provider.retries = 0
                return reservation

    def chat(self, system, messages, specs=(), **kwargs):
        """One call, reserved before and settled after, retried while its failure is worth retrying. A failure
        that outlasts the retries leaves as a ProviderError whatever the provider raised — an SDK's connection
        error, a gateway's malformed reply — so whoever handles a model call's failure (M5 §3) sees one kind.
        Keyword arguments (the gateway's per-call `effort`) pass through to the provider untouched: the meter
        books a call, it does not shape it."""
        bound = self.pricing.bound()
        last: Exception | None = None
        with self._lock:
            call, self.calls = self.calls, self.calls + 1      # one id per call, whatever thread makes it
        for attempt in range(self.max_retries + 1):
            reservation = self._reserve(f"{self.operation}#{call}.{attempt}", bound, last)
            try:
                try:
                    turn = self.provider.chat(system, messages, specs, **kwargs)
                except Exception as e:  # noqa: BLE001 - the call may well have been charged: unknown, not free
                    # Released only when the request is known never to have reached the model; otherwise unknown.
                    unbilled = bool(getattr(e, "unbilled", False))
                    self.receipts.append(self.account.settle(reservation.id, None,
                                                             "released" if unbilled else "unknown"))
                    last = e
                    if unbilled and _retryable(e):
                        with self._lock:
                            self.rate_limited += 1
                    if not _retryable(e):
                        break                  # the same request would fail the same way: do not pay for it again
                    if attempt < self.max_retries:
                        import time
                        time.sleep(min(30.0, 2.0 ** attempt))
                    continue
                self.receipts.append(self.account.settle(reservation.id,
                                                         self.pricing.measure(turn.usage or {}), "settled"))
            finally:
                # Settled first, then counted out: a waiter woken here must find the reservation released, or it
                # would be refused with nothing in flight and read that as the budget's end.
                with self._settled:
                    self._in_flight -= 1
                    if self._in_flight == 0 and isinstance(self._own, int):
                        self.provider.retries = self._own      # restored once no call is in flight
                    self._settled.notify_all()
            return turn
        raise _as_provider_error(last) from (None if isinstance(last, _provider_error()) else last)


def _provider_error():
    from ..providers import ProviderError
    return ProviderError


def _as_provider_error(e: Exception) -> Exception:
    """A model call's failure as a ProviderError, with what the meter knew of it: whether it was worth retrying,
    whether it never reached the model, and the HTTP status by which an account refusal is told."""
    if isinstance(e, _provider_error()):
        return e
    out = _provider_error()(f"{type(e).__name__}: {str(e)[:400]}", retryable=_retryable(e),
                            unbilled=bool(getattr(e, "unbilled", False)))
    status = getattr(e, "status_code", None)
    if isinstance(status, int):
        out.status_code = status
    return out


# ---------------------------------------------------------------------------- the top-level schedule
STOP_REASONS = ("rounds_done", "no_proposal", "patience", "infrastructure_failures", "budget_exhausted",
                "cancelled")


@dataclass(frozen=True)
class Schedule:
    """What the core fixes before the activity starts. A research policy may ask to stop or to collect more; it
    cannot add rounds or open a nested campaign (docs/modules/05_EXECUTION_BUDGET.md §4)."""
    worker_rounds: int = 3
    research_slots: int = 0                   # only an explicitly enabled M7 activity proposes research changes
    patience: int = 2                         # rounds in a row that install nothing
    max_infrastructure_failures: int = 2      # in a row
    candidates_per_round: int = 1
    collection_batches: int = 2               # v0.6: bounded collect batches per worker segment, never reset

    def stop(self, *, round_index: int, idle: int, infra_failures: int, proposals: bool,
             affordable: bool) -> str:
        if not affordable:
            return "budget_exhausted"
        if round_index >= self.worker_rounds:
            return "rounds_done"
        if infra_failures >= self.max_infrastructure_failures:
            return "infrastructure_failures"
        if idle >= self.patience:
            return "patience"
        if not proposals:
            return "no_proposal"
        return ""
