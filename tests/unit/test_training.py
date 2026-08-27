"""Curricula: overrides, ramps, criteria, persisted state, and the runner
integration that stamps a stage onto every row."""

from __future__ import annotations

import csv

import pytest
import yaml

from alhazen.config.models import Model, RewardPulses
from alhazen.core.commands import Command
from alhazen.core.events import EventSchema
from alhazen.core.trial import Outcome
from alhazen.core.trial import outcomes as outcomes_of
from alhazen.errors import ConfigError
from alhazen.task.plan import TrialPlan
from alhazen.task.reward_policy import RewardPolicy
from alhazen.training import (
    Curriculum,
    Ramp,
    Stage,
    StageCriteria,
    TrainingState,
    TrainingSupervisor,
    register_metric,
)
from alhazen.training.criteria import completed_rate, decide, mean_rt_ms, success_rate
from alhazen.training.stages import apply_stage, ramped_values
from support import COMPLETED, RunForFrames, SessionHarness, make_session_config

COMPLETED_TRIAL = {"completed": True, "success": True, "rt_ms": 300.0}
FAILED_TRIAL = {"completed": True, "success": False, "rt_ms": 500.0}
BROKEN_TRIAL = {"completed": False, "success": None, "rt_ms": None}


class Timing(Model):
    hold_ms: float = 500.0


class Params(Model):
    timing: Timing = Timing()
    window_dva: float = 5.0
    n_trials: int = 3


class TestOverrides:
    def test_a_dotted_path_reaches_a_nested_field(self):
        stage = Stage(name="easy", overrides={"timing.hold_ms": 100.0})
        assert apply_stage(Params(), stage).timing.hold_ms == 100.0

    def test_a_path_the_task_does_not_have_names_the_stage(self):
        # "validation failed" is useless when a curriculum has six stages and
        # forty overrides between them.
        stage = Stage(name="stage-2", overrides={"timing.hold_seconds": 1.0})
        with pytest.raises(ConfigError, match="stage-2.*hold_seconds"):
            apply_stage(Params(), stage)

    def test_a_value_the_model_rejects_names_the_stage_too(self):
        class Strict(Model):
            n: int = 1

        stage = Stage(name="bad", overrides={"n": "not a number"})
        with pytest.raises(ConfigError, match="bad"):
            apply_stage(Strict(), stage)

    def test_overrides_apply_to_the_original_not_to_the_previous_stage(self):
        # Otherwise a demoted subject would keep the harder stage's settings.
        easy = Stage(name="easy", overrides={"window_dva": 8.0})
        hard = Stage(name="hard", overrides={"timing.hold_ms": 900.0})
        base = Params()
        after_easy = apply_stage(base, easy)
        after_hard = apply_stage(base, hard)
        assert after_easy.window_dva == 8.0
        assert after_hard.window_dva == 5.0  # back to the task's own value


class TestRamps:
    def test_a_ramp_moves_with_completed_trials_and_stops_at_the_end(self):
        ramp = Ramp(param="window_dva", start=5.0, end=2.0, over_completed_trials=100)
        assert ramp.value_at(0) == 5.0
        assert ramp.value_at(50) == pytest.approx(3.5)
        assert ramp.value_at(100) == 2.0
        # Never past the end: a ramp cannot take the task somewhere the stage
        # never declared.
        assert ramp.value_at(400) == 2.0

    def test_ramped_values_are_reported_for_the_record(self):
        stage = Stage(
            name="shrink",
            ramps=[Ramp(param="window_dva", start=5.0, end=1.0, over_completed_trials=10)],
        )
        assert ramped_values(stage, 5) == {"window_dva": pytest.approx(3.0)}

    def test_a_parameter_cannot_be_both_set_and_ramped(self):
        with pytest.raises(ValueError, match="set or ramped"):
            Stage(
                name="both",
                overrides={"window_dva": 3.0},
                ramps=[Ramp(param="window_dva", start=5.0, end=1.0, over_completed_trials=10)],
            )


