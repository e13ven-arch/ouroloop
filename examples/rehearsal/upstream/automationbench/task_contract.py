"""The fingerprint of the task inputs a rollout used."""
from __future__ import annotations

import hashlib
import json


def task_contract_payload(*, example_id, prompt: list, info: dict) -> dict:
    if not isinstance(info.get("assertions"), list):
        raise TypeError("task info.assertions must be a list")
    return {"schema": "automationbench.task-contract.v1", "example_id": example_id,
            "task_name": info.get("task_name"),
            "prompt": [{"role": m.get("role"), "content": m.get("content")} for m in prompt],
            "assertions_multiset": sorted(json.dumps(a, sort_keys=True) for a in info["assertions"]),
            "initial_state": info.get("initial_state", {}),
            "zapier_tools": sorted(info.get("zapier_tools", []))}


def task_contract_sha256(*, example_id, prompt: list, info: dict) -> str:
    payload = task_contract_payload(example_id=example_id, prompt=prompt, info=info)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
