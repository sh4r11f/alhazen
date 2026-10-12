"""A Python environment for an installed release, built from the release's own lock file.

Secret it hides: how the tools that build an environment are found (``uv``,
and ``git`` when the lock file takes a package from a git repository), which
Python and which optional dependencies are asked for, the exact command, and
where environments live. A caller gives a release folder and gets back the
interpreter that runs it, or a :class:`SetupError` that says what is missing
in words an operator can act on.

Why it exists: an installed release still needs an interpreter that has the
experiment's dependencies (alhazen at the pinned version, PsychoPy, and so
on). Without this module the operator builds that by hand and points the
install at it. The experiment's author already recorded the exact versions in
``uv.lock``, so the rig can reproduce them without anyone typing a command.

How an environment is built:

1. :func:`read_plan` reads the release's ``pyproject.toml``, ``uv.lock`` and
   ``.python-version`` and decides the Python version, the extras and whether
   git is needed. It runs no code from the release and touches no network.
2. :meth:`Environments.build` runs ``uv sync --frozen`` for the release with
   the environment placed OUTSIDE the release folder
   (``<workspace>/hub/envs/<sha12>``), so the release's declared files stay
   exactly the published ones. ``--frozen`` means the lock file is used as it
   is: nothing is resolved again on the rig, so two rigs get the same
   versions.
3. The experiment itself is installed into that environment as an editable
   package, which is what registers its tasks (the ``alhazen.tasks`` entry
   points) while its code keeps being imported from the release folder.

What callers must NOT rely on: any safety property. Building an environment
runs the experiment's build backend and installs the packages its lock file
names; it is as much "running the release's code" as a launch is, so it comes
after the trust step (``InstallStore.trusted_record``), never before.

Price: one environment per release (uv hard-links packages from its cache, so
a second release of the same experiment costs little disk), and the build
backend may leave its metadata folder (``src/<name>.egg-info``) in the
release folder; that folder is not one of the release's declared files and no
check counts it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

# The files of a release this module reads. All three are ordinary package
# files: the author's project description, the versions they locked, and the
# Python version they developed on.
PROJECT_FILE = "pyproject.toml"
LOCK_FILE = "uv.lock"
PYTHON_PIN_FILE = ".python-version"

# The extras an experiment may define for running on a rig, most complete
# first. ``rig`` is an experiment's own "everything this rig needs" (a real
# window plus its hardware packages); ``psychopy`` is the real window alone.
# The first one the project defines is installed; an experiment that defines
# neither gets its plain dependencies (enough to simulate).
RIG_EXTRAS = ("rig", "psychopy")

# A Python request this module passes on: MAJOR, MAJOR.MINOR or
# MAJOR.MINOR.PATCH. Anything else in ``.python-version`` (an implementation
# name, a path) is refused by name rather than guessed at.
_PYTHON_VERSION = re.compile(r"\d{1,2}(?:\.\d{1,3}){0,2}")
# A package or extra name as the packaging standard spells it (PEP 508).
_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?")
# How much of a failed build's output goes into the error message. The whole
# output is in the build's log file; the end is where uv names the cause.
_ERROR_TAIL_CHARS = 1500
# CREATE_NO_WINDOW: a console program started from the windowless app must
# not open a console window of its own (0 where the flag does not exist).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SetupError(ValueError):
    """An environment that cannot be built, with a code the page keys on.

    Codes: ``setup_unavailable`` (this computer or this release lacks what an
    automatic build needs), ``git_missing`` (the lock file takes a package
    from git and no git is installed), ``setup_failed`` (uv ran and reported
    an error) and ``setup_cancelled`` (the app was closing)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Tools:
    """The programs a build runs: ``uv`` always, ``git`` only when the lock
    file names a git source (None when no git was found)."""

    uv: Path
    git: Path | None


@dataclass(frozen=True)
class Plan:
    """What one release's environment is built from, decided without running
    anything: the Python version to ask uv for, the extras to install, and
    the names of the packages its lock file takes from git."""

    python: str
    extras: tuple[str, ...]
    git_packages: tuple[str, ...]


