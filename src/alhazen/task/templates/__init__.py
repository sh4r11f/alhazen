"""Ready-made tasks an experiment can run, subclass, or crib from.

Everything else in the task layer is parts — phases, reward policy, the
Task contract. This package holds whole *tasks*: the procedures every lab
needs before its own experiment can begin, written once against the same
machinery an experiment would use, registered under the ``alhazen.tasks``
entry-point group so ``alhazen run --task rf-map-v1`` works with nothing
installed but alhazen itself.

A downstream experiment uses a template three ways, in increasing order of
involvement: run it as-is with a params YAML; subclass a preset to change
defaults or add analysis; or import its pieces (a phase, the live map) into
a task of its own.
"""

from alhazen.task.templates.rf_mapping import (
    MTRFMapTask,
    RFMapParams,
    RFMapTask,
    V1RFMapTask,
    V2RFMapTask,
    V4RFMapTask,
)

__all__ = [
    "MTRFMapTask",
    "RFMapParams",
    "RFMapTask",
    "V1RFMapTask",
    "V2RFMapTask",
    "V4RFMapTask",
]
