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
- the experimenter's manual reward goes on the valve next, ahead of every
  queued drop (which all follow it, in order) but never cutting short the
  train already there; the end-of-trial pay takes its turn;
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
from alhazen.session.builder import build_session, make_manual_reward
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
# Drops told apart by their width, and two manual rewards, so the order they
# reached the valve in reads straight off the device's `attempts`.
D1 = RewardPulses(n_pulses=1, pulse_ms=51, inter_pulse_ms=0)
D2 = RewardPulses(n_pulses=1, pulse_ms=52, inter_pulse_ms=0)
D3 = RewardPulses(n_pulses=1, pulse_ms=53, inter_pulse_ms=0)
MANUAL = RewardPulses(n_pulses=2, pulse_ms=80, inter_pulse_ms=40)
MANUAL_2 = RewardPulses(n_pulses=2, pulse_ms=90, inter_pulse_ms=40)


@pytest.fixture
def queued():
    """A QueuedReward over a ScriptedReward (a fresh one built from the
    keyword arguments, or the one passed in), closed after the test whatever
    happened — a worker thread left running would outlive the test."""
    made: list[QueuedReward] = []

    def make(
        device: ScriptedReward | None = None, **scripted
    ) -> tuple[QueuedReward, ScriptedReward]:
        device = device if device is not None else ScriptedReward(**scripted)
        wrapper = QueuedReward(device)
        made.append(wrapper)
        return wrapper, device

    yield make
    for wrapper in made:
        # A held device would keep close() waiting on its queue: let
        # everything through first — past the door too, for a gated one.
        if isinstance(wrapper.dispenser, ScriptedReward):
            wrapper.dispenser.release(1000)
        if isinstance(wrapper.dispenser, GatedReward):
            wrapper.dispenser.open_door()
        wrapper.close()


def request(reason: str = "hold", frame: int = 0, pulses: RewardPulses = DROP) -> RewardRequest:
    return RewardRequest(pulses=pulses, reason=reason, frame=frame)


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


class DropsWithTheFirstOnTheValve(RequestRewardOnFrames):
    """Asks for drops on the listed frames, and on its frame 1 waits until
    the device has started a delivery — frame 0's drop, handed over after
    frame 0's flip. Everything asked for from then on (a drop, the manual
    key) meets that drop already on the valve, rather than racing the worker
    for it. Test-only: a real phase never touches the dispenser."""

    def __init__(self, n_frames, then, device: ScriptedReward, on_frames: tuple[int, ...]):
        super().__init__(n_frames, then, on_frames=on_frames)
        self._device = device
        self._frames_done = 0

    def on_frame(self, ctx: TrialContext):
        if self._frames_done == 1:
            self._device.wait_for_attempts(1)
        self._frames_done += 1
        return super().on_frame(ctx)


class GatedReward(ScriptedReward):
    """A ScriptedReward whose deliveries after the first ``admit`` wait at a
    door until the test calls ``open_door()`` — before the device counts them
    as started.

    ScriptedReward's hold keeps a delivery on the valve, where it already
    counts as started (it is in ``attempts``). Asserting that a delivery had
    NOT started at some moment needs the worker kept from handing it over at
    all, and the door is where it waits. Only the worker thread calls
    ``deliver``, one delivery at a time, so the count needs no lock."""

    def __init__(self, admit: int, **scripted):
        super().__init__(**scripted)
        self._admit = admit
        self._arrived = 0
        self._door = threading.Event()

    def deliver(self, pulses: RewardPulses) -> None:
        self._arrived += 1
        if self._arrived > self._admit and not self._door.wait(timeout=5.0):
            raise TimeoutError("GatedReward: the test never opened the door")
        super().deliver(pulses)

    def open_door(self) -> None:
        self._door.set()


def signal_on_enqueue(wrapper: QueuedReward) -> threading.Semaphore:
    """A semaphore released once for every delivery that joins the worker's
    line from now on.

    A ``deliver_next`` caller blocks until its delivery is done, so a test
    that must act once that delivery is *in line* — let the valve go, so
    that the worker's next pick is the one under test — needs to know that
    moment, and the device cannot tell it: nothing reaches the device until
    the worker takes the job. So this wraps the one private method every
    delivery joins the line through."""
    joined = threading.Semaphore(0)
    enqueue = wrapper._enqueue

    def signalling(job, *, front):
        ahead = enqueue(job, front=front)
        joined.release()
        return ahead

    wrapper._enqueue = signalling  # type: ignore[method-assign]
    return joined


