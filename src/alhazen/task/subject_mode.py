"""The one place that decides how a subject answers: a key, or a saccade.

A task package is written once and run on whichever rig a session happens to
load — a human at a keyboard, or a monkey with an eye tracker and a reward
line. Which of those two response paths a trial uses must not fall out of
*which rig file was loaded* (a monkey rig can run a keyboard-trained task
during shaping; a human rig can be wired with an eye tracker for a gaze
study). It is a property of the task's own design, so it is declared here, as
a named value a task's params model carries, and read in exactly one place:
:func:`response_phases`.

Reward is not threaded through this module. ``SACCADE_AND_REWARD`` names the
response *modality* — the subject answers by looking, not by pressing — not
the payout: whether an outcome earns juice is already decided by
``alhazen.task.reward_policy.RewardPolicy``, keyed on the outcome name, for
both modes alike. A keyboard task with a reward policy pays out through the
same mechanism.
"""

from __future__ import annotations

from enum import Enum

from alhazen.core.trial import Outcome, Phase
from alhazen.task.phases.gaze import LandingCheck, StimulusResponse
from alhazen.task.phases.response import ResponseWindow


class SubjectMode(str, Enum):
    """How a trial's response phase is built. A task's params model declares
    one of these; nothing infers it from rig hardware or config."""

    KEYBOARD = "keyboard"
    SACCADE_AND_REWARD = "saccade_and_reward"


def response_phases(
    mode: SubjectMode,
    *,
    stimulus_key: str = "target",
    timeout_s: float = 2.0,
    on_timeout: Outcome | None = None,
    # KEYBOARD
    keys: dict[str, Outcome] | None = None,
    # SACCADE_AND_REWARD
    depart_region: str = "fixation",
    target_region: str = "target",
    landing_timeout_s: float = 0.5,
    on_hit: Outcome | None = None,
    on_miss: Outcome | None = None,
) -> list[Phase]:
    """The phases one response window's worth of trial needs, for the given
    mode. Appended straight into a task's ``build_trial`` phase list.

    Raises rather than silently building an incomplete trial: a mode missing
    the outcomes it needs would otherwise fail deep inside the engine, on
    whichever frame a subject first triggered the phase, instead of at
    build time where the mistake was actually made.
    """
    if mode is SubjectMode.KEYBOARD:
        if not keys:
            raise ValueError("SubjectMode.KEYBOARD needs keys mapped to outcomes")
        if on_timeout is None:
            raise ValueError("SubjectMode.KEYBOARD needs on_timeout")
        return [
            ResponseWindow(
                keys=keys,
                timeout_s=timeout_s,
                on_timeout=on_timeout,
                stimulus_keys=[stimulus_key],
            )
        ]
    if mode is SubjectMode.SACCADE_AND_REWARD:
        if on_hit is None or on_miss is None:
            raise ValueError("SubjectMode.SACCADE_AND_REWARD needs on_hit and on_miss")
        return [
            StimulusResponse(
                stimulus_key=stimulus_key,
                depart_region=depart_region,
                timeout_s=timeout_s,
                on_timeout=on_timeout or on_miss,
            ),
            LandingCheck(
                region=target_region,
                timeout_s=landing_timeout_s,
                on_hit=on_hit,
                on_miss=on_miss,
            ),
        ]
    raise AssertionError(mode)  # pragma: no cover - Enum exhausted above
