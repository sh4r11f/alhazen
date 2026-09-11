"""The ``sorted_stream`` spike source: units published live by an external sorter.

The seam under test is the subscriber, so the wire format, the row map, the
timebase, the watchdog and the fault path are all exercised for real, with
only the socket faked. The fake is a queue of multipart frames the test
fills, which is exactly what a ZeroMQ SUB socket hands back.

Two facts shape almost every test here:

- ``poll_once`` drains the socket until it is empty, so a test that wants
  the session clock to move *between* two fetches has to fill the queue
  twice. Filling it once and polling twice reads both messages at the same
  instant, which is not what a live stream does and hides the offset
  estimator's behaviour.
- The tests drive ``poll_once`` directly instead of calling ``start()``,
  the same way ``TestSpikeGLXSource`` drives ``_poll_once``. A background
  thread polling the same fake queue would race with the test.
"""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest

from alhazen.config.models import SpikeSourceConfig
from alhazen.devices.spikes import UNITS_GRACE_MS, SortedStreamSource, make_spikes
from alhazen.errors import SpikeSourceError
from alhazen.testing import FakeClock

RATE = 30_000.0


class FakeSubscriber:
    """A queue of multipart frames, with the socket's own timeout contract.

    ``recv`` returns None when nothing is waiting, which is what the real
    subscriber returns when its poll times out.
    """

    def __init__(self) -> None:
        self.frames: list[list[bytes]] = []
        self.closed = False
        self._lock = threading.Lock()

    def recv(self, timeout_ms: float) -> list[bytes] | None:
        with self._lock:
            return self.frames.pop(0) if self.frames else None

    def send(self, *messages: list[bytes]) -> None:
        with self._lock:
            self.frames.extend(messages)

    def close(self) -> None:
        self.closed = True


# ----------------------------------------------------------------------
# Message builders — the wire contract, in one place
# ----------------------------------------------------------------------


def units_msg(ids, rate: float = RATE, labels=None) -> list[bytes]:
    """The message that carries the sample rate, and must come first."""
    header = {
        "type": "units",
        "unit_ids": list(ids),
        "labels": list(labels) if labels is not None else ["good"] * len(list(ids)),
        "sample_rate_hz": rate,
    }
    return [json.dumps(header).encode()]


def spikes_msg(samples, units, covered, seq: int | None = None) -> list[bytes]:
    header = {
        "type": "spikes",
        "stream": "imec0",
        "covered_until_sample": int(covered),
        "n": len(list(samples)),
    }
    if seq is not None:
        header["seq"] = seq
    return [
        json.dumps(header).encode(),
        np.asarray(samples, np.int64).tobytes(),
        np.asarray(units, np.int32).tobytes(),
    ]


def heartbeat_msg(covered, seq: int | None = None) -> list[bytes]:
    header = {"type": "heartbeat", "covered_until_sample": int(covered)}
    if seq is not None:
        header["seq"] = seq
    return [json.dumps(header).encode()]


def make_source(sub: FakeSubscriber, clock: FakeClock | None = None, **overrides):
    """A connected source on a fake socket, ready to be polled by hand."""
    cfg = SpikeSourceConfig(backend="sorted_stream", address="tcp://x:1", **overrides)
    source = SortedStreamSource(cfg, subscriber_factory=lambda: sub)
    # The builder's order: connect() opens the socket, configure() hands
    # over the session clock, start() spawns the thread. These tests skip
    # start() and poll by hand.
    source.connect()
    source.configure(clock if clock is not None else FakeClock(10.0))
    return source


