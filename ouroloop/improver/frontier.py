"""The frontier of partial packages (docs/design/IMPROVER.md §9.5; DESIGN §6.18, v1.8): <workspace>/rounds/frontier.json.

A partial package is one the held-out screen rejected only for its confirmed regressions while it confirmed at least
`frontier_min_fixes` fixes (screen.partial_of). finance-v7 made them every round — a global heuristic right for the
studied cases and wrong for a held-out task with the opposite requirement — and threw them away, so the next round
tried another global heuristic. The frontier keeps them: their files, declaration, fixed and regressed held-out tasks
(the ids stay on the host), their regression evidence and how often a merge was tried. Each round the focus list asks
R to merge the best entry into a conditioned version — the same mechanism, its trigger narrowed so it no longer
acts on the regression shapes, the conditions taken from the task's own documents — in candidates/merge/, declared
`merge_of: <entry id>`. The merge is verified and screened like any package: accepted, it is installed and the entry
is `merged`; rejected, the entry's `attempts` grows and at `frontier_max_attempts` it is `abandoned` (a rejected merge
that is itself partial takes the entry's place, the attempts inherited). An entry whose fixes the working line all
passes by then is `subsumed`; the frontier keeps the `frontier_max` strongest entries (the most confirmed fixes, then
the fewest attempts), the rest `dropped`.

Not done here: a merge of two accepted packages (stacking covers it), an entry installed as it is (it has a confirmed
regression)."""
from __future__ import annotations

import difflib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..contracts import locked, write_json

STATUSES = ("active", "merged", "subsumed", "abandoned", "dropped")
DIFF_CHARS = 12000          # of one entry's diff in R's inputs


@dataclass
class FrontierEntry:
    id: str
    round_index: int
    tag: str                            # the round's directory (r<i> | s<i>)
    dir: str                            # the candidate directory
    line: str
    files_ref: str                      # the package's files (a candidate_files object)
    declaration_ref: str
    base_package_ref: str               # the working line's package the candidate was built on
    fixed: tuple = ()                   # confirmed fixes (held-out task ids: host only)
    regressed: tuple = ()               # confirmed regressions (held-out task ids: host only)
    regression_ref: str | None = None   # the regression evidence object of its screen
    screen_ref: str | None = None
    mechanism: str = ""
    kind: str = ""
    heuristic_terms: dict = field(default_factory=dict)      # path -> terms V0 noted
    attempts: int = 0                   # merges tried
    status: str = "active"
    history: tuple = ()                 # ({round_id, dir, outcome}) of the merges tried

    def __post_init__(self):
        self.fixed, self.regressed = tuple(self.fixed or ()), tuple(self.regressed or ())
        self.history = tuple(dict(h) for h in self.history or ())
        if self.status not in STATUSES:
            raise ValueError(f"frontier entry status {self.status!r}: one of {list(STATUSES)}")

    def to_json(self) -> dict:
        d = asdict(self)
        d["fixed"], d["regressed"], d["history"] = list(self.fixed), list(self.regressed), list(self.history)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "FrontierEntry":
        return cls(**d)


