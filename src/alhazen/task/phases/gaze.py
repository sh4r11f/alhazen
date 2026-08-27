"""Gaze-driven phases: acquire fixation, hold it, respond, land.

Between them these four are the state machine of every fixation-and-saccade
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
from typing import Any

from alhazen.core.trial import Outcome, PhaseAction, TrialContext


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
    """Wait to see where the saccade lands.

    Where gaze *actually* arrives is the measurement, whether or not it was
    inside the target, so the endpoint is recorded either way — on a timeout
    from the last verifiable sample. Recording only successful landings would
    leave a dataset of exactly the trials that agreed with the hypothesis.
    """

    name = "landing_check"

    def __init__(
        self,
        region: str = "target",
        timeout_s: float = 0.5,
        on_hit: Outcome | None = None,
        on_miss: Outcome | None = None,
        stimulus_keys: list[str] | None = None,
        landed_event: str | None = "LANDED",
        record_prefix: str = "endpoint",
    ) -> None:
        if on_hit is None or on_miss is None:
            raise ValueError("LandingCheck needs both on_hit and on_miss outcomes")
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


def region_center_px(ctx: TrialContext, region: str) -> tuple[float, float]:
    """Convenience for tasks placing a stimulus on a named region."""
    value: Any = ctx.regions[region].center
    return value
