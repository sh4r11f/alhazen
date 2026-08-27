"""The reusable phases. A task composes these; it writes its own only for
something genuinely new.

Every phase here takes plain values — seconds, region names, stimulus keys,
Outcomes — and touches nothing but the TrialContext. That is what lets all of
them be tested against a fake clock and scripted inputs with no display, no
tracker, and no session.
"""

from alhazen.task.phases.gaze import (
    AcquireFixation,
    HoldFixation,
    LandingCheck,
    StimulusResponse,
)
from alhazen.task.phases.response import AdjustmentLoop, ResponseWindow
from alhazen.task.phases.sequence import FrameSequence
from alhazen.task.phases.simple import Blank, Feedback

__all__ = [
    "AcquireFixation",
    "AdjustmentLoop",
    "Blank",
    "Feedback",
    "FrameSequence",
    "HoldFixation",
    "LandingCheck",
    "ResponseWindow",
    "StimulusResponse",
]
