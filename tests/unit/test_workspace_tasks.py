"""The workspace and an experiment that ships several tasks.

run.py declares them once — `TASKS = {...}` and `run_experiment(tasks=TASKS)` —
and the workspace reads that table from the file: it lists the tasks with the
project, reads each one's parameter schema, sends the chosen one as `--task`
right after the mode, keeps `--task` out of the extra arguments, and records
the task on the run. A table it cannot read is an error it reports and a
launch it refuses, never a project shown as having one task.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# test_workspace's `workspace` fixture (a registered stub project and a
# Workspace over it) serves these tests too; pytest finds it through this
# module's namespace, and __all__ says the import is deliberate.
from test_workspace import request_for, workspace

from alhazen.cli.workspace import project_tasks

__all__ = ["request_for", "workspace"]

MULTI = (
    "import json, sys\n"
    "def run_experiment(**kwargs): pass\n"
    "class A: pass\n"
    "class B: pass\n"
    'TASKS = {"mt-tuning": (A, "configs/task-tuning.yaml"), '
    '"mib-search": (B, "configs/task-search.yaml")}\n'
    "print(json.dumps(sys.argv[1:]), flush=True)\n"
    "if False:\n"
    '    run_experiment(tasks=TASKS, default_task="mib-search")\n'
)


def write_run_py(root: Path, text: str) -> None:
    (root / "run.py").write_text(text, encoding="utf-8")


class TestReadingTheTable:
    def test_one_task_is_no_table(self, tmp_path):
        write_run_py(tmp_path, "run_experiment(task_class=MyTask)\n")
        assert project_tasks(tmp_path) == {"tasks": [], "default": None, "error": None}

    def test_no_run_py_is_no_table(self, tmp_path):
        assert project_tasks(tmp_path) == {"tasks": [], "default": None, "error": None}

    def test_the_table_lists_names_params_and_the_default(self, tmp_path):
        write_run_py(tmp_path, MULTI)
        assert project_tasks(tmp_path) == {
            "tasks": [
                {"name": "mt-tuning", "params": "configs/task-tuning.yaml"},
                {"name": "mib-search", "params": "configs/task-search.yaml"},
            ],
            "default": "mib-search",
            "error": None,
        }

    def test_without_default_task_the_first_declared_is_the_default(self, tmp_path):
        write_run_py(tmp_path, MULTI.replace(', default_task="mib-search"', ""))
        assert project_tasks(tmp_path)["default"] == "mt-tuning"

    def test_a_default_named_through_a_module_level_name(self, tmp_path):
        write_run_py(
            tmp_path,
            MULTI.replace('default_task="mib-search"', "default_task=DEFAULT").replace(
                "TASKS = {", 'DEFAULT = "mt-tuning"\nTASKS = {'
            ),
        )
        assert project_tasks(tmp_path)["default"] == "mt-tuning"

    def test_a_params_file_given_as_an_expression_is_unknown_not_guessed(self, tmp_path):
        write_run_py(
            tmp_path,
            MULTI.replace('"configs/task-tuning.yaml"', 'HERE / "configs/task-tuning.yaml"'),
        )
        assert project_tasks(tmp_path)["tasks"][0] == {"name": "mt-tuning", "params": None}

    @pytest.mark.parametrize(
        "binding",
        [
            "HERE = Path(__file__).parent",
            "HERE = Path(__file__).resolve().parent",
            "HERE: Path = Path(__file__).parent",
        ],
    )
    def test_run_pys_own_folder_form_is_read(self, tmp_path, binding):
        """`HERE / "configs" / "x.yaml"` is how amodal-averaging's run.py
        names its files: relative to run.py, so the command works from any
        directory. The workspace reads it as the project-relative path."""
        text = MULTI.replace('"configs/task-tuning.yaml"', 'HERE / "configs" / "task-tuning.yaml"')
        write_run_py(tmp_path, f"from pathlib import Path\n{binding}\n{text}")
        tasks = project_tasks(tmp_path)["tasks"]
        assert tasks[0]["params"] == "configs/task-tuning.yaml"
        # The string entry beside it is read as before.
        assert tasks[1]["params"] == "configs/task-search.yaml"

    @pytest.mark.parametrize(
        "binding, entry",
        [
            # A folder that is not run.py's: not the project, not guessed.
            ('HERE = Path("/data")', 'HERE / "configs" / "x.yaml"'),
            ("HERE = Path.home()", 'HERE / "configs" / "x.yaml"'),
            # A non-string operand in the chain.
            ("HERE = Path(__file__).parent", 'HERE / "configs" / NAME'),
            # A bare name with no path after it.
            ("HERE = Path(__file__).parent", "HERE"),
            # A call rather than a chain.
            ("HERE = Path(__file__).parent", 'HERE.joinpath("x.yaml")'),
        ],
    )
    def test_other_expressions_are_unknown_not_guessed(self, tmp_path, binding, entry):
        text = MULTI.replace('"configs/task-tuning.yaml"', entry)
        write_run_py(tmp_path, f"from pathlib import Path\n{binding}\n{text}")
        result = project_tasks(tmp_path)
        assert result["error"] is None
        assert result["tasks"][0] == {"name": "mt-tuning", "params": None}

    def test_a_windows_path_in_the_table_is_recorded_in_posix_form(self, tmp_path):
        write_run_py(
            tmp_path, MULTI.replace("configs/task-tuning.yaml", "configs\\\\task-tuning.yaml")
        )
        assert project_tasks(tmp_path)["tasks"][0]["params"] == "configs/task-tuning.yaml"

    @pytest.mark.parametrize(
        "text, message",
        [
            # The table written inline, not as a name the file binds.
            ('run_experiment(tasks={"a": (A, "x.yaml")})\n', "module-level dict literal"),
            # A name bound to something the file alone cannot read.
            ("TASKS = build()\nrun_experiment(tasks=TASKS)\n", "module-level dict literal"),
            # A name never bound at the top level.
            ("run_experiment(tasks=TASKS)\n", "module-level dict literal"),
            # A key that is not a string.
            (
                'TASKS = {1: (A, "x.yaml")}\nrun_experiment(tasks=TASKS)\n',
                "module-level dict literal",
            ),
            ("TASKS = {}\nrun_experiment(tasks=TASKS)\n", "table is empty"),
            (
                'TASKS = {"a": (A, "x.yaml")}\nrun_experiment(tasks=TASKS, default_task="b")\n',
                "default_task 'b' is not one of its tasks: a",
            ),
            (
                'TASKS = {"a": (A, "x.yaml")}\nrun_experiment(tasks=TASKS, default_task=pick())\n',
                "default_task= must be a string literal",
            ),
            ("def broken(:\n", "cannot be read for its tasks"),
        ],
    )
    def test_a_table_the_file_cannot_show_is_an_error_with_the_shape(self, tmp_path, text, message):
        write_run_py(tmp_path, text)
        result = project_tasks(tmp_path)
        assert result["tasks"] == [] and result["default"] is None
        assert message in result["error"]


class TestTheProjectAndItsLaunches:
    def multi(self, workspace):
        root = Path(workspace.projects[0]["path"])
        write_run_py(root, MULTI)
        return workspace.projects[0]["id"]

    def test_describe_lists_the_tasks(self, workspace):
        key = self.multi(workspace)
        described = workspace.describe(key)
        assert [task["name"] for task in described["tasks"]] == ["mt-tuning", "mib-search"]
        assert described["default_task"] == "mib-search" and described["tasks_error"] is None

    def test_a_single_task_project_describes_none(self, workspace):
        described = workspace.describe(workspace.projects[0]["id"])
        assert (described["tasks"], described["default_task"], described["tasks_error"]) == (
            [],
            None,
            None,
        )

    def test_the_task_rides_right_after_the_mode(self, workspace):
        key = self.multi(workspace)
        request = request_for(workspace, project=key, mode="simulate", task="mt-tuning")
        command = workspace._command(request, workspace.directory / "job")
        start = command.index("--mode")
        assert command[start : start + 4] == ["--mode", "simulate", "--task", "mt-tuning"]

    def test_no_task_asked_for_runs_the_default(self, workspace):
        key = self.multi(workspace)
        command = workspace._command(
            request_for(workspace, project=key, mode="simulate"), workspace.directory / "job"
        )
        assert command[command.index("--task") + 1] == "mib-search"

    def test_an_undeclared_task_is_refused_with_the_choices(self, workspace):
        key = self.multi(workspace)
        with pytest.raises(
            ValueError, match="declares no task 'nope'; choose one of mt-tuning, mib-search"
        ):
            workspace._command(
                request_for(workspace, project=key, mode="simulate", task="nope"),
                workspace.directory / "job",
            )

    def test_task_in_the_extras_is_refused_for_a_project_with_a_menu(self, workspace):
        key = self.multi(workspace)
        request = request_for(
            workspace, project=key, mode="simulate", extra_args="--task mt-tuning"
        )
        with pytest.raises(ValueError, match="--task is set from the dashboard controls"):
            workspace._command(request, workspace.directory / "job")

    def test_task_in_the_extras_still_passes_through_for_a_project_without_one(self, workspace):
        # An experiment that reads its own --task from argv (no tasks= table)
        # keeps getting it from the field, as before.
        request = request_for(workspace, mode="simulate", extra_args="--task mib-detect")
        command = workspace._command(request, workspace.directory / "job")
        assert command[-2:] == ["--task", "mib-detect"] and "--task" not in command[:-2]

    def test_a_task_for_a_single_task_project_is_refused(self, workspace):
        request = request_for(workspace, mode="simulate", task="mt-tuning")
        with pytest.raises(ValueError, match="declares one task, takes no task"):
            workspace._command(request, workspace.directory / "job")

    def test_a_table_that_cannot_be_read_refuses_the_launch_with_the_same_words(self, workspace):
        root = Path(workspace.projects[0]["path"])
        write_run_py(root, 'run_experiment(tasks={"a": (A, "x.yaml")})\n')
        described = workspace.describe(workspace.projects[0]["id"])
        assert "module-level dict literal" in described["tasks_error"]
        with pytest.raises(ValueError, match="module-level dict literal"):
            workspace._command(request_for(workspace, mode="simulate"), workspace.directory / "job")

    def test_a_launched_run_records_its_task_and_the_child_receives_it(self, workspace):
        key = self.multi(workspace)
        run = workspace.start(
            request_for(workspace, project=key, mode="simulate", task="mt-tuning")
        )
        workspace.worker.join(timeout=60)
        detail = workspace.detail(run["id"])
        assert detail["task"] == "mt-tuning" and detail["status"] == "completed"
        argv = json.loads(
            Path(detail["directory"], "console.log").read_text(encoding="utf-8").splitlines()[0]
        )
        assert argv[argv.index("--task") + 1] == "mt-tuning"
        assert (
            json.loads(Path(detail["directory"], "run.json").read_text(encoding="utf-8"))["task"]
            == "mt-tuning"
        )

    def test_a_single_task_run_records_no_task(self, workspace):
        run = workspace.start(request_for(workspace, mode="simulate"))
        workspace.worker.join(timeout=60)
        assert workspace.detail(run["id"])["task"] is None


class TestTheSchemaPerTask:
    def test_the_read_names_the_task_and_is_cached_per_task(self, workspace, monkeypatch):
        key = self.multi_id(workspace)
        spawned: list[list[str]] = []

        def fake_run(command, **kwargs):
            spawned.append(command)
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps({"properties": {"n": len(spawned)}}), stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert workspace.schema(key, "mt-tuning")["properties"]["n"] == 1
        assert workspace.schema(key, "mib-search")["properties"]["n"] == 2
        assert workspace.schema(key, "mt-tuning")["properties"]["n"] == 1
        assert workspace.schema(key)["properties"]["n"] == 3
        # The child is told which task; asked for none, it is told nothing
        # and answers with run.py's default.
        assert spawned[0][-1] == "mt-tuning" and spawned[1][-1] == "mib-search"
        assert spawned[2][-1].endswith("run.py")

    def multi_id(self, workspace):
        root = Path(workspace.projects[0]["path"])
        write_run_py(root, MULTI)
        return workspace.projects[0]["id"]

    def test_the_schema_of_each_declared_task_in_the_projects_interpreter(self, workspace):
        """A real child process, as the page's dropdowns get it: a run.py with
        two tasks answers with the named one's parameter schema."""
        root = Path(workspace.projects[0]["path"])
        write_run_py(
            root,
            "from pydantic import BaseModel\n"
            "class ParamsA(BaseModel):\n    n: int = 1\n"
            "class ParamsB(BaseModel):\n    m: int = 2\n"
            "class A:\n    params_model = ParamsA\n"
            "class B:\n    params_model = ParamsB\n"
            'TASKS = {"a": (A, "configs/a.yaml"), "b": (B, "configs/b.yaml")}\n'
            'if __name__ == "__main__":\n'
            '    raise RuntimeError("must not launch")\n'
            '    run_experiment(tasks=TASKS, default_task="b")\n',
        )
        key = workspace.projects[0]["id"]
        assert set(workspace.schema(key, "a")["properties"]) == {"n"}
        assert set(workspace.schema(key)["properties"]) == {"m"}, "no name: the table's default"
        with pytest.raises(ValueError, match="declares no task 'c'; it declares a, b"):
            workspace.schema(key, "c")

    def test_a_task_name_for_a_single_task_project_is_refused(self, workspace):
        root = Path(workspace.projects[0]["path"])
        write_run_py(
            root,
            "from pydantic import BaseModel\n"
            "class Params(BaseModel):\n    n: int = 1\n"
            "class One:\n    params_model = Params\n"
            'if __name__ == "__main__":\n'
            "    run_experiment(task_class=One)\n",
        )
        key = workspace.projects[0]["id"]
        assert set(workspace.schema(key)["properties"]) == {"n"}
        with pytest.raises(ValueError, match="declares one task"):
            workspace.schema(key, "other")
