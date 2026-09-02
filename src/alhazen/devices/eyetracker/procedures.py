"""Validation and drift correction: what a session does with a calibrated
tracker, the same way on every backend.

A calibration fits the tracker's gaze model and says nothing about how good
the fit is. The two procedures here are how a session finds out, and what it
does about a small error:

- **Validation** shows the calibration's targets again, one at a time, and
  measures where the tracker says the subject is looking while they fixate
  each. The per-target error, in degrees of visual angle, is the number that
  decides whether today's fixation window is generous or impossible — and it
  is reported on the dashboard, not just in the log.
- **Drift correction** shows one target at the screen centre, measures the
  offset between it and the reported gaze, and applies that offset to every
  gaze position from then on (:class:`GazeCorrection`, which the session's
  input provider consults). A headrest settles, a camera gets nudged, and a
  calibration that was right at 9 am is a degree off at 10; a drift
  correction is the thirty-second fix, a recalibration the five-minute one.
  An offset too large to be a drift is refused, because shifting a gaze
  model that no longer applies would only hide that it does not.

Both are generic on purpose: they use the ``EyeTracker`` protocol's
``get_gaze()``, the display, the screen and a key source, and nothing else —
so they run on an EyeLink, a TRACKPixx3, the mouse and a scripted replay
alike, and are tested on the last of those.

Coordinates: **centered px** (origin at the screen centre, y up) throughout.
Targets are drawn in that frame; a ``GazeSample`` arrives in screen px and is
converted here with ``Screen.screen_to_centered`` — the same conversion the
input provider makes, and then the same correction, so a validation measures
exactly what a phase would see.

Walking a target: the target appears, the first ``settle_s`` are ignored
(the saccade to it), and then a window of ``sample_s`` of gaze is averaged
into the measurement. How that window is chosen is ``advance``:

- ``manual``: the experimenter presses SPACE when the subject is on the
  target, and the window is the ``sample_s`` that follow;
- ``auto``: the newest ``sample_s`` of gaze is watched, and the first window
  in which every sample is within ``stable_deg`` of the window's mean is
  taken as the fixation. A target that never gets one within ``timeout_s``
  is recorded as missed — a fact about the subject, reported as such.

SPACE accepts in either mode (in ``auto``, it takes the window as it stands,
once there is a full one), BACKSPACE steps back a target, ESC abandons the
procedure: the same three roles as the calibration walk, so the experimenter
learns one set of keys.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from alhazen.config.models import SELF_DRIVEN_CALIBRATION_TYPES, EyeTrackerConfig
from alhazen.core.clock import Clock
from alhazen.devices.eyetracker.protocol import EyeTracker, ProgressHook
from alhazen.display.backend import DisplayBackend
from alhazen.display.screen import Screen
from alhazen.stimuli.base import Stimulus
from alhazen.stimuli.fixation import make_fixation

log = logging.getLogger(__name__)

# The three key roles every eye-tracker procedure in alhazen reads. The
# viewpixx calibration walk imports these, so the two never drift apart.
ACCEPT_KEYS = ("space", "return", "num_enter")
REDO_KEY = "backspace"
ABORT_KEY = "escape"
PROCEDURE_KEYS = (*ACCEPT_KEYS, REDO_KEY, ABORT_KEY)

Advance = Literal["manual", "auto"]

# The procedures' "trial": recording segments a backend needs to hand out
# gaze (the EyeLink only samples while recording) are opened under this
# index. Real trials start at 1, so a reader cutting the eye record by trial
# can never mistake a validation's samples for a trial's.
PROCEDURE_TRIAL_INDEX = 0

# The target: a small bright disc, drawn through the same factory a task's
# fixation point uses, so it looks like the thing the subject fixates all day.
TARGET_SIZE_DVA = 0.5
TARGET_COLOR = (1.0, 1.0, 1.0)


@dataclass(frozen=True)
class ProcedureTiming:
    """How long each part of a target's measurement takes. One object rather
    than five keyword arguments, so a test can shorten all of it at once."""

    settle_s: float = 0.5  # gaze ignored after a target appears: the saccade to it
    sample_s: float = 0.3  # the window of gaze averaged into a measurement
    stable_deg: float = 1.0  # auto: every sample within this of the window mean = fixating
    timeout_s: float = 10.0  # auto: give up on a target after this long


# The timing every procedure runs with unless a caller (a test) says otherwise.
DEFAULT_TIMING = ProcedureTiming()


@dataclass(frozen=True)
class TargetError:
    """One target's measurement: where it was, where gaze landed, how far off.

    ``gaze_px`` and ``error_deg`` are None for a missed target — no usable
    gaze was measured while it was up — which is reported, never skipped.
    """

    target_px: tuple[float, float]
    gaze_px: tuple[float, float] | None
    error_deg: float | None
    n_samples: int


@dataclass(frozen=True)
class ValidationResult:
    """Every target's error, and the verdict against the rig's threshold."""

    targets: tuple[TargetError, ...]
    threshold_deg: float
    t: float  # session clock, when the procedure finished
    advance: str = "manual"
    aborted: bool = False  # ESC before the last target: what was measured is kept, no verdict

    @property
    def measured(self) -> list[TargetError]:
        return [target for target in self.targets if target.error_deg is not None]

    @property
    def n_missed(self) -> int:
        return len(self.targets) - len(self.measured)

    @property
    def mean_error_deg(self) -> float | None:
        errors = [t.error_deg for t in self.measured if t.error_deg is not None]
        return sum(errors) / len(errors) if errors else None

    @property
    def max_error_deg(self) -> float | None:
        errors = [t.error_deg for t in self.measured if t.error_deg is not None]
        return max(errors) if errors else None

    @property
    def accepted(self) -> bool:
        """Passed: every target measured, and the worst within the threshold.

        A missed target fails the validation rather than being left out of
        the average: the region of the screen it stands for is unmeasured,
        and "accurate everywhere we could measure" is not the question.
        """
        worst = self.max_error_deg
        return (
            not self.aborted
            and self.n_missed == 0
            and worst is not None
            and worst <= self.threshold_deg
        )

    def summary(self) -> str:
        """One line for a log or a panel."""
        if self.aborted:
            return f"validation aborted after {len(self.targets)} target(s)"
        mean, worst = self.mean_error_deg, self.max_error_deg
        if mean is None or worst is None:
            return f"validation FAILED: no target measured ({len(self.targets)} missed)"
        verdict = "passed" if self.accepted else "FAILED"
        line = (
            f"validation {verdict}: mean {mean:.2f}°, worst {worst:.2f}° "
            f"(limit {self.threshold_deg:g}°)"
        )
        if self.n_missed:
            line += f", {self.n_missed} target(s) missed"
        return line

    def payload(self) -> dict[str, Any]:
        """What the session event carries — the numbers, JSON-shaped."""
        return {
            "accepted": self.accepted,
            "aborted": self.aborted,
            "advance": self.advance,
            "threshold_deg": self.threshold_deg,
            "mean_error_deg": self.mean_error_deg,
            "max_error_deg": self.max_error_deg,
            "n_targets": len(self.targets),
            "n_missed": self.n_missed,
            "targets": [
                {
                    "target_px": list(target.target_px),
                    "gaze_px": list(target.gaze_px) if target.gaze_px is not None else None,
                    "error_deg": target.error_deg,
                    "n_samples": target.n_samples,
                }
                for target in self.targets
            ],
        }


@dataclass(frozen=True)
class DriftResult:
    """One centre target's offset, and whether it was applied."""

    target_px: tuple[float, float]
    gaze_px: tuple[float, float] | None  # None: no usable gaze while the target was up
    offset_px: tuple[float, float] | None  # target − gaze, the shift that would correct it
    offset_deg: float | None
    max_deg: float  # the rig's limit, above which an offset is not a drift
    applied: bool
    t: float
    note: str = ""

    def summary(self) -> str:
        if self.offset_deg is None:
            return f"drift correction not applied: {self.note or 'no gaze measured'}"
        state = "applied" if self.applied else "REFUSED"
        line = f"drift correction {state}: offset {self.offset_deg:.2f}° (limit {self.max_deg:g}°)"
        return f"{line} — {self.note}" if self.note else line

    def payload(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "target_px": list(self.target_px),
            "gaze_px": list(self.gaze_px) if self.gaze_px is not None else None,
            "offset_px": list(self.offset_px) if self.offset_px is not None else None,
            "offset_deg": self.offset_deg,
            "max_deg": self.max_deg,
            "note": self.note,
        }


