"""The `alhazen dashboard` command end to end: serve, answer, stop, release.

`serve()`, `_serve()` and `_dashboard()` had no test of their own: the HTTP
tests build a DashboardServer directly. These drive the command itself — in
process with the blocking call stubbed, and as the real subprocess a person
starts — through the whole life of a server: the URL it prints, an
authenticated request, the stop signal the workspace uses on this platform
(a console break on Windows, SIGINT elsewhere), a clean exit code, and the
workspace lock released for the next start.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from http.client import HTTPConnection
from urllib.parse import urlsplit

import pytest

from alhazen.cli import dashboard as dashboard_module
from alhazen.cli.dashboard import DashboardServer, record_holder, serve, workspace_lock
from alhazen.cli.main import main
from alhazen.cli.workspace import Workspace


class TestServeInProcess:
    def test_serve_arms_the_break_prints_the_url_and_releases_the_lock(
        self, tmp_path, monkeypatch, capsys
    ):
        """serve_forever is replaced by the interrupt that stops a real
        server, so the whole path around it runs: the console-break handler
        is armed first, the URL is printed, the workspace is closed (which is
        what stops an active run) and the lock is free again afterwards."""
        armed, closed = [], []
        monkeypatch.setattr(dashboard_module, "interrupt_on_console_break", lambda: armed.append(1))

        recorded = []

        def interrupted(self, poll_interval):
            # What a second `alhazen dashboard` would read while this one
            # serves: the holder record, with the address to open instead.
            recorded.append(json.loads((state / "server.json").read_text(encoding="utf-8")))
            raise KeyboardInterrupt

        monkeypatch.setattr(DashboardServer, "serve_forever", interrupted)
        monkeypatch.setattr(Workspace, "close", lambda self: closed.append(1))
        state = tmp_path / "state"
        args = argparse.Namespace(state_dir=str(state), project=[], port=0, no_browser=True)
        assert serve(args) == 0
        out = capsys.readouterr().out
        assert out.startswith("Alhazen dashboard: http://127.0.0.1:")
        assert "#token=" in out.splitlines()[0]
        assert armed == [1] and closed == [1]
        assert recorded[0]["pid"] == os.getpid()
        assert recorded[0]["url"] == out.splitlines()[0].removeprefix("Alhazen dashboard: ")
        # Released with the lock: a record left behind would name a dead
        # process to the next server, which must simply take the workspace.
        assert not (state / "server.json").exists()
        with workspace_lock(state):
            pass

    def test_a_workspace_already_open_is_refused_naming_its_holder(self, tmp_path, capsys):
        """The refusal says which process has the workspace and where its page
        is, so the person who forgot a server in another terminal can open
        that page or stop that process, instead of guessing which of their
        windows holds it. It used to name only the directory."""
        state = tmp_path / "state"
        with workspace_lock(state):
            record_holder(state, url="http://127.0.0.1:4242/#token=abc")
            assert main(["dashboard", "--no-browser", "--state-dir", str(state)]) == 1
        err = capsys.readouterr().err
        assert "CANNOT OPEN DASHBOARD: A dashboard already has this workspace open" in err
        assert f"process {os.getpid()}" in err
        assert "http://127.0.0.1:4242/#token=abc" in err
        assert "--state-dir" in err

    def test_a_refusal_with_no_record_still_names_the_directory(self, tmp_path, capsys):
        # A holder from a build before the record existed, or one whose
        # record was removed by hand: the lock alone is still the truth.
        state = tmp_path / "state"
        with workspace_lock(state):
            (state / "server.json").unlink()
            assert main(["dashboard", "--no-browser", "--state-dir", str(state)]) == 1
        err = capsys.readouterr().err
        assert "already has this workspace open" in err and str(state) in err


class TestTheCommand:
    def test_the_command_serves_answers_and_stops_cleanly(self, tmp_path):
        """The real subprocess, stopped the way the workspace stops its own
        children on this platform. On Windows that is CTRL_BREAK_EVENT, which
        would have killed the server on the spot before _serve armed the
        handler — leaving the lock held and any run stranded; exit code 0
        and a free lock afterwards are the proof it now stops properly."""
        state = tmp_path / "state"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "alhazen.cli.main",
                "dashboard",
                "--no-browser",
                "--port",
                "0",
                "--state-dir",
                str(state),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            # Kept apart from stdout: `-m alhazen.cli.main` makes runpy warn on
            # stderr that the module was already imported (the package
            # re-exports main), and the first stdout line must be the URL.
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            # Its own process group, exactly as the workspace starts a run, so
            # the break reaches it and not this test.
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            start_new_session=os.name != "nt",
        )
        try:
            assert process.stdout is not None
            line = process.stdout.readline()
            assert line.startswith("Alhazen dashboard: "), line
            url = urlsplit(line.split(": ", 1)[1].strip())
            token = url.fragment.removeprefix("token=")
            assert url.hostname == "127.0.0.1" and url.port and token
            connection = HTTPConnection("127.0.0.1", url.port, timeout=10)
            connection.request("GET", "/", headers={"X-Alhazen-Token": token})
            page = connection.getresponse()
            assert page.status == 200 and b"Configure a run" in page.read()
            connection.request("GET", "/api/state", headers={"X-Alhazen-Token": token})
            state_response = connection.getresponse()
            assert state_response.status == 200
            assert json.loads(state_response.read())["directory"] == str(state.resolve())
            connection.close()
            # While it serves, the workspace is its alone.
            with (
                pytest.raises(ValueError, match="already has this workspace"),
                workspace_lock(state),
            ):
                pytest.fail("a second server took a workspace that is in use")
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGINT)
            output, errors = process.communicate(timeout=30)
        finally:
            process.kill()
        assert process.returncode == 0, (process.returncode, output, errors)
        with workspace_lock(state):
            pass
