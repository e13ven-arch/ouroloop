"""A cross-domain activity (DESIGN §6.14, stage 4e) on the fake upstream: two of its mini domains as one suite, the
stratified split per domain, the final and the release confined to one domain, the smoke's checks, and the config's
two new keys."""
from __future__ import annotations

import pytest
import rsi

from ouroloop.activity import phases
from ouroloop.activity.checks import startup_checks
from ouroloop.activity.config import ConfigError, resolve
from ouroloop.activity.workspace import open_activity
from ouroloop.adapters import automationbench as ab
from ouroloop.runtime.worker import ProcessWorker


def quiet(_line):
    pass


def cross(root, upstream, provider=None, **sections):
    """An activity over the rsi and marketing domains, split by the stratified rule (no declared split: the merged
    suite is not what rsi.write_split freezes), the final on rsi's test half."""
    cfg = rsi.config(root, upstream, bench={"domain": None, "domains": ["rsi", "marketing"], "split_rule": "stratified",
                                            "ratios": {"train": 0.5, "dev": 0.0, "test": 0.5}, "families": {}},
                     final={"domains": ["rsi"]}, **sections)
    provider = provider or rsi.BusinessWorker()
    return open_activity(cfg, out=quiet, worker_factory=lambda: ProcessWorker(for_tests=True),
                         provider_factory=lambda table: provider)


def test_several_domains_load_as_one_suite_strat_by_domain_and_the_final_keeps_to_its_domains(tmp_path, fake_upstream):
    a = cross(tmp_path, fake_upstream)
    suite = a.suite
    assert a.domain == "rsi+marketing" and suite.members == ["rsi", "marketing"]
    assert set(suite.ids()) == set(rsi.suite(fake_upstream).ids()) | set(ab.load_suite("marketing", fake_upstream).ids())
    assert suite.domain_of("marketing.lead_followup") == "marketing" and suite.domain_of(rsi.TRAIN[0]) == "rsi"
    assert ab.stratum(suite, "marketing.lead_followup").startswith("marketing|")
    assert ab.stratum(rsi.suite(fake_upstream), rsi.TRAIN[0]) == ab.stratum(suite, rsi.TRAIN[0]).split("|", 1)[1]
    counts = a.splits.counts()
    assert counts["dev"]["tasks"] == 0 and counts["train"]["tasks"] + counts["test"]["tasks"] == len(suite)
    by_domain = {d: [t for t in a.splits.ids("train") if suite.domain_of(t) == d] for d in suite.members}
    assert by_domain["marketing"] and by_domain["rsi"]                      # each domain has a train half
    # the final's domains: rsi only, in the plan and in what the release would read
    assert a.final_domains == ["rsi"]
    assert a.final_tasks("test") == [t for t in a.splits.ids("test") if t.startswith("rsi.")]
    assert all(t.startswith("marketing.") or t in a.final_tasks("train") for t in a.splits.ids("train"))
    phases.freeze(a, out=quiet)
    plan = phases.final_plan(a)
    assert set(plan.tasks) == set(a.final_tasks("test")) and plan.arms == {"W0": a.seed_release().ref()}
    # the smoke's checks run over the merged suite; the stream and the rounds may start
    report = phases.smoke(a, checks_only=True, out=quiet)
    assert report["ok"] and report["splits"]["counts"] == counts
    assert startup_checks(a, phase="stream", needs_model=False) == []
    assert a.results.manifest_path().parent.name == "rsi+marketing"


def test_the_configs_domains_are_checked(tmp_path, fake_upstream):
    base = rsi.config(tmp_path, fake_upstream)
    k = resolve({**base, "bench": {**base["bench"], "domains": ["rsi", "marketing"], "domain": None}})
    assert k["bench"]["domain"] == "rsi+marketing" and k["bench"]["domains"] == ["rsi", "marketing"]
    assert k["final"]["domains"] is None
    with pytest.raises(ConfigError, match="name the domains once"):
        resolve({**base, "bench": {**base["bench"], "domains": ["rsi", "marketing"], "domain": "rsi"}})
    for bad in ([], ["rsi", "rsi"], ["a+b"], "rsi"):
        with pytest.raises(ConfigError, match="distinct domain names"):
            resolve({**base, "bench": {**base["bench"], "domains": bad, "domain": None}})
    with pytest.raises(ConfigError, match=r"\[final\] domains"):
        resolve({**base, "final": {"domains": ["sales"]}})
    assert resolve({**base, "final": {"domains": ["rsi"]}})["final"]["domains"] == ["rsi"]
    # a single-domain config is what it was: no domains key set, the same K sections
    assert resolve(base)["bench"]["domains"] is None and resolve(base)["bench"]["domain"] == "rsi"
