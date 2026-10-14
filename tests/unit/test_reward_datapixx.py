"""The DATAPixx3 reward gate (devices/reward.py DatapixxReward), its sync
lines (devices/sync.py DatapixxSync), the shared DATAPixx3 link
(devices/vpixx.py) and their rig config, against the stand-in libdpx in
tests/fake_sdk.py.

The fake is written from VPixx's documentation (a schedule plays 16-bit
words onto DOUT 0-15, the port returns to the static register when it ends,
DPxGetDoutValue reads the register); it proves what alhazen asks of libdpx,
in what order, under which lock, and what it refuses. It proves nothing
about the pins: the bench protocol in docs/datapixx-reward.md does that.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import alhazen
from alhazen.config.loader import load_rig
from alhazen.config.models import (
    DevicesConfig,
    EyeTrackerConfig,
    RewardHwConfig,
    RewardPulses,
    SyncHwConfig,
)
from alhazen.config.reward_calibration import ul_per_pulse_on
from alhazen.devices import reward as reward_module
from alhazen.devices.eyetracker import ViewPixxTracker
from alhazen.devices.reward import (
    DatapixxReward,
    NidaqReward,
    QueuedReward,
    SimulatedReward,
    build_gate_words,
    make_reward,
)
from alhazen.devices.sync import DatapixxSync, make_sync
from alhazen.devices.vpixx import dpx_link
from alhazen.errors import RewardError, RewardUncertainError, SyncError, TrackerError
from alhazen.testing import FakeClock
from fake_sdk import FakeLibdpx, install_fake_libdpx, install_fake_pypixxlib
from support import SCREEN

PUMP = {"model": "WPI SP220i", "syringe_diameter_mm": 14.5, "rate_ul_per_min": 3000.0}
GATE = RewardHwConfig(backend="datapixx", channel="dout0", polarity="active_low", pump=PUMP)
TRAIN = RewardPulses(n_pulses=2, pulse_ms=3, inter_pulse_ms=2)


@pytest.fixture
def libdpx(monkeypatch) -> FakeLibdpx:
    return install_fake_libdpx(monkeypatch)


def gate(**changes: Any) -> RewardHwConfig:
    return RewardHwConfig(**{**GATE.model_dump(exclude_unset=True), **changes})


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------


class TestConfig:
    def test_a_gate_line_is_the_datapixx3_digital_output_it_names(self):
        assert GATE.device == "DATAPIXX3"
        assert GATE.line == "DATAPIXX3/dout0"
        assert GATE.calibration_identity() == {
            "backend": "datapixx",
            "polarity": "active_low",
            "pump": PUMP,
        }

    def test_ni_daq_defaults_and_identity_are_unchanged(self):
        line = RewardHwConfig(backend="nidaq")
        assert (line.device, line.channel, line.voltage) == ("Dev1", "ao0", 5.0)
        assert line.line == "Dev1/ao0"
        assert line.calibration_identity() == {"backend": None, "polarity": None, "pump": None}
        assert isinstance(make_reward(RewardHwConfig(backend="simulated")), SimulatedReward)

    @pytest.mark.parametrize(
        ("missing", "match"),
        [
            ("channel", "needs channel: dout<N>"),
            ("polarity", "needs polarity"),
            ("pump", "needs pump"),
        ],
    )
    def test_the_wiring_facts_have_no_defaults(self, missing, match):
        settings = {
            "backend": "datapixx",
            "channel": "dout0",
            "polarity": "active_low",
            "pump": PUMP,
        }
        del settings[missing]
        with pytest.raises(ValidationError, match=match):
            RewardHwConfig(**settings)

    def test_only_the_schedule_word_can_carry_a_timed_gate(self):
        with pytest.raises(ValidationError, match="dout0-dout15"):
            gate(channel="dout16")
        with pytest.raises(ValidationError, match="needs channel"):
            gate(channel="ao0")

    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("device", "VIEWPIXX", "device must be 'DATAPIXX3'"),
            ("voltage", 3.3, "no voltage setting"),
            ("dout_buffer_address", 0x12000000, "sample ring"),
            ("dout_buffer_address", 0x75EEC000, "camera image"),
            ("dout_buffer_address", 8_000_001, "even"),
        ],
    )
    def test_settings_that_cannot_be_right_are_refused(self, field, value, match):
        with pytest.raises(ValidationError, match=match):
            gate(**{field: value})

    def test_gate_settings_are_refused_on_an_ni_daq_line(self):
        with pytest.raises(ValidationError, match="ignores polarity, pump"):
            RewardHwConfig(backend="nidaq", polarity="active_low", pump=PUMP)

    def test_a_dumped_ni_daq_line_loads_again(self):
        line = RewardHwConfig(backend="nidaq", device="Dev2", channel="ao1", voltage=4.5)
        assert RewardHwConfig.model_validate(line.model_dump()) == line

    def test_a_simulated_stand_in_may_keep_the_gate_settings(self):
        stood_down = GATE.model_copy(update={"backend": "simulated"})
        assert RewardHwConfig.model_validate(stood_down.model_dump()).backend == "simulated"

    def test_datapixx_sync_lines_name_a_dout_bit(self):
        SyncHwConfig(backend="datapixx", event_lines={"STIM_ON": "DATAPIXX3/dout17"})
        with pytest.raises(ValidationError, match="DATAPIXX3/dout<N>"):
            SyncHwConfig(backend="datapixx", event_lines={"STIM_ON": "Dev1/port0/line0"})

    def test_a_datapixx_gate_and_datapixx_sync_cannot_share_the_port(self):
        sync = SyncHwConfig(backend="datapixx", event_lines={"STIM_ON": "DATAPIXX3/dout17"})
        with pytest.raises(ValidationError, match="cannot share"):
            DevicesConfig(reward=GATE, sync=sync)
        DevicesConfig(reward=GATE, sync=SyncHwConfig(backend="nidaq", event_lines={}))
        DevicesConfig(reward=RewardHwConfig(backend="nidaq"), sync=sync)

    def test_the_shared_vpixx_rig_still_has_no_reward_line(self):
        rig = load_rig(Path(alhazen.__file__).parent / "rigs" / "rig-vpixx.yaml")
        assert rig.devices.reward is None
        assert rig.devices.eyetracker is not None and rig.devices.eyetracker.backend == "viewpixx"

    def test_an_ni_daq_calibration_never_counts_for_the_gate(self):
        calibration = {
            "widths": {
                200: {"line": "Dev1/ao0", "voltage": 5.0, "ul_per_pulse": 100.0},
                50: {
                    "line": "DATAPIXX3/dout0",
                    "voltage": 5.0,
                    "ul_per_pulse": 2.5,
                    "backend": "datapixx",
                    "polarity": "active_low",
                    "pump": PUMP,
                },
            }
        }
        assert ul_per_pulse_on(calibration, pulse_ms=200, hardware=GATE) is None
        assert ul_per_pulse_on(calibration, pulse_ms=50, hardware=GATE)["ul_per_pulse"] == 2.5
        faster = gate(pump={**PUMP, "rate_ul_per_min": 6000.0})
        assert ul_per_pulse_on(calibration, pulse_ms=50, hardware=faster) is None
        ni = RewardHwConfig(backend="nidaq")
        assert ul_per_pulse_on(calibration, pulse_ms=200, hardware=ni)["ul_per_pulse"] == 100.0


# ----------------------------------------------------------------------
# The words
# ----------------------------------------------------------------------


class TestGateWords:
    def test_an_active_low_train_rests_high_around_its_pulses(self):
        words = build_gate_words(TRAIN, bit=0, polarity="active_low", port_value=0)
        assert [w & 1 for w in words] == [1, 0, 0, 0, 1, 1, 0, 0, 0, 1, 1, 1]

    def test_every_other_low_bit_keeps_the_static_level(self):
        port = (1 << 3) | (1 << 9) | (1 << 17)
        words = build_gate_words(TRAIN, bit=0, polarity="active_low", port_value=port)
        assert {w & ~1 for w in words} == {(1 << 3) | (1 << 9)}  # bit 17 is not in the word
        assert all(0 <= w <= 0xFFFF for w in words)

    def test_an_active_high_gate_is_the_mirror_image(self):
        words = build_gate_words(TRAIN, bit=5, polarity="active_high", port_value=0)
        assert [(w >> 5) & 1 for w in words] == [0, 1, 1, 1, 0, 0, 1, 1, 1, 0, 0, 0]

    def test_back_to_back_pulses_still_end_at_rest(self):
        pulses = RewardPulses(n_pulses=2, pulse_ms=2, inter_pulse_ms=0)
        words = build_gate_words(pulses, bit=0, polarity="active_low", port_value=0)
        assert [w & 1 for w in words] == [1, 0, 0, 0, 0, 1]

    def test_what_cannot_be_a_train_is_refused(self):
        with pytest.raises(ValueError, match="bits 0-15"):
            build_gate_words(TRAIN, bit=16, polarity="active_low", port_value=0)
        with pytest.raises(ValueError, match=">= 1"):
            build_gate_words(RewardPulses(n_pulses=0), bit=0, polarity="active_low", port_value=0)


# ----------------------------------------------------------------------
# Opening and refusing
# ----------------------------------------------------------------------


class TestOpen:
    def test_opening_selects_the_datapixx3_and_sets_only_the_gate_to_rest(self, libdpx):
        libdpx.dout_register = 1 << 7
        reward = make_reward(GATE)
        assert isinstance(reward, DatapixxReward)
        assert libdpx.opens == 1 and libdpx.selected == "DATAPIXX3"
        assert libdpx.dout_writes == [(1, 1)]  # value HIGH, mask only DOUT 0
        assert libdpx.dout_register == (1 << 7) | 1
        assert reward.preflight["gate_level_before"] == "LOW"
        reward.close()
        assert libdpx.closes == 1

    @pytest.mark.parametrize(
        ("setting", "match"),
        [
            ("pixel_mode", "Pixel Mode is on"),
            ("pixel_mode_gb", "Pixel Mode GB"),
            ("pixel_mode_b", "Pixel Mode B"),
            ("button_schedules", "button schedules"),
            ("backlight_pulse", "backlight pulse"),
            ("dout_running", "already running"),
        ],
    )
    def test_another_output_mode_is_refused_before_any_write(self, libdpx, setting, match):
        setattr(libdpx, setting, True)
        with pytest.raises(RewardError, match=f"refused before any digital-output write.*{match}"):
            make_reward(GATE)
        assert libdpx.dout_writes == [] and libdpx.ram == {} and libdpx.trains == []
        assert getattr(libdpx, setting) is True  # never turned off
        assert libdpx.closes == 1  # the device it opened is released again

    def test_a_buffer_on_the_tracker_ring_or_past_the_ram_is_refused(self, libdpx):
        libdpx.buffer_base, libdpx.buffer_size = 7_900_000, 1_000_000
        with pytest.raises(RewardError, match="sample ring"):
            make_reward(GATE)
        libdpx.buffer_base, libdpx.buffer_size = 0x12000000, 0x18000000
        libdpx.ram_size = 8_000_000
        with pytest.raises(RewardError, match="past the device's RAM"):
            make_reward(GATE)
        assert libdpx.dout_writes == []

    def test_a_rest_that_will_not_read_back_is_uncertain(self, libdpx, monkeypatch):
        monkeypatch.setattr(libdpx, "DPxGetDoutValue", lambda: 0)
        with pytest.raises(RewardUncertainError, match="LINE STATE UNCERTAIN"):
            make_reward(GATE)

    def test_no_pypixxlib_is_a_loud_error(self, monkeypatch):
        import sys

        monkeypatch.setitem(sys.modules, "pypixxlib", None)
        with pytest.raises(RewardError, match="pypixxlib is not installed"):
            make_reward(GATE)


# ----------------------------------------------------------------------
# Delivering
# ----------------------------------------------------------------------


class TestDeliver:
    def test_one_finite_hardware_schedule_per_delivery(self, libdpx):
        libdpx.dout_register = 1 << 4
        reward = make_reward(GATE)
        reward.deliver(TRAIN)

        expected = build_gate_words(TRAIN, bit=0, polarity="active_low", port_value=(1 << 4) | 1)
        assert libdpx.trains == [expected]
        assert libdpx.dout_sched == (0, 1000, "hz", len(expected))  # countdown, 1 kHz
        assert libdpx.dout_buff == (8_000_000, 2 * len(expected))  # exactly the words: no wrap
        assert libdpx.dout_stops == 0
        # Rest at open, rest again before the train, read back after it; every
        # write masks DOUT 0 alone.
        assert libdpx.dout_writes == [(1, 1), (1, 1), (1, 1)]
        assert libdpx.dout_register == (1 << 4) | 1
        assert reward.last_train["samples"] == len(expected)

    def test_an_empty_delivery_plays_nothing(self, libdpx):
        reward = make_reward(GATE)
        reward.deliver(RewardPulses(n_pulses=0))
        assert libdpx.trains == []

    def test_a_mode_switched_on_mid_session_refuses_the_next_train(self, libdpx):
        reward = make_reward(GATE)
        libdpx.pixel_mode_gb = True
        with pytest.raises(RewardError, match="Pixel Mode GB"):
            reward.deliver(TRAIN)
        assert libdpx.trains == []

    def test_the_lock_is_free_between_polls(self, libdpx, monkeypatch):
        reward = make_reward(GATE)
        libdpx.polls_until_done = 4
        held: list[bool] = []
        monkeypatch.setattr(
            reward_module.time, "sleep", lambda s: held.append(reward._link.lock.locked())
        )
        reward.deliver(RewardPulses(n_pulses=1, pulse_ms=1, inter_pulse_ms=0))
        assert held and not any(held)

    def test_a_schedule_that_never_ends_is_stopped_once_and_not_replayed(self, libdpx, monkeypatch):
        monkeypatch.setattr(reward_module, "_SCHEDULE_SLACK_S", 0.05)
        reward = make_reward(GATE)
        libdpx.polls_until_done = None
        with pytest.raises(RewardUncertainError, match="UNCERTAIN.*NOT retried"):
            reward.deliver(TRAIN)
        assert len(libdpx.trains) == 1
        assert libdpx.dout_stops == 1
        assert libdpx.dout_register & 1 == 1

    def test_a_usb_fault_mid_train_is_uncertain_and_not_replayed(self, libdpx, monkeypatch):
        reward = make_reward(GATE)
        libdpx.polls_until_done = 3
        asked = libdpx.DPxIsDoutSchedRunning

        def fails_while_playing() -> int:
            if libdpx.dout_running:
                libdpx.error = "DPX_ERR_USB_REQ_FAILED"
            return asked()

        monkeypatch.setattr(libdpx, "DPxIsDoutSchedRunning", fails_while_playing)
        with pytest.raises(
            RewardUncertainError, match="DPX_ERR_USB_REQ_FAILED.*UNCERTAIN.*NOT retried"
        ):
            reward.deliver(TRAIN)
        assert len(libdpx.trains) == 1 and libdpx.dout_stops == 1

    def test_a_fault_before_the_train_writes_nothing(self, libdpx):
        reward = make_reward(GATE)
        writes = list(libdpx.dout_writes)
        libdpx.unplug()
        with pytest.raises(RewardError, match="DPX_ERR_USB_REQ_FAILED"):
            reward.deliver(TRAIN)
        assert libdpx.trains == [] and libdpx.dout_writes == writes

    def test_a_device_lost_mid_train_cannot_be_confirmed_at_rest(self, libdpx, monkeypatch):
        reward = make_reward(GATE)
        start = libdpx.DPxStartDoutSched

        def start_then_unplug() -> None:
            start()
            libdpx.unplug()

        monkeypatch.setattr(libdpx, "DPxStartDoutSched", start_then_unplug)
        with pytest.raises(RewardUncertainError, match="UNCERTAIN.*LINE STATE UNCERTAIN"):
            reward.deliver(TRAIN)

    def test_the_queue_reports_a_failed_train_and_never_delivers_it_again(self, libdpx):
        queued = QueuedReward(make_reward(GATE))
        libdpx.fault_on["DPxStartDoutSched"] = "DPX_ERR_USB_REQ_FAILED"
        with pytest.raises(RewardUncertainError, match="UNCERTAIN"):
            queued.deliver(TRAIN)
        queued.wait_idle()
        assert len(libdpx.trains) <= 1 and libdpx.dout_stops == 1
        del libdpx.fault_on["DPxStartDoutSched"]
        queued.close()
        assert libdpx.closes == 1

    def test_a_closed_gate_refuses_and_close_reads_rest_back(self, libdpx):
        reward = make_reward(GATE)
        reward.close()
        assert libdpx.dout_register & 1 == 1 and libdpx.closes == 1
        with pytest.raises(RewardError, match="after the reward gate was closed"):
            reward.deliver(TRAIN)
        reward.close()  # idempotent
        assert libdpx.closes == 1


# ----------------------------------------------------------------------
# Sharing the DATAPixx3
# ----------------------------------------------------------------------


def connected_tracker(device) -> ViewPixxTracker:
    tracker = ViewPixxTracker(
        EyeTrackerConfig(backend="viewpixx"), None, SCREEN, FakeClock(), background_gaze=False
    )
    tracker.connect()
    return tracker


class TestSharedLink:
    def test_one_lock_for_the_tracker_and_the_gate(self, monkeypatch):
        device = install_fake_pypixxlib(monkeypatch)
        tracker = connected_tracker(device)
        reward = make_reward(GATE)
        assert tracker._device_lock is reward._link.lock is dpx_link(device.libdpx).lock
        reward.close()
        tracker.shutdown(None)

    def test_the_gate_released_first_leaves_the_device_to_the_tracker(self, monkeypatch):
        device = install_fake_pypixxlib(monkeypatch)
        tracker = connected_tracker(device)
        assert device.libdpx.opens == 1  # pypixxlib's TRACKPixx3() opened it
        reward = make_reward(GATE)
        assert device.libdpx.opens == 1  # the gate joined; no second open
        reward.close()
        assert device.libdpx.closes == 0 and not device.closed
        tracker.shutdown(None)
        assert device.closed and device.libdpx.closes == 1  # TRACKPixx3.close(), the last holder

    def test_the_tracker_shut_down_first_does_not_close_the_device(self, monkeypatch):
        device = install_fake_pypixxlib(monkeypatch)
        tracker = connected_tracker(device)
        reward = make_reward(GATE)
        tracker.shutdown(None)
        assert not device.closed and device.libdpx.closes == 0
        assert device.libdpx.tracker_close_steps == ["TPxUninitialize", "TPxSetSleepPictureRequest"]
        reward.deliver(TRAIN)  # still works
        reward.close()
        assert (device.libdpx.opens, device.libdpx.closes) == (1, 1)

    def test_a_tracker_after_the_gate_is_refused_before_pypixxlib_opens_again(self, monkeypatch):
        device = install_fake_pypixxlib(monkeypatch)
        reward = make_reward(GATE)  # the link's own DPxOpen
        with pytest.raises(TrackerError, match="must be connected before"):
            connected_tracker(device)
        assert device.libdpx.opens == 1
        reward.close()
        assert (device.libdpx.opens, device.libdpx.closes) == (1, 1)
        tracker = connected_tracker(device)  # alone again: fine
        tracker.shutdown(None)
        assert (device.libdpx.opens, device.libdpx.closes) == (2, 2)

    def test_gate_and_datapixx_sync_are_refused_in_one_process(self, libdpx):
        sync_cfg = SyncHwConfig(backend="datapixx", event_lines={"STIM_ON": "DATAPIXX3/dout17"})
        reward = make_reward(GATE)
        with pytest.raises(SyncError, match="cannot share the port"):
            make_sync(sync_cfg)
        reward.close()
        sync = make_sync(sync_cfg)
        with pytest.raises(RewardError, match="cannot share the port"):
            make_reward(GATE)
        sync.close()
        assert libdpx.closes == 2

    def test_every_operation_selects_the_datapixx3_first(self, libdpx):
        reward = make_reward(GATE)
        libdpx.selected = "TRACKPIXX"  # a pypixxlib object selected another device
        reward.deliver(TRAIN)
        assert libdpx.selected == "DATAPIXX3"

    def test_a_fault_left_unread_is_logged_not_lost(self, libdpx, caplog):
        reward = make_reward(GATE)
        libdpx.error = "DPX_ERR_SOMETHING"
        reward.deliver(TRAIN)
        assert "left unread by an earlier call: DPX_ERR_SOMETHING" in caplog.text

    def test_the_lock_serialises_threads(self, libdpx):
        reward = make_reward(GATE)
        link = reward._link
        link.lock.acquire()
        done = threading.Event()
        worker = threading.Thread(target=lambda: (reward.deliver(TRAIN), done.set()))
        worker.start()
        worker.join(0.1)
        assert not done.is_set() and libdpx.trains == []
        link.lock.release()
        worker.join(5)
        assert done.is_set() and len(libdpx.trains) == 1


# ----------------------------------------------------------------------
# DATAPixx3 sync lines
# ----------------------------------------------------------------------


class TestDatapixxSync:
    CFG = SyncHwConfig(
        backend="datapixx",
        pulse_ms=0.5,
        event_lines={"STIM_ON": "DATAPIXX3/dout2", "RESPONSE": "DATAPIXX3/dout17"},
    )

    def test_pulses_touch_only_their_own_bits(self, libdpx):
        libdpx.dout_register = 1 << 9
        sync = make_sync(self.CFG)
        assert isinstance(sync, DatapixxSync)
        mask = (1 << 2) | (1 << 17)
        sync.pulse("DATAPIXX3/dout17")
        assert libdpx.dout_writes == [(0, mask), (1 << 17, mask), (0, mask)]
        assert libdpx.dout_register == 1 << 9
        sync.close()
        assert libdpx.closes == 1
        with pytest.raises(SyncError, match="after the sync lines were closed"):
            sync.pulse("DATAPIXX3/dout2")

    @pytest.mark.parametrize("setting", ["pixel_mode", "pixel_mode_gb", "pixel_mode_b"])
    def test_a_pixel_mode_on_a_sync_bit_is_refused(self, libdpx, setting):
        setattr(libdpx, setting, True)
        with pytest.raises(SyncError, match="Pixel Mode"):
            make_sync(self.CFG)
        assert libdpx.dout_writes == []

    def test_unconfigured_lines_are_errors(self, libdpx):
        sync = make_sync(self.CFG)
        with pytest.raises(SyncError, match="no sync line"):
            sync.pulse("DATAPIXX3/dout3")


# ----------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------


class TestSessions:
    def test_a_human_session_never_opens_the_gate(self, tmp_path, libdpx):
        from test_subject_kind import PayingClassTask, build, human

        runner = build(tmp_path, PayingClassTask(human()), reward_line=GATE)
        assert runner._reward is None
        runner.run()
        assert libdpx.opens == 0 and libdpx.dout_writes == [] and libdpx.trains == []

    def test_a_monkey_session_plays_one_train_per_paid_trial(self, tmp_path, libdpx):
        from test_subject_kind import PLAN, OutcomeTask, build, card, monkey

        short = {"n_pulses": 1, "pulse_ms": 2, "inter_pulse_ms": 0}
        build(tmp_path, OutcomeTask(monkey(by_outcome={"HIT": short})), reward_line=GATE).run()
        assert len(libdpx.trains) == PLAN.count("HIT")
        assert libdpx.dout_register & 1 == 1 and libdpx.closes == 1
        record = card(tmp_path)["reward"]
        assert record["backend"] == "datapixx" and record["line"] == "DATAPIXX3/dout0"
        assert record["polarity"] == "active_low" and record["pump"] == PUMP

    def test_the_ni_daq_backend_is_still_what_nidaq_builds(self):
        # Constructor, defaults and import path as before this change: without
        # nidaqmx installed it is still the loud error it always was.
        with pytest.raises(RewardError, match="nidaqmx is not installed"):
            NidaqReward(RewardHwConfig(backend="nidaq"))


# ----------------------------------------------------------------------
# check-rig and the Measure rig
# ----------------------------------------------------------------------


class TestRigChecks:
    def test_check_rig_without_pulse_plays_nothing(self, libdpx):
        from alhazen.config.models import MonitorConfig, RigConfig
        from alhazen.session.checks import _check_reward

        rig = RigConfig(
            monitor=MonitorConfig(
                width_px=1920,
                height_px=1080,
                width_cm=52.0,
                distance_cm=57.0,
                refresh_rate_hz=120.0,
            ),
            devices=DevicesConfig(reward=GATE),
            data_root=Path("data"),
        )
        result = _check_reward(rig, pulse=False)
        assert result.ok, result.detail
        assert libdpx.trains == [] and set(libdpx.dout_writes) == {(1, 1)}  # rest, open + close
        assert result.evidence["preflight"]["pixel_mode"] is False
        assert "not a measurement of the pump" in " ".join(result.evidence["notes"])
        assert libdpx.closes == 1

        pulsed = _check_reward(rig, pulse=True)
        assert pulsed.ok and len(libdpx.trains) == 1 and pulsed.evidence["train"]["samples"] > 0

    def test_check_rig_closes_the_gate_after_a_failed_pulse(self, libdpx, monkeypatch):
        from alhazen.config.models import MonitorConfig, RigConfig
        from alhazen.session.checks import _check_reward

        rig = RigConfig(
            monitor=MonitorConfig(
                width_px=1920,
                height_px=1080,
                width_cm=52.0,
                distance_cm=57.0,
                refresh_rate_hz=120.0,
            ),
            devices=DevicesConfig(reward=GATE),
            data_root=Path("data"),
        )
        libdpx.fault_on["DPxStartDoutSched"] = "DPX_ERR_USB_REQ_FAILED"
        result = _check_reward(rig, pulse=True)
        assert not result.ok and "UNCERTAIN" in result.detail
        assert libdpx.closes == 1  # released: the next session can open it

    def test_the_measure_connection_check_writes_nothing(self, libdpx):
        from alhazen.modes.measure_builtin import _datapixx_connection

        measured = _datapixx_connection(GATE)
        assert measured.ok is True
        assert libdpx.dout_writes == [] and libdpx.trains == [] and libdpx.closes == 1
        libdpx.pixel_mode = True
        refused = _datapixx_connection(GATE)
        assert refused.ok is False and "Pixel Mode" in refused.summary
