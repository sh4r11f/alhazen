"""Regression tests for the final review's DATAPixx3 findings (B1, M1-M3,
m1-m5), against the vendor-faithful stand-in in tests/fake_sdk.py:
DPxWriteRam keeps only c_ushort(2 * words) bytes, DPxReadRam takes a length
in words and returns a numpy array, DPxSelectDevice clears the sticky error,
TRACKPixx3() opens and close() ends in DPxClose. A green stand-in is not
evidence of the SDK; the vendor facts it models are cited in fake_sdk.py.
"""

from __future__ import annotations

import ctypes
import threading
import time

import numpy as np
import pytest

from alhazen.config.models import (
    EyeTrackerConfig,
    RewardHwConfig,
    RewardPulses,
    SyncHwConfig,
)
from alhazen.core.trial import RewardRequest
from alhazen.devices import reward as reward_module
from alhazen.devices.eyetracker import ViewPixxTracker
from alhazen.devices.reward import (
    MAX_GATE_WORDS,
    VENDOR_RAM_CHUNK_WORDS,
    QueuedReward,
    SimulatedReward,
    build_gate_words,
    gate_word_count,
    make_reward,
)
from alhazen.devices.sync import make_sync
from alhazen.devices.vpixx import dpx_link
from alhazen.errors import RewardError, RewardUncertainError, SyncError
from alhazen.testing import FakeClock
from fake_sdk import USB_GONE, FakeLibdpx, install_fake_libdpx, install_fake_pypixxlib
from support import SCREEN

PUMP = {"model": "WPI SP220i", "syringe_diameter_mm": 14.5, "rate_ul_per_min": 3000.0}
GATE = RewardHwConfig(backend="datapixx", channel="dout0", polarity="active_low", pump=PUMP)
SHORT = RewardPulses(n_pulses=2, pulse_ms=3, inter_pulse_ms=2)


@pytest.fixture
def libdpx(monkeypatch) -> FakeLibdpx:
    lib = install_fake_libdpx(monkeypatch)
    monkeypatch.setattr(reward_module.time, "sleep", lambda s: None)
    return lib


def one_pulse_of(words: int) -> RewardPulses:
    """A single pulse whose train is exactly ``words`` words long."""
    return RewardPulses(n_pulses=1, pulse_ms=words - 2, inter_pulse_ms=0)


# ----------------------------------------------------------------------
# The stand-in models the vendor's DPxWriteRam (it is what B1 hid behind)
# ----------------------------------------------------------------------


class TestTheStandIn:
    @pytest.mark.parametrize("words", [1, 16_380, 32_767, 32_768, 40_002])
    def test_write_ram_keeps_what_a_c_ushort_byte_count_keeps(self, words):
        lib = FakeLibdpx()
        lib.DPxWriteRam(0, [1] * words)
        kept = ctypes.c_ushort(2 * words).value // 2
        assert sum(1 for address in lib.ram if lib.ram[address] == 1) == kept

    def test_read_ram_takes_words_and_returns_numpy(self):
        lib = FakeLibdpx()
        lib.DPxWriteRam(100, [5, 6, 7])
        found = lib.DPxReadRam(100, 3)
        assert isinstance(found, np.ndarray) and found.tolist() == [5, 6, 7]

    def test_selecting_a_device_clears_an_unread_error(self):
        lib = FakeLibdpx()
        lib.error = USB_GONE
        assert lib.DPxSelectDevice("DATAPIXX3") == 1
        assert lib.error == "DPX_SUCCESS"


# ----------------------------------------------------------------------
# B1: the schedule buffer is written whole, verified, or refused
# ----------------------------------------------------------------------


