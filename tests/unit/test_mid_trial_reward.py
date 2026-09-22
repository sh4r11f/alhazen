"""Mid-trial reward: a phase asks for a juice drop while the trial runs.

What is pinned here, in the order a drop lives through it:

- the request (``ctx.request_reward``) only queues, and is refused loudly for
  a task that never declared ``mid_trial_reward``;
- the engine hands it to the dispenser after the next flip and emits REWARD
  stamped with that flip, carrying ``{pulses, reason, frame}``;
- the dispenser's worker thread delivers it without blocking the frame loop,
  one delivery at a time — drops that arrive while one is on the valve queue
  up and say so (``queued_behind``), and none is dropped or merged;
- the delivery's end comes back as REWARD_DELIVERED or REWARD_FAILED, on the
  session thread, counted on the trial's record;
- between trials the runner waits for every drop before it pays the outcome,
  so no two pulse trains ever overlap, and a failure takes the pause flow;
- a session for such a task is refused at build on a rig with no dispenser,
  and rehearsal modes stand a simulated one in.

Deterministic throughout: ``ScriptedReward`` holds each delivery on the valve
until the test releases it, so "a drop requested while another is still
delivering" is a state the test puts the session in, never a race it hopes to
win. Nothing sleeps.
"""

from __future__ import annotations

import csv
import json
import logging
import threading

import pytest

from alhazen import Model, Task, TrialPlan, TrialSetup, outcomes
from alhazen.config.models import (
    DevicesConfig,
    DisplayConfig,
    Duration,
    RewardHwConfig,
    RewardPulses,
    RigConfig,
)
from alhazen.core.commands import Command
from alhazen.core.events import RESERVED_EVENTS, EventSchema
from alhazen.core.trial import PhaseAction, RewardRequest, TrialContext
from alhazen.devices.reward import QueuedReward, SimulatedReward
from alhazen.errors import ConfigError, RewardError, RewardRequestError
from alhazen.modes import Mode
from alhazen.modes.session import build_mode_session
from alhazen.paradigms.base import Condition, SimpleSequence
from alhazen.session.builder import build_session
from alhazen.task.reward_policy import RewardPolicy
from alhazen.testing import ScriptedCommands, ScriptedReward
from support import (
    COMPLETED,
    FRAME_S,
    MONITOR,
    EngineHarness,
    RequestRewardOnFrames,
    RunForFrames,
    SessionHarness,
)

DROP = RewardPulses(n_pulses=1, pulse_ms=50, inter_pulse_ms=0)
END_PAY = RewardPulses(n_pulses=3, pulse_ms=100, inter_pulse_ms=50)


@pytest.fixture
def queued():
    """A QueuedReward over a ScriptedReward, closed after the test whatever
    happened — a worker thread left running would outlive the test."""
    made: list[QueuedReward] = []

    def make(**scripted) -> tuple[QueuedReward, ScriptedReward]:
        device = ScriptedReward(**scripted)
        wrapper = QueuedReward(device)
        made.append(wrapper)
        return wrapper, device

    yield make
    for wrapper in made:
        # A held device would keep close() waiting on its queue: let
        # everything through first.
        if isinstance(wrapper.dispenser, ScriptedReward):
            wrapper.dispenser.release(1000)
        wrapper.close()


def request(reason: str = "hold", frame: int = 0) -> RewardRequest:
    return RewardRequest(pulses=DROP, reason=reason, frame=frame)


def events_named(collector, name: str):
    return [event for event in collector.events if event.name == name]


class SettleOnFrame(RequestRewardOnFrames):
    """Asks for one drop on its first frame, then on a later frame releases
    the held valve and waits for the worker to go idle before returning, so
    that delivery's completion is certainly in the queue when the engine
    drains it after this frame's flip. Test-only: a real phase never touches
    the dispenser."""

    def __init__(self, n_frames, then, device: ScriptedReward, wrapper, on_frame_index: int):
        super().__init__(n_frames, then, on_frames=(0,))
        self._device = device
        self._wrapper = wrapper
        self._at = on_frame_index
        self._frames_done = 0

    def on_frame(self, ctx: TrialContext):
        if self._frames_done == self._at:
            self._device.release(1000)
            self._wrapper.wait_idle()
        self._frames_done += 1
        return super().on_frame(ctx)


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


