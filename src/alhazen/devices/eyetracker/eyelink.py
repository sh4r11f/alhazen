"""EyeLinkTracker: the real SR Research EyeLink backend, via ``pylink``.

Physical picture: the tracker samples the eye at up to 2 kHz and streams
samples over a dedicated link to a Host PC, which writes its own EDF file on
its own disk. That EDF — not anything on this machine — is the permanent eye
record; the messages this class sends into it are what let analysis align the
eye trace to the task afterwards. ``shutdown()`` is where the file is
retrieved.

``pylink`` is imported inside :meth:`connect`, never at module import: it
ships only with SR Research's EyeLink Developer's Kit, so importing it
eagerly would break ``import alhazen`` on every machine that is not the rig.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from alhazen.config.models import EyeTrackerConfig
from alhazen.core.clock import Clock
from alhazen.devices.eyetracker.protocol import GazeSample, HostShape
from alhazen.display.backend import DisplayBackend
from alhazen.display.screen import Screen
from alhazen.errors import TrackerError

log = logging.getLogger(__name__)

# Every EyeLink model reports this value for a coordinate it could not
# measure. Read from pylink at runtime; this is the fallback for SDK builds
# that do not expose the constant.
MISSING_DATA = -32768


def is_missing_gaze(gx: float, gy: float, missing_sentinel: float) -> bool:
    """True when either raw coordinate is the tracker's MISSING_DATA sentinel.

    A blink is *data arriving that says "no eye"*, not an absence of data:
    samples keep streaming, but the gaze coordinates are set to this sentinel
    rather than the sample being dropped or the eye struct being None. Code
    that only checks for a missing struct hands (-32768, -32768) px back as a
    real position — tens of thousands of degrees off screen — and any phase
    that latches "the last known gaze" then records that as the trial's
    measurement. Subjects blink constantly; this is not an edge case.

    A free function, not an inlined check, precisely so the decision is
    testable on a machine with no tracker attached.
    """
    return gx == missing_sentinel or gy == missing_sentinel


class EyeLinkTracker:
    """pylink-backed EyeTracker for a real EyeLink rig.

    Not exercised by the default test suite (it needs pylink and a tracker);
    it is exercised by ``alhazen check-rig`` and by real sessions. Kept thin
    on purpose: each method is one piece of the documented EyeLink startup /
    per-trial / shutdown sequence and nothing more.
    """

    def __init__(
        self,
        cfg: EyeTrackerConfig,
        display: DisplayBackend | None,
        screen: Screen,
        clock: Clock,
    ) -> None:
        self._cfg = cfg
        # None is legitimate: check-rig constructs this class to exercise the
        # real connect()/shutdown() path without opening a subject window.
        # Only configure() (calibration graphics) reads the window.
        self._display = display
        self._screen = screen
        self._clock = clock
        # Everything below is hardware-derived state; the sentinels are how
        # stop_trial()/shutdown() know whether there is anything real to undo.
        self._tracker: Any = None  # pylink.EyeLink connection, once connected
        self._pylink: Any = None  # the lazily-imported module itself
        self._eye_index = 0  # 0=left, 1=right; re-resolved every trial
        self._recording = False

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the link to the Host PC and start its EDF file."""
        try:
            import pylink
        except ImportError as e:
            raise TrackerError(
                "pylink is not installed. It ships with SR Research's EyeLink Developer's "
                "Kit, which is installed on the rig from SR Research's own installer — it "
                "is NOT on PyPI, and the PyPI project named 'pylink' is an unrelated "
                "package that must never be installed here. Install the Developer's Kit on "
                "the rig, or use eyetracker backend 'mouse_sim' for development."
            ) from e

        self._pylink = pylink
        try:
            self._tracker = pylink.EyeLink(self._cfg.host_ip)
            self._tracker.openDataFile(self._cfg.edf_host_filename)
            # Stamped into the EDF header so a file found later on the Host
            # PC's disk can be traced back to the software that recorded it.
            self._tracker.sendCommand("add_file_preamble_text 'RECORDED BY alhazen'")
        except RuntimeError as e:
            raise TrackerError(
                f"EyeLink connect to {self._cfg.host_ip} failed: {e}. Check the tracker "
                f"link/IP, or use eyetracker backend 'mouse_sim'."
            ) from e

    def configure(self, screen: Screen, clock: Clock) -> None:
        """Set data filters, hand the tracker the display geometry, and
        register the calibration graphics.

        The clock arrives here as well as at construction so every backend's
        configure step has the same shape; gaze is stamped from it, never from
        the tracker's own clock (invariant 2).
        """
        self._clock = clock
        tracker = self._tracker
        # The Host must not be recording while its configuration changes.
        tracker.setOfflineMode()

        # SR Research's standard filter set: every eye event saved to the EDF
        # and available over the link. HTARGET (head-target data) is only
        # requested where the tracker software supports it — asking an older
        # tracker for it is rejected outright, hence the version check.
        version = int(tracker.getTrackerVersionString().split()[-1].split(".")[0])
        file_event_flags = "LEFT,RIGHT,FIXATION,SACCADE,BLINK,MESSAGE,BUTTON,INPUT"
        link_event_flags = "LEFT,RIGHT,FIXATION,SACCADE,BLINK,BUTTON,FIXUPDATE,INPUT"
        if version > 3:
            file_sample_flags = "LEFT,RIGHT,GAZE,HREF,RAW,AREA,HTARGET,GAZERES,BUTTON,STATUS,INPUT"
            link_sample_flags = "LEFT,RIGHT,GAZE,GAZERES,AREA,HTARGET,STATUS,INPUT"
        else:
            file_sample_flags = "LEFT,RIGHT,GAZE,HREF,RAW,AREA,GAZERES,BUTTON,STATUS,INPUT"
            link_sample_flags = "LEFT,RIGHT,GAZE,GAZERES,AREA,STATUS,INPUT"
        tracker.sendCommand(f"file_event_filter = {file_event_flags}")
        tracker.sendCommand(f"file_sample_data = {file_sample_flags}")
        tracker.sendCommand(f"link_event_filter = {link_event_flags}")
        tracker.sendCommand(f"link_sample_data = {link_sample_flags}")

        # Tell the tracker the exact pixel grid, so ITS gaze and calibration
        # coordinates line up with what we draw.
        w, h = screen.width_px, screen.height_px
        tracker.sendCommand(f"screen_pixel_coords = 0 0 {w - 1} {h - 1}")
        tracker.sendMessage(f"DISPLAY_COORDS 0 0 {w - 1} {h - 1}")

        tracker.sendCommand(f"calibration_type = {self._cfg.calibration_type}")
        # Both grids get the same area: a subject trained on a smaller
        # effective field would otherwise be calibrated over the full screen
        # and validated somewhere else entirely.
        area = self._cfg.calibration_area
        tracker.sendCommand(f"calibration_area_proportion {area} {area}")
        tracker.sendCommand(f"validation_area_proportion {area} {area}")

        if self._display is None:
            raise TrackerError(
                "EyeLinkTracker.configure() needs an open display for its calibration "
                "graphics; this instance was constructed without one (check-rig does that "
                "deliberately and never calls configure)."
            )
        # Calibration draws into this session's own window through alhazen's
        # implementation of pylink's callback surface (calibration.py). The
        # import is local because that module's factory needs pylink and
        # psychopy, neither of which may exist off the rig.
        from alhazen.devices.eyetracker.calibration import make_calibration_graphics

        graphics = make_calibration_graphics(tracker, self._display.window, screen)
        self._pylink.openGraphicsEx(graphics)

    def calibrate(self) -> None:
        """Run camera setup / calibration / validation on the Host PC. Blocks
        until the experimenter finishes or aborts.

        Aborting with ESC makes pylink raise RuntimeError from inside
        ``doTrackerSetup()``. That is a deliberate experimenter action (the
        subject needs a break), not a hardware fault, so it is logged and the
        tracker is returned to a clean state instead of aborting the session.
        """
        try:
            self._tracker.doTrackerSetup()
        except RuntimeError as e:
            log.warning("EyeLink calibration aborted by the experimenter: %s", e)
            self._tracker.exitCalibration()

    # ------------------------------------------------------------------
    # Per trial
    # ------------------------------------------------------------------

    def start_trial(self, trial_index: int, status: str) -> None:
        """Open this trial's recording segment and resolve which eye to read."""
        tracker = self._tracker
        tracker.setOfflineMode()
        tracker.sendCommand("clear_screen 0")
        # Operator-facing line on the Host PC's own screen. Distinct from
        # send_message(), which writes into the EDF that analysis reads.
        tracker.sendCommand(f"record_status_message 'Trial {trial_index}: {status}'")

        error = tracker.startRecording(1, 1, 1, 1)
        if error:
            raise TrackerError(
                f"EyeLink startRecording failed (code {error}) at trial {trial_index}. "
                f"Check the tracker link and the Host PC's recording status."
            )
        self._recording = True
        # Let samples start flowing before the first get_gaze() of the trial.
        self._pylink.pumpDelay(100)

        # Which eye is tracked can change mid-session (a recalibration, a
        # switch to the subject's better eye), so it is resolved per trial
        # rather than assumed — reading a stale eye returns no data at all,
        # which looks exactly like a subject who never fixates.
        eye_used = tracker.eyeAvailable()
        if eye_used == self._pylink.RIGHT_EYE:
            self._eye_index = 1
        elif eye_used in (self._pylink.LEFT_EYE, self._pylink.BINOCULAR):
            # GazeSample carries one (gx, gy), so binocular has to pick one
            # eye; left is the arbitrary-but-fixed choice.
            self._eye_index = 0
        else:
            raise TrackerError(
                f"EyeLink eyeAvailable() reported no usable eye ({eye_used}) at trial "
                f"{trial_index}. Check camera setup and calibration on the Host PC."
            )
        # The only durable record of which eye this trial's samples came from.
        eye_name = "RIGHT" if self._eye_index == 1 else "LEFT"
        tracker.sendMessage(f"EYE_USED {self._eye_index} {eye_name}")

    def stop_trial(self) -> None:
        """Close this trial's recording segment. Idempotent: a trial can end
        before start_trial() ever set recording, and the runner calls this in
        a ``finally`` regardless."""
        if self._recording:
            self._pylink.pumpDelay(100)  # let buffered samples flush first
            self._tracker.stopRecording()
            self._recording = False

    def is_recording(self) -> bool:
        return self._recording

    def get_gaze(self) -> GazeSample | None:
        """Newest sample for this trial's resolved eye, or None.

        None means "no verifiable position": no sample has arrived yet, the
        resolved eye is absent from the newest sample, or the sample carries
        the MISSING_DATA sentinel (a blink). All three are routine; the
        phases decide what a gap means, this method never guesses.
        """
        sample = self._tracker.getNewestSample()
        if sample is None:
            return None

        # isLeftSample()/isRightSample() can be False even when the getter
        # still hands back a non-None (stale) struct, so this is a different
        # check from the None guard below, not a redundant one.
        if self._eye_index == 1 and not sample.isRightSample():
            return None
        if self._eye_index == 0 and not sample.isLeftSample():
            return None

        eye_data = sample.getRightEye() if self._eye_index == 1 else sample.getLeftEye()
        if eye_data is None:
            return None

        gx, gy = eye_data.getGaze()
        missing = getattr(self._pylink, "MISSING_DATA", MISSING_DATA)
        if is_missing_gaze(gx, gy, missing):
            return None  # a blink: data arrived, and it says "no eye"
        return GazeSample(gx=gx, gy=gy, t=self._clock.now())

    def send_message(self, text: str) -> None:
        # Forwarded verbatim: what the text says is the message subscriber's
        # business (devices/eyetracker/messages.py), not this method's.
        self._tracker.sendMessage(text)

    def draw_host_overlay(self, shapes: list[HostShape]) -> None:
        for shape in shapes:
            if shape.kind == "cross":
                self._tracker.sendCommand(f"draw_cross {shape.x1} {shape.y1}")
            else:
                self._tracker.sendCommand(f"draw_box {shape.x1} {shape.y1} {shape.x2} {shape.y2}")

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def shutdown(self, edf_destination: Path | None, /) -> None:
        """Close the EDF, retrieve it from the Host PC, and drop the link.

        A failed transfer *raises*: the EDF is the only permanent record of
        the session's eye data and it lives on the Host PC's disk. Swallowing
        a failure here is how a completed session's eye data quietly never
        makes it off that machine.
        """
        if self._tracker is None:
            return  # connect() never ran: no file was ever opened

        if not self._tracker.isConnected():
            log.warning(
                "EyeLink link is down at shutdown; skipping EDF retrieval. The recording "
                "'%s' should still be on the EyeLink Host PC's disk — retrieve it manually "
                "before wiping anything.",
                self._cfg.edf_host_filename,
            )
            return

        if self._recording:
            # The session can end mid-trial (quit, abort); stop_trial() owns
            # the correct flush-then-stop sequence, so delegate rather than
            # re-deriving it here.
            self.stop_trial()

        self._tracker.setOfflineMode()
        self._tracker.sendCommand("clear_screen 0")
        # Give the Host PC time to finish writing before the handle closes.
        # msecDelay (a plain wait), not pumpDelay: nothing is driving a
        # window here. The EDF is irreplaceable, so this is not optional.
        self._pylink.msecDelay(500)
        self._tracker.closeDataFile()

        if edf_destination is not None:
            try:
                self._tracker.receiveDataFile(self._cfg.edf_host_filename, str(edf_destination))
            except RuntimeError as e:
                raise TrackerError(
                    f"failed to retrieve EDF '{self._cfg.edf_host_filename}' from the "
                    f"EyeLink Host PC to {edf_destination}: {e}. The recording should still "
                    f"be on the Host PC's disk — retrieve it manually before wiping anything."
                ) from e

        self._tracker.close()
