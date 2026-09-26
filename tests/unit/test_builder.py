"""build_session's device wiring and the config cross-checks that need the
experiment's own event vocabulary."""

from __future__ import annotations

import csv
import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from alhazen import CircleRegion, Model
from alhazen.config.models import (
    DEFAULT_MAX_CONSECUTIVE_DROPOUTS,
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
from alhazen.paradigms.config import SchedulerConfig
from alhazen.session.builder import (
    build_session,
    make_gaze_input_provider,
    make_input_provider,
    make_tracker_health_check,
)
from alhazen.session.runner import host_overlay_shapes
from alhazen.task.plan import TrialPlan
from alhazen.testing import FakeClock
from support import COMPLETED, MONITOR, SCREEN, RunForFrames, load_example_task

EXAMPLES = Path(__file__).parents[2] / "examples"


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
    make_source = kwargs.pop(
        "make_source",
        lambda params, rng: SimpleSequence([Condition({"c": "a"})], n_repeats=1, rng=rng),
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
        make_source=make_source,
        seed=1,
        # Zero unless a test is about the pause between trials.
        iti=kwargs.pop("iti", Duration(ms=0)),
        simulated_frame_period_s=0.0,
        date_yyyymmdd="20260826",
        **kwargs,
    )


class TestFrameQAOnADisplayWithNoPanel:
    """A simulated display's flip times measure how accurately the host can
    wait, not whether a panel is holding its refresh. A scaffolded lab rig
    ships `recycle_trial`, and its own acceptance run — `--mode simulate
    --headless`, the documented way to run an experiment on a CI box — aborted
    with "the display is not holding its 120 Hz refresh" on a loaded machine.
    There was no display."""

    def test_a_judging_policy_is_stood_down_and_said_so(self, tmp_path, caplog):
        import logging

        from alhazen.config.models import FrameQAConfig

        schema = EventSchema(("FIX_ON",))
        display = DisplayConfig(
            backend="simulated",
            frame_qa=FrameQAConfig(policy="recycle_trial", max_dropped_fraction=0.1),
        )
        with caplog.at_level(logging.INFO, logger="alhazen.session.builder"):
            built = build(tmp_path, schema, display=display)

        built.run()

        # Recorded, not acted on: the intervals still reach frames.csv, and
        # no trial is marked — only a policy that marks trials (not "log")
        # gives the rows an n_dropped_frames column.
        assert read_table(tmp_path, "frames")
        assert all("n_dropped_frames" not in row for row in read_table(tmp_path, "trials"))
        assert any(
            "not applied on a simulated display" in record.getMessage() for record in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_the_rig_files_own_policy_is_kept_for_a_real_display(self, tmp_path):
        """Nothing changes for the rig this was written for: only a display
        that reports itself simulated is exempt."""
        from alhazen.config.models import FrameQAConfig
        from alhazen.display.frames import FrameMonitor

        cfg = FrameQAConfig(policy="recycle_trial", max_dropped_fraction=0.1)
        monitor = FrameMonitor(cfg, refresh_rate_hz=120.0)
        assert monitor._cfg.policy == "recycle_trial"
        assert monitor.marks_trials

    def test_log_is_left_alone(self, tmp_path, caplog):
        import logging

        from alhazen.config.models import FrameQAConfig

        schema = EventSchema(("FIX_ON",))
        display = DisplayConfig(backend="simulated", frame_qa=FrameQAConfig(policy="log"))
        with caplog.at_level(logging.INFO, logger="alhazen.session.builder"):
            built = build(tmp_path, schema, display=display)
        built.run()
        assert all("n_dropped_frames" not in row for row in read_table(tmp_path, "trials"))
        assert not any(
            "not applied on a simulated display" in record.getMessage() for record in caplog.records
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
        # The sync device is the runner's own; reading it is how a test sees
        # what a simulated rig "did" without a DAQ attached.
        assert runner._sync.pulses == ["Dev1/port0/line0"]
        # No outcome pays out in this phase: events.csv records no REWARD,
        # which is written only once the device has delivered.
        with next(tmp_path.rglob("*_events.csv")).open() as f:
            assert [row for row in csv.DictReader(f) if row["event"] == "REWARD"] == []


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

    def test_a_trackers_own_dropout_check_reaches_the_row(self, tmp_path):
        """The engine a built session runs asks the tracker's recording_fault()
        every frame — the builder's own health check — and its words land on
        the row."""

        class DiesOnTheFirstTrial(ScriptedTracker):
            def recording_fault(self) -> str | None:
                return "no new sample for 60 ms" if len(self.trials_started) == 1 else None

        runner = build(
            tmp_path,
            EventSchema(()),
            tracker=DiesOnTheFirstTrial([], FakeClock()),
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(3, COMPLETED)]),
        )
        runner.run()
        # What the run wrote, read back from its trials table.
        with next(tmp_path.rglob("*_trials.csv")).open() as f:
            first, second = csv.DictReader(f)
        assert (first["outcome"], first["fault"]) == ("ABORTED", "tracker_stopped")
        assert first["fault_detail"] == "no new sample for 60 ms"
        assert (second["outcome"], second["fault"]) == ("COMPLETED", "none")

    @staticmethod
    def dropout_pauses(tmp_path, **kwargs) -> list[int]:
        """Run a built session whose tracker drops out on its first five
        trials, and return the length of each run of dropouts the session
        paused on, as its log reports them. Unattended, so each pause resumes
        at once; the count restarts after each."""

        class DropsOutFiveTimes(ScriptedTracker):
            def recording_fault(self) -> str | None:
                return "no new sample for 60 ms" if len(self.trials_started) <= 5 else None

        runner = build(
            tmp_path,
            EventSchema(()),
            tracker=DropsOutFiveTimes([], FakeClock()),
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(3, COMPLETED)]),
            **kwargs,
        )
        runner.run()
        log = next(tmp_path.rglob("session.log")).read_text(encoding="utf-8")
        return [int(n) for n in re.findall(r"on (\d+) trials in a row", log)]

    def test_the_rig_says_how_many_dropouts_in_a_row_pause_the_session(self, tmp_path):
        pauses = self.dropout_pauses(
            tmp_path,
            rig_eyetracker=EyeTrackerConfig(backend="eyelink", max_consecutive_dropouts=5),
        )
        assert pauses == [5]

    def test_a_rig_with_no_tracker_config_pauses_at_the_default(self, tmp_path):
        pauses = self.dropout_pauses(tmp_path)
        # Five dropouts hold one full run at the default of three.
        assert DEFAULT_MAX_CONSECUTIVE_DROPOUTS == 3
        assert pauses == [DEFAULT_MAX_CONSECUTIVE_DROPOUTS]

    def test_a_handed_in_tracker_gets_the_session_monitor(self, tmp_path):
        # The monitor is what the pause menu's C/V/D and the live monitor's
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