class TestTheRequest:
    def ctx(self, accepts: bool = True) -> TrialContext:
        ctx = EngineHarness().ctx()
        ctx.accepts_reward_requests = accepts
        return ctx

    def test_it_only_queues(self):
        # A phase touches nothing but the context: the request sits on it
        # until the engine hands it over after the flip.
        ctx = self.ctx()
        ctx.request_reward(DROP, "hold")
        assert ctx.pending_reward_requests == [RewardRequest(pulses=DROP, reason="hold")]

    def test_a_task_that_never_declared_it_is_refused_by_name(self):
        ctx = self.ctx(accepts=False)
        with pytest.raises(RewardRequestError, match="mid_trial_reward = True"):
            ctx.request_reward(DROP, "hold")
        assert ctx.pending_reward_requests == []

    @pytest.mark.parametrize(
        ("pulses", "reason", "match"),
        [
            (3, "hold", "takes a RewardPulses"),
            (RewardPulses(n_pulses=0), "hold", "delivers nothing"),
            (RewardPulses(pulse_ms=0), "hold", "delivers nothing"),
            (DROP, "", "non-empty reason"),
            (DROP, None, "non-empty reason"),
        ],
    )
    def test_a_request_that_could_not_open_the_valve_is_refused(self, pulses, reason, match):
        with pytest.raises(RewardRequestError, match=match):
            self.ctx().request_reward(pulses, reason)

    def test_through_the_engine_an_undeclared_request_stops_the_trial(self):
        # Wired with no sink — what the builder does for a task that did not
        # declare mid_trial_reward — the first request is a loud error, not
        # a drop nobody delivers.
        harness = EngineHarness()
        with pytest.raises(RewardRequestError):
            harness.engine.run_trial(
                harness.ctx(), [RequestRewardOnFrames(3, COMPLETED, on_frames=(1,))]
            )


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


