"""What the photodiode says about when the screen actually changed.

Every visual event's timestamp is taken right after ``flip()`` returns — a
claim about when photons changed, not a measurement of it. The photodiode
patch turns white on exactly the frame whose flip carries the
event, so a diode over that corner records the truth. This is where the two
are compared.

The number that comes out is display latency: how long after the software
said "now" the screen actually changed. It is a property of the rig — the
graphics stack, the panel's own processing — and it is a constant an analysis
can subtract, once measured. What it must not be is assumed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

from alhazen.analysis.io import spikeglx
from alhazen.analysis.sync import AlignmentFit
from alhazen.errors import DataError

log = logging.getLogger(__name__)

# The patch flashes once per armed event, so the diode channel should carry
# about one rising edge per armed event. This much slack absorbs a stray edge
# or a panel that overshoots on a bright frame; anything past it is not a
# noisy measurement of the right signal, it is a different signal.
#
# The failure this refuses is quiet and confident: an unconnected analog
# channel is noise, its own min/max sets the auto threshold near the middle of
# that noise, and thousands of crossings land an edge just after every event.
# The result is a plausible-looking near-zero display latency that an analysis
# would then subtract from every timestamp.
MAX_EDGES_PER_EVENT = 3.0


@dataclass(frozen=True)
class PhotodiodeReport:
    """Display latency, as measured rather than assumed."""

    n_events: int
    n_edges: int
    n_matched: int
    median_latency_ms: float
    jitter_ms: float  # median absolute deviation: robust to a stray edge
    min_latency_ms: float
    max_latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_events": int(self.n_events),
            "n_edges": int(self.n_edges),
            "n_matched": int(self.n_matched),
            "median_latency_ms": float(self.median_latency_ms),
            "jitter_ms": float(self.jitter_ms),
            "min_latency_ms": float(self.min_latency_ms),
            "max_latency_ms": float(self.max_latency_ms),
        }


def find_edges(
    trace: np.ndarray, sample_rate_hz: float, threshold: float | None = None
) -> np.ndarray:
    """Times of the trace's upward crossings, in recording seconds.

    The threshold defaults to halfway between the trace's low and high — the
    patch is a two-level signal by construction, so its own range is the
    honest place to put the threshold, rather than a fixed voltage that
    depends on the diode, the gain and the panel's brightness.
    """
    trace = np.asarray(trace, dtype=float)
    if trace.size < 2:
        raise DataError("a photodiode trace needs at least two samples")
    if threshold is None:
        low, high = float(np.min(trace)), float(np.max(trace))
        if high - low <= 0:
            raise DataError(
                "the photodiode trace never changes — check that the diode is over the "
                "patch and that the rig config armed some events"
            )
        threshold = low + (high - low) / 2.0
    above = trace > threshold
    # The LOW sample of each (low, high) pair, +1 for the first HIGH sample:
    # the instant the screen went bright.
    rising = np.flatnonzero(~above[:-1] & above[1:]) + 1
    return rising.astype(float) / sample_rate_hz


def measure_latency(
    event_times_s: Any,
    edge_times_s: Any,
    alignment: AlignmentFit,
    max_latency_ms: float = 100.0,
) -> PhotodiodeReport:
    """Compare when events were stamped with when the screen changed.

    Each event's software time is mapped into recording time through the
    alignment, then paired with the first diode edge at or after it. Edges
    before an event are never matched backwards: the screen cannot change
    before the flip that changed it, and allowing that would let a stray
    edge produce a negative latency that looks like a clock error.
    """
    events = np.sort(np.asarray(event_times_s, dtype=float))
    edges = np.sort(np.asarray(edge_times_s, dtype=float))
    if events.size == 0 or edges.size == 0:
        raise DataError(
            f"nothing to compare: {events.size} armed events and {edges.size} photodiode edges"
        )

    expected = alignment.to_neural(events)
    latencies_ms: list[float] = []
    for moment in expected:
        # The first edge at or after the flip. searchsorted gives exactly
        # that index without scanning.
        index = int(np.searchsorted(edges, moment, side="left"))
        if index >= len(edges):
            continue
        latency_ms = (edges[index] - moment) * 1000.0
        # An edge implausibly far after its event is a different event's
        # edge; counting it would inflate the estimate rather than say the
        # match failed.
        if 0.0 <= latency_ms <= max_latency_ms:
            latencies_ms.append(latency_ms)

    if not latencies_ms:
        raise DataError(
            f"no photodiode edge fell within {max_latency_ms:.0f} ms after any armed "
            f"event — the diode, the patch corner or the alignment is wrong"
        )

    values = np.asarray(latencies_ms)
    median = float(np.median(values))
    return PhotodiodeReport(
        n_events=int(events.size),
        n_edges=int(edges.size),
        n_matched=int(values.size),
        median_latency_ms=median,
        # Median absolute deviation, not standard deviation: one mismatched
        # edge should not double the reported jitter.
        jitter_ms=float(np.median(np.abs(values - median))),
        min_latency_ms=float(np.min(values)),
        max_latency_ms=float(np.max(values)),
    )


def measure_from_recording(
    run: Any,
    neural_run_dir: Any,
    alignment: AlignmentFit,
    analog_channel: int = 0,
) -> PhotodiodeReport:
    """The whole loop: read the diode channel, find its edges, compare.

    The armed event names come from the run's own snapshot, so this cannot be
    pointed at the wrong events by a caller who misremembers which ones the
    rig was marking.
    """
    armed = run.photodiode_events
    if not armed:
        raise DataError(
            f"{run.run_dir} ran with no photodiode events armed, so there is nothing its "
            f"diode trace could be compared against"
        )
    files = spikeglx.find_run_files(neural_run_dir)
    trace, rate = spikeglx.analog_channel(
        files["bin_path"], files["meta_path"], channel=analog_channel
    )
    edges = find_edges(trace, rate)
    events: list[float] = []
    for name in armed:
        events.extend(run.event_times(name))
    log.info("photodiode: %d armed events, %d edges", len(events), len(edges))
    if events and len(edges) > MAX_EDGES_PER_EVENT * len(events):
        raise DataError(
            f"analog channel {analog_channel} has {len(edges)} rising edges for "
            f"{len(events)} armed events ({len(edges) / len(events):.0f}x, limit "
            f"{MAX_EDGES_PER_EVENT:.0f}x) — the patch flashes once per event, so this "
            f"channel is not carrying the photodiode. An unconnected channel is noise, "
            f"and measuring it would return a confident near-zero display latency. "
            f"Check --analog-channel and the diode's cabling."
        )
    return measure_latency(events, edges, alignment)
