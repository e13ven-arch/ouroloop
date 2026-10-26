"""The shared contracts (docs/design/CONTRACTS.md §2, §8): identity, records, the object store and its closure, events,
redaction and the error classes."""
import json
import threading
from dataclasses import dataclass

import pytest

from reharness import contracts as ct
from reharness.evaluate.gate import GatePlan, TaskEvalPlan
from reharness.evaluate.release import GateReceipt


@ct.register
@dataclass(frozen=True)
class Note(ct.Record):
    """A record of this test only: one identifying field, one optional, two volatile."""
    TYPE = "contracts_test_note"
    text: str = ""
    extra: str | None = None
    note: str = ""
    created: str = ""


# ---------------------------------------------------------------------------- identity
def test_key_order_is_the_same_object_and_a_changed_word_is_another():
    assert ct.canonical({"a": 1, "b": [1, 2]}) == ct.canonical({"b": [1, 2], "a": 1})
    assert ct.ref_for("change", {"a": 1, "b": 2}) == ct.ref_for("change", {"b": 2, "a": 1})
    assert ct.ref_for("change", {"text": "notify the owner"}) != ct.ref_for("change", {"text": "notify the owners"})
    assert ct.ref_for("change", {"x": 1}) != ct.ref_for("other", {"x": 1})       # the kind is part of the name


def test_nan_and_infinity_are_refused(tmp_path):
    with pytest.raises(ct.ContractError):
        ct.canonical({"x": float("inf")})
    with pytest.raises(ct.ContractError):
        ct.Objects(tmp_path).put_json("change", {"threshold": float("nan")})


def test_a_ref_carries_its_kind_and_the_whole_digest():
    payload = {"text": "x"}
    ref = ct.ref_for("change", payload)
    assert ref == f"change:sha256:{ct.digest(payload)}" and ct.parse_ref(ref) == ("change", ct.digest(payload))
    assert ct.short(ref) == f"change:{ct.digest(payload)[:12]}"
    for bad in ("change:" + ct.digest(payload)[:16], "Change:sha256:" + "a" * 64, "change:sha256:" + "a" * 63, None):
        with pytest.raises(ct.ContractError):
            ct.parse_ref(bad)
    with pytest.raises(ct.ContractError, match="lowercase"):
        ct.ref_for("Bad-Kind", payload)
    with pytest.raises(ct.ContractError, match="not a release"):
        ct.check_refs([ref], kind="release")
    assert ct.check_refs([ref], kind="change") == (ref,)


def test_volatile_fields_and_absent_values_do_not_change_identity():
    a = Note(text="h", note="first look", created="2026-10-01")
    assert a.ref() == Note(text="h", note="rewritten").ref() == Note(text="h", extra=None).ref()
    assert a.payload() == {"text": "h"}
    assert Note(text="h", extra="e").ref() != a.ref() != Note(text="other").ref()


def test_a_record_round_trips_and_refuses_a_key_it_does_not_declare():
    a = Note(text="h", extra="e", note="kept, not hashed")
    data = json.loads(json.dumps(a.to_json()))
    assert data["ref"] == a.ref() and Note.from_json(data) == a
    with pytest.raises(ct.ContractError, match="does not take"):
        Note.from_json({**data, "invented": 1})


def test_the_registry_finds_a_class_by_its_kind_and_takes_each_kind_once(tmp_path):
    objects = ct.Objects(tmp_path)
    ref = objects.put(Note(text="h"))
    assert ref == Note(text="h").ref() and objects.load(ref) == Note(text="h")
    with pytest.raises(ct.ContractError, match="not a"):
        objects.load(ref, GatePlan)
    with pytest.raises(ct.ContractError, match="no record type"):
        objects.load(objects.put_json("unregistered_kind", {"x": 1}))

    @dataclass(frozen=True)
    class Impostor(ct.Record):
        TYPE = "contracts_test_note"
    with pytest.raises(ct.ContractError, match="registered twice"):
        ct.register(Impostor)
    assert ct.TYPES["contracts_test_note"] is Note
    assert ct.register(Note) is Note                                  # the same class again is no change


# ---------------------------------------------------------------------------- the store
def test_objects_are_written_once_and_read_back(tmp_path):
    objects = ct.Objects(tmp_path)
    ref = objects.put("change", {"text": "x"})
    path = objects.path(ref)
    before = path.stat().st_mtime_ns
    assert objects.put_json("change", {"text": "x"}) == ref and path.stat().st_mtime_ns == before
    assert objects.get(ref) == {"text": "x"} and objects.has(ref)
    assert objects.refs("change") == [ref]
    assert path.read_bytes() == ct.canonical({"text": "x"})
    missing = "change:sha256:" + "0" * 64
    assert not objects.has(missing)
    with pytest.raises(KeyError):
        objects.get(missing)


def test_a_stored_object_must_hash_to_its_contents(tmp_path):
    objects = ct.Objects(tmp_path)
    ref = objects.put_json("change", {"text": "original"})
    objects.path(ref).write_text(json.dumps({"text": "tampered"}), encoding="utf-8")
    with pytest.raises(ct.ContractError, match="does not hash"):
        objects.get(ref)