class TestQueuedReward:
    def test_submit_returns_while_the_valve_is_still_open(self, queued):
        wrapper, device = queued(hold=True)
        assert wrapper.submit(request("a")) == 0
        device.wait_for_attempts(1)
        # Still on the valve, and the caller already has its answer.
        assert device.deliveries == []
        assert wrapper.completed() == []

    def test_requests_queue_behind_a_delivery_and_none_is_dropped_or_merged(self, queued):
        wrapper, device = queued(hold=True)
        ahead = [wrapper.submit(request(reason)) for reason in ("a", "b", "c")]
        assert ahead == [0, 1, 2]

        device.release(3)
        wrapper.wait_idle()

        # Three requests, three separate deliveries, in the order asked.
        assert device.attempts == [DROP, DROP, DROP]
        assert [done.request.reason for done in wrapper.completed()] == ["a", "b", "c"]
        assert device.max_on_valve == 1

    def test_a_failure_is_reported_and_the_worker_carries_on(self, queued):
        wrapper, device = queued(fail=[1])
        wrapper.submit(request("first"))
        wrapper.submit(request("second"))
        wrapper.wait_idle()

        first, second = wrapper.completed()
        assert first.request.reason == "first"
        assert first.error == "RewardError: scripted failure on delivery 1"
        assert second.error is None
        assert device.deliveries == [DROP]

    def test_completed_hands_each_completion_over_once(self, queued):
        wrapper, _ = queued()
        wrapper.submit(request())
        wrapper.wait_idle()
        assert len(wrapper.completed()) == 1
        assert wrapper.completed() == []

    def test_a_synchronous_delivery_waits_its_turn(self, queued):
        # The manual key and the end-of-trial pay call deliver(); it must go
        # on the valve only after the drops already queued, never beside one.
        wrapper, device = queued(hold=True)
        wrapper.submit(request("drop"))
        device.wait_for_attempts(1)

        def operator():
            # Both let through at once. A deliver() that went straight to the
            # device, beside the drop still waiting on the valve, would show
            # as two on the valve at once in max_on_valve.
            device.release(2)

        releaser = threading.Thread(target=operator)
        releaser.start()
        wrapper.deliver(END_PAY)
        releaser.join()

        assert device.attempts == [DROP, END_PAY]
        assert device.max_on_valve == 1
        # A synchronous delivery reports to its caller, not the queue.
        assert [done.request.reason for done in wrapper.completed()] == ["drop"]

    def test_a_synchronous_failure_is_raised_on_the_callers_thread(self, queued):
        wrapper, _ = queued(fail=[1])
        with pytest.raises(RewardError, match="scripted failure"):
            wrapper.deliver(END_PAY)

    def test_a_finished_synchronous_delivery_is_not_counted_ahead(self, queued):
        wrapper, _ = queued()
        wrapper.deliver(END_PAY)
        assert wrapper.submit(request()) == 0

    def test_close_delivers_what_is_queued_then_releases_the_device(self, queued):
        wrapper, device = queued()
        wrapper.submit(request())
        wrapper.wait_idle()
        wrapper.completed()
        wrapper.submit(request("late"))
        wrapper.close()
        assert device.attempts == [DROP, DROP]
        assert device.closed

    def test_nothing_is_accepted_after_close(self, queued):
        wrapper, _ = queued()
        wrapper.close()
        with pytest.raises(RewardError, match="after the reward worker was closed"):
            wrapper.submit(request())
        with pytest.raises(RewardError, match="after the reward worker was closed"):
            wrapper.deliver(DROP)

    def test_a_completion_nobody_reported_is_said_at_close(self, queued, caplog):
        wrapper, _ = queued(fail=[1])
        wrapper.submit(request("orphan"))
        wrapper.wait_idle()
        with caplog.at_level(logging.ERROR, logger="alhazen.devices.reward"):
            wrapper.close()
        assert any(
            "'orphan'" in record.getMessage() and "FAILED" in record.getMessage()
            for record in caplog.records
        ), [record.getMessage() for record in caplog.records]

    def test_the_device_is_closed_even_when_stopping_the_worker_fails(self, queued):
        wrapper, device = queued()

        def broken_put(item):
            raise RuntimeError("queue gone")

        wrapper._jobs.put = broken_put  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="queue gone"):
            wrapper.close()
        assert device.closed
        # Stop the worker the broken put left waiting, so no thread outlives
        # the test.
        del wrapper._jobs.put
        wrapper._jobs.put(None)
        wrapper._thread.join()


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class TestInTheFrameLoop:
    def test_reward_is_stamped_with_the_flip_after_the_request(self, queued):
        wrapper, _ = queued()
        harness = EngineHarness(reward_requests=wrapper)
        ctx = harness.ctx()
        phase = RequestRewardOnFrames(4, COMPLETED, on_frames=(2,))
        harness.engine.run_trial(ctx, [phase])
        harness.engine.settle_rewards(ctx)

        (reward,) = events_named(harness.collector, "REWARD")
        # Frame 2 is the third on_frame call; its flip is the third flip.
        assert reward.t == pytest.approx(3 * FRAME_S)
        assert reward.payload == {
            "manual": False,
            "pulses": DROP.model_dump(mode="json"),
            "reason": "hold",
            "frame": 2,
        }

    def test_the_frame_index_counts_across_phases(self, queued):
        wrapper, _ = queued()
        harness = EngineHarness(reward_requests=wrapper)
        ctx = harness.ctx()
        phases = [
            RunForFrames(2, PhaseAction.ADVANCE),
            RequestRewardOnFrames(2, COMPLETED, on_frames=(0,)),
        ]
        harness.engine.run_trial(ctx, phases)
        harness.engine.settle_rewards(ctx)
        # Three flips in the first phase (two CONTINUE, one ADVANCE), so the
        # second phase's first frame is frame 3 of the trial.
        (reward,) = events_named(harness.collector, "REWARD")
        assert reward.payload["frame"] == 3

    def test_the_frame_is_the_index_the_per_frame_inputs_use(self, queued):
        # `frame` joins against the database's frame_inputs table: the flip
        # the REWARD is stamped with is the flip that index was recorded at.
        wrapper, _ = queued()
        harness = EngineHarness(reward_requests=wrapper)
        flips: dict[int, float] = {}
        harness.engine._on_frame_input = lambda trial, index, t, inputs: flips.__setitem__(index, t)
        ctx = harness.ctx()
        harness.engine.run_trial(ctx, [RequestRewardOnFrames(5, COMPLETED, on_frames=(1, 4))])
        harness.engine.settle_rewards(ctx)
        for reward in events_named(harness.collector, "REWARD"):
            assert flips[reward.payload["frame"]] == reward.t

    def test_the_frame_loop_never_waits_for_the_pump(self, queued):
        # The whole trial runs to its end while the one drop it asked for is
        # still on the valve: a pulse train must not stall a pursuit.
        wrapper, device = queued(hold=True)
        harness = EngineHarness(reward_requests=wrapper)
        ctx = harness.ctx()
        result = harness.engine.run_trial(
            ctx, [RequestRewardOnFrames(10, COMPLETED, on_frames=(0,))]
        )
        assert result.outcome is COMPLETED
        assert device.deliveries == []
        assert "REWARD_DELIVERED" not in harness.collector.names()

        device.release()
        harness.engine.settle_rewards(ctx)
        assert device.deliveries == [DROP]

    def test_a_drop_behind_another_says_how_far_behind(self, queued):
        wrapper, device = queued(hold=True)
        harness = EngineHarness(reward_requests=wrapper)
        ctx = harness.ctx()
        harness.engine.run_trial(ctx, [RequestRewardOnFrames(6, COMPLETED, on_frames=(0, 2, 4))])
        device.release(3)
        harness.engine.settle_rewards(ctx)

        rewards = events_named(harness.collector, "REWARD")
        assert "queued_behind" not in rewards[0].payload
        assert [r.payload.get("queued_behind") for r in rewards[1:]] == [1, 2]
        # Every one delivered, separately.
        assert len(events_named(harness.collector, "REWARD_DELIVERED")) == 3
        assert device.attempts == [DROP, DROP, DROP]

    def test_two_requests_on_one_frame_are_two_drops(self, queued):
        wrapper, device = queued()
        harness = EngineHarness(reward_requests=wrapper)

        class Twice(RunForFrames):
            def on_frame(self, ctx):
                if not self.frames_seen:
                    ctx.request_reward(DROP, "left")
                    ctx.request_reward(DROP, "right")
                return super().on_frame(ctx)

        ctx = harness.ctx()
        harness.engine.run_trial(ctx, [Twice(2, COMPLETED)])
        harness.engine.settle_rewards(ctx)
        assert [e.payload["reason"] for e in events_named(harness.collector, "REWARD")] == [
            "left",
            "right",
        ]
        assert ctx.record["n_mid_trial_rewards"] == 2

    def test_delivery_is_reported_during_the_trial_it_belongs_to(self, queued):
        wrapper, device = queued(hold=True)
        harness = EngineHarness(reward_requests=wrapper)
        ctx = harness.ctx()
        harness.engine.run_trial(
            ctx,
            [SettleOnFrame(5, COMPLETED, device, wrapper, on_frame_index=2)],
        )
        names = harness.collector.names()
        # Drained on frame 2, before the trial ended: in the record's own
        # trial, between its REWARD and its TRIAL_END.
        assert names.index("REWARD") < names.index("REWARD_DELIVERED") < names.index("TRIAL_END")
        (delivered,) = events_named(harness.collector, "REWARD_DELIVERED")
        assert delivered.payload == {
            "pulses": DROP.model_dump(mode="json"),
            "reason": "hold",
            "frame": 0,
        }
        assert delivered.t == pytest.approx(3 * FRAME_S)
        assert ctx.record["n_mid_trial_rewards"] == 1
        assert ctx.record["rewarded"] is True
        assert ctx.record["t_reward_delivered"] == pytest.approx(3 * FRAME_S)

    def test_a_failure_carries_the_reason_and_does_not_stop_the_trial(self, queued):
        wrapper, device = queued(fail=[1])
        harness = EngineHarness(reward_requests=wrapper)
        ctx = harness.ctx()
        phase = RequestRewardOnFrames(4, COMPLETED, on_frames=(0,), reason="pursuit")
        result = harness.engine.run_trial(ctx, [phase])
        harness.engine.settle_rewards(ctx)

        assert result.outcome is COMPLETED
        (failed,) = events_named(harness.collector, "REWARD_FAILED")
        assert failed.payload["reason"] == "pursuit"
        assert failed.payload["frame"] == 0
        assert "scripted failure" in failed.payload["error"]
        assert "REWARD_DELIVERED" not in harness.collector.names()
        assert ctx.record["n_mid_trial_reward_failures"] == 1
        assert ctx.record["n_mid_trial_rewards"] == 0
        assert ctx.record["rewarded"] is False

    def test_an_earlier_drop_keeps_the_trial_rewarded_after_a_failure(self, queued):
        wrapper, _ = queued(fail=[2])
        harness = EngineHarness(reward_requests=wrapper)
        ctx = harness.ctx()
        harness.engine.run_trial(ctx, [RequestRewardOnFrames(4, COMPLETED, on_frames=(0, 1))])
        harness.engine.settle_rewards(ctx)
        assert ctx.record["rewarded"] is True
        assert ctx.record["n_mid_trial_rewards"] == 1
        assert ctx.record["n_mid_trial_reward_failures"] == 1

    def test_the_counts_are_zero_not_absent_on_a_trial_with_no_drops(self, queued):
        wrapper, _ = queued()
        harness = EngineHarness(reward_requests=wrapper)
        record = harness.engine.run_trial(harness.ctx(), [RunForFrames(1, COMPLETED)]).record
        assert record["n_mid_trial_rewards"] == 0
        assert record["n_mid_trial_reward_failures"] == 0
        assert "rewarded" not in record

    def test_a_task_without_mid_trial_reward_writes_no_counts(self):
        harness = EngineHarness()
        record = harness.engine.run_trial(harness.ctx(), [RunForFrames(1, COMPLETED)]).record
        assert "n_mid_trial_rewards" not in record
        assert "n_mid_trial_reward_failures" not in record

    def test_a_request_the_trial_ended_before_commanding_is_said(self, queued, caplog):
        # Queued in on_enter; the experimenter skips the trial before its
        # first flip. With no flip to stamp it the drop is never commanded,
        # and the log says so rather than nothing.
        wrapper, device = queued()
        harness = EngineHarness(
            reward_requests=wrapper, commands=ScriptedCommands([[Command.SKIP_TRIAL]])
        )

        class AskOnEnter(RunForFrames):
            def on_enter(self, ctx):
                ctx.request_reward(DROP, "on_enter")

        ctx = harness.ctx()
        harness.engine.run_trial(ctx, [AskOnEnter(3, COMPLETED)])
        with caplog.at_level(logging.WARNING, logger="alhazen.core.engine"):
            harness.engine.settle_rewards(ctx)
        assert device.attempts == []
        assert "REWARD" not in harness.collector.names()
        assert any("on_enter" in record.getMessage() for record in caplog.records)

    def test_settle_is_a_no_op_without_mid_trial_reward(self):
        harness = EngineHarness()
        ctx = harness.ctx()
        harness.engine.run_trial(ctx, [RunForFrames(1, COMPLETED)])
        harness.engine.settle_rewards(ctx)  # nothing to wait for, nothing emitted
        assert harness.collector.names()[-1] == "TRIAL_END"

    def test_reward_delivered_is_a_reserved_event(self):
        assert "REWARD_DELIVERED" in RESERVED_EVENTS


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


