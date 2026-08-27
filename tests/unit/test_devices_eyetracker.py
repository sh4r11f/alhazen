"""Eye-tracker backends: the blink sentinel, deterministic replay, tracker
messages, and the lifecycle guarantees the runner depends on."""

from __future__ import annotations

import importlib.util

import pytest

from alhazen.config.models import EyeTrackerConfig
from alhazen.core.events import Event
from alhazen.devices.eyetracker import (
    EyeTracker,
    GazeSample,
    ScriptedTracker,
    TrackerMessageSubscriber,
    is_missing_gaze,
    make_tracker,
)
from alhazen.devices.eyetracker.eyelink import MISSING_DATA
from alhazen.errors import ConfigError, TrackerError
from alhazen.testing import FakeClock
from support import SCREEN


def sample(t: float, x: float, y: float) -> tuple[float, GazeSample]:
    return (t, GazeSample(gx=x, gy=y, t=t))


class TestMissingDataSentinel:
    def test_sentinel_in_either_coordinate_is_missing(self):
        # A blink streams samples whose coordinates are the sentinel, not an
        # absence of samples — treating one as a position records a gaze tens
        # of thousands of degrees off screen as if the subject looked there.
        assert is_missing_gaze(MISSING_DATA, MISSING_DATA, MISSING_DATA)
        assert is_missing_gaze(500.0, MISSING_DATA, MISSING_DATA)
        assert is_missing_gaze(MISSING_DATA, 500.0, MISSING_DATA)

    def test_real_coordinates_are_not_missing(self):
        assert not is_missing_gaze(0.0, 0.0, MISSING_DATA)
        assert not is_missing_gaze(-1.0, 960.0, MISSING_DATA)

    def test_sentinel_value_matches_every_eyelink_model(self):
        assert MISSING_DATA == -32768


class TestScriptedTracker:
    def test_replays_the_newest_sample_as_of_now(self):
        clock = FakeClock()
        tracker = ScriptedTracker([sample(0.0, 10.0, 10.0), sample(1.0, 20.0, 20.0)], clock)
        assert tracker.get_gaze() == GazeSample(gx=10.0, gy=10.0, t=0.0)
        clock.advance(1.5)
        # Not interpolated and not "the next one": the newest sample already
        # recorded, exactly like a real tracker's newest-sample call.
        assert tracker.get_gaze() == GazeSample(gx=20.0, gy=20.0, t=1.0)

    def test_no_sample_before_the_script_starts(self):
        clock = FakeClock()
        tracker = ScriptedTracker([sample(1.0, 5.0, 5.0)], clock)
        assert tracker.get_gaze() is None

    def test_none_entry_is_a_blink(self):
        clock = FakeClock(start=1.0)
        tracker = ScriptedTracker([sample(0.0, 5.0, 5.0), (0.5, None)], clock)
        assert tracker.get_gaze() is None

    def test_unsorted_script_is_ordered_defensively(self):
        clock = FakeClock(start=0.4)
        tracker = ScriptedTracker([sample(0.5, 2.0, 2.0), sample(0.0, 1.0, 1.0)], clock)
        assert tracker.get_gaze() == GazeSample(gx=1.0, gy=1.0, t=0.0)

    def test_stop_trial_is_idempotent(self):
        # The runner calls this in a finally, so it can arrive on a trial that
        # never started recording — twice, even.
        tracker = ScriptedTracker([], FakeClock())
        tracker.stop_trial()
        tracker.start_trial(1, "attempt 1")
        assert tracker.is_recording()
        tracker.stop_trial()
        tracker.stop_trial()
        assert not tracker.is_recording()

    def test_satisfies_the_protocol(self):
        assert isinstance(ScriptedTracker([], FakeClock()), EyeTracker)


class TestTrackerMessages:
    def make(self, message_map=None):
        tracker = ScriptedTracker([], FakeClock())
        return tracker, TrackerMessageSubscriber(tracker, message_map)

    def test_default_text_is_the_lowercased_event_name(self):
        tracker, subscriber = self.make()
        subscriber(Event(name="STIM_ON", t=0.0, trial_index=1))
        subscriber(Event(name="TRIAL_END", t=1.0, trial_index=1))
        assert tracker.sent_messages == ["stim_on", "trial_end"]

    def test_string_override_replaces_one_event(self):
        tracker, subscriber = self.make({"TRIAL_START": "START_OF_TRIAL"})
        subscriber(Event(name="TRIAL_START", t=0.0, trial_index=3))
        subscriber(Event(name="STIM_ON", t=0.1, trial_index=3))
        assert tracker.sent_messages == ["START_OF_TRIAL", "stim_on"]

    def test_callable_override_sees_the_whole_event(self):
        # This is how a ported experiment reproduces its legacy EDF strings
        # from its own package — those strings never enter alhazen.
        tracker, subscriber = self.make(
            {
                "TRIAL_START": lambda e: f"TRIALID {e.trial_index}",
                "REWARD": lambda e: (
                    "manual_reward" if e.payload.get("manual") is True else "reward_given"
                ),
            }
        )
        subscriber(Event(name="TRIAL_START", t=0.0, trial_index=7))
        subscriber(Event(name="REWARD", t=0.5, trial_index=7, payload={"manual": True}))
        subscriber(Event(name="REWARD", t=0.9, trial_index=7))
        assert tracker.sent_messages == ["TRIALID 7", "manual_reward", "reward_given"]


class TestMakeTracker:
    def test_scripted_backend_is_rejected(self):
        cfg = EyeTrackerConfig(backend="scripted")
        with pytest.raises(ConfigError, match="test-only"):
            make_tracker(cfg, None, SCREEN, FakeClock())

    def test_mouse_sim_needs_a_display(self):
        cfg = EyeTrackerConfig(backend="mouse_sim")
        with pytest.raises(ConfigError, match="display"):
            make_tracker(cfg, None, SCREEN, FakeClock())

    def test_eyelink_constructs_without_pylink_installed(self):
        # Construction must not import the SDK: check-rig and session build
        # both construct before they connect, and the actionable error
        # belongs at connect() time.
        tracker = make_tracker(EyeTrackerConfig(backend="eyelink"), None, SCREEN, FakeClock())
        assert not tracker.is_recording()

    def test_missing_pylink_names_the_developers_kit_and_the_pypi_trap(self):
        if importlib.util.find_spec("pylink") is not None:  # pragma: no cover - rig only
            pytest.skip("pylink is installed on this machine")
        tracker = make_tracker(EyeTrackerConfig(backend="eyelink"), None, SCREEN, FakeClock())
        with pytest.raises(TrackerError) as excinfo:
            tracker.connect()
        message = str(excinfo.value)
        assert "Developer's Kit" in message
        # The PyPI project of the same name is a different package entirely,
        # and installing it would shadow the real SDK on the rig.
        assert "unrelated" in message
        assert "mouse_sim" in message