def wait_joined(joined: threading.Semaphore, n: int = 1) -> None:
    """Block until ``n`` more deliveries have joined the line — failing
    rather than hanging when one never does."""
    for _ in range(n):
        if not joined.acquire(timeout=5.0):
            raise TimeoutError("a delivery never joined the reward worker's line")


class Caller:
    """A blocking reward call on a thread of its own — the session thread,
    in a real session — so the test can drive the valve while it waits.

    Keeps what the call raised, on that thread, and what the device had
    started at the moment the call returned."""

    def __init__(self, call, device: ScriptedReward | None = None) -> None:
        self.error: BaseException | None = None
        self.started_when_returned: list[RewardPulses] | None = None
        self._call = call
        self._device = device
        self._thread = threading.Thread(target=self._run, name="test-reward-caller")
        self._thread.start()

    def _run(self) -> None:
        try:
            self._call()
        except BaseException as e:  # kept for the test to assert on
            self.error = e
        # Taken first thing after the return, before the test can release
        # anything else.
        if self._device is not None:
            self.started_when_returned = list(self._device.attempts)

    def join(self) -> None:
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            raise TimeoutError("the reward call never returned")


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
        # The end-of-trial pay calls deliver(); it must go on the valve only
        # after the drops already queued, never beside one.
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
        with pytest.raises(RewardError, match="after the reward worker was closed"):
            wrapper.deliver_next(DROP)

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
        # Stopping the worker means waiting for it to finish the line, and
        # that wait can end in an exception (a Ctrl-C while a stuck delivery
        # is still on the valve). The device is released anyway.
        wrapper, device = queued(hold=True)
        wrapper.submit(request())
        device.wait_for_attempts(1)  # held on the valve: close() must wait for it

        def broken_join(timeout=None):
            raise RuntimeError("wait interrupted")

        wrapper._thread.join = broken_join  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="wait interrupted"):
            wrapper.close()
        assert device.closed
        # Let the held delivery finish and the worker stop, so no thread
        # outlives the test.
        del wrapper._thread.join
        device.release()
        wrapper._thread.join()