class GazeCorrection:
    """The shift a drift correction adds to every gaze position.

    Held by the session and consulted by its input provider *after* the
    screen-to-centered conversion, so the shift is in centered px like the
    positions it corrects. Corrections accumulate — each one measures the
    residual with the previous ones in force and adds to them — and a new
    calibration resets the lot, because it replaces the model they were
    correcting.
    """

    def __init__(self) -> None:
        self._dx = 0.0
        self._dy = 0.0
        # Session time of the last change; None until a correction is applied.
        self.t: float | None = None

    @property
    def offset(self) -> tuple[float, float]:
        return (self._dx, self._dy)

    @property
    def active(self) -> bool:
        return self._dx != 0.0 or self._dy != 0.0

    def apply(self, gaze: tuple[float, float]) -> tuple[float, float]:
        """A corrected position — the same position when nothing is applied."""
        return (gaze[0] + self._dx, gaze[1] + self._dy)

    def shift_by(self, dx: float, dy: float, t: float) -> None:
        self._dx += dx
        self._dy += dy
        self.t = t

    def reset(self, t: float) -> None:
        self._dx = 0.0
        self._dy = 0.0
        self.t = t


def validation_targets(cfg: EyeTrackerConfig, screen: Screen) -> list[tuple[float, float]]:
    """Where a validation's targets go: the calibration's own grid.

    The grid comes from the same layout function the viewpixx calibration
    walks, for the layouts alhazen can lay out. The EyeLink accepts layouts
    its Host PC owns and alhazen cannot enumerate (H3, HV3, ...); a rig
    configured with one of those is validated over HV5 instead, and the log
    says so — the point is a measurement over the field, and five points
    cover it.
    """
    from alhazen.devices.eyetracker.viewpixx import calibration_targets

    layout = cfg.calibration_type
    if layout not in SELF_DRIVEN_CALIBRATION_TYPES:
        log.info(
            "calibration_type %r has no target layout of alhazen's own; validating over HV5",
            layout,
        )
        layout = "HV5"
    return calibration_targets(layout, screen, cfg.calibration_area)


