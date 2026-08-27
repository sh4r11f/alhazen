"""Paradigm schedulers: what to present next, and when the session is done.

Every scheduler here satisfies ``TrialSource`` structurally, draws all
randomness from an injected Generator, and obeys the one shared rule — an
outcome with ``completed=False`` produced no measurement, so its condition is
re-served rather than counted or scored.
"""

from alhazen.paradigms.adjustment import AdjustmentTrials
from alhazen.paradigms.base import Condition, SimpleSequence, TrialSource
from alhazen.paradigms.blocks import BlockPlan
from alhazen.paradigms.config import (
    BlockConfig,
    QuestConfig,
    SchedulerConfig,
    StaircaseConfig,
    make_scheduler,
)
from alhazen.paradigms.constant import ConstantStimuli
from alhazen.paradigms.questplus import QuestPlus, QuestPlusEstimator, weibull
from alhazen.paradigms.staircase import InterleavedStaircases, UpDownStaircase

__all__ = [
    "AdjustmentTrials",
    "BlockConfig",
    "BlockPlan",
    "Condition",
    "ConstantStimuli",
    "InterleavedStaircases",
    "QuestConfig",
    "QuestPlus",
    "QuestPlusEstimator",
    "SchedulerConfig",
    "SimpleSequence",
    "StaircaseConfig",
    "TrialSource",
    "UpDownStaircase",
    "make_scheduler",
    "weibull",
]
