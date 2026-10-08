"""The four roles (docs/DESIGN.md, roles and layouts): a model store behind one interface ("dir", "hf"), serving as the one
way a trained model is asked, a deploy between the gate's verdict and the champion pointer, and a rollback to the last
good ancestor. Old configs (transport, [models]) still load. Nothing trains, nothing leaves this machine."""
import importlib.util
import json
import sys

import httpx
import pytest

from conftest import accept
from jobs_fixtures import FAKE_HF, keys  # noqa: F401 - keys is a fixture
from ouroloop import config
from ouroloop.decision import Noul
from ouroloop.evolve.gate import freeze_template, records
from ouroloop.evolve.pipeline import deploy, deploy_version, evolve_model, full_recipe, parent_ref
from ouroloop.evolve.plan import decide
from ouroloop.evolve.train import JobTrainer, TrainError
from ouroloop.evolve.registry import Registry
from ouroloop.ledger import Ledger, PromotionConflict
from ouroloop.release import code_version
from ouroloop.policy import Policy
from ouroloop.release import Revoked, revoke
from ouroloop.runtime import Runtime
from ouroloop.serving import PROBE, Serving
from ouroloop.stores import make
from ouroloop.trainers import signature
from test_counting import Says, session
from test_gate_record import ACT_GATE, CFG, SOME, OracleTrainer, collect, ctx

pytestmark = pytest.mark.usefixtures("loose_risk")

EXAMPLE = {"id": "e", "state": "say yes", "question": "done?", "candidates": [{"name": "yes"}, {"name": "no"}],
           "target": [1.0, 0.0], "meta": {"weight": 1.0}}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A stand-in for huggingface_hub, keeping repositories under tmp_path/hub."""
    path = tmp_path / "fake_hf.py"
    path.write_text(FAKE_HF)
    monkeypatch.setenv("FAKE_HF", str(tmp_path / "hub"))
    spec = importlib.util.spec_from_file_location("huggingface_hub", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)
    return tmp_path / "hub"


def signature_job(jobs, job_id: str) -> str:
    """A job directory whose run the signature plugin trained. -> the job's directory."""
    d = jobs / job_id
    d.mkdir(parents=True)
    for name in ("train.jsonl", "val.jsonl"):
        (d / name).write_text(json.dumps(EXAMPLE) + "\n")
    (d / "spec.json").write_text(json.dumps({"job": job_id}))
    signature.train({"options": {}}, str(d), 0)
    return str(d)


class Spy(Serving):
    """Serving that records what it was asked: each backend spec, every request to a backend it built, deploys and
    drops. With `down`, a deploy fails as a model server that cannot load the model does."""

    def __init__(self, down: bool = False):
        super().__init__()
        self.down, self.specs, self.requests, self.deploys, self.drops = down, [], [], [], []

    def backend(self, spec):
        self.specs.append(spec)
        inner, requests = super().backend(spec), self.requests

        class Seen:
            name = inner.name

            def decide(self, batch):
                requests.extend(batch)
                return inner.decide(batch)
        return Seen()

    def deploy(self, spec):
        self.deploys.append(spec)
        if self.down:
            raise httpx.ConnectError("the model server is down")
        return super().deploy(spec)

    def drop(self, spec):
        self.drops.append(spec)


class NoBackend(OracleTrainer):
    """An Oracle trainer that never builds a backend: whatever asks its model goes through serving."""

    def backend(self, result):
        raise AssertionError("the trainer was asked for a backend")


def workspace(tmp_path, serving=None, n=150):
    rt = config.build_runtime(CFG, tmp_path)
    rt.serving = serving or rt.serving
    collect(rt, n)
    freeze_template(rt.root, ACT_GATE)
    return rt


# ---------------------------------------------------------------- the model store
@pytest.mark.parametrize("table", [{"type": "dir"}, {"type": "dir", "root": "models"},
                                   {"type": "hf", "prefix": "org/ouroloop-"}])
