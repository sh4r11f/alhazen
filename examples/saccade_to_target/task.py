"""A saccade task built entirely from library phases.

Acquire fixation, hold it through a jittered foreperiod, a target appears and
the subject looks at it, and where their gaze lands is the measurement. Four
phases, none of them written here — this file declares vocabulary, geometry
and timing, and composes ``alhazen.task.phases``.

That is the point of the example: a whole trial state machine, which would
otherwise be four hand-written classes bolted to one experiment, is
configuration over library parts.
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


class SaccadeParams(Model):
    fix_size_dva: float = 0.3
    fix_window_dva: float = 2.0
    target_size_dva: float = 0.5
    target_window_dva: float = 3.0
    eccentricity_dva: float = 8.0
    acquire_timeout: Duration = Duration(ms=2000)
    fixation_hold: Duration = Duration(ms=400)
    fixation_jitter: Duration = Duration(ms=100)
    response_timeout: Duration = Duration(ms=800)
    landing_timeout: Duration = Duration(ms=400)
    iti: Duration = Duration(ms=300)
    paradigm: SchedulerConfig = SchedulerConfig(kind="constant", n_per_condition=2)


class SaccadeTask(Task):
    name = "saccade-to-target"
    events = EventSchema(("FIX_ON", "FIX_ACQUIRED", "STIM_ON", "SACCADE_ONSET", "LANDED"))
    outcomes = outcomes(
        # Landed in the target window: the trial measured what it exists to
        # measure, and it was correct.
        CORRECT=dict(completed=True, success=True),
        # A saccade happened and landed somewhere else. Still a measurement —
        # the endpoint IS the data — so it completes, unsuccessfully.
        MISSED_TARGET=dict(completed=True, success=False),
        # These three ended before any endpoint existed, so they re-queue.
        FIX_NOT_ACQUIRED=dict(completed=False),
        FIX_BREAK=dict(completed=False),
        NO_SACCADE=dict(completed=False),
    )
    params_model = SaccadeParams
    reward = RewardPolicy(by_outcome={"CORRECT": RewardPulses(n_pulses=2, pulse_ms=100)})

    def conditions(self, rng: np.random.Generator) -> list[Condition]:
        # Left and right targets, so a bias in one direction cannot masquerade
        # as an effect.
        return [Condition({"side": side}) for side in ("left", "right")]

    def build_trial(self, setup: TrialSetup) -> TrialPlan:
        params: SaccadeParams = self.params  # type: ignore[assignment]
        hz = setup.refresh_rate_hz
        screen = setup.screen
        sign = -1.0 if setup.condition.params["side"] == "left" else 1.0
        target_pos = (sign * screen.deg2px(params.eccentricity_dva), 0.0)

        return TrialPlan(
            phases=[
                phases.AcquireFixation(
                    timeout_s=params.acquire_timeout.seconds(hz),
                    on_timeout=self.outcomes["FIX_NOT_ACQUIRED"],
                ),
                phases.HoldFixation(
                    duration_s=params.fixation_hold.seconds(hz),
                    jitter_s=params.fixation_jitter.seconds(hz),
                    on_break=self.outcomes["FIX_BREAK"],
                ),
                phases.StimulusResponse(
                    stimulus_key="target",
                    depart_region="fixation",
                    timeout_s=params.response_timeout.seconds(hz),
                    on_timeout=self.outcomes["NO_SACCADE"],
                    response_event="SACCADE_ONSET",
                    concurrent=["fixation"],
                ),
                phases.LandingCheck(
                    region="target",
                    timeout_s=params.landing_timeout.seconds(hz),
                    on_hit=self.outcomes["CORRECT"],
                    on_miss=self.outcomes["MISSED_TARGET"],
                    stimulus_keys=["target"],
                ),
            ],
            stimuli={
                "fixation": make_fixation(setup.display, screen, params.fix_size_dva),
                "target": make_fixation(
                    setup.display, screen, params.target_size_dva, pos=target_pos
                ),
            },
            regions={
                "fixation": CircleRegion((0.0, 0.0), screen.deg2px(params.fix_window_dva)),
                "target": CircleRegion(target_pos, screen.deg2px(params.target_window_dva)),
            },
            # Both coordinates: a landing plot marks the target from the
            # pair, and half of one is not a position.
            record={
                "target_x_dva": sign * params.eccentricity_dva,
                "target_y_dva": 0.0,
            },
        )

    def score(self, record: dict) -> dict:
        """A derived measure, computed by the experiment and never by the
        engine: how far the endpoint fell short of the target."""
        endpoint = record.get("endpoint_x_dva")
        if endpoint is not None:
            record["gain"] = endpoint / record["target_x_dva"]
        return record
