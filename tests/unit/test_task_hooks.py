"""What a task supplies to every way of starting it.

A session can be started four ways — ``alhazen run --task``, an experiment's
own ``run.py`` (``run_experiment``), ``build_mode_session`` and
``build_session(task=...)`` — and they used to disagree. Only ``run.py`` was
ever handed the subject's wording, the experiment's params file and its params
hook, so a real session started with ``alhazen run`` showed no instructions,
ran the params model's defaults in place of the experiment's file, and could
not start a task that needs to know who and which session — and said nothing.
The task is the one thing all four are handed, so all three live on it now
(``Task.instructions``, ``Task.default_params``, ``Task.params_hook``).

These tests start the same task each way and check that the subject is shown
the same screen and the session runs the same params; that an unattended
simulation still never waits for a key; that a run-mode session names a task
that never said what its subject reads; that a declared params file that is
missing stops the session by name; that ``run.py``'s own arguments still take
precedence; and that a task written before any of this behaves exactly as it
did.
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
from alhazen.task.task import (
    declared_params_hook,
    declares_instructions,
    default_params_path,
    task_instructions,
)
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


# ----------------------------------------------------------------------
# The params a task runs with: its declared file, and its params hook
# ----------------------------------------------------------------------


def install(monkeypatch, *tasks) -> None:
    """Register tasks by name the way an installed package would — for tasks
    built inside a test, around a file only that test knows."""
    registered = {**INSTALLED, **{task.name: task for task in tasks}}
    monkeypatch.setattr(
        "alhazen.cli.tasks.installed_tasks",
        lambda: {
            name: SimpleNamespace(name=name, load=lambda task=task: task)
            for name, task in registered.items()
        },
    )


def params_file(path: Path, n_per_condition: int) -> Path:
    """A task params file whose design is recognisable by its trial count."""
    path.write_text(
        yaml.safe_dump(
            {"paradigm": {"kind": "sequence", "n_per_condition": n_per_condition}, "iti": {"ms": 0}}
        )
    )
    return path


def task_with_file(path: Path | str | None) -> type[SilentTask]:
    """A task that declares ``path`` as its params file."""

    class FileTask(SilentTask):
        name = "file-task"

        @classmethod
        def default_params(cls):
            return path

    return FileTask


def trials(data_root: Path) -> list[dict]:
    """The rows of the one run under ``data_root``."""
    import csv

    runs = sorted(data_root.glob("sub-*/ses-*/run-*"))
    assert len(runs) == 1, runs
    with next(runs[0].glob("*_trials.csv")).open() as handle:
        return list(csv.DictReader(handle))


def snapshot_source(data_root: Path) -> str:
    """Where the run's snapshot says its task params came from."""
    runs = sorted(data_root.glob("sub-*/ses-*/run-*"))
    assert len(runs) == 1, runs
    snapshot = yaml.safe_load((runs[0] / "config_snapshot.yaml").read_text(encoding="utf-8"))
    return snapshot["config"]["sources"]["task"]


def import_module_from(path: Path, monkeypatch):
    """Import a module written into tmp_path as a real module with a real
    file, the way an experiment package's task.py is imported."""
    import importlib.util
    import sys

    name = f"alz_hooks_{path.stem}_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