def test_a_dir_store_and_an_hf_store_behind_one_interface(tmp_path, hub, monkeypatch, table):
    monkeypatch.chdir(tmp_path)
    jobs = tmp_path / "jobs"
    table = {**table, **({"root": str(jobs)} if table == {"type": "dir"} else {})}
    store = make(table).here()
    job = signature_job(jobs, "m_0001_x")
    ref = store.publish(f"{job}/run", job, "m_0001_x")
    assert store.job(ref) == "m_0001_x" and store.table["type"] == table["type"]
    assert json.loads(open(f"{store.fetch(ref)}/model.json").read())["table"]["done?"]
    if table.get("root") == str(jobs):                      # the run is published where it was trained
        assert store.publish(f"{job}/run", job, "m_0001_x") == ref
    else:
        with pytest.raises(OSError):                        # a ref names one run: publishing again never replaces it
            store.publish(f"{job}/run", job, "m_0001_x")
    other = "m_0001_x" if table["type"] == "hf" else f"org/ouroloop-m_0001_x@{'a' * 40}"
    for bad in (other, "../x", "/etc/passwd", f"else/where@{'a' * 40}", 7):
        assert store.job(bad) is None
        with pytest.raises(ValueError, match="not a model ref"):
            store.fetch(bad)
    # in-process serving over the store: the plugin that trained it loads it, answering as its ref
    backend = Serving(store=store).backend({"type": "model", "model": ref, "trainer": "signature"})
    assert backend.decide([PROBE])[0].model == ref


def test_old_configs_with_transport_and_models_still_load(tmp_path):
    old = {"trainer": {"type": "signature", "jobs_root": "~/jobs"}, "models": {"jobs_root": "/srv/jobs"}}
    assert config.store_table(old) == {"type": "dir", "root": "/srv/jobs"}
    assert config.store_table({"trainer": {"type": "signature", "jobs_root": "~/jobs"}}) == {"type": "dir",
                                                                                            "root": "~/jobs"}
    hf = {"trainer": {"type": "signature", "transport": "hf", "hf_prefix": "org/o-"}, "models": {"url": "http://g:1"}}
    assert config.store_table(hf) == {"type": "hf", "prefix": "org/o-"}
    assert config.serving_table(hf) == {"url": "http://g:1"} and config.serving_table(old) == {}
    trainer = config.build_trainer(hf)
    assert trainer.store.table == {"type": "hf", "prefix": "org/o-"} and trainer.serving.url == "http://g:1"
    rt = config.build_runtime({**CFG, **old}, tmp_path)
    assert rt.serving.store.table == {"type": "dir", "root": "/srv/jobs"} and not rt.serving.url
    for bad, why in (({**old, "serving": {}}, "keep one"),
                     ({"trainer": {"type": "x", "transport": "s3"}}, "transport"),
                     ({"store": {"type": "dir"}, "models": {"jobs_root": "/a", "hf_prefix": "org/o-"}},
                      r"\[store\] and \[models\] hf_prefix, \[models\] jobs_root: .* keep one")):
        with pytest.raises(ValueError, match=why):
            config.build_serving(bad)
    with pytest.raises(ValueError, match=r"\[store\] and \[trainer\] transport, \[trainer\] hf_prefix"):
        config.build_trainer({"trainer": hf["trainer"], "store": {"type": "dir"}, "serving": {"url": "http://x:1"}})
    # an old model server's config: [models] jobs_root and hf_prefix, no transport, loaded refs from the Hub
    server = {"models": {"jobs_root": "~/.ouroloop/jobs", "hf_prefix": "org/o-"}}
    assert config.store_table(server) == {"type": "hf", "prefix": "org/o-"}
    assert config.build_serving(server).store.job(f"org/o-m_0001_x@{'a' * 40}") == "m_0001_x"


