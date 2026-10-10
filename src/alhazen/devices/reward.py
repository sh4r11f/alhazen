"""Reward delivery: pure waveform builders plus simulated, NI-DAQ and
DATAPixx3 backends.

A rig drives its juice/water line one of two ways. An NI-DAQ analog output
drives a solenoid (or a pump's driver) with an active-high waveform:
:func:`build_reward_waveform`. A DATAPixx3 digital output drives a pump's
own TTL gate — a WPI SP220i runs while its Gate input is held LOW — with a
hardware-timed DOUT schedule: :func:`build_gate_words`. Both builders are
pure, so the timing that controls how much the subject receives is testable
on any machine, and the backends only play it out.

Reward *policy* (which outcome earns what) is not here: it lives in
``alhazen.task.reward_policy``, so a backend never decides what a trial
earned — it only plays out the pulse train it is handed.

:class:`QueuedReward` wraps a backend for a task that asks for reward while a
trial runs: it serialises every delivery onto one worker thread, so a
mid-trial drop never blocks the frame loop and never overlaps another
delivery on the valve. The experimenter's manual reward overrides the queue
(``QueuedReward.deliver_manual``): it cancels every drop still waiting and is
delivered once, as soon as the pulse train already on the valve finishes —
which is never cut short.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np

from alhazen.config.models import (
    DATAPIXX_DOUT_BUFFER_BYTES,
    DATAPIXX_SCHEDULE_BITS,
    TRACKPIXX_IMAGE_BASE,
    TRACKPIXX_IMAGE_SIZE,
    RewardHwConfig,
    RewardPulses,
    dout_bit_of,
)
from alhazen.core.trial import RewardCompletion, RewardRequest
from alhazen.devices.vpixx import (
    HOST_DEVICE,
    PYPIXXLIB_HELP,
    dpx_fault,
    dpx_link,
    load_libdpx,
    missing_calls,
    pixel_mode_b,
)
from alhazen.errors import RewardError, RewardUncertainError

log = logging.getLogger(__name__)

# The DAQ plays the buffer out on its own hardware clock at this rate, so
# pulse widths are exact regardless of Python-side scheduling jitter — and
# pulse width is exactly what sets the delivered volume.
SAMPLE_RATE_HZ = 1000


@runtime_checkable
class RewardDispenser(Protocol):
    def deliver(self, pulses: RewardPulses) -> None: ...

    def close(self) -> None: ...


def build_reward_waveform(
    voltage: float, pulses: RewardPulses, rate_hz: int = SAMPLE_RATE_HZ
) -> np.ndarray:
    """The analog-output buffer for one reward delivery, one sample per
    ``1/rate_hz`` seconds.

    Always ends at 0 V. A finite NI analog-output task *latches* its last
    written sample on the physical line after the task ends, so a waveform
    ending high (``inter_pulse_ms=0``, whose final sample belongs to the
    pulse) would leave the valve open indefinitely once delivery is "done".
    """
    if pulses.pulse_ms < 1 or pulses.n_pulses < 1:
        raise ValueError(
            f"cannot build a reward waveform from {pulses!r}: n_pulses and pulse_ms must be "
            f">= 1 (a zero-length or zero-count delivery never opens the valve)"
        )
    on = np.full(round(pulses.pulse_ms * rate_hz / 1000), float(voltage))
    off = np.zeros(round(pulses.inter_pulse_ms * rate_hz / 1000))
    waveform = np.concatenate([np.concatenate([on, off]) for _ in range(pulses.n_pulses)])
    if waveform[-1] != 0.0:
        waveform = np.append(waveform, 0.0)
    return waveform


# The longest train the DATAPixx3 gate plays: its schedule buffer
# (DATAPIXX_DOUT_BUFFER_BYTES from dout_buffer_address) holds this many
# one-millisecond words, a little over 8.7 minutes. Checked arithmetically,
# before any list is built, so a pulse count of any size costs nothing.
MAX_GATE_WORDS = DATAPIXX_DOUT_BUFFER_BYTES // 2


def gate_word_count(pulses: RewardPulses, rate_hz: int = SAMPLE_RATE_HZ) -> int:
    """How many schedule words :func:`build_gate_words` makes for ``pulses``,
    computed without building them."""
    on = round(pulses.pulse_ms * rate_hz / 1000)
    off = round(pulses.inter_pulse_ms * rate_hz / 1000)
    return 2 + pulses.n_pulses * (on + off)


def build_gate_words(
    pulses: RewardPulses,
    *,
    bit: int,
    polarity: Literal["active_high", "active_low"],
    port_value: int,
    rate_hz: int = SAMPLE_RATE_HZ,
) -> list[int]:
    """The DATAPixx3 DOUT schedule words for one reward train on ``bit``.

    One 16-bit word per ``1/rate_hz`` seconds. The device plays each word
    onto DOUT 0-15 *as a whole* for the length of the schedule (VPixx:
    DPxWriteDoutBuffer takes values 0-65535, one per sample), so every bit
    other than the gate carries ``port_value``'s level for that bit — the
    static register as read just before the train — and is left exactly as
    it was: the train never pulses, clears or resets another line.

    The words start with one word at rest, then hold the gate at its active
    level for ``pulse_ms`` and at rest for ``inter_pulse_ms``, ``n_pulses``
    times, and end with one more word at rest whatever came before it. VPixx
    documents that when a schedule ends the port returns to the static
    register "almost immediately", replacing the last word, and recommends
    one extra frame; the gate's static bit is held at rest (DatapixxReward),
    so the line rests after the train either way, and a train never ends on
    an active word (the reason build_reward_waveform ends at 0 V).

    A train longer than MAX_GATE_WORDS is refused (ValueError) before any
    word is made: never truncated.
    """
    if pulses.pulse_ms < 1 or pulses.n_pulses < 1:
        raise ValueError(
            f"cannot build reward gate words from {pulses!r}: n_pulses and pulse_ms must be "
            f">= 1 (a zero-length or zero-count delivery never opens the gate)"
        )
    if not 0 <= bit < DATAPIXX_SCHEDULE_BITS:
        raise ValueError(f"a DOUT schedule word carries bits 0-15, not bit {bit}")
    count = gate_word_count(pulses, rate_hz)
    if count > MAX_GATE_WORDS:
        raise ValueError(
            f"a reward train of {count} words ({count / rate_hz:g} s) is longer than the "
            f"{MAX_GATE_WORDS} words the gate's schedule buffer holds; it is refused, never "
            f"shortened"
        )
    mask = 1 << bit
    others = port_value & 0xFFFF & ~mask
    rest = others | (mask if polarity == "active_low" else 0)
    active = others | (0 if polarity == "active_low" else mask)
    on = round(pulses.pulse_ms * rate_hz / 1000)
    off = round(pulses.inter_pulse_ms * rate_hz / 1000)
    return [rest, *([active] * on + [rest] * off) * pulses.n_pulses, rest]


class SimulatedReward:
    """Records deliveries instead of touching hardware — dev machines, tests,
    and any dry run that must exercise trial timing without rewarding."""

    def __init__(self) -> None:
        self.deliveries: list[RewardPulses] = []

    def deliver(self, pulses: RewardPulses) -> None:
        self.deliveries.append(pulses)
        # INFO, not DEBUG: a reward is a session-meaningful thing that whoever
        # is watching the console during a dry run wants to see by default.
        log.info("simulated reward: %s", pulses)

    def close(self) -> None:
        return  # nothing was opened


class NidaqReward:
    """Delivers reward as a finite buffered analog-output waveform."""

    def __init__(self, cfg: RewardHwConfig) -> None:
        try:
            # Lazy: nidaqmx is a rig-only dependency, and importing it at
            # module level would break `import alhazen` everywhere else.
            import nidaqmx  # noqa: F401  (imported only to prove the SDK is present)
        except ImportError as e:
            # Loud at construction, never a silent no-op backend: a session
            # that runs to completion without ever rewarding the subject
            # produces unusable behavior and nobody notices until afterwards.
            raise RewardError(
                "nidaqmx is not installed — install alhazen's [nidaq] extra on the rig, or "
                "use reward backend 'simulated'"
            ) from e
        self._cfg = cfg

    def deliver(self, pulses: RewardPulses) -> None:
        if pulses.n_pulses == 0 or pulses.pulse_ms == 0:
            # A deliberately empty spec (a training stage that has not earned
            # juice yet) is a no-op, not an error — but it is logged, because
            # "the pump never fired" must never be silent.
            log.info("reward delivery skipped: %s delivers nothing", pulses)
            return

        import nidaqmx
        from nidaqmx.constants import AcquisitionType, VoltageUnits

        waveform = build_reward_waveform(self._cfg.voltage, pulses)
        channel = f"{self._cfg.device}/{self._cfg.channel}"
        try:
            with nidaqmx.Task() as task:
                task.ao_channels.add_ao_voltage_chan(
                    channel, min_val=0.0, max_val=10.0, units=VoltageUnits.VOLTS
                )
                task.timing.cfg_samp_clk_timing(
                    rate=SAMPLE_RATE_HZ,
                    sample_mode=AcquisitionType.FINITE,
                    samps_per_chan=len(waveform),
                )
                task.write(waveform.tolist(), auto_start=True)
                task.wait_until_done(timeout=10.0 + len(waveform) / SAMPLE_RATE_HZ)
                task.stop()
        except nidaqmx.DaqError as e:
            # Chained so the nidaqmx traceback survives, while callers still
            # only need one alhazen exception type to catch.
            raise RewardError(f"reward output failed on {channel}: {e}") from e

    def close(self) -> None:
        # Each delivery opens and closes its own task inside a `with`, so
        # there is nothing held between deliveries. Defined for symmetry with
        # NidaqSync, which does hold its tasks.
        return


# How often the reward worker asks the DATAPixx3 whether a train's schedule
# has finished, once the train's own length has nearly passed. Detection
# only: the gate's length is the hardware schedule's, never this.
_SCHEDULE_POLL_S = 0.002
# How long before a train's own end the polling starts.
_SCHEDULE_POLL_LEAD_S = 0.005
# How long past its own length a schedule may still report running before
# the train counts as stuck: far past any USB delay, short enough that a
# stuck gate is stopped by hand quickly.
_SCHEDULE_SLACK_S = 1.0
# The most words handed to one DPxWriteRam (or asked of one DPxReadRam):
# VPixx's own DPxWriteDoutBuffer never passes more than 0xFFF0 // 4 words per
# call ("maximum allowable size of data per call to DPxWriteRam()"), and
# pypixxlib's DPxWriteRam packs its byte count as a ctypes.c_ushort, so a
# longer list is silently cut (32,768 words become 0 bytes).
VENDOR_RAM_CHUNK_WORDS = 0xFFF0 // 4
# Every libdpx call the gate makes, checked before the device is touched so a
# pypixxlib without one is a RewardError naming it, never an AttributeError
# half-way through. DPxIsDoutPixelModeB is optional (older pypixxlib; see
# pixel_mode_b).
GATE_CALLS = (
    "DPxOpen",
    "DPxClose",
    "DPxSelectDevice",
    "DPxGetError",
    "DPxGetErrorString",
    "DPxClearError",
    "DPxUpdateRegCache",
    "DPxGetFirmwareRev",
    "DPxIsDoutPixelMode",
    "DPxIsDoutPixelModeGB",
    "DPxIsDoutButtonSchedules",
    "DPxIsDoutBacklightPulse",
    "DPxIsDoutSchedRunning",
    "DPxGetDoutValue",
    "DPxSetDoutValue",
    "DPxGetRamSize",
    "TPxGetBuffBaseAddr",
    "TPxGetBuffSize",
    "DPxWriteRam",
    "DPxReadRam",
    "DPxSetDoutBuff",
    "DPxSetDoutSched",
    "DPxStartDoutSched",
    "DPxStopDoutSched",
)

_UNCERTAIN = (
    "LINE STATE UNCERTAIN — the gate could not be confirmed at rest, so the pump may still be "
    "running: stop it at the pump (its front panel, or switch it off) and check the wiring "
    "before the next delivery"
)


def read_gate_state(lib: Any, cfg: RewardHwConfig) -> dict[str, Any]:
    """Read what decides whether a reward gate on ``cfg``'s line is safe to
    drive. No digital-output value is written; the one register-cache round
    trip (DPxUpdateRegCache: libdpx writes its local register cache back to
    the device, then reads the registers) is the only way pypixxlib offers to
    refresh them. The caller holds the DATAPixx3 link's lock with the
    DATAPixx3 selected. A device fault, or a call this pypixxlib lacks or
    rejects, raises RewardError."""
    bit = dout_bit_of(cfg.channel)
    mask = 1 << (bit if bit is not None else 0)
    try:
        lib.DPxUpdateRegCache()
        pixel_b, pixel_b_basis = pixel_mode_b(lib)
        base, size = int(lib.TPxGetBuffBaseAddr()), int(lib.TPxGetBuffSize())
        value = int(lib.DPxGetDoutValue())
        state: dict[str, Any] = {
            "pixel_mode": bool(lib.DPxIsDoutPixelMode()),
            "pixel_mode_gb": bool(lib.DPxIsDoutPixelModeGB()),
            "pixel_mode_b": pixel_b,
            "pixel_mode_b_basis": pixel_b_basis,
            "button_schedules": bool(lib.DPxIsDoutButtonSchedules()),
            "backlight_pulse": bool(lib.DPxIsDoutBacklightPulse()),
            "dout_schedule_running": bool(lib.DPxIsDoutSchedRunning()),
            # The static register as libdpx reads it back: register evidence,
            # not a measurement of the pins.
            "dout_value": value,
            "gate_level_before": "HIGH" if value & mask else "LOW",
            "ram_bytes": int(lib.DPxGetRamSize()),
            "trackpixx_ring": [base, size],
        }
    except (AttributeError, TypeError) as e:
        raise RewardError(
            f"reward gate {cfg.line}: this pypixxlib lacks or rejects a call alhazen needs "
            f"({type(e).__name__}: {e}); {PYPIXXLIB_HELP}"
        ) from e
    fault = dpx_fault(lib)
    if fault is not None:
        raise RewardError(f"reward gate {cfg.line}: the {HOST_DEVICE} reported {fault}")
    return state


def gate_refusal(state: dict[str, Any], cfg: RewardHwConfig) -> str | None:
    """Why a reward gate must not be driven in this device state, or None.

    Any other output mode that owns a bit the schedule writes (DOUT 0-15) or
    a bit VPixx does not document the schedule leaving alone (16-23) — or one
    whose state cannot be read — a DOUT schedule already running, and a
    buffer outside the RAM or on the TRACKPixx3's own regions."""
    address = cfg.dout_buffer_address
    end = address + DATAPIXX_DOUT_BUFFER_BYTES
    base, size = state["trackpixx_ring"]
    if state["pixel_mode"]:
        return (
            "Pixel Mode is on: every DOUT follows the screen's top-left pixel, so the gate "
            "would open and close with the stimulus"
        )
    if state["pixel_mode_gb"]:
        return (
            "Pixel Mode GB is on: DOUT 8-23 follow the screen's top-left pixel, and the "
            "reward schedule writes DOUT 0-15 as a whole while it plays"
        )
    if state["pixel_mode_b"] is None:
        return (
            f"whether Pixel Mode B (DOUT 16-23 from the screen) is on cannot be read: "
            f"{state['pixel_mode_b_basis']}. VPixx does not document whether a DOUT schedule "
            f"leaves DOUT 16-23 alone, so the gate is not driven blind; {PYPIXXLIB_HELP}"
        )
    if state["pixel_mode_b"]:
        return (
            "Pixel Mode B is on: DOUT 16-23 follow the screen's top-left pixel, and VPixx "
            "does not document whether a DOUT schedule leaves DOUT 16-23 alone"
        )
    if state["button_schedules"]:
        return (
            "DOUT button schedules are on: a button press would play a stored waveform "
            "onto the port, gate included"
        )
    if state["backlight_pulse"]:
        return (
            "the backlight pulse is on: DOUT 15 gates the display backlight, and the "
            "reward schedule writes DOUT 0-15 as a whole while it plays"
        )
    if state["dout_schedule_running"]:
        return "a DOUT schedule is already running (another program, or a train not ended)"
    if end > state["ram_bytes"]:
        return (
            f"the schedule buffer {address:#x}-{end:#x} runs past the device's RAM "
            f"({state['ram_bytes']:#x} bytes)"
        )
    for name, start, length in (
        ("sample ring", base, size),
        ("camera image", TRACKPIXX_IMAGE_BASE, TRACKPIXX_IMAGE_SIZE),
    ):
        if length > 0 and address < start + length and end > start:
            return (
                f"the schedule buffer {address:#x}-{end:#x} overlaps the TRACKPixx3 {name} "
                f"({start:#x}-{start + length:#x})"
            )
    return None


