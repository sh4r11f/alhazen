"""Launcher tests use real child processes and HTTP, without a renderer or rig."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest
import yaml

from alhazen.cli.dashboard import DashboardServer, workspace_lock
from alhazen.cli.workspace import Launch, Workspace, inside, mapping, script_actions

RIG = Path(__file__).parents[2] / "examples/minimal_fixation/rig-sim.yaml"


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "experiment with spaces"
    (root / "configs").mkdir(parents=True)
    (root / "configs/rig-sim.yaml").write_bytes(RIG.read_bytes())
    (root / "configs/task.yaml").write_text("speed: 3\nduration: {ms: 100}\n")
    (root / "run.py").write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "print(json.dumps(sys.argv[1:]), flush=True)\n"
        "if '--out' in sys.argv:\n"
        "    out = Path(sys.argv[sys.argv.index('--out') + 1])\n"
        "    (out / 'clip.mp4').write_bytes(b'0123456789')\n"
    )
    space = Workspace(tmp_path / "state")
    space.add(str(root), sys.executable)
    yield space
    space.close()


def request_for(workspace, **overrides):
    return Launch(
        **{
            "project": workspace.projects[0]["id"],
            "mode": "movie",
            "rig": "configs/rig-sim.yaml",
            **overrides,
        }
    )


def finish(workspace, run):
    workspace.worker.join(timeout=10)
    assert not workspace.worker.is_alive()
    return workspace.detail(run["id"])


class TestProjects:
    def test_registry_discovery_and_roundtrip(self, workspace):
        p = workspace.describe(workspace.projects[0]["id"])
        assert p["rigs"] == ["configs/rig-sim.yaml"]
        assert p["configs"] == ["configs/task.yaml"]
        assert workspace.config(p["id"], p["configs"][0])["values"]["speed"] == 3
        workspace.add(p["path"], sys.executable)
        assert len(workspace.projects) == 1
        restored = Workspace(workspace.directory)
        assert restored.state()["projects"] == workspace.state()["projects"]
        workspace.remove(p["id"])
        assert workspace.state()["projects"] == []

    def test_invalid_paths_and_interpreter(self, workspace, tmp_path):
        with pytest.raises(ValueError, match="No run.py"):
            workspace.add(str(tmp_path))
        with pytest.raises(ValueError, match="interpreter does not exist"):
            workspace.add(workspace.projects[0]["path"], str(tmp_path / "missing"))
        with pytest.raises(ValueError, match="not registered"):
            workspace.describe("missing")
        with pytest.raises(ValueError, match="YAML"):
            workspace.config(workspace.projects[0]["id"], "run.py")

    def test_script_discovery_does_not_import(self, workspace):
        root = Path(workspace.projects[0]["path"])
        package = root / "src/my_experiment"
        package.mkdir(parents=True)
        (package / "preview.py").write_text(
            "raise RuntimeError('do not import')\n"
            "parser.add_argument('--out')\nparser.add_argument('--task-config')\n"
            "parser.add_argument('--rig')\nif __name__ == '__main__': main()\n"
        )
        (package / "movie.py").write_text("def views(): pass\n")
        actions = script_actions(root)
        assert len(actions) == 1
        assert actions[0]["module"] == "my_experiment.preview"
        assert actions[0]["params_flag"] == "--task-config"
        args = workspace._command(
            request_for(workspace, mode=actions[0]["id"], parameters={"speed": 4}),
            workspace.directory / "job",
        )
        assert "-m" in args and "--task-config" in args and "--rig" in args
        with pytest.raises(ValueError, match="dashboard controls"):
            workspace._command(
                request_for(workspace, mode=actions[0]["id"], script_args="--out=/tmp/elsewhere"),
                workspace.directory,
            )


class TestLaunches:
    def test_real_process_media_snapshot_logs_and_history(self, workspace):
        original = Path(workspace.projects[0]["path"]) / "configs/task.yaml"
        content = original.read_bytes()
        run = finish(
            workspace,
            workspace.start(
                request_for(workspace, parameters={"speed": 7, "duration": {"ms": 150}})
            ),
        )
        assert run["status"] == "completed" and run["returncode"] == 0
        assert workspace.active is None
        assert run["artifacts"][0]["path"] == "clip.mp4"
        assert '--mode", "movie"' in run["log"]
        assert yaml.safe_load((Path(run["directory"]) / "params.yaml").read_text())["speed"] == 7
        assert (Path(run["directory"]) / "rig.yaml").read_bytes() == RIG.read_bytes()
        assert original.read_bytes() == content
        restored = Workspace(workspace.directory)
        assert restored.detail(run["id"])["status"] == "completed"
        run2 = finish(workspace, workspace.start(request_for(workspace)))
        assert run2["directory"] != run["directory"]
        assert Path(run["directory"], "media/clip.mp4").exists()

    @pytest.mark.parametrize("mode", ["simulate", "test", "run", "demo", "measure", "movie"])
    def test_standard_modes(self, workspace, mode):
        req = request_for(
            workspace,
            mode=mode,
            subject="s01",
            seed=42,
            parameters=None if mode == "measure" else {"speed": 2},
            headless=mode == "simulate",
            mouse=mode == "test",
            sheet=True,
            columns=2,
            clips=["one"],
        )
        cmd = workspace._command(req, workspace.directory / "job")
        assert cmd[:2] == [sys.executable, "-u"]
        assert cmd[cmd.index("--mode") + 1] == mode
        assert cmd[cmd.index("--seed") + 1] == "42"
        assert ("--params" in cmd) == (mode != "measure")
        if mode == "movie":
            assert "--sheet" in cmd and "--clip" in cmd and "--columns" in cmd
        if mode == "demo":
            assert "--screenshots" in cmd
        if mode == "simulate":
            assert "--headless" in cmd
        if mode == "test":
            assert "--mouse" in cmd

    @pytest.mark.parametrize(
        "args, message",
        [
            ({"mode": "run"}, "subject ID"),
            ({"mode": "movie", "headless": True}, "only simulate"),
            ({"mode": "run", "mouse": True}, "only test"),
            ({"mode": "missing"}, "Unknown experiment"),
            ({"rig": "../outside.yaml"}, "inside"),
            ({"rig": "missing.yaml"}, "existing rig"),
            ({"script_args": "--out bad"}, "standalone scripts"),
            ({"parameters": {}, "parameters_yaml": "speed: 2"}, "either"),
            ({"parameters_yaml": "[1,2]"}, "mapping"),
            ({"mode": "measure", "parameters": {"speed": 2}}, "does not use task parameters"),
        ],
    )
    def test_invalid_launches_fail_before_creating_a_run(self, workspace, args, message):
        with pytest.raises(ValueError, match=message):
            workspace.start(request_for(workspace, **args))
        assert workspace.runs == {}

    def test_nonzero_exit_and_spawn_failure_are_visible(self, workspace):
        root = Path(workspace.projects[0]["path"])
        (root / "run.py").write_text("raise RuntimeError('bad parameter combination')")
        run = finish(workspace, workspace.start(request_for(workspace)))
        assert run["status"] == "failed" and "bad parameter combination" in run["log"]
        workspace.projects[0]["python"] = str(root / "missing-python")
        failed = workspace.start(request_for(workspace))
        assert failed["status"] == "failed" and failed["error"]
        assert workspace.active is None

    def test_stop_and_overlapping_runs(self, workspace, monkeypatch):
        started, stopped = threading.Event(), threading.Event()

        class Process:
            pid = 123

            def wait(self, timeout=None):
                started.set()
                assert stopped.wait(timeout=5)
                return -2

            def poll(self):
                return -2 if stopped.is_set() else None

        monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
        monkeypatch.setattr(workspace, "_signal", lambda process, force: stopped.set())
        run = workspace.start(request_for(workspace))
        assert started.wait(timeout=5)
        with pytest.raises(ValueError, match="Another run"):
            workspace.start(request_for(workspace))
        with pytest.raises(ValueError, match="Stop this experiment"):
            workspace.remove(run["project"])
        workspace.stop(run["id"])
        assert finish(workspace, run)["status"] == "cancelled"
        with pytest.raises(ValueError, match="no longer active"):
            workspace.stop(run["id"])

    def test_interrupted_history_and_log_tail(self, workspace):
        run = finish(workspace, workspace.start(request_for(workspace)))
        directory = Path(run["directory"])
        record = json.loads((directory / "run.json").read_text())
        record["status"] = "running"
        (directory / "run.json").write_text(json.dumps(record))
        (directory / "console.log").write_text(
            "x" * 80000 + "\nhttp://127.0.0.1:1234/?token=abc_-123"
        )
        restored = Workspace(workspace.directory)
        detail = restored.detail(run["id"])
        assert detail["status"] == "interrupted"
        assert len(detail["log"]) == 65536
        assert detail["monitor"] == "http://127.0.0.1:1234/?token=abc_-123"


class TestBoundaries:
    def test_only_one_server_can_recover_and_use_a_workspace(self, tmp_path):
        with (
            workspace_lock(tmp_path),
            pytest.raises(ValueError, match="already has this workspace"),
            workspace_lock(tmp_path),
        ):
            pytest.fail("two servers acquired the same workspace")
        with workspace_lock(tmp_path):
            pass

    @pytest.mark.parametrize(
        "content", ["[]", "speed: [", "speed: .nan", "day: 2026-01-01", "x: &x [*x]", "1: value"]
    )
    def test_parameter_errors_are_explicit(self, content):
        with pytest.raises(ValueError):
            mapping(content)

    def test_traversal_and_symlink_media(self, workspace, tmp_path):
        with pytest.raises(ValueError, match="inside"):
            inside(tmp_path, "../elsewhere")
        if os.name == "nt":
            pytest.skip("symlink creation needs Windows developer mode")
        run = finish(workspace, workspace.start(request_for(workspace)))
        secret = tmp_path / "secret.png"
        secret.write_bytes(b"private")
        root = Path(run["directory"]) / "media"
        (root / "escape.png").symlink_to(secret)
        assert [a["path"] for a in workspace.detail(run["id"])["artifacts"]] == ["clip.mp4"]
        with pytest.raises(ValueError, match="inside"):
            inside(root, "escape.png")


@pytest.fixture
def http(workspace):
    server = DashboardServer(workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, body=None, headers=None, method=None):
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        merged = {"X-Alhazen-Token": server.token, **(headers or {})}
        if body is not None:
            merged["Content-Type"] = "application/json"
        conn.request(
            method or ("POST" if body is not None else "GET"),
            path,
            json.dumps(body) if body is not None else None,
            merged,
        )
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    yield request, server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


class TestHTTP:
    def test_page_state_and_yaml(self, http, workspace):
        call, _ = http
        status, headers, page = call("/")
        assert status == 200 and b"Configure a run" in page
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert call("/workspace.js")[0] == 200
        assert call("/workspace.css")[0] == 200
        assert json.loads(call("/api/state")[2])["projects"][0]["name"] == "experiment with spaces"
        assert json.loads(call("/api/parameters", {"text": "speed: 2"})[2])["values"] == {
            "speed": 2
        }
        assert call("/api/parameters", {"text": 2})[0] == 400
        assert call("/no-such-file")[0] == 404
        key = workspace.projects[0]["id"]
        assert call(f"/api/config?project={key}&path=configs/task.yaml")[0] == 200
        assert call(f"/api/config?project={key}&path=../../secret.yaml")[0] == 400

    @pytest.mark.parametrize(
        "headers",
        [
            {"X-Alhazen-Token": "wrong"},
            {"Origin": "https://other.example"},
            {"Host": "attacker.example"},
        ],
    )
    def test_auth_host_and_origin(self, http, headers):
        call, _ = http
        assert call("/api/state", headers=headers)[0] == 403
        assert call("/api/runs", {}, headers=headers)[0] == 403

    def test_encoded_api_paths_cannot_bypass_authentication(self, http):
        call, _ = http
        assert call("/%61pi/state", headers={"X-Alhazen-Token": ""})[0] == 403
        assert call("/%61pi/runs", {}, headers={"X-Alhazen-Token": ""})[0] == 403

    def test_invalid_requests(self, http):
        call, _ = http
        assert call("/api/state?a=1&a=2")[0] == 400
        assert call("/api/runs", {"mode": "movie"})[0] == 400
        assert call("/api/projects", {"path": []})[0] == 400
        assert call("/api/projects", [1, 2])[0] == 400
        assert call("/api/stop", headers={"Content-Length": "-1"}, method="POST")[0] == 413
        assert call("/api/runs/missing")[0] == 400

    def test_launch_and_video_ranges(self, http, workspace):
        call, server = http
        status, _, body = call("/api/runs", request_for(workspace).model_dump())
        assert status == 201
        run = finish(workspace, json.loads(body))
        url = f"/media/{run['id']}/clip.mp4?token={server.token}"
        status, headers, data = call(url, headers={"Range": "bytes=2-5"})
        assert status == 206 and data == b"2345"
        assert headers["Content-Range"] == "bytes 2-5/10"
        assert headers["Content-Type"] == "video/mp4"
        assert call(url, headers={"Range": "bytes=-3"})[2] == b"789"
        assert call(url, headers={"Range": "bytes=6-"})[2] == b"6789"
        assert call(url, headers={"Range": "bytes=100-"})[0] == 416
        assert call(url, headers={"Range": "invalid"})[0] == 416
        assert call(url)[2] == b"0123456789"
        assert call(f"/media/{run['id']}/../params.yaml")[0] == 400
        assert call(f"/media/{run['id']}/missing.png")[0] == 404
        assert call("/media/missing/clip.mp4")[0] == 404


class TestParameterSchema:
    def test_schema_uses_project_interpreter_without_launching(self, workspace):
        root = Path(workspace.projects[0]["path"])
        (root / "run.py").write_text(
            "from pydantic import BaseModel\nfrom typing import Literal\n"
            "class Params(BaseModel):\n"
            '    motion: Literal["static", "moving"] = "static"\n'
            '    stimuli: tuple[str, ...] = ("bars", "kanizsa")\n'
            "class ExampleTask:\n    params_model = Params\n"
            'if __name__ == "__main__":\n'
            '    raise RuntimeError("must not launch")\n'
            "    run_experiment(task_class=ExampleTask)\n",
            encoding="utf-8",
        )
        schema = workspace.schema(workspace.projects[0]["id"])
        assert schema["properties"]["motion"]["enum"] == ["static", "moving"]
        assert schema["properties"]["stimuli"]["default"] == ["bars", "kanizsa"]
        assert not workspace.runs

    def test_missing_schema_is_an_explicit_error(self, workspace):
        with pytest.raises(ValueError, match="Cannot read task parameter choices"):
            workspace.schema(workspace.projects[0]["id"])
