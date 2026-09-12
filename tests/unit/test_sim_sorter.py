"""The simulated sorter: does it publish the contract it claims to publish?

Two layers, for two different questions.

The schedule tests drive ``step()`` with explicit times and a recording
socket, so *what* is published and *when* is asserted exactly, with no
sockets and no waiting. They are the ones that would catch this simulator
drifting away from docs/live-spikes.md and quietly teaching a rehearsal the
wrong lesson.

The end-to-end tests point the real consumer, and then the real
``check-rig``, at a real ZeroMQ socket. Those are the point of the whole
module: a rehearsal is only worth running if a clean rehearsal predicts a
clean rig, and a broken sorter is only worth simulating if check-rig
actually refuses it.
"""

from __future__ import annotations

import json
import time

import numpy as np
import pytest

from alhazen.config.models import (
    DevicesConfig,
    DisplayConfig,
    RigConfig,
    SpikeSourceConfig,
)
from alhazen.errors import AlhazenError
from alhazen.session.checks import check_rig
from alhazen.testing.sorter import FAULTS, SortedSpikePublisher, SorterSim, describe_fault
from support import MONITOR


class RecordingSocket:
    """Stands in for a PUB socket: keeps every frame list it was handed."""

    def __init__(self) -> None:
        self.sent: list[list[bytes]] = []

    def send_multipart(self, frames: list[bytes]) -> None:
        self.sent.append(frames)

    def close(self, linger: int = 0) -> None:  # pragma: no cover - never owned here
        pass

    # What the publisher recorded, decoded the way a consumer would read it.
    def headers(self, kind: str | None = None) -> list[dict]:
        out = [json.loads(frames[0]) for frames in self.sent]
        return [h for h in out if kind is None or h["type"] == kind]


def publisher(**overrides) -> tuple[SortedSpikePublisher, RecordingSocket]:
    socket = RecordingSocket()
    pub = SortedSpikePublisher(SorterSim(**overrides), socket=socket)
    pub.bind()
    return pub, socket


def run_for(pub: SortedSpikePublisher, seconds: float, step_s: float = 0.01) -> None:
    """Step the publisher across ``seconds`` of pretend time.

    Time is supplied, never read, so these tests neither sleep nor flake.
    """
    t = 100.0  # not zero: an absolute clock that happens to start at 0 hides
    n = int(seconds / step_s)  # off-by-one errors between "now" and "elapsed"
    for i in range(n + 1):
        pub.step(t + i * step_s)


class TestTheSchedule:
    """The three message types, on their contracted periods."""

    def test_units_is_the_first_thing_published(self):
        # A subscriber that hears a timed message first has to hold it; one
        # that hears units first does not. Costs nothing to get right.
        pub, socket = publisher()
        pub.step(100.0)
        assert socket.headers()[0]["type"] == "units"

    def test_units_carries_the_sample_rate_and_the_unit_ids(self):
        pub, socket = publisher(n_units=3, sample_rate_hz=25_000.0)
        pub.step(100.0)
        header = socket.headers("units")[0]
        assert header["sample_rate_hz"] == 25_000.0
        assert len(header["unit_ids"]) == 3
        assert len(header["labels"]) == 3

    def test_unit_ids_are_not_row_indices(self):
        # A consumer that confuses the two passes against 0..n-1 and fails on
        # a real sorter, so this simulator must never hand it 0..n-1.
        assert SorterSim(n_units=4).unit_ids == (11, 18, 25, 32)

    def test_units_is_re_announced_on_its_period(self):
        # The rule the whole late-joiner path rests on: check-rig is always a
        # late joiner, so one announcement at startup is not a stream anybody
        # can join.
        pub, socket = publisher(units_period_ms=1000.0)
        run_for(pub, 3.0)
        announcements = len(socket.headers("units"))
        # Three seconds at one per second, give or take the first one.
        assert 3 <= announcements <= 4

    def test_heartbeats_keep_coming_when_nothing_fires(self):
        # Silence must be distinguishable from a quiet brain, so a stream
        # with no spikes at all still publishes coverage.
        pub, socket = publisher(firing_hz=0.0, heartbeat_period_ms=200.0)
        run_for(pub, 1.0)
        assert socket.headers("spikes") == []
        assert 5 <= len(socket.headers("heartbeat")) <= 6

    def test_a_busy_stream_still_heartbeats(self):
        # A consumer is entitled to time its watchdog off heartbeats alone; a
        # stream that stopped sending them because it was busy would starve it.
        pub, socket = publisher(firing_hz=500.0)
        run_for(pub, 1.0)
        assert socket.headers("spikes")
        assert socket.headers("heartbeat")


