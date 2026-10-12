"""Releases {W, R, K} and the active pointer (docs/design/EVALUATE.md §7; CONTRACTS §5): the seed, compare-and-swap
installs authorised by a registered gate receipt, idempotent replays, revocation and rollback to the last good release;
the model component's binding point (§8); and the code identity every K carries."""
import json

import pytest

from ouroloop.contracts import ContractError, Events, Objects
from ouroloop.evaluate import release as rel
from ouroloop.evaluate.gate import GatePlan, TaskEvalPlan
from ouroloop.evaluate.model import ModelIdentity, check_binding, identity_of
from ouroloop.evaluate.release import GateReceipt, InstallReceipt, NotAuthorized, Release, Releases, Stale


def ref(objects, kind, **payload):
    return objects.put_json(kind, payload or {"name": kind})


@pytest.fixture
def ws(tmp_path):
    """A workspace with its seed release active: W0, R0, K."""
    objects = Objects(tmp_path)
    releases = Releases(tmp_path, objects)
    seed = releases.seed(ref(objects, "package", w="W0"), ref(objects, "improver", r="R0"), ref(objects, "core", k="K"))
    return objects, releases, seed


def accepted(objects, releases, text, *, parent=None, verdict="accepted", kind="full", plan=None, register=True):
    """A candidate W, a gate receipt for it, and the release that receipt would install on `parent`."""
    package = ref(objects, "package", w=text)
    parent = parent or releases.active()
    plan_ref = objects.put(plan) if plan is not None else ref(objects, "plan", plan=text)
    receipt = GateReceipt(plan_ref=plan_ref, subject_ref=package, control_ref=parent.package_ref,
                          parent_release=parent.ref(), results_ref=ref(objects, "gate_results", r=text),
                          verdict=verdict, kind=kind)
    receipt_ref = objects.put(receipt)
    if register:
        releases.register_receipt(receipt_ref, plan=plan_ref)
    return releases.candidate(package, receipt_ref, parent=parent), receipt_ref


def install(releases, candidate, receipt_ref, op, expected=None):
    return releases.install(candidate, receipt_ref, expected_parent=expected or candidate.parent_ref, operation_id=op)


# ---------------------------------------------------------------------------- the record
def test_a_release_names_w_r_and_k_by_kind_and_only_a_gate_release_has_a_receipt(ws):
    objects, _, seed = ws
    triple = {"package_ref": seed.package_ref, "improver_ref": seed.improver_ref, "core_ref": seed.core_ref}
    with pytest.raises(ContractError, match="a package"):
        Release(**{**triple, "package_ref": seed.core_ref})
    with pytest.raises(ContractError, match="improver"):
        Release(**{**triple, "improver_ref": seed.package_ref})
    with pytest.raises(ContractError, match="release reason"):
        Release(**triple, reason="manual")
    with pytest.raises(ContractError, match="names its receipt"):
        Release(**triple, parent_ref=seed.ref(), reason="gate")
    with pytest.raises(ContractError, match="names its receipt"):
        Release(**triple, receipt_ref=ref(objects, "receipt"), reason="rollback")
    with pytest.raises(ContractError, match="a receipt"):
        Release(**triple, parent_ref=seed.ref(), receipt_ref=ref(objects, "plan"), reason="gate")
    with pytest.raises(ContractError, match="names the screen"):
        Release(**triple, parent_ref=seed.ref(), reason="screen")
    with pytest.raises(ContractError, match="names the screen"):
        Release(**triple, parent_ref=seed.ref(), screen_ref=ref(objects, "screen"), reason="import")
    with pytest.raises(ContractError, match="screen result"):
        Release(**triple, parent_ref=seed.ref(), screen_ref=ref(objects, "receipt"), reason="screen")
    assert Release(**triple, parent_ref=seed.ref(), screen_ref=ref(objects, "screen"), reason="screen").reason \
        == "screen"
    assert objects.load(seed.ref()) == seed
    with pytest.raises(ContractError, match="receipt kind"):
        GateReceipt(kind="provisional")
    with pytest.raises(ContractError, match="verdict"):
        GateReceipt(verdict="maybe")
    with pytest.raises(ContractError, match="install status"):
        InstallReceipt(status="done")


