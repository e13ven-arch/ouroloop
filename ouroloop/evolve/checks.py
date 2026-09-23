"""Data checks before training (PLAN §4.2). Each one comes from a defect that silently wasted a TDE run:
truncated states (Exp 005 multi-hop), answer-position leakage (HotpotQA), split leakage, benchmark overlap,
and test sets too small to detect a real difference."""
from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class CheckReport:
    blocking: list[str] = field(default_factory=list)      # defects: do not train
    insufficient: list[str] = field(default_factory=list)  # not enough data yet: keep collecting
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.blocking and not self.insufficient


def ngrams(text: str, n: int = 13) -> set[str]:
    tokens = text.split()
    return {" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def _key(e: dict) -> str:
    return hashlib.sha1(f"{e['state']}\x00{e['question']}".encode()).hexdigest()


def min_detectable_difference(n: int, discordance: float = 0.15) -> float:
    """Approximate paired difference detectable at alpha 0.05 with power 0.8."""
    return (1.96 + 0.84) * math.sqrt(discordance / n) if n else float("inf")


def check(ds, *, eval_states: list[str] = (), max_truncated: float = 0.05, min_train: int = 1000,
          max_mde: float = 0.03, discordance: float = 0.15, position_warn: float = 0.10,
          position_block: float = 0.25, minority_warn: float = 0.05) -> CheckReport:
    r = CheckReport()
    all_examples = [e for part in ds.splits.values() for e in part]
    by_point: dict[str, list[dict]] = {}
    for e in all_examples:
        by_point.setdefault(e["dataset"], []).append(e)

    for point, examples in by_point.items():
        truncated = sum(bool(e["meta"].get("truncated")) for e in examples) / len(examples)
        r.stats[f"{point}.truncated"] = round(truncated, 4)
        if truncated > max_truncated:
            r.blocking.append(f"{point}: {truncated:.0%} of states are truncated (limit {max_truncated:.0%})")
        gold = [max(range(len(e["target"])), key=e["target"].__getitem__) for e in examples]
        labels = Counter(e["candidates"][g]["name"] for e, g in zip(examples, gold))
        minority = min(labels.values()) / len(examples) if len(labels) > 1 else 0.0
        if minority < minority_warn:
            r.warnings.append(f"{point}: minority label share {minority:.1%}")
        if examples[0]["primitive"] == "choice":
            k = min(len(e["candidates"]) for e in examples)
            share = max(Counter(g for g in gold if g < k).values()) / len(examples)
            skew = share - 1.0 / k
            r.stats[f"{point}.position_skew"] = round(skew, 4)
            if skew > position_block:
                r.blocking.append(f"{point}: one candidate position holds {share:.0%} of answers (leakage?)")
            elif skew > position_warn:
                r.warnings.append(f"{point}: candidate position skew {skew:+.0%}")

    # Real logs repeat states across sessions (the same error recurs), which is the deployed distribution, not
    # leakage; report the share. Synthetic or rewritten data should be split so that this stays at zero.
    train_keys = {_key(e) for e in ds.splits["train"]}
    repeated = sum(_key(e) in train_keys for e in ds.splits["test"])
    r.stats["test_seen_in_train"] = round(repeated / len(ds.splits["test"]), 4) if ds.splits["test"] else 0.0
    if repeated:
        r.warnings.append(f"{repeated} of {len(ds.splits['test'])} test states also occur in training")

    if eval_states:
        grams = set().union(*(ngrams(s) for s in eval_states))
        hits = sum(bool(ngrams(e["state"]) & grams) for e in ds.splits["train"])
        if hits:
            r.blocking.append(f"{hits} training states share a 13-gram with the evaluation sets")

    n_train, n_test = len(ds.splits["train"]), len(ds.splits["test"])
    mde = min_detectable_difference(n_test, discordance)
    r.stats.update({"train": n_train, "calibration": len(ds.splits["calibration"]), "test": n_test,
                    "test_mde": round(mde, 4)})
    if n_train < min_train:
        r.insufficient.append(f"{n_train} training examples; need {min_train}")
    if mde > max_mde:
        r.insufficient.append(f"a {n_test}-item test set detects only {mde:.1%} differences; need {max_mde:.0%}")
    return r
