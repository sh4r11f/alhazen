"""The Alhazen app (cli/app.py): the window, the rule that quits the app, and
the launcher behind the icon.

Edge, the server process and the message box are stood in for: a real one of
each needs a desktop session. What is real: the dashboard server the launcher
talks to (loopback HTTP), the holder record it reads, and the file lock that
makes two clicks start one server. The whole app was also run by hand on
Windows 11 (docs/hub/app.md, "What was verified")."""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from alhazen.cli import app as app_module
from alhazen.cli import dashboard as dashboard_module
from alhazen.cli.app import (
    OTHER_SERVER_GRACE_S,
    PREVIOUS_LOG_NAME,
    PROFILE_LOCK_NAME,
    WINDOW_START_GRACE_S,
    AppWindow,
    ask_for_window,
    find_edge,
    launch,
    log_line,
    profile_is_held,
    report_problem,
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

    def __init__(self, code=None, pid=4321):
        self.code = code
        self.pid = pid

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
    """An AppWindow whose Edge, browser and dialog are recorded stand-ins.

    ``seen["held"]["now"]`` is whether Edge holds the app's profile (its lock
    file is there); a test sets it. ``seen["logged"]`` collects the log."""
    started: list[FakeProcess] = []
    commands: list[list[str]] = []
    opened: list[str] = []
    dismiss = threading.Event()
    shown: list[str] = []
    held = {"now": False}
    logged: list[str] = []

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
        holds_profile=lambda profile: held["now"],
        log=logged.append,
    )
    return made, {
        "started": started,
        "commands": commands,
        "opened": opened,
        "dismiss": dismiss,
        "shown": shown,
        "held": held,
        "logged": logged,
    }


