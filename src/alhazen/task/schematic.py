"""The picture of one trial that the live monitor's Trial view draws.

At the start of every trial the session publishes what the subject's screen
holds, in dva (alhazen.display.schematic): the stimuli, and the regions a gaze
is judged against. The page draws the eye's position over it, live.

Where that picture comes from, in order:

1. ``TrialPlan.schematic``, when the task supplies one. The task knows its
   stimuli exactly — a dot field, an occluder, a path — and its picture
   replaces the default whole. ``region_shapes`` builds the default's region
   outlines, for a task that wants them in its own picture.
2. Otherwise, the default: every stimulus that can describe its own outline
   (an optional ``schematic(screen)`` method, as alhazen's ``FixationPoint``
   has), then every region of the plan as an outline. A stimulus with no such
   method is NOT guessed at: its name is listed as undrawn, and the page says
   so. Reading a renderer's private attributes would draw a confident outline
   in the wrong place for every stimulus whose size does not mean what its
   attribute name suggests (a PsychoPy ShapeStim's ``size`` is a scale, not a
   size).

Everything is computed between trials, from the plan, before the trial's
first frame — never inside the frame loop.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from typing import Any

from alhazen.core.trial import CircleRegion
from alhazen.display.schematic import Circle, Shape, check_shapes
from alhazen.display.screen import Screen

log = logging.getLogger(__name__)


def region_shapes(regions: Mapping[str, CircleRegion], screen: Screen) -> tuple[Circle, ...]:
    """Each region as an outline, in dva, named by its key.

    Exact: a region is the window the phases judge gaze against, in the same
    centered px, converted with the same ``Screen`` that placed it. A region
    whose geometry cannot be drawn (a non-finite centre, a zero radius) raises
    ``ValueError`` naming it: in a task's own schematic that is a bug in the
    task. The default picture catches it per region instead (``trial_view``).
    """
    return tuple(_region_shape(name, region, screen) for name, region in regions.items())


def _region_shape(name: str, region: CircleRegion, screen: Screen) -> Circle:
    try:
        return Circle(
            (screen.px2deg(region.center[0]), screen.px2deg(region.center[1])),
            screen.px2deg(region.radius),
            name=str(name)[:80],
            role="region",
        )
    except ValueError as e:
        raise ValueError(f"region {name!r} cannot be drawn: {e}") from e


def stimulus_shapes(
    stimuli: Mapping[str, Any], screen: Screen
) -> tuple[tuple[Shape, ...], tuple[str, ...]]:
    """(shapes, undrawn): every stimulus that describes its own outline, named
    by its key, and the keys of those that do not.

    A stimulus describes itself through an optional ``schematic(screen)``
    method returning shapes in dva. What it returns is checked like a task's
    schematic: a malformed outline is a bug in that stimulus, raised here.
    """
    shapes: list[Shape] = []
    undrawn: list[str] = []
    for name, stimulus in stimuli.items():
        describe = getattr(stimulus, "schematic", None)
        if not callable(describe):
            undrawn.append(str(name))
            continue
        own = check_shapes(describe(screen), f"stimulus {name!r} schematic()")
        shapes.extend(
            dataclasses.replace(shape, name=str(name)[:80]) if shape.name is None else shape
            for shape in own
        )
    return tuple(shapes), tuple(undrawn)


def _json_value(value: object) -> object:
    """A condition value as the page can print it."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def trial_view(
    *,
    schematic: tuple[Shape, ...] | None,
    stimuli: Mapping[str, Any],
    regions: Mapping[str, CircleRegion],
    screen: Screen,
    trial_index: int,
    attempt: int,
    condition: Mapping[str, Any],
    t: float,
) -> dict[str, Any]:
    """The Trial view's payload for one trial: its picture and what it is.

    ``t`` is the session time it was built at, which is how the page says how
    old the last camera frame is.
    """
    if schematic is not None:
        shapes: tuple[Shape, ...] = schematic
        undrawn: tuple[str, ...] = ()
        source = "task"
    else:
        drawn, undrawn = stimulus_shapes(stimuli, screen)
        outlines: list[Shape] = []
        for name, region in regions.items():
            try:
                outlines.append(_region_shape(name, region, screen))
            except ValueError as e:
                # The default picture is the session's, not the task's: a
                # region it cannot draw is named on the page, with why, and
                # in the log, rather than ending the session over a picture.
                log.warning("trial view: %s", e)
                undrawn += (f"{name} (window: {e.__cause__})",)
        shapes = drawn + tuple(outlines)
        source = "default"
    return {
        "trial_index": trial_index,
        "attempt": attempt,
        "t": t,
        "screen": {
            "width_dva": screen.px2deg(screen.width_px),
            "height_dva": screen.px2deg(screen.height_px),
        },
        "source": source,
        "shapes": [shape.payload() for shape in shapes],
        "undrawn": list(undrawn),
        "condition": {str(k): _json_value(v) for k, v in condition.items()},
    }
