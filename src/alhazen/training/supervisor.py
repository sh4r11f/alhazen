"""TrainingSupervisor: the curriculum, running.

One object holds the pieces a training session needs to keep in step — the
curriculum, the subject's persisted state, and the task whose parameters the
current stage overrides — and answers the four questions the session runner
asks:

- what should go on this trial's record (stage, ramped values)?
- what happened on that trial (feed the criteria)?
- has the subject earned a move, between trials?
- what should be saved when the session ends?

Transitions happen only between trials. A stage change mid-trial would mean a
trial run half at one difficulty and half at another, recorded as one row at
whichever difficulty happened to be current when the row was written.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from alhazen.errors import ConfigError
from alhazen.task.reward_policy import RewardPolicy
from alhazen.training.criteria import decide, metric_names
from alhazen.training.stages import Curriculum, Stage, apply_stage, ramped_values
from alhazen.training.state import TrainingState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StageChange:
    """One transition, as the runner needs to report it."""

    from_stage: str
    to_stage: str
    reason: str  # "criteria" or "manual"


class TrainingSupervisor:
    """Applies a curriculum to a task across a session."""

    def __init__(
        self,
        curriculum: Curriculum,
        state: TrainingState,
        task: Any,
        data_root: Path,
        subject: str,
        session_id: str,
    ) -> None:
        _validate_metric_names(curriculum)
        self._curriculum = curriculum
        self._state = state
        self._task = task
        self._data_root = Path(data_root)
        self._subject = subject
        self._session_id = session_id
        # The task's parameters as written, before any stage touched them.
        # Every stage's overrides are applied to THIS, never to the previous
        # stage's output — otherwise overrides would accumulate and a demoted
        # subject would not actually go back.
        self._base_params: BaseModel = task.params
        self._base_reward: RewardPolicy | None = getattr(task, "reward", None)
        # Auto-transitions can be suspended by the experimenter mid-session
        # (the hold key), for the sessions where a human wants to watch a
        # stage out rather than let a criterion end it.
        self.holding = False
        self.complete = False
        self._pending: StageChange | None = None
        self.apply_current_stage()

    # ------------------------------------------------------------------
    # Where the subject is
    # ------------------------------------------------------------------

    @property
    def stage(self) -> Stage:
        return self._curriculum.stages[self._curriculum.index_of(self._state.stage)]

    @property
    def state(self) -> TrainingState:
        return self._state

    @property
    def reward_policy(self) -> RewardPolicy | None:
        """The task's reward policy *as this stage rescaled it*.

        The session runner captures a policy once at build time and pays from
        it. Every transition rebinds ``task.reward`` to a fresh copy, so the
        runner has to re-read it here or it goes on paying the previous
        stage's scale while each row stamps the new one.
        """
        policy = getattr(self._task, "reward", None)
        return policy if isinstance(policy, RewardPolicy) else None

    @property
    def stop_when_complete(self) -> bool:
        """Whether finishing the curriculum should end the session. A
        property rather than the runner reaching through two ``getattr``
        layers into a private field it cannot be typed against."""
        return self._curriculum.stop_when_complete

    def apply_current_stage(self) -> None:
        """Rebuild the task's parameters and reward policy for this stage.

        The task object is mutated rather than rebuilt because the session
        runner holds a bound ``task.build_trial``: swapping the parameters
        underneath it is what makes the next trial come out at the new
        difficulty. This is the ONLY place that mutation happens, and it only
        ever happens between trials.
        """
        stage = self.stage
        completed = self._state.completed_in(stage.name)
        self._task.params = apply_stage(self._base_params, stage, completed)
        if self._base_reward is not None:
            # Compounding scales would make an early stage's generosity
            # multiply into a later one, so this scales the ORIGINAL policy.
            self._task.reward = self._base_reward.model_copy(
                update={"scale": self._base_reward.scale * stage.reward_scale}
            )

    def stamp(self) -> dict[str, Any]:
        """What every trial's record carries about training.

        Without this on the row, a training session cannot be analysed
        afterwards: "stage 2" alone says nothing about how hard the task was
        at trial 40 of that stage versus trial 400.
        """
        stage = self.stage
        completed = self._state.completed_in(stage.name)
        stamp: dict[str, Any] = {
            "stage": stage.name,
            "stage_completed_trials": completed,
            "reward_scale": stage.reward_scale,
        }
        for path, value in ramped_values(stage, completed).items():
            # Prefixed so a ramped parameter cannot collide with a column the
            # task already writes under the same name.
            stamp[f"ramp_{path.replace('.', '_')}"] = value
        return stamp

    # ------------------------------------------------------------------
    # What happened
    # ------------------------------------------------------------------

    def observe(self, outcome: Any, record: dict[str, Any]) -> None:
        """Feed one finished attempt to the criteria.

        PAUSED attempts are skipped: the experimenter stopping for a moment
        is not evidence about the subject.
        """
        if outcome.name == "PAUSED":
            return
        summary = {
            "outcome": outcome.name,
            "completed": bool(outcome.completed),
            "success": bool(outcome.success) if outcome.success is not None else None,
            "rt_ms": record.get("rt_ms"),
            "stage": self.stage.name,
        }
        self._state.note_attempt(summary, self.stage.criteria.window)
        # Ramps advance with completed trials, so the parameters have to be
        # rebuilt now for the NEXT trial to be built at the new value.
        if summary["completed"] and self.stage.ramps:
            self.apply_current_stage()

    def transition(self) -> StageChange | None:
        """The move this subject has earned, if any. Called between trials.

        A manual command queued during a trial is honoured first: an
        experimenter pressing the promote key has watched the subject and is
        overruling the criteria on purpose.
        """
        if self._pending is not None:
            change, self._pending = self._pending, None
            return self._commit(change)
        if self.holding:
            return None
        verdict = decide(self.stage.criteria, self._state.window)
        if verdict is None:
            return None
        return self._commit(self._change_by(1 if verdict == "promote" else -1, "criteria"))

    def request(self, direction: int) -> None:
        """Queue a manual promotion (+1) or demotion (−1) from the keyboard.

        Queued rather than applied, because this arrives mid-trial and a
        stage change mid-trial would produce a row recorded at a difficulty
        that was only true for part of it.
        """
        change = self._change_by(direction, "manual")
        if change is not None:
            self._pending = change

    def toggle_hold(self) -> bool:
        """Suspend or resume automatic transitions. Returns the new state."""
        self.holding = not self.holding
        log.info("automatic stage transitions %s", "held" if self.holding else "resumed")
        return self.holding

    def _change_by(self, direction: int, reason: str) -> StageChange | None:
        """The transition ``direction`` implies from where the subject is."""
        stages = self._curriculum.stages
        index = self._curriculum.index_of(self._state.stage)
        target = index + direction
        if target < 0:
            log.info("already at the first stage; demotion ignored")
            return None
        if target >= len(stages):
            # Past the last stage: the curriculum is finished. Whether that
            # ends the session is the curriculum's own decision.
            self.complete = True
            log.info("curriculum complete: %r was the last stage", stages[index].name)
            return None
        return StageChange(stages[index].name, stages[target].name, reason)

    def _commit(self, change: StageChange | None) -> StageChange | None:
        if change is None:
            return None
        self._state.note_transition(
            change.from_stage, change.to_stage, change.reason, self._session_id
        )
        self.apply_current_stage()
        log.info("stage %s -> %s (%s)", change.from_stage, change.to_stage, change.reason)
        return change

    # ------------------------------------------------------------------

    def restore_base(self) -> None:
        """Put the task back the way it was handed over.

        A Task instance can outlive one session — an example's tests build it
        once and run two sessions from it, and a batch script does the same.
        Because ``apply_current_stage`` mutates the task in place, a second
        session would otherwise treat the first session's *last* stage as its
        base and compound every override on top of it. Called from the
        runner's teardown, so the object a caller still holds is the object
        it passed in.
        """
        self._task.params = self._base_params
        if self._base_reward is not None:
            self._task.reward = self._base_reward

    def save(self) -> Path:
        """Persist the subject's place. Its own teardown step in the runner,
        so a failure to write it is loud and does not stop the data being
        written."""
        return self._state.save(self._data_root, self._subject)


def _validate_metric_names(curriculum: Curriculum) -> None:
    """Refuse a criterion naming a metric nobody registered.

    Checked at construction — i.e. at session build — for the same reason a
    stage's parameter typo is: the alternative is a ConfigError raised the
    first time a window fills, which on a training rig means an hour into a
    session with an animal already working.
    """
    known = metric_names()
    for stage in curriculum.stages:
        for field in ("promote_when", "demote_when"):
            for name in getattr(stage.criteria, field):
                if name not in known:
                    raise ConfigError(
                        f"stage {stage.name!r} has {field} on metric {name!r}, which is not "
                        f"registered (known: {known}) — register it with "
                        f"alhazen.training.register_metric before building the session"
                    )
