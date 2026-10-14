"""Independent adverse-path tests for the DATAPixx3 analog-output (DAC)
reward gate and its coexistence with DATAPixx3 sync lines.

Written by the independent verification (worker record dacVerification),
not the implementation, against the contract at workers/dacBackend/contract
(feature/datapixx-dac-reward-2026-10-10 @ ba4fbfc). They run on
tests/fake_sdk.py's FakeLibdpx, whose DAC model was checked against
pypixxlib's published _libdpx source (findings D1-D9), with a call recorder
of our own on top, so the assertions are about the order and arguments of
the calls alhazen makes, not about the stand-in's bookkeeping.

Three of these reproduced review findings against ba4fbfc (V-m1 channel
lockout, V-m2 stop and rest in separate register writes, V-m3 silent active
gate) and pass since the fixes that followed.
Nothing here is evidence about the pins, the pump or the SDK on the rig.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import ValidationError

from alhazen.config.models import (
    DevicesConfig,
    EyeTrackerConfig,
    RewardHwConfig,
    RewardPulses,
    SyncHwConfig,
)
from alhazen.devices import reward as reward_module
from alhazen.devices.reward import make_reward
from alhazen.devices.sync import make_sync
from alhazen.errors import RewardError, RewardUncertainError, SyncError
from fake_sdk import USB_GONE, FakeLibdpx, install_fake_libdpx, install_fake_pypixxlib

PUMP = {"model": "WPI SP220i", "syringe_diameter_mm": 14.5, "rate_ul_per_min": 3000.0}
DAC0 = {"backend": "datapixx", "channel": "dac0", "polarity": "active_low", "pump": PUMP}
SYNC = SyncHwConfig(
    backend="datapixx",
    pulse_ms=0.5,
    event_lines={"TRIAL_START": "DATAPIXX3/dout0", "TRIAL_END": "DATAPIXX3/dout17"},
)
SYNC_MASK = (1 << 0) | (1 << 17)
SHORT = RewardPulses(n_pulses=3, pulse_ms=4, inter_pulse_ms=3)
DAC_REGION = (10_000_000, 10_000_000 + (1 << 20))

DOUT_CALLS = {
    "DPxSetDoutValue",
    "DPxSetDoutBuff",
    "DPxSetDoutSched",
    "DPxStartDoutSched",
    "DPxStopDoutSched",
}
RECORDED = DOUT_CALLS | {
    "DPxStopAllScheds",
    "DPxSetDacValue",
    "DPxSetDacVoltage",
    "DPxEnableDacBuffChan",
    "DPxDisableDacBuffChan",
    "DPxDisableDacBuffAllChans",
    "DPxSetDacBuff",
    "DPxSetDacSched",
    "DPxStartDacSched",
    "DPxStopDacSched",
    "DPxWriteRam",
    "DPxReadRam",
    "DPxUpdateRegCache",
    "DPxWriteRegCache",
    "DPxOpen",
    "DPxClose",
}


def cfg(**changes: Any) -> RewardHwConfig:
    return RewardHwConfig(**{**DAC0, **changes})


def volts(code: int, low: float, high: float) -> float:
    """What a 16-bit DAC code (unsigned, as RAM holds it) stands for:
    pypixxlib's conversion inverted, written here independently."""
    signed = code - 0x10000 if code >= 0x8000 else code
    return (signed + 32768) / 65536 * (high - low) + low


def record(lib: FakeLibdpx, monkeypatch) -> list[tuple[str, tuple]]:
    """Wrap the stand-in's output calls so every one lands in one ordered
    log, with its arguments."""
    log: list[tuple[str, tuple]] = []
    for name in RECORDED:
        original = getattr(lib, name, None)
        if original is None:
            continue

        def wrapper(*args, _name=name, _original=original):
            log.append((_name, tuple(args)))
            return _original(*args)

        monkeypatch.setattr(lib, name, wrapper)
    return log


def names(log) -> list[str]:
    return [name for name, _ in log]


