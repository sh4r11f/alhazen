"""The scheduler library: exact completed-count guarantees, staircase
behavior, QUEST+ convergence, and block structure."""

from __future__ import annotations

import numpy as np
import pytest

from alhazen.core.engine import TrialResult
from alhazen.core.trial import Outcome
from alhazen.paradigms import (
    AdjustmentTrials,
    BlockPlan,
    Condition,
    ConstantStimuli,
    InterleavedStaircases,
    QuestPlus,
    QuestPlusEstimator,
    SimpleSequence,
    UpDownStaircase,
    weibull,
)

HIT = Outcome("HIT", completed=True, success=True)
MISS = Outcome("MISS", completed=True, success=False)
BROKE = Outcome("BROKE", completed=False)


def result(outcome: Outcome) -> TrialResult:
    return TrialResult(outcome=outcome, record={})


def drain(source, answer=lambda condition: HIT, limit=500):
    """Run a source to exhaustion, answering each condition; returns the
    conditions served in order."""
    served = []
    while (condition := source.next()) is not None:
        served.append(condition)
        source.record(condition, result(answer(condition)))
        if len(served) > limit:
            raise AssertionError("scheduler never finished")
    return served


class TestConstantStimuli:
    def grids(self):
        return {"side": ["left", "right"], "contrast": [0.2, 0.8]}

    def test_full_factorial_repeated_n_times(self):
        source = ConstantStimuli(self.grids(), n_per_condition=3, rng=np.random.default_rng(0))
        served = drain(source)
        assert len(served) == 2 * 2 * 3
        counts = {}
        for condition in served:
            counts[condition.key()] = counts.get(condition.key(), 0) + 1
        assert set(counts.values()) == {3}

    def test_failed_attempts_never_consume_a_repetition(self):
        # The guarantee the whole scheduler exists for: an under-sampled cell
        # is a bias no analysis can undo.
        source = ConstantStimuli(self.grids(), n_per_condition=2, rng=np.random.default_rng(1))
        seen: dict[tuple, int] = {}

        def flaky(condition):
            seen[condition.key()] = seen.get(condition.key(), 0) + 1
            return BROKE if seen[condition.key()] == 1 else HIT

        drain(source, answer=flaky)
        summary = source.summary()
        assert list(summary["n_completed"]) == [2, 2, 2, 2]
        assert summary["n_attempts"].sum() == 8 + 4  # four cells failed once each

    def test_same_seed_same_order(self):
        orders = [
            [c.key() for c in drain(ConstantStimuli(self.grids(), 2, np.random.default_rng(7)))]
            for _ in range(2)
        ]
        assert orders[0] == orders[1]

    def test_empty_plans_are_rejected(self):
        with pytest.raises(ValueError, match="n_per_condition"):
            ConstantStimuli(self.grids(), n_per_condition=0, rng=np.random.default_rng(0))
        with pytest.raises(ValueError, match="at least one condition"):
            ConstantStimuli({}, rng=np.random.default_rng(0))


class TestUpDownStaircase:
    def stair(self, **kwargs):
        defaults = dict(parameter="contrast", start=0.5, step=0.1, n_up=1, n_down=2, n_trials=20)
        return UpDownStaircase(**{**defaults, **kwargs})

    def test_two_hits_step_down_one_miss_steps_up(self):
        stair = self.stair()
        stair.record(stair.next(), result(HIT))
        assert stair.value == pytest.approx(0.5)  # one hit is not enough
        stair.record(stair.next(), result(HIT))
        assert stair.value == pytest.approx(0.4)  # harder
        stair.record(stair.next(), result(MISS))
        assert stair.value == pytest.approx(0.5)  # easier again

    def test_consecutive_means_consecutive(self):
        stair = self.stair()
        for outcome in (HIT, MISS, HIT):
            stair.record(stair.next(), result(outcome))
        # The miss reset the run of hits, so the second hit is a run of one.
        assert stair.value == pytest.approx(0.6)  # only the miss stepped

    def test_incomplete_trials_do_not_move_it(self):
        stair = self.stair()
        for _ in range(5):
            stair.record(stair.next(), result(BROKE))
        assert stair.value == pytest.approx(0.5)
        assert stair.history == []

    def test_reversals_are_counted_and_bound_the_run(self):
        stair = self.stair(n_reversals=2, n_trials=None)
        drain(stair, answer=lambda c: HIT if len(stair.history) % 3 else MISS)
        assert len(stair.reversals) >= 2
        assert stair.finished

    def test_bounds_are_respected(self):
        stair = self.stair(start=0.05, step=0.1, min_value=0.0, max_value=1.0)
        stair.record(stair.next(), result(HIT))
        stair.record(stair.next(), result(HIT))
        assert stair.value == 0.0

    def test_a_staircase_must_have_a_stopping_rule(self):
        with pytest.raises(ValueError, match="must stop"):
            UpDownStaircase(parameter="c", start=0.5, step=0.1)