def write_schedule_words(lib: Any, address: int, words: list[int]) -> None:
    """Write ``words`` to DATAPixx3 RAM from ``address`` in chunks of at most
    VENDOR_RAM_CHUNK_WORDS, each at its own byte offset (two bytes a word),
    reading libdpx's sticky error after every chunk; then read the whole
    buffer back, chunk by chunk, and compare it word for word.

    RewardError, naming the chunk or the first differing word, when a chunk
    fails, a read-back is short, or a word differs. The caller holds the
    link's lock and has started nothing: a refusal here leaves no schedule
    to stop."""
    for start in range(0, len(words), VENDOR_RAM_CHUNK_WORDS):
        chunk = words[start : start + VENDOR_RAM_CHUNK_WORDS]
        lib.DPxWriteRam(address + 2 * start, chunk)
        fault = dpx_fault(lib)
        if fault is not None:
            raise RewardError(
                f"writing schedule words {start}-{start + len(chunk) - 1} to RAM at "
                f"{address + 2 * start:#x} failed: the {HOST_DEVICE} reported {fault}"
            )
    for start in range(0, len(words), VENDOR_RAM_CHUNK_WORDS):
        expected = words[start : start + VENDOR_RAM_CHUNK_WORDS]
        # DPxReadRam's length is in words; it returns a numpy array.
        found = [int(word) for word in lib.DPxReadRam(address + 2 * start, len(expected))]
        fault = dpx_fault(lib)
        if fault is not None:
            raise RewardError(
                f"reading schedule words {start}-{start + len(expected) - 1} back failed: "
                f"the {HOST_DEVICE} reported {fault}"
            )
        if len(found) != len(expected):
            raise RewardError(
                f"reading schedule words back from {address + 2 * start:#x} returned "
                f"{len(found)} words, not {len(expected)}"
            )
        for offset, (got, wanted) in enumerate(zip(found, expected, strict=True)):
            if got != wanted:
                raise RewardError(
                    f"schedule word {start + offset} read back {got:#06x}, not the "
                    f"{wanted:#06x} written: the buffer in RAM is not the train"
                )


