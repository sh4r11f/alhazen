"""YAML loading and config assembly.

Loading is generic over pydantic models so experiment packages get the same
loud, file-naming validation for their own params models that alhazen's rig
config gets. ``build_session_config`` is the one place the layers (rig file,
task params file, identity) merge into a `SessionConfig`, recording where
each layer came from.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from alhazen.config.models import RigConfig, SessionConfig, SessionInfo
from alhazen.errors import ConfigError

M = TypeVar("M", bound=BaseModel)


def load_model(path: str | Path, model: type[M]) -> M:
    """Load one YAML file into one pydantic model, converting both YAML and
    validation failures into a ConfigError that names the file — the
    experimenter fixes a file, so the error must say which one."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path} must contain a mapping at the top level, got {type(raw).__name__}"
        )
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid config in {path}:\n{e}") from e


def load_rig(path: str | Path) -> RigConfig:
    return load_model(path, RigConfig)


def load_params(path: str | Path, model: type[M]) -> M:
    """Load an experiment's task-params file against the experiment's own
    pydantic model. Same contract as the rig loader: typos fail loudly."""
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
