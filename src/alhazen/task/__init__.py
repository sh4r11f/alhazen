"""The layer experiments write against: a Task, a phase library, reward policy.

Below this line everything is framework machinery; at this line an experiment
declares what its trials are made of. Phases live in ``task.phases`` and are
deliberately dumb — they read and write only the TrialContext, take plain
seconds and names in their constructors, and never see a config model, a
device, or the bus.
"""

from alhazen.task.plan import BuildTrial, TrialPlan, TrialSetup
from alhazen.task.reward_policy import RewardPolicy
from alhazen.task.task import Task

__all__ = ["BuildTrial", "RewardPolicy", "Task", "TrialPlan", "TrialSetup"]
