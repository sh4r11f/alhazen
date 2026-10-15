"""The picture of one trial that the live monitor's Trial view draws.

At the start of every trial the session publishes a picture of the trial in
dva (alhazen.display.schematic), and the page draws the eye's position over
it, live. Where the picture comes from:

1. ``TrialPlan.schematic``, when the task supplies one: the task knows its
   stimuli exactly (a dot field, an occluder, a path), and its shapes are
   drawn as given. The plan's regions, the windows gaze is judged against,
   are added as outlines unless the task's picture already carries a shape
   with role ``"region"``: a task that draws its windows itself
   (``region_shapes``) gets exactly what it drew.
2. Otherwise, only the regions. Nothing is derived from the stimuli: a
   generic outline guessed from a renderer's attributes would be a confident
   picture of the wrong thing (a PsychoPy ShapeStim's ``size`` is a scale,
   not a size), so a task that supplies no picture is shown as its gaze
   windows alone, and the page says so once.

Everything is computed between trials, from the plan, before the trial's
first frame — never inside the frame loop.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from alhazen.core.trial import CircleRegion
from alhazen.display.schematic import Circle, Shape
from alhazen.display.screen import Screen

log = logging.getLogger(__name__)


def region_shapes(regions: Mapping[str, CircleRegion], screen: Screen) -> tuple[Circle, ...]:
    """Each region as an outline, in dva, named after its key as a window.

    The key ``fixation`` is named "Fixation window" (underscores read as
    spaces; a key that already ends in "window" is not given a second one),
    and the page shows a shape's name exactly as given.

    Exact: a region is the window the phases judge gaze against, in the same
    centered px, converted with the same ``Screen`` that placed it. A region
    whose geometry cannot be drawn (a non-finite centre, a zero radius) raises
    ``ValueError`` naming it: in a task's own schematic that is a bug in the
    task. The default picture catches it per region instead (``trial_view``).
    """
    return tuple(_region_shape(name, region, screen) for name, region in regions.items())


def region_title(key: object) -> str:
    """A region's key as the name its outline carries: ``fixation`` ->
    "Fixation window", ``target_far`` -> "Target far window"."""
    words = " ".join(str(key).replace("_", " ").split())
    title = words[:1].upper() + words[1:]
    if not title.lower().endswith("window"):
        title = f"{title} window".strip()
    return title[:80]


def _region_shape(name: str, region: CircleRegion, screen: Screen) -> Circle:
    try:
        return Circle(
            (screen.px2deg(region.center[0]), screen.px2deg(region.center[1])),
            screen.px2deg(region.radius),
            name=region_title(name),
            role="region",
        )
    except ValueError as e:
        raise ValueError(f"region {name!r} cannot be drawn: {e}") from e


def _json_value(value: object) -> object:
    """A condition value as the page can print it."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def trial_view(
    *,
    schematic: tuple[Shape, ...] | None,
    regions: Mapping[str, CircleRegion],
    screen: Screen,
    trial_index: int,
    attempt: int,
    condition: Mapping[str, Any],
    t: float,
) -> dict[str, Any]:
    """The Trial view's payload for one trial: its picture and what it is.

    ``source`` is ``"task"`` when the task supplied the picture and
    ``"regions"`` when it did not (the page then says that only the gaze
    windows are shown). ``undrawn`` names a region that could not be drawn,
    with why: the regions alhazen adds are the session's picture, not the
    task's, so a bad one is named on the page and in the log rather than
    ending the session over a picture. ``t`` is the session time it was built
    at, which is how the page says how old the last camera frame is.
    """
    own: tuple[Shape, ...] = schematic if schematic is not None else ()
    windows: list[Shape] = []
    undrawn: list[str] = []
    if not any(shape.role == "region" for shape in own):
        for name, region in regions.items():
            try:
                windows.append(_region_shape(name, region, screen))
            except ValueError as e:
                log.warning("trial view: %s", e)
                undrawn.append(f"{name} (window: {e.__cause__})")
    # Windows over the task's shapes: the page draws in list order, and a
    # window is a dashed outline that must stay visible on a filled stimulus.
    shapes = own + tuple(windows)
    return {
        "trial_index": trial_index,
        "attempt": attempt,
        "t": t,
        "screen": {
            "width_dva": screen.px2deg(screen.width_px),
            "height_dva": screen.px2deg(screen.height_px),
        },
        "source": "task" if schematic is not None else "regions",
        "shapes": [shape.payload() for shape in shapes],
        "undrawn": undrawn,
        "condition": {str(k): _json_value(v) for k, v in condition.items()},
    }
