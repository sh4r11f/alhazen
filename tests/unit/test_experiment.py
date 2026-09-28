"""`find_experiment`: the experiment a task belongs to, and its version.

Data is filed under the experiment's version, so the rules here decide which
folder a session's data lands in: the nearest pyproject.toml above the task's
module wins, installed metadata is only the fallback for a wheel, and a
version that is missing or cannot be a folder name stops the session rather
than inventing one.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

from alhazen.config import experiment as experiment_module
from alhazen.config.experiment import (
    GIVEN_BY_CALLER,
    Experiment,
    find_experiment,
    session_experiment,
)
from alhazen.errors import ConfigError


def make_project(root: Path, pyproject: str | None, module: str = "exp_pkg") -> type:
    """A task class defined in ``root/src/<module>/task.py``, with the given
    pyproject.toml at ``root`` (or none), imported fresh."""
    package = root / "src" / module
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "task.py").write_text("class MyTask:\n    pass\n")
    if pyproject is not None:
        (root / "pyproject.toml").write_text(textwrap.dedent(pyproject), encoding="utf-8")
    name = f"{module}.task"
    spec = importlib.util.spec_from_file_location(name, package / "task.py")
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded.MyTask


@pytest.fixture(autouse=True)
def forget_modules():
    yield
    for name in [n for n in sys.modules if n.startswith("exp_pkg")]:
        del sys.modules[name]


class TestFromPyproject:
    def test_the_nearest_pyproject_gives_name_version_and_root(self, tmp_path):
        task = make_project(tmp_path, '[project]\nname = "amodal-averaging"\nversion = "0.4.0"\n')
        assert find_experiment(task) == Experiment(
            name="amodal-averaging",
            version="0.4.0",
            version_source="pyproject.toml",
            root=tmp_path.resolve(),
        )

    def test_the_file_wins_over_stale_installed_metadata(self, tmp_path, monkeypatch):
        # An editable install keeps the version it was installed with; the
        # file is what the experimenter just bumped.
        task = make_project(tmp_path, '[project]\nname = "exp"\nversion = "0.5.0"\n')
        monkeypatch.setattr(
            experiment_module.metadata, "version", lambda name: pytest.fail("metadata was read")
        )
        assert find_experiment(task).version == "0.5.0"

    @pytest.mark.parametrize("version", ["1.2.0", "0.4.0rc1", "1.0.post2", "2.0+lab", "3"])
    def test_pep_440_versions_are_accepted(self, tmp_path, version):
        task = make_project(tmp_path, f'[project]\nname = "exp"\nversion = "{version}"\n')
        assert find_experiment(task).version == version

    def test_a_missing_version_is_an_error_naming_the_file(self, tmp_path):
        task = make_project(tmp_path, '[project]\nname = "exp"\n')
        with pytest.raises(ConfigError, match=r"gives no \[project\] version"):
            find_experiment(task)

    def test_a_version_that_cannot_name_a_folder_is_refused(self, tmp_path):
        task = make_project(tmp_path, '[project]\nname = "exp"\nversion = "1.0/beta"\n')
        with pytest.raises(ConfigError, match="cannot name a data folder"):
            find_experiment(task)

    def test_an_unreadable_pyproject_is_an_error_not_a_skip(self, tmp_path):
        task = make_project(tmp_path, "[project\nname = broken\n")
        with pytest.raises(ConfigError, match="cannot read the experiment's version"):
            find_experiment(task)

    def test_without_a_name_the_folder_names_the_experiment(self, tmp_path):
        root = tmp_path / "kde-vergence"
        task = make_project(root, '[project]\nversion = "0.1.0"\n')
        assert find_experiment(task).name == "kde-vergence"


class TestFromInstalledMetadata:
    def test_a_wheel_install_falls_back_to_its_distribution(self, tmp_path, monkeypatch):
        task = make_project(tmp_path, None)
        monkeypatch.setattr(
            experiment_module.metadata,
            "packages_distributions",
            lambda: {"exp_pkg": ["exp-dist"]},
        )
        monkeypatch.setattr(experiment_module.metadata, "version", lambda name: "0.7.1")
        # tmp_path has no pyproject.toml above it on any CI runner we use; the
        # walk must reach the filesystem root without finding one.
        assert [p for p in tmp_path.parents if (p / "pyproject.toml").is_file()] == []
        assert find_experiment(task) == Experiment(
            name="exp-dist", version="0.7.1", version_source="installed metadata", root=None
        )

    def test_no_pyproject_and_no_distribution_is_an_error(self, tmp_path, monkeypatch):
        task = make_project(tmp_path, None)
        monkeypatch.setattr(experiment_module.metadata, "packages_distributions", dict)
        with pytest.raises(ConfigError, match="no installed distribution ships the package"):
            find_experiment(task)


def test_alhazens_own_example_tasks_belong_to_alhazen():
    # A task defined inside alhazen's own tree (its examples, its tests) finds
    # alhazen's pyproject: a session run from there is filed under alhazen's
    # version, which is the honest answer for code that ships with alhazen.
    assert find_experiment(TestFromPyproject).name == "alhazen-vision"


class TestSessionExperiment:
    """`session_experiment`: which experiment a session's data is filed
    under, from whatever its caller said — and a refusal when nothing says."""

    def test_a_task_class_finds_its_project(self, tmp_path):
        task = make_project(tmp_path, '[project]\nname = "exp"\nversion = "0.5.0"\n')
        assert session_experiment(task, "my-task") == find_experiment(task)

    def test_a_name_renames_what_was_found_but_keeps_its_version(self, tmp_path):
        task = make_project(tmp_path, '[project]\nname = "exp"\nversion = "0.5.0"\n')
        found = session_experiment(task, "my-task", name="the-study")
        assert (found.name, found.version, found.version_source) == (
            "the-study",
            "0.5.0",
            "pyproject.toml",
        )

    def test_an_explicit_version_wins_and_is_recorded_as_given(self, tmp_path):
        task = make_project(tmp_path, '[project]\nname = "exp"\nversion = "0.5.0"\n')
        found = session_experiment(task, "my-task", version="9.0")
        # Named after the task when no name is given: the best label there is.
        assert found == Experiment("my-task", "9.0", GIVEN_BY_CALLER, None)

    def test_an_explicit_version_needs_no_task_class(self):
        found = session_experiment(None, "hand-wired", version="0.1.0", name="rig-check")
        assert found == Experiment("rig-check", "0.1.0", GIVEN_BY_CALLER, None)

    def test_an_explicit_version_is_checked_like_a_pyprojects(self):
        with pytest.raises(ConfigError, match="cannot name a data folder"):
            session_experiment(None, "t", version="../elsewhere")

    def test_nothing_to_read_a_version_from_is_refused_saying_what_to_pass(self):
        with pytest.raises(ConfigError) as refused:
            session_experiment(None, "hand-wired")
        message = str(refused.value)
        assert "experiment_version=" in message and "task=" in message
        assert "data/v<version>/" in message

    def test_an_experiment_already_found_is_used_as_it_is(self):
        found = Experiment("exp", "0.5.0", "pyproject.toml", None)
        assert session_experiment(None, "t", experiment=found) is found

    @pytest.mark.parametrize("extra", [{"version": "1.0"}, {"name": "x"}])
    def test_two_answers_are_refused(self, extra):
        found = Experiment("exp", "0.5.0", "pyproject.toml", None)
        with pytest.raises(ValueError, match="not both"):
            session_experiment(None, "t", experiment=found, **extra)
