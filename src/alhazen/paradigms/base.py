"""The paradigm scheduler contract.

A paradigm decides which condition to present next and when the session is
done. The one rule every scheduler must honor: a *non-completed* trial
(fixation break, no response, pause, abort — anything whose Outcome has
``completed=False``) did not produce its measurement, so its condition must
be re-served, never dropped and never scored. Skipping this rule is how a
scheduler silently under-samples its conditions and biases an adaptive fit.

`SimpleSequence` below is the honest minimum that enforces the rule; the
rest of the library (constant stimuli, staircases, QUEST+, adjustment)
sits beside it in this package.
"""

from __future__ import annotations

from typing import Any, Protocol

import numpy as np

from alhazen.core.engine import TrialResult


class Condition:
    """One fully-specified trial condition: the exact parameter values it is
    drawn with. ``params`` must be treated as read-only after construction —
    ``key()`` is the hashable identity schedulers count and re-queue by."""

    def __init__(self, params: dict[str, Any]) -> None:
        self.params = dict(params)

    def key(self) -> tuple:
        return tuple(sorted(self.params.items()))

    def __repr__(self) -> str:
        return f"Condition({self.params!r})"


class TrialSource(Protocol):
    """What the session runner needs from any paradigm."""

    def next(self) -> Condition | None:
        """The next condition to present; None exactly when the session is
        done (the runner's loop is simply "while next() is not None")."""
        ...

    def record(self, condition: Condition, result: TrialResult) -> None:
        """How the served condition's trial actually went. Called for every
        outcome, including PAUSED/ABORTED — deciding whether the condition
        goes back in the queue is the scheduler's job alone."""
        ...

    def summary(self) -> Any | None:
        """Optional end-of-session state (adaptive fits, counts) for the
        recorder; None when there is nothing to summarize."""
        ...


class SimpleSequence:
    """Serve a fixed list of conditions, ``n_repeats`` times each, in
    rng-shuffled order, re-queueing any non-completed attempt at the end of
    the remaining schedule."""

    def __init__(
        self,
        conditions: list[Condition],
        n_repeats: int = 1,
        rng: np.random.Generator | None = None,
        shuffle: bool = True,
    ) -> None:
        if n_repeats < 1:
            raise ValueError("n_repeats must be >= 1")
        self._queue: list[Condition] = [c for c in conditions for _ in range(n_repeats)]
        if shuffle:
            if rng is None:
                raise ValueError("shuffle=True requires the injected scheduler rng")
            # Permute indices rather than the list of Conditions in place:
            # identical draws from the same seed (same Fisher-Yates, same
            # stream), but typed for a list of objects rather than for the
            # numeric arrays Generator.shuffle is declared over.
            order = rng.permutation(len(self._queue))
            self._queue = [self._queue[i] for i in order]
        self._served: Condition | None = None

    def next(self) -> Condition | None:
        if not self._queue:
            return None
        self._served = self._queue.pop(0)
        return self._served

    def record(self, condition: Condition, result: TrialResult) -> None:
        if not result.outcome.completed:
            self._queue.append(condition)

    def summary(self) -> None:
        return None