class TestInterleavedStaircases:
    def make(self, seed=0):
        return InterleavedStaircases(
            {
                "easy": UpDownStaircase("contrast", start=0.8, step=0.1, n_trials=4),
                "hard": UpDownStaircase("contrast", start=0.2, step=0.1, n_trials=4),
            },
            rng=np.random.default_rng(seed),
        )

    def test_every_staircase_runs_to_its_own_count(self):
        source = self.make()
        served = drain(source)
        labels = [c.params["staircase"] for c in served]
        assert labels.count("easy") == labels.count("hard") == 4

    def test_serving_is_interleaved_not_blocked(self):
        labels = [c.params["staircase"] for c in drain(self.make(seed=3))]
        # Both staircases appear in the first half: a subject who tires
        # partway through must affect them equally.
        assert set(labels[:4]) == {"easy", "hard"}

    def test_deterministic_per_seed(self):
        first = [c.params["staircase"] for c in drain(self.make(seed=11))]
        second = [c.params["staircase"] for c in drain(self.make(seed=11))]
        assert first == second

    def test_a_foreign_condition_is_rejected(self):
        source = self.make()
        with pytest.raises(ValueError, match="none of"):
            source.record(Condition({"staircase": "elsewhere"}), result(HIT))


class TestQuestPlus:
    def test_estimator_recovers_a_known_threshold(self):
        rng = np.random.default_rng(0)
        true_threshold, true_slope = 0.35, 3.5
        estimator = QuestPlusEstimator(
            intensities=np.linspace(0.05, 1.0, 20),
            thresholds=np.linspace(0.05, 1.0, 20),
            slopes=[2.0, 3.5, 5.0],
        )
        for _ in range(64):
            intensity = estimator.next_intensity()
            p = weibull(intensity, true_threshold, true_slope, 0.05, 0.02)
            estimator.add_response(intensity, bool(rng.random() < p))
        estimate = estimator.estimate()
        assert estimate["threshold"] == pytest.approx(true_threshold, abs=0.1)
        # And it is more certain than it started.
        assert estimator.entropy() < np.log(20 * 20 * 3)

    def test_entropy_only_falls(self):
        estimator = QuestPlusEstimator(intensities=[0.2, 0.5, 0.8], thresholds=[0.2, 0.5, 0.8])
        before = estimator.entropy()
        estimator.add_response(0.5, True)
        assert estimator.entropy() < before

    def test_incomplete_trials_re_serve_the_same_intensity_untouched(self):
        # Scoring a fixation break as a failure would drag the threshold
        # toward wherever the subject stopped cooperating.
        source = QuestPlus("contrast", intensities=[0.2, 0.5, 0.8], thresholds=[0.2, 0.5, 0.8])
        first = source.next()
        source.record(first, result(BROKE))
        second = source.next()
        assert second.params == first.params
        # And the posterior heard nothing at all about the failed attempt.
        assert int(source.summary()["n_trials"].iloc[0]) == 0

    def test_interleaved_levels_round_robin(self):
        source = QuestPlus(
            "contrast",
            intensities=[0.2, 0.5, 0.8],
            thresholds=[0.2, 0.5, 0.8],
            n_trials=3,
            interleave_by="speed",
            interleave_levels=[2.0, 8.0],
        )
        served = drain(source)
        assert [c.params["speed"] for c in served[:4]] == [2.0, 8.0, 2.0, 8.0]
        assert len(served) == 6

    def test_the_score_callable_decides_what_success_means(self):
        # A task titrating a magnitude rather than accuracy: outcome.success
        # is irrelevant here, and the scheduler must not assume otherwise.
        seen = []
        source = QuestPlus(
            "contrast",
            intensities=[0.2, 0.5],
            thresholds=[0.2, 0.5],
            n_trials=2,
            score=lambda r: seen.append(r) or True,
        )
        condition = source.next()
        source.record(condition, result(MISS))
        assert len(seen) == 1

    def test_a_foreign_level_is_rejected(self):
        source = QuestPlus(
            "contrast",
            intensities=[0.2],
            thresholds=[0.2],
            interleave_by="speed",
            interleave_levels=[2.0],
        )
        with pytest.raises(ValueError, match="matches none"):
            source.record(Condition({"contrast": 0.2, "speed": 99.0}), result(HIT))

    def test_summary_has_one_row_per_staircase(self):
        source = QuestPlus(
            "contrast",
            intensities=[0.2, 0.5],
            thresholds=[0.2, 0.5],
            n_trials=1,
            interleave_by="speed",
            interleave_levels=[2.0, 8.0],
        )
        drain(source)
        summary = source.summary()
        assert list(summary["interleave_value"]) == [2.0, 8.0]
        assert set(summary.columns) >= {"threshold", "slope", "n_trials"}


