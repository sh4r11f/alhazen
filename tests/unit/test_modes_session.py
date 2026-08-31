"""run, test and simulate: what each mode changes, and what it must not.

The point of a rehearsal is that it rehearses THIS session, so most of these
assert that a mode left something alone. The ones that assert a change are
about the three things that are allowed to differ: trial counts, who supplies
the gaze, and which directory the data lands in.
"""

from __future__ import annotations

import pytest

from alhazen import Condition, Model, RigConfig, Task, TrialPlan, TrialSetup, outcomes
from alhazen.config.models import DevicesConfig, EyeTrackerConfig, RewardHwConfig
from alhazen.core.events import EventSchema
from alhazen.errors import ConfigError
from alhazen.modes import Mode
from alhazen.modes.rehearsal import rehearsal_root
from alhazen.modes.session import build_mode_session, next_run
from alhazen.modes.simulation import Simulation
from alhazen.paradigms.config import SchedulerConfig
from support import MONITOR, RunForFrames

EVENTS = EventSchema(("STIM_ON",))
OUTCOMES = outcomes(DONE=dict(completed=True, success=True))


class Params(Model):
    paradigm: SchedulerConfig = SchedulerConfig(n_per_condition=8)


class ModeTask(Task):
    name = "mode-task"
    events = EVENTS
    outcomes = OUTCOMES
    params_model = Params

    def conditions(self, rng):
        return [Condition({"level": v}) for v in (1, 2)]

    def build_trial(self, setup: TrialSetup) -> TrialPlan:
        return TrialPlan(phases=[RunForFrames(1, self.outcomes["DONE"])])


class SimTask(ModeTask):
    """A task that can rehearse itself."""

    name = "sim-task"

    def simulation(self, seed: int) -> Simulation:
        return Simulation(tracker=object(), describe={"seed": seed})


class Spy:
    """Stands in for build_session, recording what a mode passed down."""

    def __init__(self):
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return "runner"


def rig(tmp_path, devices=None):
    return RigConfig(
        monitor=MONITOR,
        data_root=tmp_path / "data",
        devices=devices if devices is not None else DevicesConfig(),
    )


def build(tmp_path, mode, task=None, **kw):
    spy = Spy()
    built = build_mode_session(
        mode,
        rig=rig(tmp_path, kw.pop("devices", None)),
        task=task if task is not None else ModeTask(Params()),
        subject="t01",
        session=1,
        build_session=spy,
        **kw,
    )
    return built, spy


class TestRunModeChangesNothing:
    def test_the_real_run_keeps_the_configured_trial_counts(self, tmp_path):
        built, spy = build(tmp_path, Mode.RUN)

        assert built.reductions == []
        assert spy.kwargs["task"].params.paradigm.n_per_condition == 8

    def test_the_real_run_writes_to_the_rigs_own_data_root(self, tmp_path):
        built, _ = build(tmp_path, Mode.RUN)

        assert built.data_root == tmp_path / "data"

    def test_a_real_run_does_not_start_itself(self, tmp_path):
        _, spy = build(tmp_path, Mode.RUN)

        assert spy.kwargs["auto_start"] is False


class TestTestModeShortensAndRedirects:
    def test_trial_counts_come_down(self, tmp_path):
        built, spy = build(tmp_path, Mode.TEST)

        assert [str(r) for r in built.reductions] == ["paradigm.n_per_condition: 8 -> 1"]
        assert spy.kwargs["task"].params.paradigm.n_per_condition == 1

    def test_the_data_goes_somewhere_the_analysis_will_not_find_it(self, tmp_path):
        built, spy = build(tmp_path, Mode.TEST)

        assert built.data_root == rehearsal_root(tmp_path / "data")
        assert built.data_root != tmp_path / "data"
        # And the session is actually built against that root, not merely
        # told about it.
        assert spy.kwargs["rig"].data_root == built.data_root

    def test_it_is_the_same_task_class_and_the_same_trial_builder(self, tmp_path):
        """The property the whole mode rests on: a rehearsal that went
        through different code would rehearse something else."""
        _, spy = build(tmp_path, Mode.TEST)

        assert isinstance(spy.kwargs["task"], ModeTask)

    def test_asking_for_more_than_the_design_has_does_not_lengthen_it(self, tmp_path):
        """A rehearsal must never be longer than the experiment."""
        built, spy = build(tmp_path, Mode.TEST, n_per_condition=99)

        assert built.reductions == []
        assert spy.kwargs["task"].params.paradigm.n_per_condition == 8

    def test_a_design_that_is_already_short_says_so(self, tmp_path):
        class Short(ModeTask):
            name = "short-task"

        task = Short(Params(paradigm=SchedulerConfig(n_per_condition=1)))
        built, _ = build(tmp_path, Mode.TEST, task=task)

        assert "reduced: nothing" in built.describe()