class TestMetrics:
    def test_completed_rate_counts_every_attempt(self):
        window = [COMPLETED_TRIAL, BROKEN_TRIAL, COMPLETED_TRIAL, BROKEN_TRIAL]
        assert completed_rate(window) == 0.5

    def test_success_rate_ignores_attempts_that_measured_nothing(self):
        # A broken fixation is not a wrong answer; counting it as one makes
        # an unengaged subject look like a poor discriminator.
        window = [COMPLETED_TRIAL, FAILED_TRIAL, BROKEN_TRIAL, BROKEN_TRIAL]
        assert success_rate(window) == 0.5

    def test_mean_rt_is_nan_when_nothing_recorded_one(self):
        # So a criterion on a task with no RT never reads as "fast enough".
        assert mean_rt_ms([BROKEN_TRIAL]) != mean_rt_ms([BROKEN_TRIAL])

    def test_an_experiment_can_register_its_own(self):
        register_metric("first_outcome_is_correct", lambda window: float(bool(window)))
        criteria = StageCriteria(
            window=4, min_trials=2, promote_when={"first_outcome_is_correct": 1.0}
        )
        assert decide(criteria, [COMPLETED_TRIAL] * 3) == "promote"

    def test_registering_a_name_twice_is_refused(self):
        # Two metrics under one name would make an existing curriculum mean
        # something different depending on import order.
        with pytest.raises(ConfigError, match="already registered"):
            register_metric("success_rate", lambda window: 0.0)


class TestCriteria:
    def criteria(self, **kwargs):
        defaults = dict(window=10, min_trials=4, promote_when={"success_rate": 0.75})
        return StageCriteria(**{**defaults, **kwargs})

    def test_nothing_is_decided_before_the_window_is_full_enough(self):
        assert decide(self.criteria(), [COMPLETED_TRIAL] * 3) is None
        assert decide(self.criteria(), [COMPLETED_TRIAL] * 4) == "promote"

    def test_every_promote_criterion_must_hold(self):
        criteria = self.criteria(promote_when={"success_rate": 0.75, "completed_rate": 0.9})
        window = [COMPLETED_TRIAL] * 4 + [BROKEN_TRIAL] * 4
        # Success rate is perfect among measured trials, but the subject is
        # only completing half of them.
        assert decide(criteria, window) is None

    def test_demotion_is_checked_first(self):
        criteria = self.criteria(
            promote_when={"success_rate": 0.5}, demote_when={"completed_rate": 0.3}
        )
        window = [COMPLETED_TRIAL] + [BROKEN_TRIAL] * 5
        assert decide(criteria, window) == "demote"

    def test_only_the_most_recent_window_counts(self):
        criteria = self.criteria(window=4, min_trials=4)
        # Ancient failures, recent successes.
        window = [FAILED_TRIAL] * 20 + [COMPLETED_TRIAL] * 4
        assert decide(criteria, window) == "promote"


class TestState:
    def test_round_trip(self, tmp_path):
        state = TrainingState(stage="one")
        state.note_attempt(dict(COMPLETED_TRIAL), window_size=10)
        state.note_transition("one", "two", "criteria", "ses-001_run-01")
        state.save(tmp_path, "m01")

        loaded = TrainingState.load(tmp_path, "m01", default_stage="one")
        assert loaded.stage == "two"
        assert loaded.completed_in("one") == 1
        assert loaded.history[0]["reason"] == "criteria"

    def test_a_first_session_starts_at_the_first_stage(self, tmp_path):
        assert TrainingState.load(tmp_path, "m01", default_stage="one").stage == "one"

    def test_a_corrupt_file_is_loud_and_starts_over(self, tmp_path, caplog):
        # Silently restarting an animal at stage 0 after a disk problem would
        # waste weeks and read as a behavioural regression.
        path = TrainingState.path_for(tmp_path, "m01")
        path.parent.mkdir(parents=True)
        path.write_text("stage: [this is not a stage")
        with caplog.at_level("ERROR"):
            state = TrainingState.load(tmp_path, "m01", default_stage="one")
        assert state.stage == "one"
        assert "unreadable" in caplog.text
        assert path.exists()  # the old file is left for a human to look at

    def test_a_transition_clears_the_window(self):
        # The new stage's criteria must be judged on trials run AT that
        # stage, not on the ones that earned the move.
        state = TrainingState(stage="one")
        state.note_attempt(dict(COMPLETED_TRIAL), window_size=10)
        state.note_transition("one", "two", "criteria", "s")
        assert state.window == []

    def test_the_window_is_bounded(self):
        state = TrainingState(stage="one")
        for _ in range(500):
            state.note_attempt(dict(COMPLETED_TRIAL), window_size=10)
        assert len(state.window) <= 40


