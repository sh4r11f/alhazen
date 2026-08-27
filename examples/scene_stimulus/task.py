"""A fixation trial whose stimulus is a scene designed in illusion-studio.

The scene is a drifting Gabor — a grating under a Gaussian window, its phase
advancing with time. Nothing about it is written here: it is
``drifting_gabor.json``, designed in the studio, and this file only says which
parameters this experiment varies and how long to show it for.

That is the point of the scene format. Changing what the stimulus looks like
is editing a JSON file (or opening the studio); changing what the *experiment*
does is editing this one.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from alhazen import (
    CircleRegion,
    Condition,
    Duration,
    Model,
    Task,
    TrialPlan,
    TrialSetup,
    outcomes,
)
from alhazen.core.events import EventSchema
from alhazen.scenes import SceneStimulus, load_scene
from alhazen.stimuli.fixation import make_fixation
from alhazen.task import phases

HERE = Path(__file__).parent
SCENE_PATH = HERE / "drifting_gabor.json"


class SceneParams(Model):
    fix_size_dva: float = 0.3
    fix_window_dva: float = 3.0
    # What the scene's own expressions read. The names match the scene's
    # `params.*` references, and a typo in either is caught the first time a
    # frame is rendered rather than silently drawing nothing.
    contrast: float = 0.8
    drift_hz: float = 2.0
    spatial_freq: float = 0.04
    orientations_deg: list[float] = [0.0, 45.0, 90.0, 135.0]
    stimulus_duration: Duration = Duration(ms=1000)
    iti: Duration = Duration(ms=500)


class SceneTask(Task):
    name = "scene-gabor"
    # FIX_ACQUIRED is AcquireFixation's own event: a phase from the library
    # still emits through the task's schema, so the task has to declare
    # everything the phases it composes will emit.
    events = EventSchema(("FIX_ON", "FIX_ACQUIRED", "STIM_ON"))
    outcomes = outcomes(
        VIEWED=dict(completed=True, success=True),
        FIX_BREAK=dict(completed=False),
    )
    params_model = SceneParams

    def conditions(self, rng: np.random.Generator) -> list[Condition]:
        params: SceneParams = self.params  # type: ignore[assignment]
        return [Condition({"orientation_deg": angle}) for angle in params.orientations_deg]

    def build_trial(self, setup: TrialSetup) -> TrialPlan:
        params: SceneParams = self.params  # type: ignore[assignment]
        hz = setup.refresh_rate_hz
        # Loaded per trial rather than cached: a scene is a file an
        # experimenter may be editing between runs, and a stale copy in
        # memory is a stimulus nobody can explain afterwards.
        scene = load_scene(SCENE_PATH)
        stimulus = SceneStimulus(
            setup.display,
            setup.screen,
            scene,
            params={
                "contrast": params.contrast,
                "driftHz": params.drift_hz,
                "spatialFreq": params.spatial_freq,
                # Degrees here, radians in the scene: the conversion belongs
                # where the two vocabularies meet, which is here.
                "orientation": np.radians(setup.condition.params["orientation_deg"]),
            },
        )

        return TrialPlan(
            phases=[
                phases.AcquireFixation(
                    timeout_s=Duration(ms=2000).seconds(hz),
                    on_timeout=self.outcomes["FIX_BREAK"],
                ),
                phases.Feedback(
                    stimulus_keys=["scene", "fixation"],
                    duration_s=params.stimulus_duration.seconds(hz),
                    onset_event="STIM_ON",
                    then=self.outcomes["VIEWED"],
                ),
            ],
            stimuli={
                "fixation": make_fixation(setup.display, setup.screen, params.fix_size_dva),
                "scene": stimulus,
            },
            regions={
                "fixation": CircleRegion((0.0, 0.0), setup.screen.deg2px(params.fix_window_dva))
            },
            record={"orientation_deg": setup.condition.params["orientation_deg"]},
        )
