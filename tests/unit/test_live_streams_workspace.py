"""The experiment workspace's Live monitor switches: how a launch starts the
live monitor's gaze traces and eye camera, sent to an alhazen that takes them
and remembered per rig in the workspace's own state.

Design: cli/workspace.py (LiveStreams, Workspace._live_streams).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import yaml
from test_workspace import finish, request_for, workspace  # noqa: F401 - the fixture

from alhazen.cli.main import add_mode_arguments
from alhazen.cli.workspace import LIVE_STREAMS_FILE

OFF = {"gaze_traces": False, "camera": False}


def _parse(command: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_mode_arguments(parser)
    return parser.parse_args(command[3:])


def _takes_them(workspace, monkeypatch) -> dict:  # noqa: F811
    project = workspace.project(workspace.projects[0]["id"])
    monkeypatch.setitem(project, "capabilities", ["live-streams"])
    return project


class TestTheCommand:
    @pytest.mark.parametrize("mode", ["run", "test", "simulate", "measure"])
    def test_a_mode_with_a_live_monitor_gets_both_flags(self, workspace, monkeypatch, mode):  # noqa: F811
        _takes_them(workspace, monkeypatch)
        request = request_for(
            workspace,
            mode=mode,
            subject="s01",
            initials="HD",
            parameters=None if mode == "measure" else {"speed": 2},
            live_streams={"gaze_traces": False, "camera": True},
        )
        args = _parse(workspace._command(request, workspace.directory / "job"))
        assert (args.gaze_traces, args.eye_camera) == (False, True)

    @pytest.mark.parametrize("mode", ["demo", "movie"])
    def test_a_mode_without_one_gets_none(self, workspace, monkeypatch, mode):  # noqa: F811
        _takes_them(workspace, monkeypatch)
        request = request_for(workspace, mode=mode, parameters={"speed": 2}, live_streams=OFF)
        command = workspace._command(request, workspace.directory / "job")
        assert not any("gaze-traces" in token or "eye-camera" in token for token in command)

    def test_an_older_alhazen_is_sent_nothing_and_refused_a_stream_off(self, workspace):  # noqa: F811
        on = request_for(
            workspace,
            mode="test",
            subject="s01",
            initials="HD",
            parameters={"speed": 2},
            live_streams={"gaze_traces": True, "camera": True},
        )
        command = workspace._command(on, workspace.directory / "job")
        assert not any("gaze-traces" in token for token in command)
        off = on.model_copy(update={"live_streams": on.live_streams.model_copy(update=OFF)})
        with pytest.raises(ValueError, match="cannot start the live monitor with its gaze"):
            workspace._command(off, workspace.directory / "job")

    def test_the_flags_cannot_be_typed_behind_the_switches(self, workspace, monkeypatch):  # noqa: F811
        _takes_them(workspace, monkeypatch)
        request = request_for(
            workspace,
            mode="test",
            subject="s01",
            initials="HD",
            parameters={"speed": 2},
            extra_args="--no-eye-camera",
        )
        with pytest.raises(ValueError, match="--no-eye-camera is set from the dashboard"):
            workspace._command(request, workspace.directory / "job")


class TestTheRunPageStartsFromTheRigThenTheLastChoice:
    def test_the_rig_setting_then_what_was_launched(self, workspace, monkeypatch):  # noqa: F811
        project = _takes_them(workspace, monkeypatch)
        rig_file = Path(workspace.project(project["id"])["path"]) / "configs/rig-sim.yaml"
        values = yaml.safe_load(rig_file.read_text(encoding="utf-8"))
        values.setdefault("live_monitor", {})["camera"] = False
        rig_file.write_text(yaml.safe_dump(values), encoding="utf-8")

        first = workspace.rig(project["id"], "configs/rig-sim.yaml")["live_streams"]
        assert first == {"gaze_traces": True, "camera": False, "remembered": False}

        run = workspace.start(
            request_for(
                workspace,
                mode="test",
                subject="s01",
                initials="HD",
                parameters={"speed": 2},
                live_streams={"gaze_traces": False, "camera": True},
            )
        )
        finish(workspace, run)
        again = workspace.rig(project["id"], "configs/rig-sim.yaml")["live_streams"]
        assert again == {"gaze_traces": False, "camera": True, "remembered": True}
        # In the workspace's own state, never in the experiment's files.
        stored = json.loads((workspace.directory / LIVE_STREAMS_FILE).read_text())
        assert stored == {
            f"{project['id']}|configs/rig-sim.yaml": {"gaze_traces": False, "camera": True}
        }
        # Another rig keeps its own starting position.
        other = workspace.rig(project["id"], "configs/rigs/rig-lab.yaml")["live_streams"]
        assert other["remembered"] is False

    def test_an_unreadable_memory_falls_back_to_the_rig(self, workspace, caplog):  # noqa: F811
        (workspace.directory / LIVE_STREAMS_FILE).write_text("{not json")
        key = workspace.projects[0]["id"]
        streams = workspace.rig(key, "configs/rig-sim.yaml")["live_streams"]
        assert streams == {"gaze_traces": True, "camera": True, "remembered": False}
        assert "cannot read" in caplog.text

    def test_the_project_says_whether_its_alhazen_takes_them(self, workspace, monkeypatch):  # noqa: F811
        key = workspace.projects[0]["id"]
        assert workspace.describe(key)["live_streams"] is False
        _takes_them(workspace, monkeypatch)
        assert workspace.describe(key)["live_streams"] is True