def probe_datapixx_gate(cfg: RewardHwConfig) -> dict[str, Any]:
    """The Measure rig's connection check for a DATAPixx3 gate: open the
    DATAPixx3, read what decides whether the gate is safe, and release it.
    No digital-output value is changed, not even the rest level; reading
    the registers takes one register-cache round trip (read_gate_state).
    Returns the state read and ``refusal`` (None when a gate could be
    opened on it). Raises RewardError when pypixxlib, one of its calls or
    the device is missing."""
    try:
        libdpx = load_libdpx()
    except ImportError as e:
        raise RewardError(f"pypixxlib is not installed — {PYPIXXLIB_HELP}") from e
    missing = missing_calls(libdpx, GATE_CALLS)
    if missing:
        raise RewardError(
            f"this pypixxlib lacks {', '.join(missing)}, which the DATAPixx3 reward gate "
            f"needs; {PYPIXXLIB_HELP}"
        )
    link = dpx_link(libdpx)
    probe = object()  # a holder of its own, so the link's count stays honest
    link.acquire(f"reading the {HOST_DEVICE}'s digital outputs", RewardError)
    try:
        try:
            link.connect(probe)
            state = read_gate_state(libdpx, cfg)
        except RuntimeError as e:
            raise RewardError(f"reward gate {cfg.line}: {e}") from e
        finally:
            if link.release(probe):
                libdpx.DPxClose()
    finally:
        link.lock.release()
    return {**state, "refusal": gate_refusal(state, cfg)}


