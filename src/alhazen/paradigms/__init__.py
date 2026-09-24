"""Paradigm schedulers: what to present next, and when the session is done.

Every scheduler here satisfies ``TrialSource`` structurally, draws all
randomness from an injected Generator, and obeys the one shared rule — an
outcome with ``completed=False`` produced no measurement, so its condition is
re-served rather than counted or scored.
"""

from alhazen.paradigms.adjustment import AdjustmentTrials
from alhazen.paradigms.base import Condition, SimpleSequence, TrialSource
from alhazen.paradigms.blocks import BlockPlan
from alhazen.paradigms.config import BlockConfig, QuestConfig, SchedulerConfig, StaircaseConfig
from alhazen.paradigms.config import make_scheduler as make_scheduler
from alhazen.paradigms.constant import ConstantStimuli
from alhazen.paradigms.questplus import QuestPlus
from alhazen.paradigms.questplus import QuestPlusEstimator as QuestPlusEstimator
from alhazen.paradigms.questplus import weibull as weibull
from alhazen.paradigms.staircase import InterleavedStaircases, UpDownStaircase

# `__all__` holds only the names docs/reference.md lists as public (a test in
# tests/unit/test_docs_snippets.py holds it to that). The `X as X` imports
# above are internal: they stay importable from here, for code that already
# imports them this way, but are not exported.
__all__ = [
    "AdjustmentTrials",
    "BlockConfig",
    "BlockPlan",
    "Condition",
    "ConstantStimuli",
    "InterleavedStaircases",
    "QuestConfig",
    "QuestPlus",
    "SchedulerConfig",
    "SimpleSequence",
    "StaircaseConfig",
    "TrialSource",
    "UpDownStaircase",
]
