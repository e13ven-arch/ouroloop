import json

import pytest

from ouroloop.trainers.tde import yaml_json


def test_the_trainer_config_reads_back_as_the_same_types_in_json_and_yaml():
    cfg = {"lr_backbone": 2e-05, "lr_head": 1e-4, "eps": 1e-6, "epochs": 1.0, "batch_size": 16, "big": 1e20,
           "init_from": None, "tags": [1e-5, 2, "2e-5"], "empty": {}, "path": "runs/2e-5"}
    text = yaml_json(cfg)
    assert json.loads(text) == cfg and '"lr_backbone": 2.0e-05' in text
    yaml = pytest.importorskip("yaml")        # PyYAML reads a mantissa without a dot, json's 2e-05, as a string
    back = yaml.safe_load(text)
    assert back == cfg and all(isinstance(back[k], float) for k in ("lr_backbone", "lr_head", "eps", "big"))
