"""Shrinking a design without changing it into a different one."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import model_validator

from alhazen.config.models import Model
from alhazen.errors import ConfigError
from alhazen.modes.rehearsal import rehearsal_root, shrink_params
from alhazen.paradigms.config import BlockConfig, QuestConfig, SchedulerConfig, StaircaseConfig


class OneScheduler(Model):
    paradigm: SchedulerConfig = SchedulerConfig(n_per_condition=6)


class TwoSchedulers(Model):
    """The shape kde-vergence has: two schedulers, neither called
    'paradigm'."""

    saccade_paradigm: SchedulerConfig = SchedulerConfig(n_per_condition=10)
    pursuit_paradigm: SchedulerConfig = SchedulerConfig(n_per_condition=7)


class Nested(Model):
    inner: OneScheduler = OneScheduler()


class WithBlocks(Model):
    paradigm: SchedulerConfig = SchedulerConfig(n_per_condition=3, blocks=BlockConfig(n_blocks=6))

    @model_validator(mode="after")
    def _blocks_must_be_even(self) -> WithBlocks:
        """Stands in for amodal-averaging's rule that the block count is a
        multiple of its motion levels."""
        if self.paradigm.blocks and self.paradigm.blocks.n_blocks % 2:
            raise ValueError("n_blocks must be even")
        return self


class TestItFindsSchedulersByType:
    def test_a_scheduler_not_called_paradigm_is_still_found(self):
        """The case a name-based search gets wrong, and gets wrong silently:
        the session runs at full length when a rehearsal was asked for."""
        _, reductions = shrink_params(TwoSchedulers())

        assert [str(r) for r in reductions] == [
            "saccade_paradigm.n_per_condition: 10 -> 1",
            "pursuit_paradigm.n_per_condition: 7 -> 1",
        ]

    def test_a_scheduler_inside_a_nested_model_is_found(self):
        params, reductions = shrink_params(Nested())

        assert len(reductions) == 1
        assert params.inner.paradigm.n_per_condition == 1


class TestWhatItLeavesAlone:
    def test_block_structure_survives(self):
        """Blocks are part of what a rehearsal is for — the break is where a
        subject stops concentrating — and a task may constrain the count."""
        params, _ = shrink_params(WithBlocks())

        assert params.paradigm.blocks.n_blocks == 6

    def test_it_never_raises_a_count(self):
        params, reductions = shrink_params(OneScheduler(), n_per_condition=99)

        assert reductions == []
        assert params.paradigm.n_per_condition == 6

    def test_everything_else_in_the_model_is_untouched(self):
        class Rich(Model):
            paradigm: SchedulerConfig = SchedulerConfig(n_per_condition=4)
            eccentricity_dva: float = 8.0
            levels: tuple[str, ...] = ("a", "b")

        params, _ = shrink_params(Rich())

        assert params.eccentricity_dva == 8.0 and params.levels == ("a", "b")


class TestAdaptiveRuns:
    def test_a_staircase_is_shortened_by_its_own_stopping_rule(self):
        """n_per_condition means nothing to a staircase — its length is its
        trial and reversal counts, so those are what come down."""
        params, reductions = shrink_params(
            OneScheduler(
                paradigm=SchedulerConfig(
                    kind="staircase",
                    staircase=StaircaseConfig(
                        parameter="x", start=1.0, step=0.1, n_trials=80, n_reversals=12
                    ),
                )
            ),
            max_adaptive_trials=10,
        )

        assert params.paradigm.staircase.n_trials == 10
        assert params.paradigm.staircase.n_reversals == 10
        assert len(reductions) == 2

    def test_a_quest_run_is_shortened(self):
        params, _ = shrink_params(
            OneScheduler(
                paradigm=SchedulerConfig(
                    kind="questplus",
                    quest=QuestConfig(
                        parameter="x", intensities=[1.0], thresholds=[1.0], n_trials=60
                    ),
                )
            ),
            max_adaptive_trials=10,
        )

        assert params.paradigm.quest.n_trials == 10


class TestItFailsLoudly:
    def test_a_reduction_the_task_forbids_raises_rather_than_running(self):
        """If a task's own validator refuses the shortened design, that has
        to surface here — not halfway into the session it was rehearsing."""

        class BlocksMustMatchRepeats(Model):
            paradigm: SchedulerConfig = SchedulerConfig(n_per_condition=4)

            @model_validator(mode="after")
            def _must_be_four(self):
                if self.paradigm.n_per_condition != 4:
                    raise ValueError("this design needs exactly 4 repetitions")
                return self

        with pytest.raises(ConfigError, match="not a valid"):
            shrink_params(BlocksMustMatchRepeats())

    @pytest.mark.parametrize("bad", [{"n_per_condition": 0}, {"max_adaptive_trials": 0}])
    def test_a_nonsensical_request_is_refused(self, bad):
        with pytest.raises(ValueError, match="must be >= 1"):
            shrink_params(OneScheduler(), **bad)


class TestRehearsalRoot:
    def test_it_is_a_sibling_of_the_real_root(self):
        assert rehearsal_root(Path("/data/alhazen")) == Path("/data/alhazen-rehearsal")

    def test_it_is_never_inside_the_real_root(self):
        """An analysis that globs data_root/sub-* must not reach it."""
        root = Path("/data/alhazen")

        assert root not in rehearsal_root(root).parents