def test_the_seed_is_installed_by_configuration_and_seeding_again_changes_nothing(tmp_path):
    objects = Objects(tmp_path)
    releases = Releases(tmp_path, objects)
    with pytest.raises(NotAuthorized, match="no release is active"):
        releases.active()
    w, r, k = ref(objects, "package"), ref(objects, "improver"), ref(objects, "core")
    seed = releases.seed(w, r, k)
    assert (seed.reason, seed.parent_ref, seed.receipt_ref) == ("seed", None, None)
    assert releases.active() == seed and releases.pointer()["revision"] == 1
    assert releases.seed(w, r, k) == seed
    releases.seed(ref(objects, "package", w="other"), r, k)            # a pointer already set is left alone
    assert releases.active() == seed and releases.pointer()["revision"] == 1
    assert [e["release"] for e in Events(tmp_path).read("installed")] == [seed.ref()]
    assert releases.history() == [seed]


def test_a_candidate_release_replaces_w_and_carries_r_and_k_over(ws):
    objects, releases, seed = ws
    candidate, receipt_ref = accepted(objects, releases, "W1")
    assert candidate.parent_ref == seed.ref() and candidate.receipt_ref == receipt_ref and candidate.reason == "gate"
    assert (candidate.improver_ref, candidate.core_ref) == (seed.improver_ref, seed.core_ref)
    assert candidate.package_ref != seed.package_ref


# ---------------------------------------------------------------------------- install
def test_an_accepted_registered_receipt_installs_by_compare_and_swap(ws):
    objects, releases, seed = ws
    candidate, receipt_ref = accepted(objects, releases, "W1")
    receipt = install(releases, candidate, receipt_ref, "op-1")
    assert (receipt.status, receipt.target_release, receipt.expected_parent) == ("installed", candidate.ref(),
                                                                                 seed.ref())
    assert releases.active() == candidate and releases.pointer() == {"release_ref": candidate.ref(), "revision": 2,
                                                                     "last_operation_id": "op-1"}
    assert releases.recover("op-1") == receipt and objects.load(objects.put(receipt)) == receipt
    assert releases.history() == [candidate, seed]
    assert candidate.ref() in releases.installed()


def test_two_candidates_on_one_parent_do_not_both_install(ws):
    objects, releases, seed = ws
    one, one_receipt = accepted(objects, releases, "one")
    two, two_receipt = accepted(objects, releases, "two")
    first = install(releases, one, one_receipt, "op-a")
    second = install(releases, two, two_receipt, "op-b")
    assert first.status == "installed" and second.status == "stale_parent"
    assert second.observed_pointer == one.ref() and releases.active() == one
    assert releases.recover("op-b").status == "stale_parent"
    assert [e["release"] for e in releases.events.read("stale_parent")] == [two.ref()]


def test_a_candidate_built_on_another_parent_is_stale_rather_than_rebased(ws):
    objects, releases, seed = ws
    one, one_receipt = accepted(objects, releases, "one")
    install(releases, one, one_receipt, "op-a")
    late, late_receipt = accepted(objects, releases, "late", parent=seed)
    with pytest.raises(Stale, match="built on"):
        releases.install(late, late_receipt, expected_parent=one.ref(), operation_id="op-c")
    assert releases.active() == one


def test_an_unregistered_or_unaccepted_receipt_never_installs(ws):
    objects, releases, seed = ws
    for verdict in ("rejected", "inconclusive", "invalid"):
        candidate, receipt_ref = accepted(objects, releases, verdict, verdict=verdict)
        with pytest.raises(NotAuthorized, match="not accepted"):
            install(releases, candidate, receipt_ref, f"op-{verdict}")
    handwritten, receipt_ref = accepted(objects, releases, "handwritten", register=False)
    with pytest.raises(NotAuthorized, match="not registered"):
        install(releases, handwritten, receipt_ref, "op-y")
    assert releases.active() == seed and releases.pointer()["revision"] == 1