@pytest.fixture
def lib(monkeypatch) -> FakeLibdpx:
    lib = install_fake_libdpx(monkeypatch)
    monkeypatch.setattr(reward_module.time, "sleep", lambda s: None)
    return lib


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------


class TestConfig:
    @pytest.mark.parametrize("voltage", [5.0000001, 5.5, 10.0, 2.0, 1.0, 0.0, -5.0])
    def test_a_voltage_outside_the_pumps_high_band_is_refused(self, voltage):
        with pytest.raises(ValidationError):
            cfg(voltage=voltage)

    @pytest.mark.parametrize("voltage", [math.nan, math.inf, -math.inf])
    def test_a_non_finite_voltage_is_refused(self, voltage):
        with pytest.raises(ValidationError):
            cfg(voltage=voltage)

    def test_a_dout_gate_takes_no_voltage(self):
        with pytest.raises(ValidationError):
            cfg(channel="dout0", voltage=3.3)

    def test_a_dac_gate_with_datapixx_sync_is_allowed(self):
        DevicesConfig(reward=cfg(), sync=SYNC)

    def test_a_dout_gate_with_datapixx_sync_is_still_refused(self):
        with pytest.raises(ValidationError):
            DevicesConfig(reward=cfg(channel="dout0"), sync=SYNC)


# ----------------------------------------------------------------------
# What the DAC gate drives
# ----------------------------------------------------------------------


class TestOnlyTheDac:
    def test_the_dac_gate_never_touches_the_digital_outputs(self, lib, monkeypatch):
        log = record(lib, monkeypatch)
        gate = make_reward(cfg())
        for _ in range(3):
            gate.deliver(SHORT)
        lib.polls_until_done = 10**9  # the next train never ends: the uncertain path
        with pytest.raises(RewardUncertainError):
            gate.deliver(SHORT)
        gate.close()
        assert not DOUT_CALLS & set(names(log))
        assert "DPxStopAllScheds" not in names(log)
        assert lib.dout_writes == [] and lib.trains == []
        for name, args in log:
            if name == "DPxWriteRam":
                address, words = args
                assert DAC_REGION[0] <= address and address + 2 * len(words) <= DAC_REGION[1]
            if name in ("DPxSetDacValue", "DPxEnableDacBuffChan"):
                assert args[-1] == 0, f"{name} on a channel other than DAC0: {args}"

    @pytest.mark.parametrize(
        ("channel", "span", "voltage", "polarity"),
        [
            ("dac0", (-10.0, 10.0), 5.0, "active_low"),
            ("dac0", (-10.0, 10.0), 3.3, "active_high"),
            ("dac1", (-10.0, 10.0), 2.5, "active_low"),
            ("dac2", (-5.0, 5.0), 5.0, "active_low"),
            ("dac3", (-5.0, 5.0), 5.0, "active_high"),
            ("dac0", (0.0, 10.0), 5.0, "active_low"),
        ],
    )
    def test_every_sample_is_one_of_two_levels_inside_0_to_voltage(
        self, lib, channel, span, voltage, polarity
    ):
        number = int(channel[-1])
        lib.dac_range[number] = span
        gate = make_reward(cfg(channel=channel, voltage=voltage, polarity=polarity))
        gate.deliver(SHORT)
        gate.deliver(RewardPulses(n_pulses=1, pulse_ms=1, inter_pulse_ms=0))
        gate.close()
        one_code = (span[1] - span[0]) / 65536
        assert lib.dac_trains
        for channels, words in lib.dac_trains:
            assert channels == (number,)
            levels = sorted({volts(w, *span) for w in words})
            assert len(levels) == 2, levels
            low, high = levels
            assert -one_code / 2 <= low <= 0.5 and 2.0 < high <= voltage + one_code / 2
            rest = high if polarity == "active_low" else low
            assert volts(words[0], *span) == rest and volts(words[-1], *span) == rest
        rest_code = lib.dac_static[number]
        wanted = voltage if polarity == "active_low" else 0.0
        assert abs(volts(rest_code, *span) - wanted) <= one_code

    def test_a_long_train_is_uploaded_in_whole_chunks_and_plays_exactly(self, lib, monkeypatch):
        log = record(lib, monkeypatch)
        gate = make_reward(cfg())
        pulses = RewardPulses(n_pulses=40, pulse_ms=500, inter_pulse_ms=500)  # 40,002 words
        gate.deliver(pulses)
        gate.close()
        writes = [args for name, args in log if name == "DPxWriteRam"]
        assert writes and all(len(words) <= 16_380 for _, words in writes)
        ((_, played),) = lib.dac_trains
        assert len(played) == 40_002


