"""`run_experiment(tasks=...)`: one run.py, several tasks, `--task` chooses.

An experiment that ships several tasks used to read `--task` out of argv
itself before handing the rest to run_experiment — three repositories had
three copies of that surgery, and the experiment workspace could not tell
which tasks there were. Now the table is declared once and alhazen owns the
flag: its choices are the table's keys, the chosen task's params file stands
in for `--params`, and a run.py that says two contradictory things about
which task to run is refused at import, not mid-session.
"""

from __future__ import annotations

import sys

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
        _, seen = run(tmp_path, monkeypatch, [], tasks=TASKS)
        assert seen["task_class"] is TaskA and seen["args"].params == "configs/a.yaml"

    def test_default_task_names_another_default(self, tmp_path, monkeypatch):
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
        with pytest.raises(
            ValueError,
            match="default_task 'task-c' is not one of the declared tasks: task-a, task-b",
        ):
            run_experiment(
                tasks=TASKS, default_task="task-c", default_rig=rig_file(tmp_path), argv=[]
            )
