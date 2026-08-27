"""Reward and sync: the waveform that controls delivered volume, and the
event→line mapping that makes an external recording alignable."""

from __future__ import annotations

import pytest

from alhazen.config.models import RewardHwConfig, RewardPulses, SyncHwConfig
from alhazen.core.events import Event
from alhazen.devices.reward import (
    NidaqReward,
    RewardDispenser,
    SimulatedReward,
    build_reward_waveform,
    make_reward,
)
from alhazen.devices.sync import (
    SimulatedSync,
    SyncOutput,
    make_sync,
    make_sync_subscriber,
)
from alhazen.errors import RewardError, SyncError

LINES = {"TRIAL_START": "Dev1/port0/line0", "STIM_ON": "Dev1/port0/line1"}


class TestRewardWaveform:
    def test_pulse_train_shape_and_rate(self):
        pulses = RewardPulses(n_pulses=2, pulse_ms=10, inter_pulse_ms=5)
        waveform = build_reward_waveform(5.0, pulses, rate_hz=1000)
        assert len(waveform) == 2 * (10 + 5)  # one sample per ms at 1 kHz
        assert list(waveform[:10]) == [5.0] * 10
        assert list(waveform[10:15]) == [0.0] * 5

    def test_always_ends_at_zero_volts(self):
        # A finite AO task latches its last sample on the line: a waveform
        # ending high leaves the valve open after "delivery" is done.
        pulses = RewardPulses(n_pulses=1, pulse_ms=10, inter_pulse_ms=0)
        waveform = build_reward_waveform(5.0, pulses)
        assert waveform[-1] == 0.0
        assert len(waveform) == 11  # the trailing zero is appended, not substituted

    def test_empty_spec_is_rejected(self):
        with pytest.raises(ValueError, match="n_pulses and pulse_ms"):
            build_reward_waveform(5.0, RewardPulses(n_pulses=0, pulse_ms=10))


class TestSimulatedReward:
    def test_records_the_pulse_spec(self):
        reward = SimulatedReward()
        spec = RewardPulses(n_pulses=3, pulse_ms=50, inter_pulse_ms=25)
        reward.deliver(spec)
        reward.close()
        assert reward.deliveries == [spec]

    def test_satisfies_the_protocol(self):
        assert isinstance(SimulatedReward(), RewardDispenser)


class TestMakeReward:
    def test_simulated_backend(self):
        assert isinstance(make_reward(RewardHwConfig(backend="simulated")), SimulatedReward)

    def test_nidaq_without_the_sdk_names_the_extra(self):
        with pytest.raises(RewardError, match=r"\[nidaq\]"):
            NidaqReward(RewardHwConfig(backend="nidaq"))


class TestSimulatedSync:
    def test_records_pulsed_lines_in_order(self):
        sync = SimulatedSync(LINES)
        sync.pulse("Dev1/port0/line1")
        sync.pulse("Dev1/port0/line0")
        sync.close()
        assert sync.pulses == ["Dev1/port0/line1", "Dev1/port0/line0"]

    def test_unconfigured_line_fails_loudly(self):
        sync = SimulatedSync(LINES)
        with pytest.raises(SyncError, match="line99"):
            sync.pulse("Dev1/port0/line99")

    def test_satisfies_the_protocol(self):
        assert isinstance(SimulatedSync({}), SyncOutput)


class TestMakeSync:
    def test_none_backend_is_a_true_no_op(self):
        """`none` DISABLES sync, as the config model says. It used to build a
        SimulatedSync with nothing mapped, whose `pulse()` raises — so a valid
        config (keep the map, turn sync off) built fine and then died on the
        first mapped event of trial 1."""
        sync = make_sync(SyncHwConfig(backend="none", event_lines=LINES))

        sync.pulse("Dev1/port0/line0")  # accepted and does nothing
        sync.pulse("anything at all")
        sync.close()

    def test_the_no_op_records_nothing_to_mistake_for_a_pulse(self):
        sync = make_sync(SyncHwConfig(backend="none", event_lines=LINES))
        assert not hasattr(sync, "pulses")

    def test_the_no_op_satisfies_the_protocol(self):
        assert isinstance(make_sync(SyncHwConfig(backend="none")), SyncOutput)

    def test_simulated_still_refuses_an_unconfigured_line(self):
        # Unchanged: a `simulated` rig is standing in for real hardware, so a
        # line nobody configured is still a bug.
        sync = make_sync(SyncHwConfig(backend="simulated", event_lines=LINES))
        with pytest.raises(SyncError):
            sync.pulse("Dev1/port0/line99")

    def test_nidaq_without_the_sdk_names_the_extra(self):
        with pytest.raises(SyncError, match=r"\[nidaq\]"):
            make_sync(SyncHwConfig(backend="nidaq", event_lines=LINES))


class TestSyncSubscriber:
    def test_mapped_event_pulses_its_line(self):
        sync = SimulatedSync(LINES)
        on_event = make_sync_subscriber(sync, LINES)
        on_event(Event(name="STIM_ON", t=0.0, trial_index=1))
        assert sync.pulses == ["Dev1/port0/line1"]

    def test_unmapped_event_is_a_silent_no_op(self):
        # The config decides what reaches hardware: an event with no line is
        # a rig statement, not a mistake.
        sync = SimulatedSync(LINES)
        on_event = make_sync_subscriber(sync, LINES)
        on_event(Event(name="TRIAL_END", t=0.0, trial_index=1))
        assert sync.pulses == []

    def test_pulse_failures_propagate(self):
        class DeadSync:
            def pulse(self, line: str) -> None:
                raise SyncError("line is dead")

            def close(self) -> None:
                pass

        on_event = make_sync_subscriber(DeadSync(), LINES)
        # Invariant 6: a dead sync line makes every later alignment mark
        # suspect, so it must stop the session rather than be swallowed.
        with pytest.raises(SyncError, match="dead"):
            on_event(Event(name="STIM_ON", t=0.0, trial_index=1))
