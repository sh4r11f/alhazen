"""The layer experiments write against: a Task, a phase library, reward policy.

Below this line everything is framework machinery; at this line an experiment
declares what its trials are made of. Phases live in ``task.phases`` and are
deliberately dumb — they read and write only the TrialContext, take plain
seconds and names in their constructors, and never see a config model, a
device, or the bus.
"""

from alhazen.task.plan import BuildTrial, TrialPlan, TrialSetup
from alhazen.task.reward_policy import RewardPolicy
from alhazen.task.subject_mode import SubjectMode as SubjectMode
from alhazen.task.subject_mode import response_phases as response_phases
from alhazen.task.task import Task

# `__all__` holds only the names docs/reference.md lists as public (a test in
# tests/unit/test_docs_snippets.py holds it to that). The `X as X` imports
# above are internal: they stay importable from here, for code that already
# imports them this way, but are not exported.
__all__ = [
    "BuildTrial",
    "RewardPolicy",
    "Task",
    "TrialPlan",
    "TrialSetup",
]
