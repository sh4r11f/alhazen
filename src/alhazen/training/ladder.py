"""Training ladders: the stages a monkey climbs to an experiment's final task.

A monkey does not sit down at the experiment. It is shaped toward it through
a ladder of smaller tasks — hold the fixation point; saccade to a single dot;
follow it a little; follow it all the way; follow it among the other dots —
each paid on its own success, until the last rung is the experiment's own
trial. This module is that ladder as data.

A ladder is a YAML file an experiment ships (``configs/training-<name>.yaml``)
and registers in its ``run.py`` beside ``PARAMETERS``::

    LADDERS = {"Pursuit (monkey)": HERE / "configs" / "training-pursuit.yaml"}

and hands to ``run_experiment(..., ladders=LADDERS)``. Each stage names the
task it runs (one of run.py's ``TASKS``), the params file it starts from,
dotted-path overrides on those params (re-validated through the task's own
params model, as a curriculum's are — ``training.stages.apply_stage``), the
ONE outcome that is the stage's success and what that success pays. A stage
may declare a criterion (``StageCriteria``) for moving on; it is only ever
shown to the operator as a recommendation. **Which stage runs is the
operator's choice, every session** (``--mode training --stage <id>``): nothing
here promotes a subject, and nothing remembers a stage for them.

What a stage is allowed to be, enforced when it is resolved
(`resolve_stage`), before anything opens:

- a monkey's: the stage's params must declare ``subject_kind: monkey``. A
  human session never pays and never trains; a stage resolving to a human's
  params is refused, so training mode refuses a human outright;
- paid on its success alone: the stage's reward policy is rebuilt to pay
  exactly ``success`` (and the device-fault reward, ``on_fault``), whatever
  the params file's own block paid. An intermediate stage that also paid,
  say, the final task's success would train the wrong thing;
- a success the task can produce: ``success`` must be one of the task's
  outcomes, and a completed one.

This is not ``Curriculum`` (training/stages.py), which moves a subject between
stages automatically within one task. A ladder's stages may be different
tasks, and the move is a person's decision.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import model_validator

from alhazen.config.loader import load_model
from alhazen.config.models import Model, RewardPulses
from alhazen.errors import ConfigError
from alhazen.task.reward_policy import RewardPolicy
from alhazen.task.subject_kind import SubjectKind, subject_kind_of
from alhazen.training.stages import Stage, StageCriteria, apply_stage

# A ladder's name and a stage's id are folder names (the training data root
# is ``<data_root>-training/<ladder>/<stage>/``) and command-line words, so
# they are kept to what is safe as both on every OS.
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
SLUG_RULE = "lowercase letters, digits and hyphens, starting with a letter or digit, at most 48"

# What training data roots are called, beside the rig's own data root: a
# sibling, like the rehearsal root (modes/rehearsal.py), so an analysis that
# walks ``data_root`` never finds a training session, and a file listing
# shows the two apart. A rehearsal of a stage (test or simulate mode with
# ``--stage``) goes to the training root's own rehearsal sibling.
TRAINING_SUFFIX = "-training"


def _slug(value: str, what: str) -> str:
    if not SLUG.match(value):
        raise ValueError(f"{what} {value!r} must be {SLUG_RULE} characters")
    return value


class StageReward(Model):
    """What a stage pays.

    ``success`` is the delivery for the stage's success outcome; None takes
    the params file's own entry for that outcome, else its first entry (the
    final task's delivery), so a ladder need not repeat the pump settings.
    ``on_fault`` is what a trial a device cut short pays; left out, the
    params file's own ``on_fault`` stands. ``n_pulses: 0`` pays nothing for
    either.
    """

    success: RewardPulses | None = None
    on_fault: RewardPulses | None = None


class LadderStage(Model):
    """One rung: a task variant, what it pays, and when it might be done."""

    id: str
    title: str
    description: str = ""
    # One of run.py's TASKS, or a task class by import path,
    # "package.module:ClassName" (a training task that is not one of the
    # experiment's own and so stays off its menus); None takes the ladder's.
    task: str | None = None
    # A params file, relative to the ladder file's folder; None takes the
    # ladder's, else the task's own default file.
    params: str | None = None
    # Dotted path -> value, over the params file (training.stages.apply_stage).
    overrides: dict[str, Any] = {}
    # The one outcome that is this stage's success, and the only one it pays.
    success: str
    reward: StageReward = StageReward()
    # Optional, and only ever a recommendation shown to the operator.
    criterion: StageCriteria | None = None

    @model_validator(mode="after")
    def _valid(self) -> LadderStage:
        _slug(self.id, "stage id")
        if not self.title.strip():
            raise ValueError(f"stage {self.id!r} needs a title — the operator chooses by it")
        if not self.success.strip():
            raise ValueError(f"stage {self.id!r} needs its success outcome")
        if self.criterion is not None and not (
            self.criterion.promote_when or self.criterion.demote_when
        ):
            raise ValueError(
                f"stage {self.id!r} declares a criterion with no promote_when or demote_when; "
                f"leave criterion out, or say what it judges"
            )
        return self


class Ladder(Model):
    """An ordered list of stages. Ids are unique; order is the progression."""

    name: str
    title: str
    description: str = ""
    task: str | None = None
    params: str | None = None
    stages: list[LadderStage]

    @model_validator(mode="after")
    def _valid(self) -> Ladder:
        _slug(self.name, "ladder name")
        if not self.stages:
            raise ValueError(f"ladder {self.name!r} has no stages")
        ids = [stage.id for stage in self.stages]
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            raise ValueError(f"ladder {self.name!r} repeats stage id(s) {repeated}")
        missing = [stage.id for stage in self.stages if (stage.task or self.task) is None]
        if missing:
            raise ValueError(
                f"ladder {self.name!r}: stage(s) {missing} name no task, and the ladder names "
                f"none for them to take"
            )
        return self

    def stage(self, stage_id: str) -> LadderStage:
        """The stage with this id, or a ConfigError listing the ids there are."""
        for stage in self.stages:
            if stage.id == stage_id:
                return stage
        raise ConfigError(
            f"ladder {self.name!r} has no stage {stage_id!r} "
            f"(its stages, in order: {', '.join(s.id for s in self.stages)})"
        )

    def index_of(self, stage_id: str) -> int:
        return [stage.id for stage in self.stages].index(self.stage(stage_id).id)


def load_ladder(path: str | Path) -> Ladder:
    """One ladder file, validated; a ConfigError names the file otherwise."""
    return load_model(path, Ladder)


def find_ladder(spec: str, ladders: Mapping[str, Path | str] | None) -> tuple[str | None, Path]:
    """The ladder ``--ladder`` names: run.py's label for it, its file.

    ``spec`` is one of the labels run.py's ``LADDERS`` gives, a ladder's own
    ``name`` (read from the files ``LADDERS`` lists), or a path to a ladder
    file. An empty ``spec`` means the one ladder run.py registers, refused
    when it registers several or none.
    """
    registered = {label: Path(path) for label, path in (ladders or {}).items()}
    if not spec:
        if len(registered) == 1:
            label, path = next(iter(registered.items()))
            return label, path
        if not registered:
            raise ConfigError(
                "training needs a ladder: this experiment's run.py registers none "
                "(LADDERS), so name a ladder file with --ladder"
            )
        raise ConfigError(
            f"this experiment registers several ladders; name one with --ladder "
            f"({', '.join(registered)})"
        )
    if spec in registered:
        return spec, registered[spec]
    unreadable: list[str] = []
    for label, path in registered.items():
        try:
            name = load_ladder(path).name
        except ConfigError as error:
            # Said in the refusal below if nothing else matches: the ladder
            # asked for may be the one that cannot be read.
            unreadable.append(f"{label}: {error}")
            name = None
        if name == spec:
            return label, path
    candidate = Path(spec)
    if candidate.suffix in {".yaml", ".yml"} and candidate.is_file():
        return None, candidate
    known = ", ".join(registered) or "none registered"
    also = f"; unreadable: {'; '.join(unreadable)}" if unreadable else ""
    raise ConfigError(
        f"--ladder {spec!r} is not a registered ladder ({known}) or a ladder file{also}"
    )


@dataclass(frozen=True)
class ResolvedStage:
    """A stage made runnable: the task, its params, and what to record."""

    ladder: Ladder
    ladder_file: Path
    ladder_label: str | None
    stage: LadderStage
    task_name: str
    task_class: Any
    params_file: Path | None
    params: Any

    @property
    def index(self) -> int:
        return self.ladder.index_of(self.stage.id)

    def record(self) -> dict[str, Any]:
        """What session.json says about the stage (``training``)."""
        policy = getattr(self.params, "reward", None)
        return {
            "ladder": self.ladder.name,
            "ladder_title": self.ladder.title,
            "ladder_label": self.ladder_label,
            "ladder_file": self.ladder_file.name,
            "stage": self.stage.id,
            "stage_title": self.stage.title,
            "stage_number": self.index + 1,
            "stage_count": len(self.ladder.stages),
            "task": self.task_name,
            "params_file": self.params_file.name if self.params_file is not None else None,
            "overrides": dict(self.stage.overrides),
            "success": self.stage.success,
            "reward": policy.model_dump(mode="json") if policy is not None else None,
            "criterion": (
                self.stage.criterion.model_dump(mode="json")
                if self.stage.criterion is not None
                else None
            ),
        }

    def describe(self) -> str:
        """One line for the setup notes printed before trial one."""
        return (
            f"training: ladder {self.ladder.name} — stage {self.index + 1} of "
            f"{len(self.ladder.stages)}, {self.stage.id} ({self.stage.title}); pays "
            f"{self.stage.success} only"
        )


def training_root(data_root: Path | str, ladder: str, stage: str, *, rehearsal: bool) -> Path:
    """Where one stage's sessions are filed: never the rig's own data root.

    ``<data_root>-training/<ladder>/<stage>/`` for training mode, and
    ``<data_root>-training-rehearsal/<ladder>/<stage>/`` for a rehearsal of
    the stage (test or simulate mode). Inside, the layout is the experiment's
    (``v<version>/sub-.../ses-.../run-...``), so every reader of a run folder
    reads a training run.
    """
    root = Path(data_root)
    suffix = TRAINING_SUFFIX + ("-rehearsal" if rehearsal else "")
    return (
        root.parent
        / f"{root.name}{suffix}"
        / _slug(ladder, "ladder name")
        / _slug(stage, "stage id")
    )


def _stage_reward(stage: LadderStage, base: RewardPolicy | None, where: str) -> RewardPolicy:
    """The stage's policy: its success alone, and the device-fault reward."""
    pulses = stage.reward.success
    if pulses is None and base is not None:
        pulses = base.by_outcome.get(stage.success) or next(iter(base.by_outcome.values()), None)
    if pulses is None:
        raise ConfigError(
            f"stage {stage.id!r} says nothing about what {stage.success} pays and {where} has "
            f"no reward entry to take it from: give the stage reward.success"
        )
    if "on_fault" in stage.reward.model_fields_set:
        on_fault = stage.reward.on_fault
    else:
        on_fault = base.on_fault if base is not None else None
    return RewardPolicy(
        by_outcome={stage.success: pulses},
        on_fault=on_fault,
        scale=base.scale if base is not None else 1.0,
    )


