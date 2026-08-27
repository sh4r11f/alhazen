"""alhazen — a framework for building and running vision science experiments.

The curated import surface an experiment package uses day to day. Everything
here is re-exported from its home module; the homes are the API reference.

An experiment normally needs four of these: ``Task`` (subclass it),
``outcomes`` (declare them), ``EventSchema`` (declare those too), and
``build_session(task=...)``. Phases come from ``alhazen.task.phases`` and
schedulers from ``alhazen.paradigms``, which are namespaces rather than
re-exports because a task imports a handful of them by name.
"""

from alhazen.config.models import (
    DashboardConfig,
    DatabaseConfig,
    DevicesConfig,
    DisplayConfig,
    Duration,
    EyeTrackerConfig,
    FrameQAConfig,
    Model,
    MonitorConfig,
    PhotodiodeConfig,
    RewardHwConfig,
    RewardPulses,
    RigConfig,
    SessionConfig,
    SessionInfo,
    SyncHwConfig,
)
from alhazen.core.engine import QuitRequested, TrialEngine, TrialResult
from alhazen.core.events import Event, EventBus, EventSchema
from alhazen.core.trial import (
    ABORTED,
    PAUSED,
    CircleRegion,
    InputFrame,
    Outcome,
    OutcomeSet,
    Phase,
    PhaseAction,
    TrialContext,
    outcomes,
)
from alhazen.dashboard import DashboardPanel, DashboardSpec
from alhazen.display.screen import Screen
from alhazen.errors import (
    AlhazenError,
    ConfigError,
    DataError,
    DisplayError,
    FrameQAError,
    RewardError,
    SessionError,
    SyncError,
    TrackerError,
)
from alhazen.paradigms.base import Condition, SimpleSequence, TrialSource
from alhazen.paradigms.config import SchedulerConfig
from alhazen.session.builder import build_session
from alhazen.session.database import DeviceSample, ExperimentDatabase
from alhazen.session.runner import SessionRunner
from alhazen.task.plan import BuildTrial, TrialPlan, TrialSetup
from alhazen.task.reward_policy import RewardPolicy
from alhazen.task.task import Task
from alhazen.training.stages import Curriculum, Ramp, Stage, StageCriteria
from alhazen.version import get_version

__version__ = get_version()

__all__ = [
    "ABORTED",
    "PAUSED",
    "AlhazenError",
    "build_session",
    "BuildTrial",
    "CircleRegion",
    "Condition",
    "ConfigError",
    "Curriculum",
    "DataError",
    "DashboardConfig",
    "DatabaseConfig",
    "DashboardPanel",
    "DashboardSpec",
    "DevicesConfig",
    "DisplayConfig",
    "DisplayError",
    "DeviceSample",
    "Duration",
    "Event",
    "EventBus",
    "EventSchema",
    "ExperimentDatabase",
    "EyeTrackerConfig",
    "FrameQAConfig",
    "FrameQAError",
    "InputFrame",
    "Model",
    "MonitorConfig",
    "Outcome",
    "outcomes",
    "OutcomeSet",
    "Phase",
    "PhaseAction",
    "PhotodiodeConfig",
    "QuitRequested",
    "Ramp",
    "RewardError",
    "RewardHwConfig",
    "RewardPolicy",
    "RewardPulses",
    "RigConfig",
    "SchedulerConfig",
    "Screen",
    "SessionConfig",
    "SessionError",
    "SessionInfo",
    "SessionRunner",
    "SimpleSequence",
    "Stage",
    "StageCriteria",
    "SyncError",
    "SyncHwConfig",
    "Task",
    "TrackerError",
    "TrialContext",
    "TrialEngine",
    "TrialPlan",
    "TrialResult",
    "TrialSetup",
    "TrialSource",
]
