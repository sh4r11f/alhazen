"""Stand-ins for the two eye-tracker SDKs, shared by every test that drives a
real tracker backend: the backends' own unit tests, check-rig's, and the
sessions that run on one.

``pylink`` and ``pypixxlib`` ship only with the vendors' own installers and
are imported inside each backend's ``connect()`` (invariant 7), so a test
installs one of these into ``sys.modules`` first and the backend imports it
none the wiser. They are simulations written from what alhazen's code assumes
the SDKs do, in one place — not recordings of real devices. The rig
verification checklist in docs/eye-tracker.md is how those assumptions get
checked against the hardware.
"""

from __future__ import annotations

import ctypes
import math
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from alhazen.testing import FakeClock

# ---------------------------------------------------------------------------
# pylink: an EyeLink Host PC that records
# ---------------------------------------------------------------------------

# The pylink constants the EyeLink backend reads, with the values SR
# Research's eyelink.h gives them.
LEFT_EYE, RIGHT_EYE, BINOCULAR = 0, 1, 2
MISSING_DATA = -32768.0
TRIAL_OK = 0
TRIAL_ERROR = -1
REPEAT_TRIAL = 1
SKIP_TRIAL = 2
ABORT_EXPT = 3


class FakeLinkSample:
    """One link sample: the eye's gaze and the tracker's own timestamp in ms,
    which — like the real one — stays the same for as long as no newer
    sample has arrived."""

    def __init__(self, gx: float, gy: float, tracker_ms: float, eye: int = LEFT_EYE) -> None:
        self._gaze = (gx, gy)
        self._time = tracker_ms
        self._eye = eye

    def getTime(self) -> float:  # noqa: N802 - pylink's names
        return self._time

    def isLeftSample(self) -> bool:  # noqa: N802
        return self._eye in (LEFT_EYE, BINOCULAR)

    def isRightSample(self) -> bool:  # noqa: N802
        return self._eye in (RIGHT_EYE, BINOCULAR)

    def getLeftEye(self) -> types.SimpleNamespace:  # noqa: N802
        return types.SimpleNamespace(getGaze=lambda: self._gaze)

    def getRightEye(self) -> types.SimpleNamespace:  # noqa: N802
        return types.SimpleNamespace(getGaze=lambda: self._gaze)


