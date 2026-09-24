"""A fraction written beside its threshold never contradicts the comparison.

Frame QA's recycle reason, its per-trial log line, the runner's failure-streak
pause and the alignment's refusal all print a measured fraction, a threshold,
or both, and say which side of the threshold the fraction fell on. Printed
with fixed decimals they could say the opposite: "(10.0%), over the 10%
budget", "over the 8% budget" for a 7.5% one, "(80% < 80%)".
"""

from __future__ import annotations

import operator

import numpy as np
import pytest

from alhazen.data.percents import compared_percents, threshold_percent

HOLDS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


class TestThresholdPercent:
    @pytest.mark.parametrize(
        ("threshold", "shown"),
        [
            # The shipped frame-QA budget and the alignment's default.
            (0.10, "10%"),
            (0.8, "80%"),
            # Whole percents wrote this one as "8%".
            (0.075, "7.5%"),
            (0.0725, "7.25%"),
            (0.855, "85.5%"),
            # 0.55 x 100 is 55.00000000000001 in floating point; what was set
            # is 55%.
            (0.55, "55%"),
            (1.0, "100%"),
            # More decimals than anyone types: written to six.
            (1 / 3, "33.333333%"),
        ],
    )
    def test_a_threshold_is_written_as_it_was_set(self, threshold, shown):
        assert threshold_percent(threshold) == shown

    @pytest.mark.parametrize("threshold", [float("nan"), float("inf"), float("-inf")])
    def test_a_threshold_that_is_not_a_number_is_refused(self, threshold):
        with pytest.raises(ValueError, match="not a finite number"):
            threshold_percent(threshold)


class TestComparedPercents:
    @pytest.mark.parametrize(
        ("dropped", "frames", "budget", "shown"),
        [
            # One decimal made each of these "over" a budget it printed as
            # equal to the fraction, or above it.
            (21, 209, 0.10, ("10.05%", "10%")),
            (6, 119, 0.05, ("5.04%", "5%")),
            (3, 39, 0.075, ("7.7%", "7.5%")),
        ],
    )
    def test_over_a_budget_reads_over_it(self, dropped, frames, budget, shown):
        assert compared_percents(dropped / frames, ">", budget, min_places=1) == shown

    def test_the_callers_usual_decimals_are_kept_when_they_already_read_true(self):
        # Frame QA's lines have always had one decimal; a fraction that is
        # clearly over keeps exactly that.
        assert compared_percents(3 / 20, ">", 0.10, min_places=1) == ("15.0%", "10%")
        assert compared_percents(2 / 2, ">", 0.10, min_places=1) == ("100.0%", "10%")

    def test_without_a_minimum_the_fewest_decimals_that_read_true(self):
        # 7.69% is "8%" to whole percents, and 8 is over 7.5: true as printed.
        assert compared_percents(3 / 39, ">", 0.075) == ("8%", "7.5%")
        assert compared_percents(21 / 209, ">", 0.10) == ("10.05%", "10%")

    def test_equal_is_true_where_the_test_allows_it(self):
        # 3 of 30 is exactly the 10% budget: within it, and it may say so.
        assert compared_percents(3 / 30, "<=", 0.10, min_places=1) == ("10.0%", "10%")
        assert compared_percents(0.10, ">=", 0.10) == ("10%", "10%")
        # 7.49% rounds onto 7.5% at one decimal, which "<=" allows.
        assert compared_percents(0.0749, "<=", 0.075, min_places=1) == ("7.5%", "7.5%")

    def test_a_strict_comparison_never_prints_equal_numbers(self):
        # 7.49% is "7.5%" to one decimal: equal to the threshold, so "<"
        # takes a second decimal.
        assert compared_percents(0.0749, "<", 0.075, min_places=1) == ("7.49%", "7.5%")

    def test_numbers_no_percent_can_separate_fall_back_to_full_precision(self):
        # 0.1 + 0.2 is 0.30000000000000004: over 0.3, but the same percent to
        # twelve decimals. Written unscaled and in full, it still reads over.
        assert compared_percents(0.1 + 0.2, ">", 0.3) == ("0.30000000000000004", "0.3")

    def test_a_relation_that_does_not_hold_is_refused(self):
        # A caller asking for this would print a message that lies.
        with pytest.raises(ValueError, match="which is false"):
            compared_percents(3 / 30, ">", 0.10)
        with pytest.raises(ValueError, match="which is false"):
            compared_percents(21 / 209, "<=", 0.10)

    def test_an_unknown_relation_is_refused(self):
        # Not one of the four a caller's test can find; the type forbids it,
        # and at run time it is refused rather than guessed at.
        with pytest.raises(ValueError, match="relation must be one of"):
            compared_percents(0.5, "==", 0.5)

    @pytest.mark.parametrize(("value", "threshold"), [(float("nan"), 0.1), (0.1, float("inf"))])
    def test_a_number_that_is_not_finite_is_refused(self, value, threshold):
        with pytest.raises(ValueError, match="not a finite number"):
            compared_percents(value, "<", threshold)

    @pytest.mark.parametrize("min_places", [-1, 13])
    def test_a_minimum_outside_the_search_is_refused(self, min_places):
        with pytest.raises(ValueError, match="min_places must be between 0 and 12"):
            compared_percents(0.2, ">", 0.1, min_places=min_places)


