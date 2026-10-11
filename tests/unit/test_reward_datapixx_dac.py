"""The DATAPixx3 analog-output (DAC) reward gate, and DATAPixx3 reward with
DATAPixx3 sync on one device.

Against tests/fake_sdk.py's DAC model, written from pypixxlib's published
_libdpx source (DPxSetDacValue(value, channel), signed DPxGetDacValue,
DPxSetDacSched's c_uint arguments, TPxGetAllDACOutput's broken wrapper) and,
where VPixx documents nothing, conservative (a stopped or finished schedule
leaves the pin at its last sample). TestTheDacStandIn pins the model; a
green stand-in is not evidence of the SDK or the pins.
"""

from __future__ import annotations

import ctypes
import math
import sys
from pathlib import Path

import pytest

from alhazen.config.models import (
    DATAPIXX_DAC_BUFFER_ADDRESS,
    DATAPIXX_DOUT_BUFFER_ADDRESS,
    DevicesConfig,
    EyeTrackerConfig,
    RewardHwConfig,
    RewardPulses,
    SyncHwConfig,
)
from alhazen.devices import reward as reward_module
from alhazen.devices.eyetracker import ViewPixxTracker
from alhazen.devices.reward import (
    VENDOR_RAM_CHUNK_WORDS,
    build_dac_words,
    dac_code,
    dac_volts,
    make_reward,
    probe_datapixx_gate,
)
from alhazen.devices.sync import make_sync
from alhazen.devices.vpixx import dpx_link
from alhazen.errors import RewardError, RewardUncertainError, SyncError
from alhazen.testing import FakeClock
from fake_sdk import USB_GONE, FakeLibdpx, install_fake_libdpx, install_fake_pypixxlib
from support import SCREEN

PUMP = {"model": "WPI SP220i", "syringe_diameter_mm": 14.5, "rate_ul_per_min": 3000.0}
DAC = RewardHwConfig(backend="datapixx", channel="dac0", polarity="active_low", pump=PUMP)
DOUT = RewardHwConfig(backend="datapixx", channel="dout0", polarity="active_low", pump=PUMP)
SYNC = SyncHwConfig(
    backend="datapixx",
    pulse_ms=0.5,
    event_lines={"TRIAL_START": "DATAPIXX3/dout2", "TRIAL_END": "DATAPIXX3/dout17"},
)
SHORT = RewardPulses(n_pulses=2, pulse_ms=3, inter_pulse_ms=2)
SYNC_MASK = (1 << 2) | (1 << 17)


def dac(**changes) -> RewardHwConfig:
    return RewardHwConfig(**{**DAC.model_dump(exclude_unset=True), **changes})


def vendor_code(volts: float, low: float, high: float) -> int:
    """pypixxlib's DPxWriteDacBuffer conversion, restated."""
    code = math.floor((volts - low) / (high - low) * 65536 - 32768 + 0.5)
    return max(-32768, min(32767, code))


@pytest.fixture
def libdpx(monkeypatch) -> FakeLibdpx:
    lib = install_fake_libdpx(monkeypatch)
    monkeypatch.setattr(reward_module.time, "sleep", lambda s: None)
    return lib


# ----------------------------------------------------------------------
# The stand-in's DAC model
# ----------------------------------------------------------------------


class TestTheDacStandIn:
    def test_static_value_is_value_then_channel_and_reads_back_signed(self):
        lib = FakeLibdpx()
        lib.DPxSetDacValue(-1, 2)
        assert lib.dac_static[2] == 0xFFFF and lib.DPxGetDacValue(2) == -1
        with pytest.raises(AssertionError):
            lib.DPxSetDacValue(0, 5)  # channel 5: the swapped-argument mistake

    def test_set_voltage_uses_the_vendor_conversion(self):
        lib = FakeLibdpx()
        lib.dac_range[2] = (-5.0, 5.0)
        lib.DPxSetDacVoltage(5.0, 2)
        assert lib.DPxGetDacValue(2) == 32767  # clamped, 4.99985 V

    def test_a_float_onset_is_a_ctypes_error(self):
        with pytest.raises(ctypes.ArgumentError):
            FakeLibdpx().DPxSetDacSched(0.0, 1000, "hz", 10)

    def test_the_published_tracker_dac_getter_raises(self):
        lib = FakeLibdpx()
        lib.dac_tpx_getter_broken = True
        with pytest.raises(TypeError):
            lib.TPxGetAllDACOutput()

    def test_a_finished_schedule_leaves_the_pin_at_its_last_sample(self):
        lib = FakeLibdpx()
        lib.DPxWriteRam(100, [5, 6, 7])
        lib.DPxEnableDacBuffChan(1)
        lib.DPxSetDacBuff(100, 6)
        lib.DPxSetDacSched(0, 1000, "hz", 3)
        lib.DPxStartDacSched()
        lib.DPxUpdateRegCache()
        lib.DPxUpdateRegCache()
        assert lib.dac_trains == [((1,), [5, 6, 7])] and lib.dac_pin[1] == 7


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------


