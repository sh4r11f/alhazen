"""Declaring a paradigm in a config file, and building it.

A task's params model carries a ``paradigm: SchedulerConfig`` field, and
``Task.make_source`` turns it into a scheduler. That is what lets the choice
between "every condition twice" and "a QUEST+ staircase per speed" be a config
edit rather than a code edit — which is the difference between an experimenter
changing a design and an experimenter needing a programmer.

Each kind reads its own nested block, so a config that names ``questplus``
without a ``quest`` block fails at load with that said plainly, instead of
building something with silent defaults.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any, Literal

import numpy as np
from pydantic import model_validator

from alhazen.config.models import Model
from alhazen.core.engine import TrialResult
from alhazen.errors import ConfigError
from alhazen.paradigms.adjustment import AdjustmentTrials
from alhazen.paradigms.base import Condition, SimpleSequence, TrialSource
from alhazen.paradigms.blocks import BlockPlan
from alhazen.paradigms.constant import ConstantStimuli
from alhazen.paradigms.questplus import QuestPlus, StimScale
from alhazen.paradigms.staircase import InterleavedStaircases, UpDownStaircase


class StaircaseConfig(Model):
    """A transformed up-down staircase over one condition parameter."""

    parameter: str
    start: float
    step: float
    n_up: int = 1
    n_down: int = 2  # 2-down-1-up converges on ~71% correct
    n_reversals: int | None = None
    n_trials: int | None = None
    min_value: float | None = None
    max_value: float | None = None
    # One independent staircase per level of this condition key, interleaved.
    interleave_by: str | None = None

    @model_validator(mode="after")
    def _valid(self) -> StaircaseConfig:
        if self.step <= 0:
            raise ValueError("step must be > 0")
        if self.n_reversals is None and self.n_trials is None:
            raise ValueError("give n_reversals, n_trials, or both — a staircase must stop")
        return self


class QuestConfig(Model):
    """A QUEST+ staircase: the grids its posterior lives on."""

    parameter: str
    intensities: list[float]
    thresholds: list[float]
    slopes: list[float] = [3.5]
    lower_asymptotes: list[float] = [0.05]
    lapse_rates: list[float] = [0.02]
    n_trials: int = 60
    scale: StimScale = "linear"
    interleave_by: str | None = None

    @model_validator(mode="after")
    def _valid(self) -> QuestConfig:
        if not self.intensities or not self.thresholds:
            raise ValueError("intensities and thresholds must both be non-empty")
        if self.n_trials < 1:
            raise ValueError("n_trials must be >= 1")
        return self


class BlockConfig(Model):
    """Runs and blocks wrapped around whichever scheduler was chosen."""

    n_blocks: int = 1
    trials_per_block: int | None = None

    @model_validator(mode="after")
    def _valid(self) -> BlockConfig:
        if self.n_blocks < 1:
            raise ValueError("n_blocks must be >= 1")
        if self.trials_per_block is not None and self.trials_per_block < 1:
            raise ValueError("trials_per_block must be >= 1")
        return self


class SchedulerConfig(Model):
    """Which paradigm serves this task's trials."""

    kind: Literal["sequence", "constant", "staircase", "questplus", "adjustment"] = "sequence"
    # sequence / constant / adjustment: how many presentations of each cell.
    n_per_condition: int = 1
    shuffle: bool = True
    staircase: StaircaseConfig | None = None
    quest: QuestConfig | None = None
    blocks: BlockConfig | None = None

    @model_validator(mode="after")
    def _has_its_own_block(self) -> SchedulerConfig:
        if self.kind == "staircase" and self.staircase is None:
            raise ValueError("paradigm kind 'staircase' needs a 'staircase' block")
        if self.kind == "questplus" and self.quest is None:
            raise ValueError("paradigm kind 'questplus' needs a 'quest' block")
        if self.n_per_condition < 1:
            raise ValueError("n_per_condition must be >= 1")
        return self


# The kinds whose state has to carry across a block boundary. A staircase's
# whole point is that it remembers; a fresh one per block would throw away
# everything the subject just told it.
ADAPTIVE_KINDS = frozenset({"staircase", "questplus"})


def make_scheduler(
    cfg: SchedulerConfig,
    conditions: list[Condition],
    rng: np.random.Generator,
    score: Callable[[TrialResult], bool] | None = None,
    task_name: str | None = None,
) -> TrialSource:
    """Build the scheduler a config names, over the task's conditions.

    ``conditions`` are the task's own cells. The adaptive kinds take their
    varying parameter from their own config block instead, and read the task's
    conditions only for the levels they interleave over — so a task can move
    from constant stimuli to a staircase without rewriting ``conditions()``.

    With a ``blocks`` block, a **queue-based** kind gets one scheduler per
    block. That is not a style choice: sharing one queue across blocks means a
    failed condition re-queues at the end of the *whole remaining* queue and
    comes back in the final block instead of its own — and because the block
    number is part of the condition key, the runner records that retry as
    attempt 1 of a different condition. One queue per block puts the retry
    back where it belongs, which is what "end-of-block recycling" means.

    An **adaptive** kind shares one scheduler across every block, because its
    estimate must be continuous. It therefore needs ``trials_per_block`` to
    say where a block ends, and BlockPlan says so if it is missing.
    """
    if cfg.blocks is None:
        return _make_inner(cfg, conditions, rng, score, task_name)
    if cfg.kind in ADAPTIVE_KINDS:
        return BlockPlan(
            _make_inner(cfg, conditions, rng, score, task_name),
            n_blocks=cfg.blocks.n_blocks,
            trials_per_block=cfg.blocks.trials_per_block,
            rng=rng,
        )
    return BlockPlan(
        [_make_inner(cfg, conditions, rng, score, task_name) for _ in range(cfg.blocks.n_blocks)],
        trials_per_block=cfg.blocks.trials_per_block,
        rng=rng,
    )