def test_an_embedded_records_own_ref_is_not_a_reference(tmp_path):
    """A plan embeds its gate whole, own ref included, and never stores the gate: copying the plan's closure does not
    go looking for it. A field named ref that points at another object is still followed."""
    source, target = ct.Objects(tmp_path / "a"), ct.Objects(tmp_path / "b")
    gate = GatePlan(min_groups=1)
    plan_ref = source.put(TaskEvalPlan(task_ids=("t1",), gate_plan=gate.to_json()))
    assert source.get(plan_ref)["gate_plan"]["ref"] == gate.ref() and not source.has(gate.ref())
    assert gate.ref() not in ct.refs_in(source.get(plan_ref))
    results_ref = source.put_json("gate_results", {"plan": plan_ref, "attempts": {}})
    receipt_ref = source.put(GateReceipt(plan_ref=plan_ref, results_ref=results_ref))
    assert ct.copy_closure(source, target, [receipt_ref]) == {receipt_ref, plan_ref, results_ref}
    assert target.load(plan_ref, TaskEvalPlan).gate().ref() == gate.ref()
    assert target.get(receipt_ref) == source.get(receipt_ref)
    assert ct.refs_in({"gate": {"ref": receipt_ref, "min_gain": 0.05}}) == {receipt_ref}
    assert ct.refs_in(["x", {"a": [plan_ref, "plan:sha256:short"]}]) == {plan_ref}


def test_a_closure_with_a_dangling_reference_is_not_copied_in_part(tmp_path):
    source, target = ct.Objects(tmp_path / "a"), ct.Objects(tmp_path / "b")
    dangling = "change:sha256:" + "1" * 64
    ref = source.put_json("change", {"parent": dangling})
    with pytest.raises(KeyError):
        ct.copy_closure(source, target, [ref])


# ---------------------------------------------------------------------------- events and files
def test_events_are_appended_in_order_and_read_by_kind(tmp_path):
    events = ct.Events(tmp_path)
    events.append("installed", release="release:sha256:" + "a" * 64)
    events.append("revoked", target="release:sha256:" + "a" * 64, reason="because")
    assert [e["event"] for e in events.all()] == ["installed", "revoked"]
    assert [e["reason"] for e in events.read("revoked")] == ["because"]
    assert all(e["ts"] for e in events.all())
    assert [e["event"] for e in ct.Events(tmp_path).read()] == ["installed", "revoked"]     # what is on disk


def test_events_are_redacted_before_they_are_written(tmp_path):
    events = ct.Events(tmp_path)
    events.append("note", detail="api_key=sk-abcdefghijklmnopqrstuvwx",
                  nested={"auth": ["Bearer abcdefghijklmnopqrstu"]})
    text = events.path.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnopqrstuvwx" not in text and "abcdefghijklmnopqrstu" not in text
    assert text.count("[REDACTED]") == 2


def test_redact_hides_secrets_and_keeps_numbers_and_shapes():
    secrets = ["sk-" + "a" * 24, "AKIA" + "B" * 16, "ghp_" + "c" * 36, "xoxb-" + "1" * 12,
               "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12, "0123456789abcdef0123456789abcdef.ABCDEFGHIJKLMNOP",
               "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"]
    for secret in secrets:
        assert secret not in ct.redact(f"value {secret} end"), secret
    assert ct.redact("password: hunter22") == "[REDACTED]"
    assert ct.redact("max_tokens: 4096") == "max_tokens: 4096"         # a number is no secret
    assert ct.redact({"a": ["token=abc123", 7, None]}) == {"a": ["[REDACTED]", 7, None]}


def test_jsonl_lines_from_many_threads_are_written_whole(tmp_path):
    path = tmp_path / "log.jsonl"

    def write(n):
        for i in range(50):
            ct.append_jsonl(path, {"writer": n, "i": i, "pad": "x" * 500})
    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = list(ct.read_jsonl(path))
    assert len(rows) == 400 and {(r["writer"], r["i"]) for r in rows} == {(n, i) for n in range(8) for i in range(50)}
    assert list(ct.read_jsonl(tmp_path / "absent.jsonl")) == []


def test_a_durable_write_replaces_the_file_whole_and_leaves_no_temporary(tmp_path):
    path = tmp_path / "d" / "f.json"
    ct.durable_write(path, b"first")
    ct.durable_write(path, b"second")
    ct.write_json(tmp_path / "d" / "g.json", {"k": 1})
    assert path.read_bytes() == b"second" and json.loads((tmp_path / "d" / "g.json").read_text()) == {"k": 1}
    assert sorted(p.name for p in path.parent.iterdir()) == ["f.json", "g.json"]


# ---------------------------------------------------------------------------- errors (CONTRACTS §8)
def test_the_error_classes_keep_defects_infrastructure_limits_and_budget_apart():
    assert issubclass(ct.ContractError, ValueError) and issubclass(ct.TrainOnlyError, ct.ContractError)
    for infra in (ct.WorkerUnavailable, ct.StaleWorker, ct.WorkerCrashed, ct.EgressUnavailable, ct.MemorySidecarError):
        assert issubclass(infra, ct.Infrastructure) and not issubclass(infra, ct.CandidateDefect)
    assert issubclass(ct.StaleWorker, ct.WorkerUnavailable)
    # a defect may not be retried as infrastructure, and a limit is neither
    assert not issubclass(ct.CandidateDefect, ct.Infrastructure)
    for cls in (ct.LimitHit, ct.BudgetError):
        assert not issubclass(cls, (ct.CandidateDefect, ct.Infrastructure, ct.ContractError))
    assert issubclass(ct.BudgetExhausted, ct.BudgetError) and issubclass(ct.BudgetMismatch, ct.BudgetError)
    hit = ct.LimitHit("tool_calls")
    assert hit.limit == "tool_calls" and "tool_calls" in str(hit)
