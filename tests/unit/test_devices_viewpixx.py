"""The ViewPixx (TRACKPixx3) backend.

Two layers here, for two different reasons.

The *decisions* — what counts as a blink, which eye a sample carries, where
the calibration targets go — are free functions in the backend module, and
are tested directly. They are the parts that are silently wrong rather than
loudly broken when they are wrong.

The *lifecycle* is tested against a fake ``pypixxlib`` installed into
``sys.modules``. That is more than the EyeLink backend gets, and it is worth
it: unlike the EyeLink, this backend owns real bookkeeping of its own —
draining a ring buffer that overwrites itself, and moving the only copy of a
session's eye samples out of a scratch directory. Neither is device
behaviour, and neither should first run on a rig with a subject in it.
"""

from __future__ import annotations

import math
import sys
import types
from pathlib import Path

import pytest

from alhazen.config.models import EyeTrackerConfig
from alhazen.devices.eyetracker import EyeTracker, ViewPixxTracker, make_tracker
from alhazen.devices.eyetracker.viewpixx import (
    TRACKING_LOST_PX,
    calibration_targets,
    is_tracking_lost,
    select_eye,
)
from alhazen.errors import TrackerError
from alhazen.testing import FakeClock
from support import SCREEN


class FakeTrackPixx:
    """Stand-in for pypixxlib's TRACKPixx3, recording what it was asked to do.

    ``getEyePosition`` hands back whatever the test queued, in the device's
    own order and frame: [x_left, y_left, x_right, y_right], centered px.
    """

    def __init__(self) -> None:
        self.opened = False
        self.closed = False
        self.led_intensity: int | None = None
        self.eye_to_verify = 3  # pypixxlib's own default
        self.recording_folder: str | None = None
        self.samples_file: Path | None = None
        self.device_time = 100.0
        self.positions: list[float] = [0.0, 0.0, 0.0, 0.0]
        self.drains = 0
        self.calibration_points: list[tuple[float, float, int]] = []
        self.finished_calibration = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def setLEDintensity(self, value: int) -> None:  # noqa: N802 - vendor's name
        self.led_intensity = value

    def setUpDataRecording(self, folder: str) -> str:  # noqa: N802 - vendor's name
        # Mirrors pypixxlib: it picks the name itself, inside a data/
        # subdirectory of the folder it was given.
        self.recording_folder = folder
        data_dir = Path(folder) / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.samples_file = data_dir / "TPx_2026-08-27_09-00-00.csv"
        return str(self.samples_file)

    def saveBufferedData(self) -> None:  # noqa: N802 - vendor's name
        # The real one appends the device's newly-buffered samples; this one
        # appends a line per drain so a test can count them in the file.
        self.drains += 1
        assert self.samples_file is not None
        with self.samples_file.open("a") as f:
            f.write(f"drain {self.drains}\n")

    def getEyePosition(self):  # noqa: N802 - vendor's name
        return list(self.positions)

    def getTime(self) -> float:  # noqa: N802 - vendor's name
        return self.device_time

    def getEyePositionDuringCalib(self, x, y, eye):  # noqa: N802 - vendor's name
        self.calibration_points.append((x, y, eye))

    def finishCalibration(self) -> None:  # noqa: N802 - vendor's name
        self.finished_calibration = True


@pytest.fixture
def fake_pypixxlib(monkeypatch):
    """Install a fake pypixxlib for the duration of one test.

    The backend imports it inside connect(), so patching sys.modules is
    enough — and monkeypatch removes the entries again, so no other test
    inherits a fake SDK.
    """
    device = FakeTrackPixx()
    tracker_module = types.ModuleType("pypixxlib.tracker")
    tracker_module.TRACKPixx3 = lambda: device  # type: ignore[attr-defined]
    package = types.ModuleType("pypixxlib")
    package.tracker = tracker_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypixxlib", package)
    monkeypatch.setitem(sys.modules, "pypixxlib.tracker", tracker_module)
    return device


def make_viewpixx(clock=None, **cfg_kwargs) -> ViewPixxTracker:
    cfg = EyeTrackerConfig(backend="viewpixx", **cfg_kwargs)
    return ViewPixxTracker(cfg, None, SCREEN, clock or FakeClock())


