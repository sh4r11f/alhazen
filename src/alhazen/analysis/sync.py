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
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from alhazen.analysis.io import spikeglx
from alhazen.analysis.io.session import RunData
from alhazen.data.manifest import add_to_manifest
from alhazen.data.percents import compared_percents
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
# How many pulses at each end of the train the seed search may pair with an
# event. Extra pulses — a test pulse before the session, a stray edge after
# it — never count against the matched fraction, so no budget bounds them;
# a handful is what a rig actually produces.
SEED_PULSES = 8
# The most events at each end the seed search tries, whatever the matched
# fraction would allow. The search is (events × pulses) at each end, so this
# is what keeps a long session aligned with a permissive threshold from
# pairing thousands of candidates with thousands. 128 events is a quarter of
# an hour of trials at 7 s each; a late start longer than that is refused,
# and the refusal says how far in it looked.
MAX_SEED_EVENTS = 128
# Two different pairings of events to pulses that explain equally many events
# are told apart by how closely they fit: the right one fits to the clocks'
# own noise, a pairing shifted by one trial also carries the trial-to-trial
# variation in timing. Unless the winner fits at least this many times more
# closely, the events are too regular to say which pulse is whose.
AMBIGUOUS_RMS_RATIO = 2.0
# Scoring every seed against every event costs seeds × events, which on a
# long session is billions. So seeds are first screened against this many
# events, spread evenly over the session, and only the best-screened
# _SHORTLIST seeds are scored against them all. A right seed explains
# nearly every sampled event and a wrong one few, so the right one ranks at
# the top of the screen with room to spare; a one-trial shift of a regular
# train screens as well as the right one, so it reaches the shortlist too
# and the ambiguity check still sees it.
_SCREEN_EVENTS = 64
_SHORTLIST = 256
# How many (seed × event) predictions are scored in one numpy array. Keeps
# memory flat however many seeds a long session produces.
_SCORE_CHUNK = 1_000_000
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
        path.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False), encoding="utf-8")
        # A file added without a manifest entry makes `verify_manifest`
        # report it as unlisted, which means the first alignment would break
        # every later report and every `load_run` on this run (spec 6.2).
        # Only this file's entry is written: re-hashing the whole run would
        # record any file damaged since the session as if it were the
        # session's own.
        add_to_manifest(run_dir, run_dir / "manifest.yaml", [path])
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

    1. **seeds** by pairing one of the first few events with one of the
       first few pulses, and one of the last few events with one of the last
       few pulses — each such double pairing fixes an offset and scale
       exactly — and keeps whichever explains the most events, the closer
       fit breaking a tie. A bounded exhaustive search, not a heuristic;
    2. **refines** by alternating nearest-pulse matching with refitting on
       the matched pairs, until the match set stops changing.

    Trying several events at each end, not only the first and the last, is
    what lets a recording that started late or stopped early align: its
    first (or last) event has no pulse, so any seed anchored there ties it to
    another event's pulse. How many are tried is set by the matched-fraction
    threshold — see ``_seed_window``.

    Refuses, rather than returning something, when too few events match: two
    records that do not describe the same session would otherwise produce a
    transform that is confidently wrong. Refuses too when a different pairing
    explains as many events about as closely — a train so regular that a
    one-trial shift fits as well, where either answer could be the wrong one.
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

    n_ends = _seed_window(len(behavior), min_matched_fraction)
    n_end_pulses = min(SEED_PULSES, len(pulses))
    # Said in both refusals below, so a late start is recognisable from the
    # message: "compared the first and last 101 events" next to a recording
    # that began 300 events in says why nothing was found.
    compared = (
        f"compared the first and last {n_ends} events with the first and last {n_end_pulses} pulses"
    )

    offsets, scales = _seed_maps(relative, pulses, n_ends, tolerance_s)
    if len(offsets) == 0:
        raise DataError(
            f"no pairing of {event!r} pulses to events produced a plausible clock scale "
            f"(0.99-1.01) — {compared}. The recording and the session do not appear to "
            f"be the same one"
        )

    # Screen on an even sample of events (first and last included), keep the
    # best, then score those against every event. The screen's ranking uses
    # the same keys as the final one below.
    sample = np.unique(np.linspace(0, len(relative) - 1, _SCREEN_EVENTS).round().astype(int))
    counts, rms = _score_seeds(relative[sample], pulses, offsets, scales, tolerance_s)
    shortlist = np.lexsort((rms, -counts))[:_SHORTLIST]
    # Back into generation order, so the final ranking's stable tie-break
    # does not depend on how the screen happened to sort.
    shortlist.sort()
    offsets, scales = offsets[shortlist], scales[shortlist]

    counts, rms = _score_seeds(relative, pulses, offsets, scales, tolerance_s)
    # Most events explained first; among those, the closest fit. lexsort is
    # stable and keys on its LAST array first, so equal seeds keep the order
    # _seed_maps produced them in and the choice is deterministic.
    order = np.lexsort((rms, -counts))
    chosen = int(order[0])
    rival = _equally_good_rival(relative, pulses, offsets, scales, counts, rms, chosen, tolerance_s)

    offset, scale = float(offsets[chosen]), float(scales[chosen])
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
        # Whole percents made 399 of 500 against 80% read "(80% < 80%)": a
        # refusal that seemed to contradict itself and hid how close the fit
        # came. The shared rule writes the threshold as set and the fraction
        # with as many decimals as it takes to be visibly below it
        # ("79.8% < 80%"), and a clear miss still reads "3% < 80%".
        shown_fraction, shown_threshold = compared_percents(fraction, "<", min_matched_fraction)
        raise DataError(
            f"only {n_matched} of {len(behavior)} {event!r} events matched a pulse "
            f"({shown_fraction} < {shown_threshold}), from {len(pulses)} pulses; "
            f"the seed search {compared}. "
            f"Either these are different sessions, or pulses were dropped — an alignment "
            f"fitted from this would be confidently wrong, so it is refused."
        )

    # Checked only once the fit has enough matches to be worth having: when
    # too few match, that is the more basic refusal and the one to report.
    if rival is not None:
        rival_gap_s, rival_rms_s = rival
        raise DataError(
            f"two different pairings of {event!r} events to pulses each explain "
            f"{int(counts[chosen])} of {len(behavior)} events about equally well "
            f"({rms[chosen] * 1000:.3f} ms vs {rival_rms_s * 1000:.3f} ms rms), and they "
            f"place the same event up to {rival_gap_s:.3f} s apart. "
            f"The events are too evenly spaced to tell which pulse belongs to which event, "
            f"so either alignment could be off by whole trials and it is refused. Align on "
            f"an event whose timing varies from trial to trial, or check whether the "
            f"recording started late or stopped early."
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


def _seed_window(n_behavior: int, min_matched_fraction: float) -> int:
    """How many events at each end of the session the seed search tries.

    An event can anchor the fit only if it has a pulse. A recording that
    started k events late leaves the first k events without one, so the
    search has to reach event k (and likewise from the end for a recording
    stopped early). It never has to reach further than the matched-fraction
    threshold lets events go unmatched: past that, the fit is refused
    whatever the seed, so trying more would only cost time. That budget,
    plus one for the first event that does have a pulse, is the window —
    capped by MAX_SEED_EVENTS for a permissive threshold on a long session.
    """
    # The most events that may go unmatched while the fit is still accepted.
    # The epsilon keeps a product like 0.55 × 100 = 55.00000000000001 from
    # rounding up to 56 and costing the window one event.
    allowed_unmatched = n_behavior - math.ceil(min_matched_fraction * n_behavior - 1e-9)
    return max(1, min(n_behavior, MAX_SEED_EVENTS, allowed_unmatched + 1))


def _seed_maps(
    relative: np.ndarray, pulses: np.ndarray, n_ends: int, tolerance_s: float
) -> tuple[np.ndarray, np.ndarray]:
    """Every plausible map through one pairing at each end of the two records.

    A map is fixed exactly by two (event, pulse) pairs. The first pairs one
    of the first ``n_ends`` events with one of the first SEED_PULSES pulses;
    the second pairs one of the last ``n_ends`` events with one of the last
    SEED_PULSES pulses. Returns parallel offset and scale arrays, in the
    order they were generated, so the search is deterministic.
    """
    n_events, n_pulses = len(relative), len(pulses)
    n_end_pulses = min(SEED_PULSES, n_pulses)
    # Three broadcast axes — head pulse × tail event × tail pulse — so each
    # first event is paired with every completion in one numpy step. The
    # Python loop is over first events only: at most MAX_SEED_EVENTS rounds.
    head_pulses = np.arange(n_end_pulses)[:, None, None]
    tail_events = np.arange(n_events - n_ends, n_events)[None, :, None]
    tail_pulses = np.arange(n_pulses - n_end_pulses, n_pulses)[None, None, :]
    offset_parts: list[np.ndarray] = []
    scale_parts: list[np.ndarray] = []
    for first_event in range(n_ends):
        rise = pulses[tail_pulses] - pulses[head_pulses]
        run = relative[tail_events] - relative[first_event]
        # On a short train the two windows overlap; a pairing only makes
        # sense with the second pair strictly after the first in both records.
        ordered = (tail_pulses > head_pulses) & (run > 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            scale = rise / run
        # A scale far from 1 is a wrong pairing, not a clock difference.
        # Rejecting those keeps the search from wandering off.
        keep = ordered & (scale >= 0.99) & (scale <= 1.01)
        head_times = np.broadcast_to(pulses[head_pulses], keep.shape)[keep]
        offset_parts.append(head_times - scale[keep] * relative[first_event])
        scale_parts.append(scale[keep])

    offsets = np.concatenate(offset_parts)
    scales = np.concatenate(scale_parts)
    # Most seeds are copies of one another: with no pulse missing near the
    # ends, pairing event i with pulse a draws the same line as event i+1
    # with pulse a+1. Scoring every copy would multiply the cost by up to
    # (n_ends × SEED_PULSES)² for nothing, so maps that land in the same
    # half-tolerance cell at both the first and the last event are scored
    # once. Two such maps are within half a tolerance of each other at every
    # event in between too (both are straight lines), so they pair every
    # event with the same pulse; a map shifted by a whole trial never shares
    # a cell with the right one.
    cell = tolerance_s / 2
    ends = np.column_stack(
        [np.round(offsets / cell), np.round((offsets + scales * relative[-1]) / cell)]
    )
    _, first_of_each = np.unique(ends, axis=0, return_index=True)
    first_of_each.sort()
    return offsets[first_of_each], scales[first_of_each]


def _nearest_pulse(pulses: np.ndarray, predicted: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Index of, and distance to, the nearest pulse for each predicted time."""
    # searchsorted gives the insertion point; the nearest pulse is there or
    # one before it. Clipping keeps both candidates inside the array at the
    # ends, where one of them is simply the other again.
    insertion = np.searchsorted(pulses, predicted)
    right = np.minimum(insertion, len(pulses) - 1)
    left = np.maximum(insertion - 1, 0)
    to_right = np.abs(pulses[right] - predicted)
    to_left = np.abs(pulses[left] - predicted)
    use_left = to_left < to_right
    return np.where(use_left, left, right), np.where(use_left, to_left, to_right)


def _score_seeds(
    relative: np.ndarray,
    pulses: np.ndarray,
    offsets: np.ndarray,
    scales: np.ndarray,
    tolerance_s: float,
) -> tuple[np.ndarray, np.ndarray]:
    """How many events each seed map explains, and how closely.

    Per map: the count of events with a pulse within tolerance of where the
    map puts them, and the rms distance over those events (seconds). Unlike
    ``_match_nearest`` a pulse may count for two events here; that needs two
    events within one tolerance of each other, which the tolerance is chosen
    never to allow, and it buys scoring every seed in one vectorized pass
    instead of a Python loop per seed.
    """
    counts = np.zeros(len(offsets), dtype=int)
    rms = np.zeros(len(offsets))
    rows = max(1, _SCORE_CHUNK // len(relative))
    for start in range(0, len(offsets), rows):
        block = slice(start, start + rows)
        predicted = offsets[block, None] + scales[block, None] * relative[None, :]
        _, distance = _nearest_pulse(pulses, predicted)
        within = distance <= tolerance_s
        n_within = within.sum(axis=1)
        counts[block] = n_within
        squared = np.where(within, distance, 0.0) ** 2
        # max(…, 1) only guards the division for a seed matching nothing,
        # whose rms is then 0 — harmless, since it is ranked on count first.
        rms[block] = np.sqrt(squared.sum(axis=1) / np.maximum(n_within, 1))
    return counts, rms


def _equally_good_rival(
    relative: np.ndarray,
    pulses: np.ndarray,
    offsets: np.ndarray,
    scales: np.ndarray,
    counts: np.ndarray,
    rms: np.ndarray,
    chosen: int,
    tolerance_s: float,
) -> tuple[float, float] | None:
    """A different map that explains as many events as ``chosen``, about as closely.

    Returns (how far apart the two maps put a matched event at most, the
    rival's rms), both in seconds, or None when the choice is clear.
    This is what an evenly spaced train looks like when an end pulse is
    missing: shifting every pairing by one trial explains just as many
    events, and nothing but the fit's closeness can tell the two apart.

    Only seeds explaining as many events as ``chosen`` are candidates — one
    explaining fewer is simply worse. A candidate is a *different* map when
    it puts some event ``chosen`` matched more than a tolerance away from
    where ``chosen`` puts it; closer than that, it is the same map with a
    little noise, even if it happens to prefer a different spurious edge
    nearby. It is a rival unless ``chosen`` fits AMBIGUOUS_RMS_RATIO times
    more closely. The microsecond floor lets a noiseless train, where both
    fits are exact to rounding, count as the tie it is.
    """
    tied = np.flatnonzero(counts == counts[chosen])
    tied = tied[tied != chosen]
    close = tied[rms[tied] <= AMBIGUOUS_RMS_RATIO * rms[chosen] + 1e-6]
    if len(close) == 0:
        return None

    # Both maps are straight lines, so the gap between them is largest at
    # one end of the stretch being compared: the first and last events the
    # chosen map matched. Events before or after that stretch had no pulse
    # (a late start), so a disagreement there would decide nothing.
    _, distance = _nearest_pulse(pulses, offsets[chosen] + scales[chosen] * relative)
    matched = np.flatnonzero(distance <= tolerance_s)
    if len(matched) == 0:
        # Nothing matched, so nothing to be ambiguous about; the matched-
        # fraction refusal is what reports this fit.
        return None
    ends = relative[[matched[0], matched[-1]]]
    for candidate in close:
        gap = (offsets[candidate] - offsets[chosen]) + (scales[candidate] - scales[chosen]) * ends
        widest = float(np.max(np.abs(gap)))
        if widest > tolerance_s:
            return widest, float(rms[candidate])
    return None


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
