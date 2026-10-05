"""run, test and simulate: what each mode changes, and what it must not.

The point of a rehearsal is that it rehearses THIS session, so most of these
assert that a mode left something alone. The ones that assert a change are
about the three things that are allowed to differ: trial counts, who supplies
the gaze, and which directory the data lands in — and, since every mode runs
on every rig, which of the rig's devices the mode drives (``rig_for_mode``).
"""

from __future__ import annotations

import pytest

from alhazen import Condition, Model, RigConfig, Task, TrialPlan, TrialSetup, outcomes
from alhazen.config.models import (
    DevicesConfig,
    DisplayConfig,
    EyeTrackerConfig,
    LiveMonitorConfig,
    RecordingConfig,
    RewardHwConfig,
    SpikeSourceConfig,
    SyncHwConfig,
)
from alhazen.core.events import EventSchema
from alhazen.errors import ConfigError
from alhazen.modes import Mode, flag_refusal
from alhazen.modes.rehearsal import rehearsal_root
from alhazen.modes.session import build_mode_session, next_run, rig_for_mode
from alhazen.modes.simulation import Simulation
from alhazen.paradigms.config import BlockConfig, SchedulerConfig
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


class SimTaskWithSpikes(ModeTask):
    """A task whose simulated subject has a simulated brain as well.

    The case this exists for: an experiment whose objective is computed
    from spikes cannot rehearse itself with gaze alone, and its simulated
    neurons have to answer the trial the autopilot is running — which only
    the task can build.
    """

    name = "sim-task-spikes"

    def simulation(self, seed: int) -> Simulation:
        return Simulation(tracker=object(), spikes=object(), describe={"seed": seed})


class FakeRunner:
    """What the spy hands back in place of a SessionRunner: enough surface
    for the mode to leave its setup notes on."""

    def __init__(self):
        self.setup_notes: list[str] = []


class Spy:
    """Stands in for build_session, recording what a mode passed down."""

    def __init__(self):
        self.kwargs = None
        self.runner = FakeRunner()

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self.runner


def rig(tmp_path, devices=None, display=None):
    return RigConfig(
        monitor=MONITOR,
        data_root=tmp_path / "data",
        devices=devices if devices is not None else DevicesConfig(),
        display=display if display is not None else DisplayConfig(),
    )


# The rig every lab has: a real tracker, a real pump, real sync lines, a
# recorder to point at and a live spike stream to read. Every mode must take
# it; what differs is what each mode does with it.
LAB_DEVICES = DevicesConfig(
    eyetracker=EyeTrackerConfig(backend="eyelink"),
    reward=RewardHwConfig(backend="nidaq", device="Dev1", channel="ao0"),
    sync=SyncHwConfig(backend="nidaq", event_lines={"FIX_ON": "Dev1/port0/line0"}),
    recording=RecordingConfig(backend="spikeglx"),
    spikes=SpikeSourceConfig(backend="spikeglx"),
)