class TestCoverage:
    """``covered_until_sample``: the field a consumer waits on."""

    def test_coverage_never_moves_backwards(self):
        # The consumer's timebase refuses a stream position that regressed,
        # because it means a different acquisition.
        pub, socket = publisher()
        run_for(pub, 2.0)
        covered = [
            h["covered_until_sample"] for h in socket.headers() if "covered_until_sample" in h
        ]
        assert covered == sorted(covered)

    def test_coverage_tracks_elapsed_time_at_the_sample_rate(self):
        pub, socket = publisher(sample_rate_hz=30_000.0, firing_hz=0.0)
        run_for(pub, 1.0)
        last = socket.headers("heartbeat")[-1]["covered_until_sample"]
        # One second of a 30 kHz stream, within one heartbeat period of slack.
        assert 24_000 <= last <= 30_000

    def test_the_two_messages_of_one_tick_agree_about_coverage(self):
        # A spikes message and the heartbeat behind it disagreeing about how
        # much of the stream is complete is the sort of thing a consumer is
        # entitled to refuse; it must not come from here.
        pub, socket = publisher(firing_hz=2000.0, heartbeat_period_ms=100.0)
        run_for(pub, 0.5)
        by_tick: dict[int, set[int]] = {}
        for header in socket.headers():
            if "covered_until_sample" not in header:
                continue
            by_tick.setdefault(header["covered_until_sample"], set()).add(header["type"])
        # Every coverage value carries a heartbeat, and the spikes sharing it
        # carry the same number by construction.
        assert all("heartbeat" in kinds for kinds in by_tick.values())

    def test_no_spike_is_published_past_the_coverage_that_carries_it(self):
        # "Complete up to here" is a claim; a spike beyond it would make the
        # claim false and teach a consumer to distrust the field it waits on.
        pub, socket = publisher(firing_hz=2000.0)
        run_for(pub, 1.0)
        for frames in pub_spikes(socket):
            header = json.loads(frames[0])
            samples = np.frombuffer(frames[1], np.int64)
            assert samples.max() < header["covered_until_sample"]


def pub_spikes(socket: RecordingSocket) -> list[list[bytes]]:
    return [f for f in socket.sent if json.loads(f[0])["type"] == "spikes"]


class TestSpikeFrames:
    """The three-frame layout, which a consumer decodes without asking."""

    def test_the_header_count_matches_the_arrays(self):
        pub, socket = publisher(firing_hz=1000.0)
        run_for(pub, 1.0)
        frames_list = pub_spikes(socket)
        assert frames_list, "a 1000 Hz unit should fire within a second"
        for frames in frames_list:
            header = json.loads(frames[0])
            assert len(frames) == 3
            assert np.frombuffer(frames[1], np.int64).size == header["n"]
            assert np.frombuffer(frames[2], np.int32).size == header["n"]

    def test_spikes_arrive_in_time_order(self):
        pub, socket = publisher(firing_hz=1000.0)
        run_for(pub, 1.0)
        for frames in pub_spikes(socket):
            samples = np.frombuffer(frames[1], np.int64)
            assert (np.diff(samples) >= 0).all()

    def test_every_spike_belongs_to_an_announced_unit(self):
        pub, socket = publisher(n_units=3, firing_hz=1000.0)
        run_for(pub, 1.0)
        announced = set(SorterSim(n_units=3).unit_ids)
        for frames in pub_spikes(socket):
            assert set(np.frombuffer(frames[2], np.int32).tolist()) <= announced

    def test_the_same_seed_publishes_the_same_spikes(self):
        # Two rehearsals producing the same numbers is what makes a
        # difference between them mean something.
        def train(seed: int) -> list[int]:
            pub, socket = publisher(seed=seed, firing_hz=200.0)
            run_for(pub, 1.0)
            return [int(s) for f in pub_spikes(socket) for s in np.frombuffer(f[1], np.int64)]

        assert train(7) == train(7)
        assert train(7) != train(8)


