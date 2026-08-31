"""Running a session: the outer loop, the wiring, the recorder, the pre-session
checks."""

from alhazen.session.builder import build_session
from alhazen.session.checks import check_rig
from alhazen.session.pause import PauseMenu, build_pause_menu, pause_menu, run_pause_menu
from alhazen.session.recorder import DataRecorder
from alhazen.session.runner import SessionRunner

# The trial-building vocabulary lives in the task layer (task/plan.py),
# below this one; re-exported here because a session is where most people
# first meet it.
from alhazen.task.plan import TrialPlan, TrialSetup

__all__ = [
    "DataRecorder",
    "PauseMenu",
    "SessionRunner",
    "TrialPlan",
    "TrialSetup",
    "build_session",
    "check_rig",
    "build_pause_menu",
    "pause_menu",
    "run_pause_menu",
]