def connected(clock=None, **cfg_kwargs) -> ViewPixxTracker:
    """A tracker that has been through the real connect/configure path."""
    tracker = make_viewpixx(clock, **cfg_kwargs)
    tracker.connect()
    tracker.configure(SCREEN, tracker._clock)
    return tracker


class TestTrackingLost:
    def test_the_sentinel_in_either_coordinate_is_lost(self):
        # A blink streams samples parked at ±9000, not an absence of samples
        # — treating one as a position records a gaze hundreds of degrees off
        # screen as if the subject looked there.
        assert is_tracking_lost(TRACKING_LOST_PX, TRACKING_LOST_PX)
        assert is_tracking_lost(500.0, TRACKING_LOST_PX)
        assert is_tracking_lost(-TRACKING_LOST_PX, 500.0)

    def test_beyond_the_sentinel_is_also_lost(self):
        # VPixx's own demos test `> 9000`, not `== 9000`.
        assert is_tracking_lost(9001.0, 0.0)

    def test_nan_is_lost(self):
        # What the device hands back when the calibration polynomial cannot
        # be evaluated at all. NaN compares false against every threshold, so
        # it has to be checked for by name or it slips through as a position.
        assert is_tracking_lost(math.nan, 0.0)
        assert is_tracking_lost(0.0, math.nan)

    def test_real_coordinates_are_not_lost(self):
        assert not is_tracking_lost(0.0, 0.0)
        assert not is_tracking_lost(-940.0, 530.0)


class TestSelectEye:
    POSITIONS = [10.0, 20.0, 30.0, 40.0]  # xL, yL, xR, yR

    def test_left_and_right_read_the_documented_order(self):
        # Getting this order wrong is invisible: both eyes return plausible
        # numbers, and the session records the wrong one all the way through.
        assert select_eye(self.POSITIONS, "left") == (10.0, 20.0)
        assert select_eye(self.POSITIONS, "right") == (30.0, 40.0)

    def test_average_is_the_midpoint(self):
        assert select_eye(self.POSITIONS, "average") == (20.0, 30.0)

    def test_a_lost_eye_is_none_only_when_it_is_the_chosen_one(self):
        lost_left = [TRACKING_LOST_PX, TRACKING_LOST_PX, 30.0, 40.0]
        assert select_eye(lost_left, "left") is None
        assert select_eye(lost_left, "right") == (30.0, 40.0)

    def test_average_needs_both_eyes(self):
        # Falling back to the tracked eye would change what the number means
        # partway through a trial, with nothing in the data saying where.
        lost_right = [10.0, 20.0, TRACKING_LOST_PX, TRACKING_LOST_PX]
        assert select_eye(lost_right, "average") is None

    def test_a_short_report_is_an_error_not_a_guess(self):
        with pytest.raises(TrackerError, match="expected 4"):
            select_eye([1.0, 2.0], "left")

    def test_an_unknown_eye_is_an_error(self):
        with pytest.raises(TrackerError, match="unknown eyetracker.eye"):
            select_eye(self.POSITIONS, "cyclopean")


class TestCalibrationTargets:
    def test_hv5_is_a_centred_plus(self):
        targets = calibration_targets("HV5", SCREEN, 1.0)
        half_w, half_h = SCREEN.width_px / 2, SCREEN.height_px / 2
        assert targets == [
            (0.0, 0.0),
            (0.0, half_h),
            (0.0, -half_h),
            (-half_w, 0.0),
            (half_w, 0.0),
        ]

    def test_the_centre_comes_first(self):
        # So the experimenter can confirm the subject is tracked at all
        # before the grid walks off to a corner.
        for kind in ("HV5", "HV9", "HV13"):
            assert calibration_targets(kind, SCREEN, 0.6)[0] == (0.0, 0.0)

    def test_counts_match_the_names(self):
        assert len(calibration_targets("HV5", SCREEN, 0.6)) == 5
        assert len(calibration_targets("HV9", SCREEN, 0.6)) == 9
        assert len(calibration_targets("HV13", SCREEN, 0.6)) == 13

    def test_every_target_is_distinct(self):
        for kind in ("HV5", "HV9", "HV13"):
            targets = calibration_targets(kind, SCREEN, 0.6)
            assert len(set(targets)) == len(targets)

    def test_area_scales_the_grid_and_keeps_it_on_screen(self):
        # calibration_area is the fraction of the screen the grid spans, the
        # same meaning the EyeLink backend sends to its Host PC.
        targets = calibration_targets("HV9", SCREEN, 0.5)
        assert max(abs(x) for x, _ in targets) == SCREEN.width_px / 4
        assert max(abs(y) for _, y in targets) == SCREEN.height_px / 4

    def test_an_unlayoutable_type_names_the_config_disagreement(self):
        # Config rejects these at load time; reaching here means the two
        # lists have drifted apart, and the message says so.
        with pytest.raises(TrackerError, match="config/models.py"):
            calibration_targets("HV3", SCREEN, 0.6)