class DemoTask:
    """A stand-in for a Task: the supervisor only needs params and reward."""

    def __init__(self) -> None:
        self.params = Params()
        self.reward = RewardPolicy(by_outcome={"COMPLETED": RewardPulses(n_pulses=2)})


class TestSupervisor:
    def curriculum(self) -> Curriculum:
        return Curriculum(
            stages=[
                Stage(
                    name="easy",
                    overrides={"timing.hold_ms": 100.0},
                    reward_scale=2.0,
                    ramps=[Ramp(param="window_dva", start=8.0, end=4.0, over_completed_trials=4)],
                    criteria=StageCriteria(
                        window=4, min_trials=2, promote_when={"success_rate": 0.75}
                    ),
                ),
                Stage(name="real", criteria=StageCriteria(window=4, min_trials=2)),
            ]
        )

    def supervisor(self, tmp_path, task=None):
        task = task or DemoTask()
        return TrainingSupervisor(
            curriculum=self.curriculum(),
            state=TrainingState(stage="easy"),
            task=task,
            data_root=tmp_path,
            subject="m01",
            session_id="ses-001_run-01",
        ), task

    def test_the_stage_shapes_the_task_immediately(self, tmp_path):
        supervisor, task = self.supervisor(tmp_path)
        assert task.params.timing.hold_ms == 100.0
        assert task.params.window_dva == 8.0  # the ramp's start
        assert task.reward.scale == 2.0

    def test_ramps_advance_with_completed_trials(self, tmp_path):
        supervisor, task = self.supervisor(tmp_path)
        for _ in range(2):
            supervisor.observe(COMPLETED, {"rt_ms": 200.0})
        assert task.params.window_dva == pytest.approx(6.0)

    def test_promotion_rebuilds_the_task_at_the_new_stage(self, tmp_path):
        supervisor, task = self.supervisor(tmp_path)
        for _ in range(2):
            supervisor.observe(COMPLETED, {})
        change = supervisor.transition()
        assert (change.from_stage, change.to_stage, change.reason) == (
            "easy",
            "real",
            "criteria",
        )
        # Back to the task's own parameters: the easy stage's overrides are
        # gone, not layered under the new stage.
        assert task.params.timing.hold_ms == 500.0
        assert task.reward.scale == 1.0

    def test_a_paused_trial_is_not_evidence(self, tmp_path):
        supervisor, _ = self.supervisor(tmp_path)
        paused = Outcome("PAUSED", completed=False)
        for _ in range(5):
            supervisor.observe(paused, {})
        assert supervisor.state.window == []

    def test_holding_suspends_automatic_transitions(self, tmp_path):
        supervisor, _ = self.supervisor(tmp_path)
        supervisor.toggle_hold()
        for _ in range(4):
            supervisor.observe(COMPLETED, {})
        assert supervisor.transition() is None
        supervisor.toggle_hold()
        assert supervisor.transition() is not None

    def test_a_manual_request_overrules_the_criteria(self, tmp_path):
        supervisor, _ = self.supervisor(tmp_path)
        supervisor.request(+1)
        change = supervisor.transition()
        assert (change.to_stage, change.reason) == ("real", "manual")

    def test_demotion_below_the_first_stage_does_nothing(self, tmp_path):
        supervisor, _ = self.supervisor(tmp_path)
        supervisor.request(-1)
        assert supervisor.transition() is None
        assert supervisor.stage.name == "easy"

    def test_promotion_past_the_last_stage_completes_the_curriculum(self, tmp_path):
        supervisor, _ = self.supervisor(tmp_path)
        supervisor.request(+1)
        supervisor.transition()
        supervisor.request(+1)
        assert supervisor.transition() is None
        assert supervisor.complete is True

    def test_the_stamp_carries_stage_and_ramp_values(self, tmp_path):
        supervisor, _ = self.supervisor(tmp_path)
        supervisor.observe(COMPLETED, {})
        stamp = supervisor.stamp()
        assert stamp["stage"] == "easy"
        assert stamp["stage_completed_trials"] == 1
        assert stamp["ramp_window_dva"] == pytest.approx(7.0)


