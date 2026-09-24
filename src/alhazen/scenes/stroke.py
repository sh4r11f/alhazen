"""Stroking a path the way an HTML canvas does, as a coverage mask.

:mod:`alhazen.scenes.render` rasterises shapes by testing points of a
supersampled grid against each shape. For filled shapes that test is one line
of arithmetic; for a stroked path it is most of the work: butt, round and
square caps, miter, round and bevel joins with canvas's miter limit, dash
patterns that run along the whole path, and nonzero winding for filled
outlines. That geometry lives here so a reader of the renderer can skip it.

Everything here is a pure function of the sample grid and the path's points:
it knows nothing about canvases, colours, params or layers. The renderer calls
:func:`stroke_polyline` for lines and polygon outlines and
:func:`point_in_polygon` for polygon interiors.
"""

from __future__ import annotations

import math

import numpy as np


def point_in_polygon(
    grid_x: np.ndarray, grid_y: np.ndarray, points: list[tuple[float, float]]
) -> np.ndarray:
    """NONZERO winding, vectorised over the whole sample grid.

    Canvas's `fill()` defaults to nonzero, not even-odd, and the difference is
    not academic: a self-intersecting outline — a five-pointed star, any
    figure whose edges cross — has a filled centre under nonzero and a hole
    under even-odd. Getting this wrong draws a different shape, not a
    differently-antialiased one.
    """
    winding = np.zeros_like(grid_x, dtype=int)
    for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1], strict=True):
        # Each edge crossing a horizontal ray from the sample counts +1 when
        # it crosses upward and -1 downward; a nonzero total means inside.
        with np.errstate(divide="ignore", invalid="ignore"):
            crossing_x = (x1 - x0) * (grid_y - y0) / (y1 - y0) + x0
        to_the_right = grid_x < crossing_x
        winding += np.where((y0 <= grid_y) & (y1 > grid_y) & to_the_right, 1, 0)
        winding -= np.where((y0 > grid_y) & (y1 <= grid_y) & to_the_right, 1, 0)
    return winding != 0


def stroke_polyline(
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    points: list[tuple[float, float]],
    thickness: float,
    closed: bool = False,
    cap: str = "butt",
    join: str = "miter",
    dash: list[float] | None = None,
) -> np.ndarray:
    """A stroked polyline as a boolean coverage mask, with canvas semantics.

    A capsule per segment — which is what this used to be — is a stroke with
    ROUND caps and ROUND joins. Canvas defaults to butt caps and miter joins,
    so every line ended half a stroke-width long and every corner came out
    blunt. The three pieces here are the segment bodies, the caps at the two
    free ends, and the wedge each interior corner leaves on its outer side.
    """
    half = thickness / 2.0
    covered = np.zeros_like(grid_x, dtype=bool)

    runs = _dash_runs(points, closed, dash or [])
    for run, run_closed in runs:
        segments = list(zip(run, run[1:], strict=False))
        if run_closed and len(run) > 2:
            segments.append((run[-1], run[0]))
        if not segments:
            continue
        for index, ((x0, y0), (x1, y1)) in enumerate(segments):
            body, direction = _segment_body(grid_x, grid_y, x0, y0, x1, y1, half)
            covered |= body
            if direction is None:
                continue
            # Caps go on the two ends the path does not continue through. A
            # closed run has none.
            if not run_closed:
                if index == 0:
                    backwards = (-direction[0], -direction[1])
                    covered |= _cap(grid_x, grid_y, (x0, y0), backwards, half, cap)
                if index == len(segments) - 1:
                    covered |= _cap(grid_x, grid_y, (x1, y1), direction, half, cap)
        covered |= _joins(grid_x, grid_y, segments, half, join, run_closed)
    return covered


def _segment_body(
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    half: float,
) -> tuple[np.ndarray, tuple[float, float] | None]:
    """The rectangle of the stroke along one segment, flush at both ends."""
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length == 0:
        return np.zeros_like(grid_x, dtype=bool), None
    ux, uy = dx / length, dy / length
    along = (grid_x - x0) * ux + (grid_y - y0) * uy
    across = np.abs((grid_x - x0) * -uy + (grid_y - y0) * ux)
    return (along >= 0) & (along <= length) & (across <= half), (ux, uy)


def _cap(
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    point: tuple[float, float],
    direction: tuple[float, float],
    half: float,
    cap: str,
) -> np.ndarray:
    """What a free end adds beyond the segment's flush edge."""
    if cap == "round":
        return np.hypot(grid_x - point[0], grid_y - point[1]) <= half
    if cap == "square":
        ux, uy = direction
        beyond = (grid_x - point[0]) * ux + (grid_y - point[1]) * uy
        across = np.abs((grid_x - point[0]) * -uy + (grid_y - point[1]) * ux)
        return (beyond >= 0) & (beyond <= half) & (across <= half)
    return np.zeros_like(grid_x, dtype=bool)  # butt: nothing beyond the end


