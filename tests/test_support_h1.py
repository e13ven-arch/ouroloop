"""H1 of support's two-arm test (examples/automationbench/support_h1.py; docs/design/V0_5_PLAN.md §3): the two arms'
completed finals paired by task, read off fixture records — the finalization state and the results store — with the
v0.4 arm's W* as the control, or the v0.5 final's own W0 when the v0.4 arm has no final."""
import importlib.util
import json
from pathlib import Path

import pytest

from ouroloop.benchmark import Results, TaskResult
from ouroloop.ledger import write_json

SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "automationbench" / "support_h1.py"
TASKS = [f"support.t{i:02d}" for i in range(25)]


def load():
    spec = importlib.util.spec_from_file_location("support_h1", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_final(root, experiment_id, outcomes, *, status="completed", extra=()):
    """A sealed and run final as bench.evaluate leaves it: each attempt in the results store and in the finalization
    state with its arm."""
    results = Results(root, "support")
    attempts = []
    for arm, tasks in outcomes.items():
        for task, reps in tasks.items():
            for i, ok in enumerate(reps):
                aid = f"run{arm}/{task}/r{i}/{arm}#0"
                results.append(TaskResult(task_id=task, family_id=task, split="test", attempt=i, candidate=arm,
                                          run=f"run{arm}", manifest="m", success=bool(ok),
                                          partial_credit=1.0 if ok else 0.0, attempt_id=aid))
                attempts.append({"attempt_id": aid, "arm": arm})
    for row in extra:
        results.append(row)
        attempts.append({"attempt_id": row.attempt_id, "arm": "evolved"})
    write_json(Path(root) / "rsi" / "finalization" / f"{experiment_id}.json",
               {"plan": f"finalization:{experiment_id}", "status": status, "attempts": attempts})


def arm(rate_of):
    """{task: [three 0/1 readings]} with task i passing in the readings rate_of(i) says."""
    return {t: [int(r) for r in rate_of(i)] for i, t in enumerate(TASKS)}


def test_h1_pairs_the_two_finals_by_task_and_reads_the_preregistered_verdict(tmp_path):
    h1 = load()
    v5, v4 = tmp_path / "v5", tmp_path / "v4"
    write_final(v5, "v5-final", {"evolved": arm(lambda i: [i < 18] * 3), "base": arm(lambda i: [i < 10] * 3)})
    write_final(v4, "v4-final", {"evolved": arm(lambda i: [i < 11] * 3), "base": arm(lambda i: [i < 10] * 3)})
    out = h1.support_h1(v5, v4)
    assert out["tasks"] == 25 and out["point"] == round(7 / 25, 6)
    assert out["control"].startswith("the v0.4 arm's final W*") and out["verdict"] == "v0.5 better"
    assert out["interval95"][0] > 0 and out["replicates"] == 2000 and out["seed"] == 1
    assert h1.support_h1(v5, v4) == out                                        # seeded: the same reading twice
    # a small difference is uncertain; a worse W* is "not better"
    write_final(tmp_path / "v4b", "v4-final", {"evolved": arm(lambda i: [i < 17] * 3)})
    assert h1.support_h1(v5, tmp_path / "v4b")["verdict"] == "uncertain"
    write_final(tmp_path / "v4c", "v4-final", {"evolved": arm(lambda i: [i < 20] * 3)})
    assert h1.support_h1(v5, tmp_path / "v4c")["verdict"] == "not better"


def test_without_a_v04_final_h1_pairs_the_v05_w_star_with_its_own_finals_w0(tmp_path):
    h1 = load()
    v5, v4 = tmp_path / "v5", tmp_path / "v4"
    write_final(v5, "v5-final", {"evolved": arm(lambda i: [i < 15] * 3), "base": arm(lambda i: [i < 10] * 3)})
    (v4 / "rsi").mkdir(parents=True)                                          # the v0.4 arm installed nothing
    out = h1.support_h1(v5, v4)
    assert "own W0 arm" in out["control"] and out["point"] == round(5 / 25, 6)
    # a final that was sealed but never completed is not a final to pair with
    write_final(tmp_path / "v4r", "v4-final", {"evolved": arm(lambda i: [True] * 3)}, status="running")
    assert "own W0 arm" in h1.support_h1(v5, tmp_path / "v4r")["control"]
    with pytest.raises(ValueError, match="always runs its final"):
        h1.support_h1(v4, v5)


def test_only_valid_test_attempts_of_the_final_count(tmp_path):
    h1 = load()
    v5, v4 = tmp_path / "v5", tmp_path / "v4"
    stray = [TaskResult(task_id=TASKS[0], family_id=TASKS[0], split="test", attempt=9, candidate="evolved", run="x",
                        manifest="m", success=True, partial_credit=1.0, validity="invalid",
                        invalid_reason="the worker died", attempt_id="x/invalid#0"),
             TaskResult(task_id=TASKS[1], family_id=TASKS[1], split="dev", attempt=9, candidate="evolved", run="x",
                        manifest="m", success=True, partial_credit=1.0, attempt_id="x/dev#0")]
    write_final(v5, "v5-final", {"evolved": arm(lambda i: [False] * 3), "base": arm(lambda i: [False] * 3)},
                extra=stray)
    finals = h1.final_outcomes(v5)
    assert all(len(v) == 3 and sum(v) == 0 for v in finals["arms"]["evolved"].values())
    (v4 / "rsi").mkdir(parents=True)
    assert h1.support_h1(v5, v4)["verdict"] == "not better"


def test_the_command_prints_h1_and_each_arms_actual_spend(tmp_path, capsys):
    from ouroloop.evolve.execution import Budgets
    h1 = load()
    v5, v4 = tmp_path / "v5", tmp_path / "v4"
    write_final(v5, "v5-final", {"evolved": arm(lambda i: [i < 12] * 3), "base": arm(lambda i: [i < 10] * 3)})
    write_final(v4, "v4-final", {"evolved": arm(lambda i: [i < 10] * 3)})
    account = Budgets(v5).account("support-claude-v5", {"usd": 600.0})
    reservation = account.reserve("op1", {"usd": 2.0})
    account.settle(reservation.id, {"usd": 1.25}, "settled")
    assert h1.main(["--v5", str(v5), "--v4", str(v4)]) == 0
    printed = capsys.readouterr().out
    assert "H1" in printed and "uncertain" in printed and "v0.5 arm $1.25" in printed and "v0.4 arm $None" in printed
    assert h1.main(["--v5", str(v5), "--v4", str(v4), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["spend_usd"] == {"v5": 1.25, "v4": None}