class TestRunnerIntegration:
    def curriculum(self) -> Curriculum:
        return Curriculum(
            stages=[
                Stage(
                    name="easy",
                    criteria=StageCriteria(
                        window=2, min_trials=2, promote_when={"success_rate": 0.9}
                    ),
                ),
                Stage(name="real", criteria=StageCriteria(window=100, min_trials=100)),
            ]
        )

    def harness(self, tmp_path, **kwargs):
        task = DemoTask()
        supervisor = TrainingSupervisor(
            curriculum=self.curriculum(),
            state=TrainingState(stage="easy"),
            task=task,
            data_root=tmp_path,
            subject="t01",
            session_id="ses-001_run-01",
        )
        harness = SessionHarness(
            tmp_path,
            n_trials=4,
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, COMPLETED)]),
            declared_events=("FIX_ON",),
            **kwargs,
        )
        harness.runner._training = supervisor
        return harness, supervisor

    def read_trials(self, harness):
        with harness.paths.trials_path.open() as f:
            return list(csv.DictReader(f))

    def test_every_row_carries_its_stage(self, tmp_path):
        harness, _ = self.harness(tmp_path)
        harness.runner.run()
        rows = self.read_trials(harness)
        # Promotion happens after two completed trials, so the session shows
        # both stages — and every row says which one it was run at.
        assert [row["stage"] for row in rows] == ["easy", "easy", "real", "real"]

    def test_a_transition_emits_its_own_event(self, tmp_path):
        harness, _ = self.harness(tmp_path)
        harness.runner.run()
        changes = [e for e in harness.collector.events if e.name == "STAGE_CHANGED"]
        assert len(changes) == 1
        assert changes[0].payload == {"from": "easy", "to": "real", "reason": "criteria"}

    def test_the_state_is_saved_at_teardown(self, tmp_path):
        harness, supervisor = self.harness(tmp_path)
        harness.runner.run()
        saved = yaml.safe_load(TrainingState.path_for(tmp_path, "t01").read_text())
        assert saved["stage"] == "real"
        assert saved["history"][0]["to"] == "real"

    def test_the_stage_keys_reach_the_supervisor(self, tmp_path):
        harness, supervisor = self.harness(tmp_path)
        harness.runner.on_session_command(Command.HOLD_STAGE)
        assert supervisor.holding is True
        harness.runner.on_session_command(Command.PROMOTE_STAGE)
        # Queued, not applied: a stage change mid-trial would record a row at
        # a difficulty that was only true for part of it.
        assert supervisor.stage.name == "easy"
        assert supervisor.transition() is not None


