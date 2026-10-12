"""The Alhazen app: the dashboard in a window of its own (Windows).

Secret it hides: how an "app" is made out of the loopback dashboard and a web
browser. Which browser draws the window, how the app learns that the window
was closed, what it does then, and how a second click on the icon finds the
app that is already running.

There are two processes.

* The **launcher** (``alhazen-app``, the program behind the Start-menu icon)
  is windowless and lives for a second or two. If the app is already running
  it asks it to show its window; otherwise it starts the server and waits
  until the server answers. It has no terminal to print to, so a failure to
  start is shown in a message box, with the end of the server's log.
* The **server** (``alhazen app``) is the ordinary dashboard
  (``alhazen dashboard --hub``) with two additions: it opens the Hub page in a
  Microsoft Edge *app window* (a window with no tabs and no address bar), and
  it stops itself once every such window has been closed and nothing is
  running. Closing the window is how the app is quit.

Why the server is a *hidden console* program rather than a windowless one: a
session is a console program that the dashboard starts, and stops with a
console break. A process started with ``CREATE_NO_WINDOW`` has a console
without a window; its children inherit that console, so a run neither opens a
console window of its own nor loses the break that stops it. A windowless
parent would give every run a visible console and could not send the break.
(Both measured on Windows 11; docs/hub/app.md.)

How the window's lifetime is known: Edge is started with a profile folder of
its own (``--user-data-dir``), which no other program uses. While any Edge
process has that profile open, Edge keeps a file named ``lockfile`` in it and
removes the file when the last such process ends. So "is a window open" is
"does Edge hold the app's profile".

Not "is the Edge process this server started still alive", which was the
first rule and was wrong: the process that is started is not always the one
that goes on to show the window. Edge may pass the window to another of its
processes and end the started one at once, with success. That is certain for a
second window (it is handed to the process that already has the profile) and
was seen now and then for the first, on Windows 11. Under the first rule the
server then took its window for closed half a second after opening it, and
stopped; the window was left with "127.0.0.1 refused to connect".

The started processes still count, for two cases the file cannot cover: the
moments before Edge has taken the profile (a window asked for is given
``WINDOW_START_GRACE_S`` to appear), and an Edge that would not keep such a
file (the app then behaves as under the first rule).

Price: the window is Edge's, so Edge must be installed (it is part of
Windows 10 and 11). Without it the page opens in the default browser and a
small dialog stands in for the window: dismissing it quits the app.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alhazen.cli.dashboard import HOLDER_RECORD

# Where Edge is installed, relative to each of these folders, in the order
# Windows itself uses: system-wide (32-bit folder first, where Edge lives even
# on 64-bit Windows), then per-user.
_EDGE_ROOTS = ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")
_EDGE_PATH = ("Microsoft", "Edge", "Application", "msedge.exe")
# The window's size when it first opens; Edge remembers a resized one.
WINDOW_SIZE = "1280,900"
# How often the server looks at whether its window is still open.
SUPERVISE_POLL_S = 0.5
# How long a window that was asked for is given to appear before the app
# believes there is none. It covers the moments in which no Edge process this
# server started is alive and Edge has not taken the profile yet (Edge passing
# the window from one of its processes to another). A window still missing
# after it never came: the app falls back to the default browser.
WINDOW_START_GRACE_S = 15.0
# The file Edge keeps in a profile folder while any of its processes has the
# profile open, and removes when the last of them ends (it is opened to be
# deleted on close, so it goes even when Edge is killed).
PROFILE_LOCK_NAME = "lockfile"
# How long the launcher waits for the server to answer before saying it did
# not start. A first start imports the whole dashboard from a cold disk.
START_TIMEOUT_S = 120.0
# How long the launcher keeps waiting after ITS server exited, in case another
# one is coming up: a second click while the first is still starting loses
# the race for the workspace, and must end at the first one's window, not at
# an error box.
OTHER_SERVER_GRACE_S = 30.0
# The server's output (it has no visible console): beside the workspace state.
LOG_NAME = "app.log"
# The log of the run before, kept when the next one starts: the usual moment
# to want it is after clicking the icon again because something went wrong.
PREVIOUS_LOG_NAME = "app.previous.log"
# Locked by the launcher that is starting the server (see ``start_lock``).
START_LOCK_NAME = "app-start.lock"
# How much of that log a failed start shows.
LOG_TAIL_CHARS = 1500
# CREATE_NO_WINDOW and CREATE_NEW_PROCESS_GROUP, 0 where they do not exist.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
# MessageBoxW flags: an OK button with an error or an information icon, kept
# above other windows so it is not lost behind the browser.
_MB_ERROR = 0x10 | 0x40000
_MB_INFO = 0x40 | 0x40000


def find_edge(environ: Mapping[str, str] | None = None) -> Path | None:
    """Microsoft Edge's program on this computer, or None."""
    env = os.environ if environ is None else environ
    for name in _EDGE_ROOTS:
        root = env.get(name)
        if root and (candidate := Path(root).joinpath(*_EDGE_PATH)).is_file():
            return candidate
    return None


