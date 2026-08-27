"""BlockPlan: runs and blocks on top of any scheduler.

A block is a stretch of trials the subject experiences as one push — the unit
breaks are given between, and the unit analysis groups by. This wraps any
other scheduler, counts *completed* trials to find the boundaries, and stamps
each trial's block number into the record.

Recycling stays where the invariant puts it: with the inner scheduler, which
appends a non-completed condition to the end of its own queue. Because a block
ends on a completed-trial count rather than on a served-trial count, that
append lands the retry back inside the same block — end-of-block recycling
falls out of the composition instead of being a second, competing queue that
could double-serve the same condition.

Blocks are record-only. There is deliberately no BLOCK_START event: a block
boundary is analysis structure, not a physical instant anything needs to be
aligned to, and inventing an event for it would put a pulse on a sync line
that marks nothing that happened on screen.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from alhazen.core.engine import TrialResult
from alhazen.paradigms.base import Condition, TrialSource


class BlockPlan:
    """``n_blocks`` blocks over one inner scheduler, or one per block.

    Passing a list of sources runs a different scheduler per block (a
    practice block, then the real thing); passing one source runs it across
    every block. ``trials_per_block`` bounds a block by completed trials;
    without it, a block ends when its source is exhausted.
    """

    def __init__(
        self,
        inner: TrialSource | list[TrialSource],
        n_blocks: int | None = None,
        trials_per_block: int | None = None,
        rng: np.random.Generator | None = None,
        shuffle_blocks: bool = False,
        block_key: str = "block",
    ) -> None:
        sources = list(inner) if isinstance(inner, list) else None
        if sources is not None:
            if not sources:
                raise ValueError("BlockPlan needs at least one inner scheduler")
            if n_blocks is not None and n_blocks != len(sources):
                raise ValueError(
                    f"n_blocks={n_blocks} contradicts the {len(sources)} sources given; "
                    f"omit n_blocks when you pass one source per block"
                )
            if shuffle_blocks:
                if rng is None:
                    raise ValueError("shuffle_blocks=True requires the injected scheduler rng")
                sources = [sources[i] for i in rng.permutation(len(sources))]
            self._sources = sources
        else:
            if n_blocks is None or n_blocks < 1:
                raise ValueError("a single inner scheduler needs n_blocks >= 1")
            if n_blocks > 1 and trials_per_block is None:
                # One queue shared across blocks, with nothing to say where a
                # block ends, means the first block drains it and every later
                # block is empty — a session that silently collects a
                # fraction of what was planned. Either bound the blocks by a
                # completed-trial count, or give each block its own source.
                raise ValueError(
                    f"BlockPlan over one scheduler with n_blocks={n_blocks} needs "
                    f"trials_per_block: without it the first block serves everything the "
                    f"scheduler has and the rest are empty. Pass trials_per_block, or pass "
                    f"one scheduler per block."
                )
            # The same object across every block: an adaptive scheduler must
            # keep its state, and a queue-based one must keep its queue.
            self._sources = [inner] * n_blocks  # type: ignore[list-item]

        if trials_per_block is not None and trials_per_block < 1:
            raise ValueError(f"trials_per_block must be >= 1, got {trials_per_block}")
        self._trials_per_block = trials_per_block
        self._block_key = block_key
        self._block = 0
        self._completed_in_block = 0
        self._completed_per_block: list[int] = []
        # The condition this wrapper handed out, and the inner one it wraps,
        # so record() can give the inner scheduler back its own object. The
        # runner serves strictly one trial at a time, so one pair is enough.
        self._served: tuple[Condition, Condition] | None = None

    def next(self) -> Condition | None:
        while self._block < len(self._sources):
            if (
                self._trials_per_block is not None
                and self._completed_in_block >= self._trials_per_block
            ):
                self._end_block()
                continue
            inner_condition = self._sources[self._block].next()
            if inner_condition is None:
                # The source ran dry before the block's quota (or has no
                # quota): that is the end of this block, not of the session.
                self._end_block()
                continue
            wrapped = Condition({**inner_condition.params, self._block_key: self._block + 1})
            self._served = (wrapped, inner_condition)
            return wrapped
        return None

    def _end_block(self) -> None:
        self._completed_per_block.append(self._completed_in_block)
        self._completed_in_block = 0
        self._block += 1

    def record(self, condition: Condition, result: TrialResult) -> None:
        inner_condition = condition
        if self._served is not None and self._served[0] is condition:
            inner_condition = self._served[1]
        # Every outcome reaches the inner scheduler — it alone decides
        # re-queueing, and an adaptive one needs to hear about the trials it
        # cannot score just as much as the ones it can.
        block = min(self._block, len(self._sources) - 1)
        self._sources[block].record(inner_condition, result)
        if result.outcome.completed:
            self._completed_in_block += 1

    def summary(self) -> pd.DataFrame:
        completed = [*self._completed_per_block, self._completed_in_block]
        rows: list[dict[str, Any]] = [
            {"block": index + 1, "n_completed": count}
            for index, count in enumerate(completed[: len(self._sources)])
        ]
        return pd.DataFrame(rows)
