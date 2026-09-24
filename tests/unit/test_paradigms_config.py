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
    ADAPTIVE_KINDS,
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


def inverted(result: TrialResult) -> bool:
    """A scorer that calls a trial a success exactly when its outcome says it
    was not — the simplest stand-in for a task titrating something other than
    accuracy, and one whose effect on a staircase cannot be mistaken."""
    return not result.outcome.success


def a_staircase(blocks: BlockConfig | None = None, **overrides) -> SchedulerConfig:
    """2-down-1-up from 0.5 in steps of 0.1, three completed trials, unless
    ``overrides`` says otherwise."""
    fields = {"parameter": "contrast", "start": 0.5, "step": 0.1, "n_trials": 3, **overrides}
    return SchedulerConfig(kind="staircase", staircase=StaircaseConfig(**fields), blocks=blocks)


def a_quest(blocks: BlockConfig | None = None) -> SchedulerConfig:
    return SchedulerConfig(
        kind="questplus",
        quest=QuestConfig(
            parameter="contrast", intensities=[0.2, 0.5], thresholds=[0.2, 0.5], n_trials=3
        ),
        blocks=blocks,
    )


# One config per adaptive kind. Keyed by kind so the test below can check the
# table against ADAPTIVE_KINDS: a new adaptive kind added to make_scheduler
# without a row here fails that test instead of silently skipping this check.
ADAPTIVE_CONFIGS = {"staircase": a_staircase, "questplus": a_quest}