# ---------------------------------------------------------------- serving
def test_calibration_and_test_scoring_ask_through_serving_and_never_the_trainer(tmp_path):
    spy = Spy()
    rt = workspace(tmp_path, spy)
    card = evolve_model(rt, NoBackend("R1"))
    assert card["gate"] == "accepted" and Registry(rt.root).champion()["version"] == card["version"]
    assert spy.specs[0] == card["backend"] and spy.deploys == [card["backend"]]
    record = records(rt.root)[-1]
    cal, n = card["calibration"]["stop"]["n"], len(record.test_sessions)
    states = [r.state for r in spy.requests]
    assert states.count(PROBE.state) == 1 and len(states) >= cal + n     # calibration, test scoring, the probe
    assert rt.ask("stop", ctx(), session="s_new", phase="per_turn").model == "R1"


def test_serving_deploys_a_ref_by_loading_it_and_drops_it(tmp_path):
    jobs = tmp_path / "jobs"
    signature_job(jobs, "m_0001_x")
    serving = config.build_serving({"store": {"type": "dir", "root": str(jobs)}})
    spec = {"type": "model", "model": "m_0001_x", "trainer": "signature"}
    assert serving.deploy(spec) == "m_0001_x" and list(serving._loaded) == [("m_0001_x", "signature")]
    with pytest.raises(ValueError, match="no model"):
        serving.deploy({**spec, "model": "m_0002_none"})
    serving.drop(spec)
    assert serving._loaded == {}


def test_drop_closes_the_model_and_a_job_releases_only_a_serving_on_its_machine():
    closed = []

    class Model:
        def close(self):
            closed.append(True)

    serving = Serving()
    serving._loaded = {("m_0001_x", "signature"): Model(), ("m_0002_y", "signature"): Model()}
    serving.drop({"type": "model", "model": "m_0001_x"})
    assert closed == [True] and list(serving._loaded) == [("m_0002_y", "signature")]

    class Released(Serving):
        def release(self):
            self.released = True

    hf = {"type": "hf", "prefix": "org/o-"}
    for host, url, shares in (("user@gpu", "http://gpu:8765", True), ("user@gpu-a", "http://serve-b:8765", False),
                              (None, "http://127.0.0.1:8765", True), ("local", "http://serve-b:8765", False),
                              ("user@gpu", None, False), (None, None, True)):
        serving = Released({"url": url} if url else {})
        JobTrainer("signature", host=host, store=hf, serving=serving)._release()
        assert getattr(serving, "released", False) is shares, (host, url)


def test_only_a_run_that_is_copied_or_uploaded_keeps_publish_s_of_the_deadline(tmp_path):
    jobs = str(tmp_path / "jobs")
    for store, kept in (({"type": "dir", "root": jobs}, 0.0), ({"type": "dir", "root": str(tmp_path / "m")}, 60.0),
                        ({"type": "hf", "prefix": "org/o-"}, 60.0), (None, 0.0)):
        assert JobTrainer("signature", jobs_root=jobs, store=store, publish_s=60).publish_s == kept
    remote = JobTrainer("signature", host="u@gpu", store={"type": "dir"}, serving={"url": "http://gpu:1"},
                        publish_s=60)
    assert remote.publish_s == 0.0                  # both at ~/.ouroloop/jobs, on that host


def test_the_champion_is_the_parent_only_in_the_same_store_on_the_same_machine():
    def champion(ref, host):
        return {"version": "m_0003", "model_id": ref, "job": {"host": host},
                "backend": {"type": "model", "model": ref, "trainer": "signature"}}

    serving = {"url": "http://gpu:1"}
    on_gpu = JobTrainer("signature", host="user@gpu", store={"type": "dir"}, serving=serving)
    recipe = full_recipe(on_gpu, {"init_from": "/data/base"})
    assert parent_ref(champion("m_0003_a", None), on_gpu, recipe) == "/data/base"      # trained on another machine
    assert parent_ref(champion("m_0003_a", "user@other"), on_gpu, recipe) == "/data/base"
    assert parent_ref(champion("m_0003_a", "user@gpu"), on_gpu, recipe) == "m_0003_a"
    here = JobTrainer("signature", host="local", store={"type": "dir"})
    assert parent_ref(champion("m_0003_a", None), here, recipe) == "m_0003_a"         # None and "local": this one
    hub = JobTrainer("signature", host="user@gpu-b", store={"type": "hf", "prefix": "org/o-"})
    ref = f"org/o-m_0003_a@{'a' * 40}"
    assert parent_ref(champion(ref, "user@gpu-a"), hub, recipe) == ref                # the Hub: from anywhere


