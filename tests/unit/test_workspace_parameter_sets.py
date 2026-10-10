"""The Task parameters menu: run.py's PARAMETERS, or the entries derived
when it names none (`project_parameter_sets`).

Each entry is a label, the task it runs and its parameter file, so choosing
an entry chooses the task: the page has no separate Task menu (the owner's
request, 2026-10-06). The labels are display names only; a run keeps its
task's own name, and records the label beside it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from test_workspace import request_for, workspace

from alhazen.cli.workspace import project_parameter_sets, project_tasks

__all__ = ["request_for", "workspace"]

HEADER = (
    "from pathlib import Path\n"
    "def run_experiment(**kwargs): pass\n"
    "class A: pass\n"
    "class B: pass\n"
    "HERE = Path(__file__).parent\n"
    'TASKS = {"main": (A, HERE / "configs" / "task.yaml"), '
    '"neon": (B, "configs/task-neon.yaml")}\n'
)
CALL = "if False:\n    run_experiment(tasks=TASKS)\n"
CONFIGS = [
    "configs/task-light.yaml",
    "configs/task-neon.yaml",
    "configs/task-pilot.yaml",
    "configs/task.yaml",
]


def menu(root: Path, body: str, configs: list[str] = CONFIGS) -> dict:
    (root / "run.py").write_text(HEADER + body + CALL, encoding="utf-8")
    return project_parameter_sets(root, configs, project_tasks(root))


class TestDeclared:
    def test_each_entry_names_its_label_task_and_file_in_either_path_form(self, tmp_path):
        result = menu(
            tmp_path,
            "PARAMETERS = {\n"
            '    "Main": ("main", HERE / "configs" / "task.yaml"),\n'
            '    "Main (less trials)": ("main", "configs/task-light.yaml"),\n'
            '    "Neon": ("neon", HERE / "configs" / "task-neon.yaml"),\n'
            '    "Neon defaults": ("neon", None),\n'
            "}\n",
        )
        assert result["error"] is None
        assert result["sets"] == [
            {"label": "Main", "task": "main", "params": "configs/task.yaml"},
            {"label": "Main (less trials)", "task": "main", "params": "configs/task-light.yaml"},
            {"label": "Neon", "task": "neon", "params": "configs/task-neon.yaml"},
            {"label": "Neon defaults", "task": "neon", "params": None},
        ]
        # The default task's own file: TASKS' first entry, main, on task.yaml.
        assert result["default"] == "Main"

    def test_a_one_task_run_py_writes_the_path_alone(self, tmp_path):
        (tmp_path / "run.py").write_text(
            'PARAMETERS = {"Session": "configs/task.yaml", "Short": "configs/task-pilot.yaml"}\n'
            "run_experiment(task_class=A)\n",
            encoding="utf-8",
        )
        result = project_parameter_sets(tmp_path, CONFIGS, project_tasks(tmp_path))
        assert result["error"] is None
        assert [(s["label"], s["task"], s["params"]) for s in result["sets"]] == [
            ("Session", None, "configs/task.yaml"),
            ("Short", None, "configs/task-pilot.yaml"),
        ]
        assert result["default"] == "Session"

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ("PARAMETERS = build()\n", "module-level dict literal"),
            ("PARAMETERS = {}\n", "module-level dict literal"),
            ('PARAMETERS = {"": ("main", "configs/task.yaml")}\n', "module-level dict literal"),
            ('PARAMETERS = {"Main": "configs/task.yaml"}\n', "must be (task, params file)"),
            ('PARAMETERS = {"Main": ("other", "configs/task.yaml")}\n', "not one of TASKS"),
            ('PARAMETERS = {"Main": ("main", "configs/gone.yaml")}\n', "configs/gone.yaml"),
            ('PARAMETERS = {"Main": ("main", pick())}\n', "cannot be read without running"),
            (
                'PARAMETERS = {"Main": ("main", "configs/task.yaml"), '
                '"Main": ("neon", "configs/task-neon.yaml")}\n',
                "names 'Main' twice",
            ),
        ],
    )
    def test_a_table_that_cannot_be_read_is_said_and_the_derived_menu_offered(
        self, tmp_path, body, message
    ):
        result = menu(tmp_path, body)
        assert message in result["error"]
        # Still launchable: every task on its own file, every other file once
        # per task, labelled so that nothing is paired by a guess.
        labels = [s["label"] for s in result["sets"]]
        assert labels[:2] == ["main", "neon"]
        assert "main · light" in labels and "neon · light" in labels

    def test_no_table_read_when_the_task_table_itself_cannot_be_read(self, tmp_path):
        (tmp_path / "run.py").write_text(
            'TASKS = build()\nPARAMETERS = {"Main": ("main", "configs/task.yaml")}\n'
            "run_experiment(tasks=TASKS)\n",
            encoding="utf-8",
        )
        result = project_parameter_sets(tmp_path, CONFIGS, project_tasks(tmp_path))
        # The task table's error is project_tasks'; PARAMETERS is not judged.
        assert result["error"] is None


class TestDerived:
    def test_tasks_on_their_own_files_then_every_other_file_once_per_task(self, tmp_path):
        result = menu(tmp_path, "")
        assert result["error"] is None
        assert result["sets"] == [
            {"label": "main", "task": "main", "params": "configs/task.yaml"},
            {"label": "neon", "task": "neon", "params": "configs/task-neon.yaml"},
            {"label": "main · light", "task": "main", "params": "configs/task-light.yaml"},
            {"label": "neon · light", "task": "neon", "params": "configs/task-light.yaml"},
            {"label": "main · pilot", "task": "main", "params": "configs/task-pilot.yaml"},
            {"label": "neon · pilot", "task": "neon", "params": "configs/task-pilot.yaml"},
        ]
        assert result["default"] == "main"

    def test_a_task_naming_no_file_or_a_missing_one_runs_on_no_file(self, tmp_path):
        (tmp_path / "run.py").write_text(
            'TASKS = {"check": (A, None), "report": (B, "configs/gone.yaml")}\n'
            "run_experiment(tasks=TASKS)\n",
            encoding="utf-8",
        )
        result = project_parameter_sets(tmp_path, [], project_tasks(tmp_path))
        assert result["sets"] == [
            {"label": "check", "task": "check", "params": None},
            {"label": "report", "task": "report", "params": None, "missing": "configs/gone.yaml"},
        ]
        assert result["default"] == "check"

    def test_without_a_table_each_file_by_its_short_name_and_task_yaml_first(self, tmp_path):
        configs = [
            "configs/presets/task-x.yaml",
            "configs/params-fast.yml",
            "configs/task-pilot.yaml",
            "configs/task.yaml",
        ]
        result = project_parameter_sets(tmp_path, configs, project_tasks(tmp_path))
        assert [(s["label"], s["params"]) for s in result["sets"]] == [
            ("presets/x", "configs/presets/task-x.yaml"),
            ("fast", "configs/params-fast.yml"),
            ("pilot", "configs/task-pilot.yaml"),
            ("task", "configs/task.yaml"),
        ]
        assert result["default"] == "task"

    def test_two_files_that_would_read_the_same_show_their_paths(self, tmp_path):
        configs = ["configs/params-pilot.yaml", "configs/task-pilot.yaml", "configs/task.yaml"]
        result = project_parameter_sets(tmp_path, configs, project_tasks(tmp_path))
        assert [s["label"] for s in result["sets"]] == [
            "configs/params-pilot.yaml",
            "configs/task-pilot.yaml",
            "task",
        ]

    def test_no_files_no_entries(self, tmp_path):
        result = project_parameter_sets(tmp_path, [], project_tasks(tmp_path))
        assert result == {"sets": [], "default": None, "error": None}


TWO_TASKS = (
    "import json, sys\n"
    "def run_experiment(**kwargs): pass\n"
    "class A: pass\n"
    "class B: pass\n"
    'TASKS = {"mt-tuning": (A, "configs/task.yaml"), "mib-search": (B, None)}\n'
    'PARAMETERS = {"Tuning": ("mt-tuning", "configs/task.yaml"), '
    '"Search": ("mib-search", None)}\n'
    "print(json.dumps(sys.argv[1:]), flush=True)\n"
    "if False:\n"
    "    run_experiment(tasks=TASKS)\n"
)


class TestTheProjectAndItsLaunches:
    def tasked(self, workspace):
        root = Path(workspace.projects[0]["path"])
        (root / "run.py").write_text(TWO_TASKS, encoding="utf-8")
        return workspace.projects[0]["id"]

    def test_describe_lists_the_entries_and_the_default(self, workspace):
        described = workspace.describe(self.tasked(workspace))
        assert [s["label"] for s in described["parameter_sets"]] == ["Tuning", "Search"]
        assert described["default_parameter_set"] == "Tuning"
        assert described["parameter_sets_error"] is None

    def test_a_run_records_its_entry_and_keeps_its_task_name(self, workspace):
        key = self.tasked(workspace)
        # params: "default" since the import round (decision 5): a label alone
        # no longer launches; this runs the entry's own file as shipped.
        run = workspace.start(
            request_for(
                workspace,
                project=key,
                mode="simulate",
                task="mt-tuning",
                parameter_set="Tuning",
                params="default",
            )
        )
        workspace.worker.join(timeout=60)
        detail = workspace.detail(run["id"])
        assert (detail["task"], detail["parameter_set"]) == ("mt-tuning", "Tuning")
        record = json.loads(Path(detail["directory"], "run.json").read_text(encoding="utf-8"))
        assert record["parameter_set"] == "Tuning"
        argv = json.loads(
            Path(detail["directory"], "console.log").read_text(encoding="utf-8").splitlines()[0]
        )
        assert argv[argv.index("--task") + 1] == "mt-tuning"

    def test_a_label_without_its_file_contents_is_refused_naming_it(self, workspace):
        """POST /api/runs with a label and no text used to run the task's
        default params file while run.json recorded the label (import round
        2026-10-09: attention-clamp's 'Calibration (human)' ran the cued block,
        mbri's 'Search (RDK, human)' the monkey file). Refused now, naming
        the label, before anything is written."""
        key = self.tasked(workspace)
        with pytest.raises(ValueError, match="'Tuning' were sent without the contents of"):
            workspace.start(
                request_for(
                    workspace, project=key, mode="simulate", task="mt-tuning", parameter_set="Tuning"
                )
            )
        assert not list((workspace.directory / "runs").glob("*/run.json"))
        with pytest.raises(ValueError, match='params: "default" runs the parameters as shipped'):
            workspace.start(
                request_for(
                    workspace,
                    project=key,
                    mode="simulate",
                    task="mt-tuning",
                    parameter_set="Tuning",
                    params="default",
                    parameters_yaml="speed: 4\n",
                )
            )
        assert not list((workspace.directory / "runs").glob("*/run.json"))

    def test_default_runs_the_entry_file_and_records_its_hash(self, workspace):
        key = self.tasked(workspace)
        root = Path(workspace.projects[0]["path"])
        shipped = (root / "configs" / "task.yaml").read_bytes()
        run = workspace.start(
            request_for(
                workspace,
                project=key,
                mode="simulate",
                task="mt-tuning",
                parameter_set="Tuning",
                params="default",
            )
        )
        workspace.worker.join(timeout=60)
        detail = workspace.detail(run["id"])
        folder = Path(detail["directory"])
        record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        sent = (folder / "params.yaml").read_bytes()
        assert record["params"] == {
            "source": "parameter set file",
            "file": "configs/task.yaml",
            "sha256": hashlib.sha256(sent).hexdigest(),
            "file_sha256": hashlib.sha256(shipped).hexdigest(),
        }
        assert yaml.safe_load(sent) == yaml.safe_load(shipped)
        argv = json.loads((folder / "console.log").read_text(encoding="utf-8").splitlines()[0])
        assert argv[argv.index("--params") + 1] == str(folder / "params.yaml")

    def test_text_and_an_entry_on_no_file_are_recorded_too(self, workspace):
        key = self.tasked(workspace)
        run = workspace.start(
            request_for(
                workspace,
                project=key,
                mode="simulate",
                task="mt-tuning",
                parameter_set="Tuning",
                parameters_yaml="speed: 4\n",
            )
        )
        workspace.worker.join(timeout=60)
        folder = Path(workspace.detail(run["id"])["directory"])
        record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        assert record["params"]["source"] == "launch text"
        assert record["params"]["sha256"] == hashlib.sha256((folder / "params.yaml").read_bytes()).hexdigest()
        # "Search" names no file: its label means the task's own default.
        run = workspace.start(
            request_for(
                workspace, project=key, mode="simulate", task="mib-search", parameter_set="Search"
            )
        )
        workspace.worker.join(timeout=60)
        folder = Path(workspace.detail(run["id"])["directory"])
        record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        assert record["params"] == {"source": "task default", "file": None, "sha256": None}

    def test_an_entry_for_another_task_is_refused_before_anything_is_written(self, workspace):
        key = self.tasked(workspace)
        with pytest.raises(ValueError, match="run the task mib-search, but the launch names"):
            workspace.start(
                request_for(
                    workspace,
                    project=key,
                    mode="simulate",
                    task="mt-tuning",
                    parameter_set="Search",
                )
            )
        assert not list((workspace.directory / "runs").glob("*/run.json"))

    def test_an_unknown_entry_is_refused_with_the_choices(self, workspace):
        key = self.tasked(workspace)
        with pytest.raises(ValueError, match="no Task parameters entry 'Main'; choose one of"):
            workspace.start(
                request_for(
                    workspace, project=key, mode="simulate", task="mt-tuning", parameter_set="Main"
                )
            )

    def test_a_client_that_sends_no_entry_still_launches(self, workspace):
        key = self.tasked(workspace)
        run = workspace.start(
            request_for(workspace, project=key, mode="simulate", task="mt-tuning")
        )
        workspace.worker.join(timeout=60)
        assert workspace.detail(run["id"])["parameter_set"] is None
