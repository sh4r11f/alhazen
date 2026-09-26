"""TrialEngine invariants: flip-locked timestamps, dt semantics, command
handling, health checks, frame QA integration, and loud contract failures."""

from __future__ import annotations

import pytest

from alhazen.config.models import FrameQAConfig, RewardPulses
from alhazen.core.commands import Command
from alhazen.core.engine import QuitRequested
from alhazen.core.trial import HealthFault, InputFrame, PhaseAction
from alhazen.devices.eyetracker import TrackerMessageSubscriber
from alhazen.devices.reward import SimulatedReward
from alhazen.devices.sync import make_sync_subscriber
from alhazen.errors import FrameQAError
from alhazen.testing import FakeClock, ScriptedCommands, ScriptedInputs
from support import (
    COMPLETED,
    FAILED,
    FRAME_S,
    EngineHarness,
    RunForFrames,
    TickingClock,
    record_flips,
)


def events_named(harness, name):
    return [e for e in harness.collector.events if e.name == name]


class QueueOnFrame(RunForFrames):
    """RunForFrames that queues the given events, in order, on one of its
    frames (0 = its first on_frame call) — a phase that takes the fixation
    point off and puts the target and a cue on in the same frame."""

    name = "queue_on_frame"

    def __init__(self, n_frames: int, then, on_frame: int, names: tuple[str, ...]) -> None:
        super().__init__(n_frames, then)
        self._on_frame = on_frame
        self._names = names

    def on_frame(self, ctx):
        # frames_seen grows by one in RunForFrames.on_frame, so before that
        # call its length is this call's index.
        if len(self.frames_seen) == self._on_frame:
            for name in self._names:
                ctx.emit_on_flip(name)
        return super().on_frame(ctx)


class SlowTracker:
    """An eye tracker whose every message takes ``message_s`` of session
    time — an EyeLink message is a round trip to the Host PC — so the bus
    spends that long in the tracker's subscriber on every event."""

    def __init__(self, clock: FakeClock, message_s: float) -> None:
        self._clock = clock
        self._message_s = message_s
        self.messages: list[str] = []

    def send_message(self, text: str) -> None:
        self.messages.append(text)
        self._clock.advance(self._message_s)