def _mean(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    n = float(len(points))
    return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n)


def _spread_px(points: Sequence[tuple[float, float]]) -> float:
    """The farthest any point sits from the points' mean."""
    centre = _mean(points)
    return max(math.dist(centre, p) for p in points)


class _Walk:
    """The state one procedure carries through its targets: the devices, the
    key source, and the two things a measurement needs to know about time."""

    def __init__(
        self,
        *,
        tracker: EyeTracker,
        display: DisplayBackend,
        screen: Screen,
        clock: Clock,
        poll_keys: Callable[[], list[str]],
        correction: GazeCorrection | None,
        make_target: Callable[[tuple[float, float]], Stimulus],
        advance: Advance,
        timing: ProcedureTiming,
        progress: ProgressHook | None,
        stage: str,
    ) -> None:
        self._tracker = tracker
        self._display = display
        self._screen = screen
        self._clock = clock
        self._poll_keys = poll_keys
        self._correction = correction
        self._make_target = make_target
        self._advance = advance
        self._timing = timing
        self._progress = progress
        self._stage = stage
        self._stable_px = screen.deg2px(timing.stable_deg)

    def gaze_now(self) -> tuple[float, float] | None:
        """The tracker's newest position as a phase would see it: centered
        px, corrected — or None, which stays None (the blink rule)."""
        sample = self._tracker.get_gaze()
        if sample is None:
            return None
        gaze = self._screen.screen_to_centered(sample.gx, sample.gy)
        return self._correction.apply(gaze) if self._correction is not None else gaze

    def _frame(self, target: Stimulus) -> list[str]:
        """Draw the target and flip; return the keys pressed meanwhile.

        The flip is what paces the loop: on a real display it blocks until
        the next vertical refresh, so gaze is sampled once per frame, which
        at any refresh rate is plenty for a mean over a third of a second.
        """
        target.draw()
        self._display.flip()
        return [key.lower() for key in self._poll_keys()]

    def measure(self, index: int, total: int, position: tuple[float, float]) -> TargetError | str:
        """Show one target and measure gaze on it.

        Returns the measurement, or the key that ended it early: ``REDO_KEY``
        (step back) or ``ABORT_KEY``. Otherwise the target ends when its
        window closes — manual mode: ``sample_s`` after the accept key; auto
        mode: the first stable window — or, in auto mode, on the timeout.
        """
        target = self._make_target(position)
        shown_at = self._clock.now()
        settled_at = shown_at + self._timing.settle_s
        # Gaze during the window, newest last. In auto mode this is a rolling
        # window of the last sample_s; in manual mode it starts filling only
        # once the experimenter accepts.
        window: list[tuple[float, tuple[float, float] | None]] = []
        accepted_at: float | None = None
        last_report = -math.inf
        while True:
            keys = self._frame(target)
            now = self._clock.now()
            if ABORT_KEY in keys:
                return ABORT_KEY
            if REDO_KEY in keys:
                return REDO_KEY
            if any(key in ACCEPT_KEYS for key in keys):
                if self._advance == "manual":
                    accepted_at = now  # start (or restart) the measurement window
                    window = []
                elif self._usable(window, now):
                    # Auto mode: the experimenter takes what the window holds
                    # rather than waiting for it to settle further.
                    return self._result(position, window)
                else:
                    log.info("accept ignored: no full window of gaze on this target yet")
            if now < settled_at:
                continue  # the saccade to the target: not a measurement
            if now - last_report >= 0.5 and self._progress is not None:
                # Said by the procedure, not by the backend: what is on screen
                # and what the experimenter is waiting for.
                self._progress(self._stage, f"target {index + 1} of {total}")
                last_report = now
            gaze = self.gaze_now()
            if self._advance == "manual":
                if accepted_at is None:
                    continue  # nothing is measured until the experimenter says so
                window.append((now, gaze))
                # An accept pressed before the settle ends starts the window
                # when the settle does, not earlier.
                if now - max(accepted_at, settled_at) >= self._timing.sample_s:
                    return self._result(position, window)
                continue
            # auto: keep the newest sample_s of gaze and wait for it to hold still.
            window.append((now, gaze))
            window = [(t, g) for t, g in window if now - t <= self._timing.sample_s]
            if self._usable(window, now) and self._stable(window):
                return self._result(position, window)
            if now - shown_at >= self._timing.timeout_s:
                log.warning(
                    "target %d of %d: no stable fixation within %.1f s; recorded as missed",
                    index + 1,
                    total,
                    self._timing.timeout_s,
                )
                return TargetError(position, None, None, 0)

    def _usable(self, window: list[tuple[float, tuple[float, float] | None]], now: float) -> bool:
        """A full window's worth of gaze, with the eye seen in all of it."""
        if not window:
            return False
        full = now - window[0][0] >= self._timing.sample_s * 0.9  # one frame's slack
        return full and all(gaze is not None for _, gaze in window)

    def _stable(self, window: list[tuple[float, tuple[float, float] | None]]) -> bool:
        points = [gaze for _, gaze in window if gaze is not None]
        return _spread_px(points) <= self._stable_px

    def _result(
        self,
        position: tuple[float, float],
        window: list[tuple[float, tuple[float, float] | None]],
    ) -> TargetError:
        """The mean of the usable samples in the window, or a missed target.

        In manual mode a blink inside the window is dropped rather than
        failing the target: the experimenter saw the fixation, the mean of
        the rest is what they accepted. No usable sample at all is a miss.
        """
        points = [gaze for _, gaze in window if gaze is not None]
        if not points:
            return TargetError(position, None, None, 0)
        mean = _mean(points)
        error = self._screen.px2deg(math.dist(position, mean))
        return TargetError(position, mean, error, len(points))