# ---------------------------------------------------------------- a new store is a new module
OBJECTS = '''
class ObjectStore:
    TYPE, ON_TRAINING_HOST, ENV, REQUIREMENTS = "objects", False, ("OBJ_KEY",), ("objclient",)

    def __init__(self, table):
        self.table = dict(table)

    def here(self):
        return self

    def in_place(self, jobs_root):
        return False

    @staticmethod
    def probe():
        return {"client": True}

    @staticmethod
    def ready(record):
        return [] if (record or {}).get("client") else ["no objclient"]

    def job(self, ref):
        return ref[4:] if isinstance(ref, str) and ref.startswith("obj:") else None


STORE = ObjectStore
'''


def test_a_new_store_is_a_new_module_only(tmp_path, monkeypatch, keys):
    from ouroloop import stores
    from ouroloop.evolve import cloud as clouds
    from ouroloop.hosts import manifest, plan, stale
    from jobs_fixtures import Ssh, VastApi, a_job, options
    (tmp_path / "more").mkdir()
    (tmp_path / "more" / "objects.py").write_text(OBJECTS)
    monkeypatch.setattr(stores, "__path__", [*stores.__path__, str(tmp_path / "more")])
    monkeypatch.delitem(sys.modules, "ouroloop.stores.objects", raising=False)
    store = stores.make({"type": "objects"})
    assert "objects" in stores.types() and manifest()["stores"]["objects"] == {"client": True}
    assert "objclient" in (plan({}, tmp_path / "init", store) / "requirements.txt").read_text().splitlines()
    assert stale({"ouroloop": {"code": code_version()}}, {}, store) == ["no objclient"]
    assert stale({"ouroloop": {"code": code_version()}, "stores": {"objects": {"client": True}}}, {}, store) == []
    # a cloud job: the store's ENV is required here, and given to the instance
    api, seen = VastApi(), {}
    t = JobTrainer("signature", cloud=options("vast", keys), api=api.transport(), runner=Ssh(),
                   store={"type": "objects"}, poll_s=0, sleep=lambda s: None)
    with pytest.raises(TrainError, match="OBJ_KEY is not set here"):
        t.train(a_job(tmp_path / "ws"))
    assert not api.calls("PUT")

    def start(tag, job_id, opts, env):
        seen.update(env)
        raise clouds.CloudError("no instance in this test")
    monkeypatch.setenv("OBJ_KEY", "obj-secret")
    t.provider.start = start
    with pytest.raises(TrainError, match="no instance in this test"):
        t.train(a_job(tmp_path / "ws2"))
    assert seen["OBJ_KEY"] == "obj-secret" and "HF_TOKEN" not in seen