def window_command(edge: Path, url: str, profile: Path) -> list[str]:
    """The command that opens ``url`` in an Edge app window on ``profile``.

    ``--app`` gives the window without tabs or address bar. The profile
    folder of its own is what ties the process's life to the window's (module
    docstring) and keeps the app apart from the person's own browsing.
    ``--disable-background-mode`` stops Edge from staying alive after its
    last window closes; the two ``--no-`` flags skip its first-run questions.

    ``--disable-sync`` and ``msImplicitSignin`` off: on a new profile Edge
    otherwise signs it in with the person's Windows account and opens a
    second window asking to confirm syncing (measured: the page
    ``edge://sync-confirmation-dialog`` appeared beside the app window, and
    with either flag it did not). The app's profile holds nothing worth
    syncing, and a stray Edge window would also keep the app alive after its
    own window was closed.
    """
    return [
        str(edge),
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-sync",
        "--disable-features=msImplicitSignin",
        f"--window-size={WINDOW_SIZE}",
    ]


def message_box(text: str, *, error: bool = False) -> None:
    """Show ``text`` in a dialog and wait for OK. The only way a windowless
    program can say anything, and the stand-in window where Edge is missing."""
    if sys.platform != "win32":
        raise AssertionError("the Alhazen app's dialogs exist on Windows only")
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, "Alhazen", _MB_ERROR if error else _MB_INFO)


def profile_is_held(profile: Path) -> bool:
    """Whether an Edge process has ``profile`` open right now (module
    docstring): Edge's own lock file is there."""
    return (profile / PROFILE_LOCK_NAME).exists()


def log_line(text: str) -> None:
    """One line in the app's log, with the time of day. The log is the
    server's output (``start_server``); it is the only place a program without
    a terminal can say why it did something, above all why it stopped."""
    print(f"{time.strftime('%H:%M:%S')} {text}", flush=True)


