"""The DATAPixx3 link that every VPixx device in a session shares.

libdpx — the C library under pypixxlib — is one piece of process-wide state:
one selected device, one register cache, one sticky error flag, one open
handle. The TRACKPixx3 tracker, the DATAPixx3 reward gate and DATAPixx3 sync
lines all live on the same DATAPixx3 and all call into that one library, so
three things have to be decided once, here, rather than by each backend:

- **Who may call it, when.** Every libdpx call in alhazen runs under
  :attr:`DpxLink.lock`. The library says nothing about threads, and its
  calls are sequences (write the register cache, then read it back) that
  another thread must not interleave. The tracker's gaze reader, the reward
  worker and the session thread's sync pulses all take this one lock.
- **Who opens and closes it.** ``DPxClose`` closes the device for every user
  in the process, so the first holder opens it and only the *last* holder
  closes it (:meth:`DpxLink.release` says which one that is). A tracker shut
  down before the reward gate no longer pulls the device out from under it.
- **Which DOUT bits belong to whom.** The digital-output register is one
  24-bit word; :meth:`DpxLink.reserve` records which bits a holder drives
  and refuses overlapping reservations. A holder that plays a DOUT
  *schedule* (the reward gate) owns the whole port while it is open: VPixx
  documents that a schedule writes 16-bit words onto DOUT 0-15 as a whole,
  and does not document whether DOUT 16-23 stay independent of it, so no
  other DOUT holder may share the port with it.
- **Which device is selected.** pypixxlib's own objects select other devices
  (TRACKPixx3() selects the TRACKPixx; a VIEWPixx may be on the same USB),
  so every locked operation starts with :meth:`DpxLink.select`. Selecting
  clears libdpx's sticky error (pypixxlib's DPxSelectDevice calls
  DPxClearError first), so a fault some other caller left unread is logged
  first rather than lost, and each caller reads its own fault before it
  releases the lock.

The decision this module hides: libdpx's global state and its lifetime.
Callers learn a lock, select, connect/release, reserve. The price is one
process-wide lock: a gaze read (2 ms typical, 20–40 ms worst measured on the
rig) can delay a reward start or a sync pulse by that much. What callers
must not rely on: anything about the selected device other than
``HOST_DEVICE`` — every alhazen user selects the DATAPixx3 and nothing
selects another device.

One link per loaded library (:func:`dpx_link`), keyed by the library object:
the rig has exactly one, and a test's stand-in library gets its own, so no
test inherits another's holders.
"""

from __future__ import annotations

import logging
import threading
import weakref
from typing import Any

from alhazen.config.models import DATAPIXX_DEVICE

log = logging.getLogger(__name__)

# libdpx addresses one device at a time (``DPxSelectDevice``). The TRACKPixx3's
# registers, the digital outputs and their schedule all live on the DATAPixx3
# that hosts the camera, so that is the device every call must find selected.
HOST_DEVICE = DATAPIXX_DEVICE

# How long a caller waits for the link before it calls the device stuck: the
# same bound, and the same reason, as the tracker's LOCK_TIMEOUT_S (a normal
# hold is one USB call; two seconds is a transfer to a device that went away).
LOCK_TIMEOUT_S = 2.0


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


# VPixx's Digital Out documentation: Pixel Mode B (DOUT 16-23 follow the top-
# left pixel's blue byte) "is only available on DATAPixx3 with firmware
# revision 27 and higher". Below it, the mode cannot be on.
PIXEL_MODE_B_FIRMWARE = 27

PYPIXXLIB_HELP = (
    "install the pypixxlib that VPixx's Software Tools installer ships for this DATAPixx3's "
    "firmware (docs/eye-tracker.md says how)"
)


def missing_calls(lib: Any, names: tuple[str, ...]) -> list[str]:
    """The libdpx calls in ``names`` this pypixxlib does not have."""
    return [name for name in names if not callable(getattr(lib, name, None))]