class _CompletionNotObserved(RewardError):
    """The schedule's end could not be asked about: the link stayed busy."""


class DatapixxReward:
    """Delivers reward by holding a pump's TTL gate from a DATAPixx3 digital
    output, timed by the device's own DOUT schedule.

    Built for the WPI SP220i's Gate input (TTL I/O pin 4 against ground on
    pin 3): ``polarity: active_low`` rests the line HIGH and holds it LOW
    while dispensing. The pump runs at the rate and syringe set on its own
    panel for as long as the gate is held, so the dose is rate × gate time:
    the rig declares both (``RewardHwConfig.pump``) and a reward calibration
    is valid only for the settings it was measured at.

    What a delivery proves: the DATAPixx3 held the exact train in RAM (read
    back word for word), accepted and played the schedule, and the gate's
    static register bit read back at rest before and after it. A register
    read-back is not a measurement of the pin's voltage, and nothing here
    shows the pump ran or liquid flowed (the pump's Run output is not read):
    the delivery is *commanded*; the volume is the reward calibration's.

    Safety, in order of what can be promised:

    - **Refused before any digital-output write.** Every condition that
      would make the gate unsafe or the train wrong is read first
      (``preflight``): any other output mode on the digital outputs — Pixel
      Mode (DOUT 0-23), Pixel Mode GB (8-23, inside the schedule's 0-15),
      Pixel Mode B (16-23: VPixx does not document whether a schedule leaves
      them alone), or a Pixel Mode B this pypixxlib cannot read on firmware
      that has it — DOUT button schedules (they play stored waveforms from
      this buffer region), the backlight pulse (DOUT 15, inside the schedule
      word), another DOUT schedule already running, a buffer that would
      overwrite the TRACKPixx3's sample ring or camera image or run past the
      RAM, and a pypixxlib without one of the calls used (GATE_CALLS).
      alhazen never turns any of those modes off: a recording may rely on
      them.
    - **Only its own bit.** Every static write has the gate's bit, and only
      it, in its mask; a train's words carry every other low bit at the
      level the static register already holds.
    - **Rest first, the train exact.** The gate's static bit is set to rest
      and read back at open and before every train; the train is written in
      chunks pypixxlib transfers whole, read back word for word, and started
      only when it matches; rest is read back again after it.
    - **Hardware-timed and finite.** One schedule of fixed length with
      countdown, never wrapping; it ends on the device even if this process
      dies mid-train. Python only waits to ask whether it has ended, and
      says so when it could not ask (the shared link stayed busy) rather
      than calling the schedule stuck.
    - **Faults:** once a train is started, any fault stops the schedule
      (``DPxStopDoutSched``, never ``DPxStopAllScheds``), writes rest once
      and raises RewardUncertainError — the delivery is UNCERTAIN, never
      retried — with ``LINE STATE UNCERTAIN`` when rest could not be
      confirmed. A fault before the start (refusal, RAM write or read-back)
      raises RewardError with nothing played.
    - **What software cannot promise:** the line's level when the DATAPixx3
      powers up, resets, loses USB or power, or before this process first
      opens it. The bench protocol (docs/datapixx-reward.md) covers powering
      the pump only after rest is set, and the physical stop.

    Every libdpx call runs under the DATAPixx3 link's lock with the
    DATAPixx3 selected first (devices/vpixx.py), shared with a TRACKPixx3
    tracker; the lock is released between completion polls, and the device
    is closed only by the link's last holder. While open, the gate owns the
    whole digital-output port: no DATAPixx3 sync line may share it. A
    TRACKPixx3 on the same DATAPixx3 must be connected first (the session
    builder does so): pypixxlib's TRACKPixx3() would otherwise open the
    device a second time, and the tracker refuses that.
    """

    def __init__(self, cfg: RewardHwConfig) -> None:
        bit = dout_bit_of(cfg.channel)
        if cfg.backend != "datapixx" or bit is None or cfg.polarity is None:
            raise RewardError(f"not a DATAPixx3 reward line: {cfg!r}")  # config validates this
        try:
            libdpx = load_libdpx()
        except ImportError as e:
            # Loud at construction, never a silent no-op backend (NidaqReward).
            raise RewardError(
                f"pypixxlib is not installed — it ships with VPixx's Software Tools installer, "
                f"not PyPI; {PYPIXXLIB_HELP}; or use reward backend 'simulated'"
            ) from e
        missing = missing_calls(libdpx, GATE_CALLS)
        if missing:
            raise RewardError(
                f"reward gate {cfg.line}: this pypixxlib lacks {', '.join(missing)}, which the "
                f"gate needs. Nothing was opened; {PYPIXXLIB_HELP}"
            )
        self._cfg = cfg
        self._lib = libdpx
        self._link = dpx_link(libdpx)
        self._bit = bit
        self._mask = 1 << bit
        self._rest = self._mask if cfg.polarity == "active_low" else 0
        self._closed = False
        # The last look at the port (check-rig's evidence) and the last
        # train's numbers (what was played, and when it was seen done).
        self.preflight: dict[str, Any] = {}
        self.last_train: dict[str, Any] | None = None
        self._link.acquire(f"opening the reward gate on {cfg.line}", RewardError)
        try:
            try:
                self._link.connect(self)
            except RuntimeError as e:
                raise RewardError(f"reward gate {cfg.line}: {e}") from e
            try:
                self.preflight = self._preflight()
                self._link.reserve(self, self._mask, schedules=True)
                problem = self._write_rest()
                if problem is not None:
                    raise RewardUncertainError(
                        f"reward gate {cfg.line}: could not set its rest level ({problem}). "
                        f"{_UNCERTAIN}"
                    )
            except ValueError as e:
                self._release()
                raise RewardError(f"reward gate {cfg.line}: {e}") from e
            except BaseException:
                self._release()
                raise
        finally:
            self._link.lock.release()
        before = self.preflight["gate_level_before"]
        if before != self._level_name(self._rest):
            log.warning(
                "reward gate %s read %s when opened — the ACTIVE level for a %s gate: anything "
                "already wired to it may have been running. Its register now reads at rest.",
                cfg.line,
                before,
                cfg.polarity,
            )
        log.info(
            "reward gate on %s %s (%s, rests %s); pump declared as %s",
            HOST_DEVICE,
            cfg.channel,
            cfg.polarity,
            self._level_name(self._rest),
            cfg.pump,
        )

    # ------------------------------------------------------------------

    def deliver(self, pulses: RewardPulses) -> None:
        if self._closed:
            raise RewardError("reward delivery requested after the reward gate was closed")
        if pulses.n_pulses == 0 or pulses.pulse_ms == 0:
            # As NidaqReward: deliberately empty, a logged no-op.
            log.info("reward delivery skipped: %s delivers nothing", pulses)
            return
        line = self._cfg.line
        count = gate_word_count(pulses)
        if count > MAX_GATE_WORDS:
            # Refused before anything is built or touched: never shortened.
            raise RewardError(
                f"a reward train of {count} ms on {line} is longer than the {MAX_GATE_WORDS} ms "
                f"the gate's schedule buffer holds; nothing was played"
            )
        words: list[int] = []
        start_error: BaseException | None = None
        self._link.acquire(f"starting a reward train on {line}", RewardError)
        try:
            # Refusals, a rest that will not read back and a buffer that will
            # not read back raise from here with nothing played.
            state = self._preflight()
            problem = self._write_rest()
            if problem is not None:
                raise RewardUncertainError(
                    f"reward train on {line} not started: the gate's rest level could not be "
                    f"confirmed before it ({problem}). {_UNCERTAIN}"
                )
            words = build_gate_words(
                pulses,
                bit=self._bit,
                polarity=self._cfg.polarity or "active_low",
                port_value=state["dout_value"],
            )
            try:
                write_schedule_words(self._lib, self._cfg.dout_buffer_address, words)
            except RewardError as e:
                raise RewardError(f"reward train on {line} not started: {e}") from e
            try:
                self._start_schedule(words)
            except BaseException as e:  # reported below, with the line's state
                start_error = e
        finally:
            self._link.lock.release()
        if start_error is not None:
            self._fail(f"starting the train on {line}", start_error)
        started = time.monotonic()
        length_s = len(words) / SAMPLE_RATE_HZ
        try:
            self._wait_for_schedule(length_s, started)
        except BaseException as e:  # a fault, a stuck or unobserved schedule, an interrupt
            self._fail(f"waiting for the train on {line} to finish", e)
        done_s = time.monotonic() - started
        self._link.acquire(f"checking the reward gate on {line} after a train", RewardError)
        try:
            try:
                self._select()
                problem = self._write_rest()
            except RewardError as e:
                problem = str(e)
        finally:
            self._link.lock.release()
        if problem is not None:
            raise RewardUncertainError(
                f"the reward train on {line} finished, but the gate's register did not read "
                f"back at rest ({problem}). {_UNCERTAIN}"
            )
        self.last_train = {
            "samples": len(words),
            "train_s": length_s,
            "seen_done_after_s": round(done_s, 4),
        }

    def close(self) -> None:
        """Leave the gate at rest, read it back, and release the DATAPixx3 —
        closing it only as the link's last holder. Raises RewardError when
        the rest level could not be confirmed (after releasing).

        When the link cannot be had (another libdpx call is stuck), nothing
        is released and the gate stays closable: a later close() tries
        again. Its holder entry is never removed without the lock."""
        if self._closed:
            return
        if not self._link.lock.acquire(timeout=_LINK_TIMEOUT_S):
            raise RewardError(
                f"the reward gate on {self._cfg.line} could not be closed yet: another call into "
                f"the {HOST_DEVICE} has not returned. It is still open; close() may be called "
                f"again. {_UNCERTAIN}"
            )
        problem: str | None = None
        try:
            self._closed = True
            try:
                self._select()
                self._lib.DPxUpdateRegCache()
                if self._lib.DPxIsDoutSchedRunning():
                    self._lib.DPxStopDoutSched()
                problem = self._write_rest()
            except Exception as e:  # pypixxlib's exception type cannot be named off the rig
                problem = f"{type(e).__name__}: {e}"
            finally:
                self._release()
        finally:
            self._link.lock.release()
        if problem is not None:
            raise RewardUncertainError(
                f"reward gate {self._cfg.line} at close: {problem}. {_UNCERTAIN}"
            )

    # ------------------------------------------------------------------
    # Under the link's lock
    # ------------------------------------------------------------------

    def _select(self) -> None:
        try:
            self._link.select()
        except RuntimeError as e:
            raise RewardError(f"reward gate {self._cfg.line}: {e}") from e

    def _preflight(self) -> dict[str, Any]:
        """Select the DATAPixx3, then read what decides whether a train is
        safe (read_gate_state: no digital-output value written); refuse
        (RewardError) with the reason if it is not. The caller holds the
        link's lock."""
        self._select()
        state = read_gate_state(self._lib, self._cfg)
        refusal = gate_refusal(state, self._cfg)
        if refusal is not None:
            raise RewardError(
                f"reward gate {self._cfg.line} refused before any digital-output write: "
                f"{refusal}. alhazen does not change this setting for you — another program or "
                f"the recording may rely on it"
            )
        return state

    def _start_schedule(self, words: list[int]) -> None:
        """Start the finite schedule over the words already in RAM (written
        and read back by write_schedule_words). The caller holds the link's
        lock with the DATAPixx3 selected. VPixx's documented DOUT schedule
        sequence (docs.vpixx.com, "Send a digital output trigger on VSync"):
        DPxSetDoutBuff (a buffer of exactly the words, so the schedule never
        wraps), DPxSetDoutSched with a count (count > 0 enables the
        countdown: the schedule stops itself after that many words),
        DPxStartDoutSched, DPxUpdateRegCache."""
        lib, address = self._lib, self._cfg.dout_buffer_address
        lib.DPxSetDoutBuff(address, len(words) * 2)
        lib.DPxSetDoutSched(0, SAMPLE_RATE_HZ, "hz", len(words))
        lib.DPxStartDoutSched()
        lib.DPxUpdateRegCache()
        fault = dpx_fault(lib)
        if fault is not None:
            raise RewardError(f"the {HOST_DEVICE} reported {fault}")

    def _wait_for_schedule(self, length_s: float, started: float) -> None:
        """Return once the device reports the schedule ended; raise if it
        reports a fault, if it still reports running long past its length
        (confirmed stuck), or if it could not be asked by then because the
        shared link stayed busy (_CompletionNotObserved: the schedule is
        finite and ends on the device by itself, but its end was not seen).
        Sleeps are for when to ask, never for how long the gate is held;
        the link's lock is taken for each question and released between
        them, and a busy link before the deadline means wait and ask again."""
        lead = length_s - _SCHEDULE_POLL_LEAD_S
        if lead > 0:
            time.sleep(lead)
        deadline = started + length_s + _SCHEDULE_SLACK_S
        while True:
            remaining = deadline - time.monotonic()
            if not self._link.lock.acquire(timeout=max(remaining, 0.0)):
                raise _CompletionNotObserved(
                    f"the schedule's end could not be observed: the {HOST_DEVICE} link stayed "
                    f"busy (another libdpx call held it) until {_SCHEDULE_SLACK_S:g} s past the "
                    f"train's own {length_s:g} s. The schedule is finite and ends on the device "
                    f"by itself, but its end was not seen"
                )
            try:
                self._select()
                self._lib.DPxUpdateRegCache()
                running = bool(self._lib.DPxIsDoutSchedRunning())
                fault = dpx_fault(self._lib)
            finally:
                self._link.lock.release()
            if fault is not None:
                raise RewardError(f"the {HOST_DEVICE} reported {fault}")
            if not running:
                return
            if time.monotonic() > deadline:
                raise RewardError(
                    f"the {HOST_DEVICE} still reported the schedule running {_SCHEDULE_SLACK_S:g} "
                    f"s after the train's own {length_s:g} s (confirmed stuck)"
                )
            time.sleep(_SCHEDULE_POLL_S)

    def _write_rest(self) -> str | None:
        """Write the gate's static bit — only it — to rest and read the
        register back. None when it reads at rest, else what went wrong. The
        caller holds the lock with the DATAPixx3 selected."""
        lib = self._lib
        lib.DPxSetDoutValue(self._rest, self._mask)
        lib.DPxUpdateRegCache()
        level = int(lib.DPxGetDoutValue()) & self._mask
        fault = dpx_fault(lib)
        if fault is not None:
            return f"the {HOST_DEVICE} reported {fault}"
        if level != self._rest:
            now, wanted = self._level_name(level), self._level_name(self._rest)
            return f"it reads {now} after being set {wanted}"
        return None

    def _release(self) -> None:
        """Leave the link; close the device if this was its last holder. The
        caller holds the lock."""
        if self._link.release(self):
            self._lib.DPxClose()

    # ------------------------------------------------------------------

    def _fail(self, doing: str, error: BaseException) -> None:
        """Stop the started schedule once, write rest once, and raise
        RewardUncertainError saying what failed and whether the register
        read back at rest. The delivery is uncertain, never retried."""
        settled: str | None
        if self._link.lock.acquire(timeout=_LINK_TIMEOUT_S):
            try:
                try:
                    self._select()
                    self._lib.DPxStopDoutSched()
                    self._lib.DPxUpdateRegCache()
                    settled = self._write_rest()
                except Exception as e:  # pypixxlib's exception type cannot be named off the rig
                    settled = f"{type(e).__name__}: {e}"
            finally:
                self._link.lock.release()
        else:
            settled = f"the {HOST_DEVICE} link stayed busy"
        outcome = (
            "the schedule was stopped and the gate's register read back at rest. The delivery "
            "is UNCERTAIN (some or all of the train may have run) and is NOT retried"
            if settled is None
            else f"stopping it failed too ({settled}). The delivery is UNCERTAIN. {_UNCERTAIN}"
        )
        message = f"reward failed while {doing}: {type(error).__name__}: {error}; {outcome}"
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            log.error(message)
            raise error
        raise RewardUncertainError(message) from error

    def _level_name(self, level: int) -> str:
        return "HIGH" if level else "LOW"


