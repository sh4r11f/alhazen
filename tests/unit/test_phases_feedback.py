"""Trial feedback: the verdict is the task's and is not the outcome, and
feedback is never on screen while something is being measured."""

from __future__ import annotations

import logging
import sys
import types

import pytest

from alhazen.core.events import Event
from alhazen.core.trial import Outcome, PhaseAction
from alhazen.session.feedback import FAILURE_TONE_HZ, SUCCESS_TONE_HZ, FeedbackSounder
from alhazen.stimuli.base import NullStimulus
from alhazen.task.phases import LandingCheck, TrialFeedback
from alhazen.task.phases.simple import FAILURE_COLOR, SUCCESS_COLOR
from support import COMPLETED, FRAME_S, EngineHarness, RunForFrames

MISSED = Outcome("MISSED", completed=True, success=False)


def run_with_feedback(verdict, then=COMPLETED, duration_s=2 * FRAME_S, **kwargs):
    harness = EngineHarness()
    fixation = NullStimulus("fixation")
    ctx = harness.ctx(stimuli={"fixation": fixation})
    phases = [
        RunForFrames(1, PhaseAction.ADVANCE),
        TrialFeedback(verdict=verdict, then=then, duration_s=duration_s, **kwargs),
    ]
    result = harness.engine.run_trial(ctx, phases)
    return result, fixation, harness


class TestTheVerdictIsNotTheOutcome:
    def test_a_good_trial_goes_green_and_says_so(self):
        result, fixation, harness = run_with_feedback(lambda ctx: True)

        assert result.record["feedback"] == "success"
        assert fixation.colors == [SUCCESS_COLOR]
        assert result.outcome is COMPLETED
        (event,) = [e for e in harness.collector.events if e.name == "FEEDBACK"]
        assert event.payload == {"success": True}
        # On the flip that showed it, not when it was decided: the first
        # phase takes two flips, and the third is the one with the colour on.
        assert event.t == pytest.approx(3 * FRAME_S)

    def test_a_missed_trial_is_told_so_and_stays_completed(self):
        """The design's own rule: a saccade that missed is still a
        measurement. The subject hears it was not good enough; the scheduler
        hears the trial completed, and does not re-serve it."""
        result, fixation, _ = run_with_feedback(
            lambda ctx: ctx.record.get("endpoint_in_target", False), then=MISSED
        )

        assert result.record["feedback"] == "failure"
        assert fixation.colors == [FAILURE_COLOR]
        assert result.outcome is MISSED
        assert result.record["completed"] is True

    def test_the_outcome_may_be_decided_from_the_record(self):
        def outcome(ctx):
            return COMPLETED if ctx.record["feedback"] == "success" else MISSED

        result, _, _ = run_with_feedback(lambda ctx: False, then=outcome)
        assert result.outcome is MISSED

    def test_it_stays_up_for_at_least_the_duration_then_ends(self):
        # Two and a half frames: the phase ends on the first flip at or past
        # the duration, so the colour is up for three frames, plus one more
        # draw on the frame that returns. Two flips for the first phase, four
        # feedback flips, and the engine's blanking flip.
        result, fixation, harness = run_with_feedback(lambda ctx: True, duration_s=2.5 * FRAME_S)
        assert fixation.draw_count == 4
        assert harness.display.flip_count == 7
        assert result.outcome is COMPLETED

    def test_a_stimulus_that_cannot_change_colour_is_refused_loudly(self):
        harness = EngineHarness()
        ctx = harness.ctx(stimuli={"fixation": object()})
        with pytest.raises(TypeError, match="has no set_color"):
            harness.engine.run_trial(
                ctx, [TrialFeedback(verdict=lambda c: True, then=COMPLETED, duration_s=0.0)]
            )

    def test_a_negative_duration_is_refused(self):
        with pytest.raises(ValueError, match="duration must be >= 0"):
            TrialFeedback(verdict=lambda c: True, then=COMPLETED, duration_s=-0.1)