class TestConfig:
    def test_a_dac_gate(self):
        assert DAC.line == "DATAPIXX3/dac0" and DAC.voltage == 5.0
        assert DAC.dac_buffer_address == DATAPIXX_DAC_BUFFER_ADDRESS
        assert DAC.calibration_identity() == {
            "backend": "datapixx",
            "polarity": "active_low",
            "pump": PUMP,
        }
        dumped = RewardHwConfig(**DAC.model_dump())
        assert dumped == DAC  # a session snapshot loads again

    @pytest.mark.parametrize("volts", [2.01, 3.3, 5.0])
    def test_high_levels_in_the_pumps_band(self, volts):
        assert dac(voltage=volts).voltage == volts

    @pytest.mark.parametrize("volts", [2.0, 1.0, 0.0, -1.0, 5.01, 10.0, float("nan")])
    def test_any_other_high_level_is_refused(self, volts):
        with pytest.raises(ValueError, match="must be above 2 V and at most 5 V"):
            dac(voltage=volts)

    @pytest.mark.parametrize("channel", ["dac4", "dac", "ao0", "dacx"])
    def test_only_dac0_to_dac3(self, channel):
        with pytest.raises(ValueError, match="needs channel"):
            dac(channel=channel)

    def test_each_buffer_field_belongs_to_its_own_output(self):
        with pytest.raises(ValueError, match="dout_buffer_address belongs to a digital"):
            dac(dout_buffer_address=4_000_000)
        with pytest.raises(ValueError, match="dac_buffer_address belongs to an analog"):
            RewardHwConfig(
                **{**DOUT.model_dump(exclude_unset=True), "dac_buffer_address": 12_000_000}
            )
        with pytest.raises(ValueError, match="ignores dac_buffer_address"):
            RewardHwConfig(backend="nidaq", dac_buffer_address=12_000_000)

    @pytest.mark.parametrize(
        ("address", "what"),
        [
            (DATAPIXX_DOUT_BUFFER_ADDRESS, "digital-output gate's buffer"),
            (DATAPIXX_DOUT_BUFFER_ADDRESS - 1_000_000, "digital-output gate's buffer"),
            (0x12000000, "TRACKPixx3 sample ring"),
            (0x75EEC000, "TRACKPixx3 camera image"),
        ],
    )
    def test_the_dac_buffer_stays_off_other_regions(self, address, what):
        with pytest.raises(ValueError, match=what):
            dac(dac_buffer_address=address)

    def test_an_odd_or_negative_dac_buffer_is_refused(self):
        for address in (10_000_001, -2):
            with pytest.raises(ValueError, match="even byte address"):
                dac(dac_buffer_address=address)

    def test_a_dac_gate_shares_the_device_with_datapixx_sync(self):
        DevicesConfig(reward=DAC, sync=SYNC)  # accepted

    def test_a_dout_gate_still_does_not(self):
        with pytest.raises(ValueError, match="cannot share"):
            DevicesConfig(reward=DOUT, sync=SYNC)


# ----------------------------------------------------------------------
# Codes and samples
# ----------------------------------------------------------------------


