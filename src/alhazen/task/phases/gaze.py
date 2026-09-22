"""Gaze-driven phases: acquire fixation, hold it, respond, land.

Between them these are the state machine of every fixation-and-saccade
experiment, and they are what a ported task should be able to compose without
writing a phase of its own.

Two rules run through all of them:

- **The blink rule.** ``ctx.regions[...].contains(None)`` is False, so an
  unverifiable gaze sample is outside every region. Fixation is never credited
  when it cannot be verified, and a blink on the final frame of a hold is a
  break, not a lucky pass — which is why the gaze check comes before the
  completion check, not after.
- **Plain values in, no config.** Constructors take seconds, region names,
  stimulus keys and Outcomes. Resolving a ``Duration`` against the measured
  refresh rate is the task's job, done once in ``build_trial``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from alhazen.core.trial import CircleRegion, Outcome, PhaseAction, TrialContext


def _draw(ctx: TrialContext, keys: list[str] | tuple[str, ...]) -> None:
    """Update and draw the named stimuli for this frame."""
    for key in keys:
        stimulus = ctx.stimuli[key]
        stimulus.update(ctx.dt)
        stimulus.draw()


class AcquireFixation:
    """Wait for gaze to enter the fixation window and hold it there.

    The hold timer resets on any excursion: the subject must hold fixation
    *continuously*, not accumulate the same total with gaps in it. Optionally
    blinks the fixation point on and off, which is what draws a naive subject's
    eye to it — timed on the clock rather than on frames, so the rate is right
    whatever the refresh rate.
    """

    name = "acquire_fixation"

    def __init__(
        self,
        fixation_key: str = "fixation",
        region: str = "fixation",
        hold_s: float = 0.0,
        timeout_s: float = 2.0,
        on_timeout: Outcome | None = None,
        blink_period_s: float | None = None,
        onset_event: str | None = "FIX_ON",
        acquired_event: str | None = "FIX_ACQUIRED",
    ) -> None:
        if on_timeout is None:
            raise ValueError("AcquireFixation needs an on_timeout outcome")
        self._fixation_key = fixation_key
        self._region = region
        self._hold_s = hold_s
        self._timeout_s = timeout_s
        self._on_timeout = on_timeout
        self._blink_period_s = blink_period_s
        self._onset_event = onset_event
        self._acquired_event = acquired_event

    def on_enter(self, ctx: TrialContext) -> None:
        self._t0 = ctx.clock.now()
        self._blink_t = self._t0
        self._visible = True
        self._hold_start: float | None = None
        if self._onset_event is not None:
            ctx.emit_on_flip(self._onset_event)

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        now = ctx.clock.now()
        if now - self._t0 >= self._timeout_s:
            return self._on_timeout
        if self._blink_period_s is not None and now - self._blink_t >= self._blink_period_s:
            self._visible = not self._visible
            self._blink_t = now
        if self._visible:
            _draw(ctx, [self._fixation_key])

        if ctx.regions[self._region].contains(ctx.inputs.gaze):
            if self._hold_start is None:
                self._hold_start = now
            # Tested on the same frame the hold started, not the next one:
            # hold_s=0 means "acquired as soon as gaze is inside", and a
            # phase that spent a frame deciding otherwise would report a
            # latency one frame longer than the subject actually took.
            if now - self._hold_start >= self._hold_s:
                if self._acquired_event is not None:
                    ctx.emit_on_flip(self._acquired_event)
                ctx.record["acquire_latency_s"] = now - self._t0
                return PhaseAction.ADVANCE
        else:
            # Reset, not paused: the hold must be continuous, so the next
            # in-window frame starts an entirely fresh clock.
            self._hold_start = None
        return PhaseAction.CONTINUE


class HoldFixation:
    """Keep gaze inside a window for a jittered duration.

    The jitter is drawn once per trial from the session rng: a fixed foreperiod
    lets a subject time its response to the stimulus rather than react to it,
    which turns a reaction time into a guess about the clock.
    """

    name = "hold_fixation"

    def __init__(
        self,
        fixation_key: str = "fixation",
        region: str = "fixation",
        duration_s: float = 0.5,
        jitter_s: float = 0.0,
        on_break: Outcome | None = None,
        concurrent: list[str] | None = None,
        onset_event: str | None = None,
    ) -> None:
        if on_break is None:
            raise ValueError("HoldFixation needs an on_break outcome")
        if jitter_s < 0:
            raise ValueError("jitter_s must be >= 0")
        self._fixation_key = fixation_key
        self._region = region
        self._duration_s = duration_s
        self._jitter_s = jitter_s
        self._on_break = on_break
        self._concurrent = list(concurrent or [])
        self._onset_event = onset_event

    def on_enter(self, ctx: TrialContext) -> None:
        self._t0 = ctx.clock.now()
        self._duration = (
            ctx.rng.uniform(self._duration_s - self._jitter_s, self._duration_s + self._jitter_s)
            if self._jitter_s > 0
            else self._duration_s
        )
        ctx.record["hold_duration_s"] = self._duration
        if self._onset_event is not None:
            ctx.emit_on_flip(self._onset_event)

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        # Checked before the completion check below, deliberately: even on the
        # frame the hold would otherwise finish on, an unverifiable sample is a
        # break, never a lucky completion.
        if not ctx.regions[self._region].contains(ctx.inputs.gaze):
            return self._on_break
        _draw(ctx, [self._fixation_key, *self._concurrent])
        if ctx.clock.now() - self._t0 >= self._duration:
            return PhaseAction.ADVANCE
        return PhaseAction.CONTINUE


class StimulusResponse:
    """Show the stimulus and measure when gaze leaves the fixation window.

    Saccade onset is "gaze is no longer verifiably inside the window", not the
    tracker's own saccade parser, so a reaction time means the same thing on
    every backend rather than whatever each vendor's parser decided. A lost
    sample mid-saccade therefore also reads as departure; that is the rule
    behaving as intended, not a gap in it.

    The reaction time is measured from the *flip* that showed the stimulus
    (``ctx.record["t_<onset_event>"]``, stamped by the engine after the flip),
    never from the Python call that drew it.

    Where the eye *left from* is recorded too, as
    ``<depart_region>_x_dva``/``_y_dva``. A saccade is a displacement, and a
    displacement measured from an assumed origin is an assumption: the eye
    sits wherever the subject's fixation and the calibration put it, which is
    near the fixation point and not on it. The last sample verifiably inside
    the window is that origin, and it is the honest one.
    """

    name = "stimulus_response"

    def __init__(
        self,
        stimulus_key: str,
        depart_region: str = "fixation",
        timeout_s: float = 1.0,
        on_timeout: Outcome | None = None,
        onset_event: str = "STIM_ON",
        response_event: str | None = "RESPONSE_ONSET",
        rt_record_key: str = "rt_ms",
        concurrent: list[str] | None = None,
        start_record_prefix: str | None = None,
    ) -> None:
        if on_timeout is None:
            raise ValueError("StimulusResponse needs an on_timeout outcome")
        self._stimulus_key = stimulus_key
        self._depart_region = depart_region
        self._timeout_s = timeout_s
        self._on_timeout = on_timeout
        self._onset_event = onset_event
        self._response_event = response_event
        self._rt_record_key = rt_record_key
        self._concurrent = list(concurrent or [])
        # Named after the region departed from, so a task that launches from
        # somewhere other than a fixation point gets columns that say so.
        self._start_prefix = start_record_prefix or depart_region

    def on_enter(self, ctx: TrialContext) -> None:
        ctx.emit_on_flip(self._onset_event)
        self._launch: tuple[float, float] | None = None

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        _draw(ctx, [self._stimulus_key, *self._concurrent])
        inside = ctx.regions[self._depart_region].contains(ctx.inputs.gaze)
        if inside:
            # Latched every frame, including the frames before the flip: the
            # sample that matters is the last one still in the window, and
            # departure is detected on the frame after it.
            self._launch = ctx.inputs.gaze
        onset_t = ctx.record.get(f"t_{self._onset_event.lower()}")
        if onset_t is None:
            # The stimulus has been drawn but not yet flipped: there is no
            # honest onset time to measure a reaction from.
            return PhaseAction.CONTINUE
        now = ctx.clock.now()
        if not inside:
            ctx.record[self._rt_record_key] = (now - onset_t) * 1000.0
            self._record_launch(ctx)
            if self._response_event is not None:
                ctx.emit_on_flip(self._response_event)
            return PhaseAction.ADVANCE
        if now - onset_t >= self._timeout_s:
            return self._on_timeout
        return PhaseAction.CONTINUE

    def _record_launch(self, ctx: TrialContext) -> None:
        """Where the saccade started, in degrees.

        Nothing is written when no sample was ever verified inside the window
        — a trial whose origin is unknown must read as unknown, not as the
        centre of the screen.
        """
        if self._launch is None:
            return
        ctx.record[f"{self._start_prefix}_x_dva"] = ctx.screen.px2deg(self._launch[0])
        ctx.record[f"{self._start_prefix}_y_dva"] = ctx.screen.px2deg(self._launch[1])


class LandingCheck:
    """Wait for gaze to enter the target region — *not* where the saccade ends.

    .. warning::
       The endpoint recorded on a hit is the **first sample inside the
       region**, taken on the frame gaze crosses into it — mid-flight, for any
       window generous enough to catch a real saccade. With a 3° window a 5°
       saccade enters it 2–3° short of where it comes to rest, and that
       crossing point is what ``endpoint_x/y_dva`` and ``endpoint_error_dva``
       say. This phase answers "did the eye pass through the target?". For
       where the eye *landed*, and any analysis that filters on landing
       error, use :class:`LandingSample`. Kept as it is because experiments
       already depend on its timing and its columns.

    Where gaze arrives is recorded whether or not it was inside the target —
    on a timeout from the last verifiable sample. Recording only successful
    arrivals would leave a dataset of exactly the trials that agreed with the
    hypothesis.
    """

    name = "landing_check"

    def __init__(
        self,
        region: str = "target",
        timeout_s: float = 0.5,
        on_hit: Outcome | str | None = None,
        on_miss: Outcome | str | None = None,
        stimulus_keys: list[str] | None = None,
        landed_event: str | None = "LANDED",
        record_prefix: str = "endpoint",
    ) -> None:
        # Either may be PhaseAction.ADVANCE instead of an Outcome: the
        # endpoint is on the record either way (``<prefix>_in_target``), and a
        # trial that shows feedback needs the next phase to read it and end
        # the trial, rather than this one ending it first.
        if on_hit is None or on_miss is None:
            raise ValueError(
                "LandingCheck needs both on_hit and on_miss — an Outcome each, or "
                "PhaseAction.ADVANCE to let a following phase (TrialFeedback) end the trial"
            )
        self._region = region
        self._timeout_s = timeout_s
        self._on_hit = on_hit
        self._on_miss = on_miss
        self._stimulus_keys = list(stimulus_keys or [])
        self._landed_event = landed_event
        self._prefix = record_prefix

    def on_enter(self, ctx: TrialContext) -> None:
        self._t0 = ctx.clock.now()
        self._last_gaze: tuple[float, float] | None = None

    def _record_endpoint(
        self, ctx: TrialContext, gaze: tuple[float, float] | None, in_target: bool
    ) -> None:
        ctx.record[f"{self._prefix}_in_target"] = in_target
        if gaze is None:
            return
        # Degrees, not pixels: a saccade endpoint in px is meaningless
        # across rigs, and Screen's conversion is the exact inverse of the
        # one that placed the target.
        ctx.record[f"{self._prefix}_x_dva"] = ctx.screen.px2deg(gaze[0])
        ctx.record[f"{self._prefix}_y_dva"] = ctx.screen.px2deg(gaze[1])
        # How far it missed by, as one number. The coordinates alone cannot be
        # averaged across a condition that moves the target: a task with left
        # and right targets averages its endpoints to roughly zero and reports
        # perfect aim. A distance from the target the trial was actually given
        # stays meaningful however the trials are grouped.
        center = ctx.regions[self._region].center
        ctx.record[f"{self._prefix}_error_dva"] = ctx.screen.px2deg(
            math.hypot(gaze[0] - center[0], gaze[1] - center[1])
        )

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        _draw(ctx, self._stimulus_keys)
        if ctx.inputs.gaze is not None:
            # Latched every frame, so a track loss right before the timeout
            # still leaves a real position to fall back on.
            self._last_gaze = ctx.inputs.gaze
        if ctx.regions[self._region].contains(ctx.inputs.gaze):
            self._record_endpoint(ctx, ctx.inputs.gaze, in_target=True)
            if self._landed_event is not None:
                ctx.emit_on_flip(self._landed_event)
            return self._on_hit
        if ctx.clock.now() - self._t0 >= self._timeout_s:
            self._record_endpoint(ctx, self._last_gaze, in_target=False)
            return self._on_miss
        return PhaseAction.CONTINUE


# Where a landing is measured from: a fixed point in centered px, or a
# function of the trial context for a figure that moves (read on the frame the
# landing is judged, so it is where the figure was *then*).
Reference = tuple[float, float] | Callable[[TrialContext], tuple[float, float]]


def _landing_verdict(label: str, verdict: Outcome | str | None) -> Outcome | str:
    """A verdict LandingSample can end with: an Outcome, or PhaseAction.ADVANCE.

    Checked at construction, because anything else (None, CONTINUE, a typo)
    would only fail on the frame the landing is judged, with a subject in
    the rig.
    """
    if isinstance(verdict, Outcome):
        return verdict
    if verdict == PhaseAction.ADVANCE:
        return PhaseAction.ADVANCE
    raise ValueError(
        f"LandingSample needs both on_hit and on_miss, and {label} is {verdict!r} — "
        "pass an Outcome each, or PhaseAction.ADVANCE to let a following phase "
        "(TrialFeedback, a pursuit) run"
    )


class LandingSample:
    """Record where the saccade came to rest, and judge *that* against the
    target.

    :class:`LandingCheck` stops on the first frame gaze is inside the region,
    which is mid-flight for any window wide enough to catch a real saccade.
    This phase ignores the region until the movement is over, keeps the last
    valid gaze sample on every frame, and tests that one endpoint once, at the
    end. It ends in exactly one of two ways:

    - **fixed dwell** (``dwell_s``): ``dwell_s`` after saccade onset — long
      enough for any saccade in the task to have finished;
    - **saccade offset** (``settle_speed_dva_per_s`` with ``max_wait_s``): on
      the first *new* gaze sample whose speed since the previous new sample is
      below the threshold, or at ``max_wait_s`` after onset if the eye never
      settles. For a task whose next phase (a pursuit) starts at the landing
      and cannot wait out a fixed dwell.

    The settle rule counts only frames that carried a new sample, told apart
    by ``InputFrame.gaze_t``: a display frame with no new sample repeats the
    last position, and a speed computed across that repeat is a false zero
    that would end the phase mid-saccade. Speed is distance over the real
    time between the two samples, never over the nominal frame period. A
    missing sample (a blink, a track loss) is never settled — the blink rule
    — and also forgets the previous sample, because a speed across a gap in
    the data says nothing about the eye.

    Saccade onset is the flip-stamped time ``ctx.record["t_<onset_event>"]``:
    by default ``RESPONSE_ONSET``, which :class:`StimulusResponse` emits when
    gaze leaves the fixation window. ``onset_event=None`` times from this
    phase's own start instead.

    Records, under ``record_prefix`` (default ``endpoint``, the same names
    :class:`LandingCheck` writes, so an analysis reads either):

    - ``<prefix>_measured``: False when no valid sample arrived at all. Then
      nothing else about the endpoint is written — a trial whose landing is
      unknown must read as unknown, not as the centre of the screen — and the
      verdict is a miss (the blink rule).
    - ``<prefix>_in_target``: the verdict.
    - ``<prefix>_x_dva`` / ``_y_dva`` / ``_error_dva``: the endpoint, and its
      distance from the reference.
    - ``<prefix>_latency_ms``: from saccade onset to the endpoint sample.
    - ``<prefix>_reference_x_dva`` / ``_y_dva``: what the error was measured
      from — for a moving figure, the only record of where it was.
    - ``<prefix>_settled`` (saccade-offset mode only): False when the phase
      ended at ``max_wait_s`` rather than on a settled sample; the endpoint is
      then the last valid sample, judged the same way, and this column is
      what lets an analysis exclude it.

    ``reference`` is where the landing is measured from, in centered px, or a
    callable taking the ``TrialContext`` for a figure that moves. It defaults
    to the region's centre. When given, the verdict is "within the region's
    radius of the reference": the named region supplies the tolerance, the
    reference supplies where it is.
    """

    name = "landing_sample"

    def __init__(
        self,
        region: str = "target",
        on_hit: Outcome | str | None = None,
        on_miss: Outcome | str | None = None,
        *,
        dwell_s: float | None = None,
        settle_speed_dva_per_s: float | None = None,
        max_wait_s: float | None = None,
        onset_event: str | None = "RESPONSE_ONSET",
        reference: Reference | None = None,
        stimulus_keys: list[str] | None = None,
        landed_event: str | None = "LANDED",
        record_prefix: str = "endpoint",
    ) -> None:
        # Either verdict may be PhaseAction.ADVANCE instead of an Outcome, as
        # in LandingCheck: the verdict is on the record either way, and a
        # trial that shows feedback or starts a pursuit at the landing needs
        # the next phase to run rather than this one ending the trial.
        self._on_hit = _landing_verdict("on_hit", on_hit)
        self._on_miss = _landing_verdict("on_miss", on_miss)
        # Exactly one way of ending. Both given would leave it undecided which
        # one wins; neither would leave the phase with no end at all.
        if (dwell_s is None) == (settle_speed_dva_per_s is None):
            raise ValueError(
                "LandingSample ends one of two ways: pass exactly one of dwell_s (a fixed "
                "dwell from saccade onset) or settle_speed_dva_per_s together with max_wait_s "
                f"(at saccade offset); got dwell_s={dwell_s!r}, "
                f"settle_speed_dva_per_s={settle_speed_dva_per_s!r}"
            )
        if dwell_s is not None:
            # A zero dwell would sample the eye at onset — the launch point,
            # never a landing.
            if dwell_s <= 0:
                raise ValueError(f"dwell_s must be > 0 s, got {dwell_s}")
            if max_wait_s is not None:
                raise ValueError(
                    "max_wait_s caps the saccade-offset mode only; with dwell_s the dwell is "
                    "when the phase ends. Drop max_wait_s, or use settle_speed_dva_per_s"
                )
        if settle_speed_dva_per_s is not None:
            if settle_speed_dva_per_s <= 0:
                raise ValueError(
                    f"settle_speed_dva_per_s must be > 0 deg/s, got {settle_speed_dva_per_s}"
                )
            # No default cap: an eye that never slows below threshold (a
            # drift, a pursuit, a noisy tracker) would hold the trial forever.
            if max_wait_s is None or max_wait_s <= 0:
                raise ValueError(
                    "settle_speed_dva_per_s needs max_wait_s > 0: the time after onset to stop "
                    f"waiting for an eye that never settles (got max_wait_s={max_wait_s!r})"
                )
        self._region = region
        self._dwell_s = dwell_s
        self._settle_speed = settle_speed_dva_per_s
        self._max_wait_s = max_wait_s
        self._onset_event = onset_event
        self._reference = reference
        self._stimulus_keys = list(stimulus_keys or [])
        self._landed_event = landed_event
        self._prefix = record_prefix

    def on_enter(self, ctx: TrialContext) -> None:
        if self._onset_event is None:
            self._onset_t = ctx.clock.now()
        else:
            key = f"t_{self._onset_event.lower()}"
            onset_t = ctx.record.get(key)
            if onset_t is None:
                # Loud rather than falling back to "now": a dwell timed from
                # the wrong moment is a wrong measurement that looks right.
                raise ValueError(
                    f"LandingSample times the landing from ctx.record[{key!r}], and no earlier "
                    f"phase emitted {self._onset_event}. Put a StimulusResponse with "
                    f"response_event={self._onset_event!r} before it, name the event that marks "
                    "saccade onset with onset_event=, or pass onset_event=None to time from "
                    "this phase's start"
                )
            self._onset_t = float(onset_t)
        # The last valid sample and its time: the endpoint, whenever the
        # phase ends.
        self._endpoint: tuple[tuple[float, float], float] | None = None
        # Saccade-offset mode only: the previous NEW sample, the other end of
        # the next speed measurement.
        self._previous: tuple[tuple[float, float], float] | None = None

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        _draw(ctx, self._stimulus_keys)
        now = ctx.clock.now()
        gaze, gaze_t = ctx.inputs.gaze, ctx.inputs.gaze_t
        if gaze is not None:
            if gaze_t is None and self._settle_speed is not None:
                raise ValueError(
                    "LandingSample(settle_speed_dva_per_s=...) needs InputFrame.gaze_t to tell a "
                    "new gaze sample from a repeated one, and this frame has a position without "
                    "a time. The session's input provider sets it; a hand-built provider or a "
                    "scripted InputFrame must set gaze_t too"
                )
            # Latched on every frame, the region ignored: the endpoint is
            # wherever the eye was last seen when the phase ends. A dwell
            # has no use for sample times, so the frame's time stands in when
            # a provider supplies none.
            self._endpoint = (gaze, gaze_t if gaze_t is not None else now)

        if self._settle_speed is not None:
            assert self._max_wait_s is not None  # the constructor guarantees it
            # Settle before the cap: a sample that settles on the very frame
            # the cap expires is still a real saccade offset.
            if self._settles(ctx, gaze, gaze_t):
                return self._finish(ctx, settled=True)
            if now - self._onset_t >= self._max_wait_s:
                return self._finish(ctx, settled=False)
            return PhaseAction.CONTINUE

        assert self._dwell_s is not None
        if now - self._onset_t >= self._dwell_s:
            return self._finish(ctx, settled=None)
        return PhaseAction.CONTINUE

    def _settles(
        self, ctx: TrialContext, gaze: tuple[float, float] | None, gaze_t: float | None
    ) -> bool:
        """True when this frame carries a NEW sample slower than the threshold."""
        if gaze is None or gaze_t is None:
            # The blink rule: an unverifiable eye is never settled. The
            # previous sample is dropped too — the next valid one has nothing
            # honest to be compared with across the gap.
            self._previous = None
            return False
        previous = self._previous
        if previous is not None:
            if gaze_t == previous[1]:
                # The same sample again: no new information, and a speed
                # computed from it would be a false zero.
                return False
            if gaze_t < previous[1]:
                raise ValueError(
                    f"gaze sample times went backwards ({previous[1]} s, then {gaze_t} s): "
                    "InputFrame.gaze_t must be the sample's time on the session clock, which "
                    "only moves forward. Check the tracker backend or input provider"
                )
        self._previous = (gaze, gaze_t)
        if previous is None:
            return False  # a speed needs two samples
        threshold = self._settle_speed
        assert threshold is not None  # only called in saccade-offset mode
        (x0, y0), t0 = previous
        # Real time between the two samples, not the frame period: a tracker
        # sample can arrive late, early or twice in one frame's span.
        distance_dva = ctx.screen.px2deg(math.hypot(gaze[0] - x0, gaze[1] - y0))
        return distance_dva / (gaze_t - t0) < threshold

    def _reference_px(self, ctx: TrialContext) -> tuple[float, float]:
        if self._reference is None:
            center: tuple[float, float] = ctx.regions[self._region].center
            return center
        if callable(self._reference):
            return self._reference(ctx)
        return self._reference

    def _finish(self, ctx: TrialContext, settled: bool | None) -> str | Outcome:
        """Record the endpoint, judge it, and end the phase with the verdict."""
        p = self._prefix
        reference = self._reference_px(ctx)
        # The window is the named region's radius around the reference: the
        # region itself when the reference is its centre, and the same
        # tolerance carried along with a figure that moves.
        window = CircleRegion(reference, ctx.regions[self._region].radius)
        position = self._endpoint[0] if self._endpoint is not None else None
        hit = window.contains(position)  # None is outside: the blink rule

        ctx.record[f"{p}_measured"] = self._endpoint is not None
        ctx.record[f"{p}_in_target"] = hit
        ctx.record[f"{p}_reference_x_dva"] = ctx.screen.px2deg(reference[0])
        ctx.record[f"{p}_reference_y_dva"] = ctx.screen.px2deg(reference[1])
        if settled is not None:
            ctx.record[f"{p}_settled"] = settled
        if self._endpoint is not None:
            (x, y), t = self._endpoint
            # Degrees, and a distance as well as the coordinates, for the
            # reasons LandingCheck gives: px mean nothing across rigs, and
            # coordinates average away across targets on opposite sides.
            ctx.record[f"{p}_x_dva"] = ctx.screen.px2deg(x)
            ctx.record[f"{p}_y_dva"] = ctx.screen.px2deg(y)
            ctx.record[f"{p}_error_dva"] = ctx.screen.px2deg(
                math.hypot(x - reference[0], y - reference[1])
            )
            ctx.record[f"{p}_latency_ms"] = (t - self._onset_t) * 1000.0
            # Emitted only for a real landing: a trial that saw no eye, or
            # whose eye never settled before the cap, did not "land".
            if self._landed_event is not None and settled is not False:
                ctx.emit_on_flip(self._landed_event)
        return self._on_hit if hit else self._on_miss


def region_center_px(ctx: TrialContext, region: str) -> tuple[float, float]:
    """Convenience for tasks placing a stimulus on a named region."""
    value: Any = ctx.regions[region].center
    return value
