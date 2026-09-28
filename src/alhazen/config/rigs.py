"""Rigs by name: alhazen's shared rigs, an experiment's own, and ``extends``.

A rig file describes one machine: its panel, its devices, where its data
goes. Several experiments run on the same few machines, and each used to keep
its own copy of every one of them — five repositories, five copies of the lab
rig, drifting apart one edited comment at a time. So alhazen ships the
machines they share (``alhazen/rigs/rig-lab.yaml`` and its siblings), and an
experiment keeps only the rigs that are its own, or the part of a shared rig
it does differently.

**Names.** A rig file is named ``rig-<name>.yaml`` (or ``.yml``) and the rig
is ``<name>``: ``configs/rig-lab.yaml`` is ``lab``. On the command line
``lab``, ``rig-lab`` and ``rig-lab.yaml`` all mean that rig, and a path to an
existing file still means exactly that file. A name is looked up in two
places, in this order:

1. the experiment's own ``configs/`` folder, subfolders included
   (``configs/**/rig-<name>.yaml``) — the way the experiment workspace has
   always found rigs;
2. alhazen's shared rigs.

So an experiment's rig *shadows* a shared one of the same name: ``--rig lab``
is the experiment's lab, and ``alhazen/lab`` is how to reach the shared one
anyway. Two of the experiment's own files with one name are an error rather
than a choice, since whichever were picked, the other would be silently
ignored.

**Extending.** An experiment rig may begin with ``extends: <name>``, naming a
shared rig, and then says only what it does differently. Its settings are
merged over the shared file's (``rig_mapping``) and the result is validated
as one rig. Only shared rigs can be extended, and a shared rig extends
nothing: what a rig builds on is then always one file, the same on every
machine alhazen is installed on, never a chain to follow through an
experiment's folders.

Everything here reads files and nothing here imports a device or a session,
so the CLI, the session builder and the experiment workspace all resolve a
rig the same way.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple, TypeAlias

from alhazen.config.gamma import GAMMA_FILENAME_SUFFIX
from alhazen.config.loader import read_mapping
from alhazen.errors import ConfigError

# The shared rigs ship inside the package, as package data (pyproject's
# [tool.setuptools.package-data]); `alhazen/rigs/` is a folder of YAML, not
# a Python package.
SHARED_RIG_DIR = Path(__file__).resolve().parent.parent / "rigs"
# `alhazen/lab` names the shared rig even when the experiment has a lab of
# its own. The prefix is the package's name, so it reads as "alhazen's lab".
SHARED_PREFIX = "alhazen/"
FILE_PREFIX = "rig-"
SUFFIXES = (".yaml", ".yml")
# What a name may be: what a rig file name can carry between `rig-` and the
# suffix on every filesystem, and nothing that could be a path.
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

RigSource = Literal["experiment", "alhazen"]
# Where the experiment's own rigs are looked for: its root folder, or a
# callable that finds it. The callable is called only when a name actually
# has to be looked up there — finding the experiment reads its
# pyproject.toml (alhazen.config.experiment), and a --rig given as a path, or
# as alhazen/<name>, must keep working in a folder that has none.
ExperimentRoot: TypeAlias = "Path | Callable[[], Path | None] | None"


@dataclass(frozen=True)
class RigRef:
    """One rig, found: its name, its file, and where it comes from.

    ``source`` is ``"experiment"`` for one of the experiment's own files (and
    for any file named by its path that is not one of alhazen's) and
    ``"alhazen"`` for one of alhazen's shared rigs. ``shadowed`` is set only
    by :func:`list_rigs`: on a shared rig that an experiment rig of the same
    name hides from ``--rig <name>``.
    """

    name: str
    path: Path
    source: RigSource
    shadowed: bool = False

    @property
    def spec(self) -> str:
        """How to name this rig on the command line: ``alhazen/<name>`` for a
        shared rig, which reaches it even when an experiment rig shadows it,
        and the bare name for an experiment rig."""
        return f"{SHARED_PREFIX}{self.name}" if self.source == "alhazen" else self.name

    def describe(self) -> str:
        """One phrase for a console line: the name, whose it is, and the file."""
        if self.source == "alhazen":
            return f"{self.spec} (alhazen's shared rig, {self.path})"
        return f"{self.name} ({self.path})"


class MergedRig(NamedTuple):
    """A rig file's settings with any ``extends`` applied, before validation.

    ``values`` is what gets validated as a ``RigConfig``. ``extends`` is the
    shared rig's name and ``base`` its file, both None for a file that
    extends nothing.
    """

    values: dict[str, Any]
    extends: str | None
    base: Path | None


def rig_name(spec: str) -> str | None:
    """The rig name a command-line spelling means, or None if it is no name.

    ``lab``, ``rig-lab``, ``rig-lab.yaml`` and ``rig-lab.yml`` are all
    ``lab``, with or without a leading ``alhazen/`` (what that prefix means
    is the caller's business). Anything carrying a path separator is a path,
    not a name, and gives None.
    """
    text = spec.strip().removeprefix(SHARED_PREFIX)
    for suffix in SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    text = text.removeprefix(FILE_PREFIX)
    return text if NAME_PATTERN.fullmatch(text) else None


def file_rig_name(path: Path) -> str:
    """The name of the rig in ``path``: its stem without ``rig-``. A file
    named some other way (``examples/fixation.yaml``) is named by its stem."""
    return path.stem.removeprefix(FILE_PREFIX)


def _is_rig_file(path: Path) -> bool:
    """A ``rig-<name>.yaml`` file. A measured gamma is kept beside its rig as
    ``rig-<name>_gamma.yaml`` (config/gamma.py), which matches the pattern
    but is no rig, so it is left out by name."""
    return (
        path.name.startswith(FILE_PREFIX)
        and path.suffix in SUFFIXES
        and not path.name.endswith(GAMMA_FILENAME_SUFFIX)
        and path.is_file()
    )


def shared_rig_files() -> dict[str, Path]:
    """alhazen's shared rigs, name to file, in name order.

    A missing folder is an incomplete installation — a wheel built without
    its package data — and is said so rather than listed as "no shared rigs",
    which would send the reader looking for a typo in the name they gave.
    """
    if not SHARED_RIG_DIR.is_dir():
        raise ConfigError(
            f"alhazen's shared rigs are missing: {SHARED_RIG_DIR} does not exist. The "
            "installation is incomplete; reinstall alhazen-vision."
        )
    files = {file_rig_name(p): p for p in SHARED_RIG_DIR.iterdir() if _is_rig_file(p)}
    return dict(sorted(files.items()))


def experiment_rig_files(root: Path) -> list[tuple[str, Path]]:
    """Every rig file under ``root/configs``, subfolders included, as
    ``(name, path)`` in name order (then path order, for two files that share
    a name — :func:`resolve_rig` refuses to choose between those, and
    :func:`list_rigs` shows both). An experiment with no ``configs/`` folder
    has none."""
    configs = root / "configs"
    if not configs.is_dir():
        return []
    found = [(file_rig_name(p), p) for p in configs.rglob(f"{FILE_PREFIX}*") if _is_rig_file(p)]
    return sorted(found, key=lambda item: (item[0], item[1].as_posix()))


def list_rigs(
    experiment_root: Path | None, *, shared: Mapping[str, Path] | None = None
) -> list[RigRef]:
    """Every rig a name can reach here: the experiment's own first, then the
    shared ones, each group in name order.

    A shared rig whose name an experiment rig also has is listed with
    ``shadowed=True`` — ``--rig <name>`` gives the experiment's, and
    ``alhazen/<name>`` this one. ``experiment_root`` None lists the shared
    rigs alone. ``shared`` replaces alhazen's own shared rigs, name to file;
    the experiment workspace passes the ones the project's alhazen ships.
    """
    own = experiment_rig_files(experiment_root) if experiment_root is not None else []
    names = {name for name, _ in own}
    files = shared_rig_files() if shared is None else shared
    return [RigRef(name, path, "experiment") for name, path in own] + [
        RigRef(name, Path(path), "alhazen", shadowed=name in names)
        for name, path in sorted(files.items())
    ]


def resolve_rig(
    spec: str | Path,
    experiment_root: ExperimentRoot,
    *,
    shared: Mapping[str, Path] | None = None,
) -> RigRef:
    """The rig ``--rig spec`` means; see the module docstring for the order.

    1. A path to an existing file is that file, exactly as ``--rig`` has
       always taken it — named ``alhazen`` when it is one of the shared rigs.
    2. ``alhazen/<name>`` is the shared rig of that name.
    3. Any other name is the experiment's own rig of that name (looked for
       under ``experiment_root``), else the shared one.

    Raises ConfigError for a name nothing has — listing every rig there is,
    and whose each is — for a name two of the experiment's files share, and
    for a path that does not exist. ``shared`` is as for :func:`list_rigs`.
    """
    path = Path(spec)
    if path.is_file():
        return RigRef(
            file_rig_name(path), path, "alhazen" if _is_shared_file(path, shared) else "experiment"
        )
    text = str(spec).strip()
    name = rig_name(text)
    if name is None:
        # Not a name, so meant as a path — and there is no file there. The
        # same words the loader has always used, plus what a name looks like,
        # since the reader may not know they could have typed one.
        raise ConfigError(
            f"config file not found: {spec}. A rig is the path to its YAML file, or its "
            "name (lab for rig-lab.yaml; `alhazen rigs` lists them)"
        )
    if text.startswith(SHARED_PREFIX):
        files = shared_rig_files() if shared is None else shared
        if name not in files:
            raise ConfigError(
                f"alhazen has no shared rig named {name!r}. Its shared rigs: "
                f"{', '.join(files) or 'none'}"
            )
        return RigRef(name, Path(files[name]), "alhazen")
    root = experiment_root() if callable(experiment_root) else experiment_root
    own = experiment_rig_files(root) if root is not None else []
    matches = [found for found_name, found in own if found_name == name]
    if len(matches) > 1:
        raise ConfigError(
            f"the experiment has {len(matches)} rigs named {name!r}: "
            f"{', '.join(str(p) for p in matches)}. `--rig {name}` cannot choose between "
            "them: rename one (a rig's name is its file name without rig- and .yaml), or "
            "give the path of the one you mean"
        )
    if matches:
        return RigRef(name, matches[0], "experiment")
    files = shared_rig_files() if shared is None else shared
    if name in files:
        return RigRef(name, Path(files[name]), "alhazen")
    raise ConfigError(_no_such_rig(name, root, own, files))


def _no_such_rig(
    name: str, root: Path | None, own: list[tuple[str, Path]], files: Mapping[str, Path]
) -> str:
    """The refusal for a name nothing has: every rig there is, whose, and where."""
    own_names = {found for found, _ in own}
    lines = [f"  {found:<16} this experiment's rig, {path}" for found, path in own]
    for found, path in files.items():
        hidden = (
            f" — hidden by the experiment's own {found}; name it alhazen/{found}"
            if found in own_names
            else ""
        )
        lines.append(f"  {found:<16} alhazen's shared rig, {path}{hidden}")
    searched = (
        f"in {root / 'configs'} or among alhazen's shared rigs"
        if root is not None
        else "among alhazen's shared rigs (no experiment folder was searched)"
    )
    return (
        f"no rig named {name!r} {searched}. The rigs there are:\n"
        + ("\n".join(lines) if lines else "  none")
        + "\nA rig may also be given as the path to its YAML file."
    )


def _is_shared_file(path: Path, shared: Mapping[str, Path] | None) -> bool:
    """Whether ``path`` is one of the shared rigs' own files. Compared by
    resolved location, so a symlink or a relative spelling of one is still
    recognised as alhazen's."""
    target = path.resolve()
    if shared is None:
        return target.parent == SHARED_RIG_DIR
    return any(Path(file).resolve() == target for file in shared.values())


