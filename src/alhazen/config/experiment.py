"""Which experiment a task belongs to: its folder, its name and its version.

Two things in alhazen need to know the experiment a session runs, not just
its task. Rig names (``--rig lab``) are looked up in the experiment's own
``configs/`` before alhazen's shared rigs, so the experiment's folder must be
known without a path on the command line. And every session's data is filed
under the experiment's version (``data/v0.4.0/sub-01/...``), so data recorded
by two versions of an experiment never mix — which needs the version.

Both come from the same place: the ``pyproject.toml`` nearest above the
module that defines the task class. That is the experiment's repository in
every layout alhazen supports (a checkout run with ``python run.py``, an
editable install, a worktree), and it is read from the file rather than from
installed package metadata because an editable install's metadata keeps the
version it was installed with: bump ``version`` and forget to reinstall, and
the metadata would file new data under the old version without a word.
Installed metadata is the fallback only when there is no ``pyproject.toml`` at
all — a task installed from a wheel — and the source it came from is recorded,
so a reader can tell the two apart.

A version that cannot be found is an error, not a default: data filed under
an invented version is worse than a session that does not start.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from alhazen.errors import ConfigError
from alhazen.version import experiment_distribution_version

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - the 3.10 job exercises it
    import tomli as tomllib

# A version becomes a directory name (``v<version>``), so it is held to the
# characters every filesystem alhazen runs on accepts in one: PEP 440 versions
# (``1.2.0``, ``0.4.0rc1``, ``1.0.post2``, ``2.0+lab``) all fit.
VERSION_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]*")


@dataclass(frozen=True)
class Experiment:
    """The experiment a task belongs to.

    ``root`` is the folder holding its ``pyproject.toml``, or None for a task
    installed from a wheel (no source tree). ``version_source`` says where the
    version was read — ``"pyproject.toml"`` or ``"installed metadata"`` — and
    is recorded with the data.
    """

    name: str
    version: str
    version_source: str
    root: Path | None


def find_experiment(task_class: type) -> Experiment:
    """The experiment ``task_class`` is defined in; see the module docstring.

    Raises ConfigError, naming the file or package it looked at, when no
    version can be found or the version cannot be a directory name.
    """
    try:
        module_file = Path(inspect.getfile(task_class)).resolve()
    except (TypeError, OSError) as exc:
        raise ConfigError(
            f"cannot tell which experiment {task_class.__name__} belongs to: its module has "
            f"no file ({exc})"
        ) from exc
    for folder in module_file.parents:
        pyproject = folder / "pyproject.toml"
        if pyproject.is_file():
            return _from_pyproject(pyproject)
    return _from_metadata(task_class, module_file)


def _from_pyproject(path: Path) -> Experiment:
    try:
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read the experiment's version from {path}: {exc}") from exc
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise ConfigError(
            f"{path} gives no [project] version. Data is filed under the experiment's "
            'version (data/v<version>/...), so declare one: version = "0.1.0".'
        )
    name = project.get("name") or path.parent.name
    return Experiment(
        name=str(name),
        version=_checked(version, str(path)),
        version_source="pyproject.toml",
        root=path.parent,
    )


def _from_metadata(task_class: type, module_file: Path) -> Experiment:
    """A task installed from a wheel: the distribution that ships its package."""
    package = task_class.__module__.split(".", 1)[0]
    distributions = metadata.packages_distributions().get(package, [])
    if not distributions:
        raise ConfigError(
            f"cannot tell which experiment {task_class.__name__} belongs to: no pyproject.toml "
            f"above {module_file} and no installed distribution ships the package {package!r}. "
            "Data is filed under the experiment's version, so the task must live in a project "
            "with a [project] version."
        )
    name = distributions[0]
    return Experiment(
        name=name,
        version=_checked(
            experiment_distribution_version(name), f"the installed {name} distribution"
        ),
        version_source="installed metadata",
        root=None,
    )


def _checked(version: str, where: str) -> str:
    if not VERSION_PATTERN.fullmatch(version):
        raise ConfigError(
            f"version {version!r} in {where} cannot name a data folder: use letters, digits "
            "and . + _ - only, as PEP 440 versions do"
        )
    return version


# What `version_source` records for a version the caller handed to
# `build_session` (or `build_mode_session`) instead of one read from a file,
# so a reader of the data can tell "the project said so" from "the code that
# started the session said so".
GIVEN_BY_CALLER = "given to build_session"


def session_experiment(
    task_class: type | None,
    task_name: str,
    *,
    experiment: Experiment | None = None,
    version: str | None = None,
    name: str | None = None,
) -> Experiment:
    """The experiment a session's data is filed under, from what its caller said.

    The first of these that applies:

    1. ``experiment`` — one already found. The modes find the experiment
       once, from the task class they were handed, before they number the
       run (the run number counts within the version's folder), and pass it
       down, so the builder never looks again from a different class — a
       simulation's stand-in task, say, defined somewhere else.
    2. ``version`` — given explicitly, for a session with no task class to
       read one from (a hand-wired ``build_session``, a test). Checked like a
       pyproject's, recorded with ``version_source`` `GIVEN_BY_CALLER`, and
       named ``name`` — else the task's own name, the best label there is.
    3. ``task_class`` — `find_experiment`, the normal case; ``name`` renames
       what it found.

    With none of them there is no version to file the data under, and a
    default would invent one, so it is a ConfigError saying what to pass.
    ``experiment`` together with ``version`` or ``name`` is refused: the call
    would carry two answers and one would be ignored.
    """
    if experiment is not None:
        if version is not None or name is not None:
            raise ValueError(
                "pass experiment=, or experiment_version= / experiment_name=, not both: "
                "one of the two answers would be ignored"
            )
        return experiment
    if version is not None:
        return Experiment(
            name=name if name is not None else task_name,
            version=_checked(version, "experiment_version="),
            version_source=GIVEN_BY_CALLER,
            root=None,
        )
    if task_class is None:
        raise ConfigError(
            "cannot tell which experiment version to file this session under: there is no "
            "task= to read it from and no experiment_version= was given. Each session's data "
            "is filed under its experiment's version (data/v<version>/sub-...), so pass "
            'experiment_version="0.1.0" (and experiment_name=...), or task=<Task instance> '
            "from a project whose pyproject.toml declares [project] version."
        )
    found = find_experiment(task_class)
    return found if name is None else dataclasses.replace(found, name=name)