def _walk_targets(
    walk: _Walk, positions: Sequence[tuple[float, float]]
) -> tuple[list[TargetError], bool]:
    """Measure every target in order; returns (measurements, aborted).

    BACKSPACE re-measures the previous target — the one the experimenter
    just watched the subject miss — not the one on screen, matching the
    calibration walk. ESC keeps what was measured and stops.
    """
    results: list[TargetError] = []
    index = 0
    while index < len(positions):
        outcome = walk.measure(index, len(positions), positions[index])
        if outcome == ABORT_KEY:
            log.warning("procedure aborted by the experimenter at target %d", index + 1)
            return results, True
        if outcome == REDO_KEY:
            index = max(0, index - 1)
            del results[index:]
            continue
        assert isinstance(outcome, TargetError)
        results.append(outcome)
        index += 1
    return results, False


def _recording_segment(tracker: EyeTracker, status: str) -> Callable[[], None]:
    """Open a recording segment for a procedure if the tracker has none open,
    and return what closes it — a no-op when the tracker was already
    recording (a caller mid-trial), because that segment is not ours."""
    if tracker.is_recording():
        return lambda: None
    tracker.start_trial(PROCEDURE_TRIAL_INDEX, status)
    return tracker.stop_trial


def _default_make_target(
    display: DisplayBackend, screen: Screen
) -> Callable[[tuple[float, float]], Stimulus]:
    return lambda pos: make_fixation(display, screen, TARGET_SIZE_DVA, TARGET_COLOR, pos)


