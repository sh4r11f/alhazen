"""The Alhazen app (cli/app.py): the window, the rule that quits the app, and
the launcher behind the icon.

Edge, the server process and the message box are stood in for: a real one of
each needs a desktop session. What is real: the dashboard server the launcher
talks to (loopback HTTP), the holder record it reads, and the file lock that
makes two clicks start one server. The whole app was also run by hand on
Windows 11 (docs/hub/app.md, "What was verified")."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

import pytest

from alhazen.cli import app as app_module
from alhazen.cli import dashboard as dashboard_module
from alhazen.cli.app import (
    FAILED_START_S,
    OTHER_SERVER_GRACE_S,
    AppWindow,
    ask_for_window,
    find_edge,
    launch,
    running_server,
    server_command,
    start_lock,
    start_server,
    supervise,
    wait_until_up,
    window_command,
)
from alhazen.cli.dashboard import DashboardServer, record_holder, serve
from alhazen.cli.main import main
from alhazen.cli.workspace import Workspace

# -- stand-ins ----------------------------------------------------------------------------


class FakeProcess:
    """A started program: ``code`` is None while it runs."""

    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


class Clock:
    """Time that moves only when something sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def edge_at(root: Path) -> Path:
    program = root / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    program.parent.mkdir(parents=True)
    program.write_bytes(b"")
    return program


# -- finding Edge, and its command -----------------------------------------------------------


class TestEdge:
    def test_the_system_wide_edge_is_found_before_a_users_own(self, tmp_path):
        system = edge_at(tmp_path / "x86")
        edge_at(tmp_path / "local")
        env = {"ProgramFiles(x86)": str(tmp_path / "x86"), "LOCALAPPDATA": str(tmp_path / "local")}
        assert find_edge(env) == system

    def test_a_per_user_edge_is_found_when_it_is_the_only_one(self, tmp_path):
        own = edge_at(tmp_path / "local")
        env = {"ProgramFiles(x86)": str(tmp_path / "x86"), "LOCALAPPDATA": str(tmp_path / "local")}
        assert find_edge(env) == own

    def test_no_edge_is_none_not_an_error(self, tmp_path):
        assert find_edge({"ProgramFiles": str(tmp_path)}) is None
        assert find_edge({}) is None

    def test_the_window_is_an_app_window_on_a_profile_of_its_own(self, tmp_path):
        command = window_command(Path("msedge.exe"), "http://127.0.0.1:5/hub#token=t", tmp_path)
        assert command[0] == "msedge.exe"
        assert "--app=http://127.0.0.1:5/hub#token=t" in command
        assert f"--user-data-dir={tmp_path}" in command
        # Without these Edge opens a sync dialog in a second window on a new
        # profile, which would also outlive the app's own window.
        assert "--disable-sync" in command
        assert "--disable-features=msImplicitSignin" in command
        # And without this one it may stay alive with no window at all.
        assert "--disable-background-mode" in command


# -- the window ------------------------------------------------------------------------------


def window(tmp_path, *, edge=Path("msedge.exe"), clock=None):
    """An AppWindow whose Edge, browser and dialog are recorded stand-ins."""
    started: list[FakeProcess] = []
    commands: list[list[str]] = []
    opened: list[str] = []
    dismiss = threading.Event()
    shown: list[str] = []

    def popen(command):
        commands.append(command)
        started.append(FakeProcess())
        return started[-1]

    def dialog(text):
        shown.append(text)
        dismiss.wait(10)

    made = AppWindow(
        tmp_path / "window",
        edge=edge,
        popen=popen,
        open_browser=opened.append,
        dialog=dialog,
        clock=clock or Clock(),
    )
    return made, {
        "started": started,
        "commands": commands,
        "opened": opened,
        "dismiss": dismiss,
        "shown": shown,
    }


