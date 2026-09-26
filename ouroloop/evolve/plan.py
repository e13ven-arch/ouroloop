"""The round: data -> diagnosis -> hypothesis -> plan -> admission -> training (PLAN_v0.2 M11). Only the last step
spends GPU.

`diagnose` reads the ledger, the registry, the revocation list and the experience log and says what the data holds;
a planner turns that into a `Plan`, a hypothesis with the change it tries, what it expects to gain and the evidence
that would confirm or deny it; `admit` refuses a plan the data cannot decide, naming what is missing. Nothing trains
without a plan that passed admission. `plan` below is the built-in rule planner and the interface the research agent
(M11) implements.

The decisive fact is computable before training: with risk r and no error at all, a threshold needs `sessions_needed`
sessions (59 at r = 0.05), and only a session holding a default error can buy one, because only there does a model
act. When fewer calibration sessions hold a default error than that, no model trained on this data can ever act, so
training is provably wasted: a business user meets this as "the data is not good enough yet", and the framework says
so instead of spending compute.

A revocation is evidence, not a trigger. A champion revoked after acting in a handful of sessions leaves almost the
same data behind, and a candidate trained on it fails the same way; the rule planner asks for a revocation to bring
genuinely new data before it proposes anything.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field

from ..policy import cp_upper
from . import experience, monitor
from .checks import min_detectable_difference
from .gate import TEMPLATE, choose, load_template, records
from .registry import Registry
from .replay import default_error, in_calibration, labelled

ALPHA = 0.05        # the confidence every bound in the loop is taken at (policy.fit_threshold, gate.decide_points)
MATERIAL = 0.1      # a revocation brings new data when its sessions hold this share of the last candidate's rows


def sessions_needed(risk: float, alpha: float = ALPHA) -> int:
    """The fewest sessions a threshold can be fitted on: the smallest n with cp_upper(0, n, alpha) <= risk, that is
    with no error at all. A point that acts in several phases needs more (each phase gets a share of the risk), so
    this is the floor under every point."""
    n = max(1, math.ceil(math.log(alpha) / math.log1p(-risk))) if 0 < risk < 1 else 1
    while cp_upper(0, n, alpha) > risk:
        n += 1
    return n


@dataclass
class PointData:
    """What the ledger holds at one point, counted, nothing trained or inferred. The test sessions are the ones a
    gate record frozen now would take (gate.choose); the calibration ones are the rest as the record would split
    them."""
    point: str
    default_errors: int             # labelled rows outside held-out tasks whose routing default is wrong
    sessions: int                   # labelled sessions outside held-out tasks
    train_rows: int
    calibration_sessions: int
    error_sessions: int             # of those, the ones holding at least one default error: they can buy a threshold
    test_sessions: int
    test_rows: int
    headroom: int                   # default errors among the test rows: what a candidate could be measured fixing
    mde: float                      # the per-row difference that test set detects (checks.min_detectable_difference)

    def line(self) -> str:
        return (f"{self.point}: {self.default_errors} default errors in {self.sessions} labelled sessions; "
                f"{self.train_rows} training rows; {self.error_sessions} of {self.calibration_sessions} calibration "
                f"sessions hold one; test {self.test_sessions} sessions, {self.test_rows} rows, {self.headroom} "
                f"default errors, detects {self.mde:.2f}")


@dataclass
class Champion:
    """The champion as the registry, the revocation list and the ledger have it. `rows` are the labelled rows of the
    sessions it acted in: what a revocation of it actually adds to the training data."""
    version: str | None = None
    model: str | None = None
    revoked: str | None = None      # when it was revoked, None while it stands
    reason: str = ""
    acted_sessions: int = 0
    harmed: int = 0
    rows: int = 0

    def line(self) -> str:
        if not self.version:
            return "champion: none promoted yet"
        state = f"revoked {self.revoked} ({self.reason})" if self.revoked else "deployed"
        return (f"champion: {self.version} {state}; acted in {self.acted_sessions} sessions, harmed {self.harmed}; "
                f"{self.rows} labelled rows from them in the ledger")


@dataclass
class Diagnosis:
    """The round's reading of the data, read-only. Cards carry the default errors of a candidate's points as one
    count, so `at_last_trained` is the total, while `points` counts each point now."""
    points: dict[str, PointData] = field(default_factory=dict)
    champion: Champion = field(default_factory=Champion)
    fresh: bool = False             # the test sessions are fresh ones: a gate could accept on them, not only validate
    min_sessions: int = 0
    trained: int = 0                # candidates that trained: the budget spent
    budget: int = 0
    last: dict = field(default_factory=dict)        # the last card that trained a candidate that was not planted
    recent: list = field(default_factory=list)      # the last candidates: version, gate, reasons
    target: float = 0.0
    reached: float | None = None    # the standing accepted model's smallest fixed share, None when none stands
    accepted: str | None = None
    default_errors: int = 0
    at_last_trained: int | None = None
    minutes_per_row: float | None = None            # the last job's wall time per training row

    @property
    def train_rows(self) -> int:
        return sum(p.train_rows for p in self.points.values())

    @property
    def headroom(self) -> int:
        return sum(p.headroom for p in self.points.values())

    def lines(self) -> list[str]:
        out = [p.line() for p in self.points.values()]
        out.append(self.champion.line())
        out.append(f"{self.default_errors} default errors now, {self.at_last_trained} at the last trained candidate "
                   f"{self.last.get('version') or '-'}; {self.trained} of {self.budget} trainings spent")
        if self.reached is not None:
            out.append(f"{self.accepted} fixes {self.reached:.0%} of the default errors, target {self.target:.0%}")
        for card in self.recent:
            out.append(f"{card['version']}: {card.get('gate')} - {'; '.join(card.get('reasons') or [])[:100]}")
        return out


@dataclass
class Plan:
    """What one round proposes to do, and what would show it was right. `predicted_gain` is the share of the default
    errors the change expects to fix; `data` is what it needs and would train on."""
    hypothesis: str
    kind: str                       # "recipe" today, a change to the training recipe; "spec" later
    change: dict
    points: list
    predicted_gain: float
    evidence: str
    cost_minutes: float | None      # None when nothing has trained yet: no wall time to scale
    data: dict = field(default_factory=dict)

    def per_row(self, point: PointData) -> float:
        """The predicted gain as the gate measures it: a mean over the test rows, so that it can be compared with
        the difference that test set detects."""
        return self.predicted_gain * point.headroom / point.test_rows if point.test_rows else 0.0

    def to_json(self) -> dict:
        return asdict(self)

    def measured(self, card: dict) -> dict:
        """The prediction beside what the gate measured, for the experience log: the candidate's smallest fixed
        share of the default errors over the plan's points, and the verdict."""
        shares = [s for name in self.points
                  if (s := ((card.get("metrics") or {}).get(name, {}).get("challenger") or {}).get("fixed_share"))
                  is not None]
        return {"hypothesis": self.hypothesis, "change": self.change, "predicted_gain": self.predicted_gain,
                "gain": min(shares) if shares else None, "verdict": card.get("gate")}

    def line(self) -> str:
        cost = f"{self.cost_minutes:.0f} GPU minutes" if self.cost_minutes is not None else "cost unknown"
        return (f"{self.kind} {json.dumps(self.change, ensure_ascii=False)} at {', '.join(self.points)}: "
                f"{self.hypothesis} (predicted gain {self.predicted_gain:.0%}, {cost})")


