"""TrialEngine invariants: flip-locked timestamps, dt semantics, command
handling, health checks, frame QA integration, and loud contract failures."""

from __future__ import annotations

import pytest

from alhazen.config.models import FrameQAConfig, RewardPulses
from alhazen.core.commands import Command
from alhazen.core.engine import QuitRequested
from alhazen.core.trial import InputFrame, PhaseAction
from alhazen.devices.reward import SimulatedReward
from alhazen.errors import FrameQAError
from alhazen.testing import ScriptedCommands, ScriptedInputs
from support import COMPLETED, FAILED, FRAME_S, EngineHarness, RunForFrames


def events_named(harness, name):
    return [e for e in harness.collector.events if e.name == name]


class TestEventTiming:
    def test_trial_start_emitted_immediately_before_any_flip(self):
        harness = EngineHarness()
        harness.engine.run_trial(harness.ctx(), [RunForFrames(0, COMPLETED)])
        (start,) = events_named(harness, "TRIAL_START")
        assert start.t == 0.0  # clock had not advanced: no flip had happened yet
        assert start.payload == {"condition": "test"}

    def test_visual_events_stamped_after_their_flip(self):
        harness = EngineHarness()
        phase = RunForFrames(2, COMPLETED, emit_on_enter="FIX_ON")
        harness.engine.run_trial(harness.ctx(), [phase])
        (fix_on,) = events_named(harness, "FIX_ON")
        # Queued in on_enter, emitted only after the first flip advanced the
        # fake clock by one frame period.
        assert fix_on.t == pytest.approx(FRAME_S)

    def test_timestamps_mirrored_into_record(self):
        harness = EngineHarness()
        result = harness.engine.run_trial(
            harness.ctx(), [RunForFrames(1, COMPLETED, emit_on_enter="STIM_ON")]
        )
        assert result.record["t_trial_start"] == 0.0
        assert result.record["t_stim_on"] == pytest.approx(FRAME_S)
        assert "t_trial_end" in result.record

    def test_trial_end_carries_outcome_and_completed(self):
        harness = EngineHarness()
        harness.engine.run_trial(harness.ctx(), [RunForFrames(0, COMPLETED)])
        (end,) = events_named(harness, "TRIAL_END")
        assert end.payload == {"outcome": "COMPLETED", "completed": True}

    def test_the_record_carries_the_completed_flag(self):
        """Incomplete outcomes DO write rows, and the row said nothing about
        whether the trial completed — the flag existed only inside a
        TRIAL_END payload, where the trials table cannot see it. Downstream
        (the report, the dashboard) was left guessing from the outcome name."""
        harness = EngineHarness()

        completed = harness.engine.run_trial(harness.ctx(), [RunForFrames(0, COMPLETED)])
        broken = harness.engine.run_trial(harness.ctx(2), [RunForFrames(0, FAILED)])

        assert completed.record["completed"] is True
        assert broken.record["completed"] is False

    def test_undeclared_event_fails_loudly(self):
        harness = EngineHarness(declared_events=())
        with pytest.raises(ValueError, match="never declared"):
            harness.engine.run_trial(
                harness.ctx(), [RunForFrames(1, COMPLETED, emit_on_enter="STIM_ON")]
            )


class TestFrameLoop:
    def test_dt_measures_previous_frame(self):
        harness = EngineHarness()
        phase = RunForFrames(3, COMPLETED)
        harness.engine.run_trial(harness.ctx(), [phase])
        # First on_frame sees the default dt; later ones see the measured
        # frame period from the fake display's clock advance.
        assert phase.frames_seen[1] == pytest.approx(FRAME_S)
        assert phase.frames_seen[2] == pytest.approx(FRAME_S)

    def test_final_blank_flip_after_terminal_frame(self):
        harness = EngineHarness()
        harness.engine.run_trial(harness.ctx(), [RunForFrames(2, COMPLETED)])
        # 3 phase flips (2 CONTINUE + 1 terminal) + 1 blanking flip.
        assert harness.display.flip_count == 4

    def test_phases_advance_in_sequence(self):
        harness = EngineHarness()
        first = RunForFrames(1, PhaseAction.ADVANCE)
        second = RunForFrames(1, COMPLETED)
        result = harness.engine.run_trial(harness.ctx(), [first, second])
        assert result.outcome is COMPLETED
        assert len(first.frames_seen) == 2
        assert len(second.frames_seen) == 2

    def test_missing_terminal_outcome_is_a_programming_error(self):
        harness = EngineHarness()
        with pytest.raises(RuntimeError, match="Outcome"):
            harness.engine.run_trial(harness.ctx(), [RunForFrames(0, PhaseAction.ADVANCE)])

    def test_bad_phase_return_is_a_type_error(self):
        harness = EngineHarness()
        with pytest.raises(TypeError, match="expected PhaseAction"):
            harness.engine.run_trial(harness.ctx(), [RunForFrames(0, "DONE")])

    def test_inputs_snapshotted_each_frame(self):
        frames = [InputFrame(gaze=(0.0, 0.0)), InputFrame(gaze=(5.0, 5.0))]
        harness = EngineHarness(input_provider=ScriptedInputs(frames))
        seen = []

        class Watch(RunForFrames):
            def on_frame(self, ctx):
                seen.append(ctx.inputs.gaze)
                return super().on_frame(ctx)

        harness.engine.run_trial(harness.ctx(), [Watch(1, COMPLETED)])
        assert seen == [(0.0, 0.0), (5.0, 5.0)]


