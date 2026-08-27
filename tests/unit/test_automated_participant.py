"""The automated participant: a scripted subject for unattended demos.

It is a *device*, so it lives under the same invariants every device does —
in particular invariant 2, one clock. A demo participant that stamped its own
gaze from the wall clock would produce a trace that cannot be compared with
any other timestamp in the run.
"""

from __future__ import annotations

import pytest

from alhazen.core.events import Event
from alhazen.devices.automated import AutomatedGazeTracker, AutomatedResponse
from alhazen.devices.eyetracker import EyeTracker, HostShape
from alhazen.display.screen import Screen
from alhazen.errors import TrackerError
from alhazen.testing import FakeClock
from support import MONITOR


def configured(clock=None) -> tuple[AutomatedGazeTracker, FakeClock]:
    clock = clock or FakeClock()
    tracker = AutomatedGazeTracker()
    tracker.configure(Screen.from_monitor(MONITOR), clock)
    tracker.draw_host_overlay(
        [
            HostShape("box", 90, 90, 110, 110),
            HostShape("box", 290, 190, 310, 210),
        ]
    )
    return tracker, clock


def test_automated_gaze_moves_from_fixation_to_target() -> None:
    tracker, _clock = configured()
    tracker.start_trial(1, "attempt 1")
    assert (tracker.get_gaze().gx, tracker.get_gaze().gy) == (100, 100)

    tracker.send_message("stim_on")
    assert (tracker.get_gaze().gx, tracker.get_gaze().gy) == (300, 200)


class TestOneClock:
    def test_samples_are_stamped_from_the_session_clock(self):
        """It stamped `time.monotonic()`, which is a second clock in a session
        that is supposed to have exactly one."""
        tracker, clock = configured()
        tracker.start_trial(1, "attempt 1")

        assert tracker.get_gaze().t == clock.now()
        clock.advance(0.5)
        assert tracker.get_gaze().t == pytest.approx(0.5)

    def test_the_stamp_never_runs_ahead_of_the_session(self):
        tracker, clock = configured()
        clock.advance(3.0)

        # A monotonic stamp is seconds since boot: enormous next to a session
        # clock that starts near zero, and useless to compare against one.
        assert tracker.get_gaze().t == pytest.approx(3.0)

    def test_gaze_before_configure_says_what_is_missing(self):
        tracker = AutomatedGazeTracker()

        with pytest.raises(TrackerError, match="session clock"):
            tracker.get_gaze()

    def test_it_still_satisfies_the_tracker_protocol(self):
        tracker, _clock = configured()
        assert isinstance(tracker, EyeTracker)


def test_automated_response_answers_each_cue_once() -> None:
    response = AutomatedResponse(delay_s=0)
    response.on_event(Event("RESPONSE_CUE", 0.0, 1))
    assert response.poll().keys == ("right",)
    assert response.poll().keys == ()

    response.on_event(Event("RESPONSE_CUE", 1.0, 2))
    assert response.poll().keys == ("left",)


def test_the_automated_response_produces_no_timestamps() -> None:
    """Its monotonic reads pace how long the demo participant "thinks"; the
    answer's own time is stamped by the engine, from the session clock, when
    the response reaches a phase. Nothing here goes into the data."""
    sample = AutomatedResponse(delay_s=0).poll()

    assert not hasattr(sample, "t")
