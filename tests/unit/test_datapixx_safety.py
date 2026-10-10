"""Independent adverse-path tests for the DATAPixx3 reward gate, its sync
lines and the shared DATAPixx3 link, written by the final review rather than
the implementation.

Where the core stand-in (tests/fake_sdk.py) is narrower than pypixxlib, a test
here patches it to behave as pypixxlib's published source does, and says which
source:

- ``DPxWriteRam`` packs the byte count as ``ctypes.c_ushort`` (pypixxlib
  ``_libdpx.DPxWriteRam``), and VPixx's own ``DPxWriteDoutBuffer`` writes at
  most ``0xFFF0 // 4`` = 16,380 words per call ("maximum allowable size of data
  per call to DPxWriteRam()").
- ``TRACKPixx3()`` runs ``DpxDevice.__init__``: ``DPxOpen`` then
  ``DPxSelectDevice('TRACKPixx')``; ``TRACKPixx3.close()`` ends in
  ``DpxDevice.close`` -> ``DPxClose`` (pypixxlib ``tracker`` / ``dpxDevice``).

Tests marked ``KNOWN BUG`` fail on purpose against 2dd0cf1/2fcc3b0: each
reproduces a finding of the final review (strict xfail, so the suite stays
green and a fix turns them into XPASS failures that must be unmarked). None of
this is evidence about the pins or the pump.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
from pathlib import Path

import pytest

from alhazen.config.models import EyeTrackerConfig, RewardHwConfig, RewardPulses, SyncHwConfig
from alhazen.devices import reward as reward_module
from alhazen.devices.eyetracker import ViewPixxTracker
from alhazen.devices.reward import build_gate_words, make_reward
from alhazen.devices.sync import make_sync
from alhazen.errors import RewardError, SyncError
from alhazen.testing import FakeClock
from fake_sdk import USB_GONE, FakeLibdpx, install_fake_libdpx, install_fake_pypixxlib
from support import SCREEN

PUMP = {"model": "WPI SP220i", "syringe_diameter_mm": 14.5, "rate_ul_per_min": 3000.0}
GATE = RewardHwConfig(backend="datapixx", channel="dout0", polarity="active_low", pump=PUMP)
SHORT = RewardPulses(n_pulses=2, pulse_ms=3, inter_pulse_ms=2)
# VPixx's DPxWriteDoutBuffer chunk: the most words it ever hands DPxWriteRam.
VENDOR_WRITE_RAM_MAX_WORDS = 0xFFF0 // 4


def known_bug(reason: str):
    return pytest.mark.xfail(strict=True, reason=f"KNOWN BUG (final review): {reason}")


@pytest.fixture
def libdpx(monkeypatch) -> FakeLibdpx:
    return install_fake_libdpx(monkeypatch)


def vendor_write_ram(monkeypatch, lib: FakeLibdpx) -> list[int]:
    """Make the stand-in's DPxWriteRam keep only what pypixxlib's wrapper
    would transfer: the byte count goes through ctypes.c_ushort. Returns the
    word count of every call."""
    calls: list[int] = []
    original = lib.DPxWriteRam

    def write(address: int, words: list[int]) -> None:
        calls.append(len(words))
        transferred = ctypes.c_ushort(2 * len(words)).value // 2
        original(address, list(words)[:transferred])

    monkeypatch.setattr(lib, "DPxWriteRam", write)
    return calls


# ----------------------------------------------------------------------
# 1. A long train is truncated by pypixxlib's DPxWriteRam
# ----------------------------------------------------------------------


class TestScheduleBufferWrite:
    # 40 x (500 ms + 500 ms): 40,002 one-millisecond words. Inside the gate's
    # own 524,288-word limit and inside the Measure rig's reward.volume limits
    # (<= 500 pulses, <= 1000 ms, total open <= 60 s).
    LONG = RewardPulses(n_pulses=40, pulse_ms=500, inter_pulse_ms=500)

    def test_a_short_train_is_written_whole(self, libdpx, monkeypatch):
        calls = vendor_write_ram(monkeypatch, libdpx)
        reward = make_reward(GATE)
        reward.deliver(SHORT)
        expected = build_gate_words(SHORT, bit=0, polarity="active_low", port_value=1)
        assert calls == [len(expected)]
        assert libdpx.trains == [expected]
        reward.close()

    @known_bug(
        "DatapixxReward._start_schedule hands DPxWriteRam all words in one call; "
        "pypixxlib packs the byte count as c_ushort, so a train of >= 32,768 words "
        "is silently truncated and the schedule plays stale RAM for the rest"
    )
    def test_a_long_train_is_written_in_full(self, libdpx, monkeypatch):
        vendor_write_ram(monkeypatch, libdpx)
        monkeypatch.setattr(reward_module.time, "sleep", lambda s: None)
        reward = make_reward(GATE)
        reward.deliver(self.LONG)
        expected = build_gate_words(self.LONG, bit=0, polarity="active_low", port_value=1)
        assert libdpx.trains == [expected]

    @known_bug(
        "no DPxWriteRam call may exceed VPixx's own per-call chunk "
        f"({VENDOR_WRITE_RAM_MAX_WORDS} words, DPxWriteDoutBuffer)"
    )
    def test_no_ram_write_exceeds_the_vendor_chunk(self, libdpx, monkeypatch):
        calls = vendor_write_ram(monkeypatch, libdpx)
        monkeypatch.setattr(reward_module.time, "sleep", lambda s: None)
        reward = make_reward(GATE)
        # Refusing the train outright is an acceptable fix too.
        with contextlib.suppress(RewardError):
            reward.deliver(self.LONG)
        assert all(n <= VENDOR_WRITE_RAM_MAX_WORDS for n in calls), calls


# ----------------------------------------------------------------------
# 2. An unreadable Pixel Mode B is treated as off by the sync lines
# ----------------------------------------------------------------------


class TestUnknownPixelModeB:
    HIGH = SyncHwConfig(
        backend="datapixx", pulse_ms=0.5, event_lines={"STIM_ON": "DATAPIXX3/dout17"}
    )

    @known_bug(
        "DatapixxSync._preflight reads a missing DPxIsDoutPixelModeB as False and opens "
        "DOUT 16-23 without a refusal or a warning"
    )
    def test_sync_on_dout_16_23_does_not_assume_pixel_mode_b_off(self, libdpx, monkeypatch, caplog):
        monkeypatch.delattr(FakeLibdpx, "DPxIsDoutPixelModeB")  # an older pypixxlib
        with caplog.at_level(logging.WARNING):
            try:
                sync = make_sync(self.HIGH)
            except SyncError:
                return  # refused: acceptable
            sync.close()
        assert "Pixel Mode B" in caplog.text

    def test_the_reward_gate_says_pixel_mode_b_is_unknown(self, libdpx, monkeypatch, caplog):
        monkeypatch.delattr(FakeLibdpx, "DPxIsDoutPixelModeB")
        with caplog.at_level(logging.WARNING):
            reward = make_reward(GATE)
        assert reward.preflight["pixel_mode_b"] is None
        assert "cannot report Pixel Mode B" in caplog.text
        reward.close()


# ----------------------------------------------------------------------
# 3. A tracker read's sticky fault is consumed by the reward's select
# ----------------------------------------------------------------------


def recording_tracker(monkeypatch):
    device = install_fake_pypixxlib(monkeypatch)
    clock = FakeClock(start=10.0)
    tracker = ViewPixxTracker(
        EyeTrackerConfig(backend="viewpixx"), None, SCREEN, clock, background_gaze=False
    )
    tracker.connect()
    tracker.configure(SCREEN, clock)
    tracker.start_trial(1, "attempt 1")
    return device, tracker


def failed_gaze_read(device, tracker) -> None:
    """One gaze read on a USB hiccup: TPxBestPolyGetEyePosition writes nothing
    and raises nothing, leaving libdpx's sticky error set; the reader's read
    hold ends without reading it (GazeReader.read_now, no check due yet)."""
    device.libdpx.unplug()
    tracker._reader.read_now()
    device.libdpx.unplugged = False  # the link recovers
    assert device.libdpx.error == USB_GONE


class TestTrackerFaultAttribution:
    def test_alone_the_tracker_check_catches_the_failed_read(self, monkeypatch):
        device, tracker = recording_tracker(monkeypatch)
        failed_gaze_read(device, tracker)
        tracker._reader.check_now()
        assert tracker._reader.device_check[0] is not None
        tracker.shutdown(None)

    @known_bug(
        "DpxLink.select() (reward poll) logs and clears a fault the gaze read left "
        "unread, so the tracker's own recording check no longer sees it"
    )
    def test_a_reward_between_read_and_check_does_not_hide_the_fault(self, monkeypatch):
        device, tracker = recording_tracker(monkeypatch)
        reward = make_reward(GATE)
        failed_gaze_read(device, tracker)
        reward.deliver(SHORT)  # its polls select the DATAPixx3 under the shared lock
        tracker._reader.check_now()
        try:
            assert tracker._reader.device_check[0] is not None
        finally:
            reward.close()
            tracker.shutdown(None)


# ----------------------------------------------------------------------
# 4. Session order: the gate opens before the tracker (builder.py), so
#    pypixxlib's TRACKPixx3() issues a second DPxOpen
# ----------------------------------------------------------------------


def vendor_faithful_tracker_class(monkeypatch, device) -> None:
    """TRACKPixx3() and close() as pypixxlib's source has them."""
    lib = device.libdpx

    def construct():
        lib.DPxOpen()
        lib.DPxSelectDevice("TRACKPIXX")
        return device

    def close() -> None:
        lib.TPxUninitialize()
        lib.TPxSetSleepPictureRequest()
        lib.DPxUpdateRegCache()
        lib.DPxClose()
        device.closed = True

    monkeypatch.setattr(sys.modules["pypixxlib.tracker"], "TRACKPixx3", construct)
    monkeypatch.setattr(device, "close", close)


