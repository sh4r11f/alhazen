"""A measurement run's live monitor (modes/measure_live.py): what it shows of
the eye tracker, and when.

No hardware and no server: the tracker is a stand-in whose calibration reports
its stages and whose camera hands back a known frame, and the controller
records what it is asked to publish. What is checked is what would be quietly
wrong at the rig: a page that never shows the camera, a calibration that grows
a second walk because the rig validates in sessions, a progress line that does
not say which measurement it belongs to, an iris size change that waits for
the next refresh, an accuracy check that failed shown as passed.
"""

from __future__ import annotations

import json
import time
import urllib.request

import numpy as np
import pytest

from alhazen.config.models import EyeTrackerConfig, MonitorConfig, RigConfig
from alhazen.devices.eyetracker.protocol import (
    CalibrationResult,
    CalibrationTarget,
    CameraEye,
    CameraFrame,
)
from alhazen.display.screen import Screen
from alhazen.modes import measure_live
from alhazen.modes import measure_stats as stats
from alhazen.modes.measure import Measurement
from alhazen.modes.measure_jobs import MeasurementJob
from alhazen.modes.measure_live import (
    ACCURACY_LIMIT_DEG,
    LIVE_STATUS,
    REFRESH_S,
    MeasureLiveView,
    open_live_view,
    wants_live_view,
)
from alhazen.session.eyetracker import CAMERA_STREAM_S
from alhazen.testing import FakeClock

MONITOR = MonitorConfig(
    width_px=1920, height_px=1080, width_cm=52.0, distance_cm=57.0, refresh_rate_hz=60.0
)
SCREEN = Screen.from_monitor(MONITOR)
# A real tracker whose rig validates after every calibration, as the booth's
# does in a session: the setting a measurement run must not act on.
VIEWPIXX = EyeTrackerConfig(backend="viewpixx", validate_after_calibration=True)


def rig(eyetracker=VIEWPIXX) -> RigConfig:
    return RigConfig(monitor=MONITOR, data_root="data", devices={"eyetracker": eyetracker})


class Controller:
    """The page's server, as the view uses it: records every snapshot and
    camera frame, and hands back the tracker settings a test queued."""

    url = "http://127.0.0.1:4242/?token=abc"

    def __init__(self):
        self.states = []
        self.frames = []
        self.settings = []
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        return self.url

    def publish(self, state):
        self.states.append(state)

    def publish_camera(self, pixels, t, eyes=None):
        self.frames.append((pixels, t, eyes))

    def poll_settings(self):
        sent, self.settings = self.settings, []
        return sent

    def stop(self):
        self.stopped = True

    def panel(self, title):
        """The newest snapshot's panel of that title, or None."""
        return next((p for p in self.states[-1]["panels"] if p["title"] == title), None)


def calibrated(**kwargs) -> CalibrationResult:
    """A calibration that took, with one fitted target per corner of a square."""
    one = SCREEN.deg2px(1.0)
    targets = tuple(
        CalibrationTarget(
            target_px=(x * one, y * one),
            left_px=(x * one + 5.0, y * one),
            right_px=None,
            left_error_deg=0.2,
            right_error_deg=None,
        )
        for x, y in ((-8, 8), (8, 8), (-8, -8), (8, -8))
    )
    base = dict(
        ok=True, layout="HV9", n_targets=9, eye="left", advance="manual", t=3.0, targets=targets
    )
    return CalibrationResult(**{**base, **kwargs})


