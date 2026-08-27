"""Aligning a session's clock to a recording's clock.

Two machines, two crystal oscillators, two ideas of what a second is. The TTL
pulses put the same events in both records; this fits the map
between them — ``recording_time = offset + scale × behavior_time`` — and
reports how well it fits.

Three rules, each of which is a bug that is otherwise easy to ship:

- **The line map comes from the run's own snapshot.** A notebook that
  re-declares which event was on which line is a notebook that will one day
  be wrong about a session it was not written for.
- **Unmatched pulses are never silently dropped.** A fit that quietly ignored
  half its pulses would still produce a confident-looking transform. The
  count of matched, unmatched and extra pulses is part of the result, and a
  fit that matched too few of them refuses rather than returning.
- **The fit is stored beside the data.** An alignment recomputed differently
  next year is a different alignment; the one that was used is an artifact.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from alhazen.analysis.io import spikeglx
from alhazen.analysis.io.session import RunData
from alhazen.data.manifest import write_manifest
from alhazen.errors import DataError

log = logging.getLogger(__name__)

# How far a pulse may sit from where the fit predicts it and still be called
# the same event. Wide compared with a clock difference, narrow compared with
# an inter-trial interval, so a mismatch cannot pair the wrong events.
DEFAULT_TOLERANCE_S = 0.1
# Below this fraction matched, the two records are not the same session.
DEFAULT_MIN_MATCHED = 0.8
# How many match/refit rounds the refinement takes before giving up on
# convergence. A module constant so a test can force the cap, which is the
# case the final refit below exists for.
MAX_REFINE_ITERATIONS = 5
# Anchored to the END of the string on purpose. Unanchored, this matches any
# earlier "line<digits>" — a device path under a directory called "baseline5"
# would resolve to bit 5, putting every pulse on a wire nobody chose.
_LINE_NUMBER = re.compile(r"line\s*(\d+)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class AlignmentFit:
    """A fitted map from session time to recording time, and its residuals."""

    event: str
    offset_s: float
    scale: float
    t0_behavior_s: float
    n_behavior: int
    n_pulses: int
    n_matched: int
    residual_rms_ms: float
    residual_max_ms: float
    residuals_ms: np.ndarray = field(repr=False, default_factory=lambda: np.empty(0))

    # -- using it ------------------------------------------------------

    def to_neural(self, behavior_times_s: Any) -> np.ndarray:
        """Session-clock seconds → recording-clock seconds."""
        times = np.asarray(behavior_times_s, dtype=float)
        return self.offset_s + self.scale * (times - self.t0_behavior_s)

    def to_behavior(self, neural_times_s: Any) -> np.ndarray:
        """Recording-clock seconds → session-clock seconds."""
        times = np.asarray(neural_times_s, dtype=float)
        return self.t0_behavior_s + (times - self.offset_s) / self.scale

    # -- judging it ----------------------------------------------------

    @property
    def drift_ppm(self) -> float:
        """Clock-rate mismatch in parts per million.

        A few hundred ppm between free-running oscillators is ordinary. A
        scale far from 1 does not mean the clocks disagree that badly — it
        means the fit latched onto the wrong pulses.
        """
        return (self.scale - 1.0) * 1e6

    @property
    def n_unmatched_behavior(self) -> int:
        """Events with no pulse: pulses that were dropped."""
        return self.n_behavior - self.n_matched

    @property
    def n_extra_pulses(self) -> int:
        """Pulses matching no event: test pulses, or spurious edges."""
        return self.n_pulses - self.n_matched

    # -- keeping it ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "written": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": self.event,
            "offset_s": float(self.offset_s),
            "scale": float(self.scale),
            "t0_behavior_s": float(self.t0_behavior_s),
            "drift_ppm": float(self.drift_ppm),
            "n_behavior": int(self.n_behavior),
            "n_pulses": int(self.n_pulses),
            "n_matched": int(self.n_matched),
            "n_unmatched_behavior": int(self.n_unmatched_behavior),
            "n_extra_pulses": int(self.n_extra_pulses),
            "residual_rms_ms": float(self.residual_rms_ms),
            "residual_max_ms": float(self.residual_max_ms),
        }

    def save(self, run_dir: Path | str, system: str = "spikeglx") -> Path:
        """Write the fit beside the data it aligns.

        The stored artifact is the point: an alignment recomputed next year
        with a different tolerance is a different alignment, and the analyses
        that used this one need to be able to say which it was.
        """
        run_dir = Path(run_dir)
        path = run_dir / f"alignment_{system}.yaml"
        path.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False))
        # A run directory is append-only *by manifest rewrite*: a file added
        # without one makes `verify_manifest` report it as unlisted, which
        # means the first alignment would break every later report and every
        # `load_run` on this run. Spec 6.2 requires the rewrite; it is
        # idempotent, so rewriting twice in one report run costs only hashes.
        write_manifest(run_dir, run_dir / "manifest.yaml")
        log.info(
            "alignment written to %s: %d/%d events matched, %.3f ms rms, %.0f ppm",
            path.name,
            self.n_matched,
            self.n_behavior,
            self.residual_rms_ms,
            self.drift_ppm,
        )
        return path


def bit_index_for_line(line: str) -> int:
    """The digital-word bit a rig-config line string refers to."""
    match = _LINE_NUMBER.search(line)
    if not match:
        raise DataError(
            f"cannot read a line number from {line!r} — expected something like "
            f"'Dev1/port0/line0'. This string comes from the run's own sync.event_lines."
        )
    bit = int(match.group(1))
    if not 0 <= bit <= 15:
        raise DataError(f"line {line!r} is bit {bit}, outside the 16-bit digital word")
    return bit


def event_bit_map(event_lines: dict[str, str]) -> dict[str, int]:
    """Event name → digital-word bit, from a run's own line map.

    Two events on one line would be indistinguishable in the recording, so
    that is an error here rather than a puzzle later.
    """
    mapping = {name: bit_index_for_line(line) for name, line in event_lines.items()}
    seen: dict[int, str] = {}
    for name, bit in sorted(mapping.items()):
        if bit in seen:
            raise DataError(
                f"events {seen[bit]!r} and {name!r} are both wired to bit {bit}; their "
                f"pulses would be indistinguishable in the recording"
            )
        seen[bit] = name
    return mapping


def align_run(
    run: RunData,
    neural_run_dir: Path | str,
    event: str | None = None,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
    min_matched_fraction: float = DEFAULT_MIN_MATCHED,
) -> AlignmentFit:
    """Fit this run's clock to a recording's, using one event's pulse train.

    The event defaults to whichever mapped event has the most occurrences in
    the session: more pulses is a better-conditioned fit, and the trial-start
    line usually wins.
    """
    lines = run.sync_event_lines
    if not lines:
        raise DataError(
            f"{run.run_dir} recorded no sync line map — this session was not run with "
            f"sync configured, so there is nothing to align to"
        )
    bits = event_bit_map(lines)

    if event is None:
        # Chosen by what the session actually emitted, not by name: an event
        # mapped to a line but never fired is useless to fit on.
        counts = {name: len(run.event_times(name)) for name in bits}
        event = max(counts, key=lambda name: counts[name])
        if counts[event] == 0:
            raise DataError(
                f"none of the mapped events {sorted(bits)} occurred in this session — "
                f"nothing to align on"
            )

    if event not in bits:
        raise DataError(
            f"event {event!r} was not wired to a sync line in this run (wired: {sorted(bits)})"
        )

    files = spikeglx.find_run_files(neural_run_dir)
    pulses = spikeglx.digital_word_edges(
        files["bin_path"], files["meta_path"], bit_index=bits[event], edge="rising"
    )
    behavior = run.event_times(event)
    log.info("aligning on %s: %d events, %d pulses", event, len(behavior), len(pulses))
    return fit_alignment(
        event,
        behavior,
        pulses,
        tolerance_s=tolerance_s,
        min_matched_fraction=min_matched_fraction,
    )


def fit_alignment(
    event: str,
    behavior_times_s: Any,
    pulse_times_s: Any,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
    min_matched_fraction: float = DEFAULT_MIN_MATCHED,
) -> AlignmentFit:
    """Fit the clock map from one event's two records of itself.

    Both sequences describe the same events in the same order, but the pulse
    train may hold extras (a test pulse before the session) and may be
    missing some. So this does not zip them: it

    1. **seeds** by trying each of the first few pulses against the first
       event and each of the last few against the last — each pairing fixes
       an offset and scale exactly — and keeps whichever explains the most
       events. A small exhaustive search, not a heuristic;
    2. **refines** by alternating nearest-pulse matching with refitting on
       the matched pairs, until the match set stops changing.

    Refuses, rather than returning something, when too few events match: two
    records that do not describe the same session would otherwise produce a
    transform that is confidently wrong.
    """
    pulses = np.sort(np.asarray(pulse_times_s, dtype=float))
    behavior = np.asarray(behavior_times_s, dtype=float)
    behavior = behavior[np.isfinite(behavior)]

    if len(behavior) < 3 or len(pulses) < 3:
        raise DataError(
            f"aligning on {event!r} needs at least 3 of each: got {len(behavior)} events "
            f"and {len(pulses)} pulses"
        )

    # Fit against time-since-first-event, not raw clock values: a session
    # clock reading 1.7e9 seconds makes the least-squares badly conditioned,
    # and the scale is what matters.
    t0 = float(behavior[0])
    relative = behavior - t0
    span = float(relative[-1] - relative[0])
    if span <= 0:
        raise DataError(f"event {event!r} times are not increasing — this is not a session")

    best: tuple[int, float, float] | None = None
    head = range(min(8, len(pulses)))
    tail = range(max(0, len(pulses) - 8), len(pulses))
    for first in head:
        for last in tail:
            if last <= first:
                continue
            scale = (pulses[last] - pulses[first]) / span
            # A scale far from 1 is a wrong pairing, not a clock difference.
            # Rejecting those keeps the search from wandering off.
            if not 0.99 <= scale <= 1.01:
                continue
            offset = pulses[first] - scale * relative[0]
            matches = _match_nearest(relative, pulses, offset, scale, tolerance_s)
            n_matched = int((matches >= 0).sum())
            if best is None or n_matched > best[0]:
                best = (n_matched, offset, scale)

    if best is None:
        raise DataError(
            f"no pairing of {event!r} pulses to events produced a plausible clock scale "
            f"(0.99-1.01) — the recording and the session do not appear to be the same one"
        )

    _, offset, scale = best
    matches = _match_nearest(relative, pulses, offset, scale, tolerance_s)
    for _ in range(MAX_REFINE_ITERATIONS):
        matched = matches >= 0
        if matched.sum() < 2:
            break
        offset, scale = _fit_affine(relative[matched], pulses[matches[matched]])
        updated = _match_nearest(relative, pulses, offset, scale, tolerance_s)
        if np.array_equal(updated, matches):
            break
        matches = updated

    matched = matches >= 0
    # One last fit on the FINAL match set. Without it, a loop that ends by
    # exhausting its iterations returns a match set one round newer than the
    # offset and scale fitted from it — so the residual statistics would
    # describe a map that is not the map being returned.
    if matched.sum() >= 2:
        offset, scale = _fit_affine(relative[matched], pulses[matches[matched]])

    n_matched = int(matched.sum())
    fraction = n_matched / len(behavior)
    if fraction < min_matched_fraction:
        raise DataError(
            f"only {n_matched} of {len(behavior)} {event!r} events matched a pulse "
            f"({fraction:.0%} < {min_matched_fraction:.0%}), from {len(pulses)} pulses. "
            f"Either these are different sessions, or pulses were dropped — an alignment "
            f"fitted from this would be confidently wrong, so it is refused."
        )

    predicted = offset + scale * relative[matched]
    residuals_ms = (pulses[matches[matched]] - predicted) * 1000.0
    return AlignmentFit(
        event=event,
        offset_s=float(offset),
        scale=float(scale),
        t0_behavior_s=t0,
        n_behavior=len(behavior),
        n_pulses=len(pulses),
        n_matched=n_matched,
        residual_rms_ms=float(np.sqrt(np.mean(residuals_ms**2))),
        residual_max_ms=float(np.max(np.abs(residuals_ms))),
        residuals_ms=residuals_ms,
    )


def _fit_affine(source: np.ndarray, target: np.ndarray) -> tuple[float, float]:
    """Least squares for ``target = a + b·source``."""
    design = np.column_stack([np.ones_like(source), source])
    (a, b), *_ = np.linalg.lstsq(design, target, rcond=None)
    return float(a), float(b)


def _match_nearest(
    relative: np.ndarray,
    pulses: np.ndarray,
    offset: float,
    scale: float,
    tolerance_s: float,
) -> np.ndarray:
    """Nearest pulse to each predicted event time; each pulse used once.

    Returns an index per event, -1 where nothing fell within tolerance.
    Greedy in time order, which is right here because both sequences are
    monotonic and the tolerance is far smaller than the gap between trials.
    """
    predicted = offset + scale * relative
    matches = np.full(len(relative), -1, dtype=int)
    # searchsorted gives the insertion point; the nearest pulse is there or
    # one before it.
    insertion = np.searchsorted(pulses, predicted)
    used = np.zeros(len(pulses), dtype=bool)
    for index, (target, right) in enumerate(zip(predicted, insertion, strict=True)):
        best_index, best_distance = -1, np.inf
        for candidate in (right - 1, right):
            if 0 <= candidate < len(pulses) and not used[candidate]:
                distance = abs(pulses[candidate] - target)
                if distance < best_distance:
                    best_index, best_distance = candidate, distance
        if best_index >= 0 and best_distance <= tolerance_s:
            matches[index] = best_index
            used[best_index] = True
    return matches
