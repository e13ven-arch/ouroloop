"""The world, with the three methods the adapter uses: model_fields, model_copy(deep=True), model_dump."""
from __future__ import annotations

import copy


class Model:
    def model_copy(self, deep: bool = False) -> "Model":
        return copy.deepcopy(self) if deep else copy.copy(self)

    def model_dump(self, mode: str = "python") -> dict:
        return {k: (v.model_dump(mode) if isinstance(v, Model) else copy.deepcopy(v))
                for k, v in vars(self).items()}


class WorldMeta(Model):
    def __init__(self, schema_version: str = "0.1.0", allowed_services=None, **_extra):
        self.schema_version = schema_version
        self.allowed_services = allowed_services


class Service(Model):
    def __init__(self, **records):
        for key, value in records.items():
            setattr(self, key, value)

    def rows(self, name: str) -> list:
        if not hasattr(self, name):
            setattr(self, name, [])
        return getattr(self, name)


class WorldState(Model):
    model_fields = {"meta": None, "slack": None, "hubspot": None}

    def __init__(self, **state):
        unknown = sorted(set(state) - set(self.model_fields))
        if unknown:
            raise ValueError(f"unknown services {unknown}")
        self.meta = WorldMeta(**state.get("meta", {}))
        for name in self.model_fields:
            if name != "meta":
                setattr(self, name, Service(**state.get(name, {})))