class TestConnect:
    def test_missing_pypixxlib_names_the_vpixx_installer(self):
        if "pypixxlib" in sys.modules:  # pragma: no cover - rig only
            pytest.skip("pypixxlib is installed on this machine")
        tracker = make_viewpixx()
        with pytest.raises(TrackerError) as excinfo:
            tracker.connect()
        message = str(excinfo.value)
        assert "Software Tools" in message
        assert "NOT on PyPI" in message
        assert "mouse_sim" in message

    def test_a_device_fault_becomes_a_tracker_error(self, fake_pypixxlib, monkeypatch):
        # pypixxlib raises its own exception type, which cannot be named in
        # an except clause off the rig; whatever it is, it must reach the
        # experimenter as an actionable TrackerError.
        def explode() -> None:
            raise OSError("USB device not found")

        monkeypatch.setattr(fake_pypixxlib, "open", explode)
        with pytest.raises(TrackerError, match="DATAPixx3 is powered"):
            make_viewpixx().connect()

    def test_construction_does_not_import_the_sdk(self):
        # check-rig and session build both construct before they connect, and
        # the actionable error belongs at connect() time.
        tracker = make_tracker(EyeTrackerConfig(backend="viewpixx"), None, SCREEN, FakeClock())
        assert isinstance(tracker, ViewPixxTracker)
        assert not tracker.is_recording()

    def test_satisfies_the_protocol(self):
        assert isinstance(make_viewpixx(), EyeTracker)


class TestConfigure:
    def test_led_intensity_is_applied_only_when_the_rig_asked(self, fake_pypixxlib):
        connected()
        assert fake_pypixxlib.led_intensity is None
        connected(led_intensity=6)
        assert fake_pypixxlib.led_intensity == 6

    def test_recording_starts_in_a_scratch_directory(self, fake_pypixxlib):
        tracker = connected()
        # The run directory does not exist yet when the tracker is built, so
        # the device writes somewhere else and teardown moves the file.
        assert fake_pypixxlib.recording_folder is not None
        assert Path(fake_pypixxlib.recording_folder).is_dir()
        assert tracker._samples_path == fake_pypixxlib.samples_file


class TestGaze:
    def test_centered_device_px_become_screen_px(self, fake_pypixxlib):
        # The device reports centered px, y UP; a GazeSample is screen px, y
        # DOWN. A backend that skipped this conversion would put every gaze
        # in the wrong quadrant, and nothing downstream could tell.
        clock = FakeClock(start=2.5)
        tracker = connected(clock)
        fake_pypixxlib.positions = [100.0, 200.0, 0.0, 0.0]
        sample = tracker.get_gaze()
        assert sample is not None
        assert (sample.gx, sample.gy) == (
            SCREEN.width_px / 2 + 100.0,
            SCREEN.height_px / 2 - 200.0,
        )

    def test_gaze_is_stamped_on_the_session_clock(self, fake_pypixxlib):
        # Not the device's clock: every timed thing in a session is on one
        # clock, and getTime() would quietly introduce a second.
        clock = FakeClock(start=4.0)
        tracker = connected(clock)
        fake_pypixxlib.device_time = 999.0
        sample = tracker.get_gaze()
        assert sample is not None and sample.t == 4.0

    def test_a_blink_is_no_sample(self, fake_pypixxlib):
        tracker = connected()
        fake_pypixxlib.positions = [TRACKING_LOST_PX, TRACKING_LOST_PX, 0.0, 0.0]
        assert tracker.get_gaze() is None

    def test_the_configured_eye_is_the_one_read(self, fake_pypixxlib):
        tracker = connected(eye="right")
        fake_pypixxlib.positions = [100.0, 0.0, -100.0, 0.0]
        sample = tracker.get_gaze()
        assert sample is not None
        assert sample.gx == SCREEN.width_px / 2 - 100.0


