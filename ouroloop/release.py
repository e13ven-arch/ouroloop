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


def code_version() -> str:
    """A hash of every file of the ouroloop package: the code that builds, gates and serves."""
    h = hashlib.sha256()
    for f in sorted(p for p in PACKAGE.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(f.relative_to(PACKAGE).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


class Accepted:
    """The gate records that accepted a candidate, from their copies in <workspace>/gates/ (re-read when they change):
    record hash -> the candidate it accepted. Only a champion promoted through such a record, and thresholds stored
    with one, may answer and act (PLAN_v0.2 §0 rule 3): a champion pointer, card or calibration written by hand, or
    one a gate rejected, never does."""

    def __init__(self, root: str | Path):
        self.dir = Path(root) / "gates"
        self._key: tuple | None = None
        self._by: dict = {}

    def by(self) -> dict[str, str]:
        paths = sorted(self.dir.glob("r_*.used.json"))
        key = tuple((p.name, file_key(p)) for p in paths)
        if key != self._key:
            by = {}
            for p in paths:
                try:
                    use = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if use.get("outcome") == "accepted" and use.get("record"):
                    by[use["record"]] = use.get("by")
            self._by, self._key = by, key
        return self._by

    def promoted(self, card: dict) -> bool:
        """The card's model was promoted by the gate record it names."""
        return bool(card.get("gate_record")) and self.by().get(card["gate_record"]) == card.get("version")


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
