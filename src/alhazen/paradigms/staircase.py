"""Transformed up-down staircases, run singly or interleaved.

A staircase moves the stimulus toward the subject's threshold rather than
sampling a fixed grid: ``n_down`` consecutive successes make the task harder,
``n_up`` consecutive failures make it easier. The ratio sets which point of
the psychometric function the staircase converges on (2-down-1-up ≈ 71%
correct), and the *reversals* — the trials where direction changes — are what
an analysis averages to estimate the threshold.

Success comes from ``TrialResult.outcome.success`` and nothing else. A
scheduler that reached into the trial record would be reading measurements the
task defines, which is how a scheduler and an analysis end up disagreeing
about what "correct" meant.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from alhazen.core.engine import TrialResult
from alhazen.paradigms.base import Condition


class UpDownStaircase:
    """One transformed up-down staircase over a single condition parameter.

    Stops after ``n_reversals`` reversals or ``n_trials`` completed trials,
    whichever is given (both may be, and whichever comes first ends it).
    """

    def __init__(
        self,
        parameter: str,
        start: float,
        step: float,
        n_up: int = 1,
        n_down: int = 2,
        n_reversals: int | None = None,
        n_trials: int | None = None,
        min_value: float | None = None,
        max_value: float | None = None,
        fixed: dict[str, Any] | None = None,
    ) -> None:
        if step <= 0:
            raise ValueError("step must be > 0 — direction comes from n_up/n_down, not its sign")
        if n_up < 1 or n_down < 1:
            raise ValueError("n_up and n_down must be >= 1")
        if n_reversals is None and n_trials is None:
            # Without a stopping rule the session would never end, and the
            # experimenter would discover that only by watching it not stop.
            raise ValueError("give n_reversals, n_trials, or both — a staircase must stop")
        self._parameter = parameter
        self._value = float(start)
        self._step = float(step)
        self._n_up = n_up
        self._n_down = n_down
        self._n_reversals = n_reversals
        self._n_trials = n_trials
        self._min = min_value
        self._max = max_value
        self._fixed = dict(fixed or {})

        self._successes = 0  # consecutive, reset by a failure
        self._failures = 0  # consecutive, reset by a success
        self._last_direction = 0  # -1 = getting harder, +1 = easier, 0 = not moved yet
        self.reversals: list[float] = []
        self.history: list[tuple[float, bool]] = []  # (value, success) per completed trial

    @property
    def finished(self) -> bool:
        if self._n_reversals is not None and len(self.reversals) >= self._n_reversals:
            return True
        return self._n_trials is not None and len(self.history) >= self._n_trials

    @property
    def value(self) -> float:
        return self._value

    def next(self) -> Condition | None:
        if self.finished:
            return None
        return Condition({**self._fixed, self._parameter: self._value})

    def record(self, condition: Condition, result: TrialResult) -> None:
        if not result.outcome.completed:
            # No measurement exists, so the staircase must not move: stepping
            # on a fixation break would walk the estimate toward wherever the
            # subject happened to stop cooperating.
            return
        success = bool(result.outcome.success)
        self.history.append((self._value, success))
        if success:
            self._successes += 1
            self._failures = 0
        else:
            self._failures += 1
            self._successes = 0

        if success and self._successes >= self._n_down:
            self._step_by(-1)
            self._successes = 0
        elif not success and self._failures >= self._n_up:
            self._step_by(+1)
            self._failures = 0

    def _step_by(self, direction: int) -> None:
        """Move one step; record a reversal when the direction changes."""
        if self._last_direction != 0 and direction != self._last_direction:
            # The reversal is recorded at the value the staircase turned
            # around AT, which is the value an analysis averages.
            self.reversals.append(self._value)
        self._last_direction = direction
        value = self._value + direction * self._step
        if self._min is not None:
            value = max(value, self._min)
        if self._max is not None:
            value = min(value, self._max)
        self._value = value

    def summary_row(self) -> dict[str, Any]:
        # Mean of the last even number of reversals is the conventional
        # estimate: pairing them cancels the up/down asymmetry of the rule.
        usable = self.reversals[-(len(self.reversals) // 2 * 2) :] if self.reversals else []
        return {
            "parameter": self._parameter,
            "final_value": self._value,
            "n_trials": len(self.history),
            "n_reversals": len(self.reversals),
            "reversal_mean": float(np.mean(usable)) if usable else float("nan"),
            "finished": self.finished,
        }

    def summary(self) -> pd.DataFrame:
        return pd.DataFrame([self.summary_row()])


class InterleavedStaircases:
    """Several staircases run at once, served in random order among those not
    yet finished.

    Interleaving rather than running them one after another is what keeps the
    comparison between them fair: a subject who tires or improves over a
    session affects every staircase equally instead of only the later ones.
    Which staircase a trial belongs to travels in the condition itself (and so
    into the trial record), which is how ``record`` routes the result back.
    """

    def __init__(
        self,
        staircases: dict[str, UpDownStaircase],
        rng: np.random.Generator,
        label_key: str = "staircase",
    ) -> None:
        if not staircases:
            raise ValueError("InterleavedStaircases needs at least one staircase")
        self._staircases = dict(staircases)
        self._rng = rng
        self._label_key = label_key

    def next(self) -> Condition | None:
        unfinished = sorted(
            label for label, stair in self._staircases.items() if not stair.finished
        )
        if not unfinished:
            return None
        # Sorted first, then drawn from the injected rng: the choice depends
        # only on the seed and on which staircases are live, never on dict
        # insertion order.
        label = str(self._rng.choice(unfinished))
        condition = self._staircases[label].next()
        if condition is None:  # finished between the check and the draw
            return None
        return Condition({**condition.params, self._label_key: label})

    def record(self, condition: Condition, result: TrialResult) -> None:
        label = condition.params.get(self._label_key)
        staircase = self._staircases.get(str(label))
        if staircase is None:
            # A condition from somewhere else cannot be scored here; guessing
            # a staircase would corrupt one of them silently.
            raise ValueError(
                f"record() got a condition labelled {label!r}, which is none of "
                f"{sorted(self._staircases)}"
            )
        staircase.record(condition, result)

    def summary(self) -> pd.DataFrame:
        rows = []
        for label, staircase in sorted(self._staircases.items()):
            rows.append({self._label_key: label, **staircase.summary_row()})
        return pd.DataFrame(rows)
