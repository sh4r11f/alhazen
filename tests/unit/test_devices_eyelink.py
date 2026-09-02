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