class TestRowMap:
    def test_rows_are_units_in_first_seen_order_and_only_grow(self):
        sub = FakeSubscriber()
        clock = FakeClock(10.0)
        source = make_source(sub, clock)

        # Fetch one: two units, two spikes, one second of stream covered.
        sub.send(units_msg([7, 3]), spikes_msg([30_000, 30_010], [3, 7], covered=30_100))
        source.poll_once()

        # Two seconds later a third unit appears. The clock advances by the
        # same two seconds the stream did, so the two clocks agree and the
        # offset estimate does not move — otherwise the later spike would
        # be pulled back on top of the earlier ones and the ordering
        # assertion below would be testing the estimator, not the row map.
        clock.advance(2.0)
        sub.send(units_msg([7, 3, 9]), spikes_msg([90_000], [9], covered=90_100))
        source.poll_once()

        batch = source.drain()
        assert source.channel_ids == (7, 3, 9)  # first-seen order, only grows
        assert source.n_channels == 3
        assert batch.channels.tolist() == [1, 0, 2]  # unit 3 -> row 1, 7 -> row 0, 9 -> row 2
        assert np.all(np.diff(batch.times) >= 0)
        assert batch.covered_until is not None

    def test_a_unit_seen_again_keeps_its_row(self):
        # The row map only grows, so re-announcing the same units must not
        # renumber them: a consumer's per-row history has to stay valid.
        sub = FakeSubscriber()
        source = make_source(sub)
        sub.send(units_msg([5, 2]), units_msg([2, 5, 8]), units_msg([8, 5, 2]))
        source.poll_once()
        assert source.channel_ids == (5, 2, 8)

    def test_a_unit_that_spikes_without_being_announced_still_gets_a_row(self):
        # The units list can lag a spike by one message. Dropping the spike
        # would undercount the newest unit exactly when it appears, so it
        # gets a row and the announcement, when it comes, changes nothing.
        sub = FakeSubscriber()
        source = make_source(sub)
        sub.send(units_msg([1]), spikes_msg([30_000, 30_001], [1, 4], covered=30_100))
        source.poll_once()
        assert source.channel_ids == (1, 4)
        assert source.drain().channels.tolist() == [0, 1]


class TestTimebase:
    def test_times_land_on_the_session_clock_via_covered_until(self):
        sub = FakeSubscriber()
        source = make_source(sub, FakeClock(10.0))  # clock reads 10.0 s at receipt
        sub.send(units_msg([1]), spikes_msg([60_000], [1], covered=60_000))
        source.poll_once()

        batch = source.drain()
        # covered_until_sample 60000 at 30 kHz is 2.0 s of stream, received
        # at t = 10.0: offset = 10.0 - 2.0 = 8.0, so the spike at sample
        # 60000 lands at 10.0 s.
        assert batch.times[0] == pytest.approx(10.0, abs=1e-6)
        assert batch.covered_until == pytest.approx(10.0, abs=1e-6)

    def test_a_heartbeat_advances_coverage_with_no_spikes(self):
        # This is what lets a consumer wait for a window to be covered
        # during a silent stretch: no spikes does not mean no progress.
        sub = FakeSubscriber()
        clock = FakeClock(10.0)
        source = make_source(sub, clock)
        sub.send(units_msg([1]), heartbeat_msg(covered=30_000))
        source.poll_once()
        first = source.drain()
        assert len(first) == 0
        assert first.covered_until == pytest.approx(10.0, abs=1e-6)

        clock.advance(1.0)
        sub.send(heartbeat_msg(covered=60_000))
        source.poll_once()
        assert source.drain().covered_until == pytest.approx(11.0, abs=1e-6)

    def test_coverage_that_runs_backwards_is_refused(self):
        # Two streams crossed, or an acquisition restarted. Either way every
        # earlier observation describes a different timeline, and placing
        # the next spike on the old one would be quietly wrong.
        sub = FakeSubscriber()
        source = make_source(sub)
        sub.send(units_msg([1]), heartbeat_msg(covered=60_000))
        source.poll_once()
        sub.send(heartbeat_msg(covered=30_000))
        with pytest.raises(SpikeSourceError, match="backwards"):
            source.poll_once()

    def test_drain_before_anything_arrives_is_empty_with_no_coverage(self):
        sub = FakeSubscriber()
        source = make_source(sub)
        source.poll_once()
        batch = source.drain()
        assert len(batch) == 0
        assert batch.covered_until is None


