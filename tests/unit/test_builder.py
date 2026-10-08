"""build_session's device wiring and the config cross-checks that need the
experiment's own event vocabulary."""

from __future__ import annotations

import csv
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from alhazen import CircleRegion, Model
from alhazen.config.loader import load_rig
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
from alhazen.errors import ConfigError, DataError
from alhazen.paradigms.base import Condition, SimpleSequence
from alhazen.paradigms.blocks import BlockPlan
from alhazen.paradigms.config import BlockConfig, SchedulerConfig, make_scheduler
from alhazen.session.builder import (
    build_session,
    make_gaze_input_provider,
    make_input_provider,
    make_tracker_health_check,
)
from alhazen.session.runner import host_overlay_shapes
from alhazen.task.plan import TrialPlan
from alhazen.testing import FakeClock
from support import (
    COMPLETED,
    MONITOR,
    SCREEN,
    TEST_EXPERIMENT,
    RunForFrames,
    load_example_task,
)

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
    # A session wired from parts has no task class to read a version from,
    # so it is given one (alhazen 2.0: every run is filed under a version).
    kwargs.setdefault("experiment_version", TEST_EXPERIMENT.version)
    return build_session(
        rig=rig,
        subject="t01",
        session=1,
        run=1,
        task_name="test-task",
        task_params=kwargs.pop("task_params", Params()),
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
        trials = next((tmp_path / "v0.1.0" / "sub-t01").rglob("*_trials.csv"))
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

        trials = next((tmp_path / "v0.1.0" / "sub-t01").rglob("*_trials.csv"))
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


def tree(root: Path) -> dict[str, bytes | None]:
    """Every file and folder under ``root``, with each file's bytes."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


class TestARefusedSessionLeavesTheDataRootAsItWas:
    """Every refusal at a session's start leaves nothing behind (docs/rigs.md
    §5). The checks that can only run after the run folder exists — the
    window's size, a device that will not connect — used to leave it there,
    empty, with its run number spent; now the build removes what it made.

    The data root already holds another subject's run and the registry, so
    "as it was" is a claim about something, not about an empty folder.
    """

    def seed(self, data_root: Path) -> dict[str, bytes | None]:
        from alhazen.data.paths import SessionPaths

        other = SessionPaths.create(
            data_root, "s99", 1, 1, "test-task", "20260801", experiment_version="0.1.0"
        )
        (other.run_dir / "config_snapshot.yaml").write_text("a run", encoding="utf-8")
        (data_root / "participants.tsv").write_text(
            "participant_id\tinitials\nsub-s99\tZZ\n", encoding="utf-8"
        )
        return tree(data_root)

    def test_a_window_refused_for_its_size(self, tmp_path, monkeypatch):
        # What PsychoPyDisplay.open does on a machine whose screen is not the
        # rig's: it opens the window, measures it, and refuses.
        from alhazen.errors import DisplayError
        from alhazen.modes.session import next_run
        from alhazen.session import builder as builder_module

        class WrongSizedScreen:
            kind = "psychopy"

            def __init__(self, monitor, windowed=False):
                self.window = None

            def open(self):
                self.window = object()
                raise DisplayError("the fullscreen drawing surface is 2880x1800 pixels")

            def close(self):
                self.window = None

        monkeypatch.setattr(builder_module, "PsychoPyDisplay", WrongSizedScreen)
        before = self.seed(tmp_path)

        with pytest.raises(DisplayError, match="2880x1800"):
            build(tmp_path, EventSchema(()), display=DisplayConfig(backend="psychopy"))

        assert tree(tmp_path) == before
        # The run number it would have had is still the next one.
        assert next_run(tmp_path, "t01", 1, experiment_version="0.1.0") == 1

    def test_a_tracker_that_will_not_connect_on_a_rig_with_a_recorder(self, tmp_path):
        # The recorder's pointer used to be written before the tracker was
        # connected, so this folder kept a file and its run number was
        # refused outright afterwards.
        from alhazen.config.models import RecordingConfig
        from alhazen.errors import TrackerError

        class Unplugged(ScriptedTracker):
            def connect(self):
                raise TrackerError("no tracker answers at 100.1.1.1")

        before = self.seed(tmp_path)

        with pytest.raises(TrackerError, match="no tracker answers"):
            build(
                tmp_path,
                EventSchema(()),
                tracker=Unplugged([], FakeClock()),
                rig_recording=RecordingConfig(backend="simulated"),
            )

        assert tree(tmp_path) == before

    def test_a_refusal_made_by_the_runners_constructor(self, tmp_path):
        before = self.seed(tmp_path)

        with pytest.raises(ConfigError, match="no eye tracker to validate"):
            build(
                tmp_path, EventSchema(()), task_params=ValidatingParams(), make_source=blocks_from
            )

        assert tree(tmp_path) == before

    def test_a_data_root_that_was_not_there_is_not_there_afterwards(self, tmp_path):
        data_root = tmp_path / "data"

        with pytest.raises(ValueError, match="the scheduler refused"):
            build(data_root, EventSchema(()), make_source=refusing_scheduler)

        assert not data_root.exists()

    def test_a_snapshot_that_cannot_be_written_removes_the_folder_too(self, tmp_path, monkeypatch):
        from alhazen.session import runner as runner_module

        def full_disk(*args, **kwargs):
            raise OSError(28, "No space left on device")

        before = self.seed(tmp_path)
        runner = build(tmp_path, EventSchema(()))
        monkeypatch.setattr(runner_module, "write_run_identity", full_disk)

        with pytest.raises(OSError, match="No space left"):
            runner.run()

        assert tree(tmp_path) == before

    def test_a_build_writes_no_file_and_the_pointer_follows_the_snapshot(self, tmp_path):
        from alhazen.config.models import RecordingConfig

        runner = build(
            tmp_path, EventSchema(()), rig_recording=RecordingConfig(backend="simulated")
        )
        (run_dir,) = tmp_path.glob("v0.1.0/sub-t01/ses-001/run-*")
        # Built, not yet run: the folder and its figures, no file at all.
        assert [p for p in run_dir.rglob("*") if p.is_file()] == []

        runner.run()

        pointer = run_dir / "recording_pointer.yaml"
        assert yaml.safe_load(pointer.read_text(encoding="utf-8"))["system"] == "simulated"
        manifest = yaml.safe_load((run_dir / "manifest.yaml").read_text(encoding="utf-8"))
        assert "recording_pointer.yaml" in {entry["path"] for entry in manifest["artifacts"]}


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
        # An experiment's repository declares its version, which files the
        # session's data (config/experiment.py); the example copied out of
        # alhazen's tree needs one of its own.
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "fixation-experiment"\nversion = "0.3.0"\n', encoding="utf-8"
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


class TestTheExperimentVersionFilesTheRun:
    """alhazen 2.0: build_session files the run under its experiment's
    version — found from the task's own project, given explicitly, or
    refused — and the run folder records how it was set up."""

    @staticmethod
    def experiment_project(tmp_path, version="0.3.0"):
        """The minimal-fixation example as an experiment repository of its
        own, with the pyproject.toml every experiment has."""
        repo = tmp_path / "experiment"
        shutil.copytree(
            EXAMPLES / "minimal_fixation", repo, ignore=shutil.ignore_patterns("__pycache__")
        )
        (repo / "pyproject.toml").write_text(
            f'[project]\nname = "fixation-experiment"\nversion = "{version}"\n',
            encoding="utf-8",
        )
        module = load_example_task(repo)
        params = module.FixationParams(
            fixation_duration=Duration(ms=0),
            iti=Duration(ms=0),
            paradigm=SchedulerConfig(n_per_condition=1),
        )
        return repo, module.MinimalFixationTask(params)

    @staticmethod
    def session(tmp_path, task, **kwargs):
        kwargs.setdefault(
            "rig",
            RigConfig(
                monitor=MONITOR,
                display=DisplayConfig(backend="simulated"),
                data_root=tmp_path / "data",
            ),
        )
        return build_session(
            subject="01",
            session=1,
            run=1,
            task=task,
            seed=1,
            simulated_frame_period_s=0.0,
            date_yyyymmdd="20260826",
            **kwargs,
        )

    def test_the_version_comes_from_the_tasks_own_pyproject(self, tmp_path):
        _repo, task = self.experiment_project(tmp_path, version="0.3.0")

        self.session(tmp_path, task).run()

        run_dir = (
            tmp_path / "data" / "v0.3.0" / "sub-01" / "ses-001" / "run-01_task-minimal-fixation"
        )
        snapshot = yaml.safe_load((run_dir / "config_snapshot.yaml").read_text(encoding="utf-8"))
        provenance = snapshot["provenance"]
        assert provenance["experiment_name"] == "fixation-experiment"
        assert provenance["experiment_version"] == "0.3.0"
        assert provenance["experiment_version_source"] == "pyproject.toml"

    def test_what_spans_versions_stays_at_the_unversioned_root(self, tmp_path):
        # A subject spans versions of an experiment: the registry and the
        # database sit above the v<version>/ folders, not in one of them.
        _repo, task = self.experiment_project(tmp_path, version="0.3.0")

        self.session(tmp_path, task).run()

        data = tmp_path / "data"
        assert sorted(p.name for p in data.iterdir() if p.is_dir()) == ["v0.3.0"]
        assert (data / "participants.tsv").is_file()
        assert (data / "experiment.sqlite3").is_file()
        assert not list((data / "v0.3.0").rglob("participants.tsv"))
        assert not list((data / "v0.3.0").rglob("experiment.sqlite3"))

    def test_an_explicit_version_wins_over_the_pyproject(self, tmp_path):
        _repo, task = self.experiment_project(tmp_path, version="0.3.0")

        runner = self.session(tmp_path, task, experiment_version="9.9", experiment_name="other")
        runner.run()

        (snapshot,) = (tmp_path / "data").rglob("config_snapshot.yaml")
        assert snapshot.relative_to(tmp_path / "data").parts[0] == "v9.9"
        provenance = yaml.safe_load(snapshot.read_text(encoding="utf-8"))["provenance"]
        assert provenance["experiment_name"] == "other"
        assert provenance["experiment_version_source"] == "given to build_session"

    def test_a_session_wired_from_parts_without_a_version_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="experiment_version="):
            build(tmp_path, EventSchema(()), experiment_version=None)
        # Refused before anything was made.
        assert list(tmp_path.iterdir()) == []

    def test_a_version_that_cannot_name_a_folder_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="cannot name a data folder"):
            build(tmp_path, EventSchema(()), experiment_version="1.0/../../x")
        assert list(tmp_path.iterdir()) == []

    def test_two_answers_to_which_experiment_are_refused(self, tmp_path):
        from alhazen.config.experiment import Experiment

        found = Experiment("e", "1.0", "pyproject.toml", None)
        with pytest.raises(ValueError, match="not both"):
            build(tmp_path, EventSchema(()), experiment=found, experiment_version="2.0")

    def test_the_files_it_was_started_with_are_copied_byte_for_byte(self, tmp_path):
        repo, task = self.experiment_project(tmp_path)
        rig_file = repo / "rig-sim.yaml"
        params_file = repo / "task.yaml"
        # Comments and line endings a re-dump would lose.
        rig_file.write_bytes(
            rig_file.read_bytes().replace(b"data_root: data", b"data_root: data  # here")
        )
        rig = load_rig(rig_file).model_copy(update={"data_root": tmp_path / "data"})

        self.session(
            tmp_path,
            task,
            rig=rig,
            sources={"rig": str(rig_file), "task": str(params_file)},
            mode="run",
        ).run()

        (run_dir,) = (p.parent for p in (tmp_path / "data").rglob("session.json"))
        assert (run_dir / "rig.yaml").read_bytes() == rig_file.read_bytes()
        assert (run_dir / "params.yaml").read_bytes() == params_file.read_bytes()
        card = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        assert card["mode"] == "run"
        assert card["rig"]["file"] == str(rig_file.resolve())
        assert card["params_file"] == str(params_file.resolve())
        assert card["experiment"]["version"] == "0.3.0"

    def test_a_rig_given_as_a_path_is_the_file_copied(self, tmp_path, monkeypatch):
        repo, task = self.experiment_project(tmp_path)
        monkeypatch.chdir(tmp_path)  # the rig's `data_root: data` is relative

        self.session(tmp_path, task, rig=repo / "rig-sim.yaml").run()

        (run_dir,) = (p.parent for p in (tmp_path / "data").rglob("session.json"))
        assert (run_dir / "rig.yaml").read_bytes() == (repo / "rig-sim.yaml").read_bytes()
        # No params file was named, so none is copied, and the card says so.
        assert not (run_dir / "params.yaml").exists()
        card = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        assert card["params_file"] is None and card["files"]["params"] is None
        assert card["mode"] is None  # built directly, not through a mode

    def test_the_rig_file_that_was_loaded_wins_over_what_sources_say(self, tmp_path, monkeypatch):
        # The path handed in is what the builder loaded; a sources entry is
        # the caller's say-so, and copying it would record another file.
        repo, task = self.experiment_project(tmp_path)
        other = tmp_path / "rig-other.yaml"
        other.write_text("# not the rig that ran\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        self.session(tmp_path, task, rig=repo / "rig-sim.yaml", sources={"rig": str(other)}).run()

        (run_dir,) = (p.parent for p in (tmp_path / "data").rglob("session.json"))
        assert (run_dir / "rig.yaml").read_bytes() == (repo / "rig-sim.yaml").read_bytes()

    def test_the_command_is_recorded_with_run_py_relative_to_the_experiment(
        self, tmp_path, monkeypatch
    ):
        # What run.py hands down: its own path as the process started it
        # (absolute, as the experiment workspace launches it), then the
        # arguments as parsed. The record names run.py as a person would type
        # it from the experiment's folder, and leaves the arguments alone.
        repo, task = self.experiment_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        argv = ["--mode", "run", "--sub", "01", "--params", str(repo / "task.yaml")]

        self.session(
            tmp_path, task, rig=repo / "rig-sim.yaml", command=[str(repo / "run.py"), *argv]
        ).run()

        (run_dir,) = (p.parent for p in (tmp_path / "data").rglob("session.json"))
        card = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        snapshot = yaml.safe_load((run_dir / "config_snapshot.yaml").read_text(encoding="utf-8"))
        assert card["command"] == snapshot["command"] == ["run.py", *argv]

    def test_a_session_built_in_code_records_no_command(self, tmp_path, monkeypatch):
        repo, task = self.experiment_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        self.session(tmp_path, task, rig=repo / "rig-sim.yaml").run()

        (run_dir,) = (p.parent for p in (tmp_path / "data").rglob("session.json"))
        card = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        snapshot = yaml.safe_load((run_dir / "config_snapshot.yaml").read_text(encoding="utf-8"))
        assert card["command"] is None and snapshot["command"] is None
        # A whole rig file needs no merged copy: rig.yaml is the whole rig.
        assert not (run_dir / "rig-merged.yaml").exists()
        assert card["files"]["rig_merged"] is None

    def test_a_rig_that_extends_a_shared_one_is_also_recorded_whole(self, tmp_path):
        # rig.yaml is the experiment's half, byte for byte; rig-merged.yaml
        # is the rig the session loaded, readable without the alhazen that
        # shipped the other half.
        repo, task = self.experiment_project(tmp_path)
        rig_file = repo / "rig-laptop.yaml"
        rig_file.write_text(
            "# only what this experiment does differently on the laptop\n"
            "extends: laptop\n"
            "display:\n  backend: simulated\n"
            "live_monitor:\n  enabled: false\n"
            f"data_root: {(tmp_path / 'data').as_posix()}\n",
            encoding="utf-8",
        )

        self.session(tmp_path, task, rig=rig_file).run()

        (run_dir,) = (p.parent for p in (tmp_path / "data").rglob("session.json"))
        assert (run_dir / "rig.yaml").read_bytes() == rig_file.read_bytes()
        assert load_rig(run_dir / "rig-merged.yaml").model_dump() == load_rig(rig_file).model_dump()
        card = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        assert card["files"]["rig_merged"] == "rig-merged.yaml"
        # And the manifest vouches for it like every other file.
        manifest = yaml.safe_load((run_dir / "manifest.yaml").read_text(encoding="utf-8"))
        assert "rig-merged.yaml" in {entry["path"] for entry in manifest["artifacts"]}

    def test_a_database_from_a_newer_alhazen_is_refused_before_the_run_folder(self, tmp_path):
        # 2.0.1: one from an OLDER schema is moved aside instead (below); a
        # newer one is still refused, and still before anything is made.
        import sqlite3

        from alhazen.session.database import SCHEMA_VERSION

        with sqlite3.connect(tmp_path / "experiment.sqlite3") as db:
            db.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")
            db.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION + 1,))

        with pytest.raises(DataError, match=f"schema version {SCHEMA_VERSION + 1}"):
            build(tmp_path, EventSchema(()))
        assert not list(tmp_path.rglob("run-*"))

    def test_a_database_from_before_2_0_is_moved_aside_and_the_session_builds(self, tmp_path):
        import sqlite3
        from contextlib import closing

        # Closed, not just committed: an open handle locks the file on Windows.
        with closing(sqlite3.connect(tmp_path / "experiment.sqlite3")) as db, db:
            db.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")
            db.execute("INSERT INTO schema_info(version) VALUES (2)")

        build(tmp_path, EventSchema(()))

        assert (tmp_path / "experiment.schema2.sqlite3").is_file()
        assert list(tmp_path.rglob("run-*")), "the session built its run folder"


class TestTheSubjectsInitials:
    """alhazen 2.0: build_session records the subject's initials, checks
    them against the registry before anything is written, and never puts
    them in a path."""

    def test_they_are_recorded_in_the_snapshot_the_card_and_the_registry(self, tmp_path):
        build(tmp_path, EventSchema(()), initials="hd").run()

        (run_dir,) = (p.parent for p in tmp_path.rglob("session.json"))
        snapshot = yaml.safe_load((run_dir / "config_snapshot.yaml").read_text(encoding="utf-8"))
        assert snapshot["config"]["info"]["initials"] == "HD"
        card = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        # Age and sex beside them (session.identity.SubjectDemographics):
        # null, "not recorded", when the session was given none.
        assert card["subject"] == {"id": "t01", "initials": "HD", "age": None, "sex": None}
        registry = (tmp_path / "participants.tsv").read_text(encoding="utf-8").splitlines()
        assert registry == ["participant_id\tinitials", "sub-t01\tHD"]

    def test_they_are_in_no_file_or_folder_name(self, tmp_path):
        # Unusual letters, so a match anywhere could only be the initials.
        build(tmp_path, EventSchema(()), initials="QZX").run()

        names = [p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")]
        assert names  # the session did write its files
        assert not [name for name in names if "qzx" in name.lower()]

    def test_other_initials_for_a_recorded_subject_are_refused_before_anything(self, tmp_path):
        registry = tmp_path / "participants.tsv"
        registry.write_text("participant_id\tinitials\nsub-t01\tHD\n", encoding="utf-8")
        before = registry.read_bytes()

        with pytest.raises(DataError) as refused:
            build(tmp_path, EventSchema(()), initials="XY")

        assert str(refused.value).startswith(
            "sub-t01 is recorded as HD; this session says XY — check the subject number"
        )
        # No run folder, no database, and the registry as it was.
        assert [p.name for p in tmp_path.iterdir()] == ["participants.tsv"]
        assert registry.read_bytes() == before

    def test_a_subject_from_before_2_0_gets_them_filled_in(self, tmp_path):
        registry = tmp_path / "participants.tsv"
        registry.write_text("participant_id\nsub-t01\n", encoding="utf-8")

        build(tmp_path, EventSchema(()), initials="HD").run()

        assert registry.read_text(encoding="utf-8").splitlines() == [
            "participant_id\tinitials",
            "sub-t01\tHD",
        ]

    def test_a_session_given_none_records_none(self, tmp_path):
        build(tmp_path, EventSchema(())).run()

        (card_path,) = tmp_path.rglob("session.json")
        assert json.loads(card_path.read_text(encoding="utf-8"))["subject"]["initials"] is None
        assert (tmp_path / "participants.tsv").read_text(encoding="utf-8").splitlines() == [
            "participant_id",
            "sub-t01",
        ]

    def test_a_registry_changed_after_the_build_is_checked_again_at_the_start(self, tmp_path):
        # A built session can wait while another one registers the subject:
        # the runner checks once more, before it writes anything.
        runner = build(tmp_path, EventSchema(()), initials="HD")
        registry = tmp_path / "participants.tsv"
        registry.write_text("participant_id\tinitials\nsub-t01\tXY\n", encoding="utf-8")
        before = registry.read_bytes()

        with pytest.raises(DataError, match="recorded as XY; this session says HD"):
            runner.run()

        # Nothing written, and the folders the build made for the run are
        # gone again (they used to be left, empty, with the run number
        # spent): the data root holds the registry it was given, unchanged.
        assert [p.name for p in tmp_path.iterdir()] == ["participants.tsv"]
        assert registry.read_bytes() == before


class ValidatingParams(Model):
    """Params that ask for the eye tracker to be validated after every break,
    under the usual field name."""

    paradigm: SchedulerConfig = SchedulerConfig(
        kind="sequence",
        blocks=BlockConfig(n_blocks=2, validate_after_break=True),
    )


def blocks_from(params, rng):
    """A make_source that builds its scheduler from the params' own paradigm,
    as the default Task.make_source does."""
    return make_scheduler(params.paradigm, [Condition({"c": "a"})], rng)


def blocks_that_forget(params, rng):
    """A make_source that orders its blocks itself and builds its BlockPlan
    without passing the params' validate_after_break on — what an experiment
    with its own block order did."""
    return BlockPlan([SimpleSequence([Condition({"c": "a"})], rng=rng) for _ in range(2)], rng=rng)


class TestValidationAfterBreaksIsWiredOrRefused:
    """`blocks.validate_after_break` must end every break with a validation,
    or the session must not start: the pilot ran a design that validates
    between blocks, and nothing made it happen or said it had not."""

    def test_with_a_tracker_every_break_ends_with_a_validation(self, tmp_path):
        # The builder's own wiring end to end: the monitor it builds for the
        # tracker, on the simulated display — unattended, so the break
        # resumes at once and its validation advances by itself.
        clock = FakeClock()
        gaze = GazeSample(gx=960.0, gy=540.0, t=0.0)
        runner = build(
            tmp_path,
            EventSchema(()),
            task_params=ValidatingParams(),
            make_source=blocks_from,
            tracker=ScriptedTracker([(0.0, gaze)], clock),
            clock=clock,
        )
        runner.run()

        events = read_table(tmp_path, "events")
        names = [row["event"] for row in events]
        assert names.count("PAUSED") == 1 and names.count("RESUMED") == 1
        assert names.index("PAUSED") < names.index("VALIDATION") < names.index("RESUMED")
        assert len(read_table(tmp_path, "trials")) == 2

    def test_without_an_eye_tracker_the_session_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="no eye tracker to validate") as refused:
            build(
                tmp_path, EventSchema(()), task_params=ValidatingParams(), make_source=blocks_from
            )
        # It names the ways on: a rig with one, the stand-ins, or the switch.
        message = str(refused.value)
        assert "devices.eyetracker" in message and "--mouse" in message
        assert "validate_after_break: false" in message

    def test_a_scheduler_that_drops_the_request_is_refused_naming_it(self, tmp_path):
        clock = FakeClock()
        with pytest.raises(ConfigError, match="paradigm.blocks.validate_after_break") as refused:
            build(
                tmp_path,
                EventSchema(()),
                task_params=ValidatingParams(),
                make_source=blocks_that_forget,
                tracker=ScriptedTracker([], clock),
                clock=clock,
            )
        assert "must pass the setting on" in str(refused.value)

    def test_params_that_ask_for_nothing_are_not_checked(self, tmp_path):
        # The request comes from the params; a scheduler without the flag,
        # with params that never asked, is every session before this one.
        runner = build(tmp_path, EventSchema(()), make_source=blocks_that_forget)
        runner.run()
        assert len(read_table(tmp_path, "trials")) == 2
