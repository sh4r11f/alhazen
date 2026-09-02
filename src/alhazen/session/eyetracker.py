"""The session's view of its eye tracker: run the procedures, keep their
results, show them on the dashboard.

A tracker knows how to calibrate itself; it does not know that the session
wants a validation right after, that a new calibration voids the drift
corrections measured against the old one, that the dashboard should be
told at every stage, or that the results belong in the events table. That
is what :class:`EyeTrackerMonitor` holds, in one place, between the runner
(which calls it from the pause menu and the dashboard's buttons) and the
devices layer (which does the measuring)::

    pause menu / dashboard button
              │
              ▼
    EyeTrackerMonitor ── calibrate() ──▶ tracker.calibrate()
         │   │   │      validate() ────▶ procedures.validate()
         │   │   │      drift_correct() ▶ procedures.drift_correct()
         │   │   └── correction (GazeCorrection) ──▶ the input provider
         │   └────── emit(CALIBRATION / VALIDATION / DRIFT_CORRECTION) ──▶ event bus
         └────────── panels() ──▶ the "Eye tracker" dashboard section
                       (camera image, calibration, validation, drift)

Every result is a plain record from the devices layer; this module never
measures anything itself. The three capabilities a tracker may have beyond
the protocol — ``set_progress_hook``, ``camera_frame``, ``eye_status`` — are
looked up with ``hasattr`` here and nowhere else, so a tracker without them
(the EyeLink has no camera the session can read; the mouse has no eye) gets
the same session with those panels saying so.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from typing import Any

from alhazen.config.models import EyeTrackerConfig
from alhazen.core.clock import Clock
from alhazen.devices.eyetracker.guide import TARGET_COUNTS
from alhazen.devices.eyetracker.procedures import (
    Advance,
    DriftResult,
    GazeCorrection,
    ValidationResult,
    drift_correct,
    validate,
)
from alhazen.devices.eyetracker.protocol import CalibrationResult, CameraFrame, EyeTracker
from alhazen.display.backend import DisplayBackend
from alhazen.display.screen import Screen
from alhazen.errors import TrackerError

log = logging.getLogger(__name__)

# The dashboard section every panel here files under.
SECTION = "Eye tracker"

# How often a procedure's progress reaches the dashboard. A publish rebuilds
# every panel and, on a rig with a camera, reads a frame; the viewpixx
# calibration reports every 0.1 s refresh, which is more often than a page
# needs redrawing.
PROGRESS_PUBLISH_S = 0.5

# The dashboard status while any procedure runs. One word for all three: the
# server refuses commands unless the status is "paused", so this is also what
# keeps a second button press from landing mid-procedure.
PROCEDURE_STATUS = "calibrating"

# Where the panel's key hints send the experimenter. The pause menu lists the
# same keys (session/pause.py) and the dashboard has buttons for them.
CALIBRATE_HINT = "press C while paused, or the dashboard's Calibrate button"
VALIDATE_HINT = "press V while paused, or the dashboard's Validate button"
DRIFT_HINT = "press D while paused, or the dashboard's Drift-correct button"

# What a publish carries about a procedure: (status, message). Whatever the
# callable returns is ignored, so the runner's own publish method fits as is.
Publisher = Callable[[str, str], object]
# What a finished procedure emits: (event name, payload).
Emitter = Callable[[str, dict[str, Any]], object]


def eye_stat(status: str) -> dict[str, Any]:
    """The camera panel's eye line, as a stat strip entry.

    The viewpixx status text is either ``eyes: <which>`` or a loud sentence
    saying no eye is in the image; the first becomes a plain value, the
    second a critical one, so the strip is red exactly when the camera sees
    nothing to track.
    """
    if status.startswith("eyes: "):
        return {"label": "eyes", "value": status[len("eyes: ") :]}
    return {"label": "eyes", "value": status, "status": "critical"}


def encode_image(pixels: Any) -> str:
    """A grayscale frame as base64 of its row-major bytes — what the page's
    ``image`` form decodes straight into a canvas."""
    return base64.b64encode(pixels.tobytes()).decode("ascii")


class EyeTrackerMonitor:
    """One session's eye-tracker procedures and what they found.

    ``poll_keys`` is where the procedures read the experimenter's keys from;
    ``publisher`` and ``emit`` are set by the runner once it exists, because
    the dashboard publish and the event bus are its.
    """

    def __init__(
        self,
        tracker: EyeTracker,
        display: DisplayBackend,
        screen: Screen,
        clock: Clock,
        cfg: EyeTrackerConfig,
        *,
        poll_keys: Callable[[], list[str]],
    ) -> None:
        self._tracker = tracker
        self._display = display
        self._screen = screen
        self._clock = clock
        self._cfg = cfg
        self._poll_keys = poll_keys
        # The drift corrections in force, consulted by the input provider on
        # every gaze position (session/builder.py make_input_provider).
        self.correction = GazeCorrection()
        self.calibration: CalibrationResult | None = None
        self.validation: ValidationResult | None = None
        self.drift: DriftResult | None = None
        self.publisher: Publisher | None = None
        self.emit: Emitter | None = None
        self._last_publish = float("-inf")
        # The newest camera frame, and why the last read failed (warned once
        # per distinct reason, not once per publish).
        self._frame: CameraFrame | None = None
        self._camera_fault: str | None = None

    # ------------------------------------------------------------------
    # Procedures
    # ------------------------------------------------------------------

    def calibrate(self) -> CalibrationResult:
        """Calibrate, then validate if the rig asks for it.

        A calibration the tracker reports as good replaces the gaze model, so
        the corrections and the validation measured against the old one are
        cleared here — a "passed" from an hour ago must not sit on the
        dashboard next to a model it never measured.
        """
        self._stage("calibrating", "starting")
        # The viewpixx and EyeLink backends report their stages; a tracker
        # without the hook simply reports nothing between start and result.
        set_hook = getattr(self._tracker, "set_progress_hook", None)
        if set_hook is not None:
            set_hook(self._on_progress)
        try:
            reported = self._tracker.calibrate()
        finally:
            if set_hook is not None:
                set_hook(None)
        result = reported if reported is not None else self._no_result()
        self.calibration = result
        log.log(logging.INFO if result.ok else logging.WARNING, result.summary())
        if result.ok:
            self.correction.reset(result.t)
            self.validation = None
            self.drift = None
        self._emit(
            "CALIBRATION",
            {
                "ok": result.ok,
                "aborted": result.aborted,
                "layout": result.layout,
                "n_targets": result.n_targets,
                "eye": result.eye,
                "advance": result.advance,
                "note": result.note,
            },
        )
        # No validation of a calibration that did not happen (aborted) or
        # that the tracker itself already calls bad — the experimenter's next
        # move is to calibrate again, not to measure how bad.
        if self._cfg.validate_after_calibration and not result.aborted and result.ok is not False:
            self.validate()
        return result

    def _no_result(self) -> CalibrationResult:
        """The record for a tracker whose calibrate() reports nothing (the
        mouse, a scripted replay): a calibration of unknown outcome, said so."""
        return CalibrationResult(
            ok=None,
            layout=self._cfg.calibration_type,
            n_targets=TARGET_COUNTS.get(self._cfg.calibration_type, 0),
            eye="none (this tracker has no eye to calibrate)",
            advance=self._cfg.calibration_advance,
            t=self._clock.now(),
            note="this tracker reports no calibration result",
        )

    def validate(self) -> ValidationResult:
        """Measure the calibration's error on its own targets; see procedures.py."""
        self._stage("validating", "starting")
        result = validate(
            self._tracker,
            self._display,
            self._screen,
            self._clock,
            self._cfg,
            poll_keys=self._poll_keys,
            correction=self.correction,
            advance=self._advance(),
            progress=self._on_progress,
        )
        self.validation = result
        self._emit("VALIDATION", result.payload())
        return result

    def drift_correct(self) -> DriftResult:
        """Measure the offset on a centre target and apply it if it is a drift."""
        self._stage("drift correcting", "starting")
        result = drift_correct(
            self._tracker,
            self._display,
            self._screen,
            self._clock,
            self._cfg,
            self.correction,
            poll_keys=self._poll_keys,
            advance=self._advance(),
            progress=self._on_progress,
        )
        self.drift = result
        self._emit("DRIFT_CORRECTION", result.payload())
        return result

    def _advance(self) -> Advance | None:
        """The rig's advance mode — except on a simulated display, where
        nobody is at the keyboard to accept a target, so only ``auto`` can
        ever finish. None means "the rig's setting"."""
        return "auto" if self._display.kind == "simulated" else None

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _stage(self, stage: str, detail: str) -> None:
        """A stage boundary: always published, and it restarts the throttle."""
        log.info("eye tracker: %s — %s", stage, detail)
        self._last_publish = self._clock.now()
        if self.publisher is not None:
            self.publisher(PROCEDURE_STATUS, f"{stage}: {detail}")

    def _on_progress(self, stage: str, detail: str) -> None:
        """A procedure's progress line, published at most every PROGRESS_PUBLISH_S."""
        now = self._clock.now()
        if now - self._last_publish < PROGRESS_PUBLISH_S:
            return
        self._last_publish = now
        if self.publisher is not None:
            self.publisher(PROCEDURE_STATUS, f"{stage}: {detail}")

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.emit is not None:
            self.emit(name, payload)

    # ------------------------------------------------------------------
    # Dashboard panels
    # ------------------------------------------------------------------

    @property
    def has_camera(self) -> bool:
        """Whether this tracker can hand the session a camera image at all."""
        return hasattr(self._tracker, "camera_frame")

    def panels(self, *, camera: bool, image: bool = True) -> list[dict[str, Any]]:
        """The "Eye tracker" section: camera, calibration, validation, drift.

        ``camera`` says whether to read a fresh frame now — true while the
        session is paused or a procedure is running, when the device is not
        busy with a trial and the image is what the experimenter is looking
        for. ``image=False`` leaves the pixels out (the copy saved to disk
        at teardown keeps the numbers, not a photograph of the subject).
        """
        panels: list[dict[str, Any]] = []
        if self.has_camera:
            camera_data = self._camera(camera, image)
            panels.append({"title": "Camera", "section": SECTION, "data": camera_data})
        panels.append({"title": "Calibration", "section": SECTION, "data": self._calibration()})
        panels.append({"title": "Validation", "section": SECTION, "data": self._validation()})
        panels.append({"title": "Drift correction", "section": SECTION, "data": self._drift()})
        return panels

    def _camera(self, read: bool, image: bool) -> dict[str, Any]:
        if read:
            self._read_camera()
        if self._frame is None:
            message = "no camera image yet — read while paused or calibrating"
            if self._camera_fault is not None:
                message = f"camera image unavailable: {self._camera_fault}"
            return {"form": "empty", "message": message}
        frame = self._frame
        height, width = frame.pixels.shape[:2]
        data: dict[str, Any] = {
            "form": "image",
            "width": int(width),
            "height": int(height),
            "pixels": encode_image(frame.pixels) if image else "",
            "stats": [{"label": "read at", "value": f"{frame.t:.1f} s"}],
        }
        status = getattr(self._tracker, "eye_status", None)
        if status is not None and read:
            try:
                data["stats"].append(eye_stat(status()))
            except TrackerError as e:
                data["stats"].append({"label": "eyes", "value": str(e), "status": "critical"})
        note = "live while paused or calibrating" if read else "last frame; live again when paused"
        if self._camera_fault is not None:
            note = f"last frame; the newest read failed: {self._camera_fault}"
        if not image:
            note = "image left out of the saved copy"
        data["note"] = note
        return data

    def _read_camera(self) -> None:
        """One frame from the tracker, or the reason there is none.

        A failure is reported on the panel and warned about once per reason:
        the same fault on every publish of a long pause is noise, a new fault
        is news.
        """
        try:
            self._frame = self._tracker.camera_frame()  # type: ignore[attr-defined]
        except TrackerError as e:
            reason = str(e)
            if reason != self._camera_fault:
                log.warning("camera image not read: %s", reason)
            self._camera_fault = reason
            return
        self._camera_fault = None

    def _calibration(self) -> dict[str, Any]:
        result = self.calibration
        if result is None:
            return {"form": "empty", "message": f"no calibration this session — {CALIBRATE_HINT}"}
        data: dict[str, Any] = {
            "form": "stat",
            "value": result.verdict,
            "unit": "",
            "label": f"{result.layout} · {result.n_targets} targets · {result.advance}",
            "secondary": f"eye: {result.eye} · at {result.t:.0f} s",
            "note": result.note,
        }
        if result.ok is False:
            data["status"] = "critical"
        return data

    def _validation(self) -> dict[str, Any]:
        result = self.validation
        if result is None:
            return {"form": "empty", "message": f"no validation yet — {VALIDATE_HINT}"}
        deg = self._screen.px2deg
        targets = [[deg(t.target_px[0]), deg(t.target_px[1])] for t in result.targets]
        points = [
            [deg(t.gaze_px[0]), deg(t.gaze_px[1])] for t in result.targets if t.gaze_px is not None
        ]
        mean, worst = result.mean_error_deg, result.max_error_deg
        stats: list[dict[str, Any]] = [
            {"label": "mean error", "value": f"{mean:.2f}°" if mean is not None else "—"},
            {
                "label": "worst",
                "value": f"{worst:.2f}°" if worst is not None else "—",
                **(
                    {"status": "critical"}
                    if worst is not None and worst > result.threshold_deg
                    else {}
                ),
            },
            {"label": "missed", "value": str(result.n_missed)},
        ]
        if result.aborted:
            stats.append({"label": "verdict", "value": "aborted"})
        elif result.accepted:
            stats.append({"label": "verdict", "value": "passed"})
        else:
            stats.append({"label": "verdict", "value": "FAILED", "status": "critical"})
        # Per-target errors in reading order, so a bad corner can be named.
        per_target = ", ".join(
            f"{i + 1}: {t.error_deg:.2f}°" if t.error_deg is not None else f"{i + 1}: missed"
            for i, t in enumerate(result.targets)
        )
        return {
            "form": "scatter",
            "series": [{"name": "gaze", "slot": 1, "points": points}],
            "targets": targets,
            "x_label": "x (deg)",
            "y_label": "y (deg)",
            "equal_aspect": True,
            "stats": stats,
            "color_label": "",
            "note": f"limit {result.threshold_deg:g}° · {result.advance} · {per_target}",
        }

    def _drift(self) -> dict[str, Any]:
        result = self.drift
        dx, dy = self.correction.offset
        total = f"total correction {dx:+.0f}, {dy:+.0f} px"
        if result is None:
            return {"form": "empty", "message": f"no drift correction yet — {DRIFT_HINT}"}
        if result.offset_deg is None:
            data: dict[str, Any] = {
                "form": "stat",
                "value": "—",
                "unit": "",
                "label": "not applied",
                "secondary": total,
                "note": result.note or "no gaze measured",
                "status": "critical",
            }
            return data
        data = {
            "form": "stat",
            "value": f"{result.offset_deg:.2f}",
            "unit": "°",
            "label": "applied" if result.applied else "REFUSED",
            "secondary": f"{total} · limit {result.max_deg:g}° · at {result.t:.0f} s",
            "note": result.note,
        }
        if not result.applied:
            data["status"] = "critical"
        return data
