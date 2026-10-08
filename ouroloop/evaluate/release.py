"""Releases (docs/design/EVALUATE.md §7; CONTRACTS §5): the version an activity runs is the triple {W, R, K} — the
harness package, the improver's policy, the fixed core — with the release it came from and what installed it. The
active pointer moves only by compare-and-swap; a rollback only ever goes back to a release that was installed and is
not revoked.

    Release        parent_ref, package_ref, improver_ref, core_ref, receipt_ref, reason (seed | gate | rollback | import)
    GateReceipt    one paired gate's reading (EVALUATE §5): the plan, both arms' runs in its results object, the verdict
    InstallReceipt the fact of an install or a refusal: expected parent, operation, status, the pointer observed

A receipt installs only when a completed gate registered it ("evaluation_completed" event) and its verdict is accepted;
a plan that confirms before it installs takes only a confirmed reading. Code identity (`code_version`) is a hash of the
package's files: every K carries it, so a changed `.py` is a new K.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..contracts import (ContractError, Events, Objects, Record, check_refs, locked, now, register, short,
                         write_json)

REASONS = ("seed", "gate", "rollback", "import")
VERDICTS = ("accepted", "rejected", "inconclusive", "invalid")
RECEIPT_KINDS = ("full", "confirmed")
INSTALL_STATUS = ("installed", "stale_parent", "installation_failed")
PACKAGE = Path(__file__).resolve().parents[1]
# The package's files besides its modules that pip installs: pyproject.toml's [tool.setuptools.package-data] says the
# same. The seeds are source trees whose every file is part of what runs.
PACKAGE_DATA = ("train/jobs/hosts/bootstrap.sh", "train/jobs/hosts/start.sh")
SEEDS = "harness/seeds/"


def package_files() -> list[Path]:
    """The files of the ouroloop package an install holds: its modules, PACKAGE_DATA and the seed trees."""
    out = []
    for p in PACKAGE.rglob("*"):
        if not p.is_file() or "__pycache__" in p.parts or p.name == ".DS_Store" or p.suffix == ".pyc":
            continue
        rel = p.relative_to(PACKAGE).as_posix()
        if p.suffix == ".py" or rel in PACKAGE_DATA or rel.startswith(SEEDS):
            out.append(p)
    return sorted(out)


def code_version() -> str:
    """A hash of the package's files: the code that builds, gates and serves, the same here and in any install."""
    h = hashlib.sha256()
    for f in package_files():
        h.update(f.relative_to(PACKAGE).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


class Stale(RuntimeError):
    """The parent version moved while the candidate was evaluated. Not something to rebase automatically."""


class NotAuthorized(RuntimeError):
    """This release may not be installed or rolled back to."""


@register
@dataclass(frozen=True)
class Release(Record):
    TYPE = "release"
    parent_ref: str | None = None
    package_ref: str = ""
    improver_ref: str = ""
    core_ref: str = ""
    receipt_ref: str | None = None
    reason: str = "seed"

    def __post_init__(self) -> None:
        if self.reason not in REASONS:
            raise ContractError(f"release reason {self.reason!r}: one of {list(REASONS)}")
        check_refs([self.package_ref], kind="package", what="package")
        check_refs([self.core_ref], kind="core", what="core")
        check_refs([self.improver_ref], kind="improver", what="improver policy")
        if self.parent_ref is not None:
            check_refs([self.parent_ref], kind="release", what="parent release")
        if self.receipt_ref is not None:
            check_refs([self.receipt_ref], kind="receipt", what="gate receipt")
        if (self.reason == "gate") != (self.receipt_ref is not None):
            raise ContractError("a release installed by a gate names its receipt, and only such a release does")


@register
@dataclass(frozen=True)
class GateReceipt(Record):
    """What a frozen paired gate concluded. Only this, registered by a completed gate, can authorise an install."""
    TYPE = "receipt"
    plan_ref: str = ""
    subject_ref: str = ""                         # the candidate package
    control_ref: str = ""                         # the installed package it was compared with
    parent_release: str = ""
    results_ref: str = ""                         # every attempt of every arm, failures included
    verdict: str = "inconclusive"
    reasons: tuple = ()
    metrics: dict = field(default_factory=dict)
    uncertainty: dict = field(default_factory=dict)
    costs: dict = field(default_factory=dict)
    kind: str = "full"

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ContractError(f"verdict {self.verdict!r}: one of {list(VERDICTS)}")
        if self.kind not in RECEIPT_KINDS:
            raise ContractError(f"receipt kind {self.kind!r}: one of {list(RECEIPT_KINDS)}")
        object.__setattr__(self, "reasons", tuple(self.reasons))


@register
@dataclass(frozen=True)
class InstallReceipt(Record):
    TYPE = "install"
    operation_id: str = ""
    expected_parent: str | None = None
    target_release: str = ""
    status: str = "installed"
    observed_pointer: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in INSTALL_STATUS:
            raise ContractError(f"install status {self.status!r}: one of {list(INSTALL_STATUS)}")


class Releases:
    """The release store of one activity: <workspace>/rsi/active.json, the release objects, and their events."""

    def __init__(self, root: str | Path, objects: Objects | None = None):
        self.root = Path(root)
        self.dir = self.root / "rsi"
        self.objects = objects or Objects(self.root)
        self.events = Events(self.root)

    # ------------------------------------------------------------------ the pointer
    @property
    def active_path(self) -> Path:
        return self.dir / "active.json"

    def pointer(self) -> dict:
        if not self.active_path.exists():
            return {"release_ref": None, "revision": 0, "last_operation_id": None}
        return json.loads(self.active_path.read_text(encoding="utf-8"))

    def active_ref(self) -> str | None:
        return self.pointer()["release_ref"]

    def active(self) -> Release:
        ref = self.active_ref()
        if ref is None:
            raise NotAuthorized("no release is active: the activity has not written its seed release")
        return self.load(ref)

    def load(self, ref: str) -> Release:
        return self.objects.load(ref, Release)

    # ------------------------------------------------------------------ the seed
    def seed(self, package_ref: str, improver_ref: str, core_ref: str) -> Release:
        """The first release, installed by fixed configuration rather than an evaluation. A second call with the same
        triple is the same release; a pointer already on another release is left alone."""
        release = Release(parent_ref=None, package_ref=package_ref, improver_ref=improver_ref, core_ref=core_ref,
                          receipt_ref=None, reason="seed")
        ref = self.objects.put(release)
        with locked(self.active_path):
            if not self.active_path.exists():
                write_json(self.active_path, {"release_ref": ref, "revision": 1, "last_operation_id": None})
                self.events.append("installed", release=ref, revision=1, reason="seed")
        return release

    def candidate(self, package_ref: str, receipt_ref: str, *, parent: Release | None = None) -> Release:
        """The release a gate's receipt would install: the parent with W replaced, R and K carried over."""
        parent = parent or self.active()
        return Release(parent_ref=parent.ref(), package_ref=package_ref, improver_ref=parent.improver_ref,
                       core_ref=parent.core_ref, receipt_ref=receipt_ref, reason="gate")

    # ------------------------------------------------------------------ receipts
    def register_receipt(self, receipt_ref: str, **fields) -> dict:
        return self.events.append("evaluation_completed", receipt=receipt_ref, **fields)

    def registered_receipt(self, receipt_ref: str | None) -> bool:
        return bool(receipt_ref) and any(e.get("receipt") == receipt_ref
                                         for e in self.events.read("evaluation_completed"))

    # ------------------------------------------------------------------ install
    def install(self, candidate_release: Release, receipt_ref: str, *, expected_parent: str,
                operation_id: str) -> InstallReceipt:
        """Make the candidate release the active one, or say exactly why not. The only commit point is the
        replacement of active.json under its lock; a replay of the same operation commits once."""
        receipt = self.objects.load(receipt_ref, GateReceipt)
        if receipt.verdict != "accepted":
            raise NotAuthorized(f"the receipt's verdict is {receipt.verdict}, not accepted")
        if not self.registered_receipt(receipt_ref):
            raise NotAuthorized("this receipt was not registered by a completed gate")
        if receipt.plan_ref and self.objects.has(receipt.plan_ref):
            plan = self.objects.get(receipt.plan_ref)
            if int(plan.get("confirmation_repeats") or 0) > 0 and receipt.kind != "confirmed":
                raise NotAuthorized("the plan confirms a candidate before it installs and this receipt is a full "
                                    "reading, not a confirmed one")
        if candidate_release.receipt_ref != receipt_ref or candidate_release.package_ref != receipt.subject_ref:
            raise NotAuthorized("the release does not install the package this receipt accepted")
        if candidate_release.parent_ref != expected_parent:
            built_on = short(candidate_release.parent_ref) if candidate_release.parent_ref else None
            raise Stale(f"the candidate was built on {built_on}, not on {short(expected_parent)}")
        parent = self.load(expected_parent)
        if candidate_release.core_ref != parent.core_ref:
            raise NotAuthorized("a gate installs within one K: the candidate's core is not its parent's")
        target = self.objects.put(candidate_release)
        intent_path = self.dir / "operations" / f"{operation_id}.json"
        intent = json.loads(intent_path.read_text(encoding="utf-8")) if intent_path.exists() else {}
        if intent and intent.get("target_release") != target:
            raise ValueError(f"operation {operation_id} was recorded for another release")
        write_json(intent_path, {"operation_id": operation_id, "target_release": target,
                                 "expected_parent": expected_parent, "status": "intent", "ts": now()})
        with locked(self.active_path):
            current = self.pointer()
            if current["release_ref"] == target and current.get("last_operation_id") == operation_id:
                return self._finish(InstallReceipt(operation_id, expected_parent, target, "installed", target,
                                                   "already committed by this operation"))
            if current["release_ref"] != expected_parent:
                return self._finish(InstallReceipt(operation_id, expected_parent, target, "stale_parent",
                                                   current["release_ref"],
                                                   "the active release moved while this candidate was evaluated"))
            if self.revoked(target):
                return self._finish(InstallReceipt(operation_id, expected_parent, target, "installation_failed",
                                                   current["release_ref"], "the release or an ancestor is revoked"))
            write_json(self.active_path, {"release_ref": target, "revision": int(current["revision"]) + 1,
                                          "last_operation_id": operation_id})
        return self._finish(InstallReceipt(operation_id, expected_parent, target, "installed", target, None))

    def _finish(self, receipt: InstallReceipt) -> InstallReceipt:
        ref = self.objects.put(receipt)
        write_json(self.dir / "operations" / f"{receipt.operation_id}.json",
                   {"operation_id": receipt.operation_id, "target_release": receipt.target_release,
                    "expected_parent": receipt.expected_parent, "status": receipt.status, "receipt": ref, "ts": now()})
        self.events.append(receipt.status, release=receipt.target_release, operation=receipt.operation_id,
                           receipt=ref, reason=receipt.reason or "")
        return receipt

    def recover(self, operation_id: str):
        """What an interrupted install left behind: the receipt if it finished, otherwise the intent."""
        path = self.dir / "operations" / f"{operation_id}.json"
        if not path.exists():
            return None
        intent = json.loads(path.read_text(encoding="utf-8"))
        return self.objects.load(intent["receipt"], InstallReceipt) if intent.get("receipt") else intent

    # ------------------------------------------------------------------ revocation and rollback
    def revocations(self) -> dict:
        path = self.dir / "revoked.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"revoked": []}

    def revoked(self, release_ref: str) -> bool:
        """Whether this release or an ancestor of it is revoked."""
        ids = {e["id"] for e in self.revocations()["revoked"]}
        ref, seen = release_ref, set()
        while ref and ref not in seen:
            seen.add(ref)
            if ref in ids:
                return True
            ref = self.load(ref).parent_ref
        return False

    def revoke(self, ref: str, reason: str) -> bool:
        """Put a release out of use, recorded before any pointer moves. -> False when it already was."""
        path = self.dir / "revoked.json"
        with locked(path):
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"revoked": []}
            if any(e["id"] == ref for e in data["revoked"]):
                return False
            data["revoked"].append({"id": ref, "reason": reason, "ts": now()})
            write_json(path, data)
        self.events.append("revoked", target=ref, reason=reason)
        return True

    def installed(self) -> set[str]:
        return {e.get("release") for e in self.events.read("installed")}

    def last_good(self, ref: str | None = None) -> str | None:
        """The nearest ancestor of `ref` (the active release by default) that was installed, is not revoked and has
        the same core; None when there is none."""
        ref = ref or self.active_ref()
        if not ref:
            return None
        core = self.load(ref).core_ref
        installed = self.installed()
        cur, seen = self.load(ref).parent_ref, set()
        while cur and cur not in seen:
            seen.add(cur)
            rel = self.load(cur)
            if rel.core_ref == core and cur in installed and not self.revoked(cur):
                return cur
            cur = rel.parent_ref
        return None

    def rollback(self, reason: str, *, operation_id: str) -> InstallReceipt:
        """Revoke the active release and move the pointer to its last good ancestor. Without one, nothing is revoked
        and NotAuthorized says so."""
        current = self.active_ref()
        target = self.last_good(current)
        if target is None:
            raise NotAuthorized("no installed, unrevoked ancestor to fall back to; nothing was revoked")
        self.revoke(current, reason)
        with locked(self.active_path):
            state = self.pointer()
            write_json(self.active_path, {"release_ref": target, "revision": int(state["revision"]) + 1,
                                          "last_operation_id": operation_id})
        self.events.append("rolled_back", release=target, retired=current, reason=reason, operation=operation_id)
        return self._finish(InstallReceipt(operation_id, current, target, "installed", target, f"rollback: {reason}"))

    def history(self) -> list[Release]:
        """The active release and its ancestors, newest first."""
        out, ref, seen = [], self.active_ref(), set()
        while ref and ref not in seen:
            seen.add(ref)
            rel = self.load(ref)
            out.append(rel)
            ref = rel.parent_ref
        return out
