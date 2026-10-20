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

    def test_a_refused_start_opens_the_running_dashboard(self, tmp_path, monkeypatch, capsys):
        """Asking for the dashboard while one already serves the workspace
        shows that one: a second `alhazen dashboard` (typed again, or started
        in the background by a tool) used to print the refusal and nothing
        more, so nothing appeared on the screen. The refusal and exit code 1
        are kept; --no-browser still opens nothing."""
        opened = []
        monkeypatch.setattr(dashboard_module.webbrowser, "open", opened.append)
        state = tmp_path / "state"
        url = "http://127.0.0.1:4242/#token=abc"
        with workspace_lock(state):
            record_holder(state, url=url)
            assert main(["dashboard", "--state-dir", str(state)]) == 1
            assert opened == [url]
            assert main(["dashboard", "--no-browser", "--state-dir", str(state)]) == 1
            assert opened == [url]
        captured = capsys.readouterr()
        assert f"Opened the dashboard that is already running: {url}" in captured.out
        assert "already has this workspace open" in captured.err

    def test_a_refused_start_without_an_address_opens_nothing(self, tmp_path, monkeypatch):
        # A holder still binding its port has no address yet; nothing to open.
        opened = []
        monkeypatch.setattr(dashboard_module.webbrowser, "open", opened.append)
        state = tmp_path / "state"
        with workspace_lock(state):
            assert main(["dashboard", "--state-dir", str(state)]) == 1
        assert opened == []

    def test_the_holder_record_names_the_alhazen_and_its_shutdown(self, tmp_path):
        # What a tool that started the server in the background reads to
        # tell which alhazen serves and that POST /api/shutdown can stop it.
        state = tmp_path / "state"
        with workspace_lock(state):
            record = json.loads((state / "server.json").read_text(encoding="utf-8"))
        assert record["alhazen"] == dashboard_module.__version__
        assert record["shutdown"] is True

    def test_a_refusal_with_no_record_still_names_the_directory(self, tmp_path, capsys):
        # A holder from a build before the record existed, or one whose
        # record was removed by hand: the lock alone is still the truth.
        state = tmp_path / "state"
        with workspace_lock(state):
            (state / "server.json").unlink()
            assert main(["dashboard", "--no-browser", "--state-dir", str(state)]) == 1
        err = capsys.readouterr().err
        assert "already has this workspace open" in err and str(state) in err


def _post(server: DashboardServer, path: str, body: dict) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    connection.request(
        "POST",
        path,
        body=json.dumps(body),
        headers={"X-Alhazen-Token": server.token, "Content-Type": "application/json"},
    )
    response = connection.getresponse()
    answer = (response.status, json.loads(response.read()))
    connection.close()
    return answer


