"""TTL sync outputs: digital pulses that make an external recording alignable.

A pulse on a digital line, recorded by whatever records the neural/physio
data, is the only thing that ties a task event to a sample index in that
recording. A pulse that never fires cannot be reconstructed afterwards — so
nothing here fails quietly.

The division of labour: this module knows about *lines*, and
:func:`make_sync_subscriber` is the single place that knows which event maps
to which line. The rig config's ``event_lines`` is the source of truth for
what reaches hardware at all; an event with no entry pulses nothing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from alhazen.config.models import SyncHwConfig, datapixx_sync_bit
from alhazen.core.events import Event
from alhazen.devices.vpixx import HOST_DEVICE, dout_bits, dpx_fault, dpx_link, load_libdpx
from alhazen.errors import SyncError

log = logging.getLogger(__name__)


@runtime_checkable
class SyncOutput(Protocol):
    def pulse(self, line: str) -> None: ...

    def close(self) -> None: ...


class SimulatedSync:
    """Records pulses instead of driving lines.

    ``pulses`` is the ordered list of lines pulsed. Their times are not
    recorded here on purpose: the event that triggered each pulse is already
    in ``events.csv``, stamped on the same session clock, so a second copy
    could only ever disagree with it.
    """

    def __init__(self, event_lines: dict[str, str]) -> None:
        self._lines = set(event_lines.values())
        self.pulses: list[str] = []

    def pulse(self, line: str) -> None:
        if line not in self._lines:
            raise SyncError(
                f"no sync line {line!r} was configured; configured lines: {sorted(self._lines)}"
            )
        self.pulses.append(line)

    def close(self) -> None:
        return  # nothing was opened


class NullSync:
    """Sync turned off: every pulse is accepted and does nothing.

    Not a SimulatedSync with an empty map. That was the old implementation,
    and its ``pulse()`` *raises* on an unconfigured line — so a perfectly
    valid rig config (keep ``event_lines``, set ``backend: none`` because
    today's session has no recording attached) built successfully and then
    died on the first mapped event of trial 1.

    Nothing is recorded either, so there is no ``pulses`` list a test or a
    reader could mistake for evidence that a line fired.
    """

    def pulse(self, line: str) -> None:
        log.debug("sync disabled; not pulsing %s", line)

    def close(self) -> None:
        return  # nothing was opened


class NidaqSync:
    """One digital-output task per configured line, pulsed on demand."""

    def __init__(self, cfg: SyncHwConfig) -> None:
        try:
            # Lazy for the same reason as the reward backend: nidaqmx is
            # rig-only, and `import alhazen` must work without it.
            import nidaqmx
            from nidaqmx.constants import LineGrouping
        except ImportError as e:
            # Loud, never a silent no-op: a neurally-recorded session with no
            # alignment pulses is discovered only when someone tries to align
            # it, by which point the data cannot be rescued.
            raise SyncError(
                "nidaqmx is not installed — install alhazen's [nidaq] extra on the rig, or "
                "use sync backend 'simulated' / 'none'"
            ) from e

        self._pulse_s = cfg.pulse_ms / 1000.0
        self._tasks: dict[str, Any] = {}
        line = ""
        try:
            for line in sorted(set(cfg.event_lines.values())):
                task = nidaqmx.Task()
                # Registered BEFORE it is configured: add_do_chan/write below
                # can raise (a typo'd line name, a line the device does not
                # have), and a task created but never recorded here could
                # never be released by close() — a leaked NI handle holds the
                # line until process exit and blocks the next session.
                self._tasks[line] = task
                task.do_channels.add_do_chan(line, line_grouping=LineGrouping.CHAN_PER_LINE)
                # Drive low at setup: a line left high at task creation looks
                # like a spurious pulse to the recording system before the
                # session has even started.
                task.write(False)
        except nidaqmx.DaqError as e:
            self.close()  # release everything opened so far, including this one
            raise SyncError(f"sync line setup failed for line {line!r}: {e}") from e

    def pulse(self, line: str) -> None:
        task = self._tasks.get(line)
        if task is None:
            raise SyncError(
                f"no sync line {line!r} was configured; configured lines: {sorted(self._tasks)}"
            )
        import nidaqmx

        try:
            task.write(True)
            time.sleep(self._pulse_s)
            task.write(False)
        except nidaqmx.DaqError as e:
            raise SyncError(f"sync pulse on line {line!r} failed: {e}") from e

    def close(self) -> None:
        """Release every task, independently and idempotently.

        Called from two places where an escaping exception would do harm:
        this class's own constructor while a SyncError is already in flight,
        and session teardown, which may be unwinding something more important.
        So a failure on one line is logged and the rest are still released.
        """
        import nidaqmx

        for line, task in self._tasks.items():
            try:
                # Zero before releasing, same rule as the reward waveform's
                # trailing zero: a task closed with its line high can latch a
                # stray pulse into the recording after the session ended.
                task.write(False)
            except nidaqmx.DaqError as e:
                log.error("sync: could not zero line %s before closing: %s", line, e)
            try:
                task.close()
            except nidaqmx.DaqError as e:
                log.error("sync: could not close the DAQ task for line %s: %s", line, e)
        self._tasks.clear()


class DatapixxSync:
    """Sync lines on a DATAPixx3's digital outputs, pulsed on demand.

    Active high, like NidaqSync: each line rests LOW and a pulse holds it
    HIGH for ``pulse_ms``, timed the same way (a register write, a sleep,
    a register write), so a pulse's width carries the host's scheduling
    jitter exactly as NidaqSync's does. Every write masks this backend's own
    bits only, under the DATAPixx3 link's lock with the DATAPixx3 selected
    first (devices/vpixx.py).

    Shared hardware and its limits, said rather than hidden:

    - Any other output mode on a sync bit is refused at setup, before the
      lines are touched: a Pixel Mode (the line would carry the stimulus),
      DOUT button schedules on DOUT 0-15, the backlight pulse on DOUT 15.
      alhazen never turns any of them off.
    - Not with a DATAPixx3 reward gate: its DOUT schedule writes DOUT 0-15
      as a whole while it plays and VPixx does not document whether DOUT
      16-23 stay independent of it, so the rig config refuses the pair and
      the link refuses it again at run time.
    - The link's lock is shared with a TRACKPixx3's gaze reader: a pulse can
      start up to one gaze read late (2 ms typical, 20-40 ms worst measured
      on the rig) — measure it on the bench before aligning neural data to
      these lines rather than NI-DAQ ones.
    """

    def __init__(self, cfg: SyncHwConfig) -> None:
        bits = {line: datapixx_sync_bit(line) for line in sorted(set(cfg.event_lines.values()))}
        if any(bit is None for bit in bits.values()):
            raise SyncError(f"not DATAPixx3 sync lines: {sorted(bits)}")  # config validates this
        try:
            libdpx = load_libdpx()
        except ImportError as e:
            # Loud, never a silent no-op (NidaqSync says why).
            raise SyncError(
                "pypixxlib is not installed — it ships with VPixx's Software Tools installer, "
                "not PyPI (docs/eye-tracker.md); or use sync backend 'simulated' / 'none'"
            ) from e
        self._lib = libdpx
        self._link = dpx_link(libdpx)
        self._pulse_s = cfg.pulse_ms / 1000.0
        self._bits: dict[str, int] = {
            line: int(bit) for line, bit in bits.items() if bit is not None
        }
        self._mask = 0
        for bit in self._bits.values():
            self._mask |= 1 << bit
        self._closed = False
        self._link.acquire("opening the DATAPixx3 sync lines", SyncError)
        try:
            try:
                self._link.connect(self)
            except RuntimeError as e:
                raise SyncError(f"sync lines on the {HOST_DEVICE}: {e}") from e
            try:
                self._preflight()
                self._link.reserve(self, self._mask)
                # Drive low at setup (NidaqSync's rule: a line left high looks
                # like a spurious pulse to the recording system).
                self._write(0)
            except ValueError as e:
                self._release()
                raise SyncError(f"sync lines on the {HOST_DEVICE}: {e}") from e
            except BaseException:
                self._release()
                raise
        finally:
            self._link.lock.release()

    def pulse(self, line: str) -> None:
        bit = self._bits.get(line)
        if bit is None:
            raise SyncError(
                f"no sync line {line!r} was configured; configured lines: {sorted(self._bits)}"
            )
        if self._closed:
            raise SyncError(f"sync pulse on {line!r} after the sync lines were closed")
        self._locked_write(1 << bit, f"starting a sync pulse on {line}")
        time.sleep(self._pulse_s)
        self._locked_write(0, f"ending the sync pulse on {line}")

    def close(self) -> None:
        """Zero every line, then release the DATAPixx3 (closing it only as
        the link's last holder). Never raises: teardown may be unwinding
        something more important (NidaqSync.close)."""
        if self._closed:
            return
        self._closed = True
        if not self._link.lock.acquire(timeout=_LINK_TIMEOUT_S):
            log.error("sync: the %s did not answer; its sync lines were not zeroed", HOST_DEVICE)
            return
        try:
            try:
                self._write(0)  # selects the DATAPixx3 first
            except Exception as e:  # pypixxlib's exception type cannot be named off the rig
                log.error(
                    "sync: could not zero DOUT %s before closing: %s", dout_bits(self._mask), e
                )
            try:
                self._release()
            except Exception as e:  # pypixxlib's exception type cannot be named off the rig
                log.error("sync: could not close the %s: %s", HOST_DEVICE, e)
        finally:
            self._link.lock.release()

    # ------------------------------------------------------------------

    def _locked_write(self, value: int, doing: str) -> None:
        self._link.acquire(doing, SyncError)
        try:
            self._write(value)
        finally:
            self._link.lock.release()

    def _select(self) -> None:
        try:
            self._link.select()
        except RuntimeError as e:
            raise SyncError(f"sync lines on the {HOST_DEVICE}: {e}") from e

    def _write(self, value: int) -> None:
        """Select the DATAPixx3 and write this backend's bits — only them —
        as ``value``; raise SyncError on a device fault. The caller holds the
        lock."""
        self._select()
        self._lib.DPxSetDoutValue(value & self._mask, self._mask)
        self._lib.DPxUpdateRegCache()
        fault = dpx_fault(self._lib)
        if fault is not None:
            raise SyncError(f"sync write to DOUT {dout_bits(self._mask)} failed: {fault}")

    def _preflight(self) -> None:
        lib = self._lib
        self._select()
        lib.DPxUpdateRegCache()
        pixel = bool(lib.DPxIsDoutPixelMode())
        pixel_gb = bool(lib.DPxIsDoutPixelModeGB())
        read_b = getattr(lib, "DPxIsDoutPixelModeB", None)
        pixel_b = bool(read_b()) if read_b is not None else False
        buttons = bool(lib.DPxIsDoutButtonSchedules())
        backlight = bool(lib.DPxIsDoutBacklightPulse())
        fault = dpx_fault(lib)
        if fault is not None:
            raise SyncError(f"sync lines on the {HOST_DEVICE}: it reported {fault}")
        driven = 0
        if pixel:
            driven |= 0xFFFFFF
        if pixel_gb:
            driven |= 0xFFFF00
        if pixel_b:
            driven |= 0xFF0000
        if driven & self._mask:
            raise SyncError(
                f"sync lines on DOUT {dout_bits(driven & self._mask)} refused: a Pixel Mode "
                f"drives them from the screen's top-left pixel. alhazen does not turn it off; "
                f"move the sync lines or disable that Pixel Mode in your own setup"
            )
        if buttons and self._mask & 0xFFFF:
            raise SyncError(
                f"sync lines on DOUT {dout_bits(self._mask & 0xFFFF)} refused: DOUT button "
                f"schedules are on, and a button press plays a stored waveform onto DOUT 0-15"
            )
        if backlight and self._mask & (1 << 15):
            raise SyncError(
                "sync line on DOUT 15 refused: the backlight pulse is on and DOUT 15 gates the "
                "display backlight"
            )

    def _release(self) -> None:
        if self._link.release(self):
            self._lib.DPxClose()


# How long DatapixxSync waits for the DATAPixx3 link at close (devices/vpixx.py).
_LINK_TIMEOUT_S = 2.0


def make_sync_subscriber(sync: SyncOutput, event_lines: dict[str, str]) -> Callable[[Event], None]:
    """Bus subscriber that pulses the line an event is mapped to.

    Unmapped events are a silent no-op: the config decides which events reach
    hardware, so "no line for STIM_ON" is a rig statement, not a mistake.
    Pulse failures are *not* caught (invariant 6) — a dead sync line means
    every later alignment mark is suspect, which must stop the session.
    """
    lines = dict(event_lines)

    def on_event(event: Event) -> None:
        line = lines.get(event.name)
        if line is not None:
            sync.pulse(line)

    return on_event


def make_sync(cfg: SyncHwConfig) -> SyncOutput:
    """Construct the sync output a rig config names.

    ``none`` really is none: a no-op that accepts any line. A rig may keep its
    ``event_lines`` map and disable sync for a session with no recording
    attached, and that config has to run.
    """
    if cfg.backend == "nidaq":
        return NidaqSync(cfg)
    if cfg.backend == "datapixx":
        return DatapixxSync(cfg)
    if cfg.backend == "none":
        return NullSync()
    return SimulatedSync(cfg.event_lines)
