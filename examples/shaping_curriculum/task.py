"""A fixation task shaped across three stages.

The task itself is the simplest gaze-contingent thing there is: look at the
point, keep looking at it. What makes it a shaping demonstration is the
curriculum beside it (``curriculum.yaml``): stage 1 accepts a very loose gaze
window and a brief hold, stage 3 is the real task, and stage 2 ramps the
window down between them.

Nothing in this file knows any of that. The task declares its parameters, and
the curriculum overrides them — which is the point: a shaping protocol is a
config file an experimenter can read and change, not code.
"""

from __future__ import annotations

import numpy as np

from alhazen import (
    CircleRegion,
    Condition,
    Duration,
    Model,
    RewardPolicy,
    RewardPulses,
    Task,
    TrialPlan,
    TrialSetup,
    outcomes,
)
from alhazen.core.events import EventSchema
from alhazen.paradigms.config import SchedulerConfig
from alhazen.stimuli.fixation import make_fixation
from alhazen.task import phases


class ShapingParams(Model):
    fix_size_dva: float = 0.3
    # The two parameters the curriculum shapes: how big the window is, and
    # how long the subject has to hold it.
    fix_window_dva: float = 6.0
    hold_duration: Duration = Duration(ms=200)
    acquire_timeout: Duration = Duration(ms=2000)
    iti: Duration = Duration(ms=100)
    paradigm: SchedulerConfig = SchedulerConfig(n_per_condition=1000)


class ShapingTask(Task):
    name = "shaping-fixation"
    events = EventSchema(("FIX_ON", "FIX_ACQUIRED"))
    outcomes = outcomes(
        FIXATED=dict(completed=True, success=True),
        # Looked, then looked away: nothing measured, so it is re-served.
        FIX_BREAK=dict(completed=False),
        # Never looked at all. Completed, and unsuccessful: "this subject is
        # not engaging" IS the measurement a shaping protocol needs, and a
        # non-completed outcome here would re-serve forever against an animal
        # that has stopped working.
        NO_FIXATION=dict(completed=True, success=False),
    )
    params_model = ShapingParams
    # Doubled or halved by a stage's reward_scale: early shaping pays more
    # per trial than the final task does.
    reward = RewardPolicy(by_outcome={"FIXATED": RewardPulses(n_pulses=2, pulse_ms=100)})

    def conditions(self, rng: np.random.Generator) -> list[Condition]:
        return [Condition({"condition": "fixate"})]

    def build_trial(self, setup: TrialSetup) -> TrialPlan:
        params: ShapingParams = self.params  # type: ignore[assignment]
        hz = setup.refresh_rate_hz
        return TrialPlan(
            phases=[
                phases.AcquireFixation(
                    timeout_s=params.acquire_timeout.seconds(hz),
                    on_timeout=self.outcomes["NO_FIXATION"],
                ),
                phases.HoldFixation(
                    duration_s=params.hold_duration.seconds(hz),
                    on_break=self.outcomes["FIX_BREAK"],
                ),
                # HoldFixation advances rather than ending the trial, because
                # most tasks have something after the hold. Here there is
                # nothing, so a zero-length Blank supplies the outcome — that
                # is what its `then` parameter is for.
                phases.Blank(0.0, then=self.outcomes["FIXATED"]),
            ],
            stimuli={"fixation": make_fixation(setup.display, setup.screen, params.fix_size_dva)},
            regions={
                "fixation": CircleRegion((0.0, 0.0), setup.screen.deg2px(params.fix_window_dva))
            },
        )
