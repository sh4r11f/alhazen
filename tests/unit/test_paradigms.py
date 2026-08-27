"""The scheduler contract, on its first implementation."""

from __future__ import annotations

import numpy as np
import pytest

from alhazen.core.engine import TrialResult
from alhazen.paradigms.base import Condition, SimpleSequence
from support import COMPLETED, FAILED


def result(outcome):
    return TrialResult(outcome=outcome, record={})


class TestCondition:
    def test_key_is_order_insensitive(self):
        a = Condition({"x": 1, "y": 2})
        b = Condition({"y": 2, "x": 1})
        assert a.key() == b.key()


class TestSimpleSequence:
    def conditions(self):
        return [Condition({"c": 1}), Condition({"c": 2})]

    def test_serves_each_condition_n_times(self):
        source = SimpleSequence(self.conditions(), n_repeats=3, shuffle=False)
        served = []
        while (c := source.next()) is not None:
            served.append(c.params["c"])
            source.record(c, result(COMPLETED))
        assert sorted(served) == [1, 1, 1, 2, 2, 2]

    def test_shuffle_is_deterministic_per_rng(self):
        orders = []
        for _ in range(2):
            source = SimpleSequence(self.conditions(), n_repeats=5, rng=np.random.default_rng(7))
            order = []
            while (c := source.next()) is not None:
                order.append(c.params["c"])
                source.record(c, result(COMPLETED))
            orders.append(order)
        assert orders[0] == orders[1]

    def test_non_completed_outcomes_requeue(self):
        source = SimpleSequence([Condition({"c": 1})], n_repeats=1, shuffle=False)
        c = source.next()
        source.record(c, result(FAILED))  # did not produce a measurement
        assert source.next() is not None  # served again
        source.record(c, result(COMPLETED))
        assert source.next() is None

    def test_shuffle_requires_rng(self):
        with pytest.raises(ValueError, match="rng"):
            SimpleSequence(self.conditions(), shuffle=True)

    def test_n_repeats_validated(self):
        with pytest.raises(ValueError, match="n_repeats"):
            SimpleSequence(self.conditions(), n_repeats=0, shuffle=False)
