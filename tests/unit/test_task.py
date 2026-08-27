"""The Task class: declarations checked at definition, defaults that do the
obvious thing, and build_session reading it all."""

from __future__ import annotations

import csv

import numpy as np
import pytest

from alhazen import (
    Condition,
    DisplayConfig,
    Duration,
    Model,
    RigConfig,
    Task,
    TrialPlan,
    TrialSetup,
    build_session,
    outcomes,
)
from alhazen.core.events import EventSchema
from alhazen.errors import ConfigError
from alhazen.paradigms.config import SchedulerConfig, StaircaseConfig
from alhazen.paradigms.constant import ConstantStimuli
from alhazen.paradigms.staircase import InterleavedStaircases
from alhazen.task.reward_policy import RewardPolicy
from support import COMPLETED, MONITOR, RunForFrames

EVENTS = EventSchema(("STIM_ON",))
OUTCOMES = outcomes(DONE=dict(completed=True, success=True))


class Params(Model):
    n: int = 2
    paradigm: SchedulerConfig = SchedulerConfig()


class DemoTask(Task):
    name = "demo-task"
    events = EVENTS
    outcomes = OUTCOMES
    params_model = Params

    def build_trial(self, setup: TrialSetup) -> TrialPlan:
        return TrialPlan(phases=[RunForFrames(1, self.outcomes["DONE"])])


class TestDeclarations:
    def test_a_task_missing_a_declaration_fails_at_definition(self):
        # At class-definition time, not at the first trial with a subject
        # already in the rig.
        with pytest.raises(TypeError, match="params_model"):

            class Incomplete(Task):
                name = "incomplete"
                events = EVENTS
                outcomes = OUTCOMES

    def test_a_task_name_must_be_filename_safe(self):
        with pytest.raises(ValueError, match="lowercase"):

            class Shouty(Task):
                name = "Demo Task"
                events = EVENTS
                outcomes = OUTCOMES
                params_model = Params

    def test_an_abstract_base_declares_nothing_and_is_allowed(self):
        class SharedBase(Task):
            """A family of tasks with common helpers, not a task itself."""

        assert not hasattr(SharedBase, "name")

    def test_params_are_type_checked(self):
        class Other(Model):
            pass

        with pytest.raises(TypeError, match="Params"):
            DemoTask(Other())


class TestDefaults:
    def test_one_nameless_condition_by_default(self):
        task = DemoTask(Params())
        assert [c.params for c in task.conditions(np.random.default_rng(0))] == [{}]

    def test_score_adds_nothing_by_default(self):
        record = {"trial_index": 1}
        assert DemoTask(Params()).score(record) == record

    def test_build_trial_must_be_overridden(self):
        class NoBuild(Task):
            name = "no-build"
            events = EVENTS
            outcomes = OUTCOMES
            params_model = Params

        with pytest.raises(NotImplementedError, match="build_trial"):
            NoBuild(Params()).build_trial(None)  # type: ignore[arg-type]


class TestMakeSource:
    def test_the_paradigm_field_chooses_the_scheduler(self):
        class Grid(DemoTask):
            name = "grid-task"

            def conditions(self, rng):
                return [Condition({"side": s}) for s in ("left", "right")]

        task = Grid(Params(paradigm=SchedulerConfig(kind="constant", n_per_condition=2)))
        source = task.make_source(task.params, np.random.default_rng(0))
        assert isinstance(source, ConstantStimuli)

    def test_an_interleaved_staircase_takes_its_levels_from_the_conditions(self):
        class Levels(DemoTask):
            name = "levels-task"

            def conditions(self, rng):
                return [Condition({"speed": s}) for s in (2.0, 8.0)]

        paradigm = SchedulerConfig(
            kind="staircase",
            staircase=StaircaseConfig(
                parameter="contrast", start=0.5, step=0.1, n_trials=4, interleave_by="speed"
            ),
        )
        source = Levels(Params(paradigm=paradigm)).make_source(
            Params(paradigm=paradigm), np.random.default_rng(0)
        )
        assert isinstance(source, InterleavedStaircases)

    def test_interleaving_by_something_the_task_never_declares_fails_loudly(self):
        paradigm = SchedulerConfig(
            kind="staircase",
            staircase=StaircaseConfig(
                parameter="contrast", start=0.5, step=0.1, n_trials=4, interleave_by="speed"
            ),
        )
        task = DemoTask(Params(paradigm=paradigm))
        with pytest.raises(ConfigError, match="speed"):
            task.make_source(task.params, np.random.default_rng(0))

    def test_a_task_with_no_paradigm_field_still_schedules(self):
        class Bare(Model):
            pass

        class BareTask(DemoTask):
            name = "bare-task"
            params_model = Bare

        task = BareTask(Bare())
        source = task.make_source(Bare(), np.random.default_rng(0))
        assert source.next() is not None


class TestBuildSessionFromATask:
    def rig(self, tmp_path) -> RigConfig:
        return RigConfig(
            monitor=MONITOR, display=DisplayConfig(backend="simulated"), data_root=tmp_path
        )

    def test_a_task_supplies_everything_the_builder_needs(self, tmp_path):
        params = Params(paradigm=SchedulerConfig(kind="sequence", n_per_condition=3))
        runner = build_session(
            rig=self.rig(tmp_path),
            subject="t01",
            session=1,
            run=1,
            task=DemoTask(params),
            seed=1,
            iti=Duration(ms=0),
            simulated_frame_period_s=0.0,
            date_yyyymmdd="20260826",
        )
        runner.run()
        trials = next((tmp_path / "sub-t01").rglob("*_trials.csv"))
        with trials.open() as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 3
        assert all(row["outcome"] == "DONE" for row in rows)

    def test_a_tasks_reward_policy_is_wired(self, tmp_path):
        from alhazen.config.models import DevicesConfig, RewardHwConfig, RewardPulses

        class Paying(DemoTask):
            name = "paying-task"
            reward = RewardPolicy(by_outcome={"DONE": RewardPulses(n_pulses=1, pulse_ms=50)})

        rig = self.rig(tmp_path).model_copy(
            update={"devices": DevicesConfig(reward=RewardHwConfig(backend="simulated"))}
        )
        runner = build_session(
            rig=rig,
            subject="t01",
            session=1,
            run=1,
            task=Paying(Params()),
            seed=1,
            simulated_frame_period_s=0.0,
            date_yyyymmdd="20260826",
        )
        runner.run()
        # One trial (the default paradigm), one delivery, from the task's
        # own table — nothing in the session wiring names an outcome.
        assert len(runner._reward.deliveries) == 1

    def test_explicit_arguments_still_win(self, tmp_path):
        # A test (or a training harness) overriding one piece of a real task
        # must not have to rebuild the rest.
        runner = build_session(
            rig=self.rig(tmp_path),
            subject="t01",
            session=1,
            run=1,
            task=DemoTask(Params()),
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(0, COMPLETED)]),
            seed=1,
            simulated_frame_period_s=0.0,
            date_yyyymmdd="20260826",
        )
        runner.run()
        trials = next((tmp_path / "sub-t01").rglob("*_trials.csv"))
        assert "COMPLETED" in trials.read_text()

    def test_building_with_neither_a_task_nor_the_pieces_says_what_is_missing(self, tmp_path):
        with pytest.raises(ConfigError, match="task_name"):
            build_session(
                rig=self.rig(tmp_path),
                subject="t01",
                session=1,
                run=1,
                seed=1,
                date_yyyymmdd="20260826",
            )