class FakeEyeLinkHost:
    """``pylink.EyeLink``, simulating a Host PC that records over the link.

    While it records it takes a sample every ``1 / rate_hz`` of the session
    clock, stamped on its own clock in ms, and ``getNewestSample()`` hands
    back the newest: a new sample once the clock has moved on a tick, the same
    one when it has not — a repeat, as on the real link. A blink is a sample
    like any other, its gaze the MISSING_DATA sentinel (set ``gaze``). Once
    recording stops, for whatever reason, the newest sample freezes.

    The ways a recording stops, as a test chooses:

    - ``host_stop(code)``: the Host PC leaves record mode, and
      ``isRecording()`` answers ``code`` — TRIAL_ERROR, or the code of the
      operator's choice on the Host PC's abort menu;
    - ``pull_cable()``: samples stop arriving, while the Host PC — as far as
      pylink last heard — still records (``isRecording()`` is 0);
    - ``link_down()``: samples stop, and every call that talks to the Host PC
      raises RuntimeError, as pylink does on a dead link;
    - ``stop_at_s``: ``host_stop()`` by itself at that session time.
    """

    def __init__(self, host_ip: str, clock: FakeClock, rate_hz: float = 1000.0) -> None:
        self.host_ip = host_ip
        self.clock = clock
        self.rate_hz = rate_hz
        self.recording = False
        self.delivering = False
        self.link_up = True
        # What isRecording() answers once recording has stopped.
        self.ended_with = TRIAL_ERROR
        self.gaze: tuple[float, float] = (960.0, 540.0)
        self.eye = LEFT_EYE
        # What startRecording() returns: 0 is success, anything else a code.
        self.start_error = 0
        self.stop_at_s: float | None = None
        self.newest: FakeLinkSample | None = None
        self.commands: list[str] = []
        self.messages: list[str] = []
        self.data_file: str | None = None
        self.recordings_started = 0
        self.stop_calls = 0
        self.isrecording_calls = 0
        self.closed = False

    # --- the ways a recording stops -------------------------------------

    def host_stop(self, code: int = TRIAL_ERROR) -> None:
        self.recording = False
        self.delivering = False
        self.ended_with = code

    def pull_cable(self) -> None:
        self.delivering = False

    def link_down(self) -> None:
        self.link_up = False
        self.delivering = False

    def _scheduled_stop(self) -> None:
        if self.stop_at_s is not None and self.recording and self.clock.now() >= self.stop_at_s:
            self.stop_at_s = None
            self.host_stop()

    def _talk(self) -> None:
        """Every call that goes to the Host PC: refused on a dead link."""
        self._scheduled_stop()
        if not self.link_up:
            raise RuntimeError("link terminated")

    # --- pylink.EyeLink -------------------------------------------------

    def openDataFile(self, name: str) -> None:  # noqa: N802 - pylink's names
        self._talk()
        self.data_file = name

    def sendCommand(self, text: str) -> None:  # noqa: N802
        self._talk()
        self.commands.append(text)

    def sendMessage(self, text: str) -> None:  # noqa: N802
        self._talk()
        self.messages.append(text)

    def setOfflineMode(self) -> None:  # noqa: N802
        # Offline is "not recording" on the Host PC.
        self._talk()
        self.recording = False
        self.delivering = False

    def startRecording(self, *flags: int) -> int:  # noqa: N802
        self._talk()
        if self.start_error:
            return self.start_error
        self.recording = True
        self.delivering = True
        self.recordings_started += 1
        return 0

    def stopRecording(self) -> None:  # noqa: N802
        self._talk()
        self.stop_calls += 1
        self.recording = False
        self.delivering = False

    def isRecording(self) -> int:  # noqa: N802
        self.isrecording_calls += 1
        self._talk()
        return TRIAL_OK if self.recording else self.ended_with

    def eyeAvailable(self) -> int:  # noqa: N802
        self._talk()
        return self.eye

    def getNewestSample(self) -> FakeLinkSample | None:  # noqa: N802
        # A copy out of pylink's own buffer: never raises, and on a dead
        # link simply keeps handing back the last sample that arrived.
        self._scheduled_stop()
        if self.recording and self.delivering:
            tick_ms = math.floor(self.clock.now() * self.rate_hz) * 1000.0 / self.rate_hz
            if self.newest is None or self.newest.getTime() != tick_ms:
                self.newest = FakeLinkSample(*self.gaze, tick_ms, eye=self.eye)
        return self.newest

    def isConnected(self) -> int:  # noqa: N802
        return 1 if self.link_up else 0

    def closeDataFile(self) -> None:  # noqa: N802
        self._talk()

    def receiveDataFile(self, source: str, destination: str) -> None:  # noqa: N802
        self._talk()
        Path(destination).write_bytes(b"EDF")

    def close(self) -> None:
        self.closed = True

    def getTrackerVersionString(self) -> str:  # noqa: N802
        return "EYELINK CL 5.15"


def install_fake_pylink(monkeypatch: Any, clock: FakeClock, rate_hz: float = 1000.0) -> Any:
    """Put a pylink whose ``EyeLink()`` is a FakeEyeLinkHost on ``clock`` into
    ``sys.modules`` for one test; the returned namespace lists the hosts
    made (``hosts``) and holds the module.

    ``pumpDelay`` and ``msecDelay`` wait on the real SDK; here they move the
    simulated clock on, so samples keep arriving while the backend waits, as
    they would on the rig.
    """
    module = types.ModuleType("pylink")
    hosts: list[FakeEyeLinkHost] = []

    def make(host_ip: str) -> FakeEyeLinkHost:
        host = FakeEyeLinkHost(host_ip, clock, rate_hz)
        hosts.append(host)
        return host

    module.EyeLink = make  # type: ignore[attr-defined]
    module.EyeLinkCustomDisplay = object  # type: ignore[attr-defined]
    module.openGraphicsEx = lambda graphics: None  # type: ignore[attr-defined]
    module.pumpDelay = lambda ms: clock.advance(ms / 1000.0)  # type: ignore[attr-defined]
    module.msecDelay = lambda ms: clock.advance(ms / 1000.0)  # type: ignore[attr-defined]
    for name, value in {
        "LEFT_EYE": LEFT_EYE,
        "RIGHT_EYE": RIGHT_EYE,
        "BINOCULAR": BINOCULAR,
        "MISSING_DATA": MISSING_DATA,
        "TRIAL_OK": TRIAL_OK,
        "TRIAL_ERROR": TRIAL_ERROR,
        "REPEAT_TRIAL": REPEAT_TRIAL,
        "SKIP_TRIAL": SKIP_TRIAL,
        "ABORT_EXPT": ABORT_EXPT,
    }.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "pylink", module)
    return types.SimpleNamespace(module=module, hosts=hosts, clock=clock)


