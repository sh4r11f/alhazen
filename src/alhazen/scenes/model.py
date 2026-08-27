"""The scene format, as validated models.

A scene is JSON: a background colour and layers drawn back to front. Every
numeric field may be a literal or ``{"expr": "..."}``, which is what makes a
scene animate without any code.

alhazen renders a documented **subset** of the format. Anything outside it is
rejected by name and by path when the scene is loaded (loader.py) rather than
drawn wrongly — a scene that silently lost its text or its blend mode would
be a stimulus nobody could trust.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

# The format version this understands. A bumped version means the studio
# changed something, and its own migration is what should run first.
SUPPORTED_VERSION = 1

# What alhazen draws. Everything else in the format is out of subset.
SUPPORTED_PRIMITIVES = frozenset(
    {
        "rect",
        "circle",
        "ellipse",
        "line",
        "polygon",
        "stripes",
        "grating",
        "dotField",
        "noise",
        "group",
    }
)

# Deliberately absent, with the reason each is out:
#   text          — depends on installed fonts, so it cannot be reproduced
#   pixelProgram  — per-pixel interpreted code; a dedicated primitive is
#                   faster and every use so far has one
#   path, wedge, repeat — no experiment has needed them yet
#   ref()         — cross-layer reads make evaluation order load-bearing
OUT_OF_SUBSET_PRIMITIVES = frozenset({"text", "pixelProgram", "path", "wedge", "repeat"})


class SceneModel(BaseModel):
    """Base for the scene models.

    Unlike alhazen's config models, unknown keys are *allowed*: a scene is
    written by another tool, which may carry fields for its editor that a
    renderer has no business rejecting. What is rejected is an unknown
    primitive TYPE, which would change what is drawn.
    """

    model_config = ConfigDict(extra="allow")


class Transform(SceneModel):
    """A layer's placement: translate, then rotate, then scale, about origin."""

    translate: list[Any] | None = None  # [x, y], each a number or an expression
    rotate: Any | None = None  # radians
    scale: Any | None = None  # a number, or [sx, sy]
    origin: list[Any] | None = None  # the point rotation and scale are about


class Layer(SceneModel):
    """One drawable, with its own visibility, opacity and transform."""

    id: str | None = None
    visible: Any = None
    opacity: Any = None
    blend: str | None = None  # out of subset; rejected at load
    clip: dict[str, Any] | None = None  # out of subset; rejected at load
    element: dict[str, Any] = Field(default_factory=dict)


class Scene(SceneModel):
    version: int
    background: Any = None
    # The scene's own logical canvas, in the format's own field names. A scene
    # that declares one is drawn at that size and letterboxed onto the screen;
    # one that does not is drawn at the screen's size.
    width: int | None = None
    height: int | None = None
    layers: list[Layer] = Field(default_factory=list)

    def declared_size(self) -> tuple[int, int] | None:
        """The scene's logical canvas, or None if it declares none.

        alhazen briefly read this from an invented ``canvas: {width, height}``
        block instead of from the format's own top-level fields. Files written
        against that are still accepted — and say so — because a scene file
        outlives the version of alhazen that read it.
        """
        if self.width and self.height:
            return int(self.width), int(self.height)
        legacy = (self.model_extra or {}).get("canvas") or {}
        if legacy.get("width") and legacy.get("height"):
            log.warning(
                "this scene declares its size in a 'canvas' block; the scene format's own "
                "top-level 'width' and 'height' are what alhazen reads now. Move them up a "
                "level — the block is accepted for compatibility and will be dropped."
            )
            return int(legacy["width"]), int(legacy["height"])
        return None