def test_a_release_installs_only_the_package_and_the_k_its_receipt_accepted(ws):
    objects, releases, seed = ws
    candidate, receipt_ref = accepted(objects, releases, "W1")
    swapped = Release(parent_ref=seed.ref(), package_ref=ref(objects, "package", w="not gated"),
                      improver_ref=seed.improver_ref, core_ref=seed.core_ref, receipt_ref=receipt_ref, reason="gate")
    with pytest.raises(NotAuthorized, match="does not install the package"):
        install(releases, swapped, receipt_ref, "op-swap")
    other_k = Release(parent_ref=seed.ref(), package_ref=candidate.package_ref, improver_ref=seed.improver_ref,
                      core_ref=ref(objects, "core", k="K2"), receipt_ref=receipt_ref, reason="gate")
    with pytest.raises(NotAuthorized, match="within one K"):
        install(releases, other_k, receipt_ref, "op-k")
    assert releases.active() == seed


def test_a_plan_that_confirms_installs_only_a_confirmed_receipt(ws):
    objects, releases, seed = ws
    confirming = TaskEvalPlan(task_ids=("t1",), confirmation_repeats=1, control_pooling="pooled",
                              gate_plan=GatePlan().to_json())
    full, full_receipt = accepted(objects, releases, "full", plan=confirming)
    with pytest.raises(NotAuthorized, match="confirms a candidate before it installs"):
        install(releases, full, full_receipt, "op-full")
    done, done_receipt = accepted(objects, releases, "confirmed", plan=confirming, kind="confirmed")
    assert install(releases, done, done_receipt, "op-confirmed").status == "installed"
    # a plan without a confirmation installs a full reading
    single = TaskEvalPlan(task_ids=("t1",), gate_plan=GatePlan().to_json())
    plain, plain_receipt = accepted(objects, releases, "plain", plan=single)
    assert install(releases, plain, plain_receipt, "op-plain").status == "installed"


def test_replaying_the_same_install_operation_commits_once(ws):
    objects, releases, seed = ws
    candidate, receipt_ref = accepted(objects, releases, "W1")
    first = install(releases, candidate, receipt_ref, "op-1")
    revision = releases.pointer()["revision"]
    again = install(releases, candidate, receipt_ref, "op-1")       # a recovered controller replays the operation
    assert again == first and releases.pointer()["revision"] == revision
    other, other_receipt = accepted(objects, releases, "W2", parent=seed)
    with pytest.raises(ValueError, match="another release"):
        releases.install(other, other_receipt, expected_parent=seed.ref(), operation_id="op-1")
    assert releases.active() == candidate and releases.recover("op-unknown") is None


def test_a_committed_operation_replayed_after_the_pointer_moved_on_keeps_its_answer(ws):
    """A replay is answered from the record: it neither commits again nor rewrites an install that happened as a
    stale parent."""
    objects, releases, seed = ws
    one, one_receipt = accepted(objects, releases, "one")
    first = install(releases, one, one_receipt, "op-1")
    two, two_receipt = accepted(objects, releases, "two")
    install(releases, two, two_receipt, "op-2")
    assert install(releases, one, one_receipt, "op-1") == first
    assert releases.recover("op-1") == first and releases.active() == two
    assert [e["operation"] for e in releases.events.read("stale_parent")] == []


def test_an_install_interrupted_after_its_commit_is_recovered_as_committed(ws):
    """The pointer moved but the operation's record still says intent (the process died in between): the replay
    finds its own commit on the pointer and finishes the record, without a second commit."""
    objects, releases, seed = ws
    candidate, receipt_ref = accepted(objects, releases, "W1")
    install(releases, candidate, receipt_ref, "op-1")
    path = releases.dir / "operations" / "op-1.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "status": "intent", "receipt": None}))
    revision = releases.pointer()["revision"]
    again = install(releases, candidate, receipt_ref, "op-1")
    assert again.status == "installed" and again.reason == "already committed by this operation"
    assert releases.pointer()["revision"] == revision and releases.recover("op-1") == again


# ---------------------------------------------------------------------------- revocation and rollback
def test_a_revoked_release_and_its_descendants_never_install(ws):
    objects, releases, seed = ws
    one, one_receipt = accepted(objects, releases, "one")
    install(releases, one, one_receipt, "op-a")
    assert releases.revoke(one.ref(), "broke a task") is True
    assert releases.revoke(one.ref(), "again") is False                 # revoking twice writes nothing
    assert [e["reason"] for e in releases.revocations()["revoked"]] == ["broke a task"]
    assert [e["reason"] for e in releases.events.read("revoked")] == ["broke a task"]
    two, two_receipt = accepted(objects, releases, "two")
    objects.put(two)
    assert releases.revoked(one.ref()) and releases.revoked(two.ref()) and not releases.revoked(seed.ref())
    receipt = install(releases, two, two_receipt, "op-b")
    assert receipt.status == "installation_failed" and "revoked" in receipt.reason
    assert releases.active() == one


