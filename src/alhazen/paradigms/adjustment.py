"""Method of adjustment: the subject sets the stimulus, not the scheduler.

There is nothing to titrate here — the measurement is wherever the subject
stops turning the knob — so the scheduler's whole job is to serve the same
condition the planned number of times and re-queue any trial that ended
without a setting. The work is in the phase (task/phases/adjustment.py).
"""

from __future__ import annotations

from collections import deque

import numpy as np
import pandas as pd

from alhazen.core.engine import TrialResult
from alhazen.paradigms.base import Condition


class AdjustmentTrials:
    """``n_trials`` presentations of each condition, shuffled, re-queueing any
    trial that produced no setting."""

    def __init__(
        self,
        n_trials: int,
        conditions: list[Condition] | None = None,
        rng: np.random.Generator | None = None,
        shuffle: bool = True,
    ) -> None:
        if n_trials < 1:
            raise ValueError(f"n_trials must be >= 1, got {n_trials}")
        cells = list(conditions) if conditions else [Condition({})]
        planned = [cell for cell in cells for _ in range(n_trials)]
        # Only a plan with more than one distinct condition has an order worth
        # shuffling; a single condition repeated needs no rng to be handed in.
        if shuffle and len(cells) > 1:
            if rng is None:
                raise ValueError("shuffle=True requires the injected scheduler rng")
            planned = [planned[i] for i in rng.permutation(len(planned))]
        self._queue: deque[Condition] = deque(planned)
        self._completed = 0

    def next(self) -> Condition | None:
        if not self._queue:
            return None
        return self._queue.popleft()

    def record(self, condition: Condition, result: TrialResult) -> None:
        if result.outcome.completed:
            self._completed += 1
            return
        self._queue.append(condition)

    def summary(self) -> pd.DataFrame:
        return pd.DataFrame([{"n_completed": self._completed, "n_remaining": len(self._queue)}])