def refusing_scheduler(params, rng):
    """A make_source that raises: the build's last step before the runner,
    so every device is already held when it fails."""
    raise ValueError("the scheduler refused")


class TestAFailedBuildReleasesWhatItHeld:
    """A build that fails part-way must release everything it already holds.

    Its guard released only the live monitor, the reward worker and the spike
    source. A failure once the devices were up left the window open, the
    tracker's link connected and the sync lines' NI-DAQ tasks reserved, so
    the next session on the rig found them taken. And the display was built
    outside the guard, so a failure there left the live monitor's child process
    running.

    The devices here come from the rig config, built by the builder itself,
    with spies standing in for the factories: an EyeLink or a NidaqSync
    cannot be built on a test machine, and what is pinned is the builder's
    releasing them, not the backends' own close().
    """

    # Every release a build that got as far as its scheduler must make.
    EVERY_RELEASE = (
        "live_monitor.stop",
        "display.close",
        "tracker.shutdown",
        "sync.close",
        "reward.close",
        "spikes.close",
    )

    def wire(self, monkeypatch, failing=None):
        """Swap the live monitor, the simulated display and the device
        factories for spies that write each release into one list, returned
        with the list of trackers built. The release named ``failing``
        raises, after it has been recorded."""
        from alhazen.devices.spikes import SimulatedSpikeSource
        from alhazen.devices.sync import SimulatedSync
        from alhazen.display.simulated import SimulatedDisplay
        from alhazen.session import builder as builder_module
        from alhazen.testing import ScriptedReward

        released: list[str] = []

        def note(name):
            released.append(name)
            if name == failing:
                raise RuntimeError(f"{name} failed")

        class SpyController:
            def __init__(self, port=0, auto_open=True):
                self.url = "http://127.0.0.1:0/"

            def start(self):
                pass

            def stop(self):
                note("live_monitor.stop")

            def publish(self, state):
                pass

        class SpyDisplay(SimulatedDisplay):
            def close(self):
                super().close()
                note("display.close")

        class SpyTracker(ScriptedTracker):
            def shutdown(self, recording_destination, /):
                super().shutdown(recording_destination)
                note("tracker.shutdown")

        class SpyReward(ScriptedReward):
            def close(self):
                super().close()
                note("reward.close")

        class SpySync(SimulatedSync):
            def close(self):
                super().close()
                note("sync.close")

        class SpySpikes(SimulatedSpikeSource):
            def close(self):
                super().close()
                note("spikes.close")

        trackers: list[SpyTracker] = []

        def make_tracker(cfg, display, screen, clock):
            trackers.append(SpyTracker([], clock))
            return trackers[-1]

        monkeypatch.setattr(builder_module, "LiveMonitorController", SpyController)
        monkeypatch.setattr(builder_module, "SimulatedDisplay", SpyDisplay)
        monkeypatch.setattr(builder_module, "make_tracker", make_tracker)
        monkeypatch.setattr(builder_module, "make_reward", lambda cfg: SpyReward())
        monkeypatch.setattr(builder_module, "make_sync", lambda cfg: SpySync(cfg.event_lines))
        monkeypatch.setattr(builder_module, "make_spikes", lambda cfg: SpySpikes(cfg))
        return released, trackers

    def build_with_every_device(self, tmp_path, **kwargs):
        """A rig naming a tracker, a dispenser, sync lines and a spike
        source, with the live monitor on."""
        from alhazen.config.models import SpikeSourceConfig

        return build(
            tmp_path,
            EventSchema(("FIX_ON",)),
            rig_eyetracker=EyeTrackerConfig(backend="eyelink"),
            rig_reward=RewardHwConfig(backend="simulated"),
            rig_sync=SyncHwConfig(backend="simulated", event_lines={"TRIAL_START": "Dev1/line0"}),
            rig_spikes=SpikeSourceConfig(backend="simulated", sim_respond_to="FIX_ON"),
            live_monitor=True,
            open_live_monitor=False,
            **kwargs,
        )

    def test_a_failure_once_every_device_is_up_releases_each_of_them_once(
        self, tmp_path, monkeypatch
    ):
        released, trackers = self.wire(monkeypatch)

        with pytest.raises(ValueError, match="the scheduler refused"):
            self.build_with_every_device(tmp_path, make_source=refusing_scheduler)

        assert sorted(released) == sorted(self.EVERY_RELEASE)
        # None: a session that never began has no recording to retrieve.
        assert trackers[0].shutdowns == [None]

    def test_a_build_that_succeeds_releases_nothing(self, tmp_path, monkeypatch):
        # The releases belong to the runner's teardown from here on; a build
        # that ran them anyway would hand over a closed window and a dead link.
        released, trackers = self.wire(monkeypatch)

        runner = self.build_with_every_device(tmp_path)

        assert runner is not None
        assert released == []
        assert trackers[0].shutdowns == []

    def test_a_display_that_cannot_be_built_still_stops_the_live_monitor(
        self, tmp_path, monkeypatch
    ):
        from alhazen.errors import DisplayError
        from alhazen.session import builder as builder_module

        released, _ = self.wire(monkeypatch)

        class NoRenderer:
            def __init__(self, monitor, windowed=False):
                raise DisplayError("no renderer on this machine")

        monkeypatch.setattr(builder_module, "PsychoPyDisplay", NoRenderer)

        with pytest.raises(DisplayError, match="no renderer"):
            build(
                tmp_path,
                EventSchema(()),
                display=DisplayConfig(backend="psychopy"),
                live_monitor=True,
                open_live_monitor=False,
            )

        assert released == ["live_monitor.stop"]

    def test_a_window_refused_by_its_own_open_is_closed(self, tmp_path, monkeypatch):
        # PsychoPy's open() creates the window, then refuses it when the
        # framebuffer is not the size the rig config says. The window is up
        # at that point, and nothing else will ever close it.
        from alhazen.errors import DisplayError
        from alhazen.session import builder as builder_module

        released, _ = self.wire(monkeypatch)

        class RefusedWindow:
            kind = "psychopy"

            def __init__(self, monitor, windowed=False):
                self.window = None

            def open(self):
                self.window = object()
                raise DisplayError("the fullscreen drawing surface is 2880x1800 pixels")

            def close(self):
                self.window = None
                released.append("display.close")

        monkeypatch.setattr(builder_module, "PsychoPyDisplay", RefusedWindow)

        with pytest.raises(DisplayError, match="2880x1800"):
            build(
                tmp_path,
                EventSchema(()),
                display=DisplayConfig(backend="psychopy"),
                live_monitor=True,
                open_live_monitor=False,
            )

        assert sorted(released) == ["display.close", "live_monitor.stop"]

    @pytest.mark.parametrize("failing", EVERY_RELEASE)
    def test_a_release_that_fails_stops_neither_the_others_nor_the_build_error(
        self, tmp_path, monkeypatch, caplog, failing
    ):
        import logging

        released, _ = self.wire(monkeypatch, failing=failing)

        # The scheduler's error is the one that says what went wrong; a
        # release failing over it must not take its place.
        with (
            caplog.at_level(logging.ERROR, logger="alhazen.session.builder"),
            pytest.raises(ValueError, match="the scheduler refused"),
        ):
            self.build_with_every_device(tmp_path, make_source=refusing_scheduler)

        assert sorted(released) == sorted(self.EVERY_RELEASE)
        # Not swallowed either: the failed release is in the log, traceback
        # and all.
        logged = [r for r in caplog.records if "while aborting the build" in r.getMessage()]
        assert len(logged) == 1
        assert logged[0].exc_info is not None
        assert str(logged[0].exc_info[1]) == f"{failing} failed"

    def test_a_handed_in_tracker_is_released_like_the_rigs_own(self, tmp_path, monkeypatch):
        # The runner's teardown shuts down whatever tracker the session ran
        # with, handed in or built; a build that fails after connecting it
        # releases it the same way.
        self.wire(monkeypatch)
        tracker = ScriptedTracker([], FakeClock())

        with pytest.raises(ValueError, match="the scheduler refused"):
            build(tmp_path, EventSchema(()), tracker=tracker, make_source=refusing_scheduler)

        assert tracker.shutdowns == [None]


