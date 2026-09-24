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

import ctypes
import logging
import math
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

from alhazen.config.models import EyeTrackerConfig
from alhazen.devices.eyetracker import EyeTracker, ViewPixxTracker, make_tracker
from alhazen.devices.eyetracker import viewpixx as viewpixx_module
from alhazen.devices.eyetracker.guide import GUIDE_TITLE
from alhazen.devices.eyetracker.viewpixx import (
    AUTO_SETTLE_S,
    AUTO_STEADY_REFRESHES,
    GAZE_STALE_S,
    HOST_DEVICE,
    STATUS_REFRESH_S,
    TRACKING_LOST_PX,
    GazeReader,
    calibration_targets,
    evaluate_calibration,
    eye_in_raw,
    eye_in_view,
    image_from_pointer,
    is_tracking_lost,
    quietly,
    select_eye,
    shrink_image,
)
from alhazen.display.palette import TERMINAL_GREEN
from alhazen.errors import TrackerError
from alhazen.testing import FakeClock
from fake_sdk import FakeLibdpx, FakeTrackPixx, install_fake_pypixxlib
from support import SCREEN


@pytest.fixture
def fake_pypixxlib(monkeypatch) -> FakeTrackPixx:
    """Install a fake pypixxlib for the duration of one test (fake_sdk.py,
    shared with the check-rig and session tests that run this backend)."""
    return install_fake_pypixxlib(monkeypatch)


def make_viewpixx(clock=None, *, background_gaze=False, **cfg_kwargs) -> ViewPixxTracker:
    # No reader thread by default: a test that queues a gaze report wants
    # get_gaze() to read exactly that, on its own thread, with no race.
    cfg = EyeTrackerConfig(backend="viewpixx", **cfg_kwargs)
    return ViewPixxTracker(cfg, None, SCREEN, clock or FakeClock(), background_gaze=background_gaze)


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
    def test_missing_pypixxlib_names_the_vpixx_archive(self, monkeypatch):
        # None in sys.modules makes the import fail wherever this runs — on
        # the rig too, where the real package is installed and connect()
        # would otherwise open the real device from inside a unit test.
        monkeypatch.setitem(sys.modules, "pypixxlib", None)
        tracker = make_viewpixx()
        with pytest.raises(TrackerError) as excinfo:
            tracker.connect()
        message = str(excinfo.value)
        assert "Software Tools" in message
        assert "NOT on PyPI" in message
        assert "pypixxlib-<version>.tar.gz" in message
        assert "pip install" in message
        assert "mouse_sim" in message

    def test_a_device_fault_becomes_a_tracker_error(self, fake_pypixxlib, monkeypatch):
        # pypixxlib raises its own exception type, which cannot be named in
        # an except clause off the rig; whatever it is, it must reach the
        # experimenter as an actionable TrackerError.
        def explode() -> None:
            raise OSError("USB device not found")

        monkeypatch.setattr(sys.modules["pypixxlib.tracker"], "TRACKPixx3", explode)
        with pytest.raises(TrackerError, match="DATAPixx3 is powered"):
            make_viewpixx().connect()

    def test_wakes_the_tracker_with_the_datapixx3_selected(self, fake_pypixxlib):
        # pypixxlib's own open() is never called: it leaves the camera
        # controller selected and then writes a DATAPixx3 register (see
        # wake_tracker). The constructor opens the link; connect() puts the
        # selection where the tracker's registers are and wakes it.
        make_viewpixx().connect()
        libdpx = fake_pypixxlib.libdpx
        assert not fake_pypixxlib.opened
        assert libdpx.selected == HOST_DEVICE == "DATAPIXX3"
        assert libdpx.overlay_hidden
        assert libdpx.awake
        assert libdpx.cache_updates >= 1

    def test_a_libdpx_fault_flag_becomes_a_tracker_error(self, fake_pypixxlib):
        # libdpx's free functions do not raise: they set a sticky flag and
        # carry on. Left unread, a failed wake-up would surface later as a
        # session that recorded nothing.
        libdpx = fake_pypixxlib.libdpx
        libdpx.error = "DPX_ERR_SETREG16_ADDR_RANGE"
        libdpx.error_string = "DPxSetReg16 passed an address which was out of range"
        with pytest.raises(TrackerError, match="DPX_ERR_SETREG16_ADDR_RANGE") as excinfo:
            make_viewpixx().connect()
        assert "out of range" in str(excinfo.value)
        assert "DATAPixx3 is powered" in str(excinfo.value)
        assert libdpx.error == "DPX_SUCCESS"  # cleared: not reported twice

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

    def test_a_failed_last_drain_still_delivers_what_was_saved(
        self, fake_pypixxlib, monkeypatch, tmp_path
    ):
        # The bug this pins: the last drain ran outside the delivery's try, so
        # a device that stopped answering at the end of a session left every
        # earlier trial's samples in the temp folder and never wrote the
        # message record at all — while the drain's error was all anyone saw.
        tracker = connected()
        tracker.start_trial(1, "ok")
        fake_pypixxlib.device_time = 5.0
        tracker.send_message("stim_on")
        tracker.stop_trial()  # drain 1: trial 1's samples are on disk

        def link_down() -> None:
            raise OSError("link down")

        monkeypatch.setattr(fake_pypixxlib, "saveBufferedData", link_down)
        with pytest.raises(TrackerError, match=r"last drain failed at teardown \(link down\)"):
            tracker.shutdown(tmp_path / "run.edf")

        # Still loud, and what had been saved reached the run directory.
        assert "drain 1" in (tmp_path / "run_gaze.csv").read_text()
        assert "stim_on" in (tmp_path / "run_gaze-messages.csv").read_text()
        assert fake_pypixxlib.closed

    def test_a_failed_drain_is_logged_when_the_delivery_fails_too(
        self, fake_pypixxlib, monkeypatch, tmp_path, caplog
    ):
        # Nothing was ever drained, so there is no samples file to deliver:
        # the delivery's error propagates, and the drain's is not lost.
        tracker = connected()
        tracker.send_message("stim_on")

        def link_down() -> None:
            raise OSError("link down")

        monkeypatch.setattr(fake_pypixxlib, "saveBufferedData", link_down)
        with caplog.at_level(logging.ERROR), pytest.raises(TrackerError) as excinfo:
            tracker.shutdown(tmp_path / "run.edf")
        assert "does not exist at teardown" in str(excinfo.value)
        assert "last drain had already failed before the delivery did: link down" in caplog.text
        assert (tmp_path / "run_gaze-messages.csv").exists()
        assert fake_pypixxlib.closed

    def test_a_failed_drain_with_no_destination_still_cleans_up(self, fake_pypixxlib, monkeypatch):
        # check-rig's case: the drain failure is still raised, and the
        # scratch recording is still removed rather than left in temp.
        tracker = connected()
        scratch = tracker._scratch_dir

        def link_down() -> None:
            raise OSError("link down")

        monkeypatch.setattr(fake_pypixxlib, "saveBufferedData", link_down)
        with pytest.raises(TrackerError, match="No recording was asked for"):
            tracker.shutdown(None)
        assert scratch is not None and not scratch.exists()
        assert fake_pypixxlib.closed

    def test_no_destination_writes_nothing(self, fake_pypixxlib, tmp_path):
        # What check-rig does: a smoke test that opens and closes the device
        # without a run behind it.
        tracker = connected()
        tracker.shutdown(None)
        assert fake_pypixxlib.closed
        assert list(tmp_path.iterdir()) == []


class FakeWindow:
    """The parts of a psychopy Window the calibration routine touches."""

    def __init__(self) -> None:
        self.color = (0.0, 0.0, 0.0)
        self.flips = 0
        self._closed = False
        # What was put on this window, in order: "guide" for each guide
        # panel, "target" for each calibration target. The panel comes from
        # the display and the target from a Circle, so the one thing they
        # share — this window — is where the order can be read back.
        self.timeline: list[str] = []

    def flip(self) -> None:
        self.flips += 1


class FakeCircle:
    """A visual.Circle that only remembers where it was told to be."""

    def __init__(self, window, **kwargs) -> None:
        self.window = window
        self.pos: tuple[float, float] = (0.0, 0.0)
        self.drawn_at: list[tuple[float, float]] = []

    def draw(self) -> None:
        self.drawn_at.append(self.pos)
        self.window.timeline.append("target")


