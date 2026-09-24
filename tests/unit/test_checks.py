"""check-rig: the pre-session smoke test, driven with simulated backends."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from alhazen.config.models import (
    DevicesConfig,
    DisplayConfig,
    EyeTrackerConfig,
    RewardHwConfig,
    RigConfig,
    SpikeSourceConfig,
    SyncHwConfig,
)
from alhazen.devices.eyetracker import ViewPixxTracker
from alhazen.devices.spikes import SortedStreamSource
from alhazen.errors import ConfigError
from alhazen.session import checks
from alhazen.session.checkout import build_record
from alhazen.session.checks import check_rig, format_result
from alhazen.testing import FakeClock
from fake_sdk import FakeEyeLinkHost, install_fake_pylink, install_fake_pypixxlib
from support import MONITOR

LINES = {"TRIAL_START": "Dev1/port0/line0", "FIX_ON": "Dev1/port0/line1"}


def sim_rig(tmp_path, **devices) -> RigConfig:
    return RigConfig(
        monitor=MONITOR,
        display=DisplayConfig(backend="simulated"),
        devices=DevicesConfig(**devices),
        data_root=tmp_path / "data",
    )


def by_name(results):
    return {r.name: r for r in results}


class TestSimulatedRig:
    def test_fully_simulated_rig_passes(self, tmp_path):
        rig = sim_rig(
            tmp_path,
            reward=RewardHwConfig(backend="simulated"),
            sync=SyncHwConfig(backend="simulated", event_lines=LINES),
        )
        results = check_rig(rig)
        assert all(r.ok for r in results)
        assert [r.name for r in results] == [
            "config",
            "monitor",
            "data_root",
            "eyetracker",
            "reward",
            "sync",
            "recording",
            "spikes",
        ]
        assert "(simulated)" in by_name(results)["reward"].detail
        assert "(simulated)" in by_name(results)["sync"].detail

    def test_unconfigured_devices_are_reported_not_skipped(self, tmp_path):
        results = by_name(check_rig(sim_rig(tmp_path)))
        assert results["eyetracker"].ok
        assert "not configured" in results["eyetracker"].detail
        assert "not configured" in results["reward"].detail

    def test_mouse_sim_is_never_constructed(self, tmp_path):
        # Constructing it needs a real window, which check-rig must not open.
        rig = sim_rig(tmp_path, eyetracker=EyeTrackerConfig(backend="mouse_sim"))
        result = by_name(check_rig(rig))["eyetracker"]
        assert result.ok
        assert "no hardware" in result.detail

    def test_pulse_fires_the_reward_and_every_mapped_line(self, tmp_path):
        rig = sim_rig(
            tmp_path,
            reward=RewardHwConfig(backend="simulated"),
            sync=SyncHwConfig(backend="simulated", event_lines=LINES),
        )
        results = by_name(check_rig(rig, pulse=True))
        assert "fired one 50 ms pulse" in results["reward"].detail
        assert "pulsed 2 line(s)" in results["sync"].detail

    def test_data_root_that_cannot_be_created_fails(self, tmp_path):
        blocker = tmp_path / "data"
        blocker.write_text("not a directory")
        result = by_name(check_rig(sim_rig(tmp_path)))["data_root"]
        assert not result.ok
        assert "not writable" in result.detail


class TestRecording:
    def test_a_reachable_data_directory_passes(self, tmp_path):
        from alhazen.config.models import RecordingConfig

        rig = sim_rig(
            tmp_path,
            recording=RecordingConfig(backend="spikeglx", data_dir=tmp_path),
        )
        result = by_name(check_rig(rig))["recording"]
        assert result.ok

    def test_an_unmounted_share_fails_before_the_session(self, tmp_path):
        # The failure that actually happens: a network share that did not
        # mount, found after a session rather than before it.
        from alhazen.config.models import RecordingConfig

        rig = sim_rig(
            tmp_path,
            recording=RecordingConfig(backend="spikeglx", data_dir=tmp_path / "not-mounted"),
        )
        result = by_name(check_rig(rig))["recording"]
        assert not result.ok
        assert "not reachable" in result.detail


class TestTestOnlyBackend:
    def test_a_missing_tracker_sdk_is_a_fail_line_not_a_traceback(self, tmp_path, monkeypatch):
        # check-rig exists to tell an experimenter what is wrong with the rig.
        # Both real backends need a vendor SDK, and each must come back as a
        # FAIL naming what to install. The SDKs are made absent explicitly:
        # on the rig itself they are installed, and check_rig would otherwise
        # open the real tracker from inside a unit test.
        monkeypatch.setitem(sys.modules, "pylink", None)
        monkeypatch.setitem(sys.modules, "pypixxlib", None)
        for backend, installer in (
            ("eyelink", "Developer's Kit"),
            ("viewpixx", "Software Tools"),
        ):
            rig = sim_rig(tmp_path, eyetracker=EyeTrackerConfig(backend=backend))
            result = by_name(check_rig(rig))["eyetracker"]
            assert not result.ok
            assert installer in result.detail

    def test_the_detail_names_only_what_the_backend_actually_has(self, tmp_path, monkeypatch):
        # An EyeLink is reached over the network, so its IP is the useful half
        # of a success line — it is the thing an experimenter goes and checks.
        # A TRACKPixx3 sits inside the display chassis and has no address, so
        # printing the EyeLink's defaulted IP beside it would send them
        # looking for a network fault that cannot exist.
        class SilentTracker:
            """Connects and releases without hardware, so the OK path runs."""

            def connect(self) -> None: ...

            def shutdown(self, destination, /) -> None: ...

        monkeypatch.setattr(checks, "make_tracker", lambda *args: SilentTracker())

        rig = sim_rig(tmp_path, eyetracker=EyeTrackerConfig(backend="eyelink"))
        detail = by_name(check_rig(rig))["eyetracker"].detail
        assert detail == "eyelink at 100.1.1.1 responded"

        rig = sim_rig(tmp_path, eyetracker=EyeTrackerConfig(backend="viewpixx"))
        detail = by_name(check_rig(rig))["eyetracker"].detail
        assert detail == "viewpixx responded"

    def test_scripted_tracker_is_a_config_error(self, tmp_path):
        # A rig YAML cannot supply a gaze trajectory, so naming the replay
        # double there is a broken config — the same error build_session
        # raises, not a per-device FAIL line.
        rig = sim_rig(tmp_path, eyetracker=EyeTrackerConfig(backend="scripted"))
        with pytest.raises(ConfigError, match="test-only"):
            check_rig(rig)


class TestTheDropoutTest:
    """check-rig exercises the dropout detection a session relies on, on the
    tracker itself: record, stop the recording through the SDK behind the
    session's back, and time the session's own health check reporting it.
    Driven here against the simulated SDKs (fake_sdk.py), in simulated time."""

    def check(self, tmp_path, backend: str = "eyelink", **cfg):
        clock = FakeClock(start=100.0)
        return clock, lambda: checks._check_eyetracker(
            sim_rig(tmp_path, eyetracker=EyeTrackerConfig(backend=backend, **cfg)),
            clock=clock,
            sleep=clock.advance,
        )

    def test_an_eyelink_that_notices_a_host_side_stop_passes(self, tmp_path, monkeypatch):
        clock, run = self.check(tmp_path)
        sdk = install_fake_pylink(monkeypatch, clock)
        result = run()

        assert result.ok, result.detail
        assert result.detail.startswith(
            "eyelink at 100.1.1.1 responded; a stop through the SDK was reported in "
        )
        dropout = result.evidence["dropout"]
        assert (dropout["tested"], dropout["ok"], dropout["detected"]) == (True, True, True)
        assert dropout["limit_ms"] == 50.0
        assert 0.0 < dropout["latency_ms"] <= 50.0 + checks.DROPOUT_SLACK_S * 1000
        # The sentence a session's row would carry, written down.
        assert "isRecording -1, TRIAL_ERROR" in dropout["detail"]
        assert "stopRecording() through pylink" in dropout["stopped_by"]
        # Recording normally: no false alarm, and the gap seen is the margin
        # the limit has.
        assert dropout["false_alarm"] is None
        assert 0.0 <= dropout["longest_gap_ms"] < 50.0
        # What the check costs per frame here, measured, not assumed.
        assert dropout["check_us_max"] >= dropout["check_us_mean"] >= 0.0
        assert dropout["detecting_check_us"] >= 0.0
        # Nothing left recording or connected behind the check.
        (host,) = sdk.hosts
        assert not host.recording and host.closed

    def test_detection_that_does_not_work_fails_the_check(self, tmp_path, monkeypatch):
        # A Host PC whose stop the SDK call does not produce: samples keep
        # coming, so nothing can be noticed — the rig to fix before a session.
        clock, run = self.check(tmp_path)
        install_fake_pylink(monkeypatch, clock)
        monkeypatch.setattr(FakeEyeLinkHost, "stopRecording", lambda self: None)
        result = run()

        assert not result.ok
        assert result.detail.startswith("eyelink at 100.1.1.1 responded, but a stop through the")
        assert "NOT reported within 2 s" in result.detail
        assert result.evidence["dropout"]["detected"] is False

    def test_a_check_that_fires_on_a_normal_recording_fails(self, tmp_path, monkeypatch):
        # A tracker delivering a sample every 200 ms against a 50 ms limit
        # would abort every trial of a session: said before it can.
        clock, run = self.check(tmp_path)
        install_fake_pylink(monkeypatch, clock, rate_hz=5.0)
        result = run()

        assert not result.ok
        assert "the dropout check fired while the tracker was recording normally" in result.detail
        dropout = result.evidence["dropout"]
        assert dropout["false_alarm"].startswith("no new sample from the EyeLink for")
        assert dropout["stopped_by"] is None  # it never got as far as the stop

    def test_a_recording_that_will_not_start_fails_in_the_rigs_words(self, tmp_path, monkeypatch):
        clock, run = self.check(tmp_path)
        sdk = install_fake_pylink(monkeypatch, clock)
        monkeypatch.setattr(FakeEyeLinkHost, "startRecording", lambda self, *flags: 5)
        result = run()

        assert not result.ok
        assert "the dropout test could not run: EyeLink could not start recording" in result.detail
        assert "startRecording failed (code 5)" in result.evidence["dropout"]["error"]
        (host,) = sdk.hosts
        assert host.closed

    def test_the_limit_tested_is_the_rigs(self, tmp_path, monkeypatch):
        clock, run = self.check(tmp_path, max_sample_gap_ms=120)
        install_fake_pylink(monkeypatch, clock)
        dropout = run().evidence["dropout"]
        assert dropout["limit_ms"] == 120.0
        assert 110.0 < dropout["latency_ms"] <= 120.0 + checks.DROPOUT_SLACK_S * 1000

    def test_a_trackpixx3_that_notices_its_recording_stop_passes(self, tmp_path, monkeypatch):
        clock, run = self.check(tmp_path, backend="viewpixx")
        device = install_fake_pypixxlib(monkeypatch)
        # No reader thread: the check's own polls read for it, in simulated
        # time. On the rig the thread reads and the check only looks.
        monkeypatch.setattr(
            checks,
            "make_tracker",
            lambda cfg, display, screen, clock: ViewPixxTracker(
                cfg, display, screen, clock, background_gaze=False
            ),
        )
        result = run()

        assert result.ok, result.detail
        assert result.detail.startswith("viewpixx responded; a stop through the SDK was reported")
        dropout = result.evidence["dropout"]
        assert dropout["limit_ms"] == 100.0
        assert dropout["latency_ms"] <= 100.0 + checks.DROPOUT_SLACK_S * 1000
        assert dropout["detail"].endswith("free-run sampling is off")
        assert "TPxDisableFreeRun()" in dropout["stopped_by"]
        # The device recording again and closed, and the check's own test
        # recording not left behind in the temp folder.
        assert device.libdpx.freerun and device.closed
        assert device.recording_folder is not None
        assert not Path(device.recording_folder).exists()

    def test_a_tracker_without_dropout_detection_is_not_tested(self, tmp_path, monkeypatch):
        class SilentTracker:
            def connect(self) -> None: ...

            def shutdown(self, destination, /) -> None: ...

        monkeypatch.setattr(checks, "make_tracker", lambda *args: SilentTracker())
        clock, run = self.check(tmp_path)
        result = run()
        assert result.ok
        assert result.detail == "eyelink at 100.1.1.1 responded"
        assert result.evidence["dropout"]["tested"] is False

    def test_the_record_carries_it(self, tmp_path, monkeypatch):
        clock, run = self.check(tmp_path)
        install_fake_pylink(monkeypatch, clock)
        record = build_record("rig.yaml", [run()], pulse=False)

        summary = record.render()
        assert "dropout limit 50.0 ms; longest gap between samples while recording normally" in (
            summary
        )
        assert "health check per frame: mean" in summary
        assert "stopped by stopRecording() through pylink" in summary
        assert "reported after" in summary and "isRecording -1, TRIAL_ERROR" in summary
        assert "dropout test: PASS — a stop through the SDK was reported in" in summary
        written = json.loads(json.dumps(record.to_dict(), default=str))
        assert written["devices"]["eyetracker"]["evidence"]["dropout"]["ok"] is True


def units_msg(unit_ids=(3, 9, 14), rate: float = 30000.0) -> list[bytes]:
    header = {
        "type": "units",
        "unit_ids": list(unit_ids),
        "labels": ["good"] * len(list(unit_ids)),
        "sample_rate_hz": rate,
    }
    return [json.dumps(header).encode()]


def heartbeat_msg(covered: int) -> list[bytes]:
    return [json.dumps({"type": "heartbeat", "covered_until_sample": covered}).encode()]


class ScriptedSubscriber:
    """The messages a late joiner finds already in flight, in order.

    ``recv`` returns None once the script runs dry, exactly as the real
    subscriber does when its poll times out. Scripting the order is the
    point: with a live PUB socket, whether the check sees a heartbeat or a
    units message first is a scheduling accident, and a test that depends on
    that accident is the flake this change exists to remove.
    """

    def __init__(self, messages: list[list[bytes]]) -> None:
        self._messages = list(messages)
        self.closed = False

    def recv(self, timeout_ms: float) -> list[bytes] | None:
        return self._messages.pop(0) if self._messages else None

    def close(self) -> None:
        self.closed = True


def scripted_rig(monkeypatch, tmp_path, messages, **spike_cfg):
    """A rig whose sorted stream replays ``messages`` instead of a socket."""
    cfg = SpikeSourceConfig(backend="sorted_stream", address="tcp://127.0.0.1:5556", **spike_cfg)
    subscriber = ScriptedSubscriber(messages)
    monkeypatch.setattr(
        checks,
        "make_spikes",
        lambda c: SortedStreamSource(c, subscriber_factory=lambda: subscriber),
    )
    return sim_rig(tmp_path, spikes=cfg)


class TestSortedStreamSpikes:
    """A SUB socket connects to an endpoint nobody is publishing on without
    complaining, so unlike every other device this one can only be checked
    by listening. These tests pin that it does — and that it listens the way
    a *late joiner* has to: check-rig never witnesses a sorter's startup, so
    what it waits for is a re-announcement, not a first announcement."""

    def test_a_late_joiner_that_hears_heartbeats_first_still_passes(self, monkeypatch, tmp_path):
        # The normal case on a healthy rig: the sorter has been running for
        # an hour, so the first thing the check hears is mid-stream traffic
        # and the units message only comes with the next re-announcement.
        # This used to be a FAIL, which made a working sorter look broken
        # whenever the scheduler happened to deliver the heartbeat first.
        rig = scripted_rig(
            monkeypatch,
            tmp_path,
            [
                heartbeat_msg(covered=30_000),
                heartbeat_msg(covered=36_000),
                units_msg([3, 9, 14]),
                heartbeat_msg(covered=42_000),
            ],
        )
        result = by_name(check_rig(rig))["spikes"]
        assert result.ok, result.detail
        assert "3 units" in result.detail
        # The held heartbeats were placed once the rate arrived, so coverage
        # is known: a lag of "unknown" here would mean they were thrown away.
        assert "lag unknown" not in result.detail
        assert "lag" in result.detail

    def test_a_sorter_that_never_re_announces_units_fails_by_name(self, monkeypatch, tmp_path):
        # The stream is alive and talking, so "nothing is publishing" would
        # send the experimenter to restart a sorter that is running fine.
        # What is broken is the contract: no units message means no sample
        # rate, and no subscriber that missed the startup can ever place a
        # spike on the clock.
        monkeypatch.setattr(checks, "UNITS_GRACE_MS", 200.0)
        rig = scripted_rig(
            monkeypatch,
            tmp_path,
            [heartbeat_msg(covered=30_000), heartbeat_msg(covered=36_000)],
            heartbeat_timeout_ms=300.0,
        )
        result = by_name(check_rig(rig))["spikes"]
        assert not result.ok
        assert "never re-announced units" in result.detail
        assert "is publishing" in result.detail
        # And it says what a conformant sorter would have done.
        assert "at least every" in result.detail

    def test_a_publishing_sorter_reports_its_units_and_lag(self, tmp_path):
        # The one test on a real socket, for the wire itself. It no longer
        # depends on which message the SUB socket happens to receive first:
        # the publisher re-announces on a period, and a heartbeat arriving
        # before the first units is held rather than refused.
        zmq = pytest.importorskip("zmq")
        context = zmq.Context.instance()
        publisher = context.socket(zmq.PUB)
        port = publisher.bind_to_random_port("tcp://127.0.0.1")

        stop = threading.Event()

        def publish() -> None:
            # 30 kHz, and a covered_until that tracks wall time, so the
            # reported lag is a real number rather than a constructed one.
            # Units every fifth heartbeat: 100 ms here rather than the
            # contract's 1000 ms, so the test does not wait a second.
            t0 = time.monotonic()
            beats = 0
            while not stop.is_set():
                if beats % 5 == 0:
                    publisher.send_multipart(units_msg([3, 9, 14]))
                publisher.send_multipart(heartbeat_msg(int(30000 * (time.monotonic() - t0))))
                beats += 1
                stop.wait(0.02)

        thread = threading.Thread(target=publish, daemon=True)
        thread.start()
        try:
            rig = sim_rig(
                tmp_path,
                spikes=SpikeSourceConfig(
                    backend="sorted_stream", address=f"tcp://127.0.0.1:{port}"
                ),
            )
            result = by_name(check_rig(rig))["spikes"]
            assert result.ok, result.detail
            assert "3 units" in result.detail
            assert "lag" in result.detail
        finally:
            stop.set()
            thread.join(timeout=2.0)
            publisher.close(linger=0)

    def test_a_silent_endpoint_fails_rather_than_looking_connected(self, tmp_path):
        pytest.importorskip("zmq")
        # Nothing is bound to this port. A check that only opened the socket
        # would pass here, which is exactly the false clean bill of health
        # this backend's check exists to refuse.
        rig = sim_rig(
            tmp_path,
            spikes=SpikeSourceConfig(
                backend="sorted_stream",
                address="tcp://127.0.0.1:5999",
                heartbeat_timeout_ms=200.0,
            ),
        )
        result = by_name(check_rig(rig))["spikes"]
        assert not result.ok
        assert "no units message" in result.detail
        assert "5999" in result.detail
        # Silence and non-conformance are different faults with different
        # fixes, and must not be reported with each other's message.
        assert "never re-announced" not in result.detail


class TestFormatting:
    def test_lines_are_prefixed_by_status(self, tmp_path):
        results = check_rig(sim_rig(tmp_path))
        rendered = [format_result(r) for r in results]
        assert rendered[0].startswith("OK   config:")
