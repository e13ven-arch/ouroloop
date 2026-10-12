"""The confirmation of the screen's flips (DESIGN §6.17, IMPROVER §9, v1.7): a flip counts only when it repeats under
the candidate and the working line's own readings of the task agree the other way. finance-v7 measured 10–17% of the
tasks flipping between two runs of one package; a single reading's net flips installed noise."""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "tests" / "improver"))
import rsi  # noqa: E402
from rounds import round_activity  # noqa: E402

from ouroloop.activity import phases  # noqa: E402
from ouroloop.improver.screen import ScreenResult  # noqa: E402

HELD = tuple(sorted(rsi.HELD))


def quiet(_line):
    pass


def screen_of(a, index=0) -> ScreenResult:
    state = json.loads((a.root / "rounds" / f"r{index}" / "round.json").read_text(encoding="utf-8"))
    (row,) = state["steps"]["screened"]["screens"]
    return ScreenResult.from_json(a.objects.get(row["screen"]))


def test_a_fix_that_repeats_under_the_candidate_and_w_fails_twice_is_confirmed_and_installed(tmp_path, fake_upstream):
    """The scripted actor forgets the notification without the tool and uses the tool whenever it is offered: both
    held-out tasks flip, repeat on the second reading, and W (read once more each) fails them again."""
    s = round_activity(tmp_path / "solid", fake_upstream, "tool")
    result = phases.round_(s.a, 0, out=quiet)
    res = screen_of(s.a)
    assert result.accepted and res.accepted
    assert res.flipped_fixed == HELD and res.fixed == HELD and res.noisy == () and res.unconfirmed == ()
    assert res.confirm_attempts == 4                       # the candidate once more and W once more, per task
    runs = s.a.results.runs()
    roles = {(runs[r.run].role, str(runs[r.run].candidate)) for r in s.a.results.all() if r.task_id in HELD}
    assert ("confirm", "r0:package") in roles and any(c.startswith("confirm:") and role == "confirm"
                                                      for role, c in roles)
    rows = [r for r in json.loads("[" + ",".join((s.a.root / "ledger" / "experience.jsonl").read_text().splitlines())
                                  + "]") if r.get("stage") == "screened"]
    assert rows[-1]["fixed"] == list(HELD) and rows[-1]["flipped_fixed"] == list(HELD) \
        and rows[-1]["confirm_attempts"] == 4 and rows[-1]["noisy"] == []


def test_a_fix_the_candidates_second_reading_does_not_repeat_is_unconfirmed_and_the_package_is_rejected(
        tmp_path, fake_upstream):
    """The actor uses the package's tool only on a task's even-numbered attempts: the screen's first reading passes
    (attempt 2), the confirmation fails (attempt 3). Nothing is installed; the ledger says why."""
    actor = rsi.BusinessWorker(forget_notify=True, flaky_tool=True)
    s = round_activity(tmp_path / "flaky", fake_upstream, "tool", actor=actor)
    result = phases.round_(s.a, 0, out=quiet)
    res = screen_of(s.a)
    assert not result.accepted and not res.accepted and res.outcome == "screened"
    assert res.flipped_fixed == HELD and res.fixed == () and res.unconfirmed == HELD and res.noisy == ()
    assert res.net_flips == 0 and res.reasons == ("net flips 0 below accept_min_net_flips 1",)
    assert s.a.active_release().ref() == s.a.seed_release().ref()


def test_a_task_the_working_line_itself_passes_and_fails_is_noisy_and_counts_neither_way(tmp_path, fake_upstream):
    """Without the tool the actor posts the notification on even-numbered attempts unprompted: W failed the task
    first (attempt 1) and passes it when read again (attempt 4) — the task is noise, not a fix."""
    actor = rsi.BusinessWorker(forget_notify=True, flaky_notify=True)
    s = round_activity(tmp_path / "noisy", fake_upstream, "tool", actor=actor)
    result = phases.round_(s.a, 0, out=quiet)
    res = screen_of(s.a)
    assert not result.accepted and not res.accepted
    assert res.flipped_fixed == HELD and res.fixed == () and res.noisy == HELD and res.unconfirmed == ()
    assert res.reasons == ("net flips 0 below accept_min_net_flips 1",)