class TestTheTasksScorerReachesEveryAdaptiveKind:
    """`Task.score_trial` is how a task titrating something other than
    accuracy says what a success is, and `make_scheduler` receives it as
    `score`. It handed that scorer to QUEST+ only: the up-down staircases read
    `outcome.success` whatever the task said, so a task that overrode the hook
    and chose `kind: staircase` titrated accuracy anyway — with nothing in
    the session to say so."""

    def test_the_default_still_titrates_the_outcomes_own_success(self):
        # Every trial a HIT: the first two step down once (2-down), and the
        # third is served at the new, harder level.
        served = drain(make_scheduler(a_staircase(), sides(), rng()))

        assert [c.params["contrast"] for c in served] == pytest.approx([0.5, 0.5, 0.4])

    def test_a_scorer_that_inverts_success_moves_a_staircase_the_other_way(self):
        # The same HITs, scored as failures: each one steps up (1-up).
        source = make_scheduler(a_staircase(), sides(), rng(), score=inverted)

        served = drain(source)

        assert [c.params["contrast"] for c in served] == pytest.approx([0.5, 0.6, 0.7])

    def test_every_interleaved_staircase_hears_the_scorer(self):
        source = make_scheduler(a_staircase(interleave_by="side"), sides(), rng(), score=inverted)

        served = drain(source)

        for side in ("left", "right"):
            levels = [c.params["contrast"] for c in served if c.params["side"] == side]
            assert levels == pytest.approx([0.5, 0.6, 0.7]), side

    def test_a_staircase_in_blocks_hears_the_scorer(self):
        # BlockPlan wraps the one shared staircase; the scorer must survive
        # the wrapping, or a blocked design titrates something else again.
        source = make_scheduler(
            a_staircase(blocks=BlockConfig(n_blocks=3, trials_per_block=1)),
            sides(),
            rng(),
            score=inverted,
        )

        served = drain(source)

        assert [c.params["contrast"] for c in served] == pytest.approx([0.5, 0.6, 0.7])

    def test_an_attempt_with_no_measurement_never_reaches_the_scorer(self):
        """A broken trial produced no measurement, so there is nothing to
        score: the task's scorer would be judging a trial that never
        happened, and the staircase would step on it. The attempt is
        re-served at the same level instead."""
        scored: list[str] = []

        def spy(result: TrialResult) -> bool:
            scored.append(result.outcome.name)
            return bool(result.outcome.success)

        answers = iter([BROKE, HIT, HIT, HIT])
        source = make_scheduler(a_staircase(), sides(), rng(), score=spy)

        served = drain(source, answer=lambda condition: next(answers))

        assert served[1].params == served[0].params  # the retry, unchanged
        assert scored == ["HIT", "HIT", "HIT"]

    def test_every_adaptive_kind_has_a_case_here(self):
        assert set(ADAPTIVE_CONFIGS) == ADAPTIVE_KINDS

    @pytest.mark.parametrize("kind", sorted(ADAPTIVE_CONFIGS))
    @pytest.mark.parametrize(
        "blocks", [None, BlockConfig(n_blocks=3, trials_per_block=1)], ids=["plain", "blocks"]
    )
    def test_every_adaptive_kind_asks_the_scorer_about_every_completed_trial(self, kind, blocks):
        scored: list[TrialResult] = []

        def spy(result: TrialResult) -> bool:
            scored.append(result)
            return True

        source = make_scheduler(ADAPTIVE_CONFIGS[kind](blocks=blocks), sides(), rng(), score=spy)

        served = drain(source)

        assert len(served) == 3
        assert len(scored) == 3

    @pytest.mark.parametrize(
        "score",
        # No scorer at all, and one that says what `Task.score_trial` says by
        # default: the two ways a task that never overrode the hook arrives.
        [None, lambda result: bool(result.outcome.success)],
        ids=["no-scorer", "the-default-scorer"],
    )
    def test_a_task_that_keeps_the_default_gets_the_session_it_always_got(self, score):
        """Seed discipline: threading the scorer through must not change the
        session of a task that never overrode `score_trial`. Recorded on the
        code before the staircases took a scorer: seed 0, two interleaved
        staircases, answered HIT, HIT, MISS in turn until three reversals
        each. Both the order the rng picked and every level must match."""
        answers = iter([HIT, HIT, MISS] * 4)
        source = make_scheduler(
            a_staircase(interleave_by="side", n_trials=12, n_reversals=3),
            sides(),
            rng(),
            score=score,
        )

        served = drain(source, answer=lambda condition: next(answers))

        assert [c.params["side"] for c in served] == (["right"] * 3 + ["left"] * 6 + ["right"] * 3)
        assert [c.params["contrast"] for c in served] == pytest.approx([0.5, 0.5, 0.4] * 4)
        summary = source.summary()
        assert list(summary["n_reversals"]) == [3, 3]
        assert list(summary["reversal_mean"]) == pytest.approx([0.45, 0.45])


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
        # n_per_condition was 4 here, so each block's plan of 4 was cut to 3 —
        # the dropping TestTrialsPerBlockNeverCutsAPlan now refuses. A plan
        # of 3 is the config that serves these same six trials honestly.
        cfg = SchedulerConfig(
            kind="sequence",
            n_per_condition=3,
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


class TestTrialsPerBlockNeverCutsAPlan:
    """A queue-based kind gives every block its own full plan (cells x
    `n_per_condition`), and `trials_per_block` then ended the block by COUNT.
    Set below the plan, it abandoned whatever was still queued — and a retry
    re-queues at the tail, so retries were the first trials cut. The cells
    could end uneven, which ConstantStimuli exists to prevent, and nothing
    said so.

    Completed trials can never outnumber a queue-based plan (each planned
    trial leaves the queue only by completing), so a bound at or above the
    plan ends a block only once its plan is done, and one below it can only
    ever cut. The cutting config is refused when the scheduler is built."""

    def grid(self) -> list[Condition]:
        return [
            Condition({"side": side, "contrast": contrast})
            for side in ("left", "right")
            for contrast in (0.2, 0.8)
        ]

    def cfg(self, kind="constant", n_per_condition=2, **blocks) -> SchedulerConfig:
        return SchedulerConfig(
            kind=kind,
            n_per_condition=n_per_condition,
            blocks=BlockConfig(n_blocks=2, **blocks),
        )

    def test_a_bound_below_the_plan_is_refused_with_the_numbers(self):
        # 4 cells x n_per_condition=2 is 8 trials a block; a bound of 5 would
        # never serve 3 of them in either block.
        cfg = self.cfg(trials_per_block=5)

        with pytest.raises(ConfigError) as excinfo:
            make_scheduler(cfg, self.grid(), rng(), task_name="contrast-task")

        message = str(excinfo.value)
        assert "contrast-task" in message
        assert "trials_per_block=5" in message
        assert "n_per_condition=2" in message
        assert "8 trials" in message  # the plan it would have cut
        assert "the other 3 planned trials" in message  # dropped from each block

    @pytest.mark.parametrize("kind", ["sequence", "constant", "adjustment"])
    def test_every_queue_based_kind_is_refused(self, kind):
        with pytest.raises(ConfigError, match="trials_per_block"):
            make_scheduler(self.cfg(kind=kind, trials_per_block=7), self.grid(), rng())

    def test_retries_past_the_bound_still_leave_every_cell_its_count(self):
        """A bound equal to the plan, and every condition failing its first
        two attempts in every block: each block serves 8 planned trials plus 8
        retries, twice the bound — and every cell still reaches its count,
        inside its own block, because the bound counts COMPLETED trials."""
        source = make_scheduler(self.cfg(trials_per_block=8), self.grid(), rng())
        failures: dict[tuple, int] = {}

        def fail_twice(condition):
            key = condition.key()
            failures[key] = failures.get(key, 0) + 1
            return BROKE if failures[key] <= 2 else HIT

        answered: list[tuple[Condition, Outcome]] = []

        def answer(condition):
            outcome = fail_twice(condition)
            answered.append((condition, outcome))
            return outcome

        drain(source, answer=answer)

        completed: dict[tuple, int] = {}
        for condition, outcome in answered:
            if outcome.completed:
                completed[condition.key()] = completed.get(condition.key(), 0) + 1
        # 4 cells x 2 blocks, each (block, cell) pair completed exactly twice.
        assert len(completed) == 8
        assert set(completed.values()) == {2}
        assert list(source.summary()["n_completed"]) == [8, 8]
        assert len(answered) == 32  # 16 completed + 16 retries, nothing dropped

    def test_a_bound_above_the_plan_is_accepted(self):
        """Test mode lowers `n_per_condition` and leaves block structure
        alone, so a config whose bound matched the full plan meets a smaller
        one in a rehearsal. That must still build: it cuts nothing."""
        source = make_scheduler(self.cfg(n_per_condition=1, trials_per_block=8), self.grid(), rng())

        served = drain(source)

        assert [condition.params["block"] for condition in served] == [1] * 4 + [2] * 4

    def test_an_adaptive_kind_is_not_measured_against_n_per_condition(self):
        """An adaptive kind has no plan of cells x `n_per_condition` — it
        shares one estimator across blocks, and `trials_per_block` is its
        block length. A bound below what that product would be still builds."""
        cfg = SchedulerConfig(
            kind="staircase",
            n_per_condition=3,  # ignored by a staircase; 4 cells x 3 would be 12
            staircase=StaircaseConfig(parameter="contrast", start=0.5, step=0.1, n_trials=4),
            blocks=BlockConfig(n_blocks=2, trials_per_block=2),
        )

        served = drain(make_scheduler(cfg, self.grid(), rng()))

        assert [condition.params["block"] for condition in served] == [1, 1, 2, 2]


class TestAdaptiveBlocksLastAsLongAsTheEstimator:
    """An adaptive kind shares one estimator across its blocks, and the blocks
    end on ``n_blocks x trials_per_block`` completed trials. When that is fewer
    than the estimator's ``n_trials`` (per interleaved staircase or level),
    the last block ended the session with the staircase or QUEST+ unfinished —
    fewer trials than the config asked for, and nothing in the log to say so.
    It is refused when the scheduler is built, naming the numbers.

    A staircase stopped by reversals alone has no trial count to compare, so
    it is not checked: how many trials its reversals take is unknowable in
    advance."""

    def test_a_staircase_cut_short_by_its_blocks_is_refused_with_the_numbers(self):
        cfg = a_staircase(blocks=BlockConfig(n_blocks=2, trials_per_block=2), n_trials=6)

        with pytest.raises(ConfigError) as excinfo:
            make_scheduler(cfg, sides(), rng(), task_name="contrast-task")

        message = str(excinfo.value)
        assert "contrast-task" in message
        assert "n_blocks=2" in message
        assert "trials_per_block=2" in message
        assert "4 completed trials" in message
        assert "6" in message  # what the staircase needs

    def test_interleaved_staircases_need_their_trials_each(self):
        # Two staircases of 3 trials each need 6; three blocks of 1 give 3.
        cfg = a_staircase(
            blocks=BlockConfig(n_blocks=3, trials_per_block=1), interleave_by="side", n_trials=3
        )

        with pytest.raises(ConfigError, match="2 interleaved"):
            make_scheduler(cfg, sides(), rng())

    def test_a_staircase_with_reversals_too_is_still_checked(self):
        # n_trials is its ceiling: blocks ending below it can end the session
        # before either stopping rule fires.
        cfg = a_staircase(
            blocks=BlockConfig(n_blocks=2, trials_per_block=2), n_trials=6, n_reversals=4
        )

        with pytest.raises(ConfigError, match="trials_per_block"):
            make_scheduler(cfg, sides(), rng())

    def test_a_staircase_stopped_by_reversals_alone_builds(self):
        cfg = a_staircase(
            blocks=BlockConfig(n_blocks=2, trials_per_block=2), n_trials=None, n_reversals=4
        )

        assert isinstance(make_scheduler(cfg, sides(), rng()), BlockPlan)

    def test_quest_cut_short_by_its_blocks_is_refused(self):
        with pytest.raises(ConfigError, match="n_trials=3"):
            make_scheduler(
                a_quest(blocks=BlockConfig(n_blocks=2, trials_per_block=1)), sides(), rng()
            )

    def test_interleaved_quest_needs_its_trials_per_level(self):
        cfg = SchedulerConfig(
            kind="questplus",
            quest=QuestConfig(
                parameter="contrast",
                intensities=[0.2, 0.5],
                thresholds=[0.2, 0.5],
                n_trials=3,
                interleave_by="side",
            ),
            blocks=BlockConfig(n_blocks=1, trials_per_block=5),
        )

        with pytest.raises(ConfigError, match="6"):
            make_scheduler(cfg, sides(), rng())

    @pytest.mark.parametrize("kind", sorted(ADAPTIVE_CONFIGS))
    def test_blocks_that_cover_the_estimator_build_and_run_it_to_the_end(self, kind):
        # 3 trials, in 2 blocks of 2: the second block ends when the
        # estimator does, one trial in.
        source = make_scheduler(
            ADAPTIVE_CONFIGS[kind](blocks=BlockConfig(n_blocks=2, trials_per_block=2)),
            sides(),
            rng(),
        )

        assert [c.params["block"] for c in drain(source)] == [1, 1, 2]
