"""ConstantStimuli: the non-adaptive scheduler.

Build every combination of the condition grids, repeat each ``n_per_condition``
times, shuffle once, serve until done. Nothing adapts — the whole plan exists
at construction time.

The subtlety that matters is the one every scheduler here shares: a trial can
end without producing a measurement (a broken fixation, a missed response), and
such an attempt must not consume one of a condition's planned repetitions, or
the session ends with uneven cell counts across conditions — a bias in the data
that no analysis can undo. ``record`` re-queues those conditions instead.
"""

from __future__ import annotations

import itertools
from collections import Counter
from typing import Any

import numpy as np
import pandas as pd

from alhazen.core.engine import TrialResult
from alhazen.paradigms.base import Condition, SimpleSequence


class ConstantStimuli:
    """Full factorial × ``n_per_condition``, shuffled once, re-queueing any
    attempt that produced no measurement."""

    def __init__(
        self,
        conditions: dict[str, list[Any]],
        n_per_condition: int = 1,
        rng: np.random.Generator | None = None,
        shuffle: bool = True,
    ) -> None:
        if n_per_condition < 1:
            # An empty plan would make the very first next() return None, and
            # the session would report itself complete through the same code
            # path a finished session uses — having collected nothing, with
            # the subject already in the rig.
            raise ValueError(f"n_per_condition must be >= 1, got {n_per_condition}")
        if not conditions:
            raise ValueError("ConstantStimuli needs at least one condition grid")
        if shuffle and rng is None:
            raise ValueError("shuffle=True requires the injected scheduler rng")

        # Sorted keys, not the dict's own order: a YAML file's incidental key
        # order is not part of its meaning, but itertools.product below is
        # order-sensitive — so the same seed must reproduce the same session
        # regardless of how the file happened to be written.
        keys = sorted(conditions)
        grids = [conditions[key] for key in keys]
        cells = [
            Condition(dict(zip(keys, values, strict=True))) for values in itertools.product(*grids)
        ]
        # Repeat before shuffling, so a condition's repeats are spread through
        # the session rather than sitting in back-to-back blocks. The plan is
        # the whole list of cells, repeated as a unit (c1 c2 c1 c2, not
        # c1 c1 c2 c2): that order is what the permutation below is drawn
        # over, so it is part of what a seed reproduces.
        planned = cells * n_per_condition
        # The queue — shuffle once, serve from the front, re-queue what did
        # not complete at the back — is SimpleSequence's, shared with every
        # queue-based scheduler. Handed the finished plan with n_repeats=1 it
        # takes exactly the one permutation draw this class always took.
        self._queue = SimpleSequence(planned, n_repeats=1, rng=rng, shuffle=shuffle)
        self._completed: Counter = Counter()
        self._attempts: Counter = Counter()
        self._cells = cells

    def next(self) -> Condition | None:
        # None means every condition has had its full count of *completed*
        # presentations, not merely that many attempts.
        return self._queue.next()

    def record(self, condition: Condition, result: TrialResult) -> None:
        self._attempts[condition.key()] += 1
        if result.outcome.completed:
            self._completed[condition.key()] += 1
        # A non-completed attempt goes back on the end of the queue there.
        self._queue.record(condition, result)

    def remaining(self) -> int:
        """Planned trials still to serve, re-queued retries included."""
        return self._queue.remaining()

    def summary(self) -> pd.DataFrame:
        """One row per condition cell: what was planned, attempted, and
        actually measured. This is the table that shows whether the session
        ended balanced."""
        rows = []
        for cell in self._cells:
            key = cell.key()
            rows.append(
                {
                    **cell.params,
                    "n_attempts": self._attempts[key],
                    "n_completed": self._completed[key],
                }
            )
        return pd.DataFrame(rows)