class Tracker:
    """A tracker with a camera and an iris size, whose calibration reports
    two stages through the progress hook before it returns its result."""

    def __init__(self, clock, result=None):
        self._clock = clock
        self.result = result if result is not None else calibrated()
        self.calibrations = 0
        self.hook = None
        self.iris = 40
        self.frames_read = 0

    def set_progress_hook(self, hook):
        self.hook = hook

    def calibrate(self):
        self.calibrations += 1
        for stage in ("target 1 of 9", "target 2 of 9"):
            # Past the progress throttle and the camera's frame interval, so
            # each report publishes and streams a frame.
            self._clock.advance(1.0)
            self.hook("calibrating", stage)
        return self.result

    def camera_frame(self):
        self.frames_read += 1
        return CameraFrame(
            pixels=np.zeros((4, 6), dtype=np.uint8),
            t=self._clock.now(),
            eyes=(CameraEye(eye="left", x=2.0, y=1.0, iris_px=3.0),),
        )

    def eye_status(self):
        return "eyes: left"

    def iris_size(self):
        return self.iris

    def set_iris_size(self, px):
        self.iris = int(px)
        return self.iris

    def get_gaze(self):  # only a validation walk would ask
        raise AssertionError("a measurement run's monitor must not sample gaze")


class NoCameraTracker:
    """A tracker the session cannot read an image from (the EyeLink)."""

    def __init__(self, result):
        self.result = result

    def calibrate(self):
        return self.result


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def controller():
    return Controller()


@pytest.fixture
def view(controller, clock):
    live = MeasureLiveView(controller, rig(), subject="s01", clock=clock)
    live.note("[1/2] Calibration")
    return live


def job(key, needs=(), unavailable=None) -> MeasurementJob:
    kwargs = {} if unavailable is None else {"unavailable": lambda _rig, _inputs: unavailable}
    return MeasurementJob(
        key,
        "Group",
        key,
        "a job",
        10,
        lambda ctx: Measurement(key, "ok"),
        frozenset(needs),
        **kwargs,
    )


class TestWhenARunHasOne:
    def test_a_tracker_measurement_on_a_real_tracker_has_one(self):
        assert wants_live_view(rig(), [job("a.one"), job("tracker.x", needs={"tracker"})], {})

    def test_a_run_with_no_tracker_measurement_has_none(self):
        assert not wants_live_view(rig(), [job("a.one", needs={"display"})], {})

    def test_a_tracker_measurement_that_cannot_run_does_not_count(self):
        unavailable = job("tracker.x", needs={"tracker"}, unavailable="the tracker is a stand-in")
        assert not wants_live_view(rig(), [unavailable], {})

    def test_a_rig_without_a_tracker_has_none(self):
        bare = RigConfig(monitor=MONITOR, data_root="data")
        assert not wants_live_view(bare, [job("tracker.x", needs={"tracker"})], {})


class TestBeforeTheTrackerOpens:
    def test_the_page_names_the_run_and_shows_what_is_still_to_come(self, view, controller):
        state = controller.states[-1]
        # The one status in which the page streams the camera, passes a
        # tracker setting on and shows the message it is sent.
        assert state["status"] == LIVE_STATUS == "calibrating"
        assert state["identity"]["task_name"] == "Measure rig"
        assert state["identity"]["subject"] == "s01"
        assert state["message"] == "[1/2] Calibration"
        # No trial panels, and no camera panel for a device not yet open.
        assert [p["title"] for p in state["panels"]] == ["Calibration", "Validation"]
        assert all(p["section"] == "Eye tracker" for p in state["panels"])
        assert "no calibration yet" in controller.panel("Calibration")["data"]["message"].lower()
        assert "no accuracy check yet" in controller.panel("Validation")["data"]["message"].lower()

    def test_idling_before_any_tracker_does_nothing(self, view, controller, clock):
        before = len(controller.states)
        clock.advance(10.0)
        view.idle()
        assert len(controller.states) == before and controller.frames == []

    def test_every_snapshot_has_a_later_revision(self, view, controller):
        view.note("[2/2] Accuracy, precision & gain")
        revisions = [state["revision"] for state in controller.states]
        assert revisions == sorted(set(revisions))