def session(tmp_path, device, *, n_trials=1, policy=None, phases=None, **kwargs):
    return SessionHarness(
        tmp_path,
        n_trials=n_trials,
        reward=device,
        mid_trial_reward=True,
        reward_policy=policy,
        build_trial=lambda setup: TrialPlan(
            phases=phases()
            if phases is not None
            else [RequestRewardOnFrames(4, COMPLETED, on_frames=(0, 2))]
        ),
        **kwargs,
    )


def read_trials(harness) -> list[dict[str, str]]:
    with harness.paths.trials_path.open() as f:
        return list(csv.DictReader(f))


def read_events(harness) -> list[dict[str, str]]:
    with harness.paths.events_path.open() as f:
        return list(csv.DictReader(f))


class TestInASession:
    def test_drops_are_counted_on_the_row(self, tmp_path):
        device = ScriptedReward()
        harness = session(tmp_path, device)
        harness.runner.run()
        (row,) = read_trials(harness)
        assert row["n_mid_trial_rewards"] == "2"
        assert row["n_mid_trial_reward_failures"] == "0"
        assert row["rewarded"] == "True"
        assert device.deliveries == [DROP, DROP]

    def test_events_csv_stamps_the_frame_each_drop_was_commanded(self, tmp_path):
        harness = session(tmp_path, ScriptedReward())
        harness.runner.run()
        rewards = [row for row in read_events(harness) if row["event"] == "REWARD"]
        assert [json.loads(row["payload_json"])["frame"] for row in rewards] == [0, 2]
        assert [json.loads(row["payload_json"])["reason"] for row in rewards] == ["hold", "hold"]

    def test_the_outcomes_pay_waits_for_every_drop(self, tmp_path):
        # A drop still on the valve when the trial ends: the end-of-trial pay
        # must follow it, never run beside it on the same valve.
        device = ScriptedReward(hold=True)
        policy = RewardPolicy(by_outcome={"COMPLETED": END_PAY})
        harness = session(
            tmp_path,
            device,
            policy=policy,
            phases=lambda: [RequestRewardOnFrames(2, COMPLETED, on_frames=(1,))],
        )

        def operator():
            # The drop, then the end-of-trial pay behind it.
            device.wait_for_attempts(1)
            device.release(2)

        releaser = threading.Thread(target=operator)
        releaser.start()
        harness.runner.run()
        releaser.join()

        assert device.attempts == [DROP, END_PAY]
        assert device.max_on_valve == 1
        names = harness.collector.names()
        end_pay = [
            i
            for i, e in enumerate(harness.collector.events)
            if e.name == "REWARD" and "outcome" in e.payload
        ]
        # The drop's completion is on record before the outcome's REWARD.
        assert names.index("REWARD_DELIVERED") < end_pay[0]
        (row,) = read_trials(harness)
        assert row["rewarded"] == "True"
        assert row["n_mid_trial_rewards"] == "1"

    def test_the_manual_key_waits_behind_a_drop(self, tmp_path):
        device = ScriptedReward(hold=True)
        harness = session(
            tmp_path,
            device,
            phases=lambda: [RequestRewardOnFrames(3, COMPLETED, on_frames=(0,))],
            # Frame 0 asks for a drop; the experimenter's key lands on frame 1.
            commands=ScriptedCommands([[], [Command.MANUAL_REWARD]]),
        )

        def operator():
            device.wait_for_attempts(1)
            device.release(2)

        releaser = threading.Thread(target=operator)
        releaser.start()
        harness.runner.run()
        releaser.join()

        # The manual delivery (the harness's default RewardPulses) went on the
        # valve after the drop, never beside it.
        assert device.attempts == [DROP, RewardPulses()]
        assert device.max_on_valve == 1

    def test_a_failed_drop_takes_the_pause_flow_after_the_trial(self, tmp_path):
        paused: list = []
        device = ScriptedReward(fail=[1])
        harness = session(tmp_path, device, n_trials=2)
        harness.runner._on_pause = lambda menu: paused.append(menu) or "resume"
        harness.runner.run()

        # Recorded first — the measurement survives — then handed to a human.
        rows = read_trials(harness)
        assert [row["outcome"] for row in rows] == ["COMPLETED", "COMPLETED"]
        assert rows[0]["n_mid_trial_reward_failures"] == "1"
        assert rows[0]["rewarded"] == "True"  # its second drop arrived
        assert len(paused) == 1
        assert "REWARD FAILURE" in paused[0].title
        (failed,) = events_named(harness.collector, "REWARD_FAILED")
        assert failed.payload["reason"] == "hold"
        assert failed.trial_index == 1

    def test_no_reward_is_not_claimed_for_a_trial_that_earned_drops(self, tmp_path):
        # The outcome pays nothing at the end, but the trial paid during it:
        # NO_REWARD would say the subject got no juice, which is false.
        harness = session(
            tmp_path,
            ScriptedReward(),
            policy=RewardPolicy(by_outcome={"SOMETHING_ELSE": END_PAY}),
        )
        harness.runner.run()
        assert "NO_REWARD" not in harness.collector.names()

    def test_no_reward_still_marks_a_trial_that_earned_nothing(self, tmp_path):
        harness = session(
            tmp_path,
            ScriptedReward(),
            policy=RewardPolicy(by_outcome={"SOMETHING_ELSE": END_PAY}),
            phases=lambda: [RunForFrames(2, COMPLETED)],
        )
        harness.runner.run()
        assert "NO_REWARD" in harness.collector.names()

    def test_an_end_of_trial_failure_after_a_drop_keeps_the_trial_rewarded(self, tmp_path):
        device = ScriptedReward(fail=[3])  # the two drops arrive, the end pay fails
        harness = session(tmp_path, device, policy=RewardPolicy(by_outcome={"COMPLETED": END_PAY}))
        harness.runner.run()
        (row,) = read_trials(harness)
        assert row["rewarded"] == "True"
        assert "REWARD_FAILED" in harness.collector.names()

    def test_a_quit_mid_trial_still_records_the_drops_delivery(self, tmp_path):
        # The quit leaves the trial loop before the between-trials settle;
        # teardown settles instead, before events.csv is written.
        device = ScriptedReward()
        harness = session(
            tmp_path,
            device,
            phases=lambda: [RequestRewardOnFrames(10, COMPLETED, on_frames=(0,))],
            commands=ScriptedCommands([[], [], [Command.QUIT]]),
        )
        harness.runner.run()
        events = [row["event"] for row in read_events(harness)]
        assert "REWARD" in events
        assert "REWARD_DELIVERED" in events
        assert device.closed  # and the worker released the device

    def test_teardown_goes_on_when_the_settle_fails(self, tmp_path):
        # Every teardown step is attempted: a settle that raises must not
        # keep the trials table from being written or the device released.
        device = ScriptedReward()
        harness = session(
            tmp_path,
            device,
            phases=lambda: [RequestRewardOnFrames(10, COMPLETED, on_frames=(0,))],
            commands=ScriptedCommands([[], [], [Command.QUIT]]),
        )

        def broken_settle(ctx):
            raise RuntimeError("settle broke")

        harness.engine.settle_rewards = broken_settle  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="settle broke"):
            harness.runner.run()
        assert harness.paths.events_path.exists()
        assert device.closed


