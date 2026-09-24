"""The EyeLink backend's calibration, with pylink and psychopy stood in for.

The device-touching methods are documented as rig-only, but what alhazen
adds around ``doTrackerSetup()`` — the guide, the advance-mode command, the
result read back from the Host PC — is alhazen's own logic and is tested
here the same way the viewpixx walk is: the SDK modules are replaced in
``sys.modules`` before ``connect()`` imports them.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

from alhazen.config.models import EYELINK_CALIBRATION_TYPES, EyeTrackerConfig
from alhazen.devices.eyetracker.eyelink import (
    ABORT_RESULT,
    GUIDE_KEYS,
    NO_REPLY,
    OK_RESULT,
    EyeLinkTracker,
)
from alhazen.devices.eyetracker.guide import GUIDE_TITLE, TARGET_COUNTS, target_count
from alhazen.display.palette import TERMINAL_GREEN
from alhazen.errors import TrackerError
from alhazen.testing import FakeClock
from fake_sdk import ABORT_EXPT, MISSING_DATA, FakeEyeLinkHost, install_fake_pylink
from support import SCREEN


class FakeEyeLink:
    """The pylink.EyeLink connection object: records what it is told and
    answers the few queries calibrate() makes."""

    def __init__(self, host_ip: str) -> None:
        self.host_ip = host_ip
        self.commands: list[str] = []
        self.messages: list[str] = []
        self.setups = 0
        self.exits = 0
        # What doTrackerSetup() does: nothing, or raise the way pylink does
        # when the experimenter aborts with ESC.
        self.setup_error: RuntimeError | None = None
        # What the Host PC says about its last calibration.
        self.result_code = OK_RESULT
        self.result_message = "GOOD"
        self.result_error: Exception | None = None
        self.eye = -1  # eyeAvailable(): no sample to answer from until recording
        self.newest: FakeSample | None = None  # what getNewestSample() hands back

    def openDataFile(self, name: str) -> None:  # noqa: N802 - pylink's names
        self.data_file = name

    def sendCommand(self, text: str) -> None:  # noqa: N802
        self.commands.append(text)

    def sendMessage(self, text: str) -> None:  # noqa: N802
        self.messages.append(text)

    def setOfflineMode(self) -> None:  # noqa: N802
        pass

    def getTrackerVersionString(self) -> str:  # noqa: N802
        return "EYELINK CL 5.15"

    def doTrackerSetup(self) -> None:  # noqa: N802
        self.setups += 1
        if self.setup_error is not None:
            raise self.setup_error

    def exitCalibration(self) -> None:  # noqa: N802
        self.exits += 1

    def getCalibrationResult(self) -> int:  # noqa: N802
        if self.result_error is not None:
            raise self.result_error
        return self.result_code

    def getCalibrationMessage(self) -> str:  # noqa: N802
        return self.result_message

    def eyeAvailable(self) -> int:  # noqa: N802
        return self.eye

    def getNewestSample(self) -> FakeSample | None:  # noqa: N802
        return self.newest


class FakeSample:
    """A pylink link sample: the left eye's gaze, and the tracker's own
    timestamp in ms — which, like the real one, repeats for as long as no
    newer sample has arrived."""

    def __init__(self, gx: float, gy: float, tracker_ms: float) -> None:
        self._gaze = (gx, gy)
        self._time = tracker_ms

    def getTime(self) -> float:  # noqa: N802
        return self._time

    def isLeftSample(self) -> bool:  # noqa: N802
        return True

    def isRightSample(self) -> bool:  # noqa: N802
        return False

    def getLeftEye(self) -> types.SimpleNamespace:  # noqa: N802
        return types.SimpleNamespace(getGaze=lambda: self._gaze)


class FakeWindow:
    color = (0.0, 0.0, 0.0)

    def flip(self) -> None:
        pass


class FakeDisplay:
    kind = "fake"

    def __init__(self) -> None:
        self.window = FakeWindow()
        self.menus: list[tuple[str, str, tuple[float, float, float]]] = []

    def show_menu(self, title: str, body: str, *, color: tuple[float, float, float]) -> None:
        self.menus.append((title, body, color))


@pytest.fixture
def fake_pylink(monkeypatch):
    """A pylink whose EyeLink() hands back one recording FakeEyeLink."""
    module = types.ModuleType("pylink")
    connections: list[FakeEyeLink] = []

    def make(host_ip: str) -> FakeEyeLink:
        connection = FakeEyeLink(host_ip)
        connections.append(connection)
        return connection

    module.EyeLink = make  # type: ignore[attr-defined]
    module.EyeLinkCustomDisplay = object  # type: ignore[attr-defined]
    module.openGraphicsEx = lambda graphics: None  # type: ignore[attr-defined]
    module.OK_RESULT = OK_RESULT  # type: ignore[attr-defined]
    module.ABORT_RESULT = ABORT_RESULT  # type: ignore[attr-defined]
    module.NO_REPLY = NO_REPLY  # type: ignore[attr-defined]
    module.LEFT_EYE = 0  # type: ignore[attr-defined]
    module.RIGHT_EYE = 1  # type: ignore[attr-defined]
    module.BINOCULAR = 2  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pylink", module)
    return types.SimpleNamespace(module=module, connections=connections)


@pytest.fixture
def fake_psychopy(monkeypatch):
    """A psychopy whose waitKeys replays queued experimenter keys, and whose
    visual classes are enough for the calibration graphics to construct."""
    keys: list[str] = []

    def wait_keys(maxWait=None, keyList=None):  # noqa: N803 - psychopy's own parameter names
        assert keys, "calibrate() waited for a key nobody queued"
        return [keys.pop(0)]

    class Stim:
        """Any psychopy visual/event object the calibration graphics build."""

        def __init__(self, *args, **kwargs) -> None:
            pass

    event_module = types.ModuleType("psychopy.event")
    event_module.waitKeys = wait_keys  # type: ignore[attr-defined]
    event_module.Mouse = Stim  # type: ignore[attr-defined]
    visual_module = types.ModuleType("psychopy.visual")
    visual_module.Circle = Stim  # type: ignore[attr-defined]
    visual_module.TextStim = Stim  # type: ignore[attr-defined]
    package = types.ModuleType("psychopy")
    package.event = event_module  # type: ignore[attr-defined]
    package.visual = visual_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psychopy", package)
    monkeypatch.setitem(sys.modules, "psychopy.event", event_module)
    monkeypatch.setitem(sys.modules, "psychopy.visual", visual_module)
    return types.SimpleNamespace(keys=keys)


def configured(fake_pylink, **cfg_kwargs) -> tuple[EyeLinkTracker, FakeEyeLink, FakeDisplay]:
    """A connected and configured tracker with a display, ready to calibrate."""
    cfg = EyeTrackerConfig(backend="eyelink", **cfg_kwargs)
    display = FakeDisplay()
    tracker = EyeLinkTracker(cfg, display, SCREEN, FakeClock())
    tracker.connect()
    tracker.configure(SCREEN, FakeClock())
    (connection,) = fake_pylink.connections
    return tracker, connection, display


def guide_body(display: FakeDisplay) -> str:
    assert display.menus, "the calibration guide was never shown"
    title, body, color = display.menus[-1]
    assert title == GUIDE_TITLE
    assert color == TERMINAL_GREEN
    return body


class TestConfigure:
    def test_manual_advance_turns_automatic_calibration_off(self, fake_pylink, fake_psychopy):
        _, connection, _ = configured(fake_pylink)
        assert "enable_automatic_calibration = NO" in connection.commands

    def test_auto_advance_turns_it_on(self, fake_pylink, fake_psychopy):
        _, connection, _ = configured(fake_pylink, calibration_advance="auto")
        assert "enable_automatic_calibration = YES" in connection.commands

    def test_the_layout_and_area_still_reach_the_host(self, fake_pylink, fake_psychopy):
        _, connection, _ = configured(fake_pylink, calibration_type="HV13", calibration_area=0.5)
        assert "calibration_type = HV13" in connection.commands
        assert "calibration_area_proportion 0.5 0.5" in connection.commands


class TestLayouts:
    def test_the_guide_counts_every_layout_the_config_accepts(self):
        # The config validates the names, the guide knows the counts; the two
        # lists must agree or a valid config would fail at calibrate time.
        assert set(TARGET_COUNTS) == set(EYELINK_CALIBRATION_TYPES)
        assert target_count("H3") == 3
        assert target_count("HV13") == 13

    def test_an_unknown_layout_is_refused_by_name(self):
        with pytest.raises(ValueError, match="unknown calibration layout 'HV7'"):
            target_count("HV7")

    def test_the_config_rejects_a_layout_the_host_would_not(self):
        with pytest.raises(ValueError, match="not one the EyeLink accepts"):
            EyeTrackerConfig(backend="eyelink", calibration_type="HV7")


class TestGuide:
    def test_the_guide_precedes_the_host_setup(self, fake_pylink, fake_psychopy):
        tracker, connection, display = configured(fake_pylink, calibration_type="HV9")
        fake_psychopy.keys.append("space")
        tracker.calibrate()
        body = guide_body(display)
        assert "EyeLink (the Host PC drives the procedure)" in body
        assert "set on the Host PC" in body
        assert "HV9 — 9 targets over 60% of the screen" in body
        assert "MANUAL — press SPACE" in body
        for key, label in GUIDE_KEYS:
            assert key in body and label in body
        assert body.endswith("press SPACE to open the Host PC setup, ESC to skip")
        assert connection.setups == 1

    def test_auto_advance_is_announced(self, fake_pylink, fake_psychopy):
        tracker, _, display = configured(fake_pylink, calibration_advance="auto")
        fake_psychopy.keys.append("space")
        tracker.calibrate()
        assert "AUTO — each target is accepted by itself" in guide_body(display)

    def test_escape_at_the_guide_skips_the_host_setup(self, fake_pylink, fake_psychopy, caplog):
        tracker, connection, _ = configured(fake_pylink)
        fake_psychopy.keys.append("escape")
        with caplog.at_level(logging.WARNING):
            result = tracker.calibrate()
        assert connection.setups == 0
        assert result.aborted and result.ok is None
        assert result.verdict == "aborted"
        assert "keeps its previous calibration" in result.note
        assert "skipped at the guide" in caplog.text

    def test_without_a_display_there_is_nowhere_to_show_the_guide(self, fake_pylink):
        tracker = EyeLinkTracker(EyeTrackerConfig(backend="eyelink"), None, SCREEN, FakeClock())
        tracker.connect()
        with pytest.raises(TrackerError, match="needs an open display"):
            tracker.calibrate()

    def test_progress_is_reported_at_the_guide_and_the_setup(self, fake_pylink, fake_psychopy):
        tracker, _, _ = configured(fake_pylink)
        stages: list[tuple[str, str]] = []
        tracker.set_progress_hook(lambda stage, detail: stages.append((stage, detail)))
        fake_psychopy.keys.append("space")
        tracker.calibrate()
        assert stages == [
            ("calibration guide", "waiting for SPACE"),
            ("calibrating", "on the Host PC's setup screen"),
        ]
        tracker.set_progress_hook(None)
        fake_psychopy.keys.append("space")
        tracker.calibrate()
        assert len(stages) == 2


class TestHostResult:
    def test_a_good_calibration_carries_the_hosts_own_words(self, fake_pylink, fake_psychopy):
        tracker, connection, _ = configured(fake_pylink, calibration_type="HV5")
        connection.result_message = "GOOD"
        connection.eye = fake_pylink.module.RIGHT_EYE
        fake_psychopy.keys.append("space")
        result = tracker.calibrate()
        assert result.ok is True
        assert result.verdict == "calibrated"
        assert result.note == "Host PC: GOOD"
        assert (result.layout, result.n_targets, result.advance) == ("HV5", 5, "manual")
        assert result.eye == "right (reported by the tracker)"

    def test_the_eye_is_reported_as_the_tracker_names_it(self, fake_pylink, fake_psychopy):
        module = fake_pylink.module
        for eye, words in [
            (module.LEFT_EYE, "left (reported by the tracker)"),
            (module.BINOCULAR, "both (reported by the tracker; the session reads the left)"),
            (-1, "set on the Host PC (the tracker reports it when recording starts)"),
        ]:
            tracker, connection, _ = configured(fake_pylink)
            fake_pylink.connections.clear()
            connection.eye = eye
            fake_psychopy.keys.append("space")
            assert tracker.calibrate().eye == words

    def test_the_result_is_stamped_from_the_session_clock(self, fake_pylink, fake_psychopy):
        cfg = EyeTrackerConfig(backend="eyelink")
        clock = FakeClock()
        tracker = EyeLinkTracker(cfg, FakeDisplay(), SCREEN, clock)
        tracker.connect()
        tracker.configure(SCREEN, clock)
        clock.advance(12.5)
        fake_psychopy.keys.append("space")
        assert tracker.calibrate().t == 12.5

    def test_a_failed_calibration_is_loud(self, fake_pylink, fake_psychopy, caplog):
        tracker, connection, _ = configured(fake_pylink)
        connection.result_code = -1
        connection.result_message = "POOR"
        fake_psychopy.keys.append("space")
        with caplog.at_level(logging.ERROR):
            result = tracker.calibrate()
        assert result.ok is False
        assert result.verdict == "NOT calibrated"
        assert result.note == "Host PC: POOR (code -1) — calibrate again"
        assert "did not succeed" in caplog.text

    def test_no_reply_means_nothing_was_calibrated(self, fake_pylink, fake_psychopy, caplog):
        # The experimenter opened the setup screen, looked at the camera, and
        # left without pressing C: the Host PC has no result to give.
        tracker, connection, _ = configured(fake_pylink)
        connection.result_code = NO_REPLY
        connection.result_message = ""
        fake_psychopy.keys.append("space")
        with caplog.at_level(logging.WARNING):
            result = tracker.calibrate()
        assert result.ok is None and not result.aborted
        assert result.verdict == "result unknown"
        assert "was C pressed" in result.note
        assert "reports no calibration" in caplog.text

    def test_an_escape_on_the_host_is_an_abort(self, fake_pylink, fake_psychopy):
        tracker, connection, _ = configured(fake_pylink)
        connection.result_code = ABORT_RESULT
        connection.result_message = "ABORTED"
        fake_psychopy.keys.append("space")
        result = tracker.calibrate()
        assert result.aborted and result.ok is None
        assert "aborted with ESC" in result.note
        assert "Host PC: ABORTED" in result.note

    def test_a_code_without_a_message_is_still_named(self, fake_pylink, fake_psychopy):
        tracker, connection, _ = configured(fake_pylink)
        connection.result_message = ""
        fake_psychopy.keys.append("space")
        assert tracker.calibrate().note == "Host PC result code 0"

    def test_an_sdk_without_the_query_gives_an_unknown_result(
        self, fake_pylink, fake_psychopy, caplog
    ):
        tracker, connection, _ = configured(fake_pylink)
        connection.result_error = AttributeError("getCalibrationResult")
        fake_psychopy.keys.append("space")
        with caplog.at_level(logging.WARNING):
            result = tracker.calibrate()
        assert result.ok is None
        assert "check the Host PC" in result.note
        assert "reported no calibration result" in caplog.text

    def test_a_runtime_abort_inside_the_setup_leaves_the_tracker_clean(
        self, fake_pylink, fake_psychopy, caplog
    ):
        tracker, connection, _ = configured(fake_pylink)
        connection.setup_error = RuntimeError("ESC pressed")
        fake_psychopy.keys.append("space")
        with caplog.at_level(logging.WARNING):
            result = tracker.calibrate()
        assert connection.exits == 1
        assert result.aborted and result.ok is None
        assert result.note == "aborted on the Host PC (ESC pressed)"
        assert "aborted by the experimenter" in caplog.text


class TestGazeSampleTimes:
    """``GazeSample.t`` must say which sample this is (protocol.py): stamped
    when the sample is first read, and kept for as long as the link hands the
    same sample back. Restamped on every frame, a repeat would look like an
    eye that had stopped dead, and a velocity-ended landing would end
    mid-saccade."""

    def connected(self, fake_pylink, clock: FakeClock) -> tuple[EyeLinkTracker, FakeEyeLink]:
        tracker = EyeLinkTracker(EyeTrackerConfig(backend="eyelink"), None, SCREEN, clock)
        tracker.connect()
        (connection,) = fake_pylink.connections
        return tracker, connection

    def test_a_repeated_sample_keeps_the_time_it_was_first_read(self, fake_pylink):
        clock = FakeClock(start=1.0)
        tracker, connection = self.connected(fake_pylink, clock)
        connection.newest = FakeSample(100.0, 200.0, tracker_ms=5000.0)
        first = tracker.get_gaze()
        clock.advance(0.008)  # the next frame, and the link has nothing newer
        again = tracker.get_gaze()
        assert first is not None and again is not None
        assert first.t == again.t == 1.0

    def test_a_new_sample_is_stamped_on_the_session_clock(self, fake_pylink):
        clock = FakeClock(start=1.0)
        tracker, connection = self.connected(fake_pylink, clock)
        connection.newest = FakeSample(100.0, 200.0, tracker_ms=5000.0)
        tracker.get_gaze()
        clock.advance(0.008)
        connection.newest = FakeSample(110.0, 200.0, tracker_ms=5008.0)
        sample = tracker.get_gaze()
        # The session clock's time, never the tracker's 5008 ms: the two
        # clocks are aligned offline, not mixed online (invariant 2).
        assert sample is not None
        assert (sample.gx, sample.t) == (110.0, pytest.approx(1.008))

    def test_a_blink_is_still_none(self, fake_pylink):
        clock = FakeClock()
        tracker, connection = self.connected(fake_pylink, clock)
        connection.newest = FakeSample(-32768.0, -32768.0, tracker_ms=1.0)
        assert tracker.get_gaze() is None


# ---------------------------------------------------------------------------
# Dropout detection: a recording that dies mid-trial, against a simulated
# Host PC (fake_sdk.FakeEyeLinkHost) on the session's fake clock
# ---------------------------------------------------------------------------

# One display frame at 120 Hz: how often a session asks.
FRAME = 1 / 120


@pytest.fixture
def host_pylink(monkeypatch):
    """A pylink whose EyeLink() is a simulated Host PC recording at 1000 Hz
    on a fake clock that starts at 10 s."""
    return install_fake_pylink(monkeypatch, FakeClock(start=10.0))


def recording(host_pylink, **cfg_kwargs) -> tuple[EyeLinkTracker, FakeEyeLinkHost, FakeClock]:
    """A connected tracker with trial 1's recording segment open, as the
    runner opens it — and the Host PC behind it, and the clock."""
    clock = host_pylink.clock
    tracker = EyeLinkTracker(EyeTrackerConfig(backend="eyelink", **cfg_kwargs), None, SCREEN, clock)
    tracker.connect()
    (host,) = host_pylink.hosts
    tracker.start_trial(1, "attempt 1")
    return tracker, host, clock


def frames(tracker: EyeLinkTracker, clock: FakeClock, seconds: float) -> list[str | None]:
    """Ask the dropout check once a frame for ``seconds``, as the engine
    does, and return every answer."""
    answers = []
    for _ in range(round(seconds / FRAME)):
        clock.advance(FRAME)
        answers.append(tracker.recording_fault())
    return answers


class TestDropoutDetection:
    """recording_fault(): the stale-sample signal, and the Host PC asked why
    only once the samples have stopped."""

    def test_a_healthy_recording_reports_nothing_and_asks_the_host_nothing(self, host_pylink):
        tracker, host, clock = recording(host_pylink)
        assert frames(tracker, clock, 2.0) == [None] * 240
        # The healthy path reads the newest link sample and nothing else: no
        # isRecording() round trip in the frame loop.
        assert host.isrecording_calls == 0

    def test_a_host_pc_stop_is_reported_with_its_code(self, host_pylink):
        tracker, host, clock = recording(host_pylink)
        frames(tracker, clock, 0.1)
        host.host_stop(ABORT_EXPT)  # the operator aborted on the Host PC
        detail = tracker.recording_fault()
        assert detail is None  # the samples have not been missing long yet
        answers = frames(tracker, clock, 0.1)
        detail = next(answer for answer in answers if answer is not None)
        assert detail.startswith("no new sample from the EyeLink for ")
        assert "(limit 50 ms)" in detail
        assert "isRecording 3, ABORT_EXPT" in detail
        assert "its operator aborted the experiment" in detail
        # Asked once, at the dropout.
        assert host.isrecording_calls == 1

    def test_stale_samples_are_reported_after_the_limit_and_not_before(self, host_pylink):
        tracker, host, clock = recording(host_pylink)
        clock.advance(0.010)
        assert tracker.recording_fault() is None  # the newest sample, first seen now
        host.pull_cable()
        clock.advance(0.049)
        assert tracker.recording_fault() is None  # 49 ms: inside the 50 ms limit
        clock.advance(0.002)
        detail = tracker.recording_fault()
        assert detail is not None
        assert detail.startswith("no new sample from the EyeLink for 51 ms (limit 50 ms)")
        # The Host PC still believes it records: the samples stopped on the way.
        assert "still reports recording (isRecording 0)" in detail
        assert "check the link cable" in detail

    def test_the_limit_is_the_rigs(self, host_pylink):
        tracker, host, clock = recording(host_pylink, max_sample_gap_ms=200)
        host.pull_cable()
        assert frames(tracker, clock, 0.19) == [None] * round(0.19 / FRAME)
        assert any(frames(tracker, clock, 0.03))

    def test_a_blink_is_never_a_dropout(self, host_pylink):
        # A blink is samples arriving that say "no eye": MISSING_DATA gaze,
        # with a timestamp that still advances.
        tracker, host, clock = recording(host_pylink)
        host.gaze = (MISSING_DATA, MISSING_DATA)
        for _ in range(240):
            clock.advance(FRAME)
            assert tracker.recording_fault() is None
            assert tracker.get_gaze() is None  # no position, as a blink should be

    def test_a_sample_repeated_within_the_limit_is_not_a_dropout(self, host_pylink):
        tracker, host, clock = recording(host_pylink)
        clock.advance(0.010)
        first = tracker.get_gaze()
        # The same sample handed back twice in one instant, and then a gap
        # of 40 ms with nothing new: repeats, all inside the limit.
        assert tracker.recording_fault() is None
        assert tracker.get_gaze() == first
        host.pull_cable()
        assert frames(tracker, clock, 0.040) == [None] * round(0.040 / FRAME)
        host.delivering = True  # the link recovers before the limit
        assert frames(tracker, clock, 1.0) == [None] * 120

    def test_the_gap_between_trials_is_not_a_gap_in_the_trial(self, host_pylink):
        # No recording between trials means no samples, and the newest one
        # is from the last trial. Counted from the segment's start, an ITI
        # of any length is not a dropout.
        tracker, host, clock = recording(host_pylink)
        frames(tracker, clock, 0.5)
        tracker.stop_trial()
        clock.advance(5.0)
        tracker.start_trial(2, "attempt 1")
        assert tracker.recording_fault() is None
        assert frames(tracker, clock, 0.5) == [None] * 60

    def test_a_recording_that_never_delivers_is_a_dropout(self, host_pylink):
        # startRecording() succeeded, and nothing ever arrives.
        tracker, host, clock = recording(host_pylink)
        host.pull_cable()
        answers = frames(tracker, clock, 0.1)
        assert any(answer is not None for answer in answers)

    def test_a_link_that_is_down_is_named(self, host_pylink):
        tracker, host, clock = recording(host_pylink)
        frames(tracker, clock, 0.05)
        host.link_down()
        detail = next(answer for answer in frames(tracker, clock, 0.1) if answer is not None)
        assert "did not answer isRecording() (link terminated)" in detail
        assert "the link to it is down" in detail
        assert "100.1.1.1" in detail

    def test_a_code_alhazen_does_not_name_is_still_reported(self, host_pylink):
        tracker, host, clock = recording(host_pylink)
        host.host_stop(42)
        detail = next(answer for answer in frames(tracker, clock, 0.1) if answer is not None)
        assert "isRecording 42, a code alhazen does not name" in detail

    def test_once_reported_it_repeats_itself_and_asks_nothing_more(self, host_pylink):
        tracker, host, clock = recording(host_pylink)
        host.host_stop()
        detail = next(answer for answer in frames(tracker, clock, 0.1) if answer is not None)
        assert frames(tracker, clock, 0.1) == [detail] * 12
        assert host.isrecording_calls == 1

    def test_no_open_segment_has_nothing_to_report(self, host_pylink):
        clock = host_pylink.clock
        tracker = EyeLinkTracker(EyeTrackerConfig(backend="eyelink"), None, SCREEN, clock)
        tracker.connect()
        clock.advance(1.0)
        assert tracker.recording_fault() is None
        assert tracker.newest_sample_age_s() is None

    def test_the_age_is_the_newest_samples(self, host_pylink):
        tracker, host, clock = recording(host_pylink)
        clock.advance(0.010)
        assert tracker.newest_sample_age_s() == 0.0  # a new sample, first seen now
        host.pull_cable()
        clock.advance(0.030)
        assert tracker.newest_sample_age_s() == pytest.approx(0.030)


class TestAfterADropout:
    """What a dropout leaves for the rest of the trial and for the next one:
    the stop and the messages that follow it are logged, not raised, and the
    next start either records again or fails loudly, naming the rig."""

    def dropped(self, host_pylink, how: str = "link_down"):
        tracker, host, clock = recording(host_pylink)
        frames(tracker, clock, 0.05)
        getattr(host, how)()
        detail = next(answer for answer in frames(tracker, clock, 0.1) if answer is not None)
        return tracker, host, clock, detail

    def test_a_stop_after_a_dropout_is_logged_not_raised(self, host_pylink, caplog):
        tracker, host, clock, detail = self.dropped(host_pylink)
        with caplog.at_level(logging.WARNING):
            tracker.stop_trial()
        assert not tracker.is_recording()
        assert "stopRecording() failed after this trial's dropout" in caplog.text
        assert detail in caplog.text

    def test_a_stop_that_fails_without_a_dropout_still_raises(self, host_pylink):
        # Nothing explains it, so it is as unexpected as ever.
        tracker, host, clock = recording(host_pylink)
        host.link_down()
        with pytest.raises(RuntimeError, match="link terminated"):
            tracker.stop_trial()

    def test_messages_after_a_dropout_are_logged_not_raised(self, host_pylink, caplog):
        tracker, host, clock, detail = self.dropped(host_pylink)
        with caplog.at_level(logging.WARNING):
            tracker.send_message("trial_end")
        assert "message 'trial_end' was not written into the EDF" in caplog.text

    def test_a_message_that_fails_without_a_dropout_still_raises(self, host_pylink):
        tracker, host, clock = recording(host_pylink)
        host.link_down()
        with pytest.raises(RuntimeError, match="link terminated"):
            tracker.send_message("stim_on")

    def test_a_tracker_that_is_gone_refuses_the_next_trial_naming_the_rig(self, host_pylink):
        tracker, host, clock, detail = self.dropped(host_pylink)
        tracker.stop_trial()
        with pytest.raises(TrackerError) as excinfo:
            tracker.start_trial(2, "attempt 2")
        message = str(excinfo.value)
        assert message.startswith("EyeLink could not start recording at trial 2: the link failed")
        assert "the Host PC at 100.1.1.1" in message
        # What the previous trial died of, since it is almost always the same.
        assert f"The previous trial's recording had already been lost: {detail}." in message
        # Never pylink's bare error, but chained to it.
        assert isinstance(excinfo.value.__cause__, RuntimeError)

    def test_a_link_that_dies_as_recording_starts_keeps_the_rigs_words(
        self, host_pylink, monkeypatch, caplog
    ):
        # startRecording() succeeded, then the link went. The segment is
        # open, and the runner's finally stops it over the same dead link:
        # that stop must be logged, never raise pylink's bare error over the
        # clear one.
        tracker, host, clock = recording(host_pylink)
        tracker.stop_trial()
        started = FakeEyeLinkHost.startRecording

        def start_then_die(self, *flags):
            code = started(self, *flags)
            self.link_down()
            return code

        monkeypatch.setattr(FakeEyeLinkHost, "startRecording", start_then_die)
        with pytest.raises(TrackerError, match="the link failed as recording started"):
            tracker.start_trial(2, "attempt 1")
        with caplog.at_level(logging.WARNING):
            tracker.stop_trial()  # what the runner's finally does: no raise
        assert not tracker.is_recording()
        assert "stopRecording() failed after this trial's dropout" in caplog.text

    def test_a_start_refused_with_a_code_names_the_code(self, host_pylink):
        tracker, host, clock = recording(host_pylink)
        tracker.stop_trial()
        host.start_error = 7
        with pytest.raises(TrackerError, match=r"startRecording failed \(code 7\)"):
            tracker.start_trial(2, "attempt 1")
        assert not tracker.is_recording()

    def test_a_tracker_that_comes_back_records_again(self, host_pylink):
        tracker, host, clock, detail = self.dropped(host_pylink, how="host_stop")
        tracker.stop_trial()
        tracker.start_trial(2, "attempt 2")
        assert host.recordings_started == 2
        assert frames(tracker, clock, 0.5) == [None] * 60

    def test_simulate_dropout_stops_the_host_behind_the_backends_back(self, host_pylink):
        # What check-rig does to prove detection works on the real tracker.
        tracker, host, clock = recording(host_pylink)
        frames(tracker, clock, 0.05)
        said = tracker.simulate_dropout()
        assert "stopRecording() through pylink" in said
        assert tracker.is_recording()  # the backend was not told
        assert not host.recording
        detail = next(answer for answer in frames(tracker, clock, 0.1) if answer is not None)
        assert "isRecording -1, TRIAL_ERROR" in detail
        assert "the Host PC is no longer recording" in detail

    def test_simulate_dropout_needs_an_open_recording(self, host_pylink):
        tracker, host, clock = recording(host_pylink)
        tracker.stop_trial()
        with pytest.raises(TrackerError, match="needs an open recording"):
            tracker.simulate_dropout()


# ---------------------------------------------------------------------------
# Teardown: the EDF off the Host PC, and the link released whatever failed
# ---------------------------------------------------------------------------


def link_terminated(*args) -> None:
    """A pylink call on a link that died mid-teardown: pylink's own error."""
    raise RuntimeError("link terminated")