def test_a_rollback_returns_to_the_last_good_release(ws):
    objects, releases, seed = ws
    one, one_receipt = accepted(objects, releases, "one")
    install(releases, one, one_receipt, "op-a")
    assert releases.last_good() == seed.ref()
    receipt = releases.rollback("the new package broke a task", operation_id="op-rb")
    assert receipt.status == "installed" and receipt.target_release == seed.ref()
    assert receipt.expected_parent == one.ref() and receipt.reason.startswith("rollback")
    assert releases.active() == seed and releases.revoked(one.ref())
    events = [e["event"] for e in releases.events.all()]
    assert events.index("revoked") < events.index("rolled_back")         # recorded before the pointer moved
    assert releases.pointer()["last_operation_id"] == "op-rb"


def test_a_revoked_ancestor_is_skipped_by_the_rollback(ws):
    objects, releases, seed = ws
    one, one_receipt = accepted(objects, releases, "one")
    install(releases, one, one_receipt, "op-a")
    two, two_receipt = accepted(objects, releases, "two")
    install(releases, two, two_receipt, "op-b")
    assert releases.history() == [two, one, seed] and releases.last_good() == one.ref()
    releases.revoke(one.ref(), "also bad")
    assert releases.last_good() == seed.ref()
    assert releases.rollback("rolling back two", operation_id="op-rb").target_release == seed.ref()
    assert releases.active() == seed


def test_an_ancestor_that_was_never_installed_is_not_good(ws):
    """A release can exist without ever having been active (a candidate that went stale): last_good passes over it."""
    objects, releases, seed = ws
    one, one_receipt = accepted(objects, releases, "one")
    two, _ = accepted(objects, releases, "two", parent=one)
    objects.put(one)
    objects.put(two)
    assert releases.last_good(two.ref()) == seed.ref()
    install(releases, one, one_receipt, "op-a")
    assert releases.last_good(two.ref()) == one.ref()


def test_a_rollback_with_no_good_ancestor_is_refused_and_changes_nothing(ws):
    objects, releases, seed = ws
    with pytest.raises(NotAuthorized, match="no installed, unrevoked ancestor"):
        releases.rollback("nothing to fall back to", operation_id="op-0")
    assert releases.active() == seed and releases.revocations() == {"revoked": []}
    one, one_receipt = accepted(objects, releases, "one")
    install(releases, one, one_receipt, "op-a")
    releases.revoke(seed.ref(), "the seed is bad too")
    with pytest.raises(NotAuthorized, match="no installed, unrevoked ancestor"):
        releases.rollback("its only ancestor is revoked", operation_id="op-1")
    assert releases.active() == one                                          # the pointer stays where it was
    assert [e["id"] for e in releases.revocations()["revoked"]] == [seed.ref()]   # and nothing more was revoked


# ---------------------------------------------------------------------------- the model binding (§8)
def train_artifact(objects, *, parent="claude-x", splits=("train",)):
    return objects.put_json("train_artifact", {"location": "store://m-1", "parent_model": parent,
                                               "recipe": {"lr": 1e-5}, "splits": list(splits)})


