"""The spike-source device seam: the simulated backend end to end, and the
SpikeGLX backend's fetch loop against a fake connection — so the cursor,
the gap handling, the timebase and the fault path are all tested for real,
with only the vendor ctypes surface faked."""

from __future__ import annotations

import time

import numpy as np
import pytest

from alhazen.config.models import SpikeSourceConfig
from alhazen.core.events import Event
from alhazen.devices.spikes import (
    SimulatedSpikeSource,
    SpikeGLXLiveSource,
    make_spikes,
    parse_channels,
)
from alhazen.errors import SpikeSourceError
from alhazen.testing import FakeClock


def sim_config(**overrides) -> SpikeSourceConfig:
    defaults = dict(
        backend="simulated",
        sim_channels=2,
        sim_rf_centers_dva=((2.0, 2.0), (-2.0, -2.0)),
        sim_rf_sigma_dva=1.0,
        sim_baseline_hz=0.0,
        sim_peak_hz=400.0,
        sim_latency_ms=40.0,
        sim_duration_ms=60.0,
    )
    defaults.update(overrides)
    return SpikeSourceConfig(**defaults)


def probe_event(t: float, x: float, y: float) -> Event:
    return Event(
        name="PROBE_ON", t=t, trial_index=1, payload={"x_dva": x, "y_dva": y, "col": 0, "row": 0}
    )


class TestSimulatedSource:
    def test_flash_on_a_channels_field_makes_that_channel_fire(self):
        clock = FakeClock()
        source = SimulatedSpikeSource(sim_config())
        source.configure(clock)
        source.connect()
        source.start()

        source.on_event(probe_event(t=1.0, x=2.0, y=2.0))  # on channel 0's centre
        clock.advance(2.0)
        batch = source.drain()

        assert len(batch) > 0
        assert set(batch.channels.tolist()) == {0}  # channel 1 sits 5.7 dva away
        # Every spike inside [latency, latency + duration] after the flash.
        assert batch.times.min() >= 1.0 + 0.040
        assert batch.times.max() <= 1.0 + 0.040 + 0.060
        assert batch.covered_until == pytest.approx(clock.now())
        assert np.all(np.diff(batch.times) >= 0)

    def test_spikes_still_in_the_future_are_held_until_covered(self):
        clock = FakeClock()
        source = SimulatedSpikeSource(sim_config())
        source.configure(clock)
        source.start()
        clock.advance(1.0)
        source.on_event(probe_event(t=1.0, x=2.0, y=2.0))
        # Drained AT the flash time: the response is still in the future
        # (latency 40 ms), so nothing may leak out yet...
        assert len(source.drain()) == 0
        # ...and it arrives, complete, once the clock passes the window.
        clock.advance(1.0)
        assert len(source.drain()) > 0

    def test_baseline_fires_between_stimuli(self):
        clock = FakeClock()
        source = SimulatedSpikeSource(sim_config(sim_baseline_hz=50.0))
        source.configure(clock)
        source.start()
        clock.advance(4.0)
        batch = source.drain()
        # 2 channels x 50 Hz x 4 s = 400 expected; Poisson, seeded.
        assert 300 < len(batch) < 500
        assert set(batch.channels.tolist()) == {0, 1}

    def test_same_seed_same_events_same_spikes(self):
        def run() -> np.ndarray:
            clock = FakeClock()
            source = SimulatedSpikeSource(sim_config(sim_seed=7))
            source.configure(clock)
            source.start()
            source.on_event(probe_event(t=0.5, x=2.0, y=2.0))
            clock.advance(2.0)
            return source.drain().times

        np.testing.assert_array_equal(run(), run())

    def test_event_without_a_position_is_a_wiring_error(self):
        source = SimulatedSpikeSource(sim_config())
        source.configure(FakeClock())
        source.start()
        with pytest.raises(SpikeSourceError, match="x_dva"):
            source.on_event(Event(name="PROBE_ON", t=0.0, trial_index=1, payload={}))

    def test_drain_before_start_is_loud(self):
        source = SimulatedSpikeSource(sim_config())
        with pytest.raises(SpikeSourceError, match="before start"):
            source.drain()

    def test_auto_layout_spreads_channels(self):
        source = SimulatedSpikeSource(SpikeSourceConfig(backend="simulated", sim_channels=6))
        centers = source.rf_centers_dva
        assert centers.shape == (6, 2)
        # No two channels share a centre — otherwise per-channel maps could
        # never be told apart in a demo.
        assert len({tuple(np.round(c, 3)) for c in centers}) == 6

    def test_channel_ids_are_dense(self):
        assert SimulatedSpikeSource(sim_config()).channel_ids == (0, 1)


