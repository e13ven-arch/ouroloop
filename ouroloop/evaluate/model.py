"""The `model` component's binding point (docs/design/EVALUATE.md §8; DESIGN §7).

A candidate binds a trained artifact by writing its ref into the manifest's `model.artifact_ref`. The training chain
(ouroloop/train, V2) stores a `train_artifact` object: where the weights are, the parent model, the recipe, the data
manifest and the chain's receipts. `check_binding` is what V0 asks of such a candidate before it climbs the ladder
from V3: the base is K's actor, the artifact's parent is that base, and the artifact's data came from train only.
Serving the artifact (an `openai_compat` provider over vLLM, V2 §3.5) is the training chain's; every call still goes
through the gateway.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..contracts import ContractError, Objects, Record, parse_ref, register

TRAIN_ONLY = ("train",)


@register
@dataclass(frozen=True)
class ModelIdentity(Record):
    """Which model an arm runs and how far its identity can be pinned: a hosted alias is not frozen weights
    (`mutable`), and `observed_at` is when it was seen, not part of the identity."""
    TYPE = "model_identity"
    provider: str = ""
    base_model: str = ""
    effort: str | None = None
    max_tokens: int | None = None
    artifact_ref: str | None = None
    mutable: bool = True
    observed_at: str | None = None

    def __post_init__(self) -> None:
        if not self.provider or not self.base_model:
            raise ContractError("a model identity names its provider and base model")
        if self.artifact_ref is not None:
            parse_ref(self.artifact_ref)


def identity_of(core: dict) -> ModelIdentity:
    model = core.get("model") or {}
    return ModelIdentity(provider=str(model.get("provider") or "claude_code"),
                         base_model=str(model.get("identity") or ""), effort=model.get("effort"),
                         max_tokens=model.get("max_tokens"))


def check_binding(manifest: dict, core: dict, objects: Objects) -> list[str]:
    """Why a manifest's model binding may not run under K, or []: the base is K's actor; with an artifact, the artifact
    is a stored train_artifact whose parent is that base and whose data manifest names train lineage only."""
    model = manifest.get("model") or {}
    problems = []
    base = (core.get("model") or {}).get("identity")
    if model.get("base") != base:
        problems.append(f"model.base {model.get('base')!r} is not K's actor {base!r}")
    ref = model.get("artifact_ref")
    if ref is None:
        return problems
    try:
        kind, _ = parse_ref(ref)
    except ContractError as e:
        return [*problems, str(e)]
    if kind != "train_artifact":
        return [*problems, f"model.artifact_ref is a {kind}, not a train_artifact"]
    if not objects.has(ref):
        return [*problems, f"model.artifact_ref {ref} is not in this activity's object store"]
    artifact = objects.get(ref)
    if artifact.get("parent_model") != base:
        problems.append(f"the artifact was trained from {artifact.get('parent_model')!r}, not K's actor {base!r}")
    lineage = artifact.get("splits") or (artifact.get("data") or {}).get("splits")
    if not lineage or set(lineage) - set(TRAIN_ONLY):
        problems.append(f"the artifact's data comes from splits {sorted(lineage or [])}: a model component is trained "
                        f"on train evidence only")
    return problems
