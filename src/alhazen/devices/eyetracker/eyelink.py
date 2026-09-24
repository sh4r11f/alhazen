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
from alhazen.devices.eyetracker.guide import GUIDE_TITLE, calibration_guide, target_count
from alhazen.devices.eyetracker.protocol import (
    CalibrationResult,
    GazeSample,
    HostShape,
    ProgressHook,
)
from alhazen.display.backend import DisplayBackend
from alhazen.display.palette import TERMINAL_GREEN
from alhazen.display.screen import Screen
from alhazen.errors import TrackerError

log = logging.getLogger(__name__)

# Every EyeLink model reports this value for a coordinate it could not
# measure. Read from pylink at runtime; this is the fallback for SDK builds
# that do not expose the constant.
MISSING_DATA = -32768

# pylink's result codes for the last calibration, validation or drift
# correction (EyeLink.getCalibrationResult). Read from pylink at runtime;
# these are the fallbacks for SDK builds that do not expose the names.
OK_RESULT = 0
ABORT_RESULT = 27
NO_REPLY = 1000

# What pylink's isRecording() answers (the C API's check_recording(), which
# SR Research's own examples call to notice a tracker that stopped): TRIAL_OK
# while the Host PC records, otherwise the code its recording ended with.
# Read from pylink at runtime; the numbers here are the fallbacks for SDK
# builds that do not expose the names (the values in SR Research's eyelink.h),
# each with what it means in a fault's detail.
TRIAL_OK = 0
RECORDING_ENDED: dict[str, tuple[int, str]] = {
    "TRIAL_ERROR": (-1, "the Host PC is no longer recording"),
    "REPEAT_TRIAL": (1, "its operator ended the recording (repeat trial)"),
    "SKIP_TRIAL": (2, "its operator ended the recording (skip trial)"),
    "ABORT_EXPT": (3, "its operator aborted the experiment"),
}

