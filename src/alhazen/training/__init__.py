"""Training: curricula, criteria, and a subject's place in them.

A curriculum is data — named stages that override the task's own parameters,
with criteria for moving between them — so a shaping protocol can be read,
reviewed and changed without touching experiment code. The subject's place in
it persists between sessions, beside that subject's data.

Sits above ``task`` (it re-validates parameters through the task's own model)
and below ``session`` (the runner drives it).
"""

from alhazen.training.criteria import (
    completed_rate,
    mean_rt_ms,
    register_metric,
    success_rate,
)
from alhazen.training.stages import Curriculum, Ramp, Stage, StageCriteria
from alhazen.training.state import TrainingState
from alhazen.training.supervisor import StageChange, TrainingSupervisor

__all__ = [
    "Curriculum",
    "Ramp",
    "Stage",
    "StageChange",
    "StageCriteria",
    "TrainingState",
    "TrainingSupervisor",
    "completed_rate",
    "mean_rt_ms",
    "register_metric",
    "success_rate",
]