class AppWindow:
    """The app's window: shown on request, and asked whether it is open.

    With Edge, each :meth:`show` starts an Edge process on the app's profile.
    The window is open while Edge holds that profile, whichever of its
    processes does (module docstring). Without Edge, :meth:`show` opens the
    default browser and a dialog stands in for the window: it is "open" until
    the dialog is dismissed.

    Every change it sees goes to the log (``log``): a started process ending,
    Edge taking or letting go of the profile, the fallback to the browser.
    """

    def __init__(
        self,
        profile: Path,
        *,
        edge: Path | None,
        popen: Callable[..., Any] = subprocess.Popen,
        open_browser: Callable[[str], Any] = webbrowser.open,
        dialog: Callable[[str], None] = message_box,
        clock: Callable[[], float] = time.monotonic,
        holds_profile: Callable[[Path], bool] = profile_is_held,
        log: Callable[[str], None] = log_line,
    ) -> None:
        self.profile = profile
        self._edge = edge
        self._popen = popen
        self._open_browser = open_browser
        self._dialog = dialog
        self._clock = clock
        self._holds_profile = holds_profile
        self._log = log
        self._lock = threading.Lock()
        # Edge processes started and not yet seen to have ended, each with
        # the time it was started.
        self._processes: list[tuple[Any, float]] = []
        # When a window was last asked for (None: never), and whether Edge
        # has been seen holding the profile since: until it has, the window
        # may still be on its way (WINDOW_START_GRACE_S).
        self._asked_at: float | None = None
        self._seen_since_asked = False
        # Whether Edge was holding the profile at the last look, to log the
        # change and not every look.
        self._held = False
        # Whether a window was ever known to be up. One that was and is gone
        # was closed; one that never was, never came (failed_to_start).
        self._was_open = False
        # The stand-in dialog's thread, once the browser fallback is in use.
        self._dialog_thread: threading.Thread | None = None

    def show(self, url: str) -> None:
        """Open a window on ``url`` (or bring another one up beside it)."""
        with self._lock:
            edge = self._edge
        if edge is None:
            self._show_in_browser(url)
            return
        self.profile.mkdir(parents=True, exist_ok=True)
        process = self._popen(window_command(edge, url, self.profile))
        with self._lock:
            now = self._clock()
            self._processes.append((process, now))
            self._asked_at, self._seen_since_asked = now, False
        self._log(f"A window was asked for: started Edge (process {getattr(process, 'pid', '?')}).")

    def _show_in_browser(self, url: str) -> None:
        self._open_browser(url)
        with self._lock:
            if self._dialog_thread is not None and self._dialog_thread.is_alive():
                return
            self._dialog_thread = threading.Thread(
                target=self._dialog,
                args=(
                    "Alhazen is open in your web browser.\n\n"
                    "Leave this message open while you work. Click OK to quit Alhazen "
                    "(a running session finishes first).",
                ),
                name="alhazen-app-dialog",
                daemon=True,
            )
            self._dialog_thread.start()

    def _look(self) -> tuple[bool, bool]:
        """Look at Edge once: ``(up, starting)``. Called with the lock held.

        ``up``: a window is known to be there. Edge holds the profile, or a
        process this server started is alive. ``starting``: neither, but a
        window was asked for a moment ago and has not been seen yet, so it
        may still be coming.
        """
        now = self._clock()
        alive = []
        for process, started in self._processes:
            code = process.poll()
            if code is None:
                alive.append((process, started))
            else:
                self._log(
                    f"The Edge process started {now - started:.1f} s ago "
                    f"(process {getattr(process, 'pid', '?')}) ended with exit code {code}."
                )
        self._processes = alive
        if self._edge is None or self._asked_at is None:
            # No window was ever asked of Edge: there is nothing to look at.
            return False, False
        held = self._holds_profile(self.profile)
        if held != self._held:
            self._held = held
            self._log(
                "Edge has the app's profile open: the window is up."
                if held
                else "Edge no longer has the app's profile open."
            )
        if held:
            self._seen_since_asked = True
        waited = now - self._asked_at
        starting = not self._seen_since_asked and waited < WINDOW_START_GRACE_S
        if held or (alive and not starting):
            # Known to be up: Edge holds the profile, or (for an Edge that
            # keeps no lock file) a started process outlived the time a
            # window is given to appear.
            self._was_open = True
        return bool(alive) or held, starting and not alive and not held

    def is_open(self) -> bool:
        """Whether the app's window is open, or still on its way."""
        with self._lock:
            up, starting = self._look()
            dialog = self._dialog_thread is not None and self._dialog_thread.is_alive()
            return up or starting or dialog

    def failed_to_start(self) -> bool:
        """Edge was asked for a window and none ever came: no started process
        is alive, Edge never held the profile, and the time a window is given
        to appear has passed (a broken install, a policy that forbids it).
        :meth:`use_browser` follows. False for a window that was up and has
        been closed, and while one may still be coming."""
        with self._lock:
            if self._edge is None or self._asked_at is None:
                return False
            up, starting = self._look()
            return not up and not starting and not self._was_open

    def use_browser(self, url: str) -> None:
        """Give up on Edge for this run and show ``url`` the fallback way."""
        with self._lock:
            self._edge = None
            self._processes = []
        self._log("Edge showed no window: opening the page in the default browser instead.")
        self._show_in_browser(url)


