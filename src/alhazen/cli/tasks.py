"""Finding installed tasks by name.

An experiment package registers its Task under the ``alhazen.tasks`` entry
point group, so ``alhazen run --task saccade-bias`` works from anywhere once
the package is installed. That is what makes the CLI usable on a rig: the
experimenter does not have to remember where the code lives or what the
class is called.
"""

from __future__ import annotations

import logging
from importlib import metadata
from typing import Any

from alhazen.errors import ConfigError

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "alhazen.tasks"


def installed_tasks() -> dict[str, metadata.EntryPoint]:
    """Every registered task, by the name it registered under."""
    return {point.name: point for point in metadata.entry_points(group=ENTRY_POINT_GROUP)}


def load_task_class(name: str) -> Any:
    """The Task subclass registered under this name.

    A missing name lists what IS installed: on a rig, "no such task" with no
    further information is a dead end, and the answer is almost always that
    the package was never installed into this environment.
    """
    points = installed_tasks()
    if name not in points:
        available = sorted(points)
        raise ConfigError(
            f"no installed task named {name!r}. Installed tasks: "
            f"{available if available else 'none — install an experiment package first'}. "
            f'A package registers one under [project.entry-points."alhazen.tasks"].'
        )
    loaded = points[name].load()
    # The entry point may name a class or a factory; both are acceptable, and
    # what matters is that what comes back builds from a params model.
    if not hasattr(loaded, "params_model"):
        raise ConfigError(
            f"the entry point for {name!r} loaded {loaded!r}, which is not a Task "
            f"subclass (it has no params_model)"
        )
    return loaded
