"""A gaze-contingent fixation task: acquire fixation, then hold it.

The gaze-contingent counterpart to ``minimal_fixation``: same shape, but the
trial is driven by where the subject is actually looking. Note what is *not* here — no tracker,
no coordinate conversion, no device of any kind. A phase reads
``ctx.inputs.gaze`` in centered px and asks a named region whether it
contains that point; the rig decides where gaze comes from.

The blink rule is load-bearing in ``HoldFixation``: an unverifiable position
(a blink, a track loss) is outside every region, so it counts as a break.
Crediting fixation you cannot verify is how a task quietly rewards a subject
for closing its eyes.

The two phases are written out rather than taken from ``alhazen.task.phases``
(which has both) so this file shows what gaze-contingent phase logic actually
looks like. ``saccade_to_target`` is the same kind of trial, composed from the
library instead.
"""

from __future__ import annotations

import numpy as np

from alhazen import (
    CircleRegion,
    Condition,
    Duration,
    Model,
    Outcome,
    PhaseAction,
    Task,
    TrialContext,
    TrialPlan,
    TrialSetup,
    outcomes,
)
from alhazen.core.events import EventSchema
from alhazen.paradigms.config import SchedulerConfig
from alhazen.stimuli.fixation import make_fixation

EVENTS = EventSchema(("FIX_ON", "FIX_ACQUIRED"))

OUTCOMES = outcomes(
    # A trial where fixation was held to the end: the measurement exists.
    FIXATED=dict(completed=True, success=True),
    # Never looked at the point within the allowed time. Completed on
    # purpose: "this subject did not fixate" IS this task's measurement, and
    # a non-completed outcome would re-serve the same condition forever
    # against a subject who is not engaging.
    NO_FIXATION=dict(completed=True, success=False),
    # Looked, then looked away: the trial was interrupted before it measured
    # anything, so it is re-served (paradigms/base.py re-queues every
    # completed=False outcome).
    FIX_BREAK=dict(completed=False),
)


class GazeFixationParams(Model):
    fix_size_dva: float = 0.3
    fix_window_dva: float = 2.0  # radius of the window gaze must fall inside
    acquire_timeout: Duration = Duration(ms=2000)
    hold_duration: Duration = Duration(ms=500)
    iti: Duration = Duration(ms=200)
    # How many trials belongs to the paradigm, not to a second field beside
    # it that could disagree.
    paradigm: SchedulerConfig = SchedulerConfig(n_per_condition=5)


class AcquireFixation:
    """Show the fixation point and wait for gaze to land inside its window."""

    name = "acquire_fixation"

    def __init__(self, timeout_s: float, on_timeout: Outcome) -> None:
        self._timeout_s = timeout_s
        self._on_timeout = on_timeout
        self._t0 = 0.0

    def on_enter(self, ctx: TrialContext) -> None:
        self._t0 = ctx.clock.now()
        ctx.emit_on_flip("FIX_ON")

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        fixation = ctx.stimuli["fixation"]
        fixation.update(ctx.dt)
        fixation.draw()
        if ctx.regions["fixation"].contains(ctx.inputs.gaze):
            # Queued, not emitted: the acquisition is only true once the
            # frame that showed it has actually flipped.
            ctx.emit_on_flip("FIX_ACQUIRED")
            ctx.record["acquire_latency_s"] = ctx.clock.now() - self._t0
            return PhaseAction.ADVANCE
        if ctx.clock.now() - self._t0 >= self._timeout_s:
            return self._on_timeout
        return PhaseAction.CONTINUE


class HoldFixation:
    """Require gaze to stay inside the window for the whole hold duration."""

    name = "hold_fixation"

    def __init__(self, duration_s: float, on_held: Outcome, on_break: Outcome) -> None:
        self._duration_s = duration_s
        self._on_held = on_held
        self._on_break = on_break
        self._t0 = 0.0

    def on_enter(self, ctx: TrialContext) -> None:
        self._t0 = ctx.clock.now()

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        fixation = ctx.stimuli["fixation"]
        fixation.update(ctx.dt)
        fixation.draw()
        if not ctx.regions["fixation"].contains(ctx.inputs.gaze):
            return self._on_break
        if ctx.clock.now() - self._t0 >= self._duration_s:
            return self._on_held
        return PhaseAction.CONTINUE


class GazeFixationTask(Task):
    name = "gaze-fixation"
    events = EVENTS
    outcomes = OUTCOMES
    params_model = GazeFixationParams

    def conditions(self, rng: np.random.Generator) -> list[Condition]:
        return [Condition({"condition": "fixate"})]

    def build_trial(self, setup: TrialSetup) -> TrialPlan:
        params: GazeFixationParams = self.params  # type: ignore[assignment]
        hz = setup.refresh_rate_hz
        return TrialPlan(
            phases=[
                AcquireFixation(params.acquire_timeout.seconds(hz), OUTCOMES["NO_FIXATION"]),
                HoldFixation(
                    params.hold_duration.seconds(hz),
                    OUTCOMES["FIXATED"],
                    OUTCOMES["FIX_BREAK"],
                ),
            ],
            stimuli={"fixation": make_fixation(setup.display, setup.screen, params.fix_size_dva)},
            # Named regions are also what the tracker's operator display
            # draws, so the experimenter sees the same window the task tests.
            regions={
                "fixation": CircleRegion(
                    center=(0.0, 0.0), radius=setup.screen.deg2px(params.fix_window_dva)
                )
            },
        )
