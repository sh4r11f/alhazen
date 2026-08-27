"""Running a session: the outer loop, the wiring, the recorder, the pre-session
checks."""

from alhazen.session.builder import build_session
from alhazen.session.checks import check_rig
from alhazen.session.recorder import DataRecorder
from alhazen.session.runner import SessionRunner, pause_menu

# The trial-building vocabulary lives in the task layer (task/plan.py),
# below this one; re-exported here because a session is where most people
# first meet it.
from alhazen.task.plan import TrialPlan, TrialSetup

__all__ = [
    "DataRecorder",
    "SessionRunner",
    "TrialPlan",
    "TrialSetup",
    "build_session",
    "check_rig",
    "pause_menu",
]
