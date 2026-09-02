"""The session's eye-tracker monitor (session/eyetracker.py).

The monitor sits between the runner and the tracker: it runs the three
procedures, keeps their results, tells the dashboard, and files the events.
It measures nothing itself, so it is tested against a tracker whose gaze
follows the target (the procedures' own test subject) and trackers that
report a chosen calibration outcome, with the dashboard publisher and the
event bus replaced by lists.
"""

from __future__ import annotations

import base64
import logging
import math
from typing import Any

import numpy as np
import pytest

from alhazen.config.models import EyeTrackerConfig
from alhazen.devices.eyetracker import procedures
from alhazen.devices.eyetracker.procedures import DriftResult, TargetError, ValidationResult
from alhazen.devices.eyetracker.protocol import CalibrationResult, CameraFrame, GazeSample
from alhazen.devices.eyetracker.scripted import ScriptedTracker
from alhazen.devices.eyetracker.viewpixx import eye_status_text
from alhazen.errors import TrackerError
from alhazen.session.eyetracker import (
    PROCEDURE_STATUS,
    PROGRESS_PUBLISH_S,
    SECTION,
    EyeTrackerMonitor,
    encode_image,
    eye_stat,
)
from alhazen.testing import FakeClock, FakeDisplay, FakeStimulus
from support import FRAME_S, SCREEN


class FollowingTracker(ScriptedTracker):
    """Gaze wherever the target on screen is, plus a fixed offset, reported
    in screen px so the procedures' conversion is exercised. ``looking_at``
    is set by the target factory the ``session`` fixture installs."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__([], clock)
        self.looking_at = (0.0, 0.0)  # centered px
        self.offset_px = (0.0, 0.0)
        self.calibration: CalibrationResult | None = None
        self.calibrate_error: Exception | None = None
        self.hooks: list[Any] = []

    def get_gaze(self) -> GazeSample | None:
        cx = self.looking_at[0] + self.offset_px[0]
        cy = self.looking_at[1] + self.offset_px[1]
        gx, gy = SCREEN.centered_to_screen(cx, cy)
        return GazeSample(gx=gx, gy=gy, t=self._clock.now())

    def calibrate(self) -> CalibrationResult | None:  # type: ignore[override]
        if self.calibrate_error is not None:
            raise self.calibrate_error
        return self.calibration


class HookedTracker(FollowingTracker):
    """A tracker with the optional progress hook: reports two stages a
    second apart, the way a real procedure takes time."""

    def set_progress_hook(self, hook: Any) -> None:
        self.hooks.append(hook)

    def calibrate(self) -> CalibrationResult | None:  # type: ignore[override]
        hook = self.hooks[-1]
        if hook is not None:
            self._clock.advance(1.0)  # type: ignore[attr-defined]
            hook("calibration guide", "waiting for SPACE")
            self._clock.advance(1.0)  # type: ignore[attr-defined]
            hook("calibrating", "target 1 of 5")
        return super().calibrate()


class CameraTracker(FollowingTracker):
    """A tracker with a camera and an eye status, like the viewpixx."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.pixels = np.arange(12, dtype=np.uint8).reshape(3, 4)
        self.camera_error: TrackerError | None = None
        self.eyes = (True, True)
        self.reads = 0

    def camera_frame(self) -> CameraFrame:
        self.reads += 1
        if self.camera_error is not None:
            raise self.camera_error
        return CameraFrame(self.pixels, t=self._clock.now())

    def eye_status(self) -> str:
        return eye_status_text(*self.eyes)


def result(ok: bool | None, *, aborted: bool = False, note: str = "") -> CalibrationResult:
    return CalibrationResult(
        ok=ok,
        layout="HV5",
        n_targets=5,
        eye="left",
        advance="manual",
        t=1.0,
        note=note,
        aborted=aborted,
    )


