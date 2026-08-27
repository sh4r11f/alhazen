"""Building a scheduler from a config block.

`make_scheduler` is what turns `paradigm: {kind: ...}` in a YAML file into a
running scheduler, and it is the only path an experimenter's config takes. Two
of its branches — `questplus` and `blocks` — had no test at all, which is
where the QUEST+ port's crash and the end-of-block recycling bug both lived.
"""

from __future__ import annotations

import numpy as np
import pytest

from alhazen.core.engine import TrialResult
from alhazen.core.trial import Outcome
from alhazen.errors import ConfigError
from alhazen.paradigms.adjustment import AdjustmentTrials
from alhazen.paradigms.base import Condition, SimpleSequence
from alhazen.paradigms.blocks import BlockPlan
from alhazen.paradigms.config import (
    BlockConfig,
    QuestConfig,
    SchedulerConfig,
    StaircaseConfig,
    make_scheduler,
)
from alhazen.paradigms.constant import ConstantStimuli
from alhazen.paradigms.questplus import QuestPlus
from alhazen.paradigms.staircase import InterleavedStaircases, UpDownStaircase

HIT = Outcome("HIT", completed=True, success=True)
MISS = Outcome("MISS", completed=True, success=False)
BROKE = Outcome("BROKE", completed=False)


def result(outcome: Outcome) -> TrialResult:
    return TrialResult(outcome=outcome, record={})


def drain(source, answer=lambda condition: HIT, limit=500):
    served = []
    for _ in range(limit):
        condition = source.next()
        if condition is None:
            return served
        served.append(condition)
        source.record(condition, result(answer(condition)))
    raise AssertionError(f"scheduler served more than {limit} trials without ending")


def sides() -> list[Condition]:
    return [Condition({"side": name}) for name in ("left", "right")]


def rng() -> np.random.Generator:
    return np.random.default_rng(0)


class TestEachKindBuilds:
    def test_sequence(self):
        source = make_scheduler(SchedulerConfig(), sides(), rng())
        assert isinstance(source, SimpleSequence)

    def test_constant(self):
        source = make_scheduler(SchedulerConfig(kind="constant"), sides(), rng())
        assert isinstance(source, ConstantStimuli)

    def test_adjustment(self):
        source = make_scheduler(SchedulerConfig(kind="adjustment"), sides(), rng())
        assert isinstance(source, AdjustmentTrials)

    def test_staircase(self):
        cfg = SchedulerConfig(
            kind="staircase",
            staircase=StaircaseConfig(parameter="contrast", start=0.5, step=0.1, n_trials=4),
        )
        assert isinstance(make_scheduler(cfg, sides(), rng()), UpDownStaircase)

    def test_interleaved_staircase(self):
        cfg = SchedulerConfig(
            kind="staircase",
            staircase=StaircaseConfig(
                parameter="contrast",
                start=0.5,
                step=0.1,
                n_trials=4,
                interleave_by="side",
            ),
        )
        assert isinstance(make_scheduler(cfg, sides(), rng()), InterleavedStaircases)


class TestNonFactorialConditionsAreRefused:
    """`kind: constant` recovers condition GRIDS from the task's condition
    list and builds the factorial itself. A task whose conditions are not a
    full factorial — a list of specific pairings — therefore had trials
    invented for it: two declared cells silently became four, half of them
    combinations the experiment never asked to run."""

    def paired(self) -> list[Condition]:
        # Two specific pairings, not the 2x2 grid they would expand to.
        return [
            Condition({"side": "left", "direction": "up"}),
            Condition({"side": "right", "direction": "down"}),
        ]

    def test_a_non_factorial_list_is_refused_by_name(self):
        cfg = SchedulerConfig(kind="constant")

        with pytest.raises(ConfigError) as excinfo:
            make_scheduler(cfg, self.paired(), rng(), task_name="pairs-task")

        message = str(excinfo.value)
        assert "pairs-task" in message
        assert "2" in message and "4" in message  # declared vs invented

    def test_a_full_factorial_still_builds(self):
        grid = [
            Condition({"side": side, "direction": direction})
            for side in ("left", "right")
            for direction in ("up", "down")
        ]

        source = make_scheduler(SchedulerConfig(kind="constant"), grid, rng())

        assert len(drain(source)) == 4

    def test_a_single_key_is_always_factorial(self):
        source = make_scheduler(SchedulerConfig(kind="constant"), sides(), rng())
        assert len(drain(source)) == 2

    def test_other_kinds_honour_the_literal_cells(self):
        # Only `constant` reconstructs a grid; everything else serves the
        # task's own list, so a non-factorial design is fine there.
        source = make_scheduler(SchedulerConfig(kind="sequence"), self.paired(), rng())

        served = drain(source)

        assert {(c.params["side"], c.params["direction"]) for c in served} == {
            ("left", "up"),
            ("right", "down"),
        }