def import_task(spec: str, where: str) -> Any:
    """The task class an import path names: ``package.module:ClassName``."""
    import importlib

    from alhazen.task.task import Task

    module_name, _, attribute = spec.partition(":")
    try:
        found = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as error:
        raise ConfigError(
            f"{where} runs task {spec!r}, which cannot be imported: {error}"
        ) from error
    if not (isinstance(found, type) and issubclass(found, Task)):
        raise ConfigError(f"{where} runs task {spec!r}, which is not an alhazen Task class")
    return found


def resolve_stage(
    ladder_file: Path | str,
    stage_id: str,
    tasks: Mapping[str, tuple[Any, Path | str | None]],
    *,
    ladder_label: str | None = None,
) -> ResolvedStage:
    """Make ``stage_id`` of the ladder in ``ladder_file`` runnable.

    ``tasks`` is run.py's TASKS table (name -> (TaskClass, default params
    file or None)). Every refusal is a ConfigError naming the ladder file and
    the stage, raised before a window, a device or a folder exists.
    """
    from alhazen.config.loader import load_params

    ladder_file = Path(ladder_file)
    ladder = load_ladder(ladder_file)
    stage = ladder.stage(stage_id)
    where = f"stage {stage.id!r} of {ladder_file}"
    spec = str(stage.task or ladder.task)
    if ":" in spec:
        task_class, default_params = import_task(spec, where), None
        task_name = str(getattr(task_class, "name", task_class.__name__))
    elif spec in tasks:
        task_name = spec
        task_class, default_params = tasks[spec]
    else:
        raise ConfigError(
            f"{where} runs task {spec!r}, which is not one of this experiment's tasks "
            f"({', '.join(tasks)}) nor an import path (package.module:TaskClass)"
        )
    named = stage.params or ladder.params
    params_file: Path | None
    if named is not None:
        params_file = (ladder_file.parent / named).resolve()
        if not params_file.is_file():
            raise ConfigError(f"{where} starts from {named}, which is not a file beside the ladder")
    elif default_params is not None:
        params_file = Path(default_params)
    else:
        params_file = None
    model = task_class.params_model
    if params_file is not None:
        base_params = load_params(params_file, model)
    else:
        base_params = model()
    # The overrides, through the same path a curriculum's take.
    params = apply_stage(base_params, Stage(name=stage.id, overrides=stage.overrides))
    kind = subject_kind_of(params)
    if kind is not SubjectKind.MONKEY:
        said = kind.value if kind is not None else "nothing"
        raise ConfigError(
            f"{where}: training is for a monkey, and this stage's params declare subject_kind "
            f"{said}. Start the stage from a monkey's params file (subject_kind: monkey); a "
            f"human session never trains and never pays"
        )
    outcomes = task_class.outcomes
    if stage.success not in outcomes.names:
        raise ConfigError(
            f"{where} pays {stage.success!r}, which task {task_name} does not declare "
            f"(its outcomes: {', '.join(sorted(outcomes.names))})"
        )
    if not outcomes[stage.success].completed:
        raise ConfigError(
            f"{where} pays {stage.success!r}, which is not a completed outcome of {task_name}: "
            f"a stage's success must be a trial the subject finished"
        )
    policy = _stage_reward(stage, getattr(params, "reward", None), where)
    try:
        params = type(params).model_validate({**params.model_dump(), "reward": policy.model_dump()})
    except Exception as error:  # pydantic's ValidationError, or a model's own
        raise ConfigError(
            f"{where}: its reward is refused by the task's params:\n{error}"
        ) from error
    return ResolvedStage(
        ladder=ladder,
        ladder_file=ladder_file,
        ladder_label=ladder_label,
        stage=stage,
        task_name=task_name,
        task_class=task_class,
        params_file=params_file,
        params=params,
    )