class Session:
    """A monitor with its collaborators, and lists where the runner's
    publisher and event bus would be."""

    def __init__(self, tracker_cls: type = FollowingTracker, **cfg_kwargs: Any) -> None:
        self.clock = FakeClock()
        self.display = FakeDisplay(self.clock, FRAME_S)
        self.tracker = tracker_cls(self.clock)
        self.cfg = EyeTrackerConfig(backend="scripted", **cfg_kwargs)
        self.monitor = EyeTrackerMonitor(
            self.tracker, self.display, SCREEN, self.clock, self.cfg, poll_keys=lambda: []
        )
        self.published: list[tuple[str, str]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.monitor.publisher = lambda status, message: self.published.append((status, message))
        self.monitor.emit = lambda name, payload: self.events.append((name, payload))

    def make_target(self, display, screen, size, color, pos) -> FakeStimulus:
        """Stands in for make_fixation: the subject looks where the target is."""
        self.tracker.looking_at = pos
        return FakeStimulus("target")

    def panel(self, title: str, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("camera", False)
        panels = {p["title"]: p for p in self.monitor.panels(**kwargs)}
        return panels[title]


@pytest.fixture
def session(monkeypatch):
    """A Session factory whose targets the fake subject can see: the
    procedures draw a recording stand-in on a simulated display, which has
    no position, so the fixation factory is replaced with one that tells
    the tracker where to look."""

    def make(tracker_cls: type = FollowingTracker, **cfg_kwargs: Any) -> Session:
        s = Session(tracker_cls, **cfg_kwargs)
        monkeypatch.setattr(procedures, "make_fixation", s.make_target)
        return s

    return make


# ----------------------------------------------------------------------
# calibrate()
# ----------------------------------------------------------------------


class TestCalibrate:
    def test_tracker_without_a_result_gets_a_record_that_says_so(self, session) -> None:
        s = session(validate_after_calibration=False)
        got = s.monitor.calibrate()
        assert got.ok is None
        assert got.layout == "HV5" and got.n_targets == 5
        assert got.advance == "manual"
        assert "no eye" in got.eye
        assert got.note == "this tracker reports no calibration result"
        assert got.t == s.clock.now()
        assert s.monitor.calibration is got

    def test_result_is_kept_and_filed_as_an_event(self, session) -> None:
        s = session(validate_after_calibration=False)
        s.tracker.calibration = result(True, note="Host PC: GOOD")
        got = s.monitor.calibrate()
        assert got is s.tracker.calibration
        assert s.events == [
            (
                "CALIBRATION",
                {
                    "ok": True,
                    "aborted": False,
                    "layout": "HV5",
                    "n_targets": 5,
                    "eye": "left",
                    "advance": "manual",
                    "note": "Host PC: GOOD",
                },
            )
        ]

    def test_good_calibration_resets_the_corrections_and_the_old_results(self, session) -> None:
        s = session(validate_after_calibration=False)
        s.monitor.correction.shift_by(10.0, -4.0, 0.5)
        s.monitor.validation = ValidationResult((), 1.0, 0.5)
        s.monitor.drift = DriftResult((0, 0), None, None, None, 3.0, False, 0.5, "x")
        s.tracker.calibration = result(True)
        s.monitor.calibrate()
        assert s.monitor.correction.offset == (0.0, 0.0)
        assert s.monitor.correction.t == 1.0  # stamped with the calibration's time
        assert s.monitor.validation is None and s.monitor.drift is None

    @pytest.mark.parametrize("outcome", [result(False), result(None, aborted=True), result(None)])
    def test_other_outcomes_keep_the_corrections(self, session, outcome) -> None:
        s = session(validate_after_calibration=False)
        s.monitor.correction.shift_by(10.0, -4.0, 0.5)
        s.tracker.calibration = outcome
        s.monitor.calibrate()
        assert s.monitor.correction.offset == (10.0, -4.0)

    def test_summary_is_logged_at_warning_when_not_ok(self, session, caplog) -> None:
        s = session(validate_after_calibration=False)
        s.tracker.calibration = result(False, note="Host PC: POOR")
        with caplog.at_level(logging.WARNING, logger="alhazen.session.eyetracker"):
            s.monitor.calibrate()
        assert any("NOT calibrated" in r.message for r in caplog.records)

    def test_tracker_error_propagates(self, session) -> None:
        s = session(validate_after_calibration=False)
        s.tracker.calibrate_error = TrackerError("no display")
        with pytest.raises(TrackerError, match="no display"):
            s.monitor.calibrate()
        assert s.monitor.calibration is None

    def test_progress_hook_is_set_for_the_call_and_cleared_after(self, session) -> None:
        s = session(HookedTracker, validate_after_calibration=False)
        s.monitor.calibrate()
        assert len(s.tracker.hooks) == 2
        assert callable(s.tracker.hooks[0]) and s.tracker.hooks[1] is None

    def test_progress_hook_is_cleared_even_when_calibration_raises(self, session) -> None:
        s = session(HookedTracker, validate_after_calibration=False)
        s.tracker.calibrate_error = TrackerError("boom")
        with pytest.raises(TrackerError):
            s.monitor.calibrate()
        assert s.tracker.hooks[-1] is None


class TestAutoValidate:
    def test_validates_after_a_good_calibration_when_the_rig_asks(self, session) -> None:
        s = session(validate_after_calibration=True)
        s.tracker.calibration = result(True)
        s.monitor.calibrate()
        assert s.monitor.validation is not None
        assert s.monitor.validation.accepted
        assert [name for name, _ in s.events] == ["CALIBRATION", "VALIDATION"]

    def test_validates_after_a_calibration_of_unknown_outcome(self, session) -> None:
        # The mouse "calibrates" to nothing; a validation of it is still a
        # validation, and the only measure the session has.
        s = session(validate_after_calibration=True)
        s.monitor.calibrate()
        assert s.monitor.validation is not None

    @pytest.mark.parametrize("outcome", [result(False), result(None, aborted=True)])
    def test_no_validation_of_a_failed_or_aborted_calibration(self, session, outcome) -> None:
        s = session(validate_after_calibration=True)
        s.tracker.calibration = outcome
        s.monitor.calibrate()
        assert s.monitor.validation is None
        assert [name for name, _ in s.events] == ["CALIBRATION"]

    def test_not_when_the_rig_says_no(self, session) -> None:
        s = session(validate_after_calibration=False)
        s.tracker.calibration = result(True)
        s.monitor.calibrate()
        assert s.monitor.validation is None


# ----------------------------------------------------------------------
# validate() and drift_correct()
# ----------------------------------------------------------------------


class TestProcedures:
    def test_validation_runs_in_auto_mode_on_a_simulated_display(self, session) -> None:
        # Nobody is at the keyboard of a simulated display: manual mode would
        # wait for an accept that never comes, so the monitor asks for auto.
        s = session(calibration_advance="manual")
        got = s.monitor.validate()
        assert got.advance == "auto"
        assert got.accepted and got.n_missed == 0 and len(got.targets) == 5
        assert s.monitor.validation is got
        assert s.events[-1] == ("VALIDATION", got.payload())

    def test_validation_uses_the_corrections_in_force(self, session) -> None:
        s = session()
        s.tracker.offset_px = (40.0, 0.0)  # one degree to the right, uncorrected
        s.monitor.correction.shift_by(-40.0, 0.0, 0.0)
        got = s.monitor.validate()
        assert got.max_error_deg == pytest.approx(0.0, abs=1e-6)

    def test_drift_correction_applies_and_accumulates(self, session) -> None:
        s = session(drift_max_deg=3.0)
        s.tracker.offset_px = (40.0, 0.0)
        got = s.monitor.drift_correct()
        assert got.applied and got.offset_deg == pytest.approx(1.0)
        assert s.monitor.correction.offset == pytest.approx((-40.0, 0.0))
        assert s.monitor.drift is got
        assert s.events[-1] == ("DRIFT_CORRECTION", got.payload())

    def test_drift_beyond_the_limit_is_refused(self, session) -> None:
        s = session(drift_max_deg=0.5)
        s.tracker.offset_px = (40.0, 0.0)
        got = s.monitor.drift_correct()
        assert not got.applied
        assert s.monitor.correction.offset == (0.0, 0.0)


# ----------------------------------------------------------------------
# Reporting to the dashboard
# ----------------------------------------------------------------------


class TestPublishing:
    def test_stage_boundaries_always_publish_with_the_procedure_status(self, session) -> None:
        s = session(validate_after_calibration=False)
        s.monitor.calibrate()
        assert s.published == [(PROCEDURE_STATUS, "calibrating: starting")]

    def test_progress_is_throttled(self, session) -> None:
        s = session(validate_after_calibration=False)
        s.monitor._stage("validating", "starting")
        # Eight reports an eighth of a second apart (exact in binary, so the
        # comparison is not at the mercy of rounding): only those
        # PROGRESS_PUBLISH_S after the last publish get through.
        for i in range(8):
            s.clock.advance(0.125)
            s.monitor._on_progress("validating", f"target {i}")
        assert PROGRESS_PUBLISH_S == 0.5
        assert s.published == [
            (PROCEDURE_STATUS, "validating: starting"),
            (PROCEDURE_STATUS, "validating: target 3"),
            (PROCEDURE_STATUS, "validating: target 7"),
        ]

    def test_backend_progress_reaches_the_publisher(self, session) -> None:
        s = session(HookedTracker, validate_after_calibration=False)
        s.monitor.calibrate()
        assert s.published == [
            (PROCEDURE_STATUS, "calibrating: starting"),
            (PROCEDURE_STATUS, "calibration guide: waiting for SPACE"),
            (PROCEDURE_STATUS, "calibrating: target 1 of 5"),
        ]

    def test_a_validation_reports_each_target(self, session) -> None:
        s = session()
        s.monitor.validate()
        messages = [m for _, m in s.published]
        assert messages[0] == "validating: starting"
        assert "validating: target 1 of 5" in messages
        assert "validating: target 5 of 5" in messages

    def test_without_a_publisher_nothing_breaks(self, session) -> None:
        s = session()
        s.monitor.publisher = None
        s.monitor.emit = None
        s.monitor.validate()
        assert s.monitor.validation is not None


# ----------------------------------------------------------------------
# Panels
# ----------------------------------------------------------------------


class TestPanelSet:
    def test_every_panel_files_under_the_eye_tracker_section(self, session) -> None:
        s = session()
        panels = s.monitor.panels(camera=False)
        assert [p["title"] for p in panels] == ["Calibration", "Validation", "Drift correction"]
        assert all(p["section"] == SECTION for p in panels)
        assert all(set(p) == {"title", "section", "data"} for p in panels)

    def test_a_tracker_with_a_camera_gets_a_camera_panel_first(self, session) -> None:
        s = session(CameraTracker)
        assert s.monitor.has_camera
        assert [p["title"] for p in s.monitor.panels(camera=False)][0] == "Camera"

    def test_a_tracker_without_one_has_no_camera(self, session) -> None:
        assert not session().monitor.has_camera

    def test_empty_panels_say_which_key_runs_the_procedure(self, session) -> None:
        s = session()
        assert "press C" in s.panel("Calibration")["data"]["message"]
        assert "press V" in s.panel("Validation")["data"]["message"]
        assert "press D" in s.panel("Drift correction")["data"]["message"]
        assert all(s.panel(t)["data"]["form"] == "empty" for t in ("Calibration", "Validation"))


class TestCameraPanel:
    def test_no_frame_yet(self, session) -> None:
        s = session(CameraTracker)
        data = s.panel("Camera")["data"]
        assert data == {
            "form": "empty",
            "message": "no camera image yet — read while paused or calibrating",
        }
        assert s.tracker.reads == 0

    def test_reads_a_frame_when_asked(self, session) -> None:
        s = session(CameraTracker)
        s.clock.advance(2.0)
        data = s.panel("Camera", camera=True)["data"]
        assert data["form"] == "image"
        assert (data["width"], data["height"]) == (4, 3)
        assert base64.b64decode(data["pixels"]) == bytes(range(12))
        assert data["stats"] == [
            {"label": "read at", "value": "2.0 s"},
            {"label": "eyes", "value": "both tracked"},
        ]
        assert data["note"] == "live while paused or calibrating"

    def test_no_eye_in_the_image_is_a_critical_stat(self, session) -> None:
        s = session(CameraTracker)
        s.tracker.eyes = (False, False)
        eyes = s.panel("Camera", camera=True)["data"]["stats"][1]
        assert eyes["status"] == "critical"
        assert "NO EYE" in eyes["value"]

    def test_one_eye_only(self, session) -> None:
        s = session(CameraTracker)
        s.tracker.eyes = (False, True)
        eyes = s.panel("Camera", camera=True)["data"]["stats"][1]
        assert eyes == {"label": "eyes", "value": "right only"}

    def test_last_frame_is_shown_when_not_reading(self, session) -> None:
        s = session(CameraTracker)
        s.panel("Camera", camera=True)
        data = s.panel("Camera", camera=False)["data"]
        assert data["form"] == "image"
        assert s.tracker.reads == 1
        assert data["note"] == "last frame; live again when paused"
        # No eye line: the status would be as stale as the frame, whose
        # timestamp at least says how old it is.
        assert [st["label"] for st in data["stats"]] == ["read at"]

    def test_saved_copy_leaves_the_pixels_out(self, session) -> None:
        s = session(CameraTracker)
        s.panel("Camera", camera=True)
        data = s.panel("Camera", camera=False, image=False)["data"]
        assert data["form"] == "image" and data["pixels"] == ""
        assert data["note"] == "image left out of the saved copy"

    def test_a_failed_read_is_reported_and_warned_once_per_reason(self, session, caplog) -> None:
        s = session(CameraTracker)
        s.tracker.camera_error = TrackerError("TPxGetImagePtr returned no image")
        with caplog.at_level(logging.WARNING, logger="alhazen.session.eyetracker"):
            first = s.panel("Camera", camera=True)["data"]
            second = s.panel("Camera", camera=True)["data"]
        assert first == {
            "form": "empty",
            "message": "camera image unavailable: TPxGetImagePtr returned no image",
        }
        assert second == first
        warnings = [r for r in caplog.records if "camera image not read" in r.message]
        assert len(warnings) == 1

    def test_a_failed_read_after_a_frame_keeps_the_frame_and_says_so(self, session) -> None:
        s = session(CameraTracker)
        s.panel("Camera", camera=True)
        s.tracker.camera_error = TrackerError("device busy")
        data = s.panel("Camera", camera=True)["data"]
        assert data["form"] == "image"
        assert data["note"] == "last frame; the newest read failed: device busy"

    def test_a_new_reason_is_warned_again(self, session, caplog) -> None:
        s = session(CameraTracker)
        with caplog.at_level(logging.WARNING, logger="alhazen.session.eyetracker"):
            s.tracker.camera_error = TrackerError("one")
            s.panel("Camera", camera=True)
            s.tracker.camera_error = TrackerError("two")
            s.panel("Camera", camera=True)
        assert [r.message for r in caplog.records] == [
            "camera image not read: one",
            "camera image not read: two",
        ]

    def test_eye_status_split_matches_the_viewpixx_wording(self) -> None:
        # eye_stat() splits on the viewpixx prefix; if the wording there
        # changes, this is the test that says so.
        assert eye_stat(eye_status_text(True, True)) == {"label": "eyes", "value": "both tracked"}
        assert eye_stat(eye_status_text(True, False)) == {"label": "eyes", "value": "left only"}
        assert eye_stat(eye_status_text(False, False))["status"] == "critical"

    def test_encode_image_is_row_major_bytes(self) -> None:
        pixels = np.array([[1, 2], [3, 4]], dtype=np.uint8)
        assert base64.b64decode(encode_image(pixels)) == b"\x01\x02\x03\x04"


class TestCalibrationPanel:
    def test_result_as_a_stat_tile(self, session) -> None:
        s = session(validate_after_calibration=False)
        s.tracker.calibration = result(True, note="Host PC: GOOD")
        s.monitor.calibrate()
        data = s.panel("Calibration")["data"]
        assert data == {
            "form": "stat",
            "value": "calibrated",
            "unit": "",
            "label": "HV5 · 5 targets · manual",
            "secondary": "eye: left · at 1 s",
            "note": "Host PC: GOOD",
        }

    def test_failed_calibration_is_critical(self, session) -> None:
        s = session(validate_after_calibration=False)
        s.tracker.calibration = result(False, note="Host PC: POOR")
        s.monitor.calibrate()
        data = s.panel("Calibration")["data"]
        assert data["value"] == "NOT calibrated" and data["status"] == "critical"

    def test_aborted_and_unknown_are_not_critical(self, session) -> None:
        s = session(validate_after_calibration=False)
        s.tracker.calibration = result(None, aborted=True)
        s.monitor.calibrate()
        data = s.panel("Calibration")["data"]
        assert data["value"] == "aborted" and "status" not in data
        s.tracker.calibration = result(None)
        s.monitor.calibrate()
        data = s.panel("Calibration")["data"]
        assert data["value"] == "result unknown" and "status" not in data


class TestValidationPanel:
    def test_scatter_in_degrees_with_the_verdict(self, session) -> None:
        s = session(accuracy_max_deg=1.0)
        s.tracker.offset_px = (20.0, 0.0)  # half a degree right of every target
        s.monitor.validate()
        data = s.panel("Validation")["data"]
        assert data["form"] == "scatter" and data["equal_aspect"] is True
        assert (data["x_label"], data["y_label"]) == ("x (deg)", "y (deg)")
        assert len(data["targets"]) == 5 and data["targets"][0] == [0.0, 0.0]
        [series] = data["series"]
        assert series["name"] == "gaze" and series["slot"] == 1
        # Targets and points are in the same units: every gaze point sits
        # half a degree to the right of its target.
        for target, point in zip(data["targets"], series["points"], strict=True):
            assert point[0] - target[0] == pytest.approx(0.5)
            assert point[1] - target[1] == pytest.approx(0.0)
        stats = {st["label"]: st for st in data["stats"]}
        assert stats["mean error"]["value"] == "0.50°"
        assert stats["worst"]["value"] == "0.50°" and "status" not in stats["worst"]
        assert stats["missed"]["value"] == "0"
        assert stats["verdict"] == {"label": "verdict", "value": "passed"}
        assert data["note"].startswith("limit 1° · auto · 1: 0.50°, 2: 0.50°")

    def test_failed_validation_is_critical(self, session) -> None:
        s = session(accuracy_max_deg=0.25)
        s.tracker.offset_px = (20.0, 0.0)
        s.monitor.validate()
        stats = {st["label"]: st for st in s.panel("Validation")["data"]["stats"]}
        assert stats["worst"]["status"] == "critical"
        assert stats["verdict"] == {"label": "verdict", "value": "FAILED", "status": "critical"}

    def test_missed_targets_and_aborts_are_shown(self, session) -> None:
        s = session()
        s.monitor.validation = ValidationResult(
            targets=(
                TargetError((0.0, 0.0), (4.0, 0.0), 0.1, 10),
                TargetError((400.0, 0.0), None, None, 0),
            ),
            threshold_deg=1.0,
            t=3.0,
            advance="manual",
            aborted=True,
        )
        data = s.panel("Validation")["data"]
        assert data["targets"] == [[0.0, 0.0], [10.0, 0.0]]
        assert data["series"][0]["points"] == [[0.1, 0.0]]
        stats = {st["label"]: st for st in data["stats"]}
        assert stats["missed"]["value"] == "1"
        assert stats["verdict"]["value"] == "aborted"
        assert data["note"] == "limit 1° · manual · 1: 0.10°, 2: missed"

    def test_nothing_measured(self, session) -> None:
        s = session()
        s.monitor.validation = ValidationResult(targets=(), threshold_deg=1.0, t=3.0)
        stats = {st["label"]: st for st in s.panel("Validation")["data"]["stats"]}
        assert stats["mean error"]["value"] == "—" and stats["worst"]["value"] == "—"
        assert stats["verdict"]["value"] == "FAILED"


class TestDriftPanel:
    def test_applied(self, session) -> None:
        s = session(drift_max_deg=3.0)
        s.tracker.offset_px = (40.0, -20.0)
        s.monitor.drift_correct()
        data = s.panel("Drift correction")["data"]
        assert data["form"] == "stat"
        assert data["value"] == f"{math.hypot(1.0, 0.5):.2f}" and data["unit"] == "°"
        assert data["label"] == "applied"
        assert data["secondary"].startswith("total correction -40, +20 px · limit 3°")
        assert "status" not in data

    def test_refused_is_critical(self, session) -> None:
        s = session(drift_max_deg=0.5)
        s.tracker.offset_px = (40.0, 0.0)
        s.monitor.drift_correct()
        data = s.panel("Drift correction")["data"]
        assert data["label"] == "REFUSED" and data["status"] == "critical"
        assert data["note"] == "too large for a drift — recalibrate"
        assert data["secondary"].startswith("total correction +0, +0 px")

    def test_not_measured_is_critical(self, session) -> None:
        s = session()
        s.monitor.drift = DriftResult((0.0, 0.0), None, None, None, 3.0, False, 2.0, "aborted")
        data = s.panel("Drift correction")["data"]
        assert data["value"] == "—" and data["label"] == "not applied"
        assert data["note"] == "aborted" and data["status"] == "critical"
