"""`run_experiment`'s `--task`: every session names its task.

An experiment that ships several tasks used to read `--task` out of argv
itself before handing the rest to run_experiment — three repositories had
three copies of that surgery, and the experiment workspace could not tell
which tasks there were. Now the table is declared once and alhazen owns the
flag: its choices are the table's keys, the chosen task's params file stands
in for `--params`, and a run.py that says two contradictory things about
which task to run is refused at import, not mid-session.

Since 2.5 every session names its task (the owner's rule, 2026-10-02), with
one task (`task_class=`) as with several: `--task` is on the parser in both
forms. A command without it is deprecated, not refused — refusing it is a
MAJOR change (docs/versioning.md §1, §4) — so it still runs what it ran and
says so in a FutureWarning naming the task; `default_task=` is deprecated
with it. Measure mode runs no task and needs none named.
"""

from __future__ import annotations

import sys
import warnings

import pytest
from test_cli_modes import rig_file

from alhazen import EventSchema, Model, Task, outcomes
from alhazen.cli.modes import run_experiment


class ParamsA(Model):
    n: int = 1


class ParamsB(Model):
    m: int = 2


class TaskA(Task):
    name = "task-a"
    events = EventSchema(())
    outcomes = outcomes(DONE=dict(completed=True, success=True))
    params_model = ParamsA


class TaskB(Task):
    name = "task-b"
    events = EventSchema(())
    outcomes = outcomes(DONE=dict(completed=True, success=True))
    params_model = ParamsB


TASKS = {"task-a": (TaskA, "configs/a.yaml"), "task-b": (TaskB, "configs/b.yaml")}


def run(tmp_path, monkeypatch, argv, **kwargs):
    """run_experiment up to the session dispatch, which is replaced by a spy
    recording the parsed arguments and the task class it was handed."""
    seen: dict = {}

    def spy(args, *, task_class, params_hook):
        seen.update(args=args, task_class=task_class)
        return 0

    monkeypatch.setattr(sys.modules["alhazen.cli.main"], "_run_session", spy)
    code = run_experiment(default_rig=rig_file(tmp_path), argv=["--mode", "test", *argv], **kwargs)
    return code, seen