class TestWindow:
    def test_it_is_open_while_any_edge_process_it_started_lives(self, tmp_path):
        made, seen = window(tmp_path)
        assert not made.is_open()
        made.show("http://x/hub")
        assert made.is_open() and (tmp_path / "window").is_dir()
        # A second click: Edge hands the new window to the first process and
        # the second one exits at once, with success.
        made.show("http://x/hub")
        seen["started"][1].code = 0
        assert made.is_open()
        assert [c[1] for c in seen["commands"]] == ["--app=http://x/hub"] * 2
        seen["started"][0].code = 0
        assert not made.is_open()

    def test_a_clean_exit_is_a_closed_window_not_a_failed_start(self, tmp_path):
        made, seen = window(tmp_path)
        made.show("http://x/hub")
        seen["started"][0].code = 0
        assert not made.is_open() and not made.failed_to_start()

    def test_an_edge_that_exits_at_once_with_an_error_failed_to_start(self, tmp_path):
        clock = Clock()
        made, seen = window(tmp_path, clock=clock)
        assert not made.failed_to_start()  # nothing was started yet
        made.show("http://x/hub")
        assert not made.failed_to_start()  # it is running
        seen["started"][0].code = 1
        assert made.failed_to_start()

    def test_an_error_exit_long_after_the_start_is_just_a_closed_window(self, tmp_path):
        clock = Clock()
        made, seen = window(tmp_path, clock=clock)
        made.show("http://x/hub")
        clock.sleep(FAILED_START_S + 1)
        seen["started"][0].code = 1
        assert not made.failed_to_start()

    def test_without_edge_the_browser_opens_and_a_dialog_stands_in(self, tmp_path):
        made, seen = window(tmp_path, edge=None)
        made.show("http://x/hub")
        assert seen["opened"] == ["http://x/hub"] and seen["started"] == []
        assert made.is_open()
        # A second click opens the page again; there is still one dialog.
        made.show("http://x/hub")
        assert seen["opened"] == ["http://x/hub"] * 2
        assert len(seen["shown"]) == 1 and "Click OK to quit Alhazen" in seen["shown"][0]
        seen["dismiss"].set()
        made._dialog_thread.join(5)
        assert not made.is_open()

    def test_giving_up_on_edge_switches_to_the_browser(self, tmp_path):
        made, seen = window(tmp_path)
        made.show("http://x/hub")
        seen["started"][0].code = 1
        made.use_browser("http://x/hub")
        assert seen["opened"] == ["http://x/hub"] and made.is_open()
        assert not made.failed_to_start()
        # Later windows go the same way: Edge is not tried again.
        made.show("http://x/hub")
        assert len(seen["started"]) == 1
        seen["dismiss"].set()


# -- the rule that quits the app -------------------------------------------------------------


class ScriptedWindow:
    """A window whose answers are read from a list, one per look."""

    def __init__(self, opens, failed=False):
        self.opens = list(opens)
        self.failed = failed
        self.fallbacks: list[str] = []

    def is_open(self):
        return self.opens.pop(0) if self.opens else False

    def failed_to_start(self):
        return self.failed

    def use_browser(self, url):
        self.fallbacks.append(url)
        self.failed = False
        self.opens = [True, True, False]


class TestSupervise:
    def run(self, scripted, busy):
        stopped: list[float] = []
        clock = Clock()
        answers = list(busy)
        supervise(
            scripted,
            "http://x/hub",
            lambda: answers.pop(0) if answers else False,
            lambda: stopped.append(clock.now),
            poll_s=1.0,
            sleep=clock.sleep,
        )
        return stopped

    def test_the_app_stops_when_its_window_is_closed(self):
        # Open for three looks, then closed; nothing is running.
        assert self.run(ScriptedWindow([True, True, True, False]), []) == [4.0]

    def test_a_running_session_or_setup_keeps_it_alive_after_the_window(self):
        # Closed at the first look, but busy for two more.
        assert self.run(ScriptedWindow([False]), [True, True, False]) == [3.0]

    def test_a_window_reopened_meanwhile_is_waited_for_again(self):
        # Closed while busy, then opened again by a click on the icon.
        scripted = ScriptedWindow([False, True, True, False])
        assert self.run(scripted, [True]) == [4.0]

    def test_an_edge_that_never_showed_a_window_falls_back_instead_of_quitting(self):
        scripted = ScriptedWindow([False], failed=True)
        stopped = self.run(scripted, [])
        assert scripted.fallbacks == ["http://x/hub"]
        # It then lived as long as the stand-in dialog did.
        assert stopped == [4.0]


