"""What a task is handed to build a trial, and what it hands back.

These two live here rather than with the session runner because the *task*
layer is what speaks them: ``build_trial`` is a task's method, and the runner
is simply its caller. Keeping them below the session layer is also what lets
``Task`` type its own contract without importing the thing that runs it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from alhazen.config.models import SessionConfig
from alhazen.core.trial import CircleRegion
from alhazen.display.backend import DisplayBackend
from alhazen.display.screen import Screen
from alhazen.paradigms.base import Condition


@dataclass(frozen=True)
class TrialSetup:
    """Everything needed to place and construct one trial, and nothing that
    could reach hardware: no bus, no tracker, no command source."""

    cfg: SessionConfig
    screen: Screen
    display: DisplayBackend
    rng: np.random.Generator
    refresh_rate_hz: float
    trial_index: int
    attempt: int
    condition: Condition


@dataclass
class TrialPlan:
    """What ``build_trial`` returns. The runner assembles the TrialContext
    from this, so trial records stay uniform across experiments."""

    phases: list[Any]
    stimuli: dict[str, Any] = field(default_factory=dict)
    regions: dict[str, CircleRegion] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=dict)


BuildTrial = Callable[[TrialSetup], TrialPlan]
