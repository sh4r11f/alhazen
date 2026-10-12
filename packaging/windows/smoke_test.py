"""Check an installed Alhazen app, run by the app's OWN Python:

    "%LOCALAPPDATA%\\Alhazen\\app\\Scripts\\python.exe" packaging\\windows\\smoke_test.py

It tests the claim the app makes: on a computer with nothing else installed,
an experiment can be set up and the app can serve its pages. Three checks:

1. The app finds its own uv and git, in its ``tools`` folder, not whatever
   the computer's PATH happens to hold.
2. With uv, git and Python removed from PATH, it builds the environment of an
   experiment whose lock file takes alhazen from a git tag, as the real
   experiments do. The environment must then run that pinned alhazen and list
   the experiment's task.
3. The dashboard starts from the installed files and serves the Hub page.

Needs a network connection (the build downloads Python and packages). Exits
0 when all three hold; otherwise the failing check raises and says why.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from alhazen.hub.environments import (
    Environments,
    Plan,
    app_tools_folder,
    build_environ,
    find_tools,
    read_plan,
)

# The alhazen release the test experiment pins, as a git tag: what the lock
# file of a real experiment looks like. Any published tag would do.
PINNED_TAG = "v2.15.0"
PINNED_VERSION = "2.15.0"
SHA = "50" * 32  # a stand-in release digest; only names the environment folder

PROJECT = f"""\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "smoke-task"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["alhazen-vision"]

[project.optional-dependencies]
# Defined and empty: the app installs the `psychopy` extra when a project has
# one, and PsychoPy itself would add ten minutes to this check.
psychopy = []

[project.entry-points."alhazen.tasks"]
smoke-task = "smoke_task:Task"

[tool.uv.sources]
alhazen-vision = {{ git = "https://github.com/sh4r11f/alhazen", tag = "{PINNED_TAG}" }}

[tool.setuptools.packages.find]
where = ["src"]
"""


def step(text: str) -> None:
    print(f"\n== {text}", flush=True)


def write_release(root: Path) -> None:
    (root / "src" / "smoke_task").mkdir(parents=True)
    (root / "src" / "smoke_task" / "__init__.py").write_text("class Task: ...\n", encoding="utf-8")
    (root / "run.py").write_text("print('run')\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(PROJECT, encoding="utf-8")
    (root / ".python-version").write_text("3.12\n", encoding="utf-8")


def bare_path() -> str:
    """A PATH with Windows itself and nothing a developer would have added:
    no uv, no git, no Python."""
    windows = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return os.pathsep.join([str(Path(windows) / "System32"), windows])


def check_tools() -> None:
    step("1. the app's own uv and git are the ones it uses")
    tools = find_tools()
    folder = app_tools_folder()
    print(f"uv:  {tools.uv}\ngit: {tools.git}")
    assert tools.uv == folder / "uv.exe", f"uv is not the app's own: {tools.uv}"
    assert tools.git == folder / "git" / "cmd" / "git.exe", f"git is not the app's own: {tools.git}"


def check_setup(work: Path) -> None:
    step("2. an experiment that takes alhazen from git is set up with a bare PATH")
    tools = find_tools()
    release = work / "release"
    write_release(release)
    bare = {**os.environ, "PATH": bare_path()}
    # The author's step, done here with the app's own tools: lock the project.
    # (An author would run `uv lock`; the result is the uv.lock a release ships.)
    locking = build_environ(bare, tools, work / "unused")
    subprocess.run([str(tools.uv), "lock", "--project", str(release)], check=True, env=locking)
    plan = read_plan(release, python_min="3.10")
    print(f"plan: {plan}")
    assert plan == Plan(python="3.12", extras=("psychopy",), git_packages=("alhazen-vision",))

    started = time.monotonic()
    python = Environments(work / "hub").build(
        release, SHA, plan, tools, on_output=lambda line: print(f"  uv: {line}"), environ=bare
    )
    print(f"built in {time.monotonic() - started:.0f} s: {python}")
    report = subprocess.run(
        [
            str(python),
            "-c",
            "import json, sys, importlib.metadata as m, alhazen, smoke_task;"
            "print(json.dumps({'alhazen': alhazen.__version__,"
            " 'python': list(sys.version_info[:2]),"
            " 'tasks': sorted(e.name for e in m.entry_points(group='alhazen.tasks')),"
            " 'experiment': smoke_task.__file__}))",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=bare,
    )
    seen = json.loads(report.stdout.strip().splitlines()[-1])
    print(f"the environment reports: {seen}")
    assert seen["alhazen"] == PINNED_VERSION, seen
    assert seen["python"] == [3, 12], seen
    assert "smoke-task" in seen["tasks"], seen
    # Imported from the release folder, not from a copy in the environment.
    # Compared as files, not as text: Windows may spell the temporary folder
    # with a short name (RUNNER~1) in one place and its long name in another.
    assert os.path.samefile(seen["experiment"], release / "src" / "smoke_task" / "__init__.py"), (
        seen
    )


def check_dashboard(work: Path) -> None:
    step("3. the dashboard starts from the installed files and serves the Hub page")
    state = work / "state"
    server = subprocess.Popen(
        [sys.executable, "-m", "alhazen", "dashboard", "--hub", "--no-browser"]
        + ["--state-dir", str(state)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        deadline = time.monotonic() + 120
        url = None
        while time.monotonic() < deadline and url is None:
            assert server.poll() is None, f"the dashboard exited with code {server.returncode}"
            try:
                url = json.loads((state / "server.json").read_text(encoding="utf-8")).get("url")
            except (OSError, ValueError):
                # Not written yet, or being written: look again.
                time.sleep(0.5)
        assert url, "the dashboard did not write its address within 120 s"
        origin = url.split("/#token=", 1)[0]
        with urllib.request.urlopen(f"{origin}/hub", timeout=10) as response:
            page = response.read().decode("utf-8")
        assert response.status == 200 and "Alhazen Experiment Hub" in page
        print(f"served {origin}/hub ({len(page)} characters)")
    finally:
        server.terminate()
        server.wait(30)


def main() -> int:
    print(f"Python: {sys.executable}")
    with tempfile.TemporaryDirectory(prefix="alhazen-smoke-") as folder:
        work = Path(folder)
        check_tools()
        check_setup(work)
        check_dashboard(work)
    print("\nAll three checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