@pytest.fixture
def fake_psychopy(monkeypatch):
    """A psychopy whose waitKeys replays a queued list of experimenter keys.

    Injected the same way as the fake pypixxlib: calibrate() imports psychopy
    inside the method, so replacing the modules is enough. This is what lets
    the accept/redo/abort walk over the target grid be tested with no window
    and no device — the walk is alhazen's own logic, not the vendor's.
    """
    keys: list[str | None] = []
    circles: list[FakeCircle] = []
    texts: list[FakeText] = []
    windows: list[FakeWindow] = []
    # The session clock the walk reads; every wait moves it one refresh on,
    # the way a real waitKeys(maxWait=STATUS_REFRESH_S) spends that long.
    clocks: list[FakeClock] = []

    # Presses that arrived while the walk was busy between two waits. PsychoPy
    # keeps them in its buffer until something empties it: waitKeys does when
    # it starts waiting (unless told not to), and so does clearEvents.
    buffered: list[str] = []
    clears: list[str | None] = []
    waits: list[bool] = []  # each wait's clearEvents argument

    def clear_events(eventType=None):  # noqa: N803 - psychopy's own parameter name
        clears.append(eventType)
        buffered.clear()

    def wait_keys(maxWait=None, keyList=None, clearEvents=True):  # noqa: N803 - psychopy's names
        waits.append(clearEvents)
        if clearEvents:
            buffered.clear()
        if buffered:
            return [buffered.pop(0)]
        for clock in clocks:
            clock.advance(STATUS_REFRESH_S)
        if keys:
            # None queued means "no key this refresh" — the window is still
            # open, nobody pressed anything, and the walk decides for itself.
            key = keys.pop(0)
            return None if key is None else [key]
        # Out of keys: the window has gone away, as far as the walk can tell.
        # Without this a walk that waits with a timeout would spin forever.
        for window in windows:
            window._closed = True
        return None

    event_module = types.ModuleType("psychopy.event")
    event_module.waitKeys = wait_keys  # type: ignore[attr-defined]
    event_module.clearEvents = clear_events  # type: ignore[attr-defined]

    def make_circle(window, **kwargs):
        if window not in windows:
            windows.append(window)
        circle = FakeCircle(window, **kwargs)
        circles.append(circle)
        return circle

    def make_text(window, **kwargs):
        text = FakeText(window, **kwargs)
        texts.append(text)
        return text

    visual_module = types.ModuleType("psychopy.visual")
    visual_module.Circle = make_circle  # type: ignore[attr-defined]
    visual_module.TextStim = make_text  # type: ignore[attr-defined]
    package = types.ModuleType("psychopy")
    package.event = event_module  # type: ignore[attr-defined]
    package.visual = visual_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psychopy", package)
    monkeypatch.setitem(sys.modules, "psychopy.event", event_module)
    monkeypatch.setitem(sys.modules, "psychopy.visual", visual_module)
    return types.SimpleNamespace(
        keys=keys,
        circles=circles,
        texts=texts,
        windows=windows,
        clocks=clocks,
        buffered=buffered,
        clears=clears,
        waits=waits,
    )


class FakeText:
    """A visual.TextStim that only remembers what it was told to say."""

    def __init__(self, window, **kwargs) -> None:
        self.text = kwargs.get("text", "")
        self.shown: list[str] = []

    def draw(self) -> None:
        self.shown.append(self.text)


class FakeDisplay:
    kind = "fake"

    def __init__(self) -> None:
        self.window = FakeWindow()
        self.messages: list[str] = []
        # (title, body, colour) of every menu-style panel — the guide.
        self.menus: list[tuple[str, str, tuple[float, float, float]]] = []

    # Deliberately without `reflow`: a display backend written outside
    # alhazen before the argument existed. The tracker must never pass it.
    def show_message(self, text: str) -> None:
        self.messages.append(text)

    def show_menu(self, title: str, body: str, *, color: tuple[float, float, float]) -> None:
        self.menus.append((title, body, color))
        self.window.timeline.append("guide")


def calibrating(fake_pypixxlib, fake_psychopy=None, **cfg_kwargs) -> ViewPixxTracker:
    """A connected tracker with a display, ready to calibrate.

    With the psychopy fake passed in, its window and clock are registered so
    that running out of queued keys closes the window and every wait moves
    the session clock on — what the guide loop and auto advance need.
    """
    cfg = EyeTrackerConfig(backend="viewpixx", **cfg_kwargs)
    clock = FakeClock()
    display = FakeDisplay()
    tracker = ViewPixxTracker(cfg, display, SCREEN, clock, background_gaze=False)
    tracker.connect()
    tracker.configure(SCREEN, clock)
    if fake_psychopy is not None:
        fake_psychopy.windows.append(display.window)
        fake_psychopy.clocks.append(clock)
    return tracker


def guide_body(tracker: ViewPixxTracker) -> str:
    """The last guide panel the display drew."""
    menus = tracker._display.menus  # type: ignore[union-attr]
    assert menus, "the calibration guide was never shown"
    title, body, color = menus[-1]
    assert title == GUIDE_TITLE
    assert color == TERMINAL_GREEN
    return body


# The guide screen comes first, and SPACE there starts the walk.
START = "space"