class TestWhereATaskSaysItsParamsAre:
    """``default_params_path``: what the task declares, resolved and checked."""

    def test_a_task_that_names_no_file_gets_none(self):
        assert default_params_path(SilentTask) is None
        assert SilentTask.default_params() is None

    def test_an_absolute_path_is_taken_as_it_is(self, tmp_path):
        path = params_file(tmp_path / "task.yaml", 3)

        assert default_params_path(task_with_file(path)) == path

    def test_a_relative_path_is_relative_to_the_file_the_method_is_written_in(
        self, tmp_path, monkeypatch
    ):
        """Not the working directory — `alhazen run` is started from
        anywhere — and not the file of whichever subclass inherits it: the
        file the path was written in."""
        package = tmp_path / "repo" / "src" / "pkg"
        package.mkdir(parents=True)
        (package / "configs").mkdir()
        params_file(package / "configs" / "task.yaml", 3)
        (package / "base_task.py").write_text(
            "from alhazen import Task\n\n\n"
            "class PackagedBase(Task):\n"
            "    @classmethod\n"
            "    def default_params(cls):\n"
            "        return 'configs/task.yaml'\n"
        )
        module = import_module_from(package / "base_task.py", monkeypatch)

        class Inheriting(module.PackagedBase):
            name = "inheriting-task"
            events = EVENTS
            outcomes = OUTCOMES
            params_model = Params

        # A working directory where the relative path would find nothing.
        monkeypatch.chdir(tmp_path)

        found = default_params_path(Inheriting)
        assert found == (package / "configs" / "task.yaml").resolve()

    def test_a_named_file_that_does_not_exist_is_refused_by_name(self, tmp_path):
        missing = tmp_path / "configs" / "task.yaml"

        with pytest.raises(ConfigError) as refused:
            default_params_path(task_with_file(missing))

        message = str(refused.value)
        assert str(missing) in message
        assert "FileTask.default_params()" in message
        assert "--params" in message

    def test_something_that_is_not_a_path_is_refused(self):
        with pytest.raises(TypeError, match=r"default_params\(\).*path"):
            default_params_path(task_with_file(42))  # type: ignore[arg-type]

    def test_a_relative_path_with_no_file_to_be_relative_to_is_refused(self):
        """A method typed at a prompt or built by exec has no file, so a
        relative path could only mean the working directory — which is the
        guess this refuses to make."""
        namespace: dict = {"SilentTask": SilentTask}
        exec(  # noqa: S102 - a class whose method has no source file, on purpose
            "class Typed(SilentTask):\n"
            "    name = 'typed-task'\n"
            "    @classmethod\n"
            "    def default_params(cls):\n"
            "        return 'configs/task.yaml'\n",
            namespace,
        )

        with pytest.raises(ConfigError, match="Return an absolute path"):
            default_params_path(namespace["Typed"])

    def test_an_entry_point_factory_names_no_file(self):
        def factory(params):
            return SilentTask(params)

        assert default_params_path(factory) is None
        assert declared_params_hook(factory) is None

    @pytest.mark.parametrize(
        ("hook", "declared", "said"),
        [
            ("default_params", lambda self: None, "an ordinary method"),
            ("params_hook", lambda self, params, args: params, "an ordinary method"),
            # Reads like the declarations beside it, and is not one.
            ("default_params", "configs/task.yaml", "the value 'configs/task.yaml'"),
        ],
    )
    def test_the_params_hooks_must_be_classmethods(self, hook, declared, said):
        """Both run before the task exists — they decide the params it is
        built with — so there is no instance to call them on. Written as
        plain methods they would fail with a missing-argument TypeError at
        the rig, and as values with "not callable"; refused when the class is
        written instead."""
        with pytest.raises(TypeError, match="@classmethod") as refused:
            type("Misdeclared", (SilentTask,), {"name": "misdeclared", hook: declared})

        assert f"declares {hook} as {said}" in str(refused.value)