class TestGazeInputProvider:
    def test_screen_px_become_centered_px(self, tmp_path):
        # The one conversion site in the codebase: trackers report y down
        # from the top-left, phases read y up from the centre.
        clock = FakeClock()
        tracker = ScriptedTracker([(0.0, GazeSample(gx=960.0, gy=440.0, t=0.0))], clock)
        provide = make_gaze_input_provider(tracker, SCREEN)
        assert provide() == InputFrame(gaze=(0.0, 100.0), gaze_t=0.0)

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

    def test_the_samples_own_time_rides_along(self):
        # gaze_t is the time the tracker took the sample, not the time the
        # frame asked for it: a frame that brings no new sample repeats the
        # previous one with the SAME time, which is the only way a phase can
        # tell a repeat (a false zero speed) from a sample of a still eye.
        clock = FakeClock()
        tracker = ScriptedTracker(
            [
                (0.0, GazeSample(gx=960.0, gy=540.0, t=0.0)),
                (0.010, GazeSample(gx=970.0, gy=540.0, t=0.010)),
            ],
            clock,
        )
        provide = make_gaze_input_provider(tracker, SCREEN)
        assert provide().gaze_t == 0.0
        clock.advance(0.005)  # a frame, but no new sample yet
        assert provide().gaze_t == 0.0
        clock.advance(0.010)
        frame = provide()
        assert frame.gaze == (10.0, 0.0) and frame.gaze_t == pytest.approx(0.010)

    def test_a_blink_carries_no_time(self):
        # None whenever gaze is None: a time without a position would let a
        # phase count a blink as a sample.
        clock = FakeClock()
        tracker = ScriptedTracker(
            [(0.0, GazeSample(gx=960.0, gy=540.0, t=0.0)), (0.01, None)], clock
        )
        provide = make_gaze_input_provider(tracker, SCREEN)
        clock.advance(0.02)
        assert provide() == InputFrame(gaze=None, gaze_t=None)

    def test_the_drift_correction_moves_the_position_not_the_time(self):
        clock = FakeClock(start=2.5)
        tracker = ScriptedTracker([(0.0, GazeSample(gx=980.0, gy=540.0, t=2.25))], clock)
        correction = GazeCorrection()
        correction.shift_by(-20.0, 0.0, clock.now())
        provide = make_input_provider(SCREEN, tracker=tracker, correction=correction)
        assert provide() == InputFrame(gaze=(0.0, 0.0), gaze_t=2.25)

    def test_health_check_reports_a_stopped_tracker(self):
        tracker = ScriptedTracker([], FakeClock())
        check = make_tracker_health_check(tracker)
        failed = check()
        assert failed is not None
        # The reason is the fault vocabulary; the detail says which question
        # failed, for the row's fault_detail and the log.
        assert failed.reason == "tracker_stopped"
        assert failed.detail is not None and "is_recording() is False" in failed.detail
        tracker.start_trial(1, "attempt 1")
        assert check() is None

    def test_health_check_asks_a_tracker_that_can_tell_whether_it_still_delivers(self):
        """The segment flag cannot see a recording that died mid-trial; a
        backend's recording_fault() can, and its words become the detail."""

        class DyingTracker(ScriptedTracker):
            fault: str | None = None

            def recording_fault(self) -> str | None:
                return self.fault

        tracker = DyingTracker([], FakeClock())
        tracker.start_trial(1, "attempt 1")
        check = make_tracker_health_check(tracker)
        assert check() is None
        tracker.fault = "no new sample for 60 ms"
        failed = check()
        assert failed is not None
        assert (failed.reason, failed.detail) == ("tracker_stopped", "no new sample for 60 ms")

    def test_a_closed_segment_is_reported_before_the_device_is_asked(self):
        # With no segment open there is no recording to ask about: the flag
        # answers, and recording_fault() is never called.
        asked: list[bool] = []

        class Counting(ScriptedTracker):
            def recording_fault(self) -> str | None:
                asked.append(True)
                return None

        check = make_tracker_health_check(Counting([], FakeClock()))
        failed = check()
        assert failed is not None and failed.reason == "tracker_stopped"
        assert asked == []


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


