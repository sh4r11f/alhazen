"""alhazen.hub.environments: the plan read from a release's own files, the
uv command, the tools found on this computer, and the build's handling of a
uv that succeeds, fails, is stopped or lies. uv itself is stood in for by a
small Python program (``FAKE_UV``), because a real build downloads a Python
and hundreds of megabytes; one opt-in test at the end runs the real uv."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from tests.hub.setup_support import fake_uv

from alhazen.hub.environments import (
    Environments,
    Plan,
    SetupError,
    Tools,
    build_command,
    build_environ,
    find_tools,
    interpreter_of,
    read_plan,
    require_git,
)

SHA = "ab12cd34ef56" + "0" * 52

PYPROJECT = """\
[project]
name = "demo-task"
version = "1.0.0"
dependencies = ["alhazen-vision>=2.14.0"]
{extras}
"""

LOCK = """\
version = 1
requires-python = ">=3.10"

[[package]]
name = "alhazen-vision"
version = "2.15.0"
source = {{ {alhazen} }}

[[package]]
name = "numpy"
version = "2.2.0"
source = {{ registry = "https://pypi.org/simple" }}
{more}
"""

FROM_GIT = 'git = "https://github.com/sh4r11f/alhazen?tag=v2.15.0#0c1d899"'
FROM_INDEX = 'registry = "https://pypi.org/simple"'


def release(tmp_path, *, extras="", alhazen=FROM_INDEX, more="", pin="3.11\n", lock=True):
    """A release folder with the files a plan is read from."""
    root = tmp_path / "release"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(PYPROJECT.format(extras=extras), encoding="utf-8")
    if lock:
        (root / "uv.lock").write_text(LOCK.format(alhazen=alhazen, more=more), encoding="utf-8")
    if pin is not None:
        (root / ".python-version").write_text(pin, encoding="utf-8")
    return root


# -- the plan ----------------------------------------------------------------------------


class TestPlan:
    def test_the_python_is_the_one_the_release_pins(self, tmp_path):
        assert read_plan(release(tmp_path), python_min="3.10").python == "3.11"

    def test_comments_and_blank_lines_in_the_pin_are_skipped(self, tmp_path):
        root = release(tmp_path, pin="# developed on\n\n  3.12.4  \n")
        assert read_plan(root, python_min="3.10").python == "3.12.4"

    def test_without_a_pin_it_is_the_manifests_oldest_python(self, tmp_path):
        assert read_plan(release(tmp_path, pin=None), python_min="3.10").python == "3.10"

    @pytest.mark.parametrize("pin", ["pypy@3.10\n", "--system\n", "C:/Python311/python.exe\n"])
    def test_a_pin_that_is_not_a_version_number_is_refused_by_name(self, tmp_path, pin):
        with pytest.raises(SetupError) as refusal:
            read_plan(release(tmp_path, pin=pin), python_min="3.10")
        assert refusal.value.code == "setup_unavailable"
        assert pin.strip() in refusal.value.message and ".python-version" in refusal.value.message

    def test_no_pin_and_no_manifest_version_is_refused(self, tmp_path):
        with pytest.raises(SetupError) as refusal:
            read_plan(release(tmp_path, pin=None), python_min="")
        assert refusal.value.code == "setup_unavailable" and "manifest" in refusal.value.message

    def test_the_rig_extra_wins_over_the_psychopy_extra(self, tmp_path):
        extras = textwrap.dedent(
            """
            [project.optional-dependencies]
            movie = ["alhazen-vision[movie]"]
            psychopy = ["alhazen-vision[psychopy]"]
            rig = ["demo-task[psychopy]", "nidaqmx"]
            """
        )
        assert read_plan(release(tmp_path, extras=extras)).extras == ("rig",)

    def test_the_psychopy_extra_is_found_however_it_is_spelled(self, tmp_path):
        extras = '\n[project.optional-dependencies]\nPsychoPy = ["alhazen-vision[psychopy]"]\n'
        # The name goes to uv as the project wrote it.
        assert read_plan(release(tmp_path, extras=extras)).extras == ("PsychoPy",)

    def test_a_project_with_neither_extra_gets_none(self, tmp_path):
        extras = '\n[project.optional-dependencies]\nmovie = ["alhazen-vision[movie]"]\n'
        assert read_plan(release(tmp_path, extras=extras)).extras == ()
        assert read_plan(release(tmp_path / "plain")).extras == ()

    def test_packages_taken_from_git_are_named(self, tmp_path):
        more = textwrap.dedent(
            """
            [[package]]
            name = "rdk-generator"
            version = "0.3.0"
            source = { git = "https://github.com/sh4r11f/rdk-generator?rev=fb699e6#fb699e6" }
            """
        )
        plan = read_plan(release(tmp_path, alhazen=FROM_GIT, more=more))
        assert plan.git_packages == ("alhazen-vision", "rdk-generator")
        assert read_plan(release(tmp_path / "index")).git_packages == ()

    def test_a_release_without_a_lock_file_cannot_be_set_up(self, tmp_path):
        with pytest.raises(SetupError) as refusal:
            read_plan(release(tmp_path, lock=False), python_min="3.10")
        assert refusal.value.code == "setup_unavailable"
        assert "uv.lock" in refusal.value.message and "interpreter" in refusal.value.message

    def test_a_release_without_a_pyproject_cannot_be_set_up(self, tmp_path):
        root = release(tmp_path)
        (root / "pyproject.toml").unlink()
        with pytest.raises(SetupError) as refusal:
            read_plan(root, python_min="3.10")
        assert refusal.value.code == "setup_unavailable"
        assert "pyproject.toml" in refusal.value.message

    def test_an_unreadable_lock_file_names_itself(self, tmp_path):
        root = release(tmp_path)
        (root / "uv.lock").write_text("version = = 1\n", encoding="utf-8")
        with pytest.raises(SetupError) as refusal:
            read_plan(root, python_min="3.10")
        assert "uv.lock cannot be read" in refusal.value.message


# -- the tools ----------------------------------------------------------------------------


def app_layout(tmp_path, *, uv=True, git=True):
    """An installed app's folders: ``<home>/app`` and ``<home>/tools``."""
    suffix = ".exe" if sys.platform == "win32" else ""
    home = tmp_path / "Alhazen"
    (home / "app").mkdir(parents=True)
    (home / "tools" / "git" / "cmd").mkdir(parents=True)
    if uv:
        (home / "tools" / f"uv{suffix}").write_bytes(b"")
    if git:
        (home / "tools" / "git" / "cmd" / f"git{suffix}").write_bytes(b"")
    return home, suffix