# ----------------------------------------------------------------------
# DAC reward + DATAPixx3 sync in one real session
# ----------------------------------------------------------------------


def build(tmp_path, subject_kind: str = "monkey"):
    from test_subject_kind import MONITOR, OutcomeTask, human, monkey

    from alhazen.config.models import DisplayConfig, Duration, RigConfig
    from alhazen.session.builder import build_session

    rig = RigConfig(
        monitor=MONITOR,
        display=DisplayConfig(backend="simulated"),
        devices=DevicesConfig(
            eyetracker=EyeTrackerConfig(backend="viewpixx"), reward=cfg(), sync=SYNC
        ),
        data_root=tmp_path,
    )
    drop = {"n_pulses": 2, "pulse_ms": 3, "inter_pulse_ms": 2}
    params = monkey(by_outcome={"HIT": drop}) if subject_kind == "monkey" else human()
    return build_session(
        rig=rig,
        subject="r01",
        session=1,
        run=1,
        task=OutcomeTask(params),
        seed=3,
        iti=Duration(ms=0),
        simulated_frame_period_s=0.0,
        date_yyyymmdd="20261010",
    )


class TestOneSession:
    def test_tracker_dac_reward_and_sync_on_dout0_and_dout17(self, tmp_path, monkeypatch):
        device = install_fake_pypixxlib(monkeypatch)
        lib = device.libdpx
        monkeypatch.setattr(reward_module.time, "sleep", lambda s: None)
        runner = build(tmp_path)
        log = record(lib, monkeypatch)
        runner.run()
        calls = names(log)
        # Both happened, interleaved: a sync write before and after a DAC train.
        starts = [i for i, n in enumerate(calls) if n == "DPxStartDacSched"]
        syncs = [i for i, n in enumerate(calls) if n == "DPxSetDoutValue"]
        assert starts and syncs
        assert min(syncs) < starts[0] < max(syncs)
        # The gate never reached the digital outputs; sync only its own bits.
        assert not {"DPxSetDoutBuff", "DPxSetDoutSched", "DPxStartDoutSched"} & set(calls)
        assert "DPxStopAllScheds" not in calls
        assert all(mask & ~SYNC_MASK == 0 for value, mask in lib.dout_writes)
        assert {channels for channels, _ in lib.dac_trains} == {(0,)}
        assert lib.dout_register & SYNC_MASK == 0  # sync lines idle at the end
        assert lib.dac_static[0] == 16_384  # the gate at rest (5 V) at the end
        assert (lib.opens, lib.closes) == (1, 1)

    def test_a_human_session_drives_neither_dac_nor_reward(self, tmp_path, monkeypatch):
        device = install_fake_pypixxlib(monkeypatch)
        monkeypatch.setattr(reward_module.time, "sleep", lambda s: None)
        build(tmp_path, subject_kind="human").run()
        assert device.libdpx.dac_trains == []


class TestRuntimeExclusivity:
    def test_a_dout_gate_then_sync_is_refused(self, lib):
        gate = make_reward(cfg(channel="dout0"))
        with pytest.raises(SyncError):
            make_sync(SYNC)
        gate.close()

    def test_sync_then_a_dout_gate_is_refused(self, lib):
        sync = make_sync(SYNC)
        with pytest.raises(RewardError):
            make_reward(cfg(channel="dout0"))
        sync.close()

    def test_sync_then_a_dac_gate_opens(self, lib):
        sync = make_sync(SYNC)
        gate = make_reward(cfg())
        gate.deliver(SHORT)
        sync.pulse("DATAPIXX3/dout0")
        gate.close()
        sync.close()
        assert (lib.opens, lib.closes) == (1, 1)