class TestShutDown:
    """POST /api/shutdown: the page's Shut down button."""

    @pytest.fixture
    def server(self, tmp_path):
        import threading

        workspace = Workspace(tmp_path / "state")
        server = DashboardServer(workspace, 0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        yield server, thread
        if thread.is_alive():
            server.shutdown()
        thread.join(10)
        server.server_close()
        server.uploads.close()
        server.library.close()
        workspace.close()

    def test_it_stops_serving_after_answering(self, server, capsys):
        server, thread = server
        assert _post(server, "/api/shutdown", {}) == (200, {"ok": True})
        thread.join(10)
        assert not thread.is_alive()
        assert "Shut down from the dashboard page." in capsys.readouterr().out

    def test_it_is_refused_while_a_session_runs(self, server):
        """A click must never end a session: the page disables the button,
        and the server refuses too, for a page whose state is stale."""
        server, thread = server
        server.workspace.active = "a-run"
        status, answer = _post(server, "/api/shutdown", {})
        assert status == 409 and "session is running" in answer["error"]
        assert not server.shutting_down
        server.workspace.active = None
        assert thread.is_alive()

    def test_a_launch_after_it_is_refused(self, server):
        server, _ = server
        server.shutting_down = True
        status, answer = _post(server, "/api/runs", {})
        assert status == 409 and "shutting down" in answer["error"]

    def test_the_library_form_refuses_an_address_it_cannot_use(self, server):
        server, _ = server
        status, answer = _post(
            server, "/api/library/connect", {"url": "http://example.com", "token": "x"}
        )
        assert status == 502 and "https" in answer["error"]
        status, _ = _post(server, "/api/library/connect", {"url": 3})
        assert status == 400

    def test_it_needs_the_token(self, server):
        server, thread = server
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        connection.request(
            "POST", "/api/shutdown", body="{}", headers={"Content-Type": "application/json"}
        )
        assert connection.getresponse().status == 403
        connection.close()
        assert thread.is_alive()


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

    def test_the_command_shut_down_from_the_page_exits_cleanly(self, tmp_path):
        """The real subprocess stopped by the page's Shut down: exit code 0
        and the workspace lock free for the next start, as with Ctrl+C."""
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
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            assert process.stdout is not None
            line = process.stdout.readline()
            assert line.startswith("Alhazen dashboard: "), line
            url = urlsplit(line.split(": ", 1)[1].strip())
            token = url.fragment.removeprefix("token=")
            connection = HTTPConnection("127.0.0.1", url.port, timeout=10)
            connection.request(
                "POST",
                "/api/shutdown",
                body="{}",
                headers={"X-Alhazen-Token": token, "Content-Type": "application/json"},
            )
            assert connection.getresponse().status == 200
            connection.close()
            output, errors = process.communicate(timeout=30)
        finally:
            process.kill()
        assert process.returncode == 0, (process.returncode, output, errors)
        assert "Shut down from the dashboard page." in output
        assert not (state / "server.json").exists()
        with workspace_lock(state):
            pass

    def test_the_library_appears_and_shut_down_reports_to_it(self, tmp_path):
        """End to end over HTTP against a real local platform with a machine token: the page's
        first read starts the list in the background, a later one lists the copy, and Shut
        down tells the platform the dashboard closed."""
        import time as _time
        import urllib.request

        from tests.unit.platform_support import add_version, experiment

        from alhazen.experiment_platform import library
        from alhazen.experiment_platform.library import LibrarySource
        from alhazen.experiment_platform.local import LocalPlatform
        from alhazen.experiment_platform.store import FileStore

        store = FileStore(tmp_path / "store", create=True)
        add_version(store, "lab", "dots", experiment())
        platform = LocalPlatform(store, name="Me")
        platform.start()

        def browser(method, path, body=None):  # type: ignore[no-untyped-def]
            request = urllib.request.Request(
                platform.origin + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"X-Alhazen-Token": platform.token, "Content-Type": "application/json"},
                method=method,
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())

        state = tmp_path / "state"
        process = None
        try:
            fork_id = browser("POST", "/api/forks", {"publisher": "lab", "exp": "dots"})["id"]
            token = browser("POST", "/api/machines", {"name": "Booth 2"})["token"]
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
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            assert process.stdout is not None
            line = process.stdout.readline()
            url = urlsplit(line.split(": ", 1)[1].strip())
            dash = url.fragment.removeprefix("token=")

            def ask(method, path, body=None):  # type: ignore[no-untyped-def]
                connection = HTTPConnection("127.0.0.1", url.port, timeout=10)
                connection.request(
                    method,
                    path,
                    body=json.dumps(body) if body is not None else None,
                    headers={"X-Alhazen-Token": dash, "Content-Type": "application/json"},
                )
                response = connection.getresponse()
                answer = json.loads(response.read())
                connection.close()
                return answer

            assert ask("GET", "/api/library") == {"configured": False, "items": []}
            # Written while the page is open, as Mana's kit does.
            library.save_source(
                state,
                LibrarySource.from_url(
                    platform.origin,
                    token,
                    copies=tmp_path / "copies",
                    machine="Booth 2",
                    run_jobs=("1792455795541",),
                ),
            )
            first = ask("GET", "/api/library")
            assert first["configured"] is True and first["refreshing"] is True
            deadline = _time.monotonic() + 20
            while _time.monotonic() < deadline:
                page = ask("GET", "/api/library")
                if not page["refreshing"]:
                    break
                _time.sleep(0.1)
            assert [x["id"] for x in page["items"]] == [fork_id], page
            assert ask("POST", "/api/shutdown", {}) == {"ok": True}
            output, errors = process.communicate(timeout=30)
            assert process.returncode == 0, (output, errors)
            assert "Told Library the dashboard closed (0 sessions" in output, output
            (event,) = browser("GET", "/api/machines")["events"]
            assert event["machine"] == "Booth 2" and event["jobs"] == ["1792455795541"]
            assert event["reason"] == "shut down from the page"
        finally:
            if process is not None:
                process.kill()
            platform.shutdown()