# The keys the guide lists for the EyeLink. The procedure itself runs on the
# Host PC's setup screen, which alhazen mirrors into the subject window
# through calibration.py; these are the Host PC's own keys.
GUIDE_KEYS = (
    ("C", "calibrate"),
    ("V", "validate (on the Host PC)"),
    ("SPACE/ENTER", "accept a target"),
    ("ESC", "back to the session"),
)


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

    The default test suite runs it against a stand-in pylink (the calibration
    around ``doTrackerSetup()``, and the recording and dropout detection
    against a simulated Host PC in tests/fake_sdk.py); only a real tracker
    proves the SDK behaves as the stand-in does, which is what ``alhazen
    check-rig`` and real sessions are for. Kept thin on purpose: each method
    is one piece of the documented EyeLink startup / per-trial / shutdown
    sequence and nothing more.
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
        # The newest link sample's own timestamp (tracker ms) and the session
        # time it was first read at. get_gaze() is called once per display
        # frame, and getNewestSample() hands back the same sample until a
        # newer one arrives; keeping the first-read time for it is what makes
        # a repeat recognisable as a repeat (protocol.py, GazeSample.t).
        self._sample_tracker_time: float | None = None
        self._sample_session_t = 0.0
        # Dropout detection (recording_fault). How long the newest sample may
        # go unreplaced mid-trial (eyetracker.max_sample_gap_ms), and the
        # session time this trial's recording began delivering: a gap is
        # counted from the later of that and the newest sample, so the
        # inter-trial interval — no recording, so no samples — is never
        # mistaken for a gap inside the trial.
        self._max_gap_s = cfg.sample_gap_limit_s
        self._segment_t = 0.0
        # What recording_fault() reported for the open segment, once it has:
        # the dropout, kept until the next start_trial(). Also what lets
        # stop_trial() and send_message() log, rather than raise, a link
        # failure that the dropout already explains (see there).
        self._dropout: str | None = None
        # Where calibrate() reports its stages (the dashboard, via the
        # session's monitor); None until someone asks to be told.
        self._progress: ProgressHook | None = None

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
            tracker = pylink.EyeLink(self._cfg.host_ip)
        except RuntimeError as e:
            raise TrackerError(self._connect_failed(e)) from e
        try:
            tracker.openDataFile(self._cfg.edf_host_filename)
            # Stamped into the EDF header so a file found later on the Host
            # PC's disk can be traced back to the software that recorded it.
            tracker.sendCommand("add_file_preamble_text 'RECORDED BY alhazen'")
        except BaseException as e:
            # The link is open and this tracker will never be connected, so
            # nothing else would close it: released here, as the SpikeGLX and
            # NI-DAQ backends release theirs after a failed connect.
            # BaseException, so a Ctrl+C here releases the link too.
            try:
                tracker.close()
            except Exception as close_error:  # pylink's error types are not all documented
                # The connect error is the one that says what went wrong;
                # close()'s is logged rather than raised over it.
                log.error(
                    "EyeLink close() failed as well (%s), after the connect had already "
                    "failed (%s)",
                    close_error,
                    e,
                )
            if isinstance(e, RuntimeError):
                raise TrackerError(self._connect_failed(e)) from e
            raise
        # Kept only once connected: a tracker that failed to connect holds no
        # link, so shutdown() has nothing to release.
        self._tracker = tracker

    def _connect_failed(self, error: RuntimeError) -> str:
        return (
            f"EyeLink connect to {self._cfg.host_ip} failed: {error}. Check the tracker "
            f"link/IP, or use eyetracker backend 'mouse_sim'."
        )

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
        # Who moves the calibration from one target to the next: the
        # experimenter (NO: a key per target) or the Host PC by itself once
        # gaze has settled (YES). The Host PC's own pacing applies in auto.
        auto = "YES" if self._cfg.calibration_advance == "auto" else "NO"
        tracker.sendCommand(f"enable_automatic_calibration = {auto}")

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

    def set_progress_hook(self, hook: ProgressHook | None) -> None:
        """Where calibrate() reports its stages; None to stop reporting."""
        self._progress = hook

    def _report(self, stage: str, detail: str) -> None:
        if self._progress is not None:
            self._progress(stage, detail)

    def calibrate(self) -> CalibrationResult:
        """Show the guide, then run camera setup / calibration / validation on
        the Host PC. Blocks until the experimenter finishes or aborts.

        The guide is alhazen's; the procedure is the Host PC's, mirrored into
        the subject window by calibration.py. Aborting with ESC makes pylink
        raise RuntimeError from inside ``doTrackerSetup()``. That is a
        deliberate experimenter action (the subject needs a break), not a
        hardware fault, so it is logged and the tracker is returned to a
        clean state instead of aborting the session.
        """
        if self._display is None:
            raise TrackerError(
                "EyeLinkTracker.calibrate() needs an open display for its guide; this "
                "instance was constructed without one (check-rig does that deliberately "
                "and never calibrates)."
            )
        # Local for the same reason as pylink: psychopy may not exist off the rig.
        from psychopy import event

        if not self._show_guide(event):
            note = "skipped at the guide; the tracker keeps its previous calibration"
            log.warning("EyeLink calibration %s", note)
            return self._result(None, note, aborted=True)
        self._report("calibrating", "on the Host PC's setup screen")
        try:
            self._tracker.doTrackerSetup()
        except RuntimeError as e:
            log.warning("EyeLink calibration aborted by the experimenter: %s", e)
            self._tracker.exitCalibration()
            return self._result(None, f"aborted on the Host PC ({e})", aborted=True)
        return self._host_result()

    def _show_guide(self, event: Any) -> bool:
        """Draw the calibration guide and wait; True when SPACE starts the setup.

        No live eye line here: the EyeLink's camera image is on the Host PC's
        own screen, and the setup screen that follows shows it too.
        """
        cfg = self._cfg
        body = calibration_guide(
            tracker="EyeLink (the Host PC drives the procedure)",
            eye="set on the Host PC; the session reads the eye the tracker reports",
            layout=cfg.calibration_type,
            n_targets=target_count(cfg.calibration_type),
            area=cfg.calibration_area,
            advance=cfg.calibration_advance,
            keys=GUIDE_KEYS,
            start_line="press SPACE to open the Host PC setup, ESC to skip",
        )
        self._display.show_menu(GUIDE_TITLE, body, color=TERMINAL_GREEN)  # type: ignore[union-attr]
        self._report("calibration guide", "waiting for SPACE")
        keys = event.waitKeys(keyList=["space", "escape"])
        return bool(keys) and keys[0] == "space"

    def _host_result(self) -> CalibrationResult:
        """What the Host PC says about the last calibration it ran.

        pylink keeps the result code and message of the last calibration,
        validation or drift correction; after ``doTrackerSetup()`` returns
        they describe whatever the experimenter did last on the setup screen.
        The message is the Host PC's own text ("GOOD", "POOR", validation
        error statistics), passed on verbatim rather than interpreted.
        """
        pylink = self._pylink
        try:
            code = int(self._tracker.getCalibrationResult())
            message = str(self._tracker.getCalibrationMessage()).strip()
        except (AttributeError, RuntimeError) as e:
            log.warning("EyeLink reported no calibration result: %s", e)
            return self._result(None, "the tracker did not report a result; check the Host PC")
        detail = f"Host PC: {message}" if message else f"Host PC result code {code}"
        if code == getattr(pylink, "NO_REPLY", NO_REPLY):
            note = "the Host PC reports no calibration — was C pressed on its setup screen?"
            log.warning("EyeLink calibration: %s", note)
            return self._result(None, note)
        if code == getattr(pylink, "ABORT_RESULT", ABORT_RESULT):
            note = f"the last calibration on the Host PC was aborted with ESC ({detail})"
            log.warning("EyeLink calibration: %s", note)
            return self._result(None, note, aborted=True)
        if code == getattr(pylink, "OK_RESULT", OK_RESULT):
            return self._result(True, detail)
        log.error("EyeLink calibration did not succeed (%s, code %d)", detail, code)
        return self._result(False, f"{detail} (code {code}) — calibrate again")

    def _result(self, ok: bool | None, note: str, *, aborted: bool = False) -> CalibrationResult:
        return CalibrationResult(
            ok=ok,
            layout=self._cfg.calibration_type,
            n_targets=target_count(self._cfg.calibration_type),
            eye=self._eye_reported(),
            advance=self._cfg.calibration_advance,
            t=self._clock.now(),
            note=note,
            aborted=aborted,
        )

    def _eye_reported(self) -> str:
        """Which eye the tracker says it tracks, in words for the result.

        Outside a recording the tracker may have no sample to answer from;
        that is not an error here, the eye is resolved for real at every
        start_trial().
        """
        pylink = self._pylink
        try:
            eye = self._tracker.eyeAvailable()
        except RuntimeError as e:
            return f"set on the Host PC (not reported: {e})"
        if eye == pylink.RIGHT_EYE:
            return "right (reported by the tracker)"
        if eye == pylink.LEFT_EYE:
            return "left (reported by the tracker)"
        if eye == pylink.BINOCULAR:
            return "both (reported by the tracker; the session reads the left)"
        return "set on the Host PC (the tracker reports it when recording starts)"

    # ------------------------------------------------------------------
    # Per trial
    # ------------------------------------------------------------------

    def start_trial(self, trial_index: int, status: str) -> None:
        """Open this trial's recording segment and resolve which eye to read.

        This is also where a tracker that dropped out on an earlier trial is
        found to be back, or not. Every failure here is a TrackerError that
        names the Host PC and what to check — never pylink's bare
        RuntimeError — and, when the previous trial's recording had died,
        says what it died of: a start that fails right after a dropout is
        almost always the same fault.
        """
        tracker = self._tracker
        # What the previous segment died of, if it did: kept for the error
        # message below, since a successful start clears it.
        previous = self._dropout
        try:
            tracker.setOfflineMode()
            tracker.sendCommand("clear_screen 0")
            # Operator-facing line on the Host PC's own screen. Distinct from
            # send_message(), which writes into the EDF that analysis reads.
            tracker.sendCommand(f"record_status_message 'Trial {trial_index}: {status}'")
            error = tracker.startRecording(1, 1, 1, 1)
        except RuntimeError as e:
            # pylink raises RuntimeError when the link itself fails (a pulled
            # cable, a Host PC that is off). Said as a rig fault with the
            # address to check, not as the SDK's own words alone.
            raise TrackerError(
                self._start_failed(trial_index, f"the link failed ({e})", previous)
            ) from e
        if error:
            raise TrackerError(
                self._start_failed(trial_index, f"startRecording failed (code {error})", previous)
            )
        self._recording = True
        self._dropout = None
        try:
            # Let samples start flowing before the first get_gaze() of the trial.
            self._pylink.pumpDelay(100)
            # The dropout check's clock starts here, once samples have had
            # the same 100 ms to arrive that get_gaze() gets: a recording that
            # delivers nothing from here on is dead from its first frame.
            self._segment_t = self._clock.now()

            # Which eye is tracked can change mid-session (a recalibration, a
            # switch to the subject's better eye), so it is resolved per trial
            # rather than assumed — reading a stale eye returns no data at
            # all, which looks exactly like a subject who never fixates.
            eye_used = tracker.eyeAvailable()
            if eye_used == self._pylink.RIGHT_EYE:
                self._eye_index = 1
            elif eye_used in (self._pylink.LEFT_EYE, self._pylink.BINOCULAR):
                # GazeSample carries one (gx, gy), so binocular has to pick
                # one eye; left is the arbitrary-but-fixed choice.
                self._eye_index = 0
            else:
                raise TrackerError(
                    f"EyeLink eyeAvailable() reported no usable eye ({eye_used}) at trial "
                    f"{trial_index}. Check camera setup and calibration on the Host PC."
                )
            # The only durable record of which eye this trial's samples came from.
            eye_name = "RIGHT" if self._eye_index == 1 else "LEFT"
            tracker.sendMessage(f"EYE_USED {self._eye_index} {eye_name}")
        except RuntimeError as e:
            # Recording started, then the link failed. The segment is open,
            # and the runner's finally will stop it over the same dead link:
            # kept as this segment's dropout, so that stop is logged rather
            # than raised — and never replaces this error with pylink's own.
            message = self._start_failed(
                trial_index, f"the link failed as recording started ({e})", previous
            )
            self._dropout = message
            raise TrackerError(message) from e

    def _start_failed(self, trial_index: int, what: str, previous: str | None) -> str:
        """The message a failed start raises with: what failed, what to check,
        and what the previous trial's recording died of, if it did."""
        before = (
            f" The previous trial's recording had already been lost: {previous}."
            if previous
            else ""
        )
        return (
            f"EyeLink could not start recording at trial {trial_index}: {what}. Check the "
            f"tracker link (the cable between this machine and the Host PC at "
            f"{self._cfg.host_ip}) and the Host PC's recording status, then start the "
            f"session again.{before}"
        )

    def stop_trial(self) -> None:
        """Close this trial's recording segment. Idempotent: a trial can end
        before start_trial() ever set recording, and the runner calls this in
        a ``finally`` regardless.

        After a dropout (recording_fault() reported one on this segment), a
        link failure here is logged rather than raised. The recording is
        already gone and the trial's fault is on its row; raising in the
        runner's ``finally`` would throw that row away and end the session
        over a stop with nothing left to stop. Whether the tracker is really
        gone is the next start_trial()'s to find out, and it says so loudly.
        Without a dropout, a failure here is as unexpected as it ever was and
        propagates.
        """
        if not self._recording:
            return
        try:
            self._pylink.pumpDelay(100)  # let buffered samples flush first
            self._tracker.stopRecording()
        except RuntimeError as e:
            if self._dropout is None:
                raise
            log.warning(
                "EyeLink stopRecording() failed after this trial's dropout (%s): %s. The "
                "recording was already lost; the next trial's start says whether the tracker "
                "is back.",
                self._dropout,
                e,
            )
        self._recording = False

    def is_recording(self) -> bool:
        """True from start_trial() to stop_trial(): the segment flag, which
        asks the tracker nothing. Whether that recording is still delivering
        is recording_fault()'s question."""
        return self._recording

    def recording_fault(self) -> str | None:
        """Optional capability (protocol.py): None while this trial's
        recording delivers, otherwise what failed — the tracker health
        check's detail.

        The session calls this every frame, so the healthy path asks the
        Host PC nothing. It reads pylink's newest link sample
        (``getNewestSample()``: a copy out of the buffer pylink's own link
        thread fills — no round trip to the Host PC) and compares its
        timestamp with the one seen before. While the Host PC records,
        samples arrive at its rate — 250 to 2000 per second, several per
        frame — and a blink is one of them, carrying the MISSING_DATA
        sentinel with a timestamp that still advances. So a newest sample
        that has not been replaced for ``max_sample_gap_ms`` is a recording
        that stopped, whatever the eye did.

        Only then is the Host PC asked why (``isRecording()``, _ask_host_why):
        the answer is what tells a pulled cable — the Host PC still recording,
        or not answering at all — from a Host PC that stopped recording, and
        which of its codes it stopped with. Asking it once, at the dropout,
        rather than every frame keeps a call whose cost on the rig is not yet
        measured out of the frame loop.
        """
        if not self._recording:
            return None
        if self._dropout is not None:
            return self._dropout  # judged already: say it again, ask nothing again
        try:
            age = self.newest_sample_age_s()
        except RuntimeError as e:
            self._dropout = f"the EyeLink link failed while its newest sample was read ({e})"
            return self._dropout
        assert age is not None  # a segment is open
        if age <= self._max_gap_s:
            return None
        self._dropout = (
            f"no new sample from the EyeLink for {age * 1000:.0f} ms (limit "
            f"{self._max_gap_s * 1000:g} ms); {self._ask_host_why()}"
        )
        return self._dropout

    def newest_sample_age_s(self) -> float | None:
        """Optional capability (protocol.py): seconds since the newest link
        sample was first seen — or since this segment began delivering, when
        none has arrived in it. None with no segment open."""
        if not self._recording:
            return None
        now = self._clock.now()
        self._note_newest_sample(now)
        return now - max(self._sample_session_t, self._segment_t)

    def _ask_host_why(self) -> str:
        """What the Host PC says about the recording, asked once the samples
        have gone stale; in words, for the fault's detail.

        ``isRecording()`` is 0 (TRIAL_OK) while the Host PC records, and
        otherwise the code recording ended with. Three answers, three places
        to look: 0 means the Host PC still believes it is recording, so the
        samples stopped on their way here (the cable, the network); a code
        means it stopped, and which one says whether its operator did it; no
        answer at all is a link that is down.
        """
        pylink = self._pylink
        where = f"the Host PC at {self._cfg.host_ip}"
        try:
            code = int(self._tracker.isRecording())
        except RuntimeError as e:
            return (
                f"{where} did not answer isRecording() ({e}): the link to it is down — check "
                f"the cable between this machine and the Host PC"
            )
        if code == getattr(pylink, "TRIAL_OK", TRIAL_OK):
            return (
                f"{where} still reports recording (isRecording 0), so the samples stopped on "
                f"their way here — check the link cable and the network between this machine "
                f"and the Host PC"
            )
        for name, (fallback, meaning) in RECORDING_ENDED.items():
            if code == getattr(pylink, name, fallback):
                return f"{where} reports recording ended (isRecording {code}, {name}): {meaning}"
        return f"{where} reports recording ended (isRecording {code}, a code alhazen does not name)"

    def simulate_dropout(self) -> str:
        """Optional capability (protocol.py), for ``alhazen check-rig`` only:
        stop the open recording through pylink without telling this backend,
        the way the Host PC stops when its operator ends a recording, so the
        check can prove recording_fault() notices a real stop."""
        if not self._recording:
            raise TrackerError("simulate_dropout() needs an open recording: start_trial() first")
        self._tracker.stopRecording()
        return (
            "stopRecording() through pylink, behind the session's back — the Host PC left "
            "record mode, as it does when its operator stops recording"
        )

    def _note_newest_sample(self, now: float) -> Any:
        """Read pylink's newest link sample and note when it was first seen;
        return it (None before any sample has arrived).

        Stamped the first time a sample is seen, and never restamped: the
        tracker's clock says whether it is new, the session clock says when.
        Mixing the two online would break invariant 2, so the tracker's time
        is only compared, never reported. Shared by get_gaze() and the
        dropout check, so the two can never disagree about which sample is
        new.
        """
        sample = self._tracker.getNewestSample()
        if sample is not None:
            tracker_time = sample.getTime()
            if tracker_time != self._sample_tracker_time:
                self._sample_tracker_time = tracker_time
                self._sample_session_t = now
        return sample

    def get_gaze(self) -> GazeSample | None:
        """Newest sample for this trial's resolved eye, or None.

        None means "no verifiable position": no sample has arrived yet, the
        resolved eye is absent from the newest sample, or the sample carries
        the MISSING_DATA sentinel (a blink). All three are routine; the
        phases decide what a gap means, this method never guesses.
        """
        sample = self._note_newest_sample(self._clock.now())
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
        return GazeSample(gx=gx, gy=gy, t=self._sample_session_t)

    def send_message(self, text: str) -> None:
        """Write one message into the EDF.

        Forwarded verbatim: what the text says is the message subscriber's
        business (devices/eyetracker/messages.py), not this method's. A link
        failure raises (invariant 6) — except right after a dropout on this
        trial, when the engine's own TRIAL_END is on its way here over a link
        the dropout already explains. Raising then would end the session
        inside the trial's own bookkeeping and lose its row, so the message
        is logged as unwritten instead, and the next start_trial() finds out
        loudly whether the tracker is back.
        """
        try:
            self._tracker.sendMessage(text)
        except RuntimeError as e:
            if self._dropout is None:
                raise
            log.warning(
                "EyeLink message %r was not written into the EDF: the link failed (%s) after "
                "this trial's dropout (%s)",
                text,
                e,
                self._dropout,
            )

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

        So does a link that is down, or dies, before the transfer, whenever a
        destination says a run wants its EDF. A warning in the log is not
        something the runner can see: the run would be recorded as complete
        with its eye data still on the Host PC. Raised, it is a failed
        teardown step, so the run ends ``failed`` and the message says
        where the file is. With no destination (check-rig, the accuracy
        measurement) nothing was asked for and nothing is lost, so a link
        found down is logged and the check is not failed over it; a link
        that fails while the EDF is being closed is a TrackerError all the
        same, which check-rig reports as a failed check.

        Whatever happened, the link is released (``close()``) in a
        ``finally``, as the TRACKPixx3 backend releases its device: nothing
        after teardown would close it otherwise. When close() fails as well,
        the error already on its way out is the one that propagates — it is
        the one that names the EDF — and close()'s is logged rather than
        lost.
        """
        if self._tracker is None:
            return  # connect() never ran: no file was ever opened
        # The error leaving the EDF steps, if one is: what the finally checks
        # before letting a failed close() raise over it. BaseException, not
        # Exception: a Ctrl+C during the 500 ms wait must not be replaced by
        # close()'s error either.
        failure: BaseException | None = None
        try:
            self._close_edf(edf_destination)
        except BaseException as e:
            failure = e
            raise
        finally:
            try:
                # Called whether or not the link is up, as SR Research's own
                # example scripts do at the end of a session: it is what
                # releases pylink's side of the connection.
                self._tracker.close()
            except Exception as e:  # pylink's error types are not all documented
                if failure is None:
                    raise  # nothing else failed: a link that will not close is the fault
                log.error(
                    "EyeLink close() failed as well (%s), after the shutdown had already "
                    "failed (%s)",
                    e,
                    failure,
                )

    def _close_edf(self, edf_destination: Path | None) -> None:
        """Close the EDF on the Host PC and, given a destination, retrieve it.

        Raises a TrackerError naming the file whenever a destination was
        given and the EDF did not arrive there. Without one, a link found
        down is logged, and one that fails while the EDF closes is still a
        TrackerError (not pylink's RuntimeError), so a check reports it.
        """
        if not self._tracker.isConnected():
            if edf_destination is None:
                # Nothing to retrieve and nothing lost: the file on the Host PC
                # is a check's test recording, not a session's data.
                log.warning(
                    "EyeLink link is down at shutdown; no recording was asked for, so none "
                    "is retrieved ('%s' is left on the Host PC at %s)",
                    self._cfg.edf_host_filename,
                    self._cfg.host_ip,
                )
                return
            raise TrackerError(
                self._edf_not_retrieved(
                    edf_destination, "the link to the Host PC is down at shutdown"
                )
            )

        try:
            if self._recording:
                # The session can end mid-trial (quit, abort); stop_trial()
                # owns the correct flush-then-stop sequence, so delegate
                # rather than re-deriving it here.
                self.stop_trial()

            self._tracker.setOfflineMode()
            self._tracker.sendCommand("clear_screen 0")
            # Give the Host PC time to finish writing before the handle closes.
            # msecDelay (a plain wait), not pumpDelay: nothing is driving a
            # window here. The EDF is irreplaceable, so this is not optional.
            self._pylink.msecDelay(500)
            self._tracker.closeDataFile()
        except RuntimeError as e:
            # pylink raises RuntimeError when the link fails. Up when
            # isConnected() was asked and gone a moment later is the same lost
            # retrieval as a link found down, and is said the same way: in
            # words that name the file, not pylink's, which name nothing.
            if edf_destination is None:
                # No run behind it (check-rig, the accuracy measurement), so
                # nothing is lost — but a link that dies here is still a
                # fault, and raised as pylink's bare RuntimeError it went
                # past check-rig's `except AlhazenError` as a traceback
                # instead of a failed check.
                raise TrackerError(
                    f"the EyeLink failed while closing the EDF ({e}); no recording was "
                    f"asked for, so nothing is lost ('{self._cfg.edf_host_filename}' is left "
                    f"on the Host PC at {self._cfg.host_ip}). Check the link to the Host PC."
                ) from e
            raise TrackerError(
                self._edf_not_retrieved(
                    edf_destination, f"the EyeLink failed while closing the EDF ({e})"
                )
            ) from e

        if edf_destination is not None:
            try:
                self._tracker.receiveDataFile(self._cfg.edf_host_filename, str(edf_destination))
            except RuntimeError as e:
                raise TrackerError(
                    f"failed to retrieve EDF '{self._cfg.edf_host_filename}' from the "
                    f"EyeLink Host PC to {edf_destination}: {e}. The recording should still "
                    f"be on the Host PC's disk — retrieve it manually before wiping anything."
                ) from e

    def _edf_not_retrieved(self, edf_destination: Path, what: str) -> str:
        """The message for a run whose EDF never left the Host PC: what
        failed, where the file is, and where it belongs.

        The last clause is why this cannot wait: every session on this rig
        opens its EDF under the one configured name, so the next session's
        connect() opens a new file on the Host PC under this run's file's
        name.
        """
        edf = self._cfg.edf_host_filename
        return (
            f"EyeLink EDF '{edf}' was not retrieved: {what}. This run's eye data is still on "
            f"the Host PC at {self._cfg.host_ip}, as '{edf}' — copy it from there by hand to "
            f"{edf_destination} before another session runs on this rig, since every session "
            f"opens its EDF on the Host PC under that same name."
        )