class TestShutdown:
    """shutdown() retrieves the EDF when a run asked for it, and says so
    loudly when it cannot; and it releases the link (close()) whatever
    happened before, the way the TRACKPixx3 backend releases its device."""

    def connected(self, host_pylink) -> tuple[EyeLinkTracker, FakeEyeLinkHost]:
        """A tracker that recorded one trial, as a session leaves it."""
        tracker, host, _ = recording(host_pylink)
        tracker.stop_trial()
        return tracker, host

    def test_the_edf_is_retrieved_and_the_link_released(self, host_pylink, tmp_path):
        # The happy path, unchanged: closed on the Host PC, copied into the
        # run directory under the name the runner gave, then disconnected.
        tracker, host = self.connected(host_pylink)
        tracker.shutdown(tmp_path / "run.edf")
        assert (tmp_path / "run.edf").read_bytes() == b"EDF"
        assert "clear_screen 0" in host.commands
        assert host.closed

    def test_a_link_down_with_a_run_behind_it_raises_naming_the_edf(self, host_pylink, tmp_path):
        # The bug this pins: a dead link was a WARNING and a return, so the
        # runner recorded the run as complete with its eye data still on the
        # Host PC, and the link was never closed.
        tracker, host = self.connected(host_pylink)
        host.link_down()
        destination = tmp_path / "run.edf"
        with pytest.raises(TrackerError) as excinfo:
            tracker.shutdown(destination)
        message = str(excinfo.value)
        # What failed, which file, where it is, and where it belongs.
        assert "the link to the Host PC is down at shutdown" in message
        assert "'alhazen.EDF'" in message
        assert "the Host PC at 100.1.1.1" in message
        assert f"copy it from there by hand to {destination}" in message
        assert not destination.exists()
        assert host.closed

    def test_a_link_down_with_no_run_behind_it_is_logged_and_released(self, host_pylink, caplog):
        # check-rig and the accuracy measurement pass no destination: their
        # recording is not data, so a dead link loses nothing and must not
        # turn a check into a failure. It is said, and the link released.
        tracker, host = self.connected(host_pylink)
        host.link_down()
        with caplog.at_level(logging.WARNING):
            tracker.shutdown(None)
        assert "EyeLink link is down at shutdown" in caplog.text
        assert "'alhazen.EDF'" in caplog.text
        assert host.closed

    @pytest.mark.parametrize("step", ["setOfflineMode", "closeDataFile"])
    def test_a_link_that_dies_with_no_run_behind_it_raises_a_tracker_error(
        self, host_pylink, monkeypatch, step
    ):
        # check-rig's shutdown(None). Nothing is lost, but a link that dies
        # while the EDF closes is still a fault, and it used to leave as
        # pylink's bare RuntimeError — past check-rig's `except AlhazenError`,
        # as a traceback instead of a failed check.
        tracker, host = self.connected(host_pylink)
        monkeypatch.setattr(FakeEyeLinkHost, step, link_terminated)
        with pytest.raises(TrackerError) as excinfo:
            tracker.shutdown(None)
        message = str(excinfo.value)
        assert "the EyeLink failed while closing the EDF (link terminated)" in message
        assert "'alhazen.EDF'" in message and "100.1.1.1" in message
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert host.closed

    def test_a_failed_transfer_still_releases_the_link(self, host_pylink, monkeypatch, tmp_path):
        tracker, host = self.connected(host_pylink)
        monkeypatch.setattr(FakeEyeLinkHost, "receiveDataFile", link_terminated)
        with pytest.raises(TrackerError, match=r"failed to retrieve EDF 'alhazen.EDF'"):
            tracker.shutdown(tmp_path / "run.edf")
        assert host.closed

    @pytest.mark.parametrize("step", ["setOfflineMode", "closeDataFile"])
    def test_a_link_that_dies_while_the_edf_is_closed_names_the_edf(
        self, host_pylink, monkeypatch, tmp_path, step
    ):
        # Up when shutdown() asked, gone a moment later: the same lost
        # retrieval as a link found down, said the same way — not as
        # pylink's bare error, which names no file.
        tracker, host = self.connected(host_pylink)
        monkeypatch.setattr(FakeEyeLinkHost, step, link_terminated)
        with pytest.raises(TrackerError) as excinfo:
            tracker.shutdown(tmp_path / "run.edf")
        message = str(excinfo.value)
        assert "the EyeLink failed while closing the EDF (link terminated)" in message
        assert "'alhazen.EDF'" in message
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert host.closed

    def test_a_trial_left_open_is_stopped_and_its_failure_named(
        self, host_pylink, monkeypatch, tmp_path
    ):
        # A session that ends mid-trial leaves its segment for shutdown() to
        # stop; a stop that fails there is as much a lost retrieval.
        tracker, host, _ = recording(host_pylink)
        monkeypatch.setattr(FakeEyeLinkHost, "stopRecording", link_terminated)
        with pytest.raises(TrackerError, match="'alhazen.EDF'"):
            tracker.shutdown(tmp_path / "run.edf")
        assert host.closed

    def test_a_close_that_fails_too_does_not_hide_the_first_error(
        self, host_pylink, monkeypatch, tmp_path, caplog
    ):
        # Two failures, one raise: the one naming the stranded EDF is what
        # propagates, and close()'s is logged rather than lost.
        tracker, host = self.connected(host_pylink)
        host.link_down()
        monkeypatch.setattr(FakeEyeLinkHost, "close", link_terminated)
        with caplog.at_level(logging.ERROR), pytest.raises(TrackerError, match="'alhazen.EDF'"):
            tracker.shutdown(tmp_path / "run.edf")
        assert "EyeLink close() failed as well (link terminated)" in caplog.text

    def test_a_close_that_fails_on_its_own_still_raises(self, host_pylink, monkeypatch, tmp_path):
        # The EDF is safe, but a link that would not close is still a fault.
        tracker, host = self.connected(host_pylink)
        monkeypatch.setattr(FakeEyeLinkHost, "close", link_terminated)
        with pytest.raises(RuntimeError, match="link terminated"):
            tracker.shutdown(tmp_path / "run.edf")
        assert (tmp_path / "run.edf").read_bytes() == b"EDF"


