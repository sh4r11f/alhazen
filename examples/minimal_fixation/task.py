"""The smallest complete alhazen experiment: show a fixation point, hold it
on screen for a configured duration, end the trial.

This file is the template every experiment package follows — one Task
subclass declaring its params, events and outcomes, and building its trial.
There is no hardware anywhere in sight: the same code runs against the
simulated display on a laptop and, with a psychopy rig config, against a real
window.
"""

from __future__ import annotations

from alhazen import (
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


class FixationParams(Model):
    fixation_duration: Duration = Duration(ms=200)
    fix_size_dva: float = 0.3
    iti: Duration = Duration(ms=50)
    # How many trials is the paradigm's business, not a second field that
    # could disagree with it: this one serves the single condition three
    # times.
    paradigm: SchedulerConfig = SchedulerConfig(n_per_condition=3)


class HoldFixationPeriod:
    """Draw the fixation point every frame; end the trial COMPLETED once the
    duration has elapsed on the session clock.

    Written out rather than taken from ``alhazen.task.phases`` because this
    example exists to show what a phase *is*: an object with a name, an
    on_enter, and an on_frame that touches nothing but the context.
    """

    name = "hold_fixation_period"

    def __init__(self, duration_s: float, completed: Outcome) -> None:
        self._duration_s = duration_s
        self._completed = completed
        self._t0 = 0.0

    def on_enter(self, ctx: TrialContext) -> None:
        self._t0 = ctx.clock.now()
        ctx.emit_on_flip("FIX_ON")

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        fixation = ctx.stimuli["fixation"]
        fixation.update(ctx.dt)
        fixation.draw()
        if ctx.clock.now() - self._t0 >= self._duration_s:
            return self._completed
        return PhaseAction.CONTINUE


class MinimalFixationTask(Task):
    name = "minimal-fixation"
    events = EventSchema(("FIX_ON",))
    outcomes = outcomes(COMPLETED=dict(completed=True, success=True))
    params_model = FixationParams

    def conditions(self, rng) -> list[Condition]:
        # One condition, presented n_trials times — the scheduler config in
        # the params says how many.
        return [Condition({"condition": "fixation"})]

    def build_trial(self, setup: TrialSetup) -> TrialPlan:
        params: FixationParams = self.params  # type: ignore[assignment]
        duration_s = params.fixation_duration.seconds(setup.refresh_rate_hz)
        return TrialPlan(
            phases=[HoldFixationPeriod(duration_s, self.outcomes["COMPLETED"])],
            stimuli={"fixation": make_fixation(setup.display, setup.screen, params.fix_size_dva)},
        )
