"""A sorted-spike publisher in software: the other half of the sorted_stream seam.

Every device seam in alhazen has a simulated sibling, so the whole pipeline
runs with no hardware — except one. The ``sorted_stream`` spike backend's
counterpart is not a device at all: it is somebody else's program, a
real-time spike sorter, publishing on a ZeroMQ socket from another machine.
There is nothing inside this repository to simulate, which is exactly why the
one line of ``alhazen check-rig`` that cannot be rehearsed anywhere is the one
most likely to be met for the first time with a subject already in the chair.

This module is that missing half. It publishes the wire contract of
``docs/live-spikes.md`` — ``units`` re-announced on a period, ``heartbeat`` on
a period, ``spikes`` with sample indices and unit ids — so that:

- ``alhazen check-rig --pulse`` runs end to end, with its ``spikes`` line
  genuinely passing, on a machine with no probe in anything;
- the two ways that check *fails* can be rehearsed rather than trusted. The
  ``fault`` setting publishes the specific non-conformances the contract names,
  so the failure an experimenter will one day read at 9am is one they have
  already seen, and the test suite can assert that check-rig refuses each of
  them instead of quietly passing;
- a lab's real sorter has something to be compared against. Point the same
  consumer at both: if this one works and theirs does not, the difference is
  in their publisher, and ``docs/live-spikes.md`` says which rule it broke.

**This is a transport simulator, not a spike simulator.** The spike times here
are Poisson noise with no receptive fields, no stimulus coupling, and no
relationship to anything on the screen. ``backend: simulated`` in
``devices/spikes.py`` is the one that models responses, and it is the one a
simulated experiment should use. What this models is what arrives, in which
frames, on what schedule. Treating its output as data would be a category
error, so nothing here is written to disk and the CLI says so on startup.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from alhazen.devices.spikes import UNITS_REANNOUNCE_PERIOD_MS
from alhazen.errors import AlhazenError

log = logging.getLogger(__name__)

# The non-conformances worth being able to produce on purpose. Each one is a
# failure a real sorter has, or plausibly will have, and each sends the
# experimenter somewhere different:
#
# - "silent"          the sorter died, or nothing is bound to that endpoint at
#                     all. A SUB socket connects to an address nobody
#                     publishes on and reports success, so this is the failure
#                     a connect-only check would call OK.
# - "announce_once"   the naive implementation: announce the units at startup,
#                     then never again. Works perfectly for a subscriber that
#                     witnessed the startup, and is invisible to everyone
#                     else — which is every subscriber to a PUB socket,
#                     check-rig always included. This is the one a lab writing
#                     its own sorter will actually hit.
# - "never_units"     publishes timed messages and never announces units at
#                     all: the sample rate never arrives, so no subscriber can
#                     place a spike on any clock.
# - "no_seq"          conformant, but omits the optional sequence numbers, so
#                     dropped messages become undetectable. Not a failure —
#                     a degraded report, and worth seeing stated as one.
Fault = Literal["none", "silent", "announce_once", "never_units", "no_seq"]
FAULTS: tuple[Fault, ...] = ("none", "silent", "announce_once", "never_units", "no_seq")


@dataclass(frozen=True)
class SorterSim:
    """What to publish, and how wrong to publish it.

    The defaults are a conformant sorter on a quiet cortex: four units at a
    plausible 20 Hz, a Neuropixels sample rate, and the contract's own
    periods. ``seed`` makes the spike train reproducible, so two runs against
    the same consumer produce the same numbers and a difference between them
    means something.
    """

    address: str = "tcp://127.0.0.1:5556"
    n_units: int = 4
    sample_rate_hz: float = 30_000.0
    # Per unit, not across the population: a decoder's features come from
    # individual units, so this is the number that decides whether there is
    # anything to decode.
    firing_hz: float = 20.0
    heartbeat_period_ms: float = 200.0
    units_period_ms: float = UNITS_REANNOUNCE_PERIOD_MS
    seed: int = 0
    fault: Fault = "none"

    def __post_init__(self) -> None:
        # Loud at construction rather than producing a stream that is subtly
        # not what was asked for: a rehearsal whose settings were ignored
        # teaches the wrong thing about the rig.
        if self.n_units < 1:
            raise AlhazenError(f"n_units must be >= 1, got {self.n_units}")
        if self.sample_rate_hz <= 0:
            raise AlhazenError(f"sample_rate_hz must be > 0, got {self.sample_rate_hz}")
        if self.firing_hz < 0:
            raise AlhazenError(f"firing_hz must be >= 0, got {self.firing_hz}")
        if self.heartbeat_period_ms <= 0:
            raise AlhazenError(f"heartbeat_period_ms must be > 0, got {self.heartbeat_period_ms}")
        if self.units_period_ms <= 0:
            raise AlhazenError(f"units_period_ms must be > 0, got {self.units_period_ms}")
        if self.fault not in FAULTS:
            raise AlhazenError(f"unknown fault {self.fault!r}; expected one of {', '.join(FAULTS)}")

    @property
    def unit_ids(self) -> tuple[int, ...]:
        """Unit ids as a sorter would emit them: opaque, not row indices.

        Deliberately not 0..n-1. A consumer that confuses a unit id with a
        row index passes against contiguous ids from zero and fails on a real
        sorter, which numbers units by whatever its clustering produced.
        """
        return tuple(11 + 7 * i for i in range(self.n_units))


class SortedSpikePublisher:
    """Publishes the sorted-spike wire contract on a ZeroMQ PUB socket.

    Time enters through ``step(now)`` only, and the socket is injectable, so
    the whole schedule — which message is due, what it carries, what each
    fault suppresses — is testable with no socket and no waiting. ``run()``
    is the thin real-time loop on top, and is the only part that sleeps.
    """

    def __init__(
        self,
        cfg: SorterSim,
        socket: Any | None = None,
    ) -> None:
        self._cfg = cfg
        self._socket = socket
        self._owns_socket = socket is None
        self._context: Any | None = None
        self._rng = np.random.default_rng(cfg.seed)
        # Set on the first step(): the acquisition's sample 0. Every
        # covered_until_sample is measured from it, which is what makes the
        # stream's clock and the consumer's clock two readings of the same
        # elapsed time rather than two unrelated numbers.
        self._t0: float | None = None
        self._last_units: float | None = None
        self._last_beat: float | None = None
        self._covered_sample = 0
        self._seq = 0
        self._announced = False
        self._bound_address: str | None = None

    @property
    def address(self) -> str:
        """Where a subscriber should connect.

        After ``bind()`` this is the address actually bound, which differs
        from the configured one when the port was a wildcard — the form a
        test uses to avoid colliding with whatever else is on the machine.
        """
        return self._bound_address or self._cfg.address

    def bind(self) -> str:
        """Open the PUB socket. Returns the address subscribers should use."""
        if self._socket is None:
            try:
                import zmq
            except ImportError as error:  # pragma: no cover - depends on the env
                raise AlhazenError(
                    "pyzmq is not installed, and the simulated sorter needs it — "
                    "pip install 'alhazen-vision[zmq]'"
                ) from error
            self._context = zmq.Context.instance()
            self._socket = self._context.socket(zmq.PUB)
            self._socket.bind(self._cfg.address)
            # Ask the socket where it actually landed rather than echoing the
            # config: with a wildcard port ("tcp://127.0.0.1:*") the config
            # is not an address anybody can connect to.
            endpoint = self._socket.getsockopt(zmq.LAST_ENDPOINT)
            self._bound_address = endpoint.decode() if isinstance(endpoint, bytes) else self.address
        else:
            self._bound_address = self._cfg.address
        return self.address

    def step(self, now: float) -> int:
        """Publish everything due at ``now``. Returns how many messages went out.

        Both timers are checked against the same reading of the clock, and
        the coverage is computed once, so the spikes message and the
        heartbeat that follow it in one tick agree about how much of the
        stream is complete. Two messages disagreeing about that on the same
        tick is the kind of thing a consumer is entitled to refuse.
        """
        if self._socket is None:
            raise AlhazenError("bind() the publisher before stepping it")
        if self._t0 is None:
            self._t0 = now
        # "silent" binds and says nothing: the endpoint exists, so a SUB
        # socket connects happily, and only a check that listens notices.
        if self._cfg.fault == "silent":
            return 0

        sent = 0
        if self._units_due(now):
            self._send(self._units_frames())
            self._last_units = now
            self._announced = True
            sent += 1

        if (
            self._last_beat is not None
            and now - self._last_beat < self._cfg.heartbeat_period_ms / 1000.0
        ):
            return sent
        self._last_beat = now

        # One coverage reading for this tick. Monotonic by construction, which
        # the consumer's timebase requires: a stream position that moves
        # backwards means a different acquisition, and it refuses one.
        previous, self._covered_sample = self._covered_sample, self._sample_at(now)
        samples, units = self._draw_spikes(previous, self._covered_sample)
        if samples.size:
            self._send(self._spikes_frames(samples, units, self._covered_sample))
            sent += 1
        # Sent even on a tick that carried spikes: the contract puts a floor
        # on heartbeat silence, not on total silence, and a consumer that
        # timed its watchdog off heartbeats alone must not be starved by a
        # busy stream.
        self._send(self._heartbeat_frames(self._covered_sample))
        return sent + 1

    def run(self, duration_s: float | None = None, poll_s: float = 0.005) -> None:
        """Publish in real time until ``duration_s`` elapses, or forever.

        Polls far faster than either period so a message is never more than
        ``poll_s`` late; the periods themselves are enforced in ``step``,
        against the clock, not by counting iterations.
        """
        started = time.monotonic()
        while duration_s is None or time.monotonic() - started < duration_s:
            self.step(time.monotonic())
            time.sleep(poll_s)

    def close(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None and self._owns_socket:
            # linger=0: a simulator that held the process open waiting to
            # flush messages nobody is subscribed to would hang the very
            # bench session it exists to unblock.
            socket.close(linger=0)

    # -- the wire contract, in one place per message type ----------------

    def _units_due(self, now: float) -> bool:
        if self._cfg.fault == "never_units":
            return False
        if self._cfg.fault == "announce_once":
            return not self._announced
        if self._last_units is None:
            return True
        return now - self._last_units >= self._cfg.units_period_ms / 1000.0

    def _units_frames(self) -> list[bytes]:
        header = {
            "type": "units",
            "unit_ids": list(self._cfg.unit_ids),
            "labels": ["good"] * self._cfg.n_units,
            "sample_rate_hz": self._cfg.sample_rate_hz,
        }
        return [json.dumps(header).encode()]

    def _spikes_frames(self, samples: np.ndarray, units: np.ndarray, covered: int) -> list[bytes]:
        header: dict[str, Any] = {
            "type": "spikes",
            "stream": "imec0",
            "covered_until_sample": int(covered),
            "n": int(samples.size),
        }
        if self._cfg.fault != "no_seq":
            header["seq"] = self._next_seq()
        return [
            json.dumps(header).encode(),
            np.asarray(samples, np.int64).tobytes(),
            np.asarray(units, np.int32).tobytes(),
        ]

    def _heartbeat_frames(self, covered: int) -> list[bytes]:
        header: dict[str, Any] = {"type": "heartbeat", "covered_until_sample": int(covered)}
        if self._cfg.fault != "no_seq":
            header["seq"] = self._next_seq()
        return [json.dumps(header).encode()]

    def _next_seq(self) -> int:
        seq, self._seq = self._seq, self._seq + 1
        return seq

    def _send(self, frames: list[bytes]) -> None:
        assert self._socket is not None  # step() checked; keeps mypy honest
        self._socket.send_multipart(frames)

    def _sample_at(self, now: float) -> int:
        assert self._t0 is not None
        return int((now - self._t0) * self._cfg.sample_rate_hz)

    def _draw_spikes(self, start: int, end: int) -> tuple[np.ndarray, np.ndarray]:
        """Poisson spikes for every unit in the samples ``[start, end)``.

        Drawn inside the covered interval, never past it: ``covered_until``
        means detection is *complete* up to that sample, so a spike beyond it
        would be a claim the publisher has no right to make and would teach a
        consumer to distrust the one field it waits on.
        """
        span = end - start
        if span <= 0 or self._cfg.firing_hz == 0:
            return np.empty(0, np.int64), np.empty(0, np.int32)
        seconds = span / self._cfg.sample_rate_hz
        samples: list[np.ndarray] = []
        units: list[np.ndarray] = []
        for unit in self._cfg.unit_ids:
            n = int(self._rng.poisson(self._cfg.firing_hz * seconds))
            if n == 0:
                continue
            samples.append(self._rng.integers(start, end, size=n, dtype=np.int64))
            units.append(np.full(n, unit, np.int32))
        if not samples:
            return np.empty(0, np.int64), np.empty(0, np.int32)
        all_samples = np.concatenate(samples)
        all_units = np.concatenate(units)
        # A real sorter emits in time order; a consumer that happened to rely
        # on it should be exercised against a stream that has it.
        order = np.argsort(all_samples, kind="stable")
        return all_samples[order], all_units[order]


def describe_fault(fault: Fault) -> str:
    """One line per fault, for the CLI banner: what the experimenter should
    expect check-rig to say when pointed at this publisher."""
    return {
        "none": "conformant — check-rig should report OK with a lag in milliseconds",
        "silent": "bound but publishing nothing — check-rig should FAIL with 'is the "
        "real-time sorter running and publishing?'",
        "announce_once": "announces units only at startup, never again — a late-joining "
        "check-rig should FAIL with 'the sorter never re-announced units'",
        "never_units": "never announces units at all — check-rig should FAIL with "
        "'the sorter never re-announced units'",
        "no_seq": "conformant but sends no sequence numbers — check-rig should report OK "
        "with 'drops undetectable (no seq)'",
    }[fault]


__all__ = ["FAULTS", "Fault", "SorterSim", "SortedSpikePublisher", "describe_fault"]