class TestCalibrationGuide:
    def test_the_guide_is_shown_before_the_first_target(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV9", eye="right")
        fake_psychopy.keys.extend([START] + ["space"] * 9)
        tracker.calibrate()
        body = guide_body(tracker)
        # It says which eye the session reads, what the walk is and how it
        # advances, and what the keys do — the questions an experimenter has
        # before the first target, in one place.
        assert "RIGHT eye read by the session" in body
        assert "both eyes are calibrated" in body
        assert "HV9 — 9 targets" in body
        assert "MANUAL" in body and "press SPACE" in body
        assert "BACKSPACE" in body and "ESC" in body
        # And it came first: every guide panel went on the window before the
        # first target did, so a subject never sees a target they were not
        # told about.
        timeline = tracker._display.window.timeline  # type: ignore[union-attr]
        assert "target" in timeline and timeline[0] == "guide"
        assert all(entry == "guide" for entry in timeline[: timeline.index("target")])

    def test_the_guide_shows_the_live_eye_line(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_pypixxlib.libdpx.pupils = (3.0, 2.0, 0.0, 0.0)
        # Two refreshes at the guide, then start.
        fake_psychopy.keys.extend([None, START] + ["space"] * 5)
        tracker.calibrate()
        menus = tracker._display.menus  # type: ignore[union-attr]
        assert len(menus) == 2  # redrawn every refresh while it waits
        assert all("eyes: left only" in body for _, body, _ in menus)

    def test_the_guide_says_auto_when_the_rig_advances_by_itself(
        self, fake_pypixxlib, fake_psychopy
    ):
        tracker = calibrating(
            fake_pypixxlib, fake_psychopy, calibration_type="HV5", calibration_advance="auto"
        )
        fake_psychopy.keys.extend(["escape"])
        tracker.calibrate()
        assert "AUTO" in guide_body(tracker)

    def test_escape_at_the_guide_aborts_with_nothing_sampled(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_psychopy.keys.extend(["escape", "space", "space"])
        result = tracker.calibrate()
        assert result.aborted and result.ok is None
        assert "guide" in result.note
        assert fake_pypixxlib.calibration_points == []
        assert not fake_pypixxlib.finished_calibration
        # The guide is cleared from the screen on the way out.
        assert tracker._display.window.flips >= 1  # type: ignore[union-attr]

    def test_a_closed_window_at_the_guide_aborts(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        result = tracker.calibrate()  # no keys at all: the window goes away
        assert result.aborted
        assert fake_pypixxlib.calibration_points == []

    def test_progress_is_reported_from_the_guide_and_every_target(
        self, fake_pypixxlib, fake_psychopy
    ):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        reports: list[tuple[str, str]] = []
        tracker.set_progress_hook(lambda stage, detail: reports.append((stage, detail)))
        fake_psychopy.keys.extend([START] + ["space"] * 5)
        tracker.calibrate()
        stages = [stage for stage, _ in reports]
        assert stages[0] == "calibration guide"
        details = [detail for stage, detail in reports if stage == "calibrating"]
        assert details[0].startswith("target 1 of 5")
        assert details[-1].startswith("target 5 of 5")
        assert "eyes: both tracked" in details[0]

    def test_the_hook_can_be_removed(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        reports: list[tuple[str, str]] = []
        tracker.set_progress_hook(lambda stage, detail: reports.append((stage, detail)))
        tracker.set_progress_hook(None)
        fake_psychopy.keys.extend([START] + ["space"] * 5)
        tracker.calibrate()
        assert reports == []


class TestCalibrationWalk:
    def test_calibration_needs_a_display(self, fake_pypixxlib):
        tracker = connected()  # built with display=None, as check-rig does
        with pytest.raises(TrackerError, match="needs an open display"):
            tracker.calibrate()

    def test_accepting_every_target_samples_each_one_and_fits(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_psychopy.keys.extend([START] + ["space"] * 5)
        result = tracker.calibrate()

        # Each target is handed to the device in the frame it was drawn in —
        # both are the device's own centered px, so no conversion happens and
        # none should.
        expected = calibration_targets("HV5", SCREEN, tracker._cfg.calibration_area)
        assert [(x, y) for x, y, _ in fake_pypixxlib.calibration_points] == expected
        assert fake_pypixxlib.finished_calibration
        # And the session hears what happened, in the words a panel shows.
        assert result.ok is True and not result.aborted
        assert result.verdict == "calibrated"
        assert (result.layout, result.n_targets, result.advance) == ("HV5", 5, "manual")
        assert result.eye.startswith("left")
        assert result.t == tracker._clock.now()

    def test_redo_steps_back_to_the_previous_target(self, fake_pypixxlib, fake_psychopy):
        # The point an experimenter wants to redo is almost always the one
        # they just accepted, not the one still on screen.
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_psychopy.keys.extend(
            [START, "space", "space", "backspace", "space", "space", "space", "space"]
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
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_psychopy.keys.extend([START, "backspace"] + ["space"] * 5)
        tracker.calibrate()
        assert len(fake_pypixxlib.calibration_points) == 5

    def test_abort_leaves_the_previous_calibration_alone(self, fake_pypixxlib, fake_psychopy):
        # The fit is only committed by finishCalibration(); aborting before it
        # means the device keeps whatever calibration it already had, which is
        # what an experimenter pressing escape for a subject break expects.
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_psychopy.keys.extend([START, "space", "escape"])
        result = tracker.calibrate()
        assert len(fake_pypixxlib.calibration_points) == 1
        assert not fake_pypixxlib.finished_calibration
        assert result.aborted and result.ok is None
        assert result.verdict == "aborted"
        assert "target 2 of 5" in result.note

    def test_a_closed_window_aborts_rather_than_spinning(self, fake_pypixxlib, fake_psychopy):
        # waitKeys returns None if the window goes away. Treating that as
        # "accept" would fit the model to nothing; looping would hang the rig.
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_psychopy.keys.extend([START])
        result = tracker.calibrate()
        assert fake_pypixxlib.calibration_points == []
        assert not fake_pypixxlib.finished_calibration
        assert result.aborted


class TestCalibrationKeys:
    """A key pressed during the walk reaches it once, for the target it was
    pressed at, however busy the walk was when it came."""

    def test_a_press_made_while_the_walk_was_busy_is_kept(self, fake_pypixxlib, fake_psychopy):
        """PsychoPy's waitKeys empties the keyboard buffer when it starts
        waiting, by default. Between waits the walk reads the eye status,
        flips, and reports progress; a SPACE pressed then was thrown away, and
        the experimenter pressed again and again until one landed in a wait."""
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        pressed_at: set[str] = set()

        def press_while_busy(stage: str, detail: str) -> None:
            # The report runs between the flip and the wait, exactly when a
            # press used to be lost. One press per target, and none at a wait.
            target = detail.split(" · ")[0]
            if stage == "calibrating" and target not in pressed_at:
                pressed_at.add(target)
                fake_psychopy.buffered.append("space")

        tracker.set_progress_hook(press_while_busy)
        fake_psychopy.keys.extend([START] + [None] * 10)
        result = tracker.calibrate()
        assert len(fake_pypixxlib.calibration_points) == 5
        assert result.ok is True

    def test_a_press_from_the_previous_target_does_not_accept_the_next(
        self, fake_pypixxlib, fake_psychopy
    ):
        """An impatient second SPACE, pressed while the device sampled the
        target just accepted, would accept the next one before the subject had
        looked at it. The keyboard is cleared when each target appears."""
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        sample = fake_pypixxlib.libdpx.TPxGetEyePositionDuringCalib_returnsRaw

        def sample_while_pressed_again(x, y, eye):
            fake_psychopy.buffered.append("space")
            return sample(x, y, eye)

        fake_pypixxlib.libdpx.TPxGetEyePositionDuringCalib_returnsRaw = sample_while_pressed_again
        fake_psychopy.keys.extend([START, "space", None, None, None])
        tracker.calibrate()
        assert len(fake_pypixxlib.calibration_points) == 1

    def test_the_keyboard_is_cleared_once_as_each_screen_appears(
        self, fake_pypixxlib, fake_psychopy
    ):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_psychopy.keys.extend([START, None, None] + ["space"] * 5)
        tracker.calibrate()
        # The guide once, and each of the five targets once, however many
        # refreshes each took; no wait empties the buffer on its own.
        assert fake_psychopy.clears == ["keyboard"] * 6
        assert fake_psychopy.waits and not any(fake_psychopy.waits)

    def test_the_pause_key_stops_the_walk_for_the_pause_menu(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_psychopy.keys.extend([START, "space", "p"])
        result = tracker.calibrate()
        assert len(fake_pypixxlib.calibration_points) == 1
        assert not fake_pypixxlib.finished_calibration
        assert result.aborted and result.ok is None
        assert "stopped for the pause menu at target 2 of 5" in result.note

    def test_the_pause_key_at_the_guide_goes_back_too(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_psychopy.keys.extend(["p", "space"])
        result = tracker.calibrate()
        assert result.aborted
        assert fake_pypixxlib.calibration_points == []

    def test_the_guide_names_the_pause_key(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_psychopy.keys.extend(["escape"])
        tracker.calibrate()
        body = guide_body(tracker)
        assert "P or ESC" in body
        assert "go back to the pause menu" in body


class TestAutoAdvance:
    """``calibration_advance: auto`` — the walk accepts each target itself once
    the configured eye has been in the image for long enough."""

    # Refreshes one target needs: the settle time, and then the steady run —
    # both counted in refreshes of STATUS_REFRESH_S, from the same clock. The
    # refresh on which the settle time is reached is the first of the run.
    PER_TARGET = math.ceil(AUTO_SETTLE_S / STATUS_REFRESH_S) + AUTO_STEADY_REFRESHES - 1

    def test_targets_are_accepted_without_a_key(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(
            fake_pypixxlib, fake_psychopy, calibration_type="HV5", calibration_advance="auto"
        )
        # A few spare refreshes: the clock is a float sum, and a target that
        # settles one refresh late is not what this test is about.
        fake_psychopy.keys.extend([START] + [None] * (5 * self.PER_TARGET + 5))
        result = tracker.calibrate()
        assert len(fake_pypixxlib.calibration_points) == 5
        assert fake_pypixxlib.finished_calibration
        assert result.ok is True and result.advance == "auto"

    def test_a_target_is_held_for_the_settle_time_first(self, fake_pypixxlib, fake_psychopy):
        # Fewer refreshes than one target needs: nothing may be accepted, or
        # the walk has fitted a point to a saccade still in flight.
        tracker = calibrating(
            fake_pypixxlib, fake_psychopy, calibration_type="HV5", calibration_advance="auto"
        )
        fake_psychopy.keys.extend([START] + [None] * (self.PER_TARGET - 1))
        tracker.calibrate()
        assert fake_pypixxlib.calibration_points == []

    def test_one_more_refresh_accepts(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(
            fake_pypixxlib, fake_psychopy, calibration_type="HV5", calibration_advance="auto"
        )
        fake_psychopy.keys.extend([START] + [None] * self.PER_TARGET)
        tracker.calibrate()
        assert len(fake_pypixxlib.calibration_points) == 1

    def test_the_configured_eye_has_to_be_in_view(self, fake_pypixxlib, fake_psychopy):
        # Only the left pupil is found; the session reads the right eye. Auto
        # must never accept that, however long it waits.
        tracker = calibrating(
            fake_pypixxlib,
            fake_psychopy,
            calibration_type="HV5",
            calibration_advance="auto",
            eye="right",
        )
        fake_pypixxlib.libdpx.pupils = (3.0, 2.0, 0.0, 0.0)
        fake_psychopy.keys.extend([START] + [None] * (5 * self.PER_TARGET))
        result = tracker.calibrate()
        assert fake_pypixxlib.calibration_points == []
        assert result.aborted  # the window closed on it in the end

    def test_a_lost_refresh_restarts_the_steady_count(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(
            fake_pypixxlib, fake_psychopy, calibration_type="HV5", calibration_advance="auto"
        )
        libdpx = fake_pypixxlib.libdpx
        # The eye drops out on every fourth read: never five in a row.
        reads = {"n": 0}

        def flicker():
            reads["n"] += 1
            return (0.0, 0.0, 0.0, 0.0) if reads["n"] % 4 == 0 else (3.0, 2.0, 3.0, 2.0)

        libdpx.TPxGetPupilSize = flicker  # type: ignore[method-assign]
        fake_psychopy.keys.extend([START] + [None] * (3 * self.PER_TARGET))
        tracker.calibrate()
        assert fake_pypixxlib.calibration_points == []

    def test_the_keys_still_work_in_auto_mode(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(
            fake_pypixxlib, fake_psychopy, calibration_type="HV5", calibration_advance="auto"
        )
        # SPACE accepts at once; ESC aborts.
        fake_psychopy.keys.extend([START, "space", "space", "escape"])
        result = tracker.calibrate()
        assert len(fake_pypixxlib.calibration_points) == 2
        assert result.aborted and "target 3 of 5" in result.note


class TestEyeInView:
    def test_the_configured_eye_is_the_one_that_counts(self):
        assert eye_in_view((True, False), "left")
        assert not eye_in_view((True, False), "right")
        assert eye_in_view((False, True), "right")

    def test_average_needs_both(self):
        assert eye_in_view((True, True), "average")
        assert not eye_in_view((True, False), "average")

    def test_an_unknown_eye_is_an_error(self):
        with pytest.raises(TrackerError, match="unknown eyetracker.eye"):
            eye_in_view((True, True), "cyclopean")


class TestGazeReader:
    """Gaze is read off the render thread on the rig; the tests above run the
    reader synchronously. This is the thread itself."""

    def test_the_thread_reads_and_the_caller_only_copies(self, fake_pypixxlib):
        tracker = make_viewpixx(background_gaze=True)
        tracker.connect()
        tracker.configure(SCREEN, tracker._clock)
        try:
            fake_pypixxlib.positions = [10.0, 20.0, 0.0, 0.0]
            deadline = time.monotonic() + 2.0
            sample = None
            while sample is None and time.monotonic() < deadline:
                sample = tracker.get_gaze()
                time.sleep(0.005)
            assert sample is not None, "the reader thread never delivered a sample"
            reads_before = fake_pypixxlib.reads
            for _ in range(20):
                tracker.get_gaze()
            # get_gaze() itself never touched the device: every read was the thread's.
            assert fake_pypixxlib.reads - reads_before < 200
            assert reads_before > 0
        finally:
            tracker.shutdown(None)
        assert fake_pypixxlib.closed

    def test_a_read_that_raises_surfaces_on_the_caller(self, fake_pypixxlib, monkeypatch):
        tracker = make_viewpixx(background_gaze=True)
        tracker.connect()
        tracker.configure(SCREEN, tracker._clock)
        try:

            def explode(packed, raw):
                raise OSError("USB read failed")

            monkeypatch.setattr(fake_pypixxlib.libdpx, "TPxBestPolyGetEyePosition", explode)
            deadline = time.monotonic() + 2.0
            raised = False
            while time.monotonic() < deadline:
                try:
                    tracker.get_gaze()
                except TrackerError as e:
                    assert "stopped answering" in str(e)
                    raised = True
                    break
                time.sleep(0.005)
            assert raised
        finally:
            tracker.shutdown(None)

    def test_a_stale_report_is_no_sample(self, fake_pypixxlib):
        # A reader that has fallen behind is a stalled USB call, and the
        # position it last saw is not this frame's. Simulated by a thread that
        # looks alive but never reads again: the caller must not read for it.
        clock = FakeClock()
        tracker = connected(clock)
        fake_pypixxlib.positions = [10.0, 20.0, 0.0, 0.0]
        assert tracker.get_gaze() is not None
        reader = tracker._reader
        assert reader is not None
        reader._thread = threading.current_thread()
        try:
            clock.advance(GAZE_STALE_S / 2)
            assert tracker.get_gaze() is not None  # still fresh enough
            clock.advance(GAZE_STALE_S)
            assert tracker.get_gaze() is None
        finally:
            reader._thread = None

    def test_get_gaze_before_configure_is_an_error(self, fake_pypixxlib):
        tracker = make_viewpixx()
        tracker.connect()
        with pytest.raises(TrackerError, match="before configure"):
            tracker.get_gaze()


class TestAReaderThatWillNotStop:
    """stop() waits a bounded time for the reader thread; a USB read that has
    not returned by then is abandoned. The abandoned thread used to share
    its stop flag with the next start(), which cleared it: once its read
    returned it carried on reading beside the new thread, for the session."""

    def test_an_abandoned_thread_stops_once_its_read_returns(self, monkeypatch, caplog):
        # No wait at all in stop(), so the first read is still stuck in the
        # device when stop() gives up on it — no sleeping for 2 s here.
        monkeypatch.setattr(viewpixx_module, "READER_JOIN_S", 0.0)
        stuck_in_the_device = threading.Event()
        released = threading.Event()
        readers: list[threading.Thread] = []

        def read():
            readers.append(threading.current_thread())
            if len(readers) == 1:
                stuck_in_the_device.set()
                released.wait()
            return "report"  # never looked into by the reader

        reader = GazeReader(read, FakeClock(), threading.Lock(), interval_s=0.001)
        reader.start()
        assert stuck_in_the_device.wait(5.0)
        with caplog.at_level(logging.ERROR):
            reader.stop()
        # Said loudly, not dropped in silence.
        assert "did not stop within" in caplog.text
        abandoned = readers[0]
        assert abandoned.is_alive()

        reader.start()
        try:
            released.set()
            # The abandoned thread returns from its read, sees its own stop
            # was asked for, and ends — the new start() did not undo it.
            abandoned.join(timeout=5.0)
            assert not abandoned.is_alive()
            assert reader.running
        finally:
            monkeypatch.setattr(viewpixx_module, "READER_JOIN_S", 2.0)
            reader.stop()


class TestRecordingGuard:
    def test_a_drain_after_the_device_moved_the_ring_rearms_instead_of_hanging(
        self, fake_pypixxlib
    ):
        # What a calibration does to the device: free-run off, ring at 0. A
        # drain from the old read pointer never returns on the real library.
        # Moved under an open segment: start_trial() now checks the ring
        # itself (and re-arms it), so a move before it never reaches a drain.
        tracker = connected()
        libdpx = fake_pypixxlib.libdpx
        tracker.start_trial(1, "ok")
        libdpx.freerun = False
        libdpx.buffer_base = 0
        fake_pypixxlib.last_read_addr = 0x12345678
        tracker.stop_trial()
        assert fake_pypixxlib.drains == 0  # nothing valid to save
        assert libdpx.freerun
        assert libdpx.buffer_base == fake_pypixxlib.buffer_base_addr
        assert fake_pypixxlib.last_read_addr == fake_pypixxlib.buffer_base_addr

    def test_an_armed_ring_drains_normally(self, fake_pypixxlib):
        tracker = connected()
        tracker.start_trial(1, "ok")
        tracker.stop_trial()
        assert fake_pypixxlib.drains == 1

    def test_start_trial_rearms_a_ring_found_stopped_between_trials(self, fake_pypixxlib, caplog):
        # A trial started on a device that is not recording would be a
        # dropout from its first frame; the ring is put back first, and said so.
        tracker = connected()
        fake_pypixxlib.libdpx.freerun = False
        with caplog.at_level(logging.WARNING):
            tracker.start_trial(1, "ok")
        assert fake_pypixxlib.libdpx.freerun
        assert "not recording into the session's buffer before trial 1" in caplog.text
        assert tracker.recording_fault() is None


# One display frame at 120 Hz: how often a session asks.
FRAME = 1 / 120


def recording_trial(clock: FakeClock | None = None, **cfg_kwargs) -> ViewPixxTracker:
    """A configured tracker, no reader thread, with trial 1's segment open."""
    tracker = connected(clock or FakeClock(start=10.0), **cfg_kwargs)
    tracker.start_trial(1, "attempt 1")
    return tracker


class StalledReader:
    """Make the reader look alive while it never reads again — what a USB
    call that has not returned looks like from the render thread. The test
    reads for it (``read_now``) when it wants the thread to have read."""

    def __init__(self, tracker: ViewPixxTracker) -> None:
        self.reader = tracker._reader
        assert self.reader is not None

    def __enter__(self):
        self.reader._thread = threading.current_thread()  # type: ignore[union-attr]
        return self.reader

    def __exit__(self, *exc) -> None:
        self.reader._thread = None  # type: ignore[union-attr]


class TestDropoutDetection:
    """recording_fault(): the reader died, the reader stalled, or the device
    stopped recording — and on the healthy path, no device call at all."""

    def test_the_healthy_path_touches_no_device(self, fake_pypixxlib):
        # With the reader thread doing the reading, a frame's check is only
        # attribute reads: no gaze read, no register round trip.
        tracker = recording_trial()
        libdpx = fake_pypixxlib.libdpx
        with StalledReader(tracker) as reader:
            reader.read_now()
            before = (libdpx.reads, libdpx.cache_updates)
            for _ in range(10):
                assert tracker.recording_fault() is None
            assert (libdpx.reads, libdpx.cache_updates) == before

    def test_a_reader_that_died_is_a_dropout(self, fake_pypixxlib, monkeypatch):
        tracker = recording_trial()

        def explode(packed, raw):
            raise OSError("USB read failed")

        monkeypatch.setattr(fake_pypixxlib.libdpx, "TPxBestPolyGetEyePosition", explode)
        detail = tracker.recording_fault()
        assert detail == "the TRACKPixx3 stopped answering: USB read failed"
        # Said again, and never asked again, for the rest of the trial.
        assert tracker.recording_fault() == detail

    def test_a_reader_whose_thread_died_is_reported_before_anything_is_read(
        self, fake_pypixxlib, monkeypatch
    ):
        tracker = recording_trial()
        with StalledReader(tracker) as reader:

            def explode(packed, raw):
                raise OSError("USB read failed")

            monkeypatch.setattr(fake_pypixxlib.libdpx, "TPxBestPolyGetEyePosition", explode)
            with pytest.raises(TrackerError):
                reader.read_now()  # the thread's last read, which killed it
            detail = tracker.recording_fault()
        assert detail is not None
        assert detail.startswith("the TRACKPixx3 stopped answering: the gaze reader's last device")
        assert "USB read failed" in detail

    def test_a_stalled_reader_is_reported_after_the_limit_and_not_before(self, fake_pypixxlib):
        clock = FakeClock(start=10.0)
        tracker = recording_trial(clock)
        with StalledReader(tracker) as reader:
            reader.read_now()
            clock.advance(0.090)
            assert tracker.recording_fault() is None  # 90 ms: inside the 100 ms limit
            clock.advance(0.020)
            detail = tracker.recording_fault()
        assert detail is not None
        assert detail.startswith("no gaze report from the TRACKPixx3 for 110 ms (limit 100 ms)")
        assert "stuck in a device call" in detail

    def test_a_report_repeated_within_the_limit_is_not_a_dropout(self, fake_pypixxlib):
        clock = FakeClock(start=10.0)
        tracker = recording_trial(clock)
        with StalledReader(tracker) as reader:
            reader.read_now()
            clock.advance(0.060)
            assert tracker.recording_fault() is None
            reader.read_now()  # a slow read finally came back
            clock.advance(0.060)
            assert tracker.recording_fault() is None

    def test_get_gaze_keeps_a_position_until_the_check_has_called_the_dropout(self, fake_pypixxlib):
        # The health check runs before get_gaze() in every frame. A stalled
        # reader must be called a dropout — a system fault — before the stale
        # position turns into "no gaze", which a fixation phase would blame
        # on the subject as a broken fixation.
        clock = FakeClock(start=10.0)
        tracker = recording_trial(clock, max_sample_gap_ms=200)
        fake_pypixxlib.positions = [10.0, 20.0, 0.0, 0.0]
        with StalledReader(tracker) as reader:
            reader.read_now()
            clock.advance(0.201)
            assert tracker.recording_fault() is not None
            assert tracker.get_gaze() is not None  # still the last position
            clock.advance(0.005)
            assert tracker.get_gaze() is None

    def test_the_device_stopping_its_recording_is_reported(self, fake_pypixxlib):
        # The gaze report carries on when the recording stops — two separate
        # paths through the device — so the reader asks the device itself.
        clock = FakeClock(start=10.0)
        tracker = recording_trial(clock)
        fake_pypixxlib.libdpx.freerun = False  # another program took it over
        # Not before the reader's next question: its last answer is fresh.
        assert tracker.recording_fault() is None
        clock.advance(0.050)  # half the limit: the question is due
        detail = tracker.recording_fault()
        assert detail == (
            "the TRACKPixx3 stopped recording samples into the session's buffer: "
            "free-run sampling is off"
        )

    def test_a_moved_buffer_is_named(self, fake_pypixxlib):
        clock = FakeClock(start=10.0)
        tracker = recording_trial(clock)
        fake_pypixxlib.libdpx.buffer_base = 0
        clock.advance(0.050)
        detail = tracker.recording_fault()
        assert detail is not None
        assert "its sample buffer moved (base 0x0, armed at 0x12000000)" in detail

    def test_a_device_that_went_away_is_reported(self, fake_pypixxlib):
        # libdpx's free functions do not raise: the gaze read carries on,
        # writing nothing, and only the sticky error says the transfer failed.
        clock = FakeClock(start=10.0)
        tracker = recording_trial(clock)
        fake_pypixxlib.libdpx.unplug()
        clock.advance(0.050)
        detail = tracker.recording_fault()
        assert detail is not None
        assert detail.startswith("the TRACKPixx3 did not answer a register read (")
        assert "DPX_ERR_USB_REQ_FAILED" in detail

    def test_blinks_never_read_as_a_dropout(self, fake_pypixxlib):
        # A blink is the device reporting "no eye", at full rate.
        clock = FakeClock(start=10.0)
        tracker = recording_trial(clock)
        fake_pypixxlib.positions = [TRACKING_LOST_PX] * 4
        for _ in range(240):
            clock.advance(FRAME)
            assert tracker.recording_fault() is None
            assert tracker.get_gaze() is None

    def test_a_healthy_session_second_is_quiet(self, fake_pypixxlib):
        clock = FakeClock(start=10.0)
        tracker = recording_trial(clock)
        for _ in range(240):
            clock.advance(FRAME)
            assert tracker.recording_fault() is None
            tracker.get_gaze()

    def test_no_open_segment_has_nothing_to_report(self, fake_pypixxlib):
        tracker = connected()
        fake_pypixxlib.libdpx.freerun = False
        assert tracker.recording_fault() is None
        assert tracker.newest_sample_age_s() is None

    def test_the_cutoff_never_undercuts_the_limit(self, fake_pypixxlib):
        # GAZE_STALE_S is the floor; a longer limit carries the cutoff with it.
        assert connected()._gaze_stale_s == pytest.approx(0.105)
        assert connected(max_sample_gap_ms=60)._gaze_stale_s == pytest.approx(GAZE_STALE_S)
        assert connected(max_sample_gap_ms=300)._gaze_stale_s == pytest.approx(0.305)


class TestAfterADropout:
    """What a dropout leaves behind: the drain and the messages that follow
    it are logged, not raised; the next start restarts a reader that died,
    re-checks the device, and refuses the trial loudly if it does not answer."""

    def dropped(self, fake_pypixxlib) -> tuple[ViewPixxTracker, FakeClock, str]:
        clock = FakeClock(start=10.0)
        tracker = recording_trial(clock)
        fake_pypixxlib.libdpx.unplug()
        clock.advance(0.050)
        detail = tracker.recording_fault()
        assert detail is not None
        return tracker, clock, detail

    def test_messages_after_a_dropout_are_left_out_and_logged(self, fake_pypixxlib, caplog):
        tracker, clock, detail = self.dropped(fake_pypixxlib)
        marks = len(tracker._messages)
        with caplog.at_level(logging.WARNING):
            tracker.send_message("trial_end")
        # Left out: a mark with no device time aligns nothing.
        assert len(tracker._messages) == marks
        assert "message 'trial_end'" in caplog.text
        assert "left out of the message record" in caplog.text

    def test_a_message_that_fails_without_a_dropout_still_raises(self, fake_pypixxlib):
        tracker = recording_trial()
        fake_pypixxlib.libdpx.unplug()
        with pytest.raises(OSError, match="DPX_ERR_USB_REQ_FAILED"):
            tracker.send_message("stim_on")

    def test_a_drain_that_fails_after_a_dropout_is_logged_not_raised(self, fake_pypixxlib, caplog):
        tracker, clock, detail = self.dropped(fake_pypixxlib)
        with caplog.at_level(logging.ERROR):
            tracker.stop_trial()
        assert not tracker.is_recording()
        assert "could not be saved after this trial's dropout" in caplog.text

    def test_a_drain_that_fails_without_a_dropout_still_raises(self, fake_pypixxlib):
        tracker = recording_trial()
        fake_pypixxlib.libdpx.unplug()
        with pytest.raises(OSError, match="DPX_ERR_USB_REQ_FAILED"):
            tracker.stop_trial()

    def test_a_device_that_is_gone_refuses_the_next_trial(self, fake_pypixxlib):
        tracker, clock, detail = self.dropped(fake_pypixxlib)
        tracker.stop_trial()
        with pytest.raises(TrackerError) as excinfo:
            tracker.start_trial(2, "attempt 1")
        message = str(excinfo.value)
        assert message.startswith("the TRACKPixx3 is not answering at trial 2:")
        assert "DPX_ERR_USB_REQ_FAILED" in message
        assert "DATAPixx3 is powered and its USB cable connected" in message
        assert f"The previous trial's recording had already been lost: {detail}." in message
        assert not tracker.is_recording()

    def test_a_reader_that_died_is_restarted_when_the_device_answers_again(
        self, fake_pypixxlib, monkeypatch, caplog
    ):
        clock = FakeClock(start=10.0)
        tracker = recording_trial(clock)
        libdpx = fake_pypixxlib.libdpx
        working = libdpx.TPxBestPolyGetEyePosition

        def explode(packed, raw):
            raise OSError("USB read failed")

        monkeypatch.setattr(libdpx, "TPxBestPolyGetEyePosition", explode)
        assert tracker.recording_fault() is not None
        tracker.stop_trial()
        monkeypatch.setattr(libdpx, "TPxBestPolyGetEyePosition", working)  # the cable is back
        with caplog.at_level(logging.INFO):
            tracker.start_trial(2, "attempt 1")
        reader = tracker._reader
        assert reader is not None and reader.fault is None
        assert "answers again: gaze reader restarted before trial 2" in caplog.text
        clock.advance(FRAME)
        assert tracker.recording_fault() is None

    def test_a_reader_that_still_cannot_read_refuses_the_next_trial(
        self, fake_pypixxlib, monkeypatch
    ):
        tracker = recording_trial()

        def explode(packed, raw):
            raise OSError("USB read failed")

        monkeypatch.setattr(fake_pypixxlib.libdpx, "TPxBestPolyGetEyePosition", explode)
        assert tracker.recording_fault() is not None
        tracker.stop_trial()
        with pytest.raises(TrackerError, match="not answering at trial 2: .*USB read failed"):
            tracker.start_trial(2, "attempt 1")

    def test_a_device_that_fails_the_first_marks_is_refused_in_the_rigs_words(
        self, fake_pypixxlib, monkeypatch, caplog
    ):
        # The check passed, then the trial's first messages failed. The
        # segment is open, and the runner's finally drains it: that drain
        # must be logged, never raise over the clear error.
        tracker = connected()

        def explode() -> float:
            raise OSError("link down")

        monkeypatch.setattr(fake_pypixxlib, "getTime", explode)
        with pytest.raises(TrackerError, match="not answering at trial 1: OSError: link down"):
            tracker.start_trial(1, "attempt 1")
        monkeypatch.setattr(fake_pypixxlib, "saveBufferedData", explode)
        with caplog.at_level(logging.ERROR):
            tracker.stop_trial()  # what the runner's finally does: no raise
        assert not tracker.is_recording()
        assert "could not be saved after this trial's dropout" in caplog.text

    def test_start_trial_replaces_a_verdict_from_before_it(self, fake_pypixxlib):
        # The reader's last answer may predate the trial. The start asks the
        # device itself, so an old "not recording" never faults a new trial.
        tracker = recording_trial()
        tracker.stop_trial()
        reader = tracker._reader
        assert reader is not None
        reader.note_check("stopped recording samples (an answer from before the trial)")
        tracker.start_trial(2, "attempt 1")
        assert tracker.recording_fault() is None

    def test_simulate_dropout_switches_free_run_off_behind_the_backends_back(self, fake_pypixxlib):
        # What check-rig does to prove detection works on the real device.
        clock = FakeClock(start=10.0)
        tracker = recording_trial(clock)
        said = tracker.simulate_dropout()
        assert "TPxDisableFreeRun() through pypixxlib" in said
        assert tracker.is_recording()
        assert not fake_pypixxlib.libdpx.freerun
        clock.advance(0.050)
        detail = tracker.recording_fault()
        assert detail is not None and "free-run sampling is off" in detail

    def test_simulate_dropout_needs_an_open_recording(self, fake_pypixxlib):
        with pytest.raises(TrackerError, match="needs an open recording"):
            connected().simulate_dropout()

    def test_a_stuck_device_call_fails_loudly_instead_of_hanging(self, fake_pypixxlib, monkeypatch):
        # A USB call that never returns holds the device lock forever. The
        # session's own calls give up after LOCK_TIMEOUT_S, and say why.
        monkeypatch.setattr(viewpixx_module, "LOCK_TIMEOUT_S", 0.001)
        tracker = recording_trial()
        assert tracker._device_lock.acquire()  # the stuck call
        try:
            with pytest.raises(
                TrackerError, match="did not answer within .* a device call is stuck"
            ):
                tracker.send_message("stim_on")
        finally:
            tracker._device_lock.release()

    def test_shutdown_without_a_destination_discards_the_test_recording(self, fake_pypixxlib):
        # check-rig's dropout test records for a second or two; nothing asks
        # for that recording, and it must not pile up in the temp folder.
        tracker = connected()
        scratch = tracker._scratch_dir
        assert scratch is not None and scratch.is_dir()
        tracker.shutdown(None)
        assert not scratch.exists()


class TestCalibrationRecording:
    def test_drains_before_and_rearms_after(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        libdpx = fake_pypixxlib.libdpx

        def calib_point(x, y, eye):
            # The real device switches free-run off and moves the ring here.
            fake_pypixxlib.calibration_points.append((x, y, eye))
            libdpx.freerun = False
            libdpx.buffer_base = 0

        fake_pypixxlib.getEyePositionDuringCalib = calib_point  # type: ignore[method-assign]
        fake_psychopy.keys.extend([START] + ["space"] * 5)
        tracker.calibrate()
        assert fake_pypixxlib.drains == 1  # the pre-calibration drain
        assert libdpx.freerun
        assert libdpx.buffer_base == fake_pypixxlib.buffer_base_addr
        # And the next trial's drain is a real one again.
        tracker.start_trial(1, "ok")
        tracker.stop_trial()
        assert fake_pypixxlib.drains == 2

    def test_the_dashboards_camera_read_leaves_the_ring_alone_mid_walk(
        self, fake_pypixxlib, fake_psychopy, caplog
    ):
        """The session's monitor reads the camera image on every progress
        report it publishes, and the device's per-target call has un-armed
        the ring by the second target. A read that "put it back" would blame
        itself for the move, warn twice a second for the whole walk, and
        toggle the device's ring between its own calibration calls. The one
        re-arm is calibrate()'s, when the walk is over."""
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        libdpx = fake_pypixxlib.libdpx

        def calib_point(x, y, eye):
            fake_pypixxlib.calibration_points.append((x, y, eye))
            libdpx.freerun = False
            libdpx.buffer_base = 0

        fake_pypixxlib.getEyePositionDuringCalib = calib_point  # type: ignore[method-assign]
        arms_before = libdpx.arms
        reads = 0

        def dashboard_refresh(stage: str, detail: str) -> None:
            nonlocal reads
            tracker.camera_frame()
            reads += 1
            assert libdpx.arms == arms_before, "the camera read re-armed the ring mid-walk"

        tracker.set_progress_hook(dashboard_refresh)
        fake_psychopy.keys.extend([START] + ["space"] * 5)
        with caplog.at_level(logging.WARNING, logger="alhazen.devices.eyetracker.viewpixx"):
            tracker.calibrate()
        assert reads >= 5  # the hook did run, once per target at least
        assert not any("re-pointed" in record.message for record in caplog.records)
        # The walk's own re-arm, once, at the end.
        assert libdpx.arms == arms_before + 1
        assert libdpx.freerun and libdpx.buffer_base == fake_pypixxlib.buffer_base_addr
        # And a read after the walk protects the ring again.
        libdpx.freerun = False
        tracker.camera_frame()
        assert libdpx.arms == arms_before + 2

    def test_accept_is_refused_while_no_eye_is_in_the_image(
        self, fake_pypixxlib, fake_psychopy, caplog
    ):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_pypixxlib.libdpx.pupils = (0.0, 0.0, 0.0, 0.0)
        fake_psychopy.keys.extend([START, "space", "space", "escape"])
        with caplog.at_level(logging.WARNING, logger="alhazen.devices.eyetracker.viewpixx"):
            result = tracker.calibrate()
        assert fake_pypixxlib.calibration_points == []
        assert not fake_pypixxlib.finished_calibration
        assert any("NO EYE" in shown for text in fake_psychopy.texts for shown in text.shown)
        # The guide said so as well, before the walk started.
        assert "NO EYE" in guide_body(tracker)
        assert result.aborted
        # And each refused SPACE is in the log, naming the target, so a
        # session log alone explains why the walk did not move.
        refusals = [r.message for r in caplog.records if "not accepted" in r.message]
        assert refusals == ["target 1 of 5 not accepted: no eye in the camera image"] * 2

    def test_the_status_line_names_the_tracked_eyes(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_pypixxlib.libdpx.pupils = (3.0, 2.0, 0.0, 0.0)
        fake_psychopy.keys.extend([START] + ["space"] * 5)
        tracker.calibrate()
        assert any("left only" in shown for text in fake_psychopy.texts for shown in text.shown)
        assert len(fake_pypixxlib.calibration_points) == 5

    def test_a_calibration_the_device_did_not_keep_is_reported(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_pypixxlib.calibrated_after_finish = False
        fake_psychopy.keys.extend([START] + ["space"] * 5)
        result = tracker.calibrate()
        assert fake_pypixxlib.finished_calibration
        assert tracker._display is not None
        messages = tracker._display.messages  # type: ignore[attr-defined]
        assert messages and "FAILED" in messages[0]
        # What happened, then what to do: two paragraphs, so the display's
        # prose reflow keeps them apart without being asked (and this fake,
        # which takes no `reflow`, would have raised had it been asked).
        from alhazen.display import reflow

        assert reflow(messages[0]).split("\n\n") == [
            "Calibration FAILED: the tracker reports no calibration.",
            "Check the camera sees the eyes (position, focus, LED), then calibrate again.",
        ]
        # And the result says so, for the dashboard and the log.
        assert result.ok is False and not result.aborted
        assert result.verdict == "NOT calibrated"
        assert "calibrate again" in result.note

    def test_a_kept_calibration_says_nothing_on_screen(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_psychopy.keys.extend([START] + ["space"] * 5)
        tracker.calibrate()
        assert tracker._display.messages == []  # type: ignore[attr-defined]


class TestCameraImage:
    """The camera frame: the optional capability the dashboard's eye-tracker
    tab draws. The pixel copy and the shrink are free functions; the method
    is the device calls around them."""

    def test_the_bytes_come_out_unsigned_and_in_the_same_order(self):
        # 200 is what the device means, whatever ctypes calls the byte.
        values = [0, 1, 127, 128, 200, 255]
        buffer = (ctypes.c_byte * 6)(*(v - 256 if v > 127 else v for v in values))
        pixels = image_from_pointer(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)), 2, 3)
        assert pixels.dtype == np.uint8
        assert pixels.tolist() == [[0, 1, 127], [128, 200, 255]]

    def test_the_copy_outlives_the_buffer(self):
        buffer = (ctypes.c_byte * 4)(1, 2, 3, 4)
        pixels = image_from_pointer(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)), 2, 2)
        buffer[0] = 99  # the device overwrites its buffer with the next frame
        assert pixels[0, 0] == 1

    def test_a_null_pointer_is_an_error_not_a_black_image(self):
        with pytest.raises(TrackerError, match="null image pointer"):
            image_from_pointer(ctypes.POINTER(ctypes.c_byte)(), 24, 32)

    def test_an_empty_size_is_an_error(self):
        buffer = (ctypes.c_byte * 4)(1, 2, 3, 4)
        with pytest.raises(TrackerError, match="0x0"):
            image_from_pointer(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)), 0, 0)

    def test_shrink_keeps_the_longer_side_within_the_cap(self):
        big = np.arange(700 * 900, dtype=np.uint32).reshape(700, 900).astype(np.uint8)
        small = shrink_image(big, 320)
        assert small.shape == (234, 300)  # every 3rd pixel
        # Point-sampled, not averaged: a pupil edge stays an edge.
        assert small[1, 1] == big[3, 3]
        assert small.flags["C_CONTIGUOUS"]

    def test_shrink_leaves_a_small_image_alone(self):
        small = np.zeros((24, 32), dtype=np.uint8)
        assert shrink_image(small, 320).shape == (24, 32)

    def test_the_frame_is_the_device_image_on_the_session_clock(self, fake_pypixxlib):
        clock = FakeClock()
        tracker = connected(clock)
        clock.advance(3.0)
        frame = tracker.camera_frame()
        assert frame.pixels.shape == (24, 32)
        assert int(frame.pixels[0, 0]) == 200
        assert frame.t == 3.0
        assert fake_pypixxlib.libdpx.image_reads == 1

    def test_a_large_image_is_shrunk_for_the_dashboard(self, fake_pypixxlib):
        tracker = connected()
        fake_pypixxlib.libdpx.image = np.zeros((1200, 1600), dtype=np.uint8)
        assert tracker.camera_frame().pixels.shape == (240, 320)

    def test_off_in_the_rig_config_is_an_error_that_says_so(self, fake_pypixxlib):
        tracker = connected(camera_image=False)
        with pytest.raises(TrackerError, match="camera_image"):
            tracker.camera_frame()
        assert fake_pypixxlib.libdpx.image_reads == 0

    def test_before_connect_is_an_error(self):
        tracker = make_viewpixx()
        with pytest.raises(TrackerError, match="before connect"):
            tracker.camera_frame()

    def test_no_image_from_the_device_is_an_error(self, fake_pypixxlib):
        tracker = connected()
        fake_pypixxlib.libdpx.image = None
        with pytest.raises(TrackerError, match="no camera image"):
            tracker.camera_frame()

    def test_a_libdpx_fault_during_the_read_is_an_error(self, fake_pypixxlib):
        tracker = connected()
        libdpx = fake_pypixxlib.libdpx

        def fail() -> None:
            libdpx.error = "DPX_ERR_USB_RAW_EZREAD"
            libdpx.error_string = "USB read failed"

        libdpx.on_image_read = fail
        with pytest.raises(TrackerError, match="DPX_ERR_USB_RAW_EZREAD"):
            tracker.camera_frame()
        # Cleared, so the next unrelated check does not report it again.
        assert libdpx.error == "DPX_SUCCESS"

    def test_a_read_that_moved_the_ring_rearms_it(self, fake_pypixxlib):
        tracker = connected()
        libdpx = fake_pypixxlib.libdpx

        def move_ring() -> None:
            libdpx.freerun = False
            libdpx.buffer_base = 0

        libdpx.on_image_read = move_ring
        arms_before = libdpx.arms
        tracker.camera_frame()
        assert libdpx.arms == arms_before + 1
        assert libdpx.freerun and libdpx.buffer_base == fake_pypixxlib.buffer_base_addr

    def test_the_lock_is_held_around_the_device_calls(self, fake_pypixxlib):
        # The gaze reader shares the device; the image read must not
        # interleave with its calls.
        tracker = connected()
        libdpx = fake_pypixxlib.libdpx
        libdpx.on_image_read = lambda: (
            pytest.fail("not locked") if tracker._device_lock.acquire(blocking=False) else None
        )
        tracker.camera_frame()


class TestEyeStatus:
    def test_names_the_eyes_the_camera_sees(self, fake_pypixxlib):
        tracker = connected()
        assert tracker.eye_status() == "eyes: both tracked"
        fake_pypixxlib.libdpx.pupils = (0.0, 0.0, 3.0, 2.0)
        assert tracker.eye_status() == "eyes: right only"
        fake_pypixxlib.libdpx.pupils = (0.0, 0.0, 0.0, 0.0)
        assert "NO EYE" in tracker.eye_status()

    def test_before_connect_is_an_error(self):
        with pytest.raises(TrackerError, match="before connect"):
            make_viewpixx().eye_status()


# ----------------------------------------------------------------------
# The calibration gate: "no calibration" is not "no eye"
# ----------------------------------------------------------------------


class TestEyeInRaw:
    """The raw eye vector is what says the camera measured an eye at all,
    calibration or not — the calibrated position cannot, because without a
    calibration it is NaN whether or not there is an eye."""

    def test_a_number_is_a_measured_eye(self):
        assert eye_in_raw([1.5, -0.5, 1.4, -0.4], "left")
        assert eye_in_raw([1.5, -0.5, 1.4, -0.4], "right")
        assert eye_in_raw([1.5, -0.5, 1.4, -0.4], "average")

    def test_the_buffers_initial_zero_is_no_measurement(self):
        # A call that wrote nothing leaves the ctypes buffer at zero, and an
        # "eye at exactly the origin" would defeat the point of reading it.
        assert not eye_in_raw([0.0, 0.0, 1.4, -0.4], "left")
        assert eye_in_raw([0.0, 0.0, 1.4, -0.4], "right")

    def test_nan_and_the_lost_sentinel_are_no_measurement(self):
        assert not eye_in_raw([math.nan, 0.2, 1.4, -0.4], "left")
        assert not eye_in_raw([TRACKING_LOST_PX, 0.2, 1.4, -0.4], "left")

    def test_average_needs_both(self):
        assert not eye_in_raw([1.5, -0.5, 0.0, 0.0], "average")

    def test_a_short_report_is_an_error(self):
        with pytest.raises(TrackerError, match="expected 4"):
            eye_in_raw([1.0, 2.0], "left")


class TestCalibrationGate:
    """The gaze report is a CALIBRATED read. On a device with no calibration
    every position is NaN whatever the camera sees — which the rig reported
    as "no eye" for an afternoon. Both facts have to be told apart."""

    def test_configure_says_what_the_device_holds(self, fake_pypixxlib, caplog):
        with caplog.at_level(logging.WARNING, logger="alhazen.devices.eyetracker.viewpixx"):
            connected()
        assert any(
            "holds a calibration from BEFORE this session" in r.message for r in caplog.records
        )

        caplog.clear()
        fake_pypixxlib.holds_calibration = False
        with caplog.at_level(logging.WARNING, logger="alhazen.devices.eyetracker.viewpixx"):
            connected()
        assert any("has NO calibration" in r.message for r in caplog.records)

    def test_an_uncalibrated_device_reports_no_position_whatever_the_array_holds(
        self, fake_pypixxlib, caplog
    ):
        fake_pypixxlib.holds_calibration = False
        tracker = connected()
        fake_pypixxlib.positions = [100.0, 200.0, 0.0, 0.0]  # would be a fine sample
        with caplog.at_level(logging.WARNING, logger="alhazen.devices.eyetracker.viewpixx"):
            assert tracker.get_gaze() is None
            assert tracker.get_gaze() is None
            assert tracker.get_gaze() is None
        # Said once per uncalibrated stretch, not once per frame — and it says
        # the camera does see the eye, which is the distinction that matters.
        said = [
            r
            for r in caplog.records
            if "get_gaze(): the TRACKPixx3 has no calibration" in r.message
        ]
        assert len(said) == 1
        assert "the camera sees the eye" in said[0].message

    def test_gaze_status_tells_no_calibration_from_no_eye(self, fake_pypixxlib):
        fake_pypixxlib.holds_calibration = False
        tracker = connected()
        tracker.get_gaze()
        assert tracker.gaze_status().startswith(
            "NO CALIBRATION on the device — the camera SEES the eye"
        )

        fake_pypixxlib.libdpx.raw_positions = [0.0, 0.0, 0.0, 0.0]
        tracker.get_gaze()
        assert "NO CALIBRATION" in tracker.gaze_status()
        assert "no eye in the camera image" in tracker.gaze_status()

    def test_gaze_status_on_a_calibrated_device(self, fake_pypixxlib):
        tracker = connected()
        fake_pypixxlib.positions = [100.0, 200.0, 0.0, 0.0]
        tracker.get_gaze()
        assert tracker.gaze_status() == "tracked"

        fake_pypixxlib.positions = [math.nan, math.nan, 0.0, 0.0]  # eye seen, no fit for it
        tracker.get_gaze()
        assert tracker.gaze_status().startswith(
            "eye in the camera image but no calibrated position"
        )

        fake_pypixxlib.libdpx.raw_positions = [0.0, 0.0, 0.0, 0.0]
        tracker.get_gaze()
        assert tracker.gaze_status().startswith("no eye in the camera image")

    def test_gaze_status_before_any_read(self, fake_pypixxlib):
        assert make_viewpixx().gaze_status().startswith("not reading gaze")
        assert connected().gaze_status() == "no gaze report yet"

    def test_calibration_state_is_a_fresh_read(self, fake_pypixxlib):
        tracker = connected()
        assert tracker.calibration_state() is True
        fake_pypixxlib.holds_calibration = False
        assert tracker.calibration_state() is False
        with pytest.raises(TrackerError, match="before connect"):
            make_viewpixx().calibration_state()

    def test_a_calibration_that_takes_opens_the_gate(self, fake_pypixxlib, fake_psychopy):
        fake_pypixxlib.holds_calibration = False
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_pypixxlib.positions = [100.0, 200.0, 0.0, 0.0]
        assert tracker.get_gaze() is None
        fake_psychopy.keys.extend([START] + ["space"] * 5)
        assert tracker.calibrate().ok is True
        assert tracker.get_gaze() is not None

    def test_the_raw_vectors_come_from_the_same_call_as_the_positions(self, fake_pypixxlib):
        # One USB round trip per report, not two: the reader thread's budget
        # is the frame, and TPxBestPolyGetEyePosition hands both back at once.
        tracker = connected()
        before = fake_pypixxlib.reads
        tracker.get_gaze()
        assert fake_pypixxlib.reads == before + 1


class TestVendorChatter:
    """pypixxlib prints "Recording data is not yet directly implemented" from
    two methods that then work perfectly well. On the rig it read as an
    error and stopped the operator cold. Captured, and logged as what it is."""

    def test_the_known_line_goes_to_the_debug_log_not_the_terminal(
        self, fake_pypixxlib, capsys, caplog
    ):
        with caplog.at_level(logging.DEBUG, logger="alhazen.devices.eyetracker.viewpixx"):
            tracker = connected()
            tracker.start_trial(1, "attempt 1")
            tracker.stop_trial()  # drains: the second method that prints
        assert capsys.readouterr().out == ""
        noted = [r for r in caplog.records if "usual note (expected, harmless)" in r.message]
        assert len(noted) == 2 and all(r.levelno == logging.DEBUG for r in noted)

    def test_anything_else_the_library_prints_is_kept_at_info(self, caplog):
        def chatty():
            print("Recording data is not yet directly implemented in the TRACKPixx3 -- x")
            print("firmware 2.1 is out of date")
            return 42

        with caplog.at_level(logging.DEBUG, logger="alhazen.devices.eyetracker.viewpixx"):
            assert quietly(chatty) == 42
        levels = {r.message: r.levelno for r in caplog.records}
        assert levels["pypixxlib printed: firmware 2.1 is out of date"] == logging.INFO
        assert any(level == logging.DEBUG for level in levels.values())


class TestIrisSize:
    """The expected iris size: the setting that decides whether the device
    finds a pupil in its image, read and set from the session."""

    def test_the_device_value_is_read(self, fake_pypixxlib):
        tracker = connected()
        fake_pypixxlib.libdpx.iris_size = 120
        assert tracker.iris_size() == 120

    def test_a_new_size_is_written_and_read_back(self, fake_pypixxlib):
        tracker = connected()
        updates = fake_pypixxlib.libdpx.cache_updates
        assert tracker.set_iris_size(140) == 140
        assert fake_pypixxlib.libdpx.iris_size == 140
        assert fake_pypixxlib.libdpx.cache_updates > updates  # sent to the device

    @pytest.mark.parametrize("value", [0, 513, True, 12.5, "100"])
    def test_a_size_outside_the_range_never_reaches_the_device(self, fake_pypixxlib, value):
        tracker = connected()
        before = fake_pypixxlib.libdpx.iris_size
        with pytest.raises(ValueError, match="iris size must be a whole number"):
            tracker.set_iris_size(value)
        assert fake_pypixxlib.libdpx.iris_size == before

    def test_a_device_that_holds_something_else_is_an_error(self, fake_pypixxlib):
        tracker = connected()
        fake_pypixxlib.libdpx.iris_register_max = 255
        with pytest.raises(TrackerError, match="it holds 255 px"):
            tracker.set_iris_size(300)

    def test_a_device_fault_is_an_error(self, fake_pypixxlib):
        tracker = connected()
        fake_pypixxlib.libdpx.error = "DPX_ERR_USB"
        with pytest.raises(TrackerError, match="refused iris size 100 px"):
            tracker.set_iris_size(100)

    def test_before_connect_it_is_an_error(self, fake_pypixxlib):
        with pytest.raises(TrackerError, match="before connect"):
            make_viewpixx().iris_size()

    def test_configure_applies_the_rig_value_and_logs_it(self, fake_pypixxlib, caplog):
        with caplog.at_level(logging.INFO, logger="alhazen.devices.eyetracker.viewpixx"):
            connected(iris_size_px=150)
        assert fake_pypixxlib.libdpx.iris_size == 150
        assert "expected iris size set to 150 camera px (rig config)" in caplog.text

    def test_configure_logs_the_device_value_when_the_rig_is_silent(self, fake_pypixxlib, caplog):
        fake_pypixxlib.libdpx.iris_size = 88
        with caplog.at_level(logging.INFO, logger="alhazen.devices.eyetracker.viewpixx"):
            connected()
        assert fake_pypixxlib.libdpx.iris_size == 88
        assert "expected iris size is 88 camera px, as the device holds it" in caplog.text


class TestCalibrationPlot:
    """Each target's fitted gaze per eye after a calibration: the plot
    LabMaestro shows, from the raw vectors the device measured and the
    polynomial it fitted."""

    def test_the_polynomial_is_vpixxs(self):
        # b0 + b1 x + b2 y + b3 x² + b4 y² + b5 x³ + b6 xy + b7 x²y + b8 x²y², one
        # coefficient at a time, at x = 2, y = 3.
        terms = [1, 2, 3, 4, 9, 8, 6, 12, 36]
        for index, term in enumerate(terms):
            coefficients = [0.0] * 9
            coefficients[index] = 1.0
            assert evaluate_calibration(2.0, 3.0, coefficients, coefficients) == (term, term)

    def test_each_eye_gets_its_fitted_gaze_and_error_at_every_target(
        self, fake_pypixxlib, fake_psychopy
    ):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        # Once fitted, the right eye lands 10 px right of every target.
        fake_pypixxlib.libdpx.raw_at_target = lambda x, y: (
            (x + 10.0) / 100.0 + 1.0,
            y / 100.0 + 1.0,
            x / 100.0 + 1.0,
            y / 100.0 + 1.0,
        )
        fake_psychopy.keys.extend([START] + ["space"] * 5)
        result = tracker.calibrate()
        assert result.ok is True
        expected = calibration_targets("HV5", SCREEN, tracker._cfg.calibration_area)
        assert [target.target_px for target in result.targets] == expected
        for target in result.targets:
            assert target.left_px == pytest.approx(target.target_px)
            assert target.left_error_deg == pytest.approx(0.0, abs=1e-9)
            assert target.right_px == pytest.approx(
                (target.target_px[0] + 10.0, target.target_px[1])
            )
            assert target.right_error_deg == pytest.approx(SCREEN.px2deg(10.0))

    def test_an_eye_not_measured_at_a_target_has_no_fitted_gaze_there(
        self, fake_pypixxlib, fake_psychopy
    ):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_pypixxlib.libdpx.raw_at_target = lambda x, y: (
            x / 100.0 + 1.0,
            y / 100.0 + 1.0,
            0.0,
            0.0,
        )
        fake_psychopy.keys.extend([START] + ["space"] * 5)
        result = tracker.calibrate()
        assert all(t.left_px is None and t.left_error_deg is None for t in result.targets)
        assert all(t.right_px is not None for t in result.targets)

    def test_a_calibration_that_did_not_take_has_no_plot(self, fake_pypixxlib, fake_psychopy):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_pypixxlib.calibrated_after_finish = False
        fake_psychopy.keys.extend([START] + ["space"] * 5)
        result = tracker.calibrate()
        assert result.ok is False and result.targets == ()

    def test_a_pypixxlib_without_the_raw_call_still_calibrates(
        self, fake_pypixxlib, fake_psychopy, monkeypatch, caplog
    ):
        monkeypatch.delattr(FakeLibdpx, "TPxGetEyePositionDuringCalib_returnsRaw")
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_psychopy.keys.extend([START] + ["space"] * 5)
        with caplog.at_level(logging.WARNING, logger="alhazen.devices.eyetracker.viewpixx"):
            result = tracker.calibrate()
        assert result.ok is True and result.targets == ()
        assert len(fake_pypixxlib.calibration_points) == 5
        assert "calibration plot unavailable" in result.note
        assert "calibration plot unavailable" in caplog.text

    def test_unreadable_coefficients_cost_the_plot_not_the_calibration(
        self, fake_pypixxlib, fake_psychopy, caplog
    ):
        tracker = calibrating(fake_pypixxlib, fake_psychopy, calibration_type="HV5")
        fake_pypixxlib.libdpx.coefficient_error = "DPX_ERR_USB"
        fake_psychopy.keys.extend([START] + ["space"] * 5)
        with caplog.at_level(logging.ERROR, logger="alhazen.devices.eyetracker.viewpixx"):
            result = tracker.calibrate()
        assert result.ok is True and result.targets == ()
        assert "fitted coefficients could not be read (DPX_ERR_USB" in result.note
        assert "fitted coefficients could not be read" in caplog.text