class TestTheRestTimeoutReachesTheRunner:
    def test_build_session_hands_it_to_the_runner(self, tmp_path):
        built = build(tmp_path, EventSchema(("FIX_ON",)), rest_resume_after_s=5.0)

        assert built._rest_resume_after_s == 5.0

    def test_a_wait_of_zero_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="rest_resume_after_s must be > 0"):
            build(tmp_path, EventSchema(("FIX_ON",)), rest_resume_after_s=0)


def read_table(tmp_path, suffix: str) -> list[dict[str, str]]:
    """One of the run's tables, read back from the file the session wrote."""
    (path,) = tmp_path.rglob(f"*_{suffix}.csv")
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class TestTheSessionClock:
    """``build_session(clock=...)``: the one clock every recorded time comes from.

    The builder used to make its own MonotonicClock with no way to pass one
    in. A test that handed in a tracker had to build that tracker on a
    second clock, and every phase of a built session was timed by the host's
    real clock: on a loaded machine a 3-frame stimulus could end after one
    frame (issue #62).
    """

    # Far from zero, where a real clock made at build time would start, so a
    # time read from any other clock cannot pass for one read from this.
    START = 1000.0
    FRAME = 1.0 / MONITOR.refresh_rate_hz

    def run(self, tmp_path, clock, **kwargs):
        runner = build(
            tmp_path,
            EventSchema(("STIM_ON",)),
            clock=clock,
            build_trial=lambda setup: TrialPlan(
                phases=[RunForFrames(3, COMPLETED, emit_on_enter="STIM_ON")]
            ),
            make_source=lambda params, rng: SimpleSequence(
                [Condition({"c": "a"}), Condition({"c": "b"})], n_repeats=1, rng=rng
            ),
            **kwargs,
        )
        runner.run()
        return runner

    def on_the_frame_lattice(self, t: float) -> bool:
        """This clock moves only a whole frame at a time (with no ITI), so
        every time read from it is START plus a whole number of frames. The
        tolerance covers frames.csv's six written decimals."""
        frames = (t - self.START) / self.FRAME
        return abs(frames - round(frames)) < 1e-3

    def test_every_recorded_time_is_on_the_clock_handed_in(self, tmp_path):
        clock = FakeClock(start=self.START)
        self.run(tmp_path, clock)

        events = [float(row["t"]) for row in read_table(tmp_path, "events")]
        stamps = [
            float(value)
            for row in read_table(tmp_path, "trials")
            for column, value in row.items()
            if column.startswith("t_") and value
        ]
        frame_rows = read_table(tmp_path, "frames")
        flips = [float(row["t"]) for row in frame_rows]
        assert events and stamps and flips

        # The session moved the clock itself: the simulated display advanced
        # it on every flip. Nothing else could have, since nothing else knows
        # it is a fake.
        assert clock.now() > self.START
        for t in events + stamps + flips:
            assert self.START <= t <= clock.now(), t
            assert self.on_the_frame_lattice(t), t
        # And every frame lasted exactly one frame, which is what makes a
        # phase timed in frames span the same flips on any machine.
        assert all(
            float(row["interval_s"]) == pytest.approx(self.FRAME, abs=1e-5) for row in frame_rows
        )

    def test_the_pause_between_trials_passes_on_that_clock_too(self, tmp_path):
        # The runner's wait advances a clock like this one rather than
        # sleeping: slept for real, the ITI would leave the fake where it was
        # (and a rest that resumes by itself at a deadline would never end).
        clock = FakeClock(start=self.START)
        self.run(tmp_path, clock, iti=Duration(ms=500))

        events = read_table(tmp_path, "events")
        ends = [float(r["t"]) for r in events if r["event"] == "TRIAL_END"]
        starts = [float(r["t"]) for r in events if r["event"] == "TRIAL_START"]
        assert len(starts) == 2
        assert starts[1] - ends[0] == pytest.approx(0.5)

    def test_a_clock_that_moves_only_when_told_needs_the_simulated_display(self, tmp_path):
        # On a real window nothing advances it, so the first timed phase
        # would never end: refused with a reason, before any run directory.
        with pytest.raises(ConfigError, match="Only the simulated display advances"):
            build(
                tmp_path,
                EventSchema(()),
                clock=FakeClock(),
                display=DisplayConfig(backend="psychopy"),
            )
        assert not list(tmp_path.rglob("run-*"))


