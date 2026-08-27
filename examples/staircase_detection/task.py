"""A two-alternative detection task on interleaved staircases.

The human counterpart of the saccade example: a stimulus appears on one side,
the subject presses left or right, and a staircase moves the contrast toward
their threshold. Two staircases run interleaved — one starting easy, one
starting hard — because a single staircase can converge on the wrong place if
its start is far from the threshold, and disagreeing staircases are how you
find out that happened.

Everything here is declaration: the scheduler comes from the params, the
phases from the library, and the only task-specific code is one derived
measure.
"""

from __future__ import annotations

import numpy as np

from alhazen import (
    Condition,
    Duration,
    Model,
    Task,
    TrialPlan,
    TrialSetup,
    outcomes,
)
from alhazen.core.events import EventSchema
from alhazen.display.backend import DisplayBackend
from alhazen.display.screen import Screen
from alhazen.paradigms.config import SchedulerConfig, StaircaseConfig
from alhazen.stimuli.base import NullStimulus, Stimulus
from alhazen.stimuli.fixation import make_fixation
from alhazen.task import phases


class ContrastPatch:
    """A uniform disc whose luminance IS the titrated variable.

    The whole point of the staircase is that this number moves, so it has to
    reach the screen. Built the way every stimulus in the library is: degrees
    in, pixels converted once at construction, the renderer imported lazily,
    and a recording stand-in from the factory for simulated backends.
    """

    def __init__(
        self,
        display: DisplayBackend,
        screen: Screen,
        size_dva: float,
        contrast: float,
        pos: tuple[float, float],
    ) -> None:
        from psychopy import visual

        self.contrast = contrast
        # A light disc on a black field, so contrast is simply how far the
        # patch is from the background. PsychoPy's default colour space runs
        # -1 (black) to +1 (white), hence the remap.
        level = -1.0 + 2.0 * contrast
        self._stim = visual.Circle(
            display.window,
            radius=screen.deg2px(size_dva) / 2.0,
            pos=pos,
            fillColor=(level, level, level),
            lineColor=(level, level, level),
            units="pix",
        )

    def update(self, dt: float) -> None:
        return  # static within a trial

    def draw(self) -> None:
        self._stim.draw()


class RecordedPatch(NullStimulus):
    """The simulated twin: draws nothing, and remembers what it was built at.

    Carrying the contrast is what lets a headless test assert that the
    staircase's value reached the stimulus rather than only the record — an
    adaptive variable that titrates nothing is a decorative one.
    """

    def __init__(self, contrast: float) -> None:
        super().__init__("target")
        self.contrast = contrast


def make_target(
    display: DisplayBackend,
    screen: Screen,
    size_dva: float,
    contrast: float,
    pos: tuple[float, float],
) -> Stimulus:
    if display.kind == "simulated":
        return RecordedPatch(contrast)
    return ContrastPatch(display, screen, size_dva, contrast, pos)


class DetectionParams(Model):
    fix_size_dva: float = 0.3
    target_size_dva: float = 1.0
    eccentricity_dva: float = 6.0
    stimulus_duration: Duration = Duration(frames=6)
    response_timeout: Duration = Duration(ms=2000)
    feedback_duration: Duration = Duration(ms=300)
    iti: Duration = Duration(ms=400)
    paradigm: SchedulerConfig = SchedulerConfig(
        kind="staircase",
        staircase=StaircaseConfig(
            parameter="contrast",
            start=0.5,
            step=0.05,
            n_down=2,  # two right answers make it harder, one wrong makes it easier
            n_up=1,
            n_reversals=8,
            min_value=0.0,
            max_value=1.0,
            interleave_by="start",
        ),
    )


class DetectionTask(Task):
    name = "staircase-detection"
    events = EventSchema(("STIM_ON", "RESPONSE_CUE", "RESPONSE"))
    outcomes = outcomes(
        CORRECT=dict(completed=True, success=True),
        WRONG=dict(completed=True, success=False),
        # No answer at all is not a measurement of anything: re-queue it.
        NO_RESPONSE=dict(completed=False),
    )
    params_model = DetectionParams

    def conditions(self, rng: np.random.Generator) -> list[Condition]:
        # The staircase config interleaves by "start", so these two cells
        # become two independent staircases.
        return [Condition({"start": "easy"}), Condition({"start": "hard"})]

    def build_trial(self, setup: TrialSetup) -> TrialPlan:
        params: DetectionParams = self.params  # type: ignore[assignment]
        hz = setup.refresh_rate_hz
        # The side is this trial's own draw from the session rng — the thing
        # the subject has to detect, and the thing the response is scored
        # against.
        side = "left" if setup.rng.random() < 0.5 else "right"
        sign = -1.0 if side == "left" else 1.0
        pos = (sign * setup.screen.deg2px(params.eccentricity_dva), 0.0)
        correct_key = side
        # What the staircase chose for this trial. Reading it here is the
        # whole mechanism: without it the adaptive variable would move in the
        # scheduler's own bookkeeping and change nothing the subject sees.
        contrast = float(setup.condition.params["contrast"])

        return TrialPlan(
            phases=[
                phases.Feedback(  # the stimulus itself: shown for a fixed time
                    stimulus_keys=["fixation", "target"],
                    duration_s=params.stimulus_duration.seconds(hz),
                    onset_event="STIM_ON",
                ),
                phases.ResponseWindow(
                    keys={
                        correct_key: self.outcomes["CORRECT"],
                        _other(correct_key): self.outcomes["WRONG"],
                    },
                    timeout_s=params.response_timeout.seconds(hz),
                    on_timeout=self.outcomes["NO_RESPONSE"],
                    stimulus_keys=["fixation"],
                ),
            ],
            stimuli={
                "fixation": make_fixation(setup.display, setup.screen, params.fix_size_dva),
                "target": make_target(
                    setup.display, setup.screen, params.target_size_dva, contrast, pos
                ),
            },
            record={"side": side, "correct_key": correct_key, "contrast": contrast},
        )


def _other(key: str) -> str:
    return "right" if key == "left" else "left"
