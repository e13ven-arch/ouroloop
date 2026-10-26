"""Opening an activity (docs/design/ACTIVITY.md §3, §7; CONTRACTS §3, §4): the first open freezes K and seeds the
release, a later open is the same activity, and a config, code or split that gives another K is refused."""
from __future__ import annotations

import json

import pytest
import rsi

from reharness.activity import workspace as wsp
from reharness.activity.config import ConfigError
from reharness.activity.workspace import open_activity
from reharness.contracts import short
from reharness.evaluate.release import attempt_version, improver_version
from reharness.evaluate.splits import SplitManifest
from reharness.harness.seed import build_seed


def opened(tmp_path, fake_upstream, *, lines=None, **kw):
    return rsi.activity(tmp_path, fake_upstream, out=(lines.append if lines is not None else None), **kw)


def core_frozen(activity) -> list:
    return list(activity.events.read("core_frozen"))


def test_the_first_open_writes_k_and_the_seed_release_and_a_second_open_is_the_same(tmp_path, fake_upstream):
    lines = []
    a = opened(tmp_path, fake_upstream, lines=lines)
    core = json.loads((a.root / "core.json").read_text(encoding="utf-8"))
    assert core == {**a.core.json(), "ref": a.core_ref}
    assert core["attempt_version"] == attempt_version() and core["improver_version"] == improver_version()
    assert core["split_hash"] == a.splits.hash()
    assert a.objects.has(a.core_ref)
    seed = a.active_release()
    assert seed.reason == "seed" and seed.parent_ref is None and seed.receipt_ref is None
    assert seed.core_ref == a.core_ref
    assert seed.package_ref == build_seed("stirrup", core=a.k, objects=a.objects).ref()      # W0 is K's seed
    assert a.objects.get(seed.improver_ref) == wsp.R0                                          # R0
    assert [line.split()[0] for line in lines] == ["release"] and short(seed.ref()) in lines[0]
    assert len(core_frozen(a)) == 1 and a.releases.pointer()["revision"] == 1
    again_lines = []
    b = opened(tmp_path, fake_upstream, lines=again_lines)
    assert (b.core_ref, b.releases.active_ref()) == (a.core_ref, seed.ref())
    assert again_lines == [] and len(core_frozen(b)) == 1 and b.releases.pointer()["revision"] == 1


def test_rsi_core_ref_pins_k(tmp_path, fake_upstream):
    a = opened(tmp_path, fake_upstream)
    pinned = opened(tmp_path, fake_upstream, rsi={"scope_id": "rsi-test", "core_ref": a.core_ref,
                                                  "split_ref": a.splits.hash()})
    assert pinned.core_ref == a.core_ref
    other = "core:sha256:" + "0" * 64
    with pytest.raises(ConfigError, match=r"\[rsi\] core_ref pins K core:000000000000, and this config, code and "
                                          r"split give"):
        opened(tmp_path / "fresh", fake_upstream, rsi={"scope_id": "rsi-test", "core_ref": other})
    assert not (tmp_path / "fresh" / "ws" / "core.json").exists()


def test_a_core_json_of_another_k_is_refused_with_or_without_freezing(tmp_path, fake_upstream):
    a = opened(tmp_path, fake_upstream)
    for freeze_k in (True, False):
        with pytest.raises(ConfigError, match=rf"froze K {short(a.core_ref)}, and this config and split give"):
            opened(tmp_path, fake_upstream, freeze_k=freeze_k, limits={"max_steps": 10})
    assert json.loads((a.root / "core.json").read_text(encoding="utf-8"))["ref"] == a.core_ref


def test_code_that_moved_under_the_activity_opens_the_frozen_k_and_the_phase_checks_decide(tmp_path, fake_upstream,
                                                                                            monkeypatch):
    """PLAN rule 13 (2026-10-12): the frozen K stands when only the code's versions differ; the rounds then refuse
    at their startup check, the final family (which runs the attempt code only) goes on with the improver's version
    on record."""
    from reharness.activity import checks
    from reharness.activity.checks import startup_checks
    a = opened(tmp_path, fake_upstream)
    monkeypatch.setattr(wsp, "improver_version", lambda: "0000000000000000")
    monkeypatch.setattr(checks, "improver_version", lambda: "0000000000000000")
    b = opened(tmp_path, fake_upstream, rsi={"scope_id": "rsi-test", "core_ref": a.core_ref})
    assert b.core_ref == a.core_ref and b.core.improver_version == a.core.improver_version != "0000000000000000"
    assert json.loads((b.root / "core.json").read_text(encoding="utf-8"))["ref"] == a.core_ref
    assert any("K froze the improver code" in p for p in startup_checks(b, phase="round", needs_model=False))
    assert not [p for p in startup_checks(b, phase="release", needs_model=False) if "K froze" in p]
    # the attempt code moving is refused everywhere; a changed config still is at the open
    monkeypatch.setattr(wsp, "attempt_version", lambda: "1111111111111111")
    monkeypatch.setattr(checks, "attempt_version", lambda: "1111111111111111")
    c = opened(tmp_path, fake_upstream)
    assert c.core_ref == a.core_ref
    assert any("K froze the attempt code" in p for p in startup_checks(c, phase="evaluate", needs_model=False))
    with pytest.raises(ConfigError, match="this config and split give"):
        opened(tmp_path, fake_upstream, limits={"max_steps": 10})


