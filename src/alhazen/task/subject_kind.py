"""Who is in the chair, a human or a monkey, and whether the reward line opens.

A task package runs on whichever rig a session loads, and the lab rig has a
juice line (``devices.reward``) whatever subject is sitting at it. Before
this module, whether a session paid juice followed from two facts that say
nothing about the subject: the task declared a ``RewardPolicy`` and the rig
had a dispenser. A human volunteer on the lab rig ran with the pump armed
(the manual ``r`` key opened it, and a task with a policy paid it), and a
human and a monkey session of the same task produced files that could only
be told apart by who remembered which was which.

The decision this module hides is that rule: **a session's subject kind,
declared in its params file, decides whether the reward line exists at
all, and what pays on it.** It is read in one place (`reward_for`, called
by ``Task.__init__``); the session builder asks `opens_reward_line` before
it opens a dispenser, and the run's record carries the kind on every row
and in session.json (session/identity.py), so a human run and a monkey run
can never be confused afterwards.

- ``human``: no dispenser is opened, whatever the rig has; the task pays no
  outcome; the experimenter's manual reward key does nothing. A params file
  that declares ``human`` and also carries a ``reward`` block is refused
  when it loads: one of the two is a mistake.
- ``monkey``: the params file's ``reward`` block is the policy, and it is
  required (a monkey session that pays nothing is a config mistake, not a
  choice). Every outcome it names must be one the task declares, because a
  misspelt outcome would pay nothing for a whole session. Run mode refuses a
  rig with no reward line; test and simulate stand a simulated one in. A
  monkey reads nothing, so its session shows no instruction screen.
- undeclared (``None``): the behaviour before 2.12, for tasks and files
  written before this existed: the task's class ``reward`` pays on any rig
  that has a line. Nothing infers the kind from the rig or the subject id.

This is not ``SubjectMode`` (task/subject_mode.py), which is how a trial's
response is built (a key or a saccade). A monkey answers with its eyes, but
a human gaze study does too, so the two are separate declarations.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import model_validator

from alhazen.config.models import Model
from alhazen.errors import ConfigError
from alhazen.task.reward_policy import RewardPolicy


class SubjectKind(str, Enum):
    """Who a session's subject is, as its params file declares it."""

    HUMAN = "human"
    MONKEY = "monkey"


class SubjectParams(Model):
    """The two params fields every task that separates human and monkey
    sessions carries: mix it into the task's params model.

    ``subject_kind`` is who the params file is for. ``reward`` is what a
    monkey session pays, in the params file because it is what an
    experimenter tunes between sessions (pulse width sets the volume per
    pulse; the rig file says which line it goes out on).
    """

    subject_kind: SubjectKind | None = None
    reward: RewardPolicy | None = None

    @model_validator(mode="after")
    def _reward_matches_the_subject(self) -> SubjectParams:
        if self.subject_kind is SubjectKind.HUMAN and self.reward is not None:
            raise ValueError(
                "subject_kind is human, and a human session never pays reward, but this "
                "file has a reward block. Remove the reward block, or declare "
                "subject_kind: monkey if the file is for a monkey"
            )
        if self.subject_kind is None and self.reward is not None:
            raise ValueError(
                "this file has a reward block but no subject_kind. A reward block is a "
                "monkey session's; declare subject_kind: monkey"
            )
        if self.subject_kind is SubjectKind.MONKEY:
            if self.reward is None:
                raise ValueError(
                    "subject_kind is monkey, but the file has no reward block saying what "
                    "pays: add reward: {by_outcome: {<OUTCOME>: {n_pulses, pulse_ms, "
                    "inter_pulse_ms}}}"
                )
            if not self.reward.by_outcome and self.reward.on_fault is None:
                raise ValueError(
                    "subject_kind is monkey, but its reward block pays nothing (by_outcome is "
                    "empty and on_fault unset)"
                )
            # A scale that rounds every paying entry below one pulse is the
            # same mistake in another form: a monkey session that pays nothing.
            entries = [*self.reward.by_outcome, *(["on_fault"] if self.reward.on_fault else [])]
            pays = [name for name in self.reward.by_outcome if self.reward.pulses_for(name)]
            if not pays and self.reward.pulses_for_fault() is None:
                raise ValueError(
                    f"subject_kind is monkey, but at reward scale {self.reward.scale:g} none of "
                    f"its reward entries ({', '.join(entries)}) comes to a single pulse"
                )
        return self


def subject_kind_of(params: object) -> SubjectKind | None:
    """The subject kind a task's params declare, or None for undeclared.

    Takes the params model or their plain-data dump (a session config's
    ``task_params``), so the session record reads it the same way the task
    does. A value that is not a known kind is a ConfigError, never None: an
    unknown kind silently read as "undeclared" would bring the old rule back.
    """
    if isinstance(params, Mapping):
        value: Any = params.get("subject_kind")
    else:
        value = getattr(params, "subject_kind", None)
    if value is None:
        return None
    try:
        return SubjectKind(value)
    except ValueError as e:
        known = ", ".join(kind.value for kind in SubjectKind)
        raise ConfigError(f"subject_kind {value!r} is not one of {known}") from e


def reward_for(
    class_reward: RewardPolicy | None, params: object, outcome_names: frozenset[str]
) -> RewardPolicy | None:
    """The reward policy a task instance runs with: the one place the rule
    in the module docstring is applied.

    ``class_reward`` is the task class's own ``reward`` (used only when the
    params declare no kind), ``outcome_names`` the task's outcomes.
    """
    kind = subject_kind_of(params)
    if kind is None:
        return class_reward
    if kind is SubjectKind.HUMAN:
        return None
    policy = getattr(params, "reward", None)
    if not isinstance(policy, RewardPolicy):
        # SubjectParams already refuses a monkey file without one; a params
        # model that declares subject_kind without mixing it in lands here.
        raise ConfigError(
            "subject_kind is monkey, but the params carry no reward policy; mix "
            "alhazen.SubjectParams into the task's params model and give the file a reward "
            "block"
        )
    unknown = sorted(set(policy.by_outcome) - outcome_names)
    if unknown:
        raise ConfigError(
            f"the reward block pays outcome(s) {', '.join(unknown)}, which the task does not "
            f"declare (its outcomes: {', '.join(sorted(outcome_names))}). A misspelt outcome "
            f"would pay nothing for the whole session"
        )
    return policy


def opens_reward_line(params: object) -> bool:
    """Whether a session with these params may open the rig's reward line:
    False for a human session, True otherwise (a monkey's, or undeclared)."""
    return subject_kind_of(params) is not SubjectKind.HUMAN
