"""The subject-response seam: one factory, two modes, no rig involved."""

from __future__ import annotations

import pytest

from alhazen.core.trial import CircleRegion, InputFrame, Outcome
from alhazen.task.subject_mode import SubjectMode, response_phases
from alhazen.testing import FakeStimulus, ScriptedInputs
from support import FRAME_S, EngineHarness

FIX = CircleRegion((0.0, 0.0), 40.0)
TARGET = CircleRegion((400.0, 0.0), 60.0)
IN_FIX = InputFrame(gaze=(0.0, 0.0))
ON_TARGET = InputFrame(gaze=(400.0, 0.0))
AWAY = InputFrame(gaze=(500.0, 500.0))

LEFT = Outcome("LEFT", completed=True, success=True)
NO_RESPONSE = Outcome("NO_RESPONSE", completed=False)
HIT = Outcome("HIT", completed=True, success=True)
MISS = Outcome("MISS", completed=True, success=False)


def press(*keys: str) -> InputFrame:
    return InputFrame(keys=keys)


def run(phases, inputs, declared, regions=None):
    harness = EngineHarness(input_provider=ScriptedInputs(inputs), declared_events=declared)
    ctx = harness.ctx(
        stimuli={"target": FakeStimulus("target")},
        regions=regions or {},
    )
    return harness, harness.engine.run_trial(ctx, list(phases))


def test_keyboard_mode_ends_the_trial_on_a_bound_key():
    phases = response_phases(
        SubjectMode.KEYBOARD,
        keys={"left": LEFT},
        timeout_s=10 * FRAME_S,
        on_timeout=NO_RESPONSE,
    )
    harness, result = run(phases, [press("left")], declared=("RESPONSE_CUE", "RESPONSE"))
    assert result.outcome is LEFT


def test_keyboard_mode_needs_keys_and_a_timeout_outcome():
    with pytest.raises(ValueError):
        response_phases(SubjectMode.KEYBOARD, on_timeout=NO_RESPONSE)
    with pytest.raises(ValueError):
        response_phases(SubjectMode.KEYBOARD, keys={"left": LEFT})


def test_saccade_and_reward_mode_lands_on_the_target():
    phases = response_phases(
        SubjectMode.SACCADE_AND_REWARD,
        timeout_s=10 * FRAME_S,
        landing_timeout_s=10 * FRAME_S,
        on_hit=HIT,
        on_miss=MISS,
    )
    harness, result = run(
        phases,
        [IN_FIX, AWAY, ON_TARGET],
        declared=("STIM_ON", "RESPONSE_ONSET", "LANDED"),
        regions={"fixation": FIX, "target": TARGET},
    )
    assert result.outcome is HIT
    assert result.record["endpoint_in_target"] is True


def test_saccade_and_reward_mode_reports_a_miss_on_timeout():
    phases = response_phases(
        SubjectMode.SACCADE_AND_REWARD,
        timeout_s=2 * FRAME_S,
        landing_timeout_s=2 * FRAME_S,
        on_hit=HIT,
        on_miss=MISS,
    )
    harness, result = run(
        phases,
        [IN_FIX, AWAY, AWAY, AWAY, AWAY],
        declared=("STIM_ON", "RESPONSE_ONSET", "LANDED"),
        regions={"fixation": FIX, "target": TARGET},
    )
    assert result.outcome is MISS


def test_saccade_and_reward_mode_needs_hit_and_miss_outcomes():
    with pytest.raises(ValueError):
        response_phases(SubjectMode.SACCADE_AND_REWARD, on_hit=HIT)
    with pytest.raises(ValueError):
        response_phases(SubjectMode.SACCADE_AND_REWARD, on_miss=MISS)