class TestCodes:
    @pytest.mark.parametrize("volts", [0.0, 2.5, 3.3, 4.5, 5.0])
    @pytest.mark.parametrize("span", [(-10.0, 10.0), (-5.0, 5.0)])
    def test_codes_are_pypixxlibs(self, volts, span):
        assert dac_code(volts, *span) == vendor_code(volts, *span)

    def test_known_codes(self):
        assert dac_code(0.0, -10, 10) == 0 and dac_code(5.0, -10, 10) == 16384
        assert dac_code(5.0, -5, 5) == 32767
        assert dac_volts(16384, -10, 10) == 5.0

    def test_samples_are_only_the_two_levels_and_start_and_end_at_rest(self):
        words = build_dac_words(SHORT, rest=16384, active=0)
        assert words[0] == words[-1] == 16384
        assert set(words) == {16384, 0}
        assert words == [16384, 0, 0, 0, 16384, 16384, 0, 0, 0, 16384, 16384, 16384]


# ----------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------


class TestDelivery:
    def test_a_train_plays_on_its_channel_alone_and_touches_no_digital_output(self, libdpx):
        reward = make_reward(DAC)
        reward.deliver(SHORT)
        expected = build_dac_words(SHORT, rest=16384, active=0)
        assert libdpx.dac_trains == [((0,), expected)]
        assert libdpx.dac_sched == (0, 1000, "hz", len(expected))
        assert libdpx.dac_buff == (DATAPIXX_DAC_BUFFER_ADDRESS, 2 * len(expected))
        assert {channel for _, channel in libdpx.dac_static_writes} == {0}
        assert {code for code, _ in libdpx.dac_static_writes} == {16384}  # only rest, ever
        assert libdpx.dout_writes == [] and libdpx.trains == []
        assert libdpx.dac_static[0] == 16384 and libdpx.dac_pin[0] == 16384
        reward.close()
        assert (libdpx.opens, libdpx.closes) == (1, 1)

    def test_active_high_rests_at_0_v(self, libdpx):
        reward = make_reward(dac(polarity="active_high", voltage=3.3))
        reward.deliver(SHORT)
        high = vendor_code(3.3, -10, 10)
        assert set(libdpx.dac_trains[0][1]) == {0, high}
        assert libdpx.dac_trains[0][1][0] == 0 and libdpx.dac_static[0] == 0
        reward.close()

    def test_a_5_v_channel_clamps_and_reads_back_within_one_code(self, libdpx, monkeypatch):
        libdpx.dac_range[2] = (-5.0, 5.0)
        reward = make_reward(dac(channel="dac2"))
        assert reward.preflight["rest_code"] == 32767
        get = libdpx.DPxGetDacValue
        monkeypatch.setattr(libdpx, "DPxGetDacValue", lambda ch: get(ch) - 1)
        reward.deliver(SHORT)  # one code off: accepted
        monkeypatch.setattr(libdpx, "DPxGetDacValue", lambda ch: get(ch) - 2)
        with pytest.raises(RewardUncertainError, match="reads code 0x7ffd after being set 0x7fff"):
            reward.deliver(SHORT)  # two codes off: the rest check before the train refuses
        assert len(libdpx.dac_trains) == 1  # only the accepted one played

    @pytest.mark.parametrize("words", [VENDOR_RAM_CHUNK_WORDS + 1, 40_002])
    def test_long_trains_are_uploaded_in_chunks_and_play_exactly(self, libdpx, words):
        reward = make_reward(DAC)
        pulses = RewardPulses(n_pulses=1, pulse_ms=words - 2, inter_pulse_ms=0)
        reward.deliver(pulses)
        assert libdpx.dac_trains[0][1] == build_dac_words(pulses, rest=16384, active=0)
        assert all(n <= VENDOR_RAM_CHUNK_WORDS for n in libdpx.ram_write_calls)
        reward.close()

    def test_a_corrupt_read_back_refuses_before_the_start(self, libdpx, monkeypatch):
        reward = make_reward(DAC)
        read = libdpx.DPxReadRam

        def corrupt(address, length):
            found = read(address, length)
            found[1] ^= 1
            return found

        monkeypatch.setattr(libdpx, "DPxReadRam", corrupt)
        with pytest.raises(RewardError, match="not started") as caught:
            reward.deliver(SHORT)
        assert not isinstance(caught.value, RewardUncertainError)
        assert libdpx.dac_trains == [] and libdpx.dac_sched is None

    def test_buffering_that_does_not_read_back_refuses_before_the_start(self, libdpx, monkeypatch):
        reward = make_reward(DAC)
        monkeypatch.setattr(libdpx, "DPxEnableDacBuffChan", lambda channel: None)
        with pytest.raises(RewardError, match="not started.*buffering reads back"):
            reward.deliver(SHORT)
        assert libdpx.dac_trains == []


