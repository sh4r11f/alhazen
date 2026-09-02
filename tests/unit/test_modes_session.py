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
    DashboardConfig,
    DevicesConfig,
    DisplayConfig,
    EyeTrackerConfig,
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


class Spy:
    """Stands in for build_session, recording what a mode passed down."""

    def __init__(self):
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return "runner"


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
        assert spy.kwargs["rig"].dashboard.auto_open is False
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

    def test_headless_keeps_the_dashboard_the_rig_configured(self, tmp_path):
        """--headless stops the browser, not the server: the page is still
        there for whoever wants to point a browser at the port."""
        original = rig(tmp_path).model_copy(
            update={"dashboard": DashboardConfig(enabled=True, auto_open=True, port=8765)}
        )

        driven, _ = rig_for_mode(Mode.SIMULATE, original, headless=True)

        assert driven.dashboard.enabled is True
        assert driven.dashboard.port == 8765
        assert driven.dashboard.auto_open is False


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
