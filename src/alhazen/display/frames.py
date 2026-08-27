"""Frame QA: per-frame interval recording plus a dropped-frame *policy*.

Every run records every frame interval; dropped frames are detected online
against the measured refresh rate and handled per the configured policy —
including actually aborting, which is the part a hand-rolled frame check
usually leaves commented out. Analysis gets the full interval log either
way; the policy only decides how loudly the live session reacts.

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

from alhazen.config.models import FrameQAConfig
from alhazen.errors import FrameQAError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrameRecord:
    trial_index: int
    t: float
    interval_s: float
    dropped: bool


class FrameMonitor:
    """Fed one timestamp per flip by the engine; keeps the log and applies
    the policy. The first flip of each trial only establishes the reference
    point — an inter-trial gap is not a dropped frame."""

    def __init__(self, cfg: FrameQAConfig, refresh_rate_hz: float) -> None:
        self._cfg = cfg
        self._expected = 1.0 / refresh_rate_hz
        self._threshold = self._expected * (1.0 + cfg.tolerance)
        self._records: list[FrameRecord] = []
        self._last_t: float | None = None
        self._trial_index = 0
        self._dropped_this_trial = 0

    @property
    def records(self) -> list[FrameRecord]:
        return list(self._records)

    def start_trial(self, trial_index: int) -> None:
        self._trial_index = trial_index
        self._last_t = None
        self._dropped_this_trial = 0

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
        if not dropped:
            return False

        self._dropped_this_trial += 1
        if self._cfg.policy == "log":
            log.debug("dropped frame on trial %d: %.1f ms", self._trial_index, interval * 1000)
        else:
            log.warning(
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

    @property
    def marks_trials(self) -> bool:
        """Whether the engine should count drops on the trial record — true
        for the two policies where analysis is meant to see per-trial drops."""
        return self._cfg.policy in ("mark_trial", "abort_run")

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