def diagnose(rt, points: list | None = None) -> Diagnosis:
    """Read the ledger, the registry, the revocation list and the experience log: no training, no inference, and
    nothing frozen or used up. The test sessions are the ones a record frozen now would take, so what the diagnosis
    says about the gate is what the round would meet."""
    from .pipeline import model_points
    params = load_template(rt.root)[0]
    registry = Registry(rt.root)
    points = points or model_points(rt)
    picked = choose(rt, params, records(rt.root))
    test = set(picked["test_sessions"])
    sessions, _ = rt.ledger.sessions()
    d = Diagnosis(fresh=picked["fresh"], min_sessions=params["min_sessions"],
                  target={**TEMPLATE["target"], **params.get("target", {})}["fixed_share"],
                  budget={**TEMPLATE["budget"], **params.get("budget", {})}["max_trainings"])
    acting = _acting(rt, registry, points)
    for name in points:
        rows = [(r[0].decision.get("session", ""), default_error(rt, name, r[0], r[1])) for r in labelled(rt, name)
                if not sessions.get(r[0].decision.get("session", ""), {}).get("held_out")]
        cal_frac = params["cal_frac"].get(name, params["cal_frac"].get("*", 0.15))
        cal = {s for s, _ in rows if s not in test and in_calibration(s, cal_frac)}
        test_rows = [e for s, e in rows if s in test]
        d.points[name] = PointData(
            point=name, default_errors=sum(e for _, e in rows), sessions=len({s for s, _ in rows}),
            train_rows=sum(s not in test and s not in cal for s, _ in rows), calibration_sessions=len(cal),
            error_sessions=len({s for s, e in rows if e and s in cal}),
            test_sessions=len({s for s, _ in rows if s in test}), test_rows=len(test_rows),
            headroom=sum(test_rows), mde=round(min_detectable_difference(len(test_rows)), 4))
        d.champion.rows += sum(s in acting for s, _ in rows)
    d.default_errors = sum(p.default_errors for p in d.points.values())
    _cards(rt, registry, d)
    _champion(rt, registry, d, points, acting)
    return d