class TestTheExperimentRevisionIsTheExperiments:
    """`experiment_git_sha` must describe the repository the experiment's code
    came from. The runner wrote the snapshot without saying where that was,
    so the snapshot read the working directory instead: a session started
    from a home folder, a data drive or another checkout recorded that
    folder's revision — or "not a source checkout" — for an experiment whose
    commit was sitting in its own repository all along.

    Each test builds a real repository under tmp_path holding the experiment
    code, then starts the session from a different folder, so a revision read
    from the working directory and one read from the code cannot agree."""

    @staticmethod
    def _git(repo, *args):
        # Identity per call, so the test neither needs nor writes git config.
        return subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def _commit_all(self, repo):
        """Commit everything in `repo`; the short SHA a clean tree records."""
        self._git(repo, "init", "-q")
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-q", "-m", "the experiment")
        return self._git(repo, "rev-parse", "--short", "HEAD")

    @staticmethod
    def _recorded(data_root):
        snapshot = next(data_root.rglob("config_snapshot.yaml"))
        provenance = yaml.safe_load(snapshot.read_text(encoding="utf-8"))["provenance"]
        return provenance["experiment_git_sha"]

    def test_a_task_is_described_by_the_repository_its_class_lives_in(self, tmp_path, monkeypatch):
        """The common case: `task=` a Task from an experiment package. Its
        class's source file is the experiment; that file's repository is the
        revision that ran."""
        repo = tmp_path / "experiment"
        shutil.copytree(
            EXAMPLES / "minimal_fixation", repo, ignore=shutil.ignore_patterns("__pycache__")
        )
        head = self._commit_all(repo)
        task_module = load_example_task(repo)
        elsewhere = tmp_path / "started-from-here"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        params = task_module.FixationParams(
            fixation_duration=Duration(ms=0),
            iti=Duration(ms=0),
            paradigm=SchedulerConfig(n_per_condition=1),
        )
        rig = RigConfig(
            monitor=MONITOR, display=DisplayConfig(backend="simulated"), data_root=tmp_path / "d"
        )
        build_session(
            rig=rig,
            subject="t01",
            session=1,
            run=1,
            task=task_module.MinimalFixationTask(params),
            seed=1,
            simulated_frame_period_s=0.0,
        ).run()

        assert self._recorded(tmp_path / "d") == head

    def test_without_a_task_the_trial_builder_names_the_experiment(self, tmp_path, monkeypatch):
        """A session wired piece by piece has no task class to ask, but its
        trial builder is still the experiment's own code."""
        repo = tmp_path / "experiment"
        repo.mkdir()
        (repo / "trials.py").write_text(
            "from alhazen.task.plan import TrialPlan\n"
            "from support import COMPLETED, RunForFrames\n"
            "\n"
            "def build_trial(setup):\n"
            "    return TrialPlan(phases=[RunForFrames(1, COMPLETED)])\n",
            encoding="utf-8",
        )
        head = self._commit_all(repo)
        spec = importlib.util.spec_from_file_location("experiment_trials", repo / "trials.py")
        assert spec is not None and spec.loader is not None
        trials = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(trials)
        elsewhere = tmp_path / "started-from-here"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        build(tmp_path / "d", EventSchema(()), build_trial=trials.build_trial).run()

        assert self._recorded(tmp_path / "d") == head