# Canvas's own default. Past it a miter would shoot away from the corner, so
# the join falls back to a bevel.
MITER_LIMIT = 10.0


def _joins(
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
    half: float,
    join: str,
    closed: bool,
) -> np.ndarray:
    """Fill the wedge each corner leaves on its outer side."""
    filled = np.zeros_like(grid_x, dtype=bool)
    pairs = list(zip(segments, segments[1:], strict=False))
    if closed and len(segments) > 1:
        pairs.append((segments[-1], segments[0]))
    for (a0, a1), (_b0, b1) in pairs:
        vertex = a1
        if join == "round":
            filled |= np.hypot(grid_x - vertex[0], grid_y - vertex[1]) <= half
            continue
        incoming = _unit(a0, a1)
        outgoing = _unit(vertex, b1)
        if incoming is None or outgoing is None:
            continue
        # Which side is the outside of the turn: the side the cross product
        # points away from.
        cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
        if abs(cross) < 1e-12:
            continue  # straight through: the bodies already meet
        sign = -1.0 if cross > 0 else 1.0
        n_in = (-incoming[1] * sign, incoming[0] * sign)
        n_out = (-outgoing[1] * sign, outgoing[0] * sign)
        corner_in = (vertex[0] + n_in[0] * half, vertex[1] + n_in[1] * half)
        corner_out = (vertex[0] + n_out[0] * half, vertex[1] + n_out[1] * half)
        wedge = [vertex, corner_in, corner_out]
        if join == "miter":
            bisector_x, bisector_y = n_in[0] + n_out[0], n_in[1] + n_out[1]
            norm = math.hypot(bisector_x, bisector_y)
            if norm > 1e-12:
                # Half-angle between the segments; the miter reaches
                # half / sin(theta/2) from the vertex.
                sin_half = norm / 2.0
                if sin_half > 1.0 / MITER_LIMIT:
                    reach = half / sin_half
                    tip = (
                        vertex[0] + bisector_x / norm * reach,
                        vertex[1] + bisector_y / norm * reach,
                    )
                    wedge = [vertex, corner_in, tip, corner_out]
        filled |= point_in_polygon(grid_x, grid_y, wedge)
    return filled


def _unit(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float] | None:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length else None


def _dash_runs(
    points: list[tuple[float, float]], closed: bool, dash: list[float]
) -> list[tuple[list[tuple[float, float]], bool]]:
    """Split a path into the sub-paths a dash pattern leaves drawn.

    The pattern runs along the whole path's arc length, not per segment, which
    is what makes a dash carry across a corner the way a canvas draws it. An
    empty or all-zero pattern is a solid line and returns the path untouched.
    """
    if not dash or all(entry <= 0 for entry in dash) or any(entry < 0 for entry in dash):
        return [(points, closed)]
    pattern = list(dash)
    if len(pattern) % 2:
        # Canvas repeats an odd-length pattern twice, so on and off alternate.
        pattern = pattern + pattern
    path = list(points) + ([points[0]] if closed and len(points) > 2 else [])

    runs: list[tuple[list[tuple[float, float]], bool]] = []
    current: list[tuple[float, float]] = []
    index, remaining, drawing = 0, pattern[0], True
    for start, end in zip(path, path[1:], strict=False):
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        travelled = 0.0
        while travelled < length - 1e-12:
            step = min(remaining, length - travelled)
            head = _lerp(start, end, (travelled) / length)
            tail = _lerp(start, end, (travelled + step) / length)
            if drawing:
                if not current:
                    current = [head]
                current.append(tail)
            travelled += step
            remaining -= step
            if remaining <= 1e-12:
                if drawing and current:
                    runs.append((current, False))
                    current = []
                index = (index + 1) % len(pattern)
                remaining, drawing = pattern[index], not drawing
    if current:
        runs.append((current, False))
    return runs


def _lerp(a: tuple[float, float], b: tuple[float, float], t: float) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _distance_to_segment(
    grid_x: np.ndarray, grid_y: np.ndarray, x0: float, y0: float, x1: float, y1: float
) -> np.ndarray:
    """Distance from each sample to a line segment (not its infinite line)."""
    dx, dy = x1 - x0, y1 - y0
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return np.hypot(grid_x - x0, grid_y - y0)
    t = np.clip(((grid_x - x0) * dx + (grid_y - y0) * dy) / length_squared, 0.0, 1.0)
    return np.hypot(grid_x - (x0 + t * dx), grid_y - (y0 + t * dy))
