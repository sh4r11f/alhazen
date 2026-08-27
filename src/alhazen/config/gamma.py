"""Where a measured gamma lives, and how a session finds it.

`alhazen calibrate gamma` fits the display's luminance response and writes it
beside the rig config it belongs to. This is the other half of that loop: the
file's location and its reader, in the config layer, so the session builder
can apply a stored correction without reaching up into the CLI.

Beside the rig config and named after it, because a gamma table is a property
of one physical monitor: following the wrong one is worse than having none.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

GAMMA_FILENAME_SUFFIX = "_gamma.yaml"


def gamma_path(rig_path: Path | str) -> Path:
    """Where a fit for this rig config is stored."""
    rig_path = Path(rig_path)
    return rig_path.with_name(rig_path.stem + GAMMA_FILENAME_SUFFIX)


def write_gamma(rig_path: Path | str, fit: dict[str, float]) -> Path:
    path = gamma_path(rig_path)
    path.write_text(yaml.safe_dump({"schema_version": 1, **fit}, sort_keys=False))
    log.info("gamma fit written to %s", path)
    return path


def load_gamma(rig_path: Path | str) -> dict[str, float] | None:
    """The stored fit for this rig, if one has been measured."""
    path = gamma_path(rig_path)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text()) or {}
    return {key: float(value) for key, value in data.items() if key != "schema_version"}
