"""ViewPixxTracker: the VPixx TRACKPixx3 backend, via ``pypixxlib``.

Physical picture, and how it differs from the EyeLink sitting next to it in
this package: the TRACKPixx3 is a camera inside the VPixx display chassis
(a VIEWPixx/PROPixx driven by a DATAPixx3), not a separate machine. There is
no Host PC and no EDF. Samples land in the DATAPixx3's own fixed-size RAM
ring buffer, and the stimulus computer is what drains that buffer to disk —
so a session that only drains at the end loses its oldest samples once the
ring wraps. That is why :meth:`stop_trial` drains every trial.

Three facts about the device decide most of what follows:

- **Coordinates.** The TRACKPixx3 reports gaze in *centered* px (origin at
  the screen centre, y up) — the frame PsychoPy draws in, and NOT the frame
  a ``GazeSample`` carries (screen px, y down; see protocol.py). Every sample
  is converted once, here, exactly as MouseSimTracker does for the cursor.
- **Blinks.** A lost eye is reported as a coordinate of magnitude 9000 (or
  NaN), not as a dropped sample — the same shape of trap as the EyeLink's
  -32768, with the same consequence if it is read as a position.
- **Binocular.** The device always reports both eyes; a ``GazeSample``
  carries one. Which one is configuration (``eyetracker.eye``), not a guess
  made here.

What this backend writes into the run directory, in place of the EyeLink's
retrieved EDF:

- ``<base>_gaze.csv`` — the samples, in VPixx's own CSV format, written by
  the device library's own writer. alhazen does not re-encode it: the column
  layout is VPixx's to define, and a re-encoding that drifted from it would
  be wrong in a way no error could catch.
- ``<base>_gaze-messages.csv`` — every tracker message, stamped on *both*
  clocks. Nothing can be written into the TRACKPixx3's sample stream the way
  a message is written into an EDF, so the alignment the EDF carries
  internally is recorded beside the samples instead.

``pypixxlib`` is imported inside :meth:`connect`, never at module import: it
ships with VPixx's own Software Tools installer rather than PyPI, so
importing it eagerly would break ``import alhazen`` on every machine that is
not the rig.
"""

from __future__ import annotations

import csv
import logging
import math
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from alhazen.config.models import EyeTrackerConfig
from alhazen.core.clock import Clock
from alhazen.devices.eyetracker.protocol import GazeSample, HostShape
from alhazen.display.backend import DisplayBackend
from alhazen.display.screen import Screen
from alhazen.errors import TrackerError

log = logging.getLogger(__name__)

# The TRACKPixx3 parks a coordinate at this magnitude when it could not find
# the eye. VPixx's demos filter on the literal number rather than exposing a
# named constant, so it is named here instead.
TRACKING_LOST_PX = 9000.0

# Keys the calibration routine reads. Deliberately the same three roles the
# EyeLink's own calibration screen uses, so an experimenter moving between
# the two rigs is not learning a second set.
ACCEPT_KEYS = ("space", "return", "num_enter")
REDO_KEY = "backspace"
ABORT_KEY = "escape"

# libdpx addresses one device at a time (``DPxSelectDevice``). The TRACKPixx3's
# registers — overlay, wake/sleep, LED, the sample ring buffer, the gaze
# polynomial — live on the DATAPixx3 that hosts the camera, so that is the
# device every call in this backend must find selected.
HOST_DEVICE = "DATAPIXX3"

_OPEN_FAILED = (
    "TRACKPixx3 could not be opened: {reason}. Check that the DATAPixx3 is powered "
    "and connected, that no other VPixx application holds the device, or use "
    "eyetracker backend 'mouse_sim'."
)


def wake_tracker(libdpx: Any) -> None:
    """Bring the tracker up the way VPixx's own TRACKPixx3 demos do.

    Not ``TRACKPixx3.open()``. In pypixxlib 1.9.2 the constructor and
    ``open()`` both leave libdpx addressing the camera controller — a USB
    device of its own, part number 13328 — and ``open()`` then writes the
    overlay register, which is on the DATAPixx3. The write is out of range
    for the controller, so ``open()`` fails with DPX_ERR_SETREG16_ADDR_RANGE
    on a perfectly healthy rig, every time. VPixx's demos never call it: they
    DPxOpen, hide the overlay, wake the tracker and flush the register cache
    with the DATAPixx3 selected. The constructor has already done the open,
    so this puts the selection back and does the rest.
    """
    libdpx.DPxSelectDevice(HOST_DEVICE)
    libdpx.TPxHideOverlay()
    libdpx.DPxSetTPxAwake()
    libdpx.DPxUpdateRegCache()