class TestEveryEntryPointUsesTheTasksParamsFile:
    """`alhazen run` used to ignore the experiment's params file entirely:
    with no --params it ran the params model's defaults, which for one
    experiment was 432 trials of a 576-trial design, and said nothing."""

    def test_alhazen_run_without_params_runs_the_declared_file(self, tmp_path, monkeypatch, capsys):
        declared = params_file(tmp_path / "task.yaml", 3)
        install(monkeypatch, task_with_file(declared))
        from alhazen.cli.main import main

        code = main(
            ["run", "--task", "file-task", "--rig", str(rig_file(tmp_path))]
            + ["--sub", "s01", "--ses", "1"]
        )

        assert code == 0, capsys.readouterr().err
        # The file's design (3 trials), not the model's default (2).
        assert len(trials(tmp_path / "data")) == 3
        # The snapshot is the record, and it names the file that ran...
        assert snapshot_source(tmp_path / "data") == str(declared)
        # ...and so does the terminal, before trial one.
        assert f"params: {declared}" in capsys.readouterr().out

    def test_params_on_the_command_line_takes_precedence(self, tmp_path, monkeypatch, capsys):
        declared = params_file(tmp_path / "task.yaml", 3)
        given = params_file(tmp_path / "pilot.yaml", 1)
        install(monkeypatch, task_with_file(declared))
        from alhazen.cli.main import main

        code = main(
            ["run", "--task", "file-task", "--rig", str(rig_file(tmp_path))]
            + ["--params", str(given), "--sub", "s01", "--ses", "1"]
        )

        assert code == 0, capsys.readouterr().err
        assert len(trials(tmp_path / "data")) == 1
        assert snapshot_source(tmp_path / "data") == str(given)

    def test_a_run_py_with_no_params_file_of_its_own_runs_the_declared_file(self, tmp_path, capsys):
        from alhazen.cli.modes import run_experiment

        declared = params_file(tmp_path / "task.yaml", 3)
        code = run_experiment(
            task_class=task_with_file(declared),
            default_rig=rig_file(tmp_path),
            argv=["--sub", "s01", "--ses", "1"],
        )

        assert code == 0, capsys.readouterr().err
        assert len(trials(tmp_path / "data")) == 3

    def test_a_run_py_default_params_takes_precedence(self, tmp_path, capsys):
        from alhazen.cli.modes import run_experiment

        declared = params_file(tmp_path / "task.yaml", 3)
        run_py_default = params_file(tmp_path / "run-py.yaml", 1)
        code = run_experiment(
            task_class=task_with_file(declared),
            default_rig=rig_file(tmp_path),
            default_params=run_py_default,
            argv=["--sub", "s01", "--ses", "1"],
        )

        assert code == 0, capsys.readouterr().err
        assert len(trials(tmp_path / "data")) == 1
        assert snapshot_source(tmp_path / "data") == str(run_py_default)

    def test_the_modes_without_trials_use_it_too(self, tmp_path, monkeypatch):
        """Demo looks at the stimulus the params describe; the design's
        stimulus, not the model's defaults, is the one worth judging."""
        import sys

        from alhazen.cli.main import main

        declared = params_file(tmp_path / "task.yaml", 3)
        install(monkeypatch, task_with_file(declared))
        seen = {}

        def spy(args, rig, task, params):
            seen["params"] = params
            return 0

        monkeypatch.setattr(sys.modules["alhazen.cli.main"], "_demo_task", spy)
        code = main(
            ["run", "--task", "file-task", "--mode", "demo", "--rig", str(rig_file(tmp_path))]
        )

        assert code == 0
        assert seen["params"].paradigm.n_per_condition == 3

    def test_a_declared_file_that_is_missing_stops_the_session_naming_it(
        self, tmp_path, monkeypatch, capsys
    ):
        """Never the model's defaults in its place: those are not the
        experiment, and running them silently is the bug this replaces."""
        missing = tmp_path / "configs" / "task.yaml"
        install(monkeypatch, task_with_file(missing))
        from alhazen.cli.main import main

        code = main(
            ["run", "--task", "file-task", "--rig", str(rig_file(tmp_path))]
            + ["--sub", "s01", "--ses", "1"]
        )

        assert code == 1
        err = capsys.readouterr().err
        assert "INVALID" in err
        assert str(missing) in err
        assert "default_params()" in err
        assert not (tmp_path / "data").exists()

    def test_a_missing_declared_file_is_not_consulted_when_params_are_given(
        self, tmp_path, monkeypatch, capsys
    ):
        install(monkeypatch, task_with_file(tmp_path / "nowhere.yaml"))
        given = params_file(tmp_path / "pilot.yaml", 1)
        from alhazen.cli.main import main

        code = main(
            ["run", "--task", "file-task", "--rig", str(rig_file(tmp_path))]
            + ["--params", str(given), "--sub", "s01", "--ses", "1"]
        )

        assert code == 0, capsys.readouterr().err

    def test_a_task_that_names_no_file_runs_its_models_defaults_as_before_and_says_so(
        self, tmp_path, installed, capsys
    ):
        from alhazen.cli.main import main

        code = main(
            ["run", "--task", "silent-task", "--mode", "test", "--rig", str(rig_file(tmp_path))]
            + ["--sub", "s01", "--ses", "1"]
        )

        assert code == 0, capsys.readouterr().err
        assert snapshot_source(tmp_path / "data-rehearsal") == "<defaults>"
        assert (
            "params: the defaults of Params — no --params given, and SilentTask declares no "
            "default_params()"
        ) in capsys.readouterr().out