def build(tmp_path, mode, task=None, **kw):
    spy = Spy()
    built = build_mode_session(
        mode,
        rig=rig(tmp_path, kw.pop("devices", None), kw.pop("display", None)),
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


class TestRunModeOnADevelopmentRig:
    """docs/rigs.md §5, for code that starts a run-mode session itself: the
    command line refuses earlier, but no command line stands in front of a
    script calling build_mode_session."""

    def development_rig(self, tmp_path):
        return rig(tmp_path).model_copy(update={"real_data": False})

    def test_run_mode_is_refused_before_anything_is_built(self, tmp_path):
        spy = Spy()
        with pytest.raises(ConfigError) as refused:
            build_mode_session(
                Mode.RUN,
                rig=self.development_rig(tmp_path),
                task=ModeTask(Params()),
                subject="t01",
                session=1,
                build_session=spy,
                # As the command line records them: the refusal names the rig.
                sources={
                    "rig": str(tmp_path / "rig-laptop.yaml"),
                    "rig_name": "laptop",
                    "rig_source": "alhazen",
                },
            )
        assert spy.kwargs is None
        assert not (tmp_path / "data").exists()
        message = str(refused.value)
        assert message.startswith(
            "run mode records real data, and alhazen/laptop (alhazen's shared rig) is a "
            "development rig"
        )
        assert "rehearse it on this one in test or simulate mode" in message

    def test_without_sources_the_rig_is_refused_unnamed(self, tmp_path):
        with pytest.raises(ConfigError, match="and this rig is a development rig"):
            build_mode_session(
                Mode.RUN,
                rig=self.development_rig(tmp_path),
                task=ModeTask(Params()),
                subject="t01",
                session=1,
                build_session=Spy(),
            )

    @pytest.mark.parametrize("mode", [Mode.TEST, Mode.SIMULATE])
    def test_a_rehearsal_on_it_is_built_as_before(self, tmp_path, mode):
        spy = Spy()
        build_mode_session(
            mode,
            rig=self.development_rig(tmp_path),
            task=SimTask(Params()),
            subject="t01",
            session=1,
            build_session=spy,
        )
        assert spy.kwargs["rig"].data_root == rehearsal_root(tmp_path / "data")


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

    def test_an_unseeded_subject_draws_from_the_seed_the_session_draws(self, tmp_path):
        # The bug this pins: with no --seed (every run the workspace starts),
        # the subject was handed 0 while the session drew its own seed, so
        # every rehearsal had the same subject, and re-running with the
        # printed seed changed the subject. The session's seed is what the
        # builder receives and the snapshot records.
        built, spy = build(tmp_path, Mode.SIMULATE, task=SimTask(Params()))

        drawn = spy.kwargs["seed"]
        assert isinstance(drawn, int)
        assert built.simulation.describe == {"seed": drawn}

    def test_two_unseeded_rehearsals_have_different_subjects(self, tmp_path):
        # Two fresh 32-bit draws collide once in four billion runs.
        first, _ = build(tmp_path, Mode.SIMULATE, task=SimTask(Params()))
        second, _ = build(tmp_path, Mode.SIMULATE, task=SimTask(Params()))

        assert first.simulation.describe != second.simulation.describe

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

    def test_a_simulated_spike_source_reaches_the_builder(self, tmp_path):
        built, spy = build(tmp_path, Mode.SIMULATE, task=SimTaskWithSpikes(Params()))

        assert spy.kwargs["spikes"] is built.simulation.spikes
        assert spy.kwargs["spikes"] is not None

    def test_a_task_that_supplies_no_spikes_passes_none(self, tmp_path):
        # None means "leave the rig's own", which in simulate mode is
        # nothing at all — the same rule every other stand-in follows.
        _, spy = build(tmp_path, Mode.SIMULATE, task=SimTask(Params()))

        assert spy.kwargs["spikes"] is None

    def test_the_rigs_own_probe_still_stands_down_beside_a_simulated_one(self, tmp_path):
        # The task's simulated brain does not make the real probe wanted:
        # the rig's spikeglx device is still dropped, and what reaches the
        # builder is the task's stand-in.
        built, spy = build(
            tmp_path,
            Mode.SIMULATE,
            task=SimTaskWithSpikes(Params()),
            devices=LAB_DEVICES,
        )

        assert spy.kwargs["rig"].devices.spikes is None
        assert spy.kwargs["spikes"] is built.simulation.spikes
        assert "spikes: spikeglx stands down" in built.describe()

    def test_a_simulation_of_spikes_alone_is_not_empty(self, tmp_path):
        # is_empty() asks "did the task supply any stand-in at all", which
        # is what distinguishes a task that forgot to implement simulation()
        # from one that supplied a partial subject.
        assert not Simulation(spikes=object()).is_empty()
        assert Simulation().is_empty()

    def test_the_other_trial_modes_pass_no_spikes(self, tmp_path):
        # Only simulate substitutes a subject; test and run drive whatever
        # the rig config built, which is the rig's own spike source.
        for mode in (Mode.RUN, Mode.TEST):
            _, spy = build(tmp_path, mode, task=SimTaskWithSpikes(Params()))
            assert spy.kwargs["spikes"] is None

    def test_it_runs_on_the_lab_rig_with_every_real_device_stood_down(self, tmp_path):
        """The rig file describes the machine; simulate decides what to
        drive on it, which is nothing that acts on or reads a subject."""
        built, spy = build(tmp_path, Mode.SIMULATE, task=SimTask(Params()), devices=LAB_DEVICES)

        devices = spy.kwargs["rig"].devices
        assert devices.eyetracker is None
        assert devices.reward.backend == "simulated"
        assert devices.sync.backend == "simulated"
        assert devices.recording.backend == "simulated"
        assert devices.spikes is None
        assert built.mode is Mode.SIMULATE

    def test_every_stood_down_device_is_named_before_trial_one(self, tmp_path):
        built, _ = build(tmp_path, Mode.SIMULATE, task=SimTask(Params()), devices=LAB_DEVICES)

        described = built.describe()
        for line in (
            "eyetracker: eyelink stands down",
            "reward: nidaq stands down",
            "sync: nidaq stands down",
            "recording: spikeglx stands down",
            "spikes: spikeglx stands down",
        ):
            assert line in described

    def test_the_stand_ins_keep_the_rigs_own_settings(self, tmp_path):
        """A sync line map or a pulse width is the experiment's; only the
        backend changes, so the logged pulses still say which line."""
        _, spy = build(tmp_path, Mode.SIMULATE, task=SimTask(Params()), devices=LAB_DEVICES)

        assert spy.kwargs["rig"].devices.sync.event_lines == {"FIX_ON": "Dev1/port0/line0"}

    def test_a_rig_that_is_already_simulated_is_left_alone(self, tmp_path):
        devices = DevicesConfig(
            eyetracker=EyeTrackerConfig(backend="mouse_sim"),
            reward=RewardHwConfig(backend="simulated"),
            spikes=SpikeSourceConfig(backend="simulated"),
        )

        built, spy = build(tmp_path, Mode.SIMULATE, task=SimTask(Params()), devices=devices)

        assert spy.kwargs["rig"].devices == devices
        assert built.notes == []

    def test_the_rig_file_itself_is_not_touched(self, tmp_path):
        original = rig(tmp_path, LAB_DEVICES)
        before = original.model_copy(deep=True)

        build_mode_session(
            Mode.SIMULATE,
            rig=original,
            task=SimTask(Params()),
            subject="t01",
            session=1,
            build_session=Spy(),
        )

        assert original == before


class TestHeadless:
    def test_it_takes_the_window_and_the_browser_away(self, tmp_path):
        built, spy = build(
            tmp_path,
            Mode.SIMULATE,
            task=SimTask(Params()),
            display=DisplayConfig(backend="psychopy"),
            headless=True,
        )

        assert spy.kwargs["rig"].display.backend == "simulated"
        assert spy.kwargs["rig"].live_monitor.auto_open is False
        assert "display: none (--headless)" in built.describe()

    def test_the_rest_of_the_display_config_survives(self, tmp_path):
        """Only the backend changes: the frame-QA policy the rig asked for
        is still what a headless session is judged by."""
        _, spy = build(
            tmp_path,
            Mode.SIMULATE,
            task=SimTask(Params()),
            display=DisplayConfig(backend="psychopy", warmup_flips=240),
            headless=True,
        )

        assert spy.kwargs["rig"].display.warmup_flips == 240

    @pytest.mark.parametrize("mode", [Mode.TEST, Mode.RUN])
    def test_the_other_trial_modes_refuse_it_by_name(self, tmp_path, mode):
        with pytest.raises(ConfigError, match="--headless: only simulate mode"):
            build(tmp_path, mode, headless=True)


class TestMouseAsGaze:
    def test_test_mode_on_a_rig_with_no_tracker_takes_the_mouse(self, tmp_path):
        """A laptop has no tracker and the rig file does not pretend it does;
        the mode notices and says what it did."""
        built, spy = build(tmp_path, Mode.TEST)

        assert spy.kwargs["rig"].devices.eyetracker.backend == "mouse_sim"
        assert "eyetracker: the mouse cursor stands in for gaze — this rig has none" in (
            built.describe()
        )

    def test_test_mode_on_the_lab_rig_uses_the_lab_tracker(self, tmp_path):
        """A person in the chair with the real tracker IS the rehearsal."""
        built, spy = build(tmp_path, Mode.TEST, devices=LAB_DEVICES)

        assert spy.kwargs["rig"].devices == LAB_DEVICES
        assert built.notes == []

    def test_mouse_switches_the_lab_tracker_off(self, tmp_path):
        built, spy = build(tmp_path, Mode.TEST, devices=LAB_DEVICES, mouse=True)

        assert spy.kwargs["rig"].devices.eyetracker.backend == "mouse_sim"
        assert "eyelink switched off (--mouse)" in built.describe()
        # And only the tracker: the pump still pumps for a person.
        assert spy.kwargs["rig"].devices.reward.backend == "nidaq"

    def test_a_simulated_display_has_no_window_for_a_cursor(self, tmp_path):
        built, spy = build(tmp_path, Mode.TEST, display=DisplayConfig(backend="simulated"))

        assert spy.kwargs["rig"].devices.eyetracker is None
        assert "no window for a mouse cursor" in built.describe()

    def test_asking_for_the_mouse_with_no_window_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="--mouse needs a window"):
            build(tmp_path, Mode.TEST, display=DisplayConfig(backend="simulated"), mouse=True)

    @pytest.mark.parametrize("mode", [Mode.SIMULATE, Mode.RUN])
    def test_the_other_trial_modes_refuse_it_by_name(self, tmp_path, mode):
        with pytest.raises(ConfigError, match="--mouse: only test mode"):
            build(tmp_path, mode, task=SimTask(Params()), mouse=True)