def app_tools_folder(prefix: str | None = None) -> Path:
    """Where the installed Windows app keeps its own copies of uv and git:
    a ``tools`` folder beside the app's Python environment. The installer
    lays the app out as ``<home>/app`` (that environment) and
    ``<home>/tools``; ``prefix`` is the environment's root (``sys.prefix``)."""
    return Path(prefix if prefix is not None else sys.prefix).parent / "tools"


def find_tools(
    *,
    prefix: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Tools:
    """The uv and git this computer has, the app's own copies first.

    The app's copies come first so that an installed app behaves the same on
    every computer, whatever else is on its PATH. A computer without the app
    (a developer's checkout) uses the uv and git on its PATH. No uv at all is
    a :class:`SetupError`; no git is not, because most lock files need none.
    """
    folder = app_tools_folder(prefix)
    executable = ".exe" if sys.platform == "win32" else ""
    bundled_uv = folder / f"uv{executable}"
    # MinGit, the distribution of Git for Windows made for bundling, keeps
    # its launcher in ``cmd``.
    bundled_git = folder / "git" / "cmd" / f"git{executable}"
    found_uv = bundled_uv if bundled_uv.is_file() else _path_or_none(which("uv"))
    if found_uv is None:
        raise SetupError(
            "setup_unavailable",
            "This computer has no uv, the tool that builds an experiment's Python "
            "environment. Install the Alhazen app (it brings its own), or install uv "
            "from https://docs.astral.sh/uv/, or choose a Python interpreter yourself.",
        )
    found_git = bundled_git if bundled_git.is_file() else _path_or_none(which("git"))
    return Tools(uv=found_uv, git=found_git)


def _path_or_none(found: str | None) -> Path | None:
    return Path(found) if found else None


def read_plan(release: Path, *, python_min: str = "") -> Plan:
    """Decide how ``release``'s environment is built, from its own files.

    ``python_min`` is the release manifest's oldest supported Python, used
    when the release ships no ``.python-version``: the author declared the
    experiment works there, so it is always a valid choice.

    Raises :class:`SetupError` (``setup_unavailable``) when the release has
    no ``pyproject.toml`` or no ``uv.lock``: without the lock file the rig
    would have to pick versions itself, and two rigs would no longer run the
    same thing. The operator can still choose an interpreter by hand.
    """
    project = _read_toml(
        release / PROJECT_FILE,
        missing="This release has no pyproject.toml, so its dependencies are not known. "
        "Choose a Python interpreter that already has them.",
    )
    lock = _read_toml(
        release / LOCK_FILE,
        missing="This release was packaged without its lock file (uv.lock), so the exact "
        "versions it needs are not known. Choose a Python interpreter that already has "
        "its dependencies, or ask the author to pack a release that includes uv.lock.",
    )
    return Plan(
        python=_python_request(release, python_min),
        extras=_rig_extras(project),
        git_packages=_git_packages(lock),
    )


def _read_toml(path: Path, *, missing: str) -> dict[str, Any]:
    """Parse one of the release's TOML files; a missing file gets the given
    explanation, an unreadable one names the file and the parser's reason."""
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError as exc:
        raise SetupError("setup_unavailable", missing) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SetupError(
            "setup_unavailable", f"The release's {path.name} cannot be read: {exc}"
        ) from exc


def _python_request(release: Path, python_min: str) -> str:
    """The Python version to ask uv for: the release's ``.python-version``
    when it ships one (the version the author developed and tested on),
    otherwise the manifest's oldest supported version."""
    pin = release / PYTHON_PIN_FILE
    try:
        lines = pin.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except (OSError, UnicodeDecodeError) as exc:
        raise SetupError(
            "setup_unavailable", f"The release's {PYTHON_PIN_FILE} cannot be read: {exc}"
        ) from exc
    # The file's first line that is not blank or a comment is the request
    # (the format pyenv and uv share).
    wanted = next(
        (line.strip() for line in lines if line.strip() and not line.strip().startswith("#")),
        python_min.strip(),
    )
    if not _PYTHON_VERSION.fullmatch(wanted):
        source = PYTHON_PIN_FILE if lines else "manifest"
        raise SetupError(
            "setup_unavailable",
            f"The release's {source} asks for Python {wanted!r}, which is not a version "
            "number such as 3.11. Choose a Python interpreter yourself.",
        )
    return wanted


def _rig_extras(project: dict[str, Any]) -> tuple[str, ...]:
    """The extra to install for running on a rig: the first of
    :data:`RIG_EXTRAS` the project defines, or none."""
    table = project.get("project")
    declared = table.get("optional-dependencies") if isinstance(table, dict) else None
    if not isinstance(declared, dict):
        return ()
    # Extra names compare without regard to case or to "-", "_" and "."
    # (the packaging standard's normalisation), so "PsychoPy" counts.
    by_normal = {re.sub(r"[-_.]+", "-", str(name)).lower(): str(name) for name in declared}
    for wanted in RIG_EXTRAS:
        name = by_normal.get(wanted)
        if name is not None and _NAME.fullmatch(name):
            return (name,)
    return ()


def _git_packages(lock: dict[str, Any]) -> tuple[str, ...]:
    """Names of the packages the lock file takes from a git repository
    (``source = { git = "..." }``), sorted. uv needs git to fetch them."""
    packages = lock.get("package")
    if not isinstance(packages, list):
        return ()
    names = {
        str(package.get("name", "?"))
        for package in packages
        if isinstance(package, dict)
        and isinstance(package.get("source"), dict)
        and "git" in package["source"]
    }
    return tuple(sorted(names))


def require_git(plan: Plan, tools: Tools) -> None:
    """Refuse (``git_missing``) a plan whose lock file takes a package from
    git on a computer without git, naming the packages. uv would fail on the
    same thing minutes into the build; this says it before the build starts."""
    if plan.git_packages and tools.git is None:
        raise SetupError(
            "git_missing",
            "This experiment takes "
            + ", ".join(plan.git_packages)
            + " from a git repository, and this computer has no Git. Install the "
            "Alhazen app (it brings its own), or install Git from "
            "https://git-scm.com/, then set the experiment up again.",
        )


def build_command(tools: Tools, plan: Plan, release: Path) -> list[str]:
    """The uv command that builds ``plan`` for ``release``.

    ``--frozen``: use the lock file exactly as published; never resolve
    again and never rewrite it. ``--no-dev``: the author's test and lint
    tools are not needed to run a session. The environment's location is not
    here: uv takes it from ``UV_PROJECT_ENVIRONMENT`` (:func:`build_environ`).
    """
    command = [
        str(tools.uv),
        "sync",
        "--frozen",
        "--no-dev",
        "--python",
        plan.python,
        "--project",
        str(release),
    ]
    for extra in plan.extras:
        command += ["--extra", extra]
    return command


def build_environ(base: Mapping[str, str], tools: Tools, environment: Path) -> dict[str, str]:
    """The process environment a build runs in, from the app's own (``base``).

    * ``UV_PROJECT_ENVIRONMENT`` puts the environment outside the release
      folder (uv's default is ``.venv`` inside the project).
    * ``UV_PYTHON_PREFERENCE=only-managed`` makes uv use a Python it
      downloads and manages itself, so the result does not depend on which
      Pythons happen to be installed on this computer.
    * ``VIRTUAL_ENV`` is dropped: it names the APP's environment, and uv
      would warn that it is not the project's.
    * git's folder goes first on ``PATH`` so uv finds the app's own git.
    * ``NO_COLOR`` keeps colour codes out of the log and the page.
    """
    env = dict(base)
    env.pop("VIRTUAL_ENV", None)
    env["UV_PROJECT_ENVIRONMENT"] = str(environment)
    env["UV_PYTHON_PREFERENCE"] = "only-managed"
    env["NO_COLOR"] = "1"
    if tools.git is not None:
        env["PATH"] = os.pathsep.join([str(tools.git.parent), env.get("PATH", "")])
    return env


def interpreter_of(environment: Path) -> Path:
    """The Python executable of an environment uv built at ``environment``."""
    if sys.platform == "win32":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


class Environments:
    """The environments of one workspace's installed releases.

    ``directory`` is the workspace's ``hub/`` folder; environments go under
    its ``envs/`` as ``<sha12>`` (the first twelve digits of the release's
    SHA-256, the same key the release folder's name ends with) with the
    build's full output beside each as ``<sha12>.log``.
    """

    def __init__(self, directory: Path) -> None:
        self.root = directory / "envs"
        # The uv processes now running, by release digest, so that closing
        # the app can stop them instead of leaving them behind.
        self._running: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()
        self._closing = False

    def folder(self, sha256: str) -> Path:
        return self.root / sha256[:12]

    def log_file(self, sha256: str) -> Path:
        return self.root / f"{sha256[:12]}.log"

    def build(
        self,
        release: Path,
        sha256: str,
        plan: Plan,
        tools: Tools,
        *,
        on_output: Callable[[str], None] = lambda line: None,
        environ: Mapping[str, str] | None = None,
    ) -> Path:
        """Build (or finish building) the environment of the release at
        ``release`` and return its Python interpreter.

        Safe to call again after a failure or an interruption: uv completes
        or repairs what is there, so nothing is deleted here. Each line uv
        prints goes to ``on_output`` (for the page's progress line) and to
        the log file. Raises :class:`SetupError`; a failure's message ends
        with the last lines of uv's output.
        """
        require_git(plan, tools)
        environment = self.folder(sha256)
        log_path = self.log_file(sha256)
        self.root.mkdir(parents=True, exist_ok=True)
        command = build_command(tools, plan, release)
        env = build_environ(os.environ if environ is None else environ, tools, environment)
        tail: list[str] = []
        with log_path.open("w", encoding="utf-8") as log:
            log.write("$ " + " ".join(command) + "\n")
            log.flush()
            try:
                process = subprocess.Popen(
                    command,
                    cwd=release,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    # One stream: uv reports progress and errors on stderr,
                    # and the order between the two matters for the log.
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=_NO_WINDOW,
                )
            except OSError as exc:
                raise SetupError("setup_failed", f"Could not start {tools.uv}: {exc}") from exc
            with self._lock:
                self._running[sha256] = process
            try:
                assert process.stdout is not None  # stdout=PIPE above
                for raw in process.stdout:
                    line = raw.rstrip()
                    log.write(line + "\n")
                    log.flush()
                    if line.strip():
                        tail.append(line)
                        # Only the end of the output is ever shown.
                        del tail[:-40]
                        on_output(line.strip())
                code = process.wait()
            finally:
                with self._lock:
                    self._running.pop(sha256, None)
            log.write(f"[exit code {code}]\n")
        if self._closing:
            raise SetupError("setup_cancelled", "Setting up stopped because the app was closing.")
        if code != 0:
            shown = "\n".join(tail)[-_ERROR_TAIL_CHARS:]
            raise SetupError(
                "setup_failed",
                f"Building the experiment's Python environment failed (uv exit code {code}). "
                f"The end of its output:\n{shown}",
            )
        python = interpreter_of(environment)
        if not python.is_file():
            # uv reported success but the interpreter is not where an
            # environment keeps it: say so now, not at the first launch.
            raise SetupError(
                "setup_failed",
                "uv reported success, but the environment has no Python interpreter at "
                f"{python.name}; its full output is in the build's log.",
            )
        return python

    def close(self) -> None:
        """Stop every build still running (the app is closing). A build
        stopped here raises ``setup_cancelled`` in its own thread."""
        with self._lock:
            self._closing = True
            running = list(self._running.values())
        for process in running:
            process.terminate()
