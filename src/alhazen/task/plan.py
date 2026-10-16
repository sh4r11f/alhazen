"""What a task is handed to build a trial, and what it hands back.

These two live here rather than with the session runner because the *task*
layer is what speaks them: ``build_trial`` is a task's method, and the runner
is simply its caller. Keeping them below the session layer is also what lets
``Task`` type its own contract without importing the thing that runs it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from alhazen.config.models import SessionConfig
from alhazen.core.trial import CircleRegion
from alhazen.display.backend import DisplayBackend
from alhazen.display.schematic import Shape, check_shapes
from alhazen.display.screen import Screen
from alhazen.paradigms.base import Condition


@dataclass(frozen=True)
class TrialSetup:
    """Everything needed to place and construct one trial: no bus, no
    tracker, no command source.

    It does carry ``display`` — the window — because constructing a
    stimulus needs one (``make_fixation(setup.display, ...)``). That is for
    ``build_trial`` to build stimuli with, and nothing else: drawing and
    flipping are the engine's, and a phase never sees the window (the
    TrialContext does not carry it)."""

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
    from this, so trial records stay uniform across experiments.

    ``schematic`` is optional: the trial's picture for the live monitor's
    Trial view, as shapes in dva (alhazen.display.schematic), drawn exactly
    as given; the plan's regions are added as outlines unless the picture
    carries a shape with role "region" itself. Left None, the session draws
    only the regions, nothing derived from the stimuli (task/schematic.py).
    Checked here, so a malformed one fails at ``build_trial`` rather than as
    a blank picture."""

    phases: list[Any]
    stimuli: dict[str, Any] = field(default_factory=dict)
    regions: dict[str, CircleRegion] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=dict)
    schematic: Sequence[Shape] | None = None

    def __post_init__(self) -> None:
        if self.schematic is not None:
            self.schematic = check_shapes(self.schematic, "TrialPlan.schematic")


BuildTrial = Callable[[TrialSetup], TrialPlan]
