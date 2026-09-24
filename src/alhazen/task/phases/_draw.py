"""The per-frame "update, then draw" every phase does for its stimuli.

One helper rather than the same three-line loop in every phase module: the
order matters (a stimulus is advanced by this frame's ``dt`` before it is
drawn, so what is drawn is where it is *now*), and the order a phase lists
its keys in is the order they are drawn in, later ones on top. Written once,
neither can drift between phases.
"""

from __future__ import annotations

from collections.abc import Iterable

from alhazen.core.trial import TrialContext


def draw_stimuli(ctx: TrialContext, keys: Iterable[str]) -> None:
    """Update each named stimulus by this frame's ``ctx.dt``, then draw it,
    in the order given — so a later key is drawn on top of an earlier one.

    A name the trial has no stimulus for raises ``KeyError`` from
    ``ctx.stimuli``, as it always has: a phase asked to draw something that
    does not exist is a task bug, and it must not pass as an empty frame.
    """
    for key in keys:
        stimulus = ctx.stimuli[key]
        stimulus.update(ctx.dt)
        stimulus.draw()
