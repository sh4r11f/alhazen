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

The six tests first written as strict-xfail ``KNOWN BUG`` reproductions of
the review's findings (against 2dd0cf1/2fcc3b0) pass since the safety fixes
and are unmarked. Three were rewritten to the stronger contract the fixes
give, each saying why where it stands: the gaze read's fault is now consumed
into the tracker's own dropout state rather than left pending; a tracker
connected after the gate is refused before pypixxlib's second DPxOpen (and the
real session builder is tested for one open and one close in both release
orders); an unreadable Pixel Mode B on firmware that has it refuses the gate.
The stand-in in tests/fake_sdk.py now models the vendor behaviour these tests
used to patch in (DPxWriteRam's c_ushort byte count, DPxSelectDevice clearing
the sticky error, TRACKPixx3()'s DPxOpen and close()'s DPxClose); the patches
here stay, as independent restatements of the vendor source. None of this is
evidence about the pins or the pump.
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
from alhazen.errors import RewardError, SyncError, TrackerError
from alhazen.testing import FakeClock
from fake_sdk import USB_GONE, FakeLibdpx, install_fake_libdpx, install_fake_pypixxlib
from support import SCREEN

PUMP = {"model": "WPI SP220i", "syringe_diameter_mm": 14.5, "rate_ul_per_min": 3000.0}
GATE = RewardHwConfig(backend="datapixx", channel="dout0", polarity="active_low", pump=PUMP)
SHORT = RewardPulses(n_pulses=2, pulse_ms=3, inter_pulse_ms=2)
# VPixx's DPxWriteDoutBuffer chunk: the most words it ever hands DPxWriteRam.
VENDOR_WRITE_RAM_MAX_WORDS = 0xFFF0 // 4


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

    def test_a_long_train_is_written_in_full(self, libdpx, monkeypatch):
        vendor_write_ram(monkeypatch, libdpx)
        monkeypatch.setattr(reward_module.time, "sleep", lambda s: None)
        reward = make_reward(GATE)
        reward.deliver(self.LONG)
        expected = build_gate_words(self.LONG, bit=0, polarity="active_low", port_value=1)
        assert libdpx.trains == [expected]

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

    def test_sync_on_dout_16_23_does_not_assume_pixel_mode_b_off(self, libdpx, monkeypatch, caplog):
        monkeypatch.delattr(FakeLibdpx, "DPxIsDoutPixelModeB")  # an older pypixxlib
        with caplog.at_level(logging.WARNING):
            try:
                sync = make_sync(self.HIGH)
            except SyncError:
                return  # refused: acceptable
            sync.close()
        assert "Pixel Mode B" in caplog.text

    # Rewritten with the M1 fix. It used to accept a gate that warned and drove
    # the port with Pixel Mode B unknown; the gate's schedule writes the port
    # and VPixx does not document DOUT 16-23 staying independent, so an
    # unknown Pixel Mode B now refuses it — unless the firmware revision shows
    # the mode cannot exist (VPixx: firmware 27+), which is evidence, not a
    # guess.
    def test_the_reward_gate_refuses_an_unreadable_pixel_mode_b(self, libdpx, monkeypatch):
        monkeypatch.delattr(FakeLibdpx, "DPxIsDoutPixelModeB")
        libdpx.firmware_rev = 27
        with pytest.raises(RewardError, match="Pixel Mode B.*cannot be read"):
            make_reward(GATE)
        assert libdpx.dout_writes == [] and libdpx.opens == libdpx.closes == 1

    def test_firmware_without_pixel_mode_b_needs_no_accessor(self, libdpx, monkeypatch):
        monkeypatch.delattr(FakeLibdpx, "DPxIsDoutPixelModeB")
        libdpx.firmware_rev = 26
        reward = make_reward(GATE)
        assert reward.preflight["pixel_mode_b"] is False
        assert "firmware revision 26" in reward.preflight["pixel_mode_b_basis"]
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
    and raises nothing, leaving libdpx's sticky error set.

    Rewritten with the M2 fix. It used to assert the error was still pending
    in libdpx after the read — the gap the review found, where the next
    DPxSelectDevice (the reward's) could clear it. The read now reads the
    error in its own hold of the lock and keeps it as the tracker's fault, so
    what holds afterwards is stronger: libdpx's flag is consumed and the
    reader already says what failed, before any check or anyone else's call."""
    device.libdpx.unplug()
    tracker._reader.read_now()
    device.libdpx.unplugged = False  # the link recovers
    assert device.libdpx.error == "DPX_SUCCESS"  # consumed by the tracker, not left pending
    verdict = tracker._reader.device_check
    assert verdict is not None and verdict[0] is not None and USB_GONE in verdict[0]


class TestTrackerFaultAttribution:
    def test_alone_the_tracker_check_catches_the_failed_read(self, monkeypatch):
        device, tracker = recording_tracker(monkeypatch)
        failed_gaze_read(device, tracker)
        tracker._reader.check_now()  # the device answers again: the check finds nothing
        assert tracker._reader.device_check[0] is not None
        assert USB_GONE in tracker.recording_fault()
        tracker.shutdown(None)

    def test_a_reward_between_read_and_check_does_not_hide_the_fault(self, monkeypatch):
        device, tracker = recording_tracker(monkeypatch)
        reward = make_reward(GATE)
        failed_gaze_read(device, tracker)
        reward.deliver(SHORT)  # its polls select the DATAPixx3 under the shared lock
        tracker._reader.check_now()
        try:
            assert tracker._reader.device_check[0] is not None
            detail = tracker.recording_fault()  # the trial ends as a dropout
            assert detail is not None and "gaze read" in detail and USB_GONE in detail
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

    # Rewritten with the M3 fix. It used to imitate the builder's old order by
    # hand (gate first, then the tracker) and ask for a balance the vendor
    # constructor made impossible. The builder now connects the tracker first
    # (the REAL builder path is tested below, in both release orders), and a
    # tracker connected after the gate is refused before pypixxlib's
    # TRACKPixx3() could call DPxOpen a second time.
    def test_a_tracker_after_the_gate_is_refused_without_a_second_open(self, monkeypatch):
        device = install_fake_pypixxlib(monkeypatch)
        vendor_faithful_tracker_class(monkeypatch, device)
        reward = make_reward(GATE)
        assert device.libdpx.opens == 1
        with pytest.raises(TrackerError, match="must be connected before the DATAPixx3 reward"):
            connect_tracker()
        assert device.libdpx.opens == 1  # TRACKPixx3() never ran
        reward.close()
        assert (device.libdpx.opens, device.libdpx.closes) == (1, 1)

    def test_a_real_session_opens_once_and_closes_once(self, tmp_path, monkeypatch):
        device = install_fake_pypixxlib(monkeypatch)
        vendor_faithful_tracker_class(monkeypatch, device)
        runner = session_with_tracker_and_gate(tmp_path)
        assert (device.libdpx.opens, device.libdpx.closes) == (1, 0)  # the tracker's open
        runner.run()  # teardown: tracker.shutdown, then reward.close
        assert (device.libdpx.opens, device.libdpx.closes) == (1, 1)
        assert device.libdpx.trains, "the session paid nothing through the gate"
        assert not device.closed  # TRACKPixx3.close() skipped: the gate closed last

    def test_a_failed_build_releases_gate_then_tracker_once(self, tmp_path, monkeypatch):
        device = install_fake_pypixxlib(monkeypatch)
        vendor_faithful_tracker_class(monkeypatch, device)
        from alhazen.session import builder

        def refuse(*args, **kwargs):
            raise RuntimeError("monitor refused (test)")

        monkeypatch.setattr(builder, "EyeTrackerMonitor", refuse)
        with pytest.raises(RuntimeError, match="monitor refused"):
            session_with_tracker_and_gate(tmp_path)
        # Released in reverse: the gate first (not the last holder: no
        # DPxClose), then the tracker, whose TRACKPixx3.close() closes once.
        assert (device.libdpx.opens, device.libdpx.closes) == (1, 1)
        assert device.closed


# ----------------------------------------------------------------------
# 5. A pypixxlib without one of the read-only calls
# ----------------------------------------------------------------------


class TestMissingCalls:
    def test_a_missing_read_only_call_is_a_reward_error_and_releases(self, libdpx, monkeypatch):
        monkeypatch.delattr(FakeLibdpx, "DPxIsDoutButtonSchedules")
        with pytest.raises(RewardError):
            make_reward(GATE)

    def test_the_device_is_released_whatever_escapes(self, libdpx, monkeypatch):
        monkeypatch.delattr(FakeLibdpx, "DPxIsDoutButtonSchedules")
        with pytest.raises((RewardError, AttributeError)):
            make_reward(GATE)
        # Since the m1 fix the missing call is found before the device is
        # opened at all (GATE_CALLS), so the balance holds at zero, which is
        # stronger than the 1 == 1 this asserted before.
        assert libdpx.opens == libdpx.closes == 0
        assert libdpx.dout_writes == []  # refused before any DOUT write

    def test_a_call_that_breaks_after_the_open_is_released_too(self, libdpx, monkeypatch):
        # Present but rejecting its arguments (a different pypixxlib's
        # signature): raised while reading, after the open.
        def rejects(*args):
            raise TypeError("DPxIsDoutButtonSchedules() takes 1 positional argument")

        monkeypatch.setattr(libdpx, "DPxIsDoutButtonSchedules", rejects)
        with pytest.raises(RewardError, match="lacks or rejects a call"):
            make_reward(GATE)
        assert libdpx.opens == libdpx.closes == 1
        assert libdpx.dout_writes == []


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


def session_with_tracker_and_gate(tmp_path):
    """build_session on a rig with a TRACKPixx3 and a DATAPixx3 gate, the
    builder's own order, a monkey task paying a short train per HIT."""
    from test_subject_kind import MONITOR, OutcomeTask, monkey

    from alhazen.config.models import DevicesConfig, DisplayConfig, Duration, RigConfig
    from alhazen.session.builder import build_session

    rig = RigConfig(
        monitor=MONITOR,
        display=DisplayConfig(backend="simulated"),
        devices=DevicesConfig(eyetracker=EyeTrackerConfig(backend="viewpixx"), reward=GATE),
        data_root=tmp_path,
    )
    short = {"n_pulses": 1, "pulse_ms": 2, "inter_pulse_ms": 0}
    return build_session(
        rig=rig,
        subject="t01",
        session=1,
        run=1,
        task=OutcomeTask(monkey(by_outcome={"HIT": short})),
        seed=1,
        iti=Duration(ms=0),
        simulated_frame_period_s=0.0,
        date_yyyymmdd="20261010",
    )