# ----------------------------------------------------------------------
# Refusals before any analog-output write
# ----------------------------------------------------------------------

WRITES = {"DPxSetDacValue", "DPxEnableDacBuffChan", "DPxWriteRam", "DPxStartDacSched"}


class TestRefusedBeforeAnyWrite:
    @pytest.mark.parametrize(
        ("setup", "reason"),
        [
            (lambda lib: setattr(lib, "dac_loopback", True), "loopback"),
            (lambda lib: setattr(lib, "dac_calib_raw", True), "raw"),
            (lambda lib: setattr(lib, "dac_running", True), "already running"),
            (lambda lib: lib.dac_buffered.add(3), "buffering"),
            (lambda lib: setattr(lib, "tpx_dac_mode", 7), "eye data"),
            (lambda lib: setattr(lib, "dac_tpx_getter_broken", True), "cannot be read"),
            (lambda lib: lib.dac_range.__setitem__(0, (-2.0, 2.0)), "range"),
        ],
    )
    def test_refusal(self, lib, monkeypatch, setup, reason):
        setup(lib)
        log = record(lib, monkeypatch)
        with pytest.raises(RewardError, match=reason):
            make_reward(cfg())
        assert not WRITES & set(names(log))
        assert lib.dac_static_writes == []

    def test_loopback_switched_on_between_trains_refuses_the_next(self, lib, monkeypatch):
        gate = make_reward(cfg())
        gate.deliver(SHORT)
        lib.dac_loopback = True
        log = record(lib, monkeypatch)
        with pytest.raises(RewardError, match="loopback"):
            gate.deliver(SHORT)
        assert not WRITES & set(names(log))
        assert len(lib.dac_trains) == 1
        lib.dac_loopback = False
        gate.close()

    def test_with_the_published_tracker_getter_the_gate_never_opens(self, lib):
        """Not a bug in alhazen: pypixxlib's TPxGetAllDACOutput as VPixx
        publishes it raises TypeError, and alhazen refuses rather than assume
        the tracker leaves the DAC alone. The consequence (finding V-M1): on
        that pypixxlib the DAC gate, and so DAC reward + sync, is unusable."""
        lib.dac_tpx_getter_broken = True
        with pytest.raises(RewardError, match="TRACKPixx3"):
            make_reward(cfg())


# ----------------------------------------------------------------------
# Upload
# ----------------------------------------------------------------------


class TestUpload:
    def test_a_word_corrupted_in_ram_refuses_before_the_start(self, lib, monkeypatch):
        gate = make_reward(cfg())
        original = lib.DPxWriteRam

        def flip_last(address, words):
            words = list(words)
            words[-1] ^= 0x0001
            original(address, words)

        monkeypatch.setattr(lib, "DPxWriteRam", flip_last)
        with pytest.raises(RewardError) as raised:
            gate.deliver(SHORT)
        assert not isinstance(raised.value, RewardUncertainError)
        assert lib.dac_trains == []
        monkeypatch.setattr(lib, "DPxWriteRam", original)
        gate.close()

    def test_a_short_read_back_refuses_before_the_start(self, lib, monkeypatch):
        gate = make_reward(cfg())
        original = lib.DPxReadRam
        monkeypatch.setattr(lib, "DPxReadRam", lambda a, n: original(a, n)[:-1])
        with pytest.raises(RewardError):
            gate.deliver(SHORT)
        assert lib.dac_trains == []
        monkeypatch.setattr(lib, "DPxReadRam", original)
        gate.close()

    def test_a_write_wrapped_by_the_c_ushort_byte_count_never_plays(self, lib, monkeypatch):
        """If chunking were lost, pypixxlib's c_ushort byte count would wrap
        (FakeLibdpx models it); the read-back must catch it."""
        monkeypatch.setattr(reward_module, "VENDOR_RAM_CHUNK_WORDS", 40_002)
        gate = make_reward(cfg())
        with pytest.raises(RewardError) as raised:
            gate.deliver(RewardPulses(n_pulses=40, pulse_ms=500, inter_pulse_ms=500))
        assert not isinstance(raised.value, RewardUncertainError)
        assert lib.dac_trains == []
        gate.close()