class TestFeedbackOnTheTrialsThatEndedEarly:
    """The half that was unreachable. `TrialFeedback` is `must_be_last`, and
    the engine used to return as soon as any phase produced an Outcome — so a
    fixation break or a saccade that never came ended the trial before the
    feedback phase was ever entered. A run came back with 72 completed trials
    all showing feedback and 7 failures showing none, which is the opposite
    of what feedback is for."""

    FIX_BREAK = Outcome("FIX_BREAK", completed=False)

    def run_ending_in(self, outcome, verdict=lambda ctx: True):
        harness = EngineHarness()
        fixation = NullStimulus("fixation")
        ctx = harness.ctx(stimuli={"fixation": fixation})
        phases = [
            RunForFrames(1, outcome),
            TrialFeedback(verdict=verdict, then=COMPLETED, duration_s=2 * FRAME_S),
        ]
        return harness.engine.run_trial(ctx, phases), fixation, harness

    def test_a_fixation_break_is_shown_as_a_failure(self):
        result, fixation, harness = self.run_ending_in(self.FIX_BREAK)

        assert fixation.colors == [FAILURE_COLOR]
        assert result.record["feedback"] == "failure"
        (event,) = [e for e in harness.collector.events if e.name == "FEEDBACK"]
        assert event.payload == {"success": False}

    def test_the_outcome_the_trial_already_had_is_kept(self):
        """Feedback closes a trial out; it does not turn a fixation break
        into a completed one. `then` is discarded here."""
        result, _fixation, _ = self.run_ending_in(self.FIX_BREAK)

        assert result.outcome is self.FIX_BREAK
        assert result.record["outcome"] == "FIX_BREAK"
        assert result.record["completed"] is False

    def test_the_tasks_verdict_is_not_asked_about_a_trial_that_has_none(self):
        """The predicate judges a measurement. On a fixation break there is
        no measurement, so asking it would be asking about nothing — and a
        predicate that answers True by default would show a green point for
        a trial the subject broke."""
        asked = []

        def verdict(ctx):
            asked.append(ctx.trial_index)
            return True

        result, fixation, _ = self.run_ending_in(self.FIX_BREAK, verdict=verdict)

        assert asked == []
        assert fixation.colors == [FAILURE_COLOR]
        assert result.record["feedback"] == "failure"

    def test_a_completed_outcome_from_an_earlier_phase_still_gets_the_verdict(self):
        """Ending early is not the same as failing: a task whose response
        phase ends the trial with its own completed outcome still has a
        measurement, and the predicate still judges it."""
        result, fixation, _ = self.run_ending_in(MISSED, verdict=lambda ctx: True)

        assert fixation.colors == [SUCCESS_COLOR]
        assert result.record["feedback"] == "success"
        assert result.outcome is MISSED

    def test_a_pause_shows_nothing(self):
        """PAUSED is not a trial result — somebody pressed P. Telling the
        subject they failed a trial they were still in the middle of would be
        a lie, and the pause menu is about to cover the screen anyway."""
        from alhazen.core.commands import Command
        from alhazen.testing import ScriptedCommands

        harness = EngineHarness(commands=ScriptedCommands([[], [Command.PAUSE]]))
        fixation = NullStimulus("fixation")
        ctx = harness.ctx(stimuli={"fixation": fixation})
        result = harness.engine.run_trial(
            ctx,
            [
                RunForFrames(5, COMPLETED),
                TrialFeedback(verdict=lambda ctx: True, then=COMPLETED, duration_s=2 * FRAME_S),
            ],
        )

        assert result.outcome.name == "PAUSED"
        assert fixation.colors == []
        assert "feedback" not in result.record
        assert not [e for e in harness.collector.events if e.name == "FEEDBACK"]