class TestRefusals:
    """Each refused before any analog-output write: nothing written to the
    DAC, nothing to RAM, device released."""

    @staticmethod
    def refused(libdpx, cfg=DAC, match=""):
        with pytest.raises(RewardError, match=match):
            make_reward(cfg)
        assert libdpx.dac_static_writes == [] and libdpx.ram_write_calls == []
        assert libdpx.opens == libdpx.closes

    def test_loopback(self, libdpx):
        libdpx.dac_loopback = True
        self.refused(libdpx, match="loopback")

    def test_raw_calibration(self, libdpx):
        libdpx.dac_calib_raw = True
        self.refused(libdpx, match="raw mode")

    def test_a_running_dac_schedule(self, libdpx):
        libdpx.dac_running = True
        self.refused(libdpx, match="DAC schedule is already running")

    def test_another_buffered_channel(self, libdpx):
        libdpx.dac_buffered = {3}
        self.refused(libdpx, match=r"channel\(s\) \[3\]")
        assert libdpx.dac_buffered == {3}  # never switched off

    def test_the_tracker_driving_the_channel(self, libdpx):
        libdpx.tpx_dac_mode = 7  # DAC0: average eye screen X (inferred layout)
        self.refused(libdpx, match="analog output is on.*eye data")

    @pytest.mark.parametrize("word", [7 << 8, 7 << 16, 7 << 24, 0x80000000])
    def test_the_tracker_driving_any_dac_refuses(self, libdpx, word):
        # The mode word's layout is undocumented: any non-zero word refuses,
        # whichever channel the gate is on.
        libdpx.tpx_dac_mode = word
        self.refused(libdpx, match="not assumed free")

    def test_tracker_analog_output_off_opens(self, libdpx):
        libdpx.tpx_dac_mode = 0
        make_reward(DAC).close()

    def test_an_unreadable_tracker_analog_mode(self, libdpx):
        libdpx.dac_tpx_getter_broken = True
        self.refused(libdpx, match="cannot be read.*Not assumed off")

    def test_a_range_without_the_gate_level(self, libdpx):
        libdpx.dac_range[0] = (-2.0, 2.0)
        self.refused(libdpx, match="does not hold 0 V and the gate's 5 V")

    def test_a_channel_the_device_lacks(self, libdpx, monkeypatch):
        monkeypatch.setattr(libdpx, "DPxGetDacNumChans", lambda: 2)
        self.refused(libdpx, dac(channel="dac3"), match="2 DAC channel")

    def test_a_buffer_past_the_ram(self, libdpx):
        libdpx.ram_size = DATAPIXX_DAC_BUFFER_ADDRESS
        self.refused(libdpx, match="runs past the device's RAM")

    def test_a_buffer_on_the_live_tracker_ring(self, libdpx, monkeypatch):
        monkeypatch.setattr(libdpx, "TPxGetBuffBaseAddr", lambda: DATAPIXX_DAC_BUFFER_ADDRESS)
        monkeypatch.setattr(libdpx, "TPxGetBuffSize", lambda: 4096)
        self.refused(libdpx, match="overlaps the TRACKPixx3 sample ring")

    def test_a_missing_call_opens_nothing(self, libdpx, monkeypatch):
        monkeypatch.delattr(FakeLibdpx, "DPxIsDacAdcLoopback")
        with pytest.raises(RewardError, match="lacks DPxIsDacAdcLoopback.*Nothing was opened"):
            make_reward(DAC)
        assert libdpx.opens == 0