class TestRigForMode:
    """The pure function behind the above, over the modes that never reach
    build_mode_session: they take any rig unchanged and refuse both flags."""

    @pytest.mark.parametrize("mode", [Mode.MEASURE, Mode.DEMO, Mode.MOVIE, Mode.RUN])
    def test_the_lab_rig_passes_through_untouched(self, tmp_path, mode):
        original = rig(tmp_path, LAB_DEVICES)

        driven, notes = rig_for_mode(mode, original)

        assert driven is original
        assert notes == []

    @pytest.mark.parametrize("mode", [m for m in Mode if m is not Mode.SIMULATE])
    def test_headless_is_refused_everywhere_but_simulate(self, mode):
        refusal = flag_refusal(mode, headless=True)

        assert refusal is not None
        assert refusal.startswith("--headless: only simulate mode")
        assert f"{mode.value} mode" in refusal

    @pytest.mark.parametrize("mode", [m for m in Mode if m is not Mode.TEST])
    def test_mouse_is_refused_everywhere_but_test(self, mode):
        refusal = flag_refusal(mode, mouse=True)

        assert refusal is not None
        assert refusal.startswith("--mouse: only test mode")
        assert f"{mode.value} mode" in refusal

    def test_no_flags_are_never_refused(self):
        assert all(flag_refusal(mode) is None for mode in Mode)

    def test_headless_keeps_the_live_monitor_the_rig_configured(self, tmp_path):
        """--headless stops the browser, not the server: the page is still
        there for whoever wants to point a browser at the port."""
        original = rig(tmp_path).model_copy(
            update={"live_monitor": LiveMonitorConfig(enabled=True, auto_open=True, port=8765)}
        )

        driven, _ = rig_for_mode(Mode.SIMULATE, original, headless=True)

        assert driven.live_monitor.enabled is True
        assert driven.live_monitor.port == 8765
        assert driven.live_monitor.auto_open is False