def dpx_fault(libdpx: Any) -> str | None:
    """The fault libdpx's free functions left behind, cleared — or None.

    libdpx does not raise: a failed register access sets a sticky error code
    and later calls carry on. pypixxlib's device classes check the flag after
    each method (their ``DpxExceptionDecorate``); its free functions do not,
    so a caller of those has to. Clearing it here is what keeps one fault
    from being reported again by the next unrelated check.
    """
    error = libdpx.DPxGetError()
    if error == "DPX_SUCCESS":
        return None
    detail = libdpx.DPxGetErrorString()
    libdpx.DPxClearError()
    return f"{error} ({detail})"


def recording_armed(libdpx: Any, tracker: Any) -> bool:
    """Is the device still streaming samples into the ring this backend drains?

    The per-target calibration call (``TPxGetEyePositionDuringCalib``) does
    two things VPixx's documentation does not mention: it switches free-run
    sampling OFF and re-points the ring at a 64 KB buffer at address 0, for
    its own use. After that, the class's read pointer names an address inside
    a ring that no longer exists, and ``TPxSaveToCSV`` from it never returns
    — the session hangs with it and Windows kills the process as "not
    responding" (observed on the rig, 2026-09-01). So every drain first asks
    whether the ring is still the one it was armed with.
    """
    libdpx.DPxUpdateRegCache()
    return bool(libdpx.TPxIsFreeRun()) and int(libdpx.TPxGetBuffBaseAddr()) == int(
        tracker.buffer_base_addr
    )


def arm_recording(libdpx: Any, tracker: Any) -> None:
    """Point the device's ring back at this backend's buffer and start
    free-run sampling, with the class's read pointer at the ring's start.

    The same three steps ``setUpDataRecording`` performs, minus the new file
    name: samples keep appending to the CSV the session already has.
    """
    libdpx.TPxSetBuff(tracker.buffer_base_addr, tracker.buffer_size)
    tracker.last_read_addr = tracker.buffer_base_addr
    libdpx.TPxEnableFreeRun()
    libdpx.DPxUpdateRegCache()


def eyes_detected(libdpx: Any) -> tuple[bool, bool]:
    """(left, right): is the camera fitting a pupil in each eye right now?

    ``TPxGetPupilSize`` is the semi-axes of the ellipse fitted to each pupil,
    and all four are 0.0 when there is no eye in the image. The gaze report
    cannot say this before a calibration exists — it is the tracking-lost
    sentinel until then — so this is what the calibration screen shows the
    experimenter, who otherwise accepts every target blind.
    """
    left_major, _left_minor, right_major, _right_minor = libdpx.TPxGetPupilSize()
    return float(left_major) > 0.0, float(right_major) > 0.0


def eye_status_text(left: bool, right: bool) -> str:
    """The calibration screen's one line of feedback."""
    if left and right:
        return "eyes: both tracked"
    if left or right:
        return f"eyes: {'left' if left else 'right'} only"
    return "NO EYE IN THE CAMERA IMAGE — check position, focus and LED (accept is refused)"


# How old the newest gaze report may be before get_gaze() calls it "no
# verifiable position". A reader thread that has fallen this far behind is a
# stalled USB call, and a position from before the stall is not this frame's.
GAZE_STALE_S = 0.1
# How often the calibration screen refreshes its eye status between keys.
STATUS_REFRESH_S = 0.1
# How long a failed calibration's verdict stays on screen before the pause
# menu replaces it.
CALIBRATION_FAIL_HOLD_S = 2.5


def _no_release() -> None:
    return None


def request_fine_timer() -> Callable[[], None]:
    """Ask Windows for 1 ms scheduler ticks; returns the call that gives them
    back. A no-op elsewhere, and on a Windows that refuses.

    Windows sleeps in 15.6 ms ticks unless asked otherwise, which turned the
    reader's 1 ms pause between reads into ~67 reads/s on the rig.
    """
    windll = getattr(ctypes_module(), "windll", None)
    if sys.platform != "win32" or windll is None:
        return _no_release
    winmm = windll.winmm
    if winmm.timeBeginPeriod(1) != 0:
        return _no_release

    def release() -> None:
        winmm.timeEndPeriod(1)

    return release