class TestTheManualRewardGoesNext:
    """deliver_next, the experimenter's manual reward, overrides the queue:
    it is the next delivery once the one on the valve finishes, ahead of
    every queued drop, and the drops all follow it in their order.

    Each test puts the worker in the state it is about: one drop held on the
    valve (``wait_for_attempts(1)``), the others queued behind it, and the
    manual delivery in line (``wait_joined``) — only then is the valve let
    go, so which delivery goes next is the worker's choice, never a race."""

    def queue_three_drops(self, wrapper: QueuedReward, device: ScriptedReward) -> None:
        """D1 on the valve (held), D2 and D3 queued behind it."""
        wrapper.submit(request("d1", pulses=D1))
        device.wait_for_attempts(1)
        wrapper.submit(request("d2", pulses=D2))
        wrapper.submit(request("d3", pulses=D3))

    def test_it_goes_ahead_of_the_queued_drops_and_none_is_lost(self, queued, caplog):
        wrapper, device = queued(hold=True)
        self.queue_three_drops(wrapper, device)
        joined = signal_on_enqueue(wrapper)

        with caplog.at_level(logging.INFO, logger="alhazen.devices.reward"):
            manual = Caller(lambda: wrapper.deliver_next(MANUAL))
            wait_joined(joined)
            # Everything may finish now: what is left to decide is the order.
            device.release(4)
            manual.join()
            wrapper.wait_idle()

        assert manual.error is None
        # D1 was already on the valve and finished; the manual delivery went
        # next; D2 and D3 followed, in their order.
        assert device.attempts == [D1, MANUAL, D2, D3]
        # None dropped, none merged, never two on the valve at once.
        assert device.deliveries == [D1, MANUAL, D2, D3]
        assert device.max_on_valve == 1
        # Each drop reports once, in its own order; the manual delivery
        # reports to its caller, not to the completion queue.
        assert [done.request.reason for done in wrapper.completed()] == ["d1", "d2", "d3"]
        # The log says what it overtook: those drops arrive a delivery later
        # than the queued_behind on their REWARD events.
        assert any(
            "ahead of 2 queued delivery(ies)" in record.getMessage() for record in caplog.records
        ), [record.getMessage() for record in caplog.records]

    def test_its_caller_waits_only_for_the_train_on_the_valve_and_its_own(self, queued):
        # D1 and the manual delivery reach the device; whatever comes after
        # them waits at the door, where it has not started. A caller that
        # waited for D2 or D3 would never return.
        wrapper, device = queued(GatedReward(admit=2, hold=True))
        self.queue_three_drops(wrapper, device)
        joined = signal_on_enqueue(wrapper)

        manual = Caller(lambda: wrapper.deliver_next(MANUAL), device=device)
        wait_joined(joined)
        device.release(2)  # exactly two trains: the one on the valve, and its own
        manual.join()

        assert manual.error is None
        # When it returned, D2 and D3 had not started.
        assert manual.started_when_returned == [D1, MANUAL]
        # And they were not lost: let through, they follow in their order.
        assert isinstance(device, GatedReward)
        device.open_door()
        device.release(2)
        wrapper.wait_idle()
        assert device.deliveries == [D1, MANUAL, D2, D3]

    def test_a_failure_is_raised_on_the_callers_thread_and_the_queue_carries_on(self, queued):
        wrapper, device = queued(hold=True, fail=[2])  # delivery 2 is the manual one
        self.queue_three_drops(wrapper, device)
        joined = signal_on_enqueue(wrapper)

        manual = Caller(lambda: wrapper.deliver_next(MANUAL))
        wait_joined(joined)
        device.release(4)
        manual.join()
        wrapper.wait_idle()

        # Raised out of deliver_next, on the calling thread...
        assert isinstance(manual.error, RewardError)
        assert "scripted failure on delivery 2" in str(manual.error)
        # ...while the worker went on to deliver every queued drop.
        assert device.attempts == [D1, MANUAL, D2, D3]
        assert device.deliveries == [D1, D2, D3]
        assert [(done.request.reason, done.error) for done in wrapper.completed()] == [
            ("d1", None),
            ("d2", None),
            ("d3", None),
        ]

    def test_a_drop_asked_for_while_it_waits_counts_it_ahead(self, queued):
        # submit()'s count is the queued_behind on the drop's REWARD. The
        # manual delivery waiting for the valve goes before the new drop, so
        # the count must include it.
        wrapper, device = queued(hold=True)
        assert wrapper.submit(request("d1", pulses=D1)) == 0
        device.wait_for_attempts(1)
        joined = signal_on_enqueue(wrapper)
        manual = Caller(lambda: wrapper.deliver_next(MANUAL))
        wait_joined(joined)

        # D1 on the valve and the manual delivery waiting: two ahead.
        assert wrapper.submit(request("d2", pulses=D2)) == 2

        device.release(3)
        manual.join()
        wrapper.wait_idle()
        assert device.attempts == [D1, MANUAL, D2]  # which is where it went

    def test_several_go_in_the_order_asked_all_ahead_of_the_queue(self, queued):
        wrapper, device = queued(hold=True)
        wrapper.submit(request("d1", pulses=D1))
        device.wait_for_attempts(1)
        wrapper.submit(request("d2", pulses=D2))
        joined = signal_on_enqueue(wrapper)
        first = Caller(lambda: wrapper.deliver_next(MANUAL))
        wait_joined(joined)
        second = Caller(lambda: wrapper.deliver_next(MANUAL_2))
        wait_joined(joined)

        device.release(4)
        first.join()
        second.join()
        wrapper.wait_idle()
        assert device.attempts == [D1, MANUAL, MANUAL_2, D2]

    def test_with_nothing_queued_it_goes_straight_to_the_valve(self, queued, caplog):
        wrapper, device = queued()
        with caplog.at_level(logging.INFO, logger="alhazen.devices.reward"):
            wrapper.deliver_next(MANUAL)
        assert device.deliveries == [MANUAL]
        # Counted finished before it returned: not ahead of the next drop.
        assert wrapper.submit(request()) == 0
        # Nothing was overtaken, so nothing is said about overtaking.
        assert not any("ahead of" in record.getMessage() for record in caplog.records)

    def test_the_end_of_trial_pay_does_not_jump_the_queue(self, queued):
        wrapper, device = queued(hold=True)
        wrapper.submit(request("d1", pulses=D1))
        device.wait_for_attempts(1)
        wrapper.submit(request("d2", pulses=D2))
        joined = signal_on_enqueue(wrapper)

        pay = Caller(lambda: wrapper.deliver(END_PAY))
        wait_joined(joined)
        device.release(3)
        pay.join()

        assert pay.error is None
        assert device.attempts == [D1, D2, END_PAY]


