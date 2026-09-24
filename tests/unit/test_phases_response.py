"""Response, adjustment, sequence and the small phases."""

from __future__ import annotations

import pytest

from alhazen.core.trial import InputFrame, Outcome
from alhazen.display.frames import FrameTimeline
from alhazen.task.phases import (
    AdjustmentLoop,
    Blank,
    Feedback,
    FrameSequence,
    ResponseWindow,
)
from alhazen.testing import FakeStimulus, ScriptedInputs
from support import FRAME_S, EngineHarness

LEFT = Outcome("LEFT", completed=True, success=True)
RIGHT = Outcome("RIGHT", completed=True, success=False)
NO_RESPONSE = Outcome("NO_RESPONSE", completed=False)
COMMITTED = Outcome("COMMITTED", completed=True, success=True)
GAVE_UP = Outcome("GAVE_UP", completed=False)
NOTHING = InputFrame()


def press(*keys: str) -> InputFrame:
    return InputFrame(keys=keys)


def run(phases, inputs, declared=("RESPONSE_CUE", "RESPONSE", "STIM_ON"), stimuli=None):
    harness = EngineHarness(input_provider=ScriptedInputs(inputs), declared_events=declared)
    ctx = harness.ctx(stimuli=stimuli or {"target": FakeStimulus("target")})
    return harness, harness.engine.run_trial(ctx, list(phases))


