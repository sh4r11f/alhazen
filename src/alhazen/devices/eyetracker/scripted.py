"""ScriptedTracker: deterministic gaze replay. Test-only.

Replays a fixed ``(time_s, sample)`` script against the injected session
clock, with no renderer, no vendor SDK, and no wall-clock waiting — which is
what lets a full gaze-contingent session run inside plain pytest. Session
build and ``check-rig`` both refuse the ``scripted`` backend: a rig YAML has
no way to supply a trajectory, so naming it there is a config mistake.
"""

from __future__ import annotations

from bisect import bisect_right
from pathlib import Path

from alhazen.core.clock import Clock
from alhazen.devices.eyetracker.protocol import GazeSample, HostShape
from alhazen.display.screen import Screen


class ScriptedTracker:
    """Replays ``[(time_s, GazeSample | None), ...]``.

    ``get_gaze()`` returns the latest entry whose time has already passed —
    "the newest sample as of now", which is how a real tracker's newest-sample
    call behaves; it never interpolates and never returns something from the
    future. A ``None`` entry is a scripted blink or track loss: real signal,
    not an error, and the blink rule turns it into "outside every region".
    """

    def __init__(self, samples: list[tuple[float, GazeSample | None]], clock: Clock) -> None:
        # Sorted defensively: an out-of-order script would otherwise produce
        # silently wrong lookups below instead of either working or failing.
        self._script = sorted(samples, key=lambda entry: entry[0])
        self._times = [t for t, _ in self._script]
        self._clock = clock
        self._recording = False
        # Everything send_message() was handed, in order, so a test can assert
        # on what a subscriber sent without an EDF or a mocking framework.
        self.sent_messages: list[str] = []
        self.overlays: list[list[HostShape]] = []
        self.trials_started: list[tuple[int, str]] = []
        self.shutdowns: list[Path | None] = []

    def connect(self) -> None:
        return  # the whole script is already in memory

    def configure(self, screen: Screen, clock: Clock) -> None:
        # A scripted tracker is constructed with the clock it replays against,
        # because a test builds it before there is a session. Taking the
        # session's here keeps the two in step when it is handed to one.
        self._clock = clock

    def calibrate(self) -> None:
        return  # deterministic replay needs no calibration

    def start_trial(self, trial_index: int, status: str) -> None:
        self.trials_started.append((trial_index, status))
        self._recording = True

    def stop_trial(self) -> None:
        self._recording = False

    def stop_recording(self) -> None:
        """Simulate the tracker dropping out mid-trial, so a test can exercise
        the runner's ``tracker_stopped`` health check."""
        self._recording = False

    def is_recording(self) -> bool:
        return self._recording

    def get_gaze(self) -> GazeSample | None:
        idx = bisect_right(self._times, self._clock.now()) - 1
        if idx < 0:
            return None  # the clock is still before the first entry
        return self._script[idx][1]

    def send_message(self, text: str) -> None:
        self.sent_messages.append(text)

    def draw_host_overlay(self, shapes: list[HostShape]) -> None:
        self.overlays.append(list(shapes))

    def shutdown(self, recording_destination: Path | None, /) -> None:
        self.shutdowns.append(recording_destination)
