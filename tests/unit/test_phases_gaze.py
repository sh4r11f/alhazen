"""The gaze phase library: happy paths, timeouts, and the blink rule."""

from __future__ import annotations

import pytest

from alhazen.core.trial import CircleRegion, InputFrame, Outcome
from alhazen.task.phases import AcquireFixation, HoldFixation, LandingCheck, StimulusResponse
from alhazen.testing import FakeStimulus, ScriptedInputs
from support import FRAME_S, EngineHarness

FIX = CircleRegion((0.0, 0.0), 40.0)
TARGET = CircleRegion((400.0, 0.0), 60.0)
IN_FIX = InputFrame(gaze=(0.0, 0.0))
# Inside the fixation window, but not on its centre — which is where a real
# eye sits, and the whole reason the launch point is measured rather than
# assumed to be the fixation point.
IN_FIX_OFFSET = InputFrame(gaze=(12.0, -8.0))
BLINK = InputFrame(gaze=None)
AWAY = InputFrame(gaze=(500.0, 500.0))
ON_TARGET = InputFrame(gaze=(400.0, 0.0))

TIMED_OUT = Outcome("TIMED_OUT", completed=True, success=False)
BROKE = Outcome("BROKE", completed=False)
HIT = Outcome("HIT", completed=True, success=True)
MISS = Outcome("MISS", completed=True, success=False)


def run(phases, inputs, declared=("FIX_ON", "FIX_ACQUIRED", "STIM_ON", "LANDED")):
    """Run one trial through the real engine with scripted gaze."""
    harness = EngineHarness(
        input_provider=ScriptedInputs(inputs),
        declared_events=(*declared, "SACCADE_ONSET", "RESPONSE_ONSET"),
    )
    ctx = harness.ctx(
        stimuli={"fixation": FakeStimulus("fix"), "target": FakeStimulus("target")},
        regions={"fixation": FIX, "target": TARGET},
    )
    result = harness.engine.run_trial(ctx, list(phases))
    return harness, result


class TestAcquireFixation:
    def phase(self, **kwargs):
        defaults = dict(timeout_s=10 * FRAME_S, on_timeout=TIMED_OUT, hold_s=0.0)
        return AcquireFixation(**{**defaults, **kwargs})

    def test_gaze_in_the_window_acquires_and_advances(self):
        harness, result = run([self.phase(), _EndPhase()], [AWAY, AWAY, IN_FIX])
        assert result.outcome is _DONE
        assert "FIX_ACQUIRED" in harness.collector.names()
        assert result.record["acquire_latency_s"] == pytest.approx(2 * FRAME_S)

    def test_gaze_that_never_arrives_times_out(self):
        harness, result = run([self.phase()], [AWAY])
        assert result.outcome is TIMED_OUT
        assert "FIX_ACQUIRED" not in harness.collector.names()

    def test_hold_timer_resets_on_any_excursion(self):
        # Two frames in, one out, two in: with a 3-frame hold requirement the
        # excursion must restart the clock, not be forgiven. Fixation has to
        # be continuous — a subject flicking through the window repeatedly is
        # not a subject fixating.
        phase = self.phase(hold_s=3 * FRAME_S, timeout_s=20 * FRAME_S)
        harness, result = run([phase, _EndPhase()], [IN_FIX, IN_FIX, AWAY, IN_FIX, IN_FIX])
        # Acquisition happens on the 4th in-window frame after the reset, not
        # on the 3rd frame overall.
        assert result.record["acquire_latency_s"] > 3 * FRAME_S

    def test_a_blink_is_an_excursion(self):
        phase = self.phase(hold_s=2 * FRAME_S, timeout_s=20 * FRAME_S)
        harness, result = run([phase, _EndPhase()], [IN_FIX, BLINK, IN_FIX, IN_FIX, IN_FIX])
        assert result.record["acquire_latency_s"] > 2 * FRAME_S

    def test_onset_event_is_emitted_on_enter(self):
        harness, _ = run([self.phase(), _EndPhase()], [IN_FIX])
        assert "FIX_ON" in harness.collector.names()

    def test_blinking_the_point_toggles_its_drawing(self):
        phase = self.phase(blink_period_s=FRAME_S, timeout_s=6 * FRAME_S)
        harness = EngineHarness(input_provider=ScriptedInputs([AWAY]))
        fixation = FakeStimulus("fix")
        ctx = harness.ctx(stimuli={"fixation": fixation}, regions={"fixation": FIX})
        harness.engine.run_trial(ctx, [phase])
        # Drawn on some frames and not others — otherwise the blink cue that
        # draws a naive subject's eye to the point does not exist.
        assert 0 < fixation.draw_count < 6


