"""The trial vocabulary: outcomes, phases, regions, inputs, and the context
threaded through every frame.

This is the generalization at the heart of alhazen. Rather than the engine
hard-coding one experiment's phases and outcomes, the *experiment* declares
its outcomes and composes its phases, and the engine relies only on the
small contracts defined in this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np

from alhazen.core.clock import Clock
from alhazen.display.screen import Screen, within_radius

# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """A terminal trial result, as the experiment defines it.

    ``completed`` is the one flag the framework itself interprets: a
    completed trial produced its measurement and consumes a scheduled
    repetition; a non-completed one must be re-served by the scheduler
    (paradigms/base.py). ``success`` drives feedback/reward policy and is
    None for outcomes where the notion doesn't apply.
    """

    name: str
    completed: bool
    success: bool | None = None


# Framework-reserved outcomes, produced by the engine (never by a phase):
# both are non-completed by definition — the trial ended before its
# measurement existed. PAUSED additionally writes no trials row (the runner
# enforces that split; see session/runner.py).
PAUSED = Outcome("PAUSED", completed=False)
ABORTED = Outcome("ABORTED", completed=False)
_RESERVED_OUTCOMES = {"PAUSED": PAUSED, "ABORTED": ABORTED}


_OUTCOME_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class OutcomeSet:
    """An experiment's declared outcomes plus the reserved ones, with
    attribute access (``outcomes.CORRECT``) for phase code."""

    def __init__(self, declared: dict[str, Outcome]) -> None:
        for name in declared:
            if name in _RESERVED_OUTCOMES:
                raise ValueError(f"outcome name {name!r} is reserved by alhazen")
            if not _OUTCOME_NAME_RE.match(name):
                raise ValueError(
                    f"outcome name {name!r} must be UPPER_SNAKE_CASE (it becomes a "
                    f"trials.csv value and an attribute on this set)"
                )
        self._by_name = {**declared, **_RESERVED_OUTCOMES}
        for name, outcome in self._by_name.items():
            setattr(self, name, outcome)

    def __getattr__(self, name: str) -> Outcome:
        # Only reached for names __init__ never set — declared outcomes are
        # real attributes. Exists so static checkers accept OUTCOMES.CORRECT.
        raise AttributeError(name)

    def __getitem__(self, name: str) -> Outcome:
        return self._by_name[name]

    def __iter__(self):
        return iter(self._by_name.values())

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._by_name)


def outcomes(**declared: dict[str, Any]) -> OutcomeSet:
    """Declare an experiment's outcomes:

    OUTCOMES = outcomes(
        CORRECT=dict(completed=True, success=True),
        FIX_BREAK=dict(completed=False),
        MISSED_TARGET=dict(completed=True, success=False),
    )
    """
    built = {}
    for name, spec in declared.items():
        unknown = set(spec) - {"completed", "success"}
        if unknown:
            raise ValueError(f"outcome {name!r}: unknown keys {sorted(unknown)}")
        if "completed" not in spec:
            raise ValueError(f"outcome {name!r} must declare completed=True/False")
        built[name] = Outcome(name=name, **spec)
    return OutcomeSet(built)


# ---------------------------------------------------------------------------
# Regions and per-frame inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CircleRegion:
    """A named screen region in centered px. The None rule is the blink rule:
    an unverifiable position (track loss, blink) is *outside every region* —
    fixation is only credited when it can actually be verified."""

    center: tuple[float, float]
    radius: float

    def contains(self, point: tuple[float, float] | None) -> bool:
        if point is None:
            return False
        return within_radius(point, self.center, self.radius)


@dataclass(frozen=True)
class InputFrame:
    """This frame's input snapshot, fetched once per frame by the engine so
    phases never touch a device.

    ``gaze`` is centered px (y-up) or None (unverifiable — the blink rule).
    ``keys`` are the subject's key presses since the last frame, and
    ``wheel`` the scroll-wheel movement over that frame (an adjustment
    task's knob). Fields only ever get appended, with defaults, so a phase
    or a test that cares about one of them is unaffected by the others.
    """

    gaze: tuple[float, float] | None = None
    keys: tuple[str, ...] = ()
    wheel: float = 0.0


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


class PhaseAction:
    """Per-frame instruction back to the engine. A phase's ``on_frame``
    returns CONTINUE, ADVANCE, or an Outcome that ends the whole trial."""

    CONTINUE = "CONTINUE"
    ADVANCE = "ADVANCE"


@runtime_checkable
class Phase(Protocol):
    """One step of a trial's state machine. Deliberately "dumb": a phase only
    reads/mutates the TrialContext and queues events via
    ``ctx.emit_on_flip`` — it never sees hardware, the bus, or the window.
    That separation is what makes phase logic testable with a fake clock and
    no display."""

    name: str

    def on_enter(self, ctx: TrialContext) -> None: ...

    def on_frame(self, ctx: TrialContext) -> str | Outcome: ...


# ---------------------------------------------------------------------------
# The context
# ---------------------------------------------------------------------------


@dataclass
class TrialContext:
    """Everything a phase may touch, built fresh per trial by the runner.

    ``record`` accumulates the trial's row for trials.csv; ``stimuli`` and
    ``regions`` are the task's named drawables and named windows; ``extras``
    is task-private scratch that never reaches the data files.
    """

    clock: Clock
    screen: Screen
    rng: np.random.Generator
    trial_index: int
    params: dict[str, Any]
    stimuli: dict[str, Any] = field(default_factory=dict)
    regions: dict[str, CircleRegion] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    inputs: InputFrame = field(default_factory=InputFrame)
    dt: float = 1 / 60  # duration of the previously-shown frame; set by the engine
    pending_flip_events: list[tuple[str, dict]] = field(default_factory=list)

    def emit_on_flip(self, name: str, payload: dict | None = None) -> None:
        """Queue an event to be emitted right after the next flip, stamped
        with the flip's time — the photon-honest timestamp for anything
        visual. The engine drains this queue; phases never emit directly."""
        self.pending_flip_events.append((name, payload or {}))
