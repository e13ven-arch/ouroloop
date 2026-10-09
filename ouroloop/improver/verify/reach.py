"""V2c, reach (docs/design/IMPROVER.md §8): a skill is listed in every attempt's system prompt by its name and
description, so the check is that the description speaks to more than the tasks it was written for: it must be as
close as `reach_min_score` to at least `reach_min_tasks` train tasks beyond its claimed ones. Vectors: the memory's
bge-small when the activity runs the memory sidecar, else a deterministic bag of words (`lexical_embed`)."""
from __future__ import annotations

import hashlib
import json
import math
import re

DIMS = 512
WORD = re.compile(r"[a-z0-9#@._-]+")
STOP = frozenset("a an the to of and or in on for with is are be it this that when then from by as at".split())


def lexical_embed(texts, query: bool = False) -> list[list[float]]:
    out = []
    for text in texts:
        v = [0.0] * DIMS
        for w in WORD.findall(str(text or "").lower()):
            w = w.strip("._-")
            if w and w not in STOP:
                v[int(hashlib.sha256(w.encode()).hexdigest()[:8], 16) % DIMS] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def _cos(u, v) -> float:
    return float(math.fsum(a * b for a, b in zip(u, v)))


def reach_check(files: dict[str, str], decl, ctx) -> dict:
    manifest = json.loads(files["manifest.json"])
    skill = next((s for s in manifest.get("skills") or () if s.get("name") == decl.site), None)
    if skill is None:
        return {"passed": False, "detail": f"no skill named {decl.site!r} in the candidate's manifest"}
    text = f"{str(skill['name']).replace('-', ' ').replace('_', ' ')}: {skill.get('description') or ''}"
    tasks = [t for t in ctx.train_tasks if t in ctx.train_texts]
    vectors = (ctx.embed or lexical_embed)([text] + [ctx.train_texts[t] for t in tasks])
    scores = {t: round(_cos(vectors[0], v), 4) for t, v in zip(tasks, vectors[1:])}
    reached = sorted(t for t, s in scores.items() if s >= ctx.settings.reach_min_score
                     and t not in set(decl.predicted_affected))
    need = int(ctx.settings.reach_min_tasks)
    return {"passed": len(reached) >= need, "reached": reached,
            "detail": f"the description reaches {len(reached)} train tasks beyond the claimed ones at "
                      f"{ctx.settings.reach_min_score} (at least {need})"}