class TestUncertain:
    def test_a_fault_while_polling_stops_once_writes_rest_and_never_replays(
        self, libdpx, monkeypatch
    ):
        reward = make_reward(DAC)
        libdpx.polls_until_done = 3
        running = libdpx.DPxIsDacSchedRunning

        def flaky() -> int:
            if libdpx.dac_running:
                libdpx.error = USB_GONE
            return running()

        monkeypatch.setattr(libdpx, "DPxIsDacSchedRunning", flaky)
        with pytest.raises(RewardUncertainError, match="NOT retried"):
            reward.deliver(SHORT)
        assert len(libdpx.dac_trains) == 1 and libdpx.dac_stops == 1
        # The stop left the pin mid-train (active); rest was written after it.
        assert libdpx.dac_pin[0] == 16384 and libdpx.dac_static[0] == 16384
        reward.close()

    def test_a_stuck_schedule_is_stopped_and_uncertain(self, libdpx, monkeypatch):
        monkeypatch.setattr(reward_module, "_SCHEDULE_SLACK_S", 0.02)
        reward = make_reward(DAC)
        libdpx.polls_until_done = None
        with pytest.raises(RewardUncertainError, match="confirmed stuck"):
            reward.deliver(SHORT)
        assert libdpx.dac_stops == 1 and libdpx.dac_pin[0] == 16384

    def test_a_rest_that_will_not_stick_after_a_train(self, libdpx, monkeypatch):
        reward = make_reward(DAC)
        start = libdpx.DPxStartDacSched

        def start_then_break() -> None:
            start()
            monkeypatch.setattr(libdpx, "DPxGetDacValue", lambda ch: 0)

        monkeypatch.setattr(libdpx, "DPxStartDacSched", start_then_break)
        with pytest.raises(RewardUncertainError, match="LINE STATE UNCERTAIN"):
            reward.deliver(SHORT)
        assert len(libdpx.dac_trains) == 1

    def test_close_stops_its_own_running_schedule_then_rests(self, libdpx):
        reward = make_reward(DAC)
        libdpx.dac_running = True
        libdpx.dac_pin[0] = 0
        reward.close()
        assert libdpx.dac_stops == 1 and libdpx.dac_pin[0] == 16384


# ----------------------------------------------------------------------
# DAC gate + DATAPixx3 sync on one device
# ----------------------------------------------------------------------


class TestSharingWithSync:
    def test_reward_and_sync_interleave_without_touching_each_other(self, libdpx):
        reward = make_reward(DAC)
        sync = make_sync(SYNC)
        for _ in range(3):
            sync.pulse("DATAPIXX3/dout2")
            reward.deliver(SHORT)
            sync.pulse("DATAPIXX3/dout17")
        # Every sync write masks the sync bits and only them.
        assert {mask for _, mask in libdpx.dout_writes} == {SYNC_MASK}
        assert {value for value, _ in libdpx.dout_writes} == {0, 1 << 2, 1 << 17}
        assert libdpx.trains == []  # no DOUT schedule, ever
        assert [channels for channels, _ in libdpx.dac_trains] == [(0,)] * 3
        assert {channel for _, channel in libdpx.dac_static_writes} == {0}
        assert libdpx.dout_register & SYNC_MASK == 0
        reward.close()
        sync.close()
        assert (libdpx.opens, libdpx.closes) == (1, 1)

    def test_a_dout_gate_and_sync_are_still_refused_at_run_time(self, libdpx):
        reward = make_reward(DOUT)
        with pytest.raises(SyncError):
            make_sync(SYNC)
        reward.close()

    def test_two_dac_gates_cannot_share_the_one_dac_schedule(self, libdpx):
        first = make_reward(DAC)
        with pytest.raises(RewardError, match="DAC schedule is already used"):
            make_reward(dac(channel="dac1"))
        first.close()
        assert dpx_link(libdpx).holders() == 0

    def test_the_tracker_first_then_gate_and_sync(self, monkeypatch):
        device = install_fake_pypixxlib(monkeypatch)
        monkeypatch.setattr(reward_module.time, "sleep", lambda s: None)
        tracker = ViewPixxTracker(
            EyeTrackerConfig(backend="viewpixx"), None, SCREEN, FakeClock(), background_gaze=False
        )
        tracker.connect()
        reward, sync = make_reward(DAC), make_sync(SYNC)
        reward.deliver(SHORT)
        sync.pulse("DATAPIXX3/dout2")
        tracker.shutdown(None)
        sync.close()
        reward.close()
        assert (device.libdpx.opens, device.libdpx.closes) == (1, 1)


