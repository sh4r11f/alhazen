"""A fixation point — the first concrete stimulus, and the template for the
rest of the library: dva-in, pixels-once, renderer imported lazily inside
construction, simulated backend gets a recording stand-in from the factory.
"""

from __future__ import annotations

from typing import Any

from alhazen.display.backend import DisplayBackend
from alhazen.display.screen import Screen
from alhazen.stimuli.base import NullStimulus, Stimulus


class FixationPoint:
    """A filled circle at a centered-px position (default: screen center)."""

    def __init__(
        self,
        display: DisplayBackend,
        screen: Screen,
        size_dva: float,
        fill_color: tuple[float, float, float] = (1.0, 1.0, 1.0),
        pos: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        from psychopy import visual

        self._stim: Any = visual.Circle(
            display.window,
            radius=screen.deg2px(size_dva) / 2.0,
            pos=pos,
            fillColor=fill_color,
            lineColor=fill_color,
            units="pix",
        )

    def update(self, dt: float) -> None:
        # Static stimulus; nothing advances.
        return

    def draw(self) -> None:
        self._stim.draw()


def make_fixation(
    display: DisplayBackend,
    screen: Screen,
    size_dva: float,
    fill_color: tuple[float, float, float] = (1.0, 1.0, 1.0),
    pos: tuple[float, float] = (0.0, 0.0),
) -> Stimulus:
    """Backend-appropriate fixation point: the real thing on a rendering
    backend, a recording NullStimulus on the simulated one — so the same
    task code runs unchanged on a laptop with nothing installed."""
    if display.kind == "simulated":
        return NullStimulus("fixation")
    return FixationPoint(display, screen, size_dva, fill_color, pos)
