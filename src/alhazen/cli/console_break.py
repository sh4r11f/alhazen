"""Make a Windows console break end a session the way Ctrl+C does.

The workspace's **Stop run** has to interrupt one child without touching the
launcher itself. On POSIX that is SIGINT to the child's process group. On
Windows the only signal a parent can aim at a single process group is
CTRL_BREAK_EVENT (CTRL_C_EVENT reaches every process on the console), so the
child is started in its own group and sent a console break.

Python installs no handler for the SIGBREAK a console break delivers, so the
default action runs: the process ends on the spot with STATUS_CONTROL_C_EXIT
(0xC000013A). No ``except KeyboardInterrupt`` runs, no ``finally`` runs — the
session runner's teardown never happens, so the trials file the recorder
writes at teardown, the manifest and the tracker's recording are all left
stranded, and the run's history shows a clean "cancelled". The rigs are
Windows machines, so this is the common case, not a corner.

``interrupt_on_console_break`` turns the break into the ``KeyboardInterrupt``
Ctrl+C raises, which the runner's teardown already survives: every step
runs and the interrupt is re-raised once they are done. Both entry points a
session can start from arm it (``alhazen run`` and every experiment's
``run.py`` share ``_run_session``), and so does the workspace server, which
is stopped the same way.
"""

from __future__ import annotations

import signal


def interrupt_on_console_break() -> None:
    """Raise ``KeyboardInterrupt`` on a console break; a no-op without SIGBREAK.

    Must be called on the main thread: Python delivers signals only there and
    ``signal.signal`` refuses any other, loudly. Like every Python signal
    handler this one runs between two statements, so a blocking C call that
    has not returned yet (a bare ``time.sleep(30)``, a ``Popen.wait()``)
    delays it until it does — unlike Ctrl+C on Windows, which also wakes
    ``sleep``. A session's frame loop is Python statements, so the break lands
    within a frame; the workspace's grace period before a forced kill is
    generous partly for the waits that are not.
    """
    # Looked up by name rather than attribute-tested by platform: typeshed
    # only declares SIGBREAK for win32, and mypy runs on Linux in CI too.
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is None:
        return

    def raise_interrupt(signum: int, frame: object) -> None:
        # Exactly what the default SIGINT handler does, so everything written
        # to survive Ctrl+C (`except KeyboardInterrupt`, `finally`, the
        # runner's step-by-step teardown) survives a console break unchanged.
        raise KeyboardInterrupt

    signal.signal(sigbreak, raise_interrupt)
