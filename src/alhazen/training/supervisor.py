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

from alhazen.core.trial import lost_to_fault
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


@dataclass(frozen=True)
class _Completion:
    """A promotion past the last stage: the curriculum is finished.

    Its own result rather than a None from ``_change_by``, so that asking
    what a move would be has no side effect. Only ``_commit`` acts on it —
    which means between trials, like every other move, and not the moment a
    promote key is pressed mid-trial.
    """

    last_stage: str
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
        # Set only by `_finish`, i.e. when a completion is committed between
        # trials; the runner reads it after each transition.
        self.complete = False
        self._pending: StageChange | _Completion | None = None
        # For the one warning about an RT criterion with no RT to judge
        # (`_check_rt_is_recorded`): completed trials seen this session,
        # whether any of them carried the RT, and whether we have said so.
        self._completed_seen = 0
        self._rt_seen = False
        self._rt_warned = False
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

        Two kinds of attempt are left out entirely — not counted in any
        metric, not in the window's size, not toward ``min_trials``, not
        toward a ramp:

        - PAUSED: the experimenter stopping for a moment is not evidence
          about the subject.
        - An attempt lost to a system fault (``core.trial.lost_to_fault``):
          frame QA recycled it for dropped frames, or the eye tracker stopped
          recording before its outcome was decided. The rig failed, not the
          subject. Counted, either would pull ``completed_rate`` down — a
          subject demoted because the display dropped frames — and a window
          filled with them could decide a promotion on fewer real trials than
          ``min_trials`` promises.

        A trial whose tracker stopped only during its closing phase is
        counted like any other: its outcome is the subject's own, and it
        stands (core/engine.py).
        """
        if outcome.name == "PAUSED":
            return
        fault = lost_to_fault(outcome.name, record)
        if fault is not None:
            # DEBUG: the runner has already said, at WARNING, that this trial
            # was lost to a fault and is not counted against the subject.
            log.debug(
                "stage %r: %s trial left out of the criteria window (lost to %s)",
                self.stage.name,
                outcome.name,
                fault,
            )
            return
        # The RT is read from the field the curriculum names (the phases let
        # a task rename it) but always kept as "rt_ms": that is what
        # mean_rt_ms reads, and what every state file already saved holds.
        rt = record.get(self._curriculum.rt_key)
        summary = {
            "outcome": outcome.name,
            "completed": bool(outcome.completed),
            "success": bool(outcome.success) if outcome.success is not None else None,
            "rt_ms": rt,
            "stage": self.stage.name,
        }
        # The fields an experiment's own metric asked for, under their own
        # names. Absent from this record -> None, as rt_ms is.
        for field in self._curriculum.record_fields:
            summary[field] = _storable(field, record.get(field))
        self._state.note_attempt(summary, self.stage.criteria.window)
        if summary["completed"]:
            self._check_rt_is_recorded(rt)
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
            move, self._pending = self._pending, None
            return self._commit(move)
        if self.holding:
            return None
        criteria = self.stage.criteria
        if self.complete and self._at_last_stage():
            # Finished, and still at the last stage: there is nothing left to
            # promote to. The window is not cleared by a completion (no stage
            # changed), so the promotion that finished the curriculum would
            # otherwise be re-decided — and re-announced — on every later
            # trial. Demotion is still judged: a finished subject that falls
            # apart should still be sent back.
            criteria = criteria.model_copy(update={"promote_when": {}})
        verdict = decide(criteria, self._state.window)
        if verdict is None:
            return None
        return self._commit(self._change_by(1 if verdict == "promote" else -1, "criteria"))

    def request(self, direction: int) -> None:
        """Queue a manual promotion (+1) or demotion (−1) from the keyboard.

        Queued rather than applied, because this arrives mid-trial and a
        stage change mid-trial would produce a row recorded at a difficulty
        that was only true for part of it.
        """
        move = self._change_by(direction, "manual")
        if move is not None:
            self._pending = move

    def toggle_hold(self) -> bool:
        """Suspend or resume automatic transitions. Returns the new state."""
        self.holding = not self.holding
        log.info("automatic stage transitions %s", "held" if self.holding else "resumed")
        return self.holding

    def _at_last_stage(self) -> bool:
        return self._curriculum.index_of(self._state.stage) == len(self._curriculum.stages) - 1

    def _change_by(self, direction: int, reason: str) -> StageChange | _Completion | None:
        """The move ``direction`` implies from where the subject is.

        A query: it changes nothing. Past the last stage it answers
        ``_Completion``, which only ``_commit`` acts on.
        """
        stages = self._curriculum.stages
        index = self._curriculum.index_of(self._state.stage)
        target = index + direction
        if target < 0:
            log.info("already at the first stage; demotion ignored")
            return None
        if target >= len(stages):
            return _Completion(stages[index].name, reason)
        return StageChange(stages[index].name, stages[target].name, reason)

    def _commit(self, move: StageChange | _Completion | None) -> StageChange | None:
        """Carry out a move between trials. A completion changes no stage,
        so it returns None: the runner has no STAGE_CHANGED to emit, and
        reads ``complete`` instead."""
        if move is None:
            return None
        if isinstance(move, _Completion):
            self._finish(move)
            return None
        change = move
        self._state.note_transition(
            change.from_stage, change.to_stage, change.reason, self._session_id
        )
        self.apply_current_stage()
        log.info("stage %s -> %s (%s)", change.from_stage, change.to_stage, change.reason)
        return change

    def _finish(self, completion: _Completion) -> None:
        """Mark the curriculum finished, and say so once.

        Whether that ends the session is the curriculum's own decision
        (``stop_when_complete``, acted on by the runner). A second completion
        — the promote key pressed again at the end — changes nothing and is
        only acknowledged.
        """
        if self.complete:
            log.info(
                "curriculum already complete (%r is the last stage); %s promotion ignored",
                completion.last_stage,
                completion.reason,
            )
            return
        self.complete = True
        log.info(
            "curriculum complete: %r was the last stage (%s)",
            completion.last_stage,
            completion.reason,
        )

    def _check_rt_is_recorded(self, rt: Any) -> None:
        """Warn, once per session, about an RT criterion with no RT to judge.

        ``mean_rt_ms`` is NaN over a window with no RT in it, and NaN meets
        no threshold — deliberately, so a missing RT never reads as "fast
        enough". The cost is that a curriculum whose ``rt_key`` does not
        match the field the task's phases write (their ``rt_record_key``)
        never promotes and never demotes on RT, in silence. It cannot be
        caught at build — which phases write what is only known once trials
        run — so it is caught here: once a whole ``min_trials`` of completed
        trials has gone by in this session at a stage gating on RT, and not
        one of them carried the field. One trial without an RT is normal (a
        task may leave it empty on some outcomes); none at all is a
        misconfiguration.
        """
        self._completed_seen += 1
        if rt is not None:
            self._rt_seen = True
        if self._rt_seen or self._rt_warned:
            return
        criteria = self.stage.criteria
        if "mean_rt_ms" not in criteria.promote_when and "mean_rt_ms" not in criteria.demote_when:
            return
        if self._completed_seen < criteria.min_trials:
            return
        self._rt_warned = True
        log.warning(
            "stage %r has a mean_rt_ms criterion, but none of the %d completed trials this "
            "session recorded an RT under %r (the curriculum's rt_key), so mean_rt_ms is NaN "
            "and that criterion can never be met. If the task's phases write the RT under "
            "another name (their rt_record_key), set the curriculum's rt_key to it.",
            self.stage.name,
            self._completed_seen,
            self._curriculum.rt_key,
        )

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


def _storable(field: str, value: Any) -> Any:
    """A record value as the criteria window may keep it.

    The window is written to the subject's YAML state file at teardown, and
    safe YAML holds only plain values. A numpy scalar (what a task computing
    its measures with numpy writes) is turned into the Python value it is;
    anything else that is not a plain value is refused now, naming the
    field, rather than failing the state file's write after the session.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float)):
        # Made exactly int or float: a subclass (numpy's float64 is one) is
        # not something safe YAML will write.
        return float(value) if isinstance(value, float) else int(value)
    # A numpy scalar or 0-d array: `.item()` is its plain Python value.
    item = getattr(value, "item", None)
    if callable(item) and getattr(value, "ndim", None) == 0:
        return _storable(field, item())
    raise ConfigError(
        f"the curriculum's record_fields asks the criteria window to keep {field!r}, but "
        f"this trial's record holds a {type(value).__name__} there — the window is saved in "
        f"the training state file and can keep only a number, text, true/false or nothing. "
        f"Record a single value in that field, or leave it out of record_fields."
    )


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