def _acting(rt, registry: Registry, points: list) -> set:
    """The sessions the champion acted in, from the ledger."""
    model = (registry.champion() or {}).get("model_id")
    if not model:
        return set()
    return {s for name in points for s, _ in monitor.acted(rt, name).get(model, [])}


def _cards(rt, registry: Registry, d: Diagnosis) -> None:
    """The registry's cards: the budget spent, the last candidate that really trained, the verdicts of the last few,
    and what a training minute cost last time."""
    cards = [registry.card(v) for v in registry.versions() if (registry.dir / v / "card.json").exists()]
    trained = [c for c in cards if c.get("model_id")]
    d.trained = len(trained)
    real = [c for c in trained if not c.get("planted")]
    d.last = real[-1] if real else {}
    d.at_last_trained = d.last.get("default_errors")
    d.recent = [{"version": c["version"], "gate": c.get("gate"), "reasons": c.get("reasons") or []}
                for c in cards[-3:]]
    accepted = next((c for c in reversed(cards) if c.get("gate") == "accepted"), {})
    shares = [s for m in accepted.get("metrics", {}).values()      # a card from before gain has no share
              if (s := (m.get("challenger") or {}).get("fixed_share")) is not None]
    if accepted and shares and not rt.revoked.hit(accepted["version"], accepted.get("model_id")):
        d.reached, d.accepted = min(shares), accepted["version"]
    for card in reversed(real):     # the latest job whose wall time and rows are both on the card
        rows = sum(n for part in (card.get("data") or {}).values() for k, n in part.items() if k in ("train", "val"))
        if card.get("train_seconds") is not None and rows:
            d.minutes_per_row = card["train_seconds"] / 60 / rows
            break


def _champion(rt, registry: Registry, d: Diagnosis, points: list, acting: set) -> None:
    """The champion's state: the sessions it acted in and the ones it harmed, and its revocation when it is on the
    list (its entry there says when and why). `d.champion.rows`, the labelled rows those sessions left, is counted
    with the points above."""
    card = registry.champion() or {}
    d.champion.version, d.champion.model = card.get("version"), card.get("model_id")
    d.champion.acted_sessions = len(acting)
    d.champion.harmed = sum(monitor.realized(rt, name, acting, card.get("model_id"))[0] for name in points)
    if not rt.revoked.hit(card.get("version"), card.get("model_id")):
        return
    path = rt.revoked.path
    entries = json.loads(path.read_text(encoding="utf-8"))["revoked"] if path.exists() else []
    hit = next((e for e in reversed(entries)
                if e.get("version") == card.get("version") or e.get("model") == card.get("model_id")), {})
    d.champion.revoked, d.champion.reason = hit.get("ts", ""), hit.get("reason", "")


def plan(rt, d: Diagnosis) -> tuple[Plan | None, str]:
    """The planner interface: a plan for this round, and why, or None and why not. The research agent (M11)
    implements this same function against the same diagnosis; the rule planner here is the built-in one.

    It proposes only when there is something new to learn from: nothing has trained yet, the default errors have at
    least doubled since the last trained candidate (the data a model could learn from), or a revocation brought
    genuinely new data - the sessions the revoked champion acted in are labelled in the ledger and hold at least
    MATERIAL of the rows the last candidate trained on. A revocation on its own is never a reason: the champion in
    the run that showed this acted in two sessions before it was revoked, so the next candidate saw almost the same
    data and failed the same way."""
    if not d.last:
        return _plan(d, "no candidate trained yet"), "no candidate trained yet"
    now, before = d.default_errors, d.at_last_trained
    if before is None:
        why = f"{d.last['version']} has no count of default errors to compare with"
        return _plan(d, why), why
    if now and now >= 2 * before:
        why = f"default errors doubled since {d.last['version']} ({before} -> {now})"
        return _plan(d, why), why
    if d.champion.revoked:
        rows = sum(n for part in (d.last.get("data") or {}).values() for k, n in part.items() if k in ("train", "val"))
        if d.champion.rows and d.champion.rows >= MATERIAL * rows:
            why = (f"{d.champion.version} was revoked and the {d.champion.acted_sessions} sessions it acted in "
                   f"brought {d.champion.rows} labelled rows, over {MATERIAL:.0%} of the {rows} {d.last['version']} "
                   f"trained on")
            return _plan(d, why), why
        return None, (f"{d.champion.version} was revoked after acting in {d.champion.acted_sessions} sessions, which "
                      f"left {d.champion.rows} labelled rows: too little new data to train on again; a revocation is "
                      f"evidence, not a reason to train")
    return None, f"default errors {now}, not yet twice the {before} at {d.last['version']}"


