"""The EyeLink's camera image goes to the live monitor, never to the subject's screen.

User decision (2026-10-15, round 5): the camera image the experimenter asks for
on the EyeLink's setup screen (ENTER) is not drawn on the subject's display —
nothing but targets goes there during a calibration. The experimenter sees it
where alhazen already shows camera images: the live monitor's Eye tracker
camera panel (streamed while the procedure runs) and the Trial view's camera
box (the last frame, with its age).

pylink and psychopy are stood in for: the fake Host PC's ``doTrackerSetup``
drives alhazen's calibration graphics through the callbacks the real pylink
calls (target, camera image with its caption, overlays). A software contract;
nothing here is evidence about a real EyeLink.
"""

from __future__ import annotations

import logging
import sys
import types
import urllib.request
from typing import Any

import numpy as np
import pytest
from test_calibration_targets import FakeWindow, Recorder, fake_visual

from alhazen.config.models import CalibrationTargetConfig, EyeTrackerConfig
from alhazen.devices.eyetracker.calibration import (
    draw_line_into,
    draw_lozenge_into,
    grey_camera_image,
    make_calibration_graphics,
)
from alhazen.devices.eyetracker.calibration_targets import CalibrationTargets
from alhazen.devices.eyetracker.eyelink import OK_RESULT, EyeLinkTracker
from alhazen.errors import TrackerError
from alhazen.live_monitor.runtime import LiveMonitorController
from alhazen.session.eyetracker import EyeTrackerMonitor, eye_markers
from alhazen.testing import FakeClock
from support import SCREEN

# The Host PC's palette: index 0 black, 1 mid grey, 2 white, 3 green (the pupil
# box), 4 red (the search limit). A 6x4 eye image in two rows of indices.
PALETTE = ([0, 128, 255, 0, 255], [0, 128, 255, 255, 0], [0, 128, 255, 0, 0])
ROWS = [[1, 1, 0, 0, 1, 1], [1, 2, 2, 1, 1, 1], [1, 1, 1, 1, 0, 1], [1, 1, 1, 1, 1, 1]]
CAPTION = "Left eye  thresholds 70 210"


class HostDisplay:
    """The part of pylink.EyeLinkCustomDisplay the adapter relies on: its
    draw_cross_hair() calls back into draw_line() and draw_lozenge(), with
    the colour indices the Host PC marks the image with."""

    def draw_cross_hair(self) -> None:
        self.draw_line(0, 3, 5, 3, 7)  # a crosshair line (CR_HAIR_COLOR)
        self.draw_lozenge(0, 0, 3, 2, 8)  # the pupil box (PUPIL_BOX_COLOR)