class TestModesThatDoNotRunTrials:
    @pytest.mark.parametrize("mode", [Mode.DEMO, Mode.MEASURE])
    def test_they_are_refused_here(self, tmp_path, mode):
        with pytest.raises(ValueError, match="does not run trials"):
            build(tmp_path, mode)


class TestNextRun:
    """Runs are numbered within the version's folder, where they are made
    (alhazen 2.0)."""

    def test_the_first_run_is_one(self, tmp_path):
        assert next_run(tmp_path, "t01", 1, experiment_version="0.4.0") == 1

    def test_it_counts_the_directories_that_exist(self, tmp_path):
        session = tmp_path / "v0.4.0" / "sub-t01" / "ses-001"
        (session / "run-01_task-x").mkdir(parents=True)
        (session / "run-02_task-x").mkdir()

        assert next_run(tmp_path, "t01", 1, experiment_version="0.4.0") == 3

    def test_a_directory_that_is_not_a_run_is_ignored(self, tmp_path):
        session = tmp_path / "v0.4.0" / "sub-t01" / "ses-001"
        (session / "run-notanumber").mkdir(parents=True)

        assert next_run(tmp_path, "t01", 1, experiment_version="0.4.0") == 1

    def test_each_version_counts_its_own_runs(self, tmp_path):
        (tmp_path / "v0.4.0" / "sub-t01" / "ses-001" / "run-03_task-x").mkdir(parents=True)

        assert next_run(tmp_path, "t01", 1, experiment_version="0.4.0") == 4
        assert next_run(tmp_path, "t01", 1, experiment_version="0.5.0") == 1

    def test_runs_from_before_2_0_are_in_no_versions_folder(self, tmp_path):
        # The pre-2.0 layout, directly under the root: not where a new run
        # is made, so not counted.
        (tmp_path / "sub-t01" / "ses-001" / "run-05_task-x").mkdir(parents=True)

        assert next_run(tmp_path, "t01", 1, experiment_version="0.4.0") == 1

    def test_the_number_it_gives_is_the_folder_the_session_makes(self, tmp_path):
        from alhazen.data.paths import SessionPaths

        run = next_run(tmp_path, "t01", 1, experiment_version="0.4.0")
        made = SessionPaths.create(tmp_path, "t01", 1, run, "x", experiment_version="0.4.0")
        made.trials_path.write_text("trial_index\n")

        assert next_run(tmp_path, "t01", 1, experiment_version="0.4.0") == run + 1