def _plan(d: Diagnosis, why: str) -> Plan:
    """The rule planner's plan: train the configured recipe on the data as it stands now. It changes nothing, so its
    hypothesis is about the data, and it expects to reach the template's target."""
    gain = max(d.target - (d.reached or 0.0), 0.0)
    cost = d.minutes_per_row * d.train_rows if d.minutes_per_row is not None else None
    return Plan(hypothesis=(f"{why}: a model trained on the {d.train_rows} rows now in the ledger fixes at least "
                            f"{gain:.0%} of the {d.default_errors} default errors"),
                kind="recipe", change={}, points=list(d.points), predicted_gain=gain,
                evidence=("the gate's paired bootstrap on the round's test sessions: the candidate's fixed share of "
                          "the default errors rises above the champion's, and it harms no more sessions than the "
                          "risk allows"),
                cost_minutes=cost,
                data={"train_rows": d.train_rows, "min_test_sessions": d.min_sessions,
                      "default_errors": d.default_errors, "headroom": d.headroom})


def admit(rt, p: Plan, d: Diagnosis, params: dict) -> tuple[bool, list]:
    """The hard checks before anything trains. Each reason names what is missing and what would satisfy it; a plan
    that fails any of them trains nothing and the round is recorded as skipped with the reasons."""
    reasons = []
    risk = params["risk"]
    need = sessions_needed(risk, ALPHA)
    for name in p.points:
        point = d.points.get(name)
        if point is None:
            continue
        if point.error_sessions < need:
            reasons.append(f"{name}: {point.error_sessions} calibration sessions hold a default error, a threshold "
                           f"at risk {risk} needs {need}; no model trained on this data could ever act, so collect "
                           f"{need - point.error_sessions} more")
    if not d.fresh:
        reasons.append(f"fewer than {d.min_sessions} fresh test sessions: the gate could only validate, never "
                       f"promote; collect a generation of sessions the gate has not seen")
    for name in p.points:
        point = d.points.get(name)
        if point is None:
            continue
        if point.test_sessions < d.min_sessions:
            reasons.append(f"{name}: test items from {point.test_sessions} sessions, the gate needs {d.min_sessions}; "
                           f"neither a pass nor a block could come from this point")
        elif not point.headroom:
            reasons.append(f"{name}: no default errors among the {point.test_rows} test rows; there is nothing there "
                           f"for a model to fix, so no gain could be measured")
        elif point.mde >= p.per_row(point):
            reasons.append(f"{name}: a {point.test_rows}-row test set detects {point.mde:.2f}, the plan predicts "
                           f"{p.per_row(point):.2f} ({p.predicted_gain:.0%} of {point.headroom} default errors); "
                           f"collect more test sessions or aim at a larger gain")
    if d.reached is not None and d.reached >= d.target:
        reasons.append(f"target reached: {d.accepted} fixes {d.reached:.0%} of the default errors")
    if d.trained >= d.budget:
        reasons.append(f"training budget spent ({d.trained} of {d.budget})")
    reasons += repeated(rt, p)
    return not reasons, reasons


def repeated(rt, p: Plan) -> list:
    """Whether this hypothesis already failed the same way: an earlier candidate of the same kind made the same
    change, was not accepted, and saw as many training rows as this plan would, so it would meet the same data and
    fail again. More data makes it a new hypothesis."""
    rows = p.data.get("train_rows", 0)
    for e in experience.read(rt.root):
        old = e.get("plan") or {}
        if old.get("kind") != p.kind or old.get("change") != p.change or e.get("gate") == "accepted":
            continue
        if (old.get("data") or {}).get("train_rows", 0) >= rows:
            return [f"the same {p.kind} change already failed on as much data ({e.get('version')}: {e.get('gate')} - "
                    f"{'; '.join(e.get('reasons') or [])[:80]}); a new hypothesis or more data is needed"]
    return []


def decide(rt, points: list | None = None) -> tuple[Plan | None, bool, list, Diagnosis]:
    """One round's decision: diagnose, plan, admit. -> (the plan, whether to train, the reasons, the diagnosis). The
    reasons say why the round trains nothing, or why the planner proposed what it did."""
    d = diagnose(rt, points)
    p, why = plan(rt, d)
    if p is None:
        return None, False, [why], d
    ok, reasons = admit(rt, p, d, load_template(rt.root)[0])
    return p, ok, reasons or [why], d