# ---------------------------------------------------------------------------
# pypixxlib: a TRACKPixx3 on a DATAPixx3
# ---------------------------------------------------------------------------

# The sticky error a USB transfer to a device that has gone away leaves in
# libdpx (FakeLibdpx.unplug): its free functions do not raise.
USB_GONE = "DPX_ERR_USB_REQ_FAILED"


class FakeLibdpx:
    """Stand-in for pypixxlib's ``_libdpx``: the free functions connect() uses
    to bring the tracker up, and libdpx's sticky error flag.
    """

    def __init__(self) -> None:
        self.selected: str | None = None
        self.overlay_hidden = False
        self.awake = False
        self.cache_updates = 0
        self.error = "DPX_SUCCESS"
        self.error_string = "Function executed successfully"
        # The sample ring: where the device says it is, and whether it runs.
        self.freerun = False
        self.buffer_base = 0
        self.arms = 0
        # Pupil ellipse semi-axes (left major/minor, right major/minor); all
        # zero is the device's "no eye in the image".
        self.pupils: tuple[float, float, float, float] = (3.0, 2.0, 3.0, 2.0)
        # The gaze report TPxBestPolyGetEyePosition writes: calibrated
        # positions and raw eye vectors, [x_left, y_left, x_right, y_right].
        # Raw vectors of a tracked eye are plain numbers; the device's own
        # buffers start at zero, which the backend reads as "not measured".
        self.positions: list[float] = [0.0, 0.0, 0.0, 0.0]
        self.raw_positions: list[float] = [1.5, -0.5, 1.4, -0.4]
        self.reads = 0
        # The calibration sampling call that returns raw vectors records into
        # the device's own list (FakeTrackPixx shares it), and answers with
        # [x_right, y_right, x_left, y_left] for a target. With these
        # coefficients the fit maps (raw - 1) x 100 back onto the screen, so
        # raw = target / 100 + 1 is a perfect calibration. Never exactly zero:
        # a zero raw vector is how the device reports an eye it did not measure.
        # The device whose per-target method the raw-returning call goes
        # through (set by FakeTrackPixx).
        self.device: Any = None
        self.raw_at_target: Callable[[float, float], tuple[float, float, float, float]] = (
            lambda x, y: (x / 100.0 + 1.0, y / 100.0 + 1.0, x / 100.0 + 1.0, y / 100.0 + 1.0)
        )
        identity_x = [-100.0, 100.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        identity_y = [-100.0, 0.0, 100.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        self.calibration_coefficients = identity_x + identity_y + identity_x + identity_y
        self.coefficient_error: str | None = None
        # The expected iris size register, and the largest value it keeps: a
        # test lowers the cap to mimic a device that clamps what it cannot hold.
        self.iris_size = 90
        self.iris_register_max = 10_000
        # The camera image TPxGetImagePtr hands back: 8-bit grey, row-major.
        # None is the library's NULL pointer (no image available).
        self.image: np.ndarray | None = np.full((24, 32), 200, dtype=np.uint8)
        self.image_reads = 0
        # What reading the image does to the ring, if anything — a hook a
        # test sets to mimic a device call that re-points the buffer.
        self.on_image_read: Callable[[], None] | None = None
        # A device that went away (the USB cable pulled, the DATAPixx3 off):
        # every transfer fails into the sticky error flag, and the free
        # functions carry on without raising — the gaze read leaves its
        # buffers untouched, the register cache keeps its last values.
        self.unplugged = False

    def unplug(self) -> None:
        """The device goes away (see ``unplugged``)."""
        self.unplugged = True

    def _transfer_failed(self) -> bool:
        if self.unplugged:
            self.error = USB_GONE
            self.error_string = "USB request failed"
        return self.unplugged

    def TPxGetImagePtr(self):  # noqa: N802 - vendor's name
        self.image_reads += 1
        if self.on_image_read is not None:
            self.on_image_read()
        if self.image is None:
            return ctypes.POINTER(ctypes.c_byte)(), 0, 0
        height, width = self.image.shape
        # Kept alive on the fake so the pointer stays valid until the
        # backend has copied out of it, as the device's own buffer would.
        self._image_buffer = (ctypes.c_byte * (height * width))(
            *(int(v) - 256 if v > 127 else int(v) for v in self.image.ravel())
        )
        pointer = ctypes.cast(self._image_buffer, ctypes.POINTER(ctypes.c_byte))
        return pointer, height, width

    def TPxGetEyePositionDuringCalib_returnsRaw(self, x, y, eye):  # noqa: N802 - vendor's
        # The same device call as pypixxlib's wrapper, with the raw vectors
        # handed back. Routed through the device's own method, so a test
        # that patches that method (to mimic the device un-arming the
        # sample ring) sees this call too, and every target is recorded once.
        self.device.getEyePositionDuringCalib(x, y, eye)
        return list(self.raw_at_target(x, y))

    def TPxGetCalibCoeffs(self):  # noqa: N802 - vendor's name
        if self.coefficient_error is not None:
            self.error = self.coefficient_error
        return list(self.calibration_coefficients)

    def TPxGetIrisExpectedSize(self) -> int:  # noqa: N802 - vendor's name
        return self.iris_size

    def TPxSetIrisExpectedSize(self, size: int) -> None:  # noqa: N802 - vendor's name
        self.iris_size = min(int(size), self.iris_register_max)

    def TPxSetBuff(self, base: int, size: int) -> None:  # noqa: N802 - vendor's name
        self.buffer_base = base
        self.arms += 1

    def TPxEnableFreeRun(self) -> None:  # noqa: N802 - vendor's name
        self.freerun = True

    def TPxDisableFreeRun(self) -> None:  # noqa: N802 - vendor's name
        self.freerun = False

    def TPxIsFreeRun(self) -> int:  # noqa: N802 - vendor's name
        return 1048576 if self.freerun else 0

    def TPxGetBuffBaseAddr(self) -> int:  # noqa: N802 - vendor's name
        return self.buffer_base

    def TPxGetPupilSize(self) -> tuple[float, float, float, float]:  # noqa: N802
        return self.pupils

    def TPxBestPolyGetEyePosition(self, packed, raw) -> float:  # noqa: N802 - vendor's name
        """The gaze report, both forms, written into the caller's buffers
        the way the C call does: the calibrated positions the test queued in
        ``positions``, the raw eye vectors in ``raw_positions``. On a device
        that went away, nothing is written and nothing raised."""
        self.reads += 1
        if self._transfer_failed():
            return 0.0
        for index, value in enumerate(self.positions):
            packed[index] = value
        for index, value in enumerate(self.raw_positions):
            raw[index] = value
        return 0.0

    def DPxSelectDevice(self, name: str) -> None:  # noqa: N802 - vendor's name
        self.selected = name

    def TPxHideOverlay(self) -> None:  # noqa: N802 - vendor's name
        self.overlay_hidden = True

    def DPxSetTPxAwake(self) -> None:  # noqa: N802 - vendor's name
        self.awake = True

    def DPxUpdateRegCache(self) -> None:  # noqa: N802 - vendor's name
        self.cache_updates += 1
        self._transfer_failed()

    def DPxGetError(self) -> str:  # noqa: N802 - vendor's name
        return self.error

    def DPxGetErrorString(self) -> str:  # noqa: N802 - vendor's name
        return self.error_string

    def DPxClearError(self) -> None:  # noqa: N802 - vendor's name
        self.error = "DPX_SUCCESS"


class FakeTrackPixx:
    """Stand-in for pypixxlib's TRACKPixx3, recording what it was asked to do.

    The gaze report lives on the fake libdpx (the backend reads it through
    ``TPxBestPolyGetEyePosition``, not through this class, to get the raw
    vectors pypixxlib's wrapper discards); ``positions`` and ``reads`` here
    forward to it so a test reaches everything through the one fixture.
    """

    def __init__(self) -> None:
        # The free functions the backend calls around this object. Owned by
        # the device so a test reaches both through the one fixture value.
        self.libdpx = FakeLibdpx()
        self.opened = False
        self.closed = False
        self.led_intensity: int | None = None
        self.eye_to_verify = 3  # pypixxlib's own default
        self.recording_folder: str | None = None
        self.samples_file: Path | None = None
        self.device_time = 100.0
        self.drains = 0
        self.calibration_points: list[tuple[float, float, int]] = []
        self.libdpx.device = self
        self.finished_calibration = False
        # What the device answers after finishCalibration(); False is the
        # calibration-with-no-eye case seen on the rig.
        self.calibrated_after_finish = True
        # What it answers BEFORE any calibration this session: the device
        # keeps one across runs, and the rig had one. False is a fresh
        # device, which is what the failed pilot ran on.
        self.holds_calibration = True
        # Printed by the real setUpDataRecording() and saveBufferedData().
        self.chatter = (
            "Recording data is not yet directly implemented in the TRACKPixx3 -- "
            "Please use the schedule method"
        )
        # pypixxlib's own ring layout, which the backend reads back.
        self.buffer_base_addr = 0x12000000
        self.buffer_size = 0x18000000
        self.last_read_addr = 0x12000000

    @property
    def positions(self) -> list[float]:
        return self.libdpx.positions

    @positions.setter
    def positions(self, value: list[float]) -> None:
        self.libdpx.positions = list(value)

    @property
    def reads(self) -> int:
        return self.libdpx.reads

    def _answer(self) -> None:
        """pypixxlib's device-class methods raise on a failed transfer (their
        DpxException, which only exists on the rig: an OSError stands in)."""
        if self.libdpx.unplugged:
            raise OSError(f"{USB_GONE}: the DATAPixx3 did not answer")

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def setLEDintensity(self, value: int) -> None:  # noqa: N802 - vendor's name
        self.led_intensity = value

    def setUpDataRecording(self, folder: str) -> str:  # noqa: N802 - vendor's name
        # Mirrors pypixxlib: it prints its note, then picks the name itself,
        # inside a data/ subdirectory of the folder it was given.
        print(self.chatter)
        self.recording_folder = folder
        data_dir = Path(folder) / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.samples_file = data_dir / "TPx_2026-08-27_09-00-00.csv"
        # Mirrors the real one's TPxSetBuff + TPxEnableFreeRun.
        self.libdpx.buffer_base = self.buffer_base_addr
        self.libdpx.freerun = True
        self.last_read_addr = self.buffer_base_addr
        return str(self.samples_file)

    def saveBufferedData(self) -> None:  # noqa: N802 - vendor's name
        # The real one prints its note and appends the device's newly-
        # buffered samples; this one appends a line per drain so a test can
        # count them in the file.
        self._answer()
        print(self.chatter)
        self.drains += 1
        assert self.samples_file is not None
        with self.samples_file.open("a") as f:
            f.write(f"drain {self.drains}\n")

    def isDeviceCalibrated(self) -> bool:  # noqa: N802 - vendor's name
        if self.finished_calibration:
            return self.calibrated_after_finish
        return self.holds_calibration

    def getTime(self) -> float:  # noqa: N802 - vendor's name
        self._answer()
        return self.device_time

    def getEyePositionDuringCalib(self, x, y, eye):  # noqa: N802 - vendor's name
        self.calibration_points.append((x, y, eye))

    def finishCalibration(self) -> None:  # noqa: N802 - vendor's name
        self.finished_calibration = True


def install_fake_pypixxlib(monkeypatch: Any) -> FakeTrackPixx:
    """Put a pypixxlib whose TRACKPixx3 is one FakeTrackPixx into
    ``sys.modules`` for one test, and return that device.

    The backend imports it inside connect(), so patching sys.modules is
    enough — and monkeypatch removes the entries again, so no other test
    inherits a fake SDK.
    """
    device = FakeTrackPixx()
    tracker_module = types.ModuleType("pypixxlib.tracker")
    tracker_module.TRACKPixx3 = lambda: device  # type: ignore[attr-defined]
    package = types.ModuleType("pypixxlib")
    package.tracker = tracker_module  # type: ignore[attr-defined]
    package._libdpx = device.libdpx  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypixxlib", package)
    monkeypatch.setitem(sys.modules, "pypixxlib.tracker", tracker_module)
    return device