class TestChannelParsing:
    def test_ranges_and_lists(self):
        assert parse_channels("all", 5) == [0, 1, 2, 3, 4]
        assert parse_channels("0:3", 10) == [0, 1, 2, 3]
        assert parse_channels("1,4,2", 10) == [1, 4, 2]
        assert parse_channels("0:1,8:9", 10) == [0, 1, 8, 9]

    def test_errors_name_the_problem(self):
        with pytest.raises(SpikeSourceError, match="backwards"):
            parse_channels("5:2", 10)
        with pytest.raises(SpikeSourceError, match="twice"):
            parse_channels("1,1", 10)
        with pytest.raises(SpikeSourceError, match="has 4"):
            parse_channels("0:9", 4)


# ----------------------------------------------------------------------
# The SpikeGLX backend against a fake connection
# ----------------------------------------------------------------------


class FakeConnection:
    """The vendor surface, faked: a growing in-memory stream. AP count 20 so
    the default CAR floor (16) is satisfiable with 'all'."""

    def __init__(self, rate: float = 30_000.0, channels: int = 20, running: bool = True) -> None:
        self.rate = rate
        self.channels = channels
        self.running = running
        self.data = np.zeros((0, channels), dtype=np.int16)
        self.dropped_before = 0  # samples the ring buffer discarded
        self.closed = False

    def grow(self, chunk: np.ndarray) -> None:
        self.data = np.concatenate([self.data, chunk.astype(np.int16)])

    # -- the _SglxConnection surface -----------------------------------
    def is_running(self) -> bool:
        return self.running

    def sample_rate(self, js: int, ip: int) -> float:
        return self.rate

    def sample_count(self, js: int, ip: int) -> int:
        return self.dropped_before + len(self.data)

    def acq_channel_counts(self, js: int, ip: int) -> list[int]:
        return [self.channels, 0, 1]  # AP, LF, SY

    def fetch(self, js: int, ip: int, start: int, max_samps: int, channels: list[int]):
        head = max(start, self.dropped_before)
        begin = head - self.dropped_before
        block = self.data[begin : begin + max_samps][:, channels]
        return head, block.copy()

    def close(self) -> None:
        self.closed = True


def glx_config(**overrides) -> SpikeSourceConfig:
    defaults = dict(backend="spikeglx", fetch_interval_ms=5.0, threshold_sigmas=5.0, car=False)
    defaults.update(overrides)
    return SpikeSourceConfig(**defaults)


def glx_source(fake: FakeConnection, **overrides) -> SpikeGLXLiveSource:
    return SpikeGLXLiveSource(glx_config(**overrides), connection_factory=lambda: fake)


def noisy(n: int, channels: int, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, 30, size=(n, channels)).astype(np.int16)


def with_spike(n: int, channels: int, channel: int, at: int, seed: int = 0) -> np.ndarray:
    data = noisy(n, channels, seed)
    data[at : at + 10, channel] = -2000
    return data


