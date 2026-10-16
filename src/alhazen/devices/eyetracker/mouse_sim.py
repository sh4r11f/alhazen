"""MouseSimTracker: the mouse cursor as gaze, for development with no tracker.

Lets a real gaze-contingent task be run and watched on a laptop: the phases,
the engine, and the regions all behave exactly as on the rig, with the
experimenter's own hand standing in for the subject's eye. There is no Host
PC, so messages only reach the session log.

Calibration has nothing to fit — the cursor is where the gaze is — but it is
walked as on the rig (user decision, 2026-10-15): the rig's calibration target
(``eyetracker.calibration_target``: standard, chosen pictures or random
pictures; still or pulsating; its size and pulse) is shown at each point of
the layout, through the same presenter and picture deck the real trackers'
calibrations draw (calibration_targets.py), with the same keys, the same
advance modes and the same progress stages on the live monitor as the
TRACKPixx3's walk. So a rehearsal on the mouse shows the subject's screen the
rig would show, and the validation that follows carries on with the same
deck. The result says that nothing was fitted.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

from alhazen.config.models import EyeTrackerConfig
from alhazen.core.clock import Clock
from alhazen.devices.eyetracker.calibration_targets import CalibrationTargets, TargetPresenter
from alhazen.devices.eyetracker.guide import GUIDE_TITLE, calibration_guide
from alhazen.devices.eyetracker.procedures import ABORT_KEY, ACCEPT_KEYS, REDO_KEY
from alhazen.devices.eyetracker.protocol import (
    CalibrationResult,
    GazeSample,
    HostShape,
    ProgressHook,
)
from alhazen.devices.eyetracker.viewpixx import (
    AUTO_SETTLE_S,
    AUTO_STEADY_REFRESHES,
    PAUSE_KEYS,
    STATUS_REFRESH_S,
    STOP_KEYS_LABEL,
    WALK_KEYS,
    calibration_targets,
    poll_keys,
    read_keys,
)
from alhazen.display.backend import DisplayBackend
from alhazen.display.screen import Screen

log = logging.getLogger(__name__)

# The walk's keys, in the experimenter's words (the TRACKPixx3's, without its
# camera: the cursor is always "in view").
GUIDE_KEYS = (
    ("SPACE", "accept this target"),
    ("BACKSPACE", "go back one target"),
    (STOP_KEYS_LABEL, "stop and go back to the pause menu"),
)
# What the live monitor and the guide say starts or stops the walk: the
# TRACKPixx3's words exactly, so the operator reads one instruction.
START_LINE = f"press SPACE to start, {STOP_KEYS_LABEL} to go back to the pause menu"
# The eye the result names: there is none.
NO_EYE = "none (the mouse cursor stands in for gaze)"


class MouseSimTracker:
    """Mouse-driven stand-in for a real tracker.

    ``get_gaze()`` always succeeds — the cursor's position is always known —
    unlike a real tracker, where a missing sample is routine. Tests that need
    gaze to *disappear* (the blink rule) use ScriptedTracker instead.
    """

    # The live monitor's Trial view says this in place of a camera image.
    camera_while_recording = "The mouse stand-in for an eye tracker has no camera."

    def __init__(
        self,
        display: DisplayBackend,
        screen: Screen,
        clock: Clock,
        cfg: EyeTrackerConfig | None = None,
        *,
        calibration_rng: np.random.Generator | None = None,
    ) -> None:
        # psychopy is imported inside __init__, not at module top, so that
        # importing this module stays safe on a machine with no renderer.
        from psychopy import event

        self._display = display
        self._screen = screen
        self._clock = clock
        # The rig's tracker settings: the layout, its area, the advance mode
        # and the calibration target. None (a caller that predates them) is
        # the defaults: HV5, the standard target, still.
        self._cfg = cfg if cfg is not None else EyeTrackerConfig(backend="mouse_sim")
        # What the calibration target looks like, the pictures it may show
        # and the record of every target shown — the real trackers' own
        # class, so the deck and its random order are theirs too.
        self._targets = CalibrationTargets(self._cfg.calibration_target, calibration_rng)
        self._progress: ProgressHook | None = None
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
        # Checked as the real trackers check it, at session build: the
        # target fits the screen at the outermost point and every picture it
        # may show decodes. A simulated display draws nothing, so loads none.
        if self._display.kind != "simulated":
            self._targets.prepare(screen, self._cfg.calibration_area)
        if not self._cfg.calibration_target.is_default:
            log.info("mouse_sim calibration target: %s", self._targets.style)

    # ------------------------------------------------------------------
    # Optional capabilities (protocol.py), as the real trackers have them
    # ------------------------------------------------------------------

    def set_calibration_rng(self, rng: np.random.Generator) -> None:
        """The random generator a random picture order is dealt from — the
        session seed's ``calibration_target`` stream."""
        self._targets.use_rng(rng)

    @property
    def calibration_targets(self) -> CalibrationTargets:
        """The target this calibration draws, which the session's validation
        and drift correction draw too (procedures.py)."""
        return self._targets

    def set_progress_hook(self, hook: ProgressHook | None) -> None:
        """Where the walk says which target it is on; None switches it off."""
        self._progress = hook

    def _report(self, stage: str, detail: str) -> None:
        if self._progress is not None:
            self._progress(stage, detail)

    # ------------------------------------------------------------------
    # Calibration: the targets walked, nothing fitted
    # ------------------------------------------------------------------

    def calibrate(self) -> CalibrationResult:
        """The guide's wait, then the rig's calibration target at each point
        of the layout, centre first — the TRACKPixx3's walk without a device.

        SPACE starts and accepts (or, in auto mode, each target is accepted
        once it has been up AUTO_SETTLE_S and AUTO_STEADY_REFRESHES status
        refreshes more — the cursor is always in view), BACKSPACE goes back
        one target, ESC or the pause key stops. Nothing is drawn but the
        target; the guide goes to the log and the keys to the live monitor.
        On a simulated display (nothing to draw on, nobody at the keys) the
        walk is skipped and the result says so.
        """
        cfg = self._cfg
        positions = calibration_targets(cfg.calibration_type, self._screen, cfg.calibration_area)
        n = len(positions)
        self._targets.begin_procedure()
        if self._display.kind == "simulated":
            log.info("mouse_sim tracker: no window to show calibration targets in")
            return self._result(n, "a simulated display shows no targets; nothing to fit")
        from psychopy import event, visual

        window = self._display.window
        target = self._targets.presenter(visual, window, self._screen, self._clock.now)
        body = calibration_guide(
            tracker="mouse (the cursor stands in for gaze; nothing is fitted)",
            eye=NO_EYE,
            layout=cfg.calibration_type,
            n_targets=n,
            area=cfg.calibration_area,
            advance=cfg.calibration_advance,
            keys=GUIDE_KEYS,
            start_line=START_LINE,
        )
        log.info("%s\n%s", GUIDE_TITLE, body)
        # Whatever was on the screen (the pause menu) is taken down: the
        # subject's screen is empty until the first target.
        window.flip()
        event.clearEvents("keyboard")
        if not self._wait_to_start(event, window):
            note = "aborted at the guide"
            log.warning("mouse_sim calibration %s", note)
            window.flip()
            return self._result(n, note, aborted=True)

        auto = cfg.calibration_advance == "auto"
        index = 0
        while index < n:
            target.show(positions[index])
            shown_at = self._clock.now()
            steady = 0
            pressed: str | None = None
            shown = False
            while pressed is None:
                # Drawn on every refresh, as the TRACKPixx3's walk draws it.
                target.draw()
                window.flip()
                if not shown:
                    # A press from before this target appeared belongs to the
                    # last one; cleared once, never again while it waits.
                    event.clearEvents("keyboard")
                    shown = True
                self._report("calibrating", f"target {index + 1} of {n}")
                keys = self._wait_for_keys(event, window, target)
                if keys:
                    pressed = keys[0]
                elif getattr(window, "_closed", False):
                    pressed = ABORT_KEY  # the window went away: abort, never accept
                elif auto:
                    settled = self._clock.now() - shown_at >= AUTO_SETTLE_S
                    steady = steady + 1 if settled else 0
                    if steady >= AUTO_STEADY_REFRESHES:
                        pressed = ACCEPT_KEYS[0]
            if pressed == ABORT_KEY or pressed in PAUSE_KEYS:
                how = "stopped for the pause menu" if pressed in PAUSE_KEYS else "aborted"
                note = f"{how} at target {index + 1} of {n}"
                log.warning("mouse_sim calibration %s", note)
                target.hide()
                window.flip()
                return self._result(n, note, aborted=True)
            if pressed == REDO_KEY:
                index = max(0, index - 1)
                continue
            index += 1
        target.hide()
        window.flip()
        log.info("mouse_sim calibration: %d targets shown; nothing to fit", n)
        return self._result(n, "targets shown; the mouse cursor is the gaze, so nothing was fitted")

    def _wait_to_start(self, event: Any, window: Any) -> bool:
        """The guide's wait: True when SPACE starts the walk."""
        while True:
            self._report("calibration guide", START_LINE)
            keys = read_keys(event, [ACCEPT_KEYS[0], ABORT_KEY, *PAUSE_KEYS], STATUS_REFRESH_S)
            if keys:
                return keys[0] == ACCEPT_KEYS[0]
            if getattr(window, "_closed", False):
                return False

    def _wait_for_keys(self, event: Any, window: Any, target: TargetPresenter) -> list[str] | None:
        """One refresh's wait for keys — a pulsating target redrawn each frame
        meanwhile, a still one left as drawn (the TRACKPixx3 walk's rhythm)."""
        if not target.animated:
            return read_keys(event, WALK_KEYS, STATUS_REFRESH_S)
        deadline = self._clock.now() + STATUS_REFRESH_S
        while True:
            keys = poll_keys(event, WALK_KEYS)
            if keys or getattr(window, "_closed", False) or self._clock.now() >= deadline:
                return keys
            target.draw()
            window.flip()

    def _result(self, n: int, note: str, *, aborted: bool = False) -> CalibrationResult:
        cfg = self._cfg
        return CalibrationResult(
            ok=None,
            layout=cfg.calibration_type,
            n_targets=n,
            eye=NO_EYE,
            advance=cfg.calibration_advance,
            t=self._clock.now(),
            note=note,
            aborted=aborted,
            target_style=self._targets.style,
            shown=self._targets.shown_since_begin(),
        )

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

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
        # Stamped now, and rightly: the cursor is read afresh on every call,
        # so every call is a new sample (protocol.py, GazeSample.t).
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