def install_sdk(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """pylink (connection + graphics registration) and psychopy (recording
    visual, scripted keys) stood in for; returns what the test drives."""
    recorder = Recorder()
    state = types.SimpleNamespace(recorder=recorder, graphics=None, connections=[], keys=[])

    class Connection:
        def __init__(self, host_ip: str) -> None:
            self.commands: list[str] = []
            # What the experimenter does on the Host PC's setup screen:
            # set by each test, run inside doTrackerSetup().
            self.on_setup = lambda graphics: None
            state.connections.append(self)

        def openDataFile(self, name: str) -> None:  # noqa: N802 - pylink's names
            pass

        def sendCommand(self, text: str) -> None:  # noqa: N802
            self.commands.append(text)

        def sendMessage(self, text: str) -> None:  # noqa: N802
            pass

        def setOfflineMode(self) -> None:  # noqa: N802
            pass

        def getTrackerVersionString(self) -> str:  # noqa: N802
            return "EYELINK CL 5.15"

        def doTrackerSetup(self) -> None:  # noqa: N802
            self.on_setup(state.graphics)

        def getCalibrationResult(self) -> int:  # noqa: N802
            return OK_RESULT

        def getCalibrationMessage(self) -> str:  # noqa: N802
            return "GOOD"

        def eyeAvailable(self) -> int:  # noqa: N802
            return 0

    pylink = types.ModuleType("pylink")
    pylink.EyeLink = Connection
    pylink.EyeLinkCustomDisplay = HostDisplay
    pylink.KeyInput = lambda code, modifier: (code, modifier)
    pylink.JUNK_KEY = 1
    pylink.OK_RESULT = OK_RESULT
    pylink.LEFT_EYE, pylink.RIGHT_EYE, pylink.BINOCULAR = 0, 1, 2
    pylink.CR_HAIR_COLOR, pylink.PUPIL_BOX_COLOR = 7, 8

    def open_graphics(graphics: Any) -> None:
        state.graphics = graphics

    pylink.openGraphicsEx = open_graphics
    event = types.ModuleType("psychopy.event")
    event.getKeys = lambda modifiers=False: []
    event.waitKeys = lambda keyList=None, maxWait=None: [state.keys.pop(0)] if state.keys else []
    event.Mouse = lambda win=None, visible=False: types.SimpleNamespace()
    visual = fake_visual(recorder)
    psychopy = types.ModuleType("psychopy")
    psychopy.event, psychopy.visual = event, visual
    for name, module in (
        ("pylink", pylink),
        ("psychopy", psychopy),
        ("psychopy.event", event),
        ("psychopy.visual", visual),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return state


class SubjectDisplay:
    """The subject's display: a window whose flips spend a frame, and any
    text panel put up (none may be)."""

    kind = "psychopy"

    def __init__(self, clock: FakeClock) -> None:
        self.window = FakeWindow(clock)
        self.texts: list[str] = []

    def flip(self, clear: bool = True) -> None:
        self.window.flip()

    def show_message(self, text: str, *, reflow: bool = True) -> None:
        self.texts.append(text)

    def show_menu(self, title: str, body: str, *, color: Any) -> None:
        self.texts.append(f"{title}\n{body}")


def send_camera_image(graphics: Any, frames: int = 1) -> None:
    """What the Host PC does when the experimenter presses ENTER on its setup
    screen: open the image, caption it, send its palette and its rows."""
    graphics.setup_image_display(6, 4)
    graphics.image_title(CAPTION)
    graphics.set_image_palette(*PALETTE)
    for _ in range(frames):
        for line, row in enumerate(ROWS, start=1):
            graphics.draw_image_line(6, line, len(ROWS), row)
    graphics.exit_image_display()


def a_setup_with_the_camera_image(graphics: Any) -> None:
    graphics.setup_cal_display()
    graphics.draw_cal_target(960.0, 540.0)
    graphics.erase_cal_target()
    send_camera_image(graphics, frames=3)
    graphics.draw_cal_target(960.0, 540.0)
    graphics.exit_cal_display()


class TestTheGraphics:
    def test_the_camera_image_is_never_drawn_and_goes_to_the_sink(self, monkeypatch):
        state = install_sdk(monkeypatch)
        clock = FakeClock()
        window = FakeWindow(clock)
        targets = CalibrationTargets(CalibrationTargetConfig())
        targets.prepare(SCREEN, 0.6)
        sent: list[tuple[np.ndarray, str | None]] = []
        graphics = make_calibration_graphics(
            None,
            window,
            SCREEN,
            targets=targets,
            now=clock.now,
            camera_sink=lambda pixels, caption: sent.append((pixels, caption)),
        )
        graphics.draw_cal_target(960.0, 540.0)
        target_draws = len(state.recorder.draws)
        flips = window.flips
        send_camera_image(graphics, frames=2)
        # Nothing was made or drawn for the image: no ImageStim, no draw.
        assert all(stim.kind != "image" for stim in state.recorder.made)
        assert len(state.recorder.draws) == target_draws
        # The screen was emptied when the image opened (the target taken
        # down) and when it closed: two flips with nothing drawn.
        assert window.flips == flips + 2
        assert len(sent) == 2
        for pixels, caption in sent:
            assert pixels.dtype == np.uint8 and pixels.shape == (4, 6)
            assert caption == CAPTION
        # The pulsating-target redraw does not bring the target back over the
        # image's empty screen.
        graphics.get_input_key()
        assert len(state.recorder.draws) == target_draws

    def test_the_overlays_are_white_and_the_eye_image_is_its_luminance(self, monkeypatch):
        install_sdk(monkeypatch)
        clock = FakeClock()
        sent: list[np.ndarray] = []
        graphics = make_calibration_graphics(
            None,
            FakeWindow(clock),
            SCREEN,
            now=clock.now,
            camera_sink=lambda pixels, caption: sent.append(pixels),
        )
        send_camera_image(graphics)
        (pixels,) = sent
        plain = np.array(PALETTE, dtype=np.uint8).T[np.array(ROWS)]
        luma = np.rint(plain.astype(float) @ np.array([0.299, 0.587, 0.114])).astype(np.uint8)
        # Where HostDisplay.draw_cross_hair marked the image, by the module's
        # own (separately tested) line and lozenge drawing.
        marked = plain.copy()
        draw_line_into(marked, 0, 3, 5, 3, (255, 0, 255))
        draw_lozenge_into(marked, 0, 0, 3, 2, (255, 0, 255))
        overlay = np.any(marked != plain, axis=-1)
        assert overlay.any() and not overlay.all()
        assert (pixels[overlay] == 255).all()
        assert (pixels[~overlay] == luma[~overlay]).all()

    def test_without_a_sink_the_image_goes_nowhere_and_nothing_is_drawn(self, monkeypatch):
        state = install_sdk(monkeypatch)
        clock = FakeClock()
        graphics = make_calibration_graphics(None, FakeWindow(clock), SCREEN, now=clock.now)
        send_camera_image(graphics)
        assert state.recorder.draws == []

    def test_grey_conversion(self):
        image = np.array([[[0, 255, 0], [10, 10, 10]], [[255, 0, 0], [200, 200, 200]]], np.uint8)
        marked = image.copy()
        marked[1, 1] = (0, 255, 0)  # an overlay painted over a grey pixel
        grey = grey_camera_image(image, marked)
        assert grey.dtype == np.uint8 and grey.shape == (2, 2)
        assert grey.tolist() == [[150, 10], [76, 255]]

    def test_the_subject_windows_mouse_cannot_move_the_hosts_search_limits(self, monkeypatch):
        install_sdk(monkeypatch)
        clock = FakeClock()
        graphics = make_calibration_graphics(None, FakeWindow(clock), SCREEN, now=clock.now)
        graphics.setup_image_display(384, 320)
        assert graphics.get_mouse_state() == ((192.0, 160.0), 0)


def eyelink(monkeypatch, clock: FakeClock) -> tuple[EyeLinkTracker, Any, SubjectDisplay]:
    state = install_sdk(monkeypatch)
    display = SubjectDisplay(clock)
    tracker = EyeLinkTracker(EyeTrackerConfig(backend="eyelink"), display, SCREEN, clock)
    tracker.connect()
    tracker.configure(SCREEN, clock)
    state.connections[0].on_setup = a_setup_with_the_camera_image
    return tracker, state, display


class TestTheTracker:
    def test_before_any_camera_image_the_tracker_says_how_to_get_one(self, monkeypatch):
        tracker, _state, _display = eyelink(monkeypatch, FakeClock())
        with pytest.raises(TrackerError, match="press ENTER there during a calibration"):
            tracker.camera_frame()

    def test_a_camera_setup_leaves_the_newest_image_stamped_when_it_came(self, monkeypatch):
        clock = FakeClock()
        tracker, state, display = eyelink(monkeypatch, clock)
        stages: list[tuple[str, str]] = []
        arrived: list[float] = []

        def hook(stage: str, detail: str) -> None:
            stages.append((stage, detail))
            if stage == "camera image":
                arrived.append(clock.now())

        tracker.set_progress_hook(hook)
        state.keys.append("space")
        state.connections[0].on_setup = lambda graphics: (
            graphics.setup_image_display(6, 4),
            graphics.image_title(CAPTION),
            graphics.set_image_palette(*PALETTE),
            [graphics.draw_image_line(6, i, 4, row) for i, row in enumerate(ROWS, start=1)],
            clock.advance(2.0),
            [graphics.draw_image_line(6, i, 4, row) for i, row in enumerate(ROWS, start=1)],
            graphics.exit_image_display(),
        )
        result = tracker.calibrate()
        assert result.ok is True
        frame = tracker.camera_frame()
        assert frame.t == arrived[-1] and len(arrived) == 2
        assert frame.pixels.shape == (4, 6) and frame.eyes is None
        assert ("camera image", f"{CAPTION} · shown on the live monitor") in stages
        assert display.texts == []
        assert all(stim.kind != "image" for stim in state.recorder.made)


def calibrated_monitor(monkeypatch) -> tuple[EyeTrackerMonitor, Any, list[Any], SubjectDisplay]:
    clock = FakeClock()
    tracker, state, display = eyelink(monkeypatch, clock)
    monitor = EyeTrackerMonitor(
        tracker,
        display,
        SCREEN,
        clock,
        EyeTrackerConfig(backend="eyelink", validate_after_calibration=False),
        poll_keys=lambda: [],
    )
    sent: list[Any] = []
    monitor.camera_sink = sent.append
    published: list[tuple[str, str]] = []
    monitor.publisher = lambda status, message: published.append((status, message))
    state.keys.append("space")
    monitor.calibrate()
    return monitor, state, sent, display


class TestTheSession:
    def test_the_live_monitor_receives_the_frames_during_the_setup(self, monkeypatch):
        monitor, state, sent, display = calibrated_monitor(monkeypatch)
        assert sent, "no camera frame reached the live monitor's camera channel"
        assert all(frame.pixels.shape == (4, 6) for frame in sent)
        # On the subject's screen: targets only, never the image.
        assert {draw[0] for draw in state.recorder.draws} == {"circle"}
        assert display.texts == []

    def test_a_kept_frame_is_sent_once_not_streamed_as_if_live(self, monkeypatch):
        monitor, _state, sent, _display = calibrated_monitor(monkeypatch)
        clock = monitor._clock
        clock.advance(0.1)
        monitor.stream_camera()  # the newest image, if the throttle held it back
        count = len(sent)
        assert sent[-1] is monitor._tracker.camera_frame()
        for _ in range(30):  # a long pause: the same kept image every call
            clock.advance(0.1)
            monitor.stream_camera()
        assert len(sent) == count

    def test_the_panels_show_the_image_and_say_when_it_is_live(self, monkeypatch):
        monitor, _state, _sent, _display = calibrated_monitor(monkeypatch)
        monitor.camera_sink = None  # the copy a state publish carries
        camera = next(p for p in monitor.panels(camera=True) if p["title"] == "Camera")
        assert camera["data"]["form"] == "image"
        assert camera["data"]["width"] == 6 and camera["data"]["height"] == 4
        assert camera["data"]["note"].startswith("live while the Host PC's camera setup screen")
        trial = monitor.trial_camera()
        assert trial["has_frames"] is True and trial["live"] is False
        assert "only during camera setup" in trial["reason"]

    def test_before_a_camera_setup_the_panel_says_how_to_see_one(self, monkeypatch, caplog):
        clock = FakeClock()
        tracker, _state, display = eyelink(monkeypatch, clock)
        monitor = EyeTrackerMonitor(
            tracker,
            display,
            SCREEN,
            clock,
            EyeTrackerConfig(backend="eyelink"),
            poll_keys=lambda: [],
        )
        with caplog.at_level(logging.INFO):
            camera = next(p for p in monitor.panels(camera=True) if p["title"] == "Camera")
            monitor.panels(camera=True)
        assert camera["data"]["form"] == "empty"
        assert "press ENTER there during a calibration" in camera["data"]["message"]
        # Not a fault: noted once, at info, never a warning.
        notes = [r for r in caplog.records if "camera image not read" in r.getMessage()]
        assert [r.levelno for r in notes] == [logging.INFO]

    def test_the_frame_reaches_a_real_live_monitor_page(self, monkeypatch):
        """End to end through the live monitor's camera channel: the bytes the
        page fetches are the grey image the Host PC sent."""
        controller = LiveMonitorController(auto_open=False)
        url = controller.start()
        token = url.partition("token=")[2]
        root = url.partition("/?")[0]
        try:
            clock = FakeClock()
            tracker, state, display = eyelink(monkeypatch, clock)
            monitor = EyeTrackerMonitor(
                tracker,
                display,
                SCREEN,
                clock,
                EyeTrackerConfig(backend="eyelink", validate_after_calibration=False),
                poll_keys=lambda: [],
            )
            monitor.camera_sink = lambda frame: controller.publish_camera(
                frame.pixels, frame.t, eyes=eye_markers(frame)
            )
            state.keys.append("space")
            monitor.calibrate()
            request = urllib.request.urlopen(
                f"{root}/api/camera?token={token}&after=0&wait_ms=3000", timeout=10
            )
            assert request.status == 200
            assert (request.headers["X-Frame-Width"], request.headers["X-Frame-Height"]) == (
                "6",
                "4",
            )
            assert request.read() == tracker.camera_frame().pixels.tobytes()
        finally:
            controller.stop()