def supervise(
    window: AppWindow,
    url: str,
    busy: Callable[[], bool],
    stop: Callable[[], None],
    *,
    poll_s: float = SUPERVISE_POLL_S,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = log_line,
) -> None:
    """Stop the server once its window is closed and nothing is running.

    ``busy`` is "a session is running or an experiment is being set up":
    closing the window must not cut either short, so the server stays up,
    hidden, until both are done, and is found again by the next click on the
    icon. ``stop`` is ``DashboardServer.shutdown``, which makes
    ``serve_forever`` return to the usual clean-up. Runs in its own thread.

    The reason for stopping goes to the log first (``log``): a server that
    stops is otherwise silent, and "why did the app go away" must have an
    answer on disk.
    """
    # Whether "closed, but still busy" has been said for this closing.
    said_busy = False
    while True:
        sleep(poll_s)
        if window.is_open():
            said_busy = False
            continue
        if window.failed_to_start():
            window.use_browser(url)
            continue
        if busy():
            if not said_busy:
                log(
                    "The window is closed, but a session or a setup is still running: "
                    "the app stays up until it has ended."
                )
                said_busy = True
            continue
        log("The window is closed and nothing is running: stopping.")
        stop()
        return


# -- the launcher ---------------------------------------------------------------------------


def default_directory() -> Path:
    """The workspace the app opens: the same one ``alhazen dashboard`` uses,
    so experiments registered either way show up in both."""
    return Path.home() / ".alhazen" / "dashboard"


