"""build_session's device wiring and the config cross-checks that need the
experiment's own event vocabulary."""

from __future__ import annotations

import pytest

from alhazen import CircleRegion, Model
from alhazen.config.models import (
    DevicesConfig,
    DisplayConfig,
    Duration,
    EyeTrackerConfig,
    PhotodiodeConfig,
    RewardHwConfig,
    RigConfig,
    SyncHwConfig,
)
from alhazen.core.events import EventSchema
from alhazen.core.trial import InputFrame
from alhazen.devices.eyetracker import GazeSample, ScriptedTracker
from alhazen.devices.eyetracker.procedures import GazeCorrection
from alhazen.devices.reward import SimulatedReward
from alhazen.errors import ConfigError
from alhazen.paradigms.base import Condition, SimpleSequence
from alhazen.session.builder import (
    build_session,
    make_gaze_input_provider,
    make_input_provider,
    make_tracker_health_check,
)
from alhazen.session.runner import host_overlay_shapes
from alhazen.task.plan import TrialPlan
from alhazen.testing import FakeClock
from support import COMPLETED, MONITOR, SCREEN, RunForFrames


class Params(Model):
    n_trials: int = 1


def build(tmp_path, schema, **kwargs):
    """Build a session over a simulated rig. Keyword arguments starting with
    ``rig_`` describe the rig's own devices; the rest are passed to
    build_session (including device overrides)."""
    display = kwargs.pop("display", DisplayConfig(backend="simulated"))
    # rig_reward=<config model> describes what the rig HAS; reward=<object>
    # hands build_session a device directly. Naming them apart keeps the two
    # meanings from colliding in one keyword.
    rig_devices = {name[4:]: kwargs.pop(name) for name in list(kwargs) if name.startswith("rig_")}
    rig = RigConfig(
        monitor=MONITOR,
        display=display,
        devices=DevicesConfig(**rig_devices),
        data_root=tmp_path,
    )
    build_trial = kwargs.pop(
        "build_trial", lambda setup: TrialPlan(phases=[RunForFrames(1, COMPLETED)])
    )
    return build_session(
        rig=rig,
        subject="t01",
        session=1,
        run=1,
        task_name="test-task",
        task_params=Params(),
        event_schema=schema,
        build_trial=build_trial,
        make_source=lambda params, rng: SimpleSequence(
            [Condition({"c": "a"})], n_repeats=1, rng=rng
        ),
        seed=1,
        iti=Duration(ms=0),
        simulated_frame_period_s=0.0,
        date_yyyymmdd="20260826",
        **kwargs,
    )


class TestEventNameCrossValidation:
    def test_sync_line_for_an_undeclared_event_fails_at_build(self, tmp_path):
        schema = EventSchema(("FIX_ON",))
        sync = SyncHwConfig(backend="simulated", event_lines={"STIM_ONN": "Dev1/port0/line0"})
        with pytest.raises(ConfigError) as excinfo:
            build(tmp_path, schema, rig_sync=sync)
        message = str(excinfo.value)
        assert "STIM_ONN" in message  # the typo itself
        assert "FIX_ON" in message  # and what was actually declared

    def test_reserved_event_names_are_valid_sync_keys(self, tmp_path):
        schema = EventSchema(("FIX_ON",))
        sync = SyncHwConfig(backend="simulated", event_lines={"TRIAL_START": "Dev1/line0"})
        runner = build(tmp_path, schema, rig_sync=sync)
        runner.run()

    def test_photodiode_event_is_validated_too(self, tmp_path):
        # An unmarked event would show up as a photodiode that simply never
        # flashes — the same silent failure the sync check exists to prevent.
        display = DisplayConfig(
            backend="simulated", photodiode=PhotodiodeConfig(events=["NOT_DECLARED"])
        )
        with pytest.raises(ConfigError, match="NOT_DECLARED"):
            build(tmp_path, EventSchema(("FIX_ON",)), display=display)


class TestDeviceSelection:
    def test_test_only_tracker_backend_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="test-only"):
            build(
                tmp_path,
                EventSchema(()),
                rig_eyetracker=EyeTrackerConfig(backend="scripted"),
            )

    def test_a_rig_with_no_devices_still_runs(self, tmp_path):
        # A rig may name no devices at all: every device seam stays unwired,
        # and a session still runs end to end with no device objects.
        runner = build(tmp_path, EventSchema(()))
        runner.run()
        trials = next((tmp_path / "sub-t01").rglob("*_trials.csv"))
        assert trials.read_text().count("COMPLETED") == 1

    def test_a_configured_sync_line_pulses_during_the_session(self, tmp_path):
        runner = build(
            tmp_path,
            EventSchema(()),
            rig_reward=RewardHwConfig(backend="simulated"),
            rig_sync=SyncHwConfig(
                backend="simulated", event_lines={"TRIAL_START": "Dev1/port0/line0"}
            ),
        )
        runner.run()
        # The device objects are the runner's own; reading them is how a test
        # sees what a simulated rig "did" without a DAQ attached.
        assert runner._sync.pulses == ["Dev1/port0/line0"]
        assert runner._reward.deliveries == []  # no outcome pays out in this phase


