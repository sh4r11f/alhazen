"""What a task supplies to every way of starting it.

A session can be started four ways — ``alhazen run --task``, an experiment's
own ``run.py`` (``run_experiment``), ``build_mode_session`` and
``build_session(task=...)`` — and they used to disagree about what the subject
was shown before trial one: only ``run.py`` was ever handed the wording, so a
real session started with ``alhazen run`` showed no instructions at all, and
nothing said so. The task is the one thing all four are handed, so the
wording lives on it now (``Task.instructions``). These tests start the same
task each way and check that the subject is shown the same screen, that an
unattended simulation still never waits for a key, that a run-mode session
names a task that never said, and that a task written before any of this
behaves exactly as it did.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from alhazen import Duration, Model, RigConfig, Task, TrialPlan, outcomes
from alhazen.config.models import DisplayConfig
from alhazen.core.events import EventSchema
from alhazen.errors import ConfigError
from alhazen.modes import Mode
from alhazen.modes.session import build_mode_session, undeclared_instructions_warning
from alhazen.modes.simulation import Simulation
from alhazen.paradigms.config import SchedulerConfig
from alhazen.session.builder import build_session
from alhazen.task.task import declares_instructions, task_instructions
from alhazen.training import Curriculum, Stage
from support import MONITOR, RunForFrames

# Two paragraphs, as an instructions.md would give them: the display reflows
# prose, so the second paragraph surviving is what shows the text arrived
# whole rather than truncated at its first line.
TEXT = "Look at the dot in the middle.\n\nPress SPACE to begin."
FIRST_LINE = "Look at the dot in the middle."

EVENTS = EventSchema(("STIM_ON",))
OUTCOMES = outcomes(DONE=dict(completed=True, success=True))


class Params(Model):
    # Two trials per cell, so test and simulate modes visibly reduce it.
    paradigm: SchedulerConfig = SchedulerConfig(n_per_condition=2)
    iti: Duration = Duration(ms=0)
    hold_ms: float = 500.0


class SilentTask(Task):
    """Written before a task could say what its subject reads: it declares
    nothing, and must behave exactly as every task did before."""

    name = "silent-task"
    events = EVENTS
    outcomes = OUTCOMES
    params_model = Params

    def build_trial(self, setup):
        return TrialPlan(phases=[RunForFrames(1, self.outcomes["DONE"])])

    def simulation(self, seed):
        # A subject for simulate mode. The trial reads no gaze; the tracker
        # is only what makes the simulation non-empty, which the mode needs.
        from alhazen.devices.automated import AutomatedGazeTracker

        return Simulation(tracker=AutomatedGazeTracker(), describe={"seed": seed})


class TalkingTask(SilentTask):
    """Says what its subject reads."""

    name = "talking-task"

    def instructions(self):
        return TEXT


class MuteTask(SilentTask):
    """Says, on purpose, that its subject reads nothing — an animal."""

    name = "mute-task"

    def instructions(self):
        return None


INSTALLED = {task.name: task for task in (SilentTask, TalkingTask, MuteTask)}


@pytest.fixture
def installed(monkeypatch):
    """Register this module's tasks the way an installed experiment package
    would, so ``alhazen run --task <name>`` finds them by name."""
    monkeypatch.setattr(
        "alhazen.cli.tasks.installed_tasks",
        lambda: {
            name: SimpleNamespace(name=name, load=lambda task=task: task)
            for name, task in INSTALLED.items()
        },
    )


def rig_file(tmp_path: Path, backend: str = "simulated") -> Path:
    """A rig config on disk, as the command line takes it: simulated display,
    no devices, data under tmp_path/data."""
    path = tmp_path / "rig.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "monitor": MONITOR.model_dump(),
                "display": {"backend": backend},
                "data_root": str(tmp_path / "data"),
            }
        )
    )
    return path


def rig(tmp_path: Path) -> RigConfig:
    return RigConfig(
        monitor=MONITOR, display=DisplayConfig(backend="simulated"), data_root=tmp_path / "data"
    )


def session_log(data_root: Path) -> str:
    """The session.log of the one run under ``data_root``.

    The simulated display logs every message exactly as it would have drawn
    it, so this is the record of what the subject would have read.
    """
    runs = sorted(data_root.glob("sub-*/ses-*/run-*"))
    assert len(runs) == 1, runs
    return (runs[0] / "session.log").read_text(encoding="utf-8")


def build(tmp_path: Path, task: Task, **kwargs):
    """build_session over a simulated rig, unpaced, the way a task's own
    end-to-end test calls it."""
    return build_session(
        rig=rig(tmp_path),
        subject="t01",
        session=1,
        run=1,
        task=task,
        seed=1,
        simulated_frame_period_s=0.0,
        date_yyyymmdd="20260923",
        **kwargs,
    )


def no_run_was_written(tmp_path: Path) -> bool:
    return not list((tmp_path / "data").glob("sub-*"))


class TestWhatATaskCanSay:
    """Three states, told apart without calling anything: text, a
    deliberate None, and never having said."""

    def test_a_task_that_never_said_has_not_declared(self):
        assert declares_instructions(SilentTask) is False
        assert SilentTask(Params()).instructions() is None

    def test_returning_text_is_a_declaration(self):
        assert declares_instructions(TalkingTask) is True
        assert TalkingTask(Params()).instructions() == TEXT

    def test_returning_none_on_purpose_is_a_declaration(self):
        """The whole point of the third state: a monkey task that has
        nothing to show has said so, and must not be warned about."""
        assert declares_instructions(MuteTask) is True
        assert MuteTask(Params()).instructions() is None

    def test_a_shared_base_declares_for_every_task_under_it(self):
        class AnimalTask(SilentTask):
            """Abstract in spirit: a family of tasks for animal subjects."""

            def instructions(self):
                return None

        class Fixate(AnimalTask):
            name = "fixate-for-juice"

        assert declares_instructions(Fixate) is True

    def test_an_entry_point_factory_declares_nothing(self):
        """An entry point may name a factory rather than a Task subclass.
        It has no hook, and reads as a task that never said."""

        def factory(params):
            return SilentTask(params)

        assert declares_instructions(factory) is False
        assert task_instructions(SimpleNamespace()) is None

    @pytest.mark.parametrize("value", ["Look at the dot.", None])
    def test_a_value_instead_of_a_method_is_refused_when_the_class_is_written(self, value):
        """`instructions = "..."` reads like the other declarations, and
        would otherwise fail as "'str' object is not callable" while a
        session is being built, with the subject in the chair."""
        with pytest.raises(TypeError, match=r"def instructions\(self\)"):

            class Declared(SilentTask):
                name = "declared-as-data"
                instructions = value


class TestTheTextIsChecked:
    def test_text_passes_through_exactly(self):
        assert task_instructions(TalkingTask(Params())) == TEXT

    def test_none_means_no_instruction_screen(self):
        assert task_instructions(MuteTask(Params())) is None
        assert task_instructions(SilentTask(Params())) is None

    def test_a_path_instead_of_its_contents_is_refused(self):
        class ReturnsAPath(SilentTask):
            name = "returns-a-path"

            def instructions(self):
                return Path("instructions.md")

        with pytest.raises(TypeError, match=r"ReturnsAPath\.instructions\(\).*str"):
            task_instructions(ReturnsAPath(Params()))

    @pytest.mark.parametrize("text", ["", "  \n\t\n"])
    def test_empty_text_is_refused_rather_than_shown_as_a_blank_screen(self, text):
        class Blank(SilentTask):
            name = "blank-text"

            def instructions(self):
                return text

        with pytest.raises(ConfigError, match="return None instead"):
            task_instructions(Blank(Params()))


class TestBuildSessionShowsThem:
    """``build_session(task=...)``: the one place every other entry point
    ends up, so the one place the task is asked."""

    def test_the_tasks_text_is_the_first_thing_on_screen(self, tmp_path):
        runner = build(tmp_path, TalkingTask(Params()))
        runner.run()

        assert runner._display.messages[0] == TEXT
        assert FIRST_LINE in session_log(tmp_path / "data")

    def test_a_task_that_never_said_shows_nothing_as_before(self, tmp_path):
        runner = build(tmp_path, SilentTask(Params()))
        runner.run()

        assert runner._instructions is None
        assert runner._display.messages == []

    def test_a_task_that_declared_none_shows_nothing(self, tmp_path):
        runner = build(tmp_path, MuteTask(Params()))
        runner.run()

        assert runner._display.messages == []

    def test_the_callers_own_text_wins(self, tmp_path):
        runner = build(tmp_path, TalkingTask(Params()), instructions="The caller's words.")

        assert runner._instructions == "The caller's words."

    def test_an_empty_string_from_the_caller_shows_nothing(self, tmp_path):
        """The one way for a caller to turn off a task's instruction screen:
        None means "ask the task", so "" means "none"."""
        runner = build(tmp_path, TalkingTask(Params()), instructions="")
        runner.run()

        assert runner._display.messages == []

    def test_the_text_sees_the_params_a_curriculum_stage_set(self, tmp_path):
        """Asked after the curriculum block: wording that quotes a parameter
        must quote the one the session runs at."""

        class Quoting(SilentTask):
            name = "quoting-task"

            def instructions(self):
                return f"Hold still for {self.params.hold_ms:g} ms."

        runner = build(
            tmp_path,
            Quoting(Params()),
            curriculum=Curriculum(stages=[Stage(name="easy", overrides={"hold_ms": 120.0})]),
        )

        assert runner._instructions == "Hold still for 120 ms."

    def test_broken_text_fails_before_a_run_directory_exists(self, tmp_path):
        class Broken(SilentTask):
            name = "broken-text"

            def instructions(self):
                return ""

        with pytest.raises(ConfigError, match="empty text"):
            build(tmp_path, Broken(Params()))
        assert no_run_was_written(tmp_path)

    def test_a_missing_instructions_file_surfaces_with_its_own_error(self, tmp_path):
        """Not swallowed and not reworded: the task's own exception, raised
        before anything is written, is what names the file."""

        class ReadsAFile(SilentTask):
            name = "reads-a-file"

            def instructions(self):
                return (tmp_path / "instructions.md").read_text(encoding="utf-8")

        with pytest.raises(FileNotFoundError, match="instructions.md"):
            build(tmp_path, ReadsAFile(Params()))
        assert no_run_was_written(tmp_path)


class TestEveryEntryPointShowsThem:
    """The same task, started each way a session can be started."""

    def test_alhazen_run_in_run_mode(self, tmp_path, installed, capsys):
        from alhazen.cli.main import main

        code = main(
            ["run", "--task", "talking-task", "--rig", str(rig_file(tmp_path))]
            + ["--sub", "s01", "--ses", "1"]
        )

        assert code == 0, capsys.readouterr().err
        assert FIRST_LINE in session_log(tmp_path / "data")

    def test_alhazen_run_in_test_mode(self, tmp_path, installed, capsys):
        from alhazen.cli.main import main

        code = main(
            ["run", "--task", "talking-task", "--mode", "test", "--rig", str(rig_file(tmp_path))]
            + ["--sub", "s01", "--ses", "1"]
        )

        assert code == 0, capsys.readouterr().err
        assert FIRST_LINE in session_log(tmp_path / "data-rehearsal")

    def test_a_run_py_that_passes_no_wording(self, tmp_path, capsys):
        """run.py needs no wiring of its own: the task's text reaches the
        subject through run_experiment exactly as through alhazen run."""
        from alhazen.cli.modes import run_experiment

        code = run_experiment(
            task_class=TalkingTask,
            default_rig=rig_file(tmp_path),
            argv=["--sub", "s01", "--ses", "1"],
        )

        assert code == 0, capsys.readouterr().err
        assert FIRST_LINE in session_log(tmp_path / "data")

    def test_a_run_py_that_passes_its_own_wording_takes_precedence(self, tmp_path, capsys):
        from alhazen.cli.modes import run_experiment

        code = run_experiment(
            task_class=TalkingTask,
            default_rig=rig_file(tmp_path),
            instructions=lambda: "run.py's own words.",
            argv=["--sub", "s01", "--ses", "1"],
        )

        assert code == 0, capsys.readouterr().err
        log = session_log(tmp_path / "data")
        assert "run.py's own words." in log
        assert FIRST_LINE not in log

    @pytest.mark.parametrize("mode", [Mode.RUN, Mode.TEST, Mode.SIMULATE])
    def test_build_mode_session_in_every_mode_that_runs_trials(self, tmp_path, mode):
        built = build_mode_session(
            mode, rig=rig(tmp_path), task=TalkingTask(Params()), subject="t01", session=1
        )

        assert built.runner._instructions.startswith(TEXT)


class TestSimulateStartsByItself:
    """Nobody is in the chair for a simulation, so showing it the task's
    instructions must never mean waiting for a key."""

    def test_the_gate_on_a_real_display(self):
        from alhazen.session.builder import (
            _psychopy_auto_start,
            _psychopy_await_start,
            _start_gate,
        )

        # Unattended (simulate, --auto): shown for two seconds, then it starts.
        assert _start_gate(TEXT, "psychopy", auto_start=True) is _psychopy_auto_start
        # Somebody in the chair (run, test): SPACE starts, ESC cancels.
        assert _start_gate(TEXT, "psychopy", auto_start=False) is _psychopy_await_start

    @pytest.mark.parametrize(
        ("text", "display_kind", "auto_start"),
        [
            (TEXT, "simulated", False),  # no keyboard behind a simulated display
            (TEXT, "simulated", True),
            (None, "psychopy", False),  # nothing to read, nothing to wait on
            ("", "psychopy", True),
        ],
    )
    def test_no_gate_at_all(self, text, display_kind, auto_start):
        from alhazen.session.builder import _start_gate

        assert _start_gate(text, display_kind, auto_start) is None

    def test_simulate_mode_asks_for_the_unattended_start(self, tmp_path):
        built = build_mode_session(
            Mode.SIMULATE, rig=rig(tmp_path), task=TalkingTask(Params()), subject="t01", session=1
        )

        assert built.runner._instructions == f"{TEXT}\n\nAUTOMATED DEMO — starting automatically..."
        # A simulated display: no gate at all, the session starts at once.
        assert built.runner._await_start is None

    def test_a_headless_simulation_through_alhazen_run_runs_to_the_end(
        self, tmp_path, installed, capsys
    ):
        """The lab rig's own file, a real display in it, and --headless: the
        instructions are shown (logged) and the session never stops for them."""
        from alhazen.cli.main import main

        code = main(
            ["run", "--task", "talking-task", "--mode", "simulate", "--headless"]
            + ["--rig", str(rig_file(tmp_path, backend="psychopy")), "--no-dashboard"]
        )

        assert code == 0, capsys.readouterr().err
        log = session_log(tmp_path / "data-rehearsal")
        assert FIRST_LINE in log
        assert "AUTOMATED DEMO — starting automatically" in log
        assert "session end: complete" in log


class TestRunModeNamesATaskThatNeverSaid:
    """A run-mode session is the one a subject sits through. A task that
    never said what that subject reads gets a WARNING naming the method,
    and a line in what is printed before trial one."""

    def warnings(self, caplog) -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.name == "alhazen.modes.session" and record.levelno == logging.WARNING
        ]

    def test_the_warning_names_the_method_and_both_answers(self):
        warning = undeclared_instructions_warning(SilentTask)

        assert "SilentTask.instructions(self) -> str | None" in warning
        assert "return the text" in warning
        assert "return None" in warning

    def test_run_mode_warns_and_says_so_before_trial_one(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="alhazen.modes.session"):
            built = build_mode_session(
                Mode.RUN, rig=rig(tmp_path), task=SilentTask(Params()), subject="t01", session=1
            )

        assert self.warnings(caplog) == [undeclared_instructions_warning(SilentTask)]
        described = built.describe()
        assert "instructions: none — SilentTask does not declare instructions()" in described
        # The same line reaches the run's session.log, which outlives the
        # terminal it was printed on.
        assert any("instructions: none" in note for note in built.runner.setup_notes)

    def test_through_alhazen_run(self, tmp_path, installed, caplog, capsys):
        from alhazen.cli.main import main

        with caplog.at_level(logging.WARNING, logger="alhazen.modes.session"):
            code = main(
                ["run", "--task", "silent-task", "--rig", str(rig_file(tmp_path))]
                + ["--sub", "s01", "--ses", "1"]
            )

        assert code == 0
        assert self.warnings(caplog) == [undeclared_instructions_warning(SilentTask)]
        assert "instructions: none — SilentTask" in capsys.readouterr().out
        assert "setup: instructions: none" in session_log(tmp_path / "data")

    @pytest.mark.parametrize(
        ("mode", "task", "given"),
        [
            (Mode.RUN, MuteTask, None),  # declared None on purpose: an answer
            (Mode.RUN, TalkingTask, None),  # declared text
            (Mode.RUN, SilentTask, "run.py's own words."),  # run.py answered for it
            (Mode.TEST, SilentTask, None),  # a rehearsal: shows whatever is declared
            (Mode.SIMULATE, SilentTask, None),  # nobody to read anything
        ],
    )
    def test_no_warning_where_there_is_no_gap(self, tmp_path, caplog, mode, task, given):
        with caplog.at_level(logging.WARNING, logger="alhazen.modes.session"):
            built = build_mode_session(
                mode,
                rig=rig(tmp_path),
                task=task(Params()),
                subject="t01",
                session=1,
                instructions=given,
            )

        assert self.warnings(caplog) == []
        assert "instructions: none" not in built.describe()


class TestATaskWrittenBeforeTheHooksIsUnchanged:
    """Nothing about a task that declares none of this may change, apart
    from the run-mode warning that says it declared none."""

    @pytest.mark.parametrize("mode", ["test", "simulate"])
    def test_no_instruction_screen_and_nothing_new_said(self, tmp_path, installed, capsys, mode):
        from alhazen.cli.main import main

        code = main(
            ["run", "--task", "silent-task", "--mode", mode, "--rig", str(rig_file(tmp_path))]
            + ["--sub", "s01", "--ses", "1"]
        )

        assert code == 0, capsys.readouterr().err
        out = capsys.readouterr().out
        assert "instructions" not in out
        assert "display message" not in session_log(tmp_path / "data-rehearsal")