class TestTheManualRewardHook:
    """make_manual_reward: the one closure behind the ``r`` key and the
    pause menu's R, as build_session wires it."""

    def test_no_dispenser_means_no_hook(self):
        assert make_manual_reward(None, MANUAL) is None

    def test_a_device_on_its_own_is_called_on_the_callers_thread(self):
        # A task that does not ask for reward mid-trial: the device itself,
        # exactly as before there was a worker.
        threads: list[threading.Thread] = []

        class Recording(SimulatedReward):
            def deliver(self, pulses: RewardPulses) -> None:
                threads.append(threading.current_thread())
                super().deliver(pulses)

        device = Recording()
        hook = make_manual_reward(device, MANUAL)
        assert hook is not None
        hook()
        assert device.deliveries == [MANUAL]
        assert threads == [threading.current_thread()]

    def test_through_the_worker_it_goes_ahead_of_the_queue(self, queued):
        wrapper, device = queued(hold=True)
        hook = make_manual_reward(wrapper, MANUAL)
        assert hook is not None
        wrapper.submit(request("d1", pulses=D1))
        device.wait_for_attempts(1)
        wrapper.submit(request("d2", pulses=D2))
        joined = signal_on_enqueue(wrapper)

        manual = Caller(hook)
        wait_joined(joined)
        device.release(3)
        manual.join()
        wrapper.wait_idle()
        assert device.attempts == [D1, MANUAL, D2]


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

    def test_the_manual_key_waits_for_the_drop_on_the_valve(self, tmp_path):
        # A train already on the valve is never cut short: the manual
        # delivery goes after it, never beside it or into it.
        device = ScriptedReward(hold=True)
        harness = session(
            tmp_path,
            device,
            # Frame 0 asks for a drop, which is on the valve by frame 1; the
            # experimenter's key lands on frame 2.
            phases=lambda: [DropsWithTheFirstOnTheValve(4, COMPLETED, device, on_frames=(0,))],
            commands=ScriptedCommands([[], [], [Command.MANUAL_REWARD]]),
        )
        assert harness.queued_reward is not None
        joined = signal_on_enqueue(harness.queued_reward)

        def operator():
            # The drop, then the manual delivery, in line before the valve
            # is let go.
            wait_joined(joined, 2)
            device.release(2)

        releaser = threading.Thread(target=operator)
        releaser.start()
        harness.runner.run()
        releaser.join()

        # The manual delivery (the harness's default RewardPulses) went on the
        # valve after the drop, never beside it.
        assert device.attempts == [DROP, RewardPulses()]
        assert device.max_on_valve == 1

    def test_the_manual_key_goes_ahead_of_the_queued_drops(self, tmp_path):
        device = ScriptedReward(hold=True)
        harness = session(
            tmp_path,
            device,
            # Drops on frames 0, 1 and 2: the first on the valve by frame 1,
            # the other two queued behind it. The key lands on frame 3.
            phases=lambda: [DropsWithTheFirstOnTheValve(5, COMPLETED, device, on_frames=(0, 1, 2))],
            commands=ScriptedCommands([[], [], [], [Command.MANUAL_REWARD]]),
        )
        assert harness.queued_reward is not None
        joined = signal_on_enqueue(harness.queued_reward)

        def operator():
            # Three drops, then the manual delivery, all in line while the
            # first drop is still held on the valve.
            wait_joined(joined, 4)
            device.release(1000)

        releaser = threading.Thread(target=operator)
        releaser.start()
        harness.runner.run()
        releaser.join()

        # The key's delivery went next after the drop on the valve; the two
        # queued drops followed it, and were not lost.
        assert device.attempts == [DROP, RewardPulses(), DROP, DROP]
        assert device.max_on_valve == 1
        rewards = events_named(harness.collector, "REWARD")
        drops = [event for event in rewards if not event.payload["manual"]]
        # Each drop's count of what was ahead of it when it was commanded.
        assert [event.payload.get("queued_behind") for event in drops] == [None, 1, 2]

        # Where the manual REWARD sits in the event stream, as documented:
        # between each drop's REWARD and its REWARD_DELIVERED. For the two
        # queued drops it is the delivery they waited for; the drop on the
        # valve finished first, but the key held the frame loop, so its
        # completion was drained after the manual REWARD too.
        events = harness.collector.events
        (manual_at,) = [
            index
            for index, event in enumerate(events)
            if event.name == "REWARD" and event.payload["manual"]
        ]
        commanded = {
            event.payload["frame"]: index
            for index, event in enumerate(events)
            if event.name == "REWARD" and not event.payload["manual"]
        }
        delivered = {
            event.payload["frame"]: index
            for index, event in enumerate(events)
            if event.name == "REWARD_DELIVERED"
        }
        assert sorted(delivered) == [0, 1, 2]
        for frame in (0, 1, 2):
            assert commanded[frame] < manual_at < delivered[frame]
        (row,) = read_trials(harness)
        assert row["n_mid_trial_rewards"] == "3"
        assert row["n_mid_trial_reward_failures"] == "0"

    def test_the_pause_menu_reward_still_works_between_trials(self, tmp_path):
        # Between trials settle_rewards has emptied the queue, so the pause
        # menu's reward goes straight to the valve, through the same worker.
        device = ScriptedReward()
        harness = session(
            tmp_path,
            device,
            n_trials=2,
            phases=lambda: [RequestRewardOnFrames(2, COMPLETED, on_frames=(0,))],
            # Trial 1 runs its three frames; the pause lands on trial 2's first.
            commands=ScriptedCommands([[], [], [], [Command.PAUSE]]),
        )
        # The pause menu holds the engine's hook, as build_session wires it.
        harness.runner._manual_reward = harness.engine._on_manual_reward
        choices = iter(["manual_reward", "resume"])
        harness.runner._on_pause = lambda menu: next(choices)
        harness.runner.run()

        # Trial 1's drop, the pause menu's reward, then the re-served trial's
        # drop.
        assert device.attempts == [DROP, RewardPulses(), DROP]
        names = harness.collector.names()
        (manual,) = [
            index
            for index, event in enumerate(harness.collector.events)
            if event.name == "REWARD" and event.payload.get("manual")
        ]
        assert names.index("PAUSED") < manual < names.index("RESUMED")
        assert [row["outcome"] for row in read_trials(harness)] == ["COMPLETED", "COMPLETED"]

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

    def test_the_manual_reward_goes_ahead_of_the_queue(self, tmp_path):
        # Wired by build_session itself: the engine's r key and the pause
        # menu hold one hook, and it overtakes the queued drops.
        device = ScriptedReward(hold=True)
        runner = build(tmp_path, Pursuit(Params()), reward=device)
        wrapper = runner._reward
        assert isinstance(wrapper, QueuedReward)
        try:
            assert runner._manual_reward is not None
            assert runner._engine._on_manual_reward is runner._manual_reward
            wrapper.submit(request("d1", pulses=D1))
            device.wait_for_attempts(1)
            wrapper.submit(request("d2", pulses=D2))
            joined = signal_on_enqueue(wrapper)

            manual = Caller(runner._manual_reward)
            wait_joined(joined)
            device.release(3)
            manual.join()
            wrapper.wait_idle()
            # RewardPulses() is build_session's manual reward by default.
            assert device.attempts == [D1, RewardPulses(), D2]
        finally:
            # The session never ran, so its teardown will not stop the worker.
            device.release(1000)
            wrapper.close()

    def test_without_it_the_manual_reward_reaches_the_device_itself(self, tmp_path):
        runner = build(
            tmp_path,
            Undeclared(Params()),
            rig_reward=RewardHwConfig(backend="simulated"),
        )
        assert runner._manual_reward is not None
        assert runner._engine._on_manual_reward is runner._manual_reward
        runner._manual_reward()
        assert isinstance(runner._reward, SimulatedReward)
        assert runner._reward.deliveries == [RewardPulses()]

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