class TestSimulateMode:
    def test_it_takes_the_stand_ins_from_the_task(self, tmp_path):
        built, spy = build(tmp_path, Mode.SIMULATE, task=SimTask(Params()), seed=7)

        assert spy.kwargs["tracker"] is built.simulation.tracker
        assert built.simulation.describe == {"seed": 7}

    def test_it_starts_itself(self, tmp_path):
        """Nobody is there to press SPACE at the instructions screen."""
        _, spy = build(tmp_path, Mode.SIMULATE, task=SimTask(Params()))

        assert spy.kwargs["auto_start"] is True

    def test_it_also_shortens_and_redirects(self, tmp_path):
        built, _ = build(tmp_path, Mode.SIMULATE, task=SimTask(Params()))

        assert built.reductions
        assert built.data_root == rehearsal_root(tmp_path / "data")

    def test_a_task_with_no_autopilot_is_refused_with_the_reason(self, tmp_path):
        with pytest.raises(ConfigError, match="simulation"):
            build(tmp_path, Mode.SIMULATE)

    @pytest.mark.parametrize(
        "devices",
        [
            DevicesConfig(eyetracker=EyeTrackerConfig(backend="eyelink")),
            DevicesConfig(reward=RewardHwConfig(backend="nidaq")),
        ],
    )
    def test_it_refuses_a_rig_with_real_hardware(self, tmp_path, devices):
        """Inventing a subject on a real rig writes a run directory full of
        numbers that look exactly like a session and are not one."""
        with pytest.raises(ConfigError, match="real hardware"):
            build(tmp_path, Mode.SIMULATE, task=SimTask(Params()), devices=devices)

    def test_a_simulated_tracker_is_not_real_hardware(self, tmp_path):
        devices = DevicesConfig(eyetracker=EyeTrackerConfig(backend="mouse_sim"))

        built, _ = build(tmp_path, Mode.SIMULATE, task=SimTask(Params()), devices=devices)

        assert built.mode is Mode.SIMULATE


class TestModesThatDoNotRunTrials:
    @pytest.mark.parametrize("mode", [Mode.DEMO, Mode.MEASURE])
    def test_they_are_refused_here(self, tmp_path, mode):
        with pytest.raises(ValueError, match="does not run trials"):
            build(tmp_path, mode)


class TestNextRun:
    def test_the_first_run_is_one(self, tmp_path):
        assert next_run(tmp_path, "t01", 1) == 1

    def test_it_counts_the_directories_that_exist(self, tmp_path):
        session = tmp_path / "sub-t01" / "ses-001"
        (session / "run-01_task-x").mkdir(parents=True)
        (session / "run-02_task-x").mkdir()

        assert next_run(tmp_path, "t01", 1) == 3

    def test_a_directory_that_is_not_a_run_is_ignored(self, tmp_path):
        session = tmp_path / "sub-t01" / "ses-001"
        (session / "run-notanumber").mkdir(parents=True)

        assert next_run(tmp_path, "t01", 1) == 1


class TestDescribe:
    def test_it_says_the_data_is_not_going_to_the_real_root(self, tmp_path):
        built, _ = build(tmp_path, Mode.TEST)

        assert "NOT the rig's data root" in built.describe()

    def test_it_lists_every_number_it_changed(self, tmp_path):
        built, _ = build(tmp_path, Mode.TEST)

        assert "paradigm.n_per_condition: 8 -> 1" in built.describe()