class TestLateJoiner:
    """Every subscriber to a PUB socket joins a stream already in progress,
    so timed messages before the first ``units`` are the normal case, not a
    protocol error. They are held until the sample rate arrives — and the
    sorter that never sends one is the real fault (docs/live-spikes.md)."""

    def test_a_heartbeat_before_units_is_held_not_refused(self):
        sub = FakeSubscriber()
        clock = FakeClock(10.0)
        source = make_source(sub, clock)
        sub.send(heartbeat_msg(covered=30_000))
        source.poll_once()  # must not raise: this is a late joiner, not a bad sorter
        assert source.awaiting_units
        assert "waiting for it to re-announce units" in source.describe()
        # Nothing can be placed yet, and the source says so rather than
        # inventing a timebase.
        assert source.drain().covered_until is None

        clock.advance(0.1)
        sub.send(units_msg([1]))
        source.poll_once()
        assert not source.awaiting_units
        # The held heartbeat was placed once the rate was known, so coverage
        # is available from the very first drain after the announcement —
        # and it is the coverage that heartbeat carried, at the time that
        # heartbeat arrived (10.0 s, one second of stream behind it).
        assert source.drain().covered_until == pytest.approx(10.0)

    def test_spikes_before_units_are_placed_once_units_arrives(self):
        # Dropping them would undercount the first window of every session
        # in which the sorter was started before alhazen was — silently,
        # which is the failure mode this whole module is written against.
        sub = FakeSubscriber()
        clock = FakeClock(10.0)
        source = make_source(sub, clock)
        sub.send(spikes_msg([30_000, 30_030], [7, 3], covered=30_100))
        source.poll_once()
        assert source.drain().times.size == 0  # unplaceable, so not yet handed over

        sub.send(units_msg([7, 3]))
        source.poll_once()
        batch = source.drain()
        assert batch.times.size == 2
        # Placed with the offset from the arrival time the messages actually
        # had, not from the replay, and 1 ms apart as they were on the wire.
        assert batch.times[1] - batch.times[0] == pytest.approx(30 / RATE)
        assert batch.times[0] == pytest.approx(10.0 - 100 / RATE)
        # And the rows follow first-seen order from the spikes themselves.
        assert source.channel_ids == (7, 3)

    def test_a_stream_that_never_announces_units_faults_after_the_grace(self):
        sub = FakeSubscriber()
        clock = FakeClock(0.0)
        source = make_source(sub, clock, heartbeat_timeout_ms=100.0)
        sub.send(heartbeat_msg(covered=30_000))
        source.poll_once()
        # Inside the grace this is still just a late joiner, even though it
        # is well past the heartbeat timeout — silence is not the problem.
        clock.advance(0.5)
        sub.send(heartbeat_msg(covered=36_000))
        source.poll_once()
        source.drain()

        clock.advance(UNITS_GRACE_MS / 1000.0)
        sub.send(heartbeat_msg(covered=42_000))
        source.poll_once()
        with pytest.raises(SpikeSourceError, match="never re-announced units"):
            source.drain()

    def test_the_grace_fault_names_the_period_and_the_stakes(self):
        # Heartbeats throughout, so the stream is demonstrably not silent
        # and the fault that fires is the one about the contract.
        sub = FakeSubscriber()
        clock = FakeClock(0.0)
        source = make_source(sub, clock, heartbeat_timeout_ms=100.0)
        sub.send(heartbeat_msg(covered=30_000))
        source.poll_once()
        clock.advance(1.0 + UNITS_GRACE_MS / 1000.0)
        sub.send(heartbeat_msg(covered=60_000))
        source.poll_once()
        with pytest.raises(SpikeSourceError, match=r"at least every 1000 ms.*same instant"):
            source.drain()

    def test_the_grace_is_never_shorter_than_the_silence_budget(self):
        # A rig that widened heartbeat_timeout_ms has said its stream is
        # slow, not that it wants announcements judged more harshly.
        sub = FakeSubscriber()
        clock = FakeClock(0.0)
        source = make_source(sub, clock, heartbeat_timeout_ms=10_000.0)
        sub.send(heartbeat_msg(covered=30_000))
        source.poll_once()
        clock.advance(UNITS_GRACE_MS / 1000.0 + 0.5)
        sub.send(heartbeat_msg(covered=60_000))
        source.poll_once()
        source.drain()  # the default grace has passed; this stream's has not