def _extends_name(value: Any, path: Path) -> str:
    """The shared rig an ``extends:`` value names, checked for being a name."""
    name = rig_name(value) if isinstance(value, str) else None
    if name is None:
        raise ConfigError(
            f"{path}: `extends:` must name one of alhazen's shared rigs, as lab or "
            f"alhazen/lab, not {value!r}"
        )
    return name


def rig_extends(path: Path) -> str | None:
    """The name of the shared rig the file at ``path`` extends, or None when
    it extends nothing. Reads the file, so an unreadable one raises the
    loader's ConfigError, naming it."""
    raw = read_mapping(path)
    return _extends_name(raw["extends"], path) if "extends" in raw else None


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """``override`` merged over ``base``: where both hold a mapping under the
    same key, the two are merged the same way, key by key; any other value —
    a number, a string, a list, a null — replaces what ``base`` had.

    Lists replace rather than append because a list in a rig is one setting
    (the photodiode's ``events``), and an experiment writing ``[STIM_ON]``
    means exactly that list, not the shared rig's plus STIM_ON. Neither input
    is modified.
    """
    merged = dict(base)
    for key, value in override.items():
        below = merged.get(key)
        if isinstance(value, Mapping) and isinstance(below, Mapping):
            merged[key] = deep_merge(below, value)
        else:
            merged[key] = value
    return merged