class TestHoldFixation:
    def phase(self, **kwargs):
        defaults = dict(duration_s=3 * FRAME_S, on_break=BROKE)
        return HoldFixation(**{**defaults, **kwargs})

    def test_holding_for_the_duration_advances(self):
        harness, result = run([self.phase(), _EndPhase()], [IN_FIX])
        assert result.outcome is _DONE

    def test_looking_away_breaks(self):
        harness, result = run([self.phase()], [IN_FIX, AWAY])
        assert result.outcome is BROKE

    def test_a_blink_on_the_final_frame_is_a_break(self):
        # The gaze check runs before the completion check precisely so this
        # cannot pass: a subject who blinked through the last frame did not
        # verifiably hold fixation.
        inputs = [IN_FIX, IN_FIX, IN_FIX, BLINK]
        harness, result = run([self.phase(duration_s=4 * FRAME_S)], inputs)
        assert result.outcome is BROKE

    def test_jitter_is_drawn_once_from_the_session_rng(self):
        harness, result = run(
            [self.phase(duration_s=4 * FRAME_S, jitter_s=FRAME_S), _EndPhase()], [IN_FIX]
        )
        held = result.record["hold_duration_s"]
        assert 3 * FRAME_S <= held <= 5 * FRAME_S

    def test_concurrent_stimuli_are_drawn_every_frame(self):
        harness = EngineHarness(input_provider=ScriptedInputs([IN_FIX]))
        fixation, target = FakeStimulus("fix"), FakeStimulus("target")
        ctx = harness.ctx(
            stimuli={"fixation": fixation, "target": target}, regions={"fixation": FIX}
        )
        harness.engine.run_trial(ctx, [self.phase(concurrent=["target"]), _EndPhase()])
        assert target.draw_count == fixation.draw_count > 0


class TestStimulusResponse:
    def phase(self, **kwargs):
        defaults = dict(stimulus_key="target", timeout_s=10 * FRAME_S, on_timeout=TIMED_OUT)
        return StimulusResponse(**{**defaults, **kwargs})

    def test_the_launch_point_is_measured_not_assumed(self):
        # A saccade is a displacement; a displacement from an assumed origin is
        # an assumption. The last sample verifiably inside the window is that
        # origin, and the eye is never exactly on the fixation point.
        _harness, result = run([self.phase(), _EndPhase()], [IN_FIX, IN_FIX_OFFSET, AWAY])

        # 40 px per degree.
        assert result.record["fixation_x_dva"] == pytest.approx(0.3)
        assert result.record["fixation_y_dva"] == pytest.approx(-0.2)

    def test_the_columns_are_named_after_the_region_departed_from(self):
        _harness, result = run(
            [self.phase(start_record_prefix="launch"), _EndPhase()],
            [IN_FIX_OFFSET, IN_FIX_OFFSET, AWAY],
        )
        assert result.record["launch_x_dva"] == pytest.approx(0.3)
        assert "fixation_x_dva" not in result.record

    def test_an_origin_that_was_never_verified_is_left_unknown(self):
        # Gaze lost from the first frame: this trial has no origin, and
        # calling it screen centre would invent one.
        _harness, result = run([self.phase(), _EndPhase()], [BLINK, BLINK, BLINK])
        assert "fixation_x_dva" not in result.record

    def test_departure_records_a_reaction_time_from_the_onset_flip(self):
        harness, result = run([self.phase(), _EndPhase()], [IN_FIX, IN_FIX, AWAY])
        assert result.outcome is _DONE
        # Measured from the flip that showed the stimulus, not from the call
        # that drew it: one frame period per frame the subject stayed.
        assert result.record["rt_ms"] == pytest.approx(FRAME_S * 1000, abs=1.0)
        assert "RESPONSE_ONSET" in harness.collector.names()

    def test_staying_in_the_window_times_out(self):
        harness, result = run([self.phase()], [IN_FIX])
        assert result.outcome is TIMED_OUT
        assert "rt_ms" not in result.record

    def test_track_loss_reads_as_departure(self):
        # The legacy detection rule, kept deliberately: "not verifiably
        # inside the window" is saccade onset, and a sample lost mid-saccade
        # is exactly that case.
        harness, result = run([self.phase(), _EndPhase()], [IN_FIX, BLINK])
        assert result.outcome is _DONE
        assert "rt_ms" in result.record