# ---------------------------------------------------------------- deploy
def test_a_failed_deploy_promotes_nothing_and_deploy_version_retries_it_later(tmp_path):
    rt = workspace(tmp_path, Spy(down=True))
    card = evolve_model(rt, OracleTrainer("R1"))
    reg = Registry(rt.root)
    (record,) = records(rt.root)
    assert card["gate"] == "accepted" and card["deployed"] is False and "down" in card["deploy_error"]
    assert reg.champion() is None and reg.card(card["version"])["deployed"] is False
    assert record.usage["outcome"] == "accepted"                    # the verdict stands: it was used
    assert rt.threshold("stop", "R1", "per_turn") is None             # nothing it was calibrated for acts
    d = rt.ask("stop", ctx(), session="s_1", phase="per_turn")
    assert (d.model, d.acted) == ("base", False)
    # later, with serving up: deploy_version loads it, promotes it over its parent and stores its calibrations
    path = tmp_path / "ouroloop.toml"
    path.write_text('[workspace]\nroot = "ws"\n[registry]\nbase = "base"\n[backends.base]\n'
                    'type = "test_gate_record:Model"\np_yes = 0.5\nmodel = "base"\n[points]\ndefault_backend = "base"\n'
                    '[points.stop]\nbackend = "champion"\nact = true\nexplore = 0.0\n')
    assert deploy_version(config.build_runtime(config.load(path), tmp_path), card["version"], reg)
    assert reg.champion()["version"] == card["version"] and reg.card(card["version"])["deployed"]
    assert "deploy_error" not in reg.card(card["version"])
    fresh = config.build_runtime(CFG, tmp_path)
    assert fresh.threshold("stop", "R1", "per_turn") == card["calibration"]["stop"]["thresholds"]["per_turn"]
    assert fresh.ask("stop", ctx(), session="s_2", phase="per_turn").acted
    # only a version a gate record accepted, and not revoked
    reg.record({"version": "m_0002", "parent": card["version"], "gate": "rejected", "model_id": "NO",
                "backend": {"type": "test_gate_record:Oracle", "model": "NO"}})
    accept(rt.root, {"version": "m_0003", "parent": card["version"], "model_id": "R3",
                     "backend": {"type": "test_gate_record:Oracle", "model": "R3"}})
    revoke(rt.root, "m_0003", "R3", "a test")
    for version, why in (("m_0002", "no gate record accepted it"), ("m_0003", "is revoked")):
        with pytest.raises(ValueError, match=why):
            deploy_version(fresh, version)
    assert reg.champion()["version"] == card["version"]


def test_a_failed_deploy_leaves_the_champion_as_it_was(tmp_path):
    spy = Spy()
    rt = workspace(tmp_path, spy)
    first = evolve_model(rt, OracleTrainer("R1", knows=SOME))
    reg = Registry(rt.root)
    assert reg.champion()["version"] == first["version"]
    pointer = (rt.root / "models" / "champion.json").read_text()
    collect(rt, 40, start=150)
    spy.down = True
    card = evolve_model(rt, OracleTrainer("R2"))
    assert card["gate"] == "accepted" and card["parent"] == first["version"] and card["deployed"] is False
    assert reg.champion()["version"] == first["version"]
    assert (rt.root / "models" / "champion.json").read_text() == pointer
    assert rt.threshold("stop", "R1", "per_turn") is not None and rt.threshold("stop", "R2", "per_turn") is None
    assert rt.ask("stop", ctx(), session="s_new", phase="per_turn").model == "R1"     # new sessions: the old one
    assert not any(r.startswith(f"target reached: {card['version']}") for r in decide(rt)[2])   # not deployed
    spy.down = False
    assert deploy_version(rt, card["version"]) and reg.champion()["version"] == card["version"]
    history = json.loads((rt.root / "models" / "champion.json").read_text())["history"]
    assert history[-1]["parent"] == first["version"] and len(history) == 2


def test_ouroloop_deploy_of_a_superseded_version_loads_nothing_and_a_failed_deploy_keeps_a_past_one(tmp_path):
    reg = chain(tmp_path)
    spy = Spy(down=True)
    rt = Runtime(tmp_path, {"base": Says(model="base")}, "base", serving=spy)
    with pytest.raises(PromotionConflict, match="the champion is now m_0004"):
        deploy_version(rt, "m_0001")
    assert spy.deploys == [] and reg.card("m_0001")["deployed"] == "2026-09-26T00:00:00"
    card = reg.card("m_0001")
    assert not deploy(rt, reg, card)                                # serving down: an error, not "never deployed"
    assert reg.card("m_0001")["deployed"] == "2026-09-26T00:00:00" and "down" in reg.card("m_0001")["deploy_error"]
    assert reg.last_good("m_0004") == "m_0001"


