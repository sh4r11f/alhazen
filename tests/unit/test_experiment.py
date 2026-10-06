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
    ExperimentTitle,
    StimulusDeclaration,
    experiment_stimuli,
    experiment_title,
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
            experiment_module,
            "experiment_distribution_version",
            lambda name: pytest.fail("metadata was read"),
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
        monkeypatch.setattr(
            experiment_module, "experiment_distribution_version", lambda name: "0.7.1"
        )
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


def write_pyproject(root: Path, *lines: str) -> Path:
    """``root/pyproject.toml`` holding ``lines``, one per line; returns ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


class TestExperimentTitle:
    """`experiment_title`: what the workspace calls an experiment, and the
    slug rig names are qualified with — read from the file, never imported."""

    def test_a_declared_title_is_shown_and_the_project_name_is_the_slug(self, tmp_path):
        root = write_pyproject(
            tmp_path / "checkout",
            "[project]",
            'name = "amodal-averaging"',
            'version = "0.1.0"',
            "[tool.alhazen]",
            'title = "Amodal averaging"',
        )
        assert experiment_title(root) == ExperimentTitle(
            slug="amodal-averaging", title="Amodal averaging", error=None
        )

    def test_without_a_title_the_slug_is_the_title_and_nothing_is_wrong(self, tmp_path):
        root = write_pyproject(tmp_path / "checkout", "[project]", 'name = "kde-vergence"')
        assert experiment_title(root) == ExperimentTitle("kde-vergence", "kde-vergence")
        # A [tool.alhazen] table that says other things, but no title: the same.
        write_pyproject(root, "[project]", 'name = "kde-vergence"', "[tool.alhazen]", "other = 1")
        assert experiment_title(root) == ExperimentTitle("kde-vergence", "kde-vergence")

    def test_without_a_project_name_or_a_pyproject_the_folder_names_it(self, tmp_path):
        assert experiment_title(tmp_path / "plain") == ExperimentTitle("plain", "plain")
        root = write_pyproject(tmp_path / "unnamed", "[tool.alhazen]", 'title = "Unnamed"')
        assert experiment_title(root) == ExperimentTitle("unnamed", "Unnamed")

    def test_a_relative_folder_is_named_by_its_real_folder(self, tmp_path, monkeypatch):
        """`Path(".")` has no name; the folder it stands for does."""
        (tmp_path / "here").mkdir()
        monkeypatch.chdir(tmp_path / "here")
        assert experiment_title(Path(".")).slug == "here"

    def test_the_title_is_trimmed(self, tmp_path):
        root = write_pyproject(tmp_path / "x", "[tool.alhazen]", 'title = "  Spaced  "')
        assert experiment_title(root).title == "Spaced"

    @pytest.mark.parametrize("value", ["3", '""', '"   "', "[1, 2]", "true"])
    def test_a_title_that_is_not_a_non_empty_string_is_reported_not_raised(self, tmp_path, value):
        root = write_pyproject(
            tmp_path / "x", "[project]", 'name = "demo"', "[tool.alhazen]", f"title = {value}"
        )
        found = experiment_title(root)
        # The workspace still lists the experiment, under its slug...
        assert (found.slug, found.title) == ("demo", "demo")
        # ...and says why, naming the file and what a title looks like.
        assert found.error is not None
        assert str(root / "pyproject.toml") in found.error
        assert "must be a non-empty string" in found.error

    def test_a_tool_alhazen_that_is_not_a_table_is_reported(self, tmp_path):
        root = write_pyproject(
            tmp_path / "x", "[project]", 'name = "demo"', "[tool]", "alhazen = 5"
        )
        found = experiment_title(root)
        assert (found.slug, found.title) == ("demo", "demo")
        assert found.error is not None and "[tool.alhazen] must be a table" in found.error

    def test_an_unreadable_pyproject_falls_back_to_the_folder_and_says_why(self, tmp_path):
        root = write_pyproject(tmp_path / "broken", "[project")
        found = experiment_title(root)
        assert (found.slug, found.title) == ("broken", "broken")
        assert found.error is not None and "cannot read" in found.error

    def test_the_slug_is_the_name_find_experiment_records(self, tmp_path):
        """One rule for both: the rig names the workspace shows are qualified
        with the same name a session's data records as its experiment."""
        root = tmp_path / "checkout"
        task = make_project(root, '[project]\nname = "amodal-averaging"\nversion = "0.1.0"\n')
        assert find_experiment(task).name == experiment_title(root).slug == "amodal-averaging"


class TestExperimentStimuli:
    """`experiment_stimuli`: where an experiment says its stimuli are drawn,
    read from its pyproject.toml without importing anything."""

    def write(self, root: Path, text: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "pyproject.toml").write_text(textwrap.dedent(text), encoding="utf-8")
        return root

    def test_a_declared_function_is_read_as_written(self, tmp_path):
        root = self.write(
            tmp_path / "exp",
            """
            [project]
            name = "demo"
            [tool.alhazen]
            stimuli = "  demo_pkg.stimulus_set:stimulus_images  "
            """,
        )
        assert experiment_stimuli(root) == StimulusDeclaration(
            target="demo_pkg.stimulus_set:stimulus_images"
        )

    @pytest.mark.parametrize(
        "text",
        [None, '[project]\nname = "demo"\n', '[tool.alhazen]\ntitle = "Demo"\n'],
        ids=["no pyproject", "no tool table", "no stimuli key"],
    )
    def test_declaring_nothing_is_not_an_error(self, tmp_path, text):
        root = tmp_path / "exp"
        root.mkdir()
        if text is not None:
            (root / "pyproject.toml").write_text(text, encoding="utf-8")
        assert experiment_stimuli(root) == StimulusDeclaration(target=None, error=None)

    @pytest.mark.parametrize(
        "value",
        [
            "42",
            '"no_colon"',
            '"pkg.module:"',
            '":function"',
            '"pkg..module:function"',
            '"pkg.module:function:extra"',
            '"1pkg.module:function"',
            '"pkg.module:function()"',
        ],
    )
    def test_a_value_that_names_no_function_is_reported_with_the_file(self, tmp_path, value):
        root = self.write(tmp_path / "exp", f"[tool.alhazen]\nstimuli = {value}\n")
        declaration = experiment_stimuli(root)
        assert declaration.target is None
        assert declaration.error is not None
        assert str(root.resolve() / "pyproject.toml") in declaration.error
        assert "package.module:function" in declaration.error

    def test_a_tool_alhazen_that_is_not_a_table_is_reported(self, tmp_path):
        root = self.write(tmp_path / "exp", "[tool]\nalhazen = 3\n")
        declaration = experiment_stimuli(root)
        assert declaration.target is None
        assert "[tool.alhazen] must be a table" in (declaration.error or "")

    def test_a_pyproject_that_cannot_be_read_is_reported_not_raised(self, tmp_path):
        root = self.write(tmp_path / "exp", '[tool.alhazen\nstimuli = "a:b"\n')
        declaration = experiment_stimuli(root)
        assert declaration.target is None
        assert "cannot read" in (declaration.error or "")