def pixel_mode_b(lib: Any) -> tuple[bool | None, str]:
    """Whether Pixel Mode B (DOUT 16-23 from the screen) is on, and how that
    is known: read from the device, or impossible on its firmware; None when
    neither can be said (a pypixxlib without DPxIsDoutPixelModeB on firmware
    that has the mode). Never assumed off because the call is missing. The
    caller holds the link's lock with the DATAPixx3 selected."""
    read = getattr(lib, "DPxIsDoutPixelModeB", None)
    if callable(read):
        return bool(read()), "read from the device"
    firmware = int(lib.DPxGetFirmwareRev())
    if firmware < PIXEL_MODE_B_FIRMWARE:
        return False, (
            f"impossible: firmware revision {firmware} < {PIXEL_MODE_B_FIRMWARE}, the first "
            f"with Pixel Mode B (VPixx Digital Out documentation)"
        )
    return None, (
        f"unknown: this pypixxlib has no DPxIsDoutPixelModeB and the firmware revision is "
        f"{firmware}, which has the mode"
    )


def load_libdpx() -> Any:
    """pypixxlib's libdpx wrapper, imported only when a VPixx device is used.

    pypixxlib ships with VPixx's Software Tools installer, not PyPI
    (docs/eye-tracker.md says how to install it); importing it at module
    level would break ``import alhazen`` on every machine that is not the rig.
    Raises ImportError for the caller to turn into its own device error.
    """
    from pypixxlib import _libdpx

    return _libdpx


