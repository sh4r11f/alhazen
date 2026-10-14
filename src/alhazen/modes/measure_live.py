"""The live monitor of a measurement run: the eye tracker's camera, its
calibration and the accuracy check.

A session shows its eye tracker on the live monitor: the camera image while a
procedure runs, the calibration's fitted points, the validation's errors
(session/eyetracker.py). Measure mode ran the same calibration with none of
that, so whoever stood at the rig could not see whether the camera had the
eye. This module gives a measurement run the same page, built from the same
two parts a session uses::

    run_jobs ───────────── note("[2/3] Calibration") ─────┐
    tracker.calibration ── calibrate(tracker, display) ───┤
    tracker.accuracy ───── idle() while it waits ─────────┼──▶ MeasureLiveView
                      └─── show_accuracy(...) ────────────┘         │
                                                                    │
              EyeTrackerMonitor (session/eyetracker.py) ◀───────────┤
                the camera frames, the iris size control,           │
                the Camera and Calibration panels                   │
                                                                    ▼
              LiveMonitorController (live_monitor/runtime.py)
                the page, served from its own process

What it adds is small: which panels a measurement run shows (the camera, the
calibration, and the accuracy check in the place a session's validation
takes), and when they are published.

Nothing here measures anything. The calibration is the tracker's own, started
exactly as before; the accuracy numbers are ``measure_stats.gaze_quality``'s,
handed over once the job has them. A run without a live monitor never builds
this object, and every job then runs as it always did.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from alhazen.config.models import RigConfig
from alhazen.core.clock import Clock
from alhazen.devices.eyetracker.protocol import CameraFrame
from alhazen.display.screen import Screen
from alhazen.live_monitor.runtime import LiveMonitorController, live_monitor_state
from alhazen.live_monitor.spec import LiveMonitorSpec
from alhazen.modes.measure_jobs import MeasurementJob
from alhazen.session.eyetracker import PROCEDURE_STATUS, SECTION, EyeTrackerMonitor, eye_markers

log = logging.getLogger(__name__)

# The status every publish of a measurement run carries. It is the session's
# status for "an eye-tracker procedure is running", and the one status in
# which the page does all three things a measurement needs of it: it streams
# the camera, it passes a tracker setting (the iris size) on to the device,
# and it shows the message it is sent. The pause menu's buttons stay inert in
# it, which is right: a measurement run has no pause menu to send them to.
LIVE_STATUS = PROCEDURE_STATUS

# How often the panels are republished while a job waits for the operator, so
# the camera panel's eye line ("eyes: both", or the tracker's own sentence
# when it sees none) follows the eye. The same second a paused session uses
# (session/pause_control.py CAMERA_REFRESH_S); the image itself streams on its
# own channel, about fifteen frames a second.
REFRESH_S = 1.0

# What the page's header shows where a session shows its task.
RUN_NAME = "Measure rig"

# The accuracy check's limit, in degrees: the worst target's error above which
# the measurement fails (measure_builtin._tracker_accuracy holds the rule).
ACCURACY_LIMIT_DEG = 1.0

NO_CALIBRATION = "no calibration yet in this run — the Calibration measurement runs it"
NO_ACCURACY = (
    "no accuracy check yet in this run — the Accuracy, precision & gain measurement runs it, "
    "on fresh fixations after the calibration"
)


def wants_live_view(
    rig: RigConfig, ordered: Sequence[MeasurementJob], inputs: Mapping[str, str]
) -> bool:
    """Whether this run has anything to show on a live monitor: a job that
    uses the eye tracker and can run on this rig.

    A run of display timing and a tape measure has no camera to watch and no
    calibration to plot, so it opens no page. A tracker job that is
    unavailable (the rig's tracker is a stand-in) never opens the device, so
    it does not count either.
    """
    return rig.devices.eyetracker is not None and any(
        "tracker" in job.needs and job.unavailable(rig, inputs) is None for job in ordered
    )


def _no_keys() -> list[str]:
    """The key source the session's validation and drift correction read
    from. A measurement run starts neither through the monitor (the accuracy
    check is the job's own loop), and the calibration reads its own keys."""
    return []


class MeasureLiveView:
    """One measurement run's live monitor.

    ``controller`` is already started (its URL is what the command prints);
    :meth:`close` stops it. ``clock`` is the run's clock, the one the tracker
    stamps its frames and results with, so the times on the panels are on one
    timebase.
    """

    def __init__(
        self,
        controller: LiveMonitorController,
        rig: RigConfig,
        *,
        subject: str | None,
        clock: Clock,
    ) -> None:
        self._controller = controller
        self._rig = rig
        self._screen = Screen.from_monitor(rig.monitor)
        self._subject = subject
        self._clock = clock
        self._revision = 0
        # The measurement running now, as run_jobs named it: the first half
        # of every message, so a progress line always says whose it is.
        self._note = "starting"
        # Built the first time a job hands over the open tracker (watch).
        self._monitor: EyeTrackerMonitor | None = None
        # The accuracy check's panel, once that job has measured it.
        self._accuracy: dict[str, Any] | None = None
        self._last_publish = float("-inf")

    @property
    def url(self) -> str | None:
        """The page's address, token included."""
        return self._controller.url

    # ------------------------------------------------------------------
    # What the runner and the jobs call
    # ------------------------------------------------------------------

    def note(self, message: str) -> None:
        """Say which measurement is running now, and publish it."""
        self._note = message
        self._publish(message)

    def watch(self, tracker: Any, display: Any) -> None:
        """Start showing this tracker: its camera and what its calibration
        finds. Called by each tracker job with the device the run opened; the
        first call builds the monitor and the later ones change nothing."""
        if self._monitor is not None:
            return
        cfg = self._rig.devices.eyetracker
        if cfg is None:
            raise RuntimeError("a measurement handed over a tracker on a rig that configures none")
        # A session validates right after it calibrates when the rig says so.
        # Here that walk would be a second, different accuracy measurement in
        # front of the job that measures accuracy, so it is switched off for
        # this monitor: the calibration job runs the calibration and nothing
        # else, as it did before there was a page to show it on.
        monitor = EyeTrackerMonitor(
            tracker,
            display,
            self._screen,
            self._clock,
            cfg.model_copy(update={"validate_after_calibration": False}),
            poll_keys=_no_keys,
        )
        monitor.publisher = self._on_procedure
        monitor.camera_sink = self._send_frame
        monitor.settings_source = self._controller.poll_settings
        self._monitor = monitor
        self._publish(self._note)

    def calibrate(self, tracker: Any, display: Any) -> Any:
        """Run the tracker's calibration with the page following it: each
        stage as the message, the camera streaming, the iris size control
        live, and the fitted points on the Calibration panel when it ends.

        Returns the calibration's result. The walk itself is the tracker's
        own ``calibrate()``, called once.
        """
        self.watch(tracker, display)
        assert self._monitor is not None  # watch() built it, or raised
        result = self._monitor.calibrate()
        self._publish(f"{self._note} · {result.summary()}")
        return result

    def idle(self) -> None:
        """Keep the page current while a job waits for the operator: a camera
        frame when one is due, any tracker setting the page sent, and the
        panels again about once a second. Costs nothing between those, so a
        waiting loop calls it on every pass."""
        if self._monitor is None:
            return
        # One line per setting the page changed (the iris size), published at
        # once: whoever pressed + is waiting to see it land.
        messages = self._monitor.service_live_monitor()
        if messages or self._clock.now() - self._last_publish >= REFRESH_S:
            self._publish(" · ".join([self._note, *messages]))

    def show_accuracy(
        self,
        targets_px: Sequence[tuple[float, float]],
        quality: Mapping[str, Any],
        *,
        window_ms: float,
    ) -> None:
        """Put the accuracy check on the page: each target, the mean gaze
        measured on it, and the errors, from ``measure_stats.gaze_quality``'s
        result. It takes the place a session's validation has, because it is
        the same question asked of the same calibration."""
        deg = self._screen.px2deg
        per_target = list(quality["per_target"])
        worst = float(quality["accuracy_max_dva"])
        passed = worst <= ACCURACY_LIMIT_DEG
        precision = quality["precision_rms_s2s_median_dva"]
        stats: list[dict[str, Any]] = [
            {"label": "mean error", "value": f"{quality['accuracy_mean_dva']:.2f}°"},
            {
                "label": "worst",
                "value": f"{worst:.2f}°",
                **({} if passed else {"status": "critical"}),
            },
            {
                "label": "precision",
                "value": f"{precision:.3f}°" if precision is not None else "—",
            },
            {"label": "verdict", "value": "passed"}
            if passed
            else {"label": "verdict", "value": "FAILED", "status": "critical"},
        ]
        # Per-target errors in the order shown, so a bad corner can be named.
        errors = ", ".join(
            f"{index + 1}: {target['accuracy_dva']:.2f}°" for index, target in enumerate(per_target)
        )
        self._accuracy = {
            "form": "scatter",
            "series": [
                {
                    "name": "mean gaze",
                    "slot": 1,
                    "points": [
                        [deg(target["mean_px"][0]), deg(target["mean_px"][1])]
                        for target in per_target
                    ],
                }
            ],
            "targets": [[deg(x), deg(y)] for x, y in targets_px],
            "x_label": "Horizontal gaze position (°)",
            "y_label": "Vertical gaze position (°)",
            "equal_aspect": True,
            "stats": stats,
            "color_label": "",
            "note": (
                f"limit {ACCURACY_LIMIT_DEG:g}° · {window_ms:g} ms of samples per target · "
                f"precision is RMS sample-to-sample, as polled · {errors}"
            ),
        }
        self._publish(f"{self._note} · accuracy check measured")

    def close(self) -> None:
        """Stop the page's server. Nothing is saved: the numbers are in the
        measurement report, and the image is a picture of the subject."""
        self._controller.stop()

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def _on_procedure(self, _status: str, message: str) -> None:
        """A progress line from the tracker's procedure ("calibrating: target
        3 of 9"), published under the measurement it belongs to."""
        self._publish(f"{self._note} · {message}")

    def _send_frame(self, frame: CameraFrame) -> None:
        """A streamed camera frame, onto the page's camera channel, with the
        eye markers that were read with it."""
        self._controller.publish_camera(frame.pixels, frame.t, eyes=eye_markers(frame))

    def _publish(self, message: str) -> None:
        """Send the page one snapshot: the panels as they stand, and what is
        happening as the message."""
        self._revision += 1
        self._last_publish = self._clock.now()
        state = live_monitor_state(
            revision=self._revision,
            status=LIVE_STATUS,
            identity={
                "subject": self._subject,
                "session": None,
                "run": None,
                "task_name": RUN_NAME,
            },
            # A measurement run has no trials: the page is the eye tracker's
            # section and nothing else, so none of a session's trial panels.
            trials=[],
            events=[],
            spec=LiveMonitorSpec(include_defaults=False),
            message=message,
            extra_panels=self._panels(),
        )
        self._controller.publish(state)

    def _panels(self) -> list[dict[str, Any]]:
        """The run's panels: the camera (a tracker that has one), the
        calibration, and the accuracy check where a session's validation sits.

        The camera and the calibration are the session monitor's own panels.
        Its Validation and Drift correction panels are left out: both tell
        the reader which pause-menu key starts them, and a measurement run
        has neither the menu nor the procedure.
        """
        panels: list[dict[str, Any]] = []
        calibration: dict[str, Any] = {"form": "empty", "message": NO_CALIBRATION}
        if self._monitor is not None:
            # camera=True: the device is between procedures for the whole of
            # a measurement run, so the image and the eye line are live.
            shown = {panel["title"]: panel for panel in self._monitor.panels(camera=True)}
            if "Camera" in shown:
                panels.append(shown["Camera"])
            if self._monitor.calibration is not None:
                calibration = shown["Calibration"]["data"]
        panels.append({"title": "Calibration", "section": SECTION, "data": calibration})
        accuracy = self._accuracy or {"form": "empty", "message": NO_ACCURACY}
        panels.append({"title": "Validation", "section": SECTION, "data": accuracy})
        return panels


def open_live_view(
    rig: RigConfig,
    *,
    subject: str | None,
    clock: Clock,
    open_browser: bool | None = None,
) -> MeasureLiveView:
    """Start the page for a measurement run and return its view.

    ``open_browser`` overrides the rig's ``live_monitor.auto_open`` (the
    command line's ``--no-live-monitor-browser``); None keeps the rig's
    setting. Raises :class:`~alhazen.errors.SessionError` when the server
    does not start, as a session's does.
    """
    cfg = rig.live_monitor
    controller = LiveMonitorController(
        port=cfg.port, auto_open=cfg.auto_open if open_browser is None else open_browser
    )
    controller.start()
    view = MeasureLiveView(controller, rig, subject=subject, clock=clock)
    view.note("starting: the eye tracker opens with the first measurement that uses it")
    return view