# ---------------------------------------------------------------------------
# The declaration and the build
# ---------------------------------------------------------------------------


class Params(Model):
    n_trials: int = 1


class Pursuit(Task):
    """A task that pays while gaze follows the dot, reduced to one drop."""

    name = "pursuit"
    events = EventSchema(("DOT_ON",))
    outcomes = outcomes(DONE=dict(completed=True, success=True))
    params_model = Params
    mid_trial_reward = True

    def conditions(self, rng):
        return [Condition({})]

    def make_source(self, params, rng):
        return SimpleSequence([Condition({})], n_repeats=1, rng=rng)

    def build_trial(self, setup: TrialSetup) -> TrialPlan:
        return TrialPlan(
            phases=[RequestRewardOnFrames(3, self.outcomes["DONE"], on_frames=(1,), pulses=DROP)]
        )


class Undeclared(Pursuit):
    name = "undeclared"
    mid_trial_reward = False


def rig(tmp_path, reward: RewardHwConfig | None) -> RigConfig:
    return RigConfig(
        monitor=MONITOR,
        display=DisplayConfig(backend="simulated"),
        devices=DevicesConfig(reward=reward),
        data_root=tmp_path,
    )


def build(tmp_path, task, rig_reward=None, **kwargs):
    """Build over a simulated rig; ``rig_reward`` is what the rig file says
    it has, and any ``reward=`` in kwargs hands build_session a device."""
    return build_session(
        rig=rig(tmp_path, rig_reward),
        subject="t01",
        session=1,
        run=1,
        task=task,
        seed=1,
        iti=Duration(ms=0),
        simulated_frame_period_s=0.0,
        date_yyyymmdd="20260826",
        **kwargs,
    )