class TestTrialLifecycle:
    def test_stop_trial_is_idempotent(self, fake_pypixxlib):
        # The runner calls this in a finally, so it can arrive on a trial
        # that never started recording — twice, even.
        tracker = connected()
        tracker.stop_trial()
        tracker.start_trial(1, "attempt 1")
        assert tracker.is_recording()
        tracker.stop_trial()
        tracker.stop_trial()
        assert not tracker.is_recording()

    def test_a_trial_that_never_started_does_not_drain(self, fake_pypixxlib):
        tracker = connected()
        tracker.stop_trial()
        assert fake_pypixxlib.drains == 0

    def test_every_trial_drains_the_ring_buffer(self, fake_pypixxlib):
        # The device's buffer is a fixed-size ring: a session longer than the
        # ring overwrites its own oldest samples unless it is drained as it
        # goes. Draining only at shutdown would lose data on a long session
        # and look completely normal doing it.
        tracker = connected()
        for index in range(3):
            tracker.start_trial(index, "ok")
            tracker.stop_trial()
        assert fake_pypixxlib.drains == 3

    def test_a_trial_marks_itself_in_the_message_record(self, fake_pypixxlib):
        tracker = connected()
        tracker.start_trial(7, "attempt 2")
        texts = [text for _, _, text in tracker._messages]
        assert texts == ["TRIAL 7 attempt 2", "EYE_USED left"]


class TestMessages:
    def test_a_message_is_stamped_on_both_clocks(self, fake_pypixxlib):
        # The device clock is what the sample file's timestamps are on; the
        # session clock is what every event and flip is on. Recording the
        # pair is the only thing that can align the two files afterwards.
        clock = FakeClock(start=3.0)
        tracker = connected(clock)
        fake_pypixxlib.device_time = 42.5
        tracker.send_message("stim_on")
        assert tracker._messages == [(42.5, 3.0, "stim_on")]

    def test_a_device_that_stops_answering_aborts_loudly(self, fake_pypixxlib, monkeypatch):
        # Invariant 6: a tracker that has stopped accepting messages means
        # the recording is losing its alignment marks, which must abort
        # rather than produce a session that only looks recorded.
        tracker = connected()

        def explode() -> float:
            raise OSError("link down")

        monkeypatch.setattr(fake_pypixxlib, "getTime", explode)
        with pytest.raises(OSError, match="link down"):
            tracker.send_message("stim_on")