class TestLandingCheck:
    def phase(self, **kwargs):
        defaults = dict(timeout_s=5 * FRAME_S, on_hit=HIT, on_miss=MISS)
        return LandingCheck(**{**defaults, **kwargs})

    def test_landing_in_the_region_hits_and_records_the_endpoint(self):
        harness, result = run([self.phase()], [AWAY, ON_TARGET])
        assert result.outcome is HIT
        assert result.record["endpoint_in_target"] is True
        assert result.record["endpoint_x_dva"] == pytest.approx(10.0)  # 400 px / 40 px per deg
        assert "LANDED" in harness.collector.names()

    def test_timeout_records_the_last_known_gaze(self):
        # A saccade to the wrong place is still a data point; discarding it
        # would leave a dataset of only the trials that agreed.
        harness, result = run([self.phase()], [InputFrame(gaze=(200.0, 80.0)), BLINK])
        assert result.outcome is MISS
        assert result.record["endpoint_in_target"] is False
        assert result.record["endpoint_x_dva"] == pytest.approx(5.0)
        assert result.record["endpoint_y_dva"] == pytest.approx(2.0)
        # The target sits at 10 dva on the horizontal; the miss is 5 short and
        # 2 high. Recorded as one number because coordinates cannot be
        # averaged across a condition that moves the target — a task with left
        # and right targets would average to zero and report perfect aim.
        assert result.record["endpoint_error_dva"] == pytest.approx((5.0**2 + 2.0**2) ** 0.5)

    def test_timeout_with_no_gaze_at_all_records_only_the_flag(self):
        harness, result = run([self.phase()], [BLINK])
        assert result.outcome is MISS
        assert result.record["endpoint_in_target"] is False
        assert "endpoint_x_dva" not in result.record
        assert "endpoint_error_dva" not in result.record


class TestConstructorGuards:
    def test_phases_refuse_to_be_built_without_their_outcomes(self):
        # A phase with no outcome to return would fail mid-trial, with a
        # subject in the rig, rather than at build time.
        with pytest.raises(ValueError, match="on_timeout"):
            AcquireFixation(on_timeout=None)
        with pytest.raises(ValueError, match="on_break"):
            HoldFixation(on_break=None)
        with pytest.raises(ValueError, match="on_timeout"):
            StimulusResponse("target", on_timeout=None)
        with pytest.raises(ValueError, match="on_hit and on_miss"):
            LandingCheck(on_hit=HIT, on_miss=None)


_DONE = Outcome("DONE", completed=True, success=True)


class _EndPhase:
    """Ends the trial the frame after the phase under test advanced."""

    name = "end"

    def on_enter(self, ctx):
        return

    def on_frame(self, ctx):
        return _DONE