class TestCalibrating:
    def test_the_walk_is_the_trackers_own_and_runs_once(self, view, controller, clock):
        tracker = Tracker(clock)
        result = view.calibrate(tracker, "window")
        assert result is tracker.result
        assert tracker.calibrations == 1

    def test_each_stage_is_published_under_the_measurement_it_belongs_to(
        self, view, controller, clock
    ):
        view.calibrate(Tracker(clock), "window")
        messages = [state["message"] for state in controller.states]
        assert "[1/2] Calibration · calibrating: starting" in messages
        assert "[1/2] Calibration · calibrating: target 1 of 9" in messages
        assert "[1/2] Calibration · calibrating: target 2 of 9" in messages
        # And what it found, last.
        assert messages[-1].startswith("[1/2] Calibration · ")
        assert "HV9" in messages[-1]

    def test_the_camera_streams_with_its_eye_markers_while_it_runs(self, view, controller, clock):
        view.calibrate(Tracker(clock), "window")
        assert len(controller.frames) == 2  # one per progress report
        pixels, _t, eyes = controller.frames[0]
        assert pixels.shape == (4, 6) and pixels.dtype == np.uint8
        assert eyes == [{"eye": "left", "x": 2.0, "y": 1.0, "iris_px": 3.0}]

    def test_the_camera_panel_streams_and_carries_the_iris_size_control(
        self, view, controller, clock
    ):
        view.calibrate(Tracker(clock), "window")
        camera = controller.panel("Camera")["data"]
        assert camera["form"] == "image" and camera["stream"] is True
        # The pixels travel on the camera channel, never in a snapshot.
        assert camera["pixels"] == ""
        assert {"label": "Eyes", "value": "Left"} in camera["stats"]
        assert camera["controls"][0]["setting"] == "iris_size_px"
        assert camera["controls"][0]["value"] == 40

    def test_the_calibration_panel_plots_what_was_fitted(self, view, controller, clock):
        view.calibrate(Tracker(clock), "window")
        panel = controller.panel("Calibration")["data"]
        assert panel["form"] == "scatter"
        assert len(panel["targets"]) == 4
        assert panel["series"][0]["name"] == "left eye"
        assert {"label": "Verdict", "value": "Calibrated"} in panel["stats"]

    def test_no_validation_walk_follows_even_on_a_rig_that_validates_in_sessions(
        self, view, controller, clock, monkeypatch
    ):
        # The session monitor validates after a calibration when the rig says
        # so. In a measurement run that would be a second accuracy
        # measurement in front of the job that measures accuracy.
        def refuse(*args, **kwargs):
            raise AssertionError("a measurement run's calibration must not validate")

        monkeypatch.setattr("alhazen.session.eyetracker.validate", refuse)
        view.calibrate(Tracker(clock), "window")
        assert "no accuracy check yet" in controller.panel("Validation")["data"]["message"].lower()

    def test_a_failed_calibration_is_shown_as_failed(self, view, controller, clock):
        failed = calibrated(ok=False, targets=(), note="the device rejected the fit")
        result = view.calibrate(Tracker(clock, failed), "window")
        assert result.ok is False
        panel = controller.panel("Calibration")["data"]
        assert panel["status"] == "critical"
        assert panel["note"] == "The device rejected the fit"

    def test_a_tracker_without_a_camera_has_no_camera_panel(self, view, controller):
        view.calibrate(NoCameraTracker(calibrated()), "window")
        assert [p["title"] for p in controller.states[-1]["panels"]] == [
            "Calibration",
            "Validation",
        ]
        assert controller.frames == []

    def test_a_rig_that_configures_no_tracker_is_refused_by_name(self, controller, clock):
        bare = RigConfig(monitor=MONITOR, data_root="data")
        live = MeasureLiveView(controller, bare, subject=None, clock=clock)
        with pytest.raises(RuntimeError, match="configures none"):
            live.watch(Tracker(clock), "window")