def _make_inner(
    cfg: SchedulerConfig,
    conditions: list[Condition],
    rng: np.random.Generator,
    score: Callable[[TrialResult], bool] | None,
    task_name: str | None = None,
) -> TrialSource:
    if cfg.kind == "sequence":
        return SimpleSequence(
            conditions or [Condition({})],
            n_repeats=cfg.n_per_condition,
            rng=rng,
            shuffle=cfg.shuffle,
        )
    if cfg.kind == "constant":
        return ConstantStimuli(
            _grids(conditions, task_name),
            n_per_condition=cfg.n_per_condition,
            rng=rng,
            shuffle=cfg.shuffle,
        )
    if cfg.kind == "adjustment":
        return AdjustmentTrials(
            cfg.n_per_condition, conditions=conditions or None, rng=rng, shuffle=cfg.shuffle
        )
    if cfg.kind == "staircase":
        assert cfg.staircase is not None
        return _make_staircase(cfg.staircase, conditions, rng)
    assert cfg.quest is not None
    quest = cfg.quest
    return QuestPlus(
        parameter=quest.parameter,
        intensities=quest.intensities,
        thresholds=quest.thresholds,
        slopes=quest.slopes,
        lower_asymptotes=quest.lower_asymptotes,
        lapse_rates=quest.lapse_rates,
        n_trials=quest.n_trials,
        scale=quest.scale,
        interleave_by=quest.interleave_by,
        interleave_levels=_levels(conditions, quest.interleave_by),
        score=score,
    )


def _make_staircase(
    cfg: StaircaseConfig, conditions: list[Condition], rng: np.random.Generator
) -> TrialSource:
    def build(fixed: dict[str, Any]) -> UpDownStaircase:
        return UpDownStaircase(
            parameter=cfg.parameter,
            start=cfg.start,
            step=cfg.step,
            n_up=cfg.n_up,
            n_down=cfg.n_down,
            n_reversals=cfg.n_reversals,
            n_trials=cfg.n_trials,
            min_value=cfg.min_value,
            max_value=cfg.max_value,
            fixed=fixed,
        )

    if cfg.interleave_by is None:
        return build({})
    levels = _levels(conditions, cfg.interleave_by)
    return InterleavedStaircases(
        {str(level): build({cfg.interleave_by: level}) for level in levels}, rng=rng
    )


def _grids(conditions: list[Condition], task_name: str | None = None) -> dict[str, list[Any]]:
    """Recover the condition grids from the task's condition list.

    ConstantStimuli takes grids so it can build the factorial itself; a task
    declares cells. Values keep first-seen order within a key and are
    de-duplicated, so the reconstructed grid is the one the task meant.

    A list that is not a full factorial cannot be recovered this way, and the
    round trip *invents* the missing combinations: two specific pairings
    become a 2x2 grid, half of which the experiment never asked to run. That
    is refused here rather than quietly scheduled — a session collecting
    conditions nobody chose is worse than one that would not start.
    """
    if not conditions:
        raise ConfigError("paradigm kind 'constant' needs the task to declare conditions")
    grids: dict[str, list[Any]] = {}
    for condition in conditions:
        for key, value in condition.params.items():
            values = grids.setdefault(key, [])
            if value not in values:
                values.append(value)

    expanded = math.prod(len(values) for values in grids.values())
    if expanded != len(conditions):
        who = f"task {task_name!r}" if task_name else "this task"
        raise ConfigError(
            f"paradigm kind 'constant' builds the factorial of the condition grids, but "
            f"{who} declares {len(conditions)} conditions whose grids expand to {expanded}. "
            f"Its conditions are not a full factorial, so scheduling them this way would "
            f"add {expanded - len(conditions)} cells the experiment never asked for. Use "
            f"kind 'sequence' to serve the declared list as written, or declare the full "
            f"grid."
        )
    return grids


def _levels(conditions: list[Condition], key: str | None) -> list[Any]:
    if key is None:
        return []
    levels: list[Any] = []
    for condition in conditions:
        if key not in condition.params:
            raise ConfigError(
                f"paradigm interleaves by {key!r}, which this task's conditions do not "
                f"declare (they have {sorted(condition.params)})"
            )
        value = condition.params[key]
        if value not in levels:
            levels.append(value)
    if not levels:
        raise ConfigError(
            f"paradigm interleaves by {key!r}, but this task declares no conditions to "
            f"take its levels from"
        )
    return levels