class TestRewardFollowsTheStage:
    """Every stage rebinds ``task.reward`` to a rescaled copy. The runner
    captured the policy once at build time, so after a transition it kept
    paying the old stage's scale while every row stamped the new one — the
    data claimed a scale the pump did not use.

    These tests assert on the pulses that reached the DEVICE. The existing
    supervisor tests assert on ``task.reward``, which is exactly the object
    that was always right."""

    def curriculum(self) -> Curriculum:
        return Curriculum(
            stages=[
                Stage(
                    name="easy",
                    reward_scale=3.0,
                    criteria=StageCriteria(
                        window=2, min_trials=2, promote_when={"success_rate": 0.9}
                    ),
                ),
                Stage(
                    name="real",
                    reward_scale=1.0,
                    criteria=StageCriteria(window=100, min_trials=100),
                ),
            ]
        )

    def test_delivered_pulses_follow_the_transition(self, tmp_path):
        from alhazen.devices.reward import SimulatedReward

        task = DemoTask()  # COMPLETED pays 2 pulses at scale 1
        supervisor = TrainingSupervisor(
            curriculum=self.curriculum(),
            state=TrainingState(stage="easy"),
            task=task,
            data_root=tmp_path,
            subject="t01",
            session_id="ses-001_run-01",
        )
        device = SimulatedReward()
        harness = SessionHarness(
            tmp_path,
            n_trials=4,
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, COMPLETED)]),
            reward=device,
            reward_policy=task.reward,
        )
        harness.runner._training = supervisor

        harness.runner.run()

        # Promotion lands after the second completed trial, so trials 1-2 run
        # at scale 3 (6 pulses) and trials 3-4 at scale 1 (2 pulses).
        assert [pulses.n_pulses for pulses in device.deliveries] == [6, 6, 2, 2]

    def test_each_row_stamps_the_scale_it_was_actually_paid_at(self, tmp_path):
        from alhazen.devices.reward import SimulatedReward

        task = DemoTask()
        supervisor = TrainingSupervisor(
            curriculum=self.curriculum(),
            state=TrainingState(stage="easy"),
            task=task,
            data_root=tmp_path,
            subject="t01",
            session_id="ses-001_run-01",
        )
        device = SimulatedReward()
        harness = SessionHarness(
            tmp_path,
            n_trials=4,
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, COMPLETED)]),
            reward=device,
            reward_policy=task.reward,
        )
        harness.runner._training = supervisor

        harness.runner.run()

        with harness.paths.trials_path.open() as f:
            rows = list(csv.DictReader(f))
        base = task_base_pulses = 2
        for row, delivered in zip(rows, device.deliveries, strict=True):
            assert delivered.n_pulses == round(base * float(row["reward_scale"]))
        assert task_base_pulses == 2  # the reward table itself never changed


class TestSnapshotRecordsTheStageParams:
    def test_the_snapshot_carries_the_stage_effective_params(self, tmp_path):
        """The snapshot is the record of what the session ran. It was built
        before the curriculum block reassigned the task's params, so it
        documented the task's *file* values for a session that never used
        them."""
        from alhazen.session.builder import build_session
        from alhazen.task.task import Task

        class ExampleParams(Model):
            hold_ms: float = 500.0

        class Example(Task):
            name = "example"
            events = EventSchema(("FIX_ON",))
            outcomes = outcomes_of(COMPLETED=dict(completed=True, success=True))
            params_model = ExampleParams

            def build_trial(self, setup):
                return TrialPlan(phases=[RunForFrames(0, COMPLETED)])

        runner = build_session(
            rig=make_session_config(tmp_path).rig,
            subject="t01",
            session=1,
            run=1,
            task=Example(ExampleParams()),
            curriculum=Curriculum(stages=[Stage(name="easy", overrides={"hold_ms": 120.0})]),
            date_yyyymmdd="20260826",
        )
        runner.run()

        snapshot = yaml.safe_load(runner._paths.snapshot_path.read_text())
        assert snapshot["config"]["task_params"]["hold_ms"] == 120.0


class TestCriteriaSeeTheScoredRecord:
    def test_a_score_hook_reaches_mean_rt_ms(self, tmp_path):
        """``score`` is where a task computes its derived measures. Handing
        the criteria the pre-score record meant a curriculum could never gate
        on one."""
        task = DemoTask()
        supervisor = TrainingSupervisor(
            curriculum=Curriculum(
                stages=[
                    Stage(
                        name="easy",
                        criteria=StageCriteria(
                            window=2, min_trials=2, demote_when={"mean_rt_ms": 1e9}
                        ),
                    )
                ]
            ),
            state=TrainingState(stage="easy"),
            task=task,
            data_root=tmp_path,
            subject="t01",
            session_id="ses-001_run-01",
        )
        harness = SessionHarness(
            tmp_path,
            n_trials=2,
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, COMPLETED)]),
            score=lambda record: {**record, "rt_ms": 321.0},
        )
        harness.runner._training = supervisor

        harness.runner.run()

        assert mean_rt_ms(supervisor.state.window) == pytest.approx(321.0)