class StatefulParams(Model):
    paradigm: SchedulerConfig = SchedulerConfig(n_per_condition=1)
    iti: Duration = Duration(ms=0)
    state_dir: Path | None = None
    session: int | None = None


class StatefulTask(SilentTask):
    """A scheduler that carries state across sessions, the shape of an
    adaptive search: it cannot start without knowing whose state and which
    session, and nothing but the params hook can tell it."""

    name = "stateful-task"
    params_model = StatefulParams
    hook_calls: list[tuple[str | None, int | None]] = []

    @classmethod
    def params_hook(cls, params, args):
        from alhazen.config.loader import load_rig
        from alhazen.modes.rehearsal import rehearsal_root

        cls.hook_calls.append((args.sub, args.ses))
        root = Path(load_rig(args.rig).data_root)
        # A rehearsal's state follows its data to the rehearsal root, so a
        # simulated subject can never write into a real one's search.
        if not Mode(args.mode).writes_real_data:
            root = rehearsal_root(root)
        return params.model_copy(
            update={"state_dir": root / f"sub-{args.sub}" / "state", "session": args.ses}
        )

    def make_source(self, params, rng):
        if params.state_dir is None or params.session is None:
            raise ConfigError("stateful-task needs state_dir and session, from its params hook")
        params.state_dir.mkdir(parents=True, exist_ok=True)
        (params.state_dir / f"ses-{params.session}.txt").write_text("state carried over")
        return super().make_source(params, rng)


@pytest.fixture
def stateful(monkeypatch):
    StatefulTask.hook_calls = []
    install(monkeypatch, StatefulTask)
    return StatefulTask