# How long DatapixxReward waits for the DATAPixx3 link when it must reach the
# device to make the line safe (close, or after a failure): the link's own
# bound (devices/vpixx.py).
_LINK_TIMEOUT_S = 2.0


# How long a wait for the reward worker goes before it says so in the log.
# Not a timeout — a pump train has no business being cut short, and NidaqReward
# bounds its own hardware wait — but a session sitting silently on a stuck
# delivery must at least show up in the log as exactly that.
_WAIT_WARNING_S = 5.0


@dataclass
class _Job:
    """One delivery, waiting in the worker's line or on the valve.

    ``request`` is set for a mid-trial request (reported back through the
    completion queue, drained by the session thread). ``finished`` is set for
    a synchronous delivery (manual reward, end-of-trial pay), whose caller is
    blocked on it and gets any error re-raised on its own thread.
    """

    pulses: RewardPulses
    request: RewardRequest | None = None
    finished: threading.Event | None = None
    error: BaseException | None = None


class QueuedReward:
    """One dispenser, one valve, one delivery at a time — on a worker thread.

    Wraps the rig's dispenser for a task that asks for reward mid-trial
    (``Task.mid_trial_reward``). Every delivery the session makes goes through
    it, so none ever overlaps another on the valve:

    - ``submit(request)`` — a mid-trial drop, from the engine. Returns at once
      with how many deliveries are ahead of it, because an 8 s pursuit at
      120 Hz cannot absorb a 200 ms pulse train inside a frame. Its outcome
      comes back through ``completed()``. Requests that arrive while one is
      delivering wait their turn; none is dropped or merged.
    - ``deliver(pulses)`` — the end-of-trial pay. Waits behind whatever is
      queued, then for its own delivery, and re-raises a failure on the
      caller's thread — the same contract as any ``RewardDispenser``, which
      is why the runner need not know it holds this rather than the device.
      Never cancelled.
    - ``deliver_manual(pulses)`` — the experimenter's manual reward (the ``r``
      key during a trial, R in the pause menu). The same synchronous contract
      as ``deliver``, but it overrides the queue: every drop still waiting is
      cancelled (reported through ``completed()``, never delivered), and the
      manual reward is delivered once, as soon as the delivery already on the
      valve finishes. Drops asked for after it queue as usual.

    So the order at the valve is: the delivery on it finishes (it is never
    cut short — ``deliver_manual`` says why); then the oldest waiting manual
    reward; then the oldest of everything else — which, once a manual reward
    has emptied the queue of drops, is only what was asked for after it.

    Threading: the worker only calls the dispenser and puts plain-data
    completions on a thread-safe queue; a cancellation is put there by the
    thread that asked for the manual reward. Neither ever touches the event
    bus, the recorder or a trial record — the session thread drains the
    queue and emits the events (core/engine.py), so the one-writer rule the
    bus and recorder rely on holds.
    """

    def __init__(self, dispenser: RewardDispenser) -> None:
        self.dispenser = dispenser
        self._done: queue.Queue[RewardCompletion] = queue.Queue()
        # One lock guards everything the session thread and the worker share:
        # both lines below, _outstanding and _closed. One lock, not one per
        # structure, so a count taken while a job joins a line always agrees
        # with where the job was put, and a manual reward's cancellations and
        # its own place in line are one step: the worker cannot take a job,
        # or finish one, in between.
        self._lock = threading.Lock()
        # Two conditions on that one lock, each with its own kind of waiter:
        # the worker waits on _wakeup for a job to arrive (or for close()),
        # and wait_idle waits on _idle for the last delivery to finish.
        self._wakeup = threading.Condition(self._lock)
        self._idle = threading.Condition(self._lock)
        # The jobs waiting for the valve, in two lines, each first in, first
        # out. _manual holds manual rewards (deliver_manual); _queue holds the
        # mid-trial drops (submit) and the end-of-trial pay (deliver). The
        # worker empties _manual before it takes anything from _queue (_take).
        # A job leaves its line when the worker takes it, so neither line ever
        # holds the delivery that is on the valve — which is why a manual
        # reward, cancelling what is in _queue, can never cut that one short.
        self._manual: deque[_Job] = deque()
        self._queue: deque[_Job] = deque()
        # Deliveries asked for and not yet finished or cancelled: everything
        # in both lines plus the one on the valve. What a new drop reports as
        # queued_behind (all of it goes before a job joining _queue) and what
        # wait_idle waits to reach zero.
        self._outstanding = 0
        self._closed = False
        # A daemon, so a process exiting after a crash is never held open by
        # a pump call that does not return. close() — a teardown step — is
        # what normally stops it, after the queue has been delivered.
        self._thread = threading.Thread(target=self._run, name="alhazen-reward", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------
    # The session thread's side
    # ------------------------------------------------------------------

    def submit(self, request: RewardRequest) -> int:
        """Queue a mid-trial drop; return how many deliveries are ahead of it.

        Exact when taken: everything outstanding goes before a new drop — the
        delivery on the valve, a manual reward still waiting, the drops
        queued earlier. A drop asked for while a manual reward waits for the
        valve queues behind it and is not cancelled by it: a manual reward
        cancels only what was waiting when it was asked for.
        """
        return self._enqueue(_Job(pulses=request.pulses, request=request), manual=False)

    def deliver(self, pulses: RewardPulses) -> None:
        """Deliver after everything already queued, and wait for it.

        The end-of-trial pay. It never jumps the queue and a manual reward
        never cancels it: the runner pays between trials, where no frame is
        waiting on it, and only after ``settle_rewards`` has waited for every
        drop — so it finds the queue empty anyway, and follows the trial's
        drops at the valve. It is the trial's outcome, earned and decided;
        only drops, which a manual reward replaces, are ever cancelled.
        """
        self._deliver_and_wait(pulses, manual=False)

    def deliver_manual(self, pulses: RewardPulses) -> None:
        """The experimenter's manual reward: override the queue, deliver this
        once, and wait for it.

        Every mid-trial drop still waiting for the valve is cancelled — taken
        out of the queue and reported through ``completed()`` with
        ``cancelled_by="manual"``, so each gets its own REWARD_CANCELLED and
        none vanishes — and this reward is delivered as soon as the delivery
        already on the valve finishes. Drops asked for after this call queue
        as usual, behind it: the queue builds up again on its own. The
        end-of-trial pay (``deliver``) is never cancelled; one still waiting
        is delivered after this.

        Its caller is the session thread: the ``r`` key blocks the frame it
        was pressed on until the pump is done, so that the manual REWARD is
        emitted after the delivery it records. The wait is at most the rest
        of the train already on the valve plus this one's own. Several calls
        go in the order made, each cancelling the drops waiting when it was
        made (a session makes one at a time: the key is synchronous).

        The train already on the valve is not cut short, for two reasons:

        - The dose. Pulse width is what sets the volume delivered — it is the
          pump's calibration — so a train stopped part-way delivers an amount
          nobody measured, and that drop's REWARD_DELIVERED could not say
          what the subject received.
        - The line. ``NidaqReward`` plays a finite buffered waveform, and an
          analog-output task leaves its last generated sample on the line
          when it stops; that is why ``build_reward_waveform`` always ends at
          0 V. Stopping it mid-pulse — reaching, from this thread, into a
          task the worker owns — would leave the valve open until a second
          write drove the line to 0 V, and a failure of that write would
          flood the subject.

        Interrupting would save at most one train's wait. A failure is
        re-raised here, on the caller's thread. The drops it cancelled stay
        cancelled — each is already reported — and the worker carries on
        with whatever is asked for next.
        """
        self._deliver_and_wait(pulses, manual=True)

    def completed(self) -> list[RewardCompletion]:
        """Every mid-trial completion reported since the last call, oldest
        first. Never blocks."""
        done: list[RewardCompletion] = []
        while True:
            try:
                done.append(self._done.get_nowait())
            except queue.Empty:
                return done

    def wait_idle(self) -> None:
        """Block until nothing is queued or on the valve.

        Once this returns, every mid-trial completion is already in the
        completion queue: the worker reports a request *before* it counts it
        finished (``_run``), and a cancellation is reported in the same step
        that stops counting it (``_cancel_queued_drops``).
        """
        with self._idle:
            while self._outstanding:
                if not self._idle.wait(timeout=_WAIT_WARNING_S):
                    log.warning(
                        "still waiting for %d reward delivery(ies) to finish", self._outstanding
                    )

    def close(self) -> None:
        """Deliver what is still queued, stop the worker, close the dispenser.

        Queued drops are delivered rather than discarded: each was earned and
        already recorded as commanded. The dispenser is closed even when
        stopping the worker fails, so the device is released either way.
        """
        with self._lock:
            if self._closed:
                return
            # Nothing can join a line from here on (_enqueue refuses), so the
            # worker delivers what is already waiting and then stops: _take
            # returns None only once both lines are empty.
            self._closed = True
            self._wakeup.notify()
        try:
            while self._thread.is_alive():
                self._thread.join(timeout=_WAIT_WARNING_S)
                if self._thread.is_alive():
                    log.warning("still waiting for the reward worker to finish its queue")
            unreported = self.completed()
            if unreported:
                # The session settles every trial before this runs, so these
                # can only come from a path that skipped it. Their outcomes
                # never reached events.csv; the log is the last place to say.
                for done in unreported:
                    if done.cancelled_by is not None:
                        how = f"CANCELLED by a {done.cancelled_by} reward, never delivered"
                    elif done.error is None:
                        how = "delivered"
                    else:
                        how = f"FAILED — {done.error}"
                    log.error(
                        "mid-trial reward %r (frame %s) ended after the session stopped "
                        "reporting: %s",
                        done.request.reason,
                        done.request.frame,
                        how,
                    )
        finally:
            self.dispenser.close()

    def _deliver_and_wait(self, pulses: RewardPulses, *, manual: bool) -> None:
        """Put a synchronous delivery in line — a manual reward in the manual
        line, the end-of-trial pay at the end of the queue — and block until
        the worker has delivered it, re-raising its failure on this thread."""
        job = _Job(pulses=pulses, finished=threading.Event())
        self._enqueue(job, manual=manual)
        assert job.finished is not None
        # No timeout: the worker sets this whatever the dispenser does, and
        # a real dispenser bounds its own hardware wait (NidaqReward).
        job.finished.wait()
        if job.error is not None:
            raise job.error

    def _enqueue(self, job: _Job, *, manual: bool) -> int:
        """Put a job in line and wake the worker; return how many deliveries
        are ahead of it (on the valve or earlier in line).

        A manual reward first cancels every drop waiting in the queue
        (``_cancel_queued_drops``). All of it happens under the one lock, so
        the count is exact and the cancelling and the manual reward's place
        in line are one step: the worker cannot take a job, or finish one, in
        between. A drop it has not taken yet is cancelled; one it has taken
        is on the valve, and finishes.
        """
        cancelled: list[RewardRequest] = []
        with self._lock:
            if self._closed:
                raise RewardError("reward delivery requested after the reward worker was closed")
            if not self._thread.is_alive():
                # _run catches everything a delivery raises, so this means
                # the thread itself was killed. Queuing onto it would wait
                # forever for a delivery nobody makes.
                raise RewardError("the reward worker thread is no longer running")
            if manual:
                cancelled = self._cancel_queued_drops()
                # Still ahead of it: the delivery on the valve, which always
                # finishes, and any earlier manual reward — everything
                # outstanding except what is left in the queue, which can
                # only be an end-of-trial pay, and which it goes ahead of.
                ahead = self._outstanding - len(self._queue)
                self._manual.append(job)
            else:
                # Everything outstanding goes before a job joining the queue.
                ahead = self._outstanding
                self._queue.append(job)
            self._outstanding += 1
            # notify(), not notify_all(): the worker is the only thread that
            # ever waits on _wakeup.
            self._wakeup.notify()
        if cancelled:
            # Said in the log as well as in the events (REWARD_CANCELLED,
            # which the engine emits when it drains them): drops the subject
            # earned and will not receive.
            log.warning(
                "manual reward cancels %d queued mid-trial drop(s), which will not be "
                "delivered: %s",
                len(cancelled),
                ", ".join(f"{request.reason!r} (frame {request.frame})" for request in cancelled),
            )
        return ahead

    def _cancel_queued_drops(self) -> list[RewardRequest]:
        """Take every mid-trial drop out of the queue, report each as
        cancelled, and return their requests. The caller holds the lock.

        Only drops. A synchronous job in the queue — the end-of-trial pay,
        whose caller is blocked on it — is never cancelled and keeps its
        place. The delivery on the valve is in neither line, so it is never
        touched. Each cancellation is reported in the same step that stops
        counting it as outstanding, so ``wait_idle`` returning still means
        every completion is already in the completion queue.
        """
        kept: deque[_Job] = deque()
        cancelled: list[RewardRequest] = []
        for waiting in self._queue:
            if waiting.request is None:
                kept.append(waiting)
                continue
            cancelled.append(waiting.request)
            # "manual" is the only thing that cancels a drop today, and the
            # value the REWARD_CANCELLED payload carries (core/events.py).
            # Put while holding our lock, which cannot deadlock: the
            # completion queue's own lock is only ever held inside its put and
            # get, and nothing waits for our lock while holding it.
            self._done.put(RewardCompletion(request=waiting.request, cancelled_by="manual"))
        self._queue = kept
        # Never reaches zero here — the manual reward that cancelled them is
        # counted next, in the same step — so nobody in wait_idle needs waking.
        self._outstanding -= len(cancelled)
        return cancelled

    # ------------------------------------------------------------------
    # The worker thread
    # ------------------------------------------------------------------

    def _take(self) -> _Job | None:
        """The next job for the valve, or None once close() has been called
        and both lines are empty. Blocks while there is nothing to deliver.

        The manual line first, then the queue: this is the one place the
        order at the valve is decided.
        """
        with self._wakeup:
            while True:
                if self._manual:
                    return self._manual.popleft()
                if self._queue:
                    return self._queue.popleft()
                if self._closed:
                    return None
                # Releases the lock while it waits, so the session thread
                # can add to a line; re-checked on every wake-up.
                self._wakeup.wait()

    def _run(self) -> None:
        while True:
            job = self._take()
            if job is None:
                return
            error: BaseException | None = None
            try:
                self.dispenser.deliver(job.pulses)
            except BaseException as e:  # reported, never allowed to kill the worker
                # BaseException, not Exception: a worker that died here would
                # leave _outstanding above zero for ever, and the next
                # wait_idle — between trials — would never return.
                log.exception("reward delivery of %s failed", job.pulses)
                error = e
            if job.request is not None:
                # Reported BEFORE it counts as finished, so wait_idle
                # returning guarantees the completion is already queued.
                message = None if error is None else f"{type(error).__name__}: {error}"
                self._done.put(
                    RewardCompletion(
                        request=job.request,
                        error=message,
                        uncertain=isinstance(error, RewardUncertainError),
                    )
                )
                self._finish_one()
            else:
                # Counted finished BEFORE the waiting caller is released, so
                # a request it submits next does not see this delivery as
                # still ahead of it.
                job.error = error
                self._finish_one()
                assert job.finished is not None
                job.finished.set()

    def _finish_one(self) -> None:
        with self._idle:
            self._outstanding -= 1
            self._idle.notify_all()


def make_reward(cfg: RewardHwConfig) -> RewardDispenser:
    """Construct the dispenser a rig config names. Shared by session build and
    ``check-rig`` so a clean check exercises the real constructor."""
    if cfg.backend == "nidaq":
        return NidaqReward(cfg)
    if cfg.backend == "datapixx":
        return DatapixxReward(cfg)
    return SimulatedReward()
