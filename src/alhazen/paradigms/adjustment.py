"""Method of adjustment: the subject sets the stimulus, not the scheduler.

There is nothing to titrate here — the measurement is wherever the subject
stops turning the knob — so the scheduler's whole job is to serve the same
condition the planned number of times and re-queue any trial that ended
without a setting. The work is in the phase (task/phases/adjustment.py).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from alhazen.core.engine import TrialResult
from alhazen.paradigms.base import Condition, SimpleSequence


class AdjustmentTrials(SimpleSequence):
    """``n_trials`` presentations of each condition, shuffled, re-queueing any
    trial that produced no setting.

    A SimpleSequence in everything but its defaults — no conditions means one
    nameless condition, and a single condition is never shuffled — and its
    summary, which counts the settings collected. The queue and its re-serve
    rule are SimpleSequence's own.
    """

    def __init__(
        self,
        n_trials: int,
        conditions: list[Condition] | None = None,
        rng: np.random.Generator | None = None,
        shuffle: bool = True,
    ) -> None:
        # Checked here, before SimpleSequence's own check, so the message
        # names this class's parameter rather than n_repeats.
        if n_trials < 1:
            raise ValueError(f"n_trials must be >= 1, got {n_trials}")
        cells = list(conditions) if conditions else [Condition({})]
        # Only a plan with more than one distinct condition has an order worth
        # shuffling; a single condition repeated needs no rng to be handed in,
        # and takes no draw from one that is (so the Generator it shares with
        # other blocks' schedulers is left exactly where it was).
        super().__init__(cells, n_repeats=n_trials, rng=rng, shuffle=shuffle and len(cells) > 1)
        self._completed = 0

    def record(self, condition: Condition, result: TrialResult) -> None:
        if result.outcome.completed:
            self._completed += 1
        # Whether the condition goes back on the queue is the shared queue's
        # decision, made the same way for every queue-based scheduler.
        super().record(condition, result)

    def summary(self) -> pd.DataFrame:
        return pd.DataFrame([{"n_completed": self._completed, "n_remaining": self.remaining()}])