class TestMetricNamesAreCheckedAtBuild:
    def test_an_unknown_promote_metric_fails_at_construction(self, tmp_path):
        """A typo'd metric name used to surface mid-session, the first time a
        window filled — after a subject had already worked for an hour."""
        with pytest.raises(ConfigError, match="sucess_rate"):
            TrainingSupervisor(
                curriculum=Curriculum(
                    stages=[
                        Stage(
                            name="easy", criteria=StageCriteria(promote_when={"sucess_rate": 0.8})
                        )
                    ]
                ),
                state=TrainingState(stage="easy"),
                task=DemoTask(),
                data_root=tmp_path,
                subject="t01",
                session_id="ses-001_run-01",
            )

    def test_an_unknown_demote_metric_fails_too(self, tmp_path):
        with pytest.raises(ConfigError, match="engagement"):
            TrainingSupervisor(
                curriculum=Curriculum(
                    stages=[
                        Stage(name="easy", criteria=StageCriteria(demote_when={"engagement": 0.1}))
                    ]
                ),
                state=TrainingState(stage="easy"),
                task=DemoTask(),
                data_root=tmp_path,
                subject="t01",
                session_id="ses-001_run-01",
            )

    def test_a_registered_metric_is_accepted(self, tmp_path):
        register_metric("trial_count", lambda window: float(len(window)))
        TrainingSupervisor(
            curriculum=Curriculum(
                stages=[Stage(name="easy", criteria=StageCriteria(promote_when={"trial_count": 5}))]
            ),
            state=TrainingState(stage="easy"),
            task=DemoTask(),
            data_root=tmp_path,
            subject="t01",
            session_id="ses-001_run-01",
        )


class TestStopWhenComplete:
    def supervisor(self, tmp_path, stop: bool):
        return TrainingSupervisor(
            curriculum=Curriculum(
                stages=[
                    Stage(
                        name="only",
                        criteria=StageCriteria(
                            window=2, min_trials=2, promote_when={"success_rate": 0.9}
                        ),
                    )
                ],
                stop_when_complete=stop,
            ),
            state=TrainingState(stage="only"),
            task=DemoTask(),
            data_root=tmp_path,
            subject="t01",
            session_id="ses-001_run-01",
        )

    def test_the_property_reports_the_curriculum_setting(self, tmp_path):
        assert self.supervisor(tmp_path, stop=True).stop_when_complete is True
        assert self.supervisor(tmp_path, stop=False).stop_when_complete is False

    def test_a_finished_curriculum_ends_the_session(self, tmp_path):
        harness = SessionHarness(
            tmp_path,
            n_trials=6,
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, COMPLETED)]),
        )
        harness.runner._training = self.supervisor(tmp_path, stop=True)

        harness.runner.run()

        # Promotion past the only stage completes the curriculum; the session
        # stops there rather than running the remaining four trials.
        with harness.paths.trials_path.open() as f:
            assert len(list(csv.DictReader(f))) == 2

    def test_without_the_flag_the_session_runs_on(self, tmp_path):
        harness = SessionHarness(
            tmp_path,
            n_trials=6,
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, COMPLETED)]),
        )
        harness.runner._training = self.supervisor(tmp_path, stop=False)

        harness.runner.run()

        with harness.paths.trials_path.open() as f:
            assert len(list(csv.DictReader(f))) == 6


class TestTaskInstanceIsRestored:
    def test_two_sessions_on_one_task_start_from_the_same_place(self, tmp_path):
        """A Task built once and run twice (an example's tests, a batch
        script) kept the last stage's parameters as its base, so the second
        session's stage 1 was the first session's stage 3."""
        task = DemoTask()
        original = task.params.model_copy(deep=True)
        curriculum = Curriculum(
            stages=[
                Stage(name="easy", overrides={"timing.hold_ms": 100.0}),
                Stage(name="real"),
            ]
        )

        def run_one():
            supervisor = TrainingSupervisor(
                curriculum=curriculum,
                state=TrainingState(stage="easy"),
                task=task,
                data_root=tmp_path,
                subject="t01",
                session_id="ses-001_run-01",
            )
            first = task.params.model_copy(deep=True)
            supervisor.request(+1)
            supervisor.transition()
            supervisor.restore_base()
            return first

        first_pass = run_one()
        second_pass = run_one()

        assert first_pass == second_pass
        assert task.params == original