class TestSequenceNumbers:
    def test_timed_messages_are_numbered_in_order(self):
        # The only way a dropped message is noticeable at all: a PUB socket
        # discards for a slow subscriber and tells neither end.
        pub, socket = publisher(firing_hz=500.0)
        run_for(pub, 1.0)
        seqs = [h["seq"] for h in socket.headers() if "seq" in h]
        assert seqs == list(range(len(seqs)))

    def test_units_messages_carry_no_sequence_number(self):
        # They are not timed messages; numbering them would put gaps in the
        # sequence a consumer is counting.
        pub, socket = publisher()
        run_for(pub, 2.0)
        assert all("seq" not in h for h in socket.headers("units"))


class TestFaults:
    """The non-conformances, produced on purpose."""

    def test_silent_binds_and_says_nothing(self):
        pub, socket = publisher(fault="silent")
        run_for(pub, 3.0)
        assert socket.sent == []

    def test_never_units_publishes_everything_but_units(self):
        pub, socket = publisher(fault="never_units")
        run_for(pub, 3.0)
        assert socket.headers("units") == []
        assert socket.headers("heartbeat")

    def test_announce_once_announces_exactly_once(self):
        # The naive implementation, and the one a lab writing its own sorter
        # will actually ship: correct for whoever watched it start, invisible
        # to everyone else.
        pub, socket = publisher(fault="announce_once")
        run_for(pub, 5.0)
        assert len(socket.headers("units")) == 1
        assert socket.headers("heartbeat")

    def test_no_seq_publishes_conformant_messages_without_numbers(self):
        pub, socket = publisher(fault="no_seq", firing_hz=500.0)
        run_for(pub, 1.0)
        assert socket.headers("units")
        assert all("seq" not in h for h in socket.headers())

    def test_every_fault_has_a_description(self):
        # The CLI prints one per run; a fault added without one would print a
        # KeyError at the moment somebody is trying to rehearse a failure.
        for fault in FAULTS:
            assert describe_fault(fault)


class TestRefusals:
    """Bad settings fail at construction, not by publishing something else."""

    @pytest.mark.parametrize(
        "overrides",
        [
            {"n_units": 0},
            {"sample_rate_hz": 0.0},
            {"firing_hz": -1.0},
            {"heartbeat_period_ms": 0.0},
            {"units_period_ms": -5.0},
            {"fault": "explode"},
        ],
    )
    def test_bad_settings_are_refused(self, overrides):
        with pytest.raises(AlhazenError):
            SorterSim(**overrides)

    def test_a_clock_that_runs_backwards_is_refused(self):
        # Clamping instead would publish a coverage that regressed, and the
        # consumer refuses that as "the acquisition restarted" — sending
        # whoever made the mistake to debug the wrong program.
        pub, _socket = publisher()
        pub.step(100.0)
        with pytest.raises(AlhazenError, match="backwards in time"):
            pub.step(99.0)

    def test_stepping_before_binding_is_refused(self):
        pub = SortedSpikePublisher(SorterSim())
        with pytest.raises(AlhazenError, match="bind"):
            pub.step(100.0)


# ----------------------------------------------------------------------
# End to end: a real socket, the real consumer, the real check
# ----------------------------------------------------------------------


def rehearsal_rig(tmp_path, address: str, timeout_ms: float = 2000.0) -> RigConfig:
    return RigConfig(
        monitor=MONITOR,
        display=DisplayConfig(backend="simulated"),
        devices=DevicesConfig(
            spikes=SpikeSourceConfig(
                backend="sorted_stream",
                address=address,
                fetch_interval_ms=20.0,
                heartbeat_timeout_ms=timeout_ms,
            )
        ),
        data_root=tmp_path / "data",
    )


def live_publisher(**overrides):
    """A publisher on a real socket, on a port nobody else is using."""
    pytest.importorskip("zmq")
    cfg = SorterSim(address="tcp://127.0.0.1:*", heartbeat_period_ms=20.0, **overrides)
    pub = SortedSpikePublisher(cfg)
    pub.bind()
    return pub