class TestWindow:
    def test_it_is_open_while_edge_holds_the_profile_whichever_process_does(self, tmp_path):
        """The failure this rule replaced the first one for: the Edge process
        the app starts ends at once, with success, and another Edge process
        shows the window. Under the first rule ("open while the process I
        started lives") the server stopped half a second after opening its
        window, which then said "127.0.0.1 refused to connect"."""
        clock = Clock()
        made, seen = window(tmp_path, clock=clock)
        assert not made.is_open()  # nothing was asked for yet
        made.show("http://x/hub")
        assert (tmp_path / "window").is_dir()
        # Edge passes the window on: the started process is gone at the first
        # look, and Edge has the profile.
        seen["started"][0].code = 0
        seen["held"]["now"] = True
        clock.sleep(0.5)
        assert made.is_open() and not made.failed_to_start()
        # Hours later it is still open, with no process of the app's alive.
        clock.sleep(3 * 3600)
        assert made.is_open() and not made.failed_to_start()
        # Closed: Edge lets go of the profile. Closed, not "never came".
        seen["held"]["now"] = False
        assert not made.is_open() and not made.failed_to_start()

    def test_a_window_on_its_way_counts_as_open(self, tmp_path):
        """Between the started process ending and another taking the profile
        nothing shows that a window is coming: it is given time."""
        clock = Clock()
        made, seen = window(tmp_path, clock=clock)
        made.show("http://x/hub")
        seen["started"][0].code = 0
        clock.sleep(0.5)
        assert made.is_open() and not made.failed_to_start()
        clock.sleep(WINDOW_START_GRACE_S - 1)
        assert made.is_open() and not made.failed_to_start()
        # Edge takes the profile just in time, and from then on that decides.
        seen["held"]["now"] = True
        clock.sleep(60)
        assert made.is_open()
        seen["held"]["now"] = False
        assert not made.is_open() and not made.failed_to_start()

    def test_a_window_that_never_came_is_a_failed_start_not_a_closed_window(self, tmp_path):
        clock = Clock()
        made, seen = window(tmp_path, clock=clock)
        assert not made.failed_to_start()  # nothing was asked for yet
        made.show("http://x/hub")
        assert not made.failed_to_start()  # it is running
        # Whatever the exit code: a hand-over also ends with success, so the
        # code says nothing. What says it is that no window ever appeared.
        for code in (1, 0):
            seen["started"][-1].code = code
            clock.sleep(1)
            assert made.is_open() and not made.failed_to_start()  # may still come
            clock.sleep(WINDOW_START_GRACE_S)
            assert not made.is_open() and made.failed_to_start()
            made.show("http://x/hub")  # the next round asks again

    def test_a_closed_window_is_closed_at_once_not_after_the_wait(self, tmp_path):
        """The wait is for a window that has not been seen yet. One that was
        up and is closed a few seconds after it opened is closed."""
        clock = Clock()
        made, seen = window(tmp_path, clock=clock)
        made.show("http://x/hub")
        seen["held"]["now"] = True
        clock.sleep(0.5)
        assert made.is_open()
        seen["started"][0].code = 0
        seen["held"]["now"] = False
        clock.sleep(0.5)
        assert not made.is_open() and not made.failed_to_start()

    def test_a_second_window_is_handed_to_the_edge_that_has_the_profile(self, tmp_path):
        clock = Clock()
        made, seen = window(tmp_path, clock=clock)
        made.show("http://x/hub")
        seen["held"]["now"] = True
        clock.sleep(5)
        assert made.is_open()
        # A second click: the new process hands its window over and ends.
        made.show("http://x/hub")
        seen["started"][1].code = 0
        clock.sleep(0.5)
        assert made.is_open()
        assert [c[1] for c in seen["commands"]] == ["--app=http://x/hub"] * 2
        # Both windows closed: the first process ends, the profile is free.
        seen["started"][0].code = 0
        seen["held"]["now"] = False
        assert not made.is_open() and not made.failed_to_start()

    def test_an_edge_that_keeps_no_lock_file_is_judged_by_its_processes(self, tmp_path):
        """Should Edge stop keeping the file, the app is no worse off than
        under the first rule: open while a process it started lives."""
        clock = Clock()
        made, seen = window(tmp_path, clock=clock)
        made.show("http://x/hub")
        clock.sleep(WINDOW_START_GRACE_S + 60)
        assert made.is_open()
        # It ends, with whatever code, long after it started: a closed
        # window, not a failed start.
        seen["started"][0].code = 1
        assert not made.is_open() and not made.failed_to_start()

    def test_what_it_sees_goes_to_the_log(self, tmp_path):
        clock = Clock()
        made, seen = window(tmp_path, clock=clock)
        made.show("http://x/hub")
        seen["started"][0].code = 0
        seen["held"]["now"] = True
        clock.sleep(0.5)
        made.is_open()
        made.is_open()  # a second look with nothing new says nothing new
        seen["held"]["now"] = False
        made.is_open()
        assert seen["logged"] == [
            "A window was asked for: started Edge (process 4321).",
            "The Edge process started 0.5 s ago (process 4321) ended with exit code 0.",
            "Edge has the app's profile open: the window is up.",
            "Edge no longer has the app's profile open.",
        ]

    def test_the_profile_is_held_while_edges_lock_file_is_there(self, tmp_path):
        assert not profile_is_held(tmp_path)
        (tmp_path / PROFILE_LOCK_NAME).write_bytes(b"")
        assert profile_is_held(tmp_path)

    def test_a_log_line_carries_the_time(self, capsys):
        log_line("The window is closed.")
        line = capsys.readouterr().out
        assert line.endswith(" The window is closed.\n")
        time.strptime(line.split(" ", 1)[0], "%H:%M:%S")

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
        assert seen["logged"][-1] == (
            "Edge showed no window: opening the page in the default browser instead."
        )
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
        self.logged: list[str] = []
        supervise(
            scripted,
            "http://x/hub",
            lambda: answers.pop(0) if answers else False,
            lambda: stopped.append(clock.now),
            poll_s=1.0,
            sleep=clock.sleep,
            log=self.logged.append,
        )
        return stopped

    def test_it_says_why_it_stops_before_it_does(self):
        self.run(ScriptedWindow([True, False]), [])
        assert self.logged == ["The window is closed and nothing is running: stopping."]

    def test_it_says_once_that_it_stays_for_a_session(self):
        self.run(ScriptedWindow([False]), [True, True, False])
        assert self.logged == [
            "The window is closed, but a session or a setup is still running: "
            "the app stays up until it has ended.",
            "The window is closed and nothing is running: stopping.",
        ]

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
        scripts = tmp_path / "Alhazen" / "app" / "Scripts"
        command = server_command(tmp_path, str(scripts / "pythonw.exe"))
        assert Path(command[0]) == scripts / "python.exe"
        assert command[1:] == ["-m", "alhazen", "app", "--state-dir", str(tmp_path)]
        # The name is matched whatever its case, as Windows itself does.
        assert Path(server_command(tmp_path, str(scripts / "PythonW.EXE"))[0]).name == "python.exe"
        # Any other interpreter is used as it is.
        other = str(scripts / "python3")
        assert server_command(tmp_path, other)[0] == other

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

    def test_the_log_of_the_run_before_is_kept(self, tmp_path):
        """A click after something went wrong must not erase the account of it."""

        def popen(command, **kwargs):
            return FakeProcess()

        state = tmp_path / "state"
        start_server(state, popen=popen)
        assert not (state / PREVIOUS_LOG_NAME).exists()  # a first start has none
        (state / "app.log").write_text("the run that went wrong\n", encoding="utf-8")
        start_server(state, popen=popen)
        assert (state / PREVIOUS_LOG_NAME).read_text(encoding="utf-8") == (
            "the run that went wrong\n"
        )
        assert (state / "app.log").read_text(encoding="utf-8") == ""
        # One generation: the next start replaces it.
        (state / "app.log").write_text("the next run\n", encoding="utf-8")
        start_server(state, popen=popen)
        assert (state / PREVIOUS_LOG_NAME).read_text(encoding="utf-8") == "the next run\n"

    def test_a_log_that_cannot_be_kept_is_said_and_does_not_stop_the_start(
        self, tmp_path, monkeypatch
    ):
        def refuse(source, target):
            raise PermissionError("the file is in use")

        state = tmp_path / "state"
        state.mkdir()
        (state / "app.log").write_text("old\n", encoding="utf-8")
        monkeypatch.setattr(app_module.os, "replace", refuse)
        started = []
        start_server(state, popen=lambda command, **kwargs: started.append(command))
        assert len(started) == 1
        assert (state / "app.log").read_text(encoding="utf-8") == (
            "(The log of the run before could not be kept: the file is in use)\n"
        )

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
        monkeypatch.setattr(app_module, "report_problem", done["boxes"].append)
        monkeypatch.setattr(app_module.webbrowser, "open", done["browser"].append)
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

    def test_a_problem_goes_to_a_box_on_windows_and_to_the_terminal_elsewhere(
        self, monkeypatch, capsys
    ):
        boxes = []
        monkeypatch.setattr(
            app_module, "message_box", lambda text, error=False: boxes.append((text, error))
        )
        monkeypatch.setattr(sys, "platform", "win32")
        report_problem("Alhazen could not start")
        assert boxes == [("Alhazen could not start", True)] and capsys.readouterr().err == ""
        monkeypatch.setattr(sys, "platform", "linux")
        report_problem("Alhazen could not start")
        assert len(boxes) == 1 and capsys.readouterr().err == "Alhazen could not start\n"

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
        # Its log says that it came up, and why it stopped.
        assert f"The app's server is up (process {os.getpid()})." in out
        assert "The window is closed and nothing is running: stopping." in out
        assert "console break" not in out
        # Stopped as after Ctrl+C: the holder record is gone, the lock free.
        assert not (state / "server.json").exists()
        with dashboard_module.workspace_lock(state):
            pass

    def test_the_server_outlives_the_edge_process_it_started_while_the_window_is_open(
        self, tmp_path, monkeypatch, capsys
    ):
        """The failure seen on a real computer, with the real serve(), the
        real AppWindow and the real rule that quits the app. Only Edge is a
        stand-in, and it does what Edge did: the process that is started ends
        at once, with success, while "another Edge process" has the profile
        (the lock file is there). The server must go on answering until that
        file goes, and stop then. Under the first rule it stopped half a
        second after it opened the window."""
        state = tmp_path / "state"
        lock = state.resolve() / "window" / PROFILE_LOCK_NAME

        def edge_that_passes_its_window_on(command):
            lock.write_bytes(b"")
            return FakeProcess(code=0)

        def window_with_that_edge(profile, *, edge):
            return AppWindow(profile, edge=Path("msedge.exe"), popen=edge_that_passes_its_window_on)

        seen: dict[str, object] = {}

        def the_person():
            # Wait for the app, use it a while (three looks of the rule that
            # quits it), fetch a page, then close the window.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and running_server(state) is None:
                time.sleep(0.05)
            time.sleep(3 * app_module.SUPERVISE_POLL_S)
            holder = running_server(state)
            seen["answered"] = holder is not None
            lock.unlink()

        monkeypatch.setattr(app_module, "AppWindow", window_with_that_edge)
        monkeypatch.setattr(dashboard_module, "interrupt_on_console_break", lambda: None)
        person = threading.Thread(target=the_person, daemon=True)
        person.start()
        args = argparse.Namespace(
            state_dir=str(state), project=[], port=0, no_browser=True, hub=True, window=True
        )
        assert serve(args) == 0
        person.join(10)
        assert seen == {"answered": True}
        out = capsys.readouterr().out
        # The log tells the whole story, in order. Every line of the app's
        # own carries the time; how long the process lived is not compared.
        said = [
            re.sub(r"started [0-9.]+ s ago", "started a moment ago", line)
            for line in re.findall(r"^[0-9]{2}:[0-9]{2}:[0-9]{2} (.*)$", out, re.MULTILINE)
        ]
        assert said == [
            f"The app's server is up (process {os.getpid()}).",
            "A window was asked for: started Edge (process 4321).",
            "The Edge process started a moment ago (process 4321) ended with exit code 0.",
            "Edge has the app's profile open: the window is up.",
            "Edge no longer has the app's profile open.",
            "The window is closed and nothing is running: stopping.",
        ]
        assert not (state / "server.json").exists()