class TestTheExperimentVersion:
    """A mode finds the experiment once, from the task class it was handed,
    numbers the run inside that version's folder, and hands the same
    experiment to the builder — so the run is made where it was numbered."""

    def test_the_version_comes_from_the_tasks_own_project(self, tmp_path):
        from alhazen.config.experiment import find_experiment

        built, spy = build(tmp_path, Mode.RUN)

        # ModeTask lives in alhazen's own tree, so it is alhazen's project.
        assert built.experiment == find_experiment(ModeTask)
        assert spy.kwargs["experiment"] == built.experiment
        assert built.experiment.version_source == "pyproject.toml"

    def test_an_explicit_version_wins_and_says_so(self, tmp_path):
        built, spy = build(
            tmp_path, Mode.RUN, experiment_version="0.9.0", experiment_name="the-study"
        )

        assert (built.experiment.name, built.experiment.version) == ("the-study", "0.9.0")
        assert built.experiment.version_source == "given to build_session"
        assert spy.kwargs["experiment"] is built.experiment

    def test_the_run_is_numbered_within_the_version(self, tmp_path):
        done = tmp_path / "data" / "v0.9.0" / "sub-t01" / "ses-001" / "run-02_task-mode-task"
        done.mkdir(parents=True)
        other = tmp_path / "data" / "v0.8.0" / "sub-t01" / "ses-001" / "run-07_task-mode-task"
        other.mkdir(parents=True)

        built, spy = build(tmp_path, Mode.RUN, experiment_version="0.9.0")

        assert built.run == spy.kwargs["run"] == 3

    def test_a_rehearsal_is_numbered_within_the_version_under_its_own_root(self, tmp_path):
        rehearsal = rehearsal_root(tmp_path / "data")
        (rehearsal / "v0.9.0" / "sub-t01" / "ses-001" / "run-01_task-mode-task").mkdir(parents=True)

        built, _ = build(tmp_path, Mode.TEST, experiment_version="0.9.0")

        assert built.data_root == rehearsal
        assert built.run == 2

    def test_the_mode_is_handed_down_for_the_record(self, tmp_path):
        _, spy = build(tmp_path, Mode.TEST)
        assert spy.kwargs["mode"] == "test"

    def test_the_experiment_is_found_from_the_class_handed_in(self, tmp_path, monkeypatch):
        # A simulation may swap in a task of another class, defined in
        # another project; the version is still the one the caller's task
        # declares, and it is looked up once.
        from alhazen.config import experiment as experiment_module
        from alhazen.config.experiment import Experiment

        class StandIn(SimTask):
            name = "stand-in"

        class SwapsItsTask(SimTask):
            def simulation(self, seed):
                return Simulation(tracker=object(), task=StandIn(Params()))

        asked: list[type] = []

        def find(task_class):
            asked.append(task_class)
            return Experiment(task_class.__name__, "1.0", "pyproject.toml", None)

        monkeypatch.setattr(experiment_module, "find_experiment", find)
        _, spy = build(tmp_path, Mode.SIMULATE, task=SwapsItsTask(Params()))

        assert spy.kwargs["task"].name == "stand-in"
        assert asked == [SwapsItsTask]
        assert spy.kwargs["experiment"].name == "SwapsItsTask"

    def test_describe_names_the_version_folder_and_where_the_number_came_from(self, tmp_path):
        built, _ = build(tmp_path, Mode.RUN, experiment_version="0.9.0", experiment_name="s")
        assert (
            "experiment: s 0.9.0 — filed under v0.9.0/ (version from given to build_session)"
            in built.describe().splitlines()
        )

    def test_a_project_with_no_version_is_refused_before_anything_is_built(
        self, tmp_path, monkeypatch
    ):
        from alhazen.config import experiment as experiment_module

        def no_version(task_class):
            raise ConfigError("pyproject.toml gives no [project] version")

        monkeypatch.setattr(experiment_module, "find_experiment", no_version)
        with pytest.raises(ConfigError, match="no \\[project\\] version"):
            build(tmp_path, Mode.RUN)
        assert not (tmp_path / "data").exists()