class TestChoosingATask:
    def test_task_picks_the_class_and_its_params_file(self, tmp_path, monkeypatch):
        code, seen = run(tmp_path, monkeypatch, ["--task", "task-b"], tasks=TASKS)
        assert code == 0
        assert seen["task_class"] is TaskB
        assert seen["args"].params == "configs/b.yaml"

    def test_without_task_the_first_declared_runs(self, tmp_path, monkeypatch):
        # Changed in 2.5: this still runs the first declared task, as it
        # always did, but a command naming no task is now deprecated and says
        # so (TestEverySessionNamesItsTask has the message itself).
        with pytest.warns(FutureWarning, match="without --task"):
            _, seen = run(tmp_path, monkeypatch, [], tasks=TASKS)
        assert seen["task_class"] is TaskA and seen["args"].params == "configs/a.yaml"

    def test_default_task_names_another_default(self, tmp_path, monkeypatch):
        # Changed in 2.5: default_task= still chooses until 3.0, and warns
        # that it is going; the command, naming no task, warns as well.
        with (
            pytest.warns(DeprecationWarning, match="'default_task' argument is deprecated"),
            pytest.warns(FutureWarning, match="runs task-b"),
        ):
            _, seen = run(tmp_path, monkeypatch, [], tasks=TASKS, default_task="task-b")
        assert seen["task_class"] is TaskB

    def test_an_explicit_params_file_still_wins(self, tmp_path, monkeypatch):
        _, seen = run(
            tmp_path, monkeypatch, ["--task", "task-b", "--params", "mine.yaml"], tasks=TASKS
        )
        assert seen["args"].params == "mine.yaml"

    def test_a_task_without_a_params_file_keeps_the_tasks_own_defaults(self, tmp_path, monkeypatch):
        _, seen = run(tmp_path, monkeypatch, ["--task", "task-a"], tasks={"task-a": (TaskA, None)})
        assert seen["args"].params is None

    def test_an_unknown_task_is_refused_with_the_real_names(self, tmp_path, monkeypatch, capsys):
        # argparse's own refusal: exit 2, the choices listed, nothing loaded.
        with pytest.raises(SystemExit) as exit_info:
            run(tmp_path, monkeypatch, ["--task", "task-c"], tasks=TASKS)
        assert exit_info.value.code == 2
        err = capsys.readouterr().err
        assert "task-a" in err and "task-b" in err

    def test_help_names_the_tasks(self, tmp_path, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            run_experiment(default_rig=rig_file(tmp_path), argv=["--help"], tasks=TASKS)
        out = capsys.readouterr().out
        assert "--task" in out and "task-a" in out and "task-b" in out


class TestARunPyThatContradictsItself:
    """Refused before argv is looked at: each of these has two answers to
    "which task", and the one run.py did not mean would run silently."""

    def test_neither_task_class_nor_tasks(self, tmp_path):
        with pytest.raises(TypeError, match="task_class= .* or tasks="):
            run_experiment(default_rig=rig_file(tmp_path), argv=[])

    def test_both_task_class_and_tasks(self, tmp_path):
        with pytest.raises(TypeError, match="not both"):
            run_experiment(task_class=TaskA, tasks=TASKS, default_rig=rig_file(tmp_path), argv=[])

    def test_default_params_goes_with_one_task_only(self, tmp_path):
        with pytest.raises(TypeError, match="each task names its own params file"):
            run_experiment(
                tasks=TASKS,
                default_params="configs/task.yaml",
                default_rig=rig_file(tmp_path),
                argv=[],
            )

    def test_an_empty_table(self, tmp_path):
        with pytest.raises(ValueError, match="declares no task"):
            run_experiment(tasks={}, default_rig=rig_file(tmp_path), argv=[])

    def test_a_default_that_is_not_declared(self, tmp_path):
        # Changed in 2.5: default_task= is deprecated, so passing it warns
        # before the check refuses it; the refusal is unchanged.
        with (
            pytest.warns(DeprecationWarning, match="'default_task'"),
            pytest.raises(
                ValueError,
                match="default_task 'task-c' is not one of the declared tasks: task-a, task-b",
            ),
        ):
            run_experiment(
                tasks=TASKS, default_task="task-c", default_rig=rig_file(tmp_path), argv=[]
            )


class OneTask(Task):
    """The one task of a run.py that declares it with task_class=."""

    name = "the-one"
    events = EventSchema(())
    outcomes = outcomes(DONE=dict(completed=True, success=True))
    params_model = ParamsA


def unnamed_warnings(record) -> list[str]:
    """The FutureWarnings a command without --task raised, as text."""
    return [str(w.message) for w in record if issubclass(w.category, FutureWarning)]


class TestEverySessionNamesItsTask:
    """Since 2.5, a command naming no task is deprecated: it runs the task it
    always ran and says which, in a warning whoever typed it will see. 3.0
    refuses it."""

    def test_the_warning_names_the_task_it_runs_and_that_3_0_refuses(self, tmp_path, monkeypatch):
        with pytest.warns(FutureWarning) as record:
            _, seen = run(tmp_path, monkeypatch, [], tasks=TASKS)
        assert unnamed_warnings(record) == [
            "running run.py without --task is deprecated since alhazen 2.5 and will be "
            "removed in 3.0; use --task task-a instead. This session runs task-a, the first "
            "task run.py declares; alhazen 3.0 will refuse a command that names no task"
        ]
        # The task it always ran, and the namespace says so, as it did when
        # --task defaulted to it (a params hook reading args.task sees it).
        assert seen["task_class"] is TaskA and seen["args"].task == "task-a"

    def test_with_a_default_task_it_names_that_one_and_why(self, tmp_path, monkeypatch):
        with pytest.warns(DeprecationWarning), pytest.warns(FutureWarning) as record:
            _, seen = run(tmp_path, monkeypatch, [], tasks=TASKS, default_task="task-b")
        (message,) = unnamed_warnings(record)
        assert "use --task task-b instead" in message
        assert "This session runs task-b, run.py's default_task;" in message
        assert seen["task_class"] is TaskB and seen["args"].params == "configs/b.yaml"

    def test_the_warning_points_at_run_pys_own_line(self, tmp_path, monkeypatch):
        """At the call to run_experiment, not inside alhazen: the warning
        machinery prints that line, and it is run.py the reader recognises
        (here the `run` helper in this file stands in for run.py)."""
        with pytest.warns(FutureWarning) as record:
            run(tmp_path, monkeypatch, [], tasks=TASKS)
        (warning,) = [w for w in record if issubclass(w.category, FutureWarning)]
        assert warning.filename == __file__

    def test_it_is_a_warning_python_shows_by_default(self, tmp_path, monkeypatch):
        """Under Python's default filters a DeprecationWarning raised outside
        __main__ is dropped, which is how a deprecation goes unseen until the
        release that removes it. This one is shown: with those filters in
        place, it still arrives."""
        with warnings.catch_warnings(record=True) as record:
            warnings.resetwarnings()
            warnings.simplefilter("default")
            # Python's own default for code outside __main__.
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            run(tmp_path, monkeypatch, [], tasks=TASKS)
        assert len(unnamed_warnings(record)) == 1

    @pytest.mark.parametrize(
        "kwargs", [{"tasks": TASKS}, {"task_class": OneTask}], ids=["tasks", "task_class"]
    )
    def test_naming_the_task_warns_nothing(self, tmp_path, monkeypatch, kwargs):
        task = "task-b" if "tasks" in kwargs else "the-one"
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            code, seen = run(tmp_path, monkeypatch, ["--task", task], **kwargs)
        assert code == 0 and seen["args"].task == task

    @pytest.mark.parametrize(
        "kwargs", [{"tasks": TASKS}, {"task_class": OneTask}], ids=["tasks", "task_class"]
    )
    def test_measure_mode_needs_no_task(self, tmp_path, monkeypatch, kwargs):
        """Measure checks the machine and runs no task, as `alhazen run
        --mode measure` needs none. The task it is handed only locates the
        experiment's folder for a rig name."""
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            code, seen = run(tmp_path, monkeypatch, ["--mode", "measure"], **kwargs)
        assert code == 0
        assert seen["task_class"] is (TaskA if "tasks" in kwargs else OneTask)

    @pytest.mark.parametrize("mode", ["run", "test", "simulate", "demo", "movie"])
    def test_every_other_mode_warns_without_one(self, tmp_path, monkeypatch, mode):
        with pytest.warns(FutureWarning, match="without --task"):
            run(tmp_path, monkeypatch, ["--mode", mode], tasks=TASKS)

    def test_help_and_description_promise_no_default(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            run_experiment(default_rig=rig_file(tmp_path), argv=["--help"], tasks=TASKS)
        out = " ".join(capsys.readouterr().out.split())
        # It used to say "(default: task-a)" and "without it, task-a."
        assert "(default: task-a)" not in out and "without it, task-a" not in out
        assert "--task names the one to run, and is required" in out
        assert "required in every mode but measure" in out
        assert "until alhazen 3.0 a command without it runs task-a, with a warning" in out


class TestDefaultTaskIsDeprecated:
    def test_it_warns_even_when_the_command_names_a_task(self, tmp_path, monkeypatch):
        """run.py's own line is what must change, so it warns on every run,
        pointing at that line, whatever the command said."""
        with pytest.warns(DeprecationWarning) as record:
            _, seen = run(
                tmp_path, monkeypatch, ["--task", "task-a"], tasks=TASKS, default_task="task-b"
            )
        (warning,) = [w for w in record if issubclass(w.category, DeprecationWarning)]
        assert str(warning.message) == (
            "the 'default_task' argument is deprecated since alhazen 2.5 and will be removed "
            "in 3.0; use --task on every command line instead"
        )
        assert warning.filename == __file__
        # The command's task still wins over it.
        assert seen["task_class"] is TaskA

    def test_it_warns_beside_task_class_too(self, tmp_path, monkeypatch):
        with pytest.warns(DeprecationWarning, match="'default_task'"):
            run(
                tmp_path,
                monkeypatch,
                ["--task", "the-one"],
                task_class=OneTask,
                default_task="the-one",
            )


class TestOneTaskTakesTaskToo:
    """`task_class=`: --task joins the parser with the one task's name as its
    only choice, so a run.py with one task is named like one with several."""

    def test_its_name_runs_it(self, tmp_path, monkeypatch):
        code, seen = run(tmp_path, monkeypatch, ["--task", "the-one"], task_class=OneTask)
        assert code == 0 and seen["task_class"] is OneTask

    def test_without_it_the_task_runs_and_the_warning_says_so(self, tmp_path, monkeypatch):
        with pytest.warns(FutureWarning) as record:
            _, seen = run(tmp_path, monkeypatch, [], task_class=OneTask)
        assert unnamed_warnings(record) == [
            "running run.py without --task is deprecated since alhazen 2.5 and will be "
            "removed in 3.0; use --task the-one instead. This session runs the-one, run.py's "
            "one task; alhazen 3.0 will refuse a command that names no task"
        ]
        assert seen["task_class"] is OneTask and seen["args"].task == "the-one"

    def test_another_name_is_refused_with_the_one(self, tmp_path, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exit_info:
            run(tmp_path, monkeypatch, ["--task", "task-a"], task_class=OneTask)
        assert exit_info.value.code == 2
        err = capsys.readouterr().err
        assert "invalid choice" in err and "task-a" in err and "the-one" in err

    def test_help_offers_the_one_name_and_says_it_is_required(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            run_experiment(task_class=OneTask, default_rig=rig_file(tmp_path), argv=["--help"])
        out = " ".join(capsys.readouterr().out.split())
        assert "--task {the-one}" in out
        assert "the task to run, the-one — required in every mode but measure" in out
        # The description is still the task's own docstring.
        assert "The one task of a run.py that declares it with task_class=." in out
