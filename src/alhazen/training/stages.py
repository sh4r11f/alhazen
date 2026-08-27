"""Curricula: named stages that override a task's parameters.

Shaping an animal means running the same task at a difficulty it can actually
do, and moving that difficulty as it learns. Doing that by editing config
between sessions loses the history; doing it in task code buries the schedule
in a place nobody can read. So a curriculum is data: an ordered list of
stages, each one a set of overrides on the task's own parameters, plus the
criteria for moving on.

The overrides are dotted paths into the params model — ``"timing.hold.ms"`` —
and are re-validated through that model, so a stage that asks for something
the task cannot express fails at session build naming the stage and the path,
rather than producing a session that runs at a difficulty nobody chose.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator

from alhazen.config.models import Model
from alhazen.errors import ConfigError


class Ramp(Model):
    """A parameter that slides from ``start`` to ``end`` as the subject works.

    Progress is counted in *completed* trials within the stage, not in
    attempts or in minutes: a subject who spends twenty minutes not engaging
    has not earned a harder task. The value moves linearly and stops at
    ``end`` — a ramp never overshoots into a difficulty the stage never
    declared.
    """

    param: str  # dotted path into the task params, e.g. "tolerances.fixation_dva"
    start: float
    end: float
    over_completed_trials: int

    @model_validator(mode="after")
    def _valid(self) -> Ramp:
        if self.over_completed_trials < 1:
            raise ValueError("over_completed_trials must be >= 1")
        if not self.param:
            raise ValueError("a ramp needs the parameter path it ramps")
        return self

    def value_at(self, completed_trials: int) -> float:
        """Where this ramp stands after ``completed_trials`` in the stage."""
        progress = min(max(completed_trials / self.over_completed_trials, 0.0), 1.0)
        return self.start + (self.end - self.start) * progress


class StageCriteria(Model):
    """When to move on, judged over a sliding window of recent trials.

    ``window`` is how many recent attempts are considered and ``min_trials``
    how full that window must be before any decision is taken — a criterion
    evaluated on three trials would promote on noise. Every ``promote_when``
    metric must clear its threshold; any single ``demote_when`` metric falling
    to its threshold sends the subject back.
    """

    window: int = 100
    min_trials: int = 50
    promote_when: dict[str, float] = {}  # metric name -> value it must reach (>=)
    demote_when: dict[str, float] = {}  # metric name -> value that sends it back (<=)

    @model_validator(mode="after")
    def _valid(self) -> StageCriteria:
        if self.window < 1:
            raise ValueError("window must be >= 1")
        if self.min_trials < 1:
            raise ValueError("min_trials must be >= 1")
        if self.min_trials > self.window:
            raise ValueError(
                f"min_trials ({self.min_trials}) cannot exceed window ({self.window}) — "
                f"the window would never be full enough to decide anything"
            )
        return self


class Stage(Model):
    """One step of a curriculum."""

    name: str
    # Dotted path -> value, applied to the task's params for this stage.
    overrides: dict[str, Any] = {}
    # Multiplies the task's reward policy while this stage is current: early
    # stages usually pay more per trial than the final task does.
    reward_scale: float = 1.0
    ramps: list[Ramp] = []
    criteria: StageCriteria = StageCriteria()

    @model_validator(mode="after")
    def _valid(self) -> Stage:
        if not self.name:
            raise ValueError("a stage needs a name — it is recorded on every trial")
        if self.reward_scale < 0:
            raise ValueError("reward_scale must be >= 0")
        ramped = [ramp.param for ramp in self.ramps]
        clash = sorted(set(ramped) & set(self.overrides))
        if clash:
            # A path that is both fixed and ramped has two answers; whichever
            # won would be an implementation detail rather than a decision.
            raise ValueError(
                f"stage {self.name!r} both overrides and ramps {clash} — a parameter can "
                f"be set or ramped, not both"
            )
        return self


class Curriculum(Model):
    """An ordered list of stages. Names are unique; order is the progression."""

    stages: list[Stage]
    # Whether promotion past the last stage ends the session. Off by default:
    # a subject that has finished its curriculum usually keeps working at the
    # final stage until the experimenter stops.
    stop_when_complete: bool = False

    @model_validator(mode="after")
    def _valid(self) -> Curriculum:
        if not self.stages:
            raise ValueError("a curriculum needs at least one stage")
        names = [stage.name for stage in self.stages]
        if len(set(names)) != len(names):
            duplicates = sorted({name for name in names if names.count(name) > 1})
            raise ValueError(f"stage names must be unique; repeated: {duplicates}")
        return self

    def index_of(self, name: str) -> int:
        """Position of a named stage, or a loud error listing what exists —
        a persisted state naming a stage that has since been renamed must not
        silently restart the subject at stage 0."""
        for index, stage in enumerate(self.stages):
            if stage.name == name:
                return index
        raise ConfigError(
            f"stage {name!r} is not in this curriculum (stages: "
            f"{[stage.name for stage in self.stages]})"
        )


def apply_stage(params: BaseModel, stage: Stage, completed_in_stage: int = 0) -> BaseModel:
    """The task's parameters as this stage wants them.

    Overrides first, then ramps, then one re-validation through the params
    model itself — so a stage cannot produce a parameter set the task would
    have rejected had it come from a file. The error names the stage and the
    path, because "validation failed" is useless when a curriculum has six
    stages and forty overrides between them.
    """
    data = params.model_dump()
    for path, value in stage.overrides.items():
        _set_path(data, path, value, stage.name)
    for ramp in stage.ramps:
        _set_path(data, ramp.param, ramp.value_at(completed_in_stage), stage.name)
    try:
        return type(params).model_validate(data)
    except Exception as error:  # pydantic ValidationError, or a model's own
        paths = sorted({*stage.overrides, *(ramp.param for ramp in stage.ramps)})
        raise ConfigError(
            f"stage {stage.name!r} produced parameters the task rejects (it sets {paths}):\n{error}"
        ) from error


def ramped_values(stage: Stage, completed_in_stage: int) -> dict[str, float]:
    """This stage's ramp values right now, for stamping on the trial record.

    Without these on the row, a training session's data cannot be analysed
    afterwards: "stage 2" says nothing about how hard the task was at trial
    40 versus trial 400 of that stage.
    """
    return {ramp.param: ramp.value_at(completed_in_stage) for ramp in stage.ramps}


def _set_path(data: dict[str, Any], path: str, value: Any, stage_name: str) -> None:
    """Write a dotted path into a nested dict, refusing to invent structure.

    A path that does not already exist in the params dump is a typo — the
    model would reject an unknown key anyway (``extra="forbid"``), but the
    error there names the field without saying which stage asked for it.
    """
    parts = path.split(".")
    cursor: Any = data
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            raise ConfigError(
                f"stage {stage_name!r} overrides {path!r}, but {part!r} is not a "
                f"parameter of this task"
            )
        cursor = cursor[part]
    leaf = parts[-1]
    if not isinstance(cursor, dict) or leaf not in cursor:
        raise ConfigError(
            f"stage {stage_name!r} overrides {path!r}, but {leaf!r} is not a parameter of this task"
        )
    cursor[leaf] = value
