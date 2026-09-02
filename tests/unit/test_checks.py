"""check-rig: the pre-session smoke test, driven with simulated backends."""

from __future__ import annotations

import json
import sys
import threading
import time

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
from alhazen.errors import ConfigError
from alhazen.session import checks
from alhazen.session.checks import check_rig, format_result
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


class TestSortedStreamSpikes:
    """A SUB socket connects to an endpoint nobody is publishing on without
    complaining, so unlike every other device this one can only be checked
    by listening. These tests pin that it does."""

    def test_a_publishing_sorter_reports_its_units_and_lag(self, tmp_path):
        zmq = pytest.importorskip("zmq")
        context = zmq.Context.instance()
        publisher = context.socket(zmq.PUB)
        port = publisher.bind_to_random_port("tcp://127.0.0.1")

        stop = threading.Event()

        def publish() -> None:
            # 30 kHz, and a covered_until that tracks wall time, so the
            # reported lag is a real number rather than a constructed one.
            t0 = time.monotonic()
            while not stop.is_set():
                publisher.send_multipart(
                    [
                        json.dumps(
                            {
                                "type": "units",
                                "unit_ids": [3, 9, 14],
                                "labels": ["good"] * 3,
                                "sample_rate_hz": 30000,
                            }
                        ).encode()
                    ]
                )
                publisher.send_multipart(
                    [
                        json.dumps(
                            {
                                "type": "heartbeat",
                                "covered_until_sample": int(30000 * (time.monotonic() - t0)),
                            }
                        ).encode()
                    ]
                )
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

    def test_a_stream_speaking_the_wrong_protocol_fails(self, tmp_path):
        # The other way this check can be wrong: something IS publishing, so
        # the listen succeeds, but what it says is unusable. The error has to
        # come back as a FAIL, not as a detail line under an OK.
        zmq = pytest.importorskip("zmq")
        context = zmq.Context.instance()
        publisher = context.socket(zmq.PUB)
        port = publisher.bind_to_random_port("tcp://127.0.0.1")

        stop = threading.Event()

        def publish() -> None:
            # Heartbeats with no units message ever: the sample rate has
            # nowhere to come from.
            while not stop.is_set():
                publisher.send_multipart(
                    [json.dumps({"type": "heartbeat", "covered_until_sample": 1000}).encode()]
                )
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
            assert not result.ok
            assert "units" in result.detail
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


class TestFormatting:
    def test_lines_are_prefixed_by_status(self, tmp_path):
        results = check_rig(sim_rig(tmp_path))
        rendered = [format_result(r) for r in results]
        assert rendered[0].startswith("OK   config:")
