"""Launcher tests use real child processes and HTTP, without a renderer or rig."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import socket
import subprocess
import sys
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest
import yaml

import alhazen
from alhazen.cli import workspace as workspace_module
from alhazen.cli.dashboard import DashboardServer, Handler, workspace_lock
from alhazen.cli.main import add_mode_arguments
from alhazen.cli.workspace import (
    STOP_GRACE_S,
    Launch,
    Workspace,
    parse_parameters,
    path_inside,
    script_actions,
)
from alhazen.modes import Mode

RIG = Path(__file__).parents[2] / "examples/minimal_fixation/rig-sim.yaml"
# The real probe, kept from before the fixture stubs it, for the tests of the
# probe itself.
REAL_PROBE = workspace_module.probe_interpreter


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # Registering an experiment imports alhazen in the project's interpreter,
    # which costs seconds per test; TestInterpreters runs the real probe.
    monkeypatch.setattr(
        workspace_module,
        "probe_interpreter",
        lambda python, path: {"alhazen_version": "stub", "python_version": "stub"},
    )
    root = tmp_path / "experiment with spaces"
    (root / "configs/rigs").mkdir(parents=True)
    (root / "configs/rig-sim.yaml").write_bytes(RIG.read_bytes())
    # A rig in a subdirectory: its relative path has a separator, which is
    # where Windows and POSIX records used to differ.
    (root / "configs/rigs/rig-lab.yaml").write_bytes(RIG.read_bytes())
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
        # Posix form on every OS, so a registry or run record written on a
        # Windows rig reads the same on a Mac — and CI is green on both.
        assert p["rigs"] == ["configs/rig-sim.yaml", "configs/rigs/rig-lab.yaml"]
        assert p["configs"] == ["configs/task.yaml"]
        assert not any("\\" in path for path in p["rigs"] + p["configs"])
        assert workspace.config(p["id"], p["configs"][0])["values"]["speed"] == 3
        assert "monitor" in workspace.config(p["id"], p["rigs"][1])["values"]
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

    def test_a_preview_that_does_not_parse_is_reported_not_hidden(self, workspace, caplog):
        """A preview.py with a syntax error gets no button — but silently, the
        missing button reads as "not a generator" instead of "broken". The
        warning names the file and the error, and the other scripts are still
        offered."""
        root = Path(workspace.projects[0]["path"])
        broken = root / "src/broken/preview.py"
        broken.parent.mkdir(parents=True)
        broken.write_text("parser.add_argument('--out'\nif __name__ == '__main__': main(\n")
        good = root / "src/good/preview.py"
        good.parent.mkdir(parents=True)
        good.write_text("parser.add_argument('--out')\nif __name__ == '__main__': main()\n")
        with caplog.at_level("WARNING", logger="alhazen.cli.workspace"):
            actions = script_actions(root)
        assert [a["module"] for a in actions] == ["good.preview"]
        assert len(caplog.records) == 1 and caplog.records[0].levelname == "WARNING"
        # The line and wording of a SyntaxError vary by Python version; the
        # warning must carry whatever this interpreter says about this file.
        with pytest.raises(SyntaxError) as error:
            ast.parse(broken.read_text())
        assert str(broken) in caplog.text
        assert f"line {error.value.lineno}: {error.value.msg}" in caplog.text


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
        # Relative paths in the record and the gallery are posix on every OS.
        assert run["rig"] == "configs/rig-sim.yaml"
        frames = Path(run["directory"]) / "media/frames"
        frames.mkdir()
        (frames / "first.png").write_bytes(b"png")
        listed = [a["path"] for a in workspace.detail(run["id"])["artifacts"]]
        assert listed == ["clip.mp4", "frames/first.png"]
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

    def test_a_forced_kill_is_recorded_honestly(self, workspace, monkeypatch):
        """A run that ignores the interrupt for the whole grace period is
        killed — and the history must say so, because a killed session has
        no trials file and no manifest, and "cancelled" would read as clean.
        """
        started, exited = threading.Event(), threading.Event()
        graces, signals = [], []

        class Process:
            pid = 123

            def wait(self, timeout=None):
                started.set()
                if timeout is not None:
                    # The child never reacts to the interrupt: the grace runs out.
                    graces.append(timeout)
                    raise subprocess.TimeoutExpired("run.py", timeout)
                assert exited.wait(timeout=5)
                return -9

            def poll(self):
                return -9 if exited.is_set() else None

        def send(process, force):
            signals.append(force)
            if force:
                exited.set()

        monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
        monkeypatch.setattr(workspace, "_signal", send)
        run = workspace.start(request_for(workspace))
        assert started.wait(timeout=5)
        workspace.stop(run["id"])
        detail = finish(workspace, run)
        # docs/workspace.md promises thirty seconds; an EDF transfer plus
        # manifest hashing does not fit in the ten it used to be.
        assert STOP_GRACE_S == 30 and graces == [STOP_GRACE_S]
        assert signals == [False, True]
        assert detail["status"] == "killed" and detail["stopped"] == "forced"
        assert f"killed after {STOP_GRACE_S} s" in detail["log"]
        assert "may be incomplete" in detail["log"]
        record = json.loads((Path(detail["directory"]) / "run.json").read_text())
        assert record["status"] == "killed" and record["stopped"] == "forced"

    def test_interrupted_history_and_log_tail(self, workspace):
        run = finish(workspace, workspace.start(request_for(workspace)))
        directory = Path(run["directory"])
        record = json.loads((directory / "run.json").read_text())
        record["status"] = "running"
        (directory / "run.json").write_text(json.dumps(record))
        # The runner's own line (session/runner.py logs "live dashboard: %s"
        # through "%(asctime)s %(levelname)s %(name)s: %(message)s") is the
        # contract; when a run restarts its monitor, the last URL is the live one.
        (directory / "console.log").write_text(
            "x" * 80000 + "\n2026-09-25 10:00:00,000 INFO alhazen.session.runner: "
            "live dashboard: http://127.0.0.1:1111/?token=stale\n"
            "2026-09-25 10:00:05,000 INFO alhazen.session.runner: "
            "live dashboard: http://127.0.0.1:1234/?token=abc_-123\n"
        )
        restored = Workspace(workspace.directory)
        detail = restored.detail(run["id"])
        assert detail["status"] == "interrupted"
        assert len(detail["log"]) == 65536
        assert detail["monitor"] == "http://127.0.0.1:1234/?token=abc_-123"


class TestCommandContract:
    """The launcher hand-builds run.py's flags; ``add_mode_arguments`` is the
    parser that has to accept them. A flag renamed in cli/main.py would
    otherwise break every launch silently: the child would exit on a usage
    error and the run would just read "failed". So every command the launcher
    can build is parsed with the runner's own parser — strictly (parse_args,
    not parse_known_args), so a flag the runner no longer knows is a failure.
    """

    @pytest.mark.parametrize("mode, sheet", [(m.value, False) for m in Mode] + [("movie", True)])
    def test_every_mode_command_parses_with_the_runner_parser(self, workspace, mode, sheet):
        request = request_for(
            workspace,
            mode=mode,
            subject="s01",
            session=2,
            seed=3,
            trials=4,
            parameters=None if mode == "measure" else {"speed": 2},
            headless=mode == "simulate",
            mouse=mode == "test",
            windowed=True,
            scale=0.25,
            sheet=sheet,
            columns=2 if sheet else None,
            clips=["one", "two"] if mode == "movie" else [],
        )
        command = workspace._command(request, workspace.directory / "job")
        parser = argparse.ArgumentParser()
        add_mode_arguments(parser)
        args = parser.parse_args(command[3:])  # after <python> -u run.py
        assert args.mode == mode and args.seed == 3 and args.windowed
        assert args.rig.endswith("rig-sim.yaml") and args.no_dashboard_browser
        assert (args.params is not None) == (mode != "measure")
        if Mode(mode).runs_trials:
            assert args.sub == "s01" and args.ses == 2
        if mode in {"test", "simulate"}:
            assert args.trials_per_condition == 4
        assert args.headless == (mode == "simulate") and args.mouse == (mode == "test")
        if mode == "demo":
            assert args.screenshots.endswith("media")
        if mode == "movie":
            assert args.out.endswith("media") and args.scale == 0.25
            assert args.clip == ["one", "two"]
            assert (args.sheet is not None) == sheet and args.columns == (2 if sheet else None)


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
            parse_parameters(content)

    def test_traversal_and_symlink_media(self, workspace, tmp_path):
        with pytest.raises(ValueError, match="inside"):
            path_inside(tmp_path, "../elsewhere")
        run = finish(workspace, workspace.start(request_for(workspace)))
        secret = tmp_path / "secret.png"
        secret.write_bytes(b"private")
        root = Path(run["directory"]) / "media"
        try:
            (root / "escape.png").symlink_to(secret)
        except OSError as exc:
            # Windows refuses symlinks to an account without the privilege
            # (ERROR_PRIVILEGE_NOT_HELD, 1314) unless Developer Mode is on. CI
            # runners have it, so the escape check runs there; skipping on
            # the platform alone would have hidden this test from Windows
            # entirely. Any other error is a real one.
            if getattr(exc, "winerror", None) != 1314:
                raise
            pytest.skip("symlink creation needs a privilege this account lacks")
        assert [a["path"] for a in workspace.detail(run["id"])["artifacts"]] == ["clip.mp4"]
        with pytest.raises(ValueError, match="inside"):
            path_inside(root, "escape.png")


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

    def test_a_wrong_host_says_which_url_to_open(self, http):
        """The likely cause is someone typing localhost:PORT; the refusal has
        to name the URL that works, not just say "Invalid host"."""
        call, server = http
        status, _, body = call("/api/state", headers={"Host": "attacker.example"})
        assert status == 403
        assert f"http://127.0.0.1:{server.server_port}" in json.loads(body)["error"]

    def test_a_stalled_client_cannot_hold_a_handler_forever(self, http, monkeypatch):
        """A body shorter than its Content-Length used to block rfile.read for
        as long as the client stayed connected, so one stalled tab could pin
        a handler thread for good. The handler's socket timeout ends the read
        and answers 408 with the reason; the class-level value is the contract,
        shortened here only so the test is quick."""
        assert Handler.timeout == 30
        call, server = http
        monkeypatch.setattr(Handler, "timeout", 0.5)
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=10) as sock:
            sock.sendall(
                f"POST /api/parameters HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
                f"X-Alhazen-Token: {server.token}\r\nContent-Type: application/json\r\n"
                'Content-Length: 100\r\n\r\n{"text":'.encode()
            )
            # Headers and JSON body arrive in separate segments; the server
            # closes the connection after the response, so read to EOF.
            response = b"".join(iter(lambda: sock.recv(65536), b""))
        assert response.split(b"\r\n")[0].endswith(b" 408 Request Timeout"), response
        assert b"did not arrive" in response
        # And the server is still serving.
        assert call("/api/state")[0] == 200


class TestInterpreters:
    def test_children_see_the_project_first_and_nothing_of_the_launcher(
        self, workspace, monkeypatch
    ):
        """From an installed wheel the launcher's root is its whole
        site-packages; put first on the *project's* interpreter's path it
        shadowed the project's pinned alhazen and every package beside it.
        Both children — the launch and the schema probe — must get the
        project's src/, its root, then whatever PYTHONPATH was inherited, and
        nothing else."""
        monkeypatch.setenv("PYTHONPATH", "INHERITED")
        root = workspace.projects[0]["path"]
        expected = os.pathsep.join([str(Path(root) / "src"), root, "INHERITED"])
        launcher_root = str(Path(workspace_module.__file__).resolve().parents[2])
        seen = {}

        class Process:
            pid = 123

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        def popen(command, **kwargs):
            seen["launch"] = kwargs["env"]["PYTHONPATH"]
            return Process()

        def run(command, **kwargs):
            seen["schema"] = kwargs["env"]["PYTHONPATH"]
            return subprocess.CompletedProcess(command, 0, stdout='{"properties": {}}', stderr="")

        monkeypatch.setattr(subprocess, "Popen", popen)
        monkeypatch.setattr(subprocess, "run", run)
        finish(workspace, workspace.start(request_for(workspace)))
        workspace.schema(workspace.projects[0]["id"])
        assert seen == {"launch": expected, "schema": expected}
        assert launcher_root not in expected

    def test_registration_records_which_alhazen_the_interpreter_has(self, workspace, monkeypatch):
        monkeypatch.setattr(workspace_module, "probe_interpreter", REAL_PROBE)
        project = workspace.add(workspace.projects[0]["path"], sys.executable)
        assert project["alhazen_version"] == alhazen.__version__
        assert project["python_version"] == sys.version
        restored = Workspace(workspace.directory)
        assert restored.projects[0]["alhazen_version"] == alhazen.__version__

    def test_an_interpreter_without_alhazen_is_refused_at_registration(
        self, workspace, monkeypatch
    ):
        """The wrong env used to register fine and die at the first launch's
        `import alhazen`. The refusal names the interpreter and the package to
        install, and leaves the registry as it was."""
        monkeypatch.setattr(workspace_module, "probe_interpreter", REAL_PROBE)
        root = Path(workspace.projects[0]["path"])
        # Shadow alhazen on the child's path with one that cannot import: the
        # project's own src/ comes first, so this is what the child sees.
        (root / "src/alhazen").mkdir(parents=True)
        (root / "src/alhazen/__init__.py").write_text("raise ImportError('not installed here')\n")
        before = [dict(p) for p in workspace.projects]
        with pytest.raises(ValueError, match=re.escape(sys.executable)) as refused:
            workspace.add(str(root), sys.executable)
        assert "alhazen-vision" in str(refused.value)
        assert "not installed here" in str(refused.value)
        assert workspace.projects == before

    def test_a_file_that_is_not_an_interpreter_is_refused(self, workspace, monkeypatch, tmp_path):
        monkeypatch.setattr(workspace_module, "probe_interpreter", REAL_PROBE)
        bogus = tmp_path / "not-python.txt"
        bogus.write_text("not an executable")
        with pytest.raises(ValueError, match="Cannot run the Python interpreter"):
            workspace.add(workspace.projects[0]["path"], str(bogus))


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

    def test_schema_is_cached_until_run_py_or_the_interpreter_changes(self, workspace, monkeypatch):
        """Reading the schema imports the task in a child interpreter — seconds
        each time — and the UI asks for it on every project switch. One read
        per (run.py, interpreter) is enough; editing run.py or choosing another
        interpreter must read again, because either can change the choices."""
        spawned = []

        def run(command, **kwargs):
            spawned.append(command[0])
            schema = {"properties": {"read": len(spawned)}}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(schema), stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        key = workspace.projects[0]["id"]
        assert workspace.schema(key)["properties"]["read"] == 1
        assert workspace.schema(key)["properties"]["read"] == 1 and len(spawned) == 1
        run_py = Path(workspace.projects[0]["path"]) / "run.py"
        stat = run_py.stat()
        os.utime(run_py, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        assert workspace.schema(key)["properties"]["read"] == 2
        other = str(Path(sys.executable).with_name("other-python"))
        workspace.projects[0]["python"] = other
        assert workspace.schema(key)["properties"]["read"] == 3 and spawned[-1] == other


class TestRecovery:
    def test_a_corrupt_registry_or_run_record_names_the_file(self, workspace):
        """A raw JSONDecodeError ("Expecting value: line 1 column 1") does not
        say which of the workspace's files it means; the person at the rig
        needs the path to fix or move."""
        registry = workspace.directory / "projects.json"
        registry.write_text("{not json")
        with pytest.raises(ValueError, match=re.escape(str(registry))):
            Workspace(workspace.directory)
        registry.write_text("[]")
        record = workspace.directory / "runs/broken/run.json"
        record.parent.mkdir()
        record.write_text("{not json")
        with pytest.raises(ValueError, match=re.escape(str(record))):
            Workspace(workspace.directory)
        record.write_text('{"id": "broken"}')
        with pytest.raises(ValueError, match=re.escape(str(record))):
            Workspace(workspace.directory)
