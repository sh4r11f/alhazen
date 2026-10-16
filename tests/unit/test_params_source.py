"""The params file as the operator chose it (``--params-source``,
``alhazen.task.params_source``).

The experiment workspace runs a copy of the file chosen in its Task
parameters menu (its run folder's params.yaml), so a task that named
``args.params`` in a refusal named that copy, which the operator never chose
and cannot find. The workspace now passes the chosen file beside the copy,
and a task asks for it with ``params_source(args)``; from a shell nothing
passes it and the helper is ``args.params`` as given. An experiment that
still runs on an alhazen without the helper keeps its old message.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest
from test_workspace import request_for, workspace

from test_task_hooks import SilentTask, install, params_file, rig_file

import alhazen.task
from alhazen.cli.capabilities import CAPABILITIES
from alhazen.cli.main import _params_line, add_mode_arguments
from alhazen.errors import ConfigError
from alhazen.task import params_source

__all__ = ["request_for", "workspace"]


def parsed(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_mode_arguments(parser)
    return parser.parse_args(list(argv))


class TestTheHelper:
    def test_from_a_shell_it_is_the_params_file_as_given(self):
        assert params_source(parsed("--params", "configs/task-light.yaml")) == (
            "configs/task-light.yaml"
        )

    def test_the_chosen_file_wins_over_the_copy_the_session_loads(self):
        args = parsed(
            "--params", "/state/runs/abc/params.yaml",
            "--params-source", 'configs/task-dark.yaml (Task parameters "Dark")',
        )
        assert args.params == "/state/runs/abc/params.yaml"
        assert params_source(args) == 'configs/task-dark.yaml (Task parameters "Dark")'

    def test_no_file_is_none(self):
        assert params_source(parsed()) is None

    def test_a_namespace_from_before_the_flag_still_answers(self):
        """A hook handed a namespace an older alhazen (or other code) built,
        without `params_source` or even `params`."""
        assert params_source(argparse.Namespace(params="a.yaml")) == "a.yaml"
        assert params_source(argparse.Namespace()) is None
        assert params_source(argparse.Namespace(params=Path("b.yaml"), params_source="")) == (
            "b.yaml"
        )

    def test_it_is_public_and_the_capability_says_the_flag_is_taken(self):
        assert "params_source" in alhazen.task.__all__
        assert "params-source" in CAPABILITIES


class TestTheParamsLine:
    """The line printed before trial one names the chosen file beside the copy."""

    def test_a_copy_names_what_was_chosen(self):
        args = parsed("--params", "/s/runs/r/params.yaml", "--params-source", "configs/t.yaml")
        assert _params_line(args, object(), object()) == (
            "params: /s/runs/r/params.yaml (chosen as configs/t.yaml)"
        )

    def test_a_shell_launch_reads_as_before(self):
        args = parsed("--params", "configs/t.yaml")
        assert _params_line(args, object(), object()) == "params: configs/t.yaml"


def with_capability(workspace, monkeypatch, capabilities=("params-source",)):
    project = workspace.project(workspace.projects[0]["id"])
    monkeypatch.setitem(project, "capabilities", list(capabilities))
    return project


def flag(command: list[str]) -> str | None:
    return command[command.index("--params-source") + 1] if "--params-source" in command else None


class TestTheWorkspaceSendsIt:
    """The fixture project's menu is derived (no PARAMETERS): one entry,
    "task", on configs/task.yaml, which holds ``speed: 3`` and ``duration``."""

    FILE = {"speed": 3, "duration": {"ms": 100}}

    def launch(self, workspace, **overrides):
        request = request_for(
            workspace, mode="test", subject="s01", initials="HD", parameter_set="task",
            **overrides,
        )
        return workspace._command(request, workspace.directory / "job")

    def test_the_file_as_chosen_with_its_menu_entry(self, workspace, monkeypatch):
        with_capability(workspace, monkeypatch)
        command = self.launch(workspace, parameters=dict(self.FILE))
        # The session still loads the run folder's copy; the source rides beside it.
        assert command[command.index("--params") + 1].endswith("params.yaml")
        assert flag(command) == 'configs/task.yaml (Task parameters "task")'

    def test_the_text_editor_unchanged_is_the_file_too(self, workspace, monkeypatch):
        with_capability(workspace, monkeypatch)
        text = "speed: 3\nduration: {ms: 100}\n"
        assert flag(self.launch(workspace, parameters_yaml=text)) == (
            'configs/task.yaml (Task parameters "task")'
        )

    def test_values_changed_on_the_run_page_say_so(self, workspace, monkeypatch):
        with_capability(workspace, monkeypatch)
        command = self.launch(workspace, parameters={**self.FILE, "speed": 2})
        assert flag(command) == 'configs/task.yaml as edited on the Run page (Task parameters "task")'

    def test_an_unreadable_file_names_what_was_chosen_without_a_verdict(
        self, workspace, monkeypatch
    ):
        project = with_capability(workspace, monkeypatch)
        (Path(project["path"]) / "configs/task.yaml").write_text("speed: [\n", encoding="utf-8")
        assert flag(self.launch(workspace, parameters={"speed": 2})) == (
            'configs/task.yaml (Task parameters "task")'
        )

    def test_an_entry_without_a_file_is_named_by_its_label(self, workspace, monkeypatch):
        with_capability(workspace, monkeypatch)
        monkeypatch.setattr(
            workspace,
            "describe",
            lambda key: {"parameter_sets": [{"label": "Defaults", "task": None, "params": None}]},
        )
        request = request_for(
            workspace, mode="test", subject="s01", initials="HD",
            parameter_set="Defaults", parameters={"speed": 2},
        )
        assert workspace._params_source(workspace.project(request.project), request) == (
            "the Run page's values (Task parameters \"Defaults\")"
        )

    @pytest.mark.parametrize(
        "capabilities, overrides",
        [
            # An alhazen that does not take the flag (or a record from before
            # the probe listed capabilities) is never sent it: it would refuse
            # an unknown argument.
            ((), {"parameters": {"speed": 2}}),
            (None, {"parameters": {"speed": 2}}),
            # Nothing chosen from the menu, or no parameters at all.
            (("params-source",), {"parameters": {"speed": 2}, "parameter_set": None}),
            (("params-source",), {"parameter_set": None}),
        ],
    )
    def test_nothing_is_sent_when_there_is_nothing_to_name(
        self, workspace, monkeypatch, capabilities, overrides
    ):
        project = workspace.project(workspace.projects[0]["id"])
        monkeypatch.setitem(project, "capabilities", capabilities)
        request = request_for(
            workspace, mode="test", subject="s01", initials="HD",
            **{"parameter_set": "task", **overrides},
        )
        command = workspace._command(request, workspace.directory / "job")
        assert "--params-source" not in command

    def test_a_started_session_receives_it(self, workspace, monkeypatch):
        """The fixture's run.py prints its argv: the child is told both."""
        with_capability(workspace, monkeypatch)
        request = request_for(
            workspace, mode="simulate", subject="s01", parameter_set="task",
            parameters=dict(self.FILE),
        )
        run = workspace.start(request)
        workspace.worker.join(timeout=30)
        argv = json.loads(
            (Path(run["directory"]) / "console.log").read_text(encoding="utf-8").splitlines()[0]
        )
        assert argv[argv.index("--params-source") + 1] == 'configs/task.yaml (Task parameters "task")'
        assert argv[argv.index("--params") + 1] == str(Path(run["directory"]) / "params.yaml")