def _decimals(digits: str) -> int:
    """How many decimals a printed number has."""
    return len(digits.partition(".")[2])


def assert_reads_true(value, relation, threshold, min_places, shown):
    """The property every caller relies on, checked on what was printed.

    - the two printed numbers stand in ``relation`` to each other;
    - each is a correctly rounded value of its own number (nothing nudged);
    - the value has at least ``min_places`` decimals, and no more than the
      relation needs: every shorter rounding would have read false;
    - the threshold is written as it was set, to within six decimals.
    """
    value_text, threshold_text = shown
    holds = HOLDS[relation]
    if not value_text.endswith("%"):
        # The fallback: both unscaled and exact, so reading them back gives
        # the very floats that were compared.
        assert not threshold_text.endswith("%"), shown
        assert float(value_text) == value and float(threshold_text) == threshold, shown
        assert holds(float(value_text), float(threshold_text)), shown
        return

    value_digits, threshold_digits = value_text[:-1], threshold_text[:-1]
    printed_value, printed_threshold = float(value_digits), float(threshold_digits)
    context = (value, relation, threshold, shown)
    assert holds(printed_value, printed_threshold), context

    places = _decimals(value_digits)
    assert places >= min_places, context
    assert abs(printed_value - value * 100) <= 0.5 * 10.0**-places + 1e-9, context
    for fewer in range(min_places, places):
        shorter = float(f"{value * 100:.{fewer}f}")
        assert not holds(shorter, printed_threshold), (context, fewer)

    threshold_places = _decimals(threshold_digits)
    assert threshold_places <= 6, context
    tolerance = 1e-9 if threshold_places < 6 else 0.5e-6 + 1e-9
    assert abs(printed_threshold - threshold * 100) <= tolerance, context


def relations_that_hold(value, threshold):
    """Every relation the numbers satisfy: the strict one when they differ,
    and the non-strict one either way (both of those when they are equal)."""
    return [relation for relation, holds in HOLDS.items() if holds(value, threshold)]


class TestNeverContradicts:
    """Property-style: sweep what the callers actually compare and check the
    printed numbers against the comparison, whatever it came out as."""

    # The shipped defaults, the budgets that exposed the bug, thresholds with
    # decimals of their own, one with float noise in its scaling (0.55), one
    # that is itself float noise (0.1 + 0.2), and one that needs more than
    # six decimals (1/3).
    THRESHOLDS = [0.05, 0.075, 0.10, 0.2, 0.25, 0.5, 0.55, 0.8, 0.855, 0.0725, 0.1 + 0.2, 1 / 3]

    @pytest.mark.parametrize("threshold", THRESHOLDS)
    @pytest.mark.parametrize("min_places", [0, 1])
    def test_every_count_of_up_to_60_against_each_threshold(self, threshold, min_places):
        # A dropped-frame count, or a matched-event count: every d of n.
        for n in range(1, 61):
            for d in range(n + 1):
                value = d / n
                for relation in relations_that_hold(value, threshold):
                    shown = compared_percents(value, relation, threshold, min_places=min_places)
                    assert_reads_true(value, relation, threshold, min_places, shown)

    def test_arbitrary_fractions_and_ones_a_hair_from_the_threshold(self):
        # Deterministic: a fixed seed, so a failure is reproducible.
        rng = np.random.default_rng(20260923)
        thresholds = rng.uniform(0.0, 1.0, 400)
        anywhere = rng.uniform(0.0, 1.0, 400)
        # Within four float steps of the threshold, either side, or exactly
        # on it; and within a few millionths of it. That is where rounding
        # is most likely to carry one number onto the other.
        steps = rng.integers(-4, 5, 400)
        float_steps = thresholds + steps * np.spacing(thresholds)
        millionths = thresholds + rng.uniform(-5e-6, 5e-6, 400)
        for i, threshold in enumerate(thresholds.tolist()):
            for value in (anywhere[i], float_steps[i], millionths[i]):
                value = float(value)
                for relation in relations_that_hold(value, threshold):
                    for min_places in (0, 1):
                        shown = compared_percents(value, relation, threshold, min_places=min_places)
                        assert_reads_true(value, relation, threshold, min_places, shown)