def rig_mapping(path: Path, *, shared: Mapping[str, Path] | None = None) -> MergedRig:
    """A rig file's settings, with the shared rig it ``extends`` merged under
    them, ready to validate. A file without ``extends`` is returned as read.

    The rules, each refused with the file named when broken:

    - ``extends`` names one of alhazen's shared rigs (``lab`` or
      ``alhazen/lab``) — not another of the experiment's rigs, so what a rig
      builds on is one file, the same wherever alhazen is installed;
    - a shared rig extends nothing, so there is never a chain to follow;
    - the file's settings are merged over the shared rig's by
      :func:`deep_merge`: sections merge key by key, and any other value —
      lists included — replaces;
    - an empty mapping (``devices: {}``) is refused. In a whole rig file it
      means "none of these", but merged it would keep every one of the
      shared rig's, silently — the opposite. Removing something is written
      as a null (``devices: {eyetracker: null}``), which replaces.

    ``shared`` replaces alhazen's own shared rigs, name to file (the
    experiment workspace passes the ones the project's alhazen ships).
    """
    own = read_mapping(path)
    if "extends" not in own:
        return MergedRig(own, None, None)
    if _is_shared_file(path, shared):
        raise ConfigError(
            f"{path} is one of alhazen's shared rigs and says `extends:`. A shared rig is a "
            "whole file and extends nothing; only an experiment's rig may extend one"
        )
    name = _extends_name(own["extends"], path)
    files = shared_rig_files() if shared is None else shared
    if name not in files:
        raise ConfigError(
            f"{path} extends {name!r}, which is not one of alhazen's shared rigs "
            f"({', '.join(files) or 'none'}). Only a shared rig can be extended — not another "
            "of the experiment's rigs — so write this rig out whole, or extend one of those"
        )
    base_path = Path(files[name])
    base = read_mapping(base_path)
    if "extends" in base:
        raise ConfigError(
            f"alhazen's shared rig {base_path} says `extends:`; shared rigs extend nothing, "
            f"so {path} cannot build on it. This is an installation fault: reinstall "
            "alhazen-vision"
        )
    body = {key: value for key, value in own.items() if key != "extends"}
    _refuse_empty_mappings(body, path, name, ())
    return MergedRig(deep_merge(base, body), name, base_path)