def connect_tracker() -> ViewPixxTracker:
    tracker = ViewPixxTracker(
        EyeTrackerConfig(backend="viewpixx"), None, SCREEN, FakeClock(), background_gaze=False
    )
    tracker.connect()
    return tracker


class TestOpenCloseBalance:
    def test_tracker_first_opens_once_and_closes_once(self, monkeypatch):
        device = install_fake_pypixxlib(monkeypatch)
        vendor_faithful_tracker_class(monkeypatch, device)
        tracker = connect_tracker()
        reward = make_reward(GATE)
        tracker.shutdown(None)
        reward.close()
        assert (device.libdpx.opens, device.libdpx.closes) == (1, 1)

    @known_bug(
        "build_session opens the reward (make_reward, builder.py ~840) before "
        "tracker.connect() (~882); pypixxlib's TRACKPixx3() then calls DPxOpen on an "
        "open library, and only one DPxClose follows (undocumented libdpx behaviour)"
    )
    def test_session_order_keeps_open_and_close_balanced(self, monkeypatch):
        device = install_fake_pypixxlib(monkeypatch)
        vendor_faithful_tracker_class(monkeypatch, device)
        reward = make_reward(GATE)  # the builder's order
        tracker = connect_tracker()
        tracker.shutdown(None)
        reward.close()
        assert device.libdpx.opens == device.libdpx.closes