class TestShutdown:
    def test_never_connected_is_a_no_op(self):
        # check-rig can construct without connecting, and teardown runs
        # regardless.
        make_viewpixx().shutdown(None)

    def test_the_recording_lands_in_the_run_directory(self, fake_pypixxlib, tmp_path):
        clock = FakeClock()
        tracker = connected(clock)
        tracker.start_trial(1, "ok")
        fake_pypixxlib.device_time = 5.0
        tracker.send_message("stim_on")
        tracker.stop_trial()

        destination = tmp_path / "sub-a_ses-001_run-01_task-t_20260827.edf"
        tracker.shutdown(destination)

        # The suffix belongs to the backend: a TRACKPixx3 writes CSV, so no
        # .edf is ever created, and the samples file is named like every
        # other file in the run directory.
        samples = tmp_path / "sub-a_ses-001_run-01_task-t_20260827_gaze.csv"
        messages = tmp_path / "sub-a_ses-001_run-01_task-t_20260827_gaze-messages.csv"
        assert not destination.exists()
        assert samples.exists()
        assert "drain" in samples.read_text()

        rows = messages.read_text().splitlines()
        assert rows[0] == "device_time_s,session_time_s,message"
        assert "5.0,0.0,stim_on" in rows

    def test_the_scratch_directory_is_cleaned_up(self, fake_pypixxlib, tmp_path):
        tracker = connected()
        scratch = tracker._scratch_dir
        assert scratch is not None and scratch.is_dir()
        tracker.shutdown(tmp_path / "run.edf")
        assert not scratch.exists()

    def test_an_open_trial_is_closed_and_drained_first(self, fake_pypixxlib, tmp_path):
        # A session can end mid-trial (quit, abort) and the buffered samples
        # from that trial are as real as any other trial's.
        tracker = connected()
        tracker.start_trial(1, "ok")
        tracker.shutdown(tmp_path / "run.edf")
        assert not tracker.is_recording()
        assert fake_pypixxlib.drains == 2  # the trial's, then teardown's

    def test_a_lost_recording_raises_rather_than_reporting_success(
        self, fake_pypixxlib, monkeypatch, tmp_path
    ):
        # The samples file is the only full-rate record of the session's eye
        # data. A teardown that "succeeded" without producing it is the exact
        # failure this backend must never have — so a device that buffered
        # nothing to write must not look like a clean session.
        tracker = connected()
        monkeypatch.setattr(fake_pypixxlib, "saveBufferedData", lambda: None)
        with pytest.raises(TrackerError) as excinfo:
            tracker.shutdown(tmp_path / "run.edf")
        assert "does not exist at teardown" in str(excinfo.value)
        assert "No eye samples were saved" in str(excinfo.value)
        # A DATAPixx3 left held blocks the next session from opening it, so
        # the link is dropped whatever happened to the files.
        assert fake_pypixxlib.closed

    def test_a_failed_delivery_keeps_what_it_could_save(
        self, fake_pypixxlib, monkeypatch, tmp_path
    ):
        # Two things must survive a delivery that raises: the messages, which
        # are held in memory and are gone forever if never written, and the
        # scratch directory, which may still hold the samples someone has to
        # go and rescue by hand.
        tracker = connected()
        tracker.send_message("stim_on")
        scratch = tracker._scratch_dir
        monkeypatch.setattr(fake_pypixxlib, "saveBufferedData", lambda: None)
        with pytest.raises(TrackerError):
            tracker.shutdown(tmp_path / "run.edf")
        assert (tmp_path / "run_gaze-messages.csv").exists()
        assert scratch is not None and scratch.is_dir()

    def test_no_destination_writes_nothing(self, fake_pypixxlib, tmp_path):
        # What check-rig does: a smoke test that opens and closes the device
        # without a run behind it.
        tracker = connected()
        tracker.shutdown(None)
        assert fake_pypixxlib.closed
        assert list(tmp_path.iterdir()) == []


class FakeWindow:
    """The one part of a psychopy Window the calibration routine touches."""

    def __init__(self) -> None:
        self.color = (0.0, 0.0, 0.0)
        self.flips = 0

    def flip(self) -> None:
        self.flips += 1


class FakeCircle:
    """A visual.Circle that only remembers where it was told to be."""

    def __init__(self, window, **kwargs) -> None:
        self.pos: tuple[float, float] = (0.0, 0.0)
        self.drawn_at: list[tuple[float, float]] = []

    def draw(self) -> None:
        self.drawn_at.append(self.pos)


@pytest.fixture
def fake_psychopy(monkeypatch):
    """A psychopy whose waitKeys replays a queued list of experimenter keys.

    Injected the same way as the fake pypixxlib: calibrate() imports psychopy
    inside the method, so replacing the modules is enough. This is what lets
    the accept/redo/abort walk over the target grid be tested with no window
    and no device — the walk is alhazen's own logic, not the vendor's.
    """
    keys: list[str] = []
    circles: list[FakeCircle] = []

    def wait_keys(keyList=None):  # noqa: N803 - psychopy's own parameter name
        return [keys.pop(0)] if keys else None

    event_module = types.ModuleType("psychopy.event")
    event_module.waitKeys = wait_keys  # type: ignore[attr-defined]

    def make_circle(window, **kwargs):
        circle = FakeCircle(window, **kwargs)
        circles.append(circle)
        return circle

    visual_module = types.ModuleType("psychopy.visual")
    visual_module.Circle = make_circle  # type: ignore[attr-defined]
    package = types.ModuleType("psychopy")
    package.event = event_module  # type: ignore[attr-defined]
    package.visual = visual_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psychopy", package)
    monkeypatch.setitem(sys.modules, "psychopy.event", event_module)
    monkeypatch.setitem(sys.modules, "psychopy.visual", visual_module)
    return types.SimpleNamespace(keys=keys, circles=circles)


