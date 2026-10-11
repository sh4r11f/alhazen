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


def per_eye(*, right: tuple[float, float], left: tuple[float, float]) -> list[float]:
    """A four-value per-eye array the way the TRACKPixx3 hands one back: the
    RIGHT eye's pair first, then the LEFT's — gaze, raw eye vectors, pupil
    sizes, calibration raw vectors alike (the evidence is at
    devices/eyetracker/viewpixx.py, NATIVE_EYE_ORDER). Written out here
    rather than taken from that constant, so the tests hold the backend to
    the device's order rather than to itself.
    """
    return [*right, *left]


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
        # Every per-eye array below is in the device's own order, right eye
        # first (per_eye). Pupil ellipse semi-axes (right major/minor, left
        # major/minor); all zero is the device's "no eye in the image".
        self.pupils: list[float] = per_eye(right=(3.0, 2.0), left=(3.0, 2.0))
        # Each pupil's centre in the camera image: camera px from the image's
        # top-left corner, x to the right and y down. Inside the default
        # 32 x 24 image below.
        self.pupil_centres: list[float] = per_eye(right=(20.0, 12.0), left=(8.0, 10.0))
        # The gaze report TPxBestPolyGetEyePosition writes: calibrated
        # positions and raw eye vectors, [x_right, y_right, x_left, y_left].
        # Raw vectors of a tracked eye are plain numbers; the device's own
        # buffers start at zero, which the backend reads as "not measured".
        self.positions: list[float] = per_eye(right=(0.0, 0.0), left=(0.0, 0.0))
        self.raw_positions: list[float] = per_eye(right=(1.5, -0.5), left=(1.4, -0.4))
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
        self._init_dout()
        self._init_dac()

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
        self.buffer_size = size
        self.arms += 1

    def TPxEnableFreeRun(self) -> None:  # noqa: N802 - vendor's name
        self.freerun = True

    def TPxDisableFreeRun(self) -> None:  # noqa: N802 - vendor's name
        self.freerun = False

    def TPxIsFreeRun(self) -> int:  # noqa: N802 - vendor's name
        return 1048576 if self.freerun else 0

    def TPxGetBuffBaseAddr(self) -> int:  # noqa: N802 - vendor's name
        return self.buffer_base

    def TPxGetPupilSize(self) -> list[float]:  # noqa: N802 - vendor's name
        return list(self.pupils)

    def TPxGetPupilCoordinatesInPixels(self) -> list[float]:  # noqa: N802 - vendor's name
        return list(self.pupil_centres)

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

    def DPxSelectDevice(self, name: str) -> int:  # noqa: N802 - vendor's name
        # As pypixxlib's _libdpx.DPxSelectDevice: it calls DPxClearError()
        # first ("work around where a selectdevice breaks the table"), so a
        # sticky error nobody read is gone after it; then libdpx answers
        # non-zero when the device exists.
        self.DPxClearError()
        self.selected = name
        return 1 if name in self.present else 0

    def TPxHideOverlay(self) -> None:  # noqa: N802 - vendor's name
        self.overlay_hidden = True

    def DPxSetTPxAwake(self) -> None:  # noqa: N802 - vendor's name
        self.awake = True

    def DPxUpdateRegCache(self) -> None:  # noqa: N802 - vendor's name
        self.cache_updates += 1
        if self._transfer_failed():
            return
        if "DPxUpdateRegCache" in self.fault_on or "DPxUpdateRegCache" in self.raise_on:
            self._call("DPxUpdateRegCache")
        self._advance_dout()
        self._advance_dac()
        if not self.dout_running:
            last = self.port_history[-1] if self.port_history else None
            if last != ("register", self.dout_register):
                self.port_history.append(("register", self.dout_register))

    def DPxGetError(self) -> str:  # noqa: N802 - vendor's name
        return self.error

    def DPxGetErrorString(self) -> str:  # noqa: N802 - vendor's name
        return self.error_string

    def DPxClearError(self) -> None:  # noqa: N802 - vendor's name
        self.error = "DPX_SUCCESS"

    # -----------------------------------------------------------------------
    # The DATAPixx3's digital outputs (reward gate, sync lines) and the rest
    # of libdpx's open/close. A model of what alhazen's DOUT code assumes,
    # written from VPixx's documentation (devices/reward.py cites it), not a
    # recording: a schedule plays 16-bit words onto DOUT 0-15 and the port
    # returns to the register value when it ends; DPxGetDoutValue reads the
    # register; a started schedule begins at the next register write.
    # -----------------------------------------------------------------------

    def _init_dout(self) -> None:
        self.present = {"DATAPIXX3", "TRACKPIXX"}
        self.opens = 0
        self.closes = 0
        self.buffer_size = 0
        self.ram_size = 1 << 31
        # What DPxSetDoutValue latched (24 bits), every write as (value, mask),
        # and the port's story in order: ("register", value) whenever the
        # register is written or a schedule ends, ("train", words) when one
        # starts playing.
        self.dout_register = 0
        self.dout_writes: list[tuple[int, int]] = []
        self.port_history: list[tuple[str, Any]] = []
        # DATAPixx3 RAM, one 16-bit word per even byte address; what was never
        # written reads back as 0 (a real device's RAM holds whatever was
        # there before, which is no better).
        self.ram: dict[int, int] = {}
        self.ram_write_calls: list[int] = []  # words handed to each DPxWriteRam
        self.firmware_rev = 27
        self.dout_buff: tuple[int, int] = (0, 0)
        self.dout_sched: tuple[int, int, str, int] | None = None
        self.dout_running = False
        self.dout_start_pending = False
        self.dout_stops = 0
        self.trains: list[list[int]] = []
        # How many register round trips a started schedule reports running
        # for before it ends; None: it never ends by itself (a stuck one).
        self.polls_until_done: int | None = 1
        # None until a schedule is started here: a schedule some other program
        # left running (dout_running set by a test) never ends by itself.
        self._polls_left: int | None = None
        self.pixel_mode = False
        self.pixel_mode_gb = False
        self.pixel_mode_b = False
        self.button_schedules = False
        self.backlight_pulse = False
        # function name -> sticky error code that call leaves (fault injection)
        self.fault_on: dict[str, str] = {}
        # function name -> exception that call raises (a wrapper that throws)
        self.raise_on: dict[str, BaseException] = {}
        self.tracker_close_steps: list[str] = []

    def _call(self, name: str) -> None:
        if name in self.raise_on:
            raise self.raise_on[name]
        if name in self.fault_on:
            self.error = self.fault_on[name]
            self.error_string = f"{name} failed"
        self._transfer_failed()

    def port(self) -> int:
        """What DOUT 0-23 show now: the register, unless a schedule is
        playing (then its last-loaded train's words own DOUT 0-15)."""
        return self.dout_register

    def gate_trains(self, bit: int) -> list[list[int]]:
        """Each played train as the levels of one bit, sample by sample."""
        return [[(word >> bit) & 1 for word in train] for train in self.trains]

    def DPxOpen(self) -> None:  # noqa: N802 - vendor's name
        self.opens += 1
        self._call("DPxOpen")

    def DPxClose(self) -> None:  # noqa: N802 - vendor's name
        self.closes += 1
        self._call("DPxClose")

    def DPxIsDoutPixelMode(self) -> int:  # noqa: N802 - vendor's name
        self._call("DPxIsDoutPixelMode")
        return int(self.pixel_mode)

    def DPxIsDoutPixelModeGB(self) -> int:  # noqa: N802 - vendor's name
        return int(self.pixel_mode_gb)

    def DPxIsDoutPixelModeB(self) -> int:  # noqa: N802 - vendor's name
        return int(self.pixel_mode_b)

    def DPxIsDoutButtonSchedules(self) -> int:  # noqa: N802 - vendor's name
        return int(self.button_schedules)

    def DPxIsDoutBacklightPulse(self) -> int:  # noqa: N802 - vendor's name
        return int(self.backlight_pulse)

    def DPxIsDoutSchedRunning(self) -> int:  # noqa: N802 - vendor's name
        self._call("DPxIsDoutSchedRunning")
        return int(self.dout_running)

    def DPxGetDoutValue(self) -> int:  # noqa: N802 - vendor's name
        self._call("DPxGetDoutValue")
        return self.dout_register

    def DPxSetDoutValue(self, value: int, mask: int) -> None:  # noqa: N802 - vendor's name
        self._call("DPxSetDoutValue")
        self.dout_writes.append((value, mask))
        self.dout_register = (self.dout_register & ~mask) | (value & mask)

    def DPxGetRamSize(self) -> int:  # noqa: N802 - vendor's name
        return self.ram_size

    def TPxGetBuffSize(self) -> int:  # noqa: N802 - vendor's name
        return self.buffer_size

    def DPxWriteRam(self, address: int, words: Any) -> None:  # noqa: N802 - vendor's name
        """As pypixxlib's _libdpx.DPxWriteRam: the byte count goes to the C
        call as ``ctypes.c_ushort(2 * len(words))``, so only that many bytes
        reach the device — 32,768 words become 0 bytes, 40,002 become 7,234
        words — with no error. VPixx's own DPxWriteDoutBuffer never hands it
        more than 0xFFF0 // 4 = 16,380 words at a time."""
        self._call("DPxWriteRam")
        words = [int(word) for word in words]
        self.ram_write_calls.append(len(words))
        assert address % 2 == 0, "VPixx RAM addresses are even"
        assert all(0 <= word <= 0xFFFF for word in words), "DOUT words are 16-bit"
        transferred = ctypes.c_ushort(2 * len(words)).value // 2
        for index, word in enumerate(words[:transferred]):
            self.ram[address + 2 * index] = word

    def DPxReadRam(self, address: int, length: int) -> np.ndarray:  # noqa: N802 - vendor's name
        """As pypixxlib's _libdpx.DPxReadRam: ``length`` is in WORDS (its
        byte count is a c_uint, so no 16-bit wrap), and it returns a numpy
        array of unsigned 16-bit words."""
        self._call("DPxReadRam")
        assert address % 2 == 0, "VPixx RAM addresses are even"
        return np.array(
            [self.ram.get(address + 2 * index, 0) for index in range(int(length))],
            dtype=np.uint16,
        )

    def DPxGetFirmwareRev(self) -> int:  # noqa: N802 - vendor's name
        return self.firmware_rev

    def DPxSetDoutBuff(self, address: int, size: int) -> None:  # noqa: N802 - vendor's name
        self._call("DPxSetDoutBuff")
        self.dout_buff = (address, size)

    def DPxSetDoutSched(self, onset: int, rate: int, units: str, count: int) -> None:  # noqa: N802
        self._call("DPxSetDoutSched")
        self.dout_sched = (onset, rate, units, count)

    def DPxStartDoutSched(self) -> None:  # noqa: N802 - vendor's name
        self._call("DPxStartDoutSched")
        self.dout_start_pending = True

    def DPxStopDoutSched(self) -> None:  # noqa: N802 - vendor's name
        self._call("DPxStopDoutSched")
        self.dout_stops += 1
        self.dout_start_pending = False
        if self.dout_running:
            self.dout_running = False
            self.port_history.append(("register", self.dout_register))

    def _advance_dout(self) -> None:
        """One register round trip: a pending start begins playing, a
        playing schedule counts down towards its end."""
        if self.dout_start_pending:
            self.dout_start_pending = False
            address, size = self.dout_buff
            count = self.dout_sched[3] if self.dout_sched is not None else 0
            length = min(size // 2, count)
            words = [self.ram.get(address + 2 * index, 0) for index in range(length)]
            self.trains.append(words)
            self.port_history.append(("train", words))
            self.dout_running = True
            self._polls_left = self.polls_until_done
            return
        if self.dout_running and self._polls_left is not None:
            self._polls_left -= 1
            if self._polls_left <= 0:
                self.dout_running = False
                self.port_history.append(("register", self.dout_register))

    # -----------------------------------------------------------------------
    # The DATAPixx3's analog outputs (DAC0-3 on the Analog I/O port), as
    # pypixxlib's _libdpx publishes them:
    # - DPxSetDacValue(value, channel) / DPxGetDacValue(channel): the static
    #   16-bit two's-complement code (c_int, c_int; returns a signed int);
    # - DPxSetDacVoltage(voltage, channel) converts with the channel's range;
    # - DPxGetDacRange(channel) -> (min, max) volts (DATAPixx docs: ±10 V on
    #   ch0/1, ±5 V on ch2/3; the DATAPixx3 page: ±10 V on all four);
    # - DPxSetDacSched(onset, rate, units, count): argtypes c_uint, c_uint,
    #   c_int, c_uint, so a float onset raises ctypes.ArgumentError (what
    #   DPxSetDacSchedule does with its seconds onset); count > 0 counts down;
    # - DPxSetDacBuff(address, bytes); one frame = one word per buffered
    #   channel, interleaved;
    # - TPxGetAllDACOutput(): one TRACKPixx3 analog-output mode byte per DAC
    #   (0 = schedule-controlled). As VPixx publishes it, the wrapper
    #   declares an argument the C call does not take and raises TypeError
    #   (dac_tpx_getter_broken models that).
    # Not documented, modelled conservatively: a schedule that ends or is
    # stopped leaves the pin at the sample it was on (no return to the
    # static value); a start begins at the next register write.
    # -----------------------------------------------------------------------

    def _init_dac(self) -> None:
        self.dac_range: dict[int, tuple[float, float]] = {c: (-10.0, 10.0) for c in range(4)}
        self.dac_static = [0, 0, 0, 0]  # unsigned 16-bit codes
        self.dac_pin = [0, 0, 0, 0]  # what each pin is driven to, as a code
        self.dac_static_writes: list[tuple[int, int]] = []  # (code, channel)
        self.dac_buffered: set[int] = set()
        self.dac_buff: tuple[int, int] = (0, 0)
        self.dac_sched: tuple[int, int, str, int] | None = None
        self.dac_running = False
        self.dac_start_pending = False
        self.dac_stops = 0
        self.dac_trains: list[tuple[tuple[int, ...], list[int]]] = []
        self.dac_loopback = False
        self.dac_calib_raw = False
        self.tpx_dac_mode = 0
        self.dac_tpx_getter_broken = False
        self._dac_train: list[int] = []
        self._dac_polls_left: int | None = None

    def DPxGetDacNumChans(self) -> int:  # noqa: N802 - vendor's name
        return 4

    def DPxGetDacRange(self, channel: int) -> tuple[float, float]:  # noqa: N802
        return self.dac_range[int(channel)]

    def DPxSetDacValue(self, value: int, channel: int) -> None:  # noqa: N802 - vendor's name
        self._call("DPxSetDacValue")
        assert isinstance(value, int) and isinstance(channel, int), "(value, channel) ints"
        assert -32768 <= value <= 65535, "a 16-bit DAC value"
        assert 0 <= channel < 4, f"no DAC channel {channel}"
        code = value & 0xFFFF
        self.dac_static[channel] = code
        self.dac_pin[channel] = code
        self.dac_static_writes.append((code, channel))

    def DPxGetDacValue(self, channel: int) -> int:  # noqa: N802 - vendor's name
        code = self.dac_static[int(channel)]
        return code - 0x10000 if code >= 0x8000 else code  # c_int: signed

    def DPxSetDacVoltage(self, voltage: float, channel: int) -> None:  # noqa: N802
        low, high = self.dac_range[int(channel)]
        code = math.floor((voltage - low) / (high - low) * 65536 - 32768 + 0.5)
        self.DPxSetDacValue(max(-32768, min(32767, code)), int(channel))

    def DPxGetDacVoltage(self, channel: int) -> float:  # noqa: N802 - vendor's name
        low, high = self.dac_range[int(channel)]
        signed = self.DPxGetDacValue(channel)
        return (signed + 32768) / 65536 * (high - low) + low

    def DPxIsDacBuffChan(self, channel: int) -> int:  # noqa: N802 - vendor's name
        return int(int(channel) in self.dac_buffered)

    def DPxEnableDacBuffChan(self, channel: int) -> None:  # noqa: N802 - vendor's name
        self._call("DPxEnableDacBuffChan")
        self.dac_buffered.add(int(channel))

    def DPxDisableDacBuffChan(self, channel: int) -> None:  # noqa: N802 - vendor's name
        self.dac_buffered.discard(int(channel))

    def DPxDisableDacBuffAllChans(self) -> None:  # noqa: N802 - vendor's name
        self.dac_buffered.clear()

    def DPxSetDacBuff(self, address: int, size: int) -> None:  # noqa: N802 - vendor's name
        self._call("DPxSetDacBuff")
        self.dac_buff = (int(address), size)

    def DPxSetDacSched(self, onset: int, rate: int, units: str, count: int) -> None:  # noqa: N802
        self._call("DPxSetDacSched")
        for name, value in (("onset", onset), ("rate", rate), ("count", count)):
            if not isinstance(value, int):
                # argtypes c_uint: a float (DPxSetDacSchedule's seconds onset)
                raise ctypes.ArgumentError(f"argument {name}: wrong type")
        self.dac_sched = (onset, rate, units, count)

    def DPxStartDacSched(self) -> None:  # noqa: N802 - vendor's name
        self._call("DPxStartDacSched")
        self.dac_start_pending = True

    def DPxStopDacSched(self) -> None:  # noqa: N802 - vendor's name
        self._call("DPxStopDacSched")
        self.dac_stops += 1
        self.dac_start_pending = False
        if self.dac_running:
            # Stopped mid-train: the pin stays where the train was (here,
            # half way through it).
            self.dac_running = False
            channel = next(iter(sorted(self.dac_buffered)), 0)
            if self._dac_train:
                self.dac_pin[channel] = self._dac_train[len(self._dac_train) // 2]

    def DPxIsDacSchedRunning(self) -> int:  # noqa: N802 - vendor's name
        return int(self.dac_running)

    def DPxIsDacAdcLoopback(self) -> int:  # noqa: N802 - vendor's name
        return int(self.dac_loopback)

    def DPxIsDacCalibRaw(self) -> int:  # noqa: N802 - vendor's name
        return int(self.dac_calib_raw)

    def TPxGetAllDACOutput(self) -> int:  # noqa: N802 - vendor's name
        if self.dac_tpx_getter_broken:
            raise TypeError("this function takes at least 1 argument (0 given)")
        return self.tpx_dac_mode

    def _advance_dac(self) -> None:
        if self.dac_start_pending:
            self.dac_start_pending = False
            address, size = self.dac_buff
            count = self.dac_sched[3] if self.dac_sched is not None else 0
            channels = tuple(sorted(self.dac_buffered))
            frames = min(size // (2 * max(len(channels), 1)), count)
            words = [
                self.ram.get(address + 2 * index, 0)
                for index in range(frames * max(len(channels), 1))
            ]
            self.dac_trains.append((channels, words))
            self._dac_train = words
            self.dac_running = True
            self._dac_polls_left = self.polls_until_done
            return
        if self.dac_running and self._dac_polls_left is not None:
            self._dac_polls_left -= 1
            if self._dac_polls_left <= 0:
                self.dac_running = False
                # Finished: the pin holds the last sample played.
                channels = sorted(self.dac_buffered)
                if channels and self._dac_train:
                    self.dac_pin[channels[0]] = self._dac_train[-1]

    def TPxUninitialize(self) -> None:  # noqa: N802 - vendor's name
        self.tracker_close_steps.append("TPxUninitialize")

    def TPxSetSleepPictureRequest(self) -> None:  # noqa: N802 - vendor's name
        self.tracker_close_steps.append("TPxSetSleepPictureRequest")


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
        # As pypixxlib's TRACKPixx3.close(): TPxUninitialize,
        # TPxSetSleepPictureRequest, DPxUpdateRegCache, then DpxDevice.close
        # -> DPxClose for the whole process, then DPxClearError.
        try:
            self.libdpx.TPxUninitialize()
            self.libdpx.TPxSetSleepPictureRequest()
            self.libdpx.DPxUpdateRegCache()
            self.libdpx.DPxClose()
            self.closed = True
        finally:
            self.libdpx.DPxClearError()

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
        self.libdpx.buffer_size = self.buffer_size
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

    def construct() -> FakeTrackPixx:
        # As pypixxlib: TRACKPixx3() runs DpxDevice.__init__, which calls
        # DPxOpen and then DPxSelectDevice('TRACKPixx').
        device.libdpx.DPxOpen()
        device.libdpx.DPxSelectDevice("TRACKPIXX")
        return device

    tracker_module.TRACKPixx3 = construct  # type: ignore[attr-defined]
    package = types.ModuleType("pypixxlib")
    package.tracker = tracker_module  # type: ignore[attr-defined]
    package._libdpx = device.libdpx  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypixxlib", package)
    monkeypatch.setitem(sys.modules, "pypixxlib.tracker", tracker_module)
    return device


def install_fake_libdpx(monkeypatch: Any) -> FakeLibdpx:
    """A pypixxlib with only its libdpx wrapper, for a DATAPixx3 reward gate
    or sync line with no tracker; returns that libdpx. With a tracker too,
    use install_fake_pypixxlib and its ``device.libdpx``: one library, one
    link (devices/vpixx.py)."""
    libdpx = FakeLibdpx()
    package = types.ModuleType("pypixxlib")
    package._libdpx = libdpx  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypixxlib", package)
    return libdpx
