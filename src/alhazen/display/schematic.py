"""A drawing vocabulary for the operator's picture of a trial, in dva.

The live monitor's Trial view draws the screen the subject is looking at — the
fixation point, the stimuli, the windows a gaze is judged against — so the
experimenter can see where the eye is relative to what is on it. This module
is the vocabulary that picture is written in: six shapes, every coordinate in
degrees of visual angle, centred on the screen, y up — the frame stimulus
placement already uses, through ``Screen.deg2px``.

It sits in the display layer, the lowest one that both a stimulus (which may
describe its own outline: ``FixationPoint.schematic``) and a task (which may
hand the whole picture over: ``TrialPlan.schematic``) can import.

A shape checks itself when it is built, and refuses what it cannot draw — a
non-finite coordinate, a non-positive size, a polygon of two points, an
unknown role — with a ``ValueError`` naming the field. A task that builds a
malformed schematic has a bug, and it is met at ``build_trial``, on the first
trial, not as a blank picture on the operator's screen.

What a shape is for, and so how the page draws it, is its ``role``:

- ``stimulus`` — something drawn on the subject's screen;
- ``region`` — a window a gaze is judged against (fixation window, landing
  zone); drawn as an outline, never filled, because nothing is on the screen
  there;
- ``annotation`` — a mark that is on no screen at all (a path, a label).

``approximate=True`` says the outline is not exact — a bounding shape, or the
position a moving stimulus starts from — and the page draws it dashed with
the word "approximate" beside its name. A picture must never pass off a
guess as a measurement.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# What a shape is for; see the module docstring.
ROLES = ("stimulus", "region", "annotation")
# The most points one schematic may carry, across all its shapes. A dot field
# of a few hundred dots is the largest real case; the bound keeps one publish
# small (it travels to the page at the start of every trial).
MAX_POINTS = 5000
# The longest label and shape name. A label names a thing on a small picture.
MAX_TEXT = 80

Point = tuple[float, float]


def _finite(value: object, what: str) -> float:
    """``value`` as a float, or a ValueError naming ``what``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{what} must be finite, got {number!r}")
    return number


def _point(value: object, what: str) -> Point:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 2:
        raise ValueError(f"{what} must be an (x, y) pair in dva, got {value!r}")
    return (_finite(value[0], f"{what} x"), _finite(value[1], f"{what} y"))


def _points(values: object, what: str, minimum: int) -> tuple[Point, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{what} must be a sequence of (x, y) pairs, got {values!r}")
    points = tuple(_point(p, f"{what}[{i}]") for i, p in enumerate(values))
    if len(points) < minimum:
        raise ValueError(f"{what} needs at least {minimum} point(s), got {len(points)}")
    if len(points) > MAX_POINTS:
        raise ValueError(
            f"{what} has {len(points)} points; a schematic carries at most {MAX_POINTS}"
        )
    return points


def _positive(value: object, what: str) -> float:
    number = _finite(value, what)
    if number <= 0:
        raise ValueError(f"{what} must be greater than 0, got {number!r}")
    return number


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string, got {value!r}")
    if len(value) > MAX_TEXT:
        raise ValueError(f"{what} is {len(value)} characters; at most {MAX_TEXT}")
    return value


@dataclass(frozen=True)
class _Shape:
    """What every shape carries besides its geometry."""

    name: str | None = field(default=None, kw_only=True)
    role: str = field(default="stimulus", kw_only=True)
    approximate: bool = field(default=False, kw_only=True)

    kind = ""

    def _check_common(self) -> None:
        if self.name is not None:
            _text(self.name, f"{self.kind} name")
        if self.role not in ROLES:
            raise ValueError(f"{self.kind} role must be one of {ROLES}, got {self.role!r}")
        if not isinstance(self.approximate, bool):
            raise ValueError(
                f"{self.kind} approximate must be True or False, got {self.approximate!r}"
            )

    def n_points(self) -> int:
        return 1

    def payload(self) -> dict[str, Any]:
        """The shape as the page reads it: plain JSON, its kind first."""
        out: dict[str, Any] = {"kind": self.kind, "role": self.role}
        if self.name is not None:
            out["name"] = self.name
        if self.approximate:
            out["approximate"] = True
        out.update(self._geometry())
        return out

    def _geometry(self) -> dict[str, Any]:  # pragma: no cover - every shape overrides
        raise NotImplementedError


@dataclass(frozen=True)
class Circle(_Shape):
    """A disc or ring: ``center`` (dva) and ``radius`` (dva)."""

    center: Point
    radius: float
    kind = "circle"

    def __post_init__(self) -> None:
        object.__setattr__(self, "center", _point(self.center, "circle center"))
        object.__setattr__(self, "radius", _positive(self.radius, "circle radius"))
        self._check_common()

    def _geometry(self) -> dict[str, Any]:
        return {"center": list(self.center), "radius": self.radius}


@dataclass(frozen=True)
class Rect(_Shape):
    """A rectangle: ``center`` and ``size`` (width, height) in dva, turned
    ``angle_deg`` counter-clockwise about its centre."""

    center: Point
    size: Point
    angle_deg: float = 0.0
    kind = "rect"

    def __post_init__(self) -> None:
        object.__setattr__(self, "center", _point(self.center, "rect center"))
        width, height = _point(self.size, "rect size")
        object.__setattr__(
            self, "size", (_positive(width, "rect width"), _positive(height, "rect height"))
        )
        object.__setattr__(self, "angle_deg", _finite(self.angle_deg, "rect angle_deg"))
        self._check_common()

    def _geometry(self) -> dict[str, Any]:
        return {"center": list(self.center), "size": list(self.size), "angle_deg": self.angle_deg}


@dataclass(frozen=True)
class Line(_Shape):
    """A segment from ``start`` to ``end`` (dva)."""

    start: Point
    end: Point
    kind = "line"

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _point(self.start, "line start"))
        object.__setattr__(self, "end", _point(self.end, "line end"))
        self._check_common()

    def n_points(self) -> int:
        return 2

    def _geometry(self) -> dict[str, Any]:
        return {"start": list(self.start), "end": list(self.end)}


