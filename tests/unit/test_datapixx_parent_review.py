"""Parent review: failures after a completed train stay uncertain, and
mixed definite/uncertain failures cannot turn an unknown dose into zero.
All hardware is a vendor-conformant stand-in; no pin or pump is exercised.
"""

from __future__ import annotations

import pytest
from test_reward_payer import CORRECT, PAID, Payer

from alhazen.config.models import RewardHwConfig, RewardPulses
from alhazen.core.trial import RewardCompletion, RewardRequest
from alhazen.devices import reward as reward_module
from alhazen.devices.reward import make_reward
from alhazen.errors import RewardError, RewardUncertainError
from alhazen.task.reward_policy import RewardPolicy
from fake_sdk import install_fake_libdpx
from support import EngineHarness

PUMP = {"model": "WPI SP220i", "syringe_diameter_mm": 14.5, "rate_ul_per_min": 3000.0}
GATE = RewardHwConfig(backend="datapixx", channel="dout0", polarity="active_low", pump=PUMP)
SHORT = RewardPulses(n_pulses=1, pulse_ms=3, inter_pulse_ms=0)


@pytest.fixture
def gate(monkeypatch):
    lib = install_fake_libdpx(monkeypatch)
    monkeypatch.setattr(reward_module.time, "sleep", lambda s: None)
    reward = make_reward(GATE)
    yield reward, lib
    reward.close()


def test_final_lock_failure_cannot_report_a_completed_train_as_not_delivered(gate, monkeypatch):
    reward, lib = gate
    original = reward._link.acquire

    def busy_after_train(doing, error):
        if "after a train" in doing:
            raise RewardError("shared link could not be acquired after the train")
        original(doing, error)

    # Deterministic injection at the lock boundary, after the real train path.
    monkeypatch.setattr(reward._link, "acquire", busy_after_train)
    with pytest.raises(RewardUncertainError):
        reward.deliver(SHORT)
    assert len(lib.trains) == 1


def test_sdk_exception_at_final_rest_is_an_uncertain_delivery(gate, monkeypatch):
    reward, lib = gate
    original = lib.DPxSetDoutValue
    failed = False

    def fail_once_after_train(value, mask):
        nonlocal failed
        if lib.trains and not failed:
            failed = True
            raise TypeError("SDK rejected final rest write")
        return original(value, mask)

    monkeypatch.setattr(lib, "DPxSetDoutValue", fail_once_after_train)
    with pytest.raises(RewardUncertainError):
        reward.deliver(SHORT)
    assert len(lib.trains) == 1


class CompletedDrops:
    def __init__(self, uncertain_flags):
        self.results = [
            RewardCompletion(
                request=RewardRequest(pulses=SHORT, reason="hold", frame=i),
                error="partial delivery" if uncertain else "nothing started",
                uncertain=uncertain,
            )
            for i, uncertain in enumerate(uncertain_flags)
        ]

    def wait_idle(self):
        return

    def completed(self):
        result, self.results = self.results, []
        return result


@pytest.mark.parametrize("flags", [(False, True), (True, False)])
def test_mixed_mid_trial_failures_never_invent_a_zero_delivery(flags):
    harness = EngineHarness(reward_requests=CompletedDrops(flags))
    ctx = harness.ctx()
    harness.engine.settle_rewards(ctx)
    assert ctx.record.get("rewarded") is None
    assert ctx.record["n_mid_trial_reward_failures"] == 2


def test_a_confirmed_drop_is_kept_when_other_deliveries_are_uncertain():
    harness = EngineHarness(reward_requests=CompletedDrops((False, True)))
    ctx = harness.ctx()
    ctx.record["rewarded"] = True
    harness.engine.settle_rewards(ctx)
    assert ctx.record["rewarded"] is True


def test_uncertain_end_payment_overrides_an_earlier_definite_failure():
    class Uncertain:
        def deliver(self, pulses):
            raise RewardUncertainError("train started; outcome unknown")

        def close(self):
            return

    payer = Payer(RewardPolicy(by_outcome={"CORRECT": PAID}), Uncertain())
    assert payer.deliver(CORRECT, rewarded=False, n_mid_trial_reward_failures=1)
    assert payer.ctx.record.get("rewarded") is None
    assert payer.events[-1][1]["uncertain"] is True