class TestCommands:
    def test_skip_aborts_with_reason(self):
        harness = EngineHarness(commands=ScriptedCommands([[Command.SKIP_TRIAL]]))
        result = harness.engine.run_trial(harness.ctx(), [RunForFrames(10, COMPLETED)])
        assert result.outcome.name == "ABORTED"
        assert result.record["abort_reason"] == "skipped_by_user"

    def test_pause_returns_paused_and_emits(self):
        harness = EngineHarness(commands=ScriptedCommands([[Command.PAUSE]]))
        result = harness.engine.run_trial(harness.ctx(), [RunForFrames(10, COMPLETED)])
        assert result.outcome.name == "PAUSED"
        assert len(events_named(harness, "PAUSED")) == 1

    def test_calibrate_is_pause_plus_action(self):
        harness = EngineHarness(commands=ScriptedCommands([[Command.CALIBRATE]]))
        result = harness.engine.run_trial(harness.ctx(), [RunForFrames(10, COMPLETED)])
        assert result.outcome.name == "PAUSED"
        assert result.record["pause_action"] == "calibrate"

    def test_quit_raises(self):
        harness = EngineHarness(commands=ScriptedCommands([[Command.QUIT]]))
        with pytest.raises(QuitRequested):
            harness.engine.run_trial(harness.ctx(), [RunForFrames(10, COMPLETED)])

    def test_manual_reward_does_not_end_trial(self):
        harness = EngineHarness(commands=ScriptedCommands([[Command.MANUAL_REWARD]]))
        result = harness.engine.run_trial(harness.ctx(), [RunForFrames(2, COMPLETED)])
        assert result.outcome is COMPLETED
        (reward,) = events_named(harness, "REWARD")
        assert reward.payload == {"manual": True}

    def test_manual_reward_delivers_and_then_records(self):
        # Both halves matter: the hook reaches the rig's dispenser, and the
        # event is the permanent record of a delivery that has already
        # happened — an event claiming a reward the pump never gave would be
        # a lie in the data.
        reward = SimulatedReward()
        harness = EngineHarness(
            commands=ScriptedCommands([[Command.MANUAL_REWARD]]),
            on_manual_reward=lambda: reward.deliver(RewardPulses(n_pulses=1, pulse_ms=100)),
        )
        harness.engine.run_trial(harness.ctx(), [RunForFrames(1, COMPLETED)])
        assert reward.deliveries == [RewardPulses(n_pulses=1, pulse_ms=100)]
        (event,) = events_named(harness, "REWARD")
        assert event.payload == {"manual": True}

    def test_no_reward_hook_still_records_the_event(self):
        # A rig with no reward line: the experimenter's key still leaves its
        # mark in the data, it just does not drive anything.
        harness = EngineHarness(commands=ScriptedCommands([[Command.MANUAL_REWARD]]))
        harness.engine.run_trial(harness.ctx(), [RunForFrames(1, COMPLETED)])
        assert len(events_named(harness, "REWARD")) == 1


class TestOverlay:
    def test_overlay_runs_every_frame_before_the_flip(self):
        seen: list[tuple[int, list[str]]] = []
        harness = EngineHarness(
            overlay=lambda ctx: seen.append(
                (harness.display.flip_count, [name for name, _ in ctx.pending_flip_events])
            )
        )
        harness.engine.run_trial(
            harness.ctx(), [RunForFrames(2, COMPLETED, emit_on_enter="FIX_ON")]
        )
        # Three phase frames, each observed before its own flip.
        assert [flips for flips, _ in seen] == [0, 1, 2]
        # And it sees what that frame queued, which is what lets the
        # photodiode patch mark the flip the event's timestamp refers to.
        assert [queued for _, queued in seen] == [["FIX_ON"], [], []]

    def test_no_overlay_is_the_default(self):
        harness = EngineHarness()
        result = harness.engine.run_trial(harness.ctx(), [RunForFrames(1, COMPLETED)])
        assert result.outcome is COMPLETED


class TestHealthChecks:
    def test_failing_check_aborts_with_its_reason(self):
        harness = EngineHarness(health_checks=(lambda: "tracker_stopped",))
        result = harness.engine.run_trial(harness.ctx(), [RunForFrames(10, COMPLETED)])
        assert result.outcome.name == "ABORTED"
        assert result.record["abort_reason"] == "tracker_stopped"


class TestFrameQAIntegration:
    def test_dropped_frame_marks_trial_under_mark_policy(self):
        cfg = FrameQAConfig(policy="mark_trial", tolerance=0.5)
        harness = EngineHarness(frame_qa=cfg)

        class DropOne(RunForFrames):
            def on_frame(self, ctx):
                if len(self.frames_seen) == 1:
                    # Make the NEXT flip take two frame periods.
                    harness.display.next_flip_extra = FRAME_S
                return super().on_frame(ctx)

        result = harness.engine.run_trial(harness.ctx(), [DropOne(3, COMPLETED)])
        assert result.record["n_dropped_frames"] == 1

    def test_abort_run_policy_raises_past_budget(self):
        cfg = FrameQAConfig(policy="abort_run", tolerance=0.5, max_dropped_per_trial=0)
        harness = EngineHarness(frame_qa=cfg)

        class AlwaysSlow(RunForFrames):
            def on_frame(self, ctx):
                harness.display.next_flip_extra = FRAME_S
                return super().on_frame(ctx)

        with pytest.raises(FrameQAError):
            harness.engine.run_trial(harness.ctx(), [AlwaysSlow(10, COMPLETED)])

    def test_warn_policy_never_marks_or_raises(self):
        cfg = FrameQAConfig(policy="warn", tolerance=0.5)
        harness = EngineHarness(frame_qa=cfg)

        class AlwaysSlow(RunForFrames):
            def on_frame(self, ctx):
                harness.display.next_flip_extra = FRAME_S
                return super().on_frame(ctx)

        result = harness.engine.run_trial(harness.ctx(), [AlwaysSlow(3, COMPLETED)])
        assert "n_dropped_frames" not in result.record