class SlowSync:
    """A sync output whose every pulse takes ``pulse_s`` of session time, as
    a DAQ write does."""

    def __init__(self, clock: FakeClock, pulse_s: float) -> None:
        self._clock = clock
        self._pulse_s = pulse_s
        self.pulses: list[str] = []

    def pulse(self, line: str) -> None:
        self.pulses.append(line)
        self._clock.advance(self._pulse_s)

    def close(self) -> None:
        return  # nothing was opened


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
        (the report, the live monitor) was left guessing from the outcome name."""
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


class TestFlipLockedStamps:
    """Every event a frame queued carries that frame's flip time — however
    many the frame queued, and however long the bus's subscribers took over
    the ones before it.

    Run on a TickingClock: FakeClock holds still between flips, so a stamp
    read at the flip and one read later in the same frame are the same number
    there, and these tests could not fail."""

    def test_every_event_queued_on_one_frame_carries_that_flips_time(self):
        flips: dict[int, float] = {}
        names = ("FIX_OFF", "TARGET_ON", "CUE_ON")
        harness = EngineHarness(
            clock=TickingClock(),
            declared_events=names,
            on_frame_input=record_flips(flips),
            frame_qa=FrameQAConfig(),
        )
        result = harness.engine.run_trial(
            harness.ctx(), [QueueOnFrame(3, COMPLETED, on_frame=1, names=names)]
        )

        # Exactly the flip's own time, all three — not the flip plus however
        # long it took to emit the ones queued before them.
        assert [events_named(harness, name)[0].t for name in names] == [flips[1]] * 3
        # And the record's columns, which a phase reads a reaction time from.
        assert [result.record[f"t_{name.lower()}"] for name in names] == [flips[1]] * 3
        # And the frame log's time for that flip (frames.csv). Its first
        # record is the trial's second flip, frame 1: a trial's first flip
        # only sets the reference an interval is measured from.
        assert harness.frame_monitor is not None
        assert harness.frame_monitor.records[0].t == flips[1]

    def test_a_slow_subscriber_does_not_push_the_next_event_later(self):
        clock = TickingClock()
        flips: dict[int, float] = {}
        harness = EngineHarness(
            clock=clock,
            declared_events=("FIX_OFF", "TARGET_ON"),
            on_frame_input=record_flips(flips),
        )
        # The builder's subscribers, on devices that take time: a tracker
        # message 2 ms, a sync pulse 1 ms, on every event mapped to them.
        tracker = SlowTracker(clock, message_s=0.002)
        sync = SlowSync(clock, pulse_s=0.001)
        harness.bus.subscribe(TrackerMessageSubscriber(tracker))
        harness.bus.subscribe(
            make_sync_subscriber(sync, {"FIX_OFF": "fix_line", "TARGET_ON": "target_line"})
        )
        result = harness.engine.run_trial(
            harness.ctx(),
            [QueueOnFrame(2, COMPLETED, on_frame=0, names=("FIX_OFF", "TARGET_ON"))],
        )

        # Both subscribers did their slow work on FIX_OFF before TARGET_ON
        # was emitted...
        assert tracker.messages[:3] == ["trial_start", "fix_off", "target_on"]
        assert sync.pulses == ["fix_line", "target_line"]
        # ...and TARGET_ON still carries the flip that showed it, not the
        # moment the bus got round to it 3 ms later.
        (fix_off,) = events_named(harness, "FIX_OFF")
        (target_on,) = events_named(harness, "TARGET_ON")
        assert fix_off.t == target_on.t == flips[0]
        assert result.record["t_fix_off"] == result.record["t_target_on"] == flips[0]

    def test_trial_end_is_stamped_when_emitted_not_with_the_last_phase_flip(self):
        # Only what a frame queued carries that frame's flip. TRIAL_END is
        # not queued on a flip: it is emitted after the trial's closing blank
        # flip, and stamped then, like every event the engine emits outside
        # the frame loop.
        flips: dict[int, float] = {}
        harness = EngineHarness(clock=TickingClock(), on_frame_input=record_flips(flips))
        harness.engine.run_trial(harness.ctx(), [RunForFrames(1, COMPLETED)])

        (end,) = events_named(harness, "TRIAL_END")
        # The last phase flip (frame 1), then the blank flip, then TRIAL_END.
        assert end.t > flips[1] + FRAME_S

    def test_the_flip_is_stamped_on_the_engines_clock_not_the_contexts(self):
        # One clock (CONTRIBUTING invariant 2): a context built on another
        # clock must not split the flip's time from its events' times. The
        # runner hands the context the engine's own clock, so the two never
        # differ in a session; this pins which one the engine reads.
        flips: dict[int, float] = {}
        harness = EngineHarness(on_frame_input=record_flips(flips))
        phase = RunForFrames(2, COMPLETED, emit_on_enter="FIX_ON")
        harness.engine.run_trial(harness.ctx(clock=FakeClock(start=100.0)), [phase])

        (fix_on,) = events_named(harness, "FIX_ON")
        assert flips[0] == fix_on.t == pytest.approx(FRAME_S)
        # dt is measured on the same clock: one frame period, not the floor a
        # clock that never moved would give.
        assert phase.frames_seen[1:] == [pytest.approx(FRAME_S)] * 2


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
        # A bare reason is the deprecated shape (see the test below).
        with pytest.warns(DeprecationWarning):
            result = harness.engine.run_trial(harness.ctx(), [RunForFrames(10, COMPLETED)])
        assert result.outcome.name == "ABORTED"
        assert result.record["abort_reason"] == "tracker_stopped"
        # A bare reason says nothing more, so the row gets no detail column.
        assert "fault_detail" not in result.record

    def test_a_health_fault_puts_what_the_device_said_on_the_row(self):
        said = "no new sample from the EyeLink for 57 ms (limit 50 ms); the Host PC ..."
        harness = EngineHarness(health_checks=(lambda: HealthFault("tracker_stopped", said),))
        result = harness.engine.run_trial(harness.ctx(), [RunForFrames(10, COMPLETED)])
        assert result.outcome.name == "ABORTED"
        assert result.record["abort_reason"] == result.record["fault"] == "tracker_stopped"
        assert result.record["fault_detail"] == said
        assert result.lost_to_fault == "tracker_stopped"

    def test_the_first_failing_check_wins(self):
        harness = EngineHarness(
            health_checks=(
                lambda: None,
                lambda: HealthFault("tracker_stopped", "first"),
                lambda: 1 / 0,
            )
        )
        result = harness.engine.run_trial(harness.ctx(), [RunForFrames(10, COMPLETED)])
        assert result.record["fault_detail"] == "first"

    def test_a_healthy_check_is_asked_every_frame(self):
        asked: list[bool] = []

        def check():
            asked.append(True)

        harness = EngineHarness(health_checks=(check,))
        harness.engine.run_trial(harness.ctx(), [RunForFrames(5, COMPLETED)])
        # Five CONTINUE frames and the frame that returns the outcome.
        assert len(asked) == 6

    def test_a_bare_reason_string_still_works_but_is_deprecated(self):
        # The shape every check had in 1.5.0. It keeps working until 2.0
        # (docs/versioning.md §4), and says so, naming what to return instead.
        harness = EngineHarness(health_checks=(lambda: "tracker_stopped",))
        with pytest.warns(DeprecationWarning, match="HealthFault"):
            result = harness.engine.run_trial(harness.ctx(), [RunForFrames(10, COMPLETED)])
        assert result.record["abort_reason"] == result.record["fault"] == "tracker_stopped"


class _MustBeLast(RunForFrames):
    name = "must_be_last"
    must_be_last = True


class TestPhaseOrderIsCheckedFirst:
    def test_a_misplaced_closing_phase_is_refused_before_frame_qa_starts_the_trial(self):
        # The refusal is a task bug met at the trial's start. Frame QA must not
        # have been told a trial began that never ran a frame: it would be
        # left mid-trial, holding a trial index nothing will ever end.
        harness = EngineHarness(frame_qa=FrameQAConfig())
        assert harness.frame_monitor is not None
        started: list[int] = []
        harness.frame_monitor.start_trial = started.append  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="must be the trial's last phase"):
            harness.engine.run_trial(
                harness.ctx(), [_MustBeLast(0, COMPLETED), RunForFrames(0, COMPLETED)]
            )
        assert started == []


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

    def test_a_clean_trial_writes_zero_not_nothing(self):
        """An absent cell reads back as NaN, and NaN is not "no drops": it
        inflated a column mean by a third and made astype(int) raise on the
        rig's own data. Zero is a number; absence is not."""
        for policy in ("mark_trial", "recycle_trial", "abort_run"):
            harness = EngineHarness(frame_qa=FrameQAConfig(policy=policy))
            result = harness.engine.run_trial(harness.ctx(), [RunForFrames(3, COMPLETED)])
            assert result.record["n_dropped_frames"] == 0

    def _run_with_drops(self, n_frames, drop_frames, outcome, **cfg):
        harness = EngineHarness(frame_qa=FrameQAConfig(policy="recycle_trial", **cfg))

        class DropSome(RunForFrames):
            def on_frame(self, ctx):
                if len(self.frames_seen) in drop_frames:
                    harness.display.next_flip_extra = FRAME_S
                return super().on_frame(ctx)

        return harness.engine.run_trial(harness.ctx(), [DropSome(n_frames, outcome)]), harness

    def test_recycle_trial_discards_a_trial_that_dropped_too_many_frames(self):
        """The trial finishes, but its measurement is not one: the outcome
        becomes DROPPED_FRAMES (completed=False, so the scheduler re-serves
        the condition) and what it would have been stays on the row."""
        result, harness = self._run_with_drops(9, {1, 2, 3}, COMPLETED, max_dropped_fraction=0.2)
        assert result.outcome.name == "DROPPED_FRAMES"
        assert result.outcome.completed is False
        assert result.record["outcome"] == "DROPPED_FRAMES"
        assert result.record["completed"] is False
        assert result.record["outcome_before_frame_qa"] == "COMPLETED"
        assert result.record["n_dropped_frames"] == 3
        # Ten flips are nine measured intervals: the first only sets the reference.
        assert "3 of 9 frames dropped (33.3%)" in result.record["frame_qa_reason"]
        (end,) = events_named(harness, "TRIAL_END")
        assert end.payload == {"outcome": "DROPPED_FRAMES", "completed": False}

    def test_the_recorded_reason_reads_over_the_budget_it_names(self):
        """21 of 209 is 10.05%, over the shipped 10% budget. The row used to
        say "(10.0%), over the 10% budget": a trial apparently recycled for
        sitting exactly on its budget."""
        result, _ = self._run_with_drops(
            209, set(range(1, 22)), COMPLETED, max_dropped_fraction=0.1
        )
        assert result.record["n_dropped_frames"] == 21
        assert result.record["frame_qa_reason"] == (
            "21 of 209 frames dropped (10.05%), over the 10% budget (frame_qa.max_dropped_fraction)"
        )

    def test_a_recycled_result_carries_the_outcome_the_response_earned(self):
        """The scheduler sees DROPPED_FRAMES; the reward path needs the Outcome
        the subject's response ended as — the object, because NO_REWARD turns
        on its `completed` flag and the runner has no outcome set to look a
        name up in."""
        result, _ = self._run_with_drops(9, {1, 2, 3}, COMPLETED, max_dropped_fraction=0.2)
        assert result.outcome.name == "DROPPED_FRAMES"
        assert result.outcome_before_frame_qa is COMPLETED
        assert result.response_outcome is COMPLETED

    def test_recycle_trial_keeps_a_trial_within_its_budget(self):
        result, _ = self._run_with_drops(29, {1, 2}, COMPLETED, max_dropped_fraction=0.1)
        assert result.outcome is COMPLETED
        assert result.record["n_dropped_frames"] == 2
        assert "outcome_before_frame_qa" not in result.record
        # Nothing was replaced, so the response outcome is the outcome.
        assert result.outcome_before_frame_qa is None
        assert result.response_outcome is COMPLETED

    def test_recycle_trial_leaves_an_incomplete_outcome_alone(self):
        """FAILED is already re-served, and PAUSED drives the runner's pause
        flow: replacing either would change what happens next for no gain."""
        result, _ = self._run_with_drops(4, {1, 2, 3}, FAILED, max_dropped_fraction=0.1)
        assert result.outcome is FAILED
        assert result.record["n_dropped_frames"] == 3
        assert "frame_qa_reason" not in result.record
        assert result.outcome_before_frame_qa is None
        assert result.response_outcome is FAILED

    def test_a_pause_is_its_own_response_outcome(self):
        """PAUSED is reserved and drives the runner's pause flow; frame QA
        never touches it, so nothing downstream may read it as anything else."""
        harness = EngineHarness(
            commands=ScriptedCommands([[], [Command.PAUSE]]),
            frame_qa=FrameQAConfig(policy="recycle_trial", max_dropped_fraction=0.1),
        )

        class AlwaysSlow(RunForFrames):
            def on_frame(self, ctx):
                harness.display.next_flip_extra = FRAME_S
                return super().on_frame(ctx)

        result = harness.engine.run_trial(harness.ctx(), [AlwaysSlow(5, COMPLETED)])
        assert result.outcome.name == "PAUSED"
        assert result.outcome_before_frame_qa is None
        assert result.response_outcome is result.outcome

    def test_incomplete_trials_never_add_up_to_the_recycle_abort(self):
        """The monitor counts recycles in a row and aborts the run at the
        limit. It counted trials the engine had decided not to recycle, so a
        subject having a bad run on a display dropping the odd frame stopped
        the session with a message blaming the panel — and no DROPPED_FRAMES
        row anywhere in the data to support it."""
        harness = EngineHarness(
            frame_qa=FrameQAConfig(
                policy="recycle_trial", max_dropped_fraction=0.1, max_consecutive_recycles=2
            )
        )

        class DropSome(RunForFrames):
            def on_frame(self, ctx):
                if len(self.frames_seen) in {1, 2, 3}:
                    harness.display.next_flip_extra = FRAME_S
                return super().on_frame(ctx)

        for trial in range(1, 6):
            result = harness.engine.run_trial(harness.ctx(trial), [DropSome(4, FAILED)])
            assert result.outcome is FAILED