class TestProtocolErrors:
    def test_a_units_message_with_no_rate_is_refused(self):
        sub = FakeSubscriber()
        source = make_source(sub)
        sub.send([json.dumps({"type": "units", "unit_ids": [1], "labels": ["good"]}).encode()])
        with pytest.raises(SpikeSourceError, match="sample_rate_hz"):
            source.poll_once()

    def test_a_units_message_with_a_zero_rate_is_refused(self):
        sub = FakeSubscriber()
        source = make_source(sub)
        sub.send(units_msg([1], rate=0.0))
        with pytest.raises(SpikeSourceError, match="sample_rate_hz"):
            source.poll_once()

    def test_an_unknown_message_type_is_refused(self):
        # A private contract between two packages in one lab: a type nobody
        # implements is a typo or a version skew, and reading past it would
        # mean silently ignoring whatever it was carrying.
        sub = FakeSubscriber()
        source = make_source(sub)
        sub.send(units_msg([1]), [json.dumps({"type": "waveforms"}).encode()])
        with pytest.raises(SpikeSourceError, match="waveforms"):
            source.poll_once()

    def test_a_spikes_message_whose_arrays_disagree_with_n_is_refused(self):
        sub = FakeSubscriber()
        source = make_source(sub)
        bad = spikes_msg([30_000, 30_001], [1, 1], covered=30_100)
        bad[0] = json.dumps(
            {"type": "spikes", "stream": "imec0", "covered_until_sample": 30_100, "n": 5}
        ).encode()
        sub.send(units_msg([1]), bad)
        with pytest.raises(SpikeSourceError, match="5"):
            source.poll_once()

    def test_a_spikes_message_missing_its_array_frames_is_refused(self):
        sub = FakeSubscriber()
        source = make_source(sub)
        sub.send(
            units_msg([1]),
            [json.dumps({"type": "spikes", "covered_until_sample": 30_100, "n": 1}).encode()],
        )
        with pytest.raises(SpikeSourceError, match="must carry three"):
            source.poll_once()

    def test_a_header_that_is_not_json_is_refused(self):
        sub = FakeSubscriber()
        source = make_source(sub)
        sub.send([b"not json at all"])
        with pytest.raises(SpikeSourceError, match="JSON"):
            source.poll_once()

    def test_polling_before_configure_is_refused(self):
        sub = FakeSubscriber()
        cfg = SpikeSourceConfig(backend="sorted_stream", address="tcp://x:1")
        source = SortedStreamSource(cfg, subscriber_factory=lambda: sub)
        source.connect()
        with pytest.raises(SpikeSourceError, match="configure"):
            source.poll_once()

    def test_polling_before_connect_is_refused(self):
        cfg = SpikeSourceConfig(backend="sorted_stream", address="tcp://x:1")
        source = SortedStreamSource(cfg, subscriber_factory=lambda: FakeSubscriber())
        source.configure(FakeClock())
        with pytest.raises(SpikeSourceError, match="connect"):
            source.poll_once()


class TestDroppedMessages:
    def test_a_gap_in_the_sequence_is_counted_and_logged(self, caplog):
        # ZeroMQ PUB drops messages for a slow subscriber rather than
        # blocking the publisher, and a dropped spikes message would
        # silently undercount a decoder's features. The optional seq is the
        # only way to know it happened, so a gap is loud.
        sub = FakeSubscriber()
        clock = FakeClock(10.0)
        source = make_source(sub, clock)
        sub.send(units_msg([1]), spikes_msg([30_000], [1], covered=30_100, seq=0))
        source.poll_once()
        clock.advance(1.0)
        sub.send(spikes_msg([60_000], [1], covered=60_100, seq=4))
        with caplog.at_level("WARNING"):
            source.poll_once()
        assert any("dropped" in message for message in caplog.messages)
        assert source.dropped_messages == 3
        assert "3 dropped" in source.describe()

    def test_without_a_sequence_number_the_source_says_it_cannot_tell(self):
        sub = FakeSubscriber()
        source = make_source(sub)
        sub.send(units_msg([1]), spikes_msg([30_000], [1], covered=30_100))
        source.poll_once()
        assert source.dropped_messages is None
        assert "drops undetectable" in source.describe()


