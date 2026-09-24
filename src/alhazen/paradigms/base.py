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

from collections import deque
from collections.abc import Callable
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

    # A queue-based source may also define ``remaining() -> int``: how many
    # planned trials it still has to serve, counting any re-queued retry.
    # It is optional, so not declared here (a Protocol member would make
    # every existing source fail the structural check): BlockPlan looks it
    # up with getattr and, when it is there, refuses a ``trials_per_block``
    # that would end a block before the plan is done. An adaptive source has
    # no fixed plan and simply does not define it.


class SimpleSequence:
    """Serve a fixed list of conditions, ``n_repeats`` times each, in
    rng-shuffled order, re-queueing any non-completed attempt at the end of
    the remaining schedule.

    This is the one queue every queue-based scheduler uses: AdjustmentTrials
    is a SimpleSequence with its own defaults, and ConstantStimuli builds its
    factorial plan and hands it to one. So the re-serve rule (CONTRIBUTING
    #5) is enforced here, in one place, for all three.
    """

    def __init__(
        self,
        conditions: list[Condition],
        n_repeats: int = 1,
        rng: np.random.Generator | None = None,
        shuffle: bool = True,
    ) -> None:
        if n_repeats < 1:
            raise ValueError("n_repeats must be >= 1")
        # Each condition's repeats sit together before the shuffle; the
        # order of this list is part of what a seed reproduces, so it must
        # not change (tests pin the seeded orders).
        planned = [c for c in conditions for _ in range(n_repeats)]
        if shuffle:
            if rng is None:
                raise ValueError("shuffle=True requires the injected scheduler rng")
            # Permute indices rather than the list of Conditions in place:
            # identical draws from the same seed (same Fisher-Yates, same
            # stream), but typed for a list of objects rather than for the
            # numeric arrays Generator.shuffle is declared over.
            order = rng.permutation(len(planned))
            planned = [planned[i] for i in order]
        # A deque: served from the front and re-queued at the back, both O(1).
        self._queue: deque[Condition] = deque(planned)

    def next(self) -> Condition | None:
        # Empty means every planned trial has COMPLETED, not merely been
        # attempted: a non-completed one went back on the queue in record().
        if not self._queue:
            return None
        return self._queue.popleft()

    def record(self, condition: Condition, result: TrialResult) -> None:
        if result.outcome.completed:
            return
        # Re-queued at the END, never retried immediately: an immediate retry
        # shows the identical condition twice in a row, which a subject can
        # learn to exploit ("fail this one and it comes straight back"), and
        # it clusters a hard condition's failures instead of leaving them
        # spread the way the initial shuffle spread everything else.
        self._queue.append(condition)

    def remaining(self) -> int:
        """Planned trials still to serve, re-queued retries included — the
        optional plan size BlockPlan checks ``trials_per_block`` against."""
        return len(self._queue)

    def summary(self) -> None:
        return None


def _success_from_outcome(result: TrialResult) -> bool:
    # The default scorer of both adaptive schedulers (UpDownStaircase and
    # QuestPlus), and the same rule Task.score_trial defaults to: a scheduler
    # built without a scorer titrates the outcome's own success, exactly as
    # it did before scorers existed.
    return bool(result.outcome.success)


def _verdict(score: Callable[[TrialResult], bool], result: TrialResult) -> bool:
    """Ask ``score`` about a completed trial, and insist on a real boolean.

    An adaptive scheduler steps on this answer, so a scorer that answers
    anything else must stop the session rather than be read as truthy. The
    case that matters: a ``score_trial`` that works out its verdict and
    forgets to ``return`` it hands back None, which ``bool()`` reads as a
    failure on every trial — the staircase walks to its easiest level, QUEST+
    fits an observer who never succeeds, and nothing says so.

    ``bool`` and numpy's bool are accepted (a comparison on a numpy value
    returns the latter). Ints are refused, 0 and 1 included: an int from a
    scorer is most likely a count or a magnitude (correct responses, an
    error in pixels), and ``bool()`` of that is True for every non-zero
    value — the same silent misreading the other way round. ``bool(...)``
    around the comparison the task means is a one-word fix.
    """
    verdict = score(result)
    if isinstance(verdict, bool | np.bool_):
        # bool() so what the scheduler keeps (a staircase's history, say) is
        # a Python bool whichever of the two it was.
        return bool(verdict)
    name = getattr(score, "__qualname__", None) or repr(score)
    raise TypeError(
        f"the scorer {name} returned {verdict!r} ({type(verdict).__name__}) for a completed "
        f"trial; an adaptive scheduler needs True or False to step on. A score_trial that "
        f"returns None has usually lost its `return`. One that returns a number should "
        f"return bool(...) of the comparison it means: a number is refused rather than read "
        f"as truthy, because a count or a magnitude would count as a success whenever it is "
        f"not zero."
    )