def pump(pub: SortedSpikePublisher, seconds: float) -> None:
    """Publish in real time for a while, from this thread.

    Foreground rather than a background thread: a test that fails while a
    publisher thread is still running leaves the failure and the cleanup
    racing, and the point here is short waits anyway.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pub.step(time.monotonic())
        time.sleep(0.005)


class TestAgainstTheRealCheck:
    def test_a_conformant_sorter_passes_check_rig(self, tmp_path):
        """The claim the whole rehearsal rests on: this publisher makes the
        real spikes check pass, through a real socket, for real reasons."""
        pub = live_publisher()
        rig = rehearsal_rig(tmp_path, pub.address, timeout_ms=1000.0)
        try:
            # check_rig listens synchronously, so the publisher has to be
            # pumped from somewhere: a short pre-roll establishes the
            # subscription (PUB drops everything published before one
            # exists), then check_rig's own wait is covered by the
            # re-announcement period, which is what makes a late joiner work.
            import threading

            stop = threading.Event()

            def keep_publishing():
                while not stop.is_set():
                    pub.step(time.monotonic())
                    time.sleep(0.005)

            thread = threading.Thread(target=keep_publishing, daemon=True)
            thread.start()
            try:
                result = {r.name: r for r in check_rig(rig)}["spikes"]
            finally:
                stop.set()
                thread.join(timeout=2.0)
        finally:
            pub.close()
        assert result.ok, result.detail
        assert "units @ 30000 Hz" in result.detail
        assert "lag" in result.detail

    def test_a_silent_sorter_fails_as_a_sorter_that_is_not_running(self, tmp_path):
        # Nothing is published, so nothing distinguishes this from a dead
        # process — and the message must send the experimenter to the sorter,
        # not to the wire contract.
        pub = live_publisher(fault="silent")
        rig = rehearsal_rig(tmp_path, pub.address, timeout_ms=500.0)
        try:
            result = {r.name: r for r in check_rig(rig)}["spikes"]
        finally:
            pub.close()
        assert not result.ok
        assert "is the real-time sorter running and publishing?" in result.detail

    def test_a_sorter_that_announced_once_fails_as_non_conformant(self, tmp_path):
        # The other failure, and the one that matters: something IS
        # publishing, so the message must say the sorter is alive and
        # non-conformant rather than sending anyone to restart it.
        pub = live_publisher(fault="announce_once")
        rig = rehearsal_rig(tmp_path, pub.address, timeout_ms=500.0)
        import threading

        stop = threading.Event()

        def keep_publishing():
            while not stop.is_set():
                pub.step(time.monotonic())
                time.sleep(0.005)

        thread = threading.Thread(target=keep_publishing, daemon=True)
        thread.start()
        # Let the single announcement go out and be missed, exactly as it is
        # missed by a check-rig run minutes after the sorter started.
        time.sleep(0.2)
        try:
            result = {r.name: r for r in check_rig(rig)}["spikes"]
        finally:
            stop.set()
            thread.join(timeout=2.0)
            pub.close()
        assert not result.ok
        assert "never re-announced units" in result.detail


class TestTheDocumentedRehearsal:
    """The exact procedure docs/pre-session-checkout.md tells people to run.

    Not a re-test of the pieces: this loads the shipped YAML off disk and
    checks every line of it, so the documented rehearsal cannot rot into a
    procedure that no longer works while the unit tests stay green.
    """

    def test_the_rehearsal_rig_passes_every_check_with_pulse(self, tmp_path):
        from pathlib import Path

        from alhazen.config.loader import load_rig

        rig_path = Path(__file__).parents[2] / "examples" / "rig-rehearsal.yaml"
        pub = live_publisher()
        shipped = load_rig(rig_path)
        # Two things the shipped config does that a test must not: write
        # beside the repo, and claim a fixed port. Everything else — every
        # device, every backend — is exactly what ships.
        spikes = shipped.devices.spikes.model_copy(
            update={"address": pub.address, "heartbeat_timeout_ms": 1000.0}
        )
        rig = shipped.model_copy(
            update={
                "data_root": tmp_path / "data",
                "devices": shipped.devices.model_copy(update={"spikes": spikes}),
            }
        )
        import threading

        stop = threading.Event()

        def keep_publishing():
            while not stop.is_set():
                pub.step(time.monotonic())
                time.sleep(0.005)

        thread = threading.Thread(target=keep_publishing, daemon=True)
        thread.start()
        try:
            # --pulse, because that is what the doc says to run: it fires the
            # reward and every sync line, and those paths are half the point.
            results = check_rig(rig, pulse=True)
        finally:
            stop.set()
            thread.join(timeout=2.0)
            pub.close()
        failed = [f"{r.name}: {r.detail}" for r in results if not r.ok]
        assert not failed, failed
        # Every device the framework has, so every check line is exercised —
        # a rehearsal that silently stopped covering one would be worse than
        # no rehearsal.
        assert {r.name for r in results} == {
            "config",
            "monitor",
            "data_root",
            "eyetracker",
            "reward",
            "sync",
            "recording",
            "spikes",
        }