def validate(
    tracker: EyeTracker,
    display: DisplayBackend,
    screen: Screen,
    clock: Clock,
    cfg: EyeTrackerConfig,
    *,
    poll_keys: Callable[[], list[str]],
    correction: GazeCorrection | None = None,
    advance: Advance | None = None,
    targets: Sequence[tuple[float, float]] | None = None,
    make_target: Callable[[tuple[float, float]], Stimulus] | None = None,
    progress: ProgressHook | None = None,
    timing: ProcedureTiming = DEFAULT_TIMING,
) -> ValidationResult:
    """Measure gaze error on every calibration target; see the module doc.

    ``advance`` defaults to the rig's ``calibration_advance``; ``targets``
    to the calibration's own grid (centered px). The result is returned, not
    acted on: the caller decides what a failed validation means.
    """
    advance = cfg.calibration_advance if advance is None else advance
    positions = list(targets) if targets is not None else validation_targets(cfg, screen)
    walk = _Walk(
        tracker=tracker,
        display=display,
        screen=screen,
        clock=clock,
        poll_keys=poll_keys,
        correction=correction,
        make_target=make_target or _default_make_target(display, screen),
        advance=advance,
        timing=timing,
        progress=progress,
        stage="validating",
    )
    close = _recording_segment(tracker, "validation")
    try:
        measured, aborted = _walk_targets(walk, positions)
    finally:
        close()
        display.flip()  # the last target off the screen, whatever happened
    result = ValidationResult(
        targets=tuple(measured),
        threshold_deg=cfg.accuracy_max_deg,
        t=clock.now(),
        advance=advance,
        aborted=aborted,
    )
    log.log(logging.INFO if result.accepted or aborted else logging.WARNING, result.summary())
    return result


def drift_correct(
    tracker: EyeTracker,
    display: DisplayBackend,
    screen: Screen,
    clock: Clock,
    cfg: EyeTrackerConfig,
    correction: GazeCorrection,
    *,
    poll_keys: Callable[[], list[str]],
    advance: Advance | None = None,
    target: tuple[float, float] = (0.0, 0.0),
    make_target: Callable[[tuple[float, float]], Stimulus] | None = None,
    progress: ProgressHook | None = None,
    timing: ProcedureTiming = DEFAULT_TIMING,
) -> DriftResult:
    """Measure the gaze offset on one target and, if it is a drift, apply it.

    The offset is measured with the corrections already in force, so what is
    added to ``correction`` is the residual. An offset over the rig's
    ``drift_max_deg`` is refused and reported: that is a calibration to redo,
    not a drift to absorb.
    """
    advance = cfg.calibration_advance if advance is None else advance
    walk = _Walk(
        tracker=tracker,
        display=display,
        screen=screen,
        clock=clock,
        poll_keys=poll_keys,
        correction=correction,
        make_target=make_target or _default_make_target(display, screen),
        advance=advance,
        timing=timing,
        progress=progress,
        stage="drift correcting",
    )
    close = _recording_segment(tracker, "drift correction")
    try:
        outcome = walk.measure(0, 1, target)
        # BACKSPACE has no previous target to step back to here; it just
        # measures again, which is what pressing it means anyway.
        while outcome == REDO_KEY:
            outcome = walk.measure(0, 1, target)
    finally:
        close()
        display.flip()
    now = clock.now()
    if outcome == ABORT_KEY:
        result = DriftResult(target, None, None, None, cfg.drift_max_deg, False, now, "aborted")
        log.warning(result.summary())
        return result
    assert isinstance(outcome, TargetError)
    if outcome.gaze_px is None:
        result = DriftResult(
            target, None, None, None, cfg.drift_max_deg, False, now, "no gaze measured"
        )
        log.warning(result.summary())
        return result
    offset = (target[0] - outcome.gaze_px[0], target[1] - outcome.gaze_px[1])
    offset_deg = screen.px2deg(math.hypot(*offset))
    if offset_deg > cfg.drift_max_deg:
        result = DriftResult(
            target,
            outcome.gaze_px,
            offset,
            offset_deg,
            cfg.drift_max_deg,
            False,
            now,
            "too large for a drift — recalibrate",
        )
        log.warning(result.summary())
        return result
    correction.shift_by(offset[0], offset[1], now)
    result = DriftResult(target, outcome.gaze_px, offset, offset_deg, cfg.drift_max_deg, True, now)
    log.info("%s; total correction now (%.1f, %.1f) px", result.summary(), *correction.offset)
    return result


__all__ = [
    "ABORT_KEY",
    "ACCEPT_KEYS",
    "PROCEDURE_KEYS",
    "PROCEDURE_TRIAL_INDEX",
    "REDO_KEY",
    "DriftResult",
    "GazeCorrection",
    "ProcedureTiming",
    "TargetError",
    "ValidationResult",
    "drift_correct",
    "validate",
    "validation_targets",
]