# ----------------------------------------------------------------------
# Uncertain deliveries
# ----------------------------------------------------------------------


class TestUncertain:
    def test_usb_lost_mid_train_stops_once_rests_and_never_replays(self, lib, monkeypatch):
        gate = make_reward(cfg())
        lib.polls_until_done = 10**9
        original_poll, original_start = lib.DPxIsDacSchedRunning, lib.DPxStartDacSched
        started = []

        def start():
            started.append(True)
            return original_start()

        def poll():
            if len(started) == 1:  # one transient USB fault, on the first poll after the start
                started.append(True)
                lib.error = USB_GONE
            return original_poll()

        monkeypatch.setattr(lib, "DPxStartDacSched", start)
        monkeypatch.setattr(lib, "DPxIsDacSchedRunning", poll)
        log = record(lib, monkeypatch)
        with pytest.raises(RewardUncertainError):
            gate.deliver(SHORT)
        assert names(log).count("DPxStartDacSched") == 1
        assert names(log).count("DPxStopDacSched") == 1
        assert lib.dac_pin[0] == 16_384  # rest was written after the stop

    def test_a_fault_at_the_start_is_uncertain_and_rests(self, lib, monkeypatch):
        gate = make_reward(cfg())
        lib.fault_on["DPxStartDacSched"] = "DPX_ERR_DAC_SCHED"
        with pytest.raises(RewardUncertainError):
            gate.deliver(SHORT)
        lib.fault_on.clear()
        assert lib.dac_stops == 1 and lib.dac_pin[0] == 16_384
        gate.close()

    def test_an_interrupt_while_waiting_stops_rests_and_is_re_raised(self, lib, monkeypatch):
        gate = make_reward(cfg())
        lib.polls_until_done = 10**9

        def interrupt(*args):
            raise KeyboardInterrupt

        monkeypatch.setattr(reward_module.time, "sleep", interrupt)
        with pytest.raises(KeyboardInterrupt):
            gate.deliver(RewardPulses(n_pulses=1, pulse_ms=200, inter_pulse_ms=0))
        assert lib.dac_stops == 1 and lib.dac_pin[0] == 16_384

    def test_rest_is_written_in_the_same_register_write_as_the_stop(self, lib, monkeypatch):
        gate = make_reward(cfg())
        lib.polls_until_done = 10**9
        log = record(lib, monkeypatch)
        with pytest.raises(RewardUncertainError):
            gate.deliver(SHORT)
        calls = names(log)
        stop = calls.index("DPxStopDacSched")
        after = calls[stop:]
        first_write = after.index("DPxUpdateRegCache")
        assert "DPxSetDacValue" in after[:first_write]


# ----------------------------------------------------------------------
# Leaving the device as found
# ----------------------------------------------------------------------


class TestLeavesTheDeviceUsable:
    def test_a_gate_on_another_channel_opens_after_a_closed_gate(self, lib):
        first = make_reward(cfg())
        first.deliver(SHORT)
        first.close()
        second = make_reward(cfg(channel="dac1"))
        second.deliver(SHORT)
        second.close()

    def test_the_same_channel_reopens(self, lib):
        for _ in range(2):
            gate = make_reward(cfg())
            gate.deliver(SHORT)
            gate.close()
        assert len(lib.dac_trains) == 2

    def test_a_refusal_while_the_gate_reads_active_says_so(self, lib):
        lib.dac_tpx_getter_broken = True  # any refusal; the DAC sits at 0 V (code 0)
        with pytest.raises(RewardError, match="(?i)active|running"):
            make_reward(cfg())