class TestScheduleUpload:
    @pytest.mark.parametrize(
        "words", [VENDOR_RAM_CHUNK_WORDS, VENDOR_RAM_CHUNK_WORDS + 1, 32_768, 40_002]
    )
    def test_trains_around_the_chunk_and_the_c_ushort_wrap_play_exactly(self, libdpx, words):
        reward = make_reward(GATE)
        pulses = one_pulse_of(words)
        reward.deliver(pulses)
        expected = build_gate_words(pulses, bit=0, polarity="active_low", port_value=1)
        assert len(expected) == words
        assert libdpx.trains == [expected]
        assert all(n <= VENDOR_RAM_CHUNK_WORDS for n in libdpx.ram_write_calls)
        assert sum(libdpx.ram_write_calls) == words
        reward.close()

    def test_chunks_land_at_their_own_byte_offsets(self, libdpx):
        reward = make_reward(GATE)
        reward.deliver(one_pulse_of(VENDOR_RAM_CHUNK_WORDS + 3))
        address = GATE.dout_buffer_address
        # The second chunk's first word sits 2 bytes per word further on.
        assert address + 2 * VENDOR_RAM_CHUNK_WORDS in libdpx.ram
        reward.close()

    def test_a_corrupt_read_back_refuses_before_the_start(self, libdpx, monkeypatch):
        reward = make_reward(GATE)
        read = libdpx.DPxReadRam

        def corrupt(address, length):
            words = read(address, length)
            words[len(words) // 2] ^= 1  # one gate sample flipped
            return words

        monkeypatch.setattr(libdpx, "DPxReadRam", corrupt)
        with pytest.raises(RewardError, match="not started.*read back") as caught:
            reward.deliver(SHORT)
        assert not isinstance(caught.value, RewardUncertainError)  # nothing played
        assert libdpx.trains == [] and not libdpx.dout_start_pending and libdpx.dout_stops == 0
        assert libdpx.dout_register & 1  # rest

    def test_a_short_read_back_refuses_before_the_start(self, libdpx, monkeypatch):
        reward = make_reward(GATE)
        read = libdpx.DPxReadRam
        monkeypatch.setattr(libdpx, "DPxReadRam", lambda a, n: read(a, n)[:-1])
        with pytest.raises(RewardError, match="returned .* words, not"):
            reward.deliver(SHORT)
        assert libdpx.trains == [] and not libdpx.dout_start_pending

    def test_a_failed_chunk_refuses_before_the_start(self, libdpx, monkeypatch):
        reward = make_reward(GATE)
        write = libdpx.DPxWriteRam
        calls = []

        def second_chunk_fails(address, words):
            calls.append(address)
            write(address, words)
            if len(calls) == 2:
                libdpx.error = USB_GONE

        monkeypatch.setattr(libdpx, "DPxWriteRam", second_chunk_fails)
        with pytest.raises(RewardError, match="writing schedule words 16380-.*DPX_ERR_USB"):
            reward.deliver(one_pulse_of(2 * VENDOR_RAM_CHUNK_WORDS + 5))
        assert len(calls) == 2  # stopped at the failed chunk
        assert libdpx.trains == [] and not libdpx.dout_start_pending

    def test_a_truncating_write_is_caught_by_the_read_back(self, libdpx, monkeypatch):
        # A pypixxlib that drops the tail of a chunk silently.
        reward = make_reward(GATE)
        write = libdpx.DPxWriteRam
        monkeypatch.setattr(libdpx, "DPxWriteRam", lambda a, words: write(a, list(words)[:-1]))
        with pytest.raises(RewardError, match="read back"):
            reward.deliver(SHORT)
        assert libdpx.trains == []

    def test_an_enormous_train_is_refused_without_building_it(self, libdpx):
        reward = make_reward(GATE)
        huge = RewardPulses(n_pulses=10**12, pulse_ms=1000, inter_pulse_ms=1000)
        assert gate_word_count(huge) > MAX_GATE_WORDS
        started = time.perf_counter()
        with pytest.raises(RewardError, match="longer than"):
            reward.deliver(huge)
        assert time.perf_counter() - started < 1.0
        assert libdpx.ram_write_calls == [] and libdpx.trains == []
        with pytest.raises(ValueError, match="never shortened"):
            build_gate_words(huge, bit=0, polarity="active_low", port_value=0)

    def test_the_longest_train_is_accepted_and_one_more_word_is_not(self, libdpx):
        reward = make_reward(GATE)
        with pytest.raises(RewardError, match="longer than"):
            reward.deliver(one_pulse_of(MAX_GATE_WORDS + 1))
        assert libdpx.trains == []
        reward.deliver(one_pulse_of(MAX_GATE_WORDS))
        assert len(libdpx.trains[0]) == MAX_GATE_WORDS


# ----------------------------------------------------------------------
# M1: Pixel Mode B is never assumed off
# ----------------------------------------------------------------------


class TestPixelModeB:
    HIGH = SyncHwConfig(backend="datapixx", pulse_ms=0.5, event_lines={"A": "DATAPIXX3/dout17"})
    LOW = SyncHwConfig(backend="datapixx", pulse_ms=0.5, event_lines={"A": "DATAPIXX3/dout2"})

    def test_sync_on_upper_bits_is_refused_when_it_cannot_be_read(self, libdpx, monkeypatch):
        monkeypatch.delattr(FakeLibdpx, "DPxIsDoutPixelModeB")
        libdpx.firmware_rev = 30
        with pytest.raises(SyncError, match="DOUT 17 refused.*Pixel Mode B.*cannot be read"):
            make_sync(self.HIGH)
        assert libdpx.dout_writes == [] and libdpx.opens == libdpx.closes == 1

    def test_sync_on_lower_bits_does_not_depend_on_it(self, libdpx, monkeypatch):
        monkeypatch.delattr(FakeLibdpx, "DPxIsDoutPixelModeB")
        sync = make_sync(self.LOW)
        sync.close()

    def test_firmware_before_27_cannot_have_it(self, libdpx, monkeypatch):
        monkeypatch.delattr(FakeLibdpx, "DPxIsDoutPixelModeB")
        libdpx.firmware_rev = 26
        sync = make_sync(self.HIGH)
        sync.close()

    def test_a_readable_pixel_mode_b_on_refuses_the_upper_bits(self, libdpx):
        libdpx.pixel_mode_b = True
        with pytest.raises(SyncError, match="Pixel Mode"):
            make_sync(self.HIGH)
        make_sync(self.LOW).close()


# ----------------------------------------------------------------------
# M2: a gaze read's fault stays the tracker's
# ----------------------------------------------------------------------


def recording(monkeypatch):
    device = install_fake_pypixxlib(monkeypatch)
    clock = FakeClock(start=10.0)
    tracker = ViewPixxTracker(
        EyeTrackerConfig(backend="viewpixx"), None, SCREEN, clock, background_gaze=False
    )
    tracker.connect()
    tracker.configure(SCREEN, clock)
    tracker.start_trial(1, "attempt 1")
    return device, tracker, clock


class TestGazeReadFaults:
    def test_a_transient_fault_then_good_polls_is_still_a_dropout(self, monkeypatch):
        device, tracker, clock = recording(monkeypatch)
        reward = make_reward(GATE)
        device.libdpx.unplug()
        tracker._reader.read_now()
        device.libdpx.unplugged = False
        reward.deliver(SHORT)  # many successful selects and polls
        for _ in range(5):
            clock.advance(0.03)
            tracker._reader.read_now()  # good reads and checks afterwards
        detail = tracker.recording_fault()
        assert detail is not None and "gaze read" in detail and USB_GONE in detail
        reward.close()
        tracker.shutdown(None)

    def test_tracker_alone_reports_the_failed_read_at_once(self, monkeypatch):
        device, tracker, clock = recording(monkeypatch)
        device.libdpx.unplug()
        tracker._reader.read_now()
        device.libdpx.unplugged = False
        assert USB_GONE in tracker.recording_fault()
        tracker.shutdown(None)

    def test_the_next_trial_starts_clean_when_the_device_answers(self, monkeypatch):
        device, tracker, clock = recording(monkeypatch)
        device.libdpx.unplug()
        tracker._reader.read_now()
        device.libdpx.unplugged = False
        assert tracker.recording_fault() is not None
        tracker.stop_trial()
        tracker.start_trial(2, "attempt 1")  # its own device check supersedes
        clock.advance(0.01)
        assert tracker.recording_fault() is None
        tracker.shutdown(None)


# ----------------------------------------------------------------------
# m1: a pypixxlib without a call is a device error, never a crash
# ----------------------------------------------------------------------


class TestMissingCalls:
    def test_the_gate_names_the_missing_call_and_opens_nothing(self, libdpx, monkeypatch):
        monkeypatch.delattr(FakeLibdpx, "DPxReadRam")
        with pytest.raises(RewardError, match="lacks DPxReadRam.*Nothing was opened"):
            make_reward(GATE)
        assert libdpx.opens == 0

    def test_sync_names_the_missing_call(self, libdpx, monkeypatch):
        monkeypatch.delattr(FakeLibdpx, "DPxIsDoutBacklightPulse")
        cfg = SyncHwConfig(backend="datapixx", event_lines={"A": "DATAPIXX3/dout2"})
        with pytest.raises(SyncError, match="lacks DPxIsDoutBacklightPulse"):
            make_sync(cfg)
        assert libdpx.opens == 0

    def test_check_rig_reports_rather_than_crashes(self, libdpx, monkeypatch, tmp_path):
        from alhazen.config.models import DevicesConfig, MonitorConfig, RigConfig
        from alhazen.session.checks import _check_reward

        monkeypatch.delattr(FakeLibdpx, "DPxIsDoutButtonSchedules")
        rig = RigConfig(
            monitor=MonitorConfig(
                width_px=1920, height_px=1080, width_cm=52.0, distance_cm=57.0, refresh_rate_hz=120
            ),
            devices=DevicesConfig(reward=GATE),
            data_root=tmp_path,
        )
        result = _check_reward(rig, pulse=False)
        assert not result.ok and "DPxIsDoutButtonSchedules" in result.detail


# ----------------------------------------------------------------------
# m3: an unobserved end is not called a stuck schedule
# ----------------------------------------------------------------------


class TestCompletionNotObserved:
    def test_a_busy_link_past_the_deadline_is_unobserved_not_stuck(self, libdpx, monkeypatch):
        monkeypatch.setattr(reward_module, "_SCHEDULE_SLACK_S", 0.05)
        monkeypatch.setattr(reward_module, "_LINK_TIMEOUT_S", 0.05)
        reward = make_reward(GATE)
        link = reward._link
        holding = threading.Event()
        release = threading.Event()

        def hold_link() -> None:
            with link.lock:
                holding.set()
                release.wait(5)

        def first_wait_starts_the_holder(seconds: float) -> None:
            # The train is started and the lock released; another libdpx
            # user (a stuck drain, say) takes it before the first poll.
            if not holding.is_set():
                threading.Thread(target=hold_link, daemon=True).start()
                holding.wait(5)

        monkeypatch.setattr(reward_module.time, "sleep", first_wait_starts_the_holder)
        try:
            with pytest.raises(RewardUncertainError) as caught:
                reward.deliver(RewardPulses(n_pulses=1, pulse_ms=20, inter_pulse_ms=0))
        finally:
            release.set()
        message = str(caught.value)
        assert "could not be observed" in message and "confirmed stuck" not in message
        assert "UNCERTAIN" in message
        assert len(libdpx.trains) == 1  # played once, never again

    def test_a_briefly_busy_link_just_waits(self, libdpx, monkeypatch):
        reward = make_reward(GATE)
        link = reward._link
        start = libdpx.DPxStartDoutSched

        def start_then_busy() -> None:
            start()

            def hold() -> None:
                with link.lock:
                    time.sleep(0.05)

            threading.Thread(target=hold, daemon=True).start()

        monkeypatch.setattr(libdpx, "DPxStartDoutSched", start_then_busy)
        reward.deliver(SHORT)  # no error: it asked again once the link was free
        assert len(libdpx.trains) == 1

    def test_a_confirmed_stuck_schedule_says_so(self, libdpx, monkeypatch):
        monkeypatch.setattr(reward_module, "_SCHEDULE_SLACK_S", 0.02)
        reward = make_reward(GATE)
        libdpx.polls_until_done = None
        with pytest.raises(RewardUncertainError, match="confirmed stuck"):
            reward.deliver(SHORT)
        assert len(libdpx.trains) == 1 and libdpx.dout_stops == 1


# ----------------------------------------------------------------------
# m4: lifecycle on rare failures
# ----------------------------------------------------------------------


class TestLifecycle:
    def test_a_failed_select_after_the_open_closes_it_again(self, libdpx):
        libdpx.present = set()  # DPxOpen works, no DATAPixx3 answers
        with pytest.raises(RewardError, match="no DATAPIXX3 answered"):
            make_reward(GATE)
        assert (libdpx.opens, libdpx.closes) == (1, 1)
        link = dpx_link(libdpx)
        assert not link.opened and link.holders() == 0

    def test_close_on_a_busy_link_stays_closable(self, libdpx, monkeypatch):
        monkeypatch.setattr(reward_module, "_LINK_TIMEOUT_S", 0.05)
        reward = make_reward(GATE)
        link = reward._link
        link.lock.acquire()
        try:
            with pytest.raises(RewardError, match="still open; close\\(\\) may be called again"):
                reward.close()
            assert link.holders() == 1 and libdpx.closes == 0
        finally:
            link.lock.release()
        reward.close()
        assert link.holders() == 0 and libdpx.closes == 1
        reward.close()  # idempotent
        assert libdpx.closes == 1

    def test_sync_close_on_a_busy_link_stays_closable(self, libdpx, monkeypatch):
        from alhazen.devices import sync as sync_module

        monkeypatch.setattr(sync_module, "_LINK_TIMEOUT_S", 0.05)
        sync = make_sync(SyncHwConfig(backend="datapixx", event_lines={"A": "DATAPIXX3/dout2"}))
        link = dpx_link(libdpx)
        link.lock.acquire()
        try:
            sync.close()  # logs, does not raise, does not release
            assert link.holders() == 1
        finally:
            link.lock.release()
        sync.close()
        assert link.holders() == 0 and libdpx.closes == 1


# ----------------------------------------------------------------------
# m5: an uncertain delivery is recorded as uncertain
# ----------------------------------------------------------------------


class Uncertain(SimulatedReward):
    def deliver(self, pulses) -> None:
        raise RewardUncertainError("train started, then the USB link failed (test)")


class TestUncertainRecords:
    def test_end_of_trial_failure_is_uncertain_and_not_a_zero_delivery(self, tmp_path):
        from test_subject_kind import OutcomeTask, build, events, monkey, rows

        build(tmp_path, OutcomeTask(monkey()), reward_line=GATE, reward=Uncertain()).run()
        (first, *_) = events(tmp_path, "REWARD_FAILED")
        import json

        payload = json.loads(first["payload_json"]) if "payload_json" in first else first
        assert payload["uncertain"] is True
        assert "RewardUncertainError: train started" in payload["error"]
        paid = [r for r in rows(tmp_path) if r["outcome"] == "HIT"]
        assert paid and all(r.get("rewarded", "") == "" for r in paid)  # unknown, not "False"

    def test_a_queued_drop_reports_uncertainty(self):
        queued = QueuedReward(Uncertain())
        queued.submit(RewardRequest(pulses=SHORT, reason="drop", frame=3))
        queued.wait_idle()
        (done,) = queued.completed()
        assert done.uncertain is True and "RewardUncertainError" in done.error
        queued.close()

    def test_a_plain_failure_stays_a_failure(self):
        class Plain(SimulatedReward):
            def deliver(self, pulses) -> None:
                raise RewardError("refused before anything played (test)")

        queued = QueuedReward(Plain())
        queued.submit(RewardRequest(pulses=SHORT, reason="drop", frame=3))
        queued.wait_idle()
        (done,) = queued.completed()
        assert done.uncertain is False
        queued.close()
