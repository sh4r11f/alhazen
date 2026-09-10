"""Frame QA: per-frame interval recording plus a dropped-frame *policy*.

Every run records every frame interval; dropped frames are detected online
against the measured refresh rate and handled per the configured policy —
including actually aborting, which is the part a hand-rolled frame check
usually leaves commented out. Analysis gets the full interval log either
way; the policy only decides how loudly the live session reacts.

Two things the monitor says about a trial, and where they go:

- **Per frame**, every drop is a DEBUG line. A session log that is 308 lines
  of "dropped frame" and two of anything else is not a record of the session
  (it was, on the rig, before this); the frame log already holds every
  interval.
- **Per trial**, ``end_trial()`` sums the trial up — how many frames, how many
  dropped, the worst — in one line, and under ``recycle_trial`` returns the
  verdict the engine turns into a ``DROPPED_FRAMES`` outcome.

The same module also holds ``FrameTimeline``: the other half of taking frames
seriously, where a stimulus schedule is written in frames rather than in
milliseconds that the display will round anyway.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from alhazen.config.models import FrameQAConfig
from alhazen.errors import FrameQAError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrameRecord:
    trial_index: int
    t: float
    interval_s: float
    dropped: bool


@dataclass(frozen=True)
class TrialFrameSummary:
    """What one trial's frames added up to, from :meth:`FrameMonitor.end_trial`.

    ``n_frames`` counts measured intervals: the first flip of a trial only
    sets the reference point, so a trial of N flips has N-1 of them.
    ``recycle`` is the ``recycle_trial`` policy's verdict — True when the
    trial's dropped fraction exceeded the budget — and ``reason`` says so in
    words for the trial record. Both are False/None under every other policy.
    """

    trial_index: int
    n_frames: int
    n_dropped: int
    worst_interval_s: float
    recycle: bool = False
    reason: str | None = None

    @property
    def dropped_fraction(self) -> float:
        return self.n_dropped / self.n_frames if self.n_frames else 0.0


class FrameMonitor:
    """Fed one timestamp per flip by the engine; keeps the log and applies
    the policy. The first flip of each trial only establishes the reference
    point — an inter-trial gap is not a dropped frame."""

    def __init__(self, cfg: FrameQAConfig, refresh_rate_hz: float) -> None:
        self._cfg = cfg
        self._expected = 1.0 / refresh_rate_hz
        self._threshold = self._expected * (1.0 + cfg.tolerance)
        self._records: list[FrameRecord] = []
        # The intervals alone, kept beside the records so the dashboard's
        # frame-timing panel can histogram a long session without copying
        # every record on every publish.
        self._intervals: list[float] = []
        self._n_dropped = 0
        self._last_t: float | None = None
        self._trial_index = 0
        self._frames_this_trial = 0
        self._dropped_this_trial = 0
        self._worst_this_trial = 0.0
        # Trials recycled back to back; reset by any trial that is not.
        self._consecutive_recycles = 0

    @property
    def records(self) -> list[FrameRecord]:
        return list(self._records)

    @property
    def expected_s(self) -> float:
        """One frame period at the measured refresh rate."""
        return self._expected

    @property
    def threshold_s(self) -> float:
        """The interval past which a frame counts as dropped."""
        return self._threshold

    @property
    def n_dropped(self) -> int:
        """Dropped frames over the whole session so far."""
        return self._n_dropped

    def intervals_s(self) -> np.ndarray:
        """Every measured flip-to-flip interval so far, in order, as an array."""
        return np.asarray(self._intervals, dtype=float)

    def start_trial(self, trial_index: int) -> None:
        self._trial_index = trial_index
        self._last_t = None
        self._frames_this_trial = 0
        self._dropped_this_trial = 0
        self._worst_this_trial = 0.0

    def note_flip(self, t: float) -> bool:
        """Record one flip; return True if that frame was dropped. Raises
        FrameQAError under the abort_run policy once the per-trial budget is
        exceeded — after recording, so the log always holds the evidence."""
        if self._last_t is None:
            self._last_t = t
            return False
        interval = t - self._last_t
        self._last_t = t
        dropped = interval > self._threshold
        self._records.append(
            FrameRecord(trial_index=self._trial_index, t=t, interval_s=interval, dropped=dropped)
        )
        self._intervals.append(interval)
        self._frames_this_trial += 1
        self._worst_this_trial = max(self._worst_this_trial, interval)
        if not dropped:
            return False

        self._dropped_this_trial += 1
        self._n_dropped += 1
        # DEBUG whatever the policy: the per-trial line from end_trial() is
        # what the session log carries, and the frame log holds every interval.
        log.debug(
            "dropped frame on trial %d: %.1f ms (expected %.1f ms)",
            self._trial_index,
            interval * 1000,
            self._expected * 1000,
        )
        if (
            self._cfg.policy == "abort_run"
            and self._dropped_this_trial > self._cfg.max_dropped_per_trial
        ):
            raise FrameQAError(
                f"{self._dropped_this_trial} dropped frames in trial {self._trial_index} "
                f"(budget {self._cfg.max_dropped_per_trial}, policy abort_run) — "
                f"the frame log holds the intervals"
            )
        return True

    def end_trial(self) -> TrialFrameSummary:
        """Sum the trial up: one log line if it dropped anything, and the
        ``recycle_trial`` verdict for the engine.

        The verdict is made here, at the end, rather than frame by frame:
        the budget is a fraction of the trial's frames, and a fraction is only
        known once the trial's length is. One early drop in a trial that goes
        on for 300 more frames is not a recycled trial.

        Raises ``FrameQAError`` once ``max_consecutive_recycles`` trials in a
        row have been recycled — after logging the trial, so the log holds
        the evidence. A display that bad would otherwise be re-served the
        same conditions until somebody noticed the block never ends.
        """
        n, dropped = self._frames_this_trial, self._dropped_this_trial
        recycle, reason = False, None
        if self._cfg.policy == "recycle_trial" and n > 0:
            fraction = dropped / n
            if fraction > self._cfg.max_dropped_fraction:
                recycle = True
                reason = (
                    f"{dropped} of {n} frames dropped ({fraction:.1%}), over the "
                    f"{self._cfg.max_dropped_fraction:.0%} budget (frame_qa.max_dropped_fraction)"
                )
        self._consecutive_recycles = self._consecutive_recycles + 1 if recycle else 0
        if dropped:
            # One line per trial that dropped anything. WARNING under every
            # policy that asked to hear about drops; DEBUG under ``log``,
            # which asked not to.
            level = logging.DEBUG if self._cfg.policy == "log" else logging.WARNING
            log.log(
                level,
                "trial %d: %d of %d frames dropped (%.1f%%), worst %.1f ms against %.1f ms "
                "expected%s",
                self._trial_index,
                dropped,
                n,
                100.0 * dropped / n if n else 0.0,
                self._worst_this_trial * 1000,
                self._expected * 1000,
                " — trial recycled" if recycle else "",
            )
        if self._consecutive_recycles >= self._cfg.max_consecutive_recycles:
            raise FrameQAError(
                f"{self._consecutive_recycles} trials in a row recycled for dropped frames "
                f"(frame_qa.max_consecutive_recycles), the last one trial {self._trial_index}: "
                f"{reason}. The display is not holding its {1.0 / self._expected:.0f} Hz "
                f"refresh — this is the panel, the video mode or another application, not "
                f"the subject. Close other applications, check the video mode and the "
                f"cable, and run --mode measure before the next session."
            )
        return TrialFrameSummary(
            trial_index=self._trial_index,
            n_frames=n,
            n_dropped=dropped,
            worst_interval_s=self._worst_this_trial,
            recycle=recycle,
            reason=reason,
        )

    @property
    def marks_trials(self) -> bool:
        """Whether the engine should count drops on the trial record — true
        for the policies where analysis is meant to see per-trial drops."""
        return self._cfg.policy in ("mark_trial", "recycle_trial", "abort_run")

    def save(self, path: Path) -> None:
        with path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["trial_index", "t", "interval_s", "dropped"])
            for r in self._records:
                writer.writerow([r.trial_index, f"{r.t:.6f}", f"{r.interval_s:.6f}", r.dropped])


@dataclass(frozen=True)
class Keyframe:
    """One scheduled change: at ``frame``, set ``attr`` of stimulus ``key``.

    ``ramp_to`` turns it into a linear ramp running until ``until_frame``,
    which is how a stimulus moves without a phase doing arithmetic every
    frame. Ramps interpolate floats and tuples of floats (a position).
    """

    frame: int
    key: str
    attr: str
    value: Any
    ramp_to: Any = None
    until_frame: int | None = None


class FrameTimeline:
    """A stimulus schedule indexed by frame, compiled once and then replayed.

    Frame-indexed rather than time-indexed on purpose: a display can only
    change on a flip, so "50 ms after onset" is a wish while "frame 3" is what
    actually happens. Compiling the wish into frames once, against the measured
    refresh rate, means every trial of the session shows the identical
    sequence — and that an analysis can say exactly which frame a stimulus
    moved on rather than inferring it from timestamps.

    Pure data: no stimuli, no window, no clock. ``FrameSequence`` (the phase)
    is what applies it.
    """

    def __init__(self, n_frames: int) -> None:
        if n_frames < 1:
            raise ValueError(f"a timeline needs at least one frame, got {n_frames}")
        self.n_frames = n_frames
        self._keyframes: list[Keyframe] = []
        self._visible: dict[str, list[tuple[int, bool]]] = {}
        self._events: dict[int, list[str]] = {}

    # -- building ------------------------------------------------------

    def at(self, frame: int, key: str, attr: str, value: Any) -> FrameTimeline:
        """Set an attribute on one frame. Returns self, so a timeline reads as
        a sequence of statements."""
        self._keyframes.append(Keyframe(self._check(frame), key, attr, value))
        return self

    def ramp(
        self, key: str, attr: str, start: Any, end: Any, from_frame: int, to_frame: int
    ) -> FrameTimeline:
        """Interpolate an attribute linearly between two frames."""
        if to_frame <= from_frame:
            raise ValueError(f"a ramp must end after it starts ({from_frame} -> {to_frame})")
        self._keyframes.append(
            Keyframe(
                self._check(from_frame),
                key,
                attr,
                start,
                ramp_to=end,
                until_frame=self._check(to_frame),
            )
        )
        return self

    def show(self, key: str, from_frame: int, to_frame: int | None = None) -> FrameTimeline:
        """Draw ``key`` from one frame until another (exclusive), or to the end."""
        end = self.n_frames if to_frame is None else self._check(to_frame)
        spans = self._visible.setdefault(key, [])
        spans.append((self._check(from_frame), True))
        spans.append((end, False))
        return self

    def event(self, frame: int, name: str) -> FrameTimeline:
        """Queue an event on a specific frame's flip."""
        self._events.setdefault(self._check(frame), []).append(name)
        return self

    def _check(self, frame: int) -> int:
        if not 0 <= frame <= self.n_frames:
            raise ValueError(f"frame {frame} is outside this timeline (0..{self.n_frames})")
        return frame

    # -- playback ------------------------------------------------------

    def settings_at(self, frame: int) -> list[tuple[str, str, Any]]:
        """``(stimulus key, attribute, value)`` for one frame: every keyframe
        that has taken effect by now, with ramps evaluated at this frame."""
        settings: list[tuple[str, str, Any]] = []
        for kf in self._keyframes:
            if frame < kf.frame:
                continue
            if kf.ramp_to is None or kf.until_frame is None:
                settings.append((kf.key, kf.attr, kf.value))
                continue
            span = kf.until_frame - kf.frame
            fraction = min(max((frame - kf.frame) / span, 0.0), 1.0)
            settings.append((kf.key, kf.attr, _interpolate(kf.value, kf.ramp_to, fraction)))
        return settings

    def visible_at(self, frame: int) -> list[str]:
        """Which stimuli are drawn on this frame, in the order they were
        first shown — so a timeline's draw order is its declaration order."""
        drawn = []
        for key, spans in self._visible.items():
            state = False
            for at_frame, visible in sorted(spans):
                if frame >= at_frame:
                    state = visible
            if state:
                drawn.append(key)
        return drawn

    def events_at(self, frame: int) -> list[str]:
        return list(self._events.get(frame, ()))


def _interpolate(start: Any, end: Any, fraction: float) -> Any:
    if isinstance(start, tuple):
        return tuple(s + (e - s) * fraction for s, e in zip(start, end, strict=True))
    return start + (end - start) * fraction
