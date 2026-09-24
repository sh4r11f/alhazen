"""The gaze phase library: happy paths, timeouts, and the blink rule."""

from __future__ import annotations

import pytest

from alhazen.core.trial import CircleRegion, InputFrame, Outcome, PhaseAction
from alhazen.devices.eyetracker import GazeSample, ScriptedTracker
from alhazen.session.builder import make_gaze_input_provider
from alhazen.task.phases import (
    AcquireFixation,
    HoldFixation,
    LandingCheck,
    LandingSample,
    StimulusResponse,
)
from alhazen.testing import FakeStimulus, ScriptedInputs
from support import FRAME_S, SCREEN, EngineHarness, RunForFrames

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


def run(phases, inputs, declared=("FIX_ON", "FIX_ACQUIRED", "STIM_ON", "LANDED"), regions=None):
    """Run one trial through the real engine with scripted gaze. ``regions``
    replaces the default fixation and target windows, for a test about
    their geometry."""
    harness = EngineHarness(
        input_provider=ScriptedInputs(inputs),
        declared_events=(*declared, "SACCADE_ONSET", "RESPONSE_ONSET"),
    )
    ctx = harness.ctx(
        stimuli={"fixation": FakeStimulus("fix"), "target": FakeStimulus("target")},
        regions=regions or {"fixation": FIX, "target": TARGET},
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


# ---------------------------------------------------------------------------
# LandingSample
# ---------------------------------------------------------------------------
#
# Most LandingSample trials below start with onset(): one frame that emits
# RESPONSE_ONSET, the event StimulusResponse emits at saccade onset. It
# consumes the first scripted input, and its flip stamps t_response_onset at
# one frame period; the landing phase's k-th frame then reads its input at k
# frame periods. Durations are fractions of a frame (2.5 frames, not 3) so no
# test depends on a floating-point tie at a frame boundary.


def onset():
    return RunForFrames(0, PhaseAction.ADVANCE, emit_on_enter="RESPONSE_ONSET")


def at(x, t, y=0.0):
    """A frame carrying a gaze sample at (x, y) px, taken at time t."""
    return InputFrame(gaze=(x, y), gaze_t=t)


# The frame before onset: the eye still at fixation.
LAUNCH = at(0.0, 0.0)
# 30 deg/s at 40 px per degree is 1200 px/s: 20 px per 60 Hz frame.
SETTLE_DVA_PER_S = 30.0


def dwell_phase(**kwargs):
    defaults = dict(on_hit=HIT, on_miss=MISS, dwell_s=2.5 * FRAME_S)
    return LandingSample(**{**defaults, **kwargs})


def offset_phase(**kwargs):
    defaults = dict(
        on_hit=HIT,
        on_miss=MISS,
        settle_speed_dva_per_s=SETTLE_DVA_PER_S,
        max_wait_s=10.5 * FRAME_S,
    )
    return LandingSample(**{**defaults, **kwargs})


class TestLandingSampleDwell:
    def test_the_endpoint_is_where_the_eye_rests_not_where_it_entered(self):
        # The case the phase exists for: gaze crosses into the 60 px window
        # at 350 px, mid-flight, and comes to rest on the target at 400 px.
        # LandingCheck would record 8.75 dva; the landing is at 10.
        harness, result = run(
            [onset(), dwell_phase()],
            [LAUNCH, at(350.0, FRAME_S), at(390.0, 2 * FRAME_S), at(400.0, 3 * FRAME_S)],
        )
        assert result.outcome is HIT
        assert result.record["endpoint_measured"] is True
        assert result.record["endpoint_in_target"] is True
        assert result.record["endpoint_x_dva"] == pytest.approx(10.0)
        assert result.record["endpoint_error_dva"] == pytest.approx(0.0)
        assert "LANDED" in harness.collector.names()

    def test_passing_through_the_target_is_not_landing_in_it(self):
        # An overshoot: through the window and out the far side. The region
        # is ignored during the dwell, so the crossing earns nothing.
        _harness, result = run(
            [onset(), dwell_phase()],
            [LAUNCH, at(400.0, FRAME_S), at(480.0, 2 * FRAME_S), at(500.0, 3 * FRAME_S)],
        )
        assert result.outcome is MISS
        assert result.record["endpoint_in_target"] is False
        assert result.record["endpoint_x_dva"] == pytest.approx(12.5)
        assert result.record["endpoint_error_dva"] == pytest.approx(2.5)

    def test_the_dwell_runs_from_the_onset_flip(self):
        # 2.5 frames after the flip that stamped t_response_onset (at 1 frame
        # period): the first frame past it is read at 4 frame periods, the
        # landing phase's 4th. Later samples are never reached.
        _harness, result = run(
            [onset(), dwell_phase()],
            [
                LAUNCH,
                at(100.0, FRAME_S),
                at(200.0, 2 * FRAME_S),
                at(300.0, 3 * FRAME_S),
                at(400.0, 4 * FRAME_S),
                at(900.0, 5 * FRAME_S),
            ],
        )
        assert result.record["endpoint_x_dva"] == pytest.approx(10.0)
        assert result.record["endpoint_latency_ms"] == pytest.approx(3 * FRAME_S * 1000)

    def test_a_blink_at_the_end_keeps_the_last_valid_sample(self):
        _harness, result = run(
            [onset(), dwell_phase()],
            [LAUNCH, at(350.0, FRAME_S), at(398.0, 2 * FRAME_S), BLINK],
        )
        assert result.outcome is HIT
        assert result.record["endpoint_x_dva"] == pytest.approx(398.0 / 40.0)
        assert result.record["endpoint_latency_ms"] == pytest.approx(FRAME_S * 1000)

    def test_no_valid_sample_is_recorded_as_not_measured(self):
        # A blink through the whole dwell: the landing is unknown, and says
        # so — no coordinates, no error, no invented position, and a miss
        # (unverifiable gaze is outside every region).
        harness, result = run([onset(), dwell_phase()], [LAUNCH, BLINK])
        assert result.outcome is MISS
        assert result.record["endpoint_measured"] is False
        assert result.record["endpoint_in_target"] is False
        for column in ("x_dva", "y_dva", "error_dva", "latency_ms"):
            assert f"endpoint_{column}" not in result.record
        assert "LANDED" not in harness.collector.names()

    def test_samples_without_a_time_are_enough_for_a_dwell(self):
        # A dwell needs positions only, so a provider with no gaze_t (an
        # older hand-built one) still works; the frame's time stands in.
        _harness, result = run([onset(), dwell_phase()], [IN_FIX, ON_TARGET])
        assert result.outcome is HIT
        assert result.record["endpoint_latency_ms"] == pytest.approx(3 * FRAME_S * 1000)

    def test_it_composes_with_stimulus_response(self):
        # The default onset_event is the event StimulusResponse emits, so the
        # two chain with no wiring.
        harness, result = run(
            [
                StimulusResponse("target", timeout_s=10 * FRAME_S, on_timeout=TIMED_OUT),
                dwell_phase(),
            ],
            [IN_FIX, IN_FIX, AWAY, at(390.0, 3 * FRAME_S), at(400.0, 4 * FRAME_S)],
        )
        assert result.outcome is HIT
        assert "rt_ms" in result.record
        assert result.record["endpoint_x_dva"] == pytest.approx(10.0)
        names = harness.collector.names()
        assert names.index("RESPONSE_ONSET") < names.index("LANDED")


class TestLandingSampleSaccadeOffset:
    def test_it_ends_on_the_first_new_sample_below_the_threshold(self):
        # 180 px in a frame is flight; 18 px (27 deg/s) is under 30 deg/s.
        harness, result = run(
            [onset(), offset_phase()],
            [
                LAUNCH,
                at(200.0, FRAME_S),
                at(380.0, 2 * FRAME_S),
                at(398.0, 3 * FRAME_S),
                at(399.0, 4 * FRAME_S),
            ],
        )
        assert result.outcome is HIT
        assert result.record["endpoint_settled"] is True
        assert result.record["endpoint_x_dva"] == pytest.approx(398.0 / 40.0)
        # Ended on the landing phase's 3rd frame, not a fixed dwell later.
        assert result.record["endpoint_latency_ms"] == pytest.approx(2 * FRAME_S * 1000)
        assert "LANDED" in harness.collector.names()

    def test_a_repeated_sample_is_not_a_settled_eye(self):
        # The second frame brings no new sample: the same position, with the
        # same gaze_t. Counted as a sample, its speed is zero and the phase
        # would end at 5 dva, mid-saccade. It must wait for the next NEW one.
        _harness, result = run(
            [onset(), offset_phase()],
            [
                LAUNCH,
                at(200.0, FRAME_S),
                at(200.0, FRAME_S),  # the same sample, repeated
                at(390.0, 3 * FRAME_S),
                at(400.0, 4 * FRAME_S),
            ],
        )
        assert result.outcome is HIT
        assert result.record["endpoint_settled"] is True
        assert result.record["endpoint_x_dva"] == pytest.approx(10.0)

    def test_the_speed_uses_the_real_spacing_between_samples(self):
        # 15 px in 5 ms is 75 deg/s — still moving. Divided by a nominal
        # 60 Hz frame instead it would read 22.5 deg/s and end the phase on
        # the sample at 400 px.
        _harness, result = run(
            [onset(), offset_phase()],
            [
                LAUNCH,
                at(385.0, FRAME_S),
                at(400.0, FRAME_S + 0.005),
                at(401.0, 3 * FRAME_S),
            ],
        )
        assert result.record["endpoint_x_dva"] == pytest.approx(401.0 / 40.0)

    def test_a_blink_is_never_settled_and_breaks_the_speed_chain(self):
        # Across the blink the two samples are 2 px apart over two frames —
        # slow, if they were compared. They are not: a speed across a gap in
        # the data says nothing, so the sample after the blink starts afresh
        # and the next new one is the first that can settle.
        _harness, result = run(
            [onset(), offset_phase()],
            [
                LAUNCH,
                at(390.0, FRAME_S),
                BLINK,
                at(392.0, 3 * FRAME_S),
                at(393.0, 4 * FRAME_S),
            ],
        )
        assert result.record["endpoint_x_dva"] == pytest.approx(393.0 / 40.0)
        assert result.record["endpoint_latency_ms"] == pytest.approx(3 * FRAME_S * 1000)

    def test_an_eye_that_never_settles_ends_at_the_cap(self):
        # Moving 100 px a frame throughout. The cap ends the phase, the last
        # valid sample is judged, and settled=False lets analysis exclude it.
        moving = [at(100.0 * k, k * FRAME_S) for k in range(1, 20)]
        harness, result = run([onset(), offset_phase(max_wait_s=3.5 * FRAME_S)], [LAUNCH, *moving])
        assert result.outcome is MISS
        assert result.record["endpoint_settled"] is False
        assert result.record["endpoint_measured"] is True
        # The cap expires on the frame read at 5 frame periods (4 after the
        # onset flip), whose sample is at 500 px.
        assert result.record["endpoint_x_dva"] == pytest.approx(12.5)
        # Not a landing, so no LANDED event.
        assert "LANDED" not in harness.collector.names()

    def test_a_cap_with_no_valid_sample_is_not_measured(self):
        _harness, result = run([onset(), offset_phase(max_wait_s=2.5 * FRAME_S)], [LAUNCH, BLINK])
        assert result.outcome is MISS
        assert result.record["endpoint_measured"] is False
        assert result.record["endpoint_settled"] is False
        assert "endpoint_x_dva" not in result.record

    def test_a_position_without_a_time_is_refused_loudly(self):
        # Without gaze_t a new sample cannot be told from a repeat, and
        # guessing would bring back the false-zero speed.
        with pytest.raises(ValueError, match="gaze_t"):
            run([onset(), offset_phase()], [LAUNCH, ON_TARGET])

    def test_sample_times_that_go_backwards_are_refused(self):
        with pytest.raises(ValueError, match="backwards"):
            run(
                [onset(), offset_phase()],
                [LAUNCH, at(390.0, 2 * FRAME_S), at(395.0, FRAME_S)],
            )


# Both ways of ending, for tests that must hold for either.
LANDING_MODES = pytest.mark.parametrize(
    "mode",
    [
        dict(dwell_s=2.5 * FRAME_S),
        dict(settle_speed_dva_per_s=SETTLE_DVA_PER_S, max_wait_s=10.5 * FRAME_S),
    ],
    ids=["dwell", "saccade_offset"],
)


class TestLandingSampleVerdicts:
    # Lands and stays put: settles on the 2nd new sample, and is still there
    # when the dwell ends.
    ON = [LAUNCH, *(at(400.0, k * FRAME_S) for k in range(1, 6))]
    OFF = [LAUNCH, *(at(600.0, k * FRAME_S) for k in range(1, 6))]

    @LANDING_MODES
    def test_a_hit_can_be_an_outcome(self, mode):
        _harness, result = run([onset(), LandingSample(on_hit=HIT, on_miss=MISS, **mode)], self.ON)
        assert result.outcome is HIT
        assert result.record["endpoint_in_target"] is True

    @LANDING_MODES
    def test_a_miss_can_be_an_outcome(self, mode):
        _harness, result = run([onset(), LandingSample(on_hit=HIT, on_miss=MISS, **mode)], self.OFF)
        assert result.outcome is MISS
        assert result.record["endpoint_in_target"] is False

    @LANDING_MODES
    def test_a_hit_can_advance_to_the_next_phase(self, mode):
        phase = LandingSample(on_hit=PhaseAction.ADVANCE, on_miss=MISS, **mode)
        _harness, result = run([onset(), phase, _EndPhase()], self.ON)
        assert result.outcome is _DONE
        assert result.record["endpoint_in_target"] is True

    @LANDING_MODES
    def test_a_miss_can_advance_to_the_next_phase(self, mode):
        # The verdict is on the record for the next phase (TrialFeedback, a
        # pursuit) to read, rather than this one ending the trial.
        phase = LandingSample(on_hit=HIT, on_miss=PhaseAction.ADVANCE, **mode)
        _harness, result = run([onset(), phase, _EndPhase()], self.OFF)
        assert result.outcome is _DONE
        assert result.record["endpoint_in_target"] is False


class TestLandingSampleReference:
    def test_the_default_reference_is_the_regions_centre(self):
        _harness, result = run([onset(), dwell_phase()], [LAUNCH, at(410.0, FRAME_S)])
        assert result.record["endpoint_reference_x_dva"] == pytest.approx(10.0)
        assert result.record["endpoint_reference_y_dva"] == pytest.approx(0.0)
        assert result.record["endpoint_error_dva"] == pytest.approx(0.25)

    def test_a_fixed_reference_is_what_the_error_is_measured_from(self):
        # 20 px left of the region's centre; the eye lands on the centre.
        _harness, result = run([onset(), dwell_phase(reference=(380.0, 0.0))], [LAUNCH, ON_TARGET])
        assert result.record["endpoint_error_dva"] == pytest.approx(0.5)
        assert result.record["endpoint_reference_x_dva"] == pytest.approx(9.5)

    @LANDING_MODES
    def test_a_moving_figure_is_judged_where_it_is_when_the_landing_is(self, mode):
        # The figure moves at 3000 px/s. The eye lands at 195 px — far outside
        # the named region at 400 — on the frame the landing is judged in
        # both modes (read at 4 frame periods: the dwell's end, and the first
        # sample under 30 deg/s), when the figure is at 200 px. A hit, with
        # the region's radius as the tolerance around where the figure was.
        seen = []

        def figure(ctx):
            seen.append(ctx.clock.now())
            return (3000.0 * ctx.clock.now(), 0.0)

        phase = LandingSample(on_hit=HIT, on_miss=MISS, reference=figure, **mode)
        _harness, result = run(
            [onset(), phase],
            [
                LAUNCH,
                at(100.0, FRAME_S),
                at(150.0, 2 * FRAME_S),
                at(180.0, 3 * FRAME_S),
                at(195.0, 4 * FRAME_S),
            ],
        )
        assert result.outcome is HIT
        assert seen == [pytest.approx(4 * FRAME_S)]  # read once, at the end
        assert result.record["endpoint_reference_x_dva"] == pytest.approx(5.0)
        assert result.record["endpoint_error_dva"] == pytest.approx(5.0 / 40.0)


class TestLandingSampleThroughTheInputProvider:
    def test_a_tracker_slower_than_the_display_does_not_end_it_mid_saccade(self):
        # The input layer and the phase together, as a session wires them: a
        # 30 Hz tracker behind a 60 Hz display, so every other frame repeats
        # the previous sample. The builder's provider carries each sample's
        # own time through as gaze_t, which is what keeps the repeat at
        # 200 px (a false zero speed) from being taken for the landing.
        # The provider is handed to the engine at construction, as the builder
        # hands it; it reads a tracker that runs on the harness's own clock,
        # which exists only once the harness does — so the engine gets a
        # closure that reaches the provider built just below.
        provider: dict = {}
        harness = EngineHarness(
            declared_events=("LANDED", "RESPONSE_ONSET"),
            input_provider=lambda: provider["gaze"](),
        )
        # Each sample a millisecond before its frame reads it, so no lookup
        # depends on a floating-point tie between the two clocks.
        path_px = [0.0, 200.0, 390.0, 400.0, 401.0]
        script = [
            (k / 30 - 0.001, GazeSample(gx=960.0 + x, gy=540.0, t=k / 30 - 0.001))
            for k, x in enumerate(path_px)
        ]
        tracker = ScriptedTracker(script, harness.clock)
        provider["gaze"] = make_gaze_input_provider(tracker, SCREEN)
        ctx = harness.ctx(regions={"target": TARGET})

        result = harness.engine.run_trial(ctx, [onset(), offset_phase()])

        assert result.outcome is HIT
        assert result.record["endpoint_x_dva"] == pytest.approx(10.0)
        # The sample at 400 px was taken at 3/30 s; onset flipped at 1/60 s.
        assert result.record["endpoint_latency_ms"] == pytest.approx(
            (3 / 30 - 0.001 - FRAME_S) * 1000
        )


class TestLandingSampleOnset:
    def test_a_missing_onset_is_loud(self):
        # No earlier phase emitted RESPONSE_ONSET. Timing the landing from
        # "now" instead would be a wrong measurement that looks right.
        with pytest.raises(ValueError, match="RESPONSE_ONSET"):
            run([dwell_phase()], [ON_TARGET])

    def test_onset_can_be_another_event(self):
        first = RunForFrames(0, PhaseAction.ADVANCE, emit_on_enter="SACCADE_ONSET")
        _harness, result = run(
            [first, dwell_phase(onset_event="SACCADE_ONSET")], [LAUNCH, ON_TARGET]
        )
        assert result.outcome is HIT

    def test_onset_can_be_this_phases_start(self):
        # Entered at time 0 with no onset event anywhere: 2.5 frames from the
        # phase's start is first passed by the frame read at 3 frame periods.
        _harness, result = run(
            [dwell_phase(onset_event=None)],
            [at(100.0, 0.0), at(200.0, FRAME_S), at(300.0, 2 * FRAME_S), at(400.0, 3 * FRAME_S)],
        )
        assert result.outcome is HIT
        assert result.record["endpoint_latency_ms"] == pytest.approx(3 * FRAME_S * 1000)


class TestLandingSampleDepartRegion:
    """``depart_region``: a valid sample still inside the window the saccade
    starts from has not left, so it is never the endpoint and never settles —
    but it stays in the speed chain."""

    # A blink at the cue. StimulusResponse reads it as departure (the blink
    # rule) and stamps the onset while the eye is still at fixation; the eye
    # comes back there, holds still (1 px a frame, 1.5 deg/s), and only then
    # makes the real saccade to the target at 400 px.
    BLINK_AT_THE_CUE = [
        LAUNCH,
        BLINK,
        at(2.0, 2 * FRAME_S),
        at(3.0, 3 * FRAME_S),
        at(4.0, 4 * FRAME_S),
        at(200.0, 5 * FRAME_S),
        at(390.0, 6 * FRAME_S),
        at(400.0, 7 * FRAME_S),
    ]

    def test_without_it_a_blink_at_the_cue_settles_at_fixation(self):
        # The failure the option exists for — and the default, unchanged: the
        # first slow sample after the blink "settles" at fixation, and the
        # trial ends as a miss there.
        _harness, result = run([onset(), offset_phase()], self.BLINK_AT_THE_CUE)
        assert result.outcome is MISS
        assert result.record["endpoint_settled"] is True
        assert result.record["endpoint_x_dva"] == pytest.approx(3.0 / 40.0)

    def test_with_it_the_phase_waits_for_the_real_saccade(self):
        harness, result = run(
            [onset(), offset_phase(depart_region="fixation")], self.BLINK_AT_THE_CUE
        )
        assert result.outcome is HIT
        assert result.record["endpoint_measured"] is True
        assert result.record["endpoint_settled"] is True
        assert result.record["endpoint_x_dva"] == pytest.approx(10.0)
        # Settled on the sample taken at 7 frame periods, 6 after the onset
        # flip — not on any of the still samples at fixation before it.
        assert result.record["endpoint_latency_ms"] == pytest.approx(6 * FRAME_S * 1000)
        assert "LANDED" in harness.collector.names()

    def test_an_eye_that_never_leaves_before_the_cap_is_not_measured(self):
        # Still at fixation throughout: no landing to record, rather than a
        # landing at fixation. Settled False, measured False, a miss.
        still = [at(2.0 + k, k * FRAME_S) for k in range(1, 10)]
        harness, result = run(
            [onset(), offset_phase(depart_region="fixation", max_wait_s=3.5 * FRAME_S)],
            [LAUNCH, *still],
        )
        assert result.outcome is MISS
        assert result.record["endpoint_measured"] is False
        assert result.record["endpoint_settled"] is False
        assert result.record["endpoint_in_target"] is False
        for column in ("x_dva", "y_dva", "error_dva", "latency_ms"):
            assert f"endpoint_{column}" not in result.record
        assert "LANDED" not in harness.collector.names()

    def test_the_last_sample_inside_times_the_first_one_outside(self):
        # The speed chain runs through the departure window: the last sample
        # inside it is the previous sample for the first one outside. An eye
        # drifting slowly across the edge (4 px a frame, 6 deg/s) has left and
        # is at rest, so it settles on its first sample outside, at 42 px —
        # it is not excused for having no predecessor and carried on to the
        # samples at 400 px that follow.
        _harness, result = run(
            [onset(), offset_phase(depart_region="fixation")],
            [
                LAUNCH,
                at(34.0, FRAME_S),
                at(38.0, 2 * FRAME_S),
                at(42.0, 3 * FRAME_S),
                at(400.0, 4 * FRAME_S),
                at(400.0, 5 * FRAME_S),
            ],
        )
        assert result.outcome is MISS
        assert result.record["endpoint_settled"] is True
        assert result.record["endpoint_x_dva"] == pytest.approx(42.0 / 40.0)

    def test_in_a_dwell_the_endpoint_is_the_last_sample_outside(self):
        # Out to the target, then back to fixation before the dwell ends. The
        # samples back inside have not landed anywhere: the endpoint is the
        # last one outside, on the target.
        harness, result = run(
            [onset(), dwell_phase(depart_region="fixation")],
            [
                LAUNCH,
                at(390.0, FRAME_S),
                at(400.0, 2 * FRAME_S),
                at(5.0, 3 * FRAME_S),
                at(2.0, 4 * FRAME_S),
            ],
        )
        assert result.outcome is HIT
        assert result.record["endpoint_x_dva"] == pytest.approx(10.0)
        assert result.record["endpoint_latency_ms"] == pytest.approx(FRAME_S * 1000)
        assert "LANDED" in harness.collector.names()

    def test_in_a_dwell_an_eye_that_never_left_is_not_measured(self):
        harness, result = run(
            [onset(), dwell_phase(depart_region="fixation")],
            [LAUNCH, *(at(1.0 + k, k * FRAME_S) for k in range(1, 5))],
        )
        assert result.outcome is MISS
        assert result.record["endpoint_measured"] is False
        assert "endpoint_x_dva" not in result.record
        assert "LANDED" not in harness.collector.names()

    def test_where_the_windows_overlap_the_eye_has_not_left(self):
        # A target near enough that its window overlaps the fixation window
        # (centre 90 px, radius 60: the overlap runs from 30 to 40 px). An eye
        # resting at 35 px is inside the target's window, but it has not
        # left fixation, so it is not a landing.
        regions = {"fixation": FIX, "target": CircleRegion((90.0, 0.0), 60.0)}
        _harness, result = run(
            [onset(), dwell_phase(depart_region="fixation")],
            [LAUNCH, *(at(35.0, k * FRAME_S) for k in range(1, 5))],
            regions=regions,
        )
        assert result.record["endpoint_measured"] is False
        assert result.outcome is MISS

    def test_an_unknown_region_is_named_beside_the_ones_there_are(self):
        with pytest.raises(ValueError, match=r"'fixaton'.*\['fixation', 'target'\]"):
            run([onset(), dwell_phase(depart_region="fixaton")], [LAUNCH, ON_TARGET])

    def test_a_departure_window_around_the_target_centre_is_refused(self):
        # A target whose centre is inside the fixation window: an eye landing
        # exactly on it would count as never having left, so it could never
        # be a hit. Refused when the trial starts, not discovered in the data.
        regions = {
            "fixation": CircleRegion((0.0, 0.0), 120.0),
            "target": CircleRegion((100.0, 0.0), 60.0),
        }
        with pytest.raises(ValueError, match="inside depart_region 'fixation'"):
            run(
                [onset(), dwell_phase(depart_region="fixation")],
                [LAUNCH, ON_TARGET],
                regions=regions,
            )

    def test_so_is_one_around_a_fixed_reference(self):
        with pytest.raises(ValueError, match="inside depart_region 'fixation'"):
            run(
                [onset(), dwell_phase(depart_region="fixation", reference=(10.0, 0.0))],
                [LAUNCH, ON_TARGET],
            )

    def test_a_moving_reference_is_still_read_only_at_the_landing(self):
        # Where a moving figure will be cannot be checked when the phase
        # starts, and the figure is not asked early: it is read once, on the
        # frame the landing is judged.
        seen = []

        def figure(ctx):
            seen.append(ctx.clock.now())
            return (400.0, 0.0)

        _harness, result = run(
            [onset(), dwell_phase(depart_region="fixation", reference=figure)],
            [LAUNCH, at(400.0, FRAME_S)],
        )
        assert result.outcome is HIT
        assert seen == [pytest.approx(4 * FRAME_S)]


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

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            (dict(on_hit=HIT, on_miss=None, dwell_s=0.2), "on_hit and on_miss"),
            (dict(on_hit=PhaseAction.CONTINUE, on_miss=MISS, dwell_s=0.2), "on_hit is"),
            (dict(on_hit=HIT, on_miss=MISS), "exactly one of dwell_s"),
            (
                dict(on_hit=HIT, on_miss=MISS, dwell_s=0.2, settle_speed_dva_per_s=30.0),
                "exactly one of dwell_s",
            ),
            (dict(on_hit=HIT, on_miss=MISS, dwell_s=0.0), "dwell_s must be > 0"),
            (dict(on_hit=HIT, on_miss=MISS, dwell_s=0.2, max_wait_s=0.5), "max_wait_s caps"),
            (dict(on_hit=HIT, on_miss=MISS, settle_speed_dva_per_s=30.0), "needs max_wait_s"),
            (
                dict(on_hit=HIT, on_miss=MISS, settle_speed_dva_per_s=0.0, max_wait_s=0.5),
                "settle_speed_dva_per_s must be > 0",
            ),
            (
                dict(on_hit=HIT, on_miss=MISS, settle_speed_dva_per_s=30.0, max_wait_s=0.0),
                "needs max_wait_s",
            ),
            # The window the eye leaves named as the one it should land in:
            # every sample on the target would be "not left yet".
            (
                dict(on_hit=HIT, on_miss=MISS, dwell_s=0.2, depart_region="target"),
                "is the target region itself",
            ),
        ],
    )
    def test_landing_sample_refuses_an_ambiguous_or_endless_setup(self, kwargs, match):
        # Refused at build time, naming the fix, rather than hanging or
        # guessing mid-trial with a subject in the rig.
        with pytest.raises(ValueError, match=match):
            LandingSample(**kwargs)

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            # CONTINUE as a verdict never ends the phase on a hit: it loops
            # until the timeout and then reports a miss — a landing that hit
            # the target recorded as one that did not.
            (dict(on_hit=PhaseAction.CONTINUE, on_miss=MISS), "on_hit is 'CONTINUE'"),
            (dict(on_hit=HIT, on_miss=PhaseAction.CONTINUE), "on_miss is 'CONTINUE'"),
            # A typo for ADVANCE would only fail on the frame the engine read it.
            (dict(on_hit="ADVNCE", on_miss=MISS), "on_hit is 'ADVNCE'"),
        ],
    )
    def test_landing_check_refuses_a_verdict_that_cannot_end_it(self, kwargs, match):
        with pytest.raises(ValueError, match=f"LandingCheck needs both .*{match}"):
            LandingCheck(**kwargs)

    def test_landing_check_still_takes_advance_as_a_verdict(self):
        # ADVANCE lets a following phase (TrialFeedback) end the trial.
        LandingCheck(on_hit=PhaseAction.ADVANCE, on_miss=PhaseAction.ADVANCE)


_DONE = Outcome("DONE", completed=True, success=True)


class _EndPhase:
    """Ends the trial the frame after the phase under test advanced."""

    name = "end"

    def on_enter(self, ctx):
        return

    def on_frame(self, ctx):
        return _DONE