def session(tmp_path, *, subject_kind="monkey"):
    from test_subject_kind import MONITOR, OutcomeTask, human, monkey

    from alhazen.config.models import DisplayConfig, Duration, RigConfig
    from alhazen.session.builder import build_session

    rig = RigConfig(
        monitor=MONITOR,
        display=DisplayConfig(backend="simulated"),
        devices=DevicesConfig(
            eyetracker=EyeTrackerConfig(backend="viewpixx"), reward=DAC, sync=SYNC
        ),
        data_root=tmp_path,
    )
    short = {"n_pulses": 1, "pulse_ms": 2, "inter_pulse_ms": 0}
    params = monkey(by_outcome={"HIT": short}) if subject_kind == "monkey" else human()
    return build_session(
        rig=rig,
        subject="t01",
        session=1,
        run=1,
        task=OutcomeTask(params),
        seed=1,
        iti=Duration(ms=0),
        simulated_frame_period_s=0.0,
        date_yyyymmdd="20261010",
    )


class TestRealSession:
    def test_tracker_dac_gate_and_sync_in_one_session(self, tmp_path, monkeypatch):
        device = install_fake_pypixxlib(monkeypatch)
        lib = device.libdpx
        runner = session(tmp_path)
        assert (lib.opens, lib.closes) == (1, 0)  # the tracker's open, joined by both
        runner.run()
        assert (lib.opens, lib.closes) == (1, 1)
        assert lib.dac_trains, "no reward reached the DAC"
        assert lib.dout_writes, "no sync pulse reached the digital outputs"
        assert all(mask & ~SYNC_MASK == 0 for _, mask in lib.dout_writes)
        assert lib.trains == []
        assert {channels for channels, _ in lib.dac_trains} == {(0,)}
        assert {channel for _, channel in lib.dac_static_writes} == {0}
        assert lib.dac_static[0] == 16384 and lib.dout_register & SYNC_MASK == 0

    def test_a_human_session_never_opens_the_dac_gate(self, tmp_path, monkeypatch):
        device = install_fake_pypixxlib(monkeypatch)
        session(tmp_path, subject_kind="human").run()
        assert device.libdpx.dac_static_writes == [] and device.libdpx.dac_trains == []


# ----------------------------------------------------------------------
# check-rig, Measure rig, simulate
# ----------------------------------------------------------------------


class TestChecksAndModes:
    def test_check_rig_without_pulse(self, libdpx, tmp_path):
        from alhazen.config.models import MonitorConfig, RigConfig
        from alhazen.session.checks import _check_reward

        rig = RigConfig(
            monitor=MonitorConfig(
                width_px=1920, height_px=1080, width_cm=52.0, distance_cm=57.0, refresh_rate_hz=120
            ),
            devices=DevicesConfig(reward=DAC),
            data_root=tmp_path,
        )
        result = _check_reward(rig, pulse=False)
        assert result.ok, result.detail
        assert "No digital output is touched" in result.evidence["notes"][0]
        assert result.evidence["preflight"]["levels_v"] == {"high": 5.0, "low": 0.0}
        assert libdpx.dac_trains == [] and libdpx.dout_writes == []

    def test_measure_rig_connection_probe_changes_nothing(self, libdpx):
        state = probe_datapixx_gate(DAC)
        assert state["refusal"] is None
        assert libdpx.dac_static_writes == [] and libdpx.ram_write_calls == []
        libdpx.dac_loopback = True
        assert "loopback" in probe_datapixx_gate(DAC)["refusal"]

    def test_simulate_stands_the_dac_gate_down_with_its_identity(self, monkeypatch):
        from alhazen.config.loader import load_rig
        from alhazen.devices.reward import SimulatedReward
        from alhazen.modes import Mode
        from alhazen.modes.session import rig_for_mode

        monkeypatch.delitem(sys.modules, "pypixxlib", raising=False)
        import alhazen

        rig = load_rig(str(Path(alhazen.__file__).parent / "rigs" / "rig-laptop.yaml"))
        rig = rig.model_copy(
            update={"devices": rig.devices.model_copy(update={"reward": DAC, "sync": SYNC})}
        )
        simulated, _notes = rig_for_mode(Mode.SIMULATE, rig)
        line = simulated.devices.reward
        assert line.backend == "simulated" and isinstance(make_reward(line), SimulatedReward)
        assert line.calibration_identity() == DAC.calibration_identity()
        assert "pypixxlib" not in sys.modules