def test_a_model_binding_names_ks_actor_and_a_train_only_artifact(tmp_path):
    objects = Objects(tmp_path)
    core = {"model": {"provider": "claude_code", "identity": "claude-x", "effort": "medium"}}
    assert check_binding({"model": {"base": "claude-x", "effort": "medium"}}, core, objects) == []
    good = train_artifact(objects)
    assert check_binding({"model": {"base": "claude-x", "artifact_ref": good}}, core, objects) == []
    nested = objects.put_json("train_artifact", {"parent_model": "claude-x", "data": {"splits": ["train"]}})
    assert check_binding({"model": {"base": "claude-x", "artifact_ref": nested}}, core, objects) == []
    cases = {
        "is not K's actor": {"base": "claude-y"},
        "not a content reference": {"base": "claude-x", "artifact_ref": "train_artifact:abc"},
        "not a train_artifact": {"base": "claude-x", "artifact_ref": objects.put_json("package", {"w": 1})},
        "not in this activity's object store": {"base": "claude-x",
                                                "artifact_ref": "train_artifact:sha256:" + "0" * 64},
        "was trained from 'claude-y'": {"base": "claude-x", "artifact_ref": train_artifact(objects, parent="claude-y")},
        "comes from splits ['dev', 'train']": {"base": "claude-x",
                                               "artifact_ref": train_artifact(objects, splits=("train", "dev"))},
        "comes from splits []": {"base": "claude-x", "artifact_ref": train_artifact(objects, splits=())},
    }
    for said, model in cases.items():
        problems = check_binding({"model": model}, core, objects)
        assert len(problems) == 1 and said in problems[0], (said, problems)
    both = check_binding({"model": {"base": "claude-y", "artifact_ref": train_artifact(objects, parent="claude-y")}},
                         core, objects)
    assert len(both) == 2 and "trained from 'claude-y', not K's actor 'claude-x'" in both[1]
    assert check_binding({}, core, objects) == ["model.base None is not K's actor 'claude-x'"]


def test_a_model_identity_names_provider_and_base_and_not_when_it_was_seen():
    with pytest.raises(ContractError, match="provider and base model"):
        ModelIdentity(provider="claude_code")
    with pytest.raises(ContractError):
        ModelIdentity(provider="p", base_model="m", artifact_ref="not-a-ref")
    a = ModelIdentity(provider="p", base_model="m", observed_at="2026-10-01")
    assert a.ref() == ModelIdentity(provider="p", base_model="m", observed_at="2026-10-09").ref()
    assert a.ref() != ModelIdentity(provider="p", base_model="m", effort="low").ref()
    core = {"model": {"identity": "claude-x", "effort": "medium", "max_tokens": 32000}}
    assert identity_of(core) == ModelIdentity(provider="claude_code", base_model="claude-x", effort="medium",
                                              max_tokens=32000)


# ---------------------------------------------------------------------------- code identity
def test_code_version_changes_when_a_py_or_a_seed_file_changes_and_nothing_else(tmp_path, monkeypatch):
    """Every K carries the two versions (CONTRACTS §4 v1.2: attempt_version over what an attempt runs, improver_version
    over the rest), so a changed .py is a new K; code_version hashes all of it for reports. Measured on a copy of a
    package tree, never on the checkout."""
    pkg = tmp_path / "ouroloop"
    (pkg / "evaluate").mkdir(parents=True)
    (pkg / "harness" / "seeds" / "stirrup").mkdir(parents=True)
    (pkg / "__pycache__").mkdir()
    (pkg / "evaluate" / "gate.py").write_text("MIN_GAIN = 0.05\n")
    (pkg / "harness" / "seeds" / "stirrup" / "prompt.md").write_text("Do every step.\n")
    (pkg / "notes.txt").write_text("not shipped\n")
    monkeypatch.setattr(rel, "PACKAGE", pkg)
    assert [p.relative_to(pkg).as_posix() for p in rel.package_files()] == [
        "evaluate/gate.py", "harness/seeds/stirrup/prompt.md"]
    before = rel.code_version()
    assert rel.code_version() == before and len(before) == 16
    (pkg / "notes.txt").write_text("changed, still not shipped\n")
    (pkg / "__pycache__" / "gate.cpython-312.pyc").write_bytes(b"\0compiled")
    (pkg / ".DS_Store").write_bytes(b"\0")
    assert rel.code_version() == before
    (pkg / "evaluate" / "gate.py").write_text("MIN_GAIN = 0.06\n")
    changed = rel.code_version()
    assert changed != before
    (pkg / "harness" / "seeds" / "stirrup" / "prompt.md").write_text("Do every step twice.\n")
    assert rel.code_version() not in (before, changed)
    (pkg / "harness" / "seeds" / "stirrup" / "prompt.md").write_text("Do every step.\n")
    (pkg / "evaluate" / "gate.py").write_text("MIN_GAIN = 0.05\n")
    assert rel.code_version() == before
    (pkg / "evaluate" / "gate.py").rename(pkg / "evaluate" / "other.py")       # a moved module is other code
    assert rel.code_version() != before
    # the split: the seed is the attempt's side, the gate the improver's; each hash moves with its own files only
    (pkg / "evaluate" / "other.py").rename(pkg / "evaluate" / "gate.py")
    attempt, improver = rel.attempt_version(), rel.improver_version()
    (pkg / "evaluate" / "gate.py").write_text("MIN_GAIN = 0.07\n")
    assert rel.attempt_version() == attempt and rel.improver_version() != improver
    (pkg / "harness" / "seeds" / "stirrup" / "prompt.md").write_text("Do every step, twice.\n")
    assert rel.attempt_version() != attempt