class TestWaitingForTheOperator:
    def test_frames_stream_at_the_cameras_pace_not_on_every_pass(self, view, controller, clock):
        tracker = Tracker(clock)
        view.watch(tracker, "window")
        view.idle()
        view.idle()  # no time has passed: no second frame
        assert len(controller.frames) == 1
        clock.advance(CAMERA_STREAM_S)
        view.idle()
        assert len(controller.frames) == 2

    def test_the_panels_are_republished_about_once_a_second(self, view, controller, clock):
        view.watch(Tracker(clock), "window")
        before = len(controller.states)
        clock.advance(REFRESH_S / 2)
        view.idle()
        assert len(controller.states) == before
        clock.advance(REFRESH_S / 2)
        view.idle()
        assert len(controller.states) == before + 1
        assert controller.states[-1]["message"] == "[1/2] Calibration"

    def test_an_iris_size_from_the_page_is_applied_and_said_at_once(self, view, controller, clock):
        tracker = Tracker(clock)
        view.watch(tracker, "window")
        controller.settings = [("iris_size_px", 44)]
        view.idle()  # within the same second as the last publish
        assert tracker.iris == 44
        assert controller.states[-1]["message"] == (
            "[1/2] Calibration · Iris size set to 44 px (was 40 px)."
        )
        assert controller.panel("Camera")["data"]["controls"][0]["value"] == 44

    def test_watching_the_same_tracker_again_changes_nothing(self, view, controller, clock):
        tracker = Tracker(clock)
        view.watch(tracker, "window")
        before = len(controller.states)
        view.watch(tracker, "window")
        assert len(controller.states) == before


class TestTheAccuracyCheck:
    def _quality(self, offset_deg):
        one = SCREEN.deg2px(1.0)
        targets = [(0.0, 0.0), (-8 * one, 8 * one), (8 * one, -8 * one)]
        samples = [[(x + offset_deg * one, y)] * 5 for x, y in targets]
        return targets, stats.gaze_quality(targets, samples, SCREEN)

    def test_it_takes_the_validation_panels_place(self, view, controller):
        targets, quality = self._quality(0.3)
        view.show_accuracy(targets, quality, window_ms=500.0)
        panel = controller.panel("Validation")["data"]
        assert panel["form"] == "scatter"
        # The three targets, in degrees.
        assert panel["targets"] == [
            [0.0, 0.0],
            [pytest.approx(-8.0), pytest.approx(8.0)],
            [pytest.approx(8.0), pytest.approx(-8.0)],
        ]
        # The mean gaze on each target, 0.3 degrees to the right of it.
        assert panel["series"][0]["points"][0] == [pytest.approx(0.3), pytest.approx(0.0)]
        values = {stat["label"].lower(): stat for stat in panel["stats"]}
        assert values["mean error"]["value"] == "0.30°"
        assert values["worst"] == {"label": "Worst", "value": "0.30°"}
        assert values["verdict"] == {"label": "Verdict", "value": "Passed"}
        assert "Limit 1°" in panel["note"] and "500 ms" in panel["note"]
        assert "1: 0.30°, 2: 0.30°, 3: 0.30°" in panel["note"]
        assert controller.states[-1]["message"] == "[1/2] Calibration · accuracy check measured"

    def test_a_worst_target_past_the_limit_is_shown_as_failed(self, view, controller):
        targets, quality = self._quality(ACCURACY_LIMIT_DEG + 0.5)
        view.show_accuracy(targets, quality, window_ms=500.0)
        stats_shown = controller.panel("Validation")["data"]["stats"]
        values = {stat["label"].lower(): stat for stat in stats_shown}
        assert values["worst"]["status"] == "critical"
        assert values["verdict"] == {"label": "Verdict", "value": "Failed", "status": "critical"}

    def test_the_calibration_stays_on_the_page_beside_it(self, view, controller, clock):
        view.calibrate(Tracker(clock), "window")
        targets, quality = self._quality(0.3)
        view.show_accuracy(targets, quality, window_ms=500.0)
        assert controller.panel("Calibration")["data"]["form"] == "scatter"
        assert controller.panel("Validation")["data"]["form"] == "scatter"