class TestTheTasksParamsHook:
    """A task can say how its params follow from the invocation, so
    `alhazen run --task` can start a task that needs to know who and which
    session — which it used to refuse."""

    def spy_on_the_session(self, monkeypatch) -> dict:
        """Stop at the session, keeping the params the dispatch built it with."""
        import sys

        seen: dict = {}

        def spy(args, rig, task, params, mode):
            seen["params"] = params
            seen["task"] = task
            return 0

        monkeypatch.setattr(sys.modules["alhazen.cli.main"], "_trial_session", spy)
        return seen

    def test_alhazen_run_applies_it(self, tmp_path, stateful, monkeypatch):
        from alhazen.cli.main import main

        seen = self.spy_on_the_session(monkeypatch)
        code = main(
            ["run", "--task", "stateful-task", "--mode", "test", "--rig", str(rig_file(tmp_path))]
            + ["--sub", "m01", "--ses", "4"]
        )

        assert code == 0
        assert seen["params"].session == 4
        assert seen["params"].state_dir == tmp_path / "data-rehearsal" / "sub-m01" / "state"
        # The task the dispatch built carries them, not a second one from the file.
        assert seen["task"].params.session == 4

    def test_a_task_that_needs_it_runs_through_alhazen_run(self, tmp_path, stateful, capsys):
        """End to end, the way an adaptive search is rehearsed: simulate,
        headless, nobody named — the hook sees simulate's own subject and
        session, and the state lands beside the rehearsal's data."""
        from alhazen.cli.main import main

        code = main(
            ["run", "--task", "stateful-task", "--mode", "simulate", "--headless"]
            + ["--rig", str(rig_file(tmp_path))]
        )

        assert code == 0, capsys.readouterr().err
        assert stateful.hook_calls == [("sim", 1)]
        assert (tmp_path / "data-rehearsal" / "sub-sim" / "state" / "ses-1.txt").exists()
        assert not (tmp_path / "data").exists()

    def test_the_hook_sees_a_prompted_subject_and_session(self, tmp_path, stateful, monkeypatch):
        """Settled before the hook runs. It used to run first, and a subject
        typed at the prompt reached it as None — a search state filed under
        `sub-None`, silently."""
        from alhazen.cli.main import main

        answers = iter(["m02", "7"])
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
        monkeypatch.setattr("sys.stdin", type("Tty", (), {"isatty": lambda self: True})())
        seen = self.spy_on_the_session(monkeypatch)

        code = main(
            ["run", "--task", "stateful-task", "--mode", "test", "--rig", str(rig_file(tmp_path))]
        )

        assert code == 0
        assert stateful.hook_calls == [("m02", 7)]
        assert seen["params"].state_dir == tmp_path / "data-rehearsal" / "sub-m02" / "state"

    def test_a_subject_that_cannot_be_asked_for_stops_the_session_before_the_hook(
        self, tmp_path, stateful, capsys
    ):
        """No terminal to prompt at (pytest's stdin is not one): refused as a
        usage error, and the hook — which would have filed state under a
        subject nobody named — never runs."""
        from alhazen.cli.main import main

        code = main(
            ["run", "--task", "stateful-task", "--mode", "test", "--rig", str(rig_file(tmp_path))]
        )

        assert code == 2
        assert "stdin is not a terminal" in capsys.readouterr().err
        assert stateful.hook_calls == []

    def test_demo_passes_the_subject_as_given(self, tmp_path, stateful, monkeypatch):
        """Demo and movie have nobody in the chair, so nothing is settled or
        prompted for: the hook sees the flags exactly as typed."""
        import sys

        from alhazen.cli.main import main

        monkeypatch.setattr(sys.modules["alhazen.cli.main"], "_demo_task", lambda *a: 0)
        code = main(
            ["run", "--task", "stateful-task", "--mode", "demo", "--rig", str(rig_file(tmp_path))]
        )

        assert code == 0
        assert stateful.hook_calls == [(None, None)]

    def test_a_run_py_hook_replaces_the_tasks(self, tmp_path, stateful, monkeypatch):
        """Replaces, not chains: a run.py written before the task could say
        this keeps doing exactly what it did."""
        from alhazen.cli.modes import run_experiment

        seen = self.spy_on_the_session(monkeypatch)

        def run_py_hook(params, args):
            return params.model_copy(update={"session": 99})

        code = run_experiment(
            task_class=StatefulTask,
            default_rig=rig_file(tmp_path),
            params_hook=run_py_hook,
            argv=["--mode", "test", "--sub", "m01", "--ses", "4"],
        )

        assert code == 0
        assert stateful.hook_calls == []
        assert seen["params"].session == 99
        assert seen["params"].state_dir is None

    def test_a_run_py_with_no_hook_of_its_own_gets_the_tasks(self, tmp_path, stateful, monkeypatch):
        from alhazen.cli.modes import run_experiment

        seen = self.spy_on_the_session(monkeypatch)
        code = run_experiment(
            task_class=StatefulTask,
            default_rig=rig_file(tmp_path),
            argv=["--mode", "test", "--sub", "m01", "--ses", "4"],
        )

        assert code == 0
        assert stateful.hook_calls == [("m01", 4)]
        assert seen["params"].session == 4

    def test_what_it_returns_is_checked_against_the_params_model(
        self, tmp_path, monkeypatch, capsys
    ):
        class Careless(StatefulTask):
            name = "careless-task"

            @classmethod
            def params_hook(cls, params, args):
                return {"sesion": args.ses}  # a typo the model rejects

        install(monkeypatch, Careless)
        from alhazen.cli.main import main

        code = main(
            ["run", "--task", "careless-task", "--mode", "test", "--rig", str(rig_file(tmp_path))]
            + ["--sub", "m01", "--ses", "4"]
        )

        assert code == 1
        err = capsys.readouterr().err
        assert "INVALID" in err
        assert "Careless.params_hook()" in err
        assert "sesion" in err

    def test_a_hook_that_raises_is_not_swallowed(self, tmp_path, monkeypatch):
        class Failing(StatefulTask):
            name = "failing-task"

            @classmethod
            def params_hook(cls, params, args):
                raise RuntimeError("the rig file has no data_root this hook can use")

        install(monkeypatch, Failing)
        from alhazen.cli.main import main

        with pytest.raises(RuntimeError, match="data_root"):
            main(
                ["run", "--task", "failing-task", "--mode", "test"]
                + ["--rig", str(rig_file(tmp_path)), "--sub", "m01", "--ses", "4"]
            )

    def test_a_task_that_declares_no_hook_is_built_from_its_params_unchanged(
        self, tmp_path, installed, monkeypatch
    ):
        from alhazen.cli.main import main

        seen = self.spy_on_the_session(monkeypatch)
        code = main(
            ["run", "--task", "silent-task", "--mode", "test", "--rig", str(rig_file(tmp_path))]
            + ["--sub", "s01", "--ses", "1"]
        )

        assert code == 0
        assert seen["params"] == Params()


