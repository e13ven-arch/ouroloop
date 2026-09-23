"""The model registry (PLAN §4.6): one card per trained candidate, a champion pointer, and one-step rollback."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..ledger import now


class Registry:
    def __init__(self, root: str | Path):
        self.dir = Path(root) / "models"

    def versions(self) -> list[str]:
        return sorted(p.name for p in self.dir.glob("m_*") if re.fullmatch(r"m_\d{4}", p.name)) if self.dir.exists() else []

    def next_version(self) -> str:
        versions = self.versions()
        return f"m_{int(versions[-1][2:]) + 1 if versions else 1:04d}"

    def record(self, card: dict) -> None:
        path = self.dir / card["version"] / "card.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"updated": now(), **card}, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")

    def card(self, version: str) -> dict:
        return json.loads((self.dir / version / "card.json").read_text(encoding="utf-8"))

    def _pointer(self) -> dict:
        path = self.dir / "champion.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": None, "history": []}

    def _write_pointer(self, pointer: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "champion.json").write_text(json.dumps(pointer, indent=2), encoding="utf-8")

    def champion(self) -> dict | None:
        version = self._pointer()["version"]
        return self.card(version) if version else None

    def promote(self, version: str) -> None:
        pointer = self._pointer()
        pointer["history"].append({"version": version, "ts": now()})
        pointer["version"] = version
        self._write_pointer(pointer)

    def rollback(self) -> str | None:
        """Return to the previous champion (None if there was none) and report it."""
        pointer = self._pointer()
        if pointer["history"]:
            pointer["history"].pop()
        pointer["version"] = pointer["history"][-1]["version"] if pointer["history"] else None
        self._write_pointer(pointer)
        return pointer["version"]