# -- the real Edge (opt-in) ------------------------------------------------------------------


def _edges_on(profile: Path) -> list[int]:
    """The main Edge processes whose command line names ``profile``."""
    listed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | ForEach-Object "
            '{ "$($_.ProcessId)`t$($_.CommandLine)" }',
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = []
    for line in listed.splitlines():
        pid, _, command = line.partition("\t")
        # Edge's helpers (renderers and the like) carry --type=; the one
        # process that owns the windows does not.
        if profile.name in command and "--type=" not in command:
            found.append(int(pid))
    return found


@pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("ALHAZEN_TEST_REAL_EDGE") != "1" or not find_edge(),
    reason="opens a real Microsoft Edge window: set ALHAZEN_TEST_REAL_EDGE=1 on Windows",
)
def test_the_real_edge_keeps_its_lock_file_exactly_while_its_window_is_open(tmp_path):
    """What ``profile_is_held`` rests on, checked against the Edge that is
    installed: the file appears once Edge has the profile and is gone once its
    window is closed. If an Edge update ever changes that, this fails before a
    person's window does."""
    fetched: list[str] = []

    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            fetched.append(self.path)
            body = b"<title>Alhazen test</title>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    served = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=served.serve_forever, daemon=True).start()
    profile = tmp_path / f"edge-profile-{os.getpid()}"
    profile.mkdir()
    assert not profile_is_held(profile)
    url = f"http://127.0.0.1:{served.server_address[1]}/hub"
    subprocess.Popen(window_command(find_edge(), url, profile))
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (profile_is_held(profile) and fetched):
            time.sleep(0.25)
        assert profile_is_held(profile), "Edge never made its lock file in the profile"
        assert fetched, "Edge held the profile but never fetched the page"
        # Still held a while later: it is not a start-up file.
        time.sleep(3)
        assert profile_is_held(profile)
    finally:
        # Close the window the way a person does (a close request, not a
        # kill), whichever Edge process ended up with it.
        for pid in _edges_on(profile):
            subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, check=False)
        served.shutdown()
        served.server_close()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and profile_is_held(profile):
        time.sleep(0.25)
    assert not profile_is_held(profile), "the lock file outlived Edge's window"
