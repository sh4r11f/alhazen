"""A stand-in for uv, shared by the tests of automatic setup.

A real build downloads a Python and hundreds of megabytes, so the tests run a
small Python program in uv's place. It does what the code under test relies
on uv for and nothing else: it prints lines, and on success it leaves a file
where the environment's interpreter belongs. ``FAKE_UV`` picks its behaviour:

* ``ok`` (the default): prints two lines and creates the interpreter file;
* ``fail``: prints an error on both streams and exits with code 2;
* ``hang``: prints one line, then sleeps for two minutes (to be stopped);
* ``no-python``: exits with code 0 without creating the interpreter.
"""

from __future__ import annotations

import os
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

from alhazen.hub import environments

FAKE_UV = textwrap.dedent(
    """
    import os, sys, time
    from pathlib import Path

    mode = os.environ.get("FAKE_UV", "ok")
    environment = Path(os.environ["UV_PROJECT_ENVIRONMENT"])
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    print("Using CPython 3.11.9", flush=True)
    if mode == "hang":
        print("Downloading psychopy (64.0MiB)", flush=True)
        time.sleep(120)
    if mode == "fail":
        print("error: Failed to fetch `https://pypi.org/simple/psychopy/`", flush=True)
        print("  Caused by: connection refused", file=sys.stderr, flush=True)
        sys.exit(2)
    if mode != "no-python":
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"")
    print("", flush=True)
    print("Installed 3 packages in 12ms", flush=True)
    """
)


def fake_uv(tmp_path: Path, monkeypatch) -> Callable[[str], dict[str, str]]:
    """Make every build run the stand-in, started by this test's own Python.

    Only the program is replaced: the arguments uv would have got still
    follow it, and the process environment is the one the code built. Being
    a direct child (no shell or wrapper in between) is what lets a test stop
    it the way the app stops uv. Returns ``environ(mode)``: this process's
    environment with ``FAKE_UV`` set, for a caller that passes one in.
    """
    script = tmp_path / "fake_uv.py"
    script.write_text(FAKE_UV, encoding="utf-8")
    real = environments.build_command

    def command(tools, plan, release):
        return [sys.executable, str(script), *real(tools, plan, release)[1:]]

    monkeypatch.setattr(environments, "build_command", command)

    def environ(mode: str) -> dict[str, str]:
        return {**os.environ, "FAKE_UV": mode}

    return environ
