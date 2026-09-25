"""The model registry (PLAN §4.6, PLAN_v0.2 M8): one card per trained candidate, written before it is evaluated; a
champion pointer that only a compare-and-swap promotion moves; one-step rollback, which revokes the retired model and
withdraws the gate record's acceptance of it."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..ledger import PromotionConflict, locked, now, write_json
from ..release import Accepted, Revoked, revoke


class Registry:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.dir = self.root / "models"

    def versions(self) -> list[str]:
        return sorted(p.name for p in self.dir.glob("m_*") if re.fullmatch(r"m_\d{4}", p.name)) if self.dir.exists() else []

    def next_version(self) -> str:
        versions = self.versions()
        return f"m_{int(versions[-1][2:]) + 1 if versions else 1:04d}"

    def record(self, card: dict) -> None:
        write_json(self.dir / card["version"] / "card.json", {"updated": now(), **card})

    def card(self, version: str) -> dict:
        return json.loads((self.dir / version / "card.json").read_text(encoding="utf-8"))

    def _pointer(self) -> dict:
        path = self.dir / "champion.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": None, "history": []}

    def champion(self) -> dict | None:
        """The champion's card. It lists the points the champion serves."""
        version = self._pointer()["version"]
        return self.card(version) if version else None

    def promote(self, version: str, expected_parent: str | None) -> None:
        """Compare and swap: `version` becomes the champion only while the champion is still `expected_parent`, the
        version it was built on. Two candidates built on one parent cannot both be promoted. Only a version the gate
        record on its card accepted (gate.run), over the parent it was evaluated against, and never rolled back or
        revoked, can be promoted."""
        card = self.card(version)
        if card.get("parent") != expected_parent:       # a verdict promotes the step it judged, parent -> version
            raise PromotionConflict(f"{version} was built on {card.get('parent')}, not {expected_parent}")
        if not Accepted(self.root).promoted(card, standing=True):
            raise ValueError(f"{version} was not accepted by a gate record, or was rolled back: only gate.run promotes")
        if Revoked(self.root / "harness" / "revoked.json").hit(version, card.get("model_id")):
            raise ValueError(f"{version} is revoked")
        with locked(self.dir / "champion.json"):
            pointer = self._pointer()
            if pointer["version"] != expected_parent:
                raise PromotionConflict(f"{version} was built on {expected_parent}, but the champion is now "
                                        f"{pointer['version']}")
            pointer["history"].append({"version": version, "parent": expected_parent, "ts": now()})
            pointer["version"] = version
            write_json(self.dir / "champion.json", pointer)

    def rollback(self) -> str | None:
        """Return to the previous champion (None if there was none) and report it. The retired version goes on the
        revocation list: sessions still running it take their defaults from then on."""
        with locked(self.dir / "champion.json"):
            pointer = self._pointer()
            retired = pointer["version"]
            if retired:         # revoked before the pointer moves: no moment where it is neither champion nor revoked
                from .gate import revoke_record
                card = self.card(retired) if (self.dir / retired / "card.json").exists() else {}
                revoke(self.root, retired, card.get("model_id"), "rolled back")
                if card.get("round"):
                    revoke_record(self.root, card["round"], f"{retired} rolled back")
            if pointer["history"]:
                pointer["history"].pop()
            pointer["version"] = pointer["history"][-1]["version"] if pointer["history"] else None
            write_json(self.dir / "champion.json", pointer)
        return pointer["version"]

    def seen_sessions(self) -> set[str]:
        """Every session that has been through a gate, from the gate records (spec and prompt gates write no model
        card): the test sessions of every used record, and the sessions a prompt gate ran."""
        from .gate import seen_sessions
        return seen_sessions(self.root)