def running_server(
    directory: Path, *, timeout: float = 2.0, opener: Callable[..., Any] = urllib.request.urlopen
) -> tuple[str, str] | None:
    """``(origin, token)`` of the server that has ``directory`` open and
    answers, or None. Read from the holder record the server writes beside
    its lock (``server.json``), then checked by asking it for the Hub page: a
    record left by a process that died names nobody, and must not stop a new
    server from starting."""
    try:
        record = json.loads((directory / HOLDER_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # No record, or one being written this instant: no server to talk to.
        return None
    url = record.get("url") if isinstance(record, dict) else None
    if not isinstance(url, str) or "#token=" not in url:
        return None
    origin, token = url.split("/#token=", 1)
    try:
        with opener(f"{origin}/hub", timeout=timeout) as response:
            if response.status != 200:
                return None
    except (OSError, ValueError):
        # Refused, timed out, or not HTTP: nothing is listening there now.
        return None
    return origin, token


def ask_for_window(
    origin: str, token: str, *, opener: Callable[..., Any] = urllib.request.urlopen
) -> bool:
    """Ask the running server to show its window; False when it is a plain
    ``alhazen dashboard`` (which has none) or cannot be reached."""
    request = urllib.request.Request(
        f"{origin}/api/app/window",
        data=b"{}",
        method="POST",
        headers={"X-Alhazen-Token": token, "Content-Type": "application/json"},
    )
    try:
        with opener(request, timeout=10) as response:
            return bool(response.status == 200)
    except (OSError, ValueError):
        return False


def server_command(directory: Path, executable: str | None = None) -> list[str]:
    """The command that starts the server process for ``directory``.

    The launcher runs under the windowless interpreter (``pythonw.exe``);
    the server needs the console one beside it (module docstring), so the
    name is swapped. ``-m alhazen`` rather than the ``alhazen`` script: it
    is the same interpreter and the same installed package by construction.
    """
    program = Path(executable if executable is not None else sys.executable)
    if program.name.lower() == "pythonw.exe":
        program = program.with_name("python.exe")
    return [str(program), "-m", "alhazen", "app", "--state-dir", str(directory)]


def start_server(directory: Path, *, popen: Callable[..., Any] = subprocess.Popen) -> Any:
    """Start the server hidden, its output going to the app's log file.

    The log of the run before is kept beside it (``PREVIOUS_LOG_NAME``): a
    click on the icon after something went wrong must not erase the only
    account of what went wrong.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LOG_NAME
    not_kept = ""
    # (A first start has no log to keep.)
    if path.exists():
        try:
            os.replace(path, directory / PREVIOUS_LOG_NAME)
        except OSError as exc:
            # Not a reason to refuse to start; said in the new log instead.
            not_kept = f"(The log of the run before could not be kept: {exc})\n"
    with path.open("w", encoding="utf-8") as log:
        log.write(not_kept)
        log.flush()
        return popen(
            server_command(directory),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            # Hidden console (inherited by the runs it starts), and a process
            # group of its own so nothing aimed at the launcher reaches it.
            creationflags=_NO_WINDOW | _NEW_GROUP,
        )


@contextmanager
def start_lock(directory: Path) -> Iterator[bool]:
    """Held by the one launcher that is starting the server; yields whether
    this launcher is that one.

    A server takes a few seconds to come up, and until it has there is
    nothing to tell a second click that one is on its way: without this the
    second launcher would start a second server, which loses the race for
    the workspace and overwrites the first one's log on its way out. A
    launcher that does not get the lock starts nothing and waits for the
    other's server. An operating-system lock, so a launcher that dies
    releases it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / START_LOCK_NAME).open("a+b") as handle:
        try:
            if sys.platform == "win32":
                import msvcrt

                # One byte to lock: Windows locks a range, and an empty file
                # has none.
                handle.write(b"\0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Refused: another launcher holds it. Not an error, an answer.
            yield False
            return
        try:
            yield True
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def wait_until_up(
    directory: Path,
    server: Any | None,
    *,
    timeout: float = START_TIMEOUT_S,
    is_up: Callable[[Path], Any] = running_server,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """Wait for the server just started to answer. None once a server for
    this workspace does; the reason, in words for the message box, when ours
    exits and no other comes up, or none ever answers. The reason ends with
    the tail of the server's log. ``server`` is None for a launcher that
    started none and waits for another launcher's (``start_lock``).

    "A server", not "ours": two clicks in a row start two servers, and the
    second one exits at once because the first holds the workspace. That is
    not a failure as long as the first comes up, so after ours has exited the
    wait goes on for a while (``OTHER_SERVER_GRACE_S``) before giving up.
    """
    deadline = clock() + timeout
    exited_at: float | None = None
    code = None
    while clock() < deadline:
        if is_up(directory) is not None:
            return None
        if server is None:
            pass
        elif exited_at is None:
            code = server.poll()
            if code is not None:
                exited_at = clock()
        elif clock() - exited_at >= OTHER_SERVER_GRACE_S:
            break
        sleep(0.2)
    if code is not None:
        return f"Alhazen could not start (exit code {code}).\n\n{_log_tail(directory)}"
    return (
        f"Alhazen did not start within {timeout:.0f} seconds. It may still be starting; "
        f"try the icon again in a moment.\n\n{_log_tail(directory)}"
    )


def _log_tail(directory: Path) -> str:
    path = directory / LOG_NAME
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return f"Its log ({path}) could not be read: {exc}"
    if not text:
        return f"Its log ({path}) is empty."
    return f"The end of its log ({path}):\n{text[-LOG_TAIL_CHARS:]}"


def report_problem(text: str) -> None:
    """Tell the person the app could not start: in a message box on Windows,
    where the launcher has no terminal, and on the terminal elsewhere."""
    if sys.platform == "win32":
        message_box(text, error=True)
    else:
        print(text, file=sys.stderr)


def launch(argv: list[str] | None = None) -> int:
    """The ``alhazen-app`` program: show the app, starting it if need be."""
    parser = argparse.ArgumentParser(prog="alhazen-app", description="Open the Alhazen app.")
    parser.add_argument("--state-dir", default=None, help="workspace folder (default: yours)")
    args = parser.parse_args(argv)
    directory = (
        Path(args.state_dir).expanduser().resolve() if args.state_dir else default_directory()
    )
    try:
        holder = running_server(directory)
        if holder is not None:
            origin, token = holder
            if not ask_for_window(origin, token):
                # A dashboard started from a terminal has the workspace: it
                # has no window of its own, so its page opens in the browser.
                webbrowser.open(f"{origin}/hub#token={token}")
            return 0
        with start_lock(directory) as starting:
            if not starting:
                # Another click got here first and is starting the server,
                # which opens its own window: only wait for it.
                problem = wait_until_up(directory, None)
            elif running_server(directory) is not None:
                # That other launcher finished between the look above and
                # the lock: its server is up and its window is open.
                problem = None
            else:
                problem = wait_until_up(directory, start_server(directory))
    except OSError as exc:
        problem = f"Alhazen could not start: {exc}"
    if problem is not None:
        report_problem(problem)
        return 1
    return 0
