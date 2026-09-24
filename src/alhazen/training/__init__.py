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
from alhazen.training.state import TrainingState as TrainingState
from alhazen.training.supervisor import StageChange as StageChange
from alhazen.training.supervisor import TrainingSupervisor as TrainingSupervisor

# `__all__` holds only the names docs/reference.md lists as public (a test in
# tests/unit/test_docs_snippets.py holds it to that). The `X as X` imports
# above are internal: they stay importable from here, for code that already
# imports them this way, but are not exported.
__all__ = [
    "Curriculum",
    "Ramp",
    "Stage",
    "StageCriteria",
    "completed_rate",
    "mean_rt_ms",
    "register_metric",
    "success_rate",
]