# ---------------------------------------------------------------- rollback
def chain(root) -> Registry:
    """m_0001 (deployed) <- m_0002 (revoked) <- m_0003 (accepted, never deployed) <- m_0004, the champion; each
    accepted by a gate record, models A1..A4."""
    reg = Registry(root)
    for i, deployed in ((1, "2026-09-26T00:00:00"), (2, "2026-09-26T00:00:01"), (3, False), (4, None)):
        parent = f"m_{i - 1:04d}" if i > 1 else None
        accept(root, {"version": f"m_{i:04d}", "parent": parent, "points": ["check"], "model_id": f"A{i}",
                      "backend": {"type": "test_counting:Says", "model": f"A{i}"},
                      **({"deployed": deployed} if deployed is not None else {})})
        reg.promote(f"m_{i:04d}", parent)
    revoke(root, "m_0002", "A2", "a test")
    return reg


def test_a_rollback_goes_to_the_last_good_ancestor_down_to_the_defaults(tmp_path):
    reg = chain(tmp_path)
    assert reg.rollback() == "m_0001" and reg.champion()["version"] == "m_0001"
    assert Revoked(tmp_path / "harness" / "revoked.json").hit("m_0004") == "m_0004"
    (event,) = [e for e in Ledger(tmp_path / "ledger").events() if e["type"] == "registry"]
    assert (event["event"], event["retired"], event["champion"]) == ("rollback", "m_0004", "m_0001")
    assert reg.rollback() is None and reg.champion() is None           # m_0001 had no good ancestor: the defaults
    assert reg.rollback() is None


def test_the_monitor_rolls_a_revoked_champion_back_and_new_sessions_pin_the_rolled_back_one(tmp_path, gated, frozen):
    reg = chain(tmp_path)
    spy = Spy()
    rt = Runtime(tmp_path, {"base": Says(model="base")}, "base", seed=0, serving=spy)
    rt.register(Noul("check", "ok?", phases={"here": "no"}, policy=Policy(act=True, explore=0.0)))
    gated(rt, "check", "A4", 0.5)
    frozen(tmp_path)
    rt.session("early")                                             # pinned to m_0004 before the revocation
    assert [session(rt, f"s{i}") for i in range(3)] == [5, 5, 0]
    assert reg.champion()["version"] == "m_0001" and spy.drops == [reg.card("m_0004")["backend"]]
    pins = [json.loads(rt.sessions.path(s, ".release.json").read_text())["champion"] for s in ("early", "s2")]
    assert pins == ["m_0004", "m_0001"] and rt.session("s3").model == "A1"
    d = rt.ask("check", {"s": "early"}, session="early")           # the revoked one's session: its defaults
    assert (d.model, d.acted, d.action) == ("A4", False, "no")
    assert [e["type"] for e in rt.ledger.events() if e["type"] in ("monitor", "registry")] == ["monitor", "registry"]


@pytest.mark.parametrize("frozen_gate", [True, False])
def test_a_revoked_champion_is_a_rollback_not_finished_and_the_next_session_finishes_it(tmp_path, frozen,
                                                                                         frozen_gate):
    reg = chain(tmp_path)
    if frozen_gate:
        frozen(tmp_path)
    spy = Spy()
    rt = Runtime(tmp_path, {"base": Says(model="base")}, "base", seed=0, serving=spy)
    rt.register(Noul("check", "ok?", phases={"here": "no"}, policy=Policy(act=True, explore=0.0)))
    revoke(tmp_path, "m_0004", "A4", "a monitor that died before its rollback")
    assert rt.session("s1").model == "A1" and reg.champion()["version"] == "m_0001"
    assert spy.drops == [reg.card("m_0004")["backend"]]
    (event,) = [e for e in rt.ledger.events() if e["type"] == "registry"]
    assert (event["retired"], event["champion"]) == ("m_0004", "m_0001")
    rt.session("s2")                                                 # finished once: nothing more to do
    assert len([e for e in rt.ledger.events() if e["type"] == "registry"]) == 1 and len(spy.drops) == 1
