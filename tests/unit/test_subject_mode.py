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
        on_timeout=NO_RESPONSE,
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
        on_timeout=NO_RESPONSE,
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
    with pytest.raises(ValueError, match="on_hit and on_miss"):
        response_phases(SubjectMode.SACCADE_AND_REWARD, on_hit=HIT, on_timeout=NO_RESPONSE)
    with pytest.raises(ValueError, match="on_hit and on_miss"):
        response_phases(SubjectMode.SACCADE_AND_REWARD, on_miss=MISS, on_timeout=NO_RESPONSE)


def test_saccade_and_reward_mode_needs_its_own_timeout_outcome():
    # "No saccade at all" is not a landing miss. Standing in for one, it
    # would be a completed trial (a miss usually is), never served again and
    # fed to an adaptive scheduler as the subject's wrong answer.
    with pytest.raises(ValueError, match="on_timeout"):
        response_phases(SubjectMode.SACCADE_AND_REWARD, on_hit=HIT, on_miss=MISS)


def test_saccade_and_reward_mode_ends_a_trial_with_no_saccade_as_on_timeout():
    phases = response_phases(
        SubjectMode.SACCADE_AND_REWARD,
        timeout_s=2 * FRAME_S,
        landing_timeout_s=2 * FRAME_S,
        on_timeout=NO_RESPONSE,
        on_hit=HIT,
        on_miss=MISS,
    )
    harness, result = run(
        phases,
        [IN_FIX],
        declared=("STIM_ON", "RESPONSE_ONSET", "LANDED"),
        regions={"fixation": FIX, "target": TARGET},
    )
    assert result.outcome is NO_RESPONSE
    assert "endpoint_in_target" not in result.record


# ---------------------------------------------------------------------------
# Where the saccade mode says the eye landed
# ---------------------------------------------------------------------------
#
# The mode's landing phase is LandingSample in its fixed-dwell mode: the
# landing is judged once, landing_timeout_s after the RESPONSE_ONSET flip, on
# the last valid sample outside the fixation window. With the scripts below,
# StimulusResponse reads frames 0-2 and sees the eye leave on frame 2 (a
# script that starts IN_FIX, IN_FIX); RESPONSE_ONSET is stamped by the flip
# after it, at 3 frame periods, and the landing phase reads frames 3, 4, 5, …
# A 2.5-frame dwell (a fraction, so no test rests on a floating-point tie at
# a frame boundary) therefore ends on frame 6, the first read at or past
# 3 + 2.5 frame periods.

LANDING_DWELL_S = 2.5 * FRAME_S
BLINK = InputFrame(gaze=None)
DECLARED = ("STIM_ON", "RESPONSE_ONSET", "LANDED")
REGIONS = {"fixation": FIX, "target": TARGET}


def gaze_at(x: float, y: float = 0.0) -> InputFrame:
    return InputFrame(gaze=(x, y))


def saccade_mode() -> list:
    return response_phases(
        SubjectMode.SACCADE_AND_REWARD,
        timeout_s=10 * FRAME_S,
        landing_timeout_s=LANDING_DWELL_S,
        on_timeout=NO_RESPONSE,
        on_hit=HIT,
        on_miss=MISS,
    )


class TestSaccadeModeLanding:
    def test_the_endpoint_is_where_the_eye_settled_not_where_it_crossed_in(self):
        # The eye leaves fixation (300 px), crosses the target's 60 px edge
        # mid-flight at 350 px, and comes to rest at (410, 10) px — inside
        # the target, and not where it entered. The crossing (8.75 dva) is
        # what the old first-sample-inside rule recorded; the rest point
        # (10.25, 0.25 dva at 40 px per degree) is the landing.
        harness, result = run(
            saccade_mode(),
            [
                IN_FIX,
                IN_FIX,
                gaze_at(300.0),
                gaze_at(350.0),
                gaze_at(390.0),
                gaze_at(410.0, 10.0),
            ],
            declared=DECLARED,
            regions=REGIONS,
        )
        assert result.outcome is HIT
        assert result.record["endpoint_x_dva"] == pytest.approx(10.25)
        assert result.record["endpoint_y_dva"] == pytest.approx(0.25)
        assert result.record["endpoint_measured"] is True
        assert result.record["endpoint_in_target"] is True
        # Measured from the target's centre (10, 0 dva), which the record
        # now names alongside the error.
        assert result.record["endpoint_error_dva"] == pytest.approx((0.25**2 + 0.25**2) ** 0.5)
        assert result.record["endpoint_reference_x_dva"] == pytest.approx(10.0)
        # A fixed dwell has no settle verdict to report.
        assert "endpoint_settled" not in result.record

    def test_a_hit_ends_the_trial_after_the_dwell_not_on_entering_the_target(self):
        # The trial end moved: the landing is judged landing_timeout_s after
        # the onset flip, however early the eye reached the target. LANDED
        # goes out on the flip after the judging frame (frame 6, flipped at
        # 7 frame periods), 4 frame periods after RESPONSE_ONSET (3).
        harness, result = run(
            saccade_mode(),
            [IN_FIX, IN_FIX, ON_TARGET],
            declared=DECLARED,
            regions=REGIONS,
        )
        assert result.outcome is HIT
        t = {event.name: event.t for event in harness.collector.events}
        assert t["LANDED"] - t["RESPONSE_ONSET"] >= LANDING_DWELL_S
        assert t["LANDED"] - t["RESPONSE_ONSET"] == pytest.approx(4 * FRAME_S)

    def test_passing_through_the_target_is_a_miss(self):
        # An overshoot: into the target and out the far side, resting at
        # 500 px. The first-sample-inside rule scored the crossing as a hit.
        harness, result = run(
            saccade_mode(),
            [IN_FIX, IN_FIX, gaze_at(300.0), ON_TARGET, gaze_at(480.0), gaze_at(500.0)],
            declared=DECLARED,
            regions=REGIONS,
        )
        assert result.outcome is MISS
        assert result.record["endpoint_in_target"] is False
        assert result.record["endpoint_x_dva"] == pytest.approx(12.5)
        assert result.record["endpoint_error_dva"] == pytest.approx(2.5)

    def test_a_saccade_that_never_arrives_is_on_miss_with_its_endpoint(self):
        # Landed off target: a completed, wrong answer, and where the eye went
        # is on the record, since a dataset of only the hits would agree with
        # any hypothesis.
        harness, result = run(
            saccade_mode(),
            [IN_FIX, IN_FIX, AWAY],
            declared=DECLARED,
            regions=REGIONS,
        )
        assert result.outcome is MISS
        assert result.record["endpoint_measured"] is True
        assert result.record["endpoint_in_target"] is False
        assert result.record["endpoint_x_dva"] == pytest.approx(12.5)
        assert result.record["endpoint_y_dva"] == pytest.approx(12.5)

    def test_a_blink_at_the_cue_with_no_saccade_after_it_is_not_measured(self):
        # The blink rule reads the blink as leaving fixation, so onset is
        # stamped with the eye still there. It never leaves, so there is no
        # landing: on_miss, measured False, no coordinates and no LANDED —
        # never a landing at fixation, which is what the old rule recorded.
        harness, result = run(
            saccade_mode(),
            [IN_FIX, IN_FIX, BLINK, IN_FIX],
            declared=DECLARED,
            regions=REGIONS,
        )
        assert result.outcome is MISS
        assert result.record["endpoint_measured"] is False
        assert result.record["endpoint_in_target"] is False
        for column in ("x_dva", "y_dva", "error_dva", "latency_ms"):
            assert f"endpoint_{column}" not in result.record
        assert "LANDED" not in harness.collector.names()
