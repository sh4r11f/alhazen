"""A console break must end a session the way Ctrl+C does: through teardown.

The workspace's **Stop run** reaches a Windows child with CTRL_BREAK_EVENT,
the one signal a parent can aim at a single process group. Python installs
no handler for the SIGBREAK it delivers, so without ours the child dies on
the spot (STATUS_CONTROL_C_EXIT): no ``except KeyboardInterrupt``, no
``finally``, no teardown — no trials file, no manifest, a stranded tracker
recording. These tests pin the fix at both ends: the handler itself, in a
real child on Windows, and the two entry points that must arm it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap

import pytest

from alhazen.cli.console_break import interrupt_on_console_break
from alhazen.cli.main import main


class TestHandler:
    def test_break_raises_keyboard_interrupt_where_there_is_sigbreak(self, monkeypatch):
        """Platform-independent: SIGBREAK is stubbed in, and the installed
        handler must raise the same exception Ctrl+C does."""
        installed = {}
        monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)
        monkeypatch.setattr(signal, "signal", lambda num, handler: installed.update({num: handler}))
        interrupt_on_console_break()
        assert list(installed) == [21]
        with pytest.raises(KeyboardInterrupt):
            installed[21](21, None)

    def test_no_op_without_sigbreak(self, monkeypatch):
        """POSIX has no console break; installing anything there would be a
        surprise for the signals a session does rely on."""
        monkeypatch.delattr(signal, "SIGBREAK", raising=False)
        monkeypatch.setattr(
            signal, "signal", lambda *args: pytest.fail("no handler belongs on this platform")
        )
        interrupt_on_console_break()

    @pytest.mark.skipif(
        os.name != "nt", reason="CTRL_BREAK_EVENT and SIGBREAK exist only on Windows"
    )
    def test_a_real_console_break_reaches_the_except_clause(self, tmp_path):
        """The reported failure, end to end: a child in its own process group
        (as the workspace starts them) gets CTRL_BREAK_EVENT and must reach
        its ``except KeyboardInterrupt`` and exit 0, not die with 0xC000013A.

        The child polls in short sleeps because a Python signal handler runs
        between statements: a single long ``sleep`` would defer it, which is
        a property of the platform, not of the handler.
        """
        marker = tmp_path / "marker.txt"
        child = tmp_path / "child.py"
        child.write_text(
            textwrap.dedent(
                """
                import sys, time
                from pathlib import Path
                from alhazen.cli.console_break import interrupt_on_console_break

                interrupt_on_console_break()
                marker = Path(sys.argv[1])
                try:
                    marker.write_text("started")
                    print("started", flush=True)
                    while True:
                        time.sleep(0.05)
                except KeyboardInterrupt:
                    marker.write_text("KeyboardInterrupt: teardown ran")
                """
            ),
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, str(child), str(marker)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        try:
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "started"
            process.send_signal(signal.CTRL_BREAK_EVENT)
            output, _ = process.communicate(timeout=20)
        finally:
            process.kill()
        assert marker.read_text() == "KeyboardInterrupt: teardown ran", output
        assert process.returncode == 0, (process.returncode, output)


class TestEntryPoints:
    def test_run_session_arms_the_handler_before_anything_else(self, monkeypatch):
        """Every ``alhazen run`` and every experiment's ``run.py`` goes through
        ``_run_session``; the cheapest exit (a refused flag) is enough to show
        the handler is armed first, before a rig or a task is touched."""
        armed = []
        # `alhazen.cli.main` the attribute is the re-exported function; the
        # module is reached through sys.modules, as test_cli_modes does.
        monkeypatch.setattr(
            sys.modules["alhazen.cli.main"], "interrupt_on_console_break", lambda: armed.append(1)
        )
        assert main(["run", "--mode", "run", "--headless", "--rig", "unused.yaml"]) == 2
        assert armed == [1]
