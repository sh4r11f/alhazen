"""YAML loading and config assembly.

Loading is generic over pydantic models so experiment packages get the same
loud, file-naming validation for their own params models that alhazen's rig
config gets. ``build_session_config`` is the one place the layers (rig file,
task params file, identity) merge into a `SessionConfig`, recording where
each layer came from.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from alhazen.config.models import RigConfig, SessionConfig, SessionInfo
from alhazen.errors import ConfigError

M = TypeVar("M", bound=BaseModel)


def load_model(path: str | Path, model: type[M]) -> M:
    """Load one YAML file into one pydantic model, converting both YAML and
    validation failures into a ConfigError that names the file — the
    experimenter fixes a file, so the error must say which one.

    That includes a file that cannot be read at all: one that is not UTF-8,
    a directory, or one this user may not open. Those used to escape as the
    raw OS or codec error, naming at most a byte offset."""
    raw = read_mapping(path)
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid config in {path}:\n{e}") from e


def read_mapping(path: str | Path) -> dict[str, Any]:
    """One YAML file's top-level mapping, unvalidated, with every way the
    file itself can be wrong turned into a ConfigError naming it.

    ``load_model`` is this plus validation. It is its own function because a
    rig that ``extends`` a shared one is two files merged before anything is
    validated (``alhazen.config.rigs``), and both files must fail with the
    same words a single file does. An empty file is an empty mapping.
    """
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except UnicodeDecodeError as e:
        # The usual cause is a ° or µ typed into an editor that saves as
        # Windows-1252 ("ANSI"): one byte, not UTF-8's two. The offending
        # byte is quoted so it can be found in a hex view if need be.
        bad = e.object[e.start : e.start + 1]
        raise ConfigError(
            f"{path} is not UTF-8 text (byte {bad!r} at offset {e.start}). Re-save it as "
            f"UTF-8 — a ° or µ typed in an editor that saves as ANSI/Windows-1252 is the "
            f"usual cause"
        ) from e
    except OSError as e:
        # After FileNotFoundError, which has its own message. A directory
        # reads as IsADirectoryError on POSIX but PermissionError on
        # Windows, so it is recognised by looking, not by the error's type.
        if path.is_dir():
            raise ConfigError(f"{path} is a directory, not a config file") from e
        raise ConfigError(f"cannot read config file {path}: {e.strerror or e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path} must contain a mapping at the top level, got {type(raw).__name__}"
        )
    return raw


def load_rig(path: str | Path, *, shared_rigs: Mapping[str, Path] | None = None) -> RigConfig:
    """A rig config from its file, with the monitor named after the file.

    The file may begin with ``extends: <name>``, naming one of alhazen's
    shared rigs (``alhazen/rigs/``); it is then that rig with this file's
    settings merged over it (``alhazen.config.rigs.rig_mapping`` has the
    rules), and the merged result is what is validated. ``shared_rigs``
    replaces alhazen's own shared rigs, name to file: the experiment
    workspace passes the ones the *project's* alhazen ships, which need not
    be the workspace's. Every other caller leaves it None.

    The monitor's name is what PsychoPy's monitor database, Monitor Center
    and every window opened on this machine look the panel up by, so two rig
    files sharing a name share one registration and overwrite each other's
    geometry. A rig file is one machine, so its stem — ``rig-lab``,
    ``rig-vpixx`` — is the right default, and one an experimenter never has
    to think about. A ``monitor.name`` written in the file (or in the shared
    rig it extends) still wins. For a file that extends, the stem is this
    file's: it names the machine, and the shared rig it builds on is the
    same machine by construction.
    """
    # Imported here rather than at the top: rigs.py reads its files through
    # read_mapping above, so a module-level import would be a cycle.
    from alhazen.config.rigs import rig_mapping

    path = Path(path)
    merged = rig_mapping(path, shared=shared_rigs)
    try:
        rig = RigConfig.model_validate(merged.values)
    except ValidationError as e:
        # An error in the merged result may come from either file, so both
        # are named: the experiment's first, since that is the one usually
        # being edited.
        where = str(path)
        if merged.base is not None:
            where += f" (which extends alhazen's shared rig '{merged.extends}', {merged.base})"
        raise ConfigError(f"invalid config in {where}:\n{e}") from e
    if "name" not in rig.monitor.model_fields_set:
        monitor = rig.monitor.model_copy(update={"name": path.stem})
        rig = rig.model_copy(update={"monitor": monitor})
    return rig


def load_params(path: str | Path, model: type[M]) -> M:
    """Load an experiment's task-params file against the experiment's own
    pydantic model. Same contract as the rig loader: typos fail loudly.

    An alias of :func:`load_model`, and nothing more. It stays because it is
    public (listed in the API reference) and in use: every scaffolded
    package's tests call it, and so does at least one experiment repository.
    Removing it would be a breaking change bought for no behaviour at all."""
    return load_model(path, model)


def build_session_config(
    rig: RigConfig,
    info: SessionInfo,
    task_params: BaseModel,
    sources: dict[str, str],
) -> SessionConfig:
    return SessionConfig(
        rig=rig,
        info=info,
        task_params=task_params.model_dump(mode="json"),
        sources=dict(sources),
    )