class TestQuestPlusFromAConfig:
    """The `questplus` branch was never exercised from a config, which is
    exactly how a port could ship serving conditions its task could not read."""

    def cfg(self, **overrides) -> SchedulerConfig:
        return SchedulerConfig(
            kind="questplus",
            quest=QuestConfig(
                parameter="contrast",
                intensities=[0.01, 0.05, 0.1, 0.2, 0.4],
                thresholds=[0.02, 0.05, 0.1, 0.2],
                n_trials=8,
                **overrides,
            ),
        )

    def test_it_builds_and_runs_to_its_trial_count(self):
        source = make_scheduler(self.cfg(), sides(), rng())

        assert isinstance(source, QuestPlus)
        served = drain(source)
        assert len(served) == 8

    def test_every_served_condition_carries_the_titrated_parameter(self):
        source = make_scheduler(self.cfg(), sides(), rng())

        for condition in drain(source):
            assert "contrast" in condition.params

    def test_interleaving_takes_its_levels_from_the_tasks_conditions(self):
        source = make_scheduler(self.cfg(interleave_by="side"), sides(), rng())

        served = drain(source)
        assert {condition.params["side"] for condition in served} == {"left", "right"}

    def test_interleaving_by_an_undeclared_key_fails_loudly(self):
        with pytest.raises(ConfigError, match="speed"):
            make_scheduler(self.cfg(interleave_by="speed"), sides(), rng())

    def test_a_kind_without_its_block_is_refused_at_config_time(self):
        with pytest.raises(ValueError, match="needs a 'quest' block"):
            SchedulerConfig(kind="questplus")

    def test_the_summary_reports_the_posterior(self):
        source = make_scheduler(self.cfg(), sides(), rng())
        drain(source)

        summary = source.summary()
        assert summary is not None and not summary.empty


class TestBlocksFromAConfig:
    """A `blocks:` block builds ONE source per block, not one shared across
    all of them.

    The shared form is where the bug was: a failed condition re-queues at the
    end of the whole remaining queue, so its retry lands in the *last* block
    rather than its own — and because the block number is part of the
    condition key, the runner's attempt counter starts over at 1 for it. The
    trial that comes back is recorded as a first attempt in the wrong block.
    """

    def cfg(self, n_blocks=2, **overrides) -> SchedulerConfig:
        return SchedulerConfig(
            kind="constant",
            n_per_condition=1,
            shuffle=False,
            blocks=BlockConfig(n_blocks=n_blocks, **overrides),
            **({} if "trials_per_block" in overrides else {}),
        )

    def test_it_builds_a_block_plan(self):
        assert isinstance(make_scheduler(self.cfg(), sides(), rng()), BlockPlan)

    def test_every_block_serves_the_full_condition_set(self):
        source = make_scheduler(self.cfg(n_blocks=3), sides(), rng())

        served = drain(source)

        assert [condition.params["block"] for condition in served] == [1, 1, 2, 2, 3, 3]

    def test_a_failed_trial_comes_back_inside_its_own_block(self):
        source = make_scheduler(self.cfg(n_blocks=2), sides(), rng())
        failed = {"once": False}

        def flaky(condition):
            if not failed["once"] and condition.params["block"] == 1:
                failed["once"] = True
                return BROKE
            return HIT

        served = drain(source, answer=flaky)

        blocks = [condition.params["block"] for condition in served]
        assert blocks == [1, 1, 1, 2, 2]  # three trials in block 1: two plus the retry

    def test_the_retry_is_the_same_condition_key_so_attempts_increment(self):
        """The runner counts attempts by condition key, and the block number
        is part of that key. A retry served in a different block is a
        different key, so it is recorded as attempt 1 of something else."""
        source = make_scheduler(self.cfg(n_blocks=2), sides(), rng())
        keys: list[str] = []
        failed = {"once": False}

        def flaky(condition):
            keys.append(condition.key())
            if not failed["once"] and condition.params["block"] == 1:
                failed["once"] = True
                return BROKE
            return HIT

        drain(source, answer=flaky)

        assert keys[0] == keys[2], "the retry must carry the same key as the failed attempt"

    def test_a_single_block_is_still_a_block_plan(self):
        source = make_scheduler(self.cfg(n_blocks=1), sides(), rng())
        assert [c.params["block"] for c in drain(source)] == [1, 1]

    def test_the_summary_counts_completed_trials_per_block(self):
        source = make_scheduler(self.cfg(n_blocks=2), sides(), rng())
        drain(source)

        summary = source.summary()
        assert list(summary["block"]) == [1, 2]
        assert list(summary["n_completed"]) == [2, 2]

    def test_trials_per_block_bounds_a_block(self):
        cfg = SchedulerConfig(
            kind="sequence",
            n_per_condition=4,
            shuffle=False,
            blocks=BlockConfig(n_blocks=2, trials_per_block=3),
        )

        served = drain(make_scheduler(cfg, [Condition({"i": 0})], rng()))

        assert [condition.params["block"] for condition in served] == [1, 1, 1, 2, 2, 2]

    def test_an_adaptive_paradigm_keeps_one_estimator_across_blocks(self):
        """A staircase's whole point is that it carries its estimate forward;
        a fresh one per block would throw away everything the subject just
        told it. So the adaptive kinds share one source, which is why
        `trials_per_block` is required for them."""
        cfg = SchedulerConfig(
            kind="staircase",
            staircase=StaircaseConfig(parameter="contrast", start=0.5, step=0.1, n_trials=6),
            blocks=BlockConfig(n_blocks=2, trials_per_block=3),
        )

        source = make_scheduler(cfg, sides(), rng())
        served = drain(source)

        assert [condition.params["block"] for condition in served] == [1, 1, 1, 2, 2, 2]
        # One staircase, so its levels keep moving across the boundary rather
        # than restarting at `start`.
        levels = [condition.params["contrast"] for condition in served]
        assert levels[3] != levels[0]

    def test_an_adaptive_paradigm_without_trials_per_block_is_refused(self):
        cfg = SchedulerConfig(
            kind="staircase",
            staircase=StaircaseConfig(parameter="contrast", start=0.5, step=0.1, n_trials=6),
            blocks=BlockConfig(n_blocks=2),
        )

        with pytest.raises(ValueError, match="trials_per_block"):
            make_scheduler(cfg, sides(), rng())
