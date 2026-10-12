"""Releases (docs/design/EVALUATE.md §7; CONTRACTS §5, v1.2): the version an activity runs is the triple {W, R, K} —
the harness package, the improver's policy, the fixed core — with the release it came from and what installed it. Two
lines: the working line (`rsi/active.json`), moved by the held-out screens inside the activity, and the release line
(`rsi/released.json`), moved once by the dev gate at the end of it. Each pointer moves only by compare-and-swap; a
rollback only ever goes back to a release that was installed and is not revoked.

    Release        parent_ref, package_ref, improver_ref, core_ref, receipt_ref | screen_ref,
                   reason (seed | screen | gate | decision | rollback | import)
    GateReceipt    one paired gate's reading (EVALUATE §5): the plan, both arms' runs in its results object, the verdict
    InstallReceipt the fact of an install or a refusal: expected parent, operation, status, the pointer observed

A screen result installs on the working line only when it is accepted; a receipt promotes to the release line only
when a completed gate registered it ("evaluation_completed" event) and its verdict is accepted, confirmed when the plan
confirms. An activity without dev tasks has no gate: its working line is promoted by decision (reason `decision`, no
receipt; EVALUATE §7, stage 4c), and the final's two arms are its reading. Code identity is two hashes (`attempt_version`: what an attempt runs; `improver_version`: the rest); every K
carries both, so a changed `.py` is a new K; `code_version` is the hash of all of it, for reports.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..contracts import (ContractError, Events, Objects, Record, check_refs, locked, now, register, short,
                         write_json)

REASONS = ("seed", "screen", "gate", "decision", "rollback", "import")
VERDICTS = ("accepted", "rejected", "inconclusive", "invalid")
RECEIPT_KINDS = ("full", "confirmed")
INSTALL_STATUS = ("installed", "stale_parent", "installation_failed")
PACKAGE = Path(__file__).resolve().parents[1]
# The package's files besides its modules that pip installs: pyproject.toml's [tool.setuptools.package-data] says the
# same. The seeds are source trees whose every file is part of what runs.
PACKAGE_DATA = ("train/jobs/hosts/bootstrap.sh", "train/jobs/hosts/start.sh", "memory/sidecar/flows.yaml",
                "memory/sidecar/requirements-macos-arm64.lock", "improver/prompts/diagnose.md",
                "improver/prompts/package_api.md", "improver/prompts/playbook.md", "improver/prompts/propose.md")
SEEDS = "harness/seeds/"
# What an attempt runs (DESIGN §8, v1.2): the runtime, the harness and its seeds, the attempt's own evaluate modules,
# the adapters, the providers and the contracts. Everything else of the package is the improver's side.
ATTEMPT_PREFIXES = ("runtime/", "harness/", "adapters/", "providers/")
ATTEMPT_FILES = ("evaluate/attempt.py", "evaluate/results.py", "evaluate/limits.py", "evaluate/splits.py",
                 "contracts.py", "__init__.py")


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


def is_attempt_file(rel: str) -> bool:
    return rel.startswith(ATTEMPT_PREFIXES) or rel in ATTEMPT_FILES


def _hash(files) -> str:
    h = hashlib.sha256()
    for f in files:
        h.update(f.relative_to(PACKAGE).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def attempt_version() -> str:
    """A hash of the code an attempt runs: the same here and wherever a reading of the same package is comparable."""
    return _hash(f for f in package_files() if is_attempt_file(f.relative_to(PACKAGE).as_posix()))


def improver_version() -> str:
    """A hash of the rest: the improver, the memory, the activity's phases, the gate and the final."""
    return _hash(f for f in package_files() if not is_attempt_file(f.relative_to(PACKAGE).as_posix()))


def code_version() -> str:
    """A hash of every file of the package, for reports."""
    return _hash(package_files())


