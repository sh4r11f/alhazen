from alhazen.display.backend import DisplayBackend
from alhazen.display.frames import FrameMonitor, FrameRecord
from alhazen.display.monitors import Registration as Registration
from alhazen.display.psychopy_backend import PsychoPyDisplay
from alhazen.display.screen import Screen
from alhazen.display.screen import within_radius as within_radius
from alhazen.display.simulated import SimulatedDisplay
from alhazen.display.text import reflow

# `__all__` holds only the names docs/reference.md lists as public (a test in
# tests/unit/test_docs_snippets.py holds it to that). The `X as X` imports
# above are internal: they stay importable from here, for code that already
# imports them this way, but are not exported.
__all__ = [
    "DisplayBackend",
    "FrameMonitor",
    "FrameRecord",
    "PsychoPyDisplay",
    "Screen",
    "SimulatedDisplay",
    "reflow",
]