def test_code_version_of_this_checkout_covers_its_modules():
    files = {p.relative_to(rel.PACKAGE).as_posix() for p in rel.package_files()}
    assert {"contracts.py", "evaluate/release.py", "train/jobs/hosts/bootstrap.sh"} <= files
    # R's prompts are read at every round step: an edit of one is another K
    assert {f"improver/prompts/{n}.md" for n in ("diagnose", "package_api", "playbook", "propose")} <= files
    assert not any(f.endswith(".pyc") or "__pycache__" in f for f in files)
    assert rel.code_version() == rel.code_version()
    attempt = {f for f in files if rel.is_attempt_file(f)}
    assert {"contracts.py", "evaluate/attempt.py", "runtime/runner.py", "harness/package.py",
            "harness/seeds/stirrup/manifest.json"} <= attempt
    assert {"evaluate/release.py", "evaluate/gate.py", "improver/round.py", "activity/phases.py",
            "improver/prompts/propose.md"} <= files - attempt
    assert len(rel.attempt_version()) == len(rel.improver_version()) == 16


def test_attempt_identity_reads_the_attempt_code_the_package_the_limits_the_model_the_image_and_the_split():
    base = dict(attempt_version="a1", package_ref="package:sha256:" + "1" * 64, limits={"max_steps": 50},
                model={"provider": "claude_code", "identity": "m", "effort": "medium", "max_tokens": 32000,
                       "timeout": 600}, image_id="sha256:img", split_hash="s1")
    same = rel.attempt_identity(**{**base, "model": {**base["model"], "timeout": 900}})      # not an identity field
    assert rel.attempt_identity(**base) == same and len(same) == 16
    for change in ({"attempt_version": "a2"}, {"package_ref": "package:sha256:" + "2" * 64},
                   {"limits": {"max_steps": 49}}, {"model": {**base["model"], "effort": "high"}},
                   {"image_id": None}, {"split_hash": "s2"}):
        assert rel.attempt_identity(**{**base, **change}) != same


# ---------------------------------------------------------------------------- the release line
def test_promote_by_decision_releases_the_working_line_without_a_receipt_and_by_the_gate_wants_one(ws):
    """EVALUATE §7 (stage 4c): a promotion by decision carries no receipt; one by the gate needs a registered,
    accepted receipt of the same package; promoting again under the same operation is the same release."""
    objects, releases, seed = ws
    assert releases.released_ref() is None and releases.released() is None
    with pytest.raises(NotAuthorized, match="names its receipt"):
        releases.promote(seed, None, reason="gate", operation_id="g1")
    with pytest.raises(ContractError, match="gate or decision"):
        releases.promote(seed, None, reason="screen", operation_id="x")
    with pytest.raises(NotAuthorized, match="carries no receipt"):
        releases.promote(seed, ref(objects, "receipt"), reason="decision", operation_id="d0")
    released = releases.promote(seed, None, reason="decision", operation_id="d1")
    assert (released.reason, released.parent_ref, released.package_ref, released.receipt_ref) == \
        ("decision", seed.ref(), seed.package_ref, None)
    assert releases.released_ref() == released.ref() and releases.released() == released
    assert releases.active_ref() == seed.ref()                        # the working line does not move
    assert releases.promote(seed, None, reason="decision", operation_id="d1") == released
    assert [e["reason"] for e in releases.events.read("released")] == ["decision"]
    assert released.ref() in releases.installed()