class TestWatchdog:
    def test_silent_stream_is_a_loud_fault(self):
        sub = FakeSubscriber()
        clock = FakeClock(0.0)
        source = make_source(sub, clock, heartbeat_timeout_ms=1.0)
        sub.send(units_msg([1]))
        source.poll_once()
        clock.advance(5.0)
        source.poll_once()
        with pytest.raises(SpikeSourceError, match="heartbeat"):
            source.drain()

    def test_a_message_resets_the_watchdog(self):
        sub = FakeSubscriber()
        clock = FakeClock(0.0)
        source = make_source(sub, clock, heartbeat_timeout_ms=2000.0)
        sub.send(units_msg([1]))
        source.poll_once()
        for _ in range(4):
            clock.advance(1.0)
            sub.send(heartbeat_msg(covered=int(RATE * clock.now())))
            source.poll_once()
        source.drain()  # four seconds of silence would have faulted; heartbeats did not

    def test_the_fault_names_the_address_and_the_limit(self):
        sub = FakeSubscriber()
        clock = FakeClock(0.0)
        source = make_source(sub, clock, heartbeat_timeout_ms=50.0)
        sub.send(units_msg([1]))
        source.poll_once()
        clock.advance(3.0)
        source.poll_once()
        with pytest.raises(SpikeSourceError, match=r"tcp://x:1.*3\.0 s.*50"):
            source.drain()


class TestThreadAndLifecycle:
    def test_start_polls_in_the_background(self):
        # The one test that runs the real thread: everything else drives
        # poll_once by hand, so without this the loop itself is untested.
        sub = FakeSubscriber()
        clock = FakeClock(10.0)
        cfg = SpikeSourceConfig(backend="sorted_stream", address="tcp://x:1", fetch_interval_ms=1.0)
        source = SortedStreamSource(cfg, subscriber_factory=lambda: sub)
        source.connect()
        source.configure(clock)
        sub.send(units_msg([1]), spikes_msg([30_000], [1], covered=30_100))
        source.start()
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and source.n_channels == 0:
                time.sleep(0.005)
            assert source.channel_ids == (1,)
            assert len(source.drain()) == 1
        finally:
            source.close()
        assert sub.closed

    def test_a_fault_in_the_thread_surfaces_on_drain(self):
        sub = FakeSubscriber()
        cfg = SpikeSourceConfig(backend="sorted_stream", address="tcp://x:1")
        source = SortedStreamSource(cfg, subscriber_factory=lambda: sub)
        source.connect()
        source.configure(FakeClock(10.0))
        sub.send([b"not json at all"])  # a protocol error, raised inside the thread
        source.start()
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    source.drain()
                except SpikeSourceError as error:
                    assert "JSON" in str(error)
                    break
                time.sleep(0.005)
            else:  # pragma: no cover - only on a broken fault path
                pytest.fail("the protocol error never reached the session thread")
        finally:
            source.close()

    def test_starting_twice_is_refused(self):
        sub = FakeSubscriber()
        source = make_source(sub)
        source.start()
        try:
            with pytest.raises(SpikeSourceError, match="twice"):
                source.start()
        finally:
            source.close()

    def test_starting_before_connect_is_refused(self):
        cfg = SpikeSourceConfig(backend="sorted_stream", address="tcp://x:1")
        source = SortedStreamSource(cfg, subscriber_factory=lambda: FakeSubscriber())
        source.configure(FakeClock())
        with pytest.raises(SpikeSourceError, match="connect"):
            source.start()

    def test_close_without_start_still_closes_the_socket(self):
        sub = FakeSubscriber()
        source = make_source(sub)
        source.close()
        assert sub.closed

    def test_describe_names_the_address_and_what_has_been_seen(self):
        sub = FakeSubscriber()
        source = make_source(sub)
        assert "tcp://x:1" in source.describe()
        sub.send(units_msg([7, 3]))
        source.poll_once()
        assert "2 units" in source.describe()
        assert "30000" in source.describe()


