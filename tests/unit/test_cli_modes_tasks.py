"""`run_experiment`'s `--task`: every session names its task.

An experiment that ships several tasks used to read `--task` out of argv
itself before handing the rest to run_experiment — three repositories had
three copies of that surgery, and the experiment workspace could not tell
which tasks there were. Now the table is declared once and alhazen owns the
flag: its choices are the table's keys, the chosen task's params file stands
in for `--params`, and a run.py that says two contradictory things about
which task to run is refused at import, not mid-session.

Every session names its task (the owner's rule, 2026-10-02), with one task
(`task_class=`) as with several: `--task` is on the parser in both forms.
From 2.5 a command without it was deprecated — it ran the first task with a
FutureWarning — and `default_task=` with it; 3.0 removed both
(docs/versioning.md §4): the command is a usage error naming the --task to
pass, and `default_task=` is a TypeError saying what to do. Measure mode
runs no task and needs none named; training's stage names its own.
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

    def test_without_task_nothing_runs(self, tmp_path, monkeypatch, capsys):
        # Changed in 3.0: this ran the first declared task (with a
        # FutureWarning from 2.5); now it is a usage error and nothing is
        # dispatched (TestEverySessionNamesItsTask has the message itself).
        with pytest.raises(SystemExit) as exit_info:
            _, seen = run(tmp_path, monkeypatch, [], tasks=TASKS)
        assert exit_info.value.code == 2
        assert "--task" in capsys.readouterr().err

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

    def test_a_default_task_of_any_kind_is_refused(self, tmp_path):
        # Changed in 3.0: default_task= was removed, so even one naming no
        # declared task is refused for being passed at all, before argv.
        with pytest.raises(TypeError, match="no longer takes default_task="):
            run_experiment(
                tasks=TASKS,
                default_task="task-c",  # type: ignore[arg-type]
                default_rig=rig_file(tmp_path),
                argv=[],
            )


class OneTask(Task):
    """The one task of a run.py that declares it with task_class=."""

    name = "the-one"
    events = EventSchema(())
    outcomes = outcomes(DONE=dict(completed=True, success=True))
    params_model = ParamsA


def refusal(capsys) -> str:
    """The usage error a command without --task got, on one line."""
    return " ".join(capsys.readouterr().err.split())


class TestEverySessionNamesItsTask:
    """A command naming no task is refused as a usage error, before anything
    is loaded, saying which --task to pass. From 2.5 to 2.x it ran the task
    it always ran with a FutureWarning; 3.0 removed that."""

    def test_the_refusal_lists_the_tasks_to_choose_from(self, tmp_path, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exit_info:
            run(tmp_path, monkeypatch, [], tasks=TASKS)
        # argparse's own status for a usage error, as for a misspelt task.
        assert exit_info.value.code == 2
        assert (
            "run.py (task-a | task-b): error: every session names its task; pass --task with "
            "one of task-a, task-b (alhazen 3.0 no longer runs a default task when --task is "
            "left out)"
        ) in refusal(capsys)

    def test_nothing_is_dispatched(self, tmp_path, monkeypatch):
        """Refused before the session is built: no first task runs in the
        place of the one the operator forgot to name."""
        seen: dict = {}

        def spy(args, *, task_class, params_hook):
            seen.update(args=args)
            return 0

        monkeypatch.setattr(sys.modules["alhazen.cli.main"], "_run_session", spy)
        with pytest.raises(SystemExit):
            run_experiment(default_rig=rig_file(tmp_path), argv=["--mode", "test"], tasks=TASKS)
        assert seen == {}

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

    @pytest.mark.parametrize(
        "kwargs", [{"tasks": TASKS}, {"task_class": OneTask}], ids=["tasks", "task_class"]
    )
    def test_training_mode_is_not_refused_here(self, tmp_path, monkeypatch, kwargs):
        """Training runs the stage --stage names, which names its own task;
        a training command with no --stage is the dispatch's to refuse
        (_settle_training_stage), with the message about --stage."""
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            code, seen = run(tmp_path, monkeypatch, ["--mode", "training"], **kwargs)
        assert code == 0
        assert seen["args"].task == ("task-a" if "tasks" in kwargs else "the-one")

    @pytest.mark.parametrize("mode", ["run", "test", "simulate", "demo", "movie"])
    def test_every_other_mode_refuses_without_one(self, tmp_path, monkeypatch, capsys, mode):
        with pytest.raises(SystemExit) as exit_info:
            run(tmp_path, monkeypatch, ["--mode", mode], tasks=TASKS)
        assert exit_info.value.code == 2
        assert "every session names its task" in refusal(capsys)

    def test_help_and_description_promise_no_default(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            run_experiment(default_rig=rig_file(tmp_path), argv=["--help"], tasks=TASKS)
        out = " ".join(capsys.readouterr().out.split())
        # It used to say "(default: task-a)" and "without it, task-a.", and
        # from 2.5 "until alhazen 3.0 a command without it runs task-a".
        assert "(default: task-a)" not in out and "without it, task-a" not in out
        assert "until alhazen 3.0" not in out
        assert "--task names the one to run, and is required" in out
        assert "required in every mode but measure and training" in out


class TestDefaultTaskIsRemoved:
    """Deprecated in 2.5, removed in 3.0: refused whenever it is passed,
    with what to do instead, since run.py's own line is what must change."""

    @pytest.mark.parametrize(
        ("kwargs", "argv"),
        [({"tasks": TASKS}, ["--task", "task-a"]), ({"task_class": OneTask}, ["--task", "the-one"])],
        ids=["tasks", "task_class"],
    )
    def test_it_is_refused_whatever_the_command_says(self, tmp_path, monkeypatch, kwargs, argv):
        with pytest.raises(TypeError) as error:
            run(tmp_path, monkeypatch, argv, default_task="task-b", **kwargs)
        assert str(error.value) == (
            "run_experiment() no longer takes default_task= (deprecated since alhazen 2.5, "
            "removed in 3.0): every command names its task with --task, so there is no "
            "default to choose. Delete default_task= from run.py and add --task <name> to "
            "every command that started that task without it"
        )

    def test_another_unknown_keyword_is_pythons_usual_error(self, tmp_path):
        with pytest.raises(TypeError, match="unexpected keyword argument 'default_tasks'"):
            run_experiment(
                tasks=TASKS,
                default_rig=rig_file(tmp_path),
                argv=["--task", "task-a"],
                default_tasks="task-a",  # type: ignore[arg-type]
            )


class TestOneTaskTakesTaskToo:
    """`task_class=`: --task joins the parser with the one task's name as its
    only choice, so a run.py with one task is named like one with several."""

    def test_its_name_runs_it(self, tmp_path, monkeypatch):
        code, seen = run(tmp_path, monkeypatch, ["--task", "the-one"], task_class=OneTask)
        assert code == 0 and seen["task_class"] is OneTask

    def test_without_it_the_refusal_names_the_one_task(self, tmp_path, monkeypatch, capsys):
        # Changed in 3.0: this ran the one task with a FutureWarning.
        with pytest.raises(SystemExit) as exit_info:
            run(tmp_path, monkeypatch, [], task_class=OneTask)
        assert exit_info.value.code == 2
        assert (
            "run.py (the-one): error: every session names its task; pass --task the-one "
            "(alhazen 3.0 no longer runs a default task when --task is left out)"
        ) in refusal(capsys)

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
        assert "the task to run, the-one — required in every mode but measure and training" in out
        # The description is still the task's own docstring.
        assert "The one task of a run.py that declares it with task_class=." in out
