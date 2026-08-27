"""MouseSimTracker: the mouse cursor as gaze, for development with no tracker.

Lets a real gaze-contingent task be run and watched on a laptop: the phases,
the engine, and the regions all behave exactly as on the rig, with the
experimenter's own hand standing in for the subject's eye. There is no Host
PC, so calibration is a no-op and messages only reach the session log.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from alhazen.core.clock import Clock
from alhazen.devices.eyetracker.protocol import GazeSample, HostShape
from alhazen.display.backend import DisplayBackend
from alhazen.display.screen import Screen

log = logging.getLogger(__name__)


class MouseSimTracker:
    """Mouse-driven stand-in for a real tracker.

    ``get_gaze()`` always succeeds — the cursor's position is always known —
    unlike a real tracker, where a missing sample is routine. Tests that need
    gaze to *disappear* (the blink rule) use ScriptedTracker instead.
    """

    def __init__(self, display: DisplayBackend, screen: Screen, clock: Clock) -> None:
        # psychopy is imported inside __init__, not at module top, so that
        # importing this module stays safe on a machine with no renderer.
        from psychopy import event

        self._screen = screen
        self._clock = clock
        # PsychoPy may hide the OS cursor for fullscreen windows.  In this
        # backend the cursor is the simulated gaze marker, so hiding it makes
        # the task impossible to operate by hand.
        self._mouse: Any = event.Mouse(win=display.window, visible=True)
        self._mouse.setVisible(True)
        self._recording = False

    def connect(self) -> None:
        return  # no device to open a link to

    def configure(self, screen: Screen, clock: Clock) -> None:
        # Both arrived at construction (make_tracker is given them); taking
        # them again keeps every backend's configure step identical.
        self._screen = screen
        self._clock = clock

    def calibrate(self) -> None:
        # Exists so the experimenter's calibrate key behaves identically on
        # every backend, not because the mouse needs calibrating.
        log.info("mouse_sim tracker: nothing to calibrate (gaze is the mouse cursor)")

    def start_trial(self, trial_index: int, status: str) -> None:
        self._recording = True

    def stop_trial(self) -> None:
        self._recording = False

    def is_recording(self) -> bool:
        return self._recording

    def get_gaze(self) -> GazeSample | None:
        # psychopy's Mouse reports CENTERED, y-up px; every GazeSample in
        # alhazen is SCREEN px, y-down (protocol.py). Convert here, once, at
        # this boundary, so nothing downstream has to know which backend
        # produced a sample.
        x, y = self._mouse.getPos()
        gx, gy = self._screen.centered_to_screen(x, y)
        return GazeSample(gx=gx, gy=gy, t=self._clock.now())

    def send_message(self, text: str) -> None:
        # No EDF exists here, so there is nowhere for this to land. Logged at
        # debug: a trace of what would have been written, without pretending
        # it reached anything analysis will read.
        log.debug("mouse_sim tracker message: %s", text)

    def draw_host_overlay(self, shapes: list[HostShape]) -> None:
        return  # no Host PC display to draw on

    def shutdown(self, recording_destination: Path | None, /) -> None:
        return  # nothing was opened; nothing to retrieve