class FakeDisplay:
    kind = "fake"

    def __init__(self) -> None:
        self.window = FakeWindow()


def calibrating(fake_pypixxlib, **cfg_kwargs) -> ViewPixxTracker:
    cfg = EyeTrackerConfig(backend="viewpixx", **cfg_kwargs)
    tracker = ViewPixxTracker(cfg, FakeDisplay(), SCREEN, FakeClock())
    tracker.connect()
    tracker.configure(SCREEN, FakeClock())
    return tracker


class TestCalibrationWalk:
    def test_calibration_needs_a_display(self, fake_pypixxlib):
        tracker = connected()  # built with display=None, as check-rig does
        with pytest.raises(TrackerError, match="needs an open display"):
            tracker.calibrate()

    def test_accepting_every_target_samples_each_one_and_fits(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, calibration_type="HV5")
        fake_psychopy.keys.extend(["space"] * 5)
        tracker.calibrate()

        # Each target is handed to the device in the frame it was drawn in —
        # both are the device's own centered px, so no conversion happens and
        # none should.
        expected = calibration_targets("HV5", SCREEN, tracker._cfg.calibration_area)
        assert [(x, y) for x, y, _ in fake_pypixxlib.calibration_points] == expected
        assert fake_pypixxlib.finished_calibration

    def test_redo_steps_back_to_the_previous_target(self, fake_pypixxlib, fake_psychopy):
        # The point an experimenter wants to redo is almost always the one
        # they just accepted, not the one still on screen.
        tracker = calibrating(fake_pypixxlib, calibration_type="HV5")
        fake_psychopy.keys.extend(
            ["space", "space", "backspace", "space", "space", "space", "space"]
        )
        tracker.calibrate()

        targets = calibration_targets("HV5", SCREEN, tracker._cfg.calibration_area)
        sampled = [(x, y) for x, y, _ in fake_pypixxlib.calibration_points]
        # Two accepted, then back to target 2 and forward through the rest.
        assert sampled == [targets[0], targets[1], targets[1], targets[2], targets[3], targets[4]]
        assert fake_pypixxlib.finished_calibration

    def test_redo_on_the_first_target_does_not_walk_off_the_grid(
        self, fake_pypixxlib, fake_psychopy
    ):
        tracker = calibrating(fake_pypixxlib, calibration_type="HV5")
        fake_psychopy.keys.extend(["backspace"] + ["space"] * 5)
        tracker.calibrate()
        assert len(fake_pypixxlib.calibration_points) == 5

    def test_abort_leaves_the_previous_calibration_alone(self, fake_pypixxlib, fake_psychopy):
        # The fit is only committed by finishCalibration(); aborting before it
        # means the device keeps whatever calibration it already had, which is
        # what an experimenter pressing escape for a subject break expects.
        tracker = calibrating(fake_pypixxlib, calibration_type="HV5")
        fake_psychopy.keys.extend(["space", "escape"])
        tracker.calibrate()
        assert len(fake_pypixxlib.calibration_points) == 1
        assert not fake_pypixxlib.finished_calibration

    def test_a_closed_window_aborts_rather_than_spinning(self, fake_pypixxlib, fake_psychopy):
        # waitKeys returns None if the window goes away. Treating that as
        # "accept" would fit the model to nothing; looping would hang the rig.
        tracker = calibrating(fake_pypixxlib, calibration_type="HV5")
        tracker.calibrate()
        assert fake_pypixxlib.calibration_points == []
        assert not fake_pypixxlib.finished_calibration