class RefusingTask(SilentTask):
    """A task whose params hook refuses every file, naming it the way a
    task should: as the operator chose it."""

    name = "refusing-task"

    @classmethod
    def params_hook(cls, params, args):
        raise ConfigError(f"{params_source(args)} cannot run here")


class TestATaskNamesTheChosenFile:
    """End to end through ``alhazen run``'s own dispatch: the hook's refusal
    names the chosen file when the workspace passed one, and the file as
    given otherwise."""

    def refusal(self, tmp_path, monkeypatch, capsys, *extra: str) -> str:
        install(monkeypatch, RefusingTask)
        copy = params_file(tmp_path / "params.yaml", 1)
        from alhazen.cli.main import main

        code = main(
            ["run", "--task", "refusing-task", "--mode", "simulate", "--headless"]
            + ["--rig", str(rig_file(tmp_path)), "--params", str(copy), *extra]
        )
        assert code == 1
        return capsys.readouterr().err

    def test_launched_by_the_workspace(self, tmp_path, monkeypatch, capsys):
        err = self.refusal(
            tmp_path, monkeypatch, capsys,
            "--params-source", 'configs/task-dark.yaml (Task parameters "Dark")',
        )
        assert 'INVALID: configs/task-dark.yaml (Task parameters "Dark") cannot run here' in err
        assert "params.yaml" not in err

    def test_launched_from_a_shell(self, tmp_path, monkeypatch, capsys):
        err = self.refusal(tmp_path, monkeypatch, capsys)
        assert f"INVALID: {tmp_path / 'params.yaml'} cannot run here" in err
