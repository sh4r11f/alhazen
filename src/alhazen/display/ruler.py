"""The ruler: a bar of a known angular size, and what it should measure.

A tape measure held against this bar says whether a rig config's screen width
and viewing distance are right — and if they are not, every stimulus size in
every experiment on that rig is wrong by the same factor.

Two callers draw it: ``alhazen calibrate ruler`` (``alhazen.cli.calibrate``,
which opens a window of its own) and ``--mode measure``
(``alhazen.modes.measure``, which reuses the window it measured everything
else through). It lives here, in the display layer, rather than in the CLI
module that drew it first, because modes sits below cli: measure mode
importing the ruler from ``alhazen.cli`` was an upward import, and a cycle
with ``cli.main``, which imports modes. What the ruler needs — the rig's
monitor geometry (config) and ``Screen`` (display) — is all at or below this
layer, so both callers can reach it by importing down.
"""

from __future__ import annotations

import math
from typing import Any

from alhazen.config.models import RigConfig
from alhazen.display.screen import Screen


def ruler_report(rig: RigConfig, size_dva: float = 10.0) -> str:
    """What a bar of ``size_dva`` should measure on this rig, in centimetres.

    The arithmetic is the rig config's own: if the printed number does not
    match a tape measure held against the screen, the config is wrong, not
    the ruler.
    """
    screen = Screen.from_monitor(rig.monitor)
    width_px = screen.deg2px(size_dva)
    cm_per_px = rig.monitor.width_cm / rig.monitor.width_px
    width_cm = width_px * cm_per_px
    # The same length worked out from the geometry directly, as a check on
    # the linear approximation the Screen model uses.
    exact_cm = 2.0 * rig.monitor.distance_cm * math.tan(math.radians(size_dva / 2.0))
    return "\n".join(
        [
            f"a {size_dva:g} dva bar on this rig:",
            f"  {width_px:.1f} px wide",
            f"  {width_cm:.2f} cm on the panel  (measure this with a tape)",
            f"  {exact_cm:.2f} cm by exact trigonometry at {rig.monitor.distance_cm:g} cm",
            f"  px per degree: {screen.px_per_deg:.2f}",
            "",
            "If the tape disagrees with the second line, fix monitor.width_cm or",
            "monitor.distance_cm in the rig config — every stimulus size on this rig",
            "is scaled by that same error until you do.",
        ]
    )


def draw_ruler_on(display: Any, rig: RigConfig, size_dva: float = 10.0) -> None:
    """Draw the bar in an already-open display until a key is pressed.

    The half of :func:`alhazen.cli.calibrate.draw_ruler` that measure mode can
    reuse on the window it already has: the same bar, ticks and label, without
    opening a second display to draw them in.
    """
    # psychopy stays a use-time import (the lazy-vendor-imports invariant):
    # importing this module, as measure mode and the CLI both do, must work
    # on a machine with no renderer installed.
    from psychopy import event, visual

    screen = Screen.from_monitor(rig.monitor)
    width_px = screen.deg2px(size_dva)
    # A white bar of the computed width, plus end ticks, on black: the
    # thing a tape measure is held against. Drawn in pixels, because the
    # px<->cm question is exactly what is being checked.
    bar = visual.Rect(
        display.window,
        units="pix",
        width=width_px,
        height=max(round(screen.height_px * 0.02), 4),
        fillColor="white",
        lineColor="white",
    )
    tick_height = max(round(screen.height_px * 0.10), 20)
    ticks = [
        visual.Rect(
            display.window,
            units="pix",
            width=2,
            height=tick_height,
            pos=(offset, 0),
            fillColor="white",
            lineColor="white",
        )
        for offset in (-width_px / 2.0, width_px / 2.0)
    ]
    label = visual.TextStim(
        display.window,
        units="pix",
        text=(
            f"{size_dva:g} dva = {width_px:.1f} px\n"
            f"measure between the ticks: it should be "
            f"{width_px * rig.monitor.width_cm / rig.monitor.width_px:.2f} cm\n"
            f"any key to close"
        ),
        pos=(0, -tick_height),
        height=max(round(screen.height_px * 0.025), 12),
        color="white",
    )
    # Drop whatever is already in psychopy's global key buffer. This used to
    # run in a process that had just opened its own window, so the buffer was
    # empty; `--mode measure` calls it on a window that has already collected
    # presses from the earlier measurements, and one of those left over would
    # end the ruler before a single flip — a black screen, and a report saying
    # a bar was drawn.
    event.clearEvents()
    while not event.getKeys():
        bar.draw()
        for tick in ticks:
            tick.draw()
        label.draw()
        display.flip()
