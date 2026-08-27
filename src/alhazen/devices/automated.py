"""Automated participant devices for unattended, visible demonstrations."""

from __future__ import annotations

import time
from pathlib import Path

from alhazen.core.clock import Clock
from alhazen.core.events import Event
from alhazen.devices.eyetracker import GazeSample, HostShape
from alhazen.devices.response import ResponseSample
from alhazen.display.screen import Screen
from alhazen.errors import TrackerError


class AutomatedGazeTracker:
    """Look at fixation, then at a target after ``STIM_ON``.

    The runner's host overlay supplies the task's actual regions, keeping this
    demo participant independent of any one task's pixel geometry.
    """

    def __init__(self) -> None:
        self._screen: Screen | None = None
        self._clock: Clock | None = None
        self._fixation = (0.0, 0.0)
        self._target = (0.0, 0.0)
        self._gaze = (0.0, 0.0)
        self._recording = False

    def connect(self) -> None: ...

    def configure(self, screen: Screen, clock: Clock) -> None:
        self._screen = screen
        self._clock = clock

    def calibrate(self) -> None: ...

    def start_trial(self, trial_index: int, status: str) -> None:
        self._recording = True
        self._gaze = self._fixation

    def stop_trial(self) -> None:
        self._recording = False

    def is_recording(self) -> bool:
        return self._recording

    def get_gaze(self) -> GazeSample:
        """A sample stamped on the SESSION clock.

        It used to read ``time.monotonic()``, which is a second clock inside a
        session that is supposed to have exactly one (invariant 2). Every
        other timestamp in the run — events, flips, commands — comes from the
        injected clock, and a gaze trace stamped from a different one cannot
        be compared with any of them.
        """
        if self._clock is None:
            raise TrackerError(
                "AutomatedGazeTracker.get_gaze() was called before configure(); it needs "
                "the session clock to stamp a sample"
            )
        return GazeSample(*self._gaze, t=self._clock.now())

    def send_message(self, text: str) -> None:
        if text == "trial_start":
            self._gaze = self._fixation
        elif text == "stim_on":
            self._gaze = self._target

    def draw_host_overlay(self, shapes: list[HostShape]) -> None:
        boxes = [shape for shape in shapes if shape.kind == "box"]
        if boxes:
            self._fixation = _box_center(boxes[0])
            self._target = _box_center(boxes[-1])
            self._gaze = self._fixation

    def shutdown(self, recording_destination: Path | None, /) -> None: ...


class AutomatedResponse:
    """Emit alternating left/right answers shortly after each response cue.

    This one keeps ``time.monotonic()`` on purpose. It produces no timestamps:
    the monotonic reads only pace how long the automated participant "thinks"
    before answering, and the answer's own time is stamped by the engine from
    the session clock when the response reaches a phase. A wall-clock delay is
    the right tool for pacing a demonstration; it is the wrong one for
    stamping data, and nothing here stamps data.
    """

    def __init__(self, delay_s: float = 0.35) -> None:
        self._delay_s = delay_s
        self._cue_at: float | None = None
        self._answer_right = False

    def on_event(self, event: Event) -> None:
        if event.name == "RESPONSE_CUE":
            self._cue_at = time.monotonic()

    def poll(self) -> ResponseSample:
        if self._cue_at is None or time.monotonic() - self._cue_at < self._delay_s:
            return ResponseSample()
        self._cue_at = None
        self._answer_right = not self._answer_right
        return ResponseSample(keys=("right" if self._answer_right else "left",))


def _box_center(shape: HostShape) -> tuple[float, float]:
    return ((shape.x1 + shape.x2) / 2.0, (shape.y1 + shape.y2) / 2.0)