class TestConfigAndFactory:
    def test_make_spikes_builds_a_sorted_stream(self):
        source = make_spikes(SpikeSourceConfig(backend="sorted_stream", address="tcp://x:1"))
        assert isinstance(source, SortedStreamSource)

    def test_config_refuses_spikeglx_fields_on_sorted_stream(self):
        with pytest.raises(ValueError, match="ignores"):
            SpikeSourceConfig(backend="sorted_stream", channels="0:10")

    def test_config_refuses_simulated_fields_on_sorted_stream(self):
        with pytest.raises(ValueError, match="ignores"):
            SpikeSourceConfig(backend="sorted_stream", sim_peak_hz=10.0)

    def test_config_refuses_sorted_stream_fields_on_the_other_backends(self):
        with pytest.raises(ValueError, match="ignores"):
            SpikeSourceConfig(backend="spikeglx", address="tcp://x:1")
        with pytest.raises(ValueError, match="ignores"):
            SpikeSourceConfig(backend="simulated", heartbeat_timeout_ms=100.0)

    def test_fetch_interval_is_shared_by_the_two_live_backends(self):
        # Both run a background poll loop, so both mean something by it;
        # the simulated backend has no loop and must still refuse it.
        assert SpikeSourceConfig(backend="sorted_stream", fetch_interval_ms=50.0)
        assert SpikeSourceConfig(backend="spikeglx", fetch_interval_ms=50.0)
        with pytest.raises(ValueError, match="ignores"):
            SpikeSourceConfig(backend="simulated", fetch_interval_ms=50.0)

    def test_an_address_with_no_transport_is_refused(self):
        with pytest.raises(ValueError, match="address"):
            SpikeSourceConfig(backend="sorted_stream", address="192.168.1.50:5556")

    def test_a_non_positive_heartbeat_timeout_is_refused(self):
        with pytest.raises(ValueError, match="heartbeat_timeout_ms"):
            SpikeSourceConfig(backend="sorted_stream", heartbeat_timeout_ms=0.0)


class TestRealSocket:
    """The one path the fake subscriber cannot cover: the socket itself.

    Everything above proves the message handling; this proves the frames
    survive an actual ZeroMQ PUB/SUB hop, which is where a wrong socket
    option or a mis-packed array would show up and nowhere else.
    """

    def test_frames_survive_a_real_pub_sub_hop(self):
        zmq = pytest.importorskip("zmq")
        from alhazen.devices.spikes import _ZmqSubscriber

        context = zmq.Context.instance()
        publisher = context.socket(zmq.PUB)
        port = publisher.bind_to_random_port("tcp://127.0.0.1")
        address = f"tcp://127.0.0.1:{port}"

        cfg = SpikeSourceConfig(backend="sorted_stream", address=address)
        source = SortedStreamSource(cfg, subscriber_factory=lambda: _ZmqSubscriber(address))
        clock = FakeClock(10.0)
        source.connect()
        source.configure(clock)
        try:
            # PUB drops anything published before the subscription is
            # established, and there is no way to be told when that is, so
            # the messages are republished until they land.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and source.n_channels == 0:
                publisher.send_multipart(units_msg([11, 12]))
                time.sleep(0.02)
                source.poll_once()
            assert source.channel_ids == (11, 12)

            deadline = time.monotonic() + 5.0
            batch = source.drain()
            while time.monotonic() < deadline and len(batch) == 0:
                publisher.send_multipart(spikes_msg([60_000], [12], covered=60_000))
                time.sleep(0.02)
                source.poll_once()
                batch = source.drain()
            assert batch.channels.tolist() == [1]  # unit 12 took row 1
            assert batch.times[0] == pytest.approx(10.0, abs=1e-6)
        finally:
            source.close()
            publisher.close(linger=0)

    def test_a_missing_pyzmq_names_the_extra(self, monkeypatch):
        # The import is inside __init__ so that `import alhazen` works
        # without pyzmq; this is the error an experimenter sees on a rig
        # where the extra was never installed.
        import builtins

        from alhazen.devices.spikes import _ZmqSubscriber

        real_import = builtins.__import__

        def refuse_zmq(name, *args, **kwargs):
            if name == "zmq":
                raise ImportError("no module named zmq")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse_zmq)
        with pytest.raises(SpikeSourceError, match=r"alhazen-vision\[zmq\]"):
            _ZmqSubscriber("tcp://127.0.0.1:5556")
