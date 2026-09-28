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