# ----------------------------------------------------------------------
# 5. A pypixxlib without one of the read-only calls
# ----------------------------------------------------------------------


class TestMissingCalls:
    @known_bug(
        "a pypixxlib lacking a preflight call raises AttributeError out of make_reward "
        "instead of RewardError, so check-rig (catches AlhazenError) crashes"
    )
    def test_a_missing_read_only_call_is_a_reward_error_and_releases(self, libdpx, monkeypatch):
        monkeypatch.delattr(FakeLibdpx, "DPxIsDoutButtonSchedules")
        with pytest.raises(RewardError):
            make_reward(GATE)

    def test_the_device_is_released_whatever_escapes(self, libdpx, monkeypatch):
        monkeypatch.delattr(FakeLibdpx, "DPxIsDoutButtonSchedules")
        with pytest.raises((RewardError, AttributeError)):
            make_reward(GATE)
        assert libdpx.opens == libdpx.closes == 1
        assert libdpx.dout_writes == []  # refused before any DOUT write


# ----------------------------------------------------------------------
# 6. What holds, checked independently
# ----------------------------------------------------------------------


class TestSound:
    def test_every_dout_write_is_the_gate_bit_alone_and_rest_brackets_the_train(
        self, libdpx, monkeypatch
    ):
        libdpx.dout_register = 0b1010_0000_0000_0110  # other low bits high
        reward = make_reward(GATE)
        reward.deliver(SHORT)
        assert {mask for _, mask in libdpx.dout_writes} == {1}
        train = libdpx.trains[-1]
        assert train[0] & 1 and train[-1] & 1  # starts and ends at rest (HIGH)
        assert {word & ~1 for word in train} == {0b1010_0000_0000_0110}
        assert libdpx.dout_register & 1  # static gate bit at rest afterwards
        reward.close()

    def test_simulate_stands_the_gate_down_without_pypixxlib(self, monkeypatch):
        from alhazen.config.loader import load_rig
        from alhazen.devices.reward import SimulatedReward
        from alhazen.modes import Mode
        from alhazen.modes.session import rig_for_mode

        monkeypatch.delitem(sys.modules, "pypixxlib", raising=False)
        import alhazen

        rig = load_rig(str(Path(alhazen.__file__).parent / "rigs" / "rig-laptop.yaml"))
        rig = rig.model_copy(update={"devices": rig.devices.model_copy(update={"reward": GATE})})
        simulated, notes = rig_for_mode(Mode.SIMULATE, rig)
        assert simulated.devices.reward.backend == "simulated"
        assert isinstance(make_reward(simulated.devices.reward), SimulatedReward)
        assert "pypixxlib" not in sys.modules

    def test_a_fault_while_polling_stops_once_and_never_restarts(self, libdpx, monkeypatch):
        reward = make_reward(GATE)
        libdpx.polls_until_done = 3
        original = libdpx.DPxIsDoutSchedRunning

        def flaky() -> int:
            if libdpx.dout_running:
                libdpx.error = USB_GONE
            return original()

        monkeypatch.setattr(libdpx, "DPxIsDoutSchedRunning", flaky)
        with pytest.raises(RewardError, match="UNCERTAIN"):
            reward.deliver(SHORT)
        monkeypatch.setattr(libdpx, "DPxIsDoutSchedRunning", original)
        assert len(libdpx.trains) == 1 and libdpx.dout_stops == 1
        assert libdpx.dout_register & 1
        reward.deliver(SHORT)  # a NEW delivery only when asked again
        assert len(libdpx.trains) == 2
        reward.close()