class TestTools:
    def test_the_apps_own_copies_come_before_the_path(self, tmp_path):
        home, suffix = app_layout(tmp_path)
        tools = find_tools(prefix=str(home / "app"), which=lambda name: f"/usr/bin/{name}")
        assert tools.uv == home / "tools" / f"uv{suffix}"
        assert tools.git == home / "tools" / "git" / "cmd" / f"git{suffix}"

    def test_without_the_app_the_path_is_used(self, tmp_path):
        tools = find_tools(prefix=str(tmp_path / "venv"), which=lambda name: f"/usr/bin/{name}")
        assert tools == Tools(uv=Path("/usr/bin/uv"), git=Path("/usr/bin/git"))

    def test_each_tool_is_looked_for_on_its_own(self, tmp_path):
        home, suffix = app_layout(tmp_path, git=False)
        tools = find_tools(prefix=str(home / "app"), which=lambda name: f"/usr/bin/{name}")
        assert tools.uv == home / "tools" / f"uv{suffix}" and tools.git == Path("/usr/bin/git")

    def test_no_git_is_not_an_error(self, tmp_path):
        found = {"uv": "/usr/bin/uv"}
        assert find_tools(prefix=str(tmp_path / "venv"), which=found.get).git is None

    def test_no_uv_says_what_to_install(self, tmp_path):
        with pytest.raises(SetupError) as refusal:
            find_tools(prefix=str(tmp_path / "venv"), which=lambda name: None)
        assert refusal.value.code == "setup_unavailable"
        assert "uv" in refusal.value.message and "Alhazen app" in refusal.value.message

    def test_a_git_source_without_git_is_refused_naming_the_packages(self):
        plan = Plan(python="3.11", extras=(), git_packages=("alhazen-vision", "rdk-generator"))
        with pytest.raises(SetupError) as refusal:
            require_git(plan, Tools(uv=Path("uv"), git=None))
        assert refusal.value.code == "git_missing"
        assert "alhazen-vision, rdk-generator" in refusal.value.message
        # With git, or with nothing from git, there is nothing to refuse.
        require_git(plan, Tools(uv=Path("uv"), git=Path("git")))
        require_git(Plan("3.11", (), ()), Tools(uv=Path("uv"), git=None))


# -- the command and its environment -------------------------------------------------------