@dataclass(frozen=True)
class Polygon(_Shape):
    """A closed outline through three or more ``points`` (dva)."""

    points: tuple[Point, ...]
    kind = "polygon"

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", _points(self.points, "polygon points", 3))
        self._check_common()

    def n_points(self) -> int:
        return len(self.points)

    def _geometry(self) -> dict[str, Any]:
        return {"points": [list(p) for p in self.points]}


@dataclass(frozen=True)
class Dots(_Shape):
    """A field of dots of one ``radius`` (dva) at ``points`` (dva)."""

    points: tuple[Point, ...]
    radius: float
    kind = "dots"

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", _points(self.points, "dots points", 1))
        object.__setattr__(self, "radius", _positive(self.radius, "dots radius"))
        self._check_common()

    def n_points(self) -> int:
        return len(self.points)

    def _geometry(self) -> dict[str, Any]:
        return {"points": [list(p) for p in self.points], "radius": self.radius}


@dataclass(frozen=True)
class Label(_Shape):
    """A short ``text`` placed at ``at`` (dva). Annotation by default: a
    label is the operator's, never on the subject's screen."""

    at: Point
    text: str
    role: str = field(default="annotation", kw_only=True)
    kind = "label"

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _point(self.at, "label at"))
        _text(self.text, "label text")
        self._check_common()

    def _geometry(self) -> dict[str, Any]:
        return {"at": list(self.at), "text": self.text}


Shape = Circle | Rect | Line | Polygon | Dots | Label
SHAPE_TYPES: tuple[type, ...] = (Circle, Rect, Line, Polygon, Dots, Label)


def check_shapes(shapes: object, what: str = "schematic") -> tuple[Shape, ...]:
    """``shapes`` as a tuple, every entry one of this module's shapes, and
    within ``MAX_POINTS`` altogether; a ValueError naming ``what`` otherwise.

    Each shape already checked its own fields when it was built; this checks
    what only the whole list can say.
    """
    if isinstance(shapes, (str, bytes)) or not isinstance(shapes, Sequence):
        raise ValueError(f"{what} must be a sequence of shapes, got {type(shapes).__name__}")
    checked = tuple(shapes)
    for index, shape in enumerate(checked):
        if not isinstance(shape, SHAPE_TYPES):
            raise ValueError(
                f"{what}[{index}] is a {type(shape).__name__}; a schematic holds Circle, Rect, "
                f"Line, Polygon, Dots and Label (alhazen.display.schematic)"
            )
    total = sum(shape.n_points() for shape in checked)
    if total > MAX_POINTS:
        raise ValueError(f"{what} has {total} points altogether; at most {MAX_POINTS}")
    return checked