class TestDeclaration:
    def test_the_default_is_off(self):
        assert Task.mid_trial_reward is False

    def test_a_non_boolean_is_refused_when_the_class_is_written(self):
        with pytest.raises(TypeError, match="must be True or False"):

            class Sloppy(Pursuit):
                name = "sloppy"
                mid_trial_reward = "yes"  # type: ignore[assignment]


class TestBuild:
    def test_a_rig_with_no_dispenser_is_refused_before_anything_opens(self, tmp_path):
        with pytest.raises(ConfigError, match="mid_trial_reward = True.*no reward dispenser"):
            build(tmp_path, Pursuit(Params()))
        # Refused before a run directory was made.
        assert not (tmp_path / "sub-t01").exists()

    def test_a_dispenser_handed_in_satisfies_it(self, tmp_path):
        device = ScriptedReward()
        runner = build(tmp_path, Pursuit(Params()), reward=device)
        runner.run()
        assert device.deliveries == [DROP]
        assert device.closed

    def test_a_simulated_rig_runs_the_drops_end_to_end(self, tmp_path):
        runner = build(tmp_path, Pursuit(Params()), rig_reward=RewardHwConfig(backend="simulated"))
        # Every delivery goes through the worker: the task's drops, the manual
        # key, the end-of-trial pay.
        assert isinstance(runner._reward, QueuedReward)
        assert isinstance(runner._reward.dispenser, SimulatedReward)
        simulated = runner._reward.dispenser
        runner.run()
        assert len(simulated.deliveries) == 1
        rows = runner._recorder.trials
        assert rows[0]["n_mid_trial_rewards"] == 1

    def test_a_build_that_fails_after_the_worker_started_stops_it(self, tmp_path):
        # The worker is a thread; a build that fails after starting it must
        # not leave it running with nothing to ever close it.
        class BrokenScheduler(Pursuit):
            name = "broken-scheduler"

            def make_source(self, params, rng):
                raise ValueError("no scheduler for you")

        device = ScriptedReward()
        threads_before = set(threading.enumerate())
        with pytest.raises(ValueError, match="no scheduler for you"):
            build(tmp_path, BrokenScheduler(Params()), reward=device)
        assert device.closed
        started = set(threading.enumerate()) - threads_before
        assert not [t for t in started if t.name == "alhazen-reward"]

    def test_a_task_without_it_keeps_the_device_itself(self, tmp_path):
        runner = build(
            tmp_path,
            Undeclared(Params()),
            rig_reward=RewardHwConfig(backend="simulated"),
        )
        assert isinstance(runner._reward, SimulatedReward)

    def test_a_request_from_a_task_that_did_not_declare_it_is_loud(self, tmp_path):
        runner = build(
            tmp_path,
            Undeclared(Params()),
            rig_reward=RewardHwConfig(backend="simulated"),
        )
        with pytest.raises(RewardRequestError, match="does not declare"):
            runner.run()


