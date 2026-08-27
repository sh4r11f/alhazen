"""The stimulus contract.

Experiments code against this protocol, never against a renderer's classes
directly — that is the seam that keeps the display backend swappable.
Concrete stimuli convert dva to pixels once, at
construction, via ``Screen``, and import their renderer lazily so importing
a stimulus module stays safe on a machine with no display stack installed.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Stimulus(Protocol):
    def update(self, dt: float) -> None:
        """Advance time-varying state by dt seconds (the engine passes the
        measured duration of the previously-shown frame)."""
        ...

    def draw(self) -> None:
        """Draw into the display's back buffer for the upcoming flip."""
        ...


class NullStimulus:
    """A stimulus-shaped nothing, for simulated sessions: records how often
    it was drawn so tests and QA can still assert on presentation counts."""

    def __init__(self, name: str = "null") -> None:
        self.name = name
        self.draw_count = 0
        self.updates: list[float] = []

    def update(self, dt: float) -> None:
        self.updates.append(dt)

    def draw(self) -> None:
        self.draw_count += 1