class TestSpikeGLXSource:
    def test_connect_resolves_all_to_the_ap_channels(self):
        fake = FakeConnection(channels=20)
        source = glx_source(fake)
        source.connect()
        assert source.n_channels == 20
        assert source.channel_ids == tuple(range(20))
        assert "30000 Hz" in source.describe()

    def test_connect_refuses_a_stopped_acquisition(self):
        source = glx_source(FakeConnection(running=False))
        with pytest.raises(SpikeSourceError, match="no acquisition is running"):
            source.connect()

    def test_all_on_a_nidq_stream_is_refused(self):
        source = glx_source(FakeConnection(), stream="nidq")
        with pytest.raises(SpikeSourceError, match="imec"):
            source.connect()

    def test_car_over_too_few_channels_is_refused_at_connect(self):
        source = glx_source(FakeConnection(channels=20), channels="0:7", car=True)
        with pytest.raises(SpikeSourceError, match="car"):
            source.connect()

    def test_poll_detects_a_planted_spike_on_the_session_clock(self):
        fake = FakeConnection(channels=20)
        clock = FakeClock(start=100.0)
        source = glx_source(fake, channels="0:1")
        source.connect()  # cursor starts at the live edge: sample 0
        source.configure(clock)

        # One second of stream lands; the session clock reads 101 at the
        # moment of the fetch, so sample 30_000 maps to ~101 s and the spike
        # at sample 15_000 to ~100.5 s.
        fake.grow(with_spike(30_000, 20, channel=1, at=15_000))
        clock.advance(1.0)
        source._poll_once()

        batch = source.drain()
        assert len(batch) == 1
        assert batch.channels.tolist() == [1]
        assert batch.times[0] == pytest.approx(100.5, abs=0.01)
        # Coverage trails the stream by the detector's window half only.
        assert batch.covered_until == pytest.approx(101.0, abs=0.01)

    def test_a_ring_buffer_gap_is_survived_and_counted(self, caplog):
        fake = FakeConnection(channels=20)
        clock = FakeClock()
        source = glx_source(fake, channels="0:1")
        source.connect()
        source.configure(clock)
        fake.grow(noisy(3_000, 20))
        clock.advance(0.1)
        source._poll_once()
        # The server discards everything before sample 10_000; our cursor
        # (3_000) now points into the void.
        fake.dropped_before = 10_000
        fake.data = noisy(2_000, 20, seed=1)
        clock.advance(0.4)
        with caplog.at_level("WARNING"):
            source._poll_once()
        assert any("fell behind" in message for message in caplog.messages)
        source.drain()  # and the source is still usable

    def test_a_dead_thread_surfaces_on_drain(self):
        class DyingConnection(FakeConnection):
            # Healthy through connect() (which reads the sample count once
            # for the cursor), then the socket "tears down" under the thread.
            calls = 0

            def sample_count(self, js: int, ip: int) -> int:
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("socket torn down")
                return super().sample_count(js, ip)

        source = glx_source(DyingConnection())
        source.connect()
        source.configure(FakeClock())
        source.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                source.drain()
            except SpikeSourceError as error:
                assert "socket torn down" in str(error)
                break
            time.sleep(0.01)
        else:
            pytest.fail("the thread fault never surfaced on drain()")
        source.close()

    def test_close_is_idempotent_and_releases_the_connection(self):
        fake = FakeConnection()
        source = glx_source(fake)
        source.connect()
        source.close()
        source.close()
        assert fake.closed

    def test_start_before_connect_or_configure_is_loud(self):
        source = glx_source(FakeConnection())
        with pytest.raises(SpikeSourceError, match="connect"):
            source.start()
        source.connect()
        with pytest.raises(SpikeSourceError, match="configure"):
            source.start()


class TestFactoryAndSdk:
    def test_factory_dispatches_on_backend(self):
        assert isinstance(make_spikes(SpikeSourceConfig(backend="simulated")), SimulatedSpikeSource)
        assert isinstance(make_spikes(SpikeSourceConfig(backend="spikeglx")), SpikeGLXLiveSource)

    def test_missing_sdk_error_names_where_it_ships(self):
        # This environment has no sglx bindings, which is exactly the state
        # a fresh analysis machine is in — the error must say what to do.
        source = make_spikes(SpikeSourceConfig(backend="spikeglx"))
        with pytest.raises(SpikeSourceError, match="SpikeGLX-CPP-SDK"):
            source.connect()