class TestTheDashboardAddressIsPrinted:
    """The runner logs the live dashboard's address at INFO, which reaches
    only the run's session.log: the console never said where the page was.
    With --no-dashboard-browser nothing opened it either, and the experiment
    workspace, which reads a launched run's console to embed the page, could
    never find it. The CLI now prints a `dashboard:` line before trial one."""

    class FakeController:
        """What the builder constructs and the runner talks to, minus the
        child process: the address is the only thing this test is about."""

        def __init__(self, port=0, auto_open=True):
            self.url = "http://127.0.0.1:4242/?token=abc-123"

        def start(self):
            return self.url

        def stop(self):
            pass

        def publish(self, state):
            pass

        def publish_camera(self, pixels, t):
            pass

        def poll_settings(self):
            return []

        def poll_commands(self):
            return []

        def save(self, figures_dir, state):
            pass

    def run_with(self, tmp_path, monkeypatch, *, dashboard: bool) -> str:
        declared = params_file(tmp_path / "task.yaml", 1)
        install(monkeypatch, task_with_file(declared))
        rig = rig_file(tmp_path)
        if dashboard:
            config = yaml.safe_load(rig.read_text(encoding="utf-8"))
            config["dashboard"] = {"enabled": True, "auto_open": False}
            rig.write_text(yaml.safe_dump(config), encoding="utf-8")
        monkeypatch.setattr("alhazen.session.builder.DashboardController", self.FakeController)
        from alhazen.cli.main import main

        code = main(
            ["run", "--task", "file-task", "--rig", str(rig)]
            + ["--sub", "s01", "--ses", "1", "--no-dashboard-browser"]
        )
        assert code == 0
        return code

    def test_a_session_with_a_dashboard_prints_its_address_before_trial_one(
        self, tmp_path, monkeypatch, capsys
    ):
        self.run_with(tmp_path, monkeypatch, dashboard=True)

        out = capsys.readouterr().out
        assert "dashboard: http://127.0.0.1:4242/?token=abc-123" in out
        # After the params line and before the session ran: the experimenter
        # reads it with everything else they need before trial one.
        assert out.index("params: ") < out.index("dashboard: ") < out.index("session complete")

    def test_a_session_without_a_dashboard_says_nothing_about_one(
        self, tmp_path, monkeypatch, capsys
    ):
        self.run_with(tmp_path, monkeypatch, dashboard=False)

        assert "dashboard:" not in capsys.readouterr().out