def ctypes_module() -> Any:
    import ctypes

    return ctypes


class GazeReader:
    """Reads the device's gaze report on its own thread, so a frame never
    waits on USB.

    One ``TPxBestPolyGetEyePosition`` is a USB round trip: 2 ms as a rule,
    but 20-40 ms on one call in five on the rig (measured 2026-09-01), and a
    frame at 120 Hz is 8.3 ms. Read on the render thread, that tail dropped
    thirty frames in every trial. Read here, the render thread only copies
    the newest report, and a slow read costs staleness — a sample a frame or
    two old — rather than a dropped frame.

    ``lock`` serialises every call into libdpx across the session: the
    library keeps global state (its register cache, the selected device, the
    sticky error flag) and says nothing about threads. The reader holds it
    for one read at a time and sleeps between reads, so the render thread's
    own calls (messages, drains) get in promptly.

    A read that raises stops the thread, and the next ``latest()`` re-raises
    it on the caller's thread: a tracker that stops answering must abort
    loudly (invariant 6), not quietly report "no eye" for the rest of the
    session.
    """

    def __init__(
        self,
        read: Callable[[], Sequence[float]],
        clock: Clock,
        lock: threading.Lock,
        interval_s: float = 0.004,
    ) -> None:
        self._read = read
        self._clock = clock
        self._lock = lock
        self._interval = interval_s
        self._latest: tuple[list[float], float] | None = None
        self._fault: BaseException | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self._release_timer: Callable[[], None] = _no_release

    def start(self) -> None:
        self._stop.clear()
        self._release_timer = request_fine_timer()
        self._thread = threading.Thread(target=self._run, name="trackpixx-gaze", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._release_timer()
        self._release_timer = _no_release

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def pause(self) -> None:
        """Leave the device to the caller (a calibration) until resume()."""
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def read_now(self) -> None:
        """One read, on the calling thread: what the loop does, and what a
        caller with no thread running does to refresh. A read that fails is
        recorded as the reader's fault and raised as a TrackerError."""
        try:
            with self._lock:
                positions = list(self._read())
        except Exception as e:  # pypixxlib's exception type cannot be named off the rig
            self._fault = e
            raise TrackerError(f"the TRACKPixx3 stopped answering: {e}") from e
        self._latest = (positions, self._clock.now())

    def latest(self) -> tuple[list[float], float] | None:
        """The newest (positions, session time) report, or None before the
        first read. Raises what the reader thread died of, if it did."""
        if self._fault is not None:
            raise TrackerError(f"the TRACKPixx3 stopped answering: {self._fault}") from self._fault
        return self._latest

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(self._interval * 10)
                continue
            try:
                self.read_now()
            except TrackerError:
                return  # recorded as the fault; latest() re-raises it on the caller
            time.sleep(self._interval)


def is_tracking_lost(x: float, y: float) -> bool:
    """True when a reported coordinate is the device's "no eye" report.

    A blink is *data arriving that says "no eye"*, not an absence of data:
    the TRACKPixx3 keeps streaming at full rate and parks the coordinate at
    ±9000 px — or hands back NaN, when the calibration polynomial cannot be
    evaluated at all. Read as a position, 9000 px is roughly a couple of
    hundred degrees off screen, and any phase that latches "the last known
    gaze" then records that as the trial's measurement. Subjects blink
    constantly; this is not an edge case.

    A free function, not an inlined check, precisely so the decision is
    testable on a machine with no VPixx hardware attached.
    """
    return (
        math.isnan(x) or math.isnan(y) or abs(x) >= TRACKING_LOST_PX or abs(y) >= TRACKING_LOST_PX
    )


def select_eye(positions: Sequence[float], eye: str) -> tuple[float, float] | None:
    """Reduce the device's binocular report to the one gaze a sample carries.

    ``positions`` is what ``TRACKPixx3.getEyePosition()`` returns, in VPixx's
    documented order — ``[x_left, y_left, x_right, y_right]``, centered px.
    Returns None when the requested eye is not being tracked, which the
    caller passes on as "no verifiable position" (protocol.py).

    ``average`` requires *both* eyes on purpose. Falling back to whichever
    eye is still tracked would change what the number means partway through
    a trial — a cyclopean estimate for some samples and a monocular one for
    others — with nothing in the data saying which is which. A subject whose
    second eye drops out for a moment is exactly the case the blink rule
    already handles correctly.
    """
    if len(positions) < 4:
        raise TrackerError(
            f"TRACKPixx3 reported {len(positions)} gaze values; expected 4 "
            f"(x_left, y_left, x_right, y_right). Check the pypixxlib version on this rig."
        )
    left = (float(positions[0]), float(positions[1]))
    right = (float(positions[2]), float(positions[3]))
    left_lost = is_tracking_lost(*left)
    right_lost = is_tracking_lost(*right)

    if eye == "left":
        return None if left_lost else left
    if eye == "right":
        return None if right_lost else right
    if eye == "average":
        if left_lost or right_lost:
            return None
        return ((left[0] + right[0]) / 2.0, (left[1] + right[1]) / 2.0)
    raise TrackerError(f"unknown eyetracker.eye {eye!r} — expected left, right or average")


def calibration_targets(
    calibration_type: str, screen: Screen, area: float
) -> list[tuple[float, float]]:
    """Where the calibration targets go, in centered px (y up).

    The EyeLink's Host PC owns its own target grid; the TRACKPixx3 has no
    Host PC, so alhazen lays this one out. ``area`` is the fraction of the
    screen the grid spans, matching what the EyeLink backend sends as
    ``calibration_area_proportion`` — a subject calibrated over a smaller
    effective field on one rig gets the same field on the other.

    Centre first, then outward. The order matters to the experimenter, not
    the fit: starting at the centre gives them one target to confirm the
    subject is even being tracked before the grid walks off to a corner.
    """
    w = (screen.width_px / 2.0) * area
    h = (screen.height_px / 2.0) * area
    centre = (0.0, 0.0)
    if calibration_type == "HV5":
        # A plus: centre, up, down, left, right.
        return [centre, (0.0, h), (0.0, -h), (-w, 0.0), (w, 0.0)]
    if calibration_type in ("HV9", "HV13"):
        grid = [
            centre,
            (0.0, h),
            (0.0, -h),
            (-w, 0.0),
            (w, 0.0),
            (-w, h),
            (w, h),
            (-w, -h),
            (w, -h),
        ]
        if calibration_type == "HV9":
            return grid
        # HV13 adds four points at half eccentricity, which is what makes the
        # fit describe the middle of the field rather than interpolating it.
        return [*grid, (-w / 2, h / 2), (w / 2, h / 2), (-w / 2, -h / 2), (w / 2, -h / 2)]
    raise TrackerError(
        f"calibration_type {calibration_type!r} has no target layout for the viewpixx "
        f"backend. This should have been rejected when the config loaded "
        f"(config/models.py); reaching here means those two lists disagree."
    )


class ViewPixxTracker:
    """pypixxlib-backed EyeTracker for a VPixx TRACKPixx3.

    Like EyeLinkTracker, the device-touching methods are not exercised by the
    default test suite — they need pypixxlib and a DATAPixx3. Everything that
    is a *decision* rather than a device call (which eye, what counts as a
    blink, where the targets go, what lands in the run directory) is a free
    function above or a plain path computation below, and those are tested.
    """

    def __init__(
        self,
        cfg: EyeTrackerConfig,
        display: DisplayBackend | None,
        screen: Screen,
        clock: Clock,
        *,
        background_gaze: bool = True,
    ) -> None:
        self._cfg = cfg
        # None is legitimate: check-rig constructs this class to exercise the
        # real open()/close() path without opening a subject window. Only
        # calibrate() needs the window.
        self._display = display
        self._screen = screen
        self._clock = clock
        # Everything below is hardware-derived state; the sentinels are how
        # stop_trial()/shutdown() know whether there is anything real to undo.
        self._tracker: Any = None  # pypixxlib TRACKPixx3 handle, once open
        self._libdpx: Any = None  # pypixxlib's free functions, once imported
        # One lock around every call into the device library (see GazeReader).
        self._device_lock = threading.Lock()
        # False only for callers that want every read on their own thread —
        # the unit tests, which then see exactly the report they queued.
        self._background_gaze = background_gaze
        self._reader: GazeReader | None = None
        self._recording = False
        # Where the device library is writing its CSV, and the scratch
        # directory it chose that name inside. Both are None until configure()
        # starts a recording.
        self._samples_path: Path | None = None
        self._scratch_dir: Path | None = None
        # (device clock seconds, session clock seconds, text) for every
        # message, held until shutdown writes them beside the samples.
        self._messages: list[tuple[float, float, str]] = []

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the link to the DATAPixx3 and wake its tracker."""
        try:
            from pypixxlib import _libdpx as libdpx
            from pypixxlib.tracker import TRACKPixx3
        except ImportError as e:
            raise TrackerError(
                "pypixxlib is not installed in this Python environment. It is NOT on PyPI: "
                "VPixx's Software Tools installer (vpixx.com/software-tools) leaves it on "
                "the rig as a source archive — on Windows, "
                "C:\\Program Files\\VPixx Technologies\\Software Tools\\pypixxlib\\"
                "pypixxlib-<version>.tar.gz — and that file is what to pip install into "
                "this environment. Or use eyetracker backend 'mouse_sim' for development."
            ) from e

        try:
            self._tracker = TRACKPixx3()
            self._libdpx = libdpx
            wake_tracker(libdpx)
            fault = dpx_fault(libdpx)
        except Exception as e:
            # Deliberately broad: pypixxlib signals device faults with its own
            # DpxException, which cannot be imported here to name in an
            # `except` clause — this module must import on machines with no
            # VPixx software at all. `from e` keeps the original traceback, so
            # nothing is hidden by the widening.
            raise TrackerError(_OPEN_FAILED.format(reason=e)) from e
        if fault is not None:
            raise TrackerError(_OPEN_FAILED.format(reason=fault))

    def configure(self, screen: Screen, clock: Clock) -> None:
        """Apply the rig's tracker settings and start the device recording.

        The clock arrives here as well as at construction so every backend's
        configure step has the same shape; gaze is stamped from it, never
        from the device's own clock (invariant 2).

        Recording starts *now*, for the whole session, rather than per trial.
        That is what the device library's own buffer/writer pair supports, and
        it matches what an EyeLink EDF actually contains: one continuous
        recording with the trial boundaries marked inside it.
        """
        self._screen = screen
        self._clock = clock
        if self._cfg.led_intensity is not None:
            # Only when the rig asked. Left alone, the illuminator keeps
            # whatever VPixx's own camera-setup tool left on the device —
            # the same division of labour as the EyeLink, whose camera setup
            # lives on the Host PC and not in alhazen's config.
            self._tracker.setLEDintensity(self._cfg.led_intensity)

        # The device library picks its own filename inside whatever folder it
        # is given (``<folder>/data/TPx_<timestamp>.csv``), and the run
        # directory is not known here — the tracker is built before the
        # session's paths are. So it writes into a scratch directory and
        # shutdown() moves the finished file into the run directory under the
        # run's own name.
        self._scratch_dir = Path(tempfile.mkdtemp(prefix="alhazen-trackpixx-"))
        self._samples_path = Path(self._tracker.setUpDataRecording(str(self._scratch_dir)))
        log.info(
            "TRACKPixx3 is recording to %s; teardown moves it into the run directory",
            self._samples_path,
        )
        # Gaze is read off the render thread from here on (GazeReader).
        # Looked up per read, not bound once: the handle's methods are what
        # the device answers through, and a swapped-in one must be honoured.
        self._reader = GazeReader(lambda: self._tracker.getEyePosition(), clock, self._device_lock)
        if self._background_gaze:
            self._reader.start()

    def calibrate(self) -> None:
        """Walk the target grid and fit the device's gaze model. Blocks until
        the experimenter finishes or aborts.

        Unlike the EyeLink — where ``doTrackerSetup()`` hands the whole
        procedure to the Host PC — nothing else can run this: the targets have
        to be drawn in the session's own window. The experimenter drives it,
        one target at a time, rather than a fixation-stability heuristic
        deciding when a subject is looking at the right place: a wrong guess
        there fits the model to the wrong point and every gaze position in
        the session inherits the error.

        Aborting leaves the *previous* calibration on the device untouched,
        because the fit is only committed by the ``finishCalibration()`` at
        the end. That is the same deliberate-experimenter-action-not-a-fault
        treatment the EyeLink backend gives an ESC during calibration.
        """
        if self._display is None:
            raise TrackerError(
                "ViewPixxTracker.calibrate() needs an open display to draw its targets in; "
                "this instance was constructed without one (check-rig does that deliberately "
                "and never calibrates)."
            )
        from psychopy import event, visual

        # Everything buffered so far is saved BEFORE the device's calibration
        # routine re-points the ring (recording_armed): it is the only copy.
        self._drain_buffer()
        reader = self._reader
        if reader is not None:
            reader.pause()
        try:
            self._walk_targets(event, visual)
        finally:
            # Whether the walk finished or aborted, the device is left
            # recording into this backend's ring again — a calibration that
            # got as far as one target has already switched it off.
            with self._device_lock:
                if not recording_armed(self._libdpx, self._tracker):
                    arm_recording(self._libdpx, self._tracker)
            if reader is not None:
                reader.resume()

    def _walk_targets(self, event: Any, visual: Any) -> None:
        """The target walk itself; calibrate() owns the recording around it."""
        assert self._display is not None  # calibrate() checked
        window = self._display.window
        targets = calibration_targets(
            self._cfg.calibration_type, self._screen, self._cfg.calibration_area
        )
        # The standard eye-tracking target: a disc with a hole, so the subject
        # has an unambiguous point to look at rather than a blob's centre.
        foreground = (-1.0, -1.0, -1.0)
        outer = visual.Circle(
            window, radius=12, units="pix", fillColor=foreground, lineColor=foreground
        )
        inner = visual.Circle(
            window, radius=4, units="pix", fillColor=window.color, lineColor=window.color
        )
        # The experimenter's only view of the camera: the TRACKPixx3 has no
        # Host PC, and VPixx's own viewer (LabMaestro) must not run alongside
        # a session — it shares the device server, and did hang with one.
        status = visual.TextStim(
            window,
            text="",
            pos=(0, -0.4 * self._screen.height_px),
            height=max(18.0, 0.02 * self._screen.height_px),
            color=(0.82, 0.82, 0.86),
            units="pix",
        )

        index = 0
        while index < len(targets):
            x, y = targets[index]
            outer.pos = inner.pos = (x, y)
            pressed: str | None = None
            eyes = (False, False)
            while pressed is None:
                with self._device_lock:
                    eyes = eyes_detected(self._libdpx)
                status.text = eye_status_text(*eyes)
                outer.draw()
                inner.draw()
                status.draw()
                window.flip()
                keys = event.waitKeys(
                    maxWait=STATUS_REFRESH_S, keyList=[*ACCEPT_KEYS, REDO_KEY, ABORT_KEY]
                )
                if keys:
                    pressed = keys[0]
                elif getattr(window, "_closed", False):
                    # The window went away: abort, never accept.
                    pressed = ABORT_KEY
            if pressed == ABORT_KEY:
                log.warning(
                    "TRACKPixx3 calibration aborted by the experimenter at target %d of %d; "
                    "the device keeps its previous calibration",
                    index + 1,
                    len(targets),
                )
                window.flip()
                return
            if pressed == REDO_KEY:
                # Step back rather than re-showing this one: the point the
                # experimenter wants to redo is almost always the one just
                # accepted, not the one still on screen.
                index = max(0, index - 1)
                continue
            if not any(eyes):
                # A target accepted with no eye in the image fits the model
                # to nothing, and the device reports the whole calibration as
                # absent at the end (it did, on the rig). Stay on this one.
                log.warning(
                    "target %d of %d not accepted: no eye in the camera image",
                    index + 1,
                    len(targets),
                )
                continue
            # Screen coordinates here are the device's own frame — centered
            # px, y up — which is the frame the targets were drawn in, so the
            # position passes through unconverted.
            with self._device_lock:
                self._tracker.getEyePositionDuringCalib(x, y, self._tracker.eye_to_verify)
            index += 1

        with self._device_lock:
            self._tracker.finishCalibration()
            calibrated = bool(self._tracker.isDeviceCalibrated())
        window.flip()
        if calibrated:
            log.info("TRACKPixx3 calibrated over %d targets", len(targets))
            return
        # The fit was submitted and the device did not keep it — every gaze
        # read from here would be the tracking-lost sentinel, and a session
        # that looks calibrated but is not is exactly what must not happen
        # quietly. Said on the screen the experimenter is looking at, then
        # the pause menu follows and they can run it again.
        log.error(
            "TRACKPixx3 calibration did NOT take: the device reports no calibration after "
            "%d targets. Check that the camera sees the eyes (position, focus, LED) and "
            "calibrate again.",
            len(targets),
        )
        self._display.show_message(
            "Calibration FAILED: the tracker reports no calibration.\n"
            "Check the camera sees the eyes (position, focus, LED), then calibrate again."
        )
        event.waitKeys(maxWait=CALIBRATION_FAIL_HOLD_S, keyList=[*ACCEPT_KEYS, ABORT_KEY])

    # ------------------------------------------------------------------
    # Per trial
    # ------------------------------------------------------------------

    def start_trial(self, trial_index: int, status: str) -> None:
        """Open this trial's segment in the message record.

        There is no per-trial hardware start to make: the device has been
        free-running since configure(). What a trial opens is the span the
        analysis will cut out of the continuous sample file, which is exactly
        what the messages mark.
        """
        self._recording = True
        # The only durable record, inside the run's own files, of which eye
        # each trial's samples came from. Written per trial rather than once,
        # so a trial's segment is self-describing — the same reason the
        # EyeLink backend re-sends EYE_USED every trial.
        self.send_message(f"TRIAL {trial_index} {status}")
        self.send_message(f"EYE_USED {self._cfg.eye}")

    def stop_trial(self) -> None:
        """Close this trial's segment and drain the device's buffer.

        Idempotent: a trial can end before start_trial() ever set recording,
        and the runner calls this in a ``finally`` regardless.
        """
        if not self._recording:
            return
        self._recording = False
        self._drain_buffer()

    def is_recording(self) -> bool:
        return self._recording

    def get_gaze(self) -> GazeSample | None:
        """Newest sample for the configured eye, or None.

        None means "no verifiable position": the eye is not being tracked, or
        (with ``eye: average``) one of the two is not. Both are routine; the
        phases decide what a gap means, this method never guesses.
        """
        reader = self._reader
        if reader is None:
            raise TrackerError("get_gaze() before configure(): the tracker is not recording")
        if not reader.running:
            reader.read_now()  # no thread (tests, synchronous callers): read here
        report = reader.latest()
        if report is None:
            return None  # nothing has been read yet
        positions, t = report
        if self._clock.now() - t > GAZE_STALE_S:
            return None  # the reader is behind: a stalled USB call, no position for now
        chosen = select_eye(positions, self._cfg.eye)
        if chosen is None:
            return None
        # The device speaks CENTERED, y-up px; every GazeSample in alhazen is
        # SCREEN px, y-down (protocol.py). Convert here, once, at this
        # boundary, so nothing downstream has to know which backend produced
        # a sample.
        gx, gy = self._screen.centered_to_screen(*chosen)
        return GazeSample(gx=gx, gy=gy, t=t)

    def send_message(self, text: str) -> None:
        """Record one marker against BOTH clocks.

        The EyeLink writes messages into the EDF, so an EDF carries its own
        alignment to the task. Nothing can be written into the TRACKPixx3's
        sample stream, so the alignment is recorded beside it instead: each
        message is stamped with the device clock (the one the sample file's
        timestamps are on) and the session clock (the one every event, flip
        and command is on) read at the same moment. Those pairs are what let
        an analysis put the two files together.

        Errors are not caught (invariant 6): if the device stops answering,
        the samples being written are losing their alignment marks, which
        must abort loudly rather than produce a session that only looks
        recorded.
        """
        with self._device_lock:
            device_time = float(self._tracker.getTime())
        self._messages.append((device_time, self._clock.now(), text))

    def draw_host_overlay(self, shapes: list[HostShape]) -> None:
        # No operator display exists to draw on. The device *can* put its eye
        # image on a monitor (showOverlay), but that monitor is the subject's
        # own screen — drawing fixation windows there would put them in front
        # of the subject, mid-trial.
        return

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _drain_buffer(self) -> None:
        """Append everything the device has buffered since the last drain.

        Not an optimisation. The DATAPixx3 buffers samples in a fixed-size
        ring, and a session longer than that ring silently overwrites its own
        oldest samples. Draining every trial is what keeps the written file
        complete.
        """
        if self._samples_path is None:
            return  # configure() never started a recording
        with self._device_lock:
            if not recording_armed(self._libdpx, self._tracker):
                # Draining now would hand the library a read pointer into a
                # ring that is gone, and that call never returns. Re-arm and
                # say what was lost, rather than hang the session.
                log.warning(
                    "the TRACKPixx3's sample ring was reconfigured under the session (a "
                    "calibration does this); recording re-armed, samples since the last "
                    "drain are lost"
                )
                arm_recording(self._libdpx, self._tracker)
                return
            self._tracker.saveBufferedData()

    def _write_messages(self, destination: Path) -> None:
        """Write the two-clock message record beside the samples."""
        with destination.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["device_time_s", "session_time_s", "message"])
            writer.writerows(self._messages)

    def shutdown(self, recording_destination: Path | None) -> None:
        """Drain the device, move its recording into the run directory, and
        drop the link.

        A failed move *raises*: the samples file is the only full-rate record
        of the session's eye data, and until this runs it is sitting in a
        scratch directory. Swallowing a failure here is how a completed
        session's eye data quietly stays in a temp folder that the next
        reboot clears.
        """
        if self._tracker is None:
            return  # connect() never ran: nothing was opened
        if self._reader is not None:
            self._reader.stop()  # before the drain: nothing else may touch the device

        if self._recording:
            # The session can end mid-trial (quit, abort); stop_trial() owns
            # the correct close-then-drain sequence, so delegate rather than
            # re-deriving it here.
            self.stop_trial()
        self._drain_buffer()

        try:
            if recording_destination is not None and self._samples_path is not None:
                self._deliver_recording(recording_destination)
        finally:
            # Whatever happened to the files, the device link is released —
            # a held DATAPixx3 blocks the next session from opening it.
            self._tracker.close()

    def _deliver_recording(self, destination: Path) -> None:
        """Move the device's CSV, and write the messages, into the run dir.

        ``destination`` is the path the runner hands every backend: the run
        directory's base name with the EyeLink's historical ``.edf`` suffix.
        This backend's recording is not an EDF, so it takes the stem and adds
        its own two files — which is the whole reason the suffix belongs to
        the backend and not to the runner.
        """
        samples_path = self._samples_path
        assert samples_path is not None  # only called when it is not
        samples_target = destination.with_name(f"{destination.stem}_gaze.csv")
        messages_target = destination.with_name(f"{destination.stem}_gaze-messages.csv")

        # Messages first: they are held in memory and cost nothing to write,
        # so a failure moving the (large) samples file still leaves the
        # session's alignment marks on disk rather than losing both.
        self._write_messages(messages_target)

        if not samples_path.exists():
            raise TrackerError(
                f"the TRACKPixx3 recording {samples_path} does not exist at teardown, so "
                f"nothing was written to {samples_target}. No eye samples were saved for "
                f"this run; check the DATAPixx3's buffer configuration before running again."
            )
        try:
            # shutil.move, not Path.rename: the scratch directory and the data
            # root are routinely on different filesystems, which rename cannot
            # cross.
            shutil.move(str(samples_path), str(samples_target))
        except OSError as e:
            raise TrackerError(
                f"failed to move the TRACKPixx3 recording {samples_path} to "
                f"{samples_target}: {e}. The samples are still at {samples_path} — copy "
                f"them out before that directory is cleared."
            ) from e
        self._samples_path = None
        self._discard_scratch_dir()

    def _discard_scratch_dir(self) -> None:
        """Remove the scratch directory, once its file is safely elsewhere.

        Best effort, and loud about it: a leftover temp directory is untidy,
        never a data loss, so it must not turn a successful teardown into a
        failed one.
        """
        if self._scratch_dir is None:
            return
        try:
            shutil.rmtree(self._scratch_dir)
        except OSError as e:
            log.warning(
                "could not remove the tracker's scratch directory %s: %s", self._scratch_dir, e
            )
        self._scratch_dir = None
