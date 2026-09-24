"""RewardPayer: the end-of-trial pay rule and its delivery, without a runner.

The session-level behaviour is pinned through whole sessions in
test_reward_policy.py, test_system_faults.py and test_mid_trial_reward.py.
These pin the rule where it now lives, fed one trial at a time: the payer
needs only a device, a policy, a display for its failure message and
somewhere to emit its events.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from alhazen.config.models import RewardPulses
from alhazen.core.trial import (
    ABORTED,
    DROPPED_FRAMES,
    FAULT_DROPPED_FRAMES,
    FAULT_TRACKER_STOPPED,
    PAUSED,
    Outcome,
)
from alhazen.session.reward_payer import RewardPayer
from alhazen.task.reward_policy import RewardPolicy
from alhazen.testing import FakeClock, FakeDisplay, ScriptedReward

CORRECT = Outcome("CORRECT", completed=True, success=True)
WRONG = Outcome("WRONG", completed=True, success=False)
PAID = RewardPulses(n_pulses=2, pulse_ms=40, inter_pulse_ms=60)
FAULT_PAY = RewardPulses(n_pulses=1, pulse_ms=30, inter_pulse_ms=0)


class Payer:
    """A RewardPayer wired to a scripted device, a fake display and a list of
    the events it emits, as (name, payload)."""

    def __init__(self, policy: RewardPolicy | None, device: ScriptedReward | None = None):
        self.device = device if device is not None else ScriptedReward()
        self.display = FakeDisplay(FakeClock(), 1 / 60)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.payer = RewardPayer(
            self.device, policy, self.display, lambda ctx, name, p: self.events.append((name, p))
        )

    def deliver(self, outcome: Outcome, fault: str | None = None, **record: Any) -> bool:
        # The payer reads the trial's record and index off its context, and
        # nothing else.
        self.ctx = SimpleNamespace(record=dict(record), trial_index=4)
        return self.payer.deliver(self.ctx, outcome, fault=fault)


class TestTheRule:
    def test_a_response_is_paid_by_its_outcome(self):
        payer = Payer(RewardPolicy(by_outcome={"CORRECT": PAID}))
        assert payer.deliver(CORRECT) is False
        assert payer.device.deliveries == [PAID]
        assert payer.ctx.record["rewarded"] is True
        assert payer.events == [
            ("REWARD", {"manual": False, "outcome": "CORRECT", "pulses": PAID.model_dump()})
        ]

    def test_a_dropped_frames_trial_is_paid_for_its_response(self):
        # The outcome handed over is the response frame QA replaced.
        payer = Payer(RewardPolicy(by_outcome={"CORRECT": PAID}, on_fault=FAULT_PAY))
        payer.deliver(CORRECT, fault=FAULT_DROPPED_FRAMES)
        assert payer.device.deliveries == [PAID]
        assert "fault" not in payer.events[0][1]

    def test_a_trial_the_tracker_cut_short_is_paid_the_fault_reward(self):
        payer = Payer(RewardPolicy(by_outcome={"ABORTED": PAID}, on_fault=FAULT_PAY))
        payer.deliver(ABORTED, fault=FAULT_TRACKER_STOPPED)
        assert payer.device.deliveries == [FAULT_PAY]
        # The key that tells a fault reward from a reward for a response.
        assert payer.events[0][1]["fault"] == FAULT_TRACKER_STOPPED

    def test_a_paused_trial_pays_nothing_and_says_nothing(self):
        payer = Payer(RewardPolicy(by_outcome={"PAUSED": PAID}))
        assert payer.deliver(PAUSED) is False
        assert payer.device.deliveries == []
        assert payer.events == []

    def test_no_policy_pays_nothing(self):
        payer = Payer(None)
        assert payer.deliver(CORRECT) is False
        assert payer.events == []

    def test_a_rebound_policy_is_the_one_paid_from(self):
        # Every stage transition rebinds it.
        payer = Payer(RewardPolicy(by_outcome={"CORRECT": PAID}))
        payer.payer.policy = RewardPolicy(by_outcome={"CORRECT": FAULT_PAY})
        payer.deliver(CORRECT)
        assert payer.device.deliveries == [FAULT_PAY]


class TestNothingEarned:
    def test_a_completed_trial_that_earned_nothing_says_so(self):
        payer = Payer(RewardPolicy(by_outcome={"CORRECT": PAID}))
        payer.deliver(WRONG)
        assert payer.events == [("NO_REWARD", {"outcome": "WRONG"})]

    def test_not_when_its_phases_asked_for_drops(self):
        payer = Payer(RewardPolicy(by_outcome={"CORRECT": PAID}))
        payer.deliver(WRONG, n_mid_trial_rewards_cancelled=1)
        assert payer.events == []

    def test_not_for_a_trial_that_did_not_complete(self):
        payer = Payer(RewardPolicy(by_outcome={"CORRECT": PAID}))
        payer.deliver(DROPPED_FRAMES)
        assert payer.events == []


class TestAPumpFailure:
    def test_it_is_reported_for_a_pause_and_the_trial_kept(self):
        payer = Payer(RewardPolicy(by_outcome={"CORRECT": PAID}), ScriptedReward(fail=[1]))
        assert payer.deliver(CORRECT) is True
        assert payer.ctx.record["rewarded"] is False
        assert payer.events == [("REWARD_FAILED", {"outcome": "CORRECT"})]
        assert payer.display.message_calls[-1][0] == "REWARD FAILURE — check the pump"

    def test_a_drop_that_already_arrived_keeps_the_trial_rewarded(self):
        payer = Payer(RewardPolicy(by_outcome={"CORRECT": PAID}), ScriptedReward(fail=[1]))
        payer.deliver(CORRECT, rewarded=True)
        assert payer.ctx.record["rewarded"] is True
