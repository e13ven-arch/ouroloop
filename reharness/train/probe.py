"""The completion probe: an environment label for every stop decision of an episode — was the task complete at that
moment? Moved here from the AutomationBench adapter (stage 1): it writes training labels, so it is the training stack's,
and the adapter imports nothing of the framework. It reads the episode through `snapshot()` and `score(world)` only.
"""
from __future__ import annotations


class ProbeError(RuntimeError):
    """The surface cannot tell one state from another."""


class CompletionProbe:
    """An environment label for every stop decision of an episode: was the task actually complete at that moment?

    This is the first of the two feedback paths in docs/DESIGN.md §4.2 -- a locally verifiable outcome. At each
    stop decision it keeps a snapshot of the world; after the episode it scores each snapshot with the official
    rubric and labels the decisions that were made on that state. What it trains is the completion judgement, not
    a claim that continuing is always worth it.

    It is not a checker. The scoring happens after the run, so neither StopGuard nor the view nor the agent can
    see it, and an official scorer is never consulted while the agent is working (§5.2). Nor can it fail a run: a
    state it cannot copy or score stays unlabelled.

    Why this point and this label: a Marketing task's `task_completed_correctly` is a conjunction over a median of
    twelve scored assertions, so a task is lost by leaving one requirement undone. Whether the work is finished is
    exactly the judgement that decides that, it is visible from the state at the time, and on a training task it
    can be checked. Whether a model learns it is a separate question, which the task results answer.
    """

    def __init__(self, rt, episode, max_snapshots: int = 60, surface=None):
        # `surface` is what the agent actually calls, which may wrap the episode. The probe snapshots on the
        # agent's change count, and that count comes from the surface's `mutates`: a surface that cannot answer
        # would leave the count at zero and every label would describe the state at the first stop decision. That
        # is a silent wrong label on every training example, so it is refused here rather than discovered later.
        if surface is not None and getattr(surface, "mutates", None) is None:
            raise ProbeError(f"{type(surface).__name__} does not report which calls change the world, so the "
                                f"probe cannot tell one state from another")
        self.rt = rt
        self.episode = episode
        self.max_snapshots = max_snapshots        # a bound on memory, not a judgement: ~0.1 MB per world
        self.snaps: list[dict] = []               # {"changes", "world", "decisions"}
        self.dropped = 0

    def snap(self, decision: str, changes: int) -> None:
        """At a stop decision. `changes` is the agent's count of calls that may have changed the world: an
        unchanged state is not copied again, and the decisions made on it share one snapshot."""
        if not self.snaps or self.snaps[-1]["changes"] != changes:
            world = None
            if len(self.snaps) < self.max_snapshots:
                try:
                    world = self.episode.snapshot()
                except Exception:  # noqa: BLE001 - a state that cannot be copied stays unlabelled
                    world = None
            else:
                self.dropped += 1
            self.snaps.append({"changes": changes, "world": world, "decisions": []})
        self.snaps[-1]["decisions"].append(decision)

    def label(self) -> int:
        """After the episode: label every stop decision from its own snapshot. Returns how many were labelled.

        A snapshot the scorer cannot read is recorded as a label nobody could give (label None), not skipped and
        certainly not guessed: a feedback builder then reports `unknown`, and a "no" that came from a broken
        scorer would be a wrong label on a real decision (docs/modules/02_EVIDENCE_TRAINING.md §2).
        """
        labelled = 0
        for s in self.snaps:
            if s["world"] is None:
                for d in s["decisions"]:
                    self.rt.outcome(d, None, source="env", note="completion probe",
                                    error="the state at this decision could not be copied")
                continue
            try:
                score = self.episode.score(s["world"])
            except Exception as e:  # noqa: BLE001 - the scorer failed: unknown, never a guessed label
                for d in s["decisions"]:
                    self.rt.outcome(d, None, source="env", note="completion probe",
                                    error=f"{type(e).__name__}: {e}"[:300])
                continue
            for d in s["decisions"]:
                self.rt.outcome(d, "yes" if score.success else "no", source="env", note="completion probe",
                                score=round(score.partial_credit, 6), assertions=f"{score.passed}/{score.scored}")
                labelled += 1
        self.snaps = [{**s, "world": None} for s in self.snaps]      # the worlds are not needed again
        return labelled