def attempt_identity(*, attempt_version: str, package_ref: str, limits: dict, model: dict, image_id,
                     split_hash: str) -> str:
    """AttemptIdentity (CONTRACTS §4): what makes two activities' readings of one package the same measurement — the
    attempt code, the package, the limits, the actor model's binding, the worker image and the frozen split."""
    from ..contracts import digest
    return digest({"attempt_version": attempt_version, "package_ref": package_ref, "limits": dict(limits or {}),
                   "model": {k: model.get(k) for k in ("provider", "identity", "effort", "max_tokens")},
                   "image_id": image_id, "split_hash": split_hash})[:16]


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
    screen_ref: str | None = None       # the held-out screen that accepted it (reason `screen`)

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
        if self.screen_ref is not None:
            check_refs([self.screen_ref], kind="screen", what="screen result")
        if (self.reason == "gate") != (self.receipt_ref is not None):
            raise ContractError("a release promoted by a gate names its receipt, and only such a release does")
        if (self.reason == "screen") != (self.screen_ref is not None):
            raise ContractError("a release installed by a held-out screen names the screen, and only such a release "
                                "does")


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
    """The release store of one activity: <workspace>/rsi/active.json (the working line), rsi/released.json (the
    release line), the release objects, and their events."""

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

    # ------------------------------------------------------------------ the release line
    @property
    def released_path(self) -> Path:
        return self.dir / "released.json"

    def released_pointer(self) -> dict:
        if not self.released_path.exists():
            return {"release_ref": None, "revision": 0, "last_operation_id": None}
        return json.loads(self.released_path.read_text(encoding="utf-8"))

    def released_ref(self) -> str | None:
        return self.released_pointer()["release_ref"]

    def released(self) -> Release | None:
        ref = self.released_ref()
        return self.load(ref) if ref else None

    def promote(self, work: Release, receipt_ref: str | None = None, *, reason: str = "gate",
                operation_id: str) -> Release:
        """The working line's release becomes the release line's W* — a release of `reason` built on it. With reason
        `gate` it carries the receipt, which must be registered by a completed gate and accepted (confirmed when its
        plan confirms); with reason `decision` (an activity without dev tasks, EVALUATE §7) there is no receipt.
        Promoting again under the same operation is the same release."""
        if reason not in ("gate", "decision"):
            raise ContractError(f"a promotion's reason is gate or decision, not {reason!r}")
        if reason == "gate":
            if not receipt_ref:
                raise NotAuthorized("a promotion by the gate names its receipt")
            receipt = self.objects.load(receipt_ref, GateReceipt)
            if receipt.verdict != "accepted":
                raise NotAuthorized(f"the receipt's verdict is {receipt.verdict}, not accepted")
            if not self.registered_receipt(receipt_ref):
                raise NotAuthorized("this receipt was not registered by a completed gate")
            if receipt.plan_ref and self.objects.has(receipt.plan_ref):
                plan = self.objects.get(receipt.plan_ref)
                if int(plan.get("confirmation_repeats") or 0) > 0 and receipt.kind != "confirmed":
                    raise NotAuthorized("the plan confirms before it releases and this receipt is a full reading, not "
                                        "a confirmed one")
            if receipt.subject_ref != work.package_ref:
                raise NotAuthorized("the receipt does not read the package of the release to promote")
        elif receipt_ref is not None:
            raise NotAuthorized("a promotion by decision carries no receipt")
        release = Release(parent_ref=work.ref(), package_ref=work.package_ref, improver_ref=work.improver_ref,
                          core_ref=work.core_ref, receipt_ref=receipt_ref, reason=reason)
        ref = self.objects.put(release)
        with locked(self.released_path):
            current = self.released_pointer()
            if current["release_ref"] == ref:
                return release
            write_json(self.released_path, {"release_ref": ref, "revision": int(current["revision"]) + 1,
                                            "last_operation_id": operation_id, "working": work.ref()})
        self.events.append("released", release=ref, working=work.ref(), receipt=receipt_ref, reason=reason,
                           operation=operation_id)
        self.events.append("installed", release=ref, reason=reason, operation=operation_id)
        return release

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

    def screened(self, package_ref: str, screen_ref: str, *, parent: Release | None = None) -> Release:
        """The working-line release a held-out screen's acceptance installs (IMPROVER §9)."""
        parent = parent or self.active()
        return Release(parent_ref=parent.ref(), package_ref=package_ref, improver_ref=parent.improver_ref,
                       core_ref=parent.core_ref, screen_ref=screen_ref, reason="screen")

    # ------------------------------------------------------------------ receipts
    def register_receipt(self, receipt_ref: str, **fields) -> dict:
        return self.events.append("evaluation_completed", receipt=receipt_ref, **fields)

    def registered_receipt(self, receipt_ref: str | None) -> bool:
        return bool(receipt_ref) and any(e.get("receipt") == receipt_ref
                                         for e in self.events.read("evaluation_completed"))

    # ------------------------------------------------------------------ install
    def install(self, candidate_release: Release, receipt_ref: str, *, expected_parent: str,
                operation_id: str) -> InstallReceipt:
        """Make a gate's candidate release the active one, or say exactly why not. The only commit point is the
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
        return self._commit(candidate_release, expected_parent=expected_parent, operation_id=operation_id)

    def install_screened(self, candidate_release: Release, screen_ref: str, *, expected_parent: str,
                         operation_id: str) -> InstallReceipt:
        """Make an accepted held-out screen's release the working line's active one (IMPROVER §9): the screen must say
        accepted and read this package; then the same CAS as a gate's install."""
        result = self.objects.get(screen_ref)
        if not isinstance(result, dict) or not result.get("accepted"):
            raise NotAuthorized("the screen did not accept the package: nothing installs on the working line")
        if candidate_release.screen_ref != screen_ref or candidate_release.package_ref != result.get("package_ref"):
            raise NotAuthorized("the release does not install the package this screen accepted")
        return self._commit(candidate_release, expected_parent=expected_parent, operation_id=operation_id)

    def _commit(self, candidate_release: Release, *, expected_parent: str, operation_id: str) -> InstallReceipt:
        if candidate_release.parent_ref != expected_parent:
            built_on = short(candidate_release.parent_ref) if candidate_release.parent_ref else None
            raise Stale(f"the candidate was built on {built_on}, not on {short(expected_parent)}")
        parent = self.load(expected_parent)
        if candidate_release.core_ref != parent.core_ref:
            raise NotAuthorized("an install stays within one K: the candidate's core is not its parent's")
        target = self.objects.put(candidate_release)
        intent_path = self.dir / "operations" / f"{operation_id}.json"
        intent = json.loads(intent_path.read_text(encoding="utf-8")) if intent_path.exists() else {}
        if intent and intent.get("target_release") != target:
            raise ValueError(f"operation {operation_id} was recorded for another release")
        if intent.get("status") == "installed" and intent.get("receipt"):
            return self.objects.load(intent["receipt"], InstallReceipt)     # committed before: the same answer
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