class Frontier:
    def __init__(self, root, settings):
        self.path = Path(root) / "rounds" / "frontier.json"
        self.s = settings
        self.entries: list[FrontierEntry] = []
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = [FrontierEntry.from_json(e) for e in data.get("entries") or ()]

    # ------------------------------------------------------------------ the file
    def _save(self) -> None:
        write_json(self.path, {"entries": [e.to_json() for e in self.entries]})

    def active(self, line: str | None = None) -> list[FrontierEntry]:
        out = [e for e in self.entries if e.status == "active" and (line is None or e.line == line)]
        return sorted(out, key=lambda e: (-len(e.fixed), e.attempts, e.id))

    def get(self, entry_id: str) -> FrontierEntry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    def of_candidate(self, tag: str, dir_name: str) -> FrontierEntry | None:
        return next((e for e in self.entries if e.tag == tag and e.dir == dir_name), None)

    # ------------------------------------------------------------------ in and out
    def add(self, entry: FrontierEntry) -> tuple[FrontierEntry, list[str]]:
        """Put a partial package on the frontier (the same candidate again — a resumed step — is the entry on
        record); keep the frontier_max strongest of the line's active entries, the rest dropped. -> (the entry, the
        ids dropped)."""
        with locked(self.path):
            same = self.of_candidate(entry.tag, entry.dir)
            if same is not None:
                return same, []
            entry.id = entry.id or f"f{len(self.entries) + 1}"
            self.entries.append(entry)
            dropped = []
            keep = int(self.s.frontier_max)
            for e in self.active(entry.line)[keep:]:
                e.status = "dropped"
                dropped.append(e.id)
            self._save()
        return entry, dropped

    def prune(self, line: str, line_passes) -> list[str]:
        """The line's active entries whose confirmed fixes the working line now passes every one of: subsumed."""
        passes = set(line_passes)
        out = []
        with locked(self.path):
            for e in self.active(line):
                if e.fixed and set(e.fixed) <= passes:
                    e.status = "subsumed"
                    out.append(e.id)
            if out:
                self._save()
        return out

    def picks(self, line: str, n: int) -> list[FrontierEntry]:
        """The entries a round asks R to merge: the fewest attempts first, then the most fixes, `n` at most."""
        ok = [e for e in self.active(line) if e.attempts < int(self.s.frontier_max_attempts)]
        return sorted(ok, key=lambda e: (e.attempts, -len(e.fixed), e.id))[:max(int(n), 0)]

    def merged(self, entry_id: str, *, round_id: str, dir_name: str) -> FrontierEntry | None:
        with locked(self.path):
            e = self.get(entry_id)
            if e is None or e.status != "active":
                return e
            e.status = "merged"
            e.history = (*e.history, {"round_id": round_id, "dir": dir_name, "outcome": "merged"})
            self._save()
        return e

    def failed(self, entry_id: str, *, round_id: str, dir_name: str, replacement: FrontierEntry | None = None) \
            -> tuple[FrontierEntry | None, str]:
        """A merge of the entry was rejected: one more attempt; at frontier_max_attempts the entry is abandoned. A
        rejected merge that is itself partial (`replacement`) takes the entry's place — its files, fixes,
        regressions and evidence — with the attempts inherited. -> (the entry, "abandoned" | "retried" |
        "replaced")."""
        with locked(self.path):
            e = self.get(entry_id)
            if e is None or e.status != "active":
                return e, "not_active"
            if any(h.get("round_id") == round_id and h.get("dir") == dir_name for h in e.history):
                return e, "on_record"                  # a resumed step
            e.attempts += 1
            outcome = "retried"
            if replacement is not None:
                for name in ("round_index", "tag", "dir", "files_ref", "declaration_ref", "base_package_ref", "fixed",
                             "regressed", "regression_ref", "screen_ref", "mechanism", "kind", "heuristic_terms"):
                    setattr(e, name, getattr(replacement, name))
                outcome = "replaced"
            if e.attempts >= int(self.s.frontier_max_attempts):
                e.status = "abandoned"
                outcome = "abandoned"
            e.history = (*e.history, {"round_id": round_id, "dir": dir_name, "outcome": outcome})
            self._save()
        return e, outcome

    # ------------------------------------------------------------------ what R reads
    def for_prompt(self, entry: FrontierEntry, *, base_files: dict, files: dict, declaration: dict,
                   regressions: dict | None, hide=frozenset(), scrub=None) -> dict:
        """One entry as inputs/frontier.json shows it: the package's diff against the working line it was built on
        (R's own code), its mechanism and components, the counts of confirmed fixes and regressions (held-out ids are
        never named), its regression evidence, how often it was merged. `scrub(value)` replaces a held-out id that
        slipped into a text."""
        diff: dict[str, str] = {}
        for path in sorted(set(base_files) | set(files)):
            before, after = base_files.get(path), files.get(path)
            if before == after:
                continue
            lines = difflib.unified_diff((before or "").splitlines(), (after or "").splitlines(),
                                         fromfile=f"working-line/{path}", tofile=f"{entry.dir}/{path}", lineterm="",
                                         n=2)
            diff[path] = "\n".join(lines)[:DIFF_CHARS]
        out = {"entry": entry.id, "round": entry.tag, "dir": entry.dir, "status": entry.status,
               "mechanism": entry.mechanism, "kind": entry.kind,
               "components": [{k: c.get(k) for k in ("kind", "site", "trigger", "cause_ids", "uses_model")}
                              for c in declaration.get("components") or ()],
               "fixes": len(entry.fixed), "regressions": len(entry.regressed), "attempts": entry.attempts,
               "merges": [dict(h) for h in entry.history], "heuristic_terms": dict(entry.heuristic_terms),
               "diff": diff, "regressions_evidence": regressions}
        return scrub(out) if scrub is not None and hide else out