class TestResponseWindow:
    def phase(self, **kwargs):
        defaults = dict(
            keys={"left": LEFT, "right": RIGHT},
            timeout_s=10 * FRAME_S,
            on_timeout=NO_RESPONSE,
        )
        return ResponseWindow(**{**defaults, **kwargs})

    def test_a_bound_key_ends_the_trial_with_its_outcome(self):
        harness, result = run([self.phase()], [NOTHING, press("left")])
        assert result.outcome is LEFT
        assert result.record["response_key"] == "left"
        assert "RESPONSE" in harness.collector.names()

    def test_the_other_key_gives_the_other_outcome(self):
        harness, result = run([self.phase()], [press("right")])
        assert result.outcome is RIGHT

    def test_reaction_time_runs_from_the_cue_flip(self):
        harness, result = run([self.phase()], [NOTHING, NOTHING, press("left")])
        # The cue's photons appear on frame 1's flip; the press is read at
        # the start of frame 3. One frame period of visible cue — measured
        # from the flip, not from the phase's on_enter, which ran a whole
        # frame before anything was on screen.
        assert result.record["rt_ms"] == pytest.approx(FRAME_S * 1000, abs=1.0)

    def test_a_key_read_before_the_cue_flip_is_not_a_response(self):
        # Frame 1's keys are read before that frame's flip, which is the one
        # that shows the cue: whatever they are, they were pressed with
        # nothing on screen to answer. The press is not scored, and no RT is
        # invented for it from the phase's on_enter.
        harness, result = run([self.phase(timeout_s=5 * FRAME_S)], [press("left"), NOTHING])
        assert result.outcome is NO_RESPONSE
        assert "response_key" not in result.record
        assert "rt_ms" not in result.record
        assert "RESPONSE" not in harness.collector.names()

    def test_a_key_pressed_while_the_cue_awaited_its_flip_is_not_a_response(self):
        # Frame 2 is the first frame that sees the cue's stamp, but its keys
        # are everything pressed since frame 1's read — almost all of that
        # interval spent waiting for the very flip that showed the cue. They
        # are the pre-cue queue arriving one frame late, and read as a
        # response they would give a reaction time of zero.
        harness, result = run(
            [self.phase(timeout_s=5 * FRAME_S)], [NOTHING, press("left"), NOTHING]
        )
        assert result.outcome is NO_RESPONSE
        assert "rt_ms" not in result.record

    def test_a_key_after_the_cue_is_scored_from_the_cue_flip_despite_an_earlier_press(self):
        # The subject jumps the gun with "right" on the first two frames, then
        # answers "left" once the cue has been up for a whole frame. The early
        # presses must neither decide the trial nor start the clock.
        harness, result = run(
            [self.phase()], [press("right"), press("right"), press("left"), NOTHING]
        )
        assert result.outcome is LEFT
        assert result.record["response_key"] == "left"
        # Read at the start of frame 3: one frame period after the flip that
        # showed the cue (t_response_cue), two after the phase's on_enter —
        # so one frame, not two, says the clock started at the cue's flip.
        assert result.record["rt_ms"] == pytest.approx(FRAME_S * 1000, abs=1.0)

    def test_without_an_onset_event_keys_count_from_the_first_frame(self):
        # No cue flip to wait for: the task has said the window opens when the
        # phase does, so a key read on the first frame is a response, timed
        # from the phase's on_enter. Unchanged by the wait for the cue.
        harness, result = run([self.phase(onset_event=None)], [press("left"), NOTHING])
        assert result.outcome is LEFT
        assert result.record["rt_ms"] == pytest.approx(0.0, abs=1.0)
        assert "RESPONSE_CUE" not in harness.collector.names()

    def test_unbound_keys_are_not_responses(self):
        # A hand slipping onto an unbound key is not a decision, and counting
        # it as the wrong answer would be a fabricated data point.
        harness, result = run([self.phase()], [press("q", "escape"), press("left")])
        assert result.outcome is LEFT

    def test_no_response_times_out(self):
        harness, result = run([self.phase(timeout_s=3 * FRAME_S)], [NOTHING])
        assert result.outcome is NO_RESPONSE
        assert "response_key" not in result.record

    def test_the_deadline_runs_from_the_cue_flip_not_from_phase_entry(self):
        # The subject has the whole timeout_s with the cue on screen, the same
        # window the reaction time is measured in. The cue's flip is one frame
        # after on_enter; a deadline counted from on_enter would cut that
        # frame off the end, and a key read with the cue up for exactly
        # timeout_s (frame 5, 3 frame periods after the flip) would be scored
        # a timeout.
        timeout_s = 3 * FRAME_S
        harness, result = run(
            [self.phase(timeout_s=timeout_s)],
            [NOTHING, NOTHING, NOTHING, NOTHING, press("left"), NOTHING],
        )
        assert result.outcome is LEFT
        assert result.record["rt_ms"] == pytest.approx(timeout_s * 1000, abs=1.0)

    def test_the_timeout_comes_timeout_s_after_the_cue_flip(self):
        timeout_s = 3 * FRAME_S
        harness, result = run([self.phase(timeout_s=timeout_s)], [NOTHING])
        assert result.outcome is NO_RESPONSE
        # The frame that timed out read the clock at cue + timeout_s; TRIAL_END
        # is stamped two flips later (that frame's own, then the engine's
        # blanking flip).
        after_cue = result.record["t_trial_end"] - result.record["t_response_cue"]
        assert after_cue == pytest.approx(timeout_s + 2 * FRAME_S)

    def test_without_an_onset_event_the_deadline_runs_from_phase_entry(self):
        # No cue flip to count from: the window opened on entry, and so did
        # its deadline. A key on frame 5 is too late — the window closed on
        # frame 4, timeout_s after on_enter.
        harness, result = run(
            [self.phase(timeout_s=3 * FRAME_S, onset_event=None)],
            [NOTHING, NOTHING, NOTHING, NOTHING, press("left"), NOTHING],
        )
        assert result.outcome is NO_RESPONSE

    def test_needs_keys_and_a_timeout_outcome(self):
        with pytest.raises(ValueError, match="at least one key"):
            ResponseWindow(keys={}, on_timeout=NO_RESPONSE)
        with pytest.raises(ValueError, match="on_timeout"):
            ResponseWindow(keys={"left": LEFT}, on_timeout=None)


