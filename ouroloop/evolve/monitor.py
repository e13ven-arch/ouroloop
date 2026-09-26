"""The post-act risk monitor (PLAN_v0.2 M9): the realized share of harmed sessions among those a model acted in, a
session harmed when any decision the model acted on in it, at any point, is wrong by its outcome (not among its right
answers, replay.right; a decision with no trainable outcome counts for nothing). The runtime runs it as each new
session opens, before the session is pinned (Runtime.session): once the Clopper-Pearson lower bound on the champion's
harmed share is above the frozen gate template's risk, the champion is revoked. The new session then takes its
defaults, and the sessions already pinned to it do from their next ask on.

The gate judged the model on logged states, but a decision that acts changes the states that follow it: the states
the model reaches once it acts are not the ones it was calibrated on. The monitor judges it on those.

Each look is a 1 - alpha (95%) lower bound, but it is looked at again as each session opens, so a model that stays
within its risk is revoked by chance more often than alpha, and more so the longer it runs: at its budget about 1 in 5
within 200 sessions and 1 in 3 within 2000; at 4% against a 5% budget about 1 in 10. Repeated looks err toward
revoking, the safe side."""
from __future__ import annotations

import json

from ..ledger import Ledger, Row
from ..policy import cp_lower, harmed
from ..release import revoke
from .gate import load_template, template_path
from .registry import Registry
from .replay import right


def acted(rt, point: str, sessions=None) -> dict[str, list[tuple[str, bool]]]:
    """model -> (session, correct) of the labelled decisions it acted on at `point`, in time order (within
    `sessions`, if given). Correct: the action is among the outcomes' right answers (replay.right)."""
    out: dict[str, list] = {}
    for row in rt.ledger.rows(point):
        d = row.decision
        target = row.target()
        if d.get("route") != "act" or target is None or (sessions is not None and d.get("session") not in sessions):
            continue
        out.setdefault(d.get("model", ""), []).append((d.get("session", ""), d.get("action") in right(target[0])))
    return out


def realized(rt, point: str, sessions=None, model: str | None = None) -> tuple[int, int]:
    """(harmed sessions, sessions acted in) at `point`, by `model` or by any (a session per model). For reports."""
    bad = n = 0
    for m, items in acted(rt, point, sessions).items():
        if model is None or m == model:
            by = harmed(items)
            bad, n = bad + sum(by.values()), n + len(by)
    return bad, n


class Tally:
    """Per model, the sessions it acted in (with a trainable outcome) and the harmed ones, kept from the ledger events
    appended since the last update: the ledger is append-only, so each file is read on from where it was left, and an
    update costs the new events only (`parsed` counts them; `skipped` the lines of those that were not JSON, a torn
    append, which are passed over so the rest is still read). Another process's appends are read like this one's; a
    fresh runtime reads the ledger once to catch up. An outcome refers to its decision, so the acted decisions are
    kept to judge the outcomes that come later; one outcome after another may change a decision's verdict."""

    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.parsed = 0
        self.skipped = 0                        # lines that were not JSON (a torn append)
        self._read: dict[str, int] = {}         # ledger file -> bytes read
        self._acts: dict[str, tuple] = {}       # acted decision id -> (model, session, point, action, its outcomes)
        self._wrong: dict[str, bool] = {}       # acted decision id -> wrong, once it has a trainable outcome
        self._session: dict[tuple, int] = {}    # (model, session) -> its acted decisions wrong so far
        self._count: dict[str, list] = {}       # model -> [harmed sessions, sessions acted in]
        self._points: dict[str, set] = {}       # model -> the points of its judged acts

    def update(self) -> None:
        for path in sorted(self.ledger.root.glob("*.jsonl")):
            start = self._read.get(path.name, 0)
            if path.stat().st_size <= start:
                continue
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read()
            end = chunk.rfind(b"\n") + 1        # a line still being written is read at the next update
            for line in chunk[:end].split(b"\n")[:-1]:
                start += len(line) + 1
                if line.strip():
                    self.parsed += 1
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        ev = None
                    if isinstance(ev, dict):
                        self._event(ev)
                    else:                           # a torn append: skipped, and counted; the rest is still read
                        self.skipped += 1
                self._read[path.name] = start       # past this line only once it is applied

    def _event(self, ev: dict) -> None:
        if ev.get("type") == "decision" and ev.get("route") == "act":
            self._acts[ev["id"]] = (ev.get("model", ""), ev.get("session", ""), ev.get("point"), ev.get("action"), [])
            return
        if ev.get("type") != "outcome" or ev.get("decision") not in self._acts:
            return
        did = ev["decision"]
        model, session, point, action, outcomes = self._acts[did]
        outcomes.append({k: ev.get(k) for k in ("label", "source", "weight") if k in ev})
        target = Row({}, outcomes=outcomes).target()
        old, wrong = self._wrong.get(did), target is not None and action not in right(target[0])
        if target is None or old == wrong:
            return
        count = self._count.setdefault(model, [0, 0])
        key = (model, session)
        if key not in self._session:
            self._session[key] = 0
            count[1] += 1
        self._points.setdefault(model, set()).add(point)
        was = self._session[key] > 0
        self._session[key] += int(wrong) - int(bool(old))
        count[0] += int(self._session[key] > 0) - int(was)
        self._wrong[did] = wrong

    def harmed(self, model: str) -> tuple[int, int, list[str]]:
        """(harmed sessions, sessions acted in, the points it acted at) of `model`, as of the last update."""
        bad, n = self._count.get(model, (0, 0))
        return bad, n, sorted(self._points.get(model, ()))


def watch(rt, tally: Tally, alpha: float = 0.05) -> dict | None:
    """The check before a new session is pinned: revoke the champion, if it may act and is not revoked, once the
    share of the sessions it acted in that it harmed is above the gate template's risk with confidence 1 - alpha.
    -> the revocation, also written to the ledger for people. Without a frozen gate template there is no budget and
    no check."""
    if not template_path(rt.root).exists():
        return None
    card = Registry(rt.root).champion() or {}
    version, model = card.get("version"), card.get("model_id")
    if not model or not rt.accepted.promoted(card, standing=True) or rt.revoked.hit(version, model):
        return None
    risk = load_template(rt.root)[0]["risk"]
    tally.update()
    bad, n, points = tally.harmed(model)
    if bad <= risk * n:                                     # the lower bound is below the share: within the risk
        return None
    lower = cp_lower(bad, n, alpha)
    if lower <= risk:
        return None
    at = ", ".join(points)
    reason = (f"harmed {bad} of the {n} sessions it acted in at {at}: its {1 - alpha:.0%} lower bound {lower:.3f} "
              f"is above the budget {risk}")
    if not revoke(rt.root, version, model, reason):
        return None                                         # another process revoked it first
    return rt.ledger.append({"type": "monitor", "event": "revoked", "point": at, "model": model, "version": version,
                             "harmed": bad, "sessions": n, "lower": round(lower, 4), "risk": risk})
