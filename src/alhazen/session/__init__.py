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
from alhazen.task.plan import TrialPlan as TrialPlan
from alhazen.task.plan import TrialSetup as TrialSetup

# `__all__` holds only the names docs/reference.md lists as public (a test in
# tests/unit/test_docs_snippets.py holds it to that). The `X as X` imports
# above are internal: they stay importable from here, for code that already
# imports them this way, but are not exported.
__all__ = [
    "DataRecorder",
    "PauseMenu",
    "SessionRunner",
    "build_session",
    "check_rig",
    "build_pause_menu",
    "pause_menu",
    "run_pause_menu",
]