class TestOpeningAndClosing:
    def test_opening_starts_the_server_and_publishes_a_first_page(self, monkeypatch, clock):
        made = []

        def controller(port, auto_open):
            made.append(Controller())
            made[-1].port, made[-1].auto_open = port, auto_open
            return made[-1]

        monkeypatch.setattr(measure_live, "LiveMonitorController", controller)
        live = open_live_view(rig(), subject="s01", clock=clock, open_browser=False)
        assert made[0].started and made[0].auto_open is False
        assert live.url == Controller.url
        assert made[0].states[-1]["message"].startswith("starting")

    def test_the_rigs_own_browser_setting_is_kept_unless_overridden(self, monkeypatch, clock):
        seen = []
        monkeypatch.setattr(
            measure_live,
            "LiveMonitorController",
            lambda port, auto_open: seen.append(auto_open) or Controller(),
        )
        open_live_view(rig(), subject=None, clock=clock)
        assert seen == [rig().live_monitor.auto_open]

    def test_closing_stops_the_server(self, view, controller):
        view.close()
        assert controller.stopped


class TestAgainstTheRealPage:
    """The view against the page's real server, over HTTP as the browser
    reads it: one run through a calibration, so the snapshot, the camera
    frame and a tracker setting are each seen to cross."""

    @staticmethod
    def _get_json(url):
        with urllib.request.urlopen(url, timeout=3) as response:
            return json.load(response)

    def _state_when(self, root, token, live, clock, shows):
        """The page's state once ``shows(state)`` is true.

        The view is idled meanwhile with a second passing each time, as a
        job waiting for the operator does. A snapshot published straight
        after another can be dropped on its way to the server (the
        controller never blocks to deliver one), and that republish is what
        puts the current panels on the page regardless.
        """
        deadline = time.monotonic() + 5
        state = {}
        while time.monotonic() < deadline:
            state = self._get_json(f"{root}/api/state?token={token}")
            if shows(state):
                return state
            clock.advance(REFRESH_S)
            live.idle()
            time.sleep(0.02)
        raise AssertionError(f"the page never showed it; last message {state.get('message')!r}")

    @staticmethod
    def _panel(state, title):
        return next((p["data"] for p in state.get("panels", []) if p["title"] == title), {})

    def test_a_calibration_reaches_the_browser_and_the_browser_reaches_the_tracker(self, clock):
        live = open_live_view(rig(), subject="s01", clock=clock, open_browser=False)
        token = live.url.partition("token=")[2]
        root = live.url.partition("/?")[0]
        tracker = Tracker(clock)
        try:
            live.note("[1/1] Calibration")
            live.calibrate(tracker, "window")

            # The snapshot: the status the page streams the camera in, the
            # three panels of a measurement run, and the calibration plotted.
            state = self._state_when(
                root,
                token,
                live,
                clock,
                lambda s: self._panel(s, "Calibration").get("form") == "scatter",
            )
            assert state["status"] == "calibrating"
            assert [panel["title"] for panel in state["panels"]] == [
                "Camera",
                "Calibration",
                "Validation",
            ]
            assert state["n_trials"] == 0

            # The camera frame, on its own channel, with its eye markers.
            request = f"{root}/api/camera?token={token}&after=0&wait_ms=3000"
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 200
                assert response.headers["X-Frame-Width"] == "6"
                assert json.loads(response.headers["X-Frame-Eyes"])[0]["eye"] == "left"
                assert response.read() == bytes(24)

            # An iris size typed on the page is accepted in this status, the
            # next idle pass hands it to the device, and the page shows it.
            post = urllib.request.Request(
                f"{root}/api/tracker",
                data=json.dumps(
                    {"setting": "iris_size_px", "value": 46, "request_id": "one"}
                ).encode(),
                headers={"Content-Type": "application/json", "X-Alhazen-Token": token},
                method="POST",
            )
            with urllib.request.urlopen(post, timeout=3) as response:
                assert response.status == 202
            state = self._state_when(
                root,
                token,
                live,
                clock,
                lambda s: self._panel(s, "Camera").get("controls", [{}])[0].get("value") == 46,
            )
            assert tracker.iris == 46
        finally:
            live.close()