def test_without_freezing_k_nothing_is_written(tmp_path, fake_upstream):
    a = opened(tmp_path, fake_upstream, freeze_k=False)
    assert not (a.root / "core.json").exists()
    assert a.releases.active_ref() is None and not a.objects.has(a.core_ref)
    assert core_frozen(a) == []
    # the K it read is the K a frozen open writes
    assert opened(tmp_path, fake_upstream).core_ref == a.core_ref


def test_the_split_pins(tmp_path, fake_upstream):
    a = opened(tmp_path, fake_upstream)
    declared = rsi.splits(rsi.suite(fake_upstream)).hash()
    assert a.splits.hash() == declared and a.core.split_hash == declared and a.drift == []
    with pytest.raises(ConfigError, match=rf"\[rsi\] split_ref pins 0123456789abcdef, and the frozen split is "
                                          rf"{declared}"):
        opened(tmp_path, fake_upstream, rsi={"scope_id": "rsi-test", "split_ref": "0123456789abcdef"})
    # a manifest on disk is kept whatever the config asks now, and a [bench] pin it does not match is refused
    with pytest.raises(ConfigError, match=rf"freezes split {declared}, and \[bench\] frozen_splits pins rsi to "
                                          r"0123456789abcdef"):
        opened(tmp_path, fake_upstream, bench={"frozen_splits": {"rsi": "0123456789abcdef"}})
    assert SplitManifest.load(a.results.manifest_path()).hash() == declared


def test_a_pinned_split_is_reproduced_under_either_rule_and_a_wrong_pin_is_refused(tmp_path, fake_upstream):
    """Without a manifest on disk the split is drawn from the config: a pin made under the other rule is reproduced
    by that rule, and a pin neither rule reproduces is refused before anything is frozen."""
    def open_fresh(name, **bench):
        cfg = rsi.config(tmp_path / name, fake_upstream, bench=bench)
        return open_activity(cfg, out=lambda _line: None, freeze_k=False)
    by_hash = open_fresh("hash", split_rule="hash").splits
    by_strata = open_fresh("strata", split_rule="stratified").splits
    assert by_hash.hash() != by_strata.hash()
    again = open_fresh("pinned", split_rule="stratified", frozen_splits={"rsi": by_hash.hash()}).splits
    assert again.hash() == by_hash.hash()
    with pytest.raises(ConfigError, match="neither split rule reproduces rsi's pin 0123456789abcdef"):
        open_fresh("wrong", frozen_splits={"rsi": "0123456789abcdef"})
    assert not (tmp_path / "wrong" / "ws" / "bench" / "rsi" / "splits.json").exists()


def test_the_root_accounts_allocations_are_set_once(tmp_path, fake_upstream):
    a = opened(tmp_path / "a", fake_upstream, freeze_k=False)
    root = a.root_account()
    assert root.remaining()["limits"] == {"usd": 50.0}
    assert root.remaining()["allocations"] == {"final": {"usd": 10.0}}
    assert a.root_account().remaining()["allocations"] == {"final": {"usd": 10.0}}            # reopening is the same
    other_share = opened(tmp_path / "a", fake_upstream, freeze_k=False, budget={"final_usd": 5.0})
    with pytest.raises(ConfigError, match=r"rsi-test holds allocations \{'final': \{'usd': 10.0\}\}"):
        other_share.root_account()
    # a root that has spent before its final share was set does not get one afterwards
    b = opened(tmp_path / "b", fake_upstream, freeze_k=False, budget={"final_usd": None})
    assert b.root_account().remaining()["allocations"] == {}
    account = b.account("smoke")
    account.settle(account.reserve("a call", {"usd": 0.01}).id, {"usd": 0.01})
    late = opened(tmp_path / "b", fake_upstream, freeze_k=False)
    with pytest.raises(ConfigError, match=r"has spent \{'usd': 0.01\}: the final's share is set before anything"):
        late.root_account()
    assert b.root_account().remaining()["allocations"] == {}
