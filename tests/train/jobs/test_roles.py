"""The roles of the training stack (docs/DESIGN.md, roles and layouts): a model store behind one interface ("dir",
"hf"), serving as the one way a trained model is asked and let go of, and the trainer's place beside them. Old configs
(transport, [models]) still load. Nothing trains, nothing leaves this machine. (The deploy between a gate's verdict and
the champion pointer, and the champion's rollback, went with the champion pointer in P1 step 5.)"""
import importlib.util
import json
import sys

import pytest

from jobs_fixtures import FAKE_HF, keys  # noqa: F401 - keys is a fixture
from ouroloop.train import config as train_config
from ouroloop.train.jobs.trainer import JobTrainer
from ouroloop.train.train import TrainError
from ouroloop.release import code_version
from ouroloop.train.serving import PROBE, Serving
from ouroloop.train.stores import make
from ouroloop.train.trainers import signature

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
    assert train_config.store_table(old) == {"type": "dir", "root": "/srv/jobs"}
    assert train_config.store_table({"trainer": {"type": "signature", "jobs_root": "~/jobs"}}) == {"type": "dir",
                                                                                            "root": "~/jobs"}
    hf = {"trainer": {"type": "signature", "transport": "hf", "hf_prefix": "org/o-"}, "models": {"url": "http://g:1"}}
    assert train_config.store_table(hf) == {"type": "hf", "prefix": "org/o-"}
    assert train_config.serving_table(hf) == {"url": "http://g:1"} and train_config.serving_table(old) == {}
    trainer = train_config.build_trainer(hf)
    assert trainer.store.table == {"type": "hf", "prefix": "org/o-"} and trainer.serving.url == "http://g:1"
    serving = train_config.build_serving(old)
    assert serving.store.table == {"type": "dir", "root": "/srv/jobs"} and not serving.url
    for bad, why in (({**old, "serving": {}}, "keep one"),
                     ({"trainer": {"type": "x", "transport": "s3"}}, "transport"),
                     ({"store": {"type": "dir"}, "models": {"jobs_root": "/a", "hf_prefix": "org/o-"}},
                      r"\[store\] and \[models\] hf_prefix, \[models\] jobs_root: .* keep one")):
        with pytest.raises(ValueError, match=why):
            train_config.build_serving(bad)
    with pytest.raises(ValueError, match=r"\[store\] and \[trainer\] transport, \[trainer\] hf_prefix"):
        train_config.build_trainer({"trainer": hf["trainer"], "store": {"type": "dir"}, "serving": {"url": "http://x:1"}})
    # an old model server's config: [models] jobs_root and hf_prefix, no transport, loaded refs from the Hub
    server = {"models": {"jobs_root": "~/.ouroloop/jobs", "hf_prefix": "org/o-"}}
    assert train_config.store_table(server) == {"type": "hf", "prefix": "org/o-"}
    assert train_config.build_serving(server).store.job(f"org/o-m_0001_x@{'a' * 40}") == "m_0001_x"


# ---------------------------------------------------------------- serving
def test_serving_deploys_a_ref_by_loading_it_and_drops_it(tmp_path):
    jobs = tmp_path / "jobs"
    signature_job(jobs, "m_0001_x")
    serving = train_config.build_serving({"store": {"type": "dir", "root": str(jobs)}})
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
    from ouroloop.train import stores
    from ouroloop.train.jobs import cloud as clouds
    from ouroloop.train.jobs.hosts import manifest, plan, stale
    from jobs_fixtures import Ssh, VastApi, a_job, options
    (tmp_path / "more").mkdir()
    (tmp_path / "more" / "objects.py").write_text(OBJECTS)
    monkeypatch.setattr(stores, "__path__", [*stores.__path__, str(tmp_path / "more")])
    monkeypatch.delitem(sys.modules, "ouroloop.train.stores.objects", raising=False)
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
