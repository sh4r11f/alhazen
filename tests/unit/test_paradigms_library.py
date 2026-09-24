"""The scheduler library: exact completed-count guarantees, staircase
behavior, QUEST+ convergence, and block structure."""

from __future__ import annotations

import re

import numpy as np
import pytest

from alhazen.core.engine import TrialResult
from alhazen.core.trial import Outcome
from alhazen.paradigms import (
    AdjustmentTrials,
    BlockConfig,
    BlockPlan,
    Condition,
    ConstantStimuli,
    InterleavedStaircases,
    QuestPlus,
    QuestPlusEstimator,
    SchedulerConfig,
    SimpleSequence,
    UpDownStaircase,
    make_scheduler,
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

    def test_the_score_callable_decides_what_success_means(self):
        # As for QUEST+: a task titrating a magnitude rather than accuracy
        # says what a success is, and the staircase steps on that.
        stair = self.stair(score=lambda r: not r.outcome.success)
        stair.record(stair.next(), result(HIT))
        assert stair.value == pytest.approx(0.6)  # a HIT scored as a failure: easier
        assert stair.history == [(0.5, False)]

    def test_an_incomplete_trial_never_reaches_the_score_callable(self):
        seen = []
        stair = self.stair(score=lambda r: seen.append(r) or True)
        stair.record(stair.next(), result(BROKE))
        assert seen == []
        assert stair.history == []


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

    def test_a_finished_block_leaves_a_break_until_it_is_taken(self):
        """Pending from the end of a block until the runner takes it, once,
        and never after the last block: the end of the session is not a
        rest."""
        plan = BlockPlan([self.inner(1), self.inner(1), self.inner(1)], trials_per_block=1)
        assert plan.take_block_break() is None
        first = plan.next()
        plan.record(first, result(HIT))
        assert plan.take_block_break() is None  # block 1 has not ended yet
        second = plan.next()  # ends block 1, starts block 2
        assert plan.take_block_break() == (1, 3)
        assert plan.take_block_break() is None  # taken
        plan.record(second, result(HIT))
        third = plan.next()
        assert plan.take_block_break() == (2, 3)
        plan.record(third, result(HIT))
        assert plan.next() is None
        assert plan.take_block_break() is None

    def test_breaks_can_be_switched_off(self):
        plan = BlockPlan([self.inner(1), self.inner(1)], trials_per_block=1, breaks=False)
        plan.record(plan.next(), result(HIT))
        plan.next()
        assert plan.take_block_break() is None

    def test_block_boundaries_go_in_the_session_log(self, caplog):
        """No event (the module docstring says why), but the log has to show
        where a block began and ended, or a between-block validation cannot
        be placed against the trials it covered."""
        import logging

        plan = BlockPlan([self.inner(2), self.inner(1)], trials_per_block=2)
        with caplog.at_level(logging.INFO, logger="alhazen.paradigms.blocks"):
            drain(plan)
        assert [r.message for r in caplog.records] == [
            "block 1 of 2 starts",
            "block 1 of 2 ends: 2 completed trials",
            "block 2 of 2 starts",
            "block 2 of 2 ends: 1 completed trials",
        ]

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


def every_third_breaks(source) -> list[str]:
    """Run ``source`` to exhaustion with every third SERVED trial breaking, so
    the re-queue path is part of the order, and return each served condition
    as a short tag (its values in key order)."""
    served: list[str] = []
    while (condition := source.next()) is not None:
        served.append("".join(str(v) for _, v in sorted(condition.params.items())))
        outcome = BROKE if len(served) % 3 == 0 else HIT
        source.record(condition, result(outcome))
    return served


class TestSeededOrdersAreUnchanged:
    """Seed discipline for the queue the three queue-based schedulers share.

    SimpleSequence, AdjustmentTrials and ConstantStimuli each carried their
    own copy of "shuffle once, serve from the front, re-queue a non-completed
    trial at the back"; they now share SimpleSequence's. A seed must still
    produce the session it always did, so every order below was recorded on
    the code BEFORE the copies were merged: same seed, same draws, same
    serve order including the retries, same summaries. The last number is
    the next draw from the same Generator after the session — it pins how
    many draws the scheduler took, which matters because make_scheduler
    hands one Generator to every block's scheduler in turn."""

    CONDITIONS = [Condition({"i": i}) for i in range(3)]

    # seed -> (serve order, next draw after the session)
    SEQUENCE = {
        0: (["1", "1", "2", "2", "0", "0", "2", "0"], 16527),
        1: (["2", "0", "1", "0", "2", "1", "1", "1"], 948649),
        7: (["2", "1", "0", "2", "0", "1", "0", "1"], 775685),
    }

    @pytest.mark.parametrize("seed", sorted(SEQUENCE))
    def test_simple_sequence(self, seed):
        rng = np.random.default_rng(seed)
        source = SimpleSequence(self.CONDITIONS, n_repeats=2, rng=rng)

        assert (every_third_breaks(source), int(rng.integers(1_000_000))) == self.SEQUENCE[seed]

    @pytest.mark.parametrize("seed", sorted(SEQUENCE))
    def test_adjustment_trials_serve_what_a_sequence_serves(self, seed):
        # Same plan, same draw: AdjustmentTrials differed from SimpleSequence
        # only in its defaults, and the recorded orders were already equal.
        rng = np.random.default_rng(seed)
        source = AdjustmentTrials(2, conditions=self.CONDITIONS, rng=rng)

        assert (every_third_breaks(source), int(rng.integers(1_000_000))) == self.SEQUENCE[seed]
        assert source.summary().to_dict("records") == [{"n_completed": 6, "n_remaining": 0}]

    @pytest.mark.parametrize(("seed", "next_draw"), [(0, 850624), (1, 473188), (7, 944904)])
    def test_one_adjustment_condition_takes_no_draw(self, seed, next_draw):
        # A single condition has no order to shuffle, so it must leave the
        # Generator exactly where it found it (the next draw is the seed's
        # very first).
        rng = np.random.default_rng(seed)
        source = AdjustmentTrials(3, conditions=[Condition({"i": 5})], rng=rng)

        assert every_third_breaks(source) == ["5"] * 4
        assert int(rng.integers(1_000_000)) == next_draw

    # seed -> (serve order, n_attempts per cell in summary order, next draw)
    CONSTANT = {
        0: (
            ["2x", "1x", "2y", "2x", "1y", "1x", "1y", "2y", "2y", "1x", "2y"],
            [3, 2, 2, 4],
            175267,
        ),
        1: (
            ["1y", "1x", "1y", "1x", "2x", "2x", "2y", "2y", "1y", "2x", "1y"],
            [2, 4, 3, 2],
            869025,
        ),
        7: (
            ["1x", "2x", "2y", "2x", "1x", "1y", "1y", "2y", "2y", "1y", "2y"],
            [2, 3, 2, 4],
            55531,
        ),
    }

    @pytest.mark.parametrize("seed", sorted(CONSTANT))
    def test_constant_stimuli(self, seed):
        rng = np.random.default_rng(seed)
        source = ConstantStimuli({"b": ["x", "y"], "a": [1, 2]}, n_per_condition=2, rng=rng)

        order = every_third_breaks(source)
        summary = source.summary()

        expected_order, expected_attempts, expected_draw = self.CONSTANT[seed]
        assert order == expected_order
        assert list(summary["n_attempts"]) == expected_attempts
        assert list(summary["n_completed"]) == [2, 2, 2, 2]
        assert int(rng.integers(1_000_000)) == expected_draw

    BLOCKS = {
        0: (["2x1", "1x1", "1y1", "2y1", "1y1", "2y2", "2x2", "1y2", "1x2", "2y2", "1x2"], 16527),
        1: (["1x1", "1y1", "2x1", "2y1", "2x1", "2y2", "1x2", "2x2", "1y2", "2y2", "1y2"], 948649),
        7: (["1x1", "2x1", "1y1", "2y1", "1y1", "2y2", "1y2", "2x2", "1x2", "2y2", "1x2"], 833651),
    }

    @pytest.mark.parametrize("seed", sorted(BLOCKS))
    def test_constant_stimuli_in_blocks_from_a_config(self, seed):
        # One ConstantStimuli per block, all drawing from the one Generator.
        rng = np.random.default_rng(seed)
        cfg = SchedulerConfig(kind="constant", n_per_condition=1, blocks=BlockConfig(n_blocks=2))
        grid = [Condition({"a": a, "b": b}) for a in (1, 2) for b in ("x", "y")]
        source = make_scheduler(cfg, grid, rng)

        assert (every_third_breaks(source), int(rng.integers(1_000_000))) == self.BLOCKS[seed]


class TaskThatForgotToReturn:
    """A task whose ``score_trial`` works out its verdict and never returns
    it — the mistake that used to score every trial a failure, silently."""

    def score_trial(self, result):
        _ = bool(result.outcome.success)


class TestAScorerMustAnswerTrueOrFalse:
    """An adaptive scheduler steps on its scorer's verdict. A scorer that
    returns None (a ``score_trial`` missing its ``return``) was read as False
    on every trial, so the staircase walked to its easiest level and QUEST+
    fitted an observer who never succeeds — and nothing said so. Anything but
    a real boolean (``bool`` or a numpy bool) now stops at the first scored
    trial, naming the scorer and what it returned.

    0 and 1 are refused too. A scorer returning an int is most likely
    returning a count or a magnitude (a number of correct responses, an
    error in pixels), and bool() of that is True for every non-zero value —
    the same silent misreading in the other direction. ``bool(...)`` in the
    scorer is a one-word fix that says what was meant."""

    def staircase(self, score):
        return UpDownStaircase("contrast", start=0.5, step=0.1, n_trials=4, score=score)

    def quest(self, score):
        return QuestPlus(
            "contrast", intensities=[0.2, 0.5], thresholds=[0.2, 0.5], n_trials=4, score=score
        )

    @pytest.fixture(params=["staircase", "quest"])
    def make(self, request):
        return getattr(self, request.param)

    def test_a_scorer_that_returns_none_stops_the_session(self, make):
        source = make(TaskThatForgotToReturn().score_trial)

        with pytest.raises(TypeError) as excinfo:
            source.record(source.next(), result(HIT))

        message = str(excinfo.value)
        assert "TaskThatForgotToReturn.score_trial" in message
        assert "None" in message

    @pytest.mark.parametrize("verdict", [1, 0, 0.0, "yes", np.float64(1.0)])
    def test_anything_else_that_is_not_a_boolean_is_refused(self, make, verdict):
        source = make(lambda r: verdict)

        with pytest.raises(TypeError, match=re.escape(repr(verdict))):
            source.record(source.next(), result(HIT))

    @pytest.mark.parametrize("verdict", [True, False, np.bool_(True), np.bool_(False)])
    def test_python_and_numpy_booleans_are_accepted(self, make, verdict):
        source = make(lambda r: verdict)

        source.record(source.next(), result(HIT))

        assert int(source.summary()["n_trials"].iloc[0]) == 1

    def test_the_staircase_history_holds_real_booleans(self):
        stair = self.staircase(lambda r: np.bool_(True))

        stair.record(stair.next(), result(HIT))

        assert stair.history == [(0.5, True)]
        assert type(stair.history[0][1]) is bool


class TestBlockPlanNeverCutsAPlan:
    """A hand-built BlockPlan used to end a queue-based block after
    ``trials_per_block`` completed trials whatever that block's source still
    had queued — so a bound below the plan dropped planned trials, retries
    first, silently. make_scheduler already refused that for a config; a
    source that says how much of its plan is left (``remaining()``) is now
    checked by BlockPlan itself, at construction."""

    def grid(self):
        return {"side": ["left", "right"], "contrast": [0.2, 0.8]}

    def constant(self):
        return ConstantStimuli(self.grid(), n_per_condition=2, rng=np.random.default_rng(0))

    def test_a_bound_below_a_blocks_plan_is_refused_with_the_numbers(self):
        with pytest.raises(ValueError) as excinfo:
            BlockPlan([self.constant(), self.constant()], trials_per_block=5)

        message = str(excinfo.value)
        assert "trials_per_block=5" in message
        assert "block 1" in message
        assert "8 planned trials" in message
        assert "the other 3" in message

    @pytest.mark.parametrize(
        "make",
        [
            lambda: SimpleSequence([Condition({"i": i}) for i in range(3)], shuffle=False),
            lambda: AdjustmentTrials(3),
        ],
        ids=["sequence", "adjustment"],
    )
    def test_every_queue_based_source_is_checked(self, make):
        with pytest.raises(ValueError, match="trials_per_block=2"):
            BlockPlan([make(), make()], trials_per_block=2)

    def test_one_source_shared_by_every_block_is_checked_against_them_all(self):
        # 8 planned trials over 3 blocks of 2 completed trials: 2 never served.
        with pytest.raises(ValueError, match="the other 2"):
            BlockPlan(self.constant(), n_blocks=3, trials_per_block=2)

    def test_one_source_shared_by_enough_blocks_builds(self):
        plan = BlockPlan(self.constant(), n_blocks=4, trials_per_block=2)

        assert [c.params["block"] for c in drain(plan)] == [1, 1, 2, 2, 3, 3, 4, 4]

    def test_a_bound_at_or_above_the_plan_builds(self):
        plan = BlockPlan([self.constant(), self.constant()], trials_per_block=8)

        assert len(drain(plan)) == 16

    def test_a_source_that_reports_no_plan_is_not_checked(self):
        # An adaptive source has no plan to cut, and a downstream source
        # written before remaining() existed does not report one: both build
        # exactly as they always did.
        stair = UpDownStaircase("contrast", start=0.5, step=0.1, n_trials=4)

        plan = BlockPlan(stair, n_blocks=2, trials_per_block=1)

        assert len(drain(plan)) == 2

    def test_remaining_counts_down_and_retries_count_back_up(self):
        source = self.constant()
        assert source.remaining() == 8

        first = source.next()
        assert source.remaining() == 7
        source.record(first, result(BROKE))
        assert source.remaining() == 8  # the retry is back in the plan