class TestWeibull:
    def test_runs_between_the_asymptotes(self):
        low = weibull(0.001, 0.5, 3.5, 0.05, 0.02)
        high = weibull(100.0, 0.5, 3.5, 0.05, 0.02)
        assert low == pytest.approx(0.05, abs=0.01)
        assert high == pytest.approx(0.98, abs=0.01)

    def test_every_scale_is_monotonic_in_intensity(self):
        for scale in ("linear", "log10", "dB"):
            values = [
                float(weibull(x, 0.5, 3.5, 0.05, 0.02, scale=scale))
                for x in (0.1, 0.3, 0.5, 0.7, 0.9)
            ]
            assert values == sorted(values)


class TestBlockPlan:
    def inner(self, n=4):
        return SimpleSequence([Condition({"i": i}) for i in range(n)], rng=np.random.default_rng(0))

    def test_blocks_are_stamped_into_the_condition(self):
        plan = BlockPlan(self.inner(2), n_blocks=2, trials_per_block=2)
        served = drain(plan)
        assert [c.params["block"] for c in served] == [1, 1]
        # The single inner source is exhausted by the first block; the second
        # has nothing left to serve, which ends the session.

    def test_one_source_per_block(self):
        plan = BlockPlan([self.inner(2), self.inner(2)], trials_per_block=2)
        served = drain(plan)
        assert [c.params["block"] for c in served] == [1, 1, 2, 2]

    def test_a_failed_trial_comes_back_inside_its_own_block(self):
        # End-of-block recycling: the block is bounded by COMPLETED trials, so
        # the inner scheduler's re-queue lands the retry back in this block.
        plan = BlockPlan([self.inner(3), self.inner(3)], trials_per_block=3)
        seen = {"failed": False}

        def flaky(condition):
            if not seen["failed"]:
                seen["failed"] = True
                return BROKE
            return HIT

        served = drain(plan, answer=flaky)
        blocks = [c.params["block"] for c in served]
        assert blocks == [1, 1, 1, 1, 2, 2, 2]  # four trials in block 1: three plus the retry

    def test_summary_counts_completed_trials_per_block(self):
        plan = BlockPlan([self.inner(2), self.inner(2)], trials_per_block=2)
        drain(plan)
        summary = plan.summary()
        assert list(summary["block"]) == [1, 2]
        assert list(summary["n_completed"]) == [2, 2]

    def test_contradictory_block_counts_are_rejected(self):
        with pytest.raises(ValueError, match="contradicts"):
            BlockPlan([self.inner(1), self.inner(1)], n_blocks=3)

    def test_many_blocks_over_one_source_needs_a_block_length(self):
        """Deviation 19, and untested in both repos. One queue shared across
        blocks with nothing to say where a block ends means the first block
        drains it and the rest are empty — a session that silently collects a
        fraction of its plan."""
        with pytest.raises(ValueError, match="trials_per_block"):
            BlockPlan(self.inner(4), n_blocks=3)

    def test_one_block_over_one_source_needs_nothing_extra(self):
        # A single block cannot be starved by the block before it.
        plan = BlockPlan(self.inner(2), n_blocks=1)
        assert [c.params["block"] for c in drain(plan)] == [1, 1]

    def test_a_single_source_still_needs_a_block_count(self):
        with pytest.raises(ValueError, match="n_blocks"):
            BlockPlan(self.inner(2))

    def test_zero_blocks_is_refused(self):
        with pytest.raises(ValueError, match="n_blocks"):
            BlockPlan(self.inner(2), n_blocks=0)

    def test_an_empty_source_list_is_refused(self):
        with pytest.raises(ValueError, match="at least one"):
            BlockPlan([])

    def test_a_zero_length_block_is_refused(self):
        with pytest.raises(ValueError, match="trials_per_block"):
            BlockPlan([self.inner(2)], trials_per_block=0)


class TestAdjustmentTrials:
    def test_serves_each_condition_n_times(self):
        source = AdjustmentTrials(
            2,
            conditions=[Condition({"start": 0.1}), Condition({"start": 0.9})],
            rng=np.random.default_rng(0),
        )
        served = drain(source)
        assert len(served) == 4

    def test_a_trial_with_no_setting_is_re_queued(self):
        source = AdjustmentTrials(2)
        attempts = {"n": 0}

        def flaky(condition):
            attempts["n"] += 1
            return BROKE if attempts["n"] == 1 else HIT

        served = drain(source, answer=flaky)
        assert len(served) == 3  # two settings collected, one attempt wasted
        assert int(source.summary()["n_completed"].iloc[0]) == 2
