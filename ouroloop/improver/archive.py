"""The archive of every candidate a round verified (docs/design/IMPROVER.md §10; DESIGN §6.7): its files, declaration,
verdict, screen and receipt, under rounds/r<i>/candidates/<dir>/ and as objects. It is where credit and R's inputs
read what was tried; it is not a second line of versions (the release line is evaluate's).

`duplicate(files)` finds an earlier candidate with exactly these files: the round does not pay to screen it again."""
from __future__ import annotations

import json
from pathlib import Path

from ..contracts import append_jsonl, digest, read_jsonl, write_json


class Archive:
    def __init__(self, root, objects):
        self.root = Path(root) / "rounds"
        self.index = self.root / "archive.jsonl"
        self.objects = objects

    @staticmethod
    def files_digest(files: dict[str, str]) -> str:
        return digest({p: files[p] for p in sorted(files)})

    def put(self, round_index: int, dir_name: str, files: dict[str, str], decl, verdict, *, screen=None,
            receipt_ref: str | None = None, package_ref: str | None = None, line: str = "") -> dict:
        base = self.root / f"r{round_index}" / "candidates" / dir_name
        row = {"round": round_index, "dir": dir_name, "line": line, "files_digest": self.files_digest(files),
               "declaration": self.objects.put(decl), "verdict": self.objects.put(verdict) if verdict else None,
               "screen": self.objects.put(screen) if screen is not None else None, "receipt": receipt_ref,
               "package_ref": package_ref, "kind": decl.kind, "cause": decl.cause_id.rpartition(":")[2],
               "site": decl.site, "fixed": decl.fixed}
        write_json(base / "files.json", files)
        write_json(base / "record.json", row)
        append_jsonl(self.index, row)
        return row

    def rows(self, *, line: str | None = None) -> list[dict]:
        latest: dict = {}
        for r in read_jsonl(self.index):
            if line is None or r.get("line") in ("", line):
                latest[(r["round"], r["dir"])] = r
        return list(latest.values())

    def duplicate(self, files: dict[str, str], *, before_round: int) -> dict | None:
        d = self.files_digest(files)
        return next((r for r in self.rows() if r["files_digest"] == d and r["round"] < before_round), None)

    def for_prompt(self, line: str, cause: str, n: int = 10, hide=frozenset()) -> list[dict]:
        """What R reads of earlier candidates of a cause: kind, site, the rungs passed, the screen's outcome and
        counts (a task of `hide`, held out from R, is counted, never named)."""
        hide = set(hide)
        out = []
        for r in self.rows(line=line):
            if r.get("cause") != cause:
                continue
            verdict = self.objects.get(r["verdict"]) if r.get("verdict") else {}
            screen = self.objects.get(r["screen"]) if r.get("screen") else {}
            out.append({"round": r["round"], "dir": r["dir"], "kind": r["kind"], "site": r.get("site"),
                        "declaration": r["declaration"],
                        "rungs": {k: bool(v.get("passed")) for k, v in (verdict.get("steps") or {}).items()},
                        "screen": {"outcome": screen.get("outcome"), "net_flips": screen.get("net_flips"),
                                   **{k: [t for t in screen.get(k) or () if t not in hide]
                                      for k in ("fixed", "regressed")},
                                   **{f"{k}_held_out": sum(1 for t in screen.get(k) or () if t in hide)
                                      for k in ("fixed", "regressed")}}
                        if screen else None, "gated": bool(r.get("receipt"))})
        return out[-n:]


def load_files(root, round_index: int, dir_name: str) -> dict[str, str]:
    return json.loads((Path(root) / "rounds" / f"r{round_index}" / "candidates" / dir_name / "files.json")
                      .read_text(encoding="utf-8"))