class TestFeedbackIsNeverOnScreenDuringAMeasurement:
    def test_the_engine_refuses_feedback_anywhere_but_last(self):
        harness = EngineHarness()
        ctx = harness.ctx(stimuli={"fixation": NullStimulus("fixation")})
        phases = [
            TrialFeedback(verdict=lambda c: True, then=PhaseAction.ADVANCE, duration_s=0.0),
            RunForFrames(1, COMPLETED),
        ]
        with pytest.raises(RuntimeError, match="must be the trial's last phase"):
            harness.engine.run_trial(ctx, phases)
        # Refused before a frame was drawn: no coloured dot ever reached the
        # screen, and no FEEDBACK went out.
        assert harness.display.flip_count == 0
        assert "FEEDBACK" not in harness.collector.names()

    def test_a_landing_check_can_hand_over_instead_of_ending_the_trial(self):
        """The measuring phase records where the eye landed and advances;
        feedback reads the record and ends the trial. Same numbers on the
        row as before; the only difference is who returns the outcome."""
        from alhazen.core.trial import CircleRegion, InputFrame
        from alhazen.testing import ScriptedInputs

        inputs = ScriptedInputs([InputFrame(gaze=(0.0, 0.0)), InputFrame(gaze=(100.0, 0.0))])
        harness = EngineHarness(input_provider=inputs)
        fixation = NullStimulus("fixation")
        ctx = harness.ctx(
            stimuli={"fixation": fixation, "target": NullStimulus("target")},
            regions={"target": CircleRegion(center=(100.0, 0.0), radius=20.0)},
        )
        phases = [
            LandingCheck(
                region="target",
                timeout_s=1.0,
                on_hit=PhaseAction.ADVANCE,
                on_miss=PhaseAction.ADVANCE,
                landed_event=None,
            ),
            TrialFeedback(
                verdict=lambda c: c.record["endpoint_in_target"],
                then=lambda c: COMPLETED if c.record["endpoint_in_target"] else MISSED,
                duration_s=0.0,
            ),
        ]
        result = harness.engine.run_trial(ctx, phases)

        assert result.record["endpoint_in_target"] is True
        assert result.record["feedback"] == "success"
        assert result.outcome is COMPLETED
        assert fixation.colors == [SUCCESS_COLOR]

    def test_a_landing_check_still_needs_both_answers(self):
        with pytest.raises(ValueError, match="needs both on_hit and on_miss"):
            LandingCheck(on_hit=PhaseAction.ADVANCE)


class FakeSound:
    built: list[tuple[float, float]] = []
    plays: list[float] = []
    fail_build = False
    fail_play = False

    def __init__(self, value, secs):
        if FakeSound.fail_build:
            raise RuntimeError("no audio device")
        self.value = value
        FakeSound.built.append((value, secs))

    def play(self):
        if FakeSound.fail_play:
            raise RuntimeError("stream closed")
        FakeSound.plays.append(self.value)


@pytest.fixture
def fake_sound(monkeypatch):
    FakeSound.built, FakeSound.plays = [], []
    FakeSound.fail_build = FakeSound.fail_play = False
    module = types.ModuleType("psychopy.sound")
    module.Sound = FakeSound
    package = types.ModuleType("psychopy")
    package.sound = module
    monkeypatch.setitem(sys.modules, "psychopy", package)
    monkeypatch.setitem(sys.modules, "psychopy.sound", module)
    return FakeSound


def feedback(success: bool) -> Event:
    return Event(name="FEEDBACK", t=1.0, trial_index=1, payload={"success": success})


class TestTheSounder:
    def test_it_plays_a_high_tone_for_success_and_a_low_one_for_failure(self, fake_sound):
        sounder = FeedbackSounder(types.SimpleNamespace(kind="psychopy"))
        # Both sounds are built at construction, not inside the frame loop.
        assert sorted(value for value, _ in fake_sound.built) == sorted(
            [FAILURE_TONE_HZ, SUCCESS_TONE_HZ]
        )
        sounder(feedback(True))
        sounder(feedback(False))
        sounder(Event(name="TRIAL_END", t=2.0, trial_index=1, payload={}))
        assert fake_sound.plays == [SUCCESS_TONE_HZ, FAILURE_TONE_HZ]
        assert sounder.played == [True, False]

    def test_a_simulated_display_records_but_stays_silent(self, fake_sound):
        sounder = FeedbackSounder(types.SimpleNamespace(kind="simulated"))
        sounder(feedback(True))
        assert sounder.played == [True]
        assert fake_sound.built == [] and fake_sound.plays == []

    def test_audio_that_fails_is_said_once_and_the_session_goes_on(self, fake_sound, caplog):
        fake_sound.fail_build = True
        with caplog.at_level(logging.WARNING, logger="alhazen.session.feedback"):
            sounder = FeedbackSounder(types.SimpleNamespace(kind="psychopy"))
            sounder(feedback(True))
            sounder(feedback(False))
        warnings = [r for r in caplog.records if "feedback tones are off" in r.getMessage()]
        assert len(warnings) == 1
        assert sounder.played == [True, False]