class TestDescribe:
    def test_it_says_the_data_is_not_going_to_the_real_root(self, tmp_path):
        built, _ = build(tmp_path, Mode.TEST)

        assert "NOT the rig's data root" in built.describe()

    def test_it_lists_every_number_it_changed(self, tmp_path):
        built, _ = build(tmp_path, Mode.TEST)

        assert "paradigm.n_per_condition: 8 -> 1" in built.describe()

    def test_the_same_lines_are_handed_to_the_runner_for_the_session_log(self, tmp_path):
        """Printed to a terminal, the reductions and stand-downs leave no
        trace in the run directory; the runner logs them after "session
        start" so the log says what was live and what was reduced."""
        built, spy = build(tmp_path, Mode.SIMULATE, task=SimTask(Params()), devices=LAB_DEVICES)

        assert spy.runner.setup_notes == built.describe().splitlines()
        assert any("eyetracker: eyelink stands down" in line for line in spy.runner.setup_notes)
        assert any("reduced: paradigm.n_per_condition" in line for line in spy.runner.setup_notes)


class TestTheBreakBetweenBlocksInASimulation:
    def test_simulate_mode_resumes_a_break_by_itself(self, tmp_path):
        from alhazen.modes.session import SIMULATION_REST_RESUME_S

        _, spy = build(tmp_path, Mode.SIMULATE, task=SimTask(Params()))

        assert spy.kwargs["rest_resume_after_s"] == SIMULATION_REST_RESUME_S == 10.0

    def test_a_real_run_waits_for_a_person(self, tmp_path):
        _, spy = build(tmp_path, Mode.RUN)

        assert spy.kwargs["rest_resume_after_s"] is None

    def test_a_test_run_waits_for_a_person(self, tmp_path):
        """Test mode is somebody sitting through the session: their break is
        theirs to end."""
        _, spy = build(tmp_path, Mode.TEST)

        assert spy.kwargs["rest_resume_after_s"] is None


