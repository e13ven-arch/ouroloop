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

from ..ledger import locked, new_id, now, write_json
from . import contracts as ct

# ---------------------------------------------------------------------------- states
STATES = ("drafted", "admitted", "building", "screening", "evaluating", "evaluated", "accepted", "installing",
          "installed", "rejected", "inconclusive", "invalid", "screened_out", "installation_failed",
          "stale_parent", "cancelled")
TERMINAL = frozenset({"installed", "rejected", "inconclusive", "invalid", "screened_out", "installation_failed",
                      "stale_parent", "cancelled"})
# Where each state may go. `screening` is skippable when the frozen plan has no screen, but no necessary
# preflight is skippable, and nothing walks back out of a terminal state.
NEXT: dict[str, frozenset] = {
    "drafted": frozenset({"admitted", "invalid", "cancelled"}),
    "admitted": frozenset({"building", "invalid", "inconclusive", "cancelled"}),
    "building": frozenset({"screening", "evaluating", "invalid", "inconclusive", "cancelled"}),
    "screening": frozenset({"evaluating", "screened_out", "invalid", "inconclusive", "cancelled"}),
    "evaluating": frozenset({"evaluated", "invalid", "inconclusive", "cancelled"}),
    "evaluated": frozenset({"accepted", "rejected", "inconclusive", "invalid"}),
    "accepted": frozenset({"installing", "cancelled"}),
    "installing": frozenset({"installed", "installation_failed", "stale_parent"}),
    "installation_failed": frozenset({"installing"}),      # the same accepted artifact may be installed again
}


class StateError(RuntimeError):
    """A transition the state machine does not allow. A terminal state is never rewritten."""