class TestAFailedConnect:
    """connect() opens the link and then the EDF on it. When the EDF step
    fails, the link it opened is closed before the error leaves — as the
    SpikeGLX and NI-DAQ backends clean up after their own failed connects —
    rather than left open behind a tracker that reports no connection."""

    @pytest.mark.parametrize("step", ["openDataFile", "sendCommand"])
    def test_the_link_is_released_and_the_error_is_the_rigs(self, host_pylink, monkeypatch, step):
        monkeypatch.setattr(FakeEyeLinkHost, step, link_terminated)
        tracker = EyeLinkTracker(
            EyeTrackerConfig(backend="eyelink"), None, SCREEN, host_pylink.clock
        )
        with pytest.raises(TrackerError, match="EyeLink connect to 100.1.1.1 failed"):
            tracker.connect()
        (host,) = host_pylink.hosts
        assert host.closed
        # Nothing is left for a later shutdown() to talk to: the link is gone.
        host.closed = False
        tracker.shutdown(None)
        assert not host.closed

    def test_a_close_that_fails_as_well_does_not_hide_the_connect_error(
        self, host_pylink, monkeypatch, caplog
    ):
        monkeypatch.setattr(FakeEyeLinkHost, "openDataFile", link_terminated)
        monkeypatch.setattr(FakeEyeLinkHost, "close", link_terminated)
        tracker = EyeLinkTracker(
            EyeTrackerConfig(backend="eyelink"), None, SCREEN, host_pylink.clock
        )
        with caplog.at_level(logging.ERROR), pytest.raises(TrackerError, match="connect"):
            tracker.connect()
        assert "EyeLink close() failed as well" in caplog.text