class TestCommand:
    TOOLS = Tools(uv=Path("tools/uv"), git=Path("tools/git/cmd/git"))

    def test_the_lock_file_is_used_as_published(self, tmp_path):
        plan = Plan(python="3.11", extras=("psychopy",), git_packages=())
        command = build_command(self.TOOLS, plan, tmp_path / "release")
        assert command == [
            str(Path("tools/uv")),
            "sync",
            "--frozen",
            "--no-dev",
            "--python",
            "3.11",
            "--project",
            str(tmp_path / "release"),
            "--extra",
            "psychopy",
        ]

    def test_no_extra_means_no_extra_argument(self, tmp_path):
        command = build_command(self.TOOLS, Plan("3.10", (), ()), tmp_path)
        assert "--extra" not in command

    def test_the_environment_is_outside_the_release_and_uses_a_managed_python(self, tmp_path):
        base = {"PATH": "/usr/bin", "VIRTUAL_ENV": "/the/apps/own", "HOME": "/home/x"}
        env = build_environ(base, self.TOOLS, tmp_path / "envs" / "ab12")
        assert env["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / "envs" / "ab12")
        assert env["UV_PYTHON_PREFERENCE"] == "only-managed"
        assert "VIRTUAL_ENV" not in env and env["HOME"] == "/home/x"
        # The app's git is found first; the rest of the PATH is kept.
        assert env["PATH"].split(os.pathsep) == [str(Path("tools/git/cmd")), "/usr/bin"]
        # The caller's mapping is not changed.
        assert base == {"PATH": "/usr/bin", "VIRTUAL_ENV": "/the/apps/own", "HOME": "/home/x"}

    def test_without_git_the_path_is_left_alone(self, tmp_path):
        env = build_environ({"PATH": "/usr/bin"}, Tools(uv=Path("uv"), git=None), tmp_path)
        assert env["PATH"] == "/usr/bin"


# -- the build, against a stand-in uv -------------------------------------------------------


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    return fake_uv(tmp_path, monkeypatch)


class TestBuild:
    TOOLS = Tools(uv=Path("uv"), git=None)
    PLAN = Plan(python="3.11", extras=("psychopy",), git_packages=())

    def test_a_build_returns_the_environments_interpreter(self, tmp_path, stand_in):
        envs = Environments(tmp_path / "hub")
        lines: list[str] = []
        python = envs.build(
            release(tmp_path),
            SHA,
            self.PLAN,
            self.TOOLS,
            on_output=lines.append,
            environ=stand_in("ok"),
        )
        # One environment per release, named by the digest's first twelve
        # digits, outside the release folder.
        assert python == interpreter_of(tmp_path / "hub" / "envs" / "ab12cd34ef56")
        assert python.is_file()
        assert not (tmp_path / "release" / ".venv").exists()
        # Progress carries uv's lines, without the blank ones.
        assert lines == ["Using CPython 3.11.9", "Installed 3 packages in 12ms"]
        log = envs.log_file(SHA).read_text(encoding="utf-8")
        assert log.startswith("$ ") and "--frozen" in log.splitlines()[0]
        assert "Installed 3 packages" in log and log.rstrip().endswith("[exit code 0]")

    def test_a_failed_build_reports_the_end_of_uvs_output(self, tmp_path, stand_in):
        envs = Environments(tmp_path / "hub")
        with pytest.raises(SetupError) as failure:
            envs.build(release(tmp_path), SHA, self.PLAN, self.TOOLS, environ=stand_in("fail"))
        assert failure.value.code == "setup_failed"
        message = failure.value.message
        assert "exit code 2" in message and "Failed to fetch" in message
        # stderr is part of the same stream: the cause is not lost.
        assert "connection refused" in message
        assert "[exit code 2]" in envs.log_file(SHA).read_text(encoding="utf-8")

    def test_success_without_an_interpreter_is_a_failure(self, tmp_path, stand_in):
        envs = Environments(tmp_path / "hub")
        with pytest.raises(SetupError) as failure:
            envs.build(release(tmp_path), SHA, self.PLAN, self.TOOLS, environ=stand_in("no-python"))
        assert failure.value.code == "setup_failed"
        assert "no Python interpreter" in failure.value.message

    def test_a_git_source_without_git_never_starts_uv(self, tmp_path, stand_in):
        envs = Environments(tmp_path / "hub")
        plan = Plan(python="3.11", extras=(), git_packages=("alhazen-vision",))
        with pytest.raises(SetupError) as refusal:
            envs.build(release(tmp_path), SHA, plan, self.TOOLS, environ=stand_in("ok"))
        assert refusal.value.code == "git_missing"
        assert not envs.log_file(SHA).exists() and not envs.folder(SHA).exists()

    def test_a_uv_that_cannot_be_started_is_a_failure_naming_it(self, tmp_path):
        envs = Environments(tmp_path / "hub")
        missing = Tools(uv=tmp_path / "no-such-uv", git=None)
        with pytest.raises(SetupError) as failure:
            envs.build(release(tmp_path), SHA, self.PLAN, missing)
        assert failure.value.code == "setup_failed" and "no-such-uv" in failure.value.message

    def test_closing_stops_a_running_build(self, tmp_path, stand_in):
        envs = Environments(tmp_path / "hub")
        started = threading.Event()
        outcome: list[BaseException] = []

        def build():
            try:
                envs.build(
                    release(tmp_path),
                    SHA,
                    self.PLAN,
                    self.TOOLS,
                    on_output=lambda line: started.set(),
                    environ=stand_in("hang"),
                )
            except SetupError as exc:
                outcome.append(exc)

        thread = threading.Thread(target=build)
        thread.start()
        assert started.wait(30), "the stand-in never printed"
        began = time.monotonic()
        envs.close()
        thread.join(30)
        assert not thread.is_alive()
        # Stopped at once, not when the stand-in's two-minute sleep ended.
        assert time.monotonic() - began < 20
        assert [getattr(exc, "code", None) for exc in outcome] == ["setup_cancelled"]

    def test_a_second_build_of_the_same_release_reuses_its_folder(self, tmp_path, stand_in):
        envs = Environments(tmp_path / "hub")
        root = release(tmp_path)
        first = envs.build(root, SHA, self.PLAN, self.TOOLS, environ=stand_in("ok"))
        marker = envs.folder(SHA) / "kept.txt"
        marker.write_text("x", encoding="utf-8")
        second = envs.build(root, SHA, self.PLAN, self.TOOLS, environ=stand_in("ok"))
        # Nothing is deleted between builds: uv completes what is there.
        assert first == second and marker.is_file()


# -- the real uv (opt-in: it downloads a Python and a build backend) ------------------------

REAL_UV = pytest.mark.skipif(
    os.environ.get("ALHAZEN_TEST_REAL_UV") != "1" or shutil.which("uv") is None,
    reason="set ALHAZEN_TEST_REAL_UV=1 (and have uv on PATH) to build a real environment",
)

REAL_PROJECT = """\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "demo-task"
version = "1.0.0"
requires-python = ">=3.10"
dependencies = []

[project.optional-dependencies]
psychopy = []

[project.entry-points."alhazen.tasks"]
demo-task = "demo_task:Task"

[tool.setuptools.packages.find]
where = ["src"]
"""


@REAL_UV
def test_a_real_uv_builds_an_environment_that_registers_the_experiments_tasks(tmp_path):
    root = tmp_path / "release"
    (root / "src" / "demo_task").mkdir(parents=True)
    (root / "src" / "demo_task" / "__init__.py").write_text("class Task: ...\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(REAL_PROJECT, encoding="utf-8")
    (root / ".python-version").write_text("3.11\n", encoding="utf-8")
    # The author's step: lock the project. Everything after is the rig's.
    subprocess.run(["uv", "lock", "--project", str(root)], check=True, capture_output=True)
    declared = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())

    plan = read_plan(root, python_min="3.10")
    assert plan == Plan(python="3.11", extras=("psychopy",), git_packages=())
    python = Environments(tmp_path / "hub").build(root, SHA, plan, find_tools())

    report = subprocess.run(
        [
            str(python),
            "-c",
            "import sys, importlib.metadata as m, demo_task;"
            "print(sys.version_info[:2]);"
            "print(sorted(e.name for e in m.entry_points(group='alhazen.tasks')));"
            "print(demo_task.__file__)",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert report[0] == "(3, 11)"
    assert report[1] == "['demo-task']"
    # The experiment is imported from the release folder, not from a copy
    # (compared as files: a temporary folder may be spelled two ways).
    assert os.path.samefile(report[2], root / "src" / "demo_task" / "__init__.py")
    # The release's own files are untouched; the lock was not rewritten.
    after = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and ".egg-info" not in p.as_posix() and "__pycache__" not in p.as_posix()
    )
    assert after == declared