class BudgetError(RuntimeError):
    """The operation would exceed a limit of this scope or of one of its ancestors."""


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
            raise BudgetError(f"unknown budget dimensions {unknown}: {list(LIMITS)}")
        with locked(self.path):
            book = self._read()
            saved = book["scopes"].get(scope_id)
            if saved is None:
                book["scopes"][scope_id] = {"parent": parent, "limits": limits, "allocations": {}}
                write_json(self.path, book)
            elif saved["limits"] != limits or saved.get("parent") != parent:
                raise BudgetError(f"scope {scope_id!r} already exists with other limits: {saved['limits']}")
        return BudgetAccount(self, scope_id)

    # -------------------------------------------------------------- reading
    def scope(self, scope_id: str, book: dict | None = None) -> dict:
        book = book or self._read()
        if scope_id not in book["scopes"]:
            raise BudgetError(f"no budget scope {scope_id!r}")
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
                raise BudgetError(f"allocation {name!r} does not fit in {scope_id!r}: {over}")
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
            limits = (self.scope(scope_id, book).get("allocations") or {}).get(allocation) or {}
            spent = self.usage(scope_id, book, allocation=allocation)
            for dim, limit in limits.items():
                total = spent.get(dim, 0.0) + float(bound.get(dim, 0.0))
                if total > float(limit) + 1e-9:
                    return {"scope_id": scope_id, "dimension": dim, "limit": limit, "would_be": round(total, 6),
                            "allocation": allocation}
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

    def reserve(self, scope_id: str, operation_id: str, bound: dict, *, allocation: str | None = None):
        """Hold an upper bound before the call. Refused when it would break this scope or any ancestor."""
        bound = {k: float(v) for k, v in bound.items() if v is not None and k in LIMITS}
        with locked(self.path):
            book = self._read()
            reason = self.stopped(scope_id, book)
            if reason:
                raise BudgetError(f"no further paid operations in {scope_id!r}: {reason}")
            over = self._exceeds(scope_id, book, bound, allocation=allocation)
            if over:
                raise BudgetError(f"{operation_id} would exceed {over['dimension']} of {over['scope_id']}: "
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
                breach = {k: {"bound": r["bound"].get(k, 0.0), "measured": v} for k, v in measured.items()
                          if v > float(r["bound"].get(k, 0.0)) + 1e-9}
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

    def allocate(self, name: str, limits: dict) -> dict:
        return self.budgets.allocate(self.scope_id, name, limits)

    def remaining(self) -> dict:
        book = self.budgets._read()
        entry = self.budgets.scope(self.scope_id, book)
        used = self.budgets.usage(self.scope_id, book)
        out = {dim: round(float(limit) - used.get(dim, 0.0), 6) for dim, limit in entry["limits"].items()}
        touched = self.budgets.touched(self.scope_id, book)
        return {"limits": dict(entry["limits"]),
                "used": {k: round(v, 6) for k, v in used.items() if v or k in touched},
                "remaining": out, "allocations": dict(entry.get("allocations") or {}),
                "unknown": sum(1 for r in book["reservations"].values()
                               if r["status"] == "unknown" and r["scope_id"] in
                               self.budgets.subtree(self.scope_id, book)),
                "stopped": self.budgets.stopped(self.scope_id, book)}

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
    """Build, screen, evaluate and install one experiment, recording every step's state and operation.

    The handlers belong to the owning modules (build to M1/M2, evaluate to M3 or M6, install to M3); this loop
    only decides the order, what the budget must allow before a step starts, and which state each result means.
    A candidate that cannot be verified is not started: an experiment that could only train is not an experiment
    (EX-03).
    """
    out: dict = {"experiment_id": execution.experiment_id, "states": [], "receipt": None, "installation": None}

    def step(status: str, **fields) -> None:
        out["states"].append(status)
        execution.advance(status, **fields)

    step("admitted")
    if not budget.affordable(handlers.get("verify_bound") or {}, allocation="verification"):
        step("inconclusive", reason="budget_exhausted")
        return out
    step("building")
    op = execution.operation("build", (experiment.proposal_ref,))
    build = handlers["build"](experiment, budget)
    execution.finish(op, "done" if build.status == "ready" else "failed",
                     output_refs=(build.candidate_ref,) if build.candidate_ref else ())
    out["build"] = build
    if build.status != "ready":
        step("invalid" if build.status == "invalid" else "inconclusive",
             reason="; ".join(build.errors)[:300])
        return out
    screen = handlers.get("screen")
    if screen is not None:
        step("screening")
        verdict, reasons = screen(build.candidate_ref, budget)
        if verdict != "pass":
            step("screened_out" if verdict == "screened_out" else "invalid", reason="; ".join(reasons)[:300])
            return out
    step("evaluating")
    op = execution.operation("evaluate", (build.candidate_ref,))
    receipt = handlers["evaluate"](build.candidate_ref, budget)
    out["receipt"] = receipt
    execution.finish(op, "done", output_refs=(receipt.ref(),))
    step("evaluated", receipt=receipt.ref(), verdict=receipt.verdict)
    if receipt.verdict != "accepted":
        step(receipt.verdict if receipt.verdict in STATES else "invalid",
             reason="; ".join(receipt.reasons)[:300], receipt=receipt.ref())
        return out
    step("accepted", receipt=receipt.ref())
    install = handlers.get("install")
    if install is None:
        return out
    step("installing")
    installation = install(receipt, expected_parent)
    out["installation"] = installation
    step(installation.status, reason=installation.reason or "", release=installation.target_release)
    return out


# ---------------------------------------------------------------------------- paid calls
@dataclass(frozen=True)
class Pricing:
    """What a call can cost at most, and what it did cost.

    A strict dollar budget needs a derivable upper bound: prices per token, a cap on input and output, and every
    fallback declared. A provider that may silently retry or fall back to another model has no bound we can
    compute, so a plan that asked for a strict dollar limit does not start rather than reporting a limit it never
    enforced (docs/modules/05_EXECUTION_BUDGET.md §3).
    """
    input_per_1k: float | None = None
    output_per_1k: float | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    declared_fallbacks: tuple[str, ...] = ()
    hidden_fallback: bool = False             # the provider may fall back to something it does not report
    source: str = ""                          # where the prices came from, with their date

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
        out = {"model_calls": 1, "input_tokens": usage.get("input_tokens", 0),
               "output_tokens": usage.get("output_tokens", 0)}
        if self.input_per_1k is not None and self.output_per_1k is not None:
            out["usd"] = (out["input_tokens"] / 1000 * self.input_per_1k
                          + out["output_tokens"] / 1000 * self.output_per_1k)
        return out


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
        self.operation = operation
        self.max_retries = max_retries
        self.allocation = allocation
        self.name = getattr(provider, "name", "metered")
        self.model = getattr(provider, "model", "")
        self.receipts: list[BudgetReceipt] = []
        self.calls = 0

    def chat(self, system, messages, specs=()):
        bound = self.pricing.bound()
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            reservation = self.account.reserve(f"{self.operation}#{self.calls}.{attempt}", bound,
                                              allocation=self.allocation)
            self.calls += 1
            try:
                turn = self.provider.chat(system, messages, specs)
            except Exception as e:  # noqa: BLE001 - the call may well have been charged: unknown, not free
                self.receipts.append(self.account.settle(reservation.id, None, "unknown"))
                last = e
                continue
            self.receipts.append(self.account.settle(reservation.id,
                                                     self.pricing.measure(turn.usage or {}), "settled"))
            return turn
        raise last


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