class TestInitialsReachTheBuilder:
    def test_the_subjects_initials_are_handed_down(self, tmp_path):
        _, spy = build(tmp_path, Mode.TEST, initials="HD")
        assert spy.kwargs["initials"] == "HD"

    def test_none_is_handed_down_as_none(self, tmp_path):
        _, spy = build(tmp_path, Mode.RUN)
        assert spy.kwargs["initials"] is None


class ValidatingParams(Model):
    """Two blocks whose break ends with a validation of the eye tracker."""

    paradigm: SchedulerConfig = SchedulerConfig(
        n_per_condition=1, blocks=BlockConfig(n_blocks=2, validate_after_break=True)
    )


class ValidatingTask(ModeTask):
    """A task whose design validates between blocks, with a simulated subject
    who looks at the screen's centre whatever is shown."""

    name = "validating-task"
    params_model = ValidatingParams

    def simulation(self, seed: int) -> Simulation:
        from alhazen.devices.eyetracker import GazeSample
        from alhazen.devices.eyetracker.scripted import ScriptedTracker
        from alhazen.testing import FakeClock

        # Built on a clock of its own; the session hands it the session's
        # clock when it configures the tracker.
        centre = GazeSample(gx=MONITOR.width_px / 2, gy=MONITOR.height_px / 2, t=0.0)
        return Simulation(tracker=ScriptedTracker([(0.0, centre)], FakeClock()))


class TestTheBreaksValidationInEachMode:
    """`validate_after_break` in the modes that rehearse a session. Simulate
    validates the tracker its autopilot supplies, as the rest of the
    calibration flow runs on that stand-in, and nothing waits for a key; a
    mode left with no eye tracker at all is refused before it starts."""

    def test_simulate_validates_the_autopilots_tracker_and_runs_to_the_end(self, tmp_path):
        import csv

        from alhazen.testing import FakeClock

        built = build_mode_session(
            Mode.SIMULATE,
            rig=rig(tmp_path),
            task=ValidatingTask(ValidatingParams()),
            subject="t01",
            session=1,
            headless=True,
            # Simulated time: every flip moves the session clock one frame.
            clock=FakeClock(),
        )
        built.runner.run()

        (events_path,) = built.data_root.rglob("*_events.csv")
        with events_path.open(newline="", encoding="utf-8") as f:
            names = [row["event"] for row in csv.DictReader(f)]
        assert names.index("PAUSED") < names.index("VALIDATION") < names.index("RESUMED")
        (trials_path,) = built.data_root.rglob("*_trials.csv")
        with trials_path.open(newline="", encoding="utf-8") as f:
            assert [row["block"] for row in csv.DictReader(f)] == ["1", "1", "2", "2"]

    def test_test_mode_with_no_tracker_and_no_window_for_a_mouse_is_refused(self, tmp_path):
        # A rig with a simulated display and no tracker: no mouse can stand
        # in, so there is nothing to validate, and the session says so.
        with pytest.raises(ConfigError, match="no eye tracker to validate"):
            build_mode_session(
                Mode.TEST,
                rig=rig(tmp_path, display=DisplayConfig(backend="simulated")),
                task=ValidatingTask(ValidatingParams()),
                subject="t01",
                session=1,
            )
