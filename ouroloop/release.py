"""Releases (PLAN_v0.2 M8): the (model, decision spec, harness) triple a session runs with, the gate records that
accepted what may answer and act, the revocation list that stops a release from acting, and the version of the code
that gates and serves them."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

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
    through such a record answers, and only a calibration as such a record accepted it acts (PLAN_v0.2 §0 rule 3): a
    champion pointer, card or calibration written by hand, or one a gate rejected, never does. A rollback withdraws
    the record's acceptance: its model still answers the sessions pinned to it, but neither it nor its calibrations
    act again, and it cannot be promoted again."""

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


def revoke(root: str | Path, version: str | None, model: str | None, reason: str) -> None:
    path = Path(root) / "harness" / "revoked.json"
    with locked(path):
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"revoked": []}
        data["revoked"].append({"ts": now(), "version": version, "model": model, "reason": reason})
        write_json(path, data)