class DpxLink:
    """libdpx's process-wide state, shared by every alhazen VPixx device.

    Not constructed directly: :func:`dpx_link` hands out the one link for a
    loaded library.
    """

    def __init__(self, libdpx: Any) -> None:
        self.libdpx = libdpx
        self.lock = threading.Lock()
        # Every holder, by identity. A holder is the device object itself.
        self._holders: list[object] = []
        self._opened = False
        # True when this link's own DPxOpen opened the device (connect), not
        # a vendor object's constructor (adopt): a pypixxlib TRACKPixx3()
        # built on top would open it a second time.
        self._opened_by_link = False
        # holder id -> (holder, mask of the DOUT bits it drives, whether it
        # plays DOUT schedules and so owns the whole port).
        self._reserved: dict[int, tuple[object, int, bool]] = {}

    # ------------------------------------------------------------------
    # Lifetime. Every method below expects the caller to hold ``lock``.
    # ------------------------------------------------------------------

    @property
    def opened(self) -> bool:
        return self._opened

    def connect(self, holder: object) -> None:
        """Open the device for ``holder``: ``DPxOpen`` only if no holder has
        opened it, then select the DATAPixx3 and check that it answered.

        Raises RuntimeError naming what failed; the caller turns it into its
        own device error. The caller holds ``lock``.
        """
        libdpx = self.libdpx
        opened_here = False
        if not self._opened:
            libdpx.DPxOpen()
            fault = dpx_fault(libdpx)
            if fault is not None:
                raise RuntimeError(f"DPxOpen failed: {fault}")
            self._opened = opened_here = True
            self._opened_by_link = True
        try:
            self.select()
        except RuntimeError:
            # Opened here and nobody holds it: close it again, so no open is
            # left behind with no holder to close it.
            if opened_here and not self._holders:
                self._opened = self._opened_by_link = False
                libdpx.DPxClose()
            raise
        self.hold(holder)

    def select(self) -> None:
        """Select the DATAPixx3 for the operation about to run; raise
        RuntimeError when it does not answer. The caller holds ``lock``.

        A sticky fault already pending was left by a caller that did not
        read it; DPxSelectDevice would clear it silently, so it is logged
        here first.
        """
        libdpx = self.libdpx
        pending = dpx_fault(libdpx)
        if pending is not None:
            log.warning(
                "a libdpx fault was pending before a DATAPixx3 operation, left unread by an "
                "earlier call: %s",
                pending,
            )
        if not libdpx.DPxSelectDevice(HOST_DEVICE):
            fault = dpx_fault(libdpx)
            raise RuntimeError(
                f"no {HOST_DEVICE} answered DPxSelectDevice"
                + (f" ({fault})" if fault is not None else "")
                + ": is it powered, connected by USB, and free of other VPixx software?"
            )

    def adopt(self, holder: object) -> None:
        """Register ``holder`` for a device a vendor object already opened.

        pypixxlib's TRACKPixx3 calls ``DPxOpen`` in its own constructor, so
        the tracker joins the link as a holder of an open device rather than
        opening it here. The caller holds ``lock``.
        """
        self._opened = True
        self.hold(holder)

    def hold(self, holder: object) -> None:
        if not any(existing is holder for existing in self._holders):
            self._holders.append(holder)

    def release(self, holder: object) -> bool:
        """Drop ``holder`` and its DOUT reservation; True when it was the
        last holder, which is then the one that must close the device.

        A holder that was never registered gets False: it opened nothing
        through this link and has nothing to close for anyone else. The
        caller holds ``lock``.
        """
        self._reserved.pop(id(holder), None)
        before = len(self._holders)
        self._holders = [existing for existing in self._holders if existing is not holder]
        if len(self._holders) == before:
            return False
        if self._holders:
            return False
        self._opened = self._opened_by_link = False
        return True

    def holders(self) -> int:
        return len(self._holders)

    def opened_by_link_for_others(self, holder: object) -> bool:
        """Whether this link's own DPxOpen opened the device and someone
        other than ``holder`` still holds it — the state in which a vendor
        constructor that calls DPxOpen again must not run. The caller holds
        ``lock``."""
        return (
            self._opened
            and self._opened_by_link
            and any(existing is not holder for existing in self._holders)
        )

    # ------------------------------------------------------------------
    # DOUT bit ownership
    # ------------------------------------------------------------------

    def reserve(self, holder: object, mask: int, *, schedules: bool = False) -> None:
        """Record that ``holder`` drives the DOUT bits in ``mask``.

        Refuses (ValueError) bits another holder already drives — two owners
        of one line is a wiring mistake no timing can make safe — and any
        sharing of the port with a holder that plays DOUT schedules
        (``schedules``), whichever of the two came first. The caller holds
        ``lock``."""
        for key, (other, other_mask, other_schedules) in self._reserved.items():
            if key == id(holder):
                continue
            if schedules or other_schedules:
                raise ValueError(
                    f"the {HOST_DEVICE}'s digital outputs are already used by "
                    f"{type(other).__name__} (DOUT {_bits(other_mask)}), and a reward gate's "
                    f"DOUT schedule cannot share the port: it writes DOUT 0-15 as a whole "
                    f"while it plays, and VPixx does not document whether DOUT 16-23 stay "
                    f"independent of it"
                )
            if other_mask & mask:
                raise ValueError(
                    f"DOUT bits {_bits(other_mask & mask)} are already driven by "
                    f"{type(other).__name__}; one line cannot have two owners"
                )
        self._reserved[id(holder)] = (holder, mask, schedules)

    def acquire(self, doing: str, error: type[Exception]) -> None:
        """Take ``lock`` within LOCK_TIMEOUT_S or raise ``error`` saying what
        was waiting: a libdpx call that never returns holds it for ever."""
        if not self.lock.acquire(timeout=LOCK_TIMEOUT_S):
            raise error(
                f"the {HOST_DEVICE} did not answer within {LOCK_TIMEOUT_S:g} s while {doing}: "
                f"another libdpx call is stuck (the USB link or the device stopped responding)"
            )


def _bits(mask: int) -> str:
    return ", ".join(str(bit) for bit in range(24) if mask >> bit & 1) or "none"


_LINKS: weakref.WeakKeyDictionary[Any, DpxLink] = weakref.WeakKeyDictionary()
_LINKS_LOCK = threading.Lock()


def dpx_link(libdpx: Any) -> DpxLink:
    """The one link for this loaded libdpx, created on first use."""
    with _LINKS_LOCK:
        link = _LINKS.get(libdpx)
        if link is None:
            link = DpxLink(libdpx)
            _LINKS[libdpx] = link
        return link


def dout_bits(mask: int) -> str:
    """The bit numbers in ``mask``, for messages ("0, 17")."""
    return _bits(mask)