# -- the launcher and the running server -----------------------------------------------------


@pytest.fixture
def served(tmp_path):
    """A real dashboard server on a workspace, with its holder record."""
    workspace = Workspace(tmp_path / "state")
    server = DashboardServer(workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    record_holder(workspace.directory, url=server.url)
    yield workspace.directory, server
    server.shutdown()
    server.server_close()
    thread.join(5)
    workspace.close()


class ShownWindow:
    def __init__(self):
        self.urls: list[str] = []

    def show(self, url):
        self.urls.append(url)


class TestRunningServer:
    def test_the_holder_record_leads_to_the_server(self, served):
        directory, server = served
        assert running_server(directory) == (server.origin, server.token)

    def test_no_record_or_a_record_without_an_address_is_no_server(self, tmp_path):
        assert running_server(tmp_path) is None
        record_holder(tmp_path)  # the lock is taken, the port not yet bound
        assert running_server(tmp_path) is None
        (tmp_path / "server.json").write_text("{not json", encoding="utf-8")
        assert running_server(tmp_path) is None

    def test_a_record_left_by_a_dead_server_is_no_server(self, served, tmp_path):
        directory, server = served
        stale = json.loads((directory / "server.json").read_text(encoding="utf-8"))
        server.shutdown()
        server.server_close()
        (tmp_path / "server.json").write_text(json.dumps(stale), encoding="utf-8")
        assert running_server(tmp_path, timeout=1.0) is None

    def test_the_running_app_shows_its_window_when_asked(self, served):
        _, server = served
        server.window = ShownWindow()
        assert ask_for_window(server.origin, server.token) is True
        assert server.window.urls == [server.hub_url]

    def test_a_plain_dashboard_has_no_window_to_show(self, served):
        _, server = served
        assert ask_for_window(server.origin, server.token) is False

    def test_the_request_needs_the_workspace_token(self, served):
        _, server = served
        server.window = ShownWindow()
        assert ask_for_window(server.origin, "not-the-token") is False
        assert server.window.urls == []


class TestStarting:
    def test_the_server_runs_under_the_console_interpreter(self, tmp_path):
        # The launcher is windowless (pythonw); the server must not be.
        command = server_command(tmp_path, r"C:\Alhazen\app\Scripts\pythonw.exe")
        assert Path(command[0]).name == "python.exe"
        assert Path(command[0]).parent == Path(r"C:\Alhazen\app\Scripts")
        assert command[1:] == ["-m", "alhazen", "app", "--state-dir", str(tmp_path)]
        # Any other interpreter is used as it is.
        assert server_command(tmp_path, "/usr/bin/python3")[0] == str(Path("/usr/bin/python3"))

    def test_the_server_is_started_hidden_with_its_output_in_the_log(self, tmp_path):
        calls = []

        def popen(command, **kwargs):
            kwargs["stdout"].write("hello from the server\n")
            calls.append((command, kwargs))
            return FakeProcess()

        start_server(tmp_path / "state", popen=popen)
        command, kwargs = calls[0]
        assert command[-3:] == ["app", "--state-dir", str(tmp_path / "state")]
        assert kwargs["creationflags"] == app_module._NO_WINDOW | app_module._NEW_GROUP
        log = (tmp_path / "state" / "app.log").read_text(encoding="utf-8")
        assert log == "hello from the server\n"

    def test_only_one_launcher_at_a_time_starts_a_server(self, tmp_path):
        with start_lock(tmp_path) as first:
            assert first is True
            with start_lock(tmp_path) as second:
                assert second is False
        # Released: the next click may start one again.
        with start_lock(tmp_path) as again:
            assert again is True


class TestWaiting:
    def wait(self, tmp_path, server, up_at=None, timeout=120.0):
        clock = Clock()

        def is_up(directory):
            return ("origin", "token") if up_at is not None and clock.now >= up_at else None

        problem = wait_until_up(
            tmp_path, server, timeout=timeout, is_up=is_up, clock=clock, sleep=clock.sleep
        )
        return problem, clock.now

    def test_a_server_that_answers_is_up(self, tmp_path):
        problem, waited = self.wait(tmp_path, FakeProcess(), up_at=3.0)
        assert problem is None and 3.0 <= waited < 3.5

    def test_a_server_that_exits_is_reported_with_the_end_of_its_log(self, tmp_path):
        (tmp_path / "app.log").write_text(
            "CANNOT OPEN DASHBOARD: the disk is full\n", encoding="utf-8"
        )
        problem, waited = self.wait(tmp_path, FakeProcess(code=1))
        assert "could not start (exit code 1)" in problem
        assert "CANNOT OPEN DASHBOARD: the disk is full" in problem
        # Not at once: another launcher's server may have been on its way.
        assert OTHER_SERVER_GRACE_S <= waited < OTHER_SERVER_GRACE_S + 1

    def test_losing_the_race_to_another_server_is_not_a_failure(self, tmp_path):
        # Ours exited (the other holds the workspace); the other comes up.
        problem, _ = self.wait(tmp_path, FakeProcess(code=1), up_at=8.0)
        assert problem is None

    def test_a_server_that_never_answers_is_reported_as_slow_not_dead(self, tmp_path):
        problem, waited = self.wait(tmp_path, FakeProcess(), timeout=20.0)
        assert "did not start within 20 seconds" in problem and "empty" not in problem.lower()
        assert 20.0 <= waited < 20.5

    def test_a_launcher_that_started_nothing_waits_for_the_others_server(self, tmp_path):
        assert self.wait(tmp_path, None, up_at=5.0)[0] is None
        problem, _ = self.wait(tmp_path, None, timeout=10.0)
        assert "did not start within 10 seconds" in problem

    def test_a_missing_log_is_said_not_hidden(self, tmp_path):
        problem, _ = self.wait(tmp_path, FakeProcess(code=3))
        assert "exit code 3" in problem and "could not be read" in problem


class TestLaunch:
    @pytest.fixture
    def spy(self, monkeypatch):
        """Record what the launcher does instead of doing it."""
        done: dict[str, list] = {"started": [], "boxes": [], "browser": [], "asked": []}

        def start(directory, **_):
            done["started"].append(directory)
            return FakeProcess()

        monkeypatch.setattr(app_module, "start_server", start)
        monkeypatch.setattr(
            app_module, "message_box", lambda text, error=False: done["boxes"].append(text)
        )
        monkeypatch.setattr(app_module.webbrowser, "open", done["browser"].append)
        monkeypatch.setattr(sys, "platform", "win32")
        return done

    def test_a_first_click_starts_the_server_and_waits_for_it(self, tmp_path, spy, monkeypatch):
        monkeypatch.setattr(app_module, "running_server", lambda directory: None)
        monkeypatch.setattr(app_module, "wait_until_up", lambda directory, server: None)
        assert launch(["--state-dir", str(tmp_path)]) == 0
        assert spy["started"] == [tmp_path.resolve()] and spy["boxes"] == []

    def test_a_click_while_the_app_runs_asks_for_its_window(self, served, spy):
        directory, server = served
        server.window = ShownWindow()
        assert launch(["--state-dir", str(directory)]) == 0
        assert server.window.urls == [server.hub_url]
        assert spy["started"] == [] and spy["browser"] == []

    def test_a_click_while_a_plain_dashboard_runs_opens_its_page(self, served, spy):
        directory, server = served
        assert launch(["--state-dir", str(directory)]) == 0
        assert spy["browser"] == [server.hub_url] and spy["started"] == []

    def test_a_failed_start_is_shown_in_a_box(self, tmp_path, spy, monkeypatch):
        monkeypatch.setattr(app_module, "running_server", lambda directory: None)
        monkeypatch.setattr(
            app_module, "wait_until_up", lambda directory, server: "Alhazen could not start"
        )
        assert launch(["--state-dir", str(tmp_path)]) == 1
        assert spy["boxes"] == ["Alhazen could not start"]

    def test_a_server_that_cannot_even_be_started_is_shown_too(self, tmp_path, spy, monkeypatch):
        def broken(directory, **_):
            raise OSError("python.exe is missing")

        monkeypatch.setattr(app_module, "running_server", lambda directory: None)
        monkeypatch.setattr(app_module, "start_server", broken)
        assert launch(["--state-dir", str(tmp_path)]) == 1
        assert spy["boxes"] == ["Alhazen could not start: python.exe is missing"]

    def test_a_second_click_during_a_start_starts_nothing(self, tmp_path, spy, monkeypatch):
        waited = []
        monkeypatch.setattr(app_module, "running_server", lambda directory: None)
        monkeypatch.setattr(
            app_module, "wait_until_up", lambda directory, server: waited.append(server)
        )
        with start_lock(tmp_path.resolve()) as first:
            assert first is True
            assert launch(["--state-dir", str(tmp_path)]) == 0
        # It waited for the other launcher's server (None: it has none).
        assert spy["started"] == [] and waited == [None]


# -- `alhazen app`: the server with the window ----------------------------------------------


class TestTheServer:
    def test_the_command_is_for_windows(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "linux")
        assert main(["app"]) == 1
        assert "alhazen dashboard --hub" in capsys.readouterr().err

    def test_the_command_serves_the_hub_page_with_a_window(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        served_with = []
        monkeypatch.setattr(dashboard_module, "serve", lambda args: served_with.append(args) or 0)
        assert main(["app", "--state-dir", str(tmp_path), "--port", "0"]) == 0
        args = served_with[0]
        assert args.window is True and args.hub is True and args.no_browser is True
        assert args.state_dir == str(tmp_path) and args.project == []

    def test_the_server_shows_the_window_and_stops_when_it_is_closed(
        self, tmp_path, monkeypatch, capsys
    ):
        """The real serve(): it opens the Hub page in the window, serves
        until the window is closed, then stops cleanly and frees the
        workspace. The window is a stand-in that closes after three looks."""
        made: list[ScriptedWindow] = []
        shown: list[str] = []

        class Window(ScriptedWindow):
            def __init__(self, profile, *, edge):
                super().__init__([True, True, True, False])
                self.profile = profile
                made.append(self)

            def show(self, url):
                shown.append(url)

        monkeypatch.setattr(app_module, "AppWindow", Window)
        monkeypatch.setattr(app_module, "find_edge", lambda: None)
        monkeypatch.setattr(dashboard_module, "interrupt_on_console_break", lambda: None)
        state = tmp_path / "state"
        args = argparse.Namespace(
            state_dir=str(state), project=[], port=0, no_browser=True, hub=True, window=True
        )
        assert serve(args) == 0
        out = capsys.readouterr().out
        hub_url = next(
            line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("Experiment Hub")
        )
        assert shown == [hub_url]
        assert made[0].profile == state.resolve() / "window"
        # Stopped as after Ctrl+C: the holder record is gone, the lock free.
        assert not (state / "server.json").exists()
        with dashboard_module.workspace_lock(state):
            pass