def _refuse_empty_mappings(
    body: Mapping[str, Any], path: Path, name: str, where: tuple[str, ...]
) -> None:
    """Refuse ``key: {}`` anywhere in a file that extends; see rig_mapping."""
    for key, value in body.items():
        if not isinstance(value, Mapping):
            continue
        dotted = ".".join((*where, str(key)))
        if not value:
            raise ConfigError(
                f"{path}: `{dotted}: {{}}` in a rig that extends {name!r} changes nothing — "
                f"sections are merged key by key, so an empty one keeps everything alhazen's "
                f"{name} has there. To remove one of its entries, set that entry to null "
                f"(e.g. `devices: {{eyetracker: null}}`); to change one, name it"
            )
        _refuse_empty_mappings(value, path, name, (*where, str(key)))


def local_rig_file(ref: RigRef, experiment_root: ExperimentRoot) -> Path:
    """The file that what is measured on this rig's machine is kept beside:
    a gamma fit (``<stem>_gamma.yaml``), measure mode's reports
    (``measurements/``).

    For an experiment rig, its own file, as always. A shared rig's file is
    inside alhazen's installation, which a reinstall replaces and a user may
    not be able to write, so for one of those it is the file an experiment
    rig of that name *would* be — ``<experiment>/configs/rig-<name>.yaml``,
    which need not exist. What is kept beside it is found again by the shared
    rig, and by an experiment rig of that name if one is added later (one
    that extends the shared rig, say), because both look beside that path.

    Raises ConfigError, before anything is measured, when there is no
    experiment ``configs/`` folder to keep it in.
    """
    if ref.source == "experiment":
        return ref.path
    root = experiment_root() if callable(experiment_root) else experiment_root
    configs = root / "configs" if root is not None else None
    if configs is None or not configs.is_dir():
        where = f"there is none in {root}" if root is not None else "no experiment folder is known"
        raise ConfigError(
            f"alhazen's shared rig {ref.name!r} lives inside alhazen's installation, which a "
            "reinstall replaces, so what is measured on it is kept in the experiment's configs/ "
            f"folder instead — and {where}. Run this from the experiment's folder"
        )
    return configs / f"{FILE_PREFIX}{ref.name}.yaml"
