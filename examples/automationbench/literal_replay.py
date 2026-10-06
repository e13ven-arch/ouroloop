"""The 20-draft replay behind docs/design/V0_4_PLAN.md §1.5: every draft check the v0.3 five-domain activity recorded
(`workspace-claude-v3/rsi/events.jsonl`, event `draft_check`), its draft read back from the workspace's objects, and the
literal stage judged twice — under the v0.3 rule (every train literal, case-insensitive) and under the v0.4 rule (only a
value of one train task, case-sensitive, pure numbers and header names exempt) — with the same forbidden-literal
sources the round used: the train assertion params in the activity's grading store and the train tasks' initial
states from the upstream checkout, and the same tool vocabulary allowed.

Read-only: nothing in the workspace is written. Offline: no model is called. The v0.3 rule reproduces the recorded
refusals (14 of 20); the v0.4 rule's count is the number the plan predicts (0). A planted check then writes, into the
first refused draft, one value that occurs in exactly one train task in its own case, and the v0.4 rule must refuse it:
the precision gained is not a hole.

    python examples/automationbench/literal_replay.py [--workspace DIR] [--upstream DIR] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from ouroloop.adapters import automationbench as ab
from ouroloop.agent import harness_package as hp
from ouroloop.benchmark import Results, SplitManifest, task_domain
from ouroloop.evolve import contracts as ct
from ouroloop.research.diagnosis import train_literal_index

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE / "workspace-claude-v3"
UPSTREAM = Path("~/workspace/AutomationBench").expanduser()
POOLED = "marketing+sales+support+hr+finance"


def old_rule_hits(files: dict, literals: set, allowed: set) -> list[str]:
    """The v0.3 literal stage as it ran (harness_package.literal_hits before v0.4): every train literal, matched as a
    whole token, case-insensitively, over the paths, the bodies and the manifest's decoded strings."""
    parts = list(files) + [t for p, t in files.items() if p != "manifest.json"]
    if "manifest.json" in files:
        try:
            parts.append(json.dumps(json.loads(files["manifest.json"]), ensure_ascii=False))
        except (json.JSONDecodeError, TypeError):
            parts.append(files["manifest.json"])
    body = "\n".join(parts)
    lower = body.lower()
    return [lit for lit in sorted(set(literals) - set(allowed)) if lit.lower() in lower
            and re.search(r"(?<![\w@.-])" + re.escape(lit) + r"(?![\w@-])", body, re.IGNORECASE)]


def sources(workspace: Path, upstream: Path, domain: str = POOLED) -> tuple[dict, set, set, set]:
    """-> (literal -> train tasks, header names, tool vocabulary, the assertion params' literals): what
    bench.Improvement.round_inputs hands Drafts."""
    results = Results(workspace, domain)
    manifest = SplitManifest.load(results.manifest_path())
    index, headers = train_literal_index(results)
    answers = set(index)
    suites: dict = {}
    train = manifest.ids("train")
    for task in train:
        d = task_domain(task)
        if d not in suites:
            suites[d] = ab.load_suite(d, str(upstream))
        try:
            state = suites[d].grading(task).initial_state
        except Exception:  # noqa: BLE001 - as the round reads it: a task without a state adds nothing
            continue
        hp.add_literals(index, task, state)
        headers |= hp.header_names(state)
    episode = suites[task_domain(train[0])].episode(train[0])
    vocabulary: set = set()
    for spec in episode.specs():
        vocabulary.add(spec.name)
        vocabulary |= set((spec.schema or {}).get("properties", {}))
    episode.close()
    return index, headers, vocabulary, answers


def checks(workspace: Path) -> list[dict]:
    """Every recorded draft check, in log order, with its draft's files."""
    objects = ct.Objects(workspace)
    out = []
    for line in (workspace / "rsi" / "events.jsonl").read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e.get("event") != "draft_check":
            continue
        listing = objects.get(e["draft"])["files"]
        out.append({"candidate": str(e.get("candidate") or "").rpartition("/")[2], "revision": e.get("revision"),
                    "recorded_ok": bool(e.get("ok")), "recorded_errors": list(e.get("errors") or ()),
                    "files": {p: objects.get(ref)["text"] for p, ref in listing.items()}})
    return out