class TestAdjustmentLoop:
    def phase(self, store, **kwargs):
        defaults = dict(
            adjust=lambda ctx, wheel: store.__setitem__("value", store["value"] + wheel),
            value=lambda ctx: store["value"],
            commit_key="space",
            on_commit=COMMITTED,
        )
        return AdjustmentLoop(**{**defaults, **kwargs})

    def test_wheel_turns_reach_the_task_and_commit_records_the_setting(self):
        store = {"value": 0.0}
        inputs = [
            InputFrame(wheel=1.0),
            InputFrame(wheel=2.0),
            InputFrame(wheel=-0.5),
            press("space"),
        ]
        harness, result = run([self.phase(store)], inputs)
        assert result.outcome is COMMITTED
        assert result.record["adjusted_value"] == pytest.approx(2.5)
        assert result.record["adjustment_turns"] == 3

    def test_timeout_still_records_where_the_subject_had_got_to(self):
        store = {"value": 4.0}
        phase = self.phase(store, timeout_s=3 * FRAME_S, on_timeout=GAVE_UP)
        harness, result = run([phase], [NOTHING])
        assert result.outcome is GAVE_UP
        assert result.record["adjusted_value"] == 4.0

    def test_a_timeout_needs_an_outcome_to_return(self):
        with pytest.raises(ValueError, match="on_timeout"):
            AdjustmentLoop(
                adjust=lambda ctx, w: None,
                value=lambda ctx: 0.0,
                on_commit=COMMITTED,
                timeout_s=1.0,
            )


class TestFrameSequence:
    def timeline(self) -> FrameTimeline:
        return (
            FrameTimeline(5)
            .show("target", 1, 4)
            .ramp("target", "pos", (0.0, 0.0), (40.0, 0.0), 1, 4)
            .event(2, "STIM_ON")
        )

    def test_plays_frame_by_frame_and_lands_events_on_exact_frames(self):
        target = FakeStimulus("target")
        harness, result = run(
            [self.timeline_phase(), _EndPhase()], [NOTHING], stimuli={"target": target}
        )
        (stim_on,) = [e for e in harness.collector.events if e.name == "STIM_ON"]
        # Frame index 2 is the third frame of the phase, so its flip is the
        # third — the event is stamped with that flip's time and no other.
        assert stim_on.t == pytest.approx(3 * FRAME_S)
        assert result.record["sequence_frames"] == 5

    def test_only_shown_stimuli_are_drawn(self):
        target = FakeStimulus("target")
        run([self.timeline_phase(), _EndPhase()], [NOTHING], stimuli={"target": target})
        # Shown on frames 1, 2, 3 of a 5-frame timeline.
        assert target.draw_count == 3

    def test_ramped_attribute_is_applied_to_the_stimulus(self):
        target = FakeStimulus("target")
        run([self.timeline_phase(), _EndPhase()], [NOTHING], stimuli={"target": target})
        # The last setting applied is the ramp's end value.
        assert target.pos == (40.0, 0.0)

    def timeline_phase(self):
        return FrameSequence(self.timeline())


class TestSimplePhases:
    def test_blank_waits_and_advances(self):
        harness, result = run([Blank(3 * FRAME_S), _EndPhase()], [NOTHING])
        assert result.outcome is _DONE
        assert harness.display.flip_count >= 4

    def test_feedback_draws_for_its_duration_and_can_mark_its_onset(self):
        target = FakeStimulus("target")
        harness, result = run(
            [Feedback(["target"], 3 * FRAME_S, onset_event="STIM_ON"), _EndPhase()],
            [NOTHING],
            stimuli={"target": target},
        )
        assert target.draw_count >= 3
        assert "STIM_ON" in harness.collector.names()


_DONE = Outcome("DONE", completed=True, success=True)


class _EndPhase:
    name = "end"

    def on_enter(self, ctx):
        return

    def on_frame(self, ctx):
        return _DONE