class TestSyncDisabledButStillMapped:
    """A valid config: keep `event_lines`, set `backend: none` because today's
    session has no recording attached. It built successfully and then died on
    the first mapped event of trial 1, because `none` built a SimulatedSync
    with nothing wired and its `pulse()` raises."""

    def test_a_session_with_sync_off_completes_its_trials(self, tmp_path):
        runner = build(
            tmp_path,
            EventSchema(("FIX_ON",)),
            rig_sync=SyncHwConfig(
                backend="none",
                event_lines={"TRIAL_START": "Dev1/port0/line0", "FIX_ON": "Dev1/port0/line1"},
            ),
            build_trial=lambda setup: TrialPlan(
                phases=[RunForFrames(1, COMPLETED, emit_on_enter="FIX_ON")]
            ),
        )

        runner.run()

        trials = next((tmp_path / "sub-t01").rglob("*_trials.csv"))
        assert trials.read_text().count("COMPLETED") == 1

    def test_the_line_map_is_still_validated_against_the_schema(self, tmp_path):
        # Turning sync off must not turn off the typo check: the map is still
        # config, and a session run with sync back on would use it.
        with pytest.raises(ConfigError, match="STIM_ONN"):
            build(
                tmp_path,
                EventSchema(("FIX_ON",)),
                rig_sync=SyncHwConfig(backend="none", event_lines={"STIM_ONN": "Dev1/line0"}),
            )


class TestDeviceOverrides:
    def test_a_handed_in_tracker_drives_the_session(self, tmp_path):
        # The seam a ported experiment needs: a scripted gaze trace replaying
        # through the real builder, rather than through a hand-wired copy of
        # it that could drift from what a session actually does.
        clock = FakeClock()
        tracker = ScriptedTracker([(0.0, GazeSample(gx=960.0, gy=540.0, t=0.0))], clock)
        runner = build(tmp_path, EventSchema(()), tracker=tracker)
        runner.run()
        assert tracker.trials_started  # the runner drove this object, not a config's

    def test_a_handed_in_tracker_gets_the_session_monitor(self, tmp_path):
        # The monitor is what the pause menu's C/V/D and the dashboard's
        # buttons act on, and it holds the drift correction the engine's
        # input provider applies — so a tracker handed in must get one, with
        # the rig's eye-tracker config when there is one and the test-only
        # default when the rig names no tracker at all.
        tracker = ScriptedTracker([], FakeClock())
        runner = build(tmp_path, EventSchema(()), tracker=tracker)
        monitor = runner._eyetracker
        assert monitor is not None
        assert monitor.publisher is not None and monitor.emit is not None
        assert monitor.correction.offset == (0.0, 0.0)

    def test_an_override_wins_over_the_rig_config(self, tmp_path):
        reward = SimulatedReward()
        runner = build(
            tmp_path,
            EventSchema(()),
            reward=reward,
            rig_reward=RewardHwConfig(backend="simulated"),
        )
        assert runner._reward is reward


class TestGazeInputProvider:
    def test_screen_px_become_centered_px(self, tmp_path):
        # The one conversion site in the codebase: trackers report y down
        # from the top-left, phases read y up from the centre.
        clock = FakeClock()
        tracker = ScriptedTracker([(0.0, GazeSample(gx=960.0, gy=440.0, t=0.0))], clock)
        provide = make_gaze_input_provider(tracker, SCREEN)
        assert provide() == InputFrame(gaze=(0.0, 100.0))

    def test_no_sample_stays_none(self):
        provide = make_gaze_input_provider(ScriptedTracker([], FakeClock()), SCREEN)
        # Never a guess: an unverifiable position stays unverifiable, which
        # is what puts it outside every region.
        assert provide().gaze is None

    def test_the_drift_correction_is_applied_after_the_conversion(self):
        # The correction is measured in centered px, so it is added after
        # the screen-to-centered conversion — and read on every frame, so a
        # correction applied at a pause moves the very next sample.
        clock = FakeClock()
        tracker = ScriptedTracker([(0.0, GazeSample(gx=980.0, gy=540.0, t=0.0))], clock)
        correction = GazeCorrection()
        provide = make_input_provider(SCREEN, tracker=tracker, correction=correction)
        assert provide().gaze == (20.0, 0.0)
        correction.shift_by(-20.0, 0.0, clock.now())
        assert provide().gaze == (0.0, 0.0)
        # A blink is still a blink: nothing is corrected into a position.
        tracker = ScriptedTracker([], clock)
        provide = make_input_provider(SCREEN, tracker=tracker, correction=correction)
        assert provide().gaze is None

    def test_health_check_reports_a_stopped_tracker(self):
        tracker = ScriptedTracker([], FakeClock())
        check = make_tracker_health_check(tracker)
        assert check() == "tracker_stopped"
        tracker.start_trial(1, "attempt 1")
        assert check() is None


class TestHostOverlay:
    def test_cross_at_the_centre_and_a_box_per_region(self):
        regions = {"fixation": CircleRegion(center=(0.0, 0.0), radius=40.0)}
        cross, box = host_overlay_shapes(SCREEN, regions)
        assert (cross.kind, cross.x1, cross.y1) == ("cross", 960, 540)
        # Corners normalized: centered y grows up, screen y grows down, so
        # the corners swap and x1/y1 must still be the smaller pair.
        assert (box.kind, box.x1, box.y1, box.x2, box.y2) == ("box", 920, 500, 1000, 580)

    def test_no_regions_still_draws_the_fixation_cross(self):
        assert len(host_overlay_shapes(SCREEN, {})) == 1