class TestRehearsalModes:
    @pytest.mark.parametrize("mode", [Mode.TEST, Mode.SIMULATE])
    def test_a_rig_with_no_dispenser_gets_a_simulated_one_and_says_so(self, tmp_path, mode):
        seen = {}

        class Runner:
            setup_notes: list[str] = []

        def spy(**kwargs):
            seen.update(kwargs)
            return Runner()

        class Rehearsable(Pursuit):
            name = "rehearsable"

            def simulation(self, seed):
                from alhazen.modes.simulation import Simulation

                return Simulation(tracker=object())

        built = build_mode_session(
            mode,
            rig=rig(tmp_path, None),
            task=Rehearsable(Params()),
            subject="t01",
            session=1,
            build_session=spy,
        )
        assert seen["rig"].devices.reward == RewardHwConfig(backend="simulated")
        assert "drops are logged, not pumped" in built.describe()

    def test_run_mode_does_not_invent_a_dispenser(self, tmp_path):
        seen = {}

        class Runner:
            setup_notes: list[str] = []

        def spy(**kwargs):
            seen.update(kwargs)
            return Runner()

        build_mode_session(
            Mode.RUN,
            rig=rig(tmp_path, None),
            task=Pursuit(Params()),
            subject="t01",
            session=1,
            build_session=spy,
        )
        # Left for build_session to refuse: in a real session the refusal
        # is the point.
        assert seen["rig"].devices.reward is None

    def test_a_task_without_mid_trial_reward_is_left_alone(self, tmp_path):
        seen = {}

        class Runner:
            setup_notes: list[str] = []

        def spy(**kwargs):
            seen.update(kwargs)
            return Runner()

        build_mode_session(
            Mode.TEST,
            rig=rig(tmp_path, None),
            task=Undeclared(Params()),
            subject="t01",
            session=1,
            build_session=spy,
        )
        assert seen["rig"].devices.reward is None

    def test_simulate_on_a_laptop_builds_and_runs(self, tmp_path):
        # The whole point of the substitution: a rehearsal of a mid-trial
        # task on a machine with no pump builds, and pays in the log.
        class Rehearsable(Pursuit):
            name = "rehearsable-run"

            def simulation(self, seed):
                from alhazen.modes.simulation import Simulation

                # This task reads no gaze; the stand-in subject is the task.
                return Simulation(task=self)

        built = build_mode_session(
            Mode.SIMULATE,
            rig=rig(tmp_path, None),
            task=Rehearsable(Params()),
            subject="t01",
            session=1,
            headless=True,
        )
        built.runner.run()
        assert built.runner._recorder.trials[0]["n_mid_trial_rewards"] == 1
