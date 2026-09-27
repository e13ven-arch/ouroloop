"""Releases (docs/DESIGN.md invariant 7): the (model, decision spec, harness) triple a session runs with, the gate
records that accepted what may answer and act, the revocation list that stops a release from acting, and the version of
the code that gates and serves them."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .decision import _digest
from .ledger import file_key, locked, now, write_json

PACKAGE = Path(__file__).resolve().parent
# The package's files besides its modules that pip installs: pyproject.toml's [tool.setuptools.package-data], which
# must say the same.
PACKAGE_DATA = ("hosts/bootstrap.sh", "hosts/start.sh")


@dataclass(frozen=True)
class Release:
    model: str | None       # the champion's model_id (M9: <hf id>@<sha>); None before the first promotion
    spec: str               # hash of the registered points' specs, view versions included
    harness: str | None     # hash of the prompt, tools and StopGuard settings; None when ouroloop runs no harness

    def to_json(self) -> dict:
        return asdict(self)


def spec_hash(points: dict) -> str:
    return _digest({name: p.spec_hash for name, p in points.items()})


def harness_hash(harness: dict | None) -> str | None:
    return None if harness is None else _digest(harness)


def package_files() -> list[Path]:
    """The files of the ouroloop package that an install of it holds: its modules and PACKAGE_DATA. A stray file
    beside them (.DS_Store, an editor's swap file) is never installed, so it is no part of the code."""
    return sorted(p for p in PACKAGE.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                  and (p.suffix == ".py" or p.relative_to(PACKAGE).as_posix() in PACKAGE_DATA))


def code_version() -> str:
    """A hash of the ouroloop package's files (package_files): the code that builds, gates and serves, the same here
    and in any install of it."""
    h = hashlib.sha256()
    for f in package_files():
        h.update(f.relative_to(PACKAGE).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


class Accepted:
    """The gate records that accepted a candidate, from their copies in <workspace>/gates/ (re-read when they change):
    record hash -> the candidate it accepted, and the calibrations its promotion stores. Only a champion promoted
    through such a record answers, and only a calibration as such a record accepted it acts (docs/DESIGN.md invariant
    1): a champion pointer, card or calibration written by hand, or one a gate rejected, never does. A rollback
    withdraws the record's acceptance: its model still answers the sessions pinned to it, but neither it nor its
    calibrations act again, and it cannot be promoted again."""

    def __init__(self, root: str | Path):
        self.dir = Path(root) / "gates"
        self._key: tuple | None = None
        self._by: dict = {}
        self._cal: dict = {}
        self._withdrawn: set = set()

    def by(self) -> dict[str, str]:
        paths = sorted(self.dir.glob("r_*.used.json"))
        key = tuple((p.name, file_key(p)) for p in paths)
        if key != self._key:
            by, cal, withdrawn = {}, {}, set()
            for p in paths:
                try:
                    use = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if use.get("outcome") == "accepted" and use.get("record"):
                    by[use["record"]] = use.get("by")
                    if use.get("revoked"):
                        withdrawn.add(use["record"])
                    for c in use.get("calibrations") or []:
                        cal[(use["record"], c["model"], c["spec"], c["point"])] = {
                            "temperature": c["temperature"], "thresholds": c["thresholds"]}
            self._by, self._cal, self._withdrawn, self._key = by, cal, withdrawn, key
        return self._by

    def calibration(self, record: str | None, model: str, spec: str, point: str) -> dict | None:
        """The temperature and thresholds that `record` accepted for this model, spec and point, while it stands."""
        self.by()
        return None if record in self._withdrawn else self._cal.get((record, model, spec, point))

    def promoted(self, card: dict, standing: bool = False) -> bool:
        """The card's model was promoted by the gate record it names (`standing`: and was not rolled back since)."""
        record = card.get("gate_record")
        return (bool(record) and self.by().get(record) == card.get("version")
                and not (standing and record in self._withdrawn))


class Revoked:
    """<workspace>/harness/revoked.json: registry versions and model ids that may never act again. Checked on every
    ask (re-read when the file changes): a session whose model is on it takes its defaults from then on."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._key: tuple | None = None
        self._ids: frozenset = frozenset()

    def ids(self) -> frozenset:
        key = file_key(self.path)
        if key != self._key:
            data = json.loads(self.path.read_text(encoding="utf-8")) if key else {}
            self._ids = frozenset(x for e in data.get("revoked", []) for x in (e.get("version"), e.get("model")) if x)
            self._key = key
        return self._ids

    def hit(self, *ids: str | None) -> str | None:
        """The first of `ids` that is revoked."""
        revoked = self.ids()
        return next((i for i in ids if i and i in revoked), None)


def revoke(root: str | Path, version: str | None, model: str | None, reason: str) -> bool:
    """Put the version and model on the revocation list. -> False when both were on it already (another process
    revoked them first), and nothing is written."""
    path = Path(root) / "harness" / "revoked.json"
    with locked(path):
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"revoked": []}
        listed = {x for e in data["revoked"] for x in (e.get("version"), e.get("model")) if x}
        if {version, model} - {None} <= listed:
            return False
        data["revoked"].append({"ts": now(), "version": version, "model": model, "reason": reason})
        write_json(path, data)
    return True


# ---------------------------------------------------------------------------- the task/research profile
# Everything below serves the complete-version profile of docs/modules/03_EVALUATION_RELEASE.md §4: one
# SystemRelease at a time, in <workspace>/rsi/active.json, installed by compare-and-swap. The legacy decision
# profile above (gate records, models/champion.json, calibrations) is untouched and is never read from here.
PROFILES = ("task", "research", "legacy")


class Stale(RuntimeError):
    """The parent version moved while the candidate was evaluated. Not something to rebase automatically."""


class NotAuthorized(RuntimeError):
    """This release may not run this way: a permit is not permission to start a normal session or to install."""


def resolve_profile(root: str | Path, profile: str | None) -> str:
    """Which pointer a session runs from. A workspace may hold both an old champion and a new release, so the
    profile is always named: assembling one session out of two pointers is how a model no gate accepted ends up
    acting (CT-05)."""
    root = Path(root)
    has_new = (root / "rsi" / "active.json").exists()
    has_legacy = (root / "models" / "champion.json").exists()
    if profile is None:
        if has_new and has_legacy:
            raise ValueError(f"{root} holds both a legacy champion and an installed release: name the profile "
                             f"({list(PROFILES)}); a session reads exactly one of them")
        return "task" if has_new else "legacy"
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}: one of {list(PROFILES)}")
    if profile in ("task", "research") and not has_new:
        raise ValueError(f"{root} has no rsi/active.json: nothing is installed for the {profile} profile")
    return profile


class Releases:
    """The complete-version store: releases, the active pointer, authorisations, installs and revocations."""

    def __init__(self, root: str | Path, objects=None):
        from .evolve import contracts as ct
        self.root = Path(root)
        self.dir = self.root / "rsi"
        self.objects = objects or ct.Objects(self.root)
        self.events = ct.Events(self.root)
        self._ct = ct

    # ------------------------------------------------------------------ pointer
    @property
    def active_path(self) -> Path:
        return self.dir / "active.json"

    def active(self) -> dict:
        if not self.active_path.exists():
            return {"release_ref": None, "revision": 0, "last_operation_id": None}
        return json.loads(self.active_path.read_text(encoding="utf-8"))

    def defaults(self) -> str | None:
        path = self.dir / "defaults.json"
        return json.loads(path.read_text(encoding="utf-8"))["release_ref"] if path.exists() else None

    # ------------------------------------------------------------------ authorisations
    def bootstrap(self, worker_ref: str, research_ref: str, core_ref: str, *, reason: str = "initial version",
                  activate: bool = True) -> str:
        """The first release, authorised by fixed configuration rather than by an evaluation. A baseline may not
        call this an improvement: nothing has been compared yet."""
        ct = self._ct
        auth = self.objects.put(ct.Authorization(kind="bootstrap", reason=reason))
        ref = self.objects.put(ct.SystemRelease(worker_ref=worker_ref, research_ref=research_ref,
                                                core_ref=core_ref, authorization_ref=auth))
        self.events.append("release_created", release=ref, authorization="bootstrap", reason=reason)
        if activate:
            write_json(self.dir / "defaults.json", {"release_ref": ref, "ts": now()})
            with locked(self.active_path):
                if not self.active_path.exists():
                    write_json(self.active_path, {"release_ref": ref, "revision": 1, "last_operation_id": None})
                    self.events.append("installed", release=ref, revision=1, authorization="bootstrap")
        return ref

    def permit(self, subject_ref: str, parent_release: str, plan_ref: str, scope_id: str, core_ref: str, *,
               expires_at: str = "") -> str:
        """Permission to evaluate one candidate policy set in one plan's scope. It cannot install and cannot open
        a normal session, and it never turns into an acceptance (CT-06)."""
        ct = self._ct
        ref = self.objects.put(ct.EvaluationPermit(subject_ref=subject_ref, parent_release=parent_release,
                                                  plan_ref=plan_ref, scope_id=scope_id, core_ref=core_ref,
                                                  expires_at=expires_at))
        self.events.append("permit_issued", permit=ref, subject=subject_ref, plan=plan_ref, scope=scope_id)
        return ref

    def candidate_release(self, parent_release: str, set_ref: str, permit_ref: str) -> str:
        """A release for an evaluation arm: the parent with one owner's set replaced, authorised by the permit."""
        ct = self._ct
        auth = self.objects.put(ct.Authorization(kind="evaluation_permit", ref=permit_ref,
                                                reason="isolated evaluation"))
        ref = self.objects.put(self._swap(parent_release, set_ref, auth))
        self.events.append("release_created", release=ref, authorization="evaluation_permit", permit=permit_ref)
        return ref

    def accepted_release(self, parent_release: str, set_ref: str, receipt_ref: str) -> str:
        """The release that goes live: the same W/R contents as the evaluated arm, a different identity, and an
        authorisation that names the receipt (docs/modules/00_CONTRACTS.md §1)."""
        ct = self._ct
        auth = self.objects.put(ct.Authorization(kind="accepted", ref=receipt_ref, reason="accepted receipt"))
        ref = self.objects.put(self._swap(parent_release, set_ref, auth))
        self.events.append("release_created", release=ref, authorization="accepted", receipt=receipt_ref)
        return ref

    def _swap(self, parent_release: str, set_ref: str, authorization_ref: str):
        """The parent release with the owner of `set_ref` replaced, the other side carried over exactly."""
        ct = self._ct
        parent = self.objects.load(parent_release, ct.SystemRelease)
        owner = self.objects.load(set_ref, ct.PolicySet).owner
        return ct.SystemRelease(worker_ref=set_ref if owner == "worker" else parent.worker_ref,
                                research_ref=set_ref if owner == "research" else parent.research_ref,
                                core_ref=parent.core_ref, authorization_ref=authorization_ref,
                                parent_ref=parent_release)

    def authorize(self, release_ref: str, *, profile: str, permit_ref: str | None = None,
                  plan_ref: str | None = None, scope_id: str | None = None) -> str:
        """Whether this release may run this way, and under which authorisation. -> the authorisation kind."""
        ct = self._ct
        rel = self.objects.load(release_ref, ct.SystemRelease)
        auth = self.objects.load(rel.authorization_ref, ct.Authorization)
        if self.revoked(release_ref):
            raise NotAuthorized(f"{ct.short(release_ref)} is revoked")
        if profile == "evaluation":
            if auth.kind != "evaluation_permit":
                raise NotAuthorized(f"an evaluation arm runs under a permit, not a {auth.kind} authorization")
            permit = self.objects.load(auth.ref, ct.EvaluationPermit)
            if permit_ref and permit_ref != auth.ref:
                raise NotAuthorized("this release was built for another permit")
            if plan_ref and permit.plan_ref != plan_ref:
                raise NotAuthorized(f"the permit is for plan {ct.short(permit.plan_ref)}, not "
                                    f"{ct.short(plan_ref)}")
            if scope_id and permit.scope_id != scope_id:
                raise NotAuthorized(f"the permit is for scope {permit.scope_id!r}, not {scope_id!r}")
            if permit.expires_at and permit.expires_at < now():
                raise NotAuthorized("the permit has expired")
            return auth.kind
        if auth.kind == "evaluation_permit":
            raise NotAuthorized(f"{ct.short(release_ref)} carries an evaluation permit: it may be evaluated in "
                                f"isolation, and may not open a normal session or be installed")
        if auth.kind == "accepted" and not self.registered_receipt(auth.ref):
            raise NotAuthorized(f"{ct.short(release_ref)} names a receipt no evaluator registered")
        return auth.kind

    def registered_receipt(self, receipt_ref: str | None) -> bool:
        """Whether a completed evaluation registered this receipt. The object store alone is not enough: a
        receipt is a fact about a finished operation, so the event is what makes it one."""
        if not receipt_ref:
            return False
        return any(e.get("receipt") == receipt_ref for e in self.events.of("evaluation_completed"))

    def register_receipt(self, receipt_ref: str, **fields) -> dict:
        return self.events.append("evaluation_completed", receipt=receipt_ref, **fields)

    # ------------------------------------------------------------------ install
    def install(self, receipt_ref: str, expected_parent: str | None, operation_id: str, *,
                preflight: Callable[[str], dict] | None = None):
        """Make the accepted release the active one, or say exactly why not.

        The only commit point is the replacement of active.json under the workspace lock. Everything the pointer
        will refer to is durable first, so the pointer never names a half-written object.
        """
        ct = self._ct
        receipt = self.objects.load(receipt_ref, ct.EvaluationReceipt)
        if receipt.verdict != "accepted":
            raise NotAuthorized(f"the receipt's verdict is {receipt.verdict}, not accepted")
        if not self.registered_receipt(receipt_ref):
            raise NotAuthorized("this receipt was not registered by a completed evaluation")
        if expected_parent is not None:
            parent_of_plan = self.objects.load(receipt.subject_ref, ct.PolicySet).parent_ref
            parent_sets = self.objects.load(expected_parent, ct.SystemRelease)
            if parent_of_plan not in (None, parent_sets.worker_ref, parent_sets.research_ref):
                raise Stale(f"the candidate was built on {ct.short(parent_of_plan)}, which is not part of "
                            f"{ct.short(expected_parent)}")
        target = self.accepted_release(expected_parent, receipt.subject_ref, receipt_ref)
        return self.commit(target, expected_parent, operation_id, preflight=preflight)

    def commit(self, target: str, expected_parent: str | None, operation_id: str, *,
               preflight: Callable[[str], dict] | None = None):
        """Preflight, then the compare-and-swap. Separated from `install` so a failed installation can be
        retried with the same artifacts, without evaluating anything again (ER-06)."""
        ct = self._ct
        intent_path = self.dir / "operations" / f"{operation_id}.json"
        intent = json.loads(intent_path.read_text(encoding="utf-8")) if intent_path.exists() else {}
        if intent and intent.get("target_release") != target:
            raise ValueError(f"operation {operation_id} was recorded for another release "
                             f"({ct.short(intent['target_release'])})")
        write_json(intent_path, {"operation_id": operation_id, "target_release": target,
                                 "expected_parent": expected_parent, "status": "intent", "ts": now()})
        preflight_ref = None
        if preflight is not None:
            try:
                preflight_ref = self.objects.put_json("change", {"preflight": preflight(target)})
            except Exception as e:  # noqa: BLE001 - a candidate that cannot be assembled does not go live
                return self._finish(ct.InstallationReceipt(operation_id=operation_id,
                                                           expected_parent=expected_parent, target_release=target,
                                                           status="installation_failed",
                                                           observed_pointer=self.active()["release_ref"],
                                                           reason=f"preflight: {type(e).__name__}: {e}"[:400]))
        with locked(self.active_path):
            current = self.active()
            if current["release_ref"] == target and current.get("last_operation_id") == operation_id:
                receipt = ct.InstallationReceipt(operation_id=operation_id, expected_parent=expected_parent,
                                                 target_release=target, preflight_ref=preflight_ref,
                                                 status="installed", observed_pointer=target,
                                                 reason="already committed by this operation")
                return self._finish(receipt)          # a replay of the same operation commits once (ER-08)
            if current["release_ref"] != expected_parent:
                return self._finish(ct.InstallationReceipt(
                    operation_id=operation_id, expected_parent=expected_parent, target_release=target,
                    preflight_ref=preflight_ref, status="stale_parent",
                    observed_pointer=current["release_ref"],
                    reason="the active release moved while this candidate was evaluated"))
            if self.revoked(target):
                return self._finish(ct.InstallationReceipt(
                    operation_id=operation_id, expected_parent=expected_parent, target_release=target,
                    preflight_ref=preflight_ref, status="installation_failed",
                    observed_pointer=current["release_ref"], reason="the release or an ancestor is revoked"))
            write_json(self.active_path, {"release_ref": target, "revision": int(current["revision"]) + 1,
                                          "last_operation_id": operation_id})
        return self._finish(ct.InstallationReceipt(operation_id=operation_id, expected_parent=expected_parent,
                                                   target_release=target, preflight_ref=preflight_ref,
                                                   status="installed", observed_pointer=target))

    def _finish(self, receipt):
        ref = self.objects.put(receipt)
        write_json(self.dir / "operations" / f"{receipt.operation_id}.json",
                   {"operation_id": receipt.operation_id, "target_release": receipt.target_release,
                    "expected_parent": receipt.expected_parent, "status": receipt.status,
                    "receipt": ref, "ts": now()})
        self.events.append(receipt.status, release=receipt.target_release, operation=receipt.operation_id,
                           receipt=ref, reason=receipt.reason or "")
        return receipt

    def recover(self, operation_id: str):
        """What an interrupted install left behind: the receipt if it finished, otherwise the intent to continue
        from. Recovery reads facts (the pointer, the operation), never the last log line."""
        path = self.dir / "operations" / f"{operation_id}.json"
        if not path.exists():
            return None
        intent = json.loads(path.read_text(encoding="utf-8"))
        if intent.get("receipt"):
            return self.objects.load(intent["receipt"], self._ct.InstallationReceipt)
        return intent

    # ------------------------------------------------------------------ revocation
    def revocations(self) -> dict:
        path = self.dir / "revoked.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"revoked": []}

    def revoked(self, release_ref: str | None = None) -> bool | set:
        """The revoked releases and policy sets, or whether this release (or an ancestor of it) is one."""
        ids = {e["id"] for e in self.revocations()["revoked"]}
        if release_ref is None:
            return ids
        ct = self._ct
        ref: str | None = release_ref
        seen = set()
        while ref and ref not in seen:
            seen.add(ref)
            if ref in ids:
                return True
            rel = self.objects.load(ref, ct.SystemRelease)
            if ids & {rel.worker_ref, rel.research_ref}:
                return True
            ref = rel.parent_ref
        return False

    def revoke(self, ref: str, reason: str) -> bool:
        """Put a release or policy set out of use. Recorded first, before any pointer moves: there is no moment
        where a revoked version is still the one a new session would pin."""
        path = self.dir / "revoked.json"
        with locked(path):
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"revoked": []}
            if any(e["id"] == ref for e in data["revoked"]):
                return False
            data["revoked"].append({"id": ref, "reason": reason, "ts": now()})
            write_json(path, data)
        self.events.append("revoked", target=ref, reason=reason)
        return True

    def last_good(self, release_ref: str | None) -> str | None:
        """The nearest ancestor that is authorised, not revoked and built on the same core; None: the defaults."""
        ct = self._ct
        if not release_ref:
            return None
        core = self.objects.load(release_ref, ct.SystemRelease).core_ref
        ref = self.objects.load(release_ref, ct.SystemRelease).parent_ref
        seen = set()
        while ref and ref not in seen:
            seen.add(ref)
            rel = self.objects.load(ref, ct.SystemRelease)
            auth = self.objects.load(rel.authorization_ref, ct.Authorization)
            if (rel.core_ref == core and not self.revoked(ref)
                    and (auth.kind == "bootstrap" or self.registered_receipt(auth.ref))):
                return ref
            ref = rel.parent_ref
        return None

    def rollback(self, reason: str, *, operation_id: str | None = None):
        """Revoke the active release and move the pointer to the nearest good ancestor, or to the defaults.

        Never to a version that was assembled here for the occasion: a pointer may only name a release that was
        evaluated and installed before (docs/modules/03_EVALUATION_RELEASE.md §4).
        """
        current = self.active()["release_ref"]
        if current:
            self.revoke(current, reason)
        target = self.last_good(current) or self.defaults()
        if target is None:
            raise NotAuthorized("no authorised ancestor and no defaults release to fall back to")
        op = operation_id or f"rollback-{now()}"
        with locked(self.active_path):
            state = self.active()
            write_json(self.active_path, {"release_ref": target, "revision": int(state["revision"]) + 1,
                                          "last_operation_id": op})
        self.events.append("rolled_back", release=target, retired=current, reason=reason, operation=op)
        return target