def planted_literal(forbidden: set, headers: set, vocabulary: set, kind: str = "record name") -> str:
    """One forbidden value of exactly one train task of the given kind, written as the task writes it (deterministic:
    the first such value in sorted order that is no header and no tool word); kind "number" is a numeric assertion
    value (review 2026-10-06: a finance amount)."""
    pool = sorted(forbidden - headers - vocabulary)
    if kind == "number":
        return next(lit for lit in pool if hp.NUMBER.fullmatch(lit))
    return next(lit for lit in pool if hp.literal_kind(lit) == kind)


def replay(workspace: Path = WORKSPACE, upstream: Path = UPSTREAM) -> dict:
    index, headers, vocabulary, answers = sources(workspace, upstream)
    everything = set(index)
    forbidden = hp.forbidden_literals(index, answers=answers)
    allowed = vocabulary | headers
    rows = []
    recorded = checks(workspace)
    for c in recorded:
        old = old_rule_hits(c["files"], everything, vocabulary)
        new = hp.literal_findings(c["files"], forbidden, allowed=allowed)
        rows.append({"candidate": c["candidate"], "revision": c["revision"], "recorded_ok": c["recorded_ok"],
                     "old_rule_hits": old, "new_rule_hits": sorted({f["literal"] for f in new}),
                     "shared_tasks": {h: len(index.get(h, ())) for h in old}})
    refused = [r for r in rows if not r["recorded_ok"]]
    first = next(c for c in recorded if not c["recorded_ok"])
    plant = planted_literal(forbidden, headers, vocabulary)
    planted_files = {**first["files"], "memory.md": first["files"].get("memory.md", "") + f"\nIf the status is {plant}, "
                                                                                          f"stop.\n"}
    planted = hp.literal_findings(planted_files, forbidden, allowed=allowed)
    # a numeric answer of one train task (an amount the grader checks), planted the same way: still refused
    amount = planted_literal(forbidden, headers, vocabulary, kind="number")
    amount_files = {**first["files"], "memory.md": first["files"].get("memory.md", "") + f"\nThe total is {amount}.\n"}
    amount_found = hp.literal_findings(amount_files, forbidden, allowed=allowed)
    return {"checks": len(rows), "recorded_refusals": len(refused),
            "old_rule_refusals": sum(1 for r in rows if r["old_rule_hits"]),
            "old_rule_reproduces_record": all(bool(r["old_rule_hits"]) == (not r["recorded_ok"]) for r in rows),
            "new_rule_refusals": sum(1 for r in rows if r["new_rule_hits"]),
            "planted": {"literal_kind": hp.literal_kind(plant), "tasks": len(index[plant]),
                        "refused": any(f["literal"] == plant for f in planted),
                        "output": hp.describe_hits(planted)[:300]},
            "planted_amount": {"tasks": len(index[amount]), "assertion_value": amount in answers,
                               "refused": any(f["literal"] == amount for f in amount_found)},
            "numbers": {"assertion_values": sum(1 for lit in answers if hp.NUMBER.fullmatch(lit)),
                        "of_one_task": sum(1 for lit in answers if hp.NUMBER.fullmatch(lit) and len(index[lit]) == 1),
                        "forbidden": sum(1 for lit in forbidden if hp.NUMBER.fullmatch(lit))},
            "rows": rows}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace", type=Path, default=WORKSPACE)
    parser.add_argument("--upstream", type=Path, default=UPSTREAM)
    parser.add_argument("--json", action="store_true", help="print the summary as JSON (the rows left out)")
    args = parser.parse_args(argv)
    out = replay(args.workspace, args.upstream)
    if args.json:
        print(json.dumps({k: v for k, v in out.items() if k != "rows"}))
        return 0
    for r in out["rows"]:
        print(f"{r['candidate']:>12} r{r['revision']}  recorded {'ok     ' if r['recorded_ok'] else 'refused'}  "
              f"v0.3 {r['old_rule_hits'] or '-'}  v0.4 {r['new_rule_hits'] or '-'}")
    print(f"{out['checks']} checks; recorded refusals {out['recorded_refusals']}; v0.3 rule {out['old_rule_refusals']} "
          f"(reproduces the record: {out['old_rule_reproduces_record']}); v0.4 rule {out['new_rule_refusals']}; "
          f"planted single-task {out['planted']['literal_kind']} refused: {out['planted']['refused']}; planted "
          f"single-task amount refused: {out['planted_amount']['refused']}; numbers {out['numbers']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
